from typing import List

from app.database import get_db
from app.middleware.auth import require_role
from app.middleware.tenant import get_current_org
from app.models.organization import Organization
from app.models.webhook import WEBHOOK_EVENTS, Webhook
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class WebhookCreate(BaseModel):
    url: str
    events: List[str]


@router.post("/")
async def create_webhook(
    data: WebhookCreate,
    db: AsyncSession = Depends(get_db),
    org: Organization = Depends(get_current_org),
    current_user=Depends(require_role("admin", "owner")),
):
    invalid = [e for e in data.events if e not in WEBHOOK_EVENTS]
    if invalid:
        raise HTTPException(400, f"Invalid events: {invalid}. valid: {WEBHOOK_EVENTS}")

    webhook = Webhook(org_id=org.id, url=data.url, events=data.events)
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)
    return {
        "id": str(webhook.id),
        "url": webhook.url,
        "events": webhook.events,
        "secret": webhook.secret,
    }


@router.get("/")
async def list_webhooks(
    db: AsyncSession = Depends(get_db),
    org: Organization = Depends(get_current_org),
    current_user=Depends(require_role("admin", "owner")),
):
    result = await db.execute(select(Webhook).where(Webhook.org_id == org.id))

    return result.scalars().all()


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    db: AsyncSession = Depends(get_db),
    org: Organization = Depends(get_current_org),
    current_user=Depends(require_role("admin", "owner")),
):
    result = await db.execute(
        select(Webhook).where(Webhook.id == Webhook.org_id, Webhook.org_id == org.id)
    )
    webhook = result.scalar_one_or_none()
    if not Webhook:
        raise HTTPException(404, "Webhook not found")
    await db.delete(webhook)
    await db.commit()
    return {"message": "Deleted"}
