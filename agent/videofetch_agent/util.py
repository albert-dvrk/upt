"""Мелкие утилиты агента: безопасные имена файлов, повторы с задержкой, лимитер запросов."""

from __future__ import annotations

import os
import random
import re
import threading
import time
import unicodedata
from email.utils import parsedate_to_datetime
import datetime as dt

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
MAX_NAME_BYTES = 120


def sanitize_title(title: str | None, fallback: str = "video") -> str:
    """Превращает произвольное название в безопасное имя файла (без расширения).

    Убираются разделители путей, управляющие и двунаправленные символы, точки в начале
    (запрет «..»), зарезервированные имена Windows; длина ограничена в байтах UTF-8.
    """
    text = unicodedata.normalize("NFKC", title or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn"))
    text = _FORBIDDEN_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    encoded = text.encode("utf-8")[:MAX_NAME_BYTES]
    text = encoded.decode("utf-8", errors="ignore").strip(" ._")
    if text.upper().split(".")[0] in _RESERVED:
        text = f"x{text}"  # префикс ставим после обрезки, иначе он снова удалится
    return text or fallback


def safe_join(root: str, *parts: str) -> str:
    """Склеивает путь и гарантирует, что результат остаётся внутри root."""
    root_abs = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root_abs, *parts))
    if path != root_abs and not path.startswith(root_abs + os.sep):
        raise ValueError("Путь выходит за пределы каталога хранилища")
    return path


def video_filename(video_id: int, title: str | None) -> str:
    """Имя файла: «<id>_<название>.mp4». Идентификатор в начале делает имя уникальным."""
    return f"{video_id}_{sanitize_title(title)}.mp4"


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0, rnd: random.Random | None = None) -> float:
    """Экспоненциальная задержка с «полным» джиттером: случайное число в [0, min(cap, base·2^n)]."""
    rnd = rnd or random
    return rnd.uniform(0, min(cap, base * (2 ** max(0, attempt - 1))))


def parse_retry_after(value: str | None, default: float = 5.0, cap: float = 120.0) -> float:
    """Retry-After: число секунд или HTTP-дата. Некорректное значение → default."""
    if not value:
        return default
    value = value.strip()
    try:
        return max(0.0, min(float(value), cap))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        delta = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return max(0.0, min(delta, cap))
    except (TypeError, ValueError):
        return default


class RateGate:
    """Общий для потоков ограничитель: лимит запросов в секунду + пауза после 429."""

    # Адаптивное снижение нагрузки (AIMD): при росте 429/5xx/сетевых ошибок масштаб нагрузки делится пополам,
    # при спокойной работе растёт на 0,1. Масштаб применяется и к лимиту запросов, и к числу активных потоков.
    WINDOW = 20           # ответов в окне оценки
    BAD_RATIO = 0.2       # доля «плохих» ответов, после которой нагрузка снижается
    MIN_SCALE = 0.1

    def __init__(self, rate: float | None = None, clock=time.monotonic, sleep=time.sleep) -> None:
        self._lock = threading.Lock()
        self._scale = 1.0
        self._win_total = self._win_bad = 0
        self._last_cut = -1e9
        self._rate = rate
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._clock = clock
        self._sleep = sleep

    @property
    def scale(self) -> float:
        return self._scale

    def report(self, code: int | None) -> None:
        """Учитывает ответ: code=None — сетевая ошибка. 429 снижает нагрузку сразу, прочие сбои — по доле в окне."""
        bad = code is None or code == 429 or code >= 500
        with self._lock:
            now = self._clock()
            if code == 429 and now - self._last_cut >= 2.0:
                self._scale = max(self.MIN_SCALE, self._scale / 2)
                self._last_cut = now
                self._win_total = self._win_bad = 0
                return
            self._win_total += 1
            self._win_bad += bad
            if self._win_total >= self.WINDOW:
                if self._win_bad / self._win_total >= self.BAD_RATIO:
                    self._scale = max(self.MIN_SCALE, self._scale / 2)
                    self._last_cut = now
                elif self._win_bad == 0 and self._scale < 1.0:
                    self._scale = min(1.0, self._scale + 0.1)
                self._win_total = self._win_bad = 0

    def set_rate(self, rate: float | None) -> None:
        with self._lock:
            self._rate = rate if rate and rate > 0 else None

    def block_for(self, seconds: float) -> None:
        """Приостанавливает все потоки (ответ 429 с Retry-After)."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def wait(self) -> float:
        """Блокируется до разрешённого момента; возвращает фактическое ожидание."""
        with self._lock:
            now = self._clock()
            start = max(now, self._blocked_until)
            if self._rate:
                start = max(start, self._next_slot)
                self._next_slot = start + 1.0 / (self._rate * self._scale)
            delay = start - now
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)
