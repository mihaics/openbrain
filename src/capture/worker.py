"""The drain worker: crash ordering, the interlock, the ceiling, retries."""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.capture import STATE_ROOT, CaptureConfig
from . import extract as extract_mod
from . import store as store_mod
from .identity import project_for
from .progress import Interlock, Progress
from .queue import CaptureQueue, QueueError
from .transcript import read_exchanges, windows

QUEUE_ROOT = STATE_ROOT / "capture-queue"
MAX_CONTENT_RETRIES = 1


class WorkerRefusal(RuntimeError):
    """The worker declined to run. Never a partial pass."""


@dataclass
class DrainReport:
    considered: int = 0
    processed: int = 0
    stored: int = 0
    duplicates: int = 0
    deferred: int = 0
    retried: int = 0
    dead_lettered: int = 0
    skipped_committed: int = 0
    refusal: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "candidates"}
        return out


def _db_generation() -> int:
    from ..db.connection import get_db_cursor
    with get_db_cursor() as cursor:
        cursor.execute("SELECT generation FROM capture_state WHERE id = 1")
        row = cursor.fetchone()
    return int(row["generation"] if isinstance(row, dict) else row[0])


def _work_identity(boundary, cfg, reprocess_generation=0) -> Dict[str, Any]:
    return {
        "boundary_id": boundary.boundary_id,
        "prompt_version": cfg.prompt_version,
        "model_digest": cfg.model_digest,
        "reprocess_generation": reprocess_generation,
    }


def _project_identity(cfg: CaptureConfig, cwd: str) -> str:
    """The repository a session belongs to, for `metadata.project`.

    Under subtree matching the cwd may be any directory beneath an allowlist
    entry, so the cwd alone would fragment one repository across several
    project values.
    """
    root = cfg.matched_root(cwd)
    if root is None:
        return cwd or ""
    return project_for(cwd, root)


def check_interlock(progress: Progress) -> int:
    """Return the database generation, or refuse.

    A filesystem checkpoint ahead of the database means the database was
    restored underneath the capture ledger; draining on would silently skip
    everything the restore lost.
    """
    if progress.read_intent():
        raise WorkerRefusal(
            "a reconcile intent exists - run `openbrain capture --reconcile`"
        )

    db_generation = _db_generation()
    state = progress.classify(db_generation)

    if state is Interlock.UNINITIALIZED:
        raise WorkerRefusal(
            "no generation checkpoint - run `openbrain capture --init-state`"
        )
    if state is Interlock.REFUSE:
        raise WorkerRefusal(
            f"generation checkpoint ({progress.read_checkpoint()}) is ahead of the "
            f"database ({db_generation}); a restore happened - run "
            f"`openbrain capture --reconcile`"
        )
    if state is Interlock.REBASE:
        # Another session committed. Rebase the shared checkpoint forward
        # without touching any boundary.
        progress.write_checkpoint(db_generation)
    return db_generation


def drain(cfg: CaptureConfig, *, dry_run: bool = False,
          queue_root: Path = None, state_root: Path = None) -> DrainReport:
    """Process queued sessions. One boundary per session per pass."""
    queue = CaptureQueue(queue_root or QUEUE_ROOT)
    progress = Progress(Path(state_root or STATE_ROOT))
    report = DrainReport()

    try:
        with queue.drain_lock():
            return _drain_locked(cfg, queue, progress, report, dry_run)
    except QueueError as e:
        report.refusal = str(e)
        return report
    except WorkerRefusal as e:
        report.refusal = str(e)
        return report


