from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key, require_admin
from app.core.database import get_db
from app.crud.stations import station_crud
from app.schemas.station import StationCreate, StationListResponse, StationRead, StationUpdate

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("", response_model=StationListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def list_stations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if active_only:
        from app.models.station import Station
        filters.append(Station.is_active.is_(True))
    total, items = await station_crud.get_multi(db, page=page, page_size=page_size, filters=filters or None)
    return StationListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")


@router.get("/search", response_model=StationListResponse, dependencies=[Depends(get_admin_or_legacy_key)])
async def search_stations(
    q: str = Query(..., min_length=1, description="اسم المحطة بالعربي أو الإنجليزي"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    total, items = await station_crud.search(db, query=q, page=page, page_size=page_size)
    return StationListResponse(total=total, page=page, page_size=page_size, items=items).model_dump(mode="json")


@router.get("/{station_id}", response_model=StationRead, dependencies=[Depends(get_admin_or_legacy_key)])
async def get_station(
    station_id: int,
    db: AsyncSession = Depends(get_db),
):
    station = await station_crud.get(db, station_id)
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    return StationRead.model_validate(station).model_dump(mode="json")


@router.post("", response_model=StationRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_station(
    payload: StationCreate,
    db: AsyncSession = Depends(get_db),
):
    return await station_crud.create_from_schema(db, obj_in=payload)


@router.patch("/{station_id}", response_model=StationRead, dependencies=[Depends(require_admin)])
async def update_station(
    station_id: int,
    payload: StationUpdate,
    db: AsyncSession = Depends(get_db),
):
    station = await station_crud.get(db, station_id)
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    return await station_crud.update_from_schema(db, db_obj=station, obj_in=payload)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_station(
    station_id: int,
    db: AsyncSession = Depends(get_db),
):
    deleted = await station_crud.delete(db, record_id=station_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
