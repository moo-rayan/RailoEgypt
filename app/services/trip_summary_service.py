import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.train import Train
from app.models.trip import Trip, TripStop


def _normalize_time_value(value: str | None) -> str:
    cleaned = str(value or "").strip()
    return "" if cleaned.upper() == "EMPTY" else cleaned


def _to_ar_time(value: str) -> str:
    return value.replace("AM", "ص").replace("PM", "م").replace("am", "ص").replace("pm", "م")


def _to_en_time(value: str) -> str:
    return value.replace("ص", "AM").replace("م", "PM").replace("am", "AM").replace("pm", "PM")


def _time_ar_from_any(ar_value: str | None, en_value: str | None) -> str:
    ar = _normalize_time_value(ar_value)
    if ar:
        return _to_ar_time(ar)
    en = _normalize_time_value(en_value)
    return _to_ar_time(en)


def _time_en_from_any(en_value: str | None, ar_value: str | None) -> str:
    en = _normalize_time_value(en_value)
    if en:
        return _to_en_time(en)
    ar = _normalize_time_value(ar_value)
    return _to_en_time(ar)


def calc_duration(dep: str | None, arr: str | None) -> tuple[str, str]:
    """Calculate duration between departure and arrival time strings."""

    def _to_minutes(value: str | None) -> int | None:
        text = _normalize_time_value(value)
        if not text:
            return None
        match = re.match(r"^(\d{1,2}):(\d{2})\s*(ص|م|AM|PM|am|pm)$", text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        period = match.group(3).lower()
        if period in ("م", "pm"):
            if hour != 12:
                hour += 12
        elif hour == 12:
            hour = 0
        return hour * 60 + minute

    dep_min = _to_minutes(dep)
    arr_min = _to_minutes(arr)
    if dep_min is None or arr_min is None:
        return "", ""

    diff = arr_min - dep_min
    if diff <= 0:
        diff += 24 * 60

    hours, minutes = divmod(diff, 60)
    if hours and minutes:
        return f"{hours} س و {minutes} د", f"{hours}h {minutes}m"
    if hours:
        return f"{hours} س", f"{hours}h"
    return f"{minutes} د", f"{minutes}m"


async def _sync_primary_train_summary(
    db: AsyncSession,
    trip: Trip,
    *,
    departure_ar: str,
    departure_en: str,
    arrival_ar: str,
    arrival_en: str,
    stops_count: int,
) -> None:
    primary_trip_id = (
        await db.execute(
            select(func.min(Trip.id)).where(Trip.train_number == trip.train_number)
        )
    ).scalar_one_or_none()
    if primary_trip_id != trip.id:
        return

    train = (
        await db.execute(select(Train).where(Train.train_id == trip.train_number))
    ).scalar_one_or_none()
    if not train:
        return

    train.departure_ar = departure_ar
    train.departure_en = departure_en
    train.arrival_ar = arrival_ar
    train.arrival_en = arrival_en
    train.stops_count = stops_count


async def sync_trip_summary_from_stops(
    db: AsyncSession,
    trip_id: int,
    *,
    clear_when_empty: bool = True,
    sync_train: bool = True,
) -> None:
    """Keep trip summary fields aligned with the first and last stop."""
    trip = await db.get(Trip, trip_id)
    if not trip:
        return

    result = await db.execute(
        select(TripStop)
        .where(TripStop.trip_id == trip_id)
        .order_by(TripStop.stop_order, TripStop.id)
    )
    stops = result.scalars().all()
    trip.stops_count = len(stops)

    if not stops:
        if not clear_when_empty:
            return
        trip.from_station_id = None
        trip.to_station_id = None
        trip.departure_ar = ""
        trip.departure_en = ""
        trip.arrival_ar = ""
        trip.arrival_en = ""
        trip.duration_ar = ""
        trip.duration_en = ""
        if sync_train:
            await _sync_primary_train_summary(
                db,
                trip,
                departure_ar="",
                departure_en="",
                arrival_ar="",
                arrival_en="",
                stops_count=0,
            )
        return

    first_stop = stops[0]
    last_stop = stops[-1]
    departure_ar = _time_ar_from_any(first_stop.time_ar, first_stop.time_en)
    departure_en = _time_en_from_any(first_stop.time_en, first_stop.time_ar)
    arrival_ar = _time_ar_from_any(last_stop.time_ar, last_stop.time_en)
    arrival_en = _time_en_from_any(last_stop.time_en, last_stop.time_ar)
    duration_ar, duration_en = calc_duration(departure_ar, arrival_ar)

    trip.from_station_id = first_stop.station_id
    trip.to_station_id = last_stop.station_id
    trip.departure_ar = departure_ar
    trip.departure_en = departure_en
    trip.arrival_ar = arrival_ar
    trip.arrival_en = arrival_en
    trip.duration_ar = duration_ar
    trip.duration_en = duration_en

    if sync_train:
        await _sync_primary_train_summary(
            db,
            trip,
            departure_ar=departure_ar,
            departure_en=departure_en,
            arrival_ar=arrival_ar,
            arrival_en=arrival_en,
            stops_count=len(stops),
        )
