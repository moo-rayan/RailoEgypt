from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key
from app.core.cache import cache_get, cache_set
from app.core.database import get_db
from app.crud.trips import trip_crud
from app.schemas.trip import TripListOut, TripOut

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
