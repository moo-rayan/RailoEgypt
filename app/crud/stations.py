from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.station import Station
from app.schemas.station import StationCreate, StationUpdate


class CRUDStation(CRUDBase[Station]):

    async def search(
        self,
        db: AsyncSession,
        *,
        query: str,
        page: int = 1,
        page_size: int = 20,
        active_only: bool = True,
    ) -> tuple[int, list[Station]]:
        filters = [
            or_(
                Station.name_ar.ilike(f"%{query}%"),
                Station.name_en.ilike(f"%{query}%"),
            )
        ]
        if active_only:
            filters.append(Station.is_active.is_(True))
        return await self.get_multi(db, page=page, page_size=page_size, filters=filters)

    async def get_by_name_ar(self, db: AsyncSession, name_ar: str) -> Station | None:
        from sqlalchemy import select
        result = await db.execute(
            select(Station).where(Station.name_ar == name_ar)
        )
        return result.scalar_one_or_none()

    async def get_by_name_en(self, db: AsyncSession, name_en: str) -> Station | None:
        from sqlalchemy import select
        result = await db.execute(
            select(Station).where(Station.name_en == name_en)
        )
        return result.scalar_one_or_none()

    async def create_from_schema(
        self, db: AsyncSession, *, obj_in: StationCreate
    ) -> Station:
        return await self.create(db, obj_in=obj_in.model_dump())

    async def update_from_schema(
        self, db: AsyncSession, *, db_obj: Station, obj_in: StationUpdate
    ) -> Station:
        return await self.update(
            db, db_obj=db_obj, obj_in=obj_in.model_dump(exclude_none=True)
        )


station_crud = CRUDStation(Station)
