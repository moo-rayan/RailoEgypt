"""
Support endpoints: Contact Us + Report a Problem.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import verify_supabase_token
from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.services.admin_alert_service import create_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["Support"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ContactBody(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=10, max_length=2000)


class ReportBody(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    category_label: str = Field(..., min_length=1, max_length=100)
    details: str = Field("", max_length=2000)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user(authorization: str = Header(...)) -> dict:
    """Extract and verify user from Bearer token."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    user = await verify_supabase_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


async def _check_contact_rate_limit(user_id: str, db: AsyncSession) -> None:
    """Check if user has sent a contact message in the last hour."""
    logger.info(f"Checking rate limit for user: {user_id[:8]}")
    
    result = await db.execute(
        text("""
            SELECT created_at
            FROM "EgRailway".contact_messages
            WHERE user_id = CAST(:user_id AS UUID)
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"user_id": user_id}
    )
    row = result.mappings().first()
    
    if row:
        last_message_time = row["created_at"]
        logger.info(f"Last message time from DB: {last_message_time}, tzinfo: {last_message_time.tzinfo}")
        
        # Convert both times to UTC for comparison
        if last_message_time.tzinfo is not None:
            last_message_time = last_message_time.astimezone(timezone.utc)
        
        now = datetime.now(timezone.utc)
        time_since_last = now - last_message_time
        
        logger.info(f"Now (UTC): {now}, Last (UTC): {last_message_time}, Diff: {time_since_last}, Minutes: {time_since_last.total_seconds() / 60}")
        
        if time_since_last < timedelta(hours=1):
            minutes_remaining = int(60 - time_since_last.total_seconds() / 60)
            logger.warning(f"Rate limit hit! User {user_id[:8]} must wait {minutes_remaining} minutes")
            raise HTTPException(
                status_code=429,
                detail=f"يمكنك إرسال رسالة واحدة فقط كل ساعة. يرجى الانتظار {minutes_remaining} دقيقة أخرى."
            )
    else:
        logger.info(f"No previous message found for user: {user_id[:8]}")


# ── Contact Us ────────────────────────────────────────────────────────────────

@router.post("/contact", status_code=201)
async def submit_contact(
    body: ContactBody,
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user["id"]
    
    # Check rate limit - one message per hour
    await _check_contact_rate_limit(user_id, db)
    
    email = user.get("email", "")
    meta = user.get("user_metadata", {})
    display_name = meta.get("full_name") or meta.get("name") or email

    await db.execute(
        text("""
            INSERT INTO "EgRailway".contact_messages
                (user_id, display_name, email, subject, message)
            VALUES
                (CAST(:user_id AS UUID), :display_name, :email, :subject, :message)
        """),
        {
            "user_id": user_id,
            "display_name": display_name,
            "email": email,
            "subject": body.subject,
            "message": body.message,
        },
    )
    await db.commit()

    # Admin alert (fire-and-forget)
    try:
        await create_alert(
            alert_type="contact",
            title="رسالة تواصل جديدة",
            body=f"{display_name}: {body.subject}",
            metadata={"user_id": user_id, "subject": body.subject},
            navigate_to="/admin/support",
        )
    except Exception as exc:
        logger.error("Failed to create contact alert: %s", exc)

    logger.info("📩 Contact message from %s: %s", user_id[:8], body.subject)
    return {"ok": True}


async def _check_report_rate_limit(user_id: str, db: AsyncSession) -> None:
    """Check if user has sent a report in the last hour."""
    logger.info(f"Checking report rate limit for user: {user_id[:8]}")
    
    result = await db.execute(
        text("""
            SELECT created_at
            FROM "EgRailway".problem_reports
            WHERE user_id = CAST(:user_id AS UUID)
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"user_id": user_id}
    )
    row = result.mappings().first()
    
    if row:
        last_report_time = row["created_at"]
        
        # Convert both times to UTC for comparison
        if last_report_time.tzinfo is not None:
            last_report_time = last_report_time.astimezone(timezone.utc)
        
        now = datetime.now(timezone.utc)
        time_since_last = now - last_report_time
        
        logger.info(f"Now (UTC): {now}, Last (UTC): {last_report_time}, Diff: {time_since_last}, Minutes: {time_since_last.total_seconds() / 60}")
        
        if time_since_last < timedelta(hours=1):
            minutes_remaining = int(60 - time_since_last.total_seconds() / 60)
            logger.warning(f"Report rate limit hit! User {user_id[:8]} must wait {minutes_remaining} minutes")
            raise HTTPException(
                status_code=429,
                detail=f"يمكنك إرسال بلاغ واحد فقط كل ساعة. يرجى الانتظار {minutes_remaining} دقيقة أخرى."
            )
    else:
        logger.info(f"No previous report found for user: {user_id[:8]}")

