"""
Real-time train tracking WebSocket endpoint.

POST /api/v1/live/ticket
    Get a short-lived HMAC ticket for WebSocket connection.
    Requires Supabase JWT in Authorization header.

WS   /api/v1/live/{train_id}?ticket=<ticket>
    Real-time tracking: contributors send GPS, listeners receive updates.

GET  /api/v1/live/status/{train_id}
    Quick check if tracking is active for a train.
"""

import asyncio
import json
import logging
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.core.admin_auth import get_admin_or_legacy_key
from pydantic import BaseModel
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionFactory, get_db
from app.core.security import create_ticket, require_authenticated_user, verify_supabase_token, verify_ticket
from app.models.profile import Profile
from app.models.station import Station
from app.models.trip import TripStop
from app.services.tracking_manager import tracking_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["Live Tracking"])


# ── Ticket request schema ────────────────────────────────────────────────────

class TicketRequest(BaseModel):
    train_id: str
    role: str = "listener"  # "contributor" or "listener"
    trip_id: int | None = None
    from_station_name: str | None = None  # contributor's boarding station (Arabic)
    to_station_name: str | None = None    # contributor's alighting station (Arabic)


class TicketResponse(BaseModel):
    ticket: str
    expires_in: int = 43200  # 12 hours


# ── REST: Get ticket ─────────────────────────────────────────────────────────

