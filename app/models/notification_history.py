"""
Notification history model.

Stores a record of every push notification sent from the admin dashboard.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, func
from app.core.database import Base


class NotificationHistory(Base):
    __tablename__ = "notification_history"
    __table_args__ = (
        Index("idx_notification_history_created_at", "created_at"),
        {"schema": "EgRailway"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    sent_by = Column(String, nullable=False, default="admin")
    total_tokens = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    invalid_removed = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
