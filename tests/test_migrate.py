"""Tests for the schema migration and the startup barrier."""
import uuid
from unittest.mock import Mock, patch

import pytest


def _cursor_ctx(rows=None):
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.fetchall.return_value = rows if rows is not None else []
    return ctx


GOOD_COLUMNS = [
    {"column_name": "access_count", "data_type": "integer",
     "is_nullable": "NO", "column_default": "0"},
    {"column_name": "last_accessed_at", "data_type": "timestamp without time zone",
     "is_nullable": "YES", "column_default": None},
]


class TestMigrate:
    def test_adds_both_columns_in_one_transaction(self):
        from src.cli import migrate

        ctx = _cursor_ctx()
        with patch('src.cli.migrate.get_db_cursor', return_value=ctx) as mock_cur:
            migrate.apply_migration()

        sql = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list)
        assert "access_count" in sql
        assert "last_accessed_at" in sql
        assert "IF NOT EXISTS" in sql
        # One cursor context == one transaction. A half-migrated schema is
        # worse than an unmigrated one.
        assert mock_cur.call_count == 1

    def test_creates_only_capture_state(self):
        """backup.py fails on any TOC entry outside its allowlist, so every
        object the migration creates must be a deliberate, allowlisted one.

        capture_state is the single permitted addition - it carries the
        restore interlock and its three TOC identities were added to the
        allowlist in the same change. Anything else is still forbidden.
        """
        from src.cli import migrate

        ctx = _cursor_ctx()
        with patch('src.cli.migrate.get_db_cursor', return_value=ctx):
            migrate.apply_migration()

        sql = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list).upper()
        assert sql.count("CREATE TABLE") == 1
        assert "CAPTURE_STATE" in sql
        for forbidden in ("CREATE FUNCTION", "CREATE SEQUENCE", "CREATE INDEX",
                          "CREATE VIEW"):
            assert forbidden not in sql, f"migration must not {forbidden}"


class TestValidateSchema:
    """Memory-column checks, with the capture_state check isolated out."""

    @pytest.fixture(autouse=True)
    def _skip_capture_state(self):
        with patch('src.db.connection._validate_capture_state'):
            yield

    def test_passes_on_correct_columns(self):
        from src.db import connection

        ctx = _cursor_ctx(GOOD_COLUMNS)
        with patch('src.db.connection.get_db_cursor', return_value=ctx):
            connection.validate_schema()

    def test_missing_column_raises(self):
        from src.db.connection import SchemaMismatchError, validate_schema

        ctx = _cursor_ctx([GOOD_COLUMNS[0]])
        with patch('src.db.connection.get_db_cursor', return_value=ctx):
            with pytest.raises(SchemaMismatchError, match="openbrain migrate"):
                validate_schema()

    def test_wrong_type_raises(self):
        """IF NOT EXISTS silently accepts a pre-existing column of the wrong
        type, so existence alone is not validation."""
        from src.db.connection import SchemaMismatchError, validate_schema

        wrong = [dict(GOOD_COLUMNS[0], data_type="bigint"), GOOD_COLUMNS[1]]
        ctx = _cursor_ctx(wrong)
        with patch('src.db.connection.get_db_cursor', return_value=ctx):
            with pytest.raises(SchemaMismatchError, match="bigint"):
                validate_schema()

    def test_wrong_nullability_raises(self):
        from src.db.connection import SchemaMismatchError, validate_schema

        wrong = [dict(GOOD_COLUMNS[0], is_nullable="YES"), GOOD_COLUMNS[1]]
        ctx = _cursor_ctx(wrong)
        with patch('src.db.connection.get_db_cursor', return_value=ctx):
            with pytest.raises(SchemaMismatchError, match="nullable"):
                validate_schema()

    def test_wrong_default_raises(self):
        from src.db.connection import SchemaMismatchError, validate_schema

        wrong = [dict(GOOD_COLUMNS[0], column_default=None), GOOD_COLUMNS[1]]
        ctx = _cursor_ctx(wrong)
        with patch('src.db.connection.get_db_cursor', return_value=ctx):
            with pytest.raises(SchemaMismatchError, match="default"):
                validate_schema()


