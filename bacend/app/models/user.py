import enum
import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import Boolean, Column, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship


class UserRole(str, enum.Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)
    hashed_pw = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.CUSTOMER)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    tickets = relationship(
        "Ticket", back_populates="user", foreign_keys="Ticket.user_id"
    )
    conversations = relationship("Conversation", back_populates="user")
