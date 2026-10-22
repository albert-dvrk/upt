from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SessionDep, ViewerDep
from app.services import alerts as alerts_svc

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/alerts")
def alerts(session: SessionDep, _: ViewerDep):
    return alerts_svc.compute_alerts(session)


@router.get("/downloads")
def downloads(session: SessionDep, _: ViewerDep):
    return alerts_svc.active_downloads(session)
