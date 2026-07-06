from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key, require_admin
from app.core.cache import cache_delete_pattern
from app.core.database import get_db
from app.crud.trains import train_crud
from app.crud.stations import station_crud
from app.models.trip import Trip
from app.schemas.train import (
    TrainCreate,
    TrainListResponse,
    TrainRead,
    TrainSearchParams,
    TrainUpdate,
)

router = APIRouter(prefix="/trains", tags=["trains"])


def _calc_duration(dep: str, arr: str) -> tuple[str, str]:
    """Calculate duration between departure and arrival time strings.
    Accepts formats like '6:20 AM', '6:20 ص', '3:50 م', '3:50 PM'.
    Returns (duration_ar, duration_en) e.g. ('1 س و 30 د', '1h 30m').
    """
    import re

    def _to_minutes(t: str) -> int | None:
        t = t.strip()
        if not t:
            return None
        m = re.match(r"(\d{1,2}):(\d{2})\s*(ص|م|AM|PM|am|pm)", t)
        if not m:
            return None
        h, mi, period = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if period in ("م", "pm"):
            if h != 12:
                h += 12
        elif h == 12:
            h = 0
        return h * 60 + mi

    dep_min = _to_minutes(dep)
    arr_min = _to_minutes(arr)
    if dep_min is None or arr_min is None:
        return ("", "")

    diff = arr_min - dep_min
    if diff <= 0:
        diff += 24 * 60  # next day

    hours, mins = divmod(diff, 60)
    if hours and mins:
        return (f"{hours} س و {mins} د", f"{hours}h {mins}m")
    elif hours:
        return (f"{hours} س", f"{hours}h")
    else:
        return (f"{mins} د", f"{mins}m")


@router.get("", response_model=TrainListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def list_trains(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    train_type: str | None = Query(None, description="نوع القطار"),
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    params = TrainSearchParams(train_type=train_type, is_active=True if active_only else None)
    total, items = await train_crud.search(db, params=params, page=page, page_size=page_size)
    return TrainListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")


@router.get("/search", response_model=TrainListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def search_trains(
    from_station: str | None = Query(None, description="محطة الانطلاق"),
    to_station: str | None = Query(None, description="محطة الوصول"),
    train_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    params = TrainSearchParams(
        from_station=from_station,
        to_station=to_station,
        train_type=train_type,
    )
    total, items = await train_crud.search(db, params=params, page=page, page_size=page_size)
    return TrainListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")


@router.get("/{train_number}", response_model=TrainRead, dependencies=[Depends(get_admin_or_legacy_key)])
async def get_train(
    train_number: str,
    db: AsyncSession = Depends(get_db),
):
    train = await train_crud.get_by_train_id(db, train_number)
    if not train:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Train not found")
    return TrainRead.model_validate(train).model_dump(mode="json")


@router.post("", response_model=TrainRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_train(
    payload: TrainCreate,
    db: AsyncSession = Depends(get_db),
):
    existing = await train_crud.get_by_train_id(db, payload.train_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Train {payload.train_id} already exists",
        )
    result = await train_crud.create_from_schema(db, obj_in=payload)

    # Auto-create a default trip linked to this train
    from_station = await station_crud.get_by_name_ar(db, payload.start_station_ar) if payload.start_station_ar else None
    to_station = await station_crud.get_by_name_ar(db, payload.end_station_ar) if payload.end_station_ar else None
    dur_ar, dur_en = _calc_duration(payload.departure_ar, payload.arrival_ar)
    trip = Trip(
        train_number=payload.train_id,
        from_station_id=from_station.id if from_station else None,
        to_station_id=to_station.id if to_station else None,
        departure_ar=payload.departure_ar,
        departure_en=payload.departure_en,
        arrival_ar=payload.arrival_ar,
        arrival_en=payload.arrival_en,
        duration_ar=dur_ar,
        duration_en=dur_en,
        stops_count=payload.stops_count,
        has_fares=False,
    )
    db.add(trip)
    await db.commit()

    await cache_delete_pattern("trains:*")
    await cache_delete_pattern("trips:*")
    return result


@router.patch("/{train_number}", response_model=TrainRead, dependencies=[Depends(require_admin)])
async def update_train(
    train_number: str,
    payload: TrainUpdate,
    db: AsyncSession = Depends(get_db),
):
    train = await train_crud.get_by_train_id(db, train_number)
    if not train:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Train not found")
    result = await train_crud.update_from_schema(db, db_obj=train, obj_in=payload)

    # Sync the primary trip (first trip) with updated train data
    trip_result = await db.execute(
        select(Trip).where(Trip.train_number == train_number).order_by(Trip.id).limit(1)
    )
    trip = trip_result.scalar_one_or_none()
    if trip:
        if payload.departure_ar is not None:
            trip.departure_ar = payload.departure_ar
        if payload.departure_en is not None:
            trip.departure_en = payload.departure_en
        if payload.arrival_ar is not None:
            trip.arrival_ar = payload.arrival_ar
        if payload.arrival_en is not None:
            trip.arrival_en = payload.arrival_en
        if payload.stops_count is not None:
            trip.stops_count = payload.stops_count
        # Recalculate duration if departure or arrival changed
        new_dep = payload.departure_ar if payload.departure_ar is not None else trip.departure_ar
        new_arr = payload.arrival_ar if payload.arrival_ar is not None else trip.arrival_ar
        dur_ar, dur_en = _calc_duration(new_dep, new_arr)
        if dur_ar:
            trip.duration_ar = dur_ar
            trip.duration_en = dur_en
        if payload.start_station_ar is not None:
            from_station = await station_crud.get_by_name_ar(db, payload.start_station_ar) if payload.start_station_ar else None
            trip.from_station_id = from_station.id if from_station else None
        if payload.end_station_ar is not None:
            to_station = await station_crud.get_by_name_ar(db, payload.end_station_ar) if payload.end_station_ar else None
            trip.to_station_id = to_station.id if to_station else None
        await db.commit()

    await cache_delete_pattern("trains:*")
    await cache_delete_pattern("trips:*")
    return result


@router.delete("/{train_number}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_train(
    train_number: str,
    db: AsyncSession = Depends(get_db),
):
    train = await train_crud.get_by_train_id(db, train_number)
    if not train:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Train not found")
    await train_crud.delete(db, record_id=train.id)
    await cache_delete_pattern("trains:*")
