"""Tests for tag provenance and the tags a content update must not destroy."""
import json
import uuid
from unittest.mock import Mock, patch

import pytest


def _cursor_ctx(row=None, rowcount=1):
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.fetchone.return_value = row
    ctx.rowcount = rowcount
    return ctx


class TestExplicitTagsAreUserOwned:
    """Explicit submission is user intent and must be recorded as such.

    Preserving an old automatic provenance for a tag the user deliberately
    re-submitted launders a protected tag into a removable one: retag is then
    entitled to delete it.
    """

    def test_resubmitted_tag_becomes_user_provenance(self):
        from src.db import queries

        ctx = _cursor_ctx(row={"tag_sources": {"go": "entity", "note": "user"}})
        with patch('src.db.queries.get_db_cursor', return_value=ctx):
            queries.update_memory_tags(uuid.uuid4(), ["go", "note"])

        written = json.loads(ctx.execute.call_args[0][1][1])
        assert written["go"] == "user", (
            "a tag the user explicitly submitted must be user-owned, not entity"
        )
        assert written["note"] == "user"


class TestContentUpdatePreservesUserTags:
    """The live bug: updating content wiped every hand-added tag."""

    def _merge(self, stored_tags, stored_sources, recomputed):
        from src.db import queries

        ctx = _cursor_ctx(row={"tags": stored_tags, "tag_sources": stored_sources})
        with patch('src.db.queries.get_db_cursor', return_value=ctx):
            return queries.merge_tags_for_content_update(
                uuid.uuid4(), recomputed,
            )

    def test_user_tag_survives_a_content_update(self):
        merged_tags, merged_sources = self._merge(
            ["mytag", "auto"], {"mytag": "user", "auto": "default"},
            {"python": "keyword"},
        )

        assert "mytag" in merged_tags, "a user tag must survive a content update"
        assert merged_sources["mytag"] == "user"
        assert "python" in merged_tags, "recomputed automatic tags still apply"

    def test_unknown_provenance_tag_survives(self):
        """Rows predating provenance tracking must fail closed."""
        merged_tags, merged_sources = self._merge(
            ["legacy"], {}, {"python": "keyword"},
        )

        assert "legacy" in merged_tags
        assert merged_sources["legacy"] == "user"

    def test_stale_automatic_tags_are_dropped(self):
        merged_tags, _ = self._merge(
            ["oldkw"], {"oldkw": "keyword"}, {"python": "keyword"},
        )

        assert "oldkw" not in merged_tags, (
            "an automatic tag the recomputation no longer produces should go"
        )

    def test_protected_provenance_wins_a_collision(self):
        """stored python:user + recomputed python:keyword must stay user."""
        _tags, merged_sources = self._merge(
            ["python"], {"python": "user"}, {"python": "keyword"},
        )

        assert merged_sources["python"] == "user", (
            "letting the automatic value win downgrades a user tag, and retag "
            "would then be entitled to remove it"
        )

    def test_reads_under_row_lock(self):
        """Read-modify-write races an explicit tag update without one."""
        from src.db import queries

        ctx = _cursor_ctx(row={"tags": [], "tag_sources": {}})
        with patch('src.db.queries.get_db_cursor', return_value=ctx):
            queries.merge_tags_for_content_update(uuid.uuid4(), {"a": "keyword"})

        sql = ctx.execute.call_args_list[0][0][0]
        assert "FOR UPDATE" in sql


class TestMcpUpdateHandler:
    async def test_content_update_without_tags_keeps_user_tags(self):
        """End-to-end reproduction of the live bug through the MCP handler."""
        import src.main as mcp

        merged = (["mytag", "python"], {"mytag": "user", "python": "keyword"})

        with patch('src.main.get_memory_by_id', return_value={"source": "cli"}), \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"python": "keyword"}), \
             patch('src.main.merge_tags_for_content_update', return_value=merged), \
             patch('src.main.create_validated_embedding', return_value=[0.1] * 1024), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.update_memory', return_value=True) as mock_update:
            await mcp.handle_memory_update({
                "memory_id": str(uuid.uuid4()), "content": "new content",
            })

        kwargs = mock_update.call_args.kwargs
        assert "mytag" in kwargs["tags"], "content update destroyed a user tag"
        assert kwargs["tag_sources"]["mytag"] == "user"

    async def test_explicit_tags_still_replace(self):
        """Passing tags explicitly is still a replacement, not a merge."""
        import src.main as mcp

        with patch('src.main.get_memory_by_id', return_value={"source": "cli"}), \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"only": "user"}), \
             patch('src.main.merge_tags_for_content_update') as mock_merge, \
             patch('src.main.create_validated_embedding', return_value=[0.1] * 1024), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.update_memory', return_value=True) as mock_update:
            await mcp.handle_memory_update({
                "memory_id": str(uuid.uuid4()), "content": "new", "tags": ["only"],
            })

        mock_merge.assert_not_called()
        assert mock_update.call_args.kwargs["tags"] == ["only"]
