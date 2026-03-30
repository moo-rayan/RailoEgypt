from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _async_url(url: str) -> str:
    """Convert plain postgresql:// URL to asyncpg-compatible one."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://",   "postgresql+asyncpg://", 1)
    return url


# ── pgbouncer (transaction mode) compatibility ───────────────────────────────
#
# pgbouncer in "transaction" pool mode does NOT support prepared statements.
# asyncpg (used by SQLAlchemy) normally PREPAREs every query before EXECUTEing
# it.  When SQLAlchemy keeps its own connection pool *on top of* pgbouncer,
# a connection checked-out from the SA pool may now point to a different
# PostgreSQL backend — and the old prepared statement doesn't exist there.
#
# Fix:
#   • NullPool  – no SA-side pooling; pgbouncer is already the pool.
#                 Every session gets a fresh connection to pgbouncer, so
#                 there is never a stale prepared-statement reference.
#   • statement_cache_size=0  – asyncpg won't try to *reuse* prepared
#                 statements across queries on the same connection.
#   • server_settings jit=off – avoid JIT overhead for short OLTP queries.
# ──────────────────────────────────────────────────────────────────────────────

_CONNECT_ARGS: dict = {
    "statement_cache_size": 0,
    "server_settings": {"jit": "off"},
}

engine = create_async_engine(
    _async_url(settings.database_url),
    connect_args=_CONNECT_ARGS,
    poolclass=NullPool,
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
