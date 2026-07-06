"""
Secure offline data bundle endpoint.

GET /data/version  → lightweight version check
GET /data/bundle   → AES-256 encrypted bundle of all stations, trips, trains, trip_paths
"""

import copy
import gzip
import hashlib
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
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
from app.services.train_seat_layout_importer import (
    SeatLayoutImportError,
    import_layouts_from_enr_for_train,
)

BUNDLE_REDIS_VERSION_KEY = "bundle:current_version"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data-bundle"])


class SeatLayoutAdminUpdate(BaseModel):
    layout: dict[str, Any] = Field(..., description="Full train seat layout JSON")


class SeatLayoutApplyToTypeRequest(BaseModel):
    train_type_ar: str = Field(..., min_length=1)
    layout: dict[str, Any] = Field(..., description="Full train seat layout JSON")


class SeatLayoutAdminCreateRequest(BaseModel):
    train_number: str = Field(..., min_length=1)
    class_code: str = Field(..., min_length=1)
    class_name_ar: str = Field(..., min_length=1)
    class_name_en: str = ""
    coach_count: int = Field(1, ge=1, le=40)
    seats_per_coach: int = Field(24, ge=1, le=120)


class SeatLayoutAdminCopyRequest(BaseModel):
    target_train_number: str = Field(..., min_length=1)
    source_layout_id: int | None = None
    source_train_type_ar: str | None = None
    source_class_code: str | None = None


def _seat_layout_hash(layout: dict[str, Any]) -> str:
    raw = json.dumps(layout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seat_flag_from_position_type(seat: dict[str, Any], flag: str) -> bool:
    position_type = str(seat.get("position_type") or "inner")
    if flag == "window":
        return bool(seat.get("is_window")) or position_type in ("window", "window_aisle")
    if flag == "aisle":
        return bool(seat.get("is_aisle")) or position_type in ("aisle", "window_aisle")
    return False


def _coerce_seat_direction(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    direction = str(value or "").strip().lower()
    if direction in {
        "1",
        "true",
        "reverse",
        "reversed",
        "backward",
        "back",
        "right",
        "down",
        "bottom",
    }:
        return 1
    return 0


def _coerce_coordinate(value: Any) -> int | float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if number.is_integer() else round(number, 2)


def _prepare_admin_seat_layout_for_target(
    *,
    train_number: str,
    enr_train_id: str,
    layout: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(layout, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Layout must be a JSON object",
        )

    coaches = layout.get("coaches")
    if not isinstance(coaches, list) or not coaches:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Layout must contain at least one coach",
        )

    total_seats = 0
    total_window = 0
    total_aisle = 0
    for coach_index, coach in enumerate(coaches):
        if not isinstance(coach, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Coach #{coach_index + 1} must be a JSON object",
            )

        seats = coach.get("seats")
        if not isinstance(seats, list) or not seats:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Coach #{coach_index + 1} must contain seats",
            )

        coach_window = 0
        coach_aisle = 0
        seen_numbers: set[str] = set()
        for seat_index, seat in enumerate(seats):
            if not isinstance(seat, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Seat #{seat_index + 1} in coach #{coach_index + 1} "
                        "must be a JSON object"
                    ),
                )
            seat_number = str(seat.get("number") or "").strip()
            if not seat_number:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Seat #{seat_index + 1} in coach #{coach_index + 1} "
                        "does not have a number"
                    ),
                )
            if seat_number in seen_numbers:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Duplicate seat number {seat_number} in coach "
                        f"#{coach_index + 1}"
                    ),
                )
            seen_numbers.add(seat_number)
            seat["number"] = seat_number

            seat["x"] = _coerce_coordinate(seat.get("x"))
            seat["y"] = _coerce_coordinate(seat.get("y"))
            seat["position_type"] = str(seat.get("position_type") or "inner")
            seat["is_window"] = _seat_flag_from_position_type(seat, "window")
            seat["is_aisle"] = _seat_flag_from_position_type(seat, "aisle")
            seat["direction"] = _coerce_seat_direction(
                seat.get("direction", seat.get("seat_direction", seat.get("dir")))
            )
            coach_window += 1 if seat["is_window"] else 0
            coach_aisle += 1 if seat["is_aisle"] else 0

        coach["seat_count"] = len(seats)
        coach["window_seat_count"] = coach_window
        coach["aisle_seat_count"] = coach_aisle
        if not coach.get("coach_order"):
            coach["coach_order"] = coach_index + 1
        if not coach.get("coach_name"):
            coach["coach_name"] = str(coach["coach_order"])

        total_seats += len(seats)
        total_window += coach_window
        total_aisle += coach_aisle

    prepared = dict(layout)
    prepared["schema_version"] = max(2, int(prepared.get("schema_version") or 1))
    prepared["train_number"] = train_number
    prepared["enr_train_id"] = enr_train_id
    prepared["coach_count"] = len(coaches)
    prepared["seat_count"] = total_seats
    prepared["window_seat_count"] = total_window
    prepared["aisle_seat_count"] = total_aisle

    return prepared, {
        "coach_count": len(coaches),
        "seat_count": total_seats,
        "window_seat_count": total_window,
        "aisle_seat_count": total_aisle,
    }


