from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Train(Base):
    __tablename__ = "trains"
    __table_args__ = (
        Index("idx_trains_train_id", "train_id"),
        Index("idx_trains_type_en", "type_en"),
        Index("idx_trains_start_en", "start_station_en"),
        Index("idx_trains_end_en", "end_station_en"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    train_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    type_ar: Mapped[str] = mapped_column(Text, nullable=False)
    type_en: Mapped[str] = mapped_column(Text, nullable=False)
    start_station_ar: Mapped[str] = mapped_column(Text, nullable=False)
    start_station_en: Mapped[str] = mapped_column(Text, nullable=False)
    end_station_ar: Mapped[str] = mapped_column(Text, nullable=False)
    end_station_en: Mapped[str] = mapped_column(Text, nullable=False)
    stops_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    trips: Mapped[list["Trip"]] = relationship("Trip", back_populates="train")
