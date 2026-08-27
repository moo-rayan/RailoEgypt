from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RewardRedemptionRequest(Base):
    """Admin-reviewed request to redeem contribution reward points."""

    __tablename__ = "reward_redemption_requests"
    __table_args__ = (
        Index("idx_reward_redemption_user_created", "user_id", "created_at"),
        Index("idx_reward_redemption_status_created", "status", "created_at"),
        {"schema": "EgRailway"},
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("EgRailway.profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    reward_key: Mapped[str] = mapped_column(String(80), nullable=False)
    reward_title_ar: Mapped[str] = mapped_column(Text, nullable=False)
    reward_title_en: Mapped[str] = mapped_column(Text, nullable=False)
    points_required: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    user_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    admin_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("EgRailway.profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
