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
    dedup_hours: float = 0,
) -> None:
    """Insert an admin alert row. Fire-and-forget — errors are logged, not raised.

    If dedup_hours > 0 and metadata contains 'user_id' + 'train_id',
    skip the insert when a matching alert already exists within that window.
    """
    try:
        meta = metadata or {}
        meta_json = json.dumps(meta, ensure_ascii=False, default=str).replace("'", "''")

        async with AsyncSessionFactory() as session:
            # DB-level deduplication for contribution alerts
            if dedup_hours > 0 and meta.get("user_id") and meta.get("train_id"):
                check_sql = (
                    'SELECT 1 FROM "EgRailway".admin_alerts '
                    f"WHERE alert_type = {_q(alert_type)} "
                    f"AND metadata->>'user_id' = {_q(meta['user_id'])} "
                    f"AND metadata->>'train_id' = {_q(meta['train_id'])} "
                    f"AND created_at > NOW() - INTERVAL '{int(dedup_hours)} hours' "
                    "LIMIT 1"
                )
                dup = await session.execute(text(check_sql))
                if dup.scalar_one_or_none() is not None:
                    logger.debug(
                        "⏭️ Duplicate alert skipped: type=%s user=%s train=%s",
                        alert_type, meta['user_id'][:8], meta['train_id'],
                    )
                    return

            sql = (
                'INSERT INTO "EgRailway".admin_alerts '
                "(alert_type, title, body, metadata, navigate_to) VALUES "
                f"({_q(alert_type)}, {_q(title)}, {_q(body)}, "
                f"'{meta_json}'::jsonb, {_q(navigate_to or '')})"
            )
            await session.execute(text(sql))
            await session.commit()
        logger.info("🔔 Admin alert created: type=%s title=%s", alert_type, title[:50])
    except Exception as exc:
        logger.error("❌ Failed to create admin alert: %s", exc)
