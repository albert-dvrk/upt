"""Фоновая проверка: молчащие серверы, истёкшие аренды, лимит времени задания."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.services import leases

log = logging.getLogger("videofetch.reaper")


def reap_once(session_factory: sessionmaker, settings: Settings) -> dict[str, int]:
    with session_factory() as session:
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
