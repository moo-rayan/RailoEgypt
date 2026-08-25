"""
Global Chat endpoints – one real-time chat room shared by all users.
"""

from __future__ import annotations

import json
import logging
import random
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import cast, select, update as sa_update
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import AsyncSessionFactory
from app.core.security import create_ticket, verify_supabase_token, verify_ticket
from app.models.profile import Profile
from app.services.chat_report_service import check_user_banned, submit_report
from app.services.global_chat_manager import GLOBAL_CHAT_RESOURCE, global_chat_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/global-chat", tags=["Global Chat"])


class GlobalChatTicketRequest(BaseModel):
    anonymous: bool | None = None


class GlobalChatPreferencesRequest(BaseModel):
    anonymous: bool = Field(..., description="True=anonymous alias, False=real name")


class GlobalChatReportRequest(BaseModel):
    reported_user_id: str = Field(..., min_length=1, max_length=100)
    message_id: str = Field(..., min_length=1, max_length=100)
    message_text: str = Field(..., min_length=1, max_length=500)
    report_reason: str = Field("", max_length=300)


async def _require_user(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="User ID not found")
    return user


async def _resolve_chat_identity(user: dict, anonymous: bool | None) -> dict:
    user_id = user["id"]
    user_meta = user.get("user_metadata", {}) or {}
    real_name = (
        user_meta.get("full_name", "")
        or user_meta.get("name", "")
        or user_meta.get("display_name", "")
        or "مجهول"
    )
    user_avatar = user_meta.get("avatar_url", "") or user_meta.get("picture", "") or ""

    chat_alias = None
    is_anonymous = False

    async with AsyncSessionFactory() as session:
        row = await session.execute(
            select(Profile.chat_alias, Profile.chat_anonymous).where(Profile.id == cast(user_id, UUID))
        )
        profile = row.first()

        if profile is not None:
            chat_alias = profile.chat_alias
            is_anonymous = profile.chat_anonymous if anonymous is None else anonymous
            updates = {}

            if not chat_alias:
                chat_alias = f"مسافر {random.randint(1000, 9999)}"
                updates["chat_alias"] = chat_alias

            if anonymous is not None and anonymous != profile.chat_anonymous:
                updates["chat_anonymous"] = anonymous
                is_anonymous = anonymous

            if updates:
                await session.execute(
                    sa_update(Profile).where(Profile.id == cast(user_id, UUID)).values(**updates)
                )
                await session.commit()
        else:
            chat_alias = f"مسافر {random.randint(1000, 9999)}"
            is_anonymous = bool(anonymous is True)

    return {
        "user_id": user_id,
        "user_name": chat_alias if is_anonymous else real_name,
        "user_avatar": "" if is_anonymous else user_avatar,
        "chat_alias": chat_alias,
        "chat_anonymous": is_anonymous,
    }


