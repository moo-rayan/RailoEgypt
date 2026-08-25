from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import text

from app.core.cache import get_redis
from app.core.database import AsyncSessionFactory
from app.services import fcm_service
from app.services.chat_report_service import check_user_banned

logger = logging.getLogger(__name__)

GLOBAL_CHAT_RESOURCE = "global_chat"
GLOBAL_CHAT_TOPIC = "global_chat"

_MAX_MESSAGE_LENGTH = 150
_RATE_LIMIT_SECONDS = 5
_MAX_MESSAGES_STORED = 500
_RATE_KEY = "gchat:rate:{user_id}"
_PUBSUB_CHANNEL = "gchat:events"
_SETTINGS_KEY = "enabled"

VALID_MESSAGE_TYPES = {"normal"}
VALID_ADMIN_NAMES = {"مشرف", "مسؤول"}

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTISPACE_RE = re.compile(r" {3,}")


def sanitize_message(text_value: str) -> str:
    text_value = text_value.strip()
    text_value = _CONTROL_RE.sub("", text_value)
    text_value = html.escape(text_value, quote=True)
    text_value = _MULTISPACE_RE.sub("  ", text_value)
    return text_value[:_MAX_MESSAGE_LENGTH]


def normalize_admin_name(name: str | None) -> str:
    clean_name = sanitize_message(name or "").strip()
    if clean_name == "المشرف":
        clean_name = "مشرف"
    return clean_name if clean_name in VALID_ADMIN_NAMES else "مشرف"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


@dataclass
class GlobalChatRoom:
    connections: dict[str, WebSocket] = field(default_factory=dict)
    admin_observers: dict[str, WebSocket] = field(default_factory=dict)


