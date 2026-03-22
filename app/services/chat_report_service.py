"""
Chat Report & Ban Service – handles report submission and ban checking.

Uses direct database access via SQLAlchemy (asyncpg).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)


async def check_user_banned(user_id: str) -> dict:
    """
    Check if a user is currently banned from chat.
    Returns {"banned": bool, "reason": str|None, "expires_at": str|None}
    """
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                text("""
                    SELECT reason, ban_type, expires_at
                    FROM "EgRailway".chat_bans
                    WHERE user_id = :uid
                      AND is_active = true
                      AND (ban_type = 'permanent' OR expires_at > now())
                    LIMIT 1
                """),
                {"uid": user_id},
            )
            row = result.mappings().first()

            if row:
                expires = row["expires_at"]
                return {
                    "banned": True,
                    "reason": row["reason"] or "",
                    "expires_at": expires.isoformat() if expires else None,
                    "ban_type": row["ban_type"] or "temporary",
                }

            return {"banned": False, "reason": None, "expires_at": None}

    except Exception as exc:
        logger.error("Ban check error: %s", exc)
        return {"banned": False, "reason": None, "expires_at": None}


async def submit_report(
    reporter_id: str,
    reported_user_id: str,
    train_id: str,
    message_id: str,
    message_text: str,
    report_reason: str = "",
) -> dict:
    """
    Submit a chat report.
    Returns {"ok": bool, "error": str|None}
    """
    if reporter_id == reported_user_id:
        return {"ok": False, "error": "cannot_report_self"}

    try:
        async with AsyncSessionFactory() as session:
            await session.execute(
                text("""
                    INSERT INTO "EgRailway".chat_reports
                        (reporter_id, reported_user_id, train_id,
                         message_id, message_text, report_reason, status)
                    VALUES
                        (:reporter_id, :reported_user_id, :train_id,
                         :message_id, :message_text, :report_reason, 'pending')
                """),
                {
                    "reporter_id": reporter_id,
                    "reported_user_id": reported_user_id,
                    "train_id": train_id,
                    "message_id": message_id,
                    "message_text": message_text[:500],
                    "report_reason": (report_reason or "")[:300],
                },
            )
            await session.commit()

        logger.info(
            "📋 Report submitted: reporter=%s reported=%s train=%s msg=%s",
            reporter_id[:8], reported_user_id[:8], train_id, message_id[:8],
        )

        # Create admin dashboard alert
        from app.services.admin_alert_service import create_alert
        await create_alert(
            alert_type="report",
            title=f"بلاغ جديد على رسالة في قطار {train_id}",
            body=f"الرسالة: {message_text[:100]}{'…' if len(message_text) > 100 else ''}"
                 + (f"\nالسبب: {report_reason}" if report_reason else ""),
            metadata={
                "train_id": train_id,
                "reporter_id": reporter_id,
                "reported_user_id": reported_user_id,
                "message_id": message_id,
            },
            navigate_to=f"/admin/contributors?train={train_id}",
        )

        return {"ok": True}

    except Exception as exc:
        err_str = str(exc)
        if "unique" in err_str.lower() or "duplicate" in err_str.lower() or "23505" in err_str:
            return {"ok": False, "error": "already_reported"}
        logger.error("Report submit error: %s", exc)
        return {"ok": False, "error": "server_error"}
