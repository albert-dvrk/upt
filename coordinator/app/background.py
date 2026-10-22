"""Фоновая проверка: молчащие серверы, истёкшие аренды, лимит времени задания."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.services import leases

log = logging.getLogger("videofetch.reaper")


REAPER_LOCK_ID = 727_001  # ключ рекомендательной блокировки PostgreSQL


def reap_once(session_factory: sessionmaker, settings: Settings) -> dict[str, int]:
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            # Несколько процессов координатора (uvicorn --workers): проверку выполняет один за раз,
            # остальные пропускают цикл. Блокировка снимается вместе с транзакцией.
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": REAPER_LOCK_ID}):
                return {}
        stats = leases.reap_expired(session, settings=settings)
        session.commit()
    return stats


async def reaper_loop(session_factory: sessionmaker, stop: asyncio.Event, settings: Settings) -> None:
    while not stop.is_set():
        try:
            stats = await asyncio.to_thread(reap_once, session_factory, settings)
            if any(stats.values()):
                log.info("reaper: %s", stats)
        except Exception:  # БД может быть временно недоступна — пробуем в следующем цикле
            log.exception("reaper: ошибка цикла")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.reaper_interval_seconds)
        except asyncio.TimeoutError:
            pass
