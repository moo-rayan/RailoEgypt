from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _async_url(url: str) -> str:
    """Convert plain postgresql:// URL to asyncpg-compatible one."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://",   "postgresql+asyncpg://", 1)
    return url


# ── Supabase Supavisor — session mode (port 5432) ────────────────────────────
#
# Supabase provides two pooler modes on the same hostname:
#   • port 6543 — Transaction mode (pgbouncer): does NOT support prepared
#                 statements. Requires NullPool + statement_cache_size=0.
#   • port 5432 — Session mode (Supavisor): DOES support prepared statements.
#                 Each client connection is pinned to one PostgreSQL backend
#                 for its entire lifetime, so prepared statements stay valid.
#
# We use session mode (port 5432) + SQLAlchemy's built-in connection pool.
# This gives the best performance: warm TCP connections are reused across
# requests, no reconnection overhead per query.
#
# statement_cache_size=0 is kept as defense-in-depth — it ensures asyncpg
# never tries to reuse a cached prepared statement, avoiding any edge case.
# ──────────────────────────────────────────────────────────────────────────────

_CONNECT_ARGS: dict = {
    "statement_cache_size": 0,
    "server_settings": {"jit": "off"},
}

engine = create_async_engine(
    _async_url(settings.database_url),
    connect_args=_CONNECT_ARGS,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=600,
    pool_timeout=30,
    echo=False,
)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
