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
STEP B  → navigate_orders()                     — reads from pagination cache, NO API call
STEP C  → returns page 2 (orders 6-10)          — zero LLM calls, zero API calls

UNIFIED RESPONSE SHAPE (all paths):
{
    "message": "Found 50 orders. Showing page 1.",   # always present
    "ui": {                                           # present only for orders/tickets/comparison
        "type": "orders",
        "data": [...],
        "pagination": {"page": 1, "next": True, "previous": False}
    },
    "tool_calls": [...],
    "tokens_used": 0,
    "cost_usd": 0.0,
    "from_cache": False,
}
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
from app.core.redis_client import redis
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
• Comparing selected items
• Never render images or markdown image links.
• If tool responses contain image URLs, ignore them.
• Only show text information about orders.

Then ask how you can help.

Rules:

1. ALWAYS call search_knowledge_base before answering policy or FAQ questions.
2. NEVER invent information — only use tool results.
3. Check order status BEFORE processing a refund.
4. Escalate to human support if the user is angry or asks for a human.
5. Keep responses concise, but greetings may include a short bullet list of capabilities.
6. Clearly confirm actions taken (example: "I have cancelled your order.").
7. If the issue cannot be resolved after two tool calls, create a support ticket.
8. Never create more than one ticket for the same order. If a ticket already exists, inform the user instead of creating a new one.

Your goal is to help customers quickly resolve issues with their orders and purchases.

IMPORTANT:
For any user-specific request about orders, tickets, refunds, cancellations, stock, tracking,
account data, or comparison of selected backend items, you MUST use the appropriate tool and MUST NOT answer from general reasoning.

Order rules:
- If the user asks to show, list, view, or browse orders, always call list_orders first.
- If the user asks for next, previous, refresh, first, last, or a page number for orders, use navigate_orders.
- If the user asks about one specific order, use check_order_status.

Comparison output format (CRITICAL):
After compare_backend_items returns results, you MUST reply with ONLY this JSON.
No markdown, no prose before or after it — raw JSON only:

{
  "summary": "one sentence plain English answer for the chat bubble",
  "query_type": "comparison",
  "supported": true,
  "answer": "name of the best product or null",
  "reason": "one line why it is best or null",
  "follow_up_question": null,
  "warning": null,
  "ranked_products": [
    {
      "rank": 1,
      "id": "product_id_from_tool_result",
      "name": "Product Name",
      "price": 99.99,
      "rating": 4.5,
      "stock": 10,
      "why": "one line reason for this rank"
    }
  ]
}

Rules for the JSON:
- query_type must be exactly one of: "comparison", "unsupported", "needs_preference"
- If the user asks about sales, profit, demand, or trending (data not in backend results):
    set query_type="unsupported", supported=false, warning="Sales data not available", ranked_products=null
- If you cannot answer without knowing the user's use-case (gaming, music, gift):
    set query_type="needs_preference", follow_up_question="What will you use this for?", ranked_products=null
- Never invent fields not present in the tool result (do not guess stock, rating, or price)
- ranked_products must use IDs from the tool result only
- summary is the only field shown in the chat bubble — keep it under 20 words

Comparison rules:
- If the user asks to compare products, selected orders, or backend items, use compare_backend_items.
- compare_backend_items must receive the selected item or order IDs as an array.
- Never compare selected items from memory or general reasoning.
- Always base comparison only on data returned by compare_backend_items or other tool results.
- When comparing items, consider only the fields provided by the backend data, such as price, rating, review count, delivery details, return rate, review summary, status, or other available comparison fields.
- Do not invent missing comparison values.
- If fewer than 2 valid items are available, clearly say that comparison cannot be completed.
- After tool results are returned, summarize which item is best, why it is best, and mention important strengths or weaknesses briefly.
- If the backend provides a ranking or score, use it in the explanation and do not override it with made-up reasoning.

Ticket rules:
- If the user asks to show, list, or view tickets, use list_tickets.
- If the user asks for next, previous, refresh, first, last, or a page number for tickets, use navigate_tickets.
- If the user asks about one specific ticket or provides a ticket ID, use get_ticket_details.
- Do not use check_order_status for ticket IDs.

Policy rules:
- Always call search_knowledge_base before answering policy or FAQ questions.

Never fabricate order, ticket, refund, stock, tracking, or comparison information.
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
    "compare",
    "selected",
    "compare_backend_items",
}

_GREETING_KEYWORDS = {
    "hi",
    "hello",
    "hey",
    "hii",
    "helo",
    "good morning",
    "good afternoon",
    "good evening",
}


def _is_greeting(message: str) -> bool:
    msg = message.strip().lower()
    return msg in _GREETING_KEYWORDS


