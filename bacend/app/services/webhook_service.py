import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import structlog
from app.database import AsyncSessionLocal
from app.models.webhook import Webhook
from sqlalchemy import select

logger = structlog.get_logger()


def _sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


async def fire_event(event_type: str, payload: dict, org_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Webhook).where(Webhook.org_id == org_id, Webhook.is_active == True)
        )

        webhooks = result.scalars().all()

        relevant = [w for w in webhooks if event_type in w.events]
        if not relevant:
            return

        body = json.dumps(
            {
                "event": event_type,
                "timestamps": datetime.now(timezone.utc).isoformat(),
                "data": payload,
            }
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = [
                _deliver(client, webhook, body, event_type) for webhook in relevant
            ]
            await asyncio.gather(*tasks, return_exceptions=True)


async def _deliver(client: httpx.AsyncClient, webhook: Webhook, body: str, event: str):
    signature = _sign_payload(body, Webhook.secret)

    try:
        response = await client.post(
            Webhook.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-256": f"sha256={signature}",
                "X-Event-Type": event,
            },
        )

        logger.info("Webhook devliverd", url=webhook.url, status=response.status_code)

        async with AsyncSessionLocal() as db:
            from sqlalchemy import update

            await db.execute(
                update(Webhook)
                .where(Webhook.id == webhook.id)
                .values(
                    total_deliveries=Webhook.total_deliveries + 1,
                    last_trigged_at=datetime.now(timezone.utc),
                ),
            )

            await db.commit()

    except Exception as e:
        logger.error("Webhook delivery failed", url=webhook.url, error=str(e))
        async with AsyncSessionLocal() as db:
            from sqlalchemy import update

            await db.execute(
                update(Webhook)
                .where(Webhook.id == webhook.id)
                .values(failed_deliveries=Webhook.failed_deliveries + 1)
            )
            await db.commit()
