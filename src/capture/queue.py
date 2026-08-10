"""The capture queue: crash-safe, concurrency-safe, and claimable.

Six agent configurations and manual drains all write here, so every rule
below exists because something concurrent breaks without it.
"""
import fcntl
import json
import os
import re
import time
import uuid as uuid_mod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
STALE_CLAIM_SECONDS = 3600


class QueueError(RuntimeError):
    """A queue operation refused rather than doing something unsafe."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """temp -> fsync -> replace -> fsync dir.

    A same-name overwrite would expose a truncated file to a concurrent
    reader, which is exactly what a drain running beside a Stop hook does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{uuid_mod.uuid4().hex[:8]}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class CaptureQueue:
    """One JSON file per session, claimed by rename."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- paths -----------------------------------------------------------
    def _entry(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".drain.lock"

    # ---- validation ------------------------------------------------------
    def _validate(self, session_id: str, transcript_path: str,
                  transcript_roots: List[str]) -> Path:
        if not UUID_RE.match(session_id or ""):
            raise QueueError(f"session_id is not a UUID: {session_id!r}")

        path = Path(transcript_path)
        if path.is_symlink():
            raise QueueError("transcript path is a symlink")
        resolved = path.resolve()
        if not any(str(resolved).startswith(str(Path(r).resolve()) + os.sep)
                   for r in transcript_roots):
            raise QueueError(f"transcript path outside configured roots: {resolved}")
        return resolved

    # ---- writing ---------------------------------------------------------
    def enqueue(self, session_id: str, transcript_path: str, cwd: str,
                transcript_roots: List[str]) -> Path:
        """Record that a session has new turns. Collapses repeats by name."""
        resolved = self._validate(session_id, transcript_path, transcript_roots)
        entry = self._entry(session_id)
        atomic_write_json(entry, {
            "session_id": session_id,
            "transcript_path": str(resolved),
            "cwd": str(Path(cwd).resolve()) if cwd else "",
            "enqueued_at": time.time(),
        })
        return entry

    # ---- draining --------------------------------------------------------
    @contextmanager
    def drain_lock(self):
        """Non-blocking: a second drain exits quietly rather than interleaving."""
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QueueError("another drain is running")
            yield
        finally:
            os.close(fd)

    def pending(self) -> List[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.glob("*.json")
                      if not p.name.startswith("."))

    def claim(self, entry: Path, boundary: Dict[str, Any],
              generation: int, work_identity: Dict[str, Any]) -> Optional[Path]:
        """Claim by atomic rename, freezing the boundary into the claim.

        The claim records the exact boundary it targets. Without that, a
        transcript that grows during a retry silently turns the claim into a
        *different* boundary carrying the old retry count.
        """
        claim_path = self.root / f"{entry.stem}.{uuid_mod.uuid4().hex[:8]}.processing"
        try:
            payload = json.loads(entry.read_text())
        except (OSError, ValueError):
            return None
        try:
            os.rename(entry, claim_path)
        except OSError:
            return None  # another drain got it, or the hook rewrote it

        payload.update({
            "boundary": boundary,
            "generation": generation,
            "work_identity": work_identity,
            "claimed_at": time.time(),
            "retries": payload.get("retries", 0),
            "not_before": payload.get("not_before"),
        })
        atomic_write_json(claim_path, payload)
        return claim_path

    def release(self, claim_path: Path, *, retry: bool = False,
                not_before: Optional[float] = None,
                dead_letter: bool = False) -> None:
        """Return work to the queue, defer it, or dead-letter it."""
        try:
            payload = json.loads(claim_path.read_text())
        except (OSError, ValueError):
            payload = {"session_id": claim_path.name.split(".")[0]}

        if dead_letter:
            atomic_write_json(self.root / f"{payload['session_id']}.deadletter",
                              payload)
            claim_path.unlink(missing_ok=True)
            return

        if retry:
            payload["retries"] = payload.get("retries", 0) + 1
        if not_before is not None:
            payload["not_before"] = not_before

        target = self._entry(payload["session_id"])
        if target.exists():
            # A newer hook event arrived mid-drain: it wins, and this claim is
            # simply dropped. Overwriting it would lose the newer turns.
            claim_path.unlink(missing_ok=True)
            return
        atomic_write_json(target, payload)
        claim_path.unlink(missing_ok=True)

    def complete(self, claim_path: Path) -> None:
        """Delete only the claimed file, never a newly recreated entry."""
        claim_path.unlink(missing_ok=True)
        _fsync_dir(self.root)

    def recover_stale_claims(self, older_than: float = STALE_CLAIM_SECONDS) -> int:
        """A crashed drain leaves claims behind; return them to the queue."""
        recovered = 0
        for claim in self.root.glob("*.processing"):
            try:
                age = time.time() - claim.stat().st_mtime
            except OSError:
                continue
            if age < older_than:
                continue
            self.release(claim)
            recovered += 1
        return recovered

    def eligible(self, entry: Path) -> bool:
        """Deferred work becomes eligible at not_before, not by age."""
        try:
            payload = json.loads(entry.read_text())
        except (OSError, ValueError):
            return False
        not_before = payload.get("not_before")
        return not_before is None or time.time() >= not_before
