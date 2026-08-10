"""Tests for transcript search."""
import json
import os
from pathlib import Path

import pytest


def _rec(**kw):
    return json.dumps(kw)


@pytest.fixture
def corpus(tmp_path):
    """Two personas, one with a nested subagent transcript."""
    roots = {}
    for persona in ("alpha", "beta"):
        root = tmp_path / persona / "projects"
        proj = root / "-home-user-proj"
        proj.mkdir(parents=True)
        sid = f"{persona[0]*8}-1111-1111-1111-111111111111"
        (proj / f"{sid}.jsonl").write_text("\n".join([
            _rec(type="user", uuid="u1", cwd="/home/user/proj",
                 timestamp="2026-08-01T10:00:00Z",
                 message={"content": [{"type": "text",
                                       "text": f"needle in {persona}"}]}),
            _rec(type="user", uuid="u2",
                 message={"content": [{"type": "tool_result",
                                       "content": "needle in tool output"}]}),
        ]) + "\n")
        if persona == "alpha":
            sub = proj / sid / "subagents"
            sub.mkdir(parents=True)
            (sub / "agent-deadbeef.jsonl").write_text(_rec(
                type="assistant", uuid="a1", cwd="/home/user/proj",
                message={"content": [{"type": "text",
                                      "text": "needle from a subagent"}]}) + "\n")
        roots[persona] = root
    return roots


class TestDiscovery:
    def test_finds_nested_subagent_transcripts(self, corpus):
        """A one-level glob finds 54 of 205 real files; recursion is required."""
        from src.capture.search import discover

        found = discover(corpus)
        assert len(found) == 3
        assert any(p.parent.name == "subagents" for _, _, p in found)

    def test_traversal_is_lexicographic_not_mtime(self, corpus, tmp_path):
        """Ordering by a mutable key lets an append move a file across a
        cursor boundary and duplicate or skip records."""
        from src.capture.search import discover

        before = [str(p) for _, _, p in discover(corpus)]
        target = Path(before[0])
        os.utime(target, (0, 0))          # make it the oldest
        after = [str(p) for _, _, p in discover(corpus)]

        assert before == after
        assert after == sorted(after)

    def test_overlapping_roots_are_deduplicated(self, corpus):
        from src.capture.search import discover

        doubled = dict(corpus)
        doubled["alpha-again"] = corpus["alpha"]
        assert len(discover(doubled)) == len(discover(corpus))


class TestMatching:
    def test_finds_conversation_text(self, corpus):
        from src.capture.search import search

        result = search("needle", corpus)
        assert len(result.matches) == 3

    def test_tool_output_is_never_matched(self, corpus):
        """75 of 85 user records in a real session were tool_result."""
        from src.capture.search import search

        result = search("tool output", corpus)
        assert result.matches == []

    def test_subagents_are_identified_with_both_ids(self, corpus):
        """Only the parent collapses every subagent; only the stem is not a
        session id."""
        from src.capture.search import search

        sub = [m for m in search("needle", corpus).matches if m.kind == "subagent"]
        assert len(sub) == 1
        assert sub[0].agent_id == "agent-deadbeef"
        assert sub[0].session_id.endswith("-1111-1111-1111-111111111111")

    def test_subagents_can_be_excluded(self, corpus):
        from src.capture.search import search

        result = search("needle", corpus, include_subagents=False)
        assert all(m.kind == "session" for m in result.matches)

    def test_persona_filter(self, corpus):
        from src.capture.search import search

        result = search("needle", corpus, persona="beta")
        assert {m.persona for m in result.matches} == {"beta"}

    def test_project_comes_from_record_cwd(self, corpus):
        """Claude's directory slug collapses separators and hyphens alike and
        cannot be decoded back."""
        from src.capture.search import search

        result = search("needle", corpus, project="/home/user/proj")
        assert result.matches
        assert all(m.project == "/home/user/proj" for m in result.matches)

    def test_case_insensitive(self, corpus):
        from src.capture.search import search

        assert search("NEEDLE", corpus).matches


