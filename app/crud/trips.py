from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.models.station import Station
from app.models.trip import Trip, TripStop


class CRUDTrip:

    async def get_by_id(self, db: AsyncSession, trip_id: int) -> Trip | None:
        result = await db.execute(
            select(Trip)
            .options(selectinload(Trip.stops).selectinload(TripStop.station))
            .where(Trip.id == trip_id)
        )
        return result.scalar_one_or_none()

    async def search(
        self,
        db: AsyncSession,
        *,
        from_station_ar: str | None = None,
        from_station_id: int | None = None,
        to_station_ar: str | None = None,
        to_station_id: int | None = None,
        stop_station_id: int | None = None,
        train_number: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[int, list[Trip]]:

        ts_from = aliased(TripStop, name="ts_from")
        ts_to   = aliased(TripStop, name="ts_to")
        ts_stop = aliased(TripStop, name="ts_stop")

        data_q  = select(Trip).options(selectinload(Trip.stops).selectinload(TripStop.station))
        count_q = select(func.count(distinct(Trip.id))).select_from(Trip)

        # ------------------------------------------------------------------
        # 0. Stop-based station ID search (finds ALL trips passing through)
        # ------------------------------------------------------------------
        if stop_station_id:
            stop_cond = and_(
                ts_stop.trip_id == Trip.id,
                ts_stop.station_id == stop_station_id,
            )
            data_q  = data_q.join(ts_stop, stop_cond).distinct()
            count_q = count_q.join(ts_stop, stop_cond)

        # ------------------------------------------------------------------
        # 1. ID-based stop search (fast integer comparison on trip_stops)
        #    When BOTH IDs provided → find trips passing through both in order
        #    When only ONE ID → terminal station check on trips table
        # ------------------------------------------------------------------
        direct: list = []
        id_handled = False

        if from_station_id and to_station_id and not stop_station_id:
            # Both stations → JOIN trip_stops twice (integer comparison, very fast)
            from_cond_id = and_(
                ts_from.trip_id == Trip.id,
                ts_from.station_id == from_station_id,
            )
            to_cond_id = and_(
                ts_to.trip_id == Trip.id,
                ts_to.station_id == to_station_id,
            )
            data_q = (
                data_q
                .join(ts_from, from_cond_id)
                .join(ts_to, to_cond_id)
                .where(ts_from.stop_order < ts_to.stop_order)
                .distinct()
            )
            count_q = (
                count_q
                .join(ts_from, from_cond_id)
                .join(ts_to, to_cond_id)
                .where(ts_from.stop_order < ts_to.stop_order)
            )
            id_handled = True
        else:
            if from_station_id and not stop_station_id:
                direct.append(Trip.from_station_id == from_station_id)
            if to_station_id and not stop_station_id:
                direct.append(Trip.to_station_id == to_station_id)

        if train_number:
            direct.append(Trip.train_number == train_number)

        # ------------------------------------------------------------------
        # 2. Stop-based name search (ILIKE fallback for when IDs unavailable)
        #    Finds trips that pass through both stations in the right order.
        # ------------------------------------------------------------------
        use_from = bool(from_station_ar and not from_station_id and not id_handled)
        use_to   = bool(to_station_ar   and not to_station_id   and not id_handled)

        st_from = aliased(Station, name="st_from")
        st_to   = aliased(Station, name="st_to")

        def from_cond():
            return and_(
                ts_from.trip_id == Trip.id,
                ts_from.station_id == st_from.id,
                or_(
                    st_from.name_ar.ilike(f"%{from_station_ar}%"),
                    st_from.name_en.ilike(f"%{from_station_ar}%"),
                ),
            )

        def to_cond():
            return and_(
                ts_to.trip_id == Trip.id,
                ts_to.station_id == st_to.id,
                or_(
                    st_to.name_ar.ilike(f"%{to_station_ar}%"),
                    st_to.name_en.ilike(f"%{to_station_ar}%"),
                ),
            )

        if use_from and use_to:
            data_q = (
                data_q
                .join(ts_from, from_cond())
                .join(ts_to,   to_cond())
                .where(ts_from.stop_order < ts_to.stop_order)
                .distinct()
            )
            count_q = (
                count_q
                .join(ts_from, from_cond())
                .join(ts_to,   to_cond())
                .where(ts_from.stop_order < ts_to.stop_order)
            )
        elif use_from:
            data_q  = data_q.join(ts_from, from_cond()).distinct()
            count_q = count_q.join(ts_from, from_cond())
        elif use_to:
            data_q  = data_q.join(ts_to, to_cond()).distinct()
            count_q = count_q.join(ts_to, to_cond())

        # Apply direct filters after joins
        if direct:
            data_q  = data_q.where(and_(*direct))
            count_q = count_q.where(and_(*direct))

        total = (await db.execute(count_q)).scalar_one()
        trips = (await db.execute(data_q.offset(skip).limit(limit))).scalars().all()
        return total, list(trips)

    async def get_by_train_number(self, db: AsyncSession, train_number: str) -> list[Trip]:
        result = await db.execute(
            select(Trip)
            .options(selectinload(Trip.stops).selectinload(TripStop.station))
            .where(Trip.train_number == train_number)
        )
        return list(result.scalars().all())


trip_crud = CRUDTrip()
