"""Tests for the optional reranker seam."""
from unittest.mock import Mock, patch

import pytest


def _cfg(**over):
    from src.config.ranking import RankingConfig

    block = {"rerank": {"enabled": True, "url": "http://rr", "model": "m",
                        "top_n": 3}}
    block["rerank"].update(over)
    return RankingConfig(config_data=block)


def _candidates(n=5):
    return [{"id": i, "content": f"doc {i}", "score": 1.0 - i * 0.1} for i in range(n)]


def _response(results):
    r = Mock()
    r.json.return_value = {"results": results}
    r.raise_for_status = Mock()
    return r


class TestBypass:
    def test_disabled_returns_unchanged(self):
        from src.config.ranking import RankingConfig
        from src.rerank import rerank

        cands = _candidates()
        assert rerank("q", cands, RankingConfig(config_data={})) is cands

    def test_bypassed_without_a_text_query(self):
        """Vector-only and no-signal searches have nothing to send."""
        from src.rerank import rerank

        cands = _candidates()
        assert rerank("   ", cands, _cfg()) is cands


class TestOrdering:
    def test_score_is_authoritative_not_array_order(self):
        from src.rerank import rerank

        with patch('src.rerank.requests.post', return_value=_response([
            {"index": 0, "relevance_score": 0.1},
            {"index": 2, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.5},
        ])):
            out = rerank("q", _candidates(), _cfg())

        assert [c["id"] for c in out[:3]] == [2, 1, 0]

    def test_omitted_documents_keep_sql_order_and_are_appended(self):
        """The endpoint returns at most top_n and need not cover every doc."""
        from src.rerank import rerank

        with patch('src.rerank.requests.post', return_value=_response([
            {"index": 2, "relevance_score": 0.9},
        ])):
            out = rerank("q", _candidates(), _cfg())

        assert out[0]["id"] == 2
        assert [c["id"] for c in out[1:]] == [0, 1, 3, 4]

    def test_candidates_beyond_top_n_are_preserved(self):
        from src.rerank import rerank

        with patch('src.rerank.requests.post', return_value=_response([
            {"index": 0, "relevance_score": 0.5},
        ])):
            out = rerank("q", _candidates(5), _cfg())

        assert len(out) == 5, "reranking must not drop candidates"


class TestRejection:
    @pytest.mark.parametrize("results", [
        [{"index": 99, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 0, "relevance_score": float("nan")}],
        [{"index": 0, "relevance_score": float("inf")}],
        [{"index": 0, "relevance_score": "high"}],
        ["not an object"],
        [{"index": 0, "relevance_score": 0.1}] * 9,
    ])
    def test_malformed_response_falls_back_to_sql_order(self, results):
        from src.rerank import rerank

        cands = _candidates()
        with patch('src.rerank.requests.post', return_value=_response(results)):
            out = rerank("q", cands, _cfg())

        assert [c["id"] for c in out] == [c["id"] for c in cands]

    def test_unreachable_endpoint_falls_back(self):
        from src.rerank import rerank

        cands = _candidates()
        with patch('src.rerank.requests.post', side_effect=OSError("refused")):
            out = rerank("q", cands, _cfg())

        assert [c["id"] for c in out] == [c["id"] for c in cands]


class TestCardinality:
    def test_top_n_below_limit_still_returns_everything(self):
        """min(limit, candidate_count) with no duplication - 'exactly limit'
        is impossible when fewer candidates exist."""
        from src.rerank import rerank

        with patch('src.rerank.requests.post', return_value=_response([
            {"index": 1, "relevance_score": 0.9},
        ])):
            out = rerank("q", _candidates(5), _cfg(top_n=2))

        ids = [c["id"] for c in out]
        assert sorted(ids) == [0, 1, 2, 3, 4]
        assert len(ids) == len(set(ids)), "no duplication"
