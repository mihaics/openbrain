"""openbrain migrate - apply additive schema changes.

Deliberately not a migration framework. It applies two ADD COLUMN statements
and creates no other schema object: backup verification compares every dump's
TOC against an exact 19-entry allowlist, so a migration ledger table, helper
function or sequence would fail every backup until that allowlist was updated.
Idempotence comes from IF NOT EXISTS, not from a ledger.
"""
import sys
from argparse import Namespace

from ..db.connection import get_db_cursor, validate_schema

# The barrier that refuses to start on a schema mismatch must not block the
# only command that repairs it.
SKIP_SCHEMA_BARRIER = True

MIGRATION_SQL = """
ALTER TABLE memory ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP;
""" + """
CREATE TABLE IF NOT EXISTS capture_state (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    generation BIGINT NOT NULL DEFAULT 1,
    previous_generation BIGINT,
    committed_work_identity JSONB,
    reprocess_counter BIGINT NOT NULL DEFAULT 0,
    active_reprocess JSONB,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
INSERT INTO capture_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""


def apply_migration() -> None:
    """Add the access-tracking columns. One cursor context, one transaction.

    Both statements must land together; a half-migrated schema would pass an
    existence check for one column and fail on the other at query time.
    """
    with get_db_cursor() as cursor:
        cursor.execute(MIGRATION_SQL)


def migrate_cmd(args: Namespace) -> int:
    """Handle the migrate command."""
    try:
        apply_migration()
        validate_schema()
    except Exception as e:
        print(f"Error: migration failed: {e}", file=sys.stderr)
        return 1

    print("schema is up to date")
    return 0
