from datetime import datetime
import uuid

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NewsView(Base):
    __tablename__ = "news_views"
    __table_args__ = (
        UniqueConstraint("news_id", "user_id", name="uq_news_views_news_user"),
        Index("idx_news_views_news_id", "news_id"),
        Index("idx_news_views_user_id", "user_id"),
        {"schema": "EgRailway"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(
        ForeignKey("EgRailway.news.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("EgRailway.profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    viewed_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
