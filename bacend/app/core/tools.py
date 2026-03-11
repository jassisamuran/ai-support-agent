import httpx
from app.database import AsyncSessionLocal, settings
from app.models.ticket import Ticket, TicketPriority
from sqlalchemy import select

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
    {
        "type": "function",
        "function": {
            "name": "sentiment_detection",
            "description": "Detect sentiment of customer message. Use when customer is complaining or angry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Customer message"}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create a support ticket when issue cannot be solved automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                },
                "required": ["title", "description", "priority"],
            },
        },
    },
]


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
    org_id: str = None,
) -> dict:

    async with AsyncSessionLocal() as db:
        if conversation_id:
            result = await db.execute(
                select(Ticket).where(Ticket.conversation_id == conversation_id)
            )
            existing_ticket = result.scalar_one_or_none()

            if existing_ticket:
                return {
                    "ticket_id": str(existing_ticket.id),
                    "message": "A support ticket already exists for this issue.",
                }

        ticket = Ticket(
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            description=description,
            priority=TicketPriority(priority.lower()),
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


async def sentiment_detection(message: str, context: dict | None = None) -> dict:

    negative_words = [
        "bad",
        "terrible",
        "worst",
        "angry",
        "damaged",
        "late",
        "complaint",
        "refund",
    ]

    sentiment = "POSITIVE"

    for word in negative_words:
        if word in message.lower():
            sentiment = "NEGATIVE"

    return {"sentiment": sentiment, "message": message}


TOOL_EXECUTOR = {
    "check_order_status": check_order_status,
    "initiate_refund": initiate_refund,
    "create_ticket": create_ticket,
    "sentiment_detection": sentiment_detection,
}
