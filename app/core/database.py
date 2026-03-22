from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _async_url(url: str) -> str:
    """Convert plain postgresql:// URL to asyncpg-compatible one."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://",   "postgresql+asyncpg://", 1)
    return url


_CONNECT_ARGS: dict = {}
if "pooler.supabase.com" in settings.database_url and ":6543" in settings.database_url:
    _CONNECT_ARGS = {"prepared_statement_cache_size": 0, "statement_cache_size": 0}

engine = create_async_engine(
    _async_url(settings.database_url),
    connect_args=_CONNECT_ARGS,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
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
