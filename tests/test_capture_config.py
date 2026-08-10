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
    def test_exact_resolved_path_matches(self, tmp_path):
        cfg = _cfg(projects=[str(tmp_path)])
        assert cfg.allows_project(str(tmp_path)) is True

    def test_sibling_and_parent_do_not_match(self, tmp_path):
        child = tmp_path / "child"
        child.mkdir()
        cfg = _cfg(projects=[str(child)])

        assert cfg.allows_project(str(tmp_path)) is False
        assert cfg.allows_project(str(tmp_path / "other")) is False


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
