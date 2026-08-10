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

    def test_creates_no_other_schema_object(self):
        """backup.py fails on any TOC entry outside its 19-entry allowlist,
        so a ledger table or helper function would break every backup."""
        from src.cli import migrate

        ctx = _cursor_ctx()
        with patch('src.cli.migrate.get_db_cursor', return_value=ctx):
            migrate.apply_migration()

        sql = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list).upper()
        for forbidden in ("CREATE TABLE", "CREATE FUNCTION", "CREATE SEQUENCE",
                          "CREATE INDEX"):
            assert forbidden not in sql, f"migration must not {forbidden}"


class TestValidateSchema:
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
