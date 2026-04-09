"""
Admin endpoints for live tracking management.

All endpoints require admin authentication (Supabase JWT + is_admin).
Read endpoints: monitor + fulladmin. Write endpoints: fulladmin only.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from app.core.admin_auth import AdminUser, get_admin_or_legacy_key, require_fulladmin
from app.services.audit_service import audit
from app.services.ban_service import ban_contributor, is_banned, list_bans, unban_contributor
from app.services.tracking_manager import tracking_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live/admin", tags=["Live Admin"])


# ── Request schemas ───────────────────────────────────────────────────────────

class BanRequest(BaseModel):
    user_id: str
    reason: str = ""
    duration_minutes: int = 0  # 0 = permanent


class UnbanRequest(BaseModel):
    user_id: str


class SetLeaderRequest(BaseModel):
    train_id: str
    user_id: str


class RemoveLeaderRequest(BaseModel):
    train_id: str


class SetMaxContributorsRequest(BaseModel):
    train_id: str
    max_active: int  # new limit (1-50)


class SuspendRequest(BaseModel):
    train_id: str
    user_id: str
    reason: str = ""
    duration_minutes: int = 0  # 0 = permanent until manually unsuspended


class UnsuspendRequest(BaseModel):
    train_id: str
    user_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/ban", dependencies=[Depends(require_fulladmin)])
async def ban_contributor_endpoint(body: BanRequest, request: Request):
    """
    Ban a contributor from contributing.
    duration_minutes=0 means permanent ban.
    Also kicks them from any active room.
    """
    # First kick from all active rooms
    for room in tracking_manager.all_rooms_info():
        for c in room["contributors"]:
            if c["user_id"] == body.user_id:
                await tracking_manager.kick_contributor(
                    train_id=room["train_id"],
                    user_id=body.user_id,
                    reason=f"banned: {body.reason}",
                )
                break

    # Store ban in Redis
    success = await ban_contributor(
        user_id=body.user_id,
        reason=body.reason,
        duration_minutes=body.duration_minutes,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store ban",
        )

    duration_text = f"{body.duration_minutes} minutes" if body.duration_minutes > 0 else "permanent"
    audit.log_admin_action(
        request,
        action=f"ban_contributor ({duration_text})",
        metadata={"target_user": body.user_id, "reason": body.reason, "duration": body.duration_minutes},
    )
    return {"ok": True, "message": f"User {body.user_id[:8]}... banned ({duration_text})"}


@router.post("/unban", dependencies=[Depends(require_fulladmin)])
async def unban_contributor_endpoint(body: UnbanRequest, request: Request):
    """Remove a contributor's ban."""
    removed = await unban_contributor(body.user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active ban found for this user",
        )
    audit.log_admin_action(request, action="unban_contributor", metadata={"target_user": body.user_id})
    return {"ok": True, "message": f"User {body.user_id[:8]}... unbanned"}


@router.get("/bans", dependencies=[Depends(get_admin_or_legacy_key)])
async def list_bans_endpoint():
    """List all currently banned contributors."""
    bans = await list_bans()
    return {"total": len(bans), "bans": bans}


@router.post("/set-leader", dependencies=[Depends(require_fulladmin)])
async def set_leader_endpoint(body: SetLeaderRequest, request: Request):
    """Set a contributor as the room leader (only their updates are used)."""
    success = tracking_manager.set_leader(
        train_id=body.train_id,
        user_id=body.user_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contributor not found in the specified room",
        )
    audit.log_admin_action(request, action="set_leader", metadata={"train_id": body.train_id, "target_user": body.user_id})
    return {"ok": True, "message": f"User {body.user_id[:8]}... set as leader"}


@router.post("/remove-leader", dependencies=[Depends(require_fulladmin)])
async def remove_leader_endpoint(body: RemoveLeaderRequest, request: Request):
    """Remove leader from a room (revert to auto aggregation)."""
    success = tracking_manager.remove_leader(body.train_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No leader set for this room",
        )
    audit.log_admin_action(request, action="remove_leader", metadata={"train_id": body.train_id})
    return {"ok": True, "message": "Leader removed, reverted to auto aggregation"}


