"""Resumable, retry-aware cursor pagination collector.

The library never performs network I/O itself: callers inject a
:class:`~resumable_page_fetcher.models.Transport` implementation.
"""

from .clock import Clock, FakeClock, SystemClock
from .exceptions import (
    CheckpointError,
    CursorLoopError,
    EmptyPageLimitError,
    PaginationError,
    RateLimitError,
    RetryExhaustedError,
    ServerError,
    StaleResponseError,
)
from .models import Checkpoint, PageRequest, PageResponse, Transport
from .paginator import Paginator
from .retry import RetryExecutor, RetryPolicy, parse_retry_after
from .storage import CheckpointStore, JsonlCheckpointStore

__all__ = [
    "Checkpoint",
    "CheckpointError",
    "CheckpointStore",
    "Clock",
    "CursorLoopError",
    "EmptyPageLimitError",
    "FakeClock",
    "JsonlCheckpointStore",
    "PageRequest",
    "PageResponse",
    "PaginationError",
    "Paginator",
    "parse_retry_after",
    "RateLimitError",
    "RetryExhaustedError",
    "RetryExecutor",
    "RetryPolicy",
    "ServerError",
    "StaleResponseError",
    "SystemClock",
    "Transport",
]
