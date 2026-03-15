import asyncio
import json

import httpx
from sqlalchemy import select

from app.core.pagination_cache import (
    CACHE_TTL,
    PAGE_SIZE,
    NavigationIntent,
    OrderSnapshot,
    PaginationState,
    apply_navigation,
    clear_state,
    get_state,
    parse_navigation_intent,
    set_state,
)
from app.database import AsyncSessionLocal, settings
from app.models.ticket import Ticket, TicketPriority

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
            "name": "summarise_orders",
            "description": (
                "Return a summary of customer orders for a time range. "
                "Use when the user asks for totals or analytics instead of individual orders. "
                "Examples: "
                "'summary of last 3 days orders', "
                "'how much did I spend last 4 days', "
                "'total orders this week', "
                "'order summary for last 10 days'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": (
                            "Number of past days to include in the summary. "
                            "Examples: "
                            "'last 3 days' → days=3, "
                            "'last 4 days' → days=4, "
                            "'last 2 weeks' → days=14"
                        ),
                        "minimum": 1,
                        "maximum": 3650,
                    }
                },
                "required": ["days"],
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
    {
        "type": "function",
        "function": {
            "name": "list_orders",
            "description": (
                "Fetch the customer's order history for a date range. "
                "Always call this FIRST when the user asks 'show my orders', "
                "'what did I order last month', or any multi-order query. "
                "Results are paginated — call navigate_orders for next/previous pages. "
                "DO NOT call this again when the user says next/previous — use navigate_orders instead. "
                "For periods not in the enum (e.g. 'last 5 months'), use custom_days instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": [
                            "last_week",
                            "last_month",
                            "last_3_months",
                            "last_6_months",
                            "last_year",
                            "all_time",
                        ],
                        "description": (
                            "Time period to fetch orders for. "
                            "Use 'last_6_months' when user says 'last 4 months', 'last 5 months', "
                            "'last 6 months' — it is the closest standard window. "
                            "For any other custom range, use custom_days instead."
                        ),
                    },
                    "custom_days": {
                        "type": "integer",
                        "description": (
                            "Fetch orders from the last N days. "
                            "Use this when the user asks for a period not in the enum — "
                            "e.g. 'last 5 months' = 150, 'last 10 weeks' = 70, 'last 2 months' = 60. "
                            "When custom_days is set, period is ignored. Max: 730 (2 years)."
                        ),
                        "minimum": 1,
                        "maximum": 730,
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": [
                            "all",
                            "pending",
                            "processing",
                            "shipped",
                            "delivered",
                            "cancelled",
                            "refunded",
                        ],
                        "description": "Filter orders by status. Default: all",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max orders to fetch in one API call (default 50, max 100). "
                            "The chatbot paginates locally — do not set this to 5."
                        ),
                        "default": 50,
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["date_desc", "date_asc", "amount_desc", "amount_asc"],
                        "description": "Sort order. Default: date_desc (newest first)",
                        "default": "date_desc",
                    },
                },
                "required": ["period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_orders",
            "description": (
                "Navigate between pages of orders already fetched by list_orders. "
                "Use when the user says 'next', 'previous', 'prev', 'go to page 3', etc. "
                "This does NOT call the API again — it reads from cache. "
                "If no order list is cached yet, call list_orders first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": [
                            "next",
                            "previous",
                            "first",
                            "last",
                            "specific",
                            "refresh",
                        ],
                        "description": "Navigation direction",
                    },
                    "page_number": {
                        "type": "integer",
                        "description": "Required only when direction='specific'. The page number to jump to.",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tickets",
            "description": (
                "Fetch the customer's support tickets. "
                "Use when user asks 'show my tickets', 'what tickets do I have'. "
                "Results are paginated — use navigate_tickets for next/previous."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "open", "in_progress", "resolved", "closed"],
                        "default": "all",
                    },
                    "limit": {"type": "integer", "default": 50, "maximum": 100},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_tickets",
            "description": (
                "Navigate between pages of tickets fetched by list_tickets. "
                "Use when user says next/previous in the context of tickets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": [
                            "next",
                            "previous",
                            "first",
                            "last",
                            "specific",
                            "refresh",
                        ],
                    },
                    "page_number": {
                        "type": "integer",
                        "description": "Required only when direction='specific'.",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_updates",
            "description": (
                "Check if any order currently on the displayed page has changed status "
                "since it was last shown. Call this when user says 'any updates?', "
                "'refresh', or when the background watcher reports a change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check_all_pages": {
                        "type": "boolean",
                        "description": "If true, checks all cached orders. If false, only current page.",
                        "default": False,
                    }
                },
                "required": [],
            },
        },
    },
]

