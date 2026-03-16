"""
agent.py — FULLY ANNOTATED
===========================

COMPLETE FLOW when user types: "show my orders for last 5 months"

STEP 1  → handle_navigation_fast_path()         — not navigation, returns None
STEP 2  → _is_data_query() check                — skips semantic cache (user-specific data)
STEP 3  → LLM call with TOOL_DEFINITIONS        — LLM decides to call list_orders
STEP 4  → TOOL_EXECUTOR["list_orders"]()        — calls Node.js API, gets 50 orders
STEP 5  → set_state() inside list_order()       — SAVES 50 ORDERS TO PAGINATION CACHE
STEP 6  → _build_page_response()                — slices orders[0:5], returns page 1
STEP 7  → tool result appended to messages      — LLM sees page 1 result
STEP 8  → LLM formats final reply               — "Found 50 orders, showing 1-5..."
STEP 9  → cache_response() skipped              — data query, NOT saved to semantic cache
STEP 10 → return to user                        — page 1 displayed

THEN when user types "next":
STEP A  → handle_navigation_fast_path()         — detected as NEXT intent
STEP B  → navigate_orders()                      — reads from pagination cache, NO API call
STEP C  → returns page 2 (orders 6-10)          — zero LLM calls, zero API calls
"""

import asyncio
import json
import random

import structlog
from app.core.evaluator import evaluate_response
from app.core.pagination_cache import (
    NavigationIntent,
    get_state,
    parse_navigation_intent,
)
from app.core.semantic_cache import cache_response, get_cached_response
from app.core.tools import (
    TOOL_DEFINITIONS,
    TOOL_EXECUTOR,
    get_order_updates,
    navigate_orders,
    navigate_tickets,
)
from app.models.organization import Organization
from app.models.prompt_version import PromptVersion
from app.services.billing_service import record_usage
from app.services.llm_service import llm_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

DEFAULT_SYSTEM_PROMPT = """
You are a professional AI customer support assistant for {company_name}, an e-commerce platform.

Be helpful, concise, and friendly.

When a user greets you (hi, hello, hey):

Introduce yourself and list your main capabilities as bullet points.

Example format:

Hello! I'm your {company_name} AI assistant. I can help with:

• Viewing and navigating your orders
• Checking order status and shipping information
• Cancelling orders
• Initiating refunds
• Viewing support tickets
• Creating or managing support tickets
• Answering policy questions (returns, shipping, warranty)

Then ask how you can help.

Rules:

1. ALWAYS call search_knowledge_base before answering policy or FAQ questions.
2. NEVER invent information — only use tool results.
3. Check order status BEFORE processing a refund.
4. Escalate to human support if the user is angry or asks for a human.
5. Keep responses concise, but greetings may include a short bullet list of capabilities.
6. Clearly confirm actions taken (example: "I have cancelled your order.").
7. If the issue cannot be resolved after two tool calls, create a support ticket.

Your goal is to help customers quickly resolve issues with their orders and purchases.
"""

_DATA_QUERY_KEYWORDS = {
    "order",
    "orders",
    "ticket",
    "tickets",
    "refund",
    "cancel",
    "stock",
    "my account",
    "shipped",
    "delivered",
    "purchase",
    "tracking",
    "invoice",
    "payment",
    "hi",
    "hello",
    "hey",
}


def _is_data_query(message: str) -> bool:
    """
    Returns True if the message is asking for user-specific data.

    WHY THIS EXISTS:
    Semantic cache is great for static answers: "what is your return policy?"
    It is DANGEROUS for personal data: "show my orders last 5 months"
    because the cached reply would contain another user's orders.

    Safe to cache:   policy questions, FAQ, shipping info, warranty
    Never cache:     anything with orders, tickets, refunds, account data
    """
    msg_lower = message.lower()
    return any(keyword in msg_lower for keyword in _DATA_QUERY_KEYWORDS)


async def _detect_context_resource(conversation_id: str) -> str:
    """
    Determine if the user is currently browsing orders or tickets
    by checking which was fetched most recently.
    Used by the navigation fast path to route "next"/"prev" correctly.
    """
    orders_state = await get_state(conversation_id, "orders")
    tickets_state = await get_state(conversation_id, "tickets")

    if orders_state and tickets_state:
        if orders_state.fetched_at > tickets_state.fetched_at:
            return "orders"
        return "tickets"

    if tickets_state:
        return "tickets"

    return "orders"


