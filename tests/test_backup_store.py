"""Tests for backup filesystem primitives."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _make_dump(d: Path, seq: int, minute: int, *, decoded=True, manifest=True):
    """Create a fake dump (+manifest) and return its path."""
    from src.cli import backup_store

    when = datetime(2026, 8, 9, 2, minute, 0, tzinfo=timezone.utc)
    p = backup_store.dump_path(d, seq, when)
    p.write_bytes(b"payload-%d" % seq)
    if manifest:
        backup_store.write_manifest(backup_store.manifest_path(p), {
            "sequence": seq,
            "dump_size": p.stat().st_size,
            "dump_sha256": backup_store.sha256_of(p),
            "verification": {
                "archive_written": when.isoformat(),
                "payload_decoded": when.isoformat() if decoded else None,
            },
        })
    return p


class TestSequence:
    def test_starts_at_one_when_empty(self, tmp_path):
        from src.cli import backup_store
        assert backup_store.next_sequence(tmp_path) == 1

    def test_recovers_from_filenames_when_state_missing(self, tmp_path):
        from src.cli import backup_store
        _make_dump(tmp_path, 7, 0)
        (tmp_path / "sequence").unlink(missing_ok=True)
        assert backup_store.next_sequence(tmp_path) == 8

    def test_recovers_from_corrupt_state(self, tmp_path):
        from src.cli import backup_store
        _make_dump(tmp_path, 4, 0)
        (tmp_path / "sequence").write_text("not a number")
        assert backup_store.next_sequence(tmp_path) == 5

    def test_gap_is_not_an_error(self, tmp_path):
        from src.cli import backup_store
        (tmp_path / "sequence").write_text("11")
        assert backup_store.next_sequence(tmp_path) == 12

    def test_peek_does_not_consume(self, tmp_path):
        """--dry-run must not burn a sequence number."""
        from src.cli import backup_store

        backup_store.ensure_dir(tmp_path)
        assert backup_store.peek_sequence(tmp_path) == 1
        assert backup_store.peek_sequence(tmp_path) == 1
        assert backup_store.next_sequence(tmp_path) == 1
        assert backup_store.peek_sequence(tmp_path) == 2


class TestPublish:
    def test_refuses_to_clobber(self, tmp_path):
        from src.cli import backup_store

        partial = tmp_path / "x.partial.1"
        partial.write_bytes(b"new")
        final = tmp_path / "x.dump"
        final.write_bytes(b"existing")

        with pytest.raises(FileExistsError):
            backup_store.publish(partial, final)

        assert final.read_bytes() == b"existing"

    def test_publishes_and_removes_partial(self, tmp_path):
        from src.cli import backup_store

        partial = tmp_path / "x.partial.1"
        partial.write_bytes(b"data")
        final = tmp_path / "x.dump"

        backup_store.publish(partial, final)

        assert final.read_bytes() == b"data"
        assert not partial.exists()


class TestManifest:
    def test_roundtrip(self, tmp_path):
        from src.cli import backup_store

        dump = _make_dump(tmp_path, 1, 0)
        loaded = backup_store.read_manifest(dump)

        assert loaded["sequence"] == 1
        assert loaded["dump_sha256"] == backup_store.sha256_of(dump)

    def test_rejects_torn_manifest(self, tmp_path):
        from src.cli import backup_store

        dump = _make_dump(tmp_path, 1, 0)
        backup_store.manifest_path(dump).write_text("{ not json")

        assert backup_store.read_manifest(dump) is None


class TestRetention:
    def test_keeps_newest_n_by_sequence(self, tmp_path):
        from src.cli import backup_store

        dumps = [_make_dump(tmp_path, seq, seq) for seq in range(1, 6)]
        doomed = backup_store.select_for_deletion(tmp_path, keep=3, current=None)

        assert set(doomed) == {dumps[0], dumps[1]}

    def test_never_deletes_current_run_output(self, tmp_path):
        """Simulated clock rollback: current dump has the lowest sequence."""
        from src.cli import backup_store

        old = [_make_dump(tmp_path, seq, seq) for seq in range(10, 14)]
        current = _make_dump(tmp_path, 2, 0)

        doomed = backup_store.select_for_deletion(tmp_path, keep=2, current=current)

        assert current not in doomed

    def test_unverified_dump_is_not_eligible(self, tmp_path):
        """archive_written but no payload_decoded: never counted, never pruned."""
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        _make_dump(tmp_path, 2, 2)
        unverified = _make_dump(tmp_path, 3, 3, decoded=False)

        doomed = backup_store.select_for_deletion(tmp_path, keep=1, current=None)

        assert unverified not in doomed

    def test_manifestless_file_never_deleted(self, tmp_path):
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        orphan = _make_dump(tmp_path, 2, 2, manifest=False)

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert orphan not in doomed

    def test_keep_set_never_emptied(self, tmp_path):
        """keep=0 must still leave one dump. A retention bug must not be able
        to delete every backup."""
        from src.cli import backup_store

        dumps = [_make_dump(tmp_path, seq, seq) for seq in range(1, 4)]
        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert len(doomed) == len(dumps) - 1
        assert dumps[-1] not in doomed  # newest by sequence survives

    def test_sha_mismatch_makes_dump_ineligible(self, tmp_path):
        from src.cli import backup_store

        good = _make_dump(tmp_path, 1, 1)
        tampered = _make_dump(tmp_path, 2, 2)
        tampered.write_bytes(b"different content entirely")

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert tampered not in doomed

    def test_unrecognized_names_ignored(self, tmp_path):
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        (tmp_path / "random.txt").write_text("x")
        (tmp_path / "openbrain-9-bad.partial.99").write_text("x")

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert all(p.name.endswith(".dump") for p in doomed)
