from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class TripStopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id:         int
    stop_order: int
    station_id: Optional[int]
    station_ar: str
    station_en: str
    time_ar:    str
    time_en:    str


class TripBase(BaseModel):
    train_number:    str
    type_ar:         str
    type_en:         str
    from_station_id: Optional[int] = None
    from_station_ar: str
    from_station_en: str
    to_station_id:   Optional[int] = None
    to_station_ar:   str
    to_station_en:   str
    departure_ar:    str
    departure_en:    str
    arrival_ar:      str
    arrival_en:      str
    duration_ar:     str
    duration_en:     str
    stops_count:     int
    fares:           Optional[dict[str, Any]] = None
    has_fares:       bool


class TripOut(TripBase):
    model_config = ConfigDict(from_attributes=True)

    id:         int
    created_at: datetime
    stops:      list[TripStopOut] = []


class TripListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id:              int
    train_number:    str
    type_ar:         str
    type_en:         str
    from_station_ar: str
    from_station_en: str
    to_station_ar:   str
    to_station_en:   str
    departure_ar:    str
    departure_en:    str
    arrival_ar:      str
    arrival_en:      str
    duration_ar:     str
    duration_en:     str
    stops_count:     int
    fares:           Optional[dict[str, Any]] = None
    has_fares:       bool
    stops:           list[TripStopOut] = []


class TripSearchQuery(BaseModel):
    from_station_ar: Optional[str] = None
    to_station_ar:   Optional[str] = None
    from_station_id: Optional[int] = None
    to_station_id:   Optional[int] = None
    train_number:    Optional[str] = None
    skip:            int = 0
    limit:           int = 20
