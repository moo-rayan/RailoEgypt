from datetime import date

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_authenticated_user
from app.services.seat_availability_service import get_seat_availability

router = APIRouter(prefix="/seat-availability", tags=["seat-availability"])


@router.get("/{train_number}", dependencies=[Depends(require_authenticated_user)])
async def search_seat_availability(
    response: Response,
    train_number: str = Path(..., pattern=r"^[0-9]{1,10}$"),
    departure_date: date = Query(...),
    trip_id: int | None = Query(None, gt=0),
    from_station_id: int | None = Query(None, gt=0),
    to_station_id: int | None = Query(None, gt=0),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return await get_seat_availability(
        db, train_number=train_number, departure_date=departure_date,
        trip_id=trip_id, from_station_id=from_station_id, to_station_id=to_station_id,
    )
