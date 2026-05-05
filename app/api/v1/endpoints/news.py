"""
News endpoints.

Public:
  GET /news          — paginated published news (for Flutter app, requires auth)

Admin:
  GET    /news/admin       — all news (published + drafts) for dashboard
  POST   /news/admin       — create news article
  PUT    /news/admin/{id}  — update news article
  DELETE /news/admin/{id}  — delete news article
  POST   /news/admin/upload-image — upload image to Supabase Storage
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_authenticated_user
from app.core.admin_auth import get_admin_or_legacy_key, require_fulladmin
from app.core.config import settings
from app.models.news import News
from app.schemas.news import NewsCreate, NewsUpdate, NewsRead, NewsList

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/news", tags=["News"])


# ── Public endpoints (Flutter app) ─────────────────────────────────────────


@router.get(
    "",
    response_model=NewsList,
    dependencies=[Depends(require_authenticated_user)],
)
async def list_published_news(
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    """Paginated published news for the mobile app."""
    page = max(1, page)
    page_size = min(max(1, page_size), 50)
    offset = (page - 1) * page_size

    total_q = await db.execute(
        select(func.count(News.id)).where(News.is_published == True)
    )
    total = total_q.scalar() or 0

    rows = await db.execute(
        select(News)
        .where(News.is_published == True)
        .order_by(desc(News.published_at))
        .offset(offset)
        .limit(page_size)
    )
    items = [NewsRead.model_validate(r) for r in rows.scalars().all()]

    return NewsList(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/latest-id",
    dependencies=[Depends(require_authenticated_user)],
)
async def get_latest_news_id(db: AsyncSession = Depends(get_db)):
    """Return the ID of the latest published news (for unread dot check)."""
    row = await db.execute(
        select(News.id, News.published_at)
        .where(News.is_published == True)
        .order_by(desc(News.published_at))
        .limit(1)
    )
    result = row.first()
    if result is None:
        return {"latest_id": None, "published_at": None}
    return {"latest_id": result.id, "published_at": result.published_at}


# ── Admin endpoints (Dashboard) ────────────────────────────────────────────


@router.get("/admin", response_model=NewsList)
async def admin_list_news(
    page: int = 1,
    page_size: int = 20,
    admin=Depends(get_admin_or_legacy_key),
    db: AsyncSession = Depends(get_db),
):
    """All news (including drafts) for admin dashboard."""
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    offset = (page - 1) * page_size

    total_q = await db.execute(select(func.count(News.id)))
    total = total_q.scalar() or 0

    rows = await db.execute(
        select(News)
        .order_by(desc(News.created_at))
        .offset(offset)
        .limit(page_size)
    )
    items = [NewsRead.model_validate(r) for r in rows.scalars().all()]

    return NewsList(items=items, total=total, page=page, page_size=page_size)


@router.post("/admin", response_model=NewsRead)
async def create_news(
    data: NewsCreate,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    """Create a news article."""
    article = News(
        title=data.title,
        body=data.body,
        image_url=data.image_url,
        is_published=data.is_published,
        published_at=datetime.now(timezone.utc) if data.is_published else None,
        created_by=uuid.UUID(admin.user_id),
    )
    db.add(article)
    await db.flush()
    await db.refresh(article)
    logger.info("News created: id=%s title=%s", article.id, article.title[:50])
    return NewsRead.model_validate(article)


@router.put("/admin/{news_id}", response_model=NewsRead)
async def update_news(
    news_id: int,
    data: NewsUpdate,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    """Update a news article."""
    row = await db.execute(select(News).where(News.id == news_id))
    article = row.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="News article not found")

    if data.title is not None:
        article.title = data.title
    if data.body is not None:
        article.body = data.body
    if data.image_url is not None:
        article.image_url = data.image_url
    if data.is_published is not None:
        was_published = article.is_published
        article.is_published = data.is_published
        # Set published_at on first publish
        if data.is_published and not was_published:
            article.published_at = datetime.now(timezone.utc)
        elif not data.is_published:
            article.published_at = None

    article.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(article)
    logger.info("News updated: id=%s", article.id)
    return NewsRead.model_validate(article)


@router.delete("/admin/{news_id}")
async def delete_news(
    news_id: int,
    admin=Depends(require_fulladmin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a news article."""
    row = await db.execute(select(News).where(News.id == news_id))
    article = row.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="News article not found")

    await db.delete(article)
    logger.info("News deleted: id=%s", news_id)
    return {"ok": True}


@router.post("/admin/upload-image")
async def upload_news_image(
    file: UploadFile = File(...),
    admin=Depends(require_fulladmin),
):
    """Upload an image to Supabase Storage and return its public URL."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 5MB")

    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = f"news/{filename}"

    # Upload via Supabase Storage REST API
    import httpx

    storage_url = f"{settings.supabase_url}/storage/v1/object/news-images/{path}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            storage_url,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": file.content_type or "image/jpeg",
            },
            content=content,
        )
        if resp.status_code not in (200, 201):
            logger.error("Storage upload failed: %s %s", resp.status_code, resp.text)
            raise HTTPException(status_code=500, detail="Image upload failed")

    public_url = f"{settings.supabase_url}/storage/v1/object/public/news-images/{path}"
    return {"url": public_url}
