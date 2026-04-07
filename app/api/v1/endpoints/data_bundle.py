"""
Secure offline data bundle endpoint.

GET /data/version  → lightweight version check
GET /data/bundle   → AES-256 encrypted bundle of all stations, trips, trains, trip_paths
"""

import gzip
import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.admin_auth import require_admin
from app.core.bundle_store import bundle_store
from app.core.cache import get_redis
from app.core.database import get_db
from app.core.encryption import encrypt_bundle
from app.core.r2_storage import r2_upload_bundle
from app.models.station import Station
from app.models.train import Train
from app.models.trip import Trip, TripStop
from app.services.railway_service import railway_graph

BUNDLE_REDIS_VERSION_KEY = "bundle:current_version"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data-bundle"])


async def _build_all_trip_paths(db: AsyncSession) -> dict[int, dict]:
    """
    Pre-compute A* railway paths for ALL trips.
    Returns dict[trip_id] -> {path: [[lat,lon]...], points: int, ...}
    
    This is expensive but only runs once per bundle build (cached 24h).
    """
    if not railway_graph.is_built:
        logger.warning("Railway graph not ready — trip paths will be empty")
        return {}
    
    # Load all trip stops with station coordinates
    rows = (
        await db.execute(
            select(TripStop, Station)
            .outerjoin(Station, TripStop.station_id == Station.id)
            .order_by(TripStop.trip_id, TripStop.stop_order)
        )
    ).all()
    
    # Group by trip_id
    trips_waypoints: dict[int, list] = {}
    for stop, station in rows:
        if station and station.latitude and station.longitude:
            if stop.trip_id not in trips_waypoints:
                trips_waypoints[stop.trip_id] = []
            trips_waypoints[stop.trip_id].append((stop, station))
    
    # Compute A* path for each trip
    trip_paths = {}
    for trip_id, waypoints in trips_waypoints.items():
        if len(waypoints) < 2:
            continue
        
        # A* between consecutive stations
        full_path = []
        for i in range(len(waypoints) - 1):
            _, from_st = waypoints[i]
            _, to_st = waypoints[i + 1]
            
            segment = railway_graph.a_star(
                from_st.longitude, from_st.latitude,
                to_st.longitude, to_st.latitude,
            )
            if segment is None:
                continue
            
            if full_path:
                full_path.extend(segment[1:])  # avoid duplicate junction
            else:
                full_path.extend(segment)
        
        if full_path:
            _, first_station = waypoints[0]
            _, last_station = waypoints[-1]
            trip_paths[trip_id] = {
                "p": full_path,  # path: [[lat,lon],...]
                "pc": len(full_path),  # points count
                "fsa": first_station.name_ar,
                "tsa": last_station.name_ar,
                "flat": first_station.latitude,
                "flon": first_station.longitude,
                "tlat": last_station.latitude,
                "tlon": last_station.longitude,
            }
    
    logger.info(f"Built {len(trip_paths)} trip paths for bundle")
    return trip_paths


async def _build_raw_bundle(db: AsyncSession) -> dict:
    """Fetch all data from DB and build the raw bundle dict."""

    # Stations
    stations_result = await db.execute(
        select(Station).where(Station.is_active.is_(True)).order_by(Station.id)
    )
    stations = [
        {
            "id": s.id,
            "name_ar": s.name_ar,
            "name_en": s.name_en,
            "lat": s.latitude,
            "lng": s.longitude,
        }
        for s in stations_result.scalars().all()
    ]

    # Trips with stops (eager load station relationship for name resolution)
    trips_result = await db.execute(
        select(Trip).options(selectinload(Trip.stops).selectinload(TripStop.station)).order_by(Trip.id)
    )
    trips = []
    for t in trips_result.scalars().all():
        trips.append({
            "id": t.id,
            "tn": t.train_number,
            "ta": t.type_ar,
            "te": t.type_en,
            "fsi": t.from_station_id,
            "fsa": t.from_station_ar,
            "fse": t.from_station_en,
            "tsi": t.to_station_id,
            "tsa": t.to_station_ar,
            "tse": t.to_station_en,
            "da": t.departure_ar,
            "de": t.departure_en,
            "aa": t.arrival_ar,
            "ae": t.arrival_en,
            "dua": t.duration_ar,
            "due": t.duration_en,
            "sc": t.stops_count,
            "hf": t.has_fares,
            "f": t.fares,
            "stops": [
                {
                    "id": st.id,
                    "o": st.stop_order,
                    "si": st.station_id,
                    "sa": st.station_ar,
                    "se": st.station_en,
                    "ta": st.time_ar,
                    "te": st.time_en,
                }
                for st in t.stops
            ],
        })

    # Trains
    trains_result = await db.execute(
        select(Train).where(Train.is_active.is_(True)).order_by(Train.id)
    )
    trains = [
        {
            "id": tr.id,
            "tid": tr.train_id,
            "ta": tr.type_ar,
            "te": tr.type_en,
            "ssa": tr.start_station_ar,
            "sse": tr.start_station_en,
            "esa": tr.end_station_ar,
            "ese": tr.end_station_en,
            "sc": tr.stops_count,
            "na": tr.note_ar,
            "ne": tr.note_en,
        }
        for tr in trains_result.scalars().all()
    ]

    # Trip paths (A* railway routing for all trips)
    trip_paths = await _build_all_trip_paths(db)

    # Railway lines (all rail polylines for map display)
    railway_lines = railway_graph.display_lines if railway_graph.is_built else []

    return {
        "stations": stations,
        "trips": trips,
        "trains": trains,
        "trip_paths": trip_paths,
        "railway_lines": railway_lines,
    }


