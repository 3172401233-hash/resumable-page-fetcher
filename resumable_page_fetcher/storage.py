"""Durable, crash-safe storage for collected records and checkpoints.

The store keeps two files in one directory:

``records.jsonl``
    One JSON-encoded record per line, appended in page order.
``checkpoint.json``
    The :class:`Checkpoint` needed to resume a run.

Atomicity of a page commit is achieved by:

1. Appending the page's records to the data file and flushing/fsync-ing.
2. Writing the new checkpoint to a temporary file, fsync-ing it and
   atomically replacing ``checkpoint.json`` via ``os.replace``.

If the process dies before step 2, the next :meth:`load` truncates any
uncommitted tail of ``records.jsonl`` back to the length recorded in
the last durable checkpoint, so records and checkpoint can never
 disagree and no record is ever visible without its checkpoint.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Protocol, runtime_checkable

from .exceptions import CheckpointError
from .models import Checkpoint

CHECKPOINT_FILE = "checkpoint.json"
RECORDS_FILE = "records.jsonl"


@runtime_checkable
class CheckpointStore(Protocol):
    """Storage contract required by :class:`Paginator`."""

    def load(self) -> Checkpoint | None:
        """Return the last committed checkpoint, or ``None`` on first run."""
        ...

    def commit(self, checkpoint: Checkpoint, records: Iterable[Any]) -> int:
        """Atomically commit one fully processed page.

        Returns the number of new (deduplicated) records persisted.
        """
        ...

    def iter_records(self) -> Iterable[Any]:
        """Re-iterate every committed record, oldest first."""
        ...


def _fsync_dir(path: str) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class JsonlCheckpointStore:
    """Reference filesystem implementation of :class:`CheckpointStore`."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.checkpoint_path = os.path.join(directory, CHECKPOINT_FILE)
        self.records_path = os.path.join(directory, RECORDS_FILE)

    def load(self) -> Checkpoint | None:
        if not os.path.exists(self.checkpoint_path):
            return None
        try:
            with open(self.checkpoint_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            checkpoint = Checkpoint(
                next_cursor=raw.get("next_cursor"),
                cursors_seen=list(raw.get("cursors_seen", [])),
                seen_item_keys=list(raw.get("seen_item_keys", [])),
                record_count=int(raw.get("record_count", len(raw.get("seen_item_keys", [])))),
                empty_streak=int(raw.get("empty_streak", 0)),
                done=bool(raw.get("done", False)),
            )
        except (OSError, ValueError, TypeError) as error:
            raise CheckpointError(f"cannot read checkpoint: {error}") from error
        self._truncate_to_checkpoint()
        return checkpoint

    def commit(self, checkpoint: Checkpoint, records: Iterable[Any]) -> int:
        records = list(records)
        with open(self.records_path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        payload = {
            "version": 1,
            "next_cursor": checkpoint.next_cursor,
            "cursors_seen": checkpoint.cursors_seen,
            "seen_item_keys": checkpoint.seen_item_keys,
            "record_count": checkpoint.record_count,
            "empty_streak": checkpoint.empty_streak,
            "done": checkpoint.done,
        }
        tmp_path = os.path.join(self.directory, f".{CHECKPOINT_FILE}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.checkpoint_path)
        _fsync_dir(self.directory)
        return len(records)

    def iter_records(self) -> Iterable[Any]:
        if not os.path.exists(self.records_path):
            return
        with open(self.records_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def _truncate_to_checkpoint(self) -> None:
        """Drop record lines beyond the count the checkpoint committed."""
        if not os.path.exists(self.records_path):
            return
        with open(self.checkpoint_path, encoding="utf-8") as handle:
            committed = int(json.load(handle).get("record_count", 0))
        with open(self.records_path, "rb+") as handle:
            kept = 0
            while kept < committed:
                line = handle.readline()
                if not line:
                    break
                kept += 1
            handle.truncate(handle.tell())
