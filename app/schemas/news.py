from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator, model_validator


_image_url_adapter = TypeAdapter(HttpUrl)


def _validate_image_url(value: str) -> str:
    value = value.strip()
    _image_url_adapter.validate_python(value)
    return value


class NewsImagesInput(BaseModel):
    image_url: str | None = None
    image_urls: list[str] | None = Field(None, max_length=5)

    @field_validator("image_url")
    @classmethod
    def valid_cover(cls, value):
        return _validate_image_url(value) if value is not None else None

    @field_validator("image_urls")
    @classmethod
    def valid_gallery(cls, values):
        if values is None:
            return values
        urls = [_validate_image_url(value) for value in values]
        if len(set(urls)) != len(urls):
            raise ValueError("News images must be unique")
        return urls

    @model_validator(mode="after")
    def cover_in_gallery(self):
        if (
            {"image_url", "image_urls"} <= self.model_fields_set
            and self.image_url is not None
            and self.image_url not in (self.image_urls or [])
        ):
            raise ValueError("The cover must be one of the news images")
        return self


class NewsCreate(NewsImagesInput):
    title: str
    body: str = ""
    is_published: bool = False

    @model_validator(mode="after")
    def initialize_gallery(self):
        if self.image_urls is None:
            self.image_urls = [self.image_url] if self.image_url else []
        if self.image_url is None and self.image_urls:
            self.image_url = self.image_urls[0]
        return self


class NewsUpdate(NewsImagesInput):
    title: str | None = None
    body: str | None = None
    is_published: bool | None = None


class NewsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    image_url: str | None
    image_urls: list[str] = Field(default_factory=list)
    is_published: bool
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    view_count: int = 0

    @field_validator("image_urls", mode="before")
    @classmethod
    def empty_gallery(cls, value):
        return value or []

    @model_validator(mode="after")
    def legacy_gallery(self):
        if not self.image_urls and self.image_url:
            self.image_urls = [self.image_url]
        if self.image_urls and self.image_url is None:
            self.image_url = self.image_urls[0]
        return self


class NewsList(BaseModel):
    items: list[NewsRead]
    total: int
    page: int
    page_size: int
