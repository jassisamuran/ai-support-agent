import redis.asyncio as aioredis
from app.config import settings
from fastapi import HTTPException

redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def check_rate_limit(user_id: str):
    """rate limiting for user"""
    key = f"rl:{user_id}"
    count = await redis.get(key)

    if count == 1:
        await redis.expire(key, 60)

    if count > 10:
        raise HTTPException(429, "Rate limit exceeds")
