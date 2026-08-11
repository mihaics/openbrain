"""Tests for the interlock, init-state, and hook installation."""
import json
from unittest.mock import patch

import pytest


@pytest.fixture
def progress(tmp_path):
    from src.capture.progress import Progress
    root = tmp_path / "state"
    root.mkdir()
    return Progress(root)


class TestGenerationInterlock:
    def test_equal_generations_are_normal(self, progress):
        from src.capture.progress import Interlock

        progress.write_checkpoint(5)
        assert progress.classify(5) is Interlock.OK

    def test_database_ahead_rebases(self, progress):
        """Ordinary multi-session operation: another session committed.

        A per-session comparison would permanently block every session after
        the first one to commit.
        """
        from src.capture.progress import Interlock

        progress.write_checkpoint(5)
        assert progress.classify(7) is Interlock.REBASE

    def test_filesystem_ahead_refuses(self, progress):
        """The only way this happens is a database restore."""
        from src.capture.progress import Interlock

        progress.write_checkpoint(9)
        assert progress.classify(4) is Interlock.REFUSE

    def test_missing_checkpoint_refuses_rather_than_initializing(self, progress):
        """Restoring a pre-capture database and re-running migrate looks
        exactly like a clean install while stale progress sits on disk."""
        from src.capture.progress import Interlock

        assert progress.classify(1) is Interlock.UNINITIALIZED

    def test_corrupt_checkpoint_refuses(self, progress):
        from src.capture.progress import Interlock

        progress.checkpoint_path.write_text("{ not json")
        assert progress.classify(1) is Interlock.UNINITIALIZED

    def test_rebase_leaves_boundaries_untouched(self, progress):
        from src.capture.worker import check_interlock

        progress.write_checkpoint(1)
        progress.write_boundary("s1", {"session_id": "s1", "byte_offset": 500})

        with patch('src.capture.worker._db_generation', return_value=4):
            check_interlock(progress)

        assert progress.read_checkpoint() == 4
        assert progress.offset("s1") == 500, "a rebase must not move a boundary"

    def test_refuses_while_a_reconcile_intent_exists(self, progress):
        from src.capture.worker import WorkerRefusal, check_interlock

        progress.write_checkpoint(1)
        progress.write_intent({"target_generation": 1})

        with patch('src.capture.worker._db_generation', return_value=1):
            with pytest.raises(WorkerRefusal, match="reconcile"):
                check_interlock(progress)

    def test_restore_refusal_names_the_command(self, progress):
        from src.capture.worker import WorkerRefusal, check_interlock

        progress.write_checkpoint(9)
        with patch('src.capture.worker._db_generation', return_value=2):
            with pytest.raises(WorkerRefusal, match="reconcile"):
                check_interlock(progress)


class TestInitState:
    def _db(self, generation=1, rows=0, **over):
        state = {"generation": generation, "previous_generation": None,
                 "committed_work_identity": None, "reprocess_counter": 0,
                 "active_reprocess": None}
        state.update(over)

        from unittest.mock import Mock
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.fetchone.side_effect = [state, {"n": rows}]
        return ctx

    def test_initializes_a_clean_install(self, progress):
        from src.capture.worker import init_state

        with patch('src.db.connection.get_db_cursor', return_value=self._db()):
            init_state(progress)

        assert progress.read_checkpoint() == 1

    def test_refuses_when_rows_already_exist(self, progress):
        from src.capture.worker import WorkerRefusal, init_state

        with patch('src.db.connection.get_db_cursor', return_value=self._db(rows=3)):
            with pytest.raises(WorkerRefusal, match="3 claude-code-auto rows"):
                init_state(progress)

    def test_refuses_on_leftover_progress(self, progress):
        from src.capture.worker import WorkerRefusal, init_state

        progress.write_boundary("s1", {"byte_offset": 10})
        with patch('src.db.connection.get_db_cursor', return_value=self._db()):
            with pytest.raises(WorkerRefusal, match="progress exists"):
                init_state(progress)

    def test_refuses_on_a_non_initial_generation(self, progress):
        from src.capture.worker import WorkerRefusal, init_state

        with patch('src.db.connection.get_db_cursor',
                   return_value=self._db(generation=4)):
            with pytest.raises(WorkerRefusal, match="generation is 4"):
                init_state(progress)

    def test_refusal_points_at_reconcile(self, progress):
        from src.capture.worker import WorkerRefusal, init_state

        with patch('src.db.connection.get_db_cursor', return_value=self._db(rows=1)):
            with pytest.raises(WorkerRefusal, match="--reconcile"):
                init_state(progress)


