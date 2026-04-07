from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.cache import cache_delete_pattern
from app.core.database import get_db
from app.crud.trains import train_crud
from app.schemas.train import (
    TrainCreate,
    TrainListResponse,
    TrainRead,
    TrainSearchParams,
    TrainUpdate,
)

router = APIRouter(prefix="/trains", tags=["trains"])


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


@router.post("", response_model=TrainRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_fulladmin)])
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
    await cache_delete_pattern("trains:*")
    return result


@router.patch("/{train_number}", response_model=TrainRead, dependencies=[Depends(require_fulladmin)])
async def update_train(
    train_number: str,
    payload: TrainUpdate,
    db: AsyncSession = Depends(get_db),
):
    train = await train_crud.get_by_train_id(db, train_number)
    if not train:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Train not found")
    result = await train_crud.update_from_schema(db, db_obj=train, obj_in=payload)
    await cache_delete_pattern("trains:*")
    return result


@router.delete("/{train_number}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_fulladmin)])
async def delete_train(
    train_number: str,
    db: AsyncSession = Depends(get_db),
):
    train = await train_crud.get_by_train_id(db, train_number)
    if not train:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Train not found")
    await train_crud.delete(db, record_id=train.id)
    await cache_delete_pattern("trains:*")
