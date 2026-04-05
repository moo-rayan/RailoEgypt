"""
Admin endpoints for chat management.

All endpoints require admin authentication (Supabase JWT + is_admin).
Read endpoints: monitor + fulladmin. Write endpoints: fulladmin only.

Queries use inline literals (no bind parameters) to avoid prepared-statement
errors with pgbouncer in transaction mode.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.database import AsyncSessionFactory
from app.services.train_chat_manager import train_chat_manager
from sqlalchemy import text

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I,
)


def _q(val: str | None) -> str:
    """Escape a string value for a SQL literal."""
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


def _quuid(val: str | None) -> str:
    """Escape a UUID value."""
    if not val or not _UUID_RE.match(str(val)):
        return "NULL"
    return "'" + str(val) + "'::uuid"

router = APIRouter(prefix="/admin/chat", tags=["Admin Chat"])


# ── Get chat messages (REST) ─────────────────────────────────────────────────

@router.get("/{train_id}/messages", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_chat_messages(
    train_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """Get recent chat messages for a train (admin access)."""
    messages = await train_chat_manager.get_messages(train_id, offset=offset, limit=limit)
    enabled = await train_chat_manager.is_chat_enabled(train_id)
    user_count = train_chat_manager.get_room_user_count(train_id)
    return {
        "train_id": train_id,
        "messages": list(reversed(messages)),
        "total": len(messages),
        "chat_enabled": enabled,
        "online_users": user_count,
    }


# ── Admin WebSocket observer (real-time) ─────────────────────────────────────

@router.websocket("/{train_id}/ws")
async def admin_chat_ws(
    ws: WebSocket,
    train_id: str,
    admin_key: str = Query(...),
):
    """
    Admin WebSocket observer for train chat.
    Receives all chat messages in real-time (read-only).
    Auth via ?admin_key= query param.
    """
    if admin_key != settings.admin_api_key:
        await ws.close(code=4003, reason="Invalid admin key")
        return

    await ws.accept()
    observer_id = f"admin_{uuid.uuid4().hex[:8]}"
    logger.info("🔭 Admin chat observer connected: %s for train %s", observer_id, train_id)

    try:
        await train_chat_manager.add_admin_observer(train_id, observer_id, ws)

        # Send initial messages + status
        messages = await train_chat_manager.get_messages(train_id, limit=50)
        enabled = await train_chat_manager.is_chat_enabled(train_id)
        user_count = train_chat_manager.get_room_user_count(train_id)

        await ws.send_json({
            "type": "init",
            "data": {
                "messages": list(reversed(messages)),
                "chat_enabled": enabled,
                "online_users": user_count,
            },
        })

        # Listen for pings and admin messages
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif data.get("type") == "admin_message":
                    msg_text = data.get("text", "")
                    result = await train_chat_manager.process_admin_message(
                        train_id=train_id,
                        text=msg_text,
                        admin_name=data.get("admin_name", "المشرف"),
                    )
                    if not result.get("ok"):
                        await ws.send_json({"type": "error", "data": result})
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        logger.info("🔭 Admin chat observer disconnected: %s", observer_id)
    except Exception as exc:
        logger.error("🔭 Admin chat observer error: %s: %s", observer_id, exc)
    finally:
        await train_chat_manager.remove_admin_observer(train_id, observer_id)


# ── Admin send message (REST) ────────────────────────────────────────────────

class AdminMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)
    admin_name: str = Field("المشرف", max_length=30)


@router.post("/{train_id}/send", dependencies=[Depends(require_fulladmin)])
async def admin_send_message(train_id: str, body: AdminMessageRequest):
    """Send a message as admin to the train chat."""
    result = await train_chat_manager.process_admin_message(
        train_id=train_id,
        text=body.text,
        admin_name=body.admin_name,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown"))
    return result


# ── Toggle chat (enable/disable) ─────────────────────────────────────────────

class ToggleChatRequest(BaseModel):
    enabled: bool = Field(..., description="True to enable, False to disable")


@router.post("/{train_id}/toggle", dependencies=[Depends(require_fulladmin)])
async def toggle_chat(train_id: str, body: ToggleChatRequest):
    """Enable or disable chat for a specific train."""
    if body.enabled:
        await train_chat_manager.enable_chat(train_id)
    else:
        await train_chat_manager.disable_chat(train_id)

    return {
        "ok": True,
        "train_id": train_id,
        "chat_enabled": body.enabled,
    }


@router.get("/{train_id}/status", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_chat_status(train_id: str):
    """Get chat status for a train."""
    enabled = await train_chat_manager.is_chat_enabled(train_id)
    user_count = train_chat_manager.get_room_user_count(train_id)
    msg_count = await train_chat_manager.get_message_count(train_id)
    return {
        "train_id": train_id,
        "chat_enabled": enabled,
        "online_users": user_count,
        "message_count": msg_count,
    }


# ── Reports management ───────────────────────────────────────────────────────

@router.get("/reports/list", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_reports(
    train_id: str | None = Query(None),
    report_status: str = Query("pending", pattern="^(pending|reviewed|dismissed|all)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get chat reports, optionally filtered by train and status."""
    try:
        async with AsyncSessionFactory() as session:
            conditions: list[str] = []
            _VALID_STATUSES = {"pending", "reviewed", "dismissed"}

            if report_status != "all" and report_status in _VALID_STATUSES:
                conditions.append(f"r.status = '{report_status}'")
            if train_id:
                conditions.append(f"r.train_id = {_q(train_id)}")

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            safe_limit = int(limit)

            result = await session.execute(
                text(f"""
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
                    LIMIT {safe_limit}
                """),
            )
            rows = result.mappings().all()

            reports = []
            for row in rows:
                reports.append({
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
                })

            return {"total": len(reports), "reports": reports}

    except Exception as exc:
        logger.error("Failed to get reports: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch reports")


