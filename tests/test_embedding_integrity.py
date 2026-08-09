"""Tests for embedding validation and stale-embedding prevention."""
import os
import sys
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
