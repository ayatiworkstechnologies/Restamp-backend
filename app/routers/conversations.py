"""RESTAMP Conversation System API Endpoints.

Implements the messaging and conversation system API:

- POST /conversations           - Create or retrieve a canonical conversation
- GET /conversations            - List conversations for the authenticated user
- GET /conversations/{id}       - Retrieve conversation details
- GET /conversations/{id}/messages - Retrieve paginated messages
- POST /conversations/{id}/messages - Send a message
- POST /conversations/{id}/read   - Mark conversation as read
- GET /conversations/unread-count - Get unread message count

Authentication: All endpoints require a valid authenticated token.
Authorization: Only conversation participants can access the conversation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user_id
from ..schemas import (
    ConversationOut,
    ConversationsResponse,
    MessageOut,
    MessagesResponse,
    EnquiryMessageOut,
)
from ..services.conversations import (
    create_conversation,
    get_conversation,
    send_message,
    get_conversation_messages,
    mark_conversation_read,
    mark_message_read,
    get_unread_count,
    get_conversation_unread_count,
)
from ..models import User


router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post(
    "/",
    response_model=ConversationOut,
    summary="Create or retrieve a canonical conversation",
)
def api_create_conversation(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    other_user_id: int,
    conversation_type: str,
    property_listing_id: int | None = None,
) -> dict:
    """Create or retrieve a canonical conversation.

    - **conversation_type**: 'PROPERTY' or 'DIRECT'
    - **other_user_id**: The other participant's user ID
    - **property_listing_id**: Required for PROPERTY type; NULL for DIRECT

    Returns the existing conversation if one already exists for this participant
    pair and type, ensuring idempotent creation.
    """
    conversation = create_conversation(
        db=db,
        user_id_1=current_user_id,
        user_id_2=other_user_id,
        conversation_type=conversation_type,
        property_listing_id=property_listing_id,
    )
    return {
        "id": conversation.id,
        "conversation_type": conversation.conversation_type,
        "property_listing_id": conversation.property_listing_id,
        "participant_a_user_id": conversation.participant_a_user_id,
        "participant_b_user_id": conversation.participant_b_user_id,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


@router.get(
    "",
    response_model=ConversationsResponse,
    summary="List conversations for the authenticated user",
)
def api_list_conversations(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """List conversations for the authenticated user, ordered by latest activity.

    Returns conversations with preview information including:
    - Latest message preview
    - Latest message timestamp
    - Unread count
    - Other participant information
    - Property information (for PROPERTY conversations)
    """
    # Get all conversations this user participates in
    conversations = (
        db.query(Conversation)
        .filter(
            (Conversation.participant_a_user_id == current_user_id)
            | (Conversation.participant_b_user_id == current_user_id)
        )
        .all()
    )

    # Build response items
    items = []
    for conv in conversations:
        # Determine the other participant
        if conv.participant_a_user_id == current_user_id:
            other_user_id = conv.participant_b_user_id
        else:
            other_user_id = conv.participant_a_user_id

        # Get the other user's basic info (limited to what's safe to share)
        from ..models import User as _User
        other_user = db.get(_User, other_user_id)

        # Get unread count for this conversation
        unread_count = get_conversation_unread_count(
            db=db,
            conversation_id=conv.id,
            user_id=current_user_id,
        )

        # Get latest message
        latest_message = None
        from ..services.conversations import get_conversation_messages
        msg_data = get_conversation_messages(
            db=db,
            conversation_id=conv.id,
            current_user_id=current_user_id,
            page=1,
            page_size=1,
        )
        if msg_data["items"]:
            latest_message = msg_data["items"][0]

        # Get property info if property conversation
        property_info = None
        if conv.conversation_type == "PROPERTY" and conv.property_listing_id:
            from ..services import listings as listing_svc
            from ..models import PropertyListing
            listing = (
                db.query(PropertyListing)
                .filter(PropertyListing.id == conv.property_listing_id)
                .first()
            )
            if listing:
                property_info = {
                    "listing_id": listing.id,
                    "title": listing.title,
                    "property_type": listing.property_type,
                    "price_display": listing.price_display,
                }

        items.append(
            {
                "id": conv.id,
                "conversation_type": conv.conversation_type,
                "other_user_id": other_user_id,
                "other_user_display_name": other_user.display_name if other_user else None,
                "property_listing_id": conv.property_listing_id,
                "property_info": property_info,
                "latest_message": latest_message,
                "latest_message_at": latest_message.get("created_at") if latest_message else None,
                "unread_count": unread_count,
            }
        )

    # Sort by latest activity (message timestamp)
    # Simple sort - in production would be more efficient with DB-level ordering
    items.sort(key=lambda x: x.get("latest_message_at") or "", reverse=True)

    # Pagination
    total = len(items)
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    total_pages = (total + page_size - 1) // page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_items = items[start:end]

    return {
        "items": page_items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get(
    "/{conversation_id}",
    response_model=ConversationOut,
    summary="Retrieve conversation details",
)
def api_get_conversation(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    conversation_id: int,
) -> dict:
    """Retrieve conversation details and participant information.

    Only participants can access a conversation. Returns 404 if the user
    is not a participant (avoids leaking conversation existence).
    """
    conversation = get_conversation(db=db, conversation_id=conversation_id, current_user_id=current_user_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or you are not a participant",
        )

    # Determine the other participant
    if conversation.participant_a_user_id == current_user_id:
        other_user_id = conversation.participant_b_user_id
    else:
        other_user_id = conversation.participant_a_user_id

    from ..models import User as _User
    other_user = db.get(_User, other_user_id)

    return {
        "id": conversation.id,
        "conversation_type": conversation.conversation_type,
        "property_listing_id": conversation.property_listing_id,
        "participant_a_user_id": conversation.participant_a_user_id,
        "participant_b_user_id": conversation.participant_b_user_id,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "other_user_id": other_user_id,
        "other_user_display_name": other_user.display_name if other_user else None,
    }


@router.get(
    "/{conversation_id}/messages",
    response_model=MessagesResponse,
    summary="Retrieve paginated messages for a conversation",
)
def api_get_conversation_messages(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    conversation_id: int,
    page: int = 1,
    page_size: int = 50,
    before_message_id: int | None = None,
) -> dict:
    """Retrieve paginated messages for a conversation.

    - **page**: Page number (1-indexed)
    - **page_size**: Number of messages per page (max 100)
    - **before_message_id**: Cursor for paginating to older messages;
      retrieve messages older than this message ID

    Only participants can read messages from a conversation.
    """
    result = get_conversation_messages(
        db=db,
        conversation_id=conversation_id,
        current_user_id=current_user_id,
        page=page,
        page_size=page_size,
        before_message_id=before_message_id,
    )
    return result


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    summary="Send a message to a conversation",
)
def api_send_message(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    conversation_id: int,
    body: str,
    message_type: str | None = None,
) -> dict:
    """Send a message to a conversation.

    - The sender identity is derived server-side from the authenticated token;
      the client must NOT provide a sender_user_id.
    - Message body must be 1-5000 characters.
    - Double-tap protection: identical messages within 60 seconds are treated
      as accidental double-taps and not stored twice.
    - Rate limiting applies per conversation per sender.

    Returns the created message.
    """
    message = send_message(
        db=db,
        conversation_id=conversation_id,
        sender_user_id=current_user_id,
        body=body,
        message_type=message_type,
    )
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_user_id": message.sender_user_id,
        "body": message.body,
        "message_type": message.message_type,
        "created_at": message.created_at,
    }


@router.post(
    "/{conversation_id}/read",
    summary="Mark conversation as read",
)
def api_mark_conversation_read(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    conversation_id: int,
) -> dict:
    """Mark a conversation as read for the authenticated user.

    Only the authenticated recipient's read state is updated.
    Reading one conversation does not affect another conversation's unread state.

    Returns the updated read state.
    """
    read_state = mark_conversation_read(
        db=db,
        conversation_id=conversation_id,
        user_id=current_user_id,
    )
    return {
        "conversation_id": read_state.conversation_id,
        "user_id": read_state.user_id,
        "last_read_message_id": read_state.last_read_message_id,
        "marked_at": read_state.marked_at,
    }


@router.post(
    "/{conversation_id}/messages/read",
    summary="Mark a specific message as read",
)
def api_mark_message_read(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    conversation_id: int,
    message_id: int,
) -> dict:
    """Mark a specific message as read, updating the user's read state.

    The user's last_read_message_id is updated to message_id if it is
    newer than the current value.
    """
    read_state = mark_message_read(
        db=db,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=current_user_id,
    )
    return {
        "conversation_id": read_state.conversation_id,
        "user_id": read_state.user_id,
        "last_read_message_id": read_state.last_read_message_id,
        "marked_at": read_state.marked_at,
    }


@router.get(
    "/unread-count",
    summary="Get unread message count for the authenticated user",
)
def api_get_unread_count(
    *,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
) -> dict:
    """Get the total unread message count across all conversations for the
    authenticated user.

    Counts messages sent after the user's last_read_message_id for each
    conversation they participate in.
    """
    unread_count = get_unread_count(db=db, user_id=current_user_id)
    return {"unread_count": unread_count}