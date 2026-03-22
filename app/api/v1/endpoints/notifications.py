"""
Push notification endpoints.

POST /notifications/register-token   — Register/update FCM token (authenticated users)
DELETE /notifications/unregister-token — Remove FCM token
POST /notifications/send              — Send notification to all users (admin only)
GET  /notifications/history           — Get sent notifications history (admin only)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_supabase_token
from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.models.admin_alert import AdminAlert
from app.models.device_token import DeviceToken
from app.models.notification_history import NotificationHistory
from app.services import fcm_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# ── Auth helpers ─────────────────────────────────────────────────────────────

async def _get_user_id(authorization: str = Header(...)) -> str:
    """Extract and verify user ID from Supabase JWT."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    uid = user.get("id", "")
    if not uid:
        raise HTTPException(status_code=401, detail="User ID not found")
    return uid


# ── Schemas ──────────────────────────────────────────────────────────────────

class RegisterTokenRequest(BaseModel):
    fcm_token: str
    device_info: str | None = None


class UnregisterTokenRequest(BaseModel):
    fcm_token: str


class SendNotificationRequest(BaseModel):
    title: str
    body: str
    data: dict | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/register-token")
async def register_token(
    body: RegisterTokenRequest,
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Register or update an FCM token for the authenticated user."""
    user_id = await _get_user_id(authorization)

    # Upsert on user_id: one token per user.
    # If user reinstalls → token changes → we UPDATE the existing row.
    stmt = pg_insert(DeviceToken).values(
        user_id=user_id,
        fcm_token=body.fcm_token,
        device_info=body.device_info,
    ).on_conflict_do_update(
        constraint="device_tokens_user_id_key",
        set_={
            "fcm_token": body.fcm_token,
            "device_info": body.device_info,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)

    logger.info("📱 Token registered for user %s…", user_id[:8])
    return {"status": "ok"}


@router.delete("/unregister-token")
async def unregister_token(
    body: UnregisterTokenRequest,
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Remove an FCM token (e.g., on logout)."""
    user_id = await _get_user_id(authorization)
    await db.execute(
        delete(DeviceToken).where(
            DeviceToken.fcm_token == body.fcm_token,
            DeviceToken.user_id == user_id,
        )
    )
    logger.info("🗑️ Token unregistered for user %s…", user_id[:8])
    return {"status": "ok"}


@router.post("/send", dependencies=[Depends(require_fulladmin)])
async def send_notification(
    body: SendNotificationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a push notification to all registered devices (admin only)."""
    # Fetch all distinct tokens
    result = await db.execute(select(DeviceToken.fcm_token))
    tokens = [row[0] for row in result.all()]

    if not tokens:
        return {"status": "no_tokens", "sent": 0, "failed": 0}

    # Send in batches of 500 (FCM limit)
    total_success = 0
    total_failure = 0
    all_invalid: list[str] = []

    for i in range(0, len(tokens), 500):
        batch = tokens[i : i + 500]
        result = await fcm_service.send_to_tokens(
            tokens=batch,
            title=body.title,
            body=body.body,
            data=body.data,
        )
        total_success += result["success"]
        total_failure += result["failure"]
        all_invalid.extend(result["invalid_tokens"])

    # Clean up invalid tokens
    if all_invalid:
        await db.execute(
            delete(DeviceToken).where(DeviceToken.fcm_token.in_(all_invalid))
        )
        logger.info("🧹 Removed %d invalid tokens", len(all_invalid))

    logger.info(
        "📤 Notification sent: %d success, %d failed, %d invalid removed",
        total_success, total_failure, len(all_invalid),
    )

    # Save to notification history
    history = NotificationHistory(
        title=body.title,
        body=body.body,
        sent_by="admin",
        total_tokens=len(tokens),
        success_count=total_success,
        failure_count=total_failure,
        invalid_removed=len(all_invalid),
    )
    db.add(history)

    return {
        "status": "sent",
        "total_tokens": len(tokens),
        "success": total_success,
        "failure": total_failure,
        "invalid_removed": len(all_invalid),
    }


@router.get("/history", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_notification_history(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Get sent notifications history (admin only)."""
    count_result = await db.execute(select(func.count(NotificationHistory.id)))
    total = count_result.scalar() or 0

    result = await db.execute(
        select(NotificationHistory)
        .order_by(NotificationHistory.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "body": r.body,
                "sent_by": r.sent_by,
                "total_tokens": r.total_tokens,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "invalid_removed": r.invalid_removed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/token-count", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_token_count(db: AsyncSession = Depends(get_db)):
    """Get total registered device token count (admin only)."""
    result = await db.execute(select(func.count(DeviceToken.id)))
    count = result.scalar() or 0
    
    result_users = await db.execute(
        select(func.count(func.distinct(DeviceToken.user_id)))
    )
    user_count = result_users.scalar() or 0

    return {"total_tokens": count, "unique_users": user_count}


# ── Admin Alerts (dashboard real-time notifications) ────────────────────────

@router.get("/alerts", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_admin_alerts(
    limit: int = 30,
    offset: int = 0,
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Get admin alerts for the dashboard (reports, contributions, etc.)."""
    query = select(AdminAlert)
    if unread_only:
        query = query.where(AdminAlert.is_read == False)
    query = query.order_by(AdminAlert.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    rows = result.scalars().all()

    # Unread count
    count_result = await db.execute(
        select(func.count(AdminAlert.id)).where(AdminAlert.is_read == False)
    )
    unread = count_result.scalar() or 0

    return {
        "unread_count": unread,
        "items": [
            {
                "id": r.id,
                "alert_type": r.alert_type,
                "title": r.title,
                "body": r.body,
                "metadata": r.metadata_,
                "navigate_to": r.navigate_to,
                "is_read": r.is_read,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/alerts/{alert_id}/read", dependencies=[Depends(get_admin_or_legacy_key)])
async def mark_alert_read(
    alert_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Mark an admin alert as read."""
    from sqlalchemy import update
    await db.execute(
        update(AdminAlert).where(AdminAlert.id == alert_id).values(is_read=True)
    )
    return {"ok": True}


@router.post("/alerts/read-all", dependencies=[Depends(get_admin_or_legacy_key)])
async def mark_all_alerts_read(db: AsyncSession = Depends(get_db)):
    """Mark all admin alerts as read."""
    from sqlalchemy import update
    await db.execute(
        update(AdminAlert).where(AdminAlert.is_read == False).values(is_read=True)
    )
    return {"ok": True}
