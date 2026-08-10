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
