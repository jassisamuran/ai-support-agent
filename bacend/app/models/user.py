import enum
import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship


class UserRole(str, enum.Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"
    OWNER = "owner"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    email = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    hashed_pw = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.CUSTOMER)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    organization = relationship("Organization", back_populates="users")
    conversations = relationship(
        "Conversation", back_populates="user", foreign_keys="Conversation.user_id"
    )
    tickets = relationship(
        "Ticket", back_populates="user", foreign_keys="Ticket.user_id"
    )
    # Email unique per org (not globally)
    __table_args__ = ({"schema": None},)
