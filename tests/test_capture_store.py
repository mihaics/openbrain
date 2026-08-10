"""Tests for the capture store: all-or-nothing, deduped, generation-safe."""
import json
import uuid
from unittest.mock import Mock, patch

import pytest


def _cfg(**over):
    from src.config.capture import CaptureConfig
    return CaptureConfig(config_data=over)


WORK = {"boundary_id": "b1", "prompt_version": "1", "model_digest": "d1",
        "reprocess_generation": 0}


class TestSourceId:
    def test_distinct_per_fact(self):
        """Without a per-fact component, three facts collapse under one id."""
        from src.capture.store import source_id_for

        assert source_id_for(WORK, "fact one") != source_id_for(WORK, "fact two")

    def test_stable_for_the_same_input(self):
        from src.capture.store import source_id_for

        assert source_id_for(WORK, "a fact") == source_id_for(WORK, "a fact")

    def test_reprocess_generation_changes_it(self):
        """Otherwise a reprocess collides with the run it supersedes."""
        from src.capture.store import source_id_for

        other = dict(WORK, reprocess_generation=1)
        assert source_id_for(WORK, "a fact") != source_id_for(other, "a fact")

    def test_insensitive_to_whitespace_and_case(self):
        from src.capture.store import source_id_for

        assert source_id_for(WORK, "A  Fact") == source_id_for(WORK, "a fact")


class TestAllOrNothing:
    def test_embedding_failure_inserts_nothing(self):
        """A failure partway through would leave half a boundary committed."""
        from src.capture.store import StoreOutcome, store_boundary
        from src.embedder import EmbeddingError

        with patch('src.capture.store.create_validated_embedding',
                   side_effect=EmbeddingError("ollama down")), \
             patch('src.capture.store.get_vector_dim', return_value=1024), \
             patch('src.capture.store.get_db_cursor') as cursor:
            outcome, report = store_boundary(
                [{"fact": "x", "category": "c"}], WORK, {}, _cfg(), 1)

        assert outcome == StoreOutcome.EMBEDDING_FAILURE
        assert report["stored"] == 0
        cursor.assert_not_called()

    def test_no_facts_is_not_a_failure(self):
        from src.capture.store import StoreOutcome, store_boundary

        outcome, _ = store_boundary([], WORK, {}, _cfg(), 1)
        assert outcome == StoreOutcome.NOTHING_TO_DO


class TestDedup:
    def _ctx(self, exact=None, nearest=None):
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.fetchone.side_effect = [exact, nearest]
        return ctx

    def test_exact_hash_match_catches_null_embedding_rows(self):
        """A vector search cannot see rows whose embedding is NULL."""
        from src.capture.store import find_duplicate

        with patch('src.capture.store.get_db_cursor',
                   return_value=self._ctx(exact={"id": uuid.uuid4()})):
            assert find_duplicate("a fact", None, 0.92) is not None

    def test_similar_row_above_threshold_is_a_duplicate(self):
        from src.capture.store import find_duplicate

        nearest = {"id": uuid.uuid4(), "content": "x", "similarity": 0.97}
        with patch('src.capture.store.get_db_cursor',
                   return_value=self._ctx(exact=None, nearest=nearest)):
            assert find_duplicate("a fact", [0.1] * 1024, 0.92) is not None

    def test_below_threshold_is_kept(self):
        from src.capture.store import find_duplicate

        nearest = {"id": uuid.uuid4(), "content": "x", "similarity": 0.5}
        with patch('src.capture.store.get_db_cursor',
                   return_value=self._ctx(exact=None, nearest=nearest)):
            assert find_duplicate("a fact", [0.1] * 1024, 0.92) is None

    def test_exactly_at_threshold_counts_as_duplicate(self):
        from src.capture.store import find_duplicate

        nearest = {"id": uuid.uuid4(), "content": "x", "similarity": 0.92}
        with patch('src.capture.store.get_db_cursor',
                   return_value=self._ctx(exact=None, nearest=nearest)):
            assert find_duplicate("a fact", [0.1] * 1024, 0.92) is not None

    def test_exhaustive_scan_not_index_assisted_ordering(self):
        """Approximate HNSW search need not return the true nearest neighbour."""
        from src.capture.store import find_duplicate

        ctx = self._ctx(exact=None, nearest=None)
        with patch('src.capture.store.get_db_cursor', return_value=ctx):
            find_duplicate("a fact", [0.1] * 1024, 0.92)

        sql = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list)
        assert "ORDER BY embedding <=>" in sql
        assert "LIMIT 1" in sql


@pytest.mark.integration
class TestTransactionalCommit:
    def _cleanup(self, connection):
        with connection.get_db_cursor() as cursor:
            cursor.execute("DELETE FROM memory WHERE source = 'claude-code-auto'")
            cursor.execute("UPDATE capture_state SET generation = 1, "
                           "previous_generation = NULL, "
                           "committed_work_identity = NULL WHERE id = 1")

    def test_rows_and_generation_commit_together(self, integration_db):
        from src.capture.store import StoreOutcome, store_boundary

        try:
            outcome, report = store_boundary(
                [{"fact": "auto capture stores rows and state together",
                  "category": "test"}],
                WORK, {"session_id": "s1", "boundary_id": "b1"}, _cfg(), 1,
            )
            assert outcome == StoreOutcome.STORED
            assert report["stored"] == 1

            with integration_db.get_db_cursor() as cursor:
                cursor.execute("SELECT generation, committed_work_identity "
                               "FROM capture_state WHERE id = 1")
                state = cursor.fetchone()
            assert state["generation"] == 2, "generation must advance with the rows"
            assert state["committed_work_identity"]["boundary_id"] == "b1"
        finally:
            self._cleanup(integration_db)

    def test_wrong_expected_generation_inserts_nothing(self, integration_db):
        """A generation that moved underneath us means another writer acted."""
        from src.capture.store import StoreOutcome, store_boundary

        try:
            outcome, _ = store_boundary(
                [{"fact": "should not land", "category": "test"}],
                WORK, {}, _cfg(), 999,
            )
            assert outcome == StoreOutcome.DB_FAILURE

            with integration_db.get_db_cursor() as cursor:
                cursor.execute("SELECT count(*) AS n FROM memory "
                               "WHERE source = 'claude-code-auto'")
                assert cursor.fetchone()["n"] == 0
        finally:
            self._cleanup(integration_db)

    def test_boundary_already_committed_is_detectable(self, integration_db):
        from src.capture.store import boundary_already_committed, store_boundary

        try:
            store_boundary([{"fact": "a durable fact for lookup", "category": "t"}],
                           WORK, {"boundary_id": "b1"}, _cfg(), 1)
            assert boundary_already_committed("b1") is True
            assert boundary_already_committed("nope") is False
        finally:
            self._cleanup(integration_db)
