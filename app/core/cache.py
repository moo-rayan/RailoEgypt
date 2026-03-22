import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=60,
            socket_keepalive=True,
            health_check_interval=30,
            max_connections=10,
            retry_on_timeout=True,
        )
    return _redis


async def cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis cache_get failed for '%s': %s", key, exc)
        return None


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        r = await get_redis()
        ttl = ttl or settings.cache_ttl_seconds
        await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Redis cache_set failed for '%s': %s", key, exc)


async def cache_delete(key: str) -> None:
    try:
        r = await get_redis()
        await r.delete(key)
    except Exception as exc:
        logger.warning("Redis cache_delete failed for '%s': %s", key, exc)


async def cache_delete_pattern(pattern: str) -> None:
    try:
        r = await get_redis()
        keys = await r.keys(pattern)
        if keys:
            await r.delete(*keys)
    except Exception as exc:
        logger.warning("Redis cache_delete_pattern failed for '%s': %s", pattern, exc)
