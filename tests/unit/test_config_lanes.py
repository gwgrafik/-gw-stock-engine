"""Plik włączający kolejne pasma (`config/lanes.toml`, zadanie 12): brak pliku albo `enabled = false` = tylko pasmo
DE40 v1; plik leży poza odciskiem pre-rejestracji v1 i jest czytany ściśle jak pliki v1."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock import prereg
from gwstock.config import LANES_FILE, LaneConfig, load_bar_rule_config, load_config, load_lanes

CONFIG = Path("config")

LANE = """
enabled = true

[[lane]]
rule = "bar_b1_m5"
rule_file = "lane_us500_b1_m5.toml"
exchange_profile = "NYSE"
currency = "USD"
slippage_guard_fraction = "0.25"
max_loss_per_trade = "1.00"
costs_file = "costs_v1.toml"
"""


@pytest.fixture
def cfg_dir(tmp_path) -> Path:
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml", "lane_us500_b1_m5.toml"):
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


def test_no_file_means_no_extra_lanes(cfg_dir):
    assert load_lanes(cfg_dir) is None


def test_the_repository_ships_no_lanes_file():
    assert not (CONFIG / LANES_FILE).exists()  # domyślnie wyłączone: włącza wyłącznie właściciel


def test_disabled_file_means_no_extra_lanes(cfg_dir):
    (cfg_dir / LANES_FILE).write_text(LANE.replace("enabled = true", "enabled = false"))
    assert load_lanes(cfg_dir) is None


def test_a_lane_is_loaded_with_its_rule_and_costs(cfg_dir):
    (cfg_dir / LANES_FILE).write_text(LANE)
    (lane,) = load_lanes(cfg_dir)
    assert lane.cfg == LaneConfig(
        "bar_b1_m5", "lane_us500_b1_m5.toml", "NYSE", "USD", Decimal("0.25"), Decimal("1.00"), "costs_v1.toml"
    )
    assert lane.bar == load_bar_rule_config(cfg_dir / "lane_us500_b1_m5.toml")
    assert lane.costs == load_config(cfg_dir)[2]
    assert lane.epic == "US500"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('rule = "bar_b1_m5"', 'rule = "orb_index_cfd"', "rule"),
        ('currency = "USD"', 'currency = "usd"', "currency"),
        ('max_loss_per_trade = "1.00"', 'max_loss_per_trade = "1.01"', "max_loss_per_trade"),
        ('slippage_guard_fraction = "0.25"', 'slippage_guard_fraction = 0.25', "slippage_guard_fraction"),
        ('costs_file = "costs_v1.toml"', 'costs_file = "../costs_v1.toml"', "costs_file"),
        ('costs_file = "costs_v1.toml"', 'costs_file = "costs_v1.toml"\nextra = 1', "unknown"),
        ("enabled = true", "enabled = 1", "enabled"),
        ('exchange_profile = "NYSE"', 'exchange_profile = "LSE"', "exchange_profile"),
    ],
)
def test_bad_lane_files_are_refused_with_the_file_and_key(cfg_dir, old, new, match):
    (cfg_dir / LANES_FILE).write_text(LANE.replace(old, new))
    with pytest.raises(ValueError, match=match):
        load_lanes(cfg_dir)


def test_an_enabled_file_without_lanes_or_with_duplicates_is_refused(cfg_dir):
    (cfg_dir / LANES_FILE).write_text("enabled = true\n")
    with pytest.raises(ValueError, match="lane"):
        load_lanes(cfg_dir)
    (cfg_dir / LANES_FILE).write_text(LANE + LANE.replace("enabled = true", ""))
    with pytest.raises(ValueError, match="duplicate"):
        load_lanes(cfg_dir)


def test_a_lane_on_the_v1_instrument_is_refused(cfg_dir):
    text = (cfg_dir / "lane_us500_b1_m5.toml").read_text().replace('instrument = "US500"', 'instrument = "DE40"')
    (cfg_dir / "lane_us500_b1_m5.toml").write_text(text)
    (cfg_dir / LANES_FILE).write_text(LANE)
    with pytest.raises(ValueError, match="DE40"):
        load_lanes(cfg_dir)


def test_the_lanes_file_is_outside_the_v1_preregistration_fingerprint():
    assert LANES_FILE not in prereg.CONFIG_FILES
