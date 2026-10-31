"""Zegar silnika: jedyne źródło czasu i oczekiwania (testy podstawiają FakeClock).

Wszystkie daty i godziny w projekcie są świadome strefy (UTC); zegar nigdy nie zwraca naiwnego datetime.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Prawdziwy zegar: czas ścienny w UTC i prawdziwe oczekiwanie."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Zegar testowy: `sleep` tylko przesuwa czas wewnętrzny i wraca natychmiast."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start: expected a timezone-aware datetime (UTC), got a naive one")
        self._start = start.astimezone(timezone.utc)
        self._now = self._start

    def now_utc(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        # jak asyncio.sleep: czas niedodatni nie cofa ani nie przesuwa zegara
        if seconds > 0:
            self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"seconds: time cannot go backwards, got {seconds}")
        self._now += timedelta(seconds=seconds)

    def elapsed_since_start(self) -> float:
        return (self._now - self._start).total_seconds()
