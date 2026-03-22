from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base

ModelT = TypeVar("ModelT", bound=Base)


class CRUDBase(Generic[ModelT]):
    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    async def get(self, db: AsyncSession, record_id: int) -> ModelT | None:
        result = await db.execute(select(self.model).where(self.model.id == record_id))
        return result.scalar_one_or_none()

    async def get_multi(
        self,
        db: AsyncSession,
        *,
        page: int = 1,
        page_size: int = 20,
        filters: list[Any] | None = None,
    ) -> tuple[int, list[ModelT]]:
        base_query = select(self.model)
        count_query = select(func.count()).select_from(self.model)

        if filters:
            for f in filters:
                base_query = base_query.where(f)
                count_query = count_query.where(f)

        total_result = await db.execute(count_query)
        total = total_result.scalar_one()

        offset = (page - 1) * page_size
        result = await db.execute(base_query.offset(offset).limit(page_size))
        items = list(result.scalars().all())

        return total, items

    async def create(self, db: AsyncSession, *, obj_in: dict[str, Any]) -> ModelT:
        db_obj = self.model(**obj_in)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self, db: AsyncSession, *, db_obj: ModelT, obj_in: dict[str, Any]
    ) -> ModelT:
        for field, value in obj_in.items():
            if value is not None:
                setattr(db_obj, field, value)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, *, record_id: int) -> bool:
        obj = await self.get(db, record_id)
        if obj is None:
            return False
        await db.delete(obj)
        await db.flush()
        return True
