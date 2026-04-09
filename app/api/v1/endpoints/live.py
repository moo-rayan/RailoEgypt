"""
HTTP-based live train tracking endpoints.

POST /api/v1/live/{train_id}/location
    Contributor sends GPS update every 30 s.
    Requires Supabase JWT in Authorization header.

GET  /api/v1/live/position/{train_id}
    Listener polls for the latest aggregated train position every 30 s.
    Requires Supabase JWT in Authorization header.

GET  /api/v1/live/status/{train_id}
    Quick check if tracking is active for a train.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key
from app.core.database import AsyncSessionFactory, get_db
from app.core.security import require_authenticated_user, verify_supabase_token
from app.models.profile import Profile
from app.models.station import Station
from app.models.trip import TripStop
from app.services.tracking_manager import tracking_manager
from app.services.train_chat_manager import train_chat_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["Live Tracking"])


# ── HTTP: Contributor sends GPS location update ───────────────────────────────

class LocationUpdateRequest(BaseModel):
    lat: float
    lng: float
    speed: float = 0.0
    bearing: float = 0.0
    trip_id: int | None = None
    from_station_name: str | None = None
    to_station_name: str | None = None
    silent: bool = False


async def _load_trip_info(train_id: str, trip_id: int) -> None:
    """Load trip stations from DB into the tracking room (if not already set)."""
    try:
        async with AsyncSessionFactory() as session:
            rows = (
                await session.execute(
                    select(TripStop, Station)
                    .outerjoin(Station, TripStop.station_id == Station.id)
                    .where(TripStop.trip_id == trip_id)
                    .order_by(TripStop.stop_order)
                )
            ).all()

            stations = []
            start_station = ""
            end_station = ""

            for stop, station in rows:
                if station and station.latitude and station.longitude:
                    stations.append({
                        "order": stop.stop_order,
                        "name_ar": station.name_ar,
                        "name_en": station.name_en,
                        "lat": station.latitude,
                        "lon": station.longitude,
                        "time_ar": stop.time_ar or "",
                        "time_en": stop.time_en or "",
                    })
                    if stop.stop_order == 1:
                        start_station = station.name_ar
                    if stop.stop_order == len(rows):
                        end_station = station.name_ar

            if stations:
                tracking_manager.set_trip_info(
                    train_id=train_id,
                    trip_id=trip_id,
                    stations=stations,
                    start_station=start_station,
                    end_station=end_station,
                )
                logger.info(
                    "📍 [%s] Trip info loaded: %d stations (trip_id=%d)",
                    train_id, len(stations), trip_id,
                )
    except Exception as exc:
        logger.warning("⚠️ [%s] Failed to load trip info: %s", train_id, exc)


@router.post("/{train_id}/location")
async def post_contributor_location(
    train_id: str,
    body: LocationUpdateRequest,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
    db: AsyncSession = Depends(get_db),
):
    """
    Contributor sends their GPS location every 30 seconds.
    Automatically registers the contributor in the room on first call.
    Returns the current room status (active/waiting) and the latest
    aggregated train position (so contributors can also see the map).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    # Ban check — return 200 with error so Flutter BG service can parse it
    from app.services.ban_service import is_banned
    ban_info = await is_banned(user_id)
    if ban_info:
        return {
            "ok": False,
            "status": "banned",
            "error": "banned",
            "message_ar": "تم حظرك من المساهمة",
            "position_data": None,
        }

    # Store user metadata
    user_meta = user.get("user_metadata", {}) or {}
    avatar_url = user_meta.get("avatar_url", "") or user_meta.get("picture", "") or ""
    display_name = (
        user_meta.get("display_name", "")
        or user_meta.get("full_name", "")
        or user_meta.get("name", "")
        or user.get("email", "").split("@")[0]
    )
    if avatar_url:
        tracking_manager.set_user_avatar(user_id, avatar_url)
    if display_name:
        tracking_manager.set_user_display_name(user_id, display_name)

    # Store trip info (from/to station names)
    if body.from_station_name or body.to_station_name:
        tracking_manager.set_user_trip_info(
            user_id,
            from_station_name=body.from_station_name or "",
            to_station_name=body.to_station_name or "",
        )

    # Store silent flag (alert-based contribution)
    if body.silent:
        tracking_manager.set_user_silent(user_id, True)

    # Load trip stations from DB if needed
    if body.trip_id:
        room = tracking_manager.get_room(train_id)
        if not room or not room.stations:
            await _load_trip_info(train_id, body.trip_id)

    # Pre-fetch captain status BEFORE the is_new_contributor check
    # to avoid an asyncio race condition (the await yields control,
    # allowing a concurrent request to also pass the new-contributor check).
    if user_id not in tracking_manager._user_captains:
        try:
            profile = (await db.execute(
                select(Profile.is_captain).where(
                    Profile.id == cast(user_id, PG_UUID)
                )
            )).scalar()
            tracking_manager.set_user_captain(user_id, bool(profile))
        except Exception as exc:
            logger.warning("⚠️ [%s] Captain check failed for %s: %s", train_id, user_id[:8], exc)

    # Register contributor if not already in the room
    # NOTE: no await between this check and add_contributor to prevent race conditions
    room = tracking_manager.get_room(train_id)
    is_new_contributor = not room or (
        user_id not in (room.contributors if room else {})
        and not any(w.user_id == user_id for w in (room.waiting_list if room else []))
    )

    if is_new_contributor:
        join_result = await tracking_manager.add_contributor(train_id, user_id)
        join_status = join_result.get("status", "active")

        if join_status == "kicked":
            return {
                "ok": False,
                "status": "kicked",
                "error": "kicked",
                "message_ar": join_result.get("message_ar", "تم طردك من الغرفة مؤقتاً"),
                "position_data": None,
            }

        if join_status == "suspended":
            return {
                "ok": False,
                "status": "suspended",
                "error": "suspended",
                "message_ar": join_result.get("message_ar", "تم إيقاف مساهمتك مؤقتاً"),
                "position_data": None,
            }

        logger.info(
            "👤+ [%s] New contributor %s joined (%s)",
            train_id, user_id[:8], join_status,
        )

    # Process the location update
    result = await tracking_manager.process_update(
        train_id=train_id,
        user_id=user_id,
        lat=body.lat,
        lng=body.lng,
        speed=body.speed,
        bearing=body.bearing,
    )

    # Build response — include current aggregated position so the contributor
    # can also display the train on their map without a separate poll.
    # IMPORTANT: don't send (0,0) — room starts at (0,0) before first
    # successful aggregate; sending it causes the map to zoom to Null Island.
    room = tracking_manager.get_room(train_id)
    position_data = None
    if room and (room.lat != 0.0 or room.lng != 0.0):
        position_data = tracking_manager.get_position_data(room)

    # Determine contributor's current waiting-list status
    contributor_status = "active"
    waiting_position = 0
    total_waiting = 0
    if room and user_id not in room.contributors:
        for i, w in enumerate(room.waiting_list):
            if w.user_id == user_id:
                contributor_status = "waiting"
                waiting_position = i + 1
                total_waiting = len(room.waiting_list)
                break

    return {
        "ok": result.get("ok", False),
        "status": contributor_status,
        "waiting_position": waiting_position,
        "total_waiting": total_waiting,
        "error": result.get("error"),
        "message_ar": result.get("message_ar"),
        "distance_m": result.get("distance_m"),
        "position_data": position_data,
    }


