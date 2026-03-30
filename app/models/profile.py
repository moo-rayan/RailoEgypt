import uuid
from datetime import datetime

from sqlalchemy import Boolean, Double, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Profile(Base):
    """
    Public user profile linked to Supabase auth.users.
    Auto-created via DB trigger on first sign-up.
    Used for tracking contributions, reputation, etc.
    """
    __tablename__ = "profiles"
    __table_args__ = (
        Index("idx_profiles_email", "email"),
        {"schema": "EgRailway"},
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
        comment="Matches auth.users.id (UUID)",
    )
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Tracking contribution fields
    is_contributor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Has the user opted-in to share live location for train tracking",
    )
    contribution_count: Mapped[int] = mapped_column(
        nullable=False, default=0,
        comment="Number of tracking sessions contributed",
    )
    reputation_score: Mapped[float] = mapped_column(
        Double, nullable=False, default=0.0,
        comment="Trust score based on contribution accuracy",
    )
    last_contribution_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="Timestamp of last tracking contribution",
    )

    is_captain: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Train captain — gets priority in contributor selection and special UI badge",
    )

    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Whether this user has admin access to the dashboard",
    )
    admin_level: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None,
        comment="Admin role: fulladmin (full access) | monitor (read-only limited access)",
    )

    chat_alias: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None,
        comment="Anonymous alias for train chat, e.g. مسافر 1234",
    )
    chat_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Whether user prefers anonymous mode in train chat",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
