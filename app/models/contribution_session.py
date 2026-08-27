from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ContributionSession(Base):
    """Server-side ledger row for one trusted tracking contribution."""

    __tablename__ = "contribution_sessions"
    __table_args__ = (
        Index("idx_contribution_sessions_user_created", "user_id", "created_at"),
        Index("idx_contribution_sessions_train_created", "train_number", "created_at"),
        {"schema": "EgRailway"},
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("EgRailway.profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    train_number: Mapped[str] = mapped_column(String(20), nullable=False)
    trip_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("EgRailway.trips.id", ondelete="SET NULL"),
        nullable=True,
    )
    from_station_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    to_station_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    contribution_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    end_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    is_silent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    session_runs_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_session_ids: Mapped[list[str]] = mapped_column(
        ARRAY(UUID(as_uuid=False)),
        nullable=False,
        default=list,
    )
    session_progress: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    accepted_updates_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_updates_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_distance_m: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    trusted_distance_m: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    credited_distance_m: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    max_route_progress_m: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    points_rate_per_km: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=1)
    points_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unseen_distance_m: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    unseen_points_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_lat: Mapped[float | None] = mapped_column(nullable=True)
    first_lng: Mapped[float | None] = mapped_column(nullable=True)
    last_lat: Mapped[float | None] = mapped_column(nullable=True)
    last_lng: Mapped[float | None] = mapped_column(nullable=True)
    max_reported_speed_kmh: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    max_rail_distance_m: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=500)
    max_train_distance_m: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=5000)
    last_session_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    last_reward_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reward_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
