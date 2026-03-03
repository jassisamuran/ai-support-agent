import enum
import secrets
import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import Boolean, Column, DateTime, Enum, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship


class PlanType(str, enum.Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    api_key = Column(
        String, unique=True, default=lambda: f"sk-{secrets.token_urlsafe(32)}"
    )
    plan = Column(Enum(PlanType), default=PlanType.FREE)

    # Per-org AI config
    system_prompt = Column(Text, nullable=True)
    company_name = Column(String, default="Our Company")
    chroma_collection = Column(String)  # Isolated knowledge base per org
    active_prompt_id = Column(UUID(as_uuid=True), nullable=True)

    # Usage & billing
    monthly_input_tokens = Column(Integer, default=0)
    monthly_output_tokens = Column(Integer, default=0)
    monthly_cost_usd = Column(Float, default=0.0)
    monthly_token_limit = Column(Integer, default=100_000)
    billing_reset_at = Column(DateTime(timezone=True))

    # Limits
    rate_limit_per_minute = Column(Integer, default=20)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    meta = Column(JSONB, default={})

    users = relationship("User", back_populates="organization")
    conversations = relationship("Conversation", back_populates="organization")
