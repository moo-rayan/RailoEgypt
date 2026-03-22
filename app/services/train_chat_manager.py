"""
Train Chat Manager – Real-time chat rooms per train.

Redis storage:
  tchat:{train_id}:msgs     — List of JSON messages (newest first)
  tchat:{train_id}:pinned   — List of pinned messages (lost/found items)
  tchat:{train_id}:count    — Total message counter
  tchat:{train_id}:rate:{uid} — Rate-limit key (TTL 5s)

All keys expire after 24 hours (train journey lifecycle).
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket

from app.core.cache import get_redis
from app.services.chat_report_service import check_user_banned

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_MESSAGE_LENGTH = 150
_RATE_LIMIT_SECONDS = 5
_MAX_MESSAGES_STORED = 200        # Keep last 200 messages per train
_MAX_PINNED_MESSAGES = 10         # Max pinned messages per train
_CHAT_TTL_SECONDS = 86400         # 24 hours
_MSG_KEY = "tchat:{train_id}:msgs"
_PIN_KEY = "tchat:{train_id}:pinned"
_COUNT_KEY = "tchat:{train_id}:count"
_RATE_KEY = "tchat:{train_id}:rate:{user_id}"
_DISABLED_KEY = "tchat:{train_id}:disabled"

# ── Message types ─────────────────────────────────────────────────────────────

VALID_MESSAGE_TYPES = {"normal", "lost_item", "found_item"}


# ── Sanitization ──────────────────────────────────────────────────────────────

# Control characters (except newline/tab)
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
# Excessive whitespace
_MULTISPACE_RE = re.compile(r' {3,}')


def sanitize_message(text: str) -> str:
    """
    Sanitize user input:
    - Strip leading/trailing whitespace
    - Remove control characters
    - HTML-encode to prevent XSS
    - Collapse excessive spaces
    - Enforce max length
    """
    text = text.strip()
    text = _CONTROL_RE.sub('', text)
    text = html.escape(text, quote=True)
    text = _MULTISPACE_RE.sub('  ', text)
    return text[:_MAX_MESSAGE_LENGTH]


# ── Chat Room ─────────────────────────────────────────────────────────────────

@dataclass
class ChatRoom:
    """In-memory state for a train chat room."""
    train_id: str
    connections: dict[str, WebSocket] = field(default_factory=dict)  # user_id → ws
    admin_observers: dict[str, WebSocket] = field(default_factory=dict)  # observer_id → ws


class TrainChatManager:
    """Manages real-time chat rooms for trains with Redis persistence."""

    def __init__(self) -> None:
        self._rooms: dict[str, ChatRoom] = {}
        self._disabled_cache: set[str] = set()  # in-memory cache of disabled trains

    def _get_or_create_room(self, train_id: str) -> ChatRoom:
        if train_id not in self._rooms:
            self._rooms[train_id] = ChatRoom(train_id=train_id)
        return self._rooms[train_id]

    # ── Connection management ─────────────────────────────────────────────

    async def join(self, train_id: str, user_id: str, ws: WebSocket) -> None:
        """Add a user to the chat room."""
        room = self._get_or_create_room(train_id)
        room.connections[user_id] = ws
        logger.info(
            "💬+ [%s] User %s joined chat (total: %d)",
            train_id, user_id[:8], len(room.connections),
        )

    async def leave(self, train_id: str, user_id: str) -> None:
        """Remove a user from the chat room."""
        room = self._rooms.get(train_id)
        if room:
            room.connections.pop(user_id, None)
            logger.info(
                "💬- [%s] User %s left chat (total: %d)",
                train_id, user_id[:8], len(room.connections),
            )
            if not room.connections:
                del self._rooms[train_id]

    # ── Rate limiting ─────────────────────────────────────────────────────

    async def check_rate_limit(self, train_id: str, user_id: str) -> bool:
        """
        Check if user can send a message (5s cooldown).
        Returns True if allowed, False if rate-limited.
        """
        try:
            r = await get_redis()
            key = _RATE_KEY.format(train_id=train_id, user_id=user_id)
            exists = await r.exists(key)
            if exists:
                return False
            await r.setex(key, _RATE_LIMIT_SECONDS, "1")
            return True
        except Exception as exc:
            logger.warning("Rate limit check failed: %s", exc)
            return True  # Allow on Redis failure

    # ── Message storage ───────────────────────────────────────────────────

    async def store_message(self, train_id: str, message: dict) -> None:
        """Store message in Redis list."""
        try:
            r = await get_redis()
            msg_key = _MSG_KEY.format(train_id=train_id)
            count_key = _COUNT_KEY.format(train_id=train_id)

            msg_json = json.dumps(message, ensure_ascii=False)
            
            pipe = r.pipeline()
            pipe.lpush(msg_key, msg_json)
            pipe.ltrim(msg_key, 0, _MAX_MESSAGES_STORED - 1)
            pipe.incr(count_key)
            pipe.expire(msg_key, _CHAT_TTL_SECONDS)
            pipe.expire(count_key, _CHAT_TTL_SECONDS)
            await pipe.execute()
            
        except Exception as exc:
            logger.error("Failed to store message: %s", exc)

    async def store_pinned(self, train_id: str, message: dict) -> None:
        """Store a pinned message (lost/found item)."""
        try:
            r = await get_redis()
            pin_key = _PIN_KEY.format(train_id=train_id)

            msg_json = json.dumps(message, ensure_ascii=False)
            
            pipe = r.pipeline()
            pipe.lpush(pin_key, msg_json)
            pipe.ltrim(pin_key, 0, _MAX_PINNED_MESSAGES - 1)
            pipe.expire(pin_key, _CHAT_TTL_SECONDS)
            await pipe.execute()

        except Exception as exc:
            logger.error("Failed to store pinned message: %s", exc)

    async def get_messages(
        self, train_id: str, offset: int = 0, limit: int = 50,
    ) -> list[dict]:
        """Get recent messages from Redis (newest first)."""
        try:
            r = await get_redis()
            key = _MSG_KEY.format(train_id=train_id)
            raw_list = await r.lrange(key, offset, offset + limit - 1)
            return [json.loads(m) for m in raw_list]
        except Exception as exc:
            logger.error("Failed to get messages: %s", exc)
            return []

    async def get_pinned(self, train_id: str) -> list[dict]:
        """Get pinned messages for a train."""
        try:
            r = await get_redis()
            key = _PIN_KEY.format(train_id=train_id)
            raw_list = await r.lrange(key, 0, _MAX_PINNED_MESSAGES - 1)
            return [json.loads(m) for m in raw_list]
        except Exception as exc:
            logger.error("Failed to get pinned messages: %s", exc)
            return []

    async def get_message_count(self, train_id: str) -> int:
        """Get total message count for a train."""
        try:
            r = await get_redis()
            key = _COUNT_KEY.format(train_id=train_id)
            count = await r.get(key)
            return int(count) if count else 0
        except Exception as exc:
            logger.error("Failed to get message count: %s", exc)
            return 0

    # ── Broadcasting ──────────────────────────────────────────────────────

    async def broadcast(
        self, train_id: str, message: dict, exclude_user: str | None = None,
    ) -> None:
        """Broadcast a message to all connected users in the room."""
        room = self._rooms.get(train_id)
        if not room:
            return

        payload = json.dumps(
            {"type": "chat_message", "data": message},
            ensure_ascii=False,
        )

        disconnected: list[str] = []
        for uid, ws in room.connections.items():
            if uid == exclude_user:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(uid)

        for uid in disconnected:
            room.connections.pop(uid, None)

        # Also send to admin observers
        dead_admins: list[str] = []
        for oid, ws in room.admin_observers.items():
            try:
                await ws.send_text(payload)
            except Exception:
                dead_admins.append(oid)
        for oid in dead_admins:
            room.admin_observers.pop(oid, None)

    async def broadcast_system(self, train_id: str, text: str) -> None:
        """Broadcast a system message to all users."""
        msg = {
            "type": "system",
            "data": {
                "id": str(uuid.uuid4()),
                "text": text,
                "timestamp": _iso_now(),
            },
        }
        room = self._rooms.get(train_id)
        if not room:
            return

        payload = json.dumps(msg, ensure_ascii=False)
        disconnected: list[str] = []
        for uid, ws in room.connections.items():
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(uid)
        for uid in disconnected:
            room.connections.pop(uid, None)

    # ── Process incoming message ──────────────────────────────────────────

    async def process_message(
        self,
        train_id: str,
        user_id: str,
        user_name: str,
        user_avatar: str,
        text: str,
        msg_type: str = "normal",
    ) -> dict:
        """
        Validate, sanitize, store and broadcast a chat message.
        Returns status dict.
        """
        # Check if chat is disabled for this train
        if not await self.is_chat_enabled(train_id):
            return {"ok": False, "error": "chat_disabled", "message_ar": "الشات متوقف حالياً"}

        # Check if user is banned
        ban_info = await check_user_banned(user_id)
        if ban_info.get("banned"):
            return {
                "ok": False,
                "error": "banned",
                "reason": ban_info.get("reason", ""),
                "expires_at": ban_info.get("expires_at"),
                "ban_type": ban_info.get("ban_type", "temporary"),
            }

        # Validate type
        if msg_type not in VALID_MESSAGE_TYPES:
            return {"ok": False, "error": "invalid_type"}

        # Validate length (raw check before sanitization)
        if not text or not text.strip():
            return {"ok": False, "error": "empty_message"}
        if len(text) > _MAX_MESSAGE_LENGTH + 10:  # Small tolerance for encoding
            return {"ok": False, "error": "too_long", "max": _MAX_MESSAGE_LENGTH}

        # Rate limit
        allowed = await self.check_rate_limit(train_id, user_id)
        if not allowed:
            return {
                "ok": False,
                "error": "rate_limited",
                "wait_seconds": _RATE_LIMIT_SECONDS,
            }

        # Sanitize
        clean_text = sanitize_message(text)
        if not clean_text:
            return {"ok": False, "error": "empty_after_sanitize"}

        # Build message object
        is_pinned = msg_type in ("lost_item", "found_item")
        # Validate avatar URL: only allow http(s) URLs
        safe_avatar = ""
        if user_avatar and user_avatar.startswith(("https://", "http://")):
            safe_avatar = user_avatar[:500]
        message = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "user_name": sanitize_message(user_name)[:30],
            "user_avatar": safe_avatar,
            "text": clean_text,
            "type": msg_type,
            "pinned": is_pinned,
            "timestamp": _iso_now(),
        }

        # Store
        await self.store_message(train_id, message)
        if is_pinned:
            await self.store_pinned(train_id, message)

        # Broadcast to all in room (including sender for confirmation)
        await self.broadcast(train_id, message)

        return {"ok": True, "message": message}

    # ── Admin observer management ────────────────────────────────────────

    async def add_admin_observer(self, train_id: str, observer_id: str, ws: WebSocket) -> None:
        """Add an admin observer to a chat room (read-only)."""
        room = self._get_or_create_room(train_id)
        room.admin_observers[observer_id] = ws
        logger.info("🔭 [%s] Admin observer %s joined chat", train_id, observer_id[:8])

    async def remove_admin_observer(self, train_id: str, observer_id: str) -> None:
        """Remove an admin observer from a chat room."""
        room = self._rooms.get(train_id)
        if room:
            room.admin_observers.pop(observer_id, None)
            logger.info("🔭 [%s] Admin observer %s left chat", train_id, observer_id[:8])

    # ── Chat toggle (enable/disable) ───────────────────────────────────────

    async def disable_chat(self, train_id: str) -> None:
        """Disable chat for a train. Persisted in Redis."""
        self._disabled_cache.add(train_id)
        try:
            r = await get_redis()
            key = _DISABLED_KEY.format(train_id=train_id)
            await r.set(key, "1")
            await r.expire(key, _CHAT_TTL_SECONDS)
        except Exception as exc:
            logger.warning("Failed to persist chat disable: %s", exc)
        # Broadcast system message
        await self.broadcast_system(train_id, "تم إيقاف الشات مؤقتاً بواسطة المشرف")
        logger.info("🔇 [%s] Chat DISABLED", train_id)

    async def enable_chat(self, train_id: str) -> None:
        """Enable chat for a train."""
        self._disabled_cache.discard(train_id)
        try:
            r = await get_redis()
            key = _DISABLED_KEY.format(train_id=train_id)
            await r.delete(key)
        except Exception as exc:
            logger.warning("Failed to persist chat enable: %s", exc)
        await self.broadcast_system(train_id, "تم تفعيل الشات من جديد")
        logger.info("🔊 [%s] Chat ENABLED", train_id)

    async def is_chat_enabled(self, train_id: str) -> bool:
        """Check if chat is enabled for a train."""
        if train_id in self._disabled_cache:
            return False
        try:
            r = await get_redis()
            key = _DISABLED_KEY.format(train_id=train_id)
            val = await r.get(key)
            if val:
                self._disabled_cache.add(train_id)
                return False
        except Exception:
            pass
        return True

    def get_room_user_count(self, train_id: str) -> int:
        """Get the number of connected users in a chat room."""
        room = self._rooms.get(train_id)
        return len(room.connections) if room else 0

    @property
    def active_rooms(self) -> int:
        return len(self._rooms)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Module singleton
train_chat_manager = TrainChatManager()
