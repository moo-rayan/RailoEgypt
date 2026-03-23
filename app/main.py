import asyncio
import json
import logging
from contextlib import asynccontextmanager

# ── Increase websockets header-line limit ────────────────────────────────────
# Browsers send all cookies for the domain during WebSocket upgrade.
# Supabase auth stores large JWT tokens in cookies which can exceed the
# default 8 KB per-line limit, causing a 400 Bad Request on handshake.
try:
    from websockets.legacy import http as _ws_http  # used by uvicorn
    _ws_http.MAX_LINE = 65536  # 64 KB (was 8192)
except (ImportError, AttributeError):
    pass
try:
    from websockets import http11 as _ws_http11
    _ws_http11.MAX_LINE = 65536
except (ImportError, AttributeError):
    pass
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from slowapi.util import get_remote_address

from app.api.v1.router import api_router
from app.core.bundle_store import bundle_store
from app.core.cache import get_redis
from app.core.config import settings
from app.core.database import AsyncSessionFactory
from app.core.logging import setup_logging
from app.core.r2_storage import r2_download_bundle, r2_download_version, r2_upload_bundle
from app.models.railway_graph import RailwayGraphData
from app.services.railway_service import railway_graph

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
logger  = logging.getLogger(__name__)

_GRAPH_CACHE_KEY = "railway:graph"   # permanent key – no TTL


async def _load_graph_from_redis(max_retries: int = 3) -> bool:
    """Load railway graph from Redis with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            r = await get_redis()
            raw = await r.get(_GRAPH_CACHE_KEY)
            if raw:
                railway_graph.restore_from_dict(json.loads(raw))
                logger.info(
                    "Railway graph loaded from Redis: %d nodes", railway_graph.node_count
                )
                return True
            return False
        except Exception as exc:
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                logger.warning(
                    "Redis read attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt, max_retries, exc, wait_time
                )
                await asyncio.sleep(wait_time)
            else:
                logger.warning("Could not load railway graph from Redis after %d attempts: %s", max_retries, exc)
                return False
    return False


async def _persist_graph_to_redis(max_retries: int = 3) -> None:
    """Persist railway graph to Redis with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            r = await get_redis()
            await r.set(
                _GRAPH_CACHE_KEY,
                json.dumps(railway_graph.to_dict(), ensure_ascii=False),
            )
            logger.info("Railway graph persisted to Redis (~permanent)")
            return
        except Exception as exc:
            if attempt < max_retries:
                wait_time = 2 ** attempt
                logger.warning(
                    "Redis write attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt, max_retries, exc, wait_time
                )
                await asyncio.sleep(wait_time)
            else:
                logger.warning("Could not persist railway graph to Redis after %d attempts: %s", max_retries, exc)


async def _load_graph_from_db() -> bool:
    """Load railway graph from PostgreSQL database."""
    try:
        async with AsyncSessionFactory() as session:
            from sqlalchemy import select
            
            result = await session.execute(
                select(RailwayGraphData)
                .order_by(RailwayGraphData.created_at.desc())
                .limit(1)
            )
            graph_record = result.scalar_one_or_none()
            
            if graph_record is None:
                logger.info("No railway graph found in database")
                return False
            
            # Restore graph from database
            railway_graph.restore_from_dict(graph_record.data)
            logger.info(
                "Railway graph loaded from database: %d nodes (version %s)",
                graph_record.node_count,
                graph_record.version
            )
            return True
    except Exception as exc:
        logger.error("Failed to load railway graph from database: %s", exc)
        return False


