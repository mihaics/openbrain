"""Ranking configuration.

Mirrors DatabaseConfig's pattern: YAML defaults, then per-field environment
overrides, so setting one variable cannot silently drop the rest of the block.

Validation runs at load rather than at first search, because a bad weight
should stop the process with the offending key named, not surface as
mysteriously reordered results hours later.
"""
import math
import os
from typing import Any, Dict, Optional

import yaml

MAX_SECONDARY_TOTAL = 0.10
MAX_TOP_N = 200


class RankingConfigError(ValueError):
    """Configuration that would break the ranking contract."""


def _env_float(name: str, current: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return float(raw)
    except ValueError:
        raise RankingConfigError(f"{name} is not a number: {raw!r}")


def _env_int(name: str, current: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return int(raw)
    except ValueError:
        raise RankingConfigError(f"{name} is not an integer: {raw!r}")


def _env_bool(name: str, current: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return current
    return raw.strip().lower() in ("1", "true", "yes", "on")


class RankingConfig:
    """Weights and reranker settings for search ranking."""

    _instance: Optional["RankingConfig"] = None

    def __init__(self, config_path: str = None, config_data: Optional[Dict] = None):
        block = config_data if config_data is not None else self._load_yaml(config_path)
        rerank = (block.get("rerank") or {}) if block else {}

        self.w_rec = _env_float("RANKING_W_REC", float(block.get("w_rec", 0.05)))
        self.w_imp = _env_float("RANKING_W_IMP", float(block.get("w_imp", 0.03)))
        self.w_acc = _env_float("RANKING_W_ACC", float(block.get("w_acc", 0.02)))
        self.half_life_days = _env_float(
            "RANKING_HALF_LIFE_DAYS", float(block.get("half_life_days", 180))
        )
        self.access_cap = _env_int(
            "RANKING_ACCESS_CAP", int(block.get("access_cap", 10))
        )
        self.access_tracking_enabled = _env_bool(
            "RANKING_ACCESS_TRACKING",
            bool((block.get("access_tracking") or {}).get("enabled", True)),
        )

        self.rerank_enabled = _env_bool(
            "RERANK_ENABLED", bool(rerank.get("enabled", False))
        )
        self.rerank_url = os.environ.get("RERANK_URL", rerank.get("url", "") or "")
        self.rerank_model = os.environ.get("RERANK_MODEL", rerank.get("model", "") or "")
        self.rerank_api_key = os.environ.get(
            "RERANK_API_KEY", rerank.get("api_key", "") or ""
        )
        self.rerank_top_n = _env_int("RERANK_TOP_N", int(rerank.get("top_n", 50)))
        self.rerank_timeout_seconds = _env_float(
            "RERANK_TIMEOUT", float(rerank.get("timeout_seconds", 5))
        )

        self._validate()

    @staticmethod
    def _load_yaml(config_path: str = None) -> Dict[str, Any]:
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "settings.yaml"
            )
        try:
            with open(config_path, "r") as f:
                return (yaml.safe_load(f) or {}).get("ranking", {}) or {}
        except Exception:
            # An absent or unreadable file must reproduce the documented
            # defaults, not fail: config is optional by design.
            return {}

    @property
    def secondary_total(self) -> float:
        return self.w_rec + self.w_imp + self.w_acc

    def _validate(self) -> None:
        for name in ("w_rec", "w_imp", "w_acc"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise RankingConfigError(
                    f"ranking.{name} must be a finite number >= 0, got {value!r}"
                )

        # The bound is what makes "secondary signals only break near-ties" true.
        # Leaving it to the defaults would let a config edit silently turn
        # recency into a relevance override.
        if self.secondary_total > MAX_SECONDARY_TOTAL + 1e-9:
            raise RankingConfigError(
                f"ranking weights w_rec+w_imp+w_acc = {self.secondary_total:.4f} "
                f"exceeds the 0.10 bound. Above it, secondary signals can "
                f"outrank relevance instead of breaking near-ties."
            )

        if not math.isfinite(self.half_life_days) or self.half_life_days <= 0:
            raise RankingConfigError(
                f"ranking.half_life_days must be > 0, got {self.half_life_days!r}"
            )
        if self.access_cap <= 0:
            raise RankingConfigError(
                f"ranking.access_cap must be > 0, got {self.access_cap!r}"
            )

        if self.rerank_enabled:
            if not self.rerank_url:
                raise RankingConfigError(
                    "ranking.rerank.url is required when rerank is enabled"
                )
            if not self.rerank_model:
                raise RankingConfigError(
                    "ranking.rerank.model is required when rerank is enabled"
                )
            if not 0 < self.rerank_top_n <= MAX_TOP_N:
                raise RankingConfigError(
                    f"ranking.rerank.top_n must be in 1..{MAX_TOP_N}, "
                    f"got {self.rerank_top_n}"
                )
            if not math.isfinite(self.rerank_timeout_seconds) or \
                    self.rerank_timeout_seconds <= 0:
                raise RankingConfigError(
                    "ranking.rerank.timeout_seconds must be > 0"
                )

    @classmethod
    def get_instance(cls, config_path: str = None) -> "RankingConfig":
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