def _drain_locked(cfg, queue, progress, report, dry_run) -> DrainReport:
    if not dry_run:
        generation = check_interlock(progress)
        queue.recover_stale_claims()
    else:
        generation = 0

    ok, detail = extract_mod.verify_model(cfg)
    if not ok:
        # One health check per drain, not per session, and an outage stops the
        # pass rather than burning a content retry on every queued item.
        report.refusal = f"model unavailable: {detail}"
        return report

    ceiling_used = 0 if dry_run else store_mod.daily_count()

    for entry in queue.pending()[: cfg.max_sessions_per_drain]:
        report.considered += 1
        if not queue.eligible(entry):
            report.deferred += 1
            continue
        if ceiling_used >= cfg.daily_ceiling:
            report.deferred += 1
            continue

        try:
            payload = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue

        transcript = Path(payload["transcript_path"])
        if not transcript.exists():
            # Only a real drain retires it. Releasing in a dry run would hand
            # release() the unclaimed entry as its own target, which takes the
            # "newer event wins" branch and unlinks the very entry a mode
            # documented to write nothing is only supposed to report on.
            if not dry_run:
                queue.release(entry, dead_letter=True)
            continue

        session_id = payload["session_id"]
        start = progress.offset(session_id)
        exchanges, boundary = read_exchanges(transcript, start)
        if not exchanges:
            if not dry_run:
                progress.write_boundary(session_id, boundary.as_dict())
                entry.unlink(missing_ok=True)
            continue

        identity = _work_identity(boundary, cfg)

        # Before invoking the model: if this boundary already committed, a
        # crash happened between commit and progress. Advance and move on.
        if not dry_run and store_mod.boundary_already_committed(boundary.boundary_id):
            progress.write_boundary(session_id, boundary.as_dict())
            entry.unlink(missing_ok=True)
            report.skipped_committed += 1
            continue

        claim = None if dry_run else queue.claim(
            entry, boundary.as_dict(), generation, identity)
        if not dry_run and claim is None:
            continue

        # Aggregate every window into ONE boundary: per-window transactions
        # would multiply the cap and advance progress after only some windows.
        facts: List[Dict[str, str]] = []
        outcome = extract_mod.Outcome.SUCCESS
        for window in windows(exchanges, cfg.max_input_tokens * 4):
            result = extract_mod.extract(window, cfg)
            if result.outcome is not extract_mod.Outcome.SUCCESS:
                outcome = result.outcome
                break
            facts.extend(result.facts)
        facts = facts[: cfg.max_facts_per_boundary]

        if outcome is extract_mod.Outcome.INFRASTRUCTURE_FAILURE:
            if claim:
                queue.release(claim, not_before=time.time() + 900)
            report.deferred += 1
            break  # an outage stops the pass; it is not this session's fault
        if outcome is extract_mod.Outcome.CONTENT_FAILURE:
            if claim:
                retries = json.loads(claim.read_text()).get("retries", 0)
                if retries >= MAX_CONTENT_RETRIES:
                    queue.release(claim, dead_letter=True)
                    report.dead_lettered += 1
                else:
                    queue.release(claim, retry=True)
                    report.retried += 1
            continue

        if dry_run:
            report.candidates.extend(
                {"session_id": session_id, **f} for f in facts)
            report.processed += 1
            continue

        # Capacity is rechecked against the real post-dedup batch below; here
        # we only avoid starting work that obviously cannot fit.
        if ceiling_used + len(facts) > cfg.daily_ceiling:
            queue.release(claim, not_before=_next_utc_midnight())
            report.deferred += 1
            continue

        status, store_report = store_mod.store_boundary(
            facts, identity,
            {"session_id": session_id, "boundary_id": boundary.boundary_id,
             "project": _project_identity(cfg, payload.get("cwd", ""))},
            cfg, generation,
        )

        if status in (store_mod.StoreOutcome.EMBEDDING_FAILURE,
                      store_mod.StoreOutcome.DB_FAILURE):
            # Infrastructure, not content: back off without consuming a retry.
            queue.release(claim, not_before=time.time() + 900)
            report.deferred += 1
            break

        # DB commit -> checkpoint -> boundary progress -> delete claim.
        if status == store_mod.StoreOutcome.STORED:
            generation += 1
            progress.write_checkpoint(generation)
        progress.write_boundary(session_id, boundary.as_dict())
        queue.complete(claim)

        report.processed += 1
        report.stored += store_report.get("stored", 0)
        report.duplicates += store_report.get("duplicates", 0)
        ceiling_used += store_report.get("stored", 0)

    return report


def _next_utc_midnight() -> float:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def init_state(progress: Progress, *, force: bool = False) -> None:
    """Initialize the checkpoint, refusing unless both sides are clean."""
    reasons = progress.dirty_reasons()

    from ..db.connection import get_db_cursor
    with get_db_cursor() as cursor:
        cursor.execute("SELECT generation, previous_generation, "
                       "committed_work_identity, reprocess_counter, "
                       "active_reprocess FROM capture_state WHERE id = 1")
        state = cursor.fetchone()
        cursor.execute("SELECT count(*) AS n FROM memory WHERE source = %s",
                       (store_mod.AUTO_SOURCE,))
        rows = cursor.fetchone()

    if state["generation"] != 1:
        reasons.append(f"capture_state.generation is {state['generation']}, not 1")
    if state["previous_generation"] is not None:
        reasons.append("capture_state.previous_generation is set")
    if state["committed_work_identity"] is not None:
        reasons.append("capture_state.committed_work_identity is set")
    if state["reprocess_counter"] != 0:
        reasons.append("capture_state.reprocess_counter is not 0")
    if state["active_reprocess"] is not None:
        reasons.append("capture_state.active_reprocess is set")
    n = rows["n"] if isinstance(rows, dict) else rows[0]
    if n:
        reasons.append(f"{n} claude-code-auto rows already exist")

    if reasons and not force:
        raise WorkerRefusal(
            "refusing to initialize; state is not clean: "
            + "; ".join(reasons)
            + ". Run `openbrain capture --reconcile` instead."
        )

    progress.root.mkdir(parents=True, exist_ok=True)
    progress.write_checkpoint(state["generation"])
