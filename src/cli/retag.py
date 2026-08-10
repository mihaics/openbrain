"""openbrain retag - recompute tags with the corrected tagger, reversibly.

The only destructive command in this sub-project. Everything here exists
because a tag the user added by hand cannot be recovered from the text:

  - a fresh verified backup of *this* database is required before any write;
  - the undo file is written and fsynced before the first mutation;
  - each row is updated under compare-and-set on everything the recomputation
    read, so a concurrently edited row is skipped rather than clobbered;
  - an applied marker is fsynced *after* each commit, never before, so a
    crash cannot authorise an undo of work this run never did;
  - tags whose provenance is `user`, or missing, are never removed.
"""
import fcntl
import hashlib
import json
import os
import sys
import uuid
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..db.connection import DatabaseConfig, get_db_cursor
from ..extractors.entities import extract_entities
from ..extractors.tagger import auto_tag
from . import backup_store as store
from .backup import database_identity, status_report

SCHEMA_VERSION = "retrieval-quality-1"
DEFAULT_MAX_BACKUP_AGE_HOURS = 24
PROTECTED_PROVENANCE = ("user",)


class RetagError(RuntimeError):
    """Refusing to proceed. Never partially applied."""


def _root() -> Path:
    return store.resolve_backup_dir()


def lock_path() -> Path:
    return _root() / ".retag.lock"


def sequence_path() -> Path:
    """Retag has its own sequence.

    Sharing the backup sequence would either consume backup numbers or race a
    running backup for one.
    """
    return _root() / ".retag-sequence"


