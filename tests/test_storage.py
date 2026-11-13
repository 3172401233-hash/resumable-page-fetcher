import json
import os
import tempfile
import unittest

from resumable_page_fetcher import Checkpoint, JsonlCheckpointStore


class JsonlStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = JsonlCheckpointStore(self.tempdir.name)

    def test_first_load_returns_none(self):
        self.assertIsNone(self.store.load())

    def test_commit_persists_records_and_checkpoint(self):
        checkpoint = Checkpoint(
            next_cursor="c2", cursors_seen=["null"], seen_item_keys=["a"], record_count=1
        )
        written = self.store.commit(checkpoint, [{"id": "a"}])

        self.assertEqual(written, 1)
        reloaded = self.store.load()
        self.assertEqual(reloaded.next_cursor, "c2")
        self.assertEqual(reloaded.seen_item_keys, ["a"])
        self.assertEqual(list(self.store.iter_records()), [{"id": "a"}])

    def test_checkpoint_is_written_via_atomic_replace(self):
        checkpoint = Checkpoint(next_cursor="c1", seen_item_keys=["x"], record_count=1)
        self.store.commit(checkpoint, [{"id": "x"}])

        self.assertTrue(os.path.exists(os.path.join(self.tempdir.name, "checkpoint.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tempdir.name, ".checkpoint.json.tmp")))

    def test_crash_before_checkpoint_commit_truncates_orphan_records(self):
        first = Checkpoint(
            next_cursor="c2", cursors_seen=["null"], seen_item_keys=["a"], record_count=1
        )
        self.store.commit(first, [{"id": "a"}])

        records_path = os.path.join(self.tempdir.name, "records.jsonl")
        with open(records_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "b"}) + "\n")
            handle.write(json.dumps({"id": "c"}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        recovered = self.store.load()

        self.assertEqual(recovered.next_cursor, "c2")
        self.assertEqual(recovered.seen_item_keys, ["a"])
        self.assertEqual(list(self.store.iter_records()), [{"id": "a"}])

    def test_iter_records_replays_every_commit_in_order(self):
        self.store.commit(
            Checkpoint(next_cursor="c2", seen_item_keys=["a", "b"], record_count=2),
            [{"id": "a"}, {"id": "b"}],
        )
        self.store.commit(
            Checkpoint(next_cursor=None, seen_item_keys=["a", "b", "c"], record_count=3, done=True),
            [{"id": "c"}],
        )

        self.assertEqual(
            [item["id"] for item in self.store.iter_records()],
            ["a", "b", "c"],
        )

    def test_record_count_is_independent_of_dedup_key_count(self):
        checkpoint = Checkpoint(
            next_cursor="c2",
            cursors_seen=["null"],
            seen_item_keys=["group-1"],
            record_count=3,
        )
        self.store.commit(
            checkpoint,
            [{"id": "a", "group": "group-1"}, {"id": "b"}, {"id": "c"}],
        )
        records_path = os.path.join(self.tempdir.name, "records.jsonl")
        with open(records_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "orphan"}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        recovered = self.store.load()

        self.assertEqual(recovered.record_count, 3)
        self.assertEqual(len(list(self.store.iter_records())), 3)


if __name__ == "__main__":
    unittest.main()
