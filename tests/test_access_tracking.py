"""Tests for access tracking: what counts, what must not, and what it costs."""
import uuid
from unittest.mock import Mock, patch

import pytest


def _cursor_ctx(rowcount=1):
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.rowcount = rowcount
    return ctx


class TestRecordAccess:
    def test_casts_the_id_array_to_uuid(self):
        """A Python list of str adapts as text[] and raises
        `operator does not exist: uuid = text`. Because the caller swallows
        exceptions, without the cast tracking would silently never work."""
        from src.db import queries

        ctx = _cursor_ctx()
        with patch('src.db.queries.get_db_cursor', return_value=ctx):
            queries.record_access([uuid.uuid4()])

        sql = ctx.execute.call_args[0][0]
        assert "::uuid[]" in sql
        assert "access_count = access_count + 1" in sql
        assert "last_accessed_at" in sql

    def test_empty_id_list_issues_no_statement(self):
        from src.db import queries

        ctx = _cursor_ctx()
        with patch('src.db.queries.get_db_cursor', return_value=ctx) as mock:
            queries.record_access([])

        mock.assert_not_called()

    def test_failure_is_swallowed(self):
        """The counter is an optimisation; the read is the product."""
        from src.db import queries

        with patch('src.db.queries.get_db_cursor', side_effect=RuntimeError("db down")):
            queries.record_access([uuid.uuid4()])  # must not raise

    def test_disabled_by_kill_switch(self):
        from src.config.ranking import RankingConfig
        from src.db import queries

        cfg = RankingConfig(config_data={"access_tracking": {"enabled": False}})
        ctx = _cursor_ctx()
        with patch('src.db.queries.RankingConfig.get_instance', return_value=cfg), \
             patch('src.db.queries.get_db_cursor', return_value=ctx) as mock:
            queries.record_access([uuid.uuid4()])

        mock.assert_not_called()


class TestWhatCounts:
    async def test_memory_get_records_access(self):
        import src.main as mcp

        mid = uuid.uuid4()
        with patch('src.main.get_memory_by_id', return_value={"id": mid, "content": "x"}), \
             patch('src.main.record_access') as mock_record:
            await mcp.handle_memory_get({"memory_id": str(mid)})

        mock_record.assert_called_once()
        assert list(mock_record.call_args[0][0]) == [mid]

    async def test_memory_search_does_not_record_access(self):
        """Appearing in a result list is not evidence a memory was used, and
        counting it would let search reinforce its own results. It also keeps
        memory_search genuinely read-only for hosts that auto-approve on that
        annotation."""
        import src.main as mcp

        with patch('src.main.search_memories', return_value=[]), \
             patch('src.main.create_validated_embedding', return_value=[0.1] * 1024), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.record_access') as mock_record:
            await mcp.handle_memory_search({"query": "q"})

        mock_record.assert_not_called()


class TestAnnotations:
    def test_memory_get_is_not_read_only_and_not_idempotent(self):
        """It mutates a counter that feeds ranking, so both hints flip -
        repeated calls change state."""
        import src.main as mcp

        tool = next(t for t in mcp.TOOLS if t.name == "memory_get")
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.idempotentHint is False

    def test_memory_search_stays_read_only(self):
        """Hosts auto-approve on this hint; a prompting search would be a
        serious regression for the tool agents call constantly."""
        import src.main as mcp

        tool = next(t for t in mcp.TOOLS if t.name == "memory_search")
        assert tool.annotations.readOnlyHint is True


@pytest.mark.integration
class TestAccessIncrementsForReal:
    """The ::uuid[] cast is the kind of thing that fails only against a real
    database, and record_access swallows exceptions - so without this test a
    broken cast would disable tracking silently and permanently."""

    def test_counter_actually_increments(self, integration_db):
        from src.db import queries

        try:
            mid = queries.insert_memory(
                source="test", content="access tracking probe", embedding=None,
            )
            assert queries.get_memory_by_id(mid)["access_count"] == 0

            queries.record_access([mid])
            assert queries.get_memory_by_id(mid)["access_count"] == 1

            queries.record_access([mid])
            row = queries.get_memory_by_id(mid)
            assert row["access_count"] == 2
            assert row["last_accessed_at"] is not None
        finally:
            with integration_db.get_db_cursor() as cursor:
                cursor.execute("DELETE FROM memory WHERE source = 'test'")