async def _persist_graph_to_db() -> None:
    """Persist railway graph to PostgreSQL database."""
    try:
        graph_data = railway_graph.to_dict()
        
        async with AsyncSessionFactory() as session:
            # Check if a record already exists
            from sqlalchemy import select, delete
            
            # Delete existing records (keep only the latest)
            await session.execute(delete(RailwayGraphData))
            
            # Insert new record
            new_record = RailwayGraphData(
                version="1.0",
                data=graph_data,
                node_count=railway_graph.node_count,
            )
            session.add(new_record)
            await session.commit()
            
            logger.info(
                "Railway graph persisted to database: %d nodes",
                railway_graph.node_count
            )
    except Exception as exc:
        logger.error("Failed to persist railway graph to database: %s", exc)


async def _load_bundle_from_r2() -> bool:
    """Try to load bundle from R2 into memory."""
    try:
        version_bytes = await r2_download_version()
        if version_bytes is None:
            return False

        gzip_bytes = await r2_download_bundle()
        if gzip_bytes is None:
            return False

        # Validate gzip magic bytes (0x1f 0x8b)
        if len(gzip_bytes) < 2 or gzip_bytes[:2] != b'\x1f\x8b':
            logger.warning("R2 bundle is not gzip-compressed, will rebuild")
            return False

        version_info = json.loads(version_bytes)
        bundle_store.set(gzip_bytes, version_info)
        return True
    except Exception as exc:
        logger.warning("Failed to load bundle from R2: %s", exc)
        return False


