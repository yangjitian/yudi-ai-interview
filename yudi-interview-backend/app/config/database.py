from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config.settings import get_settings


settings = get_settings()

_async_engine = create_async_engine(
    settings.database.url,
    echo=settings.app.app_debug,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

_async_session_factory = async_sessionmaker(
    bind=_async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
  pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
  async with _async_session_factory() as session:
    try:
      yield session
      await session.commit()
    except Exception:
      await session.rollback()
      raise
    finally:
      await session.close()


async def init_db() -> None:
  async with _async_engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
  await _async_engine.dispose()
