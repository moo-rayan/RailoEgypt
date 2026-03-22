from datetime import datetime

from sqlalchemy import Boolean, Double, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Station(Base):
    __tablename__ = "stations"
    __table_args__ = (
        Index("idx_stations_name_ar", "name_ar"),
        Index("idx_stations_name_en", "name_en"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    place_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
