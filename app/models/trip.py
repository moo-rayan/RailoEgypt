from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        Index("idx_trips_from_station",  "from_station_id"),
        Index("idx_trips_to_station",    "to_station_id"),
        Index("idx_trips_train_number",  "train_number"),
        Index("idx_trips_from_to",       "from_station_id", "to_station_id"),
        {"schema": "EgRailway"},
    )

    id:               Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)
    train_number:     Mapped[str]           = mapped_column(String(20),  ForeignKey("EgRailway.trains.train_id", ondelete="CASCADE"), nullable=False)
    from_station_id:  Mapped[Optional[int]] = mapped_column(Integer,     ForeignKey("EgRailway.stations.id", ondelete="SET NULL"), nullable=True)
    to_station_id:    Mapped[Optional[int]] = mapped_column(Integer,     ForeignKey("EgRailway.stations.id", ondelete="SET NULL"), nullable=True)
    departure_ar:     Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    departure_en:     Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    arrival_ar:       Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    arrival_en:       Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    duration_ar:      Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    duration_en:      Mapped[str]           = mapped_column(Text,        nullable=False, default="")
    stops_count:      Mapped[int]           = mapped_column(Integer,     nullable=False, default=0)
    fares:            Mapped[Optional[dict]] = mapped_column(JSONB,      nullable=True)
    has_fares:        Mapped[bool]          = mapped_column(Boolean,     nullable=False, default=False)
    created_at:       Mapped[datetime]      = mapped_column(nullable=False, server_default=func.now())

    train: Mapped["Train"] = relationship("Train", back_populates="trips", lazy="joined")
    from_station: Mapped[Optional["Station"]] = relationship(
        "Station", foreign_keys=[from_station_id], lazy="joined"
    )
    to_station: Mapped[Optional["Station"]] = relationship(
        "Station", foreign_keys=[to_station_id], lazy="joined"
    )
    stops: Mapped[list["TripStop"]] = relationship(
        "TripStop", back_populates="trip", cascade="all, delete-orphan", order_by="TripStop.stop_order"
    )

    @property
    def type_ar(self) -> str:
        return self.train.type_ar if self.train else ""

    @property
    def type_en(self) -> str:
        return self.train.type_en if self.train else ""

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


class TripStop(Base):
    __tablename__ = "trip_stops"
    __table_args__ = (
        Index("idx_trip_stops_trip",    "trip_id"),
        Index("idx_trip_stops_station", "station_id"),
        Index("idx_trip_stops_trip_order", "trip_id", "stop_order"),
        Index("idx_trip_stops_trip_station", "trip_id", "station_id"),
        {"schema": "EgRailway"},
    )

    id:         Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)
    trip_id:    Mapped[int]           = mapped_column(Integer, ForeignKey("EgRailway.trips.id", ondelete="CASCADE"), nullable=False)
    stop_order: Mapped[int]           = mapped_column(SmallInteger, nullable=False)
    station_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("EgRailway.stations.id", ondelete="SET NULL"), nullable=True)
    time_ar:    Mapped[str]           = mapped_column(Text, nullable=False, default="")
    time_en:    Mapped[str]           = mapped_column(Text, nullable=False, default="")

    trip:    Mapped["Trip"] = relationship("Trip", back_populates="stops")
    station: Mapped[Optional["Station"]] = relationship("Station", lazy="joined")

    @property
    def station_ar(self) -> str:
        return self.station.name_ar if self.station else ""

    @property
    def station_en(self) -> str:
        return self.station.name_en if self.station else ""
