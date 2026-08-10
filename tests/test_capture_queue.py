"""Tests for the capture queue's safety properties."""
import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def q(tmp_path):
    from src.capture.queue import CaptureQueue
    root = tmp_path / "queue"
    root.mkdir()
    return CaptureQueue(root)


@pytest.fixture
def transcript(tmp_path):
    roots = tmp_path / "projects"
    (roots / "proj").mkdir(parents=True)
    path = roots / "proj" / "33333333-3333-3333-3333-333333333333.jsonl"
    path.write_text("{}\n")
    return path, [str(roots)]


SID = "33333333-3333-3333-3333-333333333333"


class TestValidation:
    def test_non_uuid_session_rejected_before_becoming_a_filename(self, q, transcript):
        from src.capture.queue import QueueError

        path, roots = transcript
        with pytest.raises(QueueError, match="not a UUID"):
            q.enqueue("../../etc/passwd", str(path), "/tmp", roots)

    def test_path_outside_roots_rejected(self, q, transcript, tmp_path):
        from src.capture.queue import QueueError

        outside = tmp_path / "elsewhere.jsonl"
        outside.write_text("{}")
        _, roots = transcript
        with pytest.raises(QueueError, match="outside configured roots"):
            q.enqueue(SID, str(outside), "/tmp", roots)

    def test_symlink_rejected(self, q, transcript, tmp_path):
        from src.capture.queue import QueueError

        path, roots = transcript
        link = path.parent / "link.jsonl"
        link.symlink_to(path)
        with pytest.raises(QueueError, match="symlink"):
            q.enqueue(SID, str(link), "/tmp", roots)


class TestAtomicity:
    def test_no_temp_file_survives(self, q, transcript):
        path, roots = transcript
        q.enqueue(SID, str(path), "/tmp", roots)
        assert not list(q.root.glob("*.tmp.*"))

    def test_permissions_are_private(self, q, transcript):
        path, roots = transcript
        entry = q.enqueue(SID, str(path), "/tmp", roots)
        assert oct(entry.stat().st_mode)[-3:] == "600"
        assert oct(q.root.stat().st_mode)[-3:] == "700"

    def test_repeat_events_collapse(self, q, transcript):
        """Stop fires per turn; repeats must not accumulate entries."""
        path, roots = transcript
        q.enqueue(SID, str(path), "/tmp", roots)
        q.enqueue(SID, str(path), "/tmp", roots)
        assert len(q.pending()) == 1


class TestDrainLock:
    def test_second_drain_refuses(self, q):
        from src.capture.queue import QueueError

        with q.drain_lock():
            with pytest.raises(QueueError, match="another drain"):
                with q.drain_lock():
                    pass


class TestClaiming:
    def _enqueue(self, q, transcript):
        path, roots = transcript
        return q.enqueue(SID, str(path), "/tmp", roots)

    def test_claim_freezes_the_boundary(self, q, transcript):
        """A transcript growing during a retry must not silently become a
        different boundary carrying the old retry count."""
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {"byte_offset": 100}, generation=1,
                        work_identity={"prompt_version": "1"})

        payload = json.loads(claim.read_text())
        assert payload["boundary"]["byte_offset"] == 100
        assert payload["generation"] == 1
        assert payload["work_identity"]["prompt_version"] == "1"

    def test_claimed_entry_leaves_the_queue(self, q, transcript):
        entry = self._enqueue(q, transcript)
        q.claim(entry, {}, 1, {})
        assert q.pending() == []

    def test_a_hook_event_arriving_mid_drain_survives(self, q, transcript):
        """Only the claimed file may be deleted."""
        path, roots = transcript
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {}, 1, {})

        q.enqueue(SID, str(path), "/tmp", roots)   # new turn while draining
        q.complete(claim)

        assert len(q.pending()) == 1, "the newer event must not be deleted"

    def test_release_does_not_clobber_a_newer_entry(self, q, transcript):
        path, roots = transcript
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {"byte_offset": 1}, 1, {})
        q.enqueue(SID, str(path), "/tmp", roots)

        q.release(claim, retry=True)

        payload = json.loads(q.pending()[0].read_text())
        assert "boundary" not in payload, "the newer event must win"

    def test_retry_count_increments_on_release(self, q, transcript):
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {}, 1, {})
        q.release(claim, retry=True)

        assert json.loads(q.pending()[0].read_text())["retries"] == 1

    def test_dead_letter_leaves_the_queue(self, q, transcript):
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {}, 1, {})
        q.release(claim, dead_letter=True)

        assert q.pending() == []
        assert list(q.root.glob("*.deadletter"))

    def test_stale_claims_are_recovered(self, q, transcript):
        entry = self._enqueue(q, transcript)
        claim = q.claim(entry, {}, 1, {})
        import os
        os.utime(claim, (time.time() - 7200, time.time() - 7200))

        assert q.recover_stale_claims(older_than=3600) == 1
        assert len(q.pending()) == 1


class TestDeferral:
    def test_not_before_gates_eligibility(self, q, transcript):
        entry = self._entry = self._enqueue = None
        path, roots = transcript
        entry = q.enqueue(SID, str(path), "/tmp", roots)
        claim = q.claim(entry, {}, 1, {})
        q.release(claim, not_before=time.time() + 3600)

        deferred = q.pending()[0]
        assert q.eligible(deferred) is False

    def test_past_not_before_is_eligible(self, q, transcript):
        path, roots = transcript
        entry = q.enqueue(SID, str(path), "/tmp", roots)
        claim = q.claim(entry, {}, 1, {})
        q.release(claim, not_before=time.time() - 1)

        assert q.eligible(q.pending()[0]) is True