PERIOD_DAYS = {
    "last_week": 7,
    "last_month": 30,
    "last_3_months": 90,
    "last_6_months": 180,
    "last_year": 365,
    "all_time": 3650,
}


def _resolve_period_days(period: str, custom_days: int | None) -> int:
    """
    Convert period string or custom_days into a single integer day count.

    This is the key function for handling "last 5 months":
      - LLM sends custom_days=150 (5 * 30)
      - We pass that number directly to the API as ?days=150
      - No need to add a new enum value for every possible period

    Priority: custom_days > period
    """
    if custom_days is not None:
        return min(max(int(custom_days), 1), 730)  # clamp 1..730
    return PERIOD_DAYS.get(period, 30)


def _format_order_card(order: dict, index: int, stale: bool = False) -> str:
    """Format a single order as a readable chatbot message line."""
    oid = order.get("id") or order.get("order_id", "N/A")
    status = order.get("status", "unknown").upper()
    date = order.get("created_at", "")[:10]
    amount = order.get("total_amount", 0)
    items_count = len(order.get("items", []))
    stale_flag = " *status just changed*" if stale else ""
    return (
        f"{index}. Order #{oid} | {status}{stale_flag}\n"
        f"Date: {date} | Total: ${amount:.2f} | Items: {items_count}"
    )


async def _build_page_response(state: PaginationState) -> dict:
    items = state.current_items
    start_index = (state.current_page - 1) * state.page_size + 1
    stale_ids = state.stale_ids

    lines = []
    for i, order in enumerate(items):
        oid = order.get("id") or order.get("order_id", "")
        is_stale = oid in stale_ids

        lines.append(_format_order_card(order, start_index + i, stale=is_stale))

    state.clear_stale()
    for order in items:
        state.snapshot_order(order)
    await set_state(state)

    nav_parts = []
    if state.has_previous:
        nav_parts.append(
            f"← Type **previous** for orders {start_index - state.page_size}–{start_index - 1}"
        )
    if state.has_next:
        end_next = min(
            start_index + state.page_size + state.page_size - 1, state.total_items
        )
        nav_parts.append(
            f"→ Type **next** for orders {start_index + state.page_size}–{end_next}"
        )

    boundary_msgs = []
    if not state.has_previous:
        boundary_msgs.append(" This is the first page — no earlier orders.")
    if not state.has_next:
        boundary_msgs.append(" This is the last page — no more orders.")

    return {
        "success": True,
        "page": state.current_page,
        "total_pages": state.total_pages,
        "total_items": state.total_items,
        "showing": f"Orders {start_index}–{start_index + len(items) - 1} of {state.total_items}",
        "orders": items,
        "formatted_lines": lines,
        "navigation_hints": nav_parts,
        "boundary_messages": boundary_msgs,
        "has_next": state.has_next,
        "has_previous": state.has_previous,
    }