@contextmanager
def acquire_lock():
    """Serialize runs. Two interleaved backfills produce interdependent undo
    files that neither can safely reverse."""
    store.ensure_dir(_root())
    fd = os.open(lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RetagError("another retag is running")
        yield
    finally:
        os.close(fd)


def _next_sequence() -> int:
    path = sequence_path()
    try:
        current = int(path.read_text().strip())
    except (OSError, ValueError):
        current = 0
    nxt = current + 1
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{nxt}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    store.fsync_dir(path.parent)
    return nxt


def check_backup_gate(max_age_hours: float) -> None:
    """Refuse to rewrite tags without a fresh verified dump of THIS database."""
    report = status_report(_root())
    age = report.get("newest_verified_age_seconds")
    if age is None:
        raise RetagError(
            "no verified backup found - run `openbrain backup` first"
        )
    # A future-dated manifest yields a negative age, which would sail past a
    # naive upper-bound check.
    if age < 0:
        raise RetagError(f"newest backup is dated in the future ({age:.0f}s)")
    if age >= max_age_hours * 3600:
        raise RetagError(
            f"newest verified backup is {age / 3600:.1f}h old, older than the "
            f"{max_age_hours}h limit - run `openbrain backup` first"
        )

    identity = report.get("newest_database_identity")
    live = database_identity(DatabaseConfig.get_instance())
    if identity != live:
        raise RetagError(
            f"newest backup describes {identity}, but this database is {live}. "
            f"Refusing to rewrite tags guarded by a backup of another database."
        )


def _content_hash(content: str) -> str:
    return hashlib.md5((content or "").encode()).hexdigest()


def recompute_tags(row: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    """What the corrected tagger produces, merged with protected tags.

    Entities are re-extracted from content in memory and never written back:
    trusting the stored entities JSON would reproduce the very false positives
    being removed, and writing it would pull entities into the CAS and undo
    record for no benefit.
    """
    entities = extract_entities(row["content"])
    fresh = auto_tag(row["content"], entities, row.get("source"), [])

    stored_sources = row.get("tag_sources") or {}
    if isinstance(stored_sources, str):
        stored_sources = json.loads(stored_sources)

    merged = dict(fresh)
    for tag in row.get("tags") or []:
        provenance = stored_sources.get(tag)
        # Missing provenance fails closed: it may be a user tag from before
        # provenance was recorded, and losing one is unrecoverable.
        if provenance is None or provenance in PROTECTED_PROVENANCE:
            merged[tag] = provenance or "user"

    return sorted(merged), merged


def select_rows(limit: Optional[int]) -> List[Dict[str, Any]]:
    """Rows whose recomputed tags or provenance differ.

    Comparing tags alone would never select a row whose tag list matches but
    whose provenance map is wrong, and a limited run would never converge.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, content, source, tags, tag_sources
              FROM memory
             ORDER BY created_at ASC, id ASC
            """
        )
        rows = cursor.fetchall()

    changed = []
    for row in rows:
        row = dict(row)
        new_tags, new_sources = recompute_tags(row)
        stored_sources = row.get("tag_sources") or {}
        if isinstance(stored_sources, str):
            stored_sources = json.loads(stored_sources)
        if sorted(row.get("tags") or []) == new_tags and stored_sources == new_sources:
            continue
        row["_new_tags"] = new_tags
        row["_new_sources"] = new_sources
        changed.append(row)
        if limit is not None and len(changed) >= limit:
            break
    return changed


class UndoFile:
    """Append-only record of what was planned and what was actually applied."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def open_exclusive(self, plans: List[Dict[str, Any]]) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self._fh = os.fdopen(fd, "w")
        self._write({
            "type": "header",
            "schema_version": SCHEMA_VERSION,
            "database": database_identity(DatabaseConfig.get_instance()),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        for plan in plans:
            self._write(plan)
        self._sync()
        store.fsync_dir(self.path.parent)

    def _write(self, record: Dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, default=str) + "\n")

    def _sync(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def mark_applied(self, memory_id: str) -> None:
        """Recorded only after the row's transaction has committed.

        Written before the commit, a marker whose commit then failed would
        authorise undoing a change this run never made - and if another writer
        independently produced the same end state, undo would destroy their
        work.
        """
        self._write({"type": "applied", "id": memory_id})
        self._sync()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def read_undo_file(path: Path) -> Tuple[Dict[str, Any], List[Dict], set]:
    """Parse and validate an undo file.

    A truncated *final* record is discarded rather than fatal: appends are not
    crash-atomic, and rejecting the whole file would block undo of every
    earlier, correctly recorded mutation. A malformed record anywhere else is
    fatal.
    """
    lines = path.read_text().splitlines()
    header, plans, applied = None, [], set()

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if i == len(lines) - 1:
                break  # torn tail
            raise RetagError(f"undo file corrupt at line {i + 1}")

        kind = record.get("type")
        if kind == "header":
            header = record
        elif kind == "plan":
            if any(p["id"] == record["id"] for p in plans):
                raise RetagError(f"duplicate plan for {record['id']}")
            plans.append(record)
        elif kind == "applied":
            applied.add(record["id"])

    if header is None:
        raise RetagError("undo file has no header")
    known = {p["id"] for p in plans}
    unknown = applied - known
    if unknown:
        raise RetagError(f"applied markers without a plan: {sorted(unknown)}")

    return header, plans, applied


def _apply_row(row: Dict[str, Any]) -> bool:
    """Compare-and-set on everything the recomputation read.

    Guarding on tags alone is insufficient: provenance can change with an
    unchanged tag array, and a content or source edit changes what the tagger
    would produce. IS NOT DISTINCT FROM because both columns are nullable and
    plain equality against NULL never matches, which would skip legacy rows
    forever.
    """
    stored_sources = row.get("tag_sources") or {}
    if isinstance(stored_sources, str):
        stored_sources = json.loads(stored_sources)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            UPDATE memory
               SET tags = %s, tag_sources = %s
             WHERE id = %s
               AND content IS NOT DISTINCT FROM %s
               AND source IS NOT DISTINCT FROM %s
               AND tags IS NOT DISTINCT FROM %s
               AND tag_sources IS NOT DISTINCT FROM %s::jsonb
            """,
            (row["_new_tags"], json.dumps(row["_new_sources"]), str(row["id"]),
             row["content"], row["source"], row.get("tags") or [],
             json.dumps(stored_sources)),
        )
        return cursor.rowcount > 0


def _undo_row(plan: Dict[str, Any]) -> bool:
    """Restore before-state, guarded against every post-backfill edit.

    md5(content) is evaluated in SQL rather than hashed in Python and compared
    first, which would leave a check-then-write race.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            UPDATE memory
               SET tags = %s, tag_sources = %s
             WHERE id = %s
               AND md5(content) = %s
               AND source IS NOT DISTINCT FROM %s
               AND tags IS NOT DISTINCT FROM %s
               AND tag_sources IS NOT DISTINCT FROM %s::jsonb
            """,
            (plan["before_tags"], json.dumps(plan["before_tag_sources"]),
             plan["id"], plan["content_hash"], plan["source"],
             plan["after_tags"], json.dumps(plan["after_tag_sources"])),
        )
        return cursor.rowcount > 0


