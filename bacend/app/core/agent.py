import asyncio
import json
import random
import time

import structlog
from app.config import settings
from app.core.evaluator import evaluate_response

# from app.core.rag import search_knowledge_base
# from app.core.semantic_cache import cache_response, get_cached_response
from app.core.tools import TOOL_DEFINITIONS, TOOL_EXECUTOR
from app.models.organization import Organization
from app.models.prompt_version import PromptVersion

# from app.services.billing_service import record_usage
from app.services.llm_service import llm_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


DEFAULT_SYSTEM_PROMPT = """
You are a professional customer support agent for {company_name}.
Be helpful, empathetic, concise, and accurate.

Capabilities:
- Answer questions (ALWAYS search knowledge base first for policy/product questions)
- Check order status and shipping info
- Process refunds for eligible orders
- Create and update support tickets
- Escalate to human agents when needed

Critical rules:
1. ALWAYS call search_knowledge_base before answering any policy or FAQ question
2. NEVER make up information — only state what tools return to you
3. Check order status BEFORE processing any refund
4. Escalate if customer uses: "furious", "lawyer", "lawsuit", "terrible", or asks for human
5. Keep responses under 4 sentences unless a detailed explanation is truly needed
6. Always confirm what action you took: "I have initiated your refund..."
7. If you cannot resolve the issue after 2 tool calls, create a ticket and escalate
"""


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
    ) -> dict:
        start_time = time.time()

        # add cache history after

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
        max_iterations = 5
        print("now reached here ")
        for iteration in range(max_iterations):
            logger.info("Agent iteration", i=iteration, org=org.slug)
            llm_result = await llm_service.complete(
                messages=messages, tools=TOOL_DEFINITIONS, temperature=0.1
            )
            print("now checking this", llm_result)

            total_prompt_tokens += llm_result["prompt_tokens"]
            total_completion_tokens += llm_result["completion_tokens"]
            total_cost += llm_result["cost_usd"]
            if llm_result["used_fallback"]:
                used_fallback = llm_result["used_fallback"]

            message = llm_result["message"]
            finish_reason = llm_result["finish_reason"]

            if finish_reason == "stop":
                final_response = message.content

                # we have to cache response

                asyncio.create_task(
                    self._run_evaluation(
                        question=user_message,
                        response=final_response,
                        context=rag_content,
                        conversation_id=conversation_id,
                        org_id=str(org.id),
                    )
                )

                # we have to record billing here

                return {
                    "response": final_response,
                    "tool_calls": tool_calls_log,
                    "tokens_used": total_prompt_tokens + total_completion_tokens,
                    "cost_usd": total_cost,
                    "from_cache": False,
                    "used_fallback": used_fallback,
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

                    logger.info("Executing tool", tool=name, org=org.slug)

                    try:
                        # if name == "search_knowledge_base":
                        #     results = await search_knowledge_base(
                        #         args["query"], org_id=str(org.id)
                        #     )

                        #     if results:
                        #         rag_context = "\n\n".join(
                        #             [
                        #                 f"[Source: {r['source']}]\n{r['content']}"
                        #                 for r in results
                        #             ]
                        #         )
                        #         tool_result = rag_context
                        #     else:
                        #         tool_result = "No relevant information found in the knowledge base."

                        if name in ("escalate_to_human", "create_ticket"):
                            tool_result = json.dumps(
                                await TOOL_EXECUTOR[name](
                                    **args,
                                    user_id=user_id,
                                    conversation_id=conversation_id,
                                    org_id=str(org.id),
                                )
                            )
                        elif name in TOOL_EXECUTOR:
                            tool_result = json.dumps(await TOOL_EXECUTOR[name](**args))

                        else:
                            tool_result = f"Tool '{name}' not available."

                    except Exception as e:
                        logger.error("Tool failed", tool=name, error=str(e))
                        tool_result = json.dumps({"error": str(e)})

                    tool_calls_log.append(
                        {"name": name, "args": args, "result": tool_result[:500]}
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(tool_result),
                        }
                    )

        return {
            "response": "I'm having trouble processing your request. A ticket has been created and a human agent will follow up.",
            "tools_calls": tool_calls_log,
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


# async def _save_trace(self,org,conversation_id,user_message,final_response,tool_calls,messges_sent,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,latency_ms,used_fallback,cache_hit_db):


enterprise_agent = EnterpriseAgent()
