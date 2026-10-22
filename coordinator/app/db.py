"""Подключение к БД."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    url = url or settings.database_url
    kwargs = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        # Пул под нагрузку: обработчики синхронные (пул потоков 40), каждому нужно соединение.
        kwargs.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout)
    return create_engine(url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
