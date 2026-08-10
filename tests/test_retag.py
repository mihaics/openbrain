"""Tests for the retag backfill and its undo."""
import json
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


class TestProtectedTags:
    def test_user_tags_are_never_removed(self):
        from src.cli import retag

        row = {"id": uuid.uuid4(), "content": "a note about nothing",
               "source": "cli", "tags": ["mytag", "auto"],
               "tag_sources": {"mytag": "user", "auto": "default"}}

        tags, sources = retag.recompute_tags(row)

        assert "mytag" in tags and sources["mytag"] == "user"
        assert "auto" not in tags

    def test_unknown_provenance_fails_closed(self):
        """Rows predating provenance tracking must be protected."""
        from src.cli import retag

        row = {"id": uuid.uuid4(), "content": "a note", "source": "cli",
               "tags": ["legacy"], "tag_sources": {}}

        tags, sources = retag.recompute_tags(row)

        assert "legacy" in tags
        assert sources["legacy"] == "user"

    def test_automatic_false_positive_is_removed(self):
        from src.cli import retag

        row = {"id": uuid.uuid4(), "content": "let's go through the backlog",
               "source": "cli", "tags": ["go"], "tag_sources": {"go": "entity"}}

        tags, _ = retag.recompute_tags(row)
        assert "go" not in tags


class TestBackupGate:
    def _report(self, **over):
        base = {"newest_verified_age_seconds": 60,
                "newest_database_identity": {"host": "h", "port": 1, "name": "n"}}
        base.update(over)
        return base

    def _check(self, report, identity=None):
        from src.cli import retag

        identity = identity or {"host": "h", "port": 1, "name": "n"}
        with patch('src.cli.retag.status_report', return_value=report), \
             patch('src.cli.retag.database_identity', return_value=identity), \
             patch('src.cli.retag.DatabaseConfig'):
            retag.check_backup_gate(24)

    def test_no_backup_refuses(self):
        from src.cli.retag import RetagError

        with pytest.raises(RetagError, match="no verified backup"):
            self._check(self._report(newest_verified_age_seconds=None))

    def test_stale_backup_refuses(self):
        from src.cli.retag import RetagError

        with pytest.raises(RetagError, match="older than"):
            self._check(self._report(newest_verified_age_seconds=48 * 3600))

    def test_future_dated_backup_refuses(self):
        """A negative age would sail past a naive upper-bound check."""
        from src.cli.retag import RetagError

        with pytest.raises(RetagError, match="future"):
            self._check(self._report(newest_verified_age_seconds=-100))

    def test_identity_mismatch_refuses(self):
        from src.cli.retag import RetagError

        with pytest.raises(RetagError, match="another database"):
            self._check(self._report(
                newest_database_identity={"host": "h", "port": 1, "name": "other"}))

    def test_fresh_matching_backup_passes(self):
        self._check(self._report())


class TestUndoFileFormat:
    def _write(self, tmp_path, lines):
        path = tmp_path / "undo.jsonl"
        path.write_text("\n".join(lines) + "\n")
        return path

    def _header(self):
        from src.cli import retag
        return json.dumps({"type": "header", "schema_version": retag.SCHEMA_VERSION,
                           "database": {"name": "n"}})

    def _plan(self, mid):
        return json.dumps({"type": "plan", "id": mid, "before_tags": [],
                           "before_tag_sources": {}, "after_tags": [],
                           "after_tag_sources": {}, "content_hash": "x",
                           "source": "cli"})

    def test_roundtrip_with_markers(self, tmp_path):
        from src.cli import retag

        mid = str(uuid.uuid4())
        path = self._write(tmp_path, [
            self._header(), self._plan(mid),
            json.dumps({"type": "applied", "id": mid}),
        ])

        header, plans, applied = retag.read_undo_file(path)
        assert header["schema_version"] == retag.SCHEMA_VERSION
        assert len(plans) == 1 and applied == {mid}

    def test_truncated_final_record_is_discarded(self, tmp_path):
        """Appends are not crash-atomic. Rejecting the file would block undo
        of every earlier, correctly recorded mutation."""
        from src.cli import retag

        mid = str(uuid.uuid4())
        path = tmp_path / "undo.jsonl"
        path.write_text(self._header() + "\n" + self._plan(mid) + "\n" +
                        '{"type": "applied", "id": "trunc')

        header, plans, applied = retag.read_undo_file(path)
        assert len(plans) == 1
        assert applied == set()

    def test_malformed_mid_file_record_is_fatal(self, tmp_path):
        from src.cli import retag
        from src.cli.retag import RetagError

        mid = str(uuid.uuid4())
        path = self._write(tmp_path, [self._header(), "{ broken", self._plan(mid)])

        with pytest.raises(RetagError, match="corrupt"):
            retag.read_undo_file(path)

    def test_applied_without_plan_is_fatal(self, tmp_path):
        from src.cli import retag
        from src.cli.retag import RetagError

        path = self._write(tmp_path, [
            self._header(),
            json.dumps({"type": "applied", "id": str(uuid.uuid4())}),
        ])

        with pytest.raises(RetagError, match="without a plan"):
            retag.read_undo_file(path)

    def test_missing_header_is_fatal(self, tmp_path):
        from src.cli import retag
        from src.cli.retag import RetagError

        path = self._write(tmp_path, [self._plan(str(uuid.uuid4()))])

        with pytest.raises(RetagError, match="no header"):
            retag.read_undo_file(path)