class ReviewReportRequest(BaseModel):
    status: str = Field(..., pattern="^(reviewed|dismissed)$")
    admin_notes: str = Field("", max_length=500)


@router.post("/reports/{report_id}/review", dependencies=[Depends(require_fulladmin)])
async def review_report(report_id: str, body: ReviewReportRequest):
    """Update a report's status."""
    try:
        _VALID_REVIEW = {"reviewed", "dismissed"}
        safe_status = body.status if body.status in _VALID_REVIEW else "reviewed"
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                text(
                    'UPDATE "EgRailway".chat_reports '
                    f"SET status = '{safe_status}', admin_notes = {_q(body.admin_notes)}, updated_at = now() "
                    f"WHERE id = {_quuid(report_id)} "
                    "RETURNING id"
                ),
            )
            updated = result.first()
            await session.commit()

            if not updated:
                raise HTTPException(status_code=404, detail="Report not found")

            return {"ok": True, "report_id": report_id, "status": body.status}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to review report: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to update report")


# ── Chat ban/unban ────────────────────────────────────────────────────────────

class ChatBanRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    reason: str = Field("", max_length=300)
    ban_type: str = Field("temporary", pattern="^(temporary|permanent)$")
    duration_hours: int = Field(24, ge=1, le=8760)  # max 1 year


@router.post("/ban", dependencies=[Depends(require_fulladmin)])
async def ban_chat_user(body: ChatBanRequest):
    """Ban a user from chat."""
    try:
        expires_at = None
        if body.ban_type == "temporary":
            expires_at = datetime.now(timezone.utc) + timedelta(hours=body.duration_hours)

        _VALID_BAN = {"temporary", "permanent"}
        safe_ban_type = body.ban_type if body.ban_type in _VALID_BAN else "temporary"
        expires_sql = f"'{expires_at.isoformat()}'::timestamptz" if expires_at else "NULL"

        async with AsyncSessionFactory() as session:
            # Deactivate existing bans first
            await session.execute(
                text(
                    'UPDATE "EgRailway".chat_bans '
                    "SET is_active = false, updated_at = now() "
                    f"WHERE user_id = {_quuid(body.user_id)} AND is_active = true"
                ),
            )

            # Insert new ban
            reason = body.reason or "محظور بواسطة المشرف"
            await session.execute(
                text(
                    'INSERT INTO "EgRailway".chat_bans '
                    "(user_id, banned_by, reason, ban_type, expires_at, is_active) VALUES "
                    f"({_quuid(body.user_id)}, {_quuid(body.user_id)}, {_q(reason)}, "
                    f"'{safe_ban_type}', {expires_sql}, true)"
                ),
            )
            await session.commit()

        logger.info("🚫 Chat ban: user=%s type=%s duration=%dh", body.user_id[:8], body.ban_type, body.duration_hours)
        return {
            "ok": True,
            "user_id": body.user_id,
            "ban_type": body.ban_type,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

    except Exception as exc:
        logger.error("Failed to ban chat user: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to ban user")


class ChatUnbanRequest(BaseModel):
    user_id: str = Field(..., min_length=1)


@router.post("/unban", dependencies=[Depends(require_fulladmin)])
async def unban_chat_user(body: ChatUnbanRequest):
    """Unban a user from chat."""
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                text(
                    'UPDATE "EgRailway".chat_bans '
                    "SET is_active = false, updated_at = now() "
                    f"WHERE user_id = {_quuid(body.user_id)} AND is_active = true "
                    "RETURNING id"
                ),
            )
            updated = result.first()
            await session.commit()

            if not updated:
                raise HTTPException(status_code=404, detail="No active ban found")

            logger.info("✅ Chat unban: user=%s", body.user_id[:8])
            return {"ok": True, "user_id": body.user_id}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to unban chat user: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to unban user")


@router.get("/bans", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_chat_bans(
    active_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
):
    """Get chat bans list."""
    try:
        async with AsyncSessionFactory() as session:
            active_filter = "AND b.is_active = true AND (b.ban_type = 'permanent' OR b.expires_at > now())" if active_only else ""
            safe_limit = int(limit)

            result = await session.execute(
                text(f"""
                    SELECT
                        b.id, b.user_id, b.reason, b.ban_type,
                        b.expires_at, b.is_active, b.created_at,
                        p.display_name AS user_name,
                        p.avatar_url AS user_avatar
                    FROM "EgRailway".chat_bans b
                    LEFT JOIN "EgRailway".profiles p ON p.id = b.user_id
                    WHERE 1=1 {active_filter}
                    ORDER BY b.created_at DESC
                    LIMIT {safe_limit}
                """),
            )
            rows = result.mappings().all()

            bans = []
            for row in rows:
                bans.append({
                    "id": str(row["id"]),
                    "user_id": str(row["user_id"]),
                    "user_name": row["user_name"] or "",
                    "user_avatar": row["user_avatar"] or "",
                    "reason": row["reason"] or "",
                    "ban_type": row["ban_type"],
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                    "is_active": row["is_active"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                })

            return {"total": len(bans), "bans": bans}

    except Exception as exc:
        logger.error("Failed to get chat bans: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch bans")
