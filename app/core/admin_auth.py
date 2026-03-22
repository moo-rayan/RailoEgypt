"""
Admin authentication & authorization dependencies.

Uses Supabase JWT to verify the user, then checks is_admin + admin_level
in the profiles table. Replaces the old X-Admin-Key approach with
proper user-based authentication.

Admin Levels:
  - fulladmin: Full access to all endpoints and actions
  - monitor:   Read-only access to stations, trains, live tracking
"""

import logging
from typing import Optional

import httpx
from fastapi import Depends, Header, HTTPException, status

from app.core.config import settings
from app.core.security import verify_supabase_token

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


async def _fetch_admin_profile(user_id: str) -> Optional[dict]:
    """
    Query the profiles table via Supabase PostgREST REST API.
    Avoids SQLAlchemy/asyncpg entirely — no pgbouncer prepared-statement issues.
    """
    url = (
        f"{settings.supabase_url}/rest/v1/profiles"
        f"?select=is_admin,admin_level,email,display_name"
        f"&id=eq.{user_id}"
        f"&limit=1"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
        "Accept": "application/json",
        "Accept-Profile": "EgRailway",  # profiles table lives in EgRailway schema
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error("Admin profile fetch failed: HTTP %s", resp.status_code)
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("Admin profile fetch error: %s", exc)
        return None


async def get_admin_user(
    authorization: str = Header(..., description="Bearer <supabase_jwt>"),
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

    # 3. Check admin status via Supabase REST API (avoids pgbouncer prepared-stmt issues)
    profile = await _fetch_admin_profile(user_id)

    if profile is None:
        logger.warning("Admin auth: profile not found for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied — profile not found",
        )

    is_admin = profile.get("is_admin", False)
    admin_level = profile.get("admin_level", "")
    email = profile.get("email", "")
    display_name = profile.get("display_name", "")

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


async def require_fulladmin(
    admin: AdminUser = Depends(get_admin_user),
) -> AdminUser:
    """
    Require fulladmin level. Use this for write/modify operations.
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
) -> AdminUser:
    """
    Transitional dependency: accepts either Supabase JWT or legacy X-Admin-Key.
    SECURITY: If JWT is provided, it must be valid — no fallback to legacy key.
    Legacy key is only accepted when NO JWT header is sent.
    """
    # If JWT is provided, it MUST be valid — fail immediately if not
    if authorization and authorization.startswith("Bearer "):
        return await get_admin_user(authorization=authorization)

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