async def summarise_orders(
    days: int = 30, status_filter: str = "shipped", context: dict | None = None
):
    """For finding summary of orders according to user command"""
    conversation_id = (context or {}).get("conversation_id", "default")
    token = (context or {}).get("auth_token")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/orders/getOrderSummary",
                params={
                    "days": days,
                    "status": status_filter if status_filter != "all" else None,
                },
                headers={"Authorization": f"{token}"} if token else {},
            )

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"Failed to summarise order : {response.status_code}",
            }
        return {"success": True, "summary": response.json()}
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out. Please try again."}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_orders(
    period: str = "last_month",
    custom_days: int | None = None,
    status_filter: str = "all",
    limit: int = 50,
    sort_by: str = "date_desc",
    context: dict | None = None,
) -> dict:
    """
    HOW "LAST 5 MONTHS" IS HANDLED:
    ─────────────────────────────────
    The LLM sees "last 5 months" in the user message.
    The tool definition says: for custom periods, use custom_days.
    The LLM calls: list_orders(period="last_month", custom_days=150)
    _resolve_period_days() sees custom_days=150 and uses that.
    We pass ?days=150 to your Node.js API.
    Your Node.js getOrdersWithFilters() uses that to set fromDate.

    The key insight: you don't need a new enum for every possible period.
    custom_days handles any arbitrary range the user can ask for.
    """
    conversation_id = (context or {}).get("conversation_id", "default")
    token = (context or {}).get("auth_token")

    days = _resolve_period_days(period, custom_days)
    if custom_days is not None:
        period_label = f"the last {custom_days} days"
    else:
        period_label = period.replace("_", " ")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/orders/list",
                params={
                    "days": days,
                    "status": status_filter if status_filter != "all" else None,
                    "limit": limit,
                    "sort_by": sort_by,
                },
                headers={"Authorization": f"{token}"} if token else {},
            )

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"Failed to fetch orders: {response.status_code}",
            }

        data = response.json()
        orders = data.get("orders", data) if isinstance(data, dict) else data

        if not orders:
            return {
                "success": True,
                "total_items": 0,
                "message": f"No orders found for {period_label}.",
            }

        state = PaginationState(
            conversation_id=conversation_id,
            resource_type="orders",
            all_items=orders,
            current_page=1,
            page_size=PAGE_SIZE,
        )
        await set_state(state)

        result = await _build_page_response(state)
        result["message"] = (
            f"Found **{state.total_items} orders** from {period_label}. "
            f"Showing page 1 of {state.total_pages} ({PAGE_SIZE} per page)."
        )
        return result

    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out. Please try again."}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def navigate_orders(
    direction: str,
    page_number: int | None = None,
    context: dict | None = None,
) -> dict:
    """Navigate cached order list. Does NOT call the API."""
    conversation_id = (context or {}).get("conversation_id", "default")
    state = await get_state(conversation_id, "orders")
    print("check now ", state)

    if state is None:
        return {
            "success": False,
            "error": "no_cache",
            "message": (
                "I don't have any orders loaded yet. "
                "Please ask me to 'show my orders' first, then I can navigate between pages."
            ),
        }

    if state.is_list_stale:
        token = context.get("auth_token") if context else None
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/orders",
                headers={"Authorization": token} if token else None,
                params={"period": "last_month"},
            )

        if response.status_code != 200:
            return {"success": False, "error": "Orders fetched failed"}

        else:
            data = response.json()
            state = get_state(conversation_id, data)
            if state is None:
                return {"success": False, "error": "Failed to reload orders."}

            response["message"] = (
                "Your order list was refreshed (it was over 5 minutes old)",
                f"Now showing page 1 of {state.total_pages}.",
            )
            return response

    intent_map = {
        "next": NavigationIntent.NEXT,
        "previous": NavigationIntent.PREVIOUS,
        "first": NavigationIntent.FIRST,
        "last": NavigationIntent.LAST,
        "specific": NavigationIntent.SPECIFIC,
        "refresh": NavigationIntent.REFRESH,
    }

    intent = intent_map.get(direction, NavigationIntent.NEXT)

    if intent == NavigationIntent.REFRESH:
        return await _refresh_current_page(state, context)

    nav_result = await apply_navigation(state, intent, page=page_number)

    if nav_result["warning"]:
        page_data = await _build_page_response(state)
        page_data["warning"] = nav_result["warning"]
        page_data["at_boundary"] = nav_result["at_boundary"]
        return page_data

    result = await _build_page_response(state)
    result["navigation_result"] = nav_result
    return result


async def _refresh_current_page(state: PaginationState, context: dict | None) -> dict:
    """Re-fetch orders on the current page from the API to detect status changes."""
    token = (context or {}).get("auth_token")
    changed = []

    for order in state.current_items:
        oid = order.get("id") or order.get("order_id", "")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{settings.BACKEND_API}/api/orders/{oid}/status",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                )

            if resp.status_code == 200:
                fresh = resp.json()
                snap = state.snapshots.get(oid)
                if snap and snap.status_changed(fresh):
                    changed.append(
                        {
                            "order_id": oid,
                            "old_status": snap.status_at_fetch,
                            "new_status": fresh.get("status"),
                        }
                    )

                    for i, item in enumerate(state.all_items):
                        item_id = item.get("id") or item.get("order_id", "")
                        if item_id == oid:
                            state.all_items[i] = fresh
                            break

                    state.snapshot_order(fresh)
        except Exception:
            pass

    set_state(state)
    result = await _build_page_response(state)
    result["refreshed"] = True
    result["changed_orders"] = changed
    if changed:
        result["change_summary"] = (
            f" {len(changed)} order(s) changed status since you last viewed this page:\n"
            + "\n".join(
                f"  • Order #{c['order_id']}: {c['old_status'].upper()} → {c['new_status'].upper()}"
                for c in changed
            )
        )
    else:
        result["change_summary"] = " No status changes detected on this page."
    return result