@router.post("/ticket")
async def get_global_chat_ticket(
    body: GlobalChatTicketRequest | None = None,
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    identity = await _resolve_chat_identity(
        user,
        None if body is None else body.anonymous,
    )
    ticket = create_ticket(identity["user_id"], GLOBAL_CHAT_RESOURCE, "listener")
    return {
        "ticket": ticket,
        "user_name": identity["user_name"],
        "user_avatar": identity["user_avatar"],
        "chat_alias": identity["chat_alias"],
        "chat_anonymous": identity["chat_anonymous"],
    }


@router.get("/preferences")
async def get_global_chat_preferences(
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    user_id = user["id"]
    async with AsyncSessionFactory() as session:
        row = await session.execute(
            select(Profile.chat_alias, Profile.chat_anonymous).where(Profile.id == cast(user_id, UUID))
        )
        profile = row.first()

    if profile is None:
        return {"ok": True, "mode_chosen": False, "chat_alias": None, "chat_anonymous": False}

    return {
        "ok": True,
        "mode_chosen": bool(profile.chat_anonymous),
        "chat_alias": profile.chat_alias,
        "chat_anonymous": bool(profile.chat_anonymous),
    }


@router.post("/preferences")
async def set_global_chat_preferences(
    body: GlobalChatPreferencesRequest,
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    identity = await _resolve_chat_identity(user, body.anonymous)
    return {
        "ok": True,
        "chat_alias": identity["chat_alias"],
        "chat_anonymous": identity["chat_anonymous"],
    }


@router.websocket("/ws")
async def global_chat_websocket(
    ws: WebSocket,
    ticket: str = Query(...),
    user_name: str = Query("مجهول"),
    user_avatar: str = Query(""),
):
    ticket_data = verify_ticket(unquote(ticket), GLOBAL_CHAT_RESOURCE)
    if ticket_data is None:
        await ws.close(code=4001, reason="Invalid ticket")
        return

    user_id = ticket_data["user_id"]
    await ws.accept()
    logger.debug("💬 [global] Chat WS connected: user=%s", user_id[:8])

    try:
        await global_chat_manager.join(user_id, ws)
        messages = await global_chat_manager.get_messages(limit=50, current_user_id=user_id)
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
                await ws.send_json({"type": "error", "data": {"error": "invalid_json"}})
                continue

            msg_type_ws = data.get("type", "")
            if msg_type_ws == "message":
                result = await global_chat_manager.process_message(
                    user_id=user_id,
                    user_name=unquote(user_name),
                    user_avatar=unquote(user_avatar),
                    text_value=data.get("text", ""),
                    msg_type=data.get("msg_type", "normal"),
                    reply_to=data.get("reply_to") if isinstance(data.get("reply_to"), dict) else None,
                )
                if not result.get("ok"):
                    await ws.send_json({"type": "error", "data": result})
            elif msg_type_ws == "react":
                result = await global_chat_manager.toggle_love(
                    message_id=str(data.get("message_id", "")),
                    user_id=user_id,
                )
                if not result.get("ok"):
                    await ws.send_json({"type": "error", "data": result})
            elif msg_type_ws == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.debug("💬 [global] Chat WS disconnected: user=%s", user_id[:8])
    except Exception as exc:
        logger.error("💬 [global] Chat WS error: user=%s: %s", user_id[:8], exc)
    finally:
        await global_chat_manager.leave(user_id, ws)


@router.get("/messages")
async def get_global_chat_messages(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    messages = await global_chat_manager.get_messages(
        offset=offset,
        limit=limit,
        current_user_id=user["id"],
    )
    return {"messages": list(reversed(messages)), "count": len(messages)}


@router.get("/count")
async def get_global_chat_count():
    return {"count": await global_chat_manager.get_message_count()}


@router.post("/react/{message_id}")
async def react_love(
    message_id: str,
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    result = await global_chat_manager.toggle_love(message_id=message_id, user_id=user["id"])
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "unknown"))
    return result


@router.post("/report")
async def report_global_message(
    body: GlobalChatReportRequest,
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    reporter_id = user["id"]
    result = await submit_report(
        reporter_id=reporter_id,
        reported_user_id=body.reported_user_id,
        train_id=GLOBAL_CHAT_RESOURCE,
        message_id=body.message_id,
        message_text=body.message_text,
        report_reason=body.report_reason,
    )
    if not result.get("ok"):
        error = result.get("error", "unknown")
        if error == "already_reported":
            raise HTTPException(status_code=409, detail="تم الإبلاغ عن هذه الرسالة مسبقاً")
        if error == "cannot_report_self":
            raise HTTPException(status_code=400, detail="لا يمكنك الإبلاغ عن نفسك")
        raise HTTPException(status_code=500, detail="فشل تقديم البلاغ")
    return {"ok": True}


@router.get("/ban-status")
async def get_global_chat_ban_status(
    authorization: str = Header(..., description="Bearer <access_token>"),
):
    user = await _require_user(authorization)
    return await check_user_banned(user["id"])
