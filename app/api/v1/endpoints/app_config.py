"""
App configuration endpoints.

GET  /app/config           — Public: check maintenance/update status (first request on app open)
PUT  /app/config           — Admin: update app configuration
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.admin_auth import require_fulladmin
from app.models.app_config import AppConfig
from app.services.audit_service import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["App Config"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class AppConfigResponse(BaseModel):
    is_maintenance_mode: bool
    maintenance_message_ar: str
    maintenance_message_en: str
    force_update: bool
    min_version: str
    latest_version: str
    update_message_ar: str
    update_message_en: str
    store_url_android: str
    store_url_ios: str
    station_schedule_check_enabled: bool


class AppConfigUpdateRequest(BaseModel):
    is_maintenance_mode: Optional[bool] = None
    maintenance_message_ar: Optional[str] = None
    maintenance_message_en: Optional[str] = None
    force_update: Optional[bool] = None
    min_version: Optional[str] = None
    latest_version: Optional[str] = None
    update_message_ar: Optional[str] = None
    update_message_en: Optional[str] = None
    store_url_android: Optional[str] = None
    store_url_ios: Optional[str] = None
    station_schedule_check_enabled: Optional[bool] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/config", response_model=AppConfigResponse)
async def get_app_config(db: AsyncSession = Depends(get_db)):
    """
    Public endpoint — called as the FIRST request when the app opens.
    Returns maintenance status, force update flag, and version info.
    No authentication required.
    """
    result = await db.execute(select(AppConfig).where(AppConfig.id == 1))
    config = result.scalar_one_or_none()

    if config is None:
        # Return defaults if row doesn't exist
        return AppConfigResponse(
            is_maintenance_mode=False,
            maintenance_message_ar="التطبيق تحت الصيانة حالياً، يرجى المحاولة لاحقاً.",
            maintenance_message_en="The app is currently under maintenance. Please try again later.",
            force_update=False,
            min_version="1.0.0",
            latest_version="1.0.0",
            update_message_ar="يوجد إصدار جديد من التطبيق، يرجى التحديث للاستمرار.",
            update_message_en="A new version is available. Please update to continue.",
            store_url_android="",
            store_url_ios="",
            station_schedule_check_enabled=True,
        )

    return AppConfigResponse(
        is_maintenance_mode=config.is_maintenance_mode,
        maintenance_message_ar=config.maintenance_message_ar,
        maintenance_message_en=config.maintenance_message_en,
        force_update=config.force_update,
        min_version=config.min_version,
        latest_version=config.latest_version,
        update_message_ar=config.update_message_ar,
        update_message_en=config.update_message_en,
        store_url_android=config.store_url_android,
        store_url_ios=config.store_url_ios,
        station_schedule_check_enabled=config.station_schedule_check_enabled,
    )


@router.put("/config", response_model=AppConfigResponse, dependencies=[Depends(require_fulladmin)])
async def update_app_config(
    body: AppConfigUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint — update app configuration (maintenance mode, force update, etc.)."""
    result = await db.execute(select(AppConfig).where(AppConfig.id == 1))
    config = result.scalar_one_or_none()

    if config is None:
        config = AppConfig(id=1)
        db.add(config)

    # Only update fields that were provided
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    await db.flush()
    await db.refresh(config)

    logger.info("⚙️ App config updated: %s", list(update_data.keys()))
    audit.log_admin_action(
        request,
        action="update_app_config",
        metadata={"updated_fields": list(update_data.keys()), "values": update_data},
    )

    return AppConfigResponse(
        is_maintenance_mode=config.is_maintenance_mode,
        maintenance_message_ar=config.maintenance_message_ar,
        maintenance_message_en=config.maintenance_message_en,
        force_update=config.force_update,
        min_version=config.min_version,
        latest_version=config.latest_version,
        update_message_ar=config.update_message_ar,
        update_message_en=config.update_message_en,
        store_url_android=config.store_url_android,
        store_url_ios=config.store_url_ios,
        station_schedule_check_enabled=config.station_schedule_check_enabled,
    )
