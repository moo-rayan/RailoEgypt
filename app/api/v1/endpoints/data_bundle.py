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
from app.models.train_seat_layout import TrainSeatLayout
from app.models.trip import Trip, TripStop
from app.models.trip_fare import TripFare
from app.services.railway_service import railway_graph

BUNDLE_REDIS_VERSION_KEY = "bundle:current_version"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data-bundle"])


def _compact_seat_layout(layout_row: TrainSeatLayout) -> dict:
    """Return a mobile-friendly compact seat layout payload."""
    raw_layout = layout_row.layout or {}
    compact_coaches: list[dict] = []
    for coach in raw_layout.get("coaches", []) or []:
        compact_seats = []
        for seat in coach.get("seats", []) or []:
            position_type = seat.get("position_type") or "inner"
            position_code = {
                "inner": 0,
                "window": 1,
                "aisle": 2,
                "window_aisle": 3,
            }.get(position_type, 0)
            compact_seats.append([
                str(seat.get("number", "")),
                seat.get("x") or 0,
                seat.get("y") or 0,
                position_code,
                seat.get("row_index", -1),
            ])

        compact_coaches.append({
            "o": coach.get("coach_order") or 0,
            "n": str(coach.get("coach_name") or ""),
            "sc": coach.get("seat_count") or len(compact_seats),
            "wc": coach.get("window_seat_count") or 0,
            "ac": coach.get("aisle_seat_count") or 0,
            "rc": coach.get("row_count") or 0,
            "s": compact_seats,
        })

    return {
        "tn": layout_row.train_number,
        "c": layout_row.class_code,
        "a": layout_row.class_name_ar,
        "e": layout_row.class_name_en,
        "cc": layout_row.coach_count,
        "sc": layout_row.seat_count,
        "wc": layout_row.window_seat_count,
        "ac": layout_row.aisle_seat_count,
        "h": layout_row.layout_hash[:12],
        "ch": compact_coaches,
    }


async def _build_seat_layouts_payload(db: AsyncSession) -> dict:
    rows = (
        await db.execute(
            select(TrainSeatLayout)
            .join(Train, Train.train_id == TrainSeatLayout.train_number)
            .where(Train.is_active.is_(True))
            .order_by(TrainSeatLayout.train_number, TrainSeatLayout.class_code)
        )
    ).scalars().all()

    layouts_by_train: dict[str, list[dict]] = {}
    version_seed: list[str] = []
    for row in rows:
        layouts_by_train.setdefault(row.train_number, []).append(
            _compact_seat_layout(row)
        )
        version_seed.append(f"{row.train_number}:{row.class_code}:{row.layout_hash}")

    version = hashlib.sha256("|".join(sorted(version_seed)).encode("utf-8")).hexdigest()[:16]
    return {
        "version": version,
        "total": len(rows),
        "trains_count": len(layouts_by_train),
        "layouts": layouts_by_train,
    }


async def _build_seat_layouts_version_info(db: AsyncSession) -> dict:
    rows = (
        await db.execute(
            select(
                TrainSeatLayout.train_number,
                TrainSeatLayout.class_code,
                TrainSeatLayout.layout_hash,
            )
            .join(Train, Train.train_id == TrainSeatLayout.train_number)
            .where(Train.is_active.is_(True))
            .order_by(TrainSeatLayout.train_number, TrainSeatLayout.class_code)
        )
    ).all()
    version_seed = [
        f"{train_number}:{class_code}:{layout_hash}"
        for train_number, class_code, layout_hash in rows
    ]
    train_numbers = {str(train_number) for train_number, _, _ in rows}
    version = hashlib.sha256("|".join(sorted(version_seed)).encode("utf-8")).hexdigest()[:16]
    return {
        "version": version,
        "total": len(rows),
        "trains_count": len(train_numbers),
    }


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
            "aid": s.audio_id,
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

    # Trains — with deduplicated notes lookup table
    trains_result = await db.execute(
        select(Train).where(Train.is_active.is_(True)).order_by(Train.id)
    )
    all_trains = trains_result.scalars().all()

    # Build unique notes lookup: list of {"a": note_ar, "e": note_en}
    notes_map: dict[tuple[str, str], int] = {}  # (note_ar, note_en) -> index
    train_notes: list[dict] = []
    for tr in all_trains:
        if tr.note_ar or tr.note_en:
            key = (tr.note_ar.strip(), tr.note_en.strip())
            if key not in notes_map:
                notes_map[key] = len(train_notes)
                train_notes.append({"a": key[0], "e": key[1]})

    trains = []
    for tr in all_trains:
        item: dict = {
            "id": tr.id,
            "tid": tr.train_id,
            "ta": tr.type_ar,
            "te": tr.type_en,
            "ssa": tr.start_station_ar,
            "sse": tr.start_station_en,
            "esa": tr.end_station_ar,
            "ese": tr.end_station_en,
            "sc": tr.stops_count,
        }
        if tr.note_ar or tr.note_en:
            key = (tr.note_ar.strip(), tr.note_en.strip())
            item["ni"] = notes_map[key]  # note index into train_notes
        trains.append(item)

    # ── Fares — deduplicated class lookup + compact fare dict ──────────────
    fares_result = await db.execute(
        select(TripFare).order_by(TripFare.train_number, TripFare.from_station_id)
    )
    all_fares = fares_result.scalars().all()

    # Build unique fare classes lookup: [{"a": class_ar, "e": class_en}, ...]
    class_map: dict[tuple[str, str], int] = {}  # (class_ar, class_en) -> index
    fare_classes: list[dict] = []
    for f in all_fares:
        key = (f.class_name_ar, f.class_name_en)
        if key not in class_map:
            class_map[key] = len(fare_classes)
            fare_classes.append({"a": key[0], "e": key[1]})

    # Build fares dict: {train_number: {"from_id:to_id": [[class_idx, price], ...]}}
    fares_dict: dict[str, dict[str, list]] = {}
    for f in all_fares:
        train_fares = fares_dict.setdefault(f.train_number, {})
        route_key = f"{f.from_station_id}:{f.to_station_id}"
        route_fares = train_fares.setdefault(route_key, [])
        ci = class_map[(f.class_name_ar, f.class_name_en)]
        route_fares.append([ci, f.price])

    # Trip paths (A* railway routing for all trips)
    trip_paths = await _build_all_trip_paths(db)

    # Railway lines (all rail polylines for map display)
    railway_lines = railway_graph.display_lines if railway_graph.is_built else []

    return {
        "stations": stations,
        "trips": trips,
        "trains": trains,
        "train_notes": train_notes,
        "fare_classes": fare_classes,
        "fares": fares_dict,
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


@router.get("/seat-layouts/version")
async def get_seat_layouts_version(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Lightweight version check for train seat layouts."""
    response.headers["Cache-Control"] = "no-store"
    return await _build_seat_layouts_version_info(db)


@router.get("/seat-layouts")
async def get_seat_layouts(db: AsyncSession = Depends(get_db)):
    """
    Compact gzip-compressed seat layouts grouped by train number.

    Response shape:
      {
        "version": "...",
        "total": 35,
        "trains_count": 25,
        "layouts": {
          "1902": [{ "c": "AC 1", "a": "...", "ch": [...] }]
        }
      }
    """
    payload = await _build_seat_layouts_payload(db)
    gzip_bytes = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=6,
    )
    return Response(
        content=gzip_bytes,
        media_type="application/json",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "no-store",
        },
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
