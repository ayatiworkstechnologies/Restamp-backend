"""RESTAMP Conversation System Services.

Handles canonical conversation creation, message sending,
read/unread state management, and inbox/pagination functionality.

Key design decisions:
- Reuses existing enquiries + enquiry_messages for property-anchored conversations
- New conversations + messages tables for direct (non-property) conversations
- ConversationReadState tracks per-participant read status
- All sender identity is server-derived from authenticated token, never client-provided
- Unique constraints prevent duplicate conversations and messages
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Conversation,
    Message,
    ConversationReadState,
    Enquiry,
    EnquiryMessage,
    ActiveEnquiry,
    User,
    PropertyListing,
)
from ..services import notifications as notification_svc
from ..services import enquiries as enquiry_svc
from . import notifications as notif_svc


# Conversation type constants
CONVERSATION_TYPE_PROPERTY = "PROPERTY"
CONVERSATION_TYPE_DIRECT = "DIRECT"

# Double-tap protection window (seconds)
_DOUBLE_TAP_SECONDS = 60

# Message rate limit per thread per window
_MESSAGE_RATE_LIMIT = 30
_MESSAGE_RATE_WINDOW_SECONDS = 3600


def _utcnow_naive_seconds() -> datetime:
    """Naive UTC, whole seconds: the DATETIME columns keep no fractions, so
    the value returned at creation is exactly the value every later read
    returns."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _created_at_out(value: datetime | None) -> datetime | None:
    """Stored naive UTC -> explicit UTC for the API (serialized with "Z", so
    clients never mistake it for local time). None stays None: rows created
    before a timestamp existed have no trustworthy time."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _normalize_participant_order(
    user_id_1: int, user_id_2: int
) -> tuple[int, int]:
    """Normalise participant ordering so that (A, B) and (B, A) are equivalent.

    Returns (participant_a, participant_b) where a < b (by user ID).
    This ensures canonical conversation lookup regardless of participant order.
    """
    return (user_id_1, user_id_2) if user_id_1 < user_id_2 else (user_id_2, user_id_1)


def _validate_message_body(body: str) -> str:
    """Validate message content."""
    trimmed = body.strip()
    if not trimmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message body cannot be empty",
        )
    if len(trimmed) > 5000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message body cannot exceed 5000 characters",
        )
    return trimmed


def create_conversation(
    db: Session,
    *,
    user_id_1: int,
    user_id_2: int,
    conversation_type: str,
    property_listing_id: int | None = None,
) -> Conversation:
    """Create or retrieve a canonical conversation.

    - For PROPERTY type: requires property_listing_id. Uses unique constraint
      to ensure one conversation per (type, participant_a, participant_b, listing).
    - For DIRECT type: property_listing_id must be None. Uses unique constraint
      to ensure one conversation per (type, participant_a, participant_b).

    The participant order is normalised internally; the caller can pass users
    in any order.

    Raises 409 if a canonical conversation already exists (idempotent re-creation).
    """
    # Normalise participant ordering
    participant_a, participant_b = _normalize_participant_order(user_id_1, user_id_2)

    # Validate conversation type
    if conversation_type not in (CONVERSATION_TYPE_PROPERTY, CONVERSATION_TYPE_DIRECT):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid conversation_type: {conversation_type}. Must be 'PROPERTY' or 'DIRECT'",
        )

    # For PROPERTY type, validate property listing exists and is accessible
    if conversation_type == CONVERSATION_TYPE_PROPERTY:
        if property_listing_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="property_listing_id is required for PROPERTY conversations",
            )
        listing = (
            db.query(PropertyListing)
            .filter(
                PropertyListing.id == property_listing_id,
                PropertyListing.verification_status == "VERIFIED",
                PropertyListing.listing_status == "AVAILABLE",
                PropertyListing.deleted_at.is_(None),
            )
            .first()
        )
        if listing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Property listing not found or not accessible",
            )

    # Try to create the conversation; IntegrityError means it already exists
    try:
        conversation = Conversation(
            conversation_type=conversation_type,
            property_listing_id=property_listing_id,
            participant_a_user_id=participant_a,
            participant_b_user_id=participant_b,
            created_at=_utcnow_naive_seconds(),
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation
    except IntegrityError:
        db.rollback()
        # Conversation already exists - retrieve and return it
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.conversation_type == conversation_type,
                Conversation.participant_a_user_id == participant_a,
                Conversation.participant_b_user_id == participant_b,
                Conversation.property_listing_id == property_listing_id,
            )
            .first()
        )
        if conversation is None:
            # Try reversed participant order (shouldn't happen due to normalisation,
            # but be safe)
            conversation = (
                db.query(Conversation)
                .filter(
                    Conversation.conversation_type == conversation_type,
                    Conversation.participant_a_user_id == participant_b,
                    Conversation.participant_b_user_id == participant_a,
                    Conversation.property_listing_id == property_listing_id,
                )
                .first()
            )
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve existing conversation",
            )
        return conversation


def get_conversation(
    db: Session,
    conversation_id: int,
    current_user_id: int,
) -> Conversation | None:
    """Retrieve a conversation if the current user is a participant.

    Returns None if the user is not a participant (404, not 403, to avoid
    leaking conversation existence).
    """
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
        )
        .first()
    )
    if conversation is None:
        return None

    # Check participant membership
    is_participant = (
        conversation.participant_a_user_id == current_user_id
        or conversation.participant_b_user_id == current_user_id
    )
    # For property conversations, also verify the property is accessible
    if conversation.conversation_type == "PROPERTY" and conversation.property_listing_id:
        from ..services import listings as listing_svc
        try:
            listing_svc._visible_listing(
                db, conversation.property_listing_id
            )
        except HTTPException:
            return None  # Property not accessible

    if not is_participant:
        return None

    return conversation


def send_message(
    db: Session,
    *,
    conversation_id: int,
    sender_user_id: int,
    body: str,
    message_type: str | None = None,
) -> Message:
    """Send a message to a conversation.

    - Validates the sender is a participant in the conversation
    - Validates message body
    - Idempotent: if same body is sent twice within _DOUBLE_TAP_SECONDS,
      the second is treated as a double-tap and not stored duplicate
    - Rate limits: max _MESSAGE_RATE_LIMIT messages per thread per
      _MESSAGE_RATE_WINDOW_SECONDS
    - Persists message before triggering notifications

    Returns the created message.
    """
    # Retrieve conversation and verify participation
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    # Check participant membership
    is_sender = conversation.participant_a_user_id == sender_user_id or \
                conversation.participant_b_user_id == sender_user_id
    if not is_sender:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a participant in this conversation",
        )

    # Validate message body
    validated_body = _validate_message_body(body)

    # Double-tap protection: check if the same message was just sent
    last_similar = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.sender_user_id == sender_user_id,
            Message.body == validated_body,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_similar and (
        _utcnow_naive_seconds().timestamp()
        - last_similar.created_at.timestamp()
    ) < _DOUBLE_TAP_SECONDS:
        # Treat as double-tap; do not store duplicate
        # Still return the existing message for acknowledgment
        return last_similar

    # Rate limiting: count messages in the window for this conversation + sender
    message_count = (
        db.query(Message.id)
        .filter(
            Message.conversation_id == conversation_id,
            Message.sender_user_id == sender_user_id,
            Message.created_at >= _utcnow_naive_seconds()
            - timedelta(seconds=_MESSAGE_RATE_WINDOW_SECONDS),
        )
        .count()
    )
    if message_count >= _MESSAGE_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Message rate limit exceeded: {_MESSAGE_RATE_LIMIT} messages per "
            f"{_MESSAGE_RATE_WINDOW_SECONDS // 60} minutes",
        )

    # Persist the message
    message = Message(
        conversation_id=conversation_id,
        sender_user_id=sender_user_id,
        body=validated_body,
        message_type=message_type,
        created_at=_utcnow_naive_seconds(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    # Trigger notification for the recipient (not the sender)
    try:
        recipient_user_id = (
            conversation.participant_b_user_id
            if conversation.participant_a_user_id == sender_user_id
            else conversation.participant_a_user_id
        )
        if recipient_user_id != sender_user_id:
            notif_svc.notify_new_message(
                db=db,
                conversation_id=conversation_id,
                message_id=message.id,
                recipient_user_id=recipient_user_id,
                sender_user_id=sender_user_id,
            )
    except Exception:
        # Notification failure should not break the message persistence
        pass

    return message


def get_conversation_messages(
    db: Session,
    conversation_id: int,
    current_user_id: int,
    page: int = 1,
    page_size: int = 50,
    before_message_id: int | None = None,
) -> dict:
    """Retrieve paginated messages for a conversation.

    Messages are ordered by created_at DESC (newest first).
    Supports cursor-based pagination via before_message_id.

    Returns a dict with:
    - items: list of message summaries
    - page: current page
    - page_size: page size
    - total: total message count (approximate)
    - has_more: whether there are more pages
    """
    # Verify participation
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    is_participant = (
        conversation.participant_a_user_id == current_user_id
        or conversation.participant_b_user_id == current_user_id
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a participant in this conversation",
        )

    # Build query
    query = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
    )

    # Apply cursor if provided (messages older than this ID)
    if before_message_id is not None:
        query = query.filter(Message.id < before_message_id)

    total = query.count()
    offset = (page - 1) * page_size
    messages = query.offset(offset).limit(page_size).all()

    # Format response
    item_list = []
    for msg in messages:
        item_list.append(
            {
                "id": msg.id,
                "sender_user_id": msg.sender_user_id,  # Derived server-side
                "body": msg.body,
                "message_type": msg.message_type,
                "created_at": _created_at_out(msg.created_at),
            }
        )

    has_more = (offset + len(messages)) < total

    return {
        "items": item_list,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": has_more,
    }


def mark_conversation_read(
    db: Session,
    *,
    conversation_id: int,
    user_id: int,
) -> ConversationReadState:
    """Mark a conversation as read for a specific participant.

    Only the authenticated recipient's read state can be updated.
    Reading one conversation does not affect another conversation's state.

    Returns the updated read state row.
    """
    # Verify participation
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    is_participant = (
        conversation.participant_a_user_id == user_id
        or conversation.participant_b_user_id == user_id
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a participant in this conversation",
        )

    # Upsert the read state
    read_state = (
        db.query(ConversationReadState)
        .filter(
            ConversationReadState.conversation_id == conversation_id,
            ConversationReadState.user_id == user_id,
        )
        .first()
    )

    if read_state is None:
        read_state = ConversationReadState(
            conversation_id=conversation_id,
            user_id=user_id,
            last_read_message_id=None,
            marked_at=_utcnow_naive_seconds(),
        )
        db.add(read_state)
    else:
        read_state.marked_at = _utcnow_naive_seconds()

    db.commit()
    db.refresh(read_state)
    return read_state


def mark_message_read(
    db: Session,
    *,
    conversation_id: int,
    message_id: int,
    user_id: int,
) -> ConversationReadState:
    """Mark a specific message as read, updating the user's read state.

    The user's last_read_message_id is updated to message_id if it is
    newer than the current value.
    """
    # Verify participation
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    is_participant = (
        conversation.participant_a_user_id == user_id
        or conversation.participant_b_user_id == user_id
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a participant in this conversation",
        )

    # Verify the message belongs to this conversation
    message = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conversation_id)
        .first()
    )
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found in this conversation",
        )

    # Update read state
    read_state = (
        db.query(ConversationReadState)
        .filter(
            ConversationReadState.conversation_id == conversation_id,
            ConversationReadState.user_id == user_id,
        )
        .first()
    )

    if read_state is None:
        read_state = ConversationReadState(
            conversation_id=conversation_id,
            user_id=user_id,
            last_read_message_id=message_id,
            marked_at=_utcnow_naive_seconds(),
        )
        db.add(read_state)
    else:
        # Update last_read_message_id to the newer of the two
        current_last = read_state.last_read_message_id
        if current_last is None or message_id > current_last:
            read_state.last_read_message_id = message_id
        read_state.marked_at = _utcnow_naive_seconds()

    db.commit()
    db.refresh(read_state)
    return read_state


def get_unread_count(
    db: Session,
    *,
    user_id: int,
) -> int:
    """Calculate the unread message count for a user across all their conversations.

    Counts messages in each conversation that were sent after the user's
    last_read_message_id for that conversation.

    Returns the total unread count across all conversations.
    """
    # Get all conversations this user participates in
    conversations = (
        db.query(Conversation)
        .filter(
            (Conversation.participant_a_user_id == user_id)
            | (Conversation.participant_b_user_id == user_id)
        )
        .all()
    )

    total_unread = 0

    for conv in conversations:
        # Get the user's read state for this conversation
        read_state = (
            db.query(ConversationReadState)
            .filter(
                ConversationReadState.conversation_id == conv.id,
                ConversationReadState.user_id == user_id,
            )
            .first()
        )

        # If no read state, all messages are unread
        if read_state is None or read_state.last_read_message_id is None:
            count = (
                db.query(Message)
                .filter(Message.conversation_id == conv.id)
                .count()
            )
            total_unread += count
        else:
            # Count messages after the last read message
            unread_count = (
                db.query(Message)
                .filter(
                    Message.conversation_id == conv.id,
                    Message.id > read_state.last_read_message_id,
                )
                .count()
            )
            total_unread += unread_count

    return total_unread


def get_conversation_unread_count(
    db: Session,
    *,
    conversation_id: int,
    user_id: int,
) -> int:
    """Calculate unread count for a specific conversation.

    Returns the number of messages sent after the user's last_read_message_id
    for this conversation.
    """
    # Verify participation
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    is_participant = (
        conversation.participant_a_user_id == user_id
        or conversation.participant_b_user_id == user_id
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a participant in this conversation",
        )

    # Get the user's read state
    read_state = (
        db.query(ConversationReadState)
        .filter(
            ConversationReadState.conversation_id == conversation_id,
            ConversationReadState.user_id == user_id,
        )
        .first()
    )

    if read_state is None or read_state.last_read_message_id is None:
        count = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .count()
        )
        return count

    # Count messages after the last read message
    unread_count = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.id > read_state.last_read_message_id,
        )
        .count()
    )
    return unread_count