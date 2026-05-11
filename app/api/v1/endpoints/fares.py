"""Admin endpoints for managing trip fares."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.core.database import get_db
from app.core.security import require_authenticated_user
from app.models.station import Station
from app.models.trip_fare import TripFare
from app.services.enr_fare_refresh_service import refresh_route_fares_from_enr

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
    online_updated_at: datetime | None = None


class FareListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[FareItem]


class FareCreate(BaseModel):
    train_number: str
    from_station_id: int
    to_station_id: int
    class_name_ar: str
    class_name_en: str
    price: int


class FareUpdate(BaseModel):
    class_name_ar: str | None = None
    class_name_en: str | None = None
    price: int | None = None


class FareRefreshRequest(BaseModel):
    from_station_id: int
    to_station_id: int


class FareRefreshItem(BaseModel):
    class_ar: str
    class_en: str
    price: int
    source: str = "online"


class FareRefreshResponse(BaseModel):
    ok: bool
    departure_date: str
    seen_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    missing_train_count: int
    route_fares: dict[str, list[FareRefreshItem]]


class OnlineFareRouteStat(BaseModel):
    from_station_id: int
    from_station_ar: str
    from_station_en: str
    to_station_id: int
    to_station_ar: str
    to_station_en: str
    fare_count: int
    train_count: int
    last_online_update: datetime | None


class OnlineFareStatsResponse(BaseModel):
    total_online_updated: int
    updated_today: int
    routes_count: int
    trains_count: int
    last_online_update: datetime | None
    recent_routes: list[OnlineFareRouteStat]


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
            TripFare.online_updated_at,
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

@router.post(
    "/refresh-route",
    response_model=FareRefreshResponse,
    dependencies=[Depends(require_authenticated_user)],
)
async def refresh_route_fares(
    payload: FareRefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    """Fetch live ENR fares for a station pair, upsert known train fares, and return route fares."""
    try:
        return await refresh_route_fares_from_enr(
            db,
            from_station_id=payload.from_station_id,
            to_station_id=payload.to_station_id,
        )
    except ValueError as exc:
        if str(exc) == "station_not_found":
            raise HTTPException(status_code=404, detail="Station not found") from exc
        if str(exc) == "station_missing_enr_id":
            raise HTTPException(status_code=400, detail="Station is missing ENR ID") from exc
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Unable to refresh fares from ENR") from exc


@router.get(
    "/online-stats",
    response_model=OnlineFareStatsResponse,
    dependencies=[Depends(require_admin)],
)
async def get_online_fare_stats(
    limit: int = Query(8, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    """Return dashboard stats for fares refreshed from the live ENR endpoint."""
    online_filter = TripFare.online_updated_at.is_not(None)

    summary = (
        await db.execute(
            select(
                func.count(TripFare.id).label("total_online_updated"),
                func.count(func.distinct(TripFare.train_number)).label("trains_count"),
                func.max(TripFare.online_updated_at).label("last_online_update"),
            ).where(online_filter)
        )
    ).one()

    route_pairs = (
        select(TripFare.from_station_id, TripFare.to_station_id)
        .where(online_filter)
        .distinct()
        .subquery()
    )
    routes_count = (
        await db.execute(select(func.count()).select_from(route_pairs))
    ).scalar_one()

    cairo_tz = ZoneInfo("Africa/Cairo")
    cairo_today = datetime.now(cairo_tz).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    updated_today = (
        await db.execute(
            select(func.count(TripFare.id)).where(
                TripFare.online_updated_at >= cairo_today
            )
        )
    ).scalar_one()

    from_st = Station.__table__.alias("from_st")
    to_st = Station.__table__.alias("to_st")
    last_update = func.max(TripFare.online_updated_at).label("last_online_update")

    recent_rows = (
        await db.execute(
            select(
                TripFare.from_station_id,
                from_st.c.name_ar.label("from_station_ar"),
                from_st.c.name_en.label("from_station_en"),
                TripFare.to_station_id,
                to_st.c.name_ar.label("to_station_ar"),
                to_st.c.name_en.label("to_station_en"),
                func.count(TripFare.id).label("fare_count"),
                func.count(func.distinct(TripFare.train_number)).label("train_count"),
                last_update,
            )
            .join(from_st, TripFare.from_station_id == from_st.c.id)
            .join(to_st, TripFare.to_station_id == to_st.c.id)
            .where(online_filter)
            .group_by(
                TripFare.from_station_id,
                from_st.c.name_ar,
                from_st.c.name_en,
                TripFare.to_station_id,
                to_st.c.name_ar,
                to_st.c.name_en,
            )
            .order_by(last_update.desc())
            .limit(limit)
        )
    ).all()

    return OnlineFareStatsResponse(
        total_online_updated=summary.total_online_updated or 0,
        updated_today=updated_today or 0,
        routes_count=routes_count or 0,
        trains_count=summary.trains_count or 0,
        last_online_update=summary.last_online_update,
        recent_routes=[
            OnlineFareRouteStat(
                from_station_id=row.from_station_id,
                from_station_ar=row.from_station_ar,
                from_station_en=row.from_station_en,
                to_station_id=row.to_station_id,
                to_station_ar=row.to_station_ar,
                to_station_en=row.to_station_en,
                fare_count=row.fare_count,
                train_count=row.train_count,
                last_online_update=row.last_online_update,
            )
            for row in recent_rows
        ],
    )


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
            online_updated_at=r.online_updated_at,
        )
        for r in rows
    ]

    return FareListResponse(total=total, page=page, page_size=page_size, items=items)


@router.post("", response_model=FareItem, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_fare(
    payload: FareCreate,
    db: AsyncSession = Depends(get_db),
):
    from app.models.train import Train

    # Validate train exists
    result = await db.execute(select(Train).where(Train.train_id == payload.train_number))
    train = result.scalar_one_or_none()
    if not train:
        raise HTTPException(status_code=400, detail=f"Train {payload.train_number} not found")

    # Validate stations exist
    from_station = await db.get(Station, payload.from_station_id)
    if not from_station:
        raise HTTPException(status_code=400, detail="From station not found")
    to_station = await db.get(Station, payload.to_station_id)
    if not to_station:
        raise HTTPException(status_code=400, detail="To station not found")

    # Check duplicate
    existing = await db.execute(
        select(TripFare).where(
            TripFare.train_number == payload.train_number,
            TripFare.from_station_id == payload.from_station_id,
            TripFare.to_station_id == payload.to_station_id,
            TripFare.class_name_en == payload.class_name_en,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Fare already exists for this route/class")

    fare = TripFare(
        train_number=payload.train_number,
        from_station_id=payload.from_station_id,
        to_station_id=payload.to_station_id,
        class_name_ar=payload.class_name_ar,
        class_name_en=payload.class_name_en,
        price=payload.price,
    )
    db.add(fare)
    await db.commit()
    await db.refresh(fare)

    return FareItem(
        id=fare.id,
        train_number=fare.train_number,
        from_station_id=fare.from_station_id,
        from_station_ar=from_station.name_ar,
        from_station_en=from_station.name_en,
        to_station_id=fare.to_station_id,
        to_station_ar=to_station.name_ar,
        to_station_en=to_station.name_en,
        class_name_ar=fare.class_name_ar,
        class_name_en=fare.class_name_en,
        price=fare.price,
        online_updated_at=fare.online_updated_at,
    )


@router.get("/search-stations", dependencies=[Depends(require_admin)])
async def search_stations_for_fare(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Search stations by name (Arabic or English) for the create form."""
    from sqlalchemy import or_
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
        .limit(15)
    )
    return [{"id": r.id, "name_ar": r.name_ar, "name_en": r.name_en} for r in result.all()]


@router.get("/search-trains", dependencies=[Depends(require_admin)])
async def search_trains_for_fare(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Search trains by train_id for the create form."""
    from app.models.train import Train
    result = await db.execute(
        select(Train.train_id, Train.type_ar, Train.type_en)
        .where(Train.train_id.ilike(f"%{q}%"))
        .order_by(Train.train_id)
        .limit(15)
    )
    return [{"train_id": r.train_id, "type_ar": r.type_ar, "type_en": r.type_en} for r in result.all()]


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
        online_updated_at=fare.online_updated_at,
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
