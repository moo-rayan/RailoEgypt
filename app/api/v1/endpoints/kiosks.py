from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.admin_auth import require_admin
from app.core.database import get_db
from app.models.kiosk import Kiosk
from app.models.station import Station
from app.schemas.kiosk import KioskCreate, KioskListResponse, KioskRead, KioskUpdate

router = APIRouter(prefix="/kiosks", tags=["kiosks"])


async def _get_station_or_400(db: AsyncSession, station_id: int) -> Station:
    station = await db.get(Station, station_id)
    if station is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Station not found",
        )
    return station


async def _get_kiosk_or_404(db: AsyncSession, kiosk_id: int) -> Kiosk:
    result = await db.execute(
        select(Kiosk)
        .options(selectinload(Kiosk.station))
        .where(Kiosk.id == kiosk_id)
    )
    kiosk = result.scalar_one_or_none()
    if kiosk is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kiosk not found",
        )
    return kiosk


def _serialize_kiosk(kiosk: Kiosk) -> dict:
    return KioskRead.model_validate(kiosk).model_dump(mode="json")


@router.get("", response_model=KioskListResponse, dependencies=[Depends(require_admin)])
async def list_kiosks(
    q: str | None = Query(None, min_length=1),
    station_id: int | None = Query(None, ge=1),
    active_only: bool = Query(False),
    open_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if station_id is not None:
        filters.append(Kiosk.station_id == station_id)
    if active_only:
        filters.append(Kiosk.is_active.is_(True))
    if open_only:
        filters.append(Kiosk.is_open.is_(True))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Kiosk.merchant_name.ilike(like),
                Kiosk.seller_phone.ilike(like),
                Kiosk.platform_location.ilike(like),
                Station.name_ar.ilike(like),
                Station.name_en.ilike(like),
            )
        )

    base = select(Kiosk).join(Station, Kiosk.station_id == Station.id)
    count_q = select(func.count(Kiosk.id)).join(Station, Kiosk.station_id == Station.id)
    if filters:
        base = base.where(*filters)
        count_q = count_q.where(*filters)

    total = (await db.execute(count_q)).scalar() or 0
    offset = (page - 1) * page_size
    rows = (
        await db.execute(
            base.order_by(Kiosk.updated_at.desc(), Kiosk.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).scalars().unique().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize_kiosk(row) for row in rows],
    }


@router.get("/search-stations", dependencies=[Depends(require_admin)])
async def search_stations_for_kiosk(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Station.id, Station.name_ar, Station.name_en)
        .where(
            Station.is_active.is_(True),
            or_(
                Station.name_ar.ilike(f"%{q}%"),
                Station.name_en.ilike(f"%{q}%"),
            ),
        )
        .order_by(Station.name_ar)
        .limit(20)
    )
    return [{"id": r.id, "name_ar": r.name_ar, "name_en": r.name_en} for r in result.all()]


@router.post(
    "",
    response_model=KioskRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_kiosk(
    payload: KioskCreate,
    db: AsyncSession = Depends(get_db),
):
    await _get_station_or_400(db, payload.station_id)
    kiosk = Kiosk(**payload.model_dump())
    db.add(kiosk)
    await db.commit()
    created = await _get_kiosk_or_404(db, kiosk.id)
    return _serialize_kiosk(created)


@router.patch(
    "/{kiosk_id}",
    response_model=KioskRead,
    dependencies=[Depends(require_admin)],
)
async def update_kiosk(
    kiosk_id: int,
    payload: KioskUpdate,
    db: AsyncSession = Depends(get_db),
):
    kiosk = await _get_kiosk_or_404(db, kiosk_id)

    data = payload.model_dump(exclude_unset=True)
    if "station_id" in data:
        await _get_station_or_400(db, data["station_id"])

    for key, value in data.items():
        setattr(kiosk, key, value)

    await db.commit()
    updated = await _get_kiosk_or_404(db, kiosk.id)
    return _serialize_kiosk(updated)


@router.delete(
    "/{kiosk_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_kiosk(
    kiosk_id: int,
    db: AsyncSession = Depends(get_db),
):
    kiosk = await _get_kiosk_or_404(db, kiosk_id)
    await db.delete(kiosk)
    await db.commit()