class GlobalChatManager:
    def __init__(self) -> None:
        self._room = GlobalChatRoom()
        self._source_id = uuid.uuid4().hex
        self._pubsub_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._pubsub_task is None or self._pubsub_task.done():
            self._pubsub_task = asyncio.create_task(self._redis_pubsub_loop())

    async def stop(self) -> None:
        if self._pubsub_task is None:
            return
        self._pubsub_task.cancel()
        try:
            await self._pubsub_task
        except asyncio.CancelledError:
            pass
        self._pubsub_task = None

    async def join(self, user_id: str, ws: WebSocket) -> None:
        self._room.connections[user_id] = ws
        logger.debug("💬+ [global] User %s joined global chat (total: %d)", user_id[:8], len(self._room.connections))

    async def leave(self, user_id: str, ws: WebSocket) -> None:
        current_ws = self._room.connections.get(user_id)
        if current_ws is ws:
            self._room.connections.pop(user_id, None)
            logger.debug("💬- [global] User %s left global chat (total: %d)", user_id[:8], len(self._room.connections))

    async def add_admin_observer(self, observer_id: str, ws: WebSocket) -> None:
        self._room.admin_observers[observer_id] = ws
        logger.debug("🔭 [global] Admin observer %s joined global chat", observer_id[:8])

    async def remove_admin_observer(self, observer_id: str) -> None:
        self._room.admin_observers.pop(observer_id, None)
        logger.debug("🔭 [global] Admin observer %s left global chat", observer_id[:8])

    async def check_rate_limit(self, user_id: str) -> bool:
        try:
            r = await get_redis()
            key = _RATE_KEY.format(user_id=user_id)
            if await r.exists(key):
                return False
            await r.setex(key, _RATE_LIMIT_SECONDS, "1")
            return True
        except Exception as exc:
            logger.warning("Global chat rate limit check failed: %s", exc)
            return True

    async def get_messages(
        self,
        offset: int = 0,
        limit: int = 50,
        current_user_id: str | None = None,
    ) -> list[dict]:
        user_uuid = _uuid_or_none(current_user_id)
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT
                            m.id::text AS id,
                            m.user_id::text AS user_id,
                            m.user_name,
                            m.user_avatar,
                            m.text,
                            m.message_type AS type,
                            m.is_admin,
                            m.reply_to_message_id::text AS reply_to_message_id,
                            m.reply_to_user_name,
                            m.reply_to_text,
                            m.created_at,
                            COALESCE(rc.love_count, 0)::int AS love_count,
                            CASE
                                WHEN CAST(:current_user_id AS uuid) IS NULL THEN false
                                ELSE EXISTS (
                                    SELECT 1
                                    FROM "EgRailway".global_chat_reactions r
                                    WHERE r.message_id = m.id
                                      AND r.user_id = CAST(:current_user_id AS uuid)
                                      AND r.reaction_type = 'love'
                                )
                            END AS loved_by_me
                        FROM "EgRailway".global_chat_messages m
                        LEFT JOIN (
                            SELECT message_id, COUNT(*) AS love_count
                            FROM "EgRailway".global_chat_reactions
                            WHERE reaction_type = 'love'
                            GROUP BY message_id
                        ) rc ON rc.message_id = m.id
                        WHERE m.is_deleted = false
                        ORDER BY m.created_at DESC
                        OFFSET :offset
                        LIMIT :limit
                        """
                    ),
                    {
                        "current_user_id": user_uuid,
                        "offset": max(0, offset),
                        "limit": min(max(1, limit), 100),
                    },
                )
                return [self._serialize_message(row) for row in result.mappings().all()]
        except Exception as exc:
            logger.error("Failed to get global chat messages: %s", exc)
            return []

    async def get_message_count(self) -> int:
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM "EgRailway".global_chat_messages
                        WHERE is_deleted = false
                        """
                    )
                )
                return int(result.scalar() or 0)
        except Exception as exc:
            logger.error("Failed to count global chat messages: %s", exc)
            return 0

    async def is_chat_enabled(self) -> bool:
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT COALESCE((value->>'enabled')::boolean, true)
                        FROM "EgRailway".global_chat_settings
                        WHERE key = :key
                        """
                    ),
                    {"key": _SETTINGS_KEY},
                )
                value = result.scalar()
                return True if value is None else bool(value)
        except Exception as exc:
            logger.warning("Failed to read global chat enabled setting: %s", exc)
            return True

    async def set_chat_enabled(self, enabled: bool) -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO "EgRailway".global_chat_settings (key, value, updated_at)
                    VALUES (:key, jsonb_build_object('enabled', :enabled), now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = now()
                    """
                ),
                {"key": _SETTINGS_KEY, "enabled": enabled},
            )
            await session.commit()

        await self.broadcast_event(
            {
                "type": "status_changed",
                "data": {
                    "chat_enabled": enabled,
                    "text": "تم تفعيل الشات من جديد" if enabled else "تم إيقاف الشات مؤقتاً بواسطة المشرف",
                    "timestamp": _iso_now(),
                },
            }
        )
        logger.info("🔊 [global] Chat enabled=%s", enabled)

    async def process_message(
        self,
        user_id: str,
        user_name: str,
        user_avatar: str,
        text_value: str,
        msg_type: str = "normal",
        reply_to: dict | None = None,
    ) -> dict:
        if not await self.is_chat_enabled():
            return {"ok": False, "error": "chat_disabled", "message_ar": "الشات متوقف حالياً"}

        ban_info = await check_user_banned(user_id)
        if ban_info.get("banned"):
            return {
                "ok": False,
                "error": "banned",
                "reason": ban_info.get("reason", ""),
                "expires_at": ban_info.get("expires_at"),
                "ban_type": ban_info.get("ban_type", "temporary"),
            }

        if msg_type not in VALID_MESSAGE_TYPES:
            return {"ok": False, "error": "invalid_type"}

        if not text_value or not text_value.strip():
            return {"ok": False, "error": "empty_message"}
        if len(text_value) > _MAX_MESSAGE_LENGTH + 10:
            return {"ok": False, "error": "too_long", "max": _MAX_MESSAGE_LENGTH}

        if not await self.check_rate_limit(user_id):
            return {"ok": False, "error": "rate_limited", "wait_seconds": _RATE_LIMIT_SECONDS}

        clean_text = sanitize_message(text_value)
        if not clean_text:
            return {"ok": False, "error": "empty_after_sanitize"}

        safe_avatar = user_avatar[:500] if user_avatar.startswith(("https://", "http://")) else ""
        message = await self._insert_message(
            user_id=user_id,
            user_name=sanitize_message(user_name)[:30] or "مجهول",
            user_avatar=safe_avatar,
            text_value=clean_text,
            msg_type=msg_type,
            is_admin=False,
            reply_to=reply_to,
        )

        await self.broadcast_event({"type": "chat_message", "data": message})
        await self._send_fcm(message)
        await self._send_reply_fcm(message, sender_user_id=user_id)
        await self._trim_old_messages()
        return {"ok": True, "message": message}

    async def process_admin_message(
        self,
        text_value: str,
        admin_name: str = "مشرف",
        reply_to: dict | None = None,
    ) -> dict:
        if not text_value or not text_value.strip():
            return {"ok": False, "error": "empty_message"}
        clean_text = sanitize_message(text_value)
        if not clean_text:
            return {"ok": False, "error": "empty_after_sanitize"}

        message = await self._insert_message(
            user_id=None,
            user_name=normalize_admin_name(admin_name),
            user_avatar="",
            text_value=clean_text,
            msg_type="admin",
            is_admin=True,
            reply_to=reply_to,
        )
        await self.broadcast_event({"type": "chat_message", "data": message})
        await self._send_fcm(message)
        await self._send_reply_fcm(message, sender_user_id=None)
        return {"ok": True, "message": message}

    async def toggle_love(self, message_id: str, user_id: str) -> dict:
        msg_uuid = _uuid_or_none(message_id)
        user_uuid = _uuid_or_none(user_id)
        if msg_uuid is None or user_uuid is None:
            return {"ok": False, "error": "invalid_id"}

        try:
            async with AsyncSessionFactory() as session:
                exists = await session.execute(
                    text(
                        """
                        SELECT id
                        FROM "EgRailway".global_chat_messages
                        WHERE id = CAST(:message_id AS uuid)
                          AND is_deleted = false
                        """
                    ),
                    {"message_id": msg_uuid},
                )
                if exists.first() is None:
                    return {"ok": False, "error": "message_not_found"}

                deleted = await session.execute(
                    text(
                        """
                        DELETE FROM "EgRailway".global_chat_reactions
                        WHERE message_id = CAST(:message_id AS uuid)
                          AND user_id = CAST(:user_id AS uuid)
                          AND reaction_type = 'love'
                        RETURNING id
                        """
                    ),
                    {"message_id": msg_uuid, "user_id": user_uuid},
                )
                deleted_row = deleted.first()
                loved = deleted_row is None

                if loved:
                    await session.execute(
                        text(
                            """
                            INSERT INTO "EgRailway".global_chat_reactions
                                (message_id, user_id, reaction_type)
                            VALUES
                                (CAST(:message_id AS uuid), CAST(:user_id AS uuid), 'love')
                            ON CONFLICT (message_id, user_id, reaction_type) DO NOTHING
                            """
                        ),
                        {"message_id": msg_uuid, "user_id": user_uuid},
                    )

                count_result = await session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM "EgRailway".global_chat_reactions
                        WHERE message_id = CAST(:message_id AS uuid)
                          AND reaction_type = 'love'
                        """
                    ),
                    {"message_id": msg_uuid},
                )
                love_count = int(count_result.scalar() or 0)
                await session.commit()

            event = {
                "type": "message_reaction",
                "data": {
                    "message_id": msg_uuid,
                    "reaction": "love",
                    "love_count": love_count,
                    "user_id": user_uuid,
                    "loved": loved,
                },
            }
            await self.broadcast_event(event)
            return {"ok": True, **event["data"]}
        except Exception as exc:
            logger.error("Failed to toggle global chat reaction: %s", exc)
            return {"ok": False, "error": "internal_error"}

    async def delete_message(self, message_id: str) -> dict:
        msg_uuid = _uuid_or_none(message_id)
        if msg_uuid is None:
            return {"ok": False, "error": "invalid_id"}
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    text(
                        """
                        UPDATE "EgRailway".global_chat_messages
                        SET is_deleted = true,
                            deleted_at = now(),
                            updated_at = now()
                        WHERE id = CAST(:message_id AS uuid)
                          AND is_deleted = false
                        RETURNING id::text AS id
                        """
                    ),
                    {"message_id": msg_uuid},
                )
                row = result.mappings().first()
                await session.commit()

            if row is None:
                return {"ok": False, "error": "message_not_found"}

            await self.broadcast_event(
                {"type": "message_deleted", "data": {"message_id": msg_uuid}}
            )
            return {"ok": True, "message_id": msg_uuid}
        except Exception as exc:
            logger.error("Failed to delete global chat message: %s", exc)
            return {"ok": False, "error": "internal_error"}

    async def clear_chat(self) -> dict:
        try:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    text(
                        """
                        UPDATE "EgRailway".global_chat_messages
                        SET is_deleted = true,
                            deleted_at = COALESCE(deleted_at, now()),
                            updated_at = now()
                        WHERE is_deleted = false
                        """
                    )
                )
                await session.commit()
            await self.broadcast_event({"type": "chat_cleared", "data": {"scope": "global"}})
            return {"ok": True}
        except Exception as exc:
            logger.error("Failed to clear global chat: %s", exc)
            return {"ok": False, "error": "internal_error"}

    def get_online_user_count(self) -> int:
        return len(self._room.connections)

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        await self._broadcast_local(event)
        try:
            r = await get_redis()
            await r.publish(
                _PUBSUB_CHANNEL,
                json.dumps({"source": self._source_id, "event": event}, ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("Global chat Redis publish failed: %s", exc)

    async def _broadcast_local(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)

        dead_users: list[str] = []
        for uid, ws in list(self._room.connections.items()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_users.append(uid)
        for uid in dead_users:
            self._room.connections.pop(uid, None)

        dead_admins: list[str] = []
        for oid, ws in list(self._room.admin_observers.items()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_admins.append(oid)
        for oid in dead_admins:
            self._room.admin_observers.pop(oid, None)

    async def _redis_pubsub_loop(self) -> None:
        while True:
            pubsub = None
            try:
                r = await get_redis()
                pubsub = r.pubsub()
                await pubsub.subscribe(_PUBSUB_CHANNEL)
                async for item in pubsub.listen():
                    if item.get("type") != "message":
                        continue
                    raw = item.get("data")
                    data = json.loads(raw if isinstance(raw, str) else raw.decode())
                    if data.get("source") == self._source_id:
                        continue
                    event = data.get("event")
                    if isinstance(event, dict):
                        await self._broadcast_local(event)
            except asyncio.CancelledError:
                if pubsub is not None:
                    await pubsub.close()
                raise
            except Exception as exc:
                logger.warning("Global chat Redis pubsub loop error: %s", exc)
                if pubsub is not None:
                    try:
                        await pubsub.close()
                    except Exception:
                        pass
                await asyncio.sleep(3)

    async def _insert_message(
        self,
        user_id: str | None,
        user_name: str,
        user_avatar: str,
        text_value: str,
        msg_type: str,
        is_admin: bool,
        reply_to: dict | None,
    ) -> dict:
        reply_message_id = ""
        reply_user_name = ""
        reply_text = ""

        if isinstance(reply_to, dict):
            reply_message_id = _uuid_or_none(str(reply_to.get("message_id", ""))) or ""
            reply_user_name = sanitize_message(str(reply_to.get("user_name", "")))[:30]
            reply_text = sanitize_message(str(reply_to.get("text", "")))[:80]

        async with AsyncSessionFactory() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO "EgRailway".global_chat_messages (
                        user_id,
                        user_name,
                        user_avatar,
                        text,
                        message_type,
                        is_admin,
                        reply_to_message_id,
                        reply_to_user_name,
                        reply_to_text
                    ) VALUES (
                        CAST(:user_id AS uuid),
                        :user_name,
                        :user_avatar,
                        :text,
                        :message_type,
                        :is_admin,
                        CAST(:reply_to_message_id AS uuid),
                        :reply_to_user_name,
                        :reply_to_text
                    )
                    RETURNING
                        id::text AS id,
                        user_id::text AS user_id,
                        user_name,
                        user_avatar,
                        text,
                        message_type AS type,
                        is_admin,
                        reply_to_message_id::text AS reply_to_message_id,
                        reply_to_user_name,
                        reply_to_text,
                        created_at,
                        0::int AS love_count,
                        false AS loved_by_me
                    """
                ),
                {
                    "user_id": _uuid_or_none(user_id),
                    "user_name": user_name,
                    "user_avatar": user_avatar,
                    "text": text_value,
                    "message_type": msg_type,
                    "is_admin": is_admin,
                    "reply_to_message_id": reply_message_id or None,
                    "reply_to_user_name": reply_user_name or None,
                    "reply_to_text": reply_text or None,
                },
            )
            row = result.mappings().one()
            await session.commit()

        return self._serialize_message(row)

    async def _trim_old_messages(self) -> None:
        try:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    text(
                        """
                        WITH visible AS (
                            SELECT id
                            FROM "EgRailway".global_chat_messages
                            WHERE is_deleted = false
                            ORDER BY created_at DESC
                            OFFSET :max_messages
                        )
                        UPDATE "EgRailway".global_chat_messages m
                        SET is_deleted = true,
                            deleted_at = now(),
                            updated_at = now()
                        FROM visible
                        WHERE m.id = visible.id
                        """
                    ),
                    {"max_messages": _MAX_MESSAGES_STORED},
                )
                await session.commit()
        except Exception as exc:
            logger.warning("Failed to trim old global chat messages: %s", exc)

    async def _send_fcm(self, message: dict) -> None:
        try:
            await fcm_service.send_to_topic(
                topic=GLOBAL_CHAT_TOPIC,
                data={
                    "type": "global_chat_message",
                    "sender_id": message.get("user_id", ""),
                    "sender_name": message.get("user_name", "مجهول"),
                    "text": str(message.get("text", ""))[:100],
                    "message_id": message.get("id", ""),
                },
            )
        except Exception as exc:
            logger.warning("Global chat FCM push failed: %s", exc)

    async def _send_reply_fcm(self, message: dict, sender_user_id: str | None) -> None:
        reply_message_id = _uuid_or_none(message.get("reply_to_message_id"))
        if reply_message_id is None:
            return

        try:
            async with AsyncSessionFactory() as session:
                owner_result = await session.execute(
                    text(
                        """
                        SELECT user_id::text
                        FROM "EgRailway".global_chat_messages
                        WHERE id = CAST(:message_id AS uuid)
                          AND is_deleted = false
                        LIMIT 1
                        """
                    ),
                    {"message_id": reply_message_id},
                )
                target_user_id = owner_result.scalar()

                if not target_user_id or target_user_id == sender_user_id:
                    return

                token_result = await session.execute(
                    text(
                        """
                        SELECT fcm_token
                        FROM "EgRailway".device_tokens
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": target_user_id},
                )
                tokens = [row[0] for row in token_result.all() if row[0]]

            if not tokens:
                return

            await fcm_service.send_data_to_tokens(
                tokens=tokens,
                data={
                    "type": "global_chat_message",
                    "reply_direct": "1",
                    "sender_id": message.get("user_id", ""),
                    "sender_name": message.get("user_name", "مجهول"),
                    "text": str(message.get("text", ""))[:100],
                    "message_id": message.get("id", ""),
                    "reply_to_message_id": reply_message_id,
                },
            )
        except Exception as exc:
            logger.warning("Global chat reply FCM failed: %s", exc)

    @staticmethod
    def _serialize_message(row: Any) -> dict:
        data = dict(row)
        created_at = data.get("created_at")
        timestamp = created_at.isoformat() if hasattr(created_at, "isoformat") else _iso_now()
        if timestamp.endswith("+00:00"):
            timestamp = timestamp.replace("+00:00", "Z")
        user_id = data.get("user_id")
        return {
            "id": str(data.get("id", "")),
            "user_id": str(user_id) if user_id else "admin",
            "user_name": data.get("user_name") or "مجهول",
            "user_avatar": data.get("user_avatar") or "",
            "text": data.get("text") or "",
            "type": data.get("type") or "normal",
            "pinned": False,
            "is_pinned": False,
            "is_admin": bool(data.get("is_admin")),
            "reply_to_message_id": data.get("reply_to_message_id") or None,
            "reply_to_user_name": data.get("reply_to_user_name") or None,
            "reply_to_text": data.get("reply_to_text") or None,
            "love_count": int(data.get("love_count") or 0),
            "loved_by_me": bool(data.get("loved_by_me")),
            "timestamp": timestamp,
        }


global_chat_manager = GlobalChatManager()
