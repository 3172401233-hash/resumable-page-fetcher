"""Time abstraction so no test depends on real wall-clock time."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Minimal clock interface used by retry logic."""

    def now(self) -> float:
        """Return the current monotonic-ish time in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block (or simulate blocking) for ``seconds``."""
        ...


class SystemClock:
    """Clock backed by :mod:`time`, for production use."""

    import time as _time

    def now(self) -> float:
        return self._time.monotonic()

    def sleep(self, seconds: float) -> None:
        self._time.sleep(seconds)


class FakeClock:
    """Deterministic in-memory clock for tests.

    ``sleep`` only advances the virtual clock and records every wait in
    ``sleeps``; no real time passes.
    """

    def __init__(self, start: float = 1000.0):
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        self.sleeps.append(seconds)
        self._now += seconds
