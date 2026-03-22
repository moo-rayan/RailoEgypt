"""
Device token model for FCM push notifications.

Stores FCM tokens per user+device, with automatic deduplication.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, func
from app.core.database import Base


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    __table_args__ = (
        Index("idx_device_tokens_user_id", "user_id"),
        {"schema": "EgRailway"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    fcm_token = Column(Text, nullable=False, unique=True)
    device_info = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
