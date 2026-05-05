from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class News(Base):
    __tablename__ = "news"
    __table_args__ = {"schema": "EgRailway"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
