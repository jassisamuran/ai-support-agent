import secrets
import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

WEBHOOK_EVENTS = [
    "conversation.started",
    "conversation.resolved",
    "ticket.created",
    "ticket.escalated",
    "ticket.resolved",
    "refund.initiated",
    "agent.takeover",
]


class Webhook(Base):
    __tablename__ = "webhooks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    url = Column(String, nullable=False)
    secret = Column(String, default=lambda: secrets.token_hex(32))
    events = Column(JSONB, default=[])
    is_active = Column(Boolean, default=True)

    total_deliveries = Column(Integer, default=0)
    failed_deliveries = Column(Integer, default=0)
    last_trigged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    organization = relationship("Organization", back_populates="webhooks")