def _compute_version(raw: dict) -> str:
    """SHA-256 hash of the raw bundle content → version fingerprint."""
    content = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ── Rebuild endpoint ──────────────────────────────────────────────────────────

@router.post(
    "/rebuild",
    dependencies=[Depends(require_admin)],
)
async def rebuild_data_bundle(db: AsyncSession = Depends(get_db)):
    """
    Rebuild the encrypted data bundle from current DB state,
    store in memory and upload to R2.
    """
    try:
        logger.info("Admin triggered bundle rebuild...")
        raw = await _build_raw_bundle(db)
        version = _compute_version(raw)

        old_version = bundle_store.version_info.get("version", "")[:8] if bundle_store.version_info else "none"

        version_info = {
            "version": version,
            "stations_count": len(raw["stations"]),
            "trips_count": len(raw["trips"]),
            "trains_count": len(raw["trains"]),
            "trip_paths_count": len(raw["trip_paths"]),
        }

        encrypted = encrypt_bundle(raw)
        bundle_result = {"version": version, **encrypted}
        bundle_json = json.dumps(bundle_result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        gzip_bytes = gzip.compress(bundle_json, compresslevel=6)

        # 1. Store in process memory
        bundle_store.set(gzip_bytes, version_info)

        # 2. Upload to R2
        version_bytes = json.dumps(version_info, ensure_ascii=False).encode('utf-8')
        r2_ok = await r2_upload_bundle(gzip_bytes, version_bytes)

        # 3. Signal other workers via Redis
        try:
            r = await get_redis()
            await r.set(BUNDLE_REDIS_VERSION_KEY, version)
        except Exception:
            logger.warning("Could not write bundle version to Redis")

        logger.info(
            "Bundle rebuilt: %s → %s, size=%.1fKB, R2=%s",
            old_version, version[:8],
            len(gzip_bytes) / 1024,
            "ok" if r2_ok else "failed",
        )

        return {
            "ok": True,
            "old_version": old_version,
            **version_info,
            "size_kb": round(len(gzip_bytes) / 1024, 1),
            "r2_uploaded": r2_ok,
        }

    except Exception as exc:
        logger.error("Bundle rebuild failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Bundle rebuild failed: {exc}",
        )


@router.get("/version")
async def get_data_version(response: Response):
    """
    Lightweight version check — no encryption, just the version hash.
    
    Data is pre-built at startup and served from process memory.
    """
    response.headers["Cache-Control"] = "no-store"
    if bundle_store.version_info is not None:
        return bundle_store.version_info
    
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Data bundle not ready. Server is starting up."
    )


@router.get("/bundle")
async def get_data_bundle():
    """
    AES-256-CBC encrypted bundle of all stations, trips, and trains.
    
    Data is pre-built at startup and served from process memory.
    Returns pre-compressed gzip bytes directly (zero-copy, 0ms latency).

    Response:
        {
            "version": "abc123...",
            "iv":   "<base64>",
            "data": "<base64 AES-256-CBC ciphertext>",
            "mac":  "<HMAC-SHA256 hex>",
            "chunk_hash": "<opaque chunk verification hash>"
        }
    """
    gzip_bytes = bundle_store.gzip_bytes
    if gzip_bytes is not None:
        return Response(
            content=gzip_bytes,
            media_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "no-store",
            },
        )
    
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Data bundle not ready. Server is starting up."
    )
