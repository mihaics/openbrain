"""Auto-capture configuration.

Two forms, deliberately. The YAML block is authoritative and read by the
worker; a minimal JSON artifact is written for the Stop hook, which is
stdlib-only and cannot import PyYAML. The artifact is a cache, never the
authority - the worker re-checks the live allowlist before any model call or
storage write.
"""
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
STATE_ROOT = Path.home() / ".local" / "state" / "openbrain"
HOOK_CONFIG_PATH = STATE_ROOT / "capture-hook.json"


class CaptureConfigError(ValueError):
    """Configuration that would make capture unsafe or inoperable."""


def _env(name: str, current):
    return os.environ.get(name, current)


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    raise CaptureConfigError(f"capture.{key} must be a boolean, got {value!r}")


def _as_number(value: Any, key: str, *, integer=False):
    try:
        out = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise CaptureConfigError(f"capture.{key} is not a number: {value!r}")
    if not integer and not math.isfinite(out):
        raise CaptureConfigError(f"capture.{key} must be finite, got {value!r}")
    return out


class CaptureConfig:
    """Settings for the hook, the worker and extraction."""

    _instance: Optional["CaptureConfig"] = None

    def __init__(self, config_path: str = None, config_data: Optional[Dict] = None):
        block = config_data if config_data is not None else self._load_yaml(config_path)
        block = block or {}

        self.enabled = _as_bool(_env("CAPTURE_ENABLED", block.get("enabled", False)),
                                "enabled")
        self.projects: List[str] = [
            str(Path(p).expanduser().resolve()) for p in (block.get("projects") or [])
        ]
        self.model = _env("CAPTURE_MODEL", block.get("model", "qwen3:4b"))
        self.model_digest = _env("CAPTURE_MODEL_DIGEST", block.get("model_digest", ""))
        self.prompt_version = str(block.get("prompt_version", "1"))
        self.ollama_url = _env("CAPTURE_OLLAMA_URL",
                               block.get("ollama_url", "http://127.0.0.1:11434"))
        self.num_ctx = _as_number(block.get("num_ctx", 16384), "num_ctx", integer=True)
        self.timeout_seconds = _as_number(block.get("timeout_seconds", 120),
                                          "timeout_seconds")
        self.max_facts_per_boundary = _as_number(
            block.get("max_facts_per_boundary", 3), "max_facts_per_boundary",
            integer=True)
        self.max_fact_chars = _as_number(block.get("max_fact_chars", 1000),
                                         "max_fact_chars", integer=True)
        self.max_input_tokens = _as_number(block.get("max_input_tokens", 12000),
                                           "max_input_tokens", integer=True)
        self.max_sessions_per_drain = _as_number(
            block.get("max_sessions_per_drain", 20), "max_sessions_per_drain",
            integer=True)
        self.daily_ceiling = _as_number(block.get("daily_ceiling", 30),
                                        "daily_ceiling", integer=True)
        self.dedup_threshold = _as_number(block.get("dedup_threshold", 0.92),
                                          "dedup_threshold")
        self.min_exchange_chars = _as_number(block.get("min_exchange_chars", 200),
                                             "min_exchange_chars", integer=True)
        # All persona roots, explicitly. A missing root is visible below
        # rather than a silent omission: searching a subset of personas and
        # reporting confident misses is worse than refusing.
        default_roots = {
            "claude": str(Path.home() / ".claude" / "projects"),
            "pactos": str(Path.home() / ".claude-pactos" / "projects"),
            "quarticle": str(Path.home() / ".claude-quarticle" / "projects"),
        }
        configured = block.get("transcript_roots")
        if isinstance(configured, dict):
            self.persona_roots = {k: str(Path(v).expanduser())
                                  for k, v in configured.items()}
        elif configured:
            self.persona_roots = {Path(p).parent.name.lstrip("."): str(Path(p).expanduser())
                                  for p in configured}
        else:
            self.persona_roots = default_roots
        self.transcript_roots: List[str] = [
            str(Path(v).expanduser().resolve())
            for v in self.persona_roots.values() if Path(v).expanduser().exists()
        ]
        self._validate()

    @staticmethod
    def _load_yaml(config_path: str = None) -> Dict[str, Any]:
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "settings.yaml"
            )
        try:
            with open(config_path, "r") as f:
                return (yaml.safe_load(f) or {}).get("capture", {}) or {}
        except Exception:
            return {}

    def _validate(self) -> None:
        host = urlparse(self.ollama_url).hostname
        if host not in LOOPBACK_HOSTS:
            # Transcripts contain credentials. A remote endpoint would ship
            # them off the machine, so this is a hard refusal rather than a
            # warning.
            raise CaptureConfigError(
                f"capture.ollama_url must be loopback, got {self.ollama_url!r}"
            )

        for key in ("num_ctx", "timeout_seconds", "max_facts_per_boundary",
                    "max_fact_chars", "max_input_tokens",
                    "max_sessions_per_drain", "daily_ceiling"):
            if getattr(self, key) <= 0:
                raise CaptureConfigError(f"capture.{key} must be > 0")

        if not 0 <= self.dedup_threshold <= 1:
            raise CaptureConfigError(
                f"capture.dedup_threshold must be within 0..1, "
                f"got {self.dedup_threshold}"
            )

        if self.enabled:
            if not self.model_digest:
                raise CaptureConfigError(
                    "capture.model_digest is required when capture is enabled - "
                    "a retagged model would otherwise change behaviour while "
                    "metadata records the pinned digest"
                )
            if not self.prompt_version:
                raise CaptureConfigError("capture.prompt_version is required")

    def allows_project(self, cwd: str) -> bool:
        """Opt-in by exact resolved path. An empty allowlist allows nothing."""
        try:
            resolved = str(Path(cwd).expanduser().resolve())
        except Exception:
            return False
        return resolved in self.projects

    def hook_artifact(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "projects": self.projects,
            "transcript_roots": self.transcript_roots,
            "queue_root": str(STATE_ROOT / "capture-queue"),
        }

    @classmethod
    def get_instance(cls, config_path: str = None) -> "CaptureConfig":
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance


def write_hook_config(config: CaptureConfig, path: Path = HOOK_CONFIG_PATH) -> Path:
    """Write the hook's JSON view atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(config.hook_artifact(), indent=2).encode()

    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return path
