# database.py — Async PostgreSQL via SQLAlchemy + asyncpg connection pool
# Pool parameters are read from config so they can be tuned via environment variables.
# Automatically converts Render's postgres:// → postgresql+asyncpg://

import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

logger = logging.getLogger(__name__)


def _to_asyncpg_url(url: str) -> str:
    """
    Render.com DATABASE_URL uses postgres:// or postgresql://
    SQLAlchemy asyncpg driver requires postgresql+asyncpg://
    """
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


_db_url = _to_asyncpg_url(settings.DATABASE_URL)

engine = create_async_engine(
    _db_url,
    echo=settings.DEBUG,

    # ── Pool sizing ───────────────────────────────────────────
    # pool_size persistent connections are kept open at all times.
    # Up to max_overflow extra connections are created during bursts;
    # they are closed when returned to the pool (not recycled).
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,

    # ── Pool health & lifecycle ───────────────────────────────
    # pre_ping issues a lightweight "SELECT 1" before handing out a
    # connection, dropping and replacing any that were closed by the
    # server or a network device during idle time.
    pool_pre_ping=True,

    # Recycle connections older than pool_recycle seconds so the OS /
    # firewall does not silently drop long-lived TCP connections.
    pool_recycle=settings.DB_POOL_RECYCLE,

    # Raise PoolTimeout instead of blocking forever when every
    # connection is in use and max_overflow is exhausted.
    pool_timeout=settings.DB_POOL_TIMEOUT,

    # ── asyncpg driver options ────────────────────────────────
    connect_args={
        # Kill runaway queries after 30 s at the PostgreSQL level so a
        # slow report never blocks the whole pool.
        "server_settings": {
            "statement_timeout":       "30000",   # ms
            "idle_in_transaction_session_timeout": "60000",  # ms
            "application_name":        settings.APP_NAME,
        },
    },
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a per-request database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """
    Gracefully close all pooled connections.
    Call from the FastAPI lifespan shutdown hook so connections are not
    abandoned when the process exits (important on Render / Kubernetes).
    """
    await engine.dispose()
    logger.info("Database connection pool disposed.")