async def handle_greeting_fast_path(
    message: str,
    conversation_id: str,
    company_name: str,
    context: dict,
):
    if not _is_greeting(message):
        return None

    SESSION_TTL_SECONDS = 86400
    key = f"chat:greeting_count:{conversation_id}"

    current = await redis.get(key)
    greeting_count = int(current) + 1 if current else 1

    await redis.set(key, greeting_count, ex=SESSION_TTL_SECONDS)

    if greeting_count == 1:
        return {
            "message": (
                f"Hello! I'm your {company_name} AI assistant. "
                f"I can help with orders, refunds, support tickets, and policy questions. "
                f"How can I assist you today?"
            ),
            "ui": None,
            "tool_calls": [],
            "tokens_used": 0,
            "cost_usd": 0.0,
            "from_cache": True,
        }

    if greeting_count <= 3:
        return {
            "message": "Hi again! How can I help you today?",
            "ui": None,
            "tool_calls": [],
            "tokens_used": 0,
            "cost_usd": 0.0,
            "from_cache": True,
        }

    return {
        "message": "Hello! What can I help you with today?",
        "ui": None,
        "tool_calls": [],
        "tokens_used": 0,
        "cost_usd": 0.0,
        "from_cache": True,
    }


def _is_data_query(message: str) -> bool:
    """
    Returns True if the message is asking for user-specific data.

    Safe to cache:   policy questions, FAQ, shipping info, warranty
    Never cache:     anything with orders, tickets, refunds, account data, comparison
    """
    msg_lower = message.lower()
    return any(keyword in msg_lower for keyword in _DATA_QUERY_KEYWORDS)