async def _build_and_store_bundle() -> None:
    """Build data bundle, store in memory and upload to R2."""
    try:
        from app.api.v1.endpoints.data_bundle import _build_raw_bundle, _compute_version
        from app.core.encryption import encrypt_bundle
        import gzip

        async with AsyncSessionFactory() as session:
            logger.info("Building data bundle at startup...")
            raw = await _build_raw_bundle(session)
            version = _compute_version(raw)

            # Version info
            version_info = {
                "version": version,
                "stations_count": len(raw["stations"]),
                "trips_count": len(raw["trips"]),
                "trains_count": len(raw["trains"]),
                "trip_paths_count": len(raw["trip_paths"]),
            }

            # Encrypt → JSON → gzip
            encrypted = encrypt_bundle(raw)
            bundle_result = {"version": version, **encrypted}
            bundle_json = json.dumps(bundle_result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            gzip_bytes = gzip.compress(bundle_json, compresslevel=6)

            logger.info(
                "Bundle built: version=%s, stations=%d, trips=%d, trip_paths=%d, "
                "raw=%.1fKB, gzip=%.1fKB (%.0f%% saved)",
                version[:8], len(raw["stations"]), len(raw["trips"]),
                len(raw["trip_paths"]),
                len(bundle_json) / 1024, len(gzip_bytes) / 1024,
                (1 - len(gzip_bytes) / len(bundle_json)) * 100,
            )

            # 1. Store in process memory (instant serving)
            bundle_store.set(gzip_bytes, version_info)

            # 2. Upload to R2 (persistence across restarts)
            version_bytes = json.dumps(version_info, ensure_ascii=False).encode('utf-8')
            await r2_upload_bundle(gzip_bytes, version_bytes)

    except Exception as exc:
        logger.error("Failed to build data bundle: %s", exc)


async def _stale_contributor_scheduler():
    """Background task: remove stale contributors (no update for 120s) every 60s."""
    from app.services.tracking_manager import tracking_manager
    while True:
        try:
            await asyncio.sleep(60)
            removed = await tracking_manager.cleanup_stale_contributors()
            if removed:
                logger.info("🧹 Stale contributor cleanup: removed %d", removed)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Stale contributor scheduler error: %s", exc)


async def _account_deletion_scheduler():
    """Background task: process expired account deletion requests every 6 hours."""
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from app.models.account_deletion_request import AccountDeletionRequest
    from app.models.device_token import DeviceToken
    from app.models.profile import Profile
    import httpx

    while True:
        try:
            await asyncio.sleep(6 * 3600)  # Run every 6 hours

            now = datetime.now(timezone.utc)
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    select(AccountDeletionRequest)
                    .where(
                        AccountDeletionRequest.status == "pending",
                        AccountDeletionRequest.scheduled_deletion_at <= now,
                    )
                )
                expired = result.scalars().all()

                if not expired:
                    continue

                logger.info("Processing %d expired account deletion requests...", len(expired))

                for req in expired:
                    try:
                        # Delete user from Supabase Auth
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.delete(
                                f"{settings.supabase_url}/auth/v1/admin/users/{req.user_id}",
                                headers={
                                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                                    "apikey": settings.supabase_service_role_key,
                                },
                            )
                            if resp.status_code not in (200, 204, 404):
                                logger.error(
                                    "Failed to delete Supabase user %s: %d",
                                    req.user_id, resp.status_code,
                                )
                                continue

                        # Delete device tokens
                        await session.execute(
                            DeviceToken.__table__.delete().where(
                                DeviceToken.user_id == req.user_id
                            )
                        )

                        # Deactivate profile
                        await session.execute(
                            update(Profile)
                            .where(Profile.id == req.user_id)
                            .values(
                                is_active=False,
                                email=None,
                                display_name="Deleted User",
                                avatar_url=None,
                            )
                        )

                        req.status = "completed"
                        req.completed_at = now
                        logger.info("Account deleted: user=%s", req.user_id)

                    except Exception as exc:
                        logger.error("Error deleting user %s: %s", req.user_id, exc)

                await session.commit()

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Account deletion scheduler error: %s", exc)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    loop = asyncio.get_event_loop()

    # ── 1. Try loading from Redis (fastest) ──────────────────────────────────
    graph_loaded = await _load_graph_from_redis(max_retries=3)

    # ── 2. Redis miss → try loading from Database ────────────────────────────
    if not graph_loaded:
        logger.info("Redis cache miss, trying database...")
        graph_loaded = await _load_graph_from_db()
        
        # If loaded from DB, restore to Redis cache
        if graph_loaded:
            await _persist_graph_to_redis(max_retries=3)

    # ── 3. DB miss → build from GeoJSON then persist everywhere ──────────────
    if not graph_loaded:
        logger.info("Database miss, building from GeoJSON...")
        try:
            node_count = await loop.run_in_executor(None, railway_graph.build)
            logger.info("Railway graph built from GeoJSON: %d nodes", node_count)
        except Exception as exc:
            logger.error("Railway graph build FAILED: %s", exc)

        if railway_graph.is_built:
            await _persist_graph_to_redis(max_retries=3)
            await _persist_graph_to_db()

    # ── 4. Load bundle from R2 or build fresh ──────────────────────────────
    if railway_graph.is_built:
        r2_loaded = await _load_bundle_from_r2()
        if not r2_loaded:
            logger.info("R2 miss, building bundle from scratch...")
            await _build_and_store_bundle()

    # ── 5. Start background tasks ────────────────────────────────────────────
    deletion_task = asyncio.create_task(_account_deletion_scheduler())
    stale_task = asyncio.create_task(_stale_contributor_scheduler())

    yield

    # Cleanup: cancel background tasks
    for task in (deletion_task, stale_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class _WebSocketSafeRateLimiter:
    """SlowAPI rate limiter that bypasses WebSocket upgrade requests.

    SlowAPIMiddleware (BaseHTTPMiddleware) can reject WebSocket upgrades
    with 400 Bad Request. This wrapper routes WebSocket and lifespan
    connections directly to the app, applying rate limiting only to HTTP.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._http_app = SlowAPIMiddleware(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("websocket", "lifespan"):
            await self._app(scope, receive, send)
        else:
            await self._http_app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(
        title="TrainLiveEG API",
        description="API للقطارات المصرية - تتبع مباشر ومعلومات الرحلات",
        version="1.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # Rate Limiting (custom wrapper skips WebSocket to prevent 400 errors)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(_WebSocketSafeRateLimiter)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # GZip compression
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Routes
    app.include_router(api_router)

    return app


app = create_app()
