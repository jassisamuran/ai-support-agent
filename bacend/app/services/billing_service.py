import structlog
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.billing import BillingEvent
from app.models.organization import Organization
from sqlalchemy import select

logger = structlog.get_logger()


async def record_usage(
    org_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    conversation_id: str = None,
):
    async with AsyncSessionLocal() as db:
        event = BillingEvent(
            org_id=org_id,
            conversation_id=conversation_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=completion_tokens + prompt_tokens,
            cost_usd=cost_usd,
        )
        db.add(event)

        result = await db.execute(select(Organization).where(Organization.id == org_id))

        org = result.scalar_one_or_none()

        if org:
            org.monthly_input_tokens += prompt_tokens
            org.monthly_output_tokens += completion_tokens
            org.monthly_cost_usd += cost_usd

        await db.commit()


async def check_billing_limit(org: Organization) -> bool:
    total_tokens = org.monthly_output_tokens + org.monthly_input_tokens
    if total_tokens >= org.monthly_token_limit:
        logger.warning(
            "Org over token limit",
            org.slug,
            used=total_tokens,
            limit=org.monthly_token_limit,
        )
        return False
    return True