@router.get("/logs/{train_id}", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_room_logs(train_id: str):
    """Get event log for a tracking room (falls back to Redis if room destroyed)."""
    logs = await tracking_manager.get_room_logs(train_id)
    return {"train_id": train_id, "total": len(logs), "logs": logs}


@router.get("/rooms", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_admin_rooms():
    """List all active rooms + recently-tracked rooms (12h history) for admin dashboard."""
    active_rooms = tracking_manager.all_rooms_info()
    historical_rooms = await tracking_manager.get_recent_rooms()

    # Merge: active rooms first, then historical (no duplicates)
    all_rooms = active_rooms + historical_rooms

    # Enrich with wrong-location reports from Redis
    from app.core.cache import get_redis
    redis = await get_redis()
    for room in all_rooms:
        tid = room.get("train_id", "")
        count = await redis.hget(f"wrong_loc:{tid}", "count") if tid else None
        room["wrong_location_reports"] = int(count) if count else 0

    total_contributors = sum(r["contributors_count"] for r in all_rooms)
    total_waiting = sum(r.get("waiting_count", 0) for r in all_rooms)
    return {
        "total_rooms": len(all_rooms),
        "total_contributors": total_contributors,
        "total_waiting": total_waiting,
        "rooms": all_rooms,
    }


@router.get("/feed/{train_id}", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_room_feed(train_id: str):
    """Get live GPS update feed for a tracking room (last 100 updates)."""
    feed = tracking_manager.get_room_feed(train_id)
    return {"train_id": train_id, "total": len(feed), "feed": feed}


@router.post("/set-max-contributors", dependencies=[Depends(require_fulladmin)])
async def set_max_contributors_endpoint(body: SetMaxContributorsRequest, request: Request):
    """Update the max active contributors limit for a room."""
    if body.max_active < 1 or body.max_active > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_active must be between 1 and 50",
        )
    room = tracking_manager.get_room(body.train_id)
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )
    old_max = room.max_active_contributors
    room.max_active_contributors = body.max_active
    logger.info(
        "⚙️ [%s] max_active_contributors changed: %d → %d",
        body.train_id, old_max, body.max_active,
    )
    # If limit increased and there are waiting contributors, promote
    if body.max_active > old_max and room.waiting_list:
        await tracking_manager._promote_from_waiting_list(room)
    return {
        "ok": True,
        "train_id": body.train_id,
        "old_max": old_max,
        "new_max": body.max_active,
        "active": len(room.contributors),
        "waiting": len(room.waiting_list),
    }


@router.get("/check-ban/{user_id}", dependencies=[Depends(get_admin_or_legacy_key)])
async def check_ban_endpoint(user_id: str):
    """Check if a specific user is currently banned."""
    ban_info = await is_banned(user_id)
    if ban_info is None:
        return {"banned": False, "user_id": user_id}
    return {"banned": True, "user_id": user_id, "ban_info": ban_info}


@router.delete("/clear-position/{train_id}", dependencies=[Depends(require_fulladmin)])
async def clear_train_position_endpoint(train_id: str, request: Request):
    """Clear cached position data for a train from Redis and in-memory."""
    cleared = await tracking_manager.clear_train_position(train_id)
    if not cleared:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No position data found for this train",
        )
    audit.log_admin_action(
        request,
        action="clear_train_position",
        metadata={"train_id": train_id},
    )
    return {"ok": True, "message": f"Position data cleared for train {train_id}"}


@router.post("/suspend", dependencies=[Depends(require_fulladmin)])
async def suspend_contributor_endpoint(body: SuspendRequest, request: Request):
    """
    Suspend a contributor from updating positions (keeps them in room but rejects updates).
    duration_minutes=0 means permanent until manually unsuspended.
    """
    success = await tracking_manager.suspend_contributor(
        train_id=body.train_id,
        user_id=body.user_id,
        duration_minutes=body.duration_minutes,
        reason=body.reason,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contributor not found in the specified room",
        )
    duration_text = f"{body.duration_minutes} minutes" if body.duration_minutes > 0 else "permanent"
    audit.log_admin_action(
        request,
        action=f"suspend_contributor ({duration_text})",
        metadata={"train_id": body.train_id, "target_user": body.user_id, "reason": body.reason, "duration": body.duration_minutes},
    )
    return {"ok": True, "message": f"User {body.user_id[:8]}... suspended ({duration_text})"}


@router.post("/unsuspend", dependencies=[Depends(require_fulladmin)])
async def unsuspend_contributor_endpoint(body: UnsuspendRequest, request: Request):
    """Remove suspension from a contributor."""
    success = await tracking_manager.unsuspend_contributor(
        train_id=body.train_id,
        user_id=body.user_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active suspension found for this user in the specified room",
        )
    audit.log_admin_action(
        request,
        action="unsuspend_contributor",
        metadata={"train_id": body.train_id, "target_user": body.user_id},
    )
    return {"ok": True, "message": f"User {body.user_id[:8]}... unsuspended"}
