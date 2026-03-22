"""
Railway map API endpoints.

GET /api/v1/railway/lines
    Returns all mainline-rail polylines for background map display.
    Each element is an ordered list of [lat, lon] pairs.

GET /api/v1/railway/path?from_lat=&from_lon=&to_lat=&to_lon=
    Runs A* on the railway graph between two geographic coordinates.
    Returns the path as ordered [lat, lon] pairs.

GET /api/v1/railway/trip-path/{trip_id}
    Routes through ALL intermediate stations as waypoints (A* per segment)
    to guarantee the correct railway branch is chosen at forks.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_get, cache_set
from app.core.database import get_db
from app.core.security import require_authenticated_user
from app.models.station import Station
from app.models.trip import TripStop
from app.services.railway_service import railway_graph

router = APIRouter(prefix="/railway", tags=["Railway"])

# Cache keys & TTL
_TRIP_PATH_TTL   = 24 * 3600                      # 24 h – routes don't change


# ── helpers ──────────────────────────────────────────────────────────────────

def _require_graph() -> None:
    if not railway_graph.is_built:
        raise HTTPException(
            status_code=503,
            detail="Railway graph is still being built. Try again in a moment.",
        )


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/path", dependencies=[Depends(require_authenticated_user)])
async def get_railway_path(
    from_lat: float = Query(..., description="Departure latitude"),
    from_lon: float = Query(..., description="Departure longitude"),
    to_lat:   float = Query(..., description="Arrival latitude"),
    to_lon:   float = Query(..., description="Arrival longitude"),
):
    """
    A* shortest path on the railway graph between two coordinates.
    Returns ordered list of [lat, lon] points.
    """
    _require_graph()
    path = railway_graph.a_star(from_lon, from_lat, to_lon, to_lat)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="No railway path found between the given coordinates.",
        )
    return {"path": path, "points": len(path)}


@router.get("/trip-path/{trip_id}", dependencies=[Depends(require_authenticated_user)])
async def get_trip_railway_path(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Build the full A* railway path for a trip by routing through **every**
    intermediate station as a waypoint.  This guarantees the path follows
    the correct branch even when the railway forks.

    Algorithm:
        1. Load all trip stops ordered by stop_order.
        2. Run A* between each consecutive pair of stations.
        3. Concatenate segments (deduplicating junction points).

    Result is cached in Redis for 24 h keyed by trip_id (because two trips
    with the same start/end may take different branches).
    """
    _require_graph()

    # 1. Check cache (keyed per trip — different trips may use different branches)
    cache_key = f"railway:trip-route:{trip_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    # 2. Load ALL trip stops with their station coordinates, ordered
    rows = (
        await db.execute(
            select(TripStop, Station)
            .outerjoin(Station, TripStop.station_id == Station.id)
            .where(TripStop.trip_id == trip_id)
            .order_by(TripStop.stop_order)
        )
    ).all()

    # Keep only stops that have valid coordinates
    waypoints = [
        (stop, station)
        for stop, station in rows
        if station
        and station.latitude  is not None
        and station.longitude is not None
    ]

    if len(waypoints) < 2:
        raise HTTPException(
            status_code=422,
            detail="Not enough stations with coordinates for this trip.",
        )

    # 3. A* between each consecutive pair of stations → concatenate
    full_path: list[tuple[float, float]] = []

    for i in range(len(waypoints) - 1):
        _, from_st = waypoints[i]
        _, to_st   = waypoints[i + 1]

        segment = railway_graph.a_star(
            from_st.longitude, from_st.latitude,
            to_st.longitude,   to_st.latitude,
        )
        if segment is None:
            continue  # skip unreachable segments, keep the rest

        if full_path:
            # Avoid duplicating the junction point
            full_path.extend(segment[1:])
        else:
            full_path.extend(segment)

    if not full_path:
        raise HTTPException(
            status_code=404,
            detail="No railway path found for this trip's stations.",
        )

    _, first_station = waypoints[0]
    _, last_station  = waypoints[-1]

    result = {
        "path":         full_path,
        "points":       len(full_path),
        "from_station": first_station.name_ar,
        "to_station":   last_station.name_ar,
        "from_lat":     first_station.latitude,
        "from_lon":     first_station.longitude,
        "to_lat":       last_station.latitude,
        "to_lon":       last_station.longitude,
    }

    # 4. Cache per trip_id — routes with same endpoints but different stops
    #    get their own cache entries.
    await cache_set(cache_key, result, ttl=_TRIP_PATH_TTL)

    return result


@router.get("/trip-stations/{trip_id}", dependencies=[Depends(require_authenticated_user)])
async def get_trip_stations(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Return all stops for a trip with coordinates so the Flutter map can
    display a pin marker at each station.

    Only stops whose Station record has lat/lon populated are included.
    Result is cached for 1 h (station data rarely changes).
    """
    cache_key = f"railway:stations:{trip_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    rows = (
        await db.execute(
            select(TripStop, Station)
            .outerjoin(Station, TripStop.station_id == Station.id)
            .where(TripStop.trip_id == trip_id)
            .order_by(TripStop.stop_order)
        )
    ).all()

    stations = [
        {
            "order":    stop.stop_order,
            "name_ar":  stop.station_ar,
            "name_en":  stop.station_en,
            "lat":      station.latitude,
            "lon":      station.longitude,
            "time_ar":  stop.time_ar,
            "time_en":  stop.time_en,
        }
        for stop, station in rows
        if station
        and station.latitude  is not None
        and station.longitude is not None
    ]

    result = {"stations": stations, "count": len(stations)}
    await cache_set(cache_key, result, ttl=3600)   # 1 h
    return result