def run_backfill(limit: Optional[int], dry_run: bool, max_age_hours: float) -> int:
    rows = select_rows(limit)
    if not rows:
        print("nothing to retag")
        return 0

    removed_total: Dict[str, int] = {}
    for row in rows:
        for tag in set(row.get("tags") or []) - set(row["_new_tags"]):
            removed_total[tag] = removed_total.get(tag, 0) + 1

    print(f"{len(rows)} memories would change" if dry_run
          else f"{len(rows)} memories to change")
    for tag, n in sorted(removed_total.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  -{tag}: {n}")

    if dry_run:
        print("(dry run - nothing was written)")
        return 0

    check_backup_gate(max_age_hours)

    plans = [{
        "type": "plan",
        "id": str(row["id"]),
        "source": row["source"],
        "content_hash": _content_hash(row["content"]),
        "before_tags": list(row.get("tags") or []),
        "before_tag_sources": row.get("tag_sources") or {},
        "after_tags": row["_new_tags"],
        "after_tag_sources": row["_new_sources"],
        "at": datetime.now(timezone.utc).isoformat(),
    } for row in rows]

    seq = _next_sequence()
    undo_path = _root() / f"tag-backfill-{seq:06d}.jsonl"
    undo = UndoFile(undo_path)
    undo.open_exclusive(plans)

    applied = skipped = 0
    try:
        for row in rows:
            if _apply_row(row):
                undo.mark_applied(str(row["id"]))
                applied += 1
            else:
                skipped += 1
    finally:
        undo.close()

    print(f"applied: {applied}\nskipped (changed underneath): {skipped}")
    print(f"undo file: {undo_path}")
    return 0


def run_undo(path: Path, force: bool, dry_run: bool) -> int:
    header, plans, applied = read_undo_file(path)

    live = database_identity(DatabaseConfig.get_instance())
    if header.get("database") != live and not force:
        raise RetagError(
            f"undo file describes {header.get('database')}, this database is "
            f"{live}. Refusing without --force."
        )
    if header.get("schema_version") != SCHEMA_VERSION and not force:
        raise RetagError(
            f"undo file schema {header.get('schema_version')!r} != "
            f"{SCHEMA_VERSION!r}. Refusing without --force."
        )

    marked = [p for p in plans if p["id"] in applied]
    unmarked = [p for p in plans if p["id"] not in applied]

    if dry_run:
        print(f"{len(marked)} rows can be reverted")
        for p in unmarked:
            print(f"  INDETERMINATE {p['id']} before_tags={p['before_tags']}")
        if unmarked:
            print(f"{len(unmarked)} planned rows carry no applied marker and "
                  f"will not be touched - restore by hand if needed")
        return 0

    reverted = conflicted = 0
    for plan in marked:
        if _undo_row(plan):
            reverted += 1
        else:
            conflicted += 1
            print(f"  conflict: {plan['id']} changed since the backfill")

    # Unmarked plans are never restored automatically, not even when the row
    # still matches after_*: the documented reason a forward CAS skips a row is
    # that another writer produced that same state, so value equality cannot
    # distinguish a lost marker from someone else's work.
    print(f"reverted: {reverted}\nconflicts skipped: {conflicted}")
    if unmarked:
        print(f"indeterminate (never applied or marker lost): {len(unmarked)}")
    return 1 if conflicted else 0


def retag_cmd(args: Namespace) -> int:
    """Handle the retag command."""
    undo_file = getattr(args, "undo", None)

    if undo_file and getattr(args, "limit", None):
        print("Error: --undo cannot be combined with --limit; partially "
              "undoing a recorded set is how inconsistency starts",
              file=sys.stderr)
        return 2

    try:
        if undo_file:
            # Deliberately outside the backup gate: undo must work in an
            # emergency, which is exactly when the backup may be stale.
            with acquire_lock():
                return run_undo(Path(undo_file), getattr(args, "force", False),
                                getattr(args, "dry_run", False))

        with acquire_lock():
            return run_backfill(
                getattr(args, "limit", None),
                getattr(args, "dry_run", False),
                getattr(args, "max_backup_age", DEFAULT_MAX_BACKUP_AGE_HOURS),
            )
    except RetagError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
