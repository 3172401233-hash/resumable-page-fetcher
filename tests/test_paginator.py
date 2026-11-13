import json
import os
import tempfile
import unittest

from resumable_page_fetcher import (
    Checkpoint,
    CheckpointError,
    CursorLoopError,
    EmptyPageLimitError,
    FakeClock,
    JsonlCheckpointStore,
    PageRequest,
    PageResponse,
    Paginator,
    RateLimitError,
    RetryExhaustedError,
    RetryPolicy,
    ServerError,
    StaleResponseError,
)

from tests.fakes import FakeTransport


def item(identifier):
    return {"id": identifier}


class PaginatorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.clock = FakeClock(start=1000.0)

    def store(self):
        return JsonlCheckpointStore(self.tempdir.name)

    def paginator(self, transport, **kwargs):
        policy = kwargs.pop("retry_policy", RetryPolicy(jitter=0.0))
        return Paginator(
            transport,
            self.store(),
            clock=self.clock,
            retry_policy=policy,
            item_key=lambda record: record["id"],
            **kwargs,
        )

    def test_collects_all_pages_until_terminal_cursor(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"), item("b")), next_cursor="c1"),
                "c1": PageResponse((item("c"),), next_cursor="c2"),
                "c2": PageResponse((item("d"),), next_cursor=None),
            },
            clock=self.clock,
        )

        pages = list(self.paginator(transport).iter_pages())

        self.assertEqual(pages, [[item("a"), item("b")], [item("c")], [item("d")]])
        self.assertEqual(transport.calls, [None, "c1", "c2"])
        checkpoint = self.store().load()
        self.assertTrue(checkpoint.done)
        self.assertIsNone(checkpoint.next_cursor)
        self.assertEqual(
            [record["id"] for record in self.store().iter_records()],
            ["a", "b", "c", "d"],
        )

    def test_rate_limit_and_server_errors_retry_then_succeed(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"),), next_cursor="c1"),
                "c1": PageResponse((item("b"),), next_cursor=None),
            },
            failures={
                None: [RateLimitError(retry_after=3.0), ServerError(status_code=503)],
            },
            clock=self.clock,
        )
        policy = RetryPolicy(max_attempts=5, base_delay=1.0, factor=2.0, jitter=0.0)

        records = self.paginator(transport, retry_policy=policy).run()

        self.assertEqual([record["id"] for record in records], ["a", "b"])
        self.assertEqual(transport.calls, [None, None, None, "c1"])
        self.assertEqual(self.clock.sleeps, [3.0, 2.0])

    def test_retry_budget_exhaustion_keeps_earlier_checkpoint_intact(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"),), next_cursor="c1"),
                "c1": PageResponse((item("b"),), next_cursor=None),
            },
            failures={"c1": [ServerError(status_code=500)] * 4},
            clock=self.clock,
        )
        policy = RetryPolicy(max_attempts=4, base_delay=1.0, jitter=0.0)

        with self.assertRaises(RetryExhaustedError):
            self.paginator(transport, retry_policy=policy).run()

        checkpoint = self.store().load()
        self.assertEqual(checkpoint.next_cursor, "c1")
        self.assertFalse(checkpoint.done)
        self.assertEqual([record["id"] for record in self.store().iter_records()], ["a"])

    def test_cancelled_response_never_advances_cursor(self):
        transport = FakeTransport(
            {None: PageResponse((item("a"),), next_cursor="c1", cancelled=True)},
            clock=self.clock,
        )

        with self.assertRaises(StaleResponseError):
            self.paginator(transport).run()

        self.assertIsNone(self.store().load())
        self.assertEqual(transport.calls, [None])

    def test_expired_response_never_advances_cursor(self):
        transport = FakeTransport(
            {None: PageResponse((item("a"),), next_cursor="c1", expires_at=999.0)},
            clock=self.clock,
        )

        with self.assertRaises(StaleResponseError):
            self.paginator(transport).run()
        self.assertIsNone(self.store().load())

    def test_duplicate_items_across_pages_are_deduplicated(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"), item("b")), next_cursor="c1"),
                "c1": PageResponse((item("b"), item("c")), next_cursor=None),
            },
            clock=self.clock,
        )

        pages = list(self.paginator(transport).iter_pages())

        self.assertEqual(pages, [[item("a"), item("b")], [item("c")]])
        self.assertEqual(
            [record["id"] for record in self.store().iter_records()],
            ["a", "b", "c"],
        )

    def test_page_re_served_after_resume_does_not_duplicate_records(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"),), next_cursor="c1"),
                "c1": PageResponse((item("b"),), next_cursor="c2"),
                "c2": PageResponse((item("c"),), next_cursor=None),
            },
            clock=self.clock,
        )
        generator = self.paginator(transport).iter_pages()
        next(generator)

        replay_transport = FakeTransport(
            {
                "c1": PageResponse((item("b"), item("a")), next_cursor="c2"),
                "c2": PageResponse((item("c"),), next_cursor=None),
            },
            clock=self.clock,
        )
        records = self.paginator(replay_transport).run()

        self.assertEqual([record["id"] for record in records], ["a", "b", "c"])

    def test_repeated_cursor_raises_loop_error(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"),), next_cursor="c1"),
                "c1": PageResponse((item("b"),), next_cursor="c1"),
            },
            clock=self.clock,
        )

        with self.assertRaises(CursorLoopError):
            self.paginator(transport).run()

    def test_empty_pages_commit_and_terminal_page_finishes(self):
        transport = FakeTransport(
            {
                None: PageResponse((), next_cursor="c1"),
                "c1": PageResponse((item("a"),), next_cursor="c2"),
                "c2": PageResponse((), next_cursor=None),
            },
            clock=self.clock,
        )

        records = self.paginator(transport).run()

        self.assertEqual([record["id"] for record in records], ["a"])
        self.assertTrue(self.store().load().done)

    def test_endless_empty_pagination_is_stopped(self):
        transport = FakeTransport(
            {
                None: PageResponse((), next_cursor="c1"),
                "c1": PageResponse((), next_cursor="c2"),
                "c2": PageResponse((), next_cursor="c3"),
                "c3": PageResponse((), next_cursor="c4"),
                "c4": PageResponse((), next_cursor="c5"),
            },
            clock=self.clock,
        )

        with self.assertRaises(EmptyPageLimitError):
            self.paginator(transport, max_empty_pages=3).run()

    def test_opaque_cursors_are_passed_through_without_ordering(self):
        cursor_one = {"region": "east", "shard": [1, 2]}
        cursor_two = {"region": "west", "shard": [3]}
        token_one = json.dumps(cursor_one, sort_keys=True, separators=(",", ":"))
        transport = FakeTransport(
            [
                (None, PageResponse((item("a"),), next_cursor=cursor_one)),
                (cursor_one, PageResponse((item("b"),), next_cursor=cursor_two)),
                (cursor_two, PageResponse((item("c"),), next_cursor=None)),
            ],
            clock=self.clock,
        )

        records = self.paginator(transport).run()

        self.assertEqual([record["id"] for record in records], ["a", "b", "c"])
        checkpoint = self.store().load()
        self.assertEqual(checkpoint.next_cursor, None)
        self.assertIn(token_one, checkpoint.cursors_seen)

    def test_resumes_from_last_committed_page_after_simulated_crash(self):
        first_transport = FakeTransport(
            {
                None: PageResponse((item("a"),), next_cursor="c1"),
                "c1": PageResponse((item("b"),), next_cursor="c2"),
                "c2": PageResponse((item("c"),), next_cursor=None),
            },
            clock=self.clock,
        )
        paginator = self.paginator(first_transport)
        generator = paginator.iter_pages()
        next(generator)

        records_path = os.path.join(self.tempdir.name, "records.jsonl")
        with open(records_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item("orphan")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        second_transport = FakeTransport(
            {
                "c1": PageResponse((item("b"),), next_cursor="c2"),
                "c2": PageResponse((item("c"),), next_cursor=None),
            },
            clock=self.clock,
        )
        records = self.paginator(second_transport).run()

        self.assertEqual(second_transport.calls, ["c1", "c2"])
        self.assertEqual([record["id"] for record in records], ["a", "b", "c"])

    def test_completed_run_does_not_touch_transport_and_reiterates(self):
        transport = FakeTransport(
            {None: PageResponse((item("a"),), next_cursor=None)},
            clock=self.clock,
        )
        self.paginator(transport).run()

        fresh = self.paginator(transport)
        self.assertEqual(fresh.run(), [item("a")])
        self.assertEqual(transport.calls, [None])
        self.assertEqual([record["id"] for record in fresh.iter_records()], ["a"])

    def test_iteration_can_stop_midway_and_resume_without_duplicates(self):
        transport = FakeTransport(
            {
                None: PageResponse((item("a"), item("b")), next_cursor="c1"),
                "c1": PageResponse((item("c"),), next_cursor="c2"),
                "c2": PageResponse((item("d"),), next_cursor=None),
            },
            clock=self.clock,
        )

        generator = self.paginator(transport).iter_pages()
        next(generator)
        generator.close()

        resumed_transport = FakeTransport(
            {
                "c1": PageResponse((item("c"),), next_cursor="c2"),
                "c2": PageResponse((item("d"),), next_cursor=None),
            },
            clock=self.clock,
        )
        records = self.paginator(resumed_transport).run()

        self.assertEqual(resumed_transport.calls, ["c1", "c2"])
        self.assertEqual([record["id"] for record in records], ["a", "b", "c", "d"])

    def test_page_size_is_passed_through_to_transport(self):
        seen_requests: list[PageRequest] = []

        class CapturingTransport:
            def fetch(self, request):
                seen_requests.append(request)
                return PageResponse((item("a"),), next_cursor=None)

        self.paginator(CapturingTransport(), page_size=25).run()

        self.assertEqual(seen_requests[0].page_size, 25)

    def test_corrupt_checkpoint_raises_checkpoint_error(self):
        self.store().commit(Checkpoint(next_cursor="c1", seen_item_keys=[]), [])
        with open(os.path.join(self.tempdir.name, "checkpoint.json"), "w", encoding="utf-8") as handle:
            handle.write("{not valid json")

        with self.assertRaises(CheckpointError):
            self.store().load()

    def test_seeded_jitter_produces_reproducible_waits_across_runs(self):
        def build():
            clock = FakeClock()
            transport = FakeTransport(
                {None: PageResponse((item("a"),), next_cursor=None)},
                failures={None: [RateLimitError(retry_after=None)] * 2},
                clock=clock,
            )
            policy = RetryPolicy(base_delay=1.0, factor=2.0, jitter=0.5, jitter_seed=7)
            paginator = Paginator(
                transport,
                JsonlCheckpointStore(tempfile.mkdtemp()),
                clock=clock,
                retry_policy=policy,
            )
            paginator.run()
            return clock.sleeps

        self.assertEqual(build(), build())


if __name__ == "__main__":
    unittest.main()
