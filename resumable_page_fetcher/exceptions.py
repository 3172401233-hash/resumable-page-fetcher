"""Exception hierarchy for the resumable pagination collector."""


class PaginationError(Exception):
    """Base class for every error raised by this package."""


class RateLimitError(PaginationError):
    """The transport reports HTTP 429.

    ``retry_after`` carries the parsed ``Retry-After`` value in seconds
    (``None`` when the header is absent or unparseable).
    """

    def __init__(self, message: str = "rate limited (429)", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(PaginationError):
    """The transport reports an HTTP 5xx response."""

    def __init__(self, message: str = "server error (5xx)", status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class StaleResponseError(PaginationError):
    """A response was cancelled or expired before it could be accepted.

    Such a response must never advance the cursor or update the
    checkpoint, so it is treated as a non-retrying failure by the
    collector. The caller may start another run later and resume from
    the last committed cursor.
    """


class RetryExhaustedError(PaginationError):
    """All retry attempts for one page have been used up."""

    def __init__(self, message: str, attempts: int, last_error: BaseException):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class CursorLoopError(PaginationError):
    """A cursor that was already fetched is requested again."""

    def __init__(self, message: str, cursor: object):
        super().__init__(message)
        self.cursor = cursor


class EmptyPageLimitError(PaginationError):
    """Too many consecutive pages yielded no new records.

    This usually means the endpoint keeps paging forever without
    reaching a terminal ``next_cursor`` of ``None``.
    """


class CheckpointError(PaginationError):
    """Checkpoint data is missing, corrupt or otherwise unusable."""
