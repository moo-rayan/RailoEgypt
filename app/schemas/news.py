from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NewsCreate(BaseModel):
    title: str
    body: str = ""
    image_url: str | None = None
    is_published: bool = False


class NewsUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    image_url: str | None = None
    is_published: bool | None = None


class NewsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    image_url: str | None
    is_published: bool
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NewsList(BaseModel):
    items: list[NewsRead]
    total: int
    page: int
    page_size: int
