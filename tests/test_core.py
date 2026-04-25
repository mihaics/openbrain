"""
Core tests for Open Brain.
"""
import json
import os
import subprocess
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


class TestCLI:
    """Test CLI entry points."""

    def _run_cli(self, *args):
        repo_root = os.path.join(os.path.dirname(__file__), '..')
        return subprocess.run(
            [sys.executable, '-m', 'src.cli', *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_help_does_not_import_command_modules(self):
        result = self._run_cli('--help')

        assert result.returncode == 0
        assert 'search,store,stats,import,report,serve,exec' in result.stdout

    def test_exec_help_includes_sandbox_options(self):
        result = self._run_cli('exec', '--help')

        assert result.returncode == 0
        assert '--sandbox' in result.stdout
        assert '--allow-network' in result.stdout

    def test_exec_direct_runs_command(self):
        result = self._run_cli('exec', 'printf cli-ok')

        assert result.returncode == 0
        assert result.stdout == 'cli-ok\n'


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
    
    def test_people_entity_does_not_auto_tag(self):
        """NER misfires made the `people` tag land on ~90% of memories —
        the blanket auto-tag is intentionally dropped."""
        from extractors.tagger import auto_tag
        tags = auto_tag("meeting with Alice and Bob", entities={'people': ['Alice', 'Bob']})
        assert 'people' not in tags

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

    def _config(self):
        from embedder import EmbedderConfig
        EmbedderConfig.reset()
        return EmbedderConfig.get_instance()

    @patch('requests.post')
    def test_embed_creation(self, mock_post):
        """Test embedding creation."""
        from embedder import OllamaEmbedder

        mock_response = Mock()
        mock_response.json.return_value = {'embedding': [0.1] * 768}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        embedder = OllamaEmbedder(self._config())
        embedding = embedder.embed("test text")

        assert len(embedding) == 768
        assert mock_post.called

    @patch('requests.post')
    def test_batch_embedding(self, mock_post):
        """Test batch embedding."""
        from embedder import OllamaEmbedder

        mock_response = Mock()
        mock_response.json.return_value = {'embedding': [0.1] * 768}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        embedder = OllamaEmbedder(self._config())
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


class TestTaggerWordBoundaries:
    """Regression tests for the substring-matching tagger bug."""

    def test_short_keyword_not_substring(self):
        from extractors.tagger import auto_tag
        # 'go' used to match 'good'
        assert 'go' not in auto_tag('this is a good day')
        # 'java' used to match 'javascript'
        tags = auto_tag('learning javascript today')
        assert 'javascript' in tags
        assert 'java' not in tags

    def test_keyword_matches_with_punctuation(self):
        from extractors.tagger import auto_tag
        assert 'nodejs' in auto_tag('built with node.js')
        assert 'cpp' in auto_tag('C++ is fun')
        assert 'kubernetes' in auto_tag('K8s cluster')


class TestSerialization:
    """Test _serialize_memory content-mode options."""

    def _sample(self):
        return {
            'id': uuid.uuid4(),
            'source': 'test',
            'content': 'a' * 400,
            'tags': ['x'],
            'tag_sources': {'x': 'user'},
            'entities': {},
            'importance': 0.5,
            'created_at': datetime.now(),
        }

    def test_full_mode_returns_full_content(self):
        from main import _serialize_memory
        out = _serialize_memory(self._sample(), content_mode='full')
        assert out['content'] == 'a' * 400

    def test_snippet_mode_truncates(self):
        from main import _serialize_memory
        out = _serialize_memory(self._sample(), content_mode='snippet')
        assert len(out['content']) <= 241  # 240 + ellipsis
        assert out['content'].endswith('…')

    def test_none_mode_omits_content(self):
        from main import _serialize_memory
        out = _serialize_memory(self._sample(), content_mode='none')
        assert out['content'] is None

    def test_max_chars_caps_full(self):
        from main import _serialize_memory
        out = _serialize_memory(self._sample(), content_mode='full', content_max_chars=10)
        assert len(out['content']) <= 11

    def test_content_options_clamp_bad_max_chars(self):
        from main import _content_opts
        assert _content_opts({'content_max_chars': -100})['content_max_chars'] == 1

    def test_tag_sources_opt_in(self):
        from main import _serialize_memory
        default = _serialize_memory(self._sample())
        assert 'tag_sources' not in default
        with_sources = _serialize_memory(self._sample(), include_tag_sources=True)
        assert with_sources['tag_sources'] == {'x': 'user'}


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
        """Test memory stats aggregates dict rows from fetchone/fetchall."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx

        # get_memory_stats issues: fetchone, fetchall, fetchall, fetchone, fetchone
        mock_ctx.fetchone.side_effect = [
            {'total': 100},
            {'count': 25},
            {'count': 30},
        ]
        mock_ctx.fetchall.side_effect = [
            [{'source': 'test', 'count': 50}],
            [{'tag': 'python', 'count': 10}],
        ]

        stats = queries.get_memory_stats()
        assert stats['total'] == 100
        assert stats['by_source'] == {'test': 50}
        assert stats['top_tags'] == {'python': 10}
        assert stats['this_week'] == 25
        assert stats['this_month'] == 30

    @patch('db.queries.get_db_cursor')
    def test_get_related_min_similarity_pushed_to_sql(self, mock_cursor):
        """min_similarity should appear in the SQL, not as a post-filter."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchall.return_value = []

        queries.get_related_memories(uuid.uuid4(), limit=5, min_similarity=0.7)
        sql_text, params = mock_ctx.execute.call_args[0]
        assert ">= %s" in sql_text
        assert 0.7 in params

        # Without min_similarity the threshold clause must not be present.
        mock_ctx.execute.reset_mock()
        queries.get_related_memories(uuid.uuid4(), limit=5)
        sql_text2, _ = mock_ctx.execute.call_args[0]
        assert ">= %s" not in sql_text2

    @patch('db.queries.get_db_cursor')
    def test_trending_tags_uses_make_interval(self, mock_cursor):
        """Guard against regression to the fragile 'INTERVAL \\'%s weeks\\'' form."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchall.return_value = []

        queries.get_trending_tags(weeks=4, limit=10)
        sql_text, params = mock_ctx.execute.call_args[0]
        assert "make_interval(weeks => %s)" in sql_text
        assert params == (4, 10)

    @patch('db.queries.get_db_cursor')
    def test_count_memories(self, mock_cursor):
        """memory_count returns {count} for the filter set."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchone.return_value = {'c': 7}

        n = queries.count_memories(tags=['python'], importance_min=0.5)
        assert n == 7
        args, _ = mock_ctx.execute.call_args
        assert 'tags && %s' in args[0]
        assert 'importance >= %s' in args[0]

    @patch('db.queries.get_db_cursor')
    def test_memory_read_paths_decode_tag_sources(self, mock_cursor):
        """Opt-in provenance should work consistently for all memory read paths."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchall.return_value = [{
            'id': uuid.uuid4(),
            'source': 'test',
            'content': 'hello',
            'entities': '{"people": ["Ada"]}',
            'tags': ['x'],
            'tag_sources': '{"x": "user"}',
            'metadata': '{"k": "v"}',
            'importance': 0.5,
            'created_at': datetime.now(),
            'original_date': None,
            'language': None,
        }]

        memories = queries.get_today_memories()
        assert memories[0]['entities'] == {'people': ['Ada']}
        assert memories[0]['tag_sources'] == {'x': 'user'}
        assert memories[0]['metadata'] == {'k': 'v'}


class TestBulkDelete:
    """Regression tests for memory_bulk_delete safety + composition."""

    @patch('db.queries.get_db_cursor')
    def test_bulk_delete_requires_filter(self, mock_cursor):
        from db import queries
        with pytest.raises(ValueError):
            queries.bulk_delete_memories()

    @patch('db.queries.get_db_cursor')
    def test_bulk_delete_composes_filters(self, mock_cursor):
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.rowcount = 3

        n = queries.bulk_delete_memories(sources=['mcp'], tags_all=['deprecated'])
        assert n == 3
        sql_text, params = mock_ctx.execute.call_args[0]
        assert "source = ANY(%s)" in sql_text
        assert "tags @> %s" in sql_text
        assert params == (['mcp'], ['deprecated'])

    @patch('db.queries.get_db_cursor')
    def test_bulk_delete_by_ids_casts_to_uuid_array(self, mock_cursor):
        """Regression: without `::uuid[]`, Postgres rejects id = ANY(text[])."""
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.rowcount = 2

        ids = [uuid.uuid4(), uuid.uuid4()]
        queries.bulk_delete_memories(ids=ids)
        sql_text, params = mock_ctx.execute.call_args[0]
        assert "id = ANY(%s::uuid[])" in sql_text
        assert params == ([str(i) for i in ids],)

    def test_bulk_delete_handler_requires_boolean_true_confirm(self):
        import asyncio as _asyncio
        from main import handle_memory_bulk_delete

        result = _asyncio.run(handle_memory_bulk_delete({
            'confirm': 'true',
            'sources': ['mcp'],
        }))
        payload = json.loads(result[0].text)
        assert payload['error'] == 'memory_bulk_delete requires confirm=true'


class TestMcpArgumentValidation:
    def test_string_list_rejects_scalar(self):
        from main import _string_list
        with pytest.raises(ValueError):
            _string_list('mcp', 'sources')

    def test_string_list_strips_and_drops_empty_items(self):
        from main import _string_list
        assert _string_list([' mcp ', '', 'manual'], 'sources') == ['mcp', 'manual']


class TestFindDuplicates:
    """memory_find_duplicates query shape."""

    @patch('db.queries.get_db_cursor')
    def test_find_duplicates_scoped_by_source(self, mock_cursor):
        from db import queries

        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_ctx)
        mock_ctx.__exit__ = Mock(return_value=False)
        mock_cursor.return_value = mock_ctx
        mock_ctx.fetchall.return_value = []

        queries.find_duplicate_pairs(threshold=0.9, limit=10, sources=['mcp'])
        sql_text, params = mock_ctx.execute.call_args[0]
        assert "a.id < b.id" in sql_text
        assert ">= %s" in sql_text
        # threshold and limit are the last two params
        assert params[-2] == 0.9
        assert params[-1] == 10


class TestSerializationExtras:
    """Opt-in score / raw_content / language fields."""

    def _sample_with_extras(self):
        return {
            'id': uuid.uuid4(),
            'source': 'test',
            'content': 'hello',
            'raw_content': 'HELLO raw',
            'language': 'en',
            'tags': [],
            'tag_sources': {},
            'entities': {},
            'importance': 0.5,
            'created_at': datetime.now(),
            'score': 0.87,
        }

    def test_score_opt_in(self):
        from main import _serialize_memory
        assert 'score' not in _serialize_memory(self._sample_with_extras())
        out = _serialize_memory(self._sample_with_extras(), include_score=True)
        assert out['score'] == 0.87

    def test_language_always_present_when_set(self):
        from main import _serialize_memory
        out = _serialize_memory(self._sample_with_extras())
        assert out['language'] == 'en'

    def test_raw_opt_in(self):
        from main import _serialize_memory
        assert 'raw_content' not in _serialize_memory(self._sample_with_extras())
        out = _serialize_memory(self._sample_with_extras(), include_raw=True)
        assert out['raw_content'] == 'HELLO raw'


class TestImportanceClamp:
    def test_clamp_above_one(self):
        from main import _clamp_importance
        assert _clamp_importance(5, default=0.5) == 1.0

    def test_clamp_below_zero(self):
        from main import _clamp_importance
        assert _clamp_importance(-3, default=0.5) == 0.0

    def test_clamp_garbage_returns_default(self):
        from main import _clamp_importance
        assert _clamp_importance("nope", default=0.42) == 0.42


class TestEntityExtractorBoundaries:
    """Regression tests for entity extractor word-boundary behavior."""

    def test_tech_matches_with_punctuation(self):
        from extractors.entities import extract_entities
        ents = extract_entities("built with (python), plus docker.")
        assert 'python' in ents['technologies']
        assert 'docker' in ents['technologies']

    def test_tech_not_substring(self):
        from extractors.entities import extract_entities
        # 'javascript' should match but 'java' must NOT match inside it.
        ents = extract_entities("learning javascript today")
        assert 'javascript' in ents['technologies']
        assert 'java' not in ents['technologies']


class TestMcpToolSurface:
    """Ensure list_tools stays in sync with the dispatcher."""

    def test_tool_count_and_annotations(self):
        import asyncio as _asyncio
        from main import list_tools
        tools = _asyncio.run(list_tools())
        names = {t.name for t in tools}
        # New in this round of audit work.
        assert {"memory_bulk_delete", "memory_find_duplicates",
                "memory_activity_timeline", "memory_peak_hours"} <= names

        by_name = {t.name: t for t in tools}
        assert by_name["memory_delete"].annotations.destructiveHint is True
        assert by_name["memory_bulk_delete"].annotations.destructiveHint is True
        assert by_name["memory_search"].annotations.readOnlyHint is True
        assert by_name["memory_today"].annotations.readOnlyHint is True
        assert by_name["memory_weekly_report"].annotations.readOnlyHint is True

        # Entity-type enums should list the canonical types.
        et = by_name["memory_get_entity"].inputSchema["properties"]["entity_type"]
        assert "people" in et["enum"]
        assert "technologies" in et["enum"]


def run_tests():
    """Run all tests."""
    pytest.main([__file__, '-v'])


if __name__ == "__main__":
    run_tests()
