"""Tests for the reembed sweep."""
import os
import sys
import uuid
from unittest.mock import Mock, call, patch

import pytest


def _cursor_ctx(rowcount=1):
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.rowcount = rowcount
    return ctx


class TestZeroVectorRepair:
    def test_zero_vectors_nulled_before_replacement(self):
        from src.cli import reembed
        from src.embedder import EmbeddingError

        row_id = uuid.uuid4()
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [
            [{'id': row_id, 'content': 'text'}],   # zero-vector rows
            [],                                     # NULL rows
        ]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=EmbeddingError("ollama down")), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        executed = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list)
        assert "embedding = NULL" in executed, (
            "a zero vector must be NULLed even when replacement fails"
        )
        assert report.zeros_repaired == 1


class TestOptimisticGuard:
    def test_content_changed_is_skipped_not_written(self):
        from src.cli import reembed

        row_id = uuid.uuid4()
        ctx = _cursor_ctx(rowcount=0)   # UPDATE matched nothing
        ctx.fetchall.side_effect = [[], [{'id': row_id, 'content': 'old'}]]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   return_value=[0.1] * 1024), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert report.skipped_changed == 1
        assert report.reembedded == 0


class TestAbortBehavior:
    def test_aborts_after_three_consecutive_failures(self):
        from src.cli import reembed
        from src.embedder import EmbeddingError

        rows = [{'id': uuid.uuid4(), 'content': f'c{i}'} for i in range(10)]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=EmbeddingError("down")) as mock_embed, \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert mock_embed.call_count == 3, "must stop after 3 consecutive failures"
        assert report.reembedded == 0
        assert report.failed == 3
        report.check_invariant()


class TestSkipIds:
    def test_skipped_ids_excluded_from_both_phases(self):
        from src.cli import reembed

        skipped = uuid.uuid4()
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], []]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            reembed.run_sweep(limit=10, dry_run=False, skip_ids=[str(skipped)])

        for call_args in ctx.execute.call_args_list:
            sql, params = call_args[0][0], call_args[0][1]
            if "SELECT" in sql:
                assert "NOT (id = ANY(" in sql
                assert any(
                    isinstance(pv, list) and str(skipped) in pv for pv in params
                ), "skipped id must reach both selection queries"

    def test_rejects_malformed_skip_ids(self):
        from src.cli.reembed import _parse_skip_ids

        with pytest.raises(ValueError):
            _parse_skip_ids("not-a-uuid")

    def test_parses_and_normalizes(self):
        from src.cli.reembed import _parse_skip_ids

        u = uuid.uuid4()
        assert _parse_skip_ids(f" {u} , ") == [str(u)]
        assert _parse_skip_ids("") == []


class TestReportInvariant:
    def test_dry_run_counts_selected_not_attempted(self):
        from src.cli import reembed

        rows = [{'id': uuid.uuid4(), 'content': 'c'}]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=True)

        assert report.selected == 1
        assert report.attempted == 0
        assert report.unprocessed == 1
        report.check_invariant()

    def test_invariant_holds_after_abort(self):
        from src.cli import reembed

        rows = [{'id': uuid.uuid4(), 'content': f'c{i}'} for i in range(10)]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        # reembed catches EmbeddingError specifically, so a generic Exception
        # escapes before a report is returned. Take the class from the module
        # under test - the same identity rule as the MCP tests.
        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=reembed.EmbeddingError("down")), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert report.aborted
        assert report.attempted + report.unprocessed == report.selected
        report.check_invariant()


@pytest.mark.integration
class TestZeroVectorQueryAgainstLivePgvector:
    """The one thing a mocked cursor cannot establish: that the SQL is valid.

    Deselected by default (see pyproject addopts); run with -m integration.
    Requires the live cluster.
    """

    def test_array_fill_comparison_is_valid_sql(self):
        from src.db.connection import get_db_cursor, get_vector_dim

        dim = get_vector_dim()
        assert dim, "could not read embedding typmod from the live column"

        # Dimension comes from the live typmod, never a literal - the same
        # rule the production query follows.
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) AS n FROM memory
                WHERE embedding = array_fill(0, ARRAY[%s])::vector
                """,
                (dim,),
            )
            row = cursor.fetchone()

        count = row['n'] if isinstance(row, dict) else row[0]
        assert count >= 0


class TestSelectionOrder:
    def test_orders_by_created_at_then_id(self):
        from src.cli import reembed

        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], []]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            reembed.run_sweep(limit=10, dry_run=False)

        null_query = ctx.execute.call_args_list[-1][0][0]
        assert "ORDER BY created_at ASC, id ASC" in null_query
