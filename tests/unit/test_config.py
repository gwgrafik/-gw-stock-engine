"""Testy ładowania konfiguracji. Uruchamiać z korzenia repozytorium (ścieżki względne `config/`)."""

import dataclasses
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.config import (
    CostConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    load_config,
    load_runtime,
)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """Kopia katalogu config/ do modyfikacji w teście."""
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    return dst


def set_key(cfg_dir: Path, filename: str, key: str, toml_literal: str) -> None:
    path = cfg_dir / filename
    lines = path.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, line in enumerate(lines):
        if re.match(rf"\s*{key}\s*=", line):
            lines[i] = f"{key} = {toml_literal}"
            hit = True
    assert hit, f"{key} not found in {filename}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def drop_key(cfg_dir: Path, filename: str, key: str) -> None:
    path = cfg_dir / filename
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(rf"\s*{key}\s*=", ln)]
    assert len(kept) == len(lines) - 1, f"{key} not found exactly once in {filename}"
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


DECIMAL_KEYS = [
    ("strategy_v1.toml", "stop_atr_fraction"),
    ("strategy_v1.toml", "min_stop_spread_multiple"),
    ("strategy_v1.toml", "slippage_guard_fraction"),
    ("risk_v1.toml", "virtual_capital"),
    ("risk_v1.toml", "max_loss_per_trade"),
    ("risk_v1.toml", "max_daily_loss"),
    ("risk_v1.toml", "max_total_drawdown"),
    ("risk_v1.toml", "max_effective_leverage"),
    ("risk_v1.toml", "margin_reserve_fraction"),
    ("costs_v1.toml", "entry_slippage_points"),
    ("costs_v1.toml", "stop_exit_extra_slippage_points"),
    ("costs_v1.toml", "fx_markup"),
]
INT_KEYS = [
    ("strategy_v1.toml", "range_minutes"),
    ("strategy_v1.toml", "flatten_before_close_min"),
    ("strategy_v1.toml", "entry_deadline_after_range_min"),
    ("strategy_v1.toml", "fixed_min_size_sessions"),
    ("risk_v1.toml", "max_entries_per_day"),
    ("risk_v1.toml", "stale_quote_s"),
    ("risk_v1.toml", "mismatch_tolerance_s"),
    ("risk_v1.toml", "reconcile_interval_s"),
    ("risk_v1.toml", "ping_interval_s"),
    ("risk_v1.toml", "submit_timeout_s"),
]
STR_KEYS = [
    ("strategy_v1.toml", "instrument"),
    ("strategy_v1.toml", "exchange_profile"),
]
BOOL_KEYS = [("costs_v1.toml", "apply_fx_markup")]
ALL_KEYS = DECIMAL_KEYS + INT_KEYS + STR_KEYS + BOOL_KEYS


# --- load_config ---------------------------------------------------------


def test_config_holds_spec_starting_values():
    s, r, c = load_config(Path("config"))
    assert s.instrument == "DE40" and s.exchange_profile == "XETRA" and s.range_minutes == 5
    assert s.stop_atr_fraction == Decimal("0.10")
    assert s.min_stop_spread_multiple == Decimal("3")
    assert s.slippage_guard_fraction == Decimal("0.25")
    assert s.flatten_before_close_min == 5 and s.entry_deadline_after_range_min == 5
    assert r.virtual_capital == Decimal("40")
    assert (r.max_loss_per_trade, r.max_daily_loss, r.max_total_drawdown) == (
        Decimal("1.00"),
        Decimal("1.50"),
        Decimal("12"),
    )
    assert r.max_effective_leverage == Decimal("5") and r.max_entries_per_day == 1
    assert s.fixed_min_size_sessions == 5
    assert (
        r.stale_quote_s,
        r.mismatch_tolerance_s,
        r.reconcile_interval_s,
        r.ping_interval_s,
        r.submit_timeout_s,
    ) == (60, 60, 10, 300, 10)
    assert r.margin_reserve_fraction == Decimal("0.20")
    assert c.entry_slippage_points == Decimal("0.5")
    assert c.stop_exit_extra_slippage_points == Decimal("1.0")
    assert c.fx_markup == Decimal("0.004")
    assert c.apply_fx_markup is False


def test_load_config_returns_typed_frozen_dataclasses_without_floats():
    s, r, c = load_config(Path("config"))
    assert isinstance(s, StrategyConfig)
    assert isinstance(r, RiskConfig)
    assert isinstance(c, CostConfig)
    for cfg in (s, r, c):
        for f in dataclasses.fields(cfg):
            assert not isinstance(getattr(cfg, f.name), float), f.name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(cfg, dataclasses.fields(cfg)[0].name, None)


def test_load_config_rejects_float_money(cfg_dir: Path):
    set_key(cfg_dir, "risk_v1.toml", "max_loss_per_trade", "1.0")
    with pytest.raises(ValueError, match="use string decimals"):
        load_config(cfg_dir)


@pytest.mark.parametrize(("filename", "key"), DECIMAL_KEYS + INT_KEYS)
def test_load_config_rejects_toml_float_in_any_numeric_field(cfg_dir: Path, filename: str, key: str):
    set_key(cfg_dir, filename, key, "1.5")
    with pytest.raises(ValueError, match="use string decimals"):
        load_config(cfg_dir)


@pytest.mark.parametrize(("filename", "key"), DECIMAL_KEYS)
def test_load_config_rejects_toml_integer_for_decimal_field(cfg_dir: Path, filename: str, key: str):
    set_key(cfg_dir, filename, key, "1")
    with pytest.raises(ValueError, match=key):
        load_config(cfg_dir)


