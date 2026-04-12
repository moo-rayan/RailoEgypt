from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TripFare(Base):
    __tablename__ = "trip_fares"
    __table_args__ = (
        UniqueConstraint(
            "train_number", "from_station_id", "to_station_id", "class_name_en",
            name="uq_trip_fares_route_class",
        ),
        Index("idx_trip_fares_train_number", "train_number"),
        Index("idx_trip_fares_from_station", "from_station_id"),
        Index("idx_trip_fares_to_station",   "to_station_id"),
        Index("idx_trip_fares_route",        "from_station_id", "to_station_id"),
        Index("idx_trip_fares_class",        "class_name_en"),
        Index("idx_trip_fares_price",        "price"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Train link
    train_number: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("EgRailway.trains.train_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Station links
    from_station_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("EgRailway.stations.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_station_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("EgRailway.stations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Fare class
    class_name_ar: Mapped[str] = mapped_column(String(50), nullable=False)
    class_name_en: Mapped[str] = mapped_column(String(50), nullable=False)

    # Price
    price: Mapped[int] = mapped_column(Integer, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    train: Mapped["Train"] = relationship("Train", lazy="joined")
    from_station: Mapped["Station"] = relationship(
        "Station", foreign_keys=[from_station_id], lazy="joined"
    )
    to_station: Mapped["Station"] = relationship(
        "Station", foreign_keys=[to_station_id], lazy="joined"
    )

    @property
    def from_station_ar(self) -> str:
        return self.from_station.name_ar if self.from_station else ""

    @property
    def from_station_en(self) -> str:
        return self.from_station.name_en if self.from_station else ""

    @property
    def to_station_ar(self) -> str:
        return self.to_station.name_ar if self.to_station else ""

    @property
    def to_station_en(self) -> str:
        return self.to_station.name_en if self.to_station else ""
