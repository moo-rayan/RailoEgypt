"""Admin endpoints for managing trip fares."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.core.database import get_db
from app.models.station import Station
from app.models.trip_fare import TripFare

router = APIRouter(prefix="/fares", tags=["fares"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class FareItem(BaseModel):
    id: int
    train_number: str
    from_station_id: int
    from_station_ar: str
    from_station_en: str
    to_station_id: int
    to_station_ar: str
    to_station_en: str
    class_name_ar: str
    class_name_en: str
    price: int


class FareListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[FareItem]


class FareUpdate(BaseModel):
    class_name_ar: str | None = None
    class_name_en: str | None = None
    price: int | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _base_query():
    """Base select with station joins."""
    from_st = Station.__table__.alias("from_st")
    to_st = Station.__table__.alias("to_st")

    return (
        select(
            TripFare.id,
            TripFare.train_number,
            TripFare.from_station_id,
            from_st.c.name_ar.label("from_station_ar"),
            from_st.c.name_en.label("from_station_en"),
            TripFare.to_station_id,
            to_st.c.name_ar.label("to_station_ar"),
            to_st.c.name_en.label("to_station_en"),
            TripFare.class_name_ar,
            TripFare.class_name_en,
            TripFare.price,
        )
        .join(from_st, TripFare.from_station_id == from_st.c.id)
        .join(to_st, TripFare.to_station_id == to_st.c.id)
    ), from_st, to_st


def _apply_filters(query, count_query, from_st, to_st, *,
                    from_station, to_station, train_number, fare_class):
    """Apply search/filter conditions to both query and count query."""
    from sqlalchemy import or_
    if from_station:
        cond = or_(
            from_st.c.name_ar.ilike(f"%{from_station}%"),
            from_st.c.name_en.ilike(f"%{from_station}%"),
        )
        query = query.where(cond)
        count_query = count_query.where(cond)
    if to_station:
        cond = or_(
            to_st.c.name_ar.ilike(f"%{to_station}%"),
            to_st.c.name_en.ilike(f"%{to_station}%"),
        )
        query = query.where(cond)
        count_query = count_query.where(cond)
    if train_number:
        cond = TripFare.train_number == train_number
        query = query.where(cond)
        count_query = count_query.where(cond)
    if fare_class:
        cond = TripFare.class_name_en == fare_class
        query = query.where(cond)
        count_query = count_query.where(cond)
    return query, count_query


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", response_model=FareListResponse, dependencies=[Depends(require_admin)])
async def list_fares(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    from_station: str | None = Query(None, description="Search origin station (English)"),
    to_station: str | None = Query(None, description="Search destination station (English)"),
    train_number: str | None = Query(None, description="Exact train number"),
    fare_class: str | None = Query(None, description="Exact class name (English)"),
    db: AsyncSession = Depends(get_db),
):
    query, from_st, to_st = _base_query()

    # Count query with same joins for filters
    count_q = (
        select(func.count(TripFare.id))
        .join(from_st, TripFare.from_station_id == from_st.c.id)
        .join(to_st, TripFare.to_station_id == to_st.c.id)
    )

    query, count_q = _apply_filters(
        query, count_q, from_st, to_st,
        from_station=from_station,
        to_station=to_station,
        train_number=train_number,
        fare_class=fare_class,
    )

    # Total count
    total = (await db.execute(count_q)).scalar() or 0

    # Paginated data
    offset = (page - 1) * page_size
    query = query.order_by(TripFare.train_number, TripFare.from_station_id, TripFare.price)
    query = query.offset(offset).limit(page_size)

    rows = (await db.execute(query)).all()
    items = [
        FareItem(
            id=r.id,
            train_number=r.train_number,
            from_station_id=r.from_station_id,
            from_station_ar=r.from_station_ar,
            from_station_en=r.from_station_en,
            to_station_id=r.to_station_id,
            to_station_ar=r.to_station_ar,
            to_station_en=r.to_station_en,
            class_name_ar=r.class_name_ar,
            class_name_en=r.class_name_en,
            price=r.price,
        )
        for r in rows
    ]

    return FareListResponse(total=total, page=page, page_size=page_size, items=items)


@router.get("/classes", dependencies=[Depends(require_admin)])
async def list_fare_classes(db: AsyncSession = Depends(get_db)):
    """Get all distinct fare class names."""
    result = await db.execute(
        select(TripFare.class_name_ar, TripFare.class_name_en)
        .distinct()
        .order_by(TripFare.class_name_en)
    )
    return [{"ar": r.class_name_ar, "en": r.class_name_en} for r in result.all()]


@router.patch("/{fare_id}", response_model=FareItem, dependencies=[Depends(require_admin)])
async def update_fare(
    fare_id: int,
    payload: FareUpdate,
    db: AsyncSession = Depends(get_db),
):
    fare = await db.get(TripFare, fare_id)
    if not fare:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fare not found")

    if payload.class_name_ar is not None:
        fare.class_name_ar = payload.class_name_ar
    if payload.class_name_en is not None:
        fare.class_name_en = payload.class_name_en
    if payload.price is not None:
        fare.price = payload.price

    await db.commit()
    await db.refresh(fare)

    return FareItem(
        id=fare.id,
        train_number=fare.train_number,
        from_station_id=fare.from_station_id,
        from_station_ar=fare.from_station.name_ar,
        from_station_en=fare.from_station.name_en,
        to_station_id=fare.to_station_id,
        to_station_ar=fare.to_station.name_ar,
        to_station_en=fare.to_station.name_en,
        class_name_ar=fare.class_name_ar,
        class_name_en=fare.class_name_en,
        price=fare.price,
    )


@router.delete("/{fare_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_fare(
    fare_id: int,
    db: AsyncSession = Depends(get_db),
):
    fare = await db.get(TripFare, fare_id)
    if not fare:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fare not found")
    await db.delete(fare)
    await db.commit()