class TestSafety:
    def test_empty_query_refused(self, corpus):
        from src.capture.search import search

        with pytest.raises(ValueError, match="non-empty"):
            search("   ", corpus)

    def test_credential_shaped_query_refused(self, corpus):
        from src.capture.search import search

        with pytest.raises(ValueError, match="credential"):
            search("ghp_abcdefghijklmnopqrstuvwxyz12", corpus)

    def test_snippet_is_redacted(self, tmp_path):
        """Redaction covers the whole block before clipping: clipping first
        can remove the prefix a pattern needs."""
        from src.capture.search import search

        root = tmp_path / "p" / "projects" / "proj"
        root.mkdir(parents=True)
        (root / "cccccccc-1111-1111-1111-111111111111.jsonl").write_text(_rec(
            type="user", uuid="u1",
            message={"content": [{"type": "text",
                                  "text": "the deploy key is ghp_abcdefghijklmnopqrstuvwxyz12 ok"}]},
        ) + "\n")

        result = search("deploy key", {"p": tmp_path / "p" / "projects"})
        assert result.matches
        assert "ghp_" not in result.matches[0].snippet
        assert "REDACTED" in result.matches[0].snippet

    def test_symlinked_file_is_skipped_and_reported(self, corpus, tmp_path):
        from src.capture.search import search

        proj = corpus["beta"] / "-home-user-proj"
        real = tmp_path / "outside.jsonl"
        real.write_text(_rec(type="user", uuid="x",
                             message={"content": [{"type": "text",
                                                   "text": "needle escaped"}]}) + "\n")
        (proj / "dddddddd-1111-1111-1111-111111111111.jsonl").symlink_to(real)

        result = search("needle escaped", corpus)
        assert result.matches == []
        assert "unreadable_or_symlink" in result.truncation_reasons

    def test_oversized_record_is_skipped_with_a_reason(self, tmp_path):
        """One pathological line must not blow the budget or memory."""
        from src.capture.search import search

        root = tmp_path / "p" / "projects" / "proj"
        root.mkdir(parents=True)
        huge = _rec(type="user", uuid="u1",
                    message={"content": [{"type": "text", "text": "x" * 2_000_000}]})
        (root / "eeeeeeee-1111-1111-1111-111111111111.jsonl").write_text(huge + "\n")

        result = search("xxxx", {"p": tmp_path / "p" / "projects"})
        assert result.matches == []
        assert "oversized_record" in result.truncation_reasons

    def test_malformed_record_does_not_abandon_the_file(self, tmp_path):
        """Capture's reader stops at the first bad record; search must not."""
        from src.capture.search import search

        root = tmp_path / "p" / "projects" / "proj"
        root.mkdir(parents=True)
        (root / "ffffffff-1111-1111-1111-111111111111.jsonl").write_text(
            "{ broken\n" + _rec(type="user", uuid="u1",
                                message={"content": [{"type": "text",
                                                      "text": "needle after"}]}) + "\n")

        result = search("needle after", {"p": tmp_path / "p" / "projects"})
        assert len(result.matches) == 1
        assert "malformed_record" in result.truncation_reasons


class TestPagination:
    def test_match_limit_reports_truncation_and_a_cursor(self, corpus):
        from src.capture.search import search

        result = search("needle", corpus, limit=1)
        assert len(result.matches) == 1
        assert result.truncated
        assert "match_limit" in result.truncation_reasons
        assert result.next_cursor

    def test_cursor_continues_without_duplicating(self, corpus):
        from src.capture.search import search

        first = search("needle", corpus, limit=1)
        second = search("needle", corpus, limit=5, cursor=first.next_cursor)

        seen = {(m.session_id, m.agent_id, m.snippet) for m in first.matches}
        again = {(m.session_id, m.agent_id, m.snippet) for m in second.matches}
        assert not (seen & again), "a page must not repeat an earlier match"
        assert len(seen | again) == 3

    def test_cursor_from_a_different_query_is_rejected(self, corpus):
        """Reinterpreting it would silently page through the wrong search."""
        from src.capture.search import search

        cursor = search("needle", corpus, limit=1).next_cursor
        with pytest.raises(ValueError, match="does not match"):
            search("something else", corpus, cursor=cursor)


class TestReporting:
    def test_elapsed_ms_is_populated(self, corpus):
        """A declared-but-always-zero field is worse than no field."""
        from src.capture.search import search

        assert search("needle", corpus).elapsed_ms >= 0
        assert isinstance(search("needle", corpus).as_dict()["elapsed_ms"], int)

    def test_clean_scan_reports_no_truncation(self, corpus):
        from src.capture.search import search

        result = search("needle", corpus, limit=50)
        assert result.truncated is False
        assert result.truncation_reasons == []
        assert result.next_cursor is None
