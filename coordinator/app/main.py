"""Точка входа координатора."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import API_VERSION, __version__
from app.api import jobs as jobs_api
from app.api import workers as workers_api
from app.background import reaper_loop
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.services.errors import ServiceError


def create_app(
    session_factory: sessionmaker | None = None,
    *,
    background: bool = True,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    factory = session_factory or make_session_factory(make_engine(settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()
        task = asyncio.create_task(reaper_loop(factory, stop, settings)) if background else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task

    application = FastAPI(title="videofetch coordinator", version=__version__, lifespan=lifespan)
    application.state.session_factory = factory
    application.include_router(jobs_api.router)
    application.include_router(workers_api.router)

    @application.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": exc.message})

    @application.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        return JSONResponse(content={"status": "ready"})

    @application.get("/api/version")
    def version() -> dict:
        return {"version": __version__, "api": API_VERSION}

    return application


app = create_app()
