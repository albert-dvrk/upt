"""Разбор диапазона идентификаторов и разбиение на блоки (разделы 3 и 6.1 задания).

Границы всегда включительные. Верхняя граница None означает «без ограничения».
Блоки строятся детерминированно: следующий блок начинается ровно после конца
предыдущего, поэтому идентификаторы не пропускаются и не обрабатываются дважды.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

# Максимум, который помещается в BIGINT PostgreSQL.
MAX_ID = 2**63 - 1

_SINGLE = re.compile(r"[0-9]+")
_BRACKETS = re.compile(r"\[\s*([0-9]+)\s*,\s*([0-9]*)\s*\]")


class RangeError(ValueError):
    """Некорректное описание диапазона."""


@dataclass(frozen=True)
class IdRange:
    start: int = 0
    end: int | None = None  # включительно; None — открытый диапазон

    def __post_init__(self) -> None:
        if self.start < 0:
            raise RangeError(f"Нижняя граница должна быть неотрицательной: {self.start}")
        if self.start > MAX_ID:
            raise RangeError(f"Нижняя граница слишком велика: {self.start}")
        if self.end is not None:
            if self.end < 0:
                raise RangeError(f"Верхняя граница должна быть неотрицательной: {self.end}")
            if self.end > MAX_ID:
                raise RangeError(f"Верхняя граница слишком велика: {self.end}")
            if self.start > self.end:
                raise RangeError(f"Должно выполняться A <= B, получено {self.start} > {self.end}")

    @property
    def is_infinite(self) -> bool:
        return self.end is None

    def size(self) -> int | None:
        """Количество идентификаторов; None для открытого диапазона."""
        return None if self.end is None else self.end - self.start + 1

    def contains(self, value: int) -> bool:
        return value >= self.start and (self.end is None or value <= self.end)

    def __str__(self) -> str:
        return f"[{self.start},{'' if self.end is None else self.end}]"


def parse_range(text: str | None) -> IdRange:
    """Разбирает диапазон: ``[A,B]``, ``A``, ``[A,]`` или пустое значение."""
    if text is None or not text.strip():
        return IdRange(0, None)
    s = text.strip()
    if _SINGLE.fullmatch(s):
        return IdRange(0, int(s))
    m = _BRACKETS.fullmatch(s)
    if m:
        a, b = m.groups()
        return IdRange(int(a), int(b) if b else None)
    raise RangeError(
        f"Неизвестный формат диапазона: {text!r}. Допустимо: [A,B], A, [A,] или пустое значение"
    )


def from_to(start: int | None, end: int | None) -> IdRange:
    """Эквивалент параметров командной строки ``--from A --to B``."""
    return IdRange(0 if start is None else start, end)


def next_chunk(rng: IdRange, chunk_size: int, last_end: int | None) -> tuple[int, int] | None:
    """Границы следующего блока после ``last_end`` (None — блоков ещё не было).

    Возвращает None, когда конечный диапазон исчерпан.
    """
    if chunk_size < 1:
        raise RangeError(f"Размер блока должен быть >= 1: {chunk_size}")
    if last_end is not None and last_end < rng.start - 1:
        raise RangeError(f"last_end={last_end} лежит левее диапазона {rng}")
    first = rng.start if last_end is None else last_end + 1
    if rng.end is not None and first > rng.end:
        return None
    if first > MAX_ID:
        return None
    last = min(first + chunk_size - 1, MAX_ID)
    if rng.end is not None:
        last = min(last, rng.end)
    return first, last


def split_range(rng: IdRange, chunk_size: int, last_end: int | None = None) -> Iterator[tuple[int, int]]:
    """Лениво генерирует блоки; для открытого диапазона поток бесконечен."""
    while (chunk := next_chunk(rng, chunk_size, last_end)) is not None:
        yield chunk
        last_end = chunk[1]


def chunk_count(rng: IdRange, chunk_size: int) -> int | None:
    """Число блоков для конечного диапазона; None для открытого."""
    if chunk_size < 1:
        raise RangeError(f"Размер блока должен быть >= 1: {chunk_size}")
    size = rng.size()
    return None if size is None else -(-size // chunk_size)
