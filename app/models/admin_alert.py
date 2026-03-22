"""
Admin alert model.

Stores real-time alerts for the dashboard (chat reports, new contributions, etc.).
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from app.core.database import Base


class AdminAlert(Base):
    __tablename__ = "admin_alerts"
    __table_args__ = (
        Index("idx_admin_alerts_created_at", "created_at"),
        {"schema": "EgRailway"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String, nullable=False)       # 'report' | 'contribution'
    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    metadata_ = Column("metadata", JSONB, default={})  # extra context
    navigate_to = Column(Text, nullable=True)           # dashboard route
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
