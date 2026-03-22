from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StationBase(BaseModel):
    name_ar: str = Field(..., min_length=1)
    name_en: str = Field(..., min_length=1)
    latitude: float | None = None
    longitude: float | None = None
    place_id: str | None = None


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name_ar: str | None = None
    name_en: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    place_id: str | None = None
    is_active: bool | None = None


class StationRead(StationBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class StationListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StationRead]
