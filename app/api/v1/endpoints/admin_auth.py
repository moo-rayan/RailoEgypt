"""
Admin authentication endpoints for the Dashboard.

POST /admin/auth/verify   → Verify JWT and return admin profile (used on dashboard login)
GET  /admin/auth/me       → Get current admin user info
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.admin_auth import AdminUser, get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/auth", tags=["Admin Auth"])


class AdminProfileOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    admin_level: str
    is_fulladmin: bool


@router.post("/verify", response_model=AdminProfileOut)
async def verify_admin(
    admin: AdminUser = Depends(get_admin_user),
):
    """
    Verify that the current JWT belongs to an admin user.
    Called by Dashboard on login to confirm admin access.
    """
    logger.info("Admin verified: %s (%s)", admin.email, admin.admin_level)
    return AdminProfileOut(
        user_id=admin.user_id,
        email=admin.email,
        display_name=admin.display_name,
        admin_level=admin.admin_level,
        is_fulladmin=admin.is_fulladmin,
    )


@router.get("/me", response_model=AdminProfileOut)
async def get_admin_me(
    admin: AdminUser = Depends(get_admin_user),
):
    """Get current admin user profile."""
    return AdminProfileOut(
        user_id=admin.user_id,
        email=admin.email,
        display_name=admin.display_name,
        admin_level=admin.admin_level,
        is_fulladmin=admin.is_fulladmin,
    )
