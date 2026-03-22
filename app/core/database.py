import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _async_url(url: str) -> str:
    """Convert plain postgresql:// URL to asyncpg-compatible one."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://",   "postgresql+asyncpg://", 1)
    return url


def _unique_ps_name() -> str:
    """
    Generate a globally unique name for every prepared statement.

    Root cause of DuplicatePreparedStatementError:
    asyncpg 0.29+ uses a per-connection sequential counter to name prepared
    statements even when statement_cache_size=0.  When two SQLAlchemy pool
    connections both reach counter N and pgbouncer (transaction mode) routes
    them to the same backend PostgreSQL connection, the second PREPARE fails
    with "already exists".

    Using a UUID per statement makes collisions statistically impossible.
    """
    return f"__asyncpg_{uuid.uuid4().hex}__"


_CONNECT_ARGS: dict = {
    "statement_cache_size": 0,           # Don't cache – execute-and-discard
    "prepared_statement_name_func": _unique_ps_name,  # Unique names → no conflicts
    "server_settings": {"jit": "off"},
}

engine = create_async_engine(
    _async_url(settings.database_url),
    connect_args=_CONNECT_ARGS,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=False,   # pgbouncer transaction mode + pre_ping can misbehave
    pool_recycle=300,
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