class TestHookInstall:
    def test_preserves_unknown_keys_and_existing_hooks(self, tmp_path):
        from src.cli.hookinstall import install

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({
            "model": "opus",
            "hooks": {"Stop": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "/usr/bin/other-hook"}]}]},
        }))

        install(path)
        data = json.loads(path.read_text())

        assert data["model"] == "opus"
        commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert "/usr/bin/other-hook" in commands
        assert any("openbrain-capture-hook" in c for c in commands)

    def test_installed_entry_is_async_with_a_timeout(self, tmp_path):
        """Claude waits for synchronous command hooks."""
        from src.cli.hookinstall import install

        path = tmp_path / "settings.json"
        install(path)
        hook = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]

        assert hook["async"] is True
        assert hook["timeout"] <= 10
        assert hook["command"].startswith("/")
        assert hook["type"] == "command"

    def test_installing_twice_adds_one_entry(self, tmp_path):
        from src.cli.hookinstall import install

        path = tmp_path / "settings.json"
        install(path)
        assert install(path) == "already installed"

        commands = [h["command"] for g in json.loads(path.read_text())["hooks"]["Stop"]
                    for h in g["hooks"]]
        assert sum("openbrain" in c for c in commands) == 1

    def test_uninstall_removes_only_our_entry(self, tmp_path):
        from src.cli.hookinstall import install, uninstall

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"hooks": {"Stop": [
            {"matcher": "*", "hooks": [{"type": "command",
                                        "command": "/usr/bin/other-hook"}]}]}}))
        install(path)
        uninstall(path)

        data = json.loads(path.read_text())
        commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert commands == ["/usr/bin/other-hook"]

    def test_result_is_always_valid_json(self, tmp_path):
        from src.cli.hookinstall import install

        path = tmp_path / "settings.json"
        install(path)
        json.loads(path.read_text())
        assert not list(tmp_path.glob("*.tmp.*"))

    def test_hook_executable_exists_and_is_executable(self):
        import os
        from src.cli.hookinstall import HOOK_COMMAND

        assert os.path.isfile(HOOK_COMMAND)
        assert os.access(HOOK_COMMAND, os.X_OK)


_ENABLED = {"enabled": True, "model_digest": "deadbeefcafe0000"}


class TestDryRunIsNonMutating:
    """`--dry-run` is documented as "Report candidates, write nothing"."""

    def _queue_entry(self, root, transcript):
        sid = "55555555-5555-5555-5555-555555555555"
        root.mkdir(parents=True, exist_ok=True)
        entry = root / f"{sid}.json"
        entry.write_text(json.dumps({
            "session_id": sid,
            "transcript_path": str(transcript),
            "cwd": "/work/repo",
            "enqueued_at": 0,
        }))
        return entry

    def test_missing_transcript_leaves_the_entry_alone(self, tmp_path):
        """A transcript absent for one pass - a deleted or not-yet-flushed
        file - must not cost the queue entry in a mode that writes nothing."""
        from src.capture.worker import drain
        from src.config.capture import CaptureConfig

        queue_root = tmp_path / "queue"
        state_root = tmp_path / "state"
        state_root.mkdir()
        entry = self._queue_entry(queue_root, tmp_path / "gone.jsonl")

        with patch('src.capture.extract.verify_model', return_value=(True, "")):
            drain(CaptureConfig(config_data=_ENABLED), dry_run=True,
                  queue_root=queue_root, state_root=state_root)

        assert entry.exists(), "dry run deleted a queued entry"
        assert not list(queue_root.glob("*.deadletter"))

    def test_missing_transcript_is_dead_lettered_in_a_real_drain(self, tmp_path):
        """The real drain still retires it; only dry run is untouched."""
        from src.capture.worker import drain
        from src.config.capture import CaptureConfig

        queue_root = tmp_path / "queue"
        state_root = tmp_path / "state"
        state_root.mkdir()
        entry = self._queue_entry(queue_root, tmp_path / "gone.jsonl")

        with patch('src.capture.extract.verify_model', return_value=(True, "")), \
             patch('src.capture.worker.check_interlock', return_value=1), \
             patch('src.capture.store.daily_count', return_value=0):
            drain(CaptureConfig(config_data=_ENABLED),
                  queue_root=queue_root, state_root=state_root)

        assert not entry.exists()
        assert len(list(queue_root.glob("*.deadletter"))) == 1
