"""
Admin Alert Service — creates dashboard alerts for reports, contributions, etc.

Uses a direct DB session so it can be called from anywhere (services, managers).
Queries use inline literals (no bind parameters) to avoid prepared-statement
errors with pgbouncer in transaction mode.
"""

import json
import logging

from sqlalchemy import text

from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)


def _q(val: str | None) -> str:
    """Escape a string value for a SQL literal."""
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


async def create_alert(
    alert_type: str,
    title: str,
    body: str,
    metadata: dict | None = None,
    navigate_to: str | None = None,
) -> None:
    """Insert an admin alert row. Fire-and-forget — errors are logged, not raised."""
    try:
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, default=str).replace("'", "''")
        sql = (
            'INSERT INTO "EgRailway".admin_alerts '
            "(alert_type, title, body, metadata, navigate_to) VALUES "
            f"({_q(alert_type)}, {_q(title)}, {_q(body)}, "
            f"'{meta_json}'::jsonb, {_q(navigate_to or '')})"
        )
        async with AsyncSessionFactory() as session:
            await session.execute(text(sql))
            await session.commit()
        logger.info("🔔 Admin alert created: type=%s title=%s", alert_type, title[:50])
    except Exception as exc:
        logger.error("❌ Failed to create admin alert: %s", exc)