class TestBarrierNotSwallowed:
    """A schema mismatch must stop the servers, not leave them running-broken."""

    def test_mcp_propagates_schema_error(self):
        """init_server catches Exception and continues with _DB_READY=False.
        SchemaMismatchError must escape that handler."""
        import src.main as mcp

        with patch('src.main.connection.init_db',
                   side_effect=mcp.SchemaMismatchError("run openbrain migrate")):
            with pytest.raises(mcp.SchemaMismatchError):
                mcp.init_server()

    def test_migrate_is_exempt_from_the_barrier(self):
        """The one command that fixes the problem must not be blocked by it."""
        from src.cli import migrate

        assert migrate.SKIP_SCHEMA_BARRIER is True


CAPTURE_STATE_ROWS = [
    {"column_name": "id", "data_type": "smallint", "is_nullable": "NO"},
    {"column_name": "generation", "data_type": "bigint", "is_nullable": "NO"},
    {"column_name": "previous_generation", "data_type": "bigint", "is_nullable": "YES"},
    {"column_name": "committed_work_identity", "data_type": "jsonb", "is_nullable": "YES"},
    {"column_name": "reprocess_counter", "data_type": "bigint", "is_nullable": "NO"},
    {"column_name": "active_reprocess", "data_type": "jsonb", "is_nullable": "YES"},
    {"column_name": "updated_at", "data_type": "timestamp without time zone", "is_nullable": "NO"},
]


class TestValidateCaptureState:
    """The restore interlock reads a generation from this table, so a wrong
    shape or a wrong row count makes that comparison meaningless while
    looking healthy."""

    def _run(self, columns, row_count=1):
        from src.db import connection

        cols_ctx = _cursor_ctx(columns)
        count_ctx = _cursor_ctx()
        count_ctx.fetchone.return_value = {"n": row_count}

        with patch('src.db.connection.get_db_cursor',
                   side_effect=[cols_ctx, count_ctx]):
            connection._validate_capture_state()

    def test_passes_on_correct_shape(self):
        self._run(CAPTURE_STATE_ROWS)

    def test_missing_table_raises(self):
        from src.db.connection import SchemaMismatchError

        with pytest.raises(SchemaMismatchError, match="openbrain migrate"):
            self._run([])

    def test_missing_reprocess_column_raises(self):
        """Task 9 allocates reprocess generations here; without the column it
        cannot, and the failure must be loud."""
        from src.db.connection import SchemaMismatchError

        without = [c for c in CAPTURE_STATE_ROWS
                   if c["column_name"] != "reprocess_counter"]
        with pytest.raises(SchemaMismatchError, match="reprocess_counter"):
            self._run(without)

    def test_wrong_type_raises(self):
        from src.db.connection import SchemaMismatchError

        wrong = [dict(c, data_type="text") if c["column_name"] == "generation" else c
                 for c in CAPTURE_STATE_ROWS]
        with pytest.raises(SchemaMismatchError, match="generation"):
            self._run(wrong)

    def test_nullable_generation_raises(self):
        from src.db.connection import SchemaMismatchError

        wrong = [dict(c, is_nullable="YES") if c["column_name"] == "generation" else c
                 for c in CAPTURE_STATE_ROWS]
        with pytest.raises(SchemaMismatchError, match="NOT NULL"):
            self._run(wrong)

    @pytest.mark.parametrize("count", [0, 2])
    def test_row_count_must_be_exactly_one(self, count):
        """A CHECK(id=1) without a primary key permits many rows all at id=1."""
        from src.db.connection import SchemaMismatchError

        with pytest.raises(SchemaMismatchError, match="exactly one row"):
            self._run(CAPTURE_STATE_ROWS, row_count=count)


@pytest.mark.integration
class TestCaptureStateIsASingleton:
    def test_primary_key_rejects_a_second_row(self, integration_db):
        """CHECK alone would allow it; the PRIMARY KEY is what forbids it."""
        import psycopg2

        with pytest.raises(psycopg2.errors.UniqueViolation):
            with integration_db.get_db_cursor() as cursor:
                cursor.execute("INSERT INTO capture_state (id) VALUES (1)")

    def test_check_rejects_another_id(self, integration_db):
        import psycopg2

        with pytest.raises(psycopg2.errors.CheckViolation):
            with integration_db.get_db_cursor() as cursor:
                cursor.execute("INSERT INTO capture_state (id) VALUES (2)")
