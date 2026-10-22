"""Запуск: uvicorn mock_service.main:app --host 0.0.0.0 --port 8080

Начальное поведение задаётся переменными окружения MOCK_*.
"""

import os

from mock_service.app import Behavior, create_app


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


behavior = Behavior(
    seed=_env_int("MOCK_SEED", 1),
    found_permille=_env_int("MOCK_FOUND_PERMILLE", 200),
    forbidden_permille=_env_int("MOCK_FORBIDDEN_PERMILLE", 30),
    file_min_kb=_env_int("MOCK_FILE_MIN_KB", 64),
    file_max_kb=_env_int("MOCK_FILE_MAX_KB", 2048),
)

app = create_app(behavior)