async def handle_navigation_fast_path(
    message: str,
    conversation_id: str,
    context: dict,
):
    """
    STEP 1 in the flow.

    Checks if the message is purely a navigation command.
    If yes: handles it entirely without calling the LLM.
    If no: returns None so the main loop continues.

    For "show my orders for last 5 months" → returns None (not navigation).
    For "next" after orders are loaded → returns page 2 from pagination cache.
    """
    intent, page_num = parse_navigation_intent(message)
    if intent is None:
        return None

    resource = await _detect_context_resource(conversation_id)

    direction_map = {
        NavigationIntent.NEXT: "next",
        NavigationIntent.PREVIOUS: "previous",
        NavigationIntent.FIRST: "first",
        NavigationIntent.LAST: "last",
        NavigationIntent.SPECIFIC: "specific",
        NavigationIntent.REFRESH: "refresh",
    }
    direction = direction_map[intent]

    if resource == "tickets":
        return await navigate_tickets(
            direction=direction, page_number=page_num, context=context
        )

    context["conversation_id"] = conversation_id
    return await navigate_orders(
        direction=direction, page_number=page_num, context=context
    )


def format_navigation_response(result: dict, resource: str = "orders") -> str:
    """
    Convert the raw pagination result dict into a clean chat message string.
    Used only for fast-path navigation responses.
    """
    if not result.get("success", True):
        return result.get("message", "Something went wrong.")

    lines = []

    if result.get("warning"):
        lines.append(result["warning"])
        lines.append("")

    if result.get("change_summary"):
        lines.append(result["change_summary"])
        lines.append("")

    showing = result.get("showing", "")
    total_pages = result.get("total_pages", 1)
    current_page = result.get("page", 1)

    if showing:
        lines.append(f"**{showing}** (page {current_page}/{total_pages})")
        lines.append("")

    formatted = result.get("formatted_lines", [])
    if formatted:
        lines.extend(formatted)
        lines.append("")

    hints = result.get("navigation_hints", [])
    if hints:
        lines.extend(hints)

    for bm in result.get("boundary_messages", []):
        lines.append(bm)

    if result.get("changed"):
        lines.append("")
        lines.append(result.get("message", ""))

    return "\n".join(lines).strip()


async def _get_for_org(org: Organization, db: AsyncSession) -> str:
    """
    Load the correct system prompt for this org.
    Supports A/B testing via traffic_percent weights on PromptVersion rows.
    Falls back to org.system_prompt, then DEFAULT_SYSTEM_PROMPT.
    """
    if org.active_prompt_id:
        result = await db.execute(
            select(PromptVersion).where(
                PromptVersion.org_id == org.id,
                PromptVersion.is_active == True,
            )
        )
        versions = result.scalars().all()

        if versions:
            total_weight = sum(v.traffic_percent for v in versions)
            r = random.uniform(0, total_weight)
            cumulative = 0
            for version in versions:
                cumulative += version.traffic_percent
                if r <= cumulative:
                    version.total_uses += 1
                    await db.commit()
                    return version.content.replace("{company_name}", org.company_name)

    if org.system_prompt:
        return org.system_prompt.replace("{company_name}", org.company_name)
    return DEFAULT_SYSTEM_PROMPT.format(company_name=org.company_name)


