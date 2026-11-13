"""Resumable cursor-based pagination collector."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Iterator

from .clock import Clock, SystemClock
from .exceptions import (
    CursorLoopError,
    EmptyPageLimitError,
    PaginationError,
    StaleResponseError,
)
from .models import Checkpoint, PageRequest, PageResponse, Transport
from .retry import RetryExecutor, RetryPolicy


def _cursor_token(cursor: Any) -> str:
    """Canonical, order-free representation of an cursor.

    Cursors are compared for equality only (never by magnitude) and
    must round-trip through JSON so checkpoints survive restarts.
    """
    try:
        return json.dumps(cursor, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PaginationError(f"cursor is not JSON serializable: {error}") from error


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "_asdict"):
        return value._asdict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


class Paginator:
    """Collects pages through an injected transport with durable resume support.

    A page is only committed after the whole page has been fetched and
    deduplicated. Each commit atomically persists the new records and
    the next cursor, so a crash at any point lets the next run continue
    from the last committed page instead of restarting at page one.
    """

    def __init__(
        self,
        transport: Transport,
        store: CheckpointStore,
        *,
        initial_cursor: Any = None,
        page_size: int | None = None,
        item_key: Callable[[Any], Any] | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Clock | None = None,
        max_empty_pages: int = 3,
    ):
        self.transport = transport
        self.store = store
        self.initial_cursor = initial_cursor
        self.page_size = page_size
        self.item_key = item_key or (lambda item: json.dumps(item, sort_keys=True, default=_json_default))
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock: Clock = clock or SystemClock()
        self.max_empty_pages = max_empty_pages
        self.executor = RetryExecutor(self.retry_policy, self.clock)

    def run(self) -> list[Any]:
        """Drive collection to completion and return every stored record."""
        for _ in self.iter_pages():
            pass
        return list(self.iter_records())

    def iter_records(self) -> Iterator[Any]:
        """Re-iterate all committed records without touching the network."""
        yield from self.store.iter_records()

    def iter_pages(self) -> Iterator[list[Any]]:
        """Yield each page's newly persisted records, resuming when possible.

        Repeated calls keep yielding only pages collected after the call
        started, while :meth:`iter_records` always replays everything
        committed so far.
        """
        checkpoint = self._load_checkpoint()
        if checkpoint.done:
            return

        seen_item_keys = set(checkpoint.seen_item_keys)
        cursors_seen = set(checkpoint.cursors_seen)
        cursor = checkpoint.next_cursor

        while True:
            request = PageRequest(cursor=cursor, page_size=self.page_size)
            response: PageResponse = self.executor.run(
                lambda request=request: self._fetch_checked(request, cursors_seen)
            )

            items = list(response.items)
            new_records: list[Any] = []
            for item in items:
                key = self.item_key(item)
                if key in seen_item_keys:
                    continue
                seen_item_keys.add(key)
                new_records.append(item)

            if items:
                checkpoint.empty_streak = 0
            else:
                checkpoint.empty_streak += 1
                if checkpoint.empty_streak > self.max_empty_pages:
                    raise EmptyPageLimitError(
                        f"more than {self.max_empty_pages} consecutive empty pages"
                    )

            next_cursor = response.next_cursor
            last_page = next_cursor is None
            checkpoint.next_cursor = next_cursor
            checkpoint.seen_item_keys = list(seen_item_keys)
            checkpoint.cursors_seen = list(cursors_seen)
            checkpoint.record_count += len(new_records)
            checkpoint.done = last_page
            self.store.commit(checkpoint, new_records)

            yield new_records

            if last_page:
                return
            if _cursor_token(next_cursor) in cursors_seen:
                raise CursorLoopError("next cursor points at an already fetched page", next_cursor)
            cursor = next_cursor

    def _load_checkpoint(self) -> Checkpoint:
        loaded = self.store.load()
        if loaded is not None:
            return loaded
        return Checkpoint(next_cursor=self.initial_cursor)

    def _fetch_checked(self, request: PageRequest, cursors_seen: set[str]) -> PageResponse:
        token = _cursor_token(request.cursor)
        if token in cursors_seen:
            raise CursorLoopError("requested cursor was already fetched", request.cursor)
        response = self.transport.fetch(request)

        if response.cancelled:
            raise StaleResponseError("response was cancelled before acceptance")
        if response.expires_at is not None and response.expires_at <= self.clock.now():
            raise StaleResponseError("response expired before acceptance")

        next_token = _cursor_token(response.next_cursor)
        if next_token in cursors_seen and response.next_cursor is not None:
            raise CursorLoopError("page returned an already seen next cursor", response.next_cursor)
        if token not in cursors_seen:
            cursors_seen.add(token)
        return response
