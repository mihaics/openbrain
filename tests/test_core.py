"""
Core tests for Open Brain.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestConfig:
    """Test configuration loading."""
    
    def test_load_settings(self):
        """Test that settings.yaml loads correctly."""
        import yaml
        
        config_path = os.path.join(
            os.path.dirname(__file__), 
            '..', 'config', 'settings.yaml'
        )
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        assert 'database' in config
        assert 'embedder' in config
        assert 'mcp' in config
        assert 'tags' in config


class TestEntityExtractor:
    """Test entity extraction."""
    
    def test_email_extraction(self):
        """Test email extraction."""
        from extractors.entities import extract_entities
        
        text = "Contact me at test@example.com"
        entities = extract_entities(text)
        
        assert 'test@example.com' in entities['emails']
    
    def test_url_extraction(self):
        """Test URL extraction."""
        from extractors.entities import extract_entities
        
        text = "Check out https://github.com/test/repo"
        entities = extract_entities(text)
        
        assert any('github.com' in url for url in entities['urls'])
    
    def test_hashtag_extraction(self):
        """Test hashtag extraction."""
        from extractors.entities import extract_entities
        
        text = "Great #python #ai project"
        entities = extract_entities(text)
        
        assert '#python' in entities['hashtags']
        assert '#ai' in entities['hashtags']
    
    def test_technology_extraction(self):
        """Test technology keyword extraction."""
        from extractors.entities import extract_entities
        
        text = "Built with Python and React"
        entities = extract_entities(text)
        
        assert 'python' in entities['technologies']
        assert 'react' in entities['technologies']


class TestTagger:
    """Test auto-tagging."""
    
    def test_keyword_tagging(self):
        """Test keyword-based tagging."""
        from extractors.tagger import auto_tag
        
        text = "Working on a Python bug fix"
        tags = auto_tag(text)
        
        assert 'python' in tags
        assert 'bug' in tags
    
    def test_pattern_tagging(self):
        """Test pattern-based tagging."""
        from extractors.tagger import auto_tag
        
        text = "How to fix this error?"
        tags = auto_tag(text)
        
        assert 'error' in tags
        assert 'question' in tags
    
    def test_user_tags(self):
        """Test user-provided tags."""
        from extractors.tagger import auto_tag
        
        text = "Some note"
        tags = auto_tag(text, user_tags=['important', 'review'])
        
        assert 'important' in tags
        assert 'review' in tags
    
    def test_deny_list(self):
        """Test deny list filtering."""
        from extractors.tagger import Tagger
        
        tagger = Tagger()
        
        # Should not include denied tags
        tags = tagger.tag("test", user_tags=['password', 'valid_tag'])
        
        assert 'password' not in tags
        assert 'valid_tag' in tags


class TestEmbedder:
    """Test embedder functionality."""
    
    @patch('requests.post')
    def test_embed_creation(self, mock_post):
        """Test embedding creation."""
        from embedder import OllamaEmbedder
        
        mock_response = Mock()
        mock_response.json.return_value = {
            'embedding': [0.1] * 768
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        embedder = OllamaEmbedder()
        embedding = embedder.embed("test text")
        
        assert len(embedding) == 768
        assert mock_post.called
    
    @patch('requests.post')
    def test_batch_embedding(self, mock_post):
        """Test batch embedding."""
        from embedder import OllamaEmbedder
        
        mock_response = Mock()
        mock_response.json.return_value = {
            'embedding': [0.1] * 768
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        embedder = OllamaEmbedder()
        embeddings = embedder.embed_batch(["text1", "text2"])
        
        assert len(embeddings) == 2
        assert len(embeddings[0]) == 768


class TestAnalytics:
    """Test analytics functions."""
    
    def test_trend_analyzer_init(self):
        """Test trend analyzer initialization."""
        from analytics.trends import TrendAnalyzer
        
        analyzer = TrendAnalyzer(weeks=4)
        
        assert analyzer.weeks == 4


class TestMemoryFormatting:
    """Test memory formatting functions."""
    
    def test_format_empty_list(self):
        """Test formatting empty memory list."""
        # Import from main module
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        
        from main import format_memory_list
        
        result = format_memory_list([])
        assert "No memories found" in result
    
    def test_format_memory_list(self):
        """Test formatting memory list."""
        from main import format_memory_list
        
        memories = [
            {
                'id': str(uuid.uuid4()),
                'source': 'test',
                'content': 'Test content',
                'tags': ['test'],
                'created_at': datetime.now()
            }
        ]
        
        result = format_memory_list(memories)
        
        assert 'ID:' in result
        assert 'Source: test' in result
        assert 'Test content' in result


class TestDatabaseQueries:
    """Test database query functions (with mocking)."""
    
    @patch('db.queries.get_db_cursor')
    def test_search_memories(self, mock_cursor):
        """Test memory search."""
        from db import queries
        
        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchall.return_value = []
        
        results = queries.search_memories("test", limit=5)
        
        assert isinstance(results, list)
    
    @patch('db.queries.get_db_cursor')
    def test_get_memory_stats(self, mock_cursor):
        """Test memory stats."""
        from db import queries
        
        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        
        mock_cursor.return_value = mock_ctx
        
        # Mock fetchone for different queries
        call_count = [0]
        def mock_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                return {'total': 100}
            elif call_count[0] == 2:
                return [{'source': 'test', 'count': 50}]
            elif call_count[0] == 3:
                return [{'tag': 'python', 'count': 10}]
            elif call_count[0] == 4:
                return {'count': 25}
            return {'count': 30}
        
        mock_ctx.fetchone.side_effect = mock_fetchone
        mock_ctx.fetchall.return_value = []
        
        stats = queries.get_memory_stats()
        
        assert stats['total'] == 100


def run_tests():
    """Run all tests."""
    pytest.main([__file__, '-v'])


if __name__ == "__main__":
    run_tests()
