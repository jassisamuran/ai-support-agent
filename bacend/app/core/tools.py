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
            "description": "Search company FAQs, policies, and documentation to answer customer questions. Use for policy questions about returns, shipping, warranty, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'return policy', 'shipping costs', 'warranty')",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "Cancel an order if it hasn't been shipped yet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID to cancel",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for cancellation",
                    },
                },
                "required": ["order_id"],
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
    {
        "type": "function",
        "function": {
            "name": "check_product_stock",
            "description": "Check if a product is in stock and available for purchase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Product ID or SKU",
                    },
                    "size": {
                        "type": "string",
                        "description": "Product size if applicable (S, M, L, XL, etc.)",
                    },
                    "color": {
                        "type": "string",
                        "description": "Product color if applicable",
                    },
                },
                "required": ["product_id"],
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
    """Detect sentiment in customer messages"""

    positive_words = [
        "great",
        "excellent",
        "amazing",
        "love",
        "perfect",
        "happy",
        "satisfied",
        "wonderful",
        "fantastic",
        "good",
        "thanks",
        "appreciate",
    ]

    negative_words = [
        "bad",
        "terrible",
        "worst",
        "angry",
        "damaged",
        "late",
        "complaint",
        "refund",
        "horrible",
        "useless",
        "disappointed",
        "broken",
        "poor",
        "hate",
        "frustrate",
        "upset",
        "scam",
    ]

    message_lower = message.lower()

    positive_count = sum(1 for word in positive_words if word in message_lower)
    negative_count = sum(1 for word in negative_words if word in message_lower)

    if negative_count > positive_count:
        sentiment = "NEGATIVE"
        priority = "HIGH"
    elif positive_count > negative_count:
        sentiment = "POSITIVE"
        priority = "LOW"
    else:
        sentiment = "NEUTRAL"
        priority = "MEDIUM"

    return {
        "sentiment": sentiment,
        "priority": priority,
        "confidence": 0.85,
        "message": message,
        "action": "escalate_to_human" if sentiment == "NEGATIVE" else "continue_bot",
    }


async def cancel_order(order_id: str, reason: str = None, context: dict | None = None):
    "Cancel an order before shipment"
    try:
        token = context.get("auth_token") if context else None

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.BACKEND_API}/api/orders/{order_id}",
                json={"resaon": reason},
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        if response.status_code == 200:
            return {
                "sucess": True,
                "message": f"Order {order_id} has been cancelled successfully.",
            }
        elif response.status_code == 400:
            return {
                "success": False,
                "error": "Order cannot be cancelled (already shipped or delivered)",
            }
        else:
            return {"success": False, "error": "Failed to cancel order"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def check_product_stock(
    product_id: str, size: str = None, color: str = None, context: dict | None = None
) -> dict:
    """Check product stock availability"""
    try:
        token = context.get("auth_token") if context else None

        params = {"product_id": product_id}
        if size:
            params["size"] = size
        if color:
            params["color"] = color

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/products/{product_id}/stock",
                params=params,
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )

        if response.status_code == 200:
            stock = response.json()
            in_stock = stock.get("quantity", 0) > 0

            return {
                "success": True,
                "product_id": product_id,
                "in_stock": in_stock,
                "quantity": stock.get("quantity", 0),
                "size": size,
                "color": color,
                "message": "In stock" if in_stock else "Out of stock",
                "estimated_restock": stock.get("estimated_restock")
                if not in_stock
                else None,
            }
        else:
            return {"success": False, "error": "Product not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def search_knowledge_base(query: str, context: dict | None = None) -> dict:
    """Search knowledge base for FAQs and policies"""

    knowledge_base = {
        "return policy": {
            "title": "Return Policy",
            "content": "We offer 30-day returns for most items in original condition. Electronics have a 15-day return window. Items must be unused and in original packaging. Return shipping is free for defective items but paid for customer preference returns.",
        },
        "shipping": {
            "title": "Shipping Information",
            "content": "Standard shipping (5-7 business days): Free on orders over $50, $5.99 otherwise. Express shipping (2-3 business days): $15.99. Overnight shipping: $29.99. We ship to all 50 US states and select international locations.",
        },
        "warranty": {
            "title": "Warranty Information",
            "content": "All products come with a 1-year manufacturer warranty covering defects in materials and workmanship. This does not cover damage from misuse, accidents, or natural wear. Extended warranties (up to 3 years) are available for select items.",
        },
        "payment methods": {
            "title": "Accepted Payment Methods",
            "content": "We accept: Credit cards (Visa, Mastercard, American Express), PayPal, Apple Pay, Google Pay, and bank transfers. All payments are processed securely with industry-standard encryption (PCI-DSS compliant).",
        },
        "privacy": {
            "title": "Privacy Policy",
            "content": "We protect your personal information and do not sell it to third parties. All data is encrypted and stored securely. You can request your data or have your account deleted anytime by contacting support.",
        },
        "refund": {
            "title": "Refund Policy",
            "content": "Refunds are processed within 5-7 business days after we receive your returned item. The refund amount depends on the condition of the item. Refunded items must be in original condition with all packaging.",
        },
        "international shipping": {
            "title": "International Shipping",
            "content": "We ship to 100+ countries. Shipping costs and delivery times vary by location. International orders may be subject to customs duties and taxes. Duties are the responsibility of the customer.",
        },
    }

    query_lower = query.lower()

    for key, info in knowledge_base.items():
        if key in query_lower:
            return {"success": True, "topic": info["title"], "content": info["content"]}

    return {
        "success": False,
        "message": "Policy information not found. Please contact support for more details.",
    }


TOOL_EXECUTOR = {
    "check_order_status": check_order_status,
    "cancel_order": cancel_order,
    "initiate_refund": initiate_refund,
    "check_product_stock": check_product_stock,
    "search_knowledge_base": search_knowledge_base,
    "create_ticket": create_ticket,
    "sentiment_detection": sentiment_detection,
}