async def list_tickets(
    status_filter: str = "all",
    limit: int = 50,
    context: dict | None = None,
) -> dict:
    conversation_id = (context or {}).get("conversation_id", "default")

    async with AsyncSessionLocal() as db:
        query = select(Ticket)
        if status_filter != "all":
            query = query.where(Ticket.status == status_filter)
        query = query.limit(limit).order_by(Ticket.created_at.desc())
        results = await db.execute(query)
        tickets = results.scalars().all()

    ticket_dicts = [
        {
            "id": str(t.id),
            "title": t.title,
            "description": t.description,
            "priority": t.priority.value,
            "status": t.status,
            "created_at": str(t.created_at),
        }
        for t in tickets
    ]

    if not ticket_dicts:
        return {"success": True, "total_items": 0, "message": "No tickets found."}

    state = PaginationState(
        conversation_id=conversation_id,
        resource_type="tickets",
        all_items=ticket_dicts,
        current_page=1,
        page_size=PAGE_SIZE,
    )
    set_state(state)

    items = state.current_items
    start_index = 1
    lines = []
    for i, t in enumerate(items):
        lines.append(
            f"{i + 1}. Ticket #{t['id'][:8]}… | {t['status'].upper()} | Priority: {t['priority'].upper()}\n"
            f"   {t['title']}"
        )

    nav_parts = []
    if state.has_next:
        nav_parts.append(
            f"→ Type **next** for tickets {start_index + PAGE_SIZE}–"
            f"{min(start_index + PAGE_SIZE * 2 - 1, state.total_items)}"
        )

    return {
        "success": True,
        "page": 1,
        "total_pages": state.total_pages,
        "total_items": state.total_items,
        "showing": f"Tickets 1–{len(items)} of {state.total_items}",
        "tickets": items,
        "formatted_lines": lines,
        "navigation_hints": nav_parts,
        "message": f"Found **{state.total_items} tickets**. Showing page 1 of {state.total_pages}.",
    }


async def navigate_tickets(
    direction: str,
    page_number: int | None = None,
    context: dict | None = None,
) -> dict:

    conversation_id = (context or {}).get("conversation_id", "default")
    state = await get_state(conversation_id, "tickets")

    if state is None:
        return {
            "success": False,
            "error": "no_cache",
            "message": "Please ask me to 'show my tickets' first.",
        }

    intent_map = {
        "next": NavigationIntent.NEXT,
        "previous": NavigationIntent.PREVIOUS,
        "first": NavigationIntent.FIRST,
        "last": NavigationIntent.LAST,
        "specific": NavigationIntent.SPECIFIC,
        "refresh": NavigationIntent.REFRESH,
    }

    intent = intent_map.get(direction, NavigationIntent.NEXT)

    nav_result = apply_navigation(state, intent, page=page_number)

    items = state.current_items
    start_index = (state.current_page - 1) * state.page_size + 1
    lines = []
    for i, t in enumerate(items):
        lines.append(
            f"{start_index + i}. Ticket #{t['id'][:8]}… | {t.get('status', '').upper()} | {t.get('priority', '').upper()}\n"
            f"   {t['title']}"
        )

    nav_parts = []
    if state.has_previous:
        nav_parts.append("← Type **previous** for earlier tickets")

    if state.has_next:
        nav_parts.append("→ Type **next** for more tickets")

    boundary_msgs = []
    if not state.has_previous:
        boundary_msgs.append("This is the first page of tickets.")
    if not state.has_next:
        boundary_msgs.append("This is the last page of tickets.")

    result = {
        "success": True,
        "page": state.current_page,
        "total_pages": state.total_pages,
        "total_items": state.total_items,
        "showing": f"Tickets {start_index}–{start_index + len(items) - 1} of {state.total_items}",
        "tickets": items,
        "formatted_lines": lines,
        "navigation_hints": nav_parts,
        "boundary_messages": boundary_msgs,
    }

    if nav_result.get("warning"):
        result["warning"] = nav_result["warning"]

    return result


