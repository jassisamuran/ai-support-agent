import structlog
from app.database import get_db
from app.models.organization import Organization
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


DEFAULT_ORG_SLUG = "ecommerce-support"


DEFAULT_ORG_SLUG = "ecommerce-support"


async def get_current_org(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Organization:

    if x_api_key:
        result = await db.execute(
            select(Organization).where(
                Organization.api_key == x_api_key,
                Organization.is_active == True,
            )
        )
        print("now", x_api_key, result)
        org = result.scalar_one_or_none()

        if org:
            logger.info("Request from org via api key", org=org.slug)
            return org

    # Fallback → default organization

    if x_api_key:
        result = await db.execute(
            select(Organization).where(
                Organization.api_key == x_api_key,
                Organization.is_active == True,
            )
        )
        print("now", x_api_key, result)
        org = result.scalar_one_or_none()

        if org:
            logger.info("Request from org via api key", org=org.slug)
            return org

    # Fallback → default organization
    result = await db.execute(
        select(Organization).where(
            Organization.slug == DEFAULT_ORG_SLUG,
            Organization.is_active == True,
            Organization.slug == DEFAULT_ORG_SLUG,
            Organization.is_active == True,
        )
    )

    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=500,
            detail="Default organization not configured",
        )

    logger.info("Request using default org", org=org.slug)
    return org
