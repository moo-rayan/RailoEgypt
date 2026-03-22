"""
Redis-backed contributor ban service.

Ban keys:  ban:contributor:{user_id}
Value:     JSON {reason, banned_at, expires_at, duration_minutes, banned_by}
TTL:       duration_minutes * 60  (or no TTL for permanent bans)
"""

import json
import logging
import time
from typing import Optional

from app.core.cache import get_redis

logger = logging.getLogger(__name__)

_BAN_PREFIX = "ban:contributor:"


async def ban_contributor(
    user_id: str,
    reason: str = "",
    duration_minutes: int = 0,
    banned_by: str = "admin",
) -> bool:
    """
    Ban a contributor.
    duration_minutes=0 means permanent ban.
    Returns True on success.
    """
    try:
        r = await get_redis()
        now = time.time()
        data = {
            "user_id": user_id,
            "reason": reason,
            "banned_at": now,
            "duration_minutes": duration_minutes,
            "expires_at": now + (duration_minutes * 60) if duration_minutes > 0 else None,
            "banned_by": banned_by,
        }
        key = f"{_BAN_PREFIX}{user_id}"
        raw = json.dumps(data, ensure_ascii=False)
        if duration_minutes > 0:
            await r.setex(key, duration_minutes * 60, raw)
        else:
            await r.set(key, raw)
        logger.info("🚫 Banned user %s for %s min (reason: %s)", user_id, duration_minutes or "permanent", reason)
        return True
    except Exception as exc:
        logger.error("Failed to ban user %s: %s", user_id, exc)
        return False


async def unban_contributor(user_id: str) -> bool:
    """Remove a ban. Returns True if a ban existed and was removed."""
    try:
        r = await get_redis()
        key = f"{_BAN_PREFIX}{user_id}"
        removed = await r.delete(key)
        if removed:
            logger.info("✅ Unbanned user %s", user_id)
        return bool(removed)
    except Exception as exc:
        logger.error("Failed to unban user %s: %s", user_id, exc)
        return False


async def is_banned(user_id: str) -> Optional[dict]:
    """
    Check if a user is banned.
    Returns ban info dict if banned, None if not banned.
    """
    try:
        r = await get_redis()
        raw = await r.get(f"{_BAN_PREFIX}{user_id}")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to check ban for %s: %s", user_id, exc)
        return None


async def list_bans() -> list[dict]:
    """List all currently banned contributors."""
    try:
        r = await get_redis()
        keys = await r.keys(f"{_BAN_PREFIX}*")
        bans = []
        for key in keys:
            raw = await r.get(key)
            if raw:
                bans.append(json.loads(raw))
        return bans
    except Exception as exc:
        logger.error("Failed to list bans: %s", exc)
        return []
