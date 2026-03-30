import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None

# ── In-memory micro-cache ────────────────────────────────────────────────────
# Caches recent Redis GET results for a short TTL to avoid hammering Redis
# with hundreds of identical lookups per second (e.g. train_pos:* keys).
# A sentinel is used to distinguish "cached None" from "not cached".
_MISS = object()
_local_cache: dict[str, tuple[Any, float]] = {}
_LOCAL_TTL = 2.0        # seconds — safe because position data refreshes every 30s
_LOCAL_MAX_SIZE = 5_000


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
            max_connections=30,
            retry_on_timeout=True,
        )
    return _redis


async def cache_get(key: str) -> Any | None:
    now = time.monotonic()

    # ── Local cache hit ──
    cached = _local_cache.get(key)
    if cached is not None:
        value, expires = cached
        if now < expires:
            return value  # may be None (negative cache)
        _local_cache.pop(key, None)

    # ── Redis fetch ──
    try:
        r = await get_redis()
        raw = await r.get(key)
        result = json.loads(raw) if raw is not None else None
    except Exception as exc:
        logger.warning("Redis cache_get failed for '%s': %s", key, exc)
        return None

    # ── Store in local cache (including None for negative caching) ──
    _local_cache[key] = (result, now + _LOCAL_TTL)
    if len(_local_cache) > _LOCAL_MAX_SIZE:
        _evict_local_cache(now)

    return result


def _evict_local_cache(now: float) -> None:
    """Remove expired entries from the local micro-cache."""
    expired = [k for k, (_, exp) in _local_cache.items() if now >= exp]
    for k in expired:
        _local_cache.pop(k, None)


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        r = await get_redis()
        ttl = ttl or settings.cache_ttl_seconds
        await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        # Update local cache so subsequent reads see the new value immediately
        _local_cache[key] = (value, time.monotonic() + _LOCAL_TTL)
    except Exception as exc:
        logger.warning("Redis cache_set failed for '%s': %s", key, exc)


async def cache_delete(key: str) -> None:
    try:
        r = await get_redis()
        await r.delete(key)
        _local_cache.pop(key, None)
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
