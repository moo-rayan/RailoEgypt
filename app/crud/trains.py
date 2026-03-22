from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.train import Train
from app.schemas.train import TrainCreate, TrainSearchParams, TrainUpdate


class CRUDTrain(CRUDBase[Train]):

    async def get_by_train_id(self, db: AsyncSession, train_id: str) -> Train | None:
        result = await db.execute(select(Train).where(Train.train_id == train_id))
        return result.scalar_one_or_none()

    async def search(
        self,
        db: AsyncSession,
        *,
        params: TrainSearchParams,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[int, list[Train]]:
        filters = []

        if params.from_station:
            filters.append(
                or_(
                    Train.start_station_ar.ilike(f"%{params.from_station}%"),
                    Train.start_station_en.ilike(f"%{params.from_station}%"),
                )
            )
        if params.to_station:
            filters.append(
                or_(
                    Train.end_station_ar.ilike(f"%{params.to_station}%"),
                    Train.end_station_en.ilike(f"%{params.to_station}%"),
                )
            )
        if params.train_type:
            filters.append(
                or_(
                    Train.type_ar.ilike(f"%{params.train_type}%"),
                    Train.type_en.ilike(f"%{params.train_type}%"),
                )
            )
        if params.is_active is not None:
            filters.append(Train.is_active.is_(params.is_active))

        return await self.get_multi(db, page=page, page_size=page_size, filters=filters)

    async def create_from_schema(
        self, db: AsyncSession, *, obj_in: TrainCreate
    ) -> Train:
        return await self.create(db, obj_in=obj_in.model_dump())

    async def update_from_schema(
        self, db: AsyncSession, *, db_obj: Train, obj_in: TrainUpdate
    ) -> Train:
        return await self.update(
            db, db_obj=db_obj, obj_in=obj_in.model_dump(exclude_none=True)
        )


train_crud = CRUDTrain(Train)
