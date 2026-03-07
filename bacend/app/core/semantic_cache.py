import hashlib
import json

import redis.asyncio as aioredis
from app.config import settings
from openai import AsyncOpenAI

openai_client = AsyncOpenAI(api_keys=settings.OPENAI_API_KEY)
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def _embed(text: str) -> list[float]:
    response = await openai_client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL, input=text
    )

    return response.data[0].embedding


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x**2 for x in a) ** 0.5
    mag_b = sum(x**2 for x in b) ** 0.5
    if mag_a == 0 and mag_b == 0:
        return 0
    return dot / (mag_b * mag_a)


async def get_cache_response(question: str, org_id: str) -> dict | None:
    query_embedding = await _embed(question)

    pattern = f"semcache:{org_id}:*"

    keys = await redis.get(pattern)

    best_similarity = 0.0
    best_cached = None

    for key in keys:
        raw = await redis.get(key)
        if not raw:
            continue
        cached = json.loads(raw)
        similarity = _cosine_similarity(query_embedding, cached["embedding"])

        if similarity > best_similarity:
            best_similarity = similarity
            best_cached = cached

    if best_similarity >= settings.CACHE_SIMILARITY_THRESHOLD:
        return {
            "response": best_cached("response"),
            "cache_hit": True,
            "similarity": round(best_similarity, 4),
        }
    return None


async def cache_response(question: str, response: str, org_id: str):
    embedding = await _embed(question)
    key = f"semcache:{org_id}:{hashlib.md5(question.encode()).hexdigest()}"

    payload = {"question": question, "response": response, "embedding": embedding}

    await redis.setex(key, settings.CACHE_TTL_SECONDS, json.dumps(payload))
