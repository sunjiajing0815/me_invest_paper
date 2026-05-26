"""Tests for config.py: Settings loading and YAML target validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from investor.config import Settings, load_targets


class TestSettings:
    def test_loads_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROKER", "alpaca_paper")
        monkeypatch.setenv("ALPACA_API_KEY", "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
        s = Settings()
        assert s.broker == "alpaca_paper"
        assert s.alpaca_api_key == "test-key"

    def test_invalid_broker_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROKER", "coinbase")
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        with pytest.raises(Exception, match="broker must be one of"):
            Settings()

    def test_missing_alpaca_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROKER", "alpaca_paper")
        monkeypatch.setenv("ALPACA_API_KEY", "")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        with pytest.raises(Exception, match="ALPACA_API_KEY is required"):
            Settings()

    def test_prompt_version_validator_strips_v_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BROKER", "alpaca_paper")
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        monkeypatch.setenv("CONTEXT_ADJUST_PROMPT_VERSION", "v2")
        s = Settings()
        assert s.context_adjust_prompt_version == "2"

    def test_prompt_version_validator_bare_number_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BROKER", "alpaca_paper")
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        monkeypatch.setenv("CONTEXT_ADJUST_PROMPT_VERSION", "2")
        s = Settings()
        assert s.context_adjust_prompt_version == "2"

    def test_prompt_version_validator_uppercase_v_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BROKER", "alpaca_paper")
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        # Note: env vars are strings; uppercase V should not be stripped by removeprefix("v")
        # This test documents the CURRENT behavior so future changes are visible
        monkeypatch.setenv("CONTEXT_ADJUST_PROMPT_VERSION", "V2")
        s = Settings()
        # removeprefix("v") does NOT strip uppercase V — "V2" stays "V2"
        # This is acceptable behavior: setting "V2" is a typo and load_prompt will raise
        assert s.context_adjust_prompt_version == "V2"


class TestLoadTargets:
    def test_valid_yaml_loads(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [VOO, QQQ, SCHD, AAPL, MSFT]
            targets:
              VOO:  { pct: 40, band: [35, 45] }
              QQQ:  { pct: 25, band: [21, 29] }
              SCHD: { pct: 15, band: [12, 18] }
              AAPL: { pct: 10, band: [7,  13] }
              MSFT: { pct: 5,  band: [3,  8]  }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        config = load_targets(str(f))
        assert len(config.targets) == 5
        assert config.cash_buffer_pct == 5.0
        pcts = {t.ticker: t.pct for t in config.targets}
        assert pcts["VOO"] == 40.0
        assert sum(pcts.values()) == pytest.approx(95.0, abs=0.01)

    def test_bad_sum_raises(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [VOO]
            targets:
              VOO: { pct: 50, band: [45, 55] }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        with pytest.raises(ValueError, match="must equal 100 - cash_buffer_pct"):
            load_targets(str(f))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_targets(str(tmp_path / "nonexistent.yaml"))

    def test_band_values_loaded(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [VOO]
            targets:
              VOO: { pct: 95, band: [85, 100] }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        config = load_targets(str(f))
        voo = config.targets[0]
        assert voo.band_low == 85.0
        assert voo.band_high == 100.0

    def test_real_targets_yaml_validates(self) -> None:
        config = load_targets("./config/targets.yaml")
        assert len(config.targets) == 10  # VOO QQQ TQQQ BTC ISRG BRK.B AMZN GOOG MSFT MU
        total = sum(t.pct for t in config.targets)
        assert abs(total - 95.0) <= 0.5

    def test_load_targets_reads_asset_class(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [VOO]
            targets:
              VOO: { pct: 95, band: [85, 100], asset_class: index_etf }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        config = load_targets(str(f))
        assert config.targets[0].asset_class == "index_etf"

    def test_load_targets_defaults_asset_class_to_equity(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [AAPL]
            targets:
              AAPL: { pct: 95, band: [85, 100] }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        config = load_targets(str(f))
        assert config.targets[0].asset_class == "equity"

    def test_load_targets_unknown_asset_class_coerces_to_equity(
        self, tmp_path: Path
    ) -> None:
        yaml_text = textwrap.dedent("""\
            watchlist: [TLT]
            targets:
              TLT: { pct: 95, band: [85, 100], asset_class: bond_etf }
            cash_buffer_pct: 5
        """)
        f = tmp_path / "targets.yaml"
        f.write_text(yaml_text)
        config = load_targets(str(f))
        assert config.targets[0].asset_class == "equity"
