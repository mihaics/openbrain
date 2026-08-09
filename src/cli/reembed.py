"""Re-embed memories with NULL or all-zero embeddings."""
import fcntl
import os
import sys
import uuid
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..db.connection import get_db_cursor, get_vector_dim
from ..embedder import EmbeddingError, create_validated_embedding

LOCK_PATH = Path.home() / "backups" / "openbrain" / ".reembed.lock"
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class ReembedReport:
    """Counts with an invariant that actually holds.

        selected  = rows the queries returned
        attempted = rows the sweep tried to act on
        attempted = reembedded + zeros_repaired + skipped_changed + failed
        selected  = attempted + unprocessed

    In --dry-run, selected > 0 while attempted == 0 and every outcome
    counter stays 0 - which is why `selected` and `attempted` are separate
    fields rather than one `examined`. After an abort, `unprocessed` accounts
    for the rows that were selected but never tried.
    """
    selected: int = 0
    attempted: int = 0
    reembedded: int = 0
    zeros_repaired: int = 0
    skipped_changed: int = 0
    failed: int = 0          # embedder refused; row left as it was
    unprocessed: int = 0     # selected but never attempted (abort or dry-run)
    aborted: bool = False
    dry_run: bool = False

    def check_invariant(self) -> None:
        """Fail loudly on a counting bug rather than reporting nonsense."""
        outcomes = (self.reembedded + self.zeros_repaired
                    + self.skipped_changed + self.failed)
        assert outcomes == self.attempted, (
            f"outcome counters {outcomes} != attempted {self.attempted}"
        )
        assert self.attempted + self.unprocessed == self.selected, (
            f"attempted {self.attempted} + unprocessed {self.unprocessed} "
            f"!= selected {self.selected}"
        )

    def render(self) -> str:
        lines = [
            f"selected:          {self.selected}",
            f"attempted:         {self.attempted}",
            f"re-embedded:       {self.reembedded}",
            f"zeros repaired:    {self.zeros_repaired}",
            f"skipped (changed): {self.skipped_changed}",
            f"failed:            {self.failed}",
            f"unprocessed:       {self.unprocessed}",
        ]
        if self.dry_run:
            lines.append("(dry run - nothing was written)")
        if self.aborted:
            lines.append(
                "ABORTED after 3 consecutive embedder failures. Selection is "
                "oldest-first, so if the same rows fail every run they will "
                "block every future sweep: pass --skip-ids <id,id> to step "
                "over them, or fix their content."
            )
        return "\n".join(lines)


@contextmanager
def acquire_lock():
    """Prevent two sweeps from interleaving."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another reembed is running")
        yield
    finally:
        os.close(fd)


def run_sweep(
    limit: int,
    dry_run: bool,
    skip_ids: Optional[List[str]] = None,
) -> ReembedReport:
    """NULL any zero vectors, then re-embed rows with NULL embeddings.

    skip_ids are excluded from both phases. Selection is oldest-first, so
    three rows that deterministically fail to embed would otherwise be
    re-selected every run and abort every sweep before it reaches
    anything else.
    """
    report = ReembedReport(dry_run=dry_run)
    dim = get_vector_dim()
    skip = [str(i) for i in (skip_ids or [])]

    with acquire_lock():
        # Phase 1: zero vectors -> NULL. A zero vector is never a correct
        # resting state and repairing it must not depend on the embedder being
        # reachable, or a sweep started while Ollama is down aborts having
        # repaired nothing.
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT id, content FROM memory
                WHERE embedding IS NOT NULL
                  AND embedding = array_fill(0, ARRAY[%s])::vector
                  AND NOT (id = ANY(%s::uuid[]))
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                """,
                (dim, skip, limit),
            )
            zero_rows: List[Dict[str, Any]] = list(cursor.fetchall())
        report.selected += len(zero_rows)

        for row in zero_rows:
            if dry_run:
                report.unprocessed += 1
                continue
            report.attempted += 1
            # Guard on the embedding still being zero as well as content
            # unchanged: a concurrent repair must not be clobbered back to
            # NULL. Own cursor context so this row commits on its own.
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memory SET embedding = NULL
                    WHERE id = %s AND content = %s
                      AND embedding = array_fill(0, ARRAY[%s])::vector
                    """,
                    (str(row['id']), row['content'], dim),
                )
                repaired = cursor.rowcount
            if repaired:
                report.zeros_repaired += 1
            else:
                report.skipped_changed += 1

        # Phase 2: NULL embeddings -> fresh vectors. Rows just NULLed in phase
        # 1 are excluded by id, so they do not consume the limit twice and are
        # picked up by the next sweep instead.
        already_seen = {str(r['id']) for r in zero_rows} | set(skip)
        remaining = max(0, limit - report.selected)

        null_rows: List[Dict[str, Any]] = []
        if remaining:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, content FROM memory
                    WHERE embedding IS NULL
                      AND NOT (id = ANY(%s::uuid[]))
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (list(already_seen), remaining),
                )
                null_rows = list(cursor.fetchall())
        report.selected += len(null_rows)

        consecutive_failures = 0
        for index, row in enumerate(null_rows):
            if dry_run:
                report.unprocessed += 1
                continue
            report.attempted += 1
            try:
                vector = create_validated_embedding(row['content'], dim)
            except EmbeddingError as e:
                consecutive_failures += 1
                report.failed += 1
                print(f"embed failed for {row['id']}: {e}", file=sys.stderr)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    report.aborted = True
                    report.unprocessed += len(null_rows) - index - 1
                    break
                continue

            consecutive_failures = 0
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memory SET embedding = %s
                    WHERE id = %s AND content = %s
                    """,
                    (vector, str(row['id']), row['content']),
                )
                written = cursor.rowcount
            if written:
                report.reembedded += 1
            else:
                # Content changed between select and update. Writing a vector
                # computed from the old text would be wrong.
                report.skipped_changed += 1

    report.check_invariant()
    return report


def _parse_skip_ids(raw: str) -> List[str]:
    """Parse --skip-ids, rejecting anything that is not a UUID.

    A typo must not silently widen or narrow the sweep, so bad input is an
    error rather than a skipped entry.
    """
    ids: List[str] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        ids.append(str(uuid.UUID(chunk)))   # ValueError on malformed input
    return ids


def reembed_cmd(args: Namespace) -> int:
    """Handle the reembed command."""
    try:
        skip_ids = _parse_skip_ids(getattr(args, 'skip_ids', ''))
    except ValueError as e:
        print(f"Error: --skip-ids contains an invalid UUID: {e}", file=sys.stderr)
        return 2

    try:
        report = run_sweep(
            limit=args.limit, dry_run=args.dry_run, skip_ids=skip_ids
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(report.render())
    return 1 if report.aborted else 0
