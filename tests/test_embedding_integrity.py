"""Tests for embedding validation and stale-embedding prevention."""
import os
import sys
import uuid
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
