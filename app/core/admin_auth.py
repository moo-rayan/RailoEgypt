"""
Admin authentication & authorization dependencies.

Uses Supabase JWT to verify the user, then checks is_admin + admin_level
in the profiles table. Replaces the old X-Admin-Key approach with
proper user-based authentication.

Admin Levels:
  - fulladmin: Full access to all endpoints and actions
  - monitor:   Full access to trains, stations, trips, live tracking.
               No access to: notifications, app config, user bans, admin management.
"""

import logging
import re
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_supabase_token

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


class AdminUser:
    """Authenticated admin user context."""
    __slots__ = ("user_id", "email", "display_name", "admin_level")

    def __init__(self, user_id: str, email: str, display_name: str, admin_level: str):
        self.user_id = user_id
        self.email = email
        self.display_name = display_name
        self.admin_level = admin_level

    @property
    def is_fulladmin(self) -> bool:
        return self.admin_level == "fulladmin"

    @property
    def is_monitor(self) -> bool:
        return self.admin_level == "monitor"


async def get_admin_user(
    authorization: str = Header(..., description="Bearer <supabase_jwt>"),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """
    Verify Supabase JWT and ensure the user is an admin.
    Returns AdminUser with role info.
    Raises 401 for invalid token, 403 for non-admin users.
    """
    # 1. Validate Bearer token format
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )

    token = authorization[7:]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing access token",
        )

    # 2. Verify with Supabase
    user_data = await verify_supabase_token(token)
    if user_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = user_data.get("id", "")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID not found in token",
        )

    # 3. Validate UUID format before embedding in SQL (safety guard)
    if not _UUID_RE.match(str(user_id)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID format",
        )

    # 4. Query profiles using text() without bind parameters.
    #    Supabase pgbouncer (transaction mode) breaks asyncpg prepared statements
    #    even with statement_cache_size=0. A parameterless text() query goes through
    #    the simple-query protocol and is never prepared — no pgbouncer conflict.
    #    user_id is a Supabase-verified UUID (hex + hyphens only), so no injection risk.
    stmt = text(
        'SELECT is_admin, admin_level, email, display_name '
        'FROM "EgRailway".profiles '
        f"WHERE id = '{user_id}'::uuid"
    )
    result = await db.execute(stmt)
    row = result.one_or_none()

    if row is None:
        logger.warning("Admin auth: profile not found for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied — profile not found",
        )

    is_admin, admin_level, email, display_name = row

    if not is_admin or admin_level not in ("fulladmin", "monitor"):
        logger.warning(
            "Admin auth: non-admin user %s attempted dashboard access", user_id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied — admin privileges required",
        )

    return AdminUser(
        user_id=str(user_id),
        email=email or "",
        display_name=display_name or "",
        admin_level=admin_level,
    )


async def require_admin(
    admin: AdminUser = Depends(get_admin_user),
) -> AdminUser:
    """
    Require any admin level (fulladmin or monitor).
    Use this for operations that both roles should access (e.g. trains, stations, trips).
    """
    return admin


async def require_fulladmin(
    admin: AdminUser = Depends(get_admin_user),
) -> AdminUser:
    """
    Require fulladmin level. Use this for sensitive operations
    (e.g. notifications, app config, user bans, admin management).
    Monitor users will get 403.
    """
    if not admin.is_fulladmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires full admin privileges",
        )
    return admin


async def get_admin_or_legacy_key(
    authorization: Optional[str] = Header(None),
    x_admin_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """
    Transitional dependency: accepts either Supabase JWT or legacy X-Admin-Key.
    SECURITY: If JWT is provided, it must be valid — no fallback to legacy key.
    Legacy key is only accepted when NO JWT header is sent.
    """
    # If JWT is provided, it MUST be valid — fail immediately if not
    if authorization and authorization.startswith("Bearer "):
        return await get_admin_user(authorization=authorization, db=db)

    # Legacy key ONLY when no JWT is provided (for WebSocket/external tools)
    if x_admin_key:
        if not settings.admin_api_key or settings.admin_api_key == "change-me-admin-key":
            logger.error("🚨 Legacy admin key is not configured — rejecting request")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin API key not configured",
            )
        if x_admin_key == settings.admin_api_key:
            logger.warning("⚠️ Legacy admin key used — please migrate to JWT auth")
            return AdminUser(
                user_id="legacy-admin",
                email="admin@system",
                display_name="Legacy Admin",
                admin_level="fulladmin",
            )
        else:
            logger.warning("🚨 Invalid legacy admin key attempted")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin API key",
            )

    # No auth provided at all
    logger.warning("🚨 Admin endpoint accessed without any authentication")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required — provide Bearer token or admin key",
    )