def _prepare_admin_seat_layout(
    row: TrainSeatLayout,
    layout: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    return _prepare_admin_seat_layout_for_target(
        train_number=row.train_number,
        enr_train_id=row.enr_train_id,
        layout=layout,
    )


def _build_manual_seat_layout(
    *,
    train_number: str,
    class_code: str,
    class_name_ar: str,
    class_name_en: str,
    coach_count: int,
    seats_per_coach: int,
) -> dict[str, Any]:
    y_positions = [-72, -24, 24, 72]
    seats_in_row = len(y_positions)
    row_gap = 56
    coaches: list[dict[str, Any]] = []

    for coach_index in range(coach_count):
        seats: list[dict[str, Any]] = []
        for seat_index in range(seats_per_coach):
            column_index = seat_index % seats_in_row
            row_index = seat_index // seats_in_row
            is_window = column_index in (0, seats_in_row - 1)
            is_aisle = column_index in (1, 2)
            seats.append({
                "enr_place_id": (
                    f"manual:{train_number}:{class_code}:"
                    f"{coach_index + 1}:{seat_index + 1}"
                ),
                "number": str(seat_index + 1),
                "x": row_index * row_gap,
                "y": y_positions[column_index],
                "row_index": row_index,
                "position_type": "window" if is_window else "aisle",
                "is_window": is_window,
                "is_aisle": is_aisle,
                "direction": row_index % 2,
            })

        coaches.append({
            "coach_order": coach_index + 1,
            "coach_name": str(coach_index + 1),
            "enr_coach_id": f"manual:{train_number}:{class_code}:{coach_index + 1}",
            "type": class_name_ar,
            "code": class_code,
            "row_count": (seats_per_coach + seats_in_row - 1) // seats_in_row,
            "aisle_before_row": 0,
            "seat_count": len(seats),
            "window_seat_count": sum(1 for seat in seats if seat["is_window"]),
            "aisle_seat_count": sum(1 for seat in seats if seat["is_aisle"]),
            "rows": [],
            "seats": seats,
        })

    return {
        "schema_version": 2,
        "train_number": train_number,
        "enr_train_id": "",
        "class": {
            "code": class_code,
            "name_ar": class_name_ar,
            "name_en": class_name_en,
            "enr_class_id": "",
            "pax_class": "",
        },
        "coach_count": coach_count,
        "seat_count": coach_count * seats_per_coach,
        "window_seat_count": sum(
            coach["window_seat_count"] for coach in coaches
        ),
        "aisle_seat_count": sum(coach["aisle_seat_count"] for coach in coaches),
        "coaches": coaches,
    }


def _admin_seat_layout_summary(row: TrainSeatLayout) -> dict[str, Any]:
    return {
        "id": row.id,
        "train_number": row.train_number,
        "class_code": row.class_code,
        "class_name_ar": row.class_name_ar,
        "class_name_en": row.class_name_en,
        "enr_train_id": row.enr_train_id,
        "coach_count": row.coach_count,
        "seat_count": row.seat_count,
        "window_seat_count": row.window_seat_count,
        "aisle_seat_count": row.aisle_seat_count,
        "layout_hash": row.layout_hash,
        "source_file": row.source_file,
        "imported_at": row.imported_at.isoformat() if row.imported_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _compact_seat_layout(layout_row: TrainSeatLayout) -> dict:
    """Return a mobile-friendly compact seat layout payload."""
    raw_layout = layout_row.layout or {}
    compact_coaches: list[dict] = []
    for coach in raw_layout.get("coaches", []) or []:
        compact_seats = []
        for seat in coach.get("seats", []) or []:
            position_type = seat.get("position_type") or "inner"
            direction_value = seat.get(
                "direction",
                seat.get("seat_direction", seat.get("dir")),
            )
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
                (
                    _coerce_seat_direction(direction_value)
                    if direction_value is not None
                    else -1
                ),
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
        compact_stops = []
        for st in t.stops:
            stop_payload = {
                "id": st.id,
                "o": st.stop_order,
                "si": st.station_id,
                "sa": st.station_ar,
                "se": st.station_en,
                "ta": st.time_ar,
                "te": st.time_en,
            }
            passing_note = str(st.passing_note or "").strip()
            if not passing_note and st.passing_train_numbers:
                passing_note = "، ".join(
                    str(train_number).strip()
                    for train_number in (st.passing_train_numbers or [])
                    if str(train_number).strip()
                    and str(train_number).strip() != str(t.train_number)
                )
            if passing_note:
                stop_payload["pn"] = passing_note
            compact_stops.append(stop_payload)

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
            "stops": compact_stops,
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


@router.get(
    "/seat-layouts/admin",
    dependencies=[Depends(require_admin)],
)
async def list_admin_seat_layouts(
    db: AsyncSession = Depends(get_db),
):
    """Admin-only seat layout index with editable layout ids."""
    rows = (
        await db.execute(
            select(TrainSeatLayout)
            .join(Train, Train.train_id == TrainSeatLayout.train_number)
            .where(Train.is_active.is_(True))
            .order_by(TrainSeatLayout.train_number, TrainSeatLayout.class_code)
        )
    ).scalars().all()
    version_info = await _build_seat_layouts_version_info(db)
    return {
        **version_info,
        "layouts": [_admin_seat_layout_summary(row) for row in rows],
    }


@router.post(
    "/seat-layouts/admin",
    dependencies=[Depends(require_admin)],
)
async def create_admin_seat_layout(
    payload: SeatLayoutAdminCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a manual editable seat layout for a train/class."""
    train_number = payload.train_number.strip()
    class_code = payload.class_code.strip()
    class_name_ar = payload.class_name_ar.strip()
    class_name_en = payload.class_name_en.strip()
    if not class_code or not class_name_ar:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Class code and Arabic class name are required",
        )

    target_train = (
        await db.execute(
            select(Train).where(
                Train.train_id == train_number,
                Train.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if target_train is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target train not found",
        )

    existing = (
        await db.execute(
            select(TrainSeatLayout).where(
                TrainSeatLayout.train_number == train_number,
                TrainSeatLayout.class_code == class_code,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seat layout already exists for this train/class",
        )

    raw_layout = _build_manual_seat_layout(
        train_number=train_number,
        class_code=class_code,
        class_name_ar=class_name_ar,
        class_name_en=class_name_en,
        coach_count=payload.coach_count,
        seats_per_coach=payload.seats_per_coach,
    )
    prepared_layout, counts = _prepare_admin_seat_layout_for_target(
        train_number=train_number,
        enr_train_id="",
        layout=raw_layout,
    )
    layout_hash = _seat_layout_hash(prepared_layout)
    row = TrainSeatLayout(
        train_number=train_number,
        class_code=class_code,
        class_name_ar=class_name_ar,
        class_name_en=class_name_en,
        enr_train_id="",
        coach_count=counts["coach_count"],
        seat_count=counts["seat_count"],
        window_seat_count=counts["window_seat_count"],
        aisle_seat_count=counts["aisle_seat_count"],
        layout_hash=layout_hash,
        layout=prepared_layout,
        source_file="dashboard:manual-create",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    version_info = await _build_seat_layouts_version_info(db)

    return {
        "ok": True,
        "layout": {
            **_admin_seat_layout_summary(row),
            "layout": row.layout,
        },
        "version_info": version_info,
    }


@router.post(
    "/seat-layouts/admin/copy",
    dependencies=[Depends(require_admin)],
)
async def copy_admin_seat_layout(
    payload: SeatLayoutAdminCopyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Copy a layout from a specific train/class or from a train type/class."""
    target_train_number = payload.target_train_number.strip()
    has_source_layout = payload.source_layout_id is not None
    has_source_type = bool(
        (payload.source_train_type_ar or "").strip()
        and (payload.source_class_code or "").strip()
    )
    if has_source_layout == has_source_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Provide either source_layout_id or "
                "source_train_type_ar with source_class_code"
            ),
        )

    target_train = (
        await db.execute(
            select(Train).where(
                Train.train_id == target_train_number,
                Train.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if target_train is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target train not found",
        )

    if payload.source_layout_id is not None:
        source_row = await db.get(TrainSeatLayout, payload.source_layout_id)
    else:
        source_type = (payload.source_train_type_ar or "").strip()
        source_class_code = (payload.source_class_code or "").strip()
        source_row = (
            await db.execute(
                select(TrainSeatLayout)
                .join(Train, Train.train_id == TrainSeatLayout.train_number)
                .where(
                    Train.is_active.is_(True),
                    Train.type_ar == source_type,
                    TrainSeatLayout.class_code == source_class_code,
                )
                .order_by(
                    TrainSeatLayout.updated_at.desc(),
                    TrainSeatLayout.train_number,
                )
            )
        ).scalars().first()

    if source_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source seat layout not found",
        )
    if source_row.train_number == target_train_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Source and target train are the same",
        )

    existing = (
        await db.execute(
            select(TrainSeatLayout).where(
                TrainSeatLayout.train_number == target_train_number,
                TrainSeatLayout.class_code == source_row.class_code,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target train already has this class layout",
        )

    prepared_layout, counts = _prepare_admin_seat_layout_for_target(
        train_number=target_train_number,
        enr_train_id="",
        layout=copy.deepcopy(source_row.layout),
    )
    layout_hash = _seat_layout_hash(prepared_layout)
    row = TrainSeatLayout(
        train_number=target_train_number,
        class_code=source_row.class_code,
        class_name_ar=source_row.class_name_ar,
        class_name_en=source_row.class_name_en,
        enr_train_id="",
        coach_count=counts["coach_count"],
        seat_count=counts["seat_count"],
        window_seat_count=counts["window_seat_count"],
        aisle_seat_count=counts["aisle_seat_count"],
        layout_hash=layout_hash,
        layout=prepared_layout,
        source_file=f"dashboard:copy:{source_row.train_number}:{source_row.id}",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    version_info = await _build_seat_layouts_version_info(db)

    return {
        "ok": True,
        "source_layout": _admin_seat_layout_summary(source_row),
        "layout": {
            **_admin_seat_layout_summary(row),
            "layout": row.layout,
        },
        "version_info": version_info,
    }


@router.get(
    "/seat-layouts/admin/{layout_id}",
    dependencies=[Depends(require_admin)],
)
async def get_admin_seat_layout(
    layout_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Admin-only full raw layout payload for visual editing."""
    row = await db.get(TrainSeatLayout, layout_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seat layout not found",
        )
    return {
        **_admin_seat_layout_summary(row),
        "layout": row.layout,
    }


@router.patch(
    "/seat-layouts/admin/{layout_id}",
    dependencies=[Depends(require_admin)],
)
async def update_admin_seat_layout(
    layout_id: int,
    payload: SeatLayoutAdminUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Save a manually edited raw seat layout and bump the public layout version."""
    row = await db.get(TrainSeatLayout, layout_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seat layout not found",
        )

    prepared_layout, counts = _prepare_admin_seat_layout(row, payload.layout)
    layout_hash = _seat_layout_hash(prepared_layout)

    row.layout = prepared_layout
    row.coach_count = counts["coach_count"]
    row.seat_count = counts["seat_count"]
    row.window_seat_count = counts["window_seat_count"]
    row.aisle_seat_count = counts["aisle_seat_count"]
    row.layout_hash = layout_hash
    row.source_file = "dashboard:manual-editor"

    await db.commit()
    await db.refresh(row)
    version_info = await _build_seat_layouts_version_info(db)

    return {
        "ok": True,
        "layout": {
            **_admin_seat_layout_summary(row),
            "layout": row.layout,
        },
        "version_info": version_info,
    }


@router.delete(
    "/seat-layouts/admin/{layout_id}",
    dependencies=[Depends(require_admin)],
)
async def delete_admin_seat_layout(
    layout_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete one train/class seat layout."""
    row = await db.get(TrainSeatLayout, layout_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seat layout not found",
        )

    await db.delete(row)
    await db.commit()
    version_info = await _build_seat_layouts_version_info(db)
    return {
        "ok": True,
        "deleted": 1,
        "layout_id": layout_id,
        "version_info": version_info,
    }


@router.delete(
    "/seat-layouts/admin/{layout_id}/type",
    dependencies=[Depends(require_admin)],
)
async def delete_admin_seat_layout_for_type(
    layout_id: int,
    train_type_ar: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Delete the selected class layout from all active trains of a type."""
    source_row = await db.get(TrainSeatLayout, layout_id)
    if source_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seat layout not found",
        )

    clean_type = train_type_ar.strip()
    trains = (
        await db.execute(
            select(Train)
            .where(
                Train.is_active.is_(True),
                Train.type_ar == clean_type,
            )
            .order_by(Train.train_id)
        )
    ).scalars().all()
    if not trains:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active trains found for this type",
        )

    train_numbers = [train.train_id for train in trains]
    rows = (
        await db.execute(
            select(TrainSeatLayout).where(
                TrainSeatLayout.train_number.in_(train_numbers),
                TrainSeatLayout.class_code == source_row.class_code,
            )
        )
    ).scalars().all()

    for row in rows:
        await db.delete(row)
    await db.commit()
    version_info = await _build_seat_layouts_version_info(db)

    return {
        "ok": True,
        "train_type_ar": clean_type,
        "class_code": source_row.class_code,
        "deleted": len(rows),
        "target_trains_count": len(trains),
        "target_train_numbers": train_numbers,
        "version_info": version_info,
    }


@router.post(
    "/seat-layouts/admin/{layout_id}/apply-to-type",
    dependencies=[Depends(require_admin)],
)
async def apply_admin_seat_layout_to_type(
    layout_id: int,
    payload: SeatLayoutApplyToTypeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Apply one edited class layout to all active trains of the same type."""
    source_row = await db.get(TrainSeatLayout, layout_id)
    if source_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seat layout not found",
        )

    train_type_ar = payload.train_type_ar.strip()
    trains = (
        await db.execute(
            select(Train)
            .where(
                Train.is_active.is_(True),
                Train.type_ar == train_type_ar,
            )
            .order_by(Train.train_id)
        )
    ).scalars().all()
    if not trains:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active trains found for this type",
        )

    train_numbers = [train.train_id for train in trains]
    existing_rows = (
        await db.execute(
            select(TrainSeatLayout).where(
                TrainSeatLayout.train_number.in_(train_numbers),
                TrainSeatLayout.class_code == source_row.class_code,
            )
        )
    ).scalars().all()
    existing_by_train = {row.train_number: row for row in existing_rows}

    inserted = 0
    updated = 0
    unchanged = 0
    source_file = f"dashboard:type-editor:{train_type_ar}:{source_row.train_number}"

    for train in trains:
        target_row = existing_by_train.get(train.train_id)
        target_enr_train_id = (
            target_row.enr_train_id
            if target_row is not None
            else (
                source_row.enr_train_id
                if train.train_id == source_row.train_number
                else ""
            )
        )
        prepared_layout, counts = _prepare_admin_seat_layout_for_target(
            train_number=train.train_id,
            enr_train_id=target_enr_train_id,
            layout=copy.deepcopy(payload.layout),
        )
        layout_hash = _seat_layout_hash(prepared_layout)

        if target_row is None:
            db.add(
                TrainSeatLayout(
                    train_number=train.train_id,
                    class_code=source_row.class_code,
                    class_name_ar=source_row.class_name_ar,
                    class_name_en=source_row.class_name_en,
                    enr_train_id=target_enr_train_id,
                    coach_count=counts["coach_count"],
                    seat_count=counts["seat_count"],
                    window_seat_count=counts["window_seat_count"],
                    aisle_seat_count=counts["aisle_seat_count"],
                    layout_hash=layout_hash,
                    layout=prepared_layout,
                    source_file=source_file,
                )
            )
            inserted += 1
            continue

        if target_row.layout_hash == layout_hash:
            unchanged += 1
            continue

        target_row.class_name_ar = source_row.class_name_ar
        target_row.class_name_en = source_row.class_name_en
        target_row.enr_train_id = target_enr_train_id
        target_row.coach_count = counts["coach_count"]
        target_row.seat_count = counts["seat_count"]
        target_row.window_seat_count = counts["window_seat_count"]
        target_row.aisle_seat_count = counts["aisle_seat_count"]
        target_row.layout_hash = layout_hash
        target_row.layout = prepared_layout
        target_row.source_file = source_file
        updated += 1

    await db.commit()
    version_info = await _build_seat_layouts_version_info(db)

    return {
        "ok": True,
        "source_layout_id": layout_id,
        "train_type_ar": train_type_ar,
        "class_code": source_row.class_code,
        "target_trains_count": len(trains),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "target_train_numbers": train_numbers,
        "version_info": version_info,
    }


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


@router.post(
    "/seat-layouts/import-from-enr/{train_number}",
    dependencies=[Depends(require_admin)],
)
async def import_seat_layout_from_enr(
    train_number: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch ENR search results for the train route using departure date +2 days,
    then upsert any static seat layouts found in the response.
    """
    try:
        return await import_layouts_from_enr_for_train(db, train_number)
    except SeatLayoutImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ENR returned HTTP {exc.response.status_code}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ENR request failed: {exc.__class__.__name__}",
        ) from exc


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
