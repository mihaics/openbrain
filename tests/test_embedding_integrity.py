"""Tests for embedding validation and stale-embedding prevention."""
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import Mock, patch

import pytest


class TestCreateValidatedEmbedding:
    """create_validated_embedding rejects unusable vectors."""

    def test_returns_valid_embedding(self):
        from src.embedder import create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 1024):
            result = create_validated_embedding("text", expected_dim=1024)

        assert result == [0.1] * 1024

    def test_rejects_all_zero_vector(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.0] * 1024):
            with pytest.raises(EmbeddingError, match="all-zero"):
                create_validated_embedding("text", expected_dim=1024)

    def test_rejects_empty_embedding(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[]):
            with pytest.raises(EmbeddingError, match="empty"):
                create_validated_embedding("text", expected_dim=1024)

    def test_rejects_dimension_mismatch(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 768):
            with pytest.raises(EmbeddingError, match="dimension"):
                create_validated_embedding("text", expected_dim=1024)

    def test_skips_dimension_check_when_unknown(self):
        from src.embedder import create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 768):
            result = create_validated_embedding("text", expected_dim=None)

        assert len(result) == 768

    def test_propagates_provider_failure_as_embedding_error(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', side_effect=RuntimeError("ollama down")):
            with pytest.raises(EmbeddingError, match="ollama down"):
                create_validated_embedding("text", expected_dim=1024)


class TestEmbedBatchNullPropagation:
    """embed_batch yields None for failed items, never a zero vector."""

    def test_failed_item_becomes_none(self):
        from src.embedder import EmbedderConfig, OllamaEmbedder

        config = Mock(spec=EmbedderConfig)
        config.dimensions = 1024
        config.model = "qwen3-embedding:0.6b"
        config.ollama_base_url = "http://localhost:11434"

        embedder = OllamaEmbedder(config)

        with patch.object(embedder, 'embed', side_effect=[[0.1] * 1024, RuntimeError("boom")]):
            result = embedder.embed_batch(["ok", "fails"])

        assert result[0] == [0.1] * 1024
        assert result[1] is None, "failed item must be None, never a zero vector"

    def test_never_emits_zero_vector(self):
        from src.embedder import EmbedderConfig, OllamaEmbedder

        config = Mock(spec=EmbedderConfig)
        config.dimensions = 1024
        config.model = "qwen3-embedding:0.6b"
        config.ollama_base_url = "http://localhost:11434"

        embedder = OllamaEmbedder(config)

        with patch.object(embedder, 'embed', side_effect=RuntimeError("boom")):
            result = embedder.embed_batch(["fails"])

        assert result == [None]
        assert [0.0] * 1024 not in result


class TestUpdateMemorySentinel:
    """update_memory distinguishes 'leave alone' from 'clear'."""

    def _mock_cursor(self, mock_get_cursor):
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.rowcount = 1
        mock_get_cursor.return_value = ctx
        return ctx

    @patch('src.db.queries.get_db_cursor')
    def test_none_embedding_sets_null(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        queries.update_memory(uuid.uuid4(), content="new", embedding=None)

        sql = ctx.execute.call_args[0][0]
        assert "embedding = NULL" in sql

    @patch('src.db.queries.get_db_cursor')
    def test_unset_embedding_omits_column(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        queries.update_memory(uuid.uuid4(), tags=["a"])

        sql = ctx.execute.call_args[0][0]
        assert "embedding" not in sql

    @patch('src.db.queries.get_db_cursor')
    def test_content_without_embedding_decision_raises(self, mock_get_cursor):
        from src.db import queries

        self._mock_cursor(mock_get_cursor)
        with pytest.raises(ValueError, match="embedding"):
            queries.update_memory(uuid.uuid4(), content="new")

    @patch('src.db.queries.get_db_cursor')
    def test_tags_only_update_allowed(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        assert queries.update_memory(uuid.uuid4(), tags=["a"], importance=0.7) is True


class TestValidateEmbedding:
    """Database-layer backstop rejects zero vectors."""

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_rejects_all_zero(self, _mock_dim):
        from src.db import queries

        with pytest.raises(ValueError, match="zero"):
            queries._validate_embedding([0.0] * 1024)

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_rejects_wrong_dimension(self, _mock_dim):
        from src.db import queries

        with pytest.raises(ValueError, match="dimension"):
            queries._validate_embedding([0.1] * 768)

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_accepts_valid(self, _mock_dim):
        from src.db import queries

        assert queries._validate_embedding([0.1] * 1024) == [0.1] * 1024

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_passes_none_through(self, _mock_dim):
        from src.db import queries

        assert queries._validate_embedding(None) is None

    @patch('src.db.queries.get_db_cursor')
    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_search_path_still_validates(self, _mock_dim, mock_get_cursor):
        """search_memories used _validate_embedding_dim at line 170.

        The rename must not leave a NameError on the vector-search path, which
        no existing test covers because they all pass embedding=None.
        """
        from src.db import queries

        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.fetchall.return_value = []
        mock_get_cursor.return_value = ctx

        results = queries.search_memories("q", embedding=[0.1] * 1024, limit=5)
        assert results == []


class TestCallSiteDegradation:
    """Every embedding call site degrades instead of losing data."""

    def test_cli_store_stores_null_on_embedding_error(self):
        from src.cli.store import store_memory_cmd
        from src.embedder import EmbeddingError

        args = Mock()
        args.content = "content"
        args.source = "cli"
        args.tag = []
        args.importance = 0.5

        with patch('src.cli.store.create_validated_embedding',
                   side_effect=EmbeddingError("down")), \
             patch('src.cli.store.insert_memory', return_value=uuid.uuid4()) as mock_insert, \
             patch('src.cli.store.get_vector_dim', return_value=1024):
            rc = store_memory_cmd(args)

        assert rc == 0
        assert mock_insert.call_args.kwargs['embedding'] is None

    def test_cli_search_degrades_to_text_only(self):
        from src.cli.search import search_memories_cmd
        from src.embedder import EmbeddingError

        args = Mock()
        args.query = "q"
        args.limit = 5
        args.source = None
        args.tag = None
        args.json = True

        with patch('src.cli.search.create_validated_embedding',
                   side_effect=EmbeddingError("down")), \
             patch('src.cli.search.search_memories', return_value=[]) as mock_search, \
             patch('src.cli.search.get_vector_dim', return_value=1024):
            rc = search_memories_cmd(args)

        assert rc == 0
        assert mock_search.call_args.kwargs['embedding'] is None

    @pytest.mark.parametrize("module", [
        "src.connectors.gmail",
        "src.connectors.file_watcher",
        "src.connectors.claude_code",
        "src.connectors.whatsapp",
        "src.connectors.telegram",
    ])
    def test_connectors_import_validated_helper(self, module):
        """Every connector must route through the validated helper.

        A grep-style assertion rather than a behavioral one: it catches an
        import reverting to the raw embedder, but not a connector that routes
        through the helper and then throws the row away on failure. See
        TestConnectorDegradesOnEmbeddingError below for the behavioral check.
        """
        import importlib
        mod = importlib.import_module(module)
        assert hasattr(mod, 'create_validated_embedding'), (
            f"{module} must import create_validated_embedding"
        )
        assert not hasattr(mod, 'create_embedding'), (
            f"{module} still imports the unvalidated create_embedding"
        )


def _gmail_driver(tmp_path):
    from src.connectors.gmail import GmailConnector

    connector = GmailConnector(str(tmp_path))
    connector._process_email_content(
        "email body content", "a@b.com", "Subject line", "2024-01-01"
    )


def _file_watcher_driver(tmp_path):
    from src.connectors.file_watcher import FileWatcherConnector

    md_file = tmp_path / "note.md"
    md_file.write_text("# Title\n\nSome body text.")
    connector = FileWatcherConnector(str(tmp_path))
    connector.import_file(md_file)


def _claude_code_driver(tmp_path):
    from src.connectors.claude_code import ClaudeCodeConnector

    connector = ClaudeCodeConnector(str(tmp_path))
    connector._process_message(
        {"role": "user", "content": "hello"}, "sess-1", "Session", "2024-01-01"
    )


def _whatsapp_driver(tmp_path):
    from src.connectors.whatsapp import WhatsAppConnector

    connector = WhatsAppConnector(str(tmp_path))
    connector._process_message({
        "date": datetime.now(), "sender": "Bob", "text": "hello",
    })


def _telegram_driver(tmp_path):
    from src.connectors.telegram import TelegramConnector

    connector = TelegramConnector(str(tmp_path))
    connector._process_message({
        "text": "hello", "from": "Bob", "date": "2024-01-01",
    })


class TestConnectorDegradesOnEmbeddingError:
    """Every connector stores the row with a NULL embedding on EmbeddingError,
    instead of silently dropping it.

    Behavioral, not structural: this drives each connector's actual
    processing path with create_validated_embedding patched to raise, and
    asserts insert_memory still fires with embedding=None. Mutating any of
    these except-blocks to `return` early (dropping the row) must fail this
    test - unlike test_connectors_import_validated_helper above, which only
    checks the import shape.
    """

    @pytest.mark.parametrize("module,driver", [
        ("src.connectors.gmail", _gmail_driver),
        ("src.connectors.file_watcher", _file_watcher_driver),
        ("src.connectors.claude_code", _claude_code_driver),
        ("src.connectors.whatsapp", _whatsapp_driver),
        ("src.connectors.telegram", _telegram_driver),
    ])
    def test_connector_stores_null_on_embedding_error(self, module, driver, tmp_path):
        import importlib
        mod = importlib.import_module(module)

        # Same identity rule as the MCP/REST tests: take EmbeddingError from
        # the module under test, since each connector imports it directly
        # (`from ..embedder import EmbeddingError, ...`).
        with patch(f'{module}.create_validated_embedding',
                   side_effect=mod.EmbeddingError("down")), \
             patch(f'{module}.insert_memory', return_value=uuid.uuid4()) as mock_insert, \
             patch(f'{module}.extract_entities', return_value={}), \
             patch(f'{module}.auto_tag', return_value={"auto": "auto"}), \
             patch(f'{module}.get_vector_dim', return_value=1024):
            driver(tmp_path)

        mock_insert.assert_called_once()
        assert mock_insert.call_args.kwargs['embedding'] is None, (
            f"{module}: a failed embedding must store the row with a NULL "
            f"embedding, not drop it"
        )


class TestMcpDegradation:
    """MCP handlers degrade rather than failing or keeping stale vectors."""

    async def test_store_stores_null_on_zero_vector(self):
        """A zero vector from the provider must yield a stored memory with a
        NULL embedding and embedded: false - never an exception."""
        import json as jsonlib
        import src.main as mcp

        # src.main does `from embedder import ...`, so its EmbeddingError is
        # the top-level `embedder` class - NOT src.embedder's. They are
        # distinct classes once both identities load (verified), so raising
        # src.embedder.EmbeddingError would sail past main's except clause.
        # Always take the class from the module under test.
        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("all-zero embedding")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.insert_memory', return_value=uuid.uuid4()) as mock_insert, \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"auto": "auto"}):
            result = await mcp.handle_memory_store({
                "content": "text", "source": "test",
            })

        payload = jsonlib.loads(result[0].text)
        assert payload["embedded"] is False
        assert mock_insert.call_args.kwargs['embedding'] is None

    async def test_update_clears_stale_embedding_on_failure(self):
        """The original bug: a failed re-embed left the old vector attached to
        new content."""
        import src.main as mcp

        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("down")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.update_memory', return_value=True) as mock_update, \
             patch('src.main.get_memory_by_id', return_value={"source": "test"}), \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"auto": "auto"}), \
             patch('src.main.merge_tags_for_content_update',
                   return_value=(["auto"], {"auto": "auto"})):
            await mcp.handle_memory_update({
                "memory_id": str(uuid.uuid4()), "content": "new content",
            })

        assert mock_update.call_args.kwargs.get('embedding', 'MISSING') is None, (
            "a failed re-embed must clear the stale vector, not keep it"
        )

    async def test_search_degrades_to_text_only(self):
        import src.main as mcp

        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("down")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.search_memories', return_value=[]) as mock_search:
            await mcp.handle_memory_search({"query": "q"})

        assert mock_search.call_args.kwargs['embedding'] is None


class TestRestDegradation:
    """The REST endpoints must degrade, never lose a row or a search."""

    async def test_update_reembeds_and_retags(self):
        from src.api.main import MemoryUpdate, update_memory_endpoint

        mid = str(uuid.uuid4())
        with patch('src.api.main.create_validated_embedding',
                   return_value=[0.1] * 1024), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.extract_entities', return_value={"urls": []}), \
             patch('src.api.main.auto_tag', return_value={"auto": "auto"}), \
             patch('src.api.main.update_memory', return_value=True) as mock_update:
            await update_memory_endpoint(mid, MemoryUpdate(
                content="new", tags=["a"], source="web", importance=0.5,
            ))

        kwargs = mock_update.call_args.kwargs
        assert kwargs['embedding'] == [0.1] * 1024, "REST update must re-embed"
        assert kwargs['entities'] == {"urls": []}, "and re-extract entities"
        assert "auto" in kwargs['tags'], "and re-tag"

    async def test_update_clears_embedding_on_failure(self):
        import src.api.main as api
        from src.api.main import MemoryUpdate, update_memory_endpoint

        # Same identity trap as the MCP tests: api/main.py imports embedder
        # top-level, so take the exception class from the module under test.
        mid = str(uuid.uuid4())
        with patch('src.api.main.create_validated_embedding',
                   side_effect=api.EmbeddingError("down")), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.extract_entities', return_value={}), \
             patch('src.api.main.auto_tag', return_value={"auto": "auto"}), \
             patch('src.api.main.update_memory', return_value=True) as mock_update:
            await update_memory_endpoint(mid, MemoryUpdate(
                content="new", tags=["a"], source="web", importance=0.5,
            ))

        assert mock_update.call_args.kwargs['embedding'] is None

    async def test_create_memory_stores_null_on_embedding_error(self):
        """POST /memories must not drop the row when embedding fails."""
        import src.api.main as api
        from src.api.main import MemoryCreate, create_memory

        mock_tagger = Mock()
        mock_tagger.tag.return_value = {"auto": "auto"}

        with patch('src.api.main.create_validated_embedding',
                   side_effect=api.EmbeddingError("down")), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.extract_entities', return_value={}), \
             patch('src.api.main.get_tagger', return_value=mock_tagger), \
             patch('src.api.main.insert_memory', return_value=uuid.uuid4()) as mock_insert:
            result = await create_memory(MemoryCreate(content="new memory", source="api"))

        assert result["status"] == "stored"
        assert mock_insert.call_args.kwargs['embedding'] is None

    async def test_search_endpoint_degrades_to_text_only(self):
        """POST /memories/search must fall back to text-only, never raise."""
        import src.api.main as api
        from src.api.main import SearchRequest, search_memories_endpoint

        with patch('src.api.main.create_validated_embedding',
                   side_effect=api.EmbeddingError("down")), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.search_memories', return_value=[]) as mock_search:
            result = await search_memories_endpoint(SearchRequest(query="q"))

        assert result == []
        assert mock_search.call_args.kwargs['embedding'] is None
