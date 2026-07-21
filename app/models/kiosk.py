from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Kiosk(Base):
    __tablename__ = "kiosks"
    __table_args__ = (
        Index("idx_kiosks_station_id", "station_id"),
        Index("idx_kiosks_station_active_open", "station_id", "is_active", "is_open"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        ForeignKey("EgRailway.stations.id", ondelete="CASCADE"),
        nullable=False,
    )
    merchant_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seller_phone: Mapped[str] = mapped_column(Text, nullable=False, default="")
    menu: Mapped[dict | list] = mapped_column(JSONB, nullable=False, default=list)
    working_hours: Mapped[dict | list] = mapped_column(JSONB, nullable=False, default=dict)
    platform_location: Mapped[str] = mapped_column(Text, nullable=False, default="right")
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_phone_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    station = relationship("Station", lazy="joined")
