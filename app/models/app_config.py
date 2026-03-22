"""
App configuration model.

Singleton table (id=1) that controls maintenance mode, force update, etc.
"""

from datetime import datetime

from sqlalchemy import Boolean, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppConfig(Base):
    __tablename__ = "app_config"
    __table_args__ = {"schema": "EgRailway"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_maintenance_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    maintenance_message_ar: Mapped[str] = mapped_column(Text, nullable=False, default="التطبيق تحت الصيانة حالياً، يرجى المحاولة لاحقاً.")
    maintenance_message_en: Mapped[str] = mapped_column(Text, nullable=False, default="The app is currently under maintenance. Please try again later.")
    force_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    latest_version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    update_message_ar: Mapped[str] = mapped_column(Text, nullable=False, default="يوجد إصدار جديد من التطبيق، يرجى التحديث للاستمرار.")
    update_message_en: Mapped[str] = mapped_column(Text, nullable=False, default="A new version is available. Please update to continue.")
    store_url_android: Mapped[str] = mapped_column(Text, nullable=False, default="")
    store_url_ios: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now(), onupdate=func.now())
