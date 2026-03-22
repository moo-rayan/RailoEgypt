"""
Support endpoints: Contact Us + Report a Problem.
"""

import logging

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


# ── Contact Us ────────────────────────────────────────────────────────────────

@router.post("/contact", status_code=201)
async def submit_contact(
    body: ContactBody,
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user["id"]
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


# ── Report Problem ────────────────────────────────────────────────────────────

@router.post("/report", status_code=201)
async def submit_report(
    body: ReportBody,
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user["id"]
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