async def get_order_updates(
    check_all_pages: bool = False,
    context: dict | None = None,
) -> dict:
    """
    Check if any order the user is currently viewing has changed status.
    This is triggered by:
      1. User says "any updates?" or "refresh"
      2. Background watcher fires a change notification
    """
    conversation_id = (context or {}).get("conversation_id", "default")
    state = await get_state(conversation_id, "orders")

    if state is None:
        return {
            "success": False,
            "message": "No orders loaded. Ask to show your orders first.",
        }

    orders_to_check = state.all_items if check_all_pages else state.current_items
    token = (context or {}).get("auth_token")
    changed = []
    errors = []

    async def check_one(order: dict):
        oid = order.get("id") or order.get("order_id", "")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{settings.BACKEND_API}/api/orders/{oid}/status",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    timeout=5.0,
                )
            if resp.status_code == 200:
                fresh = resp.json()
                snap = state.snapshots.get(oid)
                if snap and snap.status_changed(fresh):
                    changed.append(
                        {
                            "order_id": oid,
                            "old_status": snap.status_at_fetch,
                            "new_status": fresh.get("status"),
                            "order": fresh,
                        }
                    )
                    for i, item in enumerate(state.all_items):
                        iid = item.get("id") or item.get("order_id", "")
                        if iid == oid:
                            state.all_items[i] = fresh
                    state.snapshot_order(fresh)
        except Exception as e:
            errors.append(str(e))

    await asyncio.gather(*[check_one(o) for o in orders_to_check])
    set_state(state)

    if not changed:
        return {
            "success": True,
            "changed": False,
            "message": "No order status changes detected. Everything looks the same.",
            "checked": len(orders_to_check),
        }

    summary_lines = [
        f"• Order #{c['order_id']}: **{c['old_status'].upper()}** → **{c['new_status'].upper()}**"
        for c in changed
    ]
    return {
        "success": True,
        "changed": True,
        "changed_count": len(changed),
        "changed_orders": changed,
        "summary": "\n".join(summary_lines),
        "message": (
            f"**{len(changed)} order(s) changed status** while you were viewing:\n"
            + "\n".join(summary_lines)
            + "\n\nType **refresh** to see the updated page."
        ),
    }


async def check_order_status(
    order_id: str, customer_email: str = None, context: dict | None = None
) -> dict:
    try:
        token = (context or {}).get("auth_token")
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.BACKEND_API}/api/orders/{order_id}/status",
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        if response.status_code != 200:
            return {"success": False, "error": f"Order {order_id} not found"}
        return {"success": True, "order": response.json()}
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
        "eta": "5-7 business days",
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
        sentiment, priority = "NEUTRAL", "MEDIUM"
    return {
        "sentiment": sentiment,
        "priority": priority,
        "confidence": 0.85,
        "message": message,
        "action": "escalate_to_human" if sentiment == "NEGATIVE" else "continue_bot",
    }


async def cancel_order(order_id: str, reason: str = None, context: dict | None = None):
    try:
        token = (context or {}).get("auth_token")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.BACKEND_API}/api/orders/{order_id}/cancel",
                json={"reason": reason},
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        if response.status_code == 200:
            return {
                "success": True,
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
    try:
        token = (context or {}).get("auth_token")
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
        return {"success": False, "error": "Product not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def search_knowledge_base(query: str, context: dict | None = None) -> dict:
    """Search knowledge base for FAQs and policies"""

    knowledge_base = {
        "return policy": {
            "title": "Return Policy",
            "content": "We offer 30-day returns for most items in original condition. Electronics have a 15-day return window. Return shipping is free for defective items.",
        },
        "shipping": {
            "title": "Shipping Information",
            "content": "Standard shipping (5-7 business days): Free on orders over $50, $5.99 otherwise. Express (2-3 days): $15.99. Overnight: $29.99.",
        },
        "warranty": {
            "title": "Warranty Information",
            "content": "All products come with a 1-year manufacturer warranty. Extended warranties up to 3 years available.",
        },
        "payment methods": {
            "title": "Accepted Payment Methods",
            "content": "Visa, Mastercard, Amex, PayPal, Apple Pay, Google Pay, bank transfers.",
        },
        "privacy": {
            "title": "Privacy Policy",
            "content": "We protect your personal information and do not sell it to third parties.",
        },
        "refund": {
            "title": "Refund Policy",
            "content": "Refunds processed within 5-7 business days after item received back.",
        },
        "international shipping": {
            "title": "International Shipping",
            "content": "We ship to 100+ countries. Duties are the responsibility of the customer.",
        },
    }
    query_lower = query.lower()
    for key, info in knowledge_base.items():
        if key in query_lower:
            return {"success": True, "topic": info["title"], "content": info["content"]}
    return {
        "success": False,
        "message": "Policy information not found. Please contact support.",
    }


TOOL_EXECUTOR = {
    "check_order_status": check_order_status,
    "cancel_order": cancel_order,
    "initiate_refund": initiate_refund,
    "check_product_stock": check_product_stock,
    "list_orders": list_orders,
    "navigate_orders": navigate_orders,
    "list_tickets": list_tickets,
    "navigate_tickets": navigate_tickets,
    "get_order_updates": get_order_updates,
    "search_knowledge_base": search_knowledge_base,
    "create_ticket": create_ticket,
    "sentiment_detection": sentiment_detection,
    "summarise_orders": summarise_orders,
}