async def _detect_context_resource(conversation_id: str) -> str:
    """
    Determine if the user is currently browsing orders or tickets
    by checking which was fetched most recently.
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
    Returns None for non-navigation messages so the main loop continues.
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
    context["conversation_id"] = conversation_id

    if resource == "tickets":
        return await navigate_tickets(
            direction=direction, page_number=page_num, context=context
        )

    return await navigate_orders(
        direction=direction, page_number=page_num, context=context
    )


def format_navigation_response(result: dict, resource: str = "orders") -> str:
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


def _build_ui_block(raw_result: dict, resource: str) -> dict:
    return {
        "type": resource,
        "data": raw_result.get(resource, []),
        "pagination": {
            "page": raw_result.get("page", 1),
            "total_pages": raw_result.get("total_pages", 1),
            "total_items": raw_result.get("total_items", 0),
            "next": raw_result.get("next", False),
            "previous": raw_result.get("previous", False),
            "showing": raw_result.get("showing"),
        },
    }


async def _get_for_org(org: Organization, db: AsyncSession) -> str:
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
    return DEFAULT_SYSTEM_PROMPT.replace("{company_name}", org.company_name)


def _paginated_response(
    raw_result: dict,
    resource: str,
    tool_calls_log: list,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    from_cache: bool = False,
) -> dict:
    return {
        "message": raw_result.get("message", f"Here are your {resource}."),
        "ui": _build_ui_block(raw_result, resource),
        "tool_calls": tool_calls_log,
        "tokens_used": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd,
        "from_cache": from_cache,
    }


def _comparison_response(
    parsed: dict,
    tool_calls_log: list,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> dict:
    """
    Return the unified response dict for a comparison result.
    The LLM has replied with structured JSON after compare_backend_items.

    Shape:
    {
        "message": "Sony PS4 is the best overall.",
        "ui": {
            "type": "comparison",
            "query_type": "comparison",
            "supported": true,
            "answer": "Sony Playstation 4 Pro",
            "reason": "Highest rating 5.0 with good stock",
            "follow_up_question": null,
            "warning": null,
            "ranked_products": [...]
        },
        ...
    }
    """
    return {
        "message": parsed.get("summary", "Here is the comparison."),
        "ui": {
            "type": "comparison",
            "query_type": parsed.get("query_type", "comparison"),
            "supported": parsed.get("supported", True),
            "answer": parsed.get("answer"),
            "reason": parsed.get("reason"),
            "follow_up_question": parsed.get("follow_up_question"),
            "warning": parsed.get("warning"),
            "ranked_products": parsed.get("ranked_products"),
        },
        "tool_calls": tool_calls_log,
        "tokens_used": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd,
        "from_cache": False,
    }


def _text_response(
    text: str,
    tool_calls_log: list,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    from_cache: bool = False,
    used_fallback=None,
) -> dict:
    return {
        "message": text,
        "ui": None,
        "tool_calls": tool_calls_log,
        "tokens_used": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd,
        "from_cache": from_cache,
        "used_fallback": used_fallback,
    }


def _try_parse_comparison(text: str) -> dict | None:
    """
    Safely attempt to parse a comparison JSON reply from the LLM.

    The LLM is instructed to reply with raw JSON after compare_backend_items.
    Sometimes it wraps the JSON in markdown fences (```json ... ```) — strip those.
    Returns the parsed dict if it looks like a comparison reply, else None.

    This is intentionally defensive:
    - Non-JSON replies (normal chat) return None without raising
    - JSON that lacks query_type returns None (not a comparison reply)
    """
    try:
        clean = text.strip()

        # Strip markdown code fences if LLM added them
        if clean.startswith("```"):
            # "```json\n{...}\n```"  →  "{...}"
            parts = clean.split("```")
            # parts[1] is the content between the first pair of fences
            if len(parts) >= 2:
                clean = parts[1]
                # Remove language tag (e.g. "json\n")
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()

        parsed = json.loads(clean)

        # Only treat it as a comparison response if query_type is present
        if "query_type" not in parsed:
            return None

        return parsed

    except (json.JSONDecodeError, ValueError, IndexError):
        # Normal text reply — not JSON, that is fine
        return None


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

        greeting_result = await handle_greeting_fast_path(
            user_message,
            conversation_id,
            "Proshop",
            context,
        )
        if greeting_result is not None:
            return greeting_result

        nav_result = await handle_navigation_fast_path(
            user_message, conversation_id, context
        )

        if nav_result is not None:
            resource = await _detect_context_resource(conversation_id)
            logger.debug("navigation fast path", resource=resource, result=nav_result)
            return _paginated_response(
                raw_result=nav_result,
                resource=resource,
                tool_calls_log=[],
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                from_cache=True,
            )

        if not _is_data_query(user_message):
            cached = await get_cached_response(user_message, str(org.id))
            if cached:
                return _text_response(
                    text=cached["response"],
                    tool_calls_log=[],
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    from_cache=True,
                )

        system_prompt = await _get_for_org(org, db)
        messages = [
            {"role": "system", "content": system_prompt},
            *conversation_history,
            {"role": "user", "content": user_message},
        ]

        tool_calls_log: list = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cost = 0.0
        used_fallback = None
        _produced_paginated_result = False
        _produced_comparison_result = False

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

            logger.debug(
                "llm iteration", iteration=iteration, finish_reason=finish_reason
            )

            if finish_reason == "stop":
                final_text = message.content
                if _produced_comparison_result:
                    parsed = _try_parse_comparison(final_text)
                    if parsed is not None:
                        logger.debug(
                            "comparison result parsed",
                            query_type=parsed.get("query_type"),
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
                        return _comparison_response(
                            parsed=parsed,
                            tool_calls_log=tool_calls_log,
                            prompt_tokens=total_prompt_tokens,
                            completion_tokens=total_completion_tokens,
                            cost_usd=total_cost,
                        )
                    logger.warning(
                        "comparison JSON parse failed, falling back to text",
                        response=final_text[:200],
                    )

                if not _is_data_query(user_message) and not _produced_paginated_result:
                    asyncio.create_task(
                        cache_response(user_message, final_text, str(org.id))
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

                return _text_response(
                    text=final_text,
                    tool_calls_log=tool_calls_log,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    cost_usd=total_cost,
                    from_cache=False,
                    used_fallback=used_fallback,
                )

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

                    if name == "compare_backend_items":
                        _produced_comparison_result = True

                    try:
                        if name == "create_ticket":
                            raw_result = await TOOL_EXECUTOR[name](
                                **args,
                                user_id=user_id,
                                conversation_id=conversation_id,
                            )
                        elif name in TOOL_EXECUTOR:
                            context["conversation_id"] = conversation_id
                            raw_result = await TOOL_EXECUTOR[name](
                                **args,
                                context=context,
                            )
                        else:
                            raw_result = {"error": f"Tool '{name}' not available."}

                        if name in ("list_orders", "navigate_orders"):
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
                            return _paginated_response(
                                raw_result=raw_result,
                                resource="orders",
                                tool_calls_log=tool_calls_log
                                + [
                                    {
                                        "name": name,
                                        "args": args,
                                        "result": str(raw_result)[:500],
                                    }
                                ],
                                prompt_tokens=total_prompt_tokens,
                                completion_tokens=total_completion_tokens,
                                cost_usd=total_cost,
                                from_cache=False,
                            )

                        if name in ("list_tickets", "navigate_tickets"):
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
                            return _paginated_response(
                                raw_result=raw_result,
                                resource="tickets",
                                tool_calls_log=tool_calls_log
                                + [
                                    {
                                        "name": name,
                                        "args": args,
                                        "result": str(raw_result)[:500],
                                    }
                                ],
                                prompt_tokens=total_prompt_tokens,
                                completion_tokens=total_completion_tokens,
                                cost_usd=total_cost,
                                from_cache=False,
                            )

                        tool_result = json.dumps(raw_result)

                    except Exception as e:
                        logger.error("tool execution error", tool=name, error=str(e))
                        tool_result = json.dumps({"error": str(e)})

                    tool_calls_log.append(
                        {"name": name, "args": args, "result": tool_result[:500]}
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result,
                        }
                    )

        return _text_response(
            text="Something went wrong. Please try again.",
            tool_calls_log=tool_calls_log,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=total_cost,
            from_cache=False,
        )

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
