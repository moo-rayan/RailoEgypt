from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TrainBase(BaseModel):
    train_id: str = Field(..., min_length=1, max_length=20)
    type_ar: str = Field(..., min_length=1)
    type_en: str = Field(..., min_length=1)
    start_station_ar: str = Field(..., min_length=1)
    start_station_en: str = Field(..., min_length=1)
    end_station_ar: str = Field(..., min_length=1)
    end_station_en: str = Field(..., min_length=1)
    stops_count: int = Field(default=0, ge=0)
    departure_ar: str = ""
    departure_en: str = ""
    arrival_ar: str = ""
    arrival_en: str = ""
    note_ar: str = ""
    note_en: str = ""


class TrainCreate(TrainBase):
    pass


class TrainUpdate(BaseModel):
    type_ar: str | None = None
    type_en: str | None = None
    start_station_ar: str | None = None
    start_station_en: str | None = None
    end_station_ar: str | None = None
    end_station_en: str | None = None
    stops_count: int | None = None
    departure_ar: str | None = None
    departure_en: str | None = None
    arrival_ar: str | None = None
    arrival_en: str | None = None
    note_ar: str | None = None
    note_en: str | None = None
    is_active: bool | None = None


class TrainRead(TrainBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TrainListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[TrainRead]


class TrainSearchParams(BaseModel):
    from_station: str | None = None
    to_station: str | None = None
    train_type: str | None = None
    is_active: bool | None = True
