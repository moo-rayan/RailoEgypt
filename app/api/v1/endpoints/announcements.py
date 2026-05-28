"""
App announcement endpoints.

Public:
  GET /announcements/active — active announcement for the mobile app

Admin:
  GET    /announcements/admin
  POST   /announcements/admin
  PUT    /announcements/admin/{id}
  DELETE /announcements/admin/{id}
  POST   /announcements/admin/upload-image
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_fulladmin
from app.core.config import settings
from app.core.database import get_db
from app.models.app_announcement import AppAnnouncement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/announcements", tags=["Announcements"])


class AnnouncementBase(BaseModel):
    version: int = Field(1, ge=1)
    is_active: bool = False
    priority: int = 0
    title_ar: str = ""
    title_en: str = ""
    body_ar: str = ""
    body_en: str = ""
    image_url: str | None = None
    display_mode: str = "dialog"
    width_ratio: float = Field(0.92, gt=0, le=1)
    max_height_ratio: float = Field(0.82, gt=0, le=1)
    image_fit: str = "cover"
    show_action_button: bool = False
    action_text_ar: str = ""
    action_text_en: str = ""
    action_url: str = ""
    show_dismiss_button: bool = True
    dismiss_text_ar: str = "إخفاء"
    dismiss_text_en: str = "Dismiss"
    dismissible: bool = True
    start_at: datetime | None = None
    end_at: datetime | None = None


class AnnouncementCreate(AnnouncementBase):
    pass


class AnnouncementUpdate(BaseModel):
    version: int | None = Field(None, ge=1)
    is_active: bool | None = None
    priority: int | None = None
    title_ar: str | None = None
    title_en: str | None = None
    body_ar: str | None = None
    body_en: str | None = None
    image_url: str | None = None
    display_mode: str | None = None
    width_ratio: float | None = Field(None, gt=0, le=1)
    max_height_ratio: float | None = Field(None, gt=0, le=1)
    image_fit: str | None = None
    show_action_button: bool | None = None
    action_text_ar: str | None = None
    action_text_en: str | None = None
    action_url: str | None = None
    show_dismiss_button: bool | None = None
    dismiss_text_ar: str | None = None
    dismiss_text_en: str | None = None
    dismissible: bool | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class AnnouncementRead(AnnouncementBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class AnnouncementList(BaseModel):
    items: list[AnnouncementRead]
    total: int


def _validate_options(data: AnnouncementBase | AnnouncementUpdate) -> None:
    mode = getattr(data, "display_mode", None)
    if mode is not None and mode not in {"dialog", "fullscreen"}:
        raise HTTPException(status_code=400, detail="display_mode must be dialog or fullscreen")

    image_fit = getattr(data, "image_fit", None)
    if image_fit is not None and image_fit not in {"cover", "contain"}:
        raise HTTPException(status_code=400, detail="image_fit must be cover or contain")


def _read(item: AppAnnouncement) -> AnnouncementRead:
    return AnnouncementRead(
        id=item.id,
        version=item.version,
        is_active=item.is_active,
        priority=item.priority,
        title_ar=item.title_ar,
        title_en=item.title_en,
        body_ar=item.body_ar,
        body_en=item.body_en,
        image_url=item.image_url,
        display_mode=item.display_mode,
        width_ratio=float(item.width_ratio),
        max_height_ratio=float(item.max_height_ratio),
        image_fit=item.image_fit,
        show_action_button=item.show_action_button,
        action_text_ar=item.action_text_ar,
        action_text_en=item.action_text_en,
        action_url=item.action_url,
        show_dismiss_button=item.show_dismiss_button,
        dismiss_text_ar=item.dismiss_text_ar,
        dismiss_text_en=item.dismiss_text_en,
        dismissible=item.dismissible,
        start_at=item.start_at,
        end_at=item.end_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/active", response_model=AnnouncementRead | None)
async def get_active_announcement(db: AsyncSession = Depends(get_db)):
    """Return the currently active announcement, if any."""
    now = func.now()
    row = await db.execute(
        select(AppAnnouncement)
        .where(
            AppAnnouncement.is_active == True,
            or_(AppAnnouncement.start_at == None, AppAnnouncement.start_at <= now),
            or_(AppAnnouncement.end_at == None, AppAnnouncement.end_at >= now),
        )
        .order_by(desc(AppAnnouncement.priority), desc(AppAnnouncement.updated_at), desc(AppAnnouncement.id))
        .limit(1)
    )
    item = row.scalar_one_or_none()
    return _read(item) if item else None


@router.get("/admin", response_model=AnnouncementList)
async def admin_list_announcements(
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    total_q = await db.execute(select(func.count(AppAnnouncement.id)))
    total = total_q.scalar() or 0
    rows = await db.execute(
        select(AppAnnouncement).order_by(
            desc(AppAnnouncement.is_active),
            desc(AppAnnouncement.priority),
            desc(AppAnnouncement.updated_at),
        )
    )
    return AnnouncementList(items=[_read(item) for item in rows.scalars().all()], total=total)


@router.post("/admin", response_model=AnnouncementRead)
async def create_announcement(
    data: AnnouncementCreate,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    _validate_options(data)
    item = AppAnnouncement(**data.model_dump(), created_by=uuid.UUID(admin.user_id))
    db.add(item)
    await db.flush()
    await db.refresh(item)
    logger.info("Announcement created: id=%s version=%s", item.id, item.version)
    return _read(item)


@router.put("/admin/{announcement_id}", response_model=AnnouncementRead)
async def update_announcement(
    announcement_id: int,
    data: AnnouncementUpdate,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    _validate_options(data)
    row = await db.execute(select(AppAnnouncement).where(AppAnnouncement.id == announcement_id))
    item = row.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Announcement not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(item, field, value)
    item.updated_at = func.now()

    await db.flush()
    await db.refresh(item)
    logger.info("Announcement updated: id=%s fields=%s", item.id, list(update_data.keys()))
    return _read(item)


@router.delete("/admin/{announcement_id}")
async def delete_announcement(
    announcement_id: int,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    row = await db.execute(select(AppAnnouncement).where(AppAnnouncement.id == announcement_id))
    item = row.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    await db.delete(item)
    return {"ok": True}


@router.post("/admin/upload-image")
async def upload_announcement_image(
    file: UploadFile = File(...),
    admin=Depends(require_fulladmin),
):
    """Upload an announcement image to Cloudflare R2 and return its public URL."""
    from app.core.r2_storage import _get_s3_client

    if not settings.r2_access_key_id or not settings.r2_public_url:
        raise HTTPException(status_code=500, detail="Storage not configured")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 5MB")

    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
    key = f"announcements/{uuid.uuid4()}.{ext.lower()}"
    client = _get_s3_client()

    import asyncio

    await asyncio.to_thread(
        client.put_object,
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=content,
        ContentType=file.content_type,
        CacheControl="public, max-age=31536000",
    )
    return {"url": f"{settings.r2_public_url.rstrip('/')}/{key}"}
