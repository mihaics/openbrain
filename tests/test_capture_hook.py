"""Tests for the Stop hook. It runs on every turn, so its contract is narrow."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOK = str(Path(__file__).resolve().parents[1] / "bin" / "openbrain-capture-hook")
SID = "44444444-4444-4444-4444-444444444444"


def run_hook(payload, home, timeout=10):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        [HOOK], input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=timeout,
    )


@pytest.fixture
def home(tmp_path):
    state = tmp_path / ".local" / "state" / "openbrain"
    state.mkdir(parents=True)
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    transcript = projects / f"{SID}.jsonl"
    transcript.write_text("{}\n")
    (state / "capture-hook.json").write_text(json.dumps({
        "enabled": True,
        "projects": [str(tmp_path / "work")],
        "transcript_roots": [str(tmp_path / "projects")],
        "queue_root": str(state / "capture-queue"),
    }))
    (tmp_path / "work").mkdir()
    return tmp_path


def _payload(home, **over):
    base = {
        "session_id": SID,
        "transcript_path": str(home / "projects" / "proj" / f"{SID}.jsonl"),
        "cwd": str(home / "work"),
        "stop_hook_active": False,
    }
    base.update(over)
    return base


def _queue(home):
    root = home / ".local" / "state" / "openbrain" / "capture-queue"
    return sorted(root.glob("*.json")) if root.exists() else []


class TestAlwaysSilentAndZero:
    @pytest.mark.parametrize("payload", [
        "", "not json", "{", "[]", "null", json.dumps({"session_id": "nope"}),
    ])
    def test_malformed_input_exits_zero_and_says_nothing(self, payload, home):
        """A non-zero Stop hook surfaces as an error in the user's session;
        exit code 2 feeds stderr back to the model."""
        result = run_hook(payload, home)

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_oversize_stdin_exits_zero(self, home):
        result = run_hook("x" * (2 * 1024 * 1024), home)
        assert result.returncode == 0
        assert result.stderr == ""

    def test_missing_config_exits_zero_and_enqueues_nothing(self, home):
        (home / ".local" / "state" / "openbrain" / "capture-hook.json").unlink()
        result = run_hook(_payload(home), home)

        assert result.returncode == 0
        assert result.stderr == ""
        assert _queue(home) == []

    def test_unwritable_queue_exits_zero(self, home):
        state = home / ".local" / "state" / "openbrain"
        queue = state / "capture-queue"
        queue.mkdir()
        queue.chmod(0o500)
        try:
            result = run_hook(_payload(home), home)
            assert result.returncode == 0
            assert result.stderr == ""
        finally:
            queue.chmod(0o700)


class TestGating:
    def test_enqueues_an_allowlisted_project(self, home):
        run_hook(_payload(home), home)
        entries = _queue(home)

        assert len(entries) == 1
        assert json.loads(entries[0].read_text())["session_id"] == SID

    def test_project_not_allowlisted_is_skipped(self, home, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        run_hook(_payload(home, cwd=str(other)), home)

        assert _queue(home) == []

    def test_disabled_config_captures_nothing(self, home):
        path = home / ".local" / "state" / "openbrain" / "capture-hook.json"
        data = json.loads(path.read_text())
        data["enabled"] = False
        path.write_text(json.dumps(data))

        run_hook(_payload(home), home)
        assert _queue(home) == []

    def test_stop_hook_active_is_respected(self, home):
        run_hook(_payload(home, stop_hook_active=True), home)
        assert _queue(home) == []

    def test_transcript_outside_roots_is_skipped(self, home, tmp_path):
        rogue = tmp_path / "rogue.jsonl"
        rogue.write_text("{}")
        run_hook(_payload(home, transcript_path=str(rogue)), home)

        assert _queue(home) == []

    def test_repeat_turns_collapse_to_one_entry(self, home):
        for _ in range(3):
            run_hook(_payload(home), home)
        assert len(_queue(home)) == 1


class TestSpeed:
    def test_stays_well_under_a_budget(self, home):
        """It runs on every assistant turn, so slowness is a real cost."""
        durations = []
        for _ in range(20):
            start = time.monotonic()
            run_hook(_payload(home), home)
            durations.append(time.monotonic() - start)

        durations.sort()
        median, worst = durations[len(durations) // 2], durations[-1]
        print(f"\nhook timing: median {median*1000:.0f}ms, max {worst*1000:.0f}ms")
        assert worst < 2.0, f"hook too slow: {worst:.2f}s"
