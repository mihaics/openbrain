"""openbrain backup - dump, verify, prune."""
import fcntl
import json as jsonlib
import os
import subprocess
import sys
import time
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import psycopg2

from ..db.connection import DatabaseConfig
from . import backup_store as store

READINESS_ATTEMPTS = 5
CONNECT_TIMEOUT_SECONDS = 10
# Sleeps are 2+4+8+16 = 30s of backoff across 5 attempts, plus up to
# 5 * CONNECT_TIMEOUT_SECONDS of connect time: ~80s worst case.
STALE_AFTER_SECONDS = 48 * 3600


class VerificationError(RuntimeError):
    """The archive is not trustworthy."""


# Object identities the dump is expected to contain. An entry outside this
# set means the schema drifted, and the run fails loudly rather than
# half-verifying against stale expectations.
# The 19 object identities this database's dump contains, compared by exact
# equality. Owner and numeric ids are stripped by _toc_identity first.
#
# Derive this set empirically, do not hand-edit it:
#   pg_restore -l dump | grep -v '^;'
# then reduce each line the way _toc_identity does. If verification reports a
# mismatch after an intentional schema change, regenerate the set.
EXPECTED_TOC_ENTRIES = {
    "ACL - SCHEMA public pg_database_owner",
    "EXTENSION - uuid-ossp",
    "EXTENSION - vector",
    'COMMENT - EXTENSION "uuid-ossp"',
    "COMMENT - EXTENSION vector",
    "FUNCTION public get_memory_stats() openbrain",
    "FUNCTION public get_today_memories(integer) openbrain",
    "TABLE public memory openbrain",
    "TABLE DATA public memory openbrain",
    "CONSTRAINT public memory memory_pkey openbrain",
    "INDEX public idx_memory_content_fts openbrain",
    "INDEX public idx_memory_created openbrain",
    "INDEX public idx_memory_embedding openbrain",
    "INDEX public idx_memory_entities openbrain",
    "INDEX public idx_memory_original_date openbrain",
    "INDEX public idx_memory_source openbrain",
    "INDEX public idx_memory_tags openbrain",
    "DEFAULT ACL public DEFAULT PRIVILEGES FOR SEQUENCES postgres",
    "DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES postgres",
}
assert len(EXPECTED_TOC_ENTRIES) == 19


