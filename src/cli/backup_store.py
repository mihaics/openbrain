"""Filesystem primitives for OpenBrain backups.

Durability rules encoded here:
  - Ordering comes from a monotonic sequence, not timestamps. UTC solves DST
    but is not monotonic across a manual clock change or NTP step.
  - Publication is no-clobber via link()+unlink(). POSIX rename() overwrites
    silently, which would destroy an existing dump.
  - The sequence is reserved (written and fsynced) before use, so a crash
    wastes a number but never reuses one. A reused number would fail
    publication on every later run and wedge backups permanently.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_BACKUP_DIR = Path.home() / "backups" / "openbrain"
SEQUENCE_FILE = "sequence"
KEEP_COUNT = 60


def resolve_backup_dir() -> Path:
    """Where backups live: OPENBRAIN_BACKUP_DIR, else ~/backups/openbrain.

    The override exists so a test-database backfill cannot read production
    manifests or write its lock, sequence and undo files into the production
    retag namespace. Resolved per call rather than captured at import, so a
    test that sets the variable is not defeated by import order.
    """
    override = os.environ.get("OPENBRAIN_BACKUP_DIR")
    return Path(override) if override else DEFAULT_BACKUP_DIR


BACKUP_DIR = resolve_backup_dir()

_NAME_RE = re.compile(
    r"^openbrain-(?P<seq>\d+)-(?P<ts>\d{8}T\d{6}\.\d{6})Z\.dump$"
)


def ensure_dir(backup_dir: Path = BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    return backup_dir


def fsync_dir(path: Path) -> None:
    """Make new directory entries durable. Fsyncing files alone is not enough."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    fsync_dir(path.parent)


def parse_dump_name(path: Path) -> Optional[Tuple[int, datetime]]:
    """Return (sequence, timestamp) for a managed dump name, else None."""
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    ts = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%S.%f").replace(
        tzinfo=timezone.utc
    )
    return int(m.group("seq")), ts


def managed_dumps(backup_dir: Path) -> List[Path]:
    return [p for p in backup_dir.glob("*.dump") if parse_dump_name(p)]


def peek_sequence(backup_dir: Path = BACKUP_DIR) -> int:
    """What next_sequence() would return, without persisting anything.

    --dry-run uses this. Calling next_sequence() would write the incremented
    value, so a dry run would silently consume a number.
    """
    state = backup_dir / SEQUENCE_FILE
    try:
        stored = int(state.read_text().strip())
    except (OSError, ValueError):
        stored = 0

    from_names = 0
    for p in managed_dumps(backup_dir):
        parsed = parse_dump_name(p)
        if parsed:
            from_names = max(from_names, parsed[0])

    return max(stored, from_names) + 1


def next_sequence(backup_dir: Path = BACKUP_DIR) -> int:
    """Reserve the next sequence number, persisting it before returning.

    Recovery: filenames are the durable record, so a missing or corrupt
    state file recomputes rather than halting backups.
    """
    ensure_dir(backup_dir)
    state = backup_dir / SEQUENCE_FILE

    stored = 0
    try:
        stored = int(state.read_text().strip())
    except (OSError, ValueError):
        stored = 0

    from_names = 0
    for p in managed_dumps(backup_dir):
        parsed = parse_dump_name(p)
        if parsed:
            from_names = max(from_names, parsed[0])

    nxt = max(stored, from_names) + 1
    _atomic_write(state, f"{nxt}\n".encode())
    return nxt


SEQ_WIDTH = 6


def dump_path(backup_dir: Path, seq: int, when: datetime) -> Path:
    """Name a dump. Sequence is zero-padded so names sort lexically too."""
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return backup_dir / f"openbrain-{seq:0{SEQ_WIDTH}d}-{stamp}Z.dump"


def manifest_path(dump: Path) -> Path:
    return dump.with_suffix(dump.suffix + ".json")


def fsync_file(path: Path) -> None:
    """Make a file's data blocks durable.

    Fsyncing only the directory and the manifest makes the *names* durable
    while leaving the archive's payload potentially unwritten - worse than
    no backup, because a power loss then leaves a durable filename and
    manifest pointing at incomplete data that later runs treat as verified.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(partial: Path, final: Path) -> None:
    """Move partial to final without ever overwriting an existing file."""
    fsync_file(partial)          # payload durable before the name exists
    os.link(partial, final)      # raises FileExistsError if final exists
    os.chmod(final, 0o600)
    partial.unlink()
    fsync_dir(final.parent)


def write_manifest(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True).encode())


def read_manifest(dump: Path) -> Optional[dict]:
    """Load a dump's manifest, or None if absent or unparseable."""
    try:
        return json.loads(manifest_path(dump).read_text())
    except (OSError, ValueError):
        return None


def is_eligible(dump: Path) -> bool:
    """A dump counts toward retention only if provably complete and verified.

    'Has a manifest' is not enough: a crash between recording
    archive_written and decoding the payload would otherwise let an
    unverified dump occupy a slot ahead of a verified one.
    """
    manifest = read_manifest(dump)
    if not manifest:
        return False
    if not manifest.get("verification", {}).get("payload_decoded"):
        return False
    try:
        if manifest.get("dump_size") != dump.stat().st_size:
            return False
        if manifest.get("dump_sha256") != sha256_of(dump):
            return False
    except OSError:
        return False
    return True


def select_for_deletion(
    backup_dir: Path,
    keep: int = KEEP_COUNT,
    current: Optional[Path] = None,
) -> List[Path]:
    """Return dumps to prune: eligible ones beyond the newest `keep`.

    The current run's output is always kept by identity. "Never delete the
    newest" is ambiguous under a clock rollback; "never delete what this run
    just created" is not.
    """
    eligible = [p for p in managed_dumps(backup_dir) if is_eligible(p)]
    eligible.sort(key=lambda p: parse_dump_name(p)[0], reverse=True)

    # Never empty the keep set. A retention bug, or a keep of 0, must not be
    # able to leave zero backups on disk.
    effective_keep = max(1, keep)
    doomed = eligible[effective_keep:]
    if current is not None:
        doomed = [p for p in doomed if p != current]
    return doomed


def delete_dump(dump: Path) -> None:
    """Delete a dump and its manifest together."""
    manifest_path(dump).unlink(missing_ok=True)
    dump.unlink(missing_ok=True)
