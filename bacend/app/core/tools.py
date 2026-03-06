import json

from app.database import AsyncSessionLocal
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


async def check_order_status(order_id: str, customer_email: str = None) -> dict:
    mock = {
        "ORD-001": {
            "status": "shipped",
            "tracking": "TRK123456",
            "eta": "2024-03-20",
            "total": 99.99,
        },
        "ORD-002": {"status": "processing", "eta": "2024-03-25", "total": 49.99},
        "ORD-003": {
            "status": "delivered",
            "delivered_at": "2024-03-10",
            "total": 199.99,
        },
    }

    if order_id in mock:
        return {"success": True, "order": mock[order_id]}

    return {"success": False, "error": f"Order {order_id} not found"}


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


async def initiate_refund(order_id: str, reason: str, amount: float = None) -> dict:

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