def _pg_restore_list(archive: Path) -> str:
    result = subprocess.run(
        ["pg_restore", "--list", str(archive)], capture_output=True
    )
    if result.returncode != 0:
        raise VerificationError(
            f"pg_restore --list failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout.decode(errors="replace")


def _pg_restore_decode(archive: Path) -> None:
    """Decode the whole payload, streamed to /dev/null.

    --list reads mostly the TOC, so it passes on an archive whose later data
    blocks are corrupt, and it reports a TABLE DATA entry even for an empty
    table. Only a full decode exercises every block. Streaming keeps memory
    bounded as volume grows.
    """
    with open(os.devnull, "wb") as devnull:
        result = subprocess.run(
            ["pg_restore", "-f", "-", str(archive)],
            stdout=devnull, stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise VerificationError(
            f"payload decode failed: {result.stderr.decode(errors='replace')}"
        )


def _toc_identity(line: str) -> Optional[str]:
    """Reduce a TOC line to its object identity, dropping the numeric ids.

    Input:  '3637; 1259 16998 INDEX public idx_memory_embedding openbrain'
    Output: 'INDEX public idx_memory_embedding openbrain'

    The leading 'N; oid oid ' prefix carries OIDs that differ between dumps,
    so it is stripped. Everything after it - including the owner - is stable
    for this single-role database and is kept, so comparison is exact.
    """
    line = line.strip()
    if not line or line.startswith(";") or ";" not in line:
        return None
    body = line.split(";", 1)[1].strip()
    parts = body.split(None, 2)
    if len(parts) < 3:
        return None
    return parts[2].strip()


def verify_archive(archive: Path) -> None:
    """Verify a dump as far as is possible without restoring it.

    This is the entirety of verification: it proves the archive is
    structurally intact, contains the expected object set, and decodes end
    to end. It does NOT prove the archive restores - nothing here exercises
    CREATE EXTENSION, schema creation, index rebuilds, or the vector(1024)
    column. See the spec's Accepted Risks.
    """
    toc = _pg_restore_list(archive)

    identities = set()
    for line in toc.splitlines():
        ident = _toc_identity(line)
        if ident:
            identities.add(ident)

    if not any(i.startswith("TABLE DATA public memory") for i in identities):
        raise VerificationError(
            "archive has no 'TABLE DATA public memory' entry - it contains no "
            "memory rows"
        )

    # Exact identity comparison, not prefix matching. A prefix test would let
    # a `memory_backup` table pass as `TABLE public memory`, or an
    # `idx_memory_tags_extra` index pass as `idx_memory_tags`.
    unexpected = sorted(identities - EXPECTED_TOC_ENTRIES)
    if unexpected:
        raise VerificationError(
            "unexpected objects in dump TOC (schema drifted; update "
            f"EXPECTED_TOC_ENTRIES): {unexpected}"
        )

    # Completeness matters as much as absence of surprises. Requiring only
    # TABLE DATA would accept an archive missing indexes, the PK, a function,
    # or an extension - structurally incomplete but superficially fine.
    missing = sorted(EXPECTED_TOC_ENTRIES - identities)
    if missing:
        raise VerificationError(
            f"dump TOC is missing expected objects: {missing}"
        )

    _pg_restore_decode(archive)


def pg_env(config: DatabaseConfig) -> Dict[str, str]:
    """Child environment carrying the password.

    Never argv: command lines are world-readable via /proc. Never a mutation
    of os.environ either - this process may go on to do other things.
    """
    env = dict(os.environ)
    env["PGPASSWORD"] = config.password
    return env


def get_db_connection(config: DatabaseConfig):
    """Connect, retrying briefly.

    A user timer with Persistent=true treats a missed event as handled once
    the service is queued, not once it succeeds. So a run firing right after
    resume, before PostgreSQL is accepting connections, gets no second chance
    until tomorrow unless it retries here.
    """
    delay = 2.0
    last: Optional[Exception] = None
    for attempt in range(READINESS_ATTEMPTS):
        try:
            # connect_timeout bounds each attempt. Without it a single
            # attempt can hang far past the intended total.
            return psycopg2.connect(
                host=config.host, port=config.port, dbname=config.name,
                user=config.user, password=config.password,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except psycopg2.OperationalError as e:
            last = e
            if attempt < READINESS_ATTEMPTS - 1:
                print(f"database not ready, retrying in {delay:.0f}s",
                      file=sys.stderr)
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"database unreachable after {READINESS_ATTEMPTS} attempts: {last}")


def snapshot_metadata(conn) -> Dict[str, Any]:
    """Export a snapshot and read the counts it will contain.

    The row count must come from the same snapshot pg_dump reads, or it
    describes a different database state than the archive. Nothing compares
    it automatically now that restore verification is gone - it exists so
    the runbook's manual check ("this archive should contain exactly N
    rows") is exact rather than approximate.

    The caller must keep this transaction open until pg_dump exits.
    """
    cur = conn.cursor()
    cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cur.execute("SELECT pg_export_snapshot()")
    snapshot_id = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM memory")
    row_count = cur.fetchone()[0]

    cur.execute("""
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'memory'::regclass
          AND attname = 'embedding' AND NOT attisdropped
    """)
    row = cur.fetchone()
    embedding_dim = int(row[0]) if row and row[0] and row[0] > 0 else None

    cur.execute("SELECT extname, extversion, extnamespace::regnamespace::text "
                "FROM pg_extension ORDER BY extname")
    extensions = [
        {"name": r[0], "version": r[1], "schema": r[2]} for r in cur.fetchall()
    ]

    cur.execute("SHOW server_version")
    server_version = cur.fetchone()[0]

    return {
        "snapshot_id": snapshot_id,
        "row_count": row_count,
        "embedding_dim": embedding_dim,
        "extensions": extensions,
        "server_version": server_version,
    }


def database_identity(config: DatabaseConfig) -> Dict[str, Any]:
    """Which database a dump came from. Never includes the password.

    The retag gate compares this against the live configuration: a reused
    backup directory could otherwise gate a destructive backfill of one
    database on a verified dump of another.
    """
    return {"host": config.host, "port": config.port, "name": config.name}


def _pg_dump_version() -> str:
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
    return out.stdout.strip()


def take_dump(backup_dir: Path, seq: int) -> Tuple[Path, Dict[str, Any]]:
    """Produce a verified dump plus its manifest data."""
    config = DatabaseConfig.get_instance()
    store.ensure_dir(backup_dir)

    started = datetime.now(timezone.utc)
    final = store.dump_path(backup_dir, seq, started)
    partial = backup_dir / f"{final.name}.partial.{os.getpid()}"

    conn = get_db_connection(config)
    try:
        meta = snapshot_metadata(conn)

        argv = [
            "pg_dump", "-Fc",
            "-h", config.host, "-p", str(config.port),
            "-U", config.user, "-d", config.name,
            "--snapshot", meta["snapshot_id"],
            "-f", str(partial),
        ]
        result = subprocess.run(argv, env=pg_env(config), capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed ({result.returncode}): "
                f"{result.stderr.decode(errors='replace')}"
            )
    finally:
        # Only now is the snapshot no longer needed.
        conn.close()

    os.chmod(partial, 0o600)

    try:
        verify_archive(partial)
    except VerificationError as e:
        # Keep the evidence. An orphaned .partial with no manifest loses the
        # diagnostic entirely, and an unverified archive must never be able to
        # pass as a good backup.
        failed_at = datetime.now(timezone.utc)
        unverified = backup_dir / f"{final.name}.unverified"
        store.publish(partial, unverified)
        # Carry the same metadata a successful manifest gets. It was all
        # collected before verification failed, and a manual recovery from a
        # partially-bad archive needs the dimension and extension versions
        # every bit as much as a clean one does.
        store.write_manifest(store.manifest_path(unverified), {
            "sequence": seq,
            "database": database_identity(config),
            "dump_started": started.isoformat(),
            "dump_finished": failed_at.isoformat(),
            "snapshot_id": meta["snapshot_id"],
            "row_count": meta["row_count"],
            "embedding_dim": meta["embedding_dim"],
            "extensions": meta["extensions"],
            "server_version": meta["server_version"],
            "pg_dump_version": _pg_dump_version(),
            "dump_size": unverified.stat().st_size,
            "dump_sha256": store.sha256_of(unverified),
            "verification": {
                "archive_written": failed_at.isoformat(),
                "payload_decoded": None,
                "payload_decode_failed": failed_at.isoformat(),
                "failure_reason": str(e),
            },
        })
        store.fsync_dir(backup_dir)
        raise

    finished = datetime.now(timezone.utc)
    manifest = {
        "sequence": seq,
        "database": database_identity(config),
        "dump_started": started.isoformat(),
        "dump_finished": finished.isoformat(),
        "snapshot_id": meta["snapshot_id"],
        "row_count": meta["row_count"],
        "embedding_dim": meta["embedding_dim"],
        "extensions": meta["extensions"],
        "server_version": meta["server_version"],
        "pg_dump_version": _pg_dump_version(),
        "dump_size": partial.stat().st_size,
        "dump_sha256": store.sha256_of(partial),
        "verification": {
            "archive_written": finished.isoformat(),
            "payload_decoded": finished.isoformat(),
            "payload_decode_failed": None,
        },
    }

    store.publish(partial, final)
    store.write_manifest(store.manifest_path(final), manifest)
    store.fsync_dir(backup_dir)
    return final, manifest


@contextmanager
def acquire_lock(backup_dir: Path):
    """Serialize runs. A manual invocation can race the timer."""
    store.ensure_dir(backup_dir)
    lock_path = backup_dir / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another backup is running")
        yield
    finally:
        os.close(fd)


def status_report(backup_dir: Path) -> Dict[str, Any]:
    """Machine-readable state. Sub-project 3's health signal consumes this."""
    dumps = store.managed_dumps(backup_dir)
    verified = [p for p in dumps if store.is_eligible(p)]

    newest_age = None
    newest_rows = None
    newest_identity = None
    if verified:
        verified.sort(key=lambda p: store.parse_dump_name(p)[0], reverse=True)
        newest = verified[0]
        manifest = store.read_manifest(newest) or {}
        decoded = manifest.get("verification", {}).get("payload_decoded")
        if decoded:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(decoded)
            newest_age = age.total_seconds()
        newest_rows = manifest.get("row_count")
        newest_identity = manifest.get("database")

    # Newest archive age, verified or not: sub-project 3 needs to distinguish
    # "no backup ran" from "a backup ran and failed verification".
    newest_archive_age = None
    all_archives = dumps + list(backup_dir.glob("*.unverified"))
    if all_archives:
        newest_mtime = max(p.stat().st_mtime for p in all_archives)
        newest_archive_age = (
            datetime.now(timezone.utc).timestamp() - newest_mtime
        )

    last_failure = None
    failures = sorted(
        backup_dir.glob("*.unverified"), key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if failures:
        fm = store.read_manifest(failures[0]) or {}
        v = fm.get("verification", {})
        last_failure = {
            "archive": failures[0].name,
            "at": v.get("payload_decode_failed"),
            "reason": v.get("failure_reason"),
        }

    return {
        "backup_dir": str(backup_dir),
        "dump_count": len(dumps),
        "verified_count": len(verified),
        "newest_archive_age_seconds": newest_archive_age,
        "newest_verified_age_seconds": newest_age,
        "newest_row_count": newest_rows,
        "newest_database_identity": newest_identity,
        "last_failure": last_failure,
        "total_bytes": sum(p.stat().st_size for p in dumps if p.exists()),
        "healthy": newest_age is not None and newest_age < STALE_AFTER_SECONDS,
    }


def _render_status(report: Dict[str, Any]) -> str:
    age = report["newest_verified_age_seconds"]
    age_text = "never" if age is None else f"{age / 3600:.1f}h ago"
    return "\n".join([
        f"backup dir:       {report['backup_dir']}",
        f"dumps:            {report['dump_count']} ({report['verified_count']} verified)",
        f"newest verified:  {age_text}",
        f"last failure:     {report['last_failure'] or 'none'}",
        f"rows in newest:   {report['newest_row_count']}",
        f"total size:       {report['total_bytes'] / 1e6:.1f} MB",
        f"healthy:          {report['healthy']}",
    ])


def backup_cmd(args: Namespace) -> int:
    """Handle the backup command."""
    primary = [bool(getattr(args, 'verify_only', False)),
               bool(getattr(args, 'status', False))]
    if sum(primary) > 1:
        print("Error: --verify-only and --status are mutually exclusive",
              file=sys.stderr)
        return 2
    if getattr(args, 'json', False) and not getattr(args, 'status', False):
        print("Error: --json is only valid with --status", file=sys.stderr)
        return 2
    if getattr(args, 'dry_run', False) and (
        getattr(args, 'status', False) or getattr(args, 'verify_only', False)
    ):
        print("Error: --dry-run is only valid for a normal backup run",
              file=sys.stderr)
        return 2

    backup_dir = store.BACKUP_DIR

    if getattr(args, 'status', False):
        report = status_report(backup_dir)
        print(jsonlib.dumps(report, indent=2) if args.json
              else _render_status(report))
        return 0 if report["healthy"] else 1

    try:
        with acquire_lock(backup_dir):
            if getattr(args, 'verify_only', False):
                verified = [p for p in store.managed_dumps(backup_dir)]
                if not verified:
                    print("Error: no dumps to verify", file=sys.stderr)
                    return 1
                verified.sort(key=lambda p: store.parse_dump_name(p)[0],
                              reverse=True)
                verify_archive(verified[0])
                print(f"verified {verified[0].name}")
                return 0

            if getattr(args, 'dry_run', False):
                # Must not call next_sequence(): it persists the incremented
                # value, so a dry run would consume a sequence number and the
                # first real dump would start at 2.
                seq = store.peek_sequence(backup_dir)
                when = datetime.now(timezone.utc)
                planned = store.dump_path(backup_dir, seq, when)
                print(f"would write {planned.name}")
                # Predict pruning as it will happen *after* the new dump
                # exists, so the preview matches the real run.
                for p in store.select_for_deletion(
                    backup_dir, store.KEEP_COUNT - 1, current=None
                ):
                    print(f"would prune {p.name}")
                return 0

            seq = store.next_sequence(backup_dir)
            dump, manifest = take_dump(backup_dir, seq)
            print(f"wrote {dump.name} ({manifest['row_count']} rows, "
                  f"{manifest['dump_size'] / 1e6:.1f} MB)")

            # Prune only after a written and verified dump. A failed run
            # must never delete a good older one.
            for p in store.select_for_deletion(backup_dir, store.KEEP_COUNT,
                                               current=dump):
                store.delete_dump(p)
                print(f"pruned {p.name}")
            store.fsync_dir(backup_dir)
            return 0

    except (RuntimeError, VerificationError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
