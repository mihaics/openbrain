"""Tests for transcript filtering and boundary tracking."""
import json
from pathlib import Path

import pytest


def _rec(**kw):
    return json.dumps(kw)


def _write(tmp_path, records, name="11111111-1111-1111-1111-111111111111.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(records) + "\n")
    return path


def _msg(text, block_type="text"):
    return {"content": [{"type": block_type, "text": text}]}


class TestFiltering:
    def test_tool_results_are_excluded(self, tmp_path):
        """75 of 85 user records in a real session were tool_result blocks -
        a top-level type filter would feed tool output to the model as if a
        human had written it."""
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="user", uuid="u1", message=_msg("real question")),
            _rec(type="user", uuid="u2", message=_msg("TOOL OUTPUT", "tool_result")),
            _rec(type="assistant", uuid="a1", message=_msg("real answer")),
        ])
        exchanges, _ = read_exchanges(path)

        assert [e.text for e in exchanges] == ["real question", "real answer"]

    @pytest.mark.parametrize("block", ["tool_use", "thinking", "redacted_thinking"])
    def test_machine_blocks_excluded(self, tmp_path, block):
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="assistant", uuid="a1", message=_msg("hidden", block)),
            _rec(type="assistant", uuid="a2", message=_msg("visible")),
        ])
        exchanges, _ = read_exchanges(path)
        assert [e.text for e in exchanges] == ["visible"]

    @pytest.mark.parametrize("rtype", [
        "file-history-snapshot", "mode", "permission-mode", "last-prompt",
        "attachment", "system",
    ])
    def test_non_conversation_records_excluded(self, tmp_path, rtype):
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type=rtype, uuid="x", message=_msg("noise")),
            _rec(type="user", uuid="u1", message=_msg("kept")),
        ])
        exchanges, _ = read_exchanges(path)
        assert [e.text for e in exchanges] == ["kept"]

    def test_meta_and_sidechain_excluded(self, tmp_path):
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="user", uuid="m", isMeta=True, message=_msg("meta")),
            _rec(type="user", uuid="s", isSidechain=True, message=_msg("side")),
            _rec(type="user", uuid="u", message=_msg("kept")),
        ])
        exchanges, _ = read_exchanges(path)
        assert [e.text for e in exchanges] == ["kept"]

    def test_no_minimum_length_rule(self, tmp_path):
        """'always use X for Y' is durable and arrives in one turn."""
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="user", uuid="u", message=_msg("always use X for Y")),
        ])
        exchanges, _ = read_exchanges(path)
        assert len(exchanges) == 1


class TestBoundaries:
    def test_offset_advances_and_resumes(self, tmp_path):
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="user", uuid="u1", message=_msg("first")),
        ])
        _, first = read_exchanges(path)

        with path.open("a") as f:
            f.write(_rec(type="user", uuid="u2", message=_msg("second")) + "\n")

        later, second = read_exchanges(path, start_offset=first.byte_offset)

        assert [e.text for e in later] == ["second"]
        assert second.byte_offset > first.byte_offset

    def test_trailing_partial_line_is_not_consumed(self, tmp_path):
        """The file is appended to while being read, so a torn final line
        must be left for the next drain rather than counted."""
        from src.capture.transcript import read_exchanges

        path = tmp_path / "22222222-2222-2222-2222-222222222222.jsonl"
        good = _rec(type="user", uuid="u1", message=_msg("complete"))
        path.write_text(good + "\n" + '{"type": "user", "uuid": "tr')

        exchanges, boundary = read_exchanges(path)

        assert [e.text for e in exchanges] == ["complete"]
        assert boundary.byte_offset == len(good) + 1

    def test_multibyte_offsets_are_byte_accurate(self, tmp_path):
        """Character counting would corrupt the watermark on any non-ASCII."""
        from src.capture.transcript import read_exchanges

        path = _write(tmp_path, [
            _rec(type="user", uuid="u1", message=_msg("ședință în București ✅")),
        ])
        _, boundary = read_exchanges(path)
        assert boundary.byte_offset == path.stat().st_size

    def test_boundary_id_is_independent_of_content(self, tmp_path):
        """A value derived from model output cannot identify the work that
        produced it."""
        from src.capture.transcript import Boundary

        a = Boundary("s", 100, "u1", "p1")
        b = Boundary("s", 100, "u1", "p1")
        c = Boundary("s", 200, "u1", "p1")

        assert a.boundary_id == b.boundary_id
        assert a.boundary_id != c.boundary_id


class TestWindows:
    def test_small_conversation_is_one_window(self):
        from src.capture.transcript import Exchange, windows

        ex = [Exchange("user", "short")]
        assert len(windows(ex, max_chars=10000)) == 1

    def test_large_conversation_splits_with_overlap(self):
        """Overlap exists so a decision made early and resolved late is not
        cut in half and lost from both pieces."""
        from src.capture.transcript import Exchange, windows

        ex = [Exchange("user", "x" * 400) for _ in range(10)]
        out = windows(ex, max_chars=1000, overlap=2)

        assert len(out) > 1
        assert any(e in out[1] for e in out[0]), "windows must overlap"
