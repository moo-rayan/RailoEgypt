"""
Admin endpoints for global chat management.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.config import settings
from app.core.database import AsyncSessionFactory
from app.services.global_chat_manager import GLOBAL_CHAT_RESOURCE, global_chat_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/global-chat", tags=["Admin Global Chat"])

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _q(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _quuid(value: str | None) -> str:
    if not value or not _UUID_RE.match(str(value)):
        return "NULL"
    return "'" + str(value) + "'::uuid"


class AdminMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)
    admin_name: str = Field("مشرف", max_length=30)
    reply_to: dict | None = None


class ToggleGlobalChatRequest(BaseModel):
    enabled: bool


class ChatBanRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    reason: str = Field("", max_length=300)
    ban_type: str = Field("temporary", pattern="^(temporary|permanent)$")
    duration_hours: int = Field(24, ge=1, le=8760)


@router.get("/messages", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_global_chat_messages(
    offset: int = Query(0, ge=0),
    limit: int = Query(80, ge=1, le=200),
):
    messages = await global_chat_manager.get_messages(offset=offset, limit=limit)
    enabled = await global_chat_manager.is_chat_enabled()
    return {
        "messages": list(reversed(messages)),
        "total": len(messages),
        "chat_enabled": enabled,
        "online_users": global_chat_manager.get_online_user_count(),
    }


@router.websocket("/ws")
async def admin_global_chat_ws(
    ws: WebSocket,
    admin_key: str = Query(...),
):
    if admin_key != settings.admin_api_key:
        await ws.close(code=4003, reason="Invalid admin key")
        return

    await ws.accept()
    observer_id = f"admin_{uuid.uuid4().hex[:8]}"
    logger.debug("🔭 [global] Admin chat observer connected: %s", observer_id)

    try:
        await global_chat_manager.add_admin_observer(observer_id, ws)
        messages = await global_chat_manager.get_messages(limit=80)
        enabled = await global_chat_manager.is_chat_enabled()

        await ws.send_json(
            {
                "type": "init",
                "data": {
                    "messages": list(reversed(messages)),
                    "chat_enabled": enabled,
                    "online_users": global_chat_manager.get_online_user_count(),
                },
            }
        )

        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif data.get("type") == "admin_message":
                result = await global_chat_manager.process_admin_message(
                    text_value=data.get("text", ""),
                    admin_name=data.get("admin_name", "مشرف"),
                    reply_to=data.get("reply_to") if isinstance(data.get("reply_to"), dict) else None,
                )
                if not result.get("ok"):
                    await ws.send_json({"type": "error", "data": result})

    except WebSocketDisconnect:
        logger.debug("🔭 [global] Admin chat observer disconnected: %s", observer_id)
    except Exception as exc:
        logger.error("🔭 [global] Admin chat observer error: %s: %s", observer_id, exc)
    finally:
        await global_chat_manager.remove_admin_observer(observer_id)


@router.get("/status", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_global_chat_status():
    return {
        "chat_enabled": await global_chat_manager.is_chat_enabled(),
        "online_users": global_chat_manager.get_online_user_count(),
        "message_count": await global_chat_manager.get_message_count(),
    }


@router.post("/send", dependencies=[Depends(get_admin_or_legacy_key)])
async def send_admin_message(body: AdminMessageRequest):
    result = await global_chat_manager.process_admin_message(
        text_value=body.text,
        admin_name=body.admin_name,
        reply_to=body.reply_to,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown"))
    return result


@router.post("/toggle", dependencies=[Depends(require_fulladmin)])
async def toggle_global_chat(body: ToggleGlobalChatRequest):
    await global_chat_manager.set_chat_enabled(body.enabled)
    return {"ok": True, "chat_enabled": body.enabled}


@router.delete("/messages/{message_id}", dependencies=[Depends(require_fulladmin)])
async def delete_global_chat_message(message_id: str):
    result = await global_chat_manager.delete_message(message_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "unknown"))
    return result


@router.delete("/clear", dependencies=[Depends(require_fulladmin)])
async def clear_global_chat():
    result = await global_chat_manager.clear_chat()
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
    return result


@router.get("/reports", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_global_chat_reports(
    report_status: str = Query("pending", pattern="^(pending|reviewed|dismissed|all)$"),
    limit: int = Query(50, ge=1, le=200),
):
    try:
        valid_statuses = {"pending", "reviewed", "dismissed"}
        conditions = [f"r.train_id = {_q(GLOBAL_CHAT_RESOURCE)}"]
        if report_status in valid_statuses:
            conditions.append(f"r.status = '{report_status}'")
        where = "WHERE " + " AND ".join(conditions)
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        r.id, r.reporter_id, r.reported_user_id, r.train_id,
                        r.message_id, r.message_text, r.report_reason,
                        r.status, r.admin_notes, r.created_at,
                        p.display_name AS reported_user_name,
                        p.avatar_url AS reported_user_avatar
                    FROM "EgRailway".chat_reports r
                    LEFT JOIN "EgRailway".profiles p ON p.id = r.reported_user_id
                    {where}
                    ORDER BY r.created_at DESC
                    LIMIT {int(limit)}
                    """
                )
            )
            rows = result.mappings().all()

        reports = [
            {
                "id": str(row["id"]),
                "reporter_id": str(row["reporter_id"]),
                "reported_user_id": str(row["reported_user_id"]),
                "reported_user_name": row["reported_user_name"] or "",
                "reported_user_avatar": row["reported_user_avatar"] or "",
                "train_id": row["train_id"],
                "message_id": row["message_id"],
                "message_text": row["message_text"],
                "report_reason": row["report_reason"] or "",
                "status": row["status"],
                "admin_notes": row["admin_notes"] or "",
                "created_at": row["created_at"].isoformat() if row["created_at"] else "",
            }
            for row in rows
        ]
        return {"total": len(reports), "reports": reports}
    except Exception as exc:
        logger.error("Failed to get global chat reports: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch reports")


@router.post("/ban", dependencies=[Depends(require_fulladmin)])
async def ban_global_chat_user(body: ChatBanRequest):
    try:
        expires_at = None
        if body.ban_type == "temporary":
            expires_at = datetime.now(timezone.utc) + timedelta(hours=body.duration_hours)

        expires_sql = f"'{expires_at.isoformat()}'::timestamptz" if expires_at else "NULL"
        safe_ban_type = body.ban_type if body.ban_type in {"temporary", "permanent"} else "temporary"
        reason = body.reason or "محظور بواسطة المشرف"

        async with AsyncSessionFactory() as session:
            await session.execute(
                text(
                    'UPDATE "EgRailway".chat_bans '
                    "SET is_active = false, updated_at = now() "
                    f"WHERE user_id = {_quuid(body.user_id)} AND is_active = true"
                )
            )
            await session.execute(
                text(
                    'INSERT INTO "EgRailway".chat_bans '
                    "(user_id, banned_by, reason, ban_type, expires_at, is_active) VALUES "
                    f"({_quuid(body.user_id)}, {_quuid(body.user_id)}, {_q(reason)}, "
                    f"'{safe_ban_type}', {expires_sql}, true)"
                )
            )
            await session.commit()

        return {
            "ok": True,
            "user_id": body.user_id,
            "ban_type": safe_ban_type,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
    except Exception as exc:
        logger.error("Failed to ban global chat user: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to ban user")
