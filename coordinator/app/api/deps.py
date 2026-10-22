from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.services.deploy import SshDeployer


def get_session(request: Request) -> Iterator[Session]:
    """Сессия на запрос. commit вызывает сам обработчик: ответ уходит только после фиксации."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_deployer(request: Request) -> SshDeployer:
    return request.app.state.deployer


DeployerDep = Annotated[SshDeployer, Depends(get_deployer)]
