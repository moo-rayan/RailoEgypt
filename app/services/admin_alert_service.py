"""
Admin Alert Service — creates dashboard alerts for reports, contributions, etc.

Uses a direct DB session so it can be called from anywhere (services, managers).
"""

import logging
from sqlalchemy import text
from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)


async def create_alert(
    alert_type: str,
    title: str,
    body: str,
    metadata: dict | None = None,
    navigate_to: str | None = None,
) -> None:
    """Insert an admin alert row. Fire-and-forget — errors are logged, not raised."""
    try:
        async with AsyncSessionFactory() as session:
            await session.execute(
                text("""
                    INSERT INTO "EgRailway".admin_alerts
                        (alert_type, title, body, metadata, navigate_to)
                    VALUES
                        (:alert_type, :title, :body, CAST(:metadata AS jsonb), :navigate_to)
                """),
                {
                    "alert_type": alert_type,
                    "title": title,
                    "body": body,
                    "metadata": __import__("json").dumps(metadata or {}),
                    "navigate_to": navigate_to or "",
                },
            )
            await session.commit()
        logger.info("🔔 Admin alert created: type=%s title=%s", alert_type, title[:50])
    except Exception as exc:
        logger.error("❌ Failed to create admin alert: %s", exc)