@pytest.mark.parametrize("literal", ['"abc"', '""', '"NaN"', '"Infinity"', '"-1.00"'])
def test_load_config_rejects_unparseable_or_non_finite_or_negative_decimal(cfg_dir: Path, literal: str):
    set_key(cfg_dir, "risk_v1.toml", "max_daily_loss", literal)
    with pytest.raises(ValueError, match="max_daily_loss"):
        load_config(cfg_dir)


@pytest.mark.parametrize(("filename", "key"), INT_KEYS)
@pytest.mark.parametrize("literal", ['"5"', "true", "-1"])
def test_load_config_rejects_wrong_value_for_integer_field(cfg_dir: Path, filename: str, key: str, literal: str):
    set_key(cfg_dir, filename, key, literal)
    with pytest.raises(ValueError, match=key):
        load_config(cfg_dir)


@pytest.mark.parametrize("literal", ['"false"', "0", "1"])
def test_load_config_requires_toml_bool_for_apply_fx_markup(cfg_dir: Path, literal: str):
    set_key(cfg_dir, "costs_v1.toml", "apply_fx_markup", literal)
    with pytest.raises(ValueError, match="apply_fx_markup"):
        load_config(cfg_dir)


@pytest.mark.parametrize(("filename", "key"), STR_KEYS)
@pytest.mark.parametrize("literal", ["5", '""'])
def test_load_config_rejects_wrong_value_for_string_field(cfg_dir: Path, filename: str, key: str, literal: str):
    set_key(cfg_dir, filename, key, literal)
    with pytest.raises(ValueError, match=key):
        load_config(cfg_dir)


@pytest.mark.parametrize(("filename", "key"), ALL_KEYS)
def test_load_config_missing_key_raises_naming_the_key(cfg_dir: Path, filename: str, key: str):
    drop_key(cfg_dir, filename, key)
    with pytest.raises(ValueError, match=key):
        load_config(cfg_dir)


@pytest.mark.parametrize("filename", ["strategy_v1.toml", "risk_v1.toml", "costs_v1.toml"])
def test_load_config_unknown_key_raises(cfg_dir: Path, filename: str):
    with (cfg_dir / filename).open("a", encoding="utf-8") as fh:
        fh.write('max_loss_per_trade_typo = "9.99"\n')
    with pytest.raises(ValueError, match="max_loss_per_trade_typo"):
        load_config(cfg_dir)


def test_load_config_missing_file_raises(cfg_dir: Path):
    (cfg_dir / "risk_v1.toml").unlink()
    with pytest.raises(FileNotFoundError):
        load_config(cfg_dir)


def test_load_config_malformed_toml_raises_value_error(cfg_dir: Path):
    (cfg_dir / "costs_v1.toml").write_text("fx_markup = = broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_dir)


# --- load_runtime --------------------------------------------------------


def test_runtime_defaults_when_files_absent(tmp_path: Path):
    rt = load_runtime(tmp_path / "no_config", tmp_path / "no_data")
    assert rt == RuntimeConfig(live_allowed=False, account_id=None)


def test_repo_runtime_toml_keeps_live_disallowed(tmp_path: Path):
    rt = load_runtime(Path("config"), tmp_path)
    assert rt.live_allowed is False and rt.account_id is None


def test_runtime_reads_values_when_present(tmp_path: Path):
    cfg, data = tmp_path / "config", tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    (cfg / "runtime.toml").write_text("live_allowed = true\n", encoding="utf-8")
    (data / "account.json").write_text(json.dumps({"account_id": "123456789"}), encoding="utf-8")
    rt = load_runtime(cfg, data)
    assert rt == RuntimeConfig(live_allowed=True, account_id="123456789")


def test_runtime_account_id_comes_only_from_account_json(tmp_path: Path):
    cfg, data = tmp_path / "config", tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    (cfg / "runtime.toml").write_text('live_allowed = false\naccount_id = "from_toml"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="account_id"):
        load_runtime(cfg, data)


def test_runtime_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RuntimeConfig(live_allowed=False, account_id=None).live_allowed = True  # type: ignore[misc]


@pytest.mark.parametrize("literal", ['"true"', "1", '"false"'])
def test_runtime_live_allowed_must_be_toml_bool(tmp_path: Path, literal: str):
    (tmp_path / "runtime.toml").write_text(f"live_allowed = {literal}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="live_allowed"):
        load_runtime(tmp_path, tmp_path / "data")


def test_runtime_unknown_key_raises(tmp_path: Path):
    (tmp_path / "runtime.toml").write_text("live_alowed = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="live_alowed"):
        load_runtime(tmp_path, tmp_path / "data")


def test_runtime_empty_runtime_toml_defaults_to_live_disallowed(tmp_path: Path):
    (tmp_path / "runtime.toml").write_text("# pusty\n", encoding="utf-8")
    assert load_runtime(tmp_path, tmp_path / "data").live_allowed is False


@pytest.mark.parametrize(
    "content",
    ["{not json", "", '["account_id"]', "{}", '{"other": "x"}', '{"account_id": 123}', '{"account_id": ""}',
     '{"account_id": null}'],
)
def test_runtime_malformed_account_json_fails_closed(tmp_path: Path, content: str):
    (tmp_path / "account.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="account"):
        load_runtime(tmp_path / "config", tmp_path)


def test_runtime_account_json_non_utf8_fails_closed(tmp_path: Path):
    (tmp_path / "account.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match="account"):
        load_runtime(tmp_path / "config", tmp_path)
