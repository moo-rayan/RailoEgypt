from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete_pattern, cache_get, cache_set

logger = logging.getLogger(__name__)

ENR_SEARCH_URL = "https://obs.enr.gov.eg/api/v1/tickets/search"
CAIRO_TZ = ZoneInfo("Africa/Cairo")

ENR_CLASS_TO_LOCAL: dict[str, tuple[str, str]] = {
    "AC 1": ("أولى مكيفة", "First Class AC"),
    "AC 2": ("ثانية مكيفة", "Second Class AC"),
    "AC 3": ("ثالثة مكيفة", "Third Class AC"),
    "GA 2": ("ثالثة تهوية", "Third Class (Fan)"),
    "PRIMUM": ("PRI VIP", "PRIMUM VIP"),
    "PRIMUM VIP": ("PRI VIP", "PRIMUM VIP"),
    "SLEEP SINGLE": ("كابينة منفردة", "Single Cabin"),
    "SLEEP DOUBLE": ("كابينة مزدوجة", "Double Cabin"),
}

ENR_CLASS_ID_FALLBACK: dict[str, tuple[str, str, str]] = {
    "1210000": ("AC 1", "أولى مكيفة", "First Class AC"),
    "1210001": ("AC 2", "ثانية مكيفة", "Second Class AC"),
    "1210059": ("GA 2", "ثالثة تهوية", "Third Class (Fan)"),
    "1210061": ("AC 3", "ثالثة مكيفة", "Third Class AC"),
}


@dataclass(slots=True)
class EnrFareRecord:
    train_number: str
    class_ar: str
    class_en: str
    price: int


def _price_from_enr(value: Any) -> int | None:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return int(round(amount / 100))


def _class_from_code(code: str, fallback_ar: str = "") -> tuple[str, str] | None:
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    mapped = ENR_CLASS_TO_LOCAL.get(normalized)
    if mapped:
        class_ar, class_en = mapped
        return (fallback_ar or class_ar, class_en)
    return None


def _extract_class_details(step: dict[str, Any]) -> dict[str, tuple[str, str]]:
    details: dict[str, tuple[str, str]] = {}
    train = step.get("train") or {}
    for service_point in train.get("servicePoints") or []:
        coach_class = service_point.get("coachClass") or {}
        class_id = str(coach_class.get("id") or "")
        if not class_id:
            continue
        params = coach_class.get("params") or {}
        code = (
            params.get("code")
            or params.get("en")
            or coach_class.get("shortName")
            or coach_class.get("name")
            or ""
        )
        ar_name = params.get("ar") or (coach_class.get("localizationMap") or {}).get("ar") or ""
        mapped = _class_from_code(str(code), fallback_ar=str(ar_name))
        if mapped:
            details[class_id] = mapped
    return details


def _parse_enr_response(payload: Any) -> list[EnrFareRecord]:
    if not isinstance(payload, list):
        return []

    records: list[EnrFareRecord] = []
    seen: set[tuple[str, str]] = set()

    for item in payload:
        if not isinstance(item, dict):
            continue
        steps = item.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        step = steps[0] if isinstance(steps[0], dict) else {}
        train = step.get("train") or {}
        train_number = str(train.get("name") or "").strip()
        if not train_number:
            continue

        class_details = _extract_class_details(step)
        classes_cost_map = item.get("classesCostMap") or {}
        if not isinstance(classes_cost_map, dict):
            classes_cost_map = {}

        for class_id, raw_price in classes_cost_map.items():
            class_id = str(class_id)
            price = _price_from_enr(raw_price)
            if price is None:
                continue
            class_pair = class_details.get(class_id)
            if class_pair is None:
                fallback = ENR_CLASS_ID_FALLBACK.get(class_id)
                if fallback:
                    _, class_ar, class_en = fallback
                    class_pair = (class_ar, class_en)
            if class_pair is None:
                logger.info("Unknown ENR fare class id %s for train %s", class_id, train_number)
                continue
            key = (train_number, class_pair[1])
            if key in seen:
                continue
            seen.add(key)
            records.append(
                EnrFareRecord(
                    train_number=train_number,
                    class_ar=class_pair[0],
                    class_en=class_pair[1],
                    price=price,
                )
            )
    return records


async def _fetch_enr_fares(from_enr_id: str, to_enr_id: str, departure_date: date) -> Any:
    params = {
        "from": from_enr_id,
        "to": to_enr_id,
        "transfers": "false",
        "with_reservations": "true",
        "without_reservations": "false",
        "skip_places_information": "true",
        "departureDate": departure_date.isoformat(),
        "searchMode": "WEB",
        "project": "enr",
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    }
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        response = await client.get(ENR_SEARCH_URL, params=params)
        response.raise_for_status()
        return response.json()


