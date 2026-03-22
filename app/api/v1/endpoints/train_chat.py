"""
Train Chat endpoints – real-time messaging per train.

WS   /api/v1/train-chat/{train_id}?ticket=<ticket>
    Real-time chat: authenticated users send/receive messages.

GET  /api/v1/train-chat/{train_id}/messages
    Get recent messages (requires auth).

GET  /api/v1/train-chat/{train_id}/pinned
    Get pinned messages (lost/found items).

GET  /api/v1/train-chat/{train_id}/count
    Lightweight message count (for badge).
"""

import json
import logging
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.security import verify_supabase_token, verify_ticket, create_ticket
from app.services.train_chat_manager import train_chat_manager
from app.services.chat_report_service import submit_report, check_user_banned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/train-chat", tags=["Train Chat"])


# ── REST: Get chat ticket ────────────────────────────────────────────────────

@router.post("/ticket/{train_id}")
async def get_chat_ticket(
    train_id: str,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """Get an HMAC ticket for chat WebSocket connection."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    # Extract user info for chat display
    user_meta = user.get("user_metadata", {}) or {}
    user_name = (
        user_meta.get("full_name", "")
        or user_meta.get("name", "")
        or user_meta.get("display_name", "")
        or "مجهول"
    )
    user_avatar = user_meta.get("avatar_url", "") or user_meta.get("picture", "") or ""

    # Create ticket with role "chatter"
    ticket = create_ticket(user_id, train_id, "listener")  # reuse role field

    return {
        "ticket": ticket,
        "user_name": user_name,
        "user_avatar": user_avatar,
    }


# ── WebSocket: Real-time chat ────────────────────────────────────────────────

@router.websocket("/{train_id}")
async def chat_websocket(
    ws: WebSocket,
    train_id: str,
    ticket: str = Query(...),
    user_name: str = Query("مجهول"),
    user_avatar: str = Query(""),
):
    """
    WebSocket endpoint for train chat.

    Client sends:
      {"type": "message", "text": "...", "msg_type": "normal|lost_item|found_item"}

    Client receives:
      {"type": "chat_message", "data": {...message...}}
      {"type": "system", "data": {"text": "..."}}
      {"type": "error", "data": {"error": "...", ...}}
    """
    # Verify ticket
    decoded_ticket = unquote(ticket)
    ticket_data = verify_ticket(decoded_ticket, train_id)
    if ticket_data is None:
        await ws.close(code=4001, reason="Invalid ticket")
        return

    user_id = ticket_data["user_id"]

    await ws.accept()
    logger.info("💬 [%s] Chat WS connected: user=%s", train_id, user_id[:8])

    try:
        # Join room
        await train_chat_manager.join(train_id, user_id, ws)

        # Send recent messages + pinned as initial payload
        messages = await train_chat_manager.get_messages(train_id, limit=50)
        pinned = await train_chat_manager.get_pinned(train_id)

        await ws.send_json({
            "type": "init",
            "data": {
                "messages": list(reversed(messages)),  # oldest first for display
                "pinned": pinned,
            },
        })

        # Listen for messages
        while True:
            raw = await ws.receive_text()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({
                    "type": "error",
                    "data": {"error": "invalid_json"},
                })
                continue

            msg_type_ws = data.get("type", "")

            if msg_type_ws == "message":
                text = data.get("text", "")
                msg_type = data.get("msg_type", "normal")

                result = await train_chat_manager.process_message(
                    train_id=train_id,
                    user_id=user_id,
                    user_name=unquote(user_name),
                    user_avatar=unquote(user_avatar),
                    text=text,
                    msg_type=msg_type,
                )

                if not result.get("ok"):
                    await ws.send_json({
                        "type": "error",
                        "data": result,
                    })

            elif msg_type_ws == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("💬 [%s] Chat WS disconnected: user=%s", train_id, user_id[:8])
    except Exception as exc:
        logger.error("💬 [%s] Chat WS error: user=%s: %s", train_id, user_id[:8], exc)
    finally:
        await train_chat_manager.leave(train_id, user_id)


# ── REST: Get messages ────────────────────────────────────────────────────────

@router.get("/{train_id}/messages")
async def get_chat_messages(
    train_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """Get recent chat messages for a train (requires auth)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    messages = await train_chat_manager.get_messages(train_id, offset=offset, limit=limit)
    return {"messages": list(reversed(messages)), "count": len(messages)}


# ── REST: Get pinned messages ─────────────────────────────────────────────────

@router.get("/{train_id}/pinned")
async def get_pinned_messages(
    train_id: str,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """Get pinned messages (lost/found items) for a train."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    pinned = await train_chat_manager.get_pinned(train_id)
    return {"pinned": pinned}


# ── REST: Message count (lightweight, for badge) ─────────────────────────────

@router.get("/{train_id}/count")
async def get_message_count(train_id: str):
    """
    Get total message count for a train.
    Lightweight endpoint (no auth) — only returns a count number.
    Used for showing unread badge on chat icon.
    """
    count = await train_chat_manager.get_message_count(train_id)
    return {"train_id": train_id, "count": count}


# ── REST: Report a message ────────────────────────────────────────────────────

class ReportRequest(BaseModel):
    reported_user_id: str = Field(..., min_length=1, max_length=100)
    train_id: str = Field(..., min_length=1, max_length=50)
    message_id: str = Field(..., min_length=1, max_length=100)
    message_text: str = Field(..., min_length=1, max_length=500)
    report_reason: str = Field("", max_length=300)


@router.post("/report")
async def report_message(
    body: ReportRequest,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """Report a chat message. Requires authentication."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    reporter_id = user.get("id", "")
    if not reporter_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    result = await submit_report(
        reporter_id=reporter_id,
        reported_user_id=body.reported_user_id,
        train_id=body.train_id,
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

    return {"ok": True, "message": "تم تقديم البلاغ بنجاح"}


# ── REST: Check ban status ────────────────────────────────────────────────────

@router.get("/ban-status")
async def get_ban_status(
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """Check if the current user is banned from chat."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    ban_info = await check_user_banned(user_id)
    return ban_info
