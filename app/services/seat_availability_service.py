"""Read-only ENR availability; seat states are never inferred from totals."""

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.station import Station
from app.models.trip import Trip
from app.services.train_seat_layout_importer import (
    CAIRO_TZ,
    ENR_SEARCH_URL,
    _class_info,
    _iter_steps,
    _normalize_coach,
)

_upstream_slots = asyncio.Semaphore(4)


def validate_departure_date(value: date, today: date | None = None) -> None:
    today = today or datetime.now(CAIRO_TZ).date()
    if not today <= value <= today + timedelta(days=15):
        raise HTTPException(422, detail="availability_date_out_of_range")


def normalize_availability(
    payload: Any, *, train_number: str, from_id: str, to_id: str,
) -> list[dict[str, Any]]:
    coaches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for step in _iter_steps(payload):
        train = step.get("train") or {}
        if str(train.get("name", "")).strip() != train_number:
            continue
        if str(step.get("fromId")) != from_id or str(step.get("toId")) != to_id:
            continue
        for raw in train.get("servicePoints") or []:
            if not isinstance(raw, dict) or raw.get("type", "COACH") != "COACH":
                continue
            coach_id = str(raw.get("id") or "")
            coach_key = (coach_id, str(raw.get("name") or ""), str(raw.get("workOrderId") or ""))
            if not coach_id or coach_key in seen:
                continue
            seen.add(coach_key)
            info = _class_info(raw)
            normalized = _normalize_coach(raw, len(coaches) + 1)
            available_ids = (
                {str(value) for value in raw["availableSeats"]}
                if isinstance(raw.get("availableSeats"), list) else None
            )
            places = {
                str(place.get("id")): place
                for place in raw.get("places") or [] if isinstance(place, dict)
            }
            available_numbers: list[str] = []
            seats = []
            for seat in normalized["seats"]:
                place = places[seat["enr_place_id"]]
                params = place.get("params") or {}
                # Missing directions use the map's existing row-based fallback.
                if "direction" not in params and "dir" not in params:
                    seat["direction"] = -1
                available = (
                    seat["enr_place_id"] in available_ids
                    if available_ids is not None else place.get("available") is True
                ) and place.get("locked") is not True and place.get("sold") is not True
                if available:
                    available_numbers.append(seat["number"])
                position = (1 if seat["is_window"] else 0) + (2 if seat["is_aisle"] else 0)
                seats.append([
                    seat["number"], seat["x"], seat["y"], position,
                    seat["row_index"], seat["direction"],
                ])
            declared = normalized["declared_seats_count"] or len(seats)
            has_map = (
                bool(seats) and len(seats) >= declared and
                (available_ids.issubset(places) if available_ids is not None else
                 all("available" in place for place in places.values()))
            )
            available_count = (
                len(available_numbers) if has_map else
                len(available_ids) if available_ids is not None else
                normalized["declared_seat_count"] or 0
            )
            try:
                price = round(float(raw["cost"]) / 100, 2) if raw.get("cost") is not None else None
            except (TypeError, ValueError):
                price = None
            coaches.append({
                "id": coach_id,
                "name": normalized["coach_name"],
                "class_code": info["code"],
                "class_name_ar": info["name_ar"],
                "class_name_en": info["name_en"],
                "price": price,
                "currency": step.get("currency") or "EGP",
                "available_count": available_count,
                "unavailable_count": max(0, declared - available_count),
                "available_seat_numbers": available_numbers,
                "has_seat_map": has_map,
                "layout": {
                    "o": normalized["coach_order"], "n": normalized["coach_name"],
                    "sc": len(seats), "wc": normalized["window_seat_count"],
                    "ac": normalized["aisle_seat_count"], "rc": normalized["row_count"],
                    "s": seats if has_map else [],
                },
            })
    return coaches


async def get_seat_availability(
    db: AsyncSession, *, train_number: str, departure_date: date,
    trip_id: int | None = None, from_station_id: int | None = None,
    to_station_id: int | None = None,
) -> dict[str, Any]:
    validate_departure_date(departure_date)
    query = select(Trip).options(selectinload(Trip.stops)).where(Trip.train_number == train_number)
    if trip_id is not None:
        query = query.where(Trip.id == trip_id)
    trip = (await db.execute(query.order_by(Trip.id).limit(1))).unique().scalar_one_or_none()
    if trip is None:
        raise HTTPException(404, detail="availability_train_not_found")
    from_station_id = from_station_id or trip.from_station_id
    to_station_id = to_station_id or trip.to_station_id
    route = list(dict.fromkeys([
        trip.from_station_id,
        *(stop.station_id for stop in trip.stops),
        trip.to_station_id,
    ]))
    if (from_station_id is None or to_station_id is None or
        from_station_id not in route or to_station_id not in route or
        route.index(from_station_id) >= route.index(to_station_id)):
        raise HTTPException(422, detail="availability_invalid_route")
    stations = (await db.execute(select(Station).where(
        Station.id.in_([from_station_id, to_station_id]),
    ))).scalars().all()
    by_id = {station.id: station for station in stations}
    start, finish = by_id.get(from_station_id), by_id.get(to_station_id)
    if not start or not finish or not start.enr_station_id or not finish.enr_station_id:
        raise HTTPException(422, detail="availability_station_mapping_missing")

    from_enr_id, to_enr_id = start.enr_station_id.strip(), finish.enr_station_id.strip()
    from_info = {"id": start.id, "name_ar": start.name_ar, "name_en": start.name_en}
    to_info = {"id": finish.id, "name_ar": finish.name_ar, "name_en": finish.name_en}
    # Return the DB connection to its pool before waiting on the external service.
    await db.commit()

    params = {
        "from": from_enr_id, "to": to_enr_id,
        "transfers": "false", "with_reservations": "true",
        "without_reservations": "false", "skip_places_information": "false",
        "departureDate": departure_date.isoformat(), "trainNumber": train_number,
        "searchMode": "WEB", "project": "enr",
    }
    try:
        async with asyncio.timeout(45):
            async with _upstream_slots:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30, connect=10),
                    headers={"Accept": "application/json", "Referer": "https://obs.enr.gov.eg/"},
                ) as client:
                    response = await client.get(ENR_SEARCH_URL, params=params)
                    response.raise_for_status()
                    payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Unexpected availability response")
        coaches = normalize_availability(
            payload, train_number=train_number,
            from_id=from_enr_id, to_id=to_enr_id,
        )
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(504, detail="availability_timeout") from exc
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(502, detail="availability_upstream_unavailable") from exc
    return {
        "train_number": train_number, "departure_date": departure_date.isoformat(),
        "from_station": from_info,
        "to_station": to_info,
        "queried_at": datetime.now(CAIRO_TZ).isoformat(),
        "coaches": coaches,
    }
