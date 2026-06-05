from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TrainSeatLayout(Base):
    __tablename__ = "train_seat_layouts"
    __table_args__ = (
        Index("idx_train_seat_layouts_train_number", "train_number"),
        Index("idx_train_seat_layouts_class_code", "class_code"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    train_number: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("EgRailway.trains.train_id", ondelete="CASCADE"),
        nullable=False,
    )
    class_code: Mapped[str] = mapped_column(Text, nullable=False)
    class_name_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    class_name_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enr_train_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    coach_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_seat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aisle_seat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    layout_hash: Mapped[str] = mapped_column(Text, nullable=False)
    layout: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_file: Mapped[str] = mapped_column(Text, nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
