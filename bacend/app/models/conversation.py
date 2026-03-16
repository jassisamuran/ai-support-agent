import enum
import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import relationship


class ConversationStatus(str, enum.Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    TAKEN_OVER = "taken_over"  # Human agent took over live


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    channel = Column(String, default="web")  # web | email | slack | whatsapp
    status = Column(Enum(ConversationStatus), default=ConversationStatus.ACTIVE)
    assigned_agent_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    meta = Column(JSONB, default={})
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    organization = relationship("Organization", back_populates="conversations")
    user = relationship("User", foreign_keys=[user_id], back_populates="conversations")
    messages = relationship(
        "Message", back_populates="conversation", order_by="Message.created_at"
    )
    ticket = relationship("Ticket", back_populates="conversation", uselist=False)


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    role = Column(Enum(MessageRole), nullable=False)
    content = Column(Text, nullable=False)
    tool_calls = Column(JSONB, nullable=True)
    tool_results = Column(JSONB, nullable=True)
    tokens_used = Column(Integer, default=0)
    from_cache = Column(Boolean, default=False)
    message_metadata = Column(JSON, nullable=True)
    eval_score = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    conversation = relationship("Conversation", back_populates="messages")
