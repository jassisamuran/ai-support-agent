import redis.asyncio as aioredis
from app.config import settings

redis = aioredis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
)
