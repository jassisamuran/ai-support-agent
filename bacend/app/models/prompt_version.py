import uuid
from datetime import datetime, timezone

from app.database import Base
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))

    name = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)

    is_active = Column(Boolean, default=False)

    traffic_percent = Column(Float, default=100.0)

    avg_accuracy = Column(Float, default=0.0)
    avg_helpfulness = Column(Float, default=0.0)

    total_uses = Column(Integer, default=0)

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    organization = relationship("Organization", back_populates="prompt_versions")
