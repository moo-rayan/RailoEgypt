from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.station import Station
from app.models.train import Train
from app.models.trip import Trip

ROW_TOLERANCE = 18
ENR_SEARCH_URL = "https://obs.enr.gov.eg/api/v1/tickets/search"
CAIRO_TZ = ZoneInfo("Africa/Cairo")


class SeatLayoutImportError(Exception):
    """Raised when ENR seat layout import cannot continue."""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _position_type(is_window: bool, is_aisle: bool) -> str:
    if is_window and is_aisle:
        return "window_aisle"
    if is_window:
        return "window"
    if is_aisle:
        return "aisle"
    return "inner"


def _seat_direction_from_value(value: Any) -> int:
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


def _class_info(coach: dict[str, Any]) -> dict[str, str]:
    coach_class = coach.get("coachClass") if isinstance(coach.get("coachClass"), dict) else {}
    params = coach_class.get("params") if isinstance(coach_class.get("params"), dict) else {}
    localization = (
        coach_class.get("localizationMap")
        if isinstance(coach_class.get("localizationMap"), dict)
        else {}
    )
    class_code = (
        _clean(params.get("code"))
        or _clean(coach_class.get("shortName"))
        or _clean(coach_class.get("name"))
        or "UNKNOWN"
    )
    return {
        "code": class_code,
        "name_ar": _clean(params.get("ar")) or _clean(localization.get("ar")),
        "name_en": (
            _clean(params.get("en"))
            or _clean(localization.get("en"))
            or _clean(coach_class.get("name"))
            or class_code
        ),
        "enr_class_id": _clean(coach_class.get("id")),
        "pax_class": _clean(params.get("pax_class")),
    }


