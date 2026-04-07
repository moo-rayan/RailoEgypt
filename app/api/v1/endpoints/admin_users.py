"""
Admin endpoints for user management.

All endpoints require admin authentication (Supabase JWT + is_admin).
Read endpoints: monitor + fulladmin. Write endpoints: fulladmin only.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select, or_, cast, String

from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.database import AsyncSessionFactory
from app.models.profile import Profile
from app.services.audit_service import audit
from app.services.ban_service import ban_contributor, is_banned, list_bans, unban_contributor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


# ── Response schemas ─────────────────────────────────────────────────────────

class UserBanRequest(BaseModel):
    user_id: str
    reason: str = ""
    duration_minutes: int = 0  # 0 = permanent


class UserUnbanRequest(BaseModel):
    user_id: str


class UserToggleAdminRequest(BaseModel):
    user_id: str
    is_admin: bool
    admin_level: Optional[str] = None  # fulladmin | monitor


class UserToggleCaptainRequest(BaseModel):
    user_id: str
    is_captain: bool


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/stats", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_user_stats():
    """Get user registration statistics: total, weekly, monthly, and daily breakdown."""
    from datetime import timedelta, date
    from sqlalchemy import Date

    try:
        created_date = cast(Profile.created_at, Date)

        async with AsyncSessionFactory() as session:
            # Total users
            total_result = await session.execute(
                select(func.count()).select_from(Profile).where(Profile.is_active == True)
            )
            total_users = total_result.scalar() or 0

            # New users this week (last 7 days)
            week_ago = date.today() - timedelta(days=7)
            weekly_result = await session.execute(
                select(func.count()).select_from(Profile)
                .where(Profile.is_active == True)
                .where(created_date >= week_ago)
            )
            weekly_new = weekly_result.scalar() or 0

            # New users this month (last 30 days)
            month_ago = date.today() - timedelta(days=30)
            monthly_result = await session.execute(
                select(func.count()).select_from(Profile)
                .where(Profile.is_active == True)
                .where(created_date >= month_ago)
            )
            monthly_new = monthly_result.scalar() or 0

            # Daily breakdown for the last 30 days
            daily_result = await session.execute(
                select(
                    created_date.label("day"),
                    func.count().label("count"),
                )
                .where(Profile.is_active == True)
                .where(created_date >= month_ago)
                .group_by(created_date)
                .order_by(created_date.asc())
            )
            daily_rows = daily_result.all()

            # Fill missing days with 0
            daily_data = []
            current = month_ago
            daily_map = {str(row.day): row.count for row in daily_rows}
            while current <= date.today():
                day_str = str(current)
                daily_data.append({"date": day_str, "count": daily_map.get(day_str, 0)})
                current += timedelta(days=1)

            # Weekly breakdown for the last 12 weeks
            twelve_weeks_ago = date.today() - timedelta(weeks=12)
            week_trunc = func.date_trunc('week', Profile.created_at)
            weekly_breakdown_result = await session.execute(
                select(
                    week_trunc.label("week"),
                    func.count().label("count"),
                )
                .where(Profile.is_active == True)
                .where(created_date >= twelve_weeks_ago)
                .group_by(week_trunc)
                .order_by(week_trunc.asc())
            )
            weekly_rows = weekly_breakdown_result.all()
            weekly_data = [
                {"week": row.week.strftime("%Y-%m-%d"), "count": row.count}
                for row in weekly_rows
            ]

            return {
                "total_users": total_users,
                "weekly_new": weekly_new,
                "monthly_new": monthly_new,
                "daily": daily_data,
                "weekly": weekly_data,
            }
    except Exception as e:
        logger.exception("Error fetching user stats")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", dependencies=[Depends(get_admin_or_legacy_key)])
async def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", description="Search by name or email"),
    sort_by: str = Query("created_at", description="Sort field"),
    sort_order: str = Query("desc", description="asc or desc"),
    filter_contributors: Optional[bool] = Query(None, description="Filter contributors only"),
    filter_captains: Optional[bool] = Query(None, description="Filter captains only"),
    filter_admins: Optional[bool] = Query(None, description="Filter admins only"),
):
    """List all users with pagination, search, and filters."""
    async with AsyncSessionFactory() as session:
        # Base query
        query = select(Profile).where(Profile.is_active == True)
        count_query = select(func.count()).select_from(Profile).where(Profile.is_active == True)

        # Search filter
        if search.strip():
            search_term = f"%{search.strip()}%"
            search_filter = or_(
                Profile.display_name.ilike(search_term),
                Profile.email.ilike(search_term),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        # Role filters
        if filter_contributors is True:
            query = query.where(Profile.is_contributor == True)
            count_query = count_query.where(Profile.is_contributor == True)
        if filter_captains is True:
            query = query.where(Profile.is_captain == True)
            count_query = count_query.where(Profile.is_captain == True)
        if filter_admins is True:
            query = query.where(Profile.is_admin == True)
            count_query = count_query.where(Profile.is_admin == True)

        # Total count
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Sorting
        sort_column = getattr(Profile, sort_by, Profile.created_at)
        if sort_order == "asc":
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        # Pagination
        offset = (page - 1) * limit
        query = query.offset(offset).limit(limit)

        result = await session.execute(query)
        users = result.scalars().all()

        # Check ban status for each user
        users_data = []
        for u in users:
            ban_info = await is_banned(u.id)
            users_data.append({
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "is_contributor": u.is_contributor,
                "contribution_count": u.contribution_count,
                "reputation_score": u.reputation_score,
                "last_contribution_at": u.last_contribution_at.isoformat() if u.last_contribution_at else None,
                "is_captain": u.is_captain,
                "is_admin": u.is_admin,
                "admin_level": u.admin_level,
                "is_active": u.is_active,
                "is_banned": ban_info is not None,
                "ban_info": ban_info,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "updated_at": u.updated_at.isoformat() if u.updated_at else None,
            })

        return {
            "users": users_data,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if total > 0 else 0,
        }


@router.get("/{user_id}", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_user_detail(user_id: str):
    """Get detailed info for a single user."""
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Profile).where(Profile.id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        ban_info = await is_banned(user.id)

        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "is_contributor": user.is_contributor,
            "contribution_count": user.contribution_count,
            "reputation_score": user.reputation_score,
            "last_contribution_at": user.last_contribution_at.isoformat() if user.last_contribution_at else None,
            "is_captain": user.is_captain,
            "is_admin": user.is_admin,
            "admin_level": user.admin_level,
            "chat_alias": user.chat_alias,
            "chat_anonymous": user.chat_anonymous,
            "is_active": user.is_active,
            "is_banned": ban_info is not None,
            "ban_info": ban_info,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        }


@router.post("/ban", dependencies=[Depends(require_fulladmin)])
async def ban_user_endpoint(body: UserBanRequest, request: Request):
    """Ban a user from contributing."""
    # Kick from active rooms
    from app.services.tracking_manager import tracking_manager
    for room in tracking_manager.all_rooms_info():
        for c in room["contributors"]:
            if c["user_id"] == body.user_id:
                await tracking_manager.kick_contributor(
                    train_id=room["train_id"],
                    user_id=body.user_id,
                    reason=f"banned: {body.reason}",
                )
                break

    success = await ban_contributor(
        user_id=body.user_id,
        reason=body.reason,
        duration_minutes=body.duration_minutes,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store ban",
        )

    duration_text = f"{body.duration_minutes} minutes" if body.duration_minutes > 0 else "permanent"
    audit.log_admin_action(
        request,
        action=f"ban_user ({duration_text})",
        metadata={"target_user": body.user_id, "reason": body.reason, "duration": body.duration_minutes},
    )
    return {"ok": True, "message": f"User banned ({duration_text})"}


@router.post("/unban", dependencies=[Depends(require_fulladmin)])
async def unban_user_endpoint(body: UserUnbanRequest, request: Request):
    """Remove a user's ban."""
    removed = await unban_contributor(body.user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active ban found for this user",
        )
    audit.log_admin_action(request, action="unban_user", metadata={"target_user": body.user_id})
    return {"ok": True, "message": "User unbanned"}


