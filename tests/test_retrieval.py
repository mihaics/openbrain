"""Tests for hybrid retrieval: lexical arm, fusion, and query structure."""
import re
from unittest.mock import Mock, patch

import pytest


def _capture_sql(**kwargs):
    """Run search_memories against a mock cursor and return (sql, params)."""
    from src.db import queries

    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.fetchall.return_value = []

    with patch('src.db.queries.get_db_cursor', return_value=ctx), \
         patch('src.db.queries.get_vector_dim', return_value=1024):
        queries.search_memories(**kwargs)

    call = ctx.execute.call_args
    return call[0][0], (call[0][1] if len(call[0]) > 1 else [])


EMB = [0.1] * 1024


class TestLexicalArm:
    def test_ors_lexemes_instead_of_anding_them(self):
        """plainto_tsquery ANDs every term, so one unmatched word returns
        nothing - measured 0 rows where an OR of the same terms matched 71."""
        sql, _ = _capture_sql(query="embeddings fail to generate", limit=5)

        assert "replace(" in sql
        assert "' & '" in sql and "' | '" in sql
        assert "::tsquery" in sql

    def test_tsquery_built_once_in_a_cte(self):
        """Repeating the expression invites the filter and the ranking
        expression drifting apart."""
        sql, _ = _capture_sql(query="alpha beta", limit=5)

        assert sql.count("plainto_tsquery") == 1

    def test_ranks_with_ts_rank_cd(self):
        sql, _ = _capture_sql(query="alpha beta", limit=5)
        assert "ts_rank_cd" in sql

    def test_ilike_metacharacters_are_escaped(self):
        """A query of '%' is otherwise a match-all pattern."""
        sql, params = _capture_sql(query="100% _sure_", limit=5)

        assert "ESCAPE" in sql
        like_params = [p for p in params if isinstance(p, str) and p.startswith("%")]
        assert like_params, "expected an ILIKE pattern parameter"
        assert r"\%" in like_params[0]
        assert r"\_" in like_params[0]

    def test_backslash_is_escaped_first(self):
        sql, params = _capture_sql(query=r"a\b", limit=5)
        like_params = [p for p in params if isinstance(p, str) and p.startswith("%")]
        assert r"\\" in like_params[0]


class TestSymmetricFusion:
    def test_both_arms_use_k_60(self):
        """Shipped code used k=1 for text and k=60 for vector, so the best
        semantic match (0.0164) scored below the 20th lexical match (0.0476)."""
        sql, _ = _capture_sql(query="alpha", embedding=EMB, limit=5)

        assert "1.0 / (1 +" not in sql.replace(" ", " ")
        assert sql.count("60 +") >= 2, "both arms must use the same k"


class TestQueryStructure:
    def test_caller_limit_is_applied_once_and_last(self):
        """Shipped code applied LIMIT inside the rrf CTE, so nothing
        downstream could promote a candidate from the wider pool."""
        sql, _ = _capture_sql(query="alpha", embedding=EMB, limit=5)

        tail = sql[sql.rindex("ORDER BY"):]
        assert "LIMIT" in tail, "final LIMIT must follow the final ORDER BY"

    def test_deterministic_tie_breakers(self):
        sql, _ = _capture_sql(query="alpha", embedding=EMB, limit=5)

        for m in re.finditer(r"ROW_NUMBER\(\) OVER \(ORDER BY (.+?)\)", sql, re.S):
            assert m.group(1).rstrip().endswith("id"), \
                "every ROW_NUMBER ordering needs a stable id tie-breaker"

    def test_desc_orderings_specify_nulls_last(self):
        """PostgreSQL defaults DESC to NULLS FIRST, which would float NULL
        importance and NULL created_at to the top."""
        sql, _ = _capture_sql(query="alpha", embedding=EMB, limit=5)

        for m in re.finditer(r"(\w+(?:\.\w+)?)\s+DESC(?!\s+NULLS)", sql):
            token = m.group(0)
            assert "NULLS LAST" in sql[m.start():m.start() + 60], \
                f"{token!r} must specify NULLS LAST"