class TestModeExclusivity:
    def test_undo_with_limit_is_a_usage_error(self):
        from src.cli.retag import retag_cmd

        args = Mock(undo="f.jsonl", limit=5, dry_run=False, force=False)
        assert retag_cmd(args) == 2


class TestPaths:
    def test_retag_paths_derive_from_the_backup_root(self, tmp_path, monkeypatch):
        """Otherwise a test-database run writes into the production namespace."""
        from src.cli import retag

        monkeypatch.setenv("OPENBRAIN_BACKUP_DIR", str(tmp_path))

        assert retag.lock_path().parent == tmp_path
        assert retag.sequence_path().parent == tmp_path

    def test_sequence_is_separate_from_the_backup_sequence(self, tmp_path, monkeypatch):
        from src.cli import backup_store, retag

        monkeypatch.setenv("OPENBRAIN_BACKUP_DIR", str(tmp_path))
        assert retag.sequence_path().name != backup_store.SEQUENCE_FILE


@pytest.mark.integration
class TestBackfillEndToEnd:
    """Exercises the forward CAS, the committed mutation, the applied-marker
    protocol and the undo CAS against real PostgreSQL.

    Without this the production run would be the first time any of that
    machinery ever touched a database.
    """

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch, integration_db):
        """Own backup root, own verified backup, own lock and sequence."""
        monkeypatch.setenv("OPENBRAIN_BACKUP_DIR", str(tmp_path))
        yield tmp_path
        with integration_db.get_db_cursor() as cursor:
            cursor.execute("DELETE FROM memory WHERE source = 'test'")

    def _seed(self, connection):
        from src.db import queries

        mid = queries.insert_memory(
            source="test", content="let's go through the backlog", embedding=None,
            tags=["go", "mytag"], tag_sources={"go": "entity", "mytag": "user"},
        )
        return mid

    def _fake_gate(self):
        return patch('src.cli.retag.check_backup_gate')

    def test_backfill_then_undo_roundtrip(self, isolated, integration_db):
        from src.cli import retag
        from src.db import queries

        mid = self._seed(integration_db)

        with self._fake_gate():
            assert retag.run_backfill(None, dry_run=False, max_age_hours=24) == 0

        row = queries.get_memory_by_id(mid)
        assert "go" not in row["tags"], "false positive should be removed"
        assert "mytag" in row["tags"], "user tag must survive"

        undo_files = list(isolated.glob("tag-backfill-*.jsonl"))
        assert len(undo_files) == 1
        header, plans, applied = retag.read_undo_file(undo_files[0])
        assert str(mid) in applied, "committed row must carry an applied marker"

        assert retag.run_undo(undo_files[0], force=False, dry_run=False) == 0
        restored = queries.get_memory_by_id(mid)
        assert sorted(restored["tags"]) == sorted(["go", "mytag"])

    def test_undo_refuses_after_a_later_content_edit(self, isolated, integration_db):
        from src.cli import retag
        from src.db import queries

        mid = self._seed(integration_db)
        with self._fake_gate():
            retag.run_backfill(None, dry_run=False, max_age_hours=24)

        with integration_db.get_db_cursor() as cursor:
            cursor.execute("UPDATE memory SET content = %s WHERE id = %s",
                           ("edited afterwards", str(mid)))

        undo_file = next(isolated.glob("tag-backfill-*.jsonl"))
        assert retag.run_undo(undo_file, force=False, dry_run=False) == 1, (
            "a content edit after the backfill must make undo conflict"
        )

    def test_writes_stay_under_the_override(self, isolated):
        from src.cli import retag

        assert retag.lock_path().parent == isolated
        assert retag.sequence_path().parent == isolated
