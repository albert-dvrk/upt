"""Точка входа координатора. Пока — только служебные эндпоинты; API заданий — неделя 2."""

from fastapi import FastAPI

from app import API_VERSION, __version__

app = FastAPI(title="videofetch coordinator", version=__version__)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/version")
def version() -> dict:
    return {"version": __version__, "api": API_VERSION}