# ── REST: Contributor explicit leave ─────────────────────────────────────────

@router.post("/{train_id}/leave")
async def contributor_leave(
    train_id: str,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """
    Called by the contributor app when the user explicitly stops contributing.
    Removes them from the room immediately rather than waiting for stale cleanup.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user["id"]
    await tracking_manager.remove_participant(train_id, user_id, "user_left")
    logger.info("🚪 [%s] Contributor %s left voluntarily", train_id, user_id[:8])
    return {"ok": True}


# ── REST: Active trains (for authenticated users) ────────────────────────────

@router.get("/active-trains")
async def get_active_trains(
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """
    Return a lightweight list of trains with active tracking rooms.
    Requires a valid Supabase JWT (any authenticated user).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    from app.core.cache import get_redis
    r = await get_redis()

    trains = []
    for room in tracking_manager.all_rooms_info():
        if room["contributors_count"] == 0:
            continue
        tid = room["train_id"]
        chat_count = await train_chat_manager.get_message_count(tid)

        # Fetch crowd report from Redis
        crowd_data = await r.hgetall(f"crowd:{tid}")
        crowded = int(crowd_data.get("crowded", 0))
        not_crowded = int(crowd_data.get("not_crowded", 0))

        # Fetch wrong-location reports from Redis
        wrong_loc_count = int(await r.hget(f"wrong_loc:{tid}", "count") or 0)

        trains.append({
            "train_id": tid,
            "start_station": room.get("start_station", ""),
            "end_station": room.get("end_station", ""),
            "speed": room["speed"],
            "status": room["status"],
            "contributors_count": room["contributors_count"],
            "chat_message_count": chat_count,
            "crowd_crowded": crowded,
            "crowd_not_crowded": not_crowded,
            "wrong_location_reports": wrong_loc_count,
        })
    return {"trains": trains, "total": len(trains)}


# ── REST: Listener polls for latest train position ────────────────────────────

@router.get("/position/{train_id}")
async def get_train_position(
    train_id: str,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """
    Listener polls this endpoint every 30 seconds to get the latest aggregated
    train position. Checks in-memory room first (most up-to-date), falls back
    to Redis cache. Requires authentication.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user.get("id", "")
    
    # Track this user as a listener (for HTTP listener counting)
    if user_id:
        tracking_manager.track_http_listener(user_id, train_id)

    # 1. In-memory room — only if there are active contributors right now
    room = tracking_manager.get_room(train_id)
    if room and (room.lat != 0.0 or room.lng != 0.0):
        cn = tracking_manager._active_contributor_count(room)
        if cn > 0:
            return {
                "found": True,
                "train_id": train_id,
                "data": tracking_manager.get_position_data(room),
            }

    # 2. Redis cache — cleared immediately when last contributor stops
    from app.core.cache import cache_get
    cached = await cache_get(f"train_pos:{train_id}")
    if cached is not None:
        return {"found": True, "train_id": train_id, "data": cached}

    return {"found": False, "train_id": train_id}


@router.get("/status/{train_id}", dependencies=[Depends(require_authenticated_user)])
async def get_tracking_status(train_id: str):
    """Check if live tracking is active for a train."""
    info = tracking_manager.room_info(train_id)
    if info is None:
        return {"active": False, "train_id": train_id}
    return {"active": True, **info}


@router.get("/active", dependencies=[Depends(require_authenticated_user)])
async def get_active_rooms():
    """List all trains with active tracking sessions."""
    return {
        "active_rooms": tracking_manager.active_rooms,
    }


# ── Dashboard endpoints ──────────────────────────────────────────────────────

@router.get("/dashboard/stats", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_dashboard_stats(db: AsyncSession = Depends(get_db)):
    """Aggregate stats for the admin dashboard."""
    from sqlalchemy import func, select as sa_select
    from app.models.station import Station as StationModel
    from app.models.train import Train as TrainModel
    from app.models.trip import Trip

    stations_count = (await db.execute(
        sa_select(func.count()).select_from(StationModel)
    )).scalar_one()
    trains_count = (await db.execute(
        sa_select(func.count()).select_from(TrainModel)
    )).scalar_one()
    trips_count = (await db.execute(
        sa_select(func.count()).select_from(Trip)
    )).scalar_one()

    return {
        "stations": stations_count,
        "trains": trains_count,
        "trips": trips_count,
        "active_rooms": tracking_manager.active_rooms,
    }


# ── Crowd Report ─────────────────────────────────────────────────────────────

_CROWD_TTL_DEFAULT = 6 * 3600   # fallback if client doesn't send ttl
_CROWD_TTL_MAX = 14 * 3600     # safety cap: 14 hours max

class CrowdReportRequest(BaseModel):
    level: str  # "crowded" or "not_crowded"
    ttl_seconds: int | None = None  # remaining trip duration from client


@router.post("/{train_id}/crowd-report")
async def submit_crowd_report(
    train_id: str,
    body: CrowdReportRequest,
    authorization: str = Header(...),
):
    """Submit or update a crowd level vote for a train. One updatable vote per user."""
    user_id = await require_authenticated_user(authorization)

    if body.level not in ("crowded", "not_crowded"):
        raise HTTPException(status_code=400, detail="level must be 'crowded' or 'not_crowded'")

    from app.core.cache import get_redis
    r = await get_redis()

    vote_key = f"crowd:{train_id}"
    user_vote_key = f"crowd:{train_id}:vote:{user_id}"

    # Check if user already voted on this train
    old_vote = await r.get(user_vote_key)
    if old_vote:
        old_vote = old_vote if isinstance(old_vote, str) else old_vote.decode()

    if old_vote == body.level:
        # Same vote — no change needed
        return {"ok": True, "level": body.level, "changed": False}

    if old_vote:
        # Decrement old vote
        await r.hincrby(vote_key, old_vote, -1)
        # Prevent negative counts
        current = await r.hget(vote_key, old_vote)
        if current is not None:
            val = int(current)
            if val < 0:
                await r.hset(vote_key, old_vote, 0)

    # Increment new vote
    await r.hincrby(vote_key, body.level, 1)

    # Set TTL on the vote key only if it doesn't have one yet
    current_ttl = await r.ttl(vote_key)
    if current_ttl < 0:
        ttl = _CROWD_TTL_DEFAULT
        if body.ttl_seconds and 60 < body.ttl_seconds <= _CROWD_TTL_MAX:
            ttl = body.ttl_seconds
        await r.expire(vote_key, ttl)

    # Store user's vote with same TTL as the vote key
    vote_ttl = await r.ttl(vote_key)
    if vote_ttl > 0:
        await r.setex(user_vote_key, vote_ttl, body.level)
    else:
        ttl = _CROWD_TTL_DEFAULT
        if body.ttl_seconds and 60 < body.ttl_seconds <= _CROWD_TTL_MAX:
            ttl = body.ttl_seconds
        await r.setex(user_vote_key, ttl, body.level)

    return {"ok": True, "level": body.level, "changed": old_vote is not None}


@router.get("/{train_id}/crowd-report")
async def get_crowd_report(
    train_id: str,
    authorization: str = Header(...),
):
    """Get current crowd report for a train, including user's own vote."""
    user_id = await require_authenticated_user(authorization)

    from app.core.cache import get_redis
    r = await get_redis()

    vote_key = f"crowd:{train_id}"
    user_vote_key = f"crowd:{train_id}:vote:{user_id}"

    data = await r.hgetall(vote_key)
    user_vote = await r.get(user_vote_key)
    if user_vote and not isinstance(user_vote, str):
        user_vote = user_vote.decode()

    crowded = int(data.get("crowded", 0))
    not_crowded = int(data.get("not_crowded", 0))
    total = crowded + not_crowded

    return {
        "train_id": train_id,
        "crowded": crowded,
        "not_crowded": not_crowded,
        "total": total,
        "my_vote": user_vote,
    }


# ── Wrong Location Report ─────────────────────────────────────────────────────

_WRONG_LOC_COOLDOWN_S = 15 * 60   # 15 minutes between reports per user per train
_WRONG_LOC_TTL = 6 * 3600         # reports expire after 6 hours (same as crowd)


@router.post("/{train_id}/wrong-location-report")
async def submit_wrong_location_report(
    train_id: str,
    authorization: str = Header(...),
):
    """Report that a train's displayed location is wrong. 15-min cooldown per user."""
    user_id = await require_authenticated_user(authorization)

    from app.core.cache import get_redis
    r = await get_redis()

    user_cooldown_key = f"wrong_loc:{train_id}:cd:{user_id}"
    report_key = f"wrong_loc:{train_id}"

    # Check cooldown
    existing = await r.get(user_cooldown_key)
    if existing:
        ttl = await r.ttl(user_cooldown_key)
        remaining_min = max(1, (ttl + 59) // 60)
        return {
            "ok": False,
            "error": "cooldown",
            "remaining_seconds": ttl if ttl > 0 else 0,
            "message_ar": f"يمكنك الإبلاغ مجدداً بعد {remaining_min} دقيقة",
        }

    # Increment report count
    await r.hincrby(report_key, "count", 1)

    # Record reporter
    import time as _time
    await r.hset(report_key, f"user:{user_id}", str(int(_time.time())))

    # Set TTL on report key only if not already set
    current_ttl = await r.ttl(report_key)
    if current_ttl < 0:
        await r.expire(report_key, _WRONG_LOC_TTL)

    # Set user cooldown
    await r.setex(user_cooldown_key, _WRONG_LOC_COOLDOWN_S, "1")

    # Get updated count
    count = int(await r.hget(report_key, "count") or 0)

    logger.info("⚠️ [%s] Wrong location report from %s (total: %d)", train_id, user_id[:8], count)

    return {"ok": True, "total_reports": count}


@router.get("/{train_id}/wrong-location-report")
async def get_wrong_location_report(
    train_id: str,
    authorization: str = Header(...),
):
    """Get wrong-location report count for a train."""
    user_id = await require_authenticated_user(authorization)

    from app.core.cache import get_redis
    r = await get_redis()

    report_key = f"wrong_loc:{train_id}"
    user_cooldown_key = f"wrong_loc:{train_id}:cd:{user_id}"

    count = int(await r.hget(report_key, "count") or 0)

    # Check if user can report
    cooldown_ttl = await r.ttl(user_cooldown_key)
    can_report = cooldown_ttl <= 0

    return {
        "train_id": train_id,
        "total_reports": count,
        "can_report": can_report,
        "cooldown_remaining": max(0, cooldown_ttl) if not can_report else 0,
    }


@router.get("/dashboard/rooms", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_dashboard_rooms():
    """List all active tracking rooms with contributor details (for dashboard)."""
    rooms = tracking_manager.all_rooms_info()
    total_contributors = sum(r["contributors_count"] for r in rooms)
    return {
        "total_rooms": len(rooms),
        "total_contributors": total_contributors,
        "rooms": rooms,
    }