@router.post("/toggle-captain", dependencies=[Depends(require_fulladmin)])
async def toggle_captain_endpoint(body: UserToggleCaptainRequest, request: Request):
    """Toggle captain status for a user."""
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Profile).where(Profile.id == body.user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        from sqlalchemy import update
        await session.execute(
            update(Profile)
            .where(Profile.id == body.user_id)
            .values(is_captain=body.is_captain)
        )
        await session.commit()

    audit.log_admin_action(
        request,
        action="toggle_captain",
        metadata={"target_user": body.user_id, "is_captain": body.is_captain},
    )
    return {"ok": True, "is_captain": body.is_captain}


@router.post("/toggle-admin")
async def toggle_admin_endpoint(
    body: UserToggleAdminRequest,
    request: Request,
    admin: "AdminUser" = Depends(require_fulladmin),
):
    """Toggle admin status and set admin level for a user. Fulladmin only."""
    from app.core.admin_auth import AdminUser

    # Prevent removing your own admin status
    if body.user_id == admin.user_id and not body.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="لا يمكنك إزالة صلاحيات الأدمن من نفسك",
        )

    # Validate admin_level
    if body.is_admin:
        if body.admin_level not in ("fulladmin", "monitor"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="مستوى الأدمن يجب أن يكون fulladmin أو monitor",
            )
    else:
        body.admin_level = None

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Profile).where(Profile.id == body.user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        from sqlalchemy import update
        await session.execute(
            update(Profile)
            .where(Profile.id == body.user_id)
            .values(is_admin=body.is_admin, admin_level=body.admin_level)
        )
        await session.commit()

    audit.log_admin_action(
        request,
        action="toggle_admin",
        metadata={
            "target_user": body.user_id,
            "is_admin": body.is_admin,
            "admin_level": body.admin_level,
        },
    )
    return {"ok": True, "is_admin": body.is_admin, "admin_level": body.admin_level}