@router.post("/ticket", response_model=TicketResponse)
async def get_tracking_ticket(
    body: TicketRequest,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange a Supabase JWT for a short-lived HMAC ticket.
    The ticket is used to authenticate the WebSocket connection.
    """
    # Extract bearer token
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    # Verify with Supabase
    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    # Validate role
    if body.role not in ("contributor", "listener"):
        raise HTTPException(status_code=400, detail="Role must be 'contributor' or 'listener'")

    # Check if contributor is banned
    if body.role == "contributor":
        from app.services.ban_service import is_banned
        ban_info = await is_banned(user_id)
        if ban_info:
            raise HTTPException(
                status_code=403,
                detail="You are banned from contributing",
            )

    # Store user avatar and display name for contributor broadcasts
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

    # Store contributor's personal trip info (boarding → alighting)
    if body.role == "contributor":
        tracking_manager.set_user_trip_info(
            user_id,
            from_station_name=body.from_station_name or "",
            to_station_name=body.to_station_name or "",
        )

        # Check if user is a train captain
        profile = (await db.execute(
            select(Profile.is_captain).where(Profile.id == cast(user_id, PG_UUID))
        )).scalar()
        tracking_manager.set_user_captain(user_id, bool(profile))

    logger.info(
        "🎫 Ticket issued: user=%s train=%s role=%s",
        user_id[:8], body.train_id, body.role,
    )

    # If contributor provides trip_id, load stations from DB (secure)
    if body.trip_id:
        # Load trip stops with station coordinates from database
        rows = (
            await db.execute(
                select(TripStop, Station)
                .outerjoin(Station, TripStop.station_id == Station.id)
                .where(TripStop.trip_id == body.trip_id)
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
                train_id=body.train_id,
                trip_id=body.trip_id,
                stations=stations,
                start_station=start_station,
                end_station=end_station,
            )

    ticket = create_ticket(user_id, body.train_id, body.role)
    return TicketResponse(ticket=ticket)


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

    trains = []
    for room in tracking_manager.all_rooms_info():
        if room["contributors_count"] == 0:
            continue
        trains.append({
            "train_id": room["train_id"],
            "start_station": room.get("start_station", ""),
            "end_station": room.get("end_station", ""),
            "speed": room["speed"],
            "status": room["status"],
            "contributors_count": room["contributors_count"],
            "listeners_count": room["listeners_count"],
        })
    return {"trains": trains, "total": len(trains)}


# ── Helper: load trip stations into room ──────────────────────────────────────

async def _ensure_trip_info(train_id: str, trip_id: int) -> None:
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
                    "📍 [%s] Trip info loaded via WS handler: %d stations",
                    train_id, len(stations),
                )
    except Exception as exc:
        logger.warning("⚠️ [%s] Failed to load trip info in WS: %s", train_id, exc)


# ── WebSocket: Live tracking ─────────────────────────────────────────────────

@router.websocket("/{train_id}")
async def live_tracking(
    websocket: WebSocket,
    train_id: str,
    ticket: str = Query(..., description="HMAC-signed ticket from /live/ticket"),
    trip_id: int | None = Query(None, description="Trip ID for loading stations (contributor)"),
):
    """
    WebSocket for real-time train tracking.

    Contributors send:
      {"type": "location_update", "lat": ..., "lng": ..., "speed": ..., "bearing": ...}

    All clients receive (compact keys):
      {"t": "ip", "d": {...}}   — initial_position
      {"t": "pu", "d": {...}}   — position_update

    Contributors also receive:
      {"type": "update_ack", "ok": true/false, "error": "...", ...}
    """
    # URL-decode ticket (WebSocket query params may arrive percent-encoded)
    ticket = unquote(ticket)
    logger.info("🔑 [%s] Ticket received (len=%d)", train_id, len(ticket))

    # Verify ticket
    ticket_data = verify_ticket(ticket, train_id)
    if ticket_data is None:
        await websocket.close(code=4001, reason="Invalid or expired ticket")
        logger.warning("🚫 WS rejected: invalid ticket for train %s", train_id)
        return

    user_id = ticket_data["user_id"]
    role = ticket_data["role"]

    # Check if contributor is banned (ticket may have been issued before ban)
    if role == "contributor":
        from app.services.ban_service import is_banned
        ban_info = await is_banned(user_id)
        if ban_info:
            await websocket.close(code=4003, reason="banned")
            logger.warning("🚫 WS rejected: user %s is banned (train %s)", user_id[:8], train_id)
            return

    await websocket.accept()
    logger.info("✅ [%s] WS accepted for user=%s role=%s", train_id, user_id[:8], role)
    logger.info("🔌 [%s] WS connected: user=%s role=%s", train_id, user_id[:8], role)

    disconnect_reason = "unknown"
    contributor_status = None  # "active" or "waiting" for contributors
    try:
        # Register participant
        if role == "contributor":
            # Ensure room has trip station data BEFORE add_contributor
            # (needed for distance calculation)
            if trip_id:
                room = tracking_manager.get_room(train_id)
                if not room or not room.stations:
                    await _ensure_trip_info(train_id, trip_id)

            result = await tracking_manager.add_contributor(train_id, user_id, websocket)
            contributor_status = result.get("status", "active")
        else:
            await tracking_manager.add_listener(train_id, user_id, websocket)

        # Send connection confirmation
        conn_data = {
            "user_id": user_id,
            "role": role,
            "train_id": train_id,
            "room_info": tracking_manager.room_info(train_id),
        }
        if contributor_status == "waiting":
            conn_data["status"] = "waiting"
            conn_data["position"] = result.get("position", 0)
            conn_data["total_waiting"] = result.get("total", 0)
            conn_data["message_ar"] = f"أنت في قائمة الانتظار (#{result.get('position', 0)}). سيتم ترقيتك تلقائياً."
        elif contributor_status == "active":
            conn_data["status"] = "active"

        await websocket.send_json({
            "type": "connected",
            "data": conn_data,
        })

        # Message loop (90s timeout = ~6× ping interval of 15s)
        _WS_RECEIVE_TIMEOUT = 90.0
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=_WS_RECEIVE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                disconnect_reason = f"انقطاع الاتصال — لا بيانات لمدة {_WS_RECEIVE_TIMEOUT:.0f} ثانية"
                logger.warning(
                    "⏰ [%s] No data from user=%s for %.0fs — closing",
                    train_id, user_id[:8], _WS_RECEIVE_TIMEOUT,
                )
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "location_update" and role == "contributor":
                lat = msg.get("lat")
                lng = msg.get("lng")
                speed = msg.get("speed", 0.0)
                bearing = msg.get("bearing", 0.0)

                if lat is None or lng is None:
                    await websocket.send_json({
                        "type": "update_ack",
                        "ok": False,
                        "error": "lat and lng are required",
                    })
                    continue

                result = await tracking_manager.process_update(
                    train_id=train_id,
                    user_id=user_id,
                    lat=float(lat),
                    lng=float(lng),
                    speed=float(speed),
                    bearing=float(bearing),
                )

                await websocket.send_json({"type": "update_ack", **result})

                # Silent disconnect after exceeding far-from-rail limit
                if result.get("error") == "silent_disconnect":
                    dist = result.get("distance_m", 0)
                    disconnect_reason = f"فصل تلقائي — بعيد عن المسار ({dist:.0f}م)"
                    logger.info(
                        "🔇 [%s] Silent disconnect for user=%s",
                        train_id, user_id[:8],
                    )
                    await tracking_manager.remove_participant(train_id, user_id, disconnect_reason)
                    await websocket.close(code=4003, reason="silent_disconnect")
                    return

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                if role != "contributor" and msg_type == "location_update":
                    await websocket.send_json({
                        "type": "error",
                        "message": "Only contributors can send location updates",
                    })
                # Listeners just keep connection open for receiving broadcasts

    except WebSocketDisconnect as wsd:
        code = getattr(wsd, 'code', 'unknown')
        if code == 1000 or code == 1001:
            disconnect_reason = "المستخدم أنهى الاتصال بنفسه"
        elif code == 4002:
            disconnect_reason = "تم الطرد بواسطة الإدارة (حظر)"
        else:
            disconnect_reason = f"انقطاع الاتصال (code={code})"
        logger.info(
            "🔌 [%s] WS disconnected: user=%s (code=%s)",
            train_id, user_id[:8], code,
        )
    except Exception as exc:
        disconnect_reason = f"خطأ غير متوقع: {type(exc).__name__}"
        logger.error("🔌 [%s] WS error for user=%s: %s", train_id, user_id[:8], exc)
    finally:
        await tracking_manager.remove_participant(train_id, user_id, disconnect_reason)


# ── REST: Tracking status ────────────────────────────────────────────────────

@router.get("/position/{train_id}")
async def get_last_position(
    train_id: str,
    authorization: str = Header(..., description="Bearer <supabase_access_token>"),
):
    """
    Get the last known position of a train from Redis cache.
    Returns immediately without waiting for WebSocket updates.
    Useful for showing train location instantly when entering the map screen.
    Requires authentication.
    """
    # Extract and verify bearer token
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    
    user = await verify_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    from app.core.cache import cache_get
    
    cached = await cache_get(f"train_pos:{train_id}")
    if cached is None:
        return {"found": False, "train_id": train_id}
    
    return {
        "found": True,
        "train_id": train_id,
        "data": cached,
    }


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
