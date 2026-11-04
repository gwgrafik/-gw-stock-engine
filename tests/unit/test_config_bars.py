"""Testy ładowania konfiguracji reguły świecowej (`config/lane_us500_b1_m5.toml`).

Uruchamiać z korzenia repozytorium (ścieżki względne `config/`).
"""

import dataclasses
import re
import shutil
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.config import BarRuleConfig, load_bar_rule_config, load_config, parse_signal_windows

FILENAME = "lane_us500_b1_m5.toml"


@pytest.fixture
def lane_path(tmp_path: Path) -> Path:
    """Kopia pliku konfiguracji pasma do modyfikacji w teście."""
    dst = tmp_path / FILENAME
    shutil.copyfile(Path("config") / FILENAME, dst)
    return dst


def set_key(path: Path, key: str, toml_literal: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, line in enumerate(lines):
        if re.match(rf"\s*{key}\s*=", line):
            lines[i] = f"{key} = {toml_literal}"
            hit = True
    assert hit, f"{key} not found in {path.name}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def drop_key(path: Path, key: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(rf"\s*{key}\s*=", ln)]
    assert len(kept) == len(lines) - 1, f"{key} not found exactly once in {path.name}"
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


DECIMAL_KEYS = [
    "min_body_fraction",
    "long_min_close_fraction",
    "short_max_close_fraction",
    "spread_buffer_fraction",
    "target_r",
    "day_stop_r",
    "min_stop_fraction",
    "slippage_filter_atr_fraction",
    "max_round_trip_cost_r",
    "assumed_slippage_points",
]
INT_KEYS = [
    "bar_minutes",
    "lookback_bars",
    "time_limit_min",
    "fixed_min_size_sessions",
    "flatten_before_close_min",
    "atr_period",
    "gap_tolerance_bars",
    "max_entries_per_day",
]
STR_KEYS = ["instrument", "session_timezone", "signal_windows"]
ALL_KEYS = DECIMAL_KEYS + INT_KEYS + STR_KEYS

POSITIVE_DECIMAL_KEYS = ["target_r", "day_stop_r", "min_stop_fraction", "slippage_filter_atr_fraction", "max_round_trip_cost_r"]
POSITIVE_INT_KEYS = ["bar_minutes", "lookback_bars", "time_limit_min", "atr_period", "max_entries_per_day"]
# ułamki w [0, 1], zero ma sens (korpus, położenie zamknięcia, bufor wybicia)
FRACTION_KEYS = ["min_body_fraction", "long_min_close_fraction", "short_max_close_fraction", "spread_buffer_fraction"]
# ułamki w (0, 1]: zero wyłączałoby filtr (stop bliżej niż 0 %) albo odrzucałoby każde wejście (poślizg > 0 x ATR)
POSITIVE_FRACTION_KEYS = ["min_stop_fraction", "slippage_filter_atr_fraction"]
ZERO_ALLOWED_DECIMAL_KEYS = ["assumed_slippage_points"]  # 0 = bez założonego poślizgu
# 0 ma sens: fixed_min_size_sessions: reguły od razu po zgodzie właściciela (jak w v1); flatten_before_close_min: godzina
# zamknięcia; gap_tolerance_bars: żadnej brakującej świecy w zakresie
ZERO_ALLOWED_INT_KEYS = ["fixed_min_size_sessions", "flatten_before_close_min", "gap_tolerance_bars"]


def test_lane_config_holds_the_frozen_research_values():
    """Wartości B1/M5 z `research/candle_strategies.py` (stan `6c15bc8`): zmiana któregokolwiek = NOWY wariant."""
    cfg = load_bar_rule_config(Path("config") / FILENAME)
    assert cfg == BarRuleConfig(
        instrument="US500",
        bar_minutes=5,
        lookback_bars=12,
        min_body_fraction=Decimal("0.60"),
        long_min_close_fraction=Decimal("0.75"),
        short_max_close_fraction=Decimal("0.25"),
        spread_buffer_fraction=Decimal("0.25"),
        target_r=Decimal("1.5"),
        time_limit_min=50,
        fixed_min_size_sessions=5,
        session_timezone="America/New_York",
        signal_windows="09:45-11:45,14:00-15:30",
        flatten_before_close_min=5,
        atr_period=14,
        gap_tolerance_bars=2,
        max_entries_per_day=10,
        day_stop_r=Decimal("3.0"),
        min_stop_fraction=Decimal("0.0001"),
        slippage_filter_atr_fraction=Decimal("0.5"),
        max_round_trip_cost_r=Decimal("0.20"),
        assumed_slippage_points=Decimal("0.15"),
    )


def test_load_bar_rule_config_returns_a_frozen_dataclass_without_floats():
    cfg = load_bar_rule_config(Path("config") / FILENAME)
    assert isinstance(cfg, BarRuleConfig)
    for f in dataclasses.fields(cfg):
        assert not isinstance(getattr(cfg, f.name), float), f.name
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.target_r = Decimal("9")  # type: ignore[misc]


def test_every_dataclass_field_is_classified_below():
    assert {f.name for f in dataclasses.fields(BarRuleConfig)} == set(ALL_KEYS)
    classified = set(
        POSITIVE_DECIMAL_KEYS
        + POSITIVE_INT_KEYS
        + FRACTION_KEYS
        + ZERO_ALLOWED_DECIMAL_KEYS
        + ZERO_ALLOWED_INT_KEYS
    )
    assert {k for k in DECIMAL_KEYS + INT_KEYS} == classified
    for key in DECIMAL_KEYS + INT_KEYS:
        if key.endswith("_fraction"):
            assert key in FRACTION_KEYS + POSITIVE_FRACTION_KEYS, key


def test_v1_loader_ignores_the_lane_file_and_still_loads():
    s, r, c = load_config(Path("config"))
    assert s.instrument == "DE40" and r.max_entries_per_day == 1 and c.apply_fx_markup is False


def test_load_bar_rule_config_rejects_float_decimals(lane_path: Path):
    set_key(lane_path, "target_r", "1.5")
    with pytest.raises(ValueError, match="use string decimals"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("key", ALL_KEYS)
def test_missing_key_raises_naming_file_and_key(lane_path: Path, key: str):
    drop_key(lane_path, key)
    with pytest.raises(ValueError, match=rf"{re.escape(FILENAME)}.*{key}"):
        load_bar_rule_config(lane_path)


def test_unknown_key_raises_naming_file_and_key(lane_path: Path):
    with lane_path.open("a", encoding="utf-8") as fh:
        fh.write('target_r_typo = "9.99"\n')
    with pytest.raises(ValueError, match=rf"{re.escape(FILENAME)}.*target_r_typo"):
        load_bar_rule_config(lane_path)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_bar_rule_config(tmp_path / FILENAME)


@pytest.mark.parametrize("content", [b"target_r = = broken\n", b"\xff\xfe\x00 = 1\n"])
def test_unreadable_toml_error_names_the_file(lane_path: Path, content: bytes):
    lane_path.write_bytes(content)
    with pytest.raises(ValueError, match=re.escape(FILENAME)):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize(
    ("key", "literal", "message"),
    [
        ("fixed_min_size_sessions", '"5"', "expected non-negative integer"),
        ("lookback_bars", "12.0", "expected integer"),
        ("time_limit_min", "true", "expected non-negative integer"),
        ("instrument", "5", "expected non-empty string"),
        ("instrument", '" "', "expected non-empty string"),
        ("target_r", "1", "use string decimals"),
        ("min_body_fraction", '"0,60"', "not a plain decimal"),
        ("min_body_fraction", '"-0.6"', "non-negative"),
    ],
)
def test_wrong_types_and_notation_are_rejected(lane_path: Path, key: str, literal: str, message: str):
    set_key(lane_path, key, literal)
    with pytest.raises(ValueError, match=rf"{FILENAME}: {key}.*{message}"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("key", POSITIVE_DECIMAL_KEYS)
@pytest.mark.parametrize("literal", ['"0"', '"0.00"'])
def test_zero_is_rejected_for_positive_decimals(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    with pytest.raises(ValueError, match=rf"{key}.*> 0"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("key", POSITIVE_INT_KEYS)
def test_zero_is_rejected_for_positive_integers(lane_path: Path, key: str):
    set_key(lane_path, key, "0")
    with pytest.raises(ValueError, match=rf"{key}.*positive"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("key", FRACTION_KEYS + POSITIVE_FRACTION_KEYS)
@pytest.mark.parametrize("literal", ['"1.01"', '"2"', '"100"'])
def test_fractions_above_one_are_rejected(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    with pytest.raises(ValueError, match=rf"{key}.*\[0, 1\]"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("key", FRACTION_KEYS)
@pytest.mark.parametrize("literal", ['"0"', '"1"', '"0.5"'])
def test_fractions_accept_the_closed_unit_interval(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    assert getattr(load_bar_rule_config(lane_path), key) == Decimal(literal.strip('"'))


@pytest.mark.parametrize("key", ZERO_ALLOWED_INT_KEYS)
def test_zero_is_accepted_where_it_has_a_meaning(lane_path: Path, key: str):
    set_key(lane_path, key, "0")
    assert getattr(load_bar_rule_config(lane_path), key) == 0


@pytest.mark.parametrize("key", POSITIVE_FRACTION_KEYS)
@pytest.mark.parametrize("literal", ['"0.0001"', '"1"', '"0.5"'])
def test_positive_fractions_accept_the_half_open_unit_interval(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    assert getattr(load_bar_rule_config(lane_path), key) == Decimal(literal.strip('"'))


@pytest.mark.parametrize("key", ZERO_ALLOWED_DECIMAL_KEYS)
@pytest.mark.parametrize("literal", ['"0"', '"0.00"', '"0.15"'])
def test_zero_is_accepted_for_decimals_where_it_has_a_meaning(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    assert getattr(load_bar_rule_config(lane_path), key) == Decimal(literal.strip('"'))


@pytest.mark.parametrize("key", ZERO_ALLOWED_DECIMAL_KEYS)
@pytest.mark.parametrize("literal", ['"-0.1"', '"-0"'])
def test_negative_decimals_are_rejected_everywhere(lane_path: Path, key: str, literal: str):
    set_key(lane_path, key, literal)
    with pytest.raises(ValueError, match=rf"{FILENAME}: {key}.*non-negative"):
        load_bar_rule_config(lane_path)


# ---------------------------------------------------------------------------------------------------------------
# strefa i okna sygnałów: ściśle, błąd z nazwą pliku i klucza
# ---------------------------------------------------------------------------------------------------------------


def test_parse_signal_windows_returns_ascending_local_time_pairs():
    assert parse_signal_windows("09:45-11:45,14:00-15:30") == ((time(9, 45), time(11, 45)), (time(14, 0), time(15, 30)))
    assert parse_signal_windows("00:00-23:59") == ((time(0, 0), time(23, 59)),)
    assert parse_signal_windows("09:45-11:45,11:45-12:00") == ((time(9, 45), time(11, 45)), (time(11, 45), time(12, 0)))


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "09:45",
        "09:45-",
        "-11:45",
        "9:45-11:45",
        "09:45-11:45,",
        ",09:45-11:45",
        "09:45-11:45;14:00-15:30",
        "09:45 - 11:45",
        "09:45-11:45, 14:00-15:30",
        "09:45-11:60",
        "24:00-24:30",
        "09:45-09:45",
        "11:45-09:45",
        "09:45-11:45,10:00-12:00",
        "14:00-15:30,09:45-11:45",
        "09:45-11:45,11:44-12:00",
        "09:45-11:45:00",
        "0945-1145",
        "09:45-1１:45",
    ],
)
def test_parse_signal_windows_rejects_malformed_or_overlapping_windows(text: str):
    with pytest.raises(ValueError, match="signal_windows"):
        parse_signal_windows(text)


@pytest.mark.parametrize("text", ["09:45-11:45;14:00", "11:45-09:45", "09:45-11:45,10:00-12:00"])
def test_load_rejects_bad_signal_windows_naming_file_and_key(lane_path: Path, text: str):
    set_key(lane_path, "signal_windows", f'"{text}"')
    with pytest.raises(ValueError, match=rf"{re.escape(FILENAME)}: signal_windows"):
        load_bar_rule_config(lane_path)


@pytest.mark.parametrize("zone", ["Nowhere/City", "America/New York", "../etc", "ET", "NYC"])
def test_load_rejects_an_unknown_time_zone_naming_file_and_key(lane_path: Path, zone: str):
    set_key(lane_path, "session_timezone", f'"{zone}"')
    with pytest.raises(ValueError, match=rf"{re.escape(FILENAME)}: session_timezone"):
        load_bar_rule_config(lane_path)


def test_load_accepts_another_known_time_zone(lane_path: Path):
    set_key(lane_path, "session_timezone", '"Europe/Berlin"')
    assert load_bar_rule_config(lane_path).session_timezone == "Europe/Berlin"


def test_lane_file_has_no_toml_floats():
    """Wszystkie ułamki i mnożniki w pliku pasma to napisy (liczba TOML-float jest odrzucana przy ładowaniu)."""
    text = (Path("config") / FILENAME).read_text(encoding="utf-8")
    assert not re.search(r"^\s*[a-z_]+\s*=\s*-?[0-9]+\.[0-9]", text, flags=re.MULTILINE)
