import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AccountDeletionRequest(Base):
    """
    Tracks account deletion requests.
    After 30 days from requested_at, the account is permanently deleted.
    Users can cancel before the grace period expires.
    """
    __tablename__ = "account_deletion_requests"
    __table_args__ = (
        Index("idx_deletion_user_id", "user_id"),
        Index("idx_deletion_status", "status"),
        {"schema": "EgRailway"},
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(
        String(36), nullable=False,
        comment="Supabase auth.users.id",
    )
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Optional reason for deletion",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        comment="pending | cancelled | completed",
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    scheduled_deletion_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="Date when account will be permanently deleted (requested_at + 30 days)",
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
