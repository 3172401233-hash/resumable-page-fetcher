"""Retry policy: Retry-After handling, capped exponential backoff, seeded jitter."""

from __future__ import annotations

import random
from email.utils import parsedate_to_datetime

from .clock import Clock, SystemClock
from .exceptions import RateLimitError, RetryExhaustedError, ServerError, StaleResponseError


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds.

    Accepts both the delta-seconds form (``"120"``) and the HTTP-date
    form. Unparseable values return ``None`` so callers fall back to
    their normal backoff schedule. Negative deltas are clamped to zero.
    """
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    reference = now if now is not None else deadline.timestamp()
    return max(0.0, deadline.timestamp() - reference)


class RetryPolicy:
    """Configuration for retrying a single page request.

    - 429 responses wait for the transport-provided ``Retry-After``
      value when available, otherwise fall back to the backoff schedule.
    - 5xx responses use exponential backoff
      ``base_delay * factor ** attempt`` capped at ``max_delay``.
    - A uniform jitter in ``[0, jitter)`` is added to every computed
      delay. The jitter source is built from ``jitter_seed`` so tests
      get reproducible waits.
    - ``StaleResponseError`` (cancelled/expired response) is never
      retried here and never moves the cursor.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        factor: float = 2.0,
        max_delay: float = 30.0,
        jitter: float = 0.25,
        jitter_seed: int = 0,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_delay < 0 or factor < 1 or max_delay < base_delay or jitter < 0:
            raise ValueError("invalid backoff parameters")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.jitter_seed = jitter_seed


class RetryExecutor:
    """Runs a callable under a :class:`RetryPolicy` with an injected clock."""

    def __init__(self, policy: RetryPolicy, clock: Clock | None = None):
        self.policy = policy
        self.clock: Clock = clock or SystemClock()
        self.rng = random.Random(policy.jitter_seed)
        self.delays: list[float] = []

    def run(self, operation):
        """Invoke ``operation`` until it succeeds or attempts run out.

        ``StaleResponseError`` propagates immediately. When every
        attempt is exhausted, :class:`RetryExhaustedError` wraps the
        last failure; no partial state is committed by this class.
        """
        last_error: Exception | None = None
        for attempt in range(self.policy.max_attempts):
            try:
                return operation()
            except StaleResponseError:
                raise
            except (RateLimitError, ServerError) as error:
                last_error = error
                if attempt == self.policy.max_attempts - 1:
                    break
                delay = self._delay_for(attempt, error)
                self.delays.append(delay)
                self.clock.sleep(delay)
        raise RetryExhaustedError(
            f"giving up after {self.policy.max_attempts} attempts",
            self.policy.max_attempts,
            last_error,  # type: ignore[arg-type]
        )

    def _delay_for(self, attempt: int, error: Exception) -> float:
        if isinstance(error, RateLimitError) and error.retry_after is not None:
            wait = max(0.0, float(error.retry_after))
        else:
            wait = min(
                self.policy.max_delay,
                self.policy.base_delay * (self.policy.factor ** attempt),
            )
        if self.policy.jitter:
            wait += self.rng.random() * self.policy.jitter
        return wait
