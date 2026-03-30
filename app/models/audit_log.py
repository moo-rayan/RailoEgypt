"""
Security audit log model.

Stores rate-limit violations, auth failures, bot detection,
suspicious requests, and other security-relevant events.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_created_at", "created_at"),
        Index("idx_audit_log_event_type", "event_type"),
        Index("idx_audit_log_ip_address", "ip_address", postgresql_where="ip_address IS NOT NULL"),
        {"schema": "EgRailway"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Event classification
    event_type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False, server_default="warning")

    # Request context
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    method = Column(String(10), nullable=True)
    path = Column(Text, nullable=True)
    status_code = Column(SmallInteger, nullable=True)

    # User context
    user_id = Column(UUID(as_uuid=False), nullable=True)

    # Event details
    description = Column(Text, nullable=False)
    metadata_ = Column("metadata", JSONB, nullable=False, server_default="{}")

    # Geo hint
    country_code = Column(String(5), nullable=True)

    # Auto-cleanup
    expires_at = Column(DateTime(timezone=True), nullable=True)
