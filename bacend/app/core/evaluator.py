"""
Automatically scores every agent response.
Uses a cheap LLM (gpt-4o-mini) to judge quality of the main LLM.
Scores are stored in DB for analytics and prompt improvement.
"""

import json

import structlog
from app.config import settings
from openai import AsyncOpenAI

logger = structlog.get_logger()
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

EVAL_PROMPT = """You are evaluating a customer support AI response.

Customer question: {question}
Agent response: {response}
Context provided to agent: {context}

Rate the response on these 3 dimensions, each 1-5:
- accuracy: Did it correctly answer the question based on context? (1=wrong, 5=perfect)
- helpfulness: Did it actually solve or progress the customer's issue? (1=useless, 5=fully resolved)
- tone: Was it professional, empathetic, and appropriate? (1=rude/cold, 5=excellent)

Return ONLY a JSON object with no explanation:
{{"accuracy": int, "helpfulness": int, "tone": int, "reasoning": "one sentence"}}"""


async def evaluate_response(
    question: str,
    response: str,
    context: str = "",
) -> dict:
    """
    Score a response. Runs after the main response is sent to user
    so it doesn't slow down the user-facing response time.
    """
    try:
        result = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": EVAL_PROMPT.format(
                        question=question, response=response, context=context[:1000]
                    ),
                }
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        scores = json.loads(result.choices[0].message.content)
        logger.info("Eval complete", scores=scores)
        return scores

    except Exception as e:
        logger.error("Evaluation failed", error=str(e))
        return {"accuracy": 0, "helpfulness": 0, "tone": 0, "reasoning": "eval failed"}
