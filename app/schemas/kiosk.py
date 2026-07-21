from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KioskStationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name_ar: str
    name_en: str


class KioskBase(BaseModel):
    station_id: int = Field(..., ge=1)
    merchant_name: str = Field(default="", max_length=500)
    seller_phone: str = Field(default="", max_length=80)
    menu: list[Any] | dict[str, Any] = Field(default_factory=list)
    working_hours: list[Any] | dict[str, Any] = Field(default_factory=dict)
    platform_location: str = Field(default="", max_length=500)
    is_open: bool = True
    is_active: bool = True
    is_phone_visible: bool = False


class KioskCreate(KioskBase):
    pass


class KioskUpdate(BaseModel):
    station_id: int | None = Field(default=None, ge=1)
    merchant_name: str | None = Field(default=None, max_length=500)
    seller_phone: str | None = Field(default=None, max_length=80)
    menu: list[Any] | dict[str, Any] | None = None
    working_hours: list[Any] | dict[str, Any] | None = None
    platform_location: str | None = Field(default=None, max_length=500)
    is_open: bool | None = None
    is_active: bool | None = None
    is_phone_visible: bool | None = None


class KioskRead(KioskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    station: KioskStationRead | None = None
    created_at: datetime
    updated_at: datetime


class KioskListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[KioskRead]
