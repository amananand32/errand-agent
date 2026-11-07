"""Injectable clocks so every timing-dependent behaviour (hold expiry, wall-clock
limits, retry backoff) is reproducible in tests."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def monotonic(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(IST)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """Deterministic clock. Simulated tool latency and backoff advance it."""

    DEFAULT_START = datetime(2026, 11, 9, 8, 0, tzinfo=IST)  # Monday

    def __init__(self, start: datetime | None = None):
        self._now = start or self.DEFAULT_START
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float = 0, minutes: float = 0) -> None:
        total = seconds + minutes * 60
        self._now += timedelta(seconds=total)
        self._mono += total