class TestBranches:
    def test_text_only_branch_runs(self):
        sql, _ = _capture_sql(query="alpha", limit=5)
        assert "ts_rank_cd" in sql
        assert "<=>" not in sql

    def test_vector_only_branch_runs(self):
        sql, _ = _capture_sql(query="", embedding=EMB, limit=5)
        assert "<=>" in sql

    def test_no_signal_branch_orders_by_importance_then_recency(self):
        sql, _ = _capture_sql(query="", limit=5)
        assert "importance DESC" in sql
        assert "NULLS LAST" in sql

    def test_filters_apply_in_every_branch(self):
        for kwargs in (
            dict(query="alpha", embedding=EMB),
            dict(query="alpha"),
            dict(query="", embedding=EMB),
            dict(query=""),
        ):
            sql, params = _capture_sql(limit=5, sources=["cli"], **kwargs)
            assert "source = ANY(" in sql
            assert ["cli"] in params


class TestLimitValidation:
    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_rejects_non_positive_limit(self, bad):
        from src.db import queries

        with pytest.raises(ValueError, match="limit"):
            queries.search_memories("alpha", limit=bad)


@pytest.mark.integration
class TestSqlValidityAgainstLivePostgres:
    """Mocked cursors verify shape, never syntax.

    Sub-project 1 shipped a zero-vector query no mocked test could validate;
    the tsquery cast and the fusion CTEs are the same trap.
    """

    def _seed(self, connection, n=3):
        import uuid as uuid_mod

        ids = []
        with connection.get_db_cursor() as cursor:
            for i in range(n):
                mid = uuid_mod.uuid4()
                cursor.execute(
                    """
                    INSERT INTO memory (id, source, content, embedding, importance)
                    VALUES (%s, 'test', %s, %s::vector, %s)
                    """,
                    (str(mid), f"embeddings fail to generate number {i}",
                     [0.1 + i * 0.01] * 1024, 0.5),
                )
                ids.append(mid)
        return ids

    def _cleanup(self, connection):
        with connection.get_db_cursor() as cursor:
            cursor.execute("DELETE FROM memory WHERE source = 'test'")

    def test_all_four_branches_are_valid_sql(self, integration_db):
        from src.db import queries

        try:
            self._seed(integration_db)
            emb = [0.1] * 1024

            hybrid = queries.search_memories("embeddings fail", embedding=emb, limit=5)
            text = queries.search_memories("embeddings fail", limit=5)
            vector = queries.search_memories("", embedding=emb, limit=5)
            recent = queries.search_memories("", limit=5)

            assert hybrid and text and vector and recent
        finally:
            self._cleanup(integration_db)

    def test_all_stopword_query_is_valid_and_matches_nothing(self, integration_db):
        """The empty tsquery must cast cleanly and disable the lexical arm."""
        from src.db import queries

        try:
            self._seed(integration_db)
            results = queries.search_memories("what should i do", limit=5)
            assert results == []
        finally:
            self._cleanup(integration_db)

    def test_ilike_metacharacter_query_is_not_match_all(self, integration_db):
        from src.db import queries

        try:
            self._seed(integration_db)
            assert queries.search_memories("%", limit=5) == []
        finally:
            self._cleanup(integration_db)

    def test_filters_are_valid_in_every_branch(self, integration_db):
        from src.db import queries

        try:
            self._seed(integration_db)
            emb = [0.1] * 1024
            for kwargs in (dict(query="embeddings", embedding=emb),
                           dict(query="embeddings"),
                           dict(query="", embedding=emb),
                           dict(query="")):
                queries.search_memories(
                    limit=5, sources=["test"], tags_all=[], importance_min=0.0,
                    importance_max=1.0, **kwargs,
                )
        finally:
            self._cleanup(integration_db)