def _iter_steps(data: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            steps.extend(_iter_steps(item))
    elif isinstance(data, dict):
        if isinstance(data.get("steps"), list):
            steps.extend(step for step in data["steps"] if isinstance(step, dict))
        else:
            for key in ("data", "items", "results", "trips"):
                if isinstance(data.get(key), list):
                    for item in data[key]:
                        steps.extend(_iter_steps(item))
    return steps


def _analyze_rows(places: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, int], int]:
    sorted_places = sorted(
        places,
        key=lambda place: _number((place.get("topLeft") or {}).get("y")),
    )
    rows: list[dict[str, Any]] = []

    for place in sorted_places:
        y = _number((place.get("topLeft") or {}).get("y"))
        row = next(
            (item for item in rows if abs(float(item["center_y"]) - y) <= ROW_TOLERANCE),
            None,
        )
        if row is None:
            row = {"center_y": y, "places": []}
            rows.append(row)
        row["places"].append(place)
        row["center_y"] = sum(
            _number((item.get("topLeft") or {}).get("y")) for item in row["places"]
        ) / len(row["places"])

    rows.sort(key=lambda row: float(row["center_y"]))
    row_by_place_id: dict[int, int] = {}
    normalized_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        row_places = row["places"]
        for place in row_places:
            row_by_place_id[id(place)] = row_index
        normalized_rows.append(
            {
                "index": row_index,
                "center_y": round(float(row["center_y"]), 2),
                "seat_count": len(row_places),
            }
        )

    aisle_before = max(0, (len(rows) - 2) // 2)
    largest_gap = float("-inf")
    for index in range(max(0, len(rows) - 1)):
        gap = float(rows[index + 1]["center_y"]) - float(rows[index]["center_y"])
        if gap > largest_gap:
            largest_gap = gap
            aisle_before = index

    return normalized_rows, row_by_place_id, aisle_before


def _normalize_coach(coach: dict[str, Any], coach_order: int) -> dict[str, Any]:
    places = [
        place
        for place in _as_list(coach.get("places"))
        if isinstance(place, dict) and isinstance(place.get("topLeft"), dict)
    ]
    rows, row_by_place_id, aisle_before = _analyze_rows(places)
    params = coach.get("params") if isinstance(coach.get("params"), dict) else {}

    seats: list[dict[str, Any]] = []
    for place in places:
        top_left = place.get("topLeft") or {}
        place_params = place.get("params") if isinstance(place.get("params"), dict) else {}
        row_index = row_by_place_id.get(id(place), -1)
        is_window = bool(rows) and (row_index == 0 or row_index == len(rows) - 1)
        is_aisle = row_index == aisle_before or row_index == aisle_before + 1
        x = _int_or_none(top_left.get("x"))
        y = _int_or_none(top_left.get("y"))
        seats.append(
            {
                "enr_place_id": _clean(place.get("id")),
                "number": _clean(place.get("number")),
                "x": x if x is not None else _number(top_left.get("x")),
                "y": y if y is not None else _number(top_left.get("y")),
                "row_index": row_index,
                "position_type": _position_type(is_window, is_aisle),
                "is_window": is_window,
                "is_aisle": is_aisle,
                "direction": _seat_direction_from_value(
                    place_params.get("direction", place_params.get("dir"))
                ),
            }
        )

    seats.sort(
        key=lambda seat: (
            _number(seat["x"]),
            _number(seat["y"]),
            _int_or_none(seat["number"]) if _int_or_none(seat["number"]) is not None else 10**9,
            str(seat["number"]),
        )
    )

    return {
        "coach_order": coach_order,
        "coach_name": _clean(coach.get("name")) or str(coach_order),
        "enr_coach_id": _clean(coach.get("id")),
        "type": _clean(coach.get("type")) or "COACH",
        "code": _clean(params.get("code")),
        "declared_seats_count": _int_or_none(params.get("seats_count")),
        "declared_seat_count": _int_or_none(params.get("seatCount")),
        "coach_rows": _int_or_none(params.get("coach_rows")),
        "coach_cols": _int_or_none(params.get("coach_cols")),
        "row_count": len(rows),
        "aisle_before_row": aisle_before,
        "seat_count": len(seats),
        "window_seat_count": sum(1 for seat in seats if seat["is_window"]),
        "aisle_seat_count": sum(1 for seat in seats if seat["is_aisle"]),
        "rows": rows,
        "seats": seats,
    }


def _layout_hash(layout: dict[str, Any]) -> str:
    raw = json.dumps(layout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_from_step(
    step: dict[str, Any],
    source_file: str,
    stats: Counter[str],
) -> list[dict[str, Any]]:
    train = step.get("train") if isinstance(step.get("train"), dict) else {}
    train_number = (
        _clean(train.get("name"))
        or _clean(train.get("trainNumber"))
        or _clean(train.get("number"))
    )
    if not train_number:
        stats["missing_train_number"] += 1
        return []

    coaches_by_class: dict[str, dict[str, Any]] = {}
    for coach in _as_list(train.get("servicePoints")):
        if not isinstance(coach, dict):
            continue
        places = _as_list(coach.get("places"))
        if not places:
            continue
        class_info = _class_info(coach)
        group = coaches_by_class.setdefault(
            class_info["code"],
            {
                "class_info": class_info,
                "coaches": [],
            },
        )
        group["coaches"].append(coach)

    candidates: list[dict[str, Any]] = []
    for class_code, group in coaches_by_class.items():
        normalized_coaches = [
            _normalize_coach(coach, index + 1)
            for index, coach in enumerate(group["coaches"])
        ]
        class_info = group["class_info"]
        seat_count = sum(coach["seat_count"] for coach in normalized_coaches)
        window_seat_count = sum(coach["window_seat_count"] for coach in normalized_coaches)
        aisle_seat_count = sum(coach["aisle_seat_count"] for coach in normalized_coaches)
        layout = {
            "schema_version": 2,
            "train_number": train_number,
            "enr_train_id": _clean(train.get("id")),
            "class": class_info,
            "coach_count": len(normalized_coaches),
            "seat_count": seat_count,
            "window_seat_count": window_seat_count,
            "aisle_seat_count": aisle_seat_count,
            "coaches": normalized_coaches,
        }
        candidates.append(
            {
                "train_number": train_number,
                "class_code": class_code,
                "class_name_ar": class_info["name_ar"],
                "class_name_en": class_info["name_en"],
                "enr_train_id": _clean(train.get("id")),
                "coach_count": len(normalized_coaches),
                "seat_count": seat_count,
                "window_seat_count": window_seat_count,
                "aisle_seat_count": aisle_seat_count,
                "layout_hash": _layout_hash(layout),
                "layout": layout,
                "source_file": source_file,
            }
        )

    return candidates


def extract_layout_candidates(data: Any, source_file: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    stats: Counter[str] = Counter()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    steps = _iter_steps(data)
    stats["steps"] = len(steps)

    for step in steps:
        for candidate in _candidate_from_step(step, source_file, stats):
            key = (candidate["train_number"], candidate["class_code"])
            existing = by_key.get(key)
            if existing:
                if existing["layout_hash"] == candidate["layout_hash"]:
                    stats["duplicate_same_layout"] += 1
                else:
                    stats["duplicate_replaced_layout"] += 1
            by_key[key] = candidate

    return list(by_key.values()), stats


async def import_layout_candidates(
    db: AsyncSession,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    train_numbers = sorted({candidate["train_number"] for candidate in candidates})
    train_rows = (
        await db.execute(text('SELECT train_id FROM "EgRailway".trains'))
    ).mappings()
    known_trains = {str(row["train_id"]) for row in train_rows}

    missing_train_numbers = sorted(set(train_numbers) - known_trains)
    valid_candidates = [
        candidate for candidate in candidates if candidate["train_number"] in known_trains
    ]

    existing_rows = (
        await db.execute(
            text(
                'SELECT train_number, class_code, layout_hash '
                'FROM "EgRailway".train_seat_layouts'
            )
        )
    ).mappings()
    existing_hashes = {
        (str(row["train_number"]), str(row["class_code"])): str(row["layout_hash"])
        for row in existing_rows
    }

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for candidate in valid_candidates:
        key = (candidate["train_number"], candidate["class_code"])
        existing_hash = existing_hashes.get(key)
        if not existing_hash:
            inserts.append(candidate)
        elif existing_hash != candidate["layout_hash"]:
            updates.append(candidate)
        else:
            unchanged.append(candidate)

    if inserts or updates:
        payloads = [
            {
                **candidate,
                "layout": json.dumps(candidate["layout"], ensure_ascii=False),
            }
            for candidate in inserts + updates
        ]
        await db.execute(
            text(
                'INSERT INTO "EgRailway".train_seat_layouts AS tsl '
                "(train_number, class_code, class_name_ar, class_name_en, "
                "enr_train_id, coach_count, seat_count, window_seat_count, "
                "aisle_seat_count, layout_hash, layout, source_file, imported_at) "
                "VALUES (:train_number, :class_code, :class_name_ar, :class_name_en, "
                ":enr_train_id, :coach_count, :seat_count, :window_seat_count, "
                ":aisle_seat_count, :layout_hash, CAST(:layout AS jsonb), "
                ":source_file, now()) "
                "ON CONFLICT (train_number, class_code) DO UPDATE SET "
                "class_name_ar = EXCLUDED.class_name_ar, "
                "class_name_en = EXCLUDED.class_name_en, "
                "enr_train_id = EXCLUDED.enr_train_id, "
                "coach_count = EXCLUDED.coach_count, "
                "seat_count = EXCLUDED.seat_count, "
                "window_seat_count = EXCLUDED.window_seat_count, "
                "aisle_seat_count = EXCLUDED.aisle_seat_count, "
                "layout_hash = EXCLUDED.layout_hash, "
                "layout = EXCLUDED.layout, "
                "source_file = EXCLUDED.source_file, "
                "imported_at = now(), "
                "updated_at = now() "
                "WHERE tsl.layout_hash IS DISTINCT FROM EXCLUDED.layout_hash"
            ),
            payloads,
        )
        await db.commit()

    return {
        "extracted_layouts": len(candidates),
        "valid_layouts": len(valid_candidates),
        "missing_train_numbers": missing_train_numbers,
        "inserted": len(inserts),
        "updated": len(updates),
        "unchanged": len(unchanged),
        "inserted_train_numbers": sorted({row["train_number"] for row in inserts}),
        "updated_train_numbers": sorted({row["train_number"] for row in updates}),
        "unchanged_train_numbers": sorted({row["train_number"] for row in unchanged}),
    }


async def _station_for_train_endpoint(
    db: AsyncSession,
    train: Train,
    *,
    start: bool,
) -> Station | None:
    station_id_column = Trip.from_station_id if start else Trip.to_station_id
    trip_result = await db.execute(
        select(station_id_column)
        .where(Trip.train_number == train.train_id, station_id_column.is_not(None))
        .order_by(Trip.id)
        .limit(1)
    )
    station_id = trip_result.scalar_one_or_none()
    if station_id is not None:
        station = await db.get(Station, station_id)
        if station:
            return station

    name_ar = train.start_station_ar if start else train.end_station_ar
    name_en = train.start_station_en if start else train.end_station_en
    result = await db.execute(
        select(Station).where(
            or_(
                Station.name_ar == name_ar,
                Station.name_en == name_en,
            )
        )
    )
    return result.scalar_one_or_none()


async def fetch_enr_search_payload(
    *,
    from_enr_station_id: str,
    to_enr_station_id: str,
    departure_date: str,
) -> tuple[dict[str, Any] | list[Any], str]:
    params = {
        "from": from_enr_station_id,
        "to": to_enr_station_id,
        "transfers": "false",
        "with_reservations": "true",
        "without_reservations": "false",
        "skip_places_information": "false",
        "departureDate": departure_date,
        "searchMode": "WEB",
        "project": "enr",
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "TrainLiveEG-Dashboard/1.0",
        "Referer": "https://obs.enr.gov.eg/",
    }
    async with httpx.AsyncClient(timeout=90, headers=headers) as client:
        response = await client.get(ENR_SEARCH_URL, params=params)
        response.raise_for_status()
        return response.json(), str(response.url)


async def import_layouts_from_enr_for_train(
    db: AsyncSession,
    train_number: str,
) -> dict[str, Any]:
    train_result = await db.execute(select(Train).where(Train.train_id == train_number))
    train = train_result.scalar_one_or_none()
    if not train:
        raise SeatLayoutImportError("Train not found")

    from_station = await _station_for_train_endpoint(db, train, start=True)
    to_station = await _station_for_train_endpoint(db, train, start=False)
    if not from_station or not to_station:
        raise SeatLayoutImportError("Train route stations were not found")
    if not from_station.enr_station_id or not to_station.enr_station_id:
        raise SeatLayoutImportError("Train route stations do not have ENR station IDs")

    departure_date = (datetime.now(CAIRO_TZ).date() + timedelta(days=2)).isoformat()
    data, request_url = await fetch_enr_search_payload(
        from_enr_station_id=from_station.enr_station_id,
        to_enr_station_id=to_station.enr_station_id,
        departure_date=departure_date,
    )
    candidates, stats = extract_layout_candidates(
        data,
        source_file=f"enr:{train_number}:{departure_date}",
    )
    import_result = await import_layout_candidates(db, candidates)
    imported_target = train_number in {
        *import_result["inserted_train_numbers"],
        *import_result["updated_train_numbers"],
        *import_result["unchanged_train_numbers"],
    }

    return {
        "ok": True,
        "train_number": train_number,
        "departure_date": departure_date,
        "from_station": {
            "name_ar": from_station.name_ar,
            "enr_station_id": from_station.enr_station_id,
        },
        "to_station": {
            "name_ar": to_station.name_ar,
            "enr_station_id": to_station.enr_station_id,
        },
        "request_url": request_url,
        "target_train_found": imported_target,
        "steps": stats["steps"],
        "duplicate_same_layout": stats["duplicate_same_layout"],
        "duplicate_replaced_layout": stats["duplicate_replaced_layout"],
        **import_result,
    }
