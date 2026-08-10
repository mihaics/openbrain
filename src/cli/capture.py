"""openbrain capture - the auto-capture surface."""
import json
import sys
import time
from argparse import Namespace
from pathlib import Path

from ..capture import store as store_mod
from ..capture.progress import Progress
from ..capture.queue import CaptureQueue
from ..capture.transcript import read_exchanges
from ..capture.worker import QUEUE_ROOT, WorkerRefusal, drain, init_state
from ..config.capture import STATE_ROOT, CaptureConfig, write_hook_config

HOOK_NAME = "openbrain-capture"


def _progress() -> Progress:
    return Progress(STATE_ROOT)


def _status(cfg) -> dict:
    queue = CaptureQueue(QUEUE_ROOT)
    progress = _progress()
    root = QUEUE_ROOT
    pending = queue.pending() if root.exists() else []
    return {
        "enabled": cfg.enabled,
        "projects": cfg.projects,
        "model": cfg.model,
        "prompt_version": cfg.prompt_version,
        "queued": len(pending),
        "claimed": len(list(root.glob("*.processing"))) if root.exists() else 0,
        "dead_lettered": len(list(root.glob("*.deadletter"))) if root.exists() else 0,
        "deferred": sum(1 for p in pending if not queue.eligible(p)),
        "generation_checkpoint": progress.read_checkpoint(),
        "reconcile_intent": progress.read_intent() is not None,
        "captured_today": store_mod.daily_count() if cfg.enabled else None,
        "daily_ceiling": cfg.daily_ceiling,
    }


def _recent_transcripts(cfg, limit: int):
    """Transcripts from allowlisted projects, newest first.

    Needed because before the hook is installed the queue is empty, so a
    dry run would otherwise have nothing to look at.
    """
    found = []
    for root in cfg.transcript_roots:
        found.extend(Path(root).glob("*/*.jsonl"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def capture_cmd(args: Namespace) -> int:
    cfg = CaptureConfig.get_instance()

    try:
        if getattr(args, "sync_hook_config", False):
            path = write_hook_config(cfg)
            print(f"wrote {path}")
            return 0

        if getattr(args, "status", False):
            report = _status(cfg)
            print(json.dumps(report, indent=2) if getattr(args, "json", False)
                  else "\n".join(f"{k}: {v}" for k, v in report.items()))
            return 0

        if getattr(args, "init_state", False):
            init_state(_progress(), force=getattr(args, "force", False))
            print(f"initialized checkpoint at generation "
                  f"{_progress().read_checkpoint()}")
            return 0

        if getattr(args, "reconcile", False):
            return _reconcile(cfg)

        if getattr(args, "scan_recent", None):
            return _scan_recent(cfg, args)

        report = drain(cfg, dry_run=getattr(args, "dry_run", False))
        if report.refusal:
            print(f"Error: {report.refusal}", file=sys.stderr)
            return 1
        print(json.dumps(report.as_dict(), indent=2))
        if getattr(args, "dry_run", False) and report.candidates:
            print("\ncandidates (nothing was written):")
            for c in report.candidates:
                print(f"  [{c.get('category')}] {c['fact'][:150]}")
        return 0

    except WorkerRefusal as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _scan_recent(cfg, args) -> int:
    """Read real transcripts without queueing, claiming or writing anything."""
    from ..capture import extract as extract_mod

    ok, detail = extract_mod.verify_model(cfg)
    if not ok:
        print(f"Error: {detail}", file=sys.stderr)
        return 1

    total = 0
    for path in _recent_transcripts(cfg, int(args.scan_recent)):
        exchanges, boundary = read_exchanges(path)
        if not exchanges:
            continue
        result = extract_mod.extract(exchanges, cfg)
        print(f"\n{path.parent.name}/{path.stem[:8]} "
              f"({len(exchanges)} exchanges) -> {result.outcome.value}")
        for fact in result.facts:
            total += 1
            print(f"  [{fact['category']}] {fact['fact'][:180]}")
    print(f"\n{total} candidate facts. Nothing was written.")
    return 0


def _reconcile(cfg) -> int:
    """Rebuild capture progress after a database restore.

    Cannot be atomic across one database generation and many files, so it is
    an intent protocol: database first, then files, intent cleared last.
    """
    from ..db.connection import get_db_cursor

    queue = CaptureQueue(QUEUE_ROOT)
    progress = _progress()

    try:
        with queue.drain_lock():
            with get_db_cursor() as cursor:
                cursor.execute("SELECT generation FROM capture_state WHERE id = 1")
                row = cursor.fetchone()
            target = int(row["generation"] if isinstance(row, dict) else row[0])

            progress.write_intent({"target_generation": target,
                                   "started_at": time.time()})

            # Every claim class, not just progress: a claim can carry a
            # generation or identity the restored database has never seen.
            for claim in QUEUE_ROOT.glob("*.processing"):
                queue.release(claim)
            for dead in QUEUE_ROOT.glob("*.deadletter"):
                dead.unlink(missing_ok=True)

            # Rebuild per-session boundaries from committed rows.
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT metadata->>'session_id' AS session_id,
                           max((metadata->>'byte_offset')::bigint) AS offset
                      FROM memory
                     WHERE source = %s AND metadata->>'session_id' IS NOT NULL
                     GROUP BY 1
                    """,
                    (store_mod.AUTO_SOURCE,),
                )
                rows = cursor.fetchall()

            for old in progress.root.glob("*.progress"):
                old.unlink(missing_ok=True)
            for r in rows:
                if r["session_id"] and r["offset"] is not None:
                    progress.write_boundary(r["session_id"], {
                        "session_id": r["session_id"],
                        "byte_offset": int(r["offset"]),
                    })

            progress.write_checkpoint(target)
            progress.clear_intent()
            print(f"reconciled to generation {target}; "
                  f"{len(rows)} session boundaries rebuilt")
            return 0
    except Exception as e:
        print(f"Error: reconcile failed: {e}", file=sys.stderr)
        return 1
