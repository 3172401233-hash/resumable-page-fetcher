"""In-memory fakes used by the test-suite; no real I/O or real time."""

from __future__ import annotations

import json
from typing import Any

from resumable_page_fetcher import (
    FakeClock,
    PageRequest,
    PageResponse,
    RateLimitError,
    ServerError,
    StaleResponseError,
)


def _token(cursor: Any) -> str:
    return json.dumps(cursor, sort_keys=True, separators=(",", ":"))


class FakeTransport:
    """Scripted transport.

    ``pages`` maps a cursor token to a :class:`PageResponse`. Per
    cursor, ``failures`` holds exceptions (or callables producing
    exceptions) raised before the canned response is returned, which
    models transient 429/5xx storms. ``latency`` is waited on the
    injected fake clock so tests stay deterministic.
    """

    def __init__(
        self,
        pages: dict[Any, PageResponse] | list[tuple[Any, PageResponse]],
        *,
        failures: dict[Any, list[Any]] | None = None,
        latency: float = 0.0,
        clock: FakeClock | None = None,
    ):
        page_items = pages.items() if isinstance(pages, dict) else pages
        self.pages = {_token(cursor): response for cursor, response in page_items}
        self.failures = {_token(cursor): list(events) for cursor, events in (failures or {}).items()}
        self.latency = latency
        self.clock = clock or FakeClock()
        self.calls: list[Any] = []

    def fetch(self, request: PageRequest) -> PageResponse:
        if self.latency:
            self.clock.sleep(self.latency)
        self.calls.append(request.cursor)
        token = _token(request.cursor)
        pending = self.failures.get(token)
        if pending:
            failure = pending.pop(0)
            raise failure() if callable(failure) else failure
        if token not in self.pages:
            raise AssertionError(f"unexpected request for cursor {request.cursor!r}")
        return self.pages[token]


def rate_limited(retry_after: float | None = None) -> RateLimitError:
    return RateLimitError(retry_after=retry_after)


def server_error(status_code: int = 503) -> ServerError:
    return ServerError(status_code=status_code)


def stale_cancelled() -> StaleResponseError:
    return StaleResponseError("cancelled")
