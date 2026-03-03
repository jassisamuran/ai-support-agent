import structlog
from app.database import get_db
from app.models.organization import Organization
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


async def get_current_org(
    x_api_key: str = Header(..., alias="X-API-Key"), db: AsyncSession = Depends(get_db)
) -> Organization:
    result = await db.execute(
        select(Organization).where(
            Organization.api_key == x_api_key, Organization.is_active
        )
    )

    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(status_code=401, detail="Invalid API key")

    logger.info("Request from org", org=org.slug)
    return org
