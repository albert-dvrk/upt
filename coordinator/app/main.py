"""Точка входа координатора."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import API_VERSION, __version__
from app.api import auth as auth_api
from app.api import jobs as jobs_api
from app.api import users as users_api
from app.api import workers as workers_api
from app.background import reaper_loop
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.services import auth as auth_svc
from app.services.deploy import DeployConfig, SshDeployer
from app.services.errors import ServiceError


log = logging.getLogger("videofetch.coordinator")

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-eval' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _read_ca(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text()
    except OSError:
        log.warning("не удалось прочитать сертификат %s: агенты получат только HTTP-доверие системы", path)
        return None


def create_app(
    session_factory: sessionmaker | None = None,
    *,
    background: bool = True,
    settings: Settings | None = None,
    deployer: SshDeployer | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    factory = session_factory or make_session_factory(make_engine(settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auth_enabled:
            with factory() as session:  # первый администратор из VF_ADMIN_PASSWORD, если пользователей ещё нет
                try:
                    if auth_svc.bootstrap_admin(session, settings):
                        session.commit()
                        log.info("создан администратор %s", settings.admin_username)
                except IntegrityError:  # несколько процессов стартуют одновременно: администратора создал соседний
                    session.rollback()
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
    application.state.settings = settings
    if settings.auth_enabled and not settings.secret_key:
        log.warning("VF_SECRET_KEY не задан: ключ сессий случайный, после перезапуска все войдут заново")
    application.state.secret_key = settings.secret_key or secrets.token_hex(32)
    application.state.login_throttle = auth_svc.LoginThrottle(settings.login_max_failures, settings.login_lock_seconds)
    application.state.agent_ca_pem = _read_ca(settings.agent_ca_file)
    application.state.deployer = deployer or SshDeployer(
        DeployConfig(
            source_dir=settings.agent_source_dir,
            coordinator_url=settings.agent_coordinator_url,
            key_dir=settings.ssh_key_dir,
            remote_dir=settings.agent_remote_dir,
            python=settings.agent_python,
            known_hosts=settings.ssh_known_hosts,
            connect_timeout=settings.ssh_connect_timeout,
            url_template=settings.default_url_template,
        )
    )
    application.include_router(auth_api.router)
    application.include_router(users_api.router)
    application.include_router(jobs_api.router)
    application.include_router(workers_api.router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        application.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")

        @application.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        # время внутри приложения (включая ожидание пула потоков): сравнивается с задержкой на клиенте
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.0f}"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path not in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            # страницы документации FastAPI подгружают Swagger/ReDoc с CDN и используют встроенные скрипты
            response.headers.setdefault("Content-Security-Policy", CSP)
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

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
