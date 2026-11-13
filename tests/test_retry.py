import unittest

from resumable_page_fetcher import (
    FakeClock,
    RateLimitError,
    RetryExhaustedError,
    RetryExecutor,
    RetryPolicy,
    ServerError,
    StaleResponseError,
    parse_retry_after,
)


class RetryPolicyTests(unittest.TestCase):
    def test_parse_retry_after_seconds_and_invalid_values(self):
        self.assertEqual(parse_retry_after("12"), 12.0)
        self.assertEqual(parse_retry_after("  3.5 "), 3.5)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("not-a-header"))

    def test_parse_retry_after_http_date_uses_injected_reference_time(self):
        delay = parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=1792567600.0)
        self.assertEqual(delay, 80.0)

    def test_parse_retry_after_clamps_negative_delta(self):
        self.assertEqual(parse_retry_after("-5"), 0.0)

    def test_success_on_first_attempt_never_sleeps(self):
        clock = FakeClock()
        executor = RetryExecutor(RetryPolicy(), clock)
        calls = []

        result = executor.run(lambda: calls.append(1) or "ok")

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_rate_limit_honors_retry_after_header(self):
        clock = FakeClock()
        policy = RetryPolicy(base_delay=1.0, factor=2.0, jitter=0.0)
        executor = RetryExecutor(policy, clock)
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimitError(retry_after=7.5)
            return "done"

        self.assertEqual(executor.run(flaky), "done")
        self.assertEqual(clock.sleeps, [7.5])

    def test_server_errors_use_capped_exponential_backoff(self):
        clock = FakeClock()
        policy = RetryPolicy(
            max_attempts=5,
            base_delay=1.0,
            factor=2.0,
            max_delay=5.0,
            jitter=0.0,
        )
        executor = RetryExecutor(policy, clock)

        def always_fails():
            raise ServerError(status_code=502)

        with self.assertRaises(RetryExhaustedError) as context:
            executor.run(always_fails)

        self.assertEqual(context.exception.attempts, 5)
        self.assertIsInstance(context.exception.last_error, ServerError)
        self.assertEqual(clock.sleeps, [1.0, 2.0, 4.0, 5.0])

    def test_jitter_is_deterministic_for_a_fixed_seed(self):
        policy_a = RetryPolicy(base_delay=1.0, factor=2.0, jitter=1.0, jitter_seed=42)
        policy_b = RetryPolicy(base_delay=1.0, factor=2.0, jitter=1.0, jitter_seed=42)
        clock_a, clock_b = FakeClock(), FakeClock()

        for executor in (RetryExecutor(policy_a, clock_a), RetryExecutor(policy_b, clock_b)):
            with self.assertRaises(RetryExhaustedError):
                executor.run(lambda: (_ for _ in ()).throw(ServerError()))

        self.assertEqual(clock_a.sleeps, clock_b.sleeps)
        self.assertTrue(all(delay >= 1.0 for delay in clock_a.sleeps))

    def test_stale_response_is_not_retried(self):
        clock = FakeClock()
        executor = RetryExecutor(RetryPolicy(max_attempts=5), clock)
        calls = {"n": 0}

        def stale():
            calls["n"] += 1
            raise StaleResponseError("expired")

        with self.assertRaises(StaleResponseError):
            executor.run(stale)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(clock.sleeps, [])

    def test_fallback_backoff_when_retry_after_header_missing(self):
        clock = FakeClock()
        policy = RetryPolicy(base_delay=1.0, factor=2.0, jitter=0.0)
        executor = RetryExecutor(policy, clock)
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError(retry_after=None)
            return "ok"

        self.assertEqual(executor.run(flaky), "ok")
        self.assertEqual(clock.sleeps, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
