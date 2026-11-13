"""Data models exchanged between the collector and the injected transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class PageRequest:
    """One page request.

    ``cursor`` is opaque: the collector never compares cursors by
    magnitude, it only stores and returns them verbatim. The first
    request uses the caller-supplied initial cursor (commonly ``None``).
    """

    cursor: Any
    page_size: int | None = None


@dataclass(frozen=True)
class PageResponse:
    """The result of a successful transport call.

    ``next_cursor`` is ``None`` (or absent in the transport's own
    representation) on the last page.

    ``expires_at`` is an optional deadline measured on the injected
    clock; ``cancelled`` lets a transport mark a response that was
    cancelled before the caller could accept it. Either condition makes
    the response stale, so the cursor must not be advanced.
    """

    items: Iterable = ()
    next_cursor: Any = None
    cancelled: bool = False
    expires_at: float | None = None


@dataclass
class Checkpoint:
    """Everything required to resume a run after a process restart.

    ``cursors_seen`` stores canonical JSON tokens of cursors purely for
    loop detection; cursors are compared by structural equality only,
    never by magnitude.
    """

    next_cursor: Any = None
    cursors_seen: list = field(default_factory=list)
    seen_item_keys: list = field(default_factory=list)
    record_count: int = 0
    empty_streak: int = 0
    done: bool = False


@runtime_checkable
class Transport(Protocol):
    """Caller-injected request transport.

    Implementations perform the real HTTP work and translate results
    into :class:`PageResponse`. They signal failures with
    :class:`RateLimitError`, :class:`ServerError` or
    :class:`StaleResponseError`; the library itself never opens a
    socket.
    """

    def fetch(self, request: PageRequest) -> PageResponse:
        """Fetch one page, raising a package error on failure."""
        ...
