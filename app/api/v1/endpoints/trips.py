from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.cache import cache_delete_pattern, cache_get, cache_set
from app.core.database import get_db
from app.crud.trips import trip_crud
from app.models.trip import TripStop
from app.schemas.trip import TripListOut, TripOut, TripStopOut

router = APIRouter(prefix="/trips", tags=["trips"])

_SEARCH_TTL = 1800   # 30 min
_DETAIL_TTL = 3600   # 1 hour


@router.get("", response_model=dict, dependencies=[Depends(get_admin_or_legacy_key)])
async def search_trips(
    from_station:    str | None = Query(None, description="اسم محطة الانطلاق (عربي أو إنجليزي)"),
    to_station:      str | None = Query(None, description="اسم محطة الوصول (عربي أو إنجليزي)"),
    from_station_id: int | None = Query(None, description="ID محطة الانطلاق"),
    to_station_id:   int | None = Query(None, description="ID محطة الوصول"),
    station_id:      int | None = Query(None, description="ID محطة - يبحث في كل الرحلات المارة بها"),
    train_number:    str | None = Query(None),
    skip:            int        = Query(0, ge=0),
    limit:           int        = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    ck = f"trips:s:{from_station or ''}:{to_station or ''}:{from_station_id or ''}:{to_station_id or ''}:{station_id or ''}:{train_number or ''}:{skip}:{limit}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    total, trips = await trip_crud.search(
        db,
        from_station_ar=from_station,
        from_station_id=from_station_id,
        to_station_ar=to_station,
        to_station_id=to_station_id,
        stop_station_id=station_id,
        train_number=train_number,
        skip=skip,
        limit=limit,
    )
    result = {
        "total": total,
        "skip":  skip,
        "limit": limit,
        "items": [TripListOut.model_validate(t).model_dump(mode="json") for t in trips],
    }
    await cache_set(ck, result, ttl=_SEARCH_TTL)
    return result


@router.get("/{trip_id}", response_model=TripOut, dependencies=[Depends(get_admin_or_legacy_key)])
async def get_trip(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
):
    ck = f"trips:d:{trip_id}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    trip = await trip_crud.get_by_id(db, trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    result = TripOut.model_validate(trip).model_dump(mode="json")
    await cache_set(ck, result, ttl=_DETAIL_TTL)
    return result


# ── Trip stop management ─────────────────────────────────────────────────────

class AddStopRequest(BaseModel):
    station_id: int
    stop_order: int
    time_ar: str
    time_en: str = ""


@router.post(
    "/{trip_id}/stops",
    response_model=TripStopOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_fulladmin)],
)
async def add_trip_stop(
    trip_id: int,
    body: AddStopRequest,
    db: AsyncSession = Depends(get_db),
):
    """Add a new stop to a trip, shifting existing stops to make room."""
    trip = await trip_crud.get_by_id(db, trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    # Clamp stop_order: min=1, max=len+1 (append)
    max_order = max((s.stop_order for s in trip.stops), default=0)
    insert_at = max(1, min(body.stop_order, max_order + 1))

    # Shift all stops with stop_order >= insert_at up by 1
    await db.execute(
        update(TripStop)
        .where(TripStop.trip_id == trip_id, TripStop.stop_order >= insert_at)
        .values(stop_order=TripStop.stop_order + 1)
    )

    stop = TripStop(
        trip_id=trip_id,
        station_id=body.station_id,
        stop_order=insert_at,
        time_ar=body.time_ar,
        time_en=body.time_en or body.time_ar,
    )
    db.add(stop)
    trip.stops_count = len(trip.stops) + 1
    await db.commit()
    await db.refresh(stop, ["station"])
    await cache_delete_pattern("trips:*")
    return TripStopOut.model_validate(stop)


@router.delete(
    "/{trip_id}/stops/{stop_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_fulladmin)],
)
async def remove_trip_stop(
    trip_id: int,
    stop_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a stop and re-sequence the remaining stops."""
    result = await db.execute(
        select(TripStop).where(
            TripStop.id == stop_id,
            TripStop.trip_id == trip_id,
        )
    )
    stop = result.scalar_one_or_none()
    if not stop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stop not found")

    deleted_order = stop.stop_order
    await db.delete(stop)
    await db.flush()

    # Shift all stops after the deleted one down by 1
    await db.execute(
        update(TripStop)
        .where(TripStop.trip_id == trip_id, TripStop.stop_order > deleted_order)
        .values(stop_order=TripStop.stop_order - 1)
    )

    trip = await trip_crud.get_by_id(db, trip_id)
    if trip:
        trip.stops_count = max(0, len(trip.stops) - 1)

    await db.commit()
    await cache_delete_pattern("trips:*")
    return {"ok": True}


@router.get("/by-train/{train_number}", response_model=list[TripOut], dependencies=[Depends(get_admin_or_legacy_key)])
async def get_trips_by_train(
    train_number: str,
    db: AsyncSession = Depends(get_db),
):
    ck = f"trips:t:{train_number}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    trips = await trip_crud.get_by_train_number(db, train_number)
    if not trips:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No trips found for this train")
    result = [TripOut.model_validate(t).model_dump(mode="json") for t in trips]
    await cache_set(ck, result, ttl=_DETAIL_TTL)
    return result
