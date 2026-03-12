import json

from app.database import AsyncSessionLocal, settings
from app.models.ticket import Ticket, TicketPriority, TicketStatus

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the current status and details of a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order ID"},
                    "customer_email": {
                        "type": "string",
                        "description": "Customer email for verification",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "initiate_refund",
            "description": "Issue a refund for an order. Only use when customer explicitly requests it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in dollars",
                    },
                },
                "required": ["order_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search company FAQs, policies, and docs to answer cutomer questions. Always call this first for policy questions.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


import httpx


async def check_order_status(
    order_id: str, customer_email: str = None, context: dict | None = None
) -> dict:
    try:
        token = context.get("auth_token") if context else None
        print("now is", token)
        token = context.get("auth_token") if context else None
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/orders/{order_id}/status",
                headers={"Authorization": token} if token else None,
            )
        if response.status_code == 200:
            data = response.json()
            print(data)
        else:
            print("API error:", response.status_code)
        if response.status_code != 200:
            return {"success": False, "error": f"Order {order_id} not found"}

        order = response.json()

        return {"success": True, "order": order}

    except Exception as e:
        return {"success": False, "error": str(e)}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def create_ticket(
    title: str,
    description: str,
    priority: str,
    user_id: str = None,
    conversation_id: str = None,
) -> dict:
    async with AsyncSessionLocal() as db:
        ticket = Ticket(
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            description=description,
            priority=TicketPriority(priority),
        )

        db.add(ticket)
        await db.commit()
        await db.refresh(ticket)
    return {"ticket_id": str(ticket.id), "message": f"Ticket created: {title}"}


async def initiate_refund(
    order_id: str, reason: str, amount: float = None, context: dict | None = None
) -> dict:

    return {
        "success": True,
        "refund_id": f"REF-{order_id}-001",
        "amount": amount or "full refund",
        "eta": "5-7 buisness days",
        "message": f"Refund initiated for order {order_id}.",
    }


TOOL_EXECUTOR = {
    "check_order_status": check_order_status,
    "initiate_refund": initiate_refund,
    "create_ticket": create_ticket,
}