class EnterpriseAgent:
    async def run(
        self,
        user_message: str,
        conversation_history: list[dict],
        user_id: str,
        conversation_id: str,
        org: Organization,
        db: AsyncSession,
        context: dict | None = None,
    ) -> dict:
        context = context or {}
        nav_result = await handle_navigation_fast_path(
            user_message, conversation_id, context
        )

        if nav_result is not None:
            current_page = nav_result.get("page", 1)
            total_pages = nav_result.get("total_pages", 1)

            resource = await _detect_context_resource(conversation_id)

            return {
                "response": format_navigation_response(nav_result, resource),
                "tool_calls": [],
                "tokens_used": 0,
                "cost_usd": 0.0,
                "from_cache": True,
                "previous": nav_result.get("previous", current_page > 1),
                "next": nav_result.get("next", current_page < total_pages),
            }

        # -----------------------------
        # SEMANTIC CACHE
        # -----------------------------
        if not _is_data_query(user_message):
            cached = await get_cached_response(user_message, str(org.id))
            if cached:
                return {
                    "response": cached["response"],
                    "tool_calls": [],
                    "tokens_used": 0,
                    "cost_usd": 0.0,
                    "from_cache": True,
                }
        system_prompt = await _get_for_org(org, db)
        messages = [
            {"role": "system", "content": system_prompt},
            *conversation_history,
            {"role": "user", "content": user_message},
        ]

        tool_calls_log = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cost = 0.0
        used_fallback = None
        rag_content = ""

        # pagination UI state
        ui_navigation = None

        _produced_paginated_result = False

        max_iterations = 5

        for iteration in range(max_iterations):
            llm_result = await llm_service.complete(
                messages=messages,
                tools=TOOL_DEFINITIONS,
                temperature=0.1,
            )

            total_prompt_tokens += llm_result["prompt_tokens"]
            total_completion_tokens += llm_result["completion_tokens"]
            total_cost += llm_result["cost_usd"]

            if llm_result["used_fallback"]:
                used_fallback = llm_result["used_fallback"]

            message = llm_result["message"]
            finish_reason = llm_result["finish_reason"]

            if finish_reason == "stop":
                final_response = message.content

                if not _is_data_query(user_message) and not _produced_paginated_result:
                    asyncio.create_task(
                        cache_response(user_message, final_response, str(org.id))
                    )

                asyncio.create_task(
                    record_usage(
                        org_id=str(org.id),
                        model=llm_result["model"],
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        cost_usd=total_cost,
                        conversation_id=conversation_id,
                    )
                )

                return {
                    "response": final_response,
                    "tool_calls": tool_calls_log,
                    "tokens_used": total_prompt_tokens + total_completion_tokens,
                    "cost_usd": total_cost,
                    "from_cache": False,
                    "used_fallback": used_fallback,
                    "previous": ui_navigation["previous"] if ui_navigation else False,
                    "next": ui_navigation["next"] if ui_navigation else False,
                }

            if finish_reason == "tool_calls" and message.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in message.tool_calls
                        ],
                    }
                )

                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments)

                    if name in (
                        "list_orders",
                        "navigate_orders",
                        "list_tickets",
                        "navigate_tickets",
                    ):
                        _produced_paginated_result = True

                    try:
                        if name in ("create_ticket",):
                            raw_result = await TOOL_EXECUTOR[name](
                                **args,
                                user_id=user_id,
                                conversation_id=conversation_id,
                                org_id=str(org.id),
                            )
                        elif name in TOOL_EXECUTOR:
                            context["conversation_id"] = conversation_id
                            raw_result = await TOOL_EXECUTOR[name](
                                **args,
                                context=context,
                            )
                        else:
                            raw_result = {"error": f"Tool '{name}' not available."}
                        if isinstance(raw_result, dict):
                            if "next" in raw_result or "previous" in raw_result:
                                ui_navigation = {
                                    "next": raw_result.get("next", False),
                                    "previous": raw_result.get("previous", False),
                                }

                        tool_result = json.dumps(raw_result)

                    except Exception as e:
                        tool_result = json.dumps({"error": str(e)})

                    tool_calls_log.append(
                        {
                            "name": name,
                            "args": args,
                            "result": tool_result[:500],
                        }
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result,
                        }
                    )

        return {
            "response": "Something went wrong. Please try again.",
            "tool_calls": tool_calls_log,
            "tokens_used": total_prompt_tokens + total_completion_tokens,
            "cost_usd": total_cost,
            "from_cache": False,
        }

    async def _run_evaluation(
        self,
        question: str,
        response: str,
        context: str,
        conversation_id: str,
        org_id: str,
    ):
        try:
            scores = await evaluate_response(question, response, context)
            logger.info("Eval scores", scores=scores, conv=conversation_id)
        except Exception as e:
            logger.error("Eval failed", error=str(e))


enterprise_agent = EnterpriseAgent()
