from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppAnnouncement(Base):
    __tablename__ = "app_announcements"
    __table_args__ = {"schema": "EgRailway"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    title_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    display_mode: Mapped[str] = mapped_column(Text, nullable=False, default="dialog")
    width_ratio: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.92)
    max_height_ratio: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.82)
    image_fit: Mapped[str] = mapped_column(Text, nullable=False, default="cover")

    show_action_button: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    action_text_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action_text_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action_url: Mapped[str] = mapped_column(Text, nullable=False, default="")

    show_dismiss_button: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dismiss_text_ar: Mapped[str] = mapped_column(Text, nullable=False, default="إخفاء")
    dismiss_text_en: Mapped[str] = mapped_column(Text, nullable=False, default="Dismiss")
    dismissible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    start_at: Mapped[datetime | None] = mapped_column(nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