@router.post("/report", status_code=201)
async def submit_report(
    body: ReportBody,
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user["id"]
    
    # Check rate limit - one report per hour
    await _check_report_rate_limit(user_id, db)
    
    email = user.get("email", "")
    meta = user.get("user_metadata", {})
    display_name = meta.get("full_name") or meta.get("name") or email

    await db.execute(
        text("""
            INSERT INTO "EgRailway".problem_reports
                (user_id, display_name, email, category, category_label, details)
            VALUES
                (CAST(:user_id AS UUID), :display_name, :email, :category, :category_label, :details)
        """),
        {
            "user_id": user_id,
            "display_name": display_name,
            "email": email,
            "category": body.category,
            "category_label": body.category_label,
            "details": body.details,
        },
    )
    await db.commit()

    # Admin alert
    try:
        await create_alert(
            alert_type="problem_report",
            title="بلاغ مشكلة جديد",
            body=f"{display_name}: {body.category_label}",
            metadata={
                "user_id": user_id,
                "category": body.category,
                "category_label": body.category_label,
            },
            navigate_to="/admin/support",
        )
    except Exception as exc:
        logger.error("Failed to create report alert: %s", exc)

    logger.info("🐛 Problem report from %s: %s", user_id[:8], body.category)
    return {"ok": True}


# ── Admin: List messages / reports ────────────────────────────────────────────

@router.get("/admin/contacts")
async def list_contacts(
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _=Depends(get_admin_or_legacy_key),
    db: AsyncSession = Depends(get_db),
):
    where = "" if status == "all" else "WHERE status = :status"
    rows = (await db.execute(
        text(f"""
            SELECT id, user_id, display_name, email, subject, message,
                   status, admin_notes, created_at, updated_at
            FROM "EgRailway".contact_messages
            {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"status": status, "limit": limit, "offset": offset},
    )).mappings().all()

    count_row = (await db.execute(
        text(f'SELECT COUNT(*) AS cnt FROM "EgRailway".contact_messages {where}'),
        {"status": status},
    )).mappings().first()

    return {
        "items": [dict(r) for r in rows],
        "total": count_row["cnt"] if count_row else 0,
    }


@router.get("/admin/reports")
async def list_reports(
    status: str = Query("all"),
    category: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _=Depends(get_admin_or_legacy_key),
    db: AsyncSession = Depends(get_db),
):
    conditions = []
    if status != "all":
        conditions.append("status = :status")
    if category != "all":
        conditions.append("category = :category")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = (await db.execute(
        text(f"""
            SELECT id, user_id, display_name, email, category, category_label,
                   details, status, admin_notes, created_at, updated_at
            FROM "EgRailway".problem_reports
            {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"status": status, "category": category, "limit": limit, "offset": offset},
    )).mappings().all()

    count_row = (await db.execute(
        text(f'SELECT COUNT(*) AS cnt FROM "EgRailway".problem_reports {where}'),
        {"status": status, "category": category},
    )).mappings().first()

    return {
        "items": [dict(r) for r in rows],
        "total": count_row["cnt"] if count_row else 0,
    }


@router.patch("/admin/contacts/{item_id}")
async def update_contact(
    item_id: int,
    status: str = Query(None),
    admin_notes: str = Query(None),
    _=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    sets = []
    params: dict = {"id": item_id}
    if status:
        sets.append("status = :status")
        params["status"] = status
    if admin_notes is not None:
        sets.append("admin_notes = :admin_notes")
        params["admin_notes"] = admin_notes
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = now()")
    await db.execute(
        text(f'UPDATE "EgRailway".contact_messages SET {", ".join(sets)} WHERE id = :id'),
        params,
    )
    await db.commit()
    return {"ok": True}


@router.patch("/admin/reports/{item_id}")
async def update_report(
    item_id: int,
    status: str = Query(None),
    admin_notes: str = Query(None),
    _=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    sets = []
    params: dict = {"id": item_id}
    if status:
        sets.append("status = :status")
        params["status"] = status
    if admin_notes is not None:
        sets.append("admin_notes = :admin_notes")
        params["admin_notes"] = admin_notes
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = now()")
    await db.execute(
        text(f'UPDATE "EgRailway".problem_reports SET {", ".join(sets)} WHERE id = :id'),
        params,
    )
    await db.commit()
    return {"ok": True}
