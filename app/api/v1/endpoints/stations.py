from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.cache import cache_get, cache_set, cache_delete_pattern
from app.core.database import get_db
from app.crud.stations import station_crud
from app.schemas.station import StationCreate, StationListResponse, StationRead, StationUpdate

router = APIRouter(prefix="/stations", tags=["stations"])

_LIST_TTL = 1800     # 30 min
_DETAIL_TTL = 3600   # 1 hour


@router.get("", response_model=StationListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def list_stations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    ck = f"stations:l:{page}:{page_size}:{active_only}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    filters = []
    if active_only:
        from app.models.station import Station
        filters.append(Station.is_active.is_(True))
    total, items = await station_crud.get_multi(db, page=page, page_size=page_size, filters=filters or None)
    result = StationListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")
    await cache_set(ck, result, ttl=_LIST_TTL)
    return result


@router.get("/search", response_model=StationListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def search_stations(
    q: str = Query(..., min_length=1, description="اسم المحطة بالعربي أو الإنجليزي"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    ck = f"stations:s:{q}:{page}:{page_size}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    total, items = await station_crud.search(db, query=q, page=page, page_size=page_size)
    result = StationListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")
    await cache_set(ck, result, ttl=_LIST_TTL)
    return result


@router.get("/{station_id}", response_model=StationRead, dependencies=[Depends(get_admin_or_legacy_key)])
async def get_station(
    station_id: int,
    db: AsyncSession = Depends(get_db),
):
    ck = f"stations:d:{station_id}"
    cached = await cache_get(ck)
    if cached is not None:
        return cached

    station = await station_crud.get(db, station_id)
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    result = StationRead.model_validate(station).model_dump(mode="json")
    await cache_set(ck, result, ttl=_DETAIL_TTL)
    return result


@router.post("", response_model=StationRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_fulladmin)])
async def create_station(
    payload: StationCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await station_crud.create_from_schema(db, obj_in=payload)
    await cache_delete_pattern("stations:*")
    return result


@router.patch("/{station_id}", response_model=StationRead, dependencies=[Depends(require_fulladmin)])
async def update_station(
    station_id: int,
    payload: StationUpdate,
    db: AsyncSession = Depends(get_db),
):
    station = await station_crud.get(db, station_id)
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    result = await station_crud.update_from_schema(db, db_obj=station, obj_in=payload)
    await cache_delete_pattern("stations:*")
    return result


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_fulladmin)])
async def delete_station(
    station_id: int,
    db: AsyncSession = Depends(get_db),
):
    deleted = await station_crud.delete(db, record_id=station_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    await cache_delete_pattern("stations:*")
