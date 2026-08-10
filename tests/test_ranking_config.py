"""Tests for ranking configuration and its enforced bounds."""
import pytest


def _load(monkeypatch, yaml_block=None, **env):
    """Load a fresh RankingConfig with optional YAML and env overrides."""
    from src.config import ranking

    ranking.RankingConfig._instance = None
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return ranking.RankingConfig(config_data=yaml_block)


class TestDefaults:
    def test_defaults_when_block_absent(self, monkeypatch):
        cfg = _load(monkeypatch, yaml_block={})

        assert cfg.w_rec == 0.05
        assert cfg.w_imp == 0.03
        assert cfg.w_acc == 0.02
        assert cfg.half_life_days == 180
        assert cfg.access_cap == 10
        assert cfg.access_tracking_enabled is True
        assert cfg.rerank_enabled is False

    def test_env_overrides_per_field(self, monkeypatch):
        cfg = _load(monkeypatch, yaml_block={"w_rec": 0.04}, RANKING_W_IMP="0.01")

        assert cfg.w_rec == 0.04, "YAML value survives when no env override"
        assert cfg.w_imp == 0.01, "env overrides that one field only"
        assert cfg.w_acc == 0.02, "untouched field keeps its default"


class TestEnforcedBounds:
    """The secondary-weight bound is a property of the system, not of the
    shipped defaults: a config that exceeds it is rejected."""

    def test_secondary_weights_over_bound_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="0.10"):
            _load(monkeypatch, yaml_block={"w_rec": 0.5, "w_imp": 0.3, "w_acc": 0.2})

    def test_bound_boundary_is_accepted(self, monkeypatch):
        cfg = _load(monkeypatch, yaml_block={"w_rec": 0.05, "w_imp": 0.03, "w_acc": 0.02})
        assert cfg.secondary_total == pytest.approx(0.10)

    def test_negative_weight_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="w_rec"):
            _load(monkeypatch, yaml_block={"w_rec": -0.01})

    def test_zero_half_life_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="half_life_days"):
            _load(monkeypatch, yaml_block={"half_life_days": 0})

    def test_zero_access_cap_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="access_cap"):
            _load(monkeypatch, yaml_block={"access_cap": 0})

    def test_non_finite_weight_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="w_imp"):
            _load(monkeypatch, yaml_block={"w_imp": float("nan")})


class TestRerankValidation:
    def test_enabled_without_url_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="rerank.url"):
            _load(monkeypatch, yaml_block={"rerank": {"enabled": True, "model": "m"}})

    def test_enabled_without_model_rejected(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="rerank.model"):
            _load(monkeypatch, yaml_block={"rerank": {"enabled": True, "url": "http://x"}})

    def test_top_n_cap_enforced(self, monkeypatch):
        from src.config.ranking import RankingConfigError

        with pytest.raises(RankingConfigError, match="top_n"):
            _load(monkeypatch, yaml_block={
                "rerank": {"enabled": True, "url": "http://x", "model": "m",
                           "top_n": 500},
            })

    def test_disabled_rerank_skips_url_validation(self, monkeypatch):
        cfg = _load(monkeypatch, yaml_block={"rerank": {"enabled": False}})
        assert cfg.rerank_enabled is False
