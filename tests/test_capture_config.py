"""Tests for auto-capture configuration and its hook artifact."""
import json

import pytest


def _cfg(**block):
    from src.config.capture import CaptureConfig
    return CaptureConfig(config_data=block)


class TestDefaults:
    def test_disabled_by_default(self):
        assert _cfg().enabled is False

    def test_allowlist_empty_by_default_and_allows_nothing(self, tmp_path):
        cfg = _cfg()
        assert cfg.projects == []
        assert cfg.allows_project(str(tmp_path)) is False

    def test_env_override_per_field(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_MODEL", "other:1b")
        assert _cfg(model="qwen3:4b").model == "other:1b"


class TestValidation:
    def test_non_loopback_endpoint_refused(self):
        """Transcripts contain credentials; a remote endpoint ships them off
        the machine."""
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="loopback"):
            _cfg(ollama_url="http://evil.example.com:11434")

    @pytest.mark.parametrize("key", [
        "num_ctx", "timeout_seconds", "max_facts_per_boundary",
        "max_fact_chars", "max_input_tokens", "max_sessions_per_drain",
        "daily_ceiling",
    ])
    def test_non_positive_caps_refused(self, key):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match=key):
            _cfg(**{key: 0})

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_threshold_outside_zero_one_refused(self, bad):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="dedup_threshold"):
            _cfg(dedup_threshold=bad)

    def test_non_finite_refused(self):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="finite"):
            _cfg(timeout_seconds=float("inf"))

    def test_enabled_requires_a_model_digest(self):
        """A retagged model would otherwise change behaviour while metadata
        records the pinned digest."""
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="model_digest"):
            _cfg(enabled=True)

    def test_strict_boolean_parsing(self):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="boolean"):
            _cfg(enabled="maybe")


class TestProjectAllowlist:
    """Subtree matching: an entry covers itself and everything beneath it.

    Entries name work trees holding many repositories, and a session may start
    in any subdirectory of any of them, so exact matching captured almost
    nothing in practice.
    """

    def test_exact_resolved_path_matches(self, tmp_path):
        cfg = _cfg(projects=[str(tmp_path)])
        assert cfg.allows_project(str(tmp_path)) is True

    def test_root_allows_a_descendant(self, tmp_path):
        deep = tmp_path / "repo" / "src" / "inner"
        deep.mkdir(parents=True)
        cfg = _cfg(projects=[str(tmp_path)])

        assert cfg.allows_project(str(deep)) is True

    def test_child_does_not_allow_its_parent(self, tmp_path):
        child = tmp_path / "child"
        child.mkdir()
        cfg = _cfg(projects=[str(child)])

        assert cfg.allows_project(str(tmp_path)) is False

    def test_sibling_does_not_match(self, tmp_path):
        allowed, other = tmp_path / "allowed", tmp_path / "other"
        allowed.mkdir()
        other.mkdir()
        cfg = _cfg(projects=[str(allowed)])

        assert cfg.allows_project(str(other)) is False

    def test_sibling_sharing_a_string_prefix_does_not_match(self, tmp_path):
        """`/work/quarticle2` must not match root `/work/quarticle`."""
        allowed, lookalike = tmp_path / "quarticle", tmp_path / "quarticle2"
        allowed.mkdir()
        lookalike.mkdir()
        cfg = _cfg(projects=[str(allowed)])

        assert cfg.allows_project(str(lookalike)) is False
        assert cfg.allows_project(str(lookalike / "repo")) is False

    def test_symlink_escaping_the_tree_is_rejected(self, tmp_path):
        """Matching is on the resolved physical path, so a link out is out."""
        allowed, outside = tmp_path / "allowed", tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        (allowed / "escape").symlink_to(outside)
        cfg = _cfg(projects=[str(allowed)])

        assert cfg.allows_project(str(allowed / "escape")) is False

    def test_symlink_into_the_tree_is_accepted(self, tmp_path):
        allowed, elsewhere = tmp_path / "allowed", tmp_path / "elsewhere"
        allowed.mkdir()
        elsewhere.mkdir()
        (elsewhere / "way-in").symlink_to(allowed)
        cfg = _cfg(projects=[str(allowed)])

        assert cfg.allows_project(str(elsewhere / "way-in")) is True

    def test_empty_allowlist_allows_nothing(self, tmp_path):
        assert _cfg(projects=[]).allows_project(str(tmp_path)) is False

    def test_nonexistent_path_under_a_root_still_matches(self, tmp_path):
        """Resolution must not require the directory to exist."""
        cfg = _cfg(projects=[str(tmp_path)])
        assert cfg.allows_project(str(tmp_path / "not" / "created")) is True

    def test_matched_root_reports_the_deepest_root(self, tmp_path):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        cfg = _cfg(projects=[str(outer), str(inner)])

        assert cfg.matched_root(str(inner / "deep")) == str(inner)
        assert cfg.matched_root(str(outer / "other")) == str(outer)
        assert cfg.matched_root(str(tmp_path)) is None


class TestProjectAllowlistValidation:
    """A bad allowlist must fail loudly, never widen silently."""

    def test_projects_must_be_a_list_not_a_string(self):
        """A bare YAML string would otherwise iterate character by character,
        turning '/' into a root that matches the entire filesystem."""
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="list"):
            _cfg(projects="/home/mihai/work")

    def test_empty_entry_refused(self):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="empty"):
            _cfg(projects=[""])

    def test_relative_entry_refused(self):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="absolute"):
            _cfg(projects=["work/quarticle"])

    def test_non_string_entry_refused(self):
        from src.config.capture import CaptureConfigError

        with pytest.raises(CaptureConfigError, match="string"):
            _cfg(projects=[123])

    def test_tilde_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = _cfg(projects=["~/work"])

        assert cfg.projects == [str(tmp_path / "work")]


class TestHookArtifact:
    def test_written_atomically_and_readable_by_stdlib_json(self, tmp_path):
        from src.config.capture import write_hook_config

        cfg = _cfg(projects=[str(tmp_path)])
        path = write_hook_config(cfg, tmp_path / "hook.json")

        data = json.loads(path.read_text())
        assert data["enabled"] is False
        assert data["projects"] == [str(tmp_path)]
        assert "queue_root" in data
        assert not list(tmp_path.glob("*.tmp.*")), "temp file must not survive"

    def test_artifact_carries_no_secrets(self, tmp_path):
        from src.config.capture import write_hook_config

        path = write_hook_config(_cfg(), tmp_path / "hook.json")
        body = path.read_text().lower()
        for forbidden in ("password", "api_key", "token", "secret"):
            assert forbidden not in body
