"""RESTAMP Conversation System Models.

Defines SQLAlchemy ORM models for:
- conversations: canonical property and direct conversations
- messages: persistent message storage
- conversation_read_states: per-participant read tracking
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum, Text, UniqueConstraint, Index, ForeignKeyConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func, text

Base = declarative_base()


class Conversation(Base):
    """Canonical conversation thread.

    Supports two conversation types:
    - PROPERTY: anchored to a property listing; one per (buyer, owner, listing) triple
    - DIRECT: property-independent; one per (participant_a, participant_b) pair

    Unique constraint uk_canonical_conversation ensures one conversation per
    (conversation_type, participant_a_user_id, participant_b_user_id,
    property_listing_id) combination. The application normalises participant
    ordering so that (A, B) and (B, A) map to the same conversation.
    """
    __tablename__ = 'conversations'

    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_type = Column(
        String(20), nullable=False,
        comment='PROPERTY or DIRECT'
    )
    property_listing_id = Column(
        mysql.BIGINT(unsigned=True), nullable=True,
        comment='NULL for direct conversations; links to property_listings for property conversations'
    )
    participant_a_user_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='First participant user ID (normalised order)'
    )
    participant_b_user_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='Second participant user ID (normalised order)'
    )
    created_at = Column(mysql.DATETIME(), nullable=True)
    updated_at = Column(
        mysql.DATETIME(),
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            'conversation_type', 'participant_a_user_id', 'participant_b_user_id',
            'property_listing_id', name='uk_canonical_conversation',
        ),
        Index('ix_conversations_participant_a', 'participant_a_user_id'),
        Index('ix_conversations_participant_b', 'participant_b_user_id'),
        Index('ix_conversations_property', 'property_listing_id'),
    )


class Message(Base):
    """Persistent message storage for both property and direct conversations.

    Each message belongs to a conversation via conversation_id.
    The sender_user_id is the authenticated sender; the client must NOT
    supply sender identity - it is derived server-side from the token.
    """
    __tablename__ = 'messages'

    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='Links to conversations.id'
    )
    sender_user_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='Authenticated sender user ID (never from client)'
    )
    body = Column(mysql.TEXT(), nullable=False)
    message_type = Column(
        String(20), nullable=True,
        comment='TEXT, IMAGE, SYSTEM, etc. Optional'
    )
    created_at = Column(mysql.DATETIME(), nullable=True)
    updated_at = Column(
        mysql.DATETIME(),
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_messages_conversation', 'conversation_id'),
        Index('ix_messages_sender', 'sender_user_id'),
        Index('ix_messages_created', 'created_at'),
    )


class ConversationReadState(Base):
    """Per-participant read state for a conversation.

    One row per (conversation_id, user_id) pair. Tracks the last message
    ID that the user has read. When a new message is sent, the recipient's
    read state is updated only when they explicitly mark messages as read.

    The authenticated user can only update their own read state.
    """
    __tablename__ = 'conversation_read_states'

    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='Links to conversations.id'
    )
    user_id = Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment='The participant whose read state this is'
    )
    last_read_message_id = Column(
        mysql.BIGINT(unsigned=True), nullable=True,
        comment='The last message ID the user has read'
    )
    marked_at = Column(mysql.DATETIME(), nullable=True)

    __table_args__ = (
        UniqueConstraint('conversation_id', 'user_id', name='uk_conversation_read_state'),
        Index('ix_read_states_conversation', 'conversation_id'),
        Index('ix_read_states_user', 'user_id'),
        ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'],
            ondelete='CASCADE',
            name='fk_read_state_conversation'
        ),
        ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            ondelete='CASCADE',
            name='fk_read_state_user'
        ),
    )