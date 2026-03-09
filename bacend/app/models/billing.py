import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"))
    conversation_id = Column(UUID(as_uuid=True), nullable=True)
    trace_id = Column(UUID(as_uuid=True), nullable=True)

    model = Column(String)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