async def refresh_route_fares_from_enr(
    db: AsyncSession,
    *,
    from_station_id: int,
    to_station_id: int,
) -> dict[str, Any]:
    departure_date = datetime.now(CAIRO_TZ).date() + timedelta(days=2)
    cache_key = f"enr:route-fares:{from_station_id}:{to_station_id}:{departure_date.isoformat()}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    station_rows = (
        await db.execute(
            text(
                'SELECT id, name_ar, name_en, enr_station_id '
                'FROM "EgRailway".stations '
                "WHERE id IN (:from_station_id, :to_station_id)"
            ),
            {
                "from_station_id": from_station_id,
                "to_station_id": to_station_id,
            },
        )
    ).mappings().all()
    stations = {int(row["id"]): row for row in station_rows}
    from_station = stations.get(from_station_id)
    to_station = stations.get(to_station_id)
    if not from_station or not to_station:
        raise ValueError("station_not_found")
    from_enr_id = str(from_station.get("enr_station_id") or "")
    to_enr_id = str(to_station.get("enr_station_id") or "")
    if not from_enr_id or not to_enr_id:
        raise ValueError("station_missing_enr_id")

    payload = await _fetch_enr_fares(from_enr_id, to_enr_id, departure_date)
    records = _parse_enr_response(payload)
    if not records:
        result = {
            "ok": True,
            "departure_date": departure_date.isoformat(),
            "seen_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "missing_train_count": 0,
            "route_fares": {},
        }
        await cache_set(cache_key, result, ttl=300)
        return result

    train_numbers = sorted({record.train_number for record in records})
    train_rows = (
        await db.execute(
            text(
                'SELECT train_id FROM "EgRailway".trains '
                "WHERE train_id = ANY(:train_numbers)"
            ),
            {"train_numbers": train_numbers},
        )
    ).mappings().all()
    existing_trains = {str(row["train_id"]) for row in train_rows}

    existing_rows = (
        await db.execute(
            text(
                'SELECT id, train_number, class_name_ar, class_name_en, price '
                'FROM "EgRailway".trip_fares '
                "WHERE from_station_id = :from_station_id "
                "AND to_station_id = :to_station_id "
                "AND train_number = ANY(:train_numbers)"
            ),
            {
                "from_station_id": from_station_id,
                "to_station_id": to_station_id,
                "train_numbers": train_numbers,
            },
        )
    ).mappings().all()
    existing_by_key = {
        (str(row["train_number"]), str(row["class_name_en"])): row
        for row in existing_rows
    }

    route_fares: dict[str, list[dict[str, Any]]] = {}
    upserts: list[dict[str, Any]] = []
    inserted_count = 0
    updated_count = 0
    unchanged_count = 0
    missing_train_count = 0

    for record in records:
        if record.train_number not in existing_trains:
            missing_train_count += 1
            continue
        route_fares.setdefault(record.train_number, []).append(
            {
                "class_ar": record.class_ar,
                "class_en": record.class_en,
                "price": record.price,
                "source": "online",
            }
        )
        existing = existing_by_key.get((record.train_number, record.class_en))
        if existing is None:
            inserted_count += 1
            upserts.append(
                {
                    "train_number": record.train_number,
                    "from_station_id": from_station_id,
                    "to_station_id": to_station_id,
                    "class_name_ar": record.class_ar,
                    "class_name_en": record.class_en,
                    "price": record.price,
                }
            )
            continue
        if int(existing["price"]) != record.price or str(existing["class_name_ar"]) != record.class_ar:
            updated_count += 1
            upserts.append(
                {
                    "train_number": record.train_number,
                    "from_station_id": from_station_id,
                    "to_station_id": to_station_id,
                    "class_name_ar": record.class_ar,
                    "class_name_en": record.class_en,
                    "price": record.price,
                }
            )
        else:
            unchanged_count += 1

    for fares in route_fares.values():
        fares.sort(key=lambda item: int(item["price"]))

    if upserts:
        await db.execute(
            text(
                'INSERT INTO "EgRailway".trip_fares '
                "(train_number, from_station_id, to_station_id, "
                "class_name_ar, class_name_en, price, online_updated_at) "
                "VALUES (:train_number, :from_station_id, :to_station_id, "
                ":class_name_ar, :class_name_en, :price, now()) "
                "ON CONFLICT (train_number, from_station_id, to_station_id, class_name_en) "
                "DO UPDATE SET "
                "class_name_ar = EXCLUDED.class_name_ar, "
                "price = EXCLUDED.price, "
                "updated_at = now(), "
                "online_updated_at = now()"
            ),
            upserts,
        )
        await db.commit()
        await cache_delete_pattern("trips:*")

    result = {
        "ok": True,
        "departure_date": departure_date.isoformat(),
        "seen_count": len(records),
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "unchanged_count": unchanged_count,
        "missing_train_count": missing_train_count,
        "route_fares": route_fares,
    }
    await cache_set(cache_key, result, ttl=300)
    return result
