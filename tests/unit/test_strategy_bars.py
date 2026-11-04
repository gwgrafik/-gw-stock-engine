"""Testy reguły świecowej B1/M5 (`strategy_bars`): liczby policzone ręcznie, granice progów, test krzyżowy z research/.

Konfiguracja `CFG` i `RISK` są wpisane w teście na stałe (wartości B1/M5 z `research/candle_strategies.py`), a osobny
test pilnuje, że `config/lane_us500_b1_m5.toml` nadal im odpowiada.

Bazowy scenariusz: US500, 12 poprzednich świec M5 po 5000 (zakres 4999-5001), więc upper = 5001, lower = 4999;
reguły instrumentu: min. wielkość 0.01, krok 0.01, 1 EUR/pkt, min. stop 1.0, krok stopu 0.1; spread 0.8, więc bufor
b = max(0.1, 0.25 x 0.8) = 0.2 (wybicie długie wymaga zamknięcia powyżej 5001.2, krótkie poniżej 4998.8).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock import strategy_bars
from gwstock.config import BarRuleConfig, RiskConfig, load_bar_rule_config
from gwstock.strategy import SKIPPED_MIN_SIZE, STOP_TOO_TIGHT, Skip
from gwstock.strategy_bars import BarEntryPlan, BarSignal, bar_signal, entry_plan
from gwstock.types import Candle, InstrumentRules, Quote, Side

D = Decimal

CFG = BarRuleConfig(
    instrument="US500",
    bar_minutes=5,
    lookback_bars=12,
    min_body_fraction=D("0.60"),
    long_min_close_fraction=D("0.75"),
    short_max_close_fraction=D("0.25"),
    spread_buffer_fraction=D("0.25"),
    target_r=D("1.5"),
    time_limit_min=50,
    fixed_min_size_sessions=5,
    session_timezone="America/New_York",  # od tu pola, których `strategy_bars` nie czyta (okna i filtry dnia: `lanes`)
    signal_windows="09:45-11:45,14:00-15:30",
    flatten_before_close_min=5,
    atr_period=14,
    gap_tolerance_bars=2,
    max_entries_per_day=10,
    day_stop_r=D("3.0"),
    min_stop_fraction=D("0.0001"),
    slippage_filter_atr_fraction=D("0.5"),
    max_round_trip_cost_r=D("0.20"),
    assumed_slippage_points=D("0.15"),
)
RISK = RiskConfig(
    virtual_capital=D("40"),
    max_loss_per_trade=D("1.00"),
    max_daily_loss=D("1.50"),
    max_total_drawdown=D("12"),
    max_effective_leverage=D("5"),
    max_entries_per_day=1,
    stale_quote_s=60,
    mismatch_tolerance_s=60,
    reconcile_interval_s=10,
    ping_interval_s=300,
    submit_timeout_s=10,
    margin_reserve_fraction=D("0.20"),
)
T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)  # 09:30 w Nowym Jorku (czas zimowy od 1.11.2026)
SPREAD = D("0.8")


def make_rules(
    *, min_size: str = "0.01", stop_step: str = "0.1", min_stop: str = "1.0", epic: str = "US500"
) -> InstrumentRules:
    return InstrumentRules(epic, D(min_size), D("0.01"), D("1"), D("0.05"), D(min_stop), D(stop_step))


RULES = make_rules()


def cndl(k: int, o: str, h: str, low: str, c: str, epic: str = "US500") -> Candle:
    return Candle(epic, T0 + timedelta(minutes=5 * k), D(o), D(h), D(low), D(c))


def flat_prior(k: int) -> Candle:
    return cndl(k, "5000", "5001", "4999", "5000")


def series(o: str, h: str, low: str, c: str, *, total: int = 13, edit: dict[int, Candle] | None = None) -> list[Candle]:
    """`total - 1` płaskich świec 4999-5001 i świeca sygnału (ostatnia); `edit` podmienia wybrane świece po indeksie."""
    out = [flat_prior(k) for k in range(total - 1)] + [cndl(total - 1, o, h, low, c)]
    for index, candle in (edit or {}).items():
        out[index] = candle
    return out


# świece sygnału z ręcznych obliczeń (powyżej): (o, h, l, c)
LONG = ("5000.4", "5004", "5000", "5003.6")  # zakres 4, korpus 3.2 (0.80), clv 0.90, c > 5001.2
SHORT = ("4999.6", "5000", "4996", "4996.4")  # zakres 4, korpus 3.2 (0.80), clv 0.10, c < 4998.8


def long_series(**kw):
    return series(*LONG, **kw)


# ---------------------------------------------------------------------------------------------------------------
# bar_signal: sygnał i jego pola
# ---------------------------------------------------------------------------------------------------------------


def test_long_signal_with_hand_computed_numbers():
    s = bar_signal(series(*LONG), SPREAD, RULES, CFG)
    # stop = min(l, upper - b) = min(5000, 5001 - 0.2) = 5000; koniec świecy sygnału = start (T0 + 60 min) + 5 min
    assert s == BarSignal(Side.BUY, D("5000"), D("1.5"), 50, T0 + timedelta(minutes=65))


def test_short_signal_with_hand_computed_numbers():
    s = bar_signal(series(*SHORT), SPREAD, RULES, CFG)
    # stop = max(h, lower + b) = max(5000, 4999 + 0.2) = 5000
    assert s == BarSignal(Side.SELL, D("5000"), D("1.5"), 50, T0 + timedelta(minutes=65))


def test_long_stop_is_the_channel_edge_minus_buffer_when_that_is_below_the_signal_low():
    # l = 5001.0 > upper - b = 5000.8 → stop 5000.8; c = 5002.8 > 5001.2, korpus 0.90, clv 0.90
    s = bar_signal(series("5001.0", "5003", "5001.0", "5002.8"), SPREAD, RULES, CFG)
    assert s is not None and (s.side, s.stop_level) == (Side.BUY, D("5000.8"))


def test_short_stop_is_the_channel_edge_plus_buffer_when_that_is_above_the_signal_high():
    # h = 4999.0 < lower + b = 4999.2 → stop 4999.2; c = 4997.2 < 4998.8, korpus 0.90, clv 0.10
    s = bar_signal(series("4999.0", "4999.0", "4997", "4997.2"), SPREAD, RULES, CFG)
    assert s is not None and (s.side, s.stop_level) == (Side.SELL, D("4999.2"))


def test_signal_end_is_the_start_of_the_last_candle_plus_the_bar_length():
    candles = long_series()
    s = bar_signal(candles, SPREAD, RULES, CFG)
    assert s is not None and s.signal_candle_end == candles[-1].start + timedelta(minutes=5)
    assert s.signal_candle_end.utcoffset() == timedelta(0)


def test_older_candles_than_n_plus_one_are_ignored():
    candles = long_series(total=20, edit={0: cndl(0, "5000", "9000", "1000", "5000")})
    assert bar_signal(candles, SPREAD, RULES, CFG) is not None


# ---------------------------------------------------------------------------------------------------------------
# granice progów (long i short): >= dla korpusu i clv, > dla wybicia
# ---------------------------------------------------------------------------------------------------------------

LONG_BOUNDARY = [
    # (o, h, l, c, sygnał?, co sprawdza)
    ("5001", "5005", "5000", "5004", True, "korpus dokładnie 0.60 (3 / 5) przechodzi"),
    ("5001.1", "5005", "5000", "5004", False, "korpus 0.58 odpada"),
    ("5000.4", "5004", "5000", "5003", True, "clv dokładnie 0.75 (3 / 4) przechodzi"),
    ("5000.4", "5004", "5000", "5002.9", False, "clv 0.725 odpada"),
    ("5000.2", "5001.2", "5000", "5001.2", False, "zamknięcie dokładnie upper + b (5001.2) nie jest wybiciem"),
    ("5000.2", "5001.3", "5000", "5001.3", True, "zamknięcie powyżej upper + b przechodzi"),
]
SHORT_BOUNDARY = [
    ("4999", "5000", "4995", "4996", True, "korpus dokładnie 0.60 (3 / 5) przechodzi"),
    ("4998.9", "5000", "4995", "4996", False, "korpus 0.58 odpada"),
    ("4999.6", "5000", "4996", "4997", True, "clv dokładnie 0.25 (1 / 4) przechodzi"),
    ("4999.6", "5000", "4996", "4997.1", False, "clv 0.275 odpada"),
    ("4999.8", "5000", "4998.8", "4998.8", False, "zamknięcie dokładnie lower - b (4998.8) nie jest wybiciem"),
    ("4999.8", "5000", "4998.7", "4998.7", True, "zamknięcie poniżej lower - b przechodzi"),
]


@pytest.mark.parametrize(("o", "h", "low", "c", "fires", "why"), LONG_BOUNDARY, ids=[b[-1] for b in LONG_BOUNDARY])
def test_long_threshold_boundaries(o, h, low, c, fires, why):
    s = bar_signal(series(o, h, low, c), SPREAD, RULES, CFG)
    assert (s is not None) is fires, why
    if s is not None:
        assert s.side is Side.BUY


@pytest.mark.parametrize(("o", "h", "low", "c", "fires", "why"), SHORT_BOUNDARY, ids=[b[-1] for b in SHORT_BOUNDARY])
def test_short_threshold_boundaries(o, h, low, c, fires, why):
    s = bar_signal(series(o, h, low, c), SPREAD, RULES, CFG)
    assert (s is not None) is fires, why
    if s is not None:
        assert s.side is Side.SELL


def test_no_signal_when_the_signal_candle_has_zero_range():
    assert bar_signal(series("5010", "5010", "5010", "5010"), SPREAD, RULES, CFG) is None


def test_no_signal_when_the_signal_candle_range_is_negative_data():
    assert bar_signal(series("5003", "5000", "5004", "5003.6"), SPREAD, RULES, CFG) is None


def test_no_signal_for_a_candle_inside_the_channel():
    assert bar_signal(series("5000", "5000.8", "4999.2", "5000.6"), SPREAD, RULES, CFG) is None


# ---------------------------------------------------------------------------------------------------------------
# zakres: dokładnie n poprzednich zakończonych świec, bez bieżącej
# ---------------------------------------------------------------------------------------------------------------

def spike_at(index: int, *, total: int = 20) -> list[Candle]:
    """Seria LONG, w której świeca `index` ma wysoki szczyt 5100: psuje wybicie, jeśli wejdzie do zakresu."""
    return long_series(total=total, edit={index: cndl(index, "5000", "5100", "4999", "5000")})


def test_range_covers_exactly_the_12_prior_candles():
    # 20 świec: sygnał to indeks 19, zakres to indeksy 7..18
    assert bar_signal(spike_at(6), SPREAD, RULES, CFG) is not None  # świeca 13. wstecz: poza zakresem
    assert bar_signal(spike_at(7), SPREAD, RULES, CFG) is None  # najstarsza świeca zakresu
    assert bar_signal(spike_at(18), SPREAD, RULES, CFG) is None  # ostatnia zakończona przed sygnałem


def test_range_low_side_covers_exactly_the_12_prior_candles_for_a_short():
    base = series(*SHORT, total=20)
    out_of_range, in_range = list(base), list(base)
    out_of_range[6] = cndl(6, "5000", "5001", "4900", "5000")  # głęboki dołek poza zakresem
    in_range[7] = cndl(7, "5000", "5001", "4900", "5000")  # ten sam dołek w najstarszej świecy zakresu
    assert bar_signal(out_of_range, SPREAD, RULES, CFG) is not None
    assert bar_signal(in_range, SPREAD, RULES, CFG) is None


def test_fewer_than_n_plus_one_candles_give_no_signal_and_exactly_n_plus_one_is_enough():
    assert bar_signal(long_series(total=12), SPREAD, RULES, CFG) is None
    assert bar_signal(long_series(total=13), SPREAD, RULES, CFG) is not None
    assert bar_signal([], SPREAD, RULES, CFG) is None


# ---------------------------------------------------------------------------------------------------------------
# bufor b = max(krok stopu, 0.25 x spread)
# ---------------------------------------------------------------------------------------------------------------

BREAKOUT_105 = ("5000.2", "5001.15", "5000", "5001.15")  # wybicie o 0.15 ponad upper (5001)


def test_buffer_is_the_stop_step_when_it_exceeds_a_quarter_of_the_spread():
    # spread 0.2 → 0.25 x 0.2 = 0.05 < krok 0.1 → b = 0.1 → próg 5001.1 < 5001.15
    assert bar_signal(series(*BREAKOUT_105), D("0.2"), RULES, CFG) is not None
    # spread 0.8 → b = 0.2 → próg 5001.2 > 5001.15
    assert bar_signal(series(*BREAKOUT_105), D("0.8"), RULES, CFG) is None


def test_buffer_without_a_stop_step_is_a_quarter_of_the_spread():
    no_step = make_rules(stop_step="0")
    assert bar_signal(series("5000.2", "5001.05", "5000", "5001.05"), D("0"), no_step, CFG) is not None  # b = 0
    assert bar_signal(series("5000.2", "5001.05", "5000", "5001.05"), D("0.4"), no_step, CFG) is None  # b = 0.1


def test_the_buffer_enters_the_stop_level():
    candles = series("5001.0", "5003", "5001.0", "5002.8")
    s_small = bar_signal(candles, D("0.2"), RULES, CFG)  # b = 0.1 → upper - b = 5000.9
    s_large = bar_signal(candles, D("1.6"), RULES, CFG)  # b = 0.4 → upper - b = 5000.6
    assert s_small is not None and s_small.stop_level == D("5000.9")
    assert s_large is not None and s_large.stop_level == D("5000.6")


# ---------------------------------------------------------------------------------------------------------------
# progi i parametry pochodzą z konfiguracji, nie z kodu
# ---------------------------------------------------------------------------------------------------------------


def test_config_file_matches_the_values_pinned_in_this_test():
    assert load_bar_rule_config(Path("config") / "lane_us500_b1_m5.toml") == CFG


def test_min_body_fraction_comes_from_the_config():
    strict = dataclasses.replace(CFG, min_body_fraction=D("0.90"))  # korpus świecy LONG to 0.80
    assert bar_signal(series(*LONG), SPREAD, RULES, strict) is None


def test_close_location_fractions_come_from_the_config():
    strict_long = dataclasses.replace(CFG, long_min_close_fraction=D("0.95"))  # clv świecy LONG to 0.90
    strict_short = dataclasses.replace(CFG, short_max_close_fraction=D("0.05"))  # clv świecy SHORT to 0.10
    assert bar_signal(series(*LONG), SPREAD, RULES, strict_long) is None
    assert bar_signal(series(*SHORT), SPREAD, RULES, strict_short) is None


def test_spread_buffer_fraction_comes_from_the_config():
    wide = dataclasses.replace(CFG, spread_buffer_fraction=D("1"))  # b = max(0.1, 0.8) = 0.8 → próg 5001.8
    assert bar_signal(series("5000.2", "5001.5", "5000", "5001.5"), SPREAD, RULES, CFG) is not None
    assert bar_signal(series("5000.2", "5001.5", "5000", "5001.5"), SPREAD, RULES, wide) is None


def test_lookback_bars_comes_from_the_config():
    three = dataclasses.replace(CFG, lookback_bars=3)  # zakres to indeksy 9..11 (sygnał: 12)
    assert bar_signal(long_series(edit={8: cndl(8, "5000", "5100", "4999", "5000")}), SPREAD, RULES, three) is not None
    assert bar_signal(long_series(edit={9: cndl(9, "5000", "5100", "4999", "5000")}), SPREAD, RULES, three) is None
    # przy domyślnym n = 12 ta sama świeca nr 8 jest już w zakresie
    assert bar_signal(long_series(edit={8: cndl(8, "5000", "5100", "4999", "5000")}), SPREAD, RULES, CFG) is None


def test_target_time_limit_and_bar_length_come_from_the_config():
    other = dataclasses.replace(CFG, target_r=D("2"), time_limit_min=30, bar_minutes=15)
    candles = series(*LONG)
    s = bar_signal(candles, SPREAD, RULES, other)
    assert s is not None
    assert (s.target_r, s.time_limit_min) == (D("2"), 30)
    assert s.signal_candle_end == candles[-1].start + timedelta(minutes=15)


# ---------------------------------------------------------------------------------------------------------------
# walidacja wejścia: błąd wywołującego to wyjątek, a nie brak sygnału
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candle_epic", "rules_epic", "cfg_instrument"),
    [("DE40", "US500", "US500"), ("US500", "DE40", "US500"), ("US500", "US500", "DE40")],
)
def test_candles_rules_and_config_must_be_for_the_same_instrument(candle_epic, rules_epic, cfg_instrument):
    candles = [dataclasses.replace(c, epic=candle_epic) for c in long_series()]
    cfg = dataclasses.replace(CFG, instrument=cfg_instrument)
    with pytest.raises(ValueError, match="same epic"):
        bar_signal(candles, SPREAD, make_rules(epic=rules_epic), cfg)


def test_one_foreign_candle_in_the_list_is_an_error():
    candles = long_series(total=20, edit={0: cndl(0, "5000", "5001", "4999", "5000", epic="DE40")})
    with pytest.raises(ValueError, match="same epic"):
        bar_signal(candles, SPREAD, RULES, CFG)


def test_candle_starts_must_be_strictly_ascending():
    candles = long_series()
    candles[5], candles[6] = candles[6], candles[5]
    with pytest.raises(ValueError, match="strictly ascending"):
        bar_signal(candles, SPREAD, RULES, CFG)
    duplicate = long_series()
    duplicate[4] = duplicate[5]
    with pytest.raises(ValueError, match="strictly ascending"):
        bar_signal(duplicate, SPREAD, RULES, CFG)


@pytest.mark.parametrize("spread", [D("-0.1"), D("NaN"), D("Infinity")])
def test_spread_must_be_a_finite_non_negative_decimal(spread):
    with pytest.raises(ValueError, match="spread"):
        bar_signal(long_series(), spread, RULES, CFG)


@pytest.mark.parametrize("spread", [0.8, 1, "0.8", None])
def test_spread_must_be_a_decimal(spread):
    with pytest.raises(TypeError, match="spread"):
        bar_signal(long_series(), spread, RULES, CFG)  # type: ignore[arg-type]


def test_candles_must_be_candle_objects():
    with pytest.raises(TypeError, match="Candle"):
        bar_signal([*long_series()[:-1], (5000, 5004, 5000, 5003)], SPREAD, RULES, CFG)  # type: ignore[list-item]


def test_bar_signal_is_pure_and_does_not_modify_its_input():
    candles = long_series(total=16)
    before = list(candles)
    first = bar_signal(candles, SPREAD, RULES, CFG)
    second = bar_signal(candles, SPREAD, RULES, CFG)
    assert first == second and first is not None
    assert candles == before


def test_bar_signal_accepts_a_tuple_of_candles():
    assert bar_signal(tuple(long_series()), SPREAD, RULES, CFG) is not None


# ---------------------------------------------------------------------------------------------------------------
# BarSignal i BarEntryPlan: niemutowalne i sprawdzane przy konstrukcji
# ---------------------------------------------------------------------------------------------------------------


def make_signal(**over) -> BarSignal:
    fields = dict(
        side=Side.BUY, stop_level=D("5000"), target_r=D("1.5"), time_limit_min=50, signal_candle_end=T0
    )
    fields.update(over)
    return BarSignal(**fields)


def test_bar_signal_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_signal().side = Side.SELL  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("side", "BUY", TypeError),
        ("stop_level", 5000.0, TypeError),
        ("stop_level", D("0"), ValueError),
        ("stop_level", D("NaN"), ValueError),
        ("target_r", 1.5, TypeError),
        ("target_r", D("0"), ValueError),
        ("time_limit_min", 0, ValueError),
        ("time_limit_min", True, ValueError),
        ("time_limit_min", 50.0, ValueError),
        ("signal_candle_end", datetime(2026, 11, 5, 14, 35), ValueError),
        ("signal_candle_end", datetime(2026, 11, 5, 15, 35, tzinfo=timezone(timedelta(hours=1))), ValueError),
    ],
)
def test_bar_signal_validates_its_fields(field, value, error):
    with pytest.raises(error, match=field):
        make_signal(**{field: value})


# ---------------------------------------------------------------------------------------------------------------
# entry_plan: odległości od ceny wejścia (long po ask, short po bid), zaokrąglenie stopu W GÓRĘ, wielkość jak w v1
# ---------------------------------------------------------------------------------------------------------------

QUOTE = Quote("US500", D("5003.5"), D("5003.7"), T0 + timedelta(minutes=66))  # spread 0.2, mid 5003.6


def plan(signal: BarSignal, *, quote: Quote = QUOTE, rules: InstrumentRules = RULES, session_index=0, allowed=False):
    return entry_plan(signal, quote, rules, RISK, CFG, session_index=session_index, rule_size_allowed=allowed)


def test_long_plan_uses_the_ask_and_the_exact_stop_distance():
    p = plan(make_signal(stop_level=D("5000")))
    # wejście po ask 5003.7, stop 5000 → 3.7 (wielokrotność kroku 0.1); cel 1.5 x 3.7 = 5.55; tryb minimalny: 0.01
    assert p == BarEntryPlan(Side.BUY, D("0.01"), D("3.7"), D("5.55"), QUOTE)


def test_short_plan_uses_the_bid_and_rounds_the_stop_distance_up_to_the_step():
    p = plan(make_signal(side=Side.SELL, stop_level=D("5006.34")))
    # wejście po bid 5003.5, stop 5006.34 → 2.84, w górę do kroku 0.1 → 2.9; cel 1.5 x 2.9 = 4.35
    assert p == BarEntryPlan(Side.SELL, D("0.01"), D("2.9"), D("4.35"), QUOTE)


def test_without_a_stop_step_the_distance_is_not_rounded():
    p = plan(make_signal(side=Side.SELL, stop_level=D("5006.34")), rules=make_rules(stop_step="0"))
    assert (p.stop_distance, p.target_distance) == (D("2.84"), D("4.260"))


def test_rule_size_is_the_smaller_of_the_loss_and_leverage_limits():
    # leverage: 5 x 40 / 5003.6 = 0.03997 → w dół do kroku 0.01 → 0.03; limit straty 1 / 3.7 = 0.27 jest większy
    assert plan(make_signal(stop_level=D("5000")), session_index=9, allowed=True).size == D("0.03")
    # stop 40 pkt: limit straty 1 / 40 = 0.025 < 0.03997 → 0.02
    wide = plan(make_signal(stop_level=D("4963.7")), session_index=9, allowed=True)
    assert (wide.stop_distance, wide.target_distance, wide.size) == (D("40.0"), D("60.00"), D("0.02"))


@pytest.mark.parametrize(
    ("session_index", "allowed", "size"),
    [(4, True, "0.01"), (5, True, "0.03"), (9, False, "0.01")],
)
def test_size_policy_needs_both_enough_sessions_and_owner_permission(session_index, allowed, size):
    p = plan(make_signal(stop_level=D("5000")), session_index=session_index, allowed=allowed)
    assert p.size == D(size)


def test_rule_size_below_the_minimum_is_skipped_but_the_minimum_mode_is_not():
    big_min = make_rules(min_size="0.1")
    assert plan(make_signal(stop_level=D("5000")), rules=big_min, session_index=9, allowed=True) == Skip(
        SKIPPED_MIN_SIZE
    )
    assert plan(make_signal(stop_level=D("5000")), rules=big_min).size == D("0.1")


@pytest.mark.parametrize(
    ("side", "stop_level"),
    [
        (Side.BUY, "5003.7"),  # stop równo na cenie wejścia (ask)
        (Side.BUY, "5004"),  # cena już poniżej poziomu stopu
        (Side.SELL, "5003.5"),  # stop równo na cenie wejścia (bid)
        (Side.SELL, "5003"),  # cena już powyżej poziomu stopu
        (Side.BUY, "5002.8"),  # 0.9 < min. stop brokera 1.0
        (Side.SELL, "5004.4"),  # 0.9 < min. stop brokera 1.0
    ],
)
def test_stop_at_or_through_the_entry_or_closer_than_the_broker_minimum_is_too_tight(side, stop_level):
    assert plan(make_signal(side=side, stop_level=D(stop_level))) == Skip(STOP_TOO_TIGHT)


def test_the_broker_minimum_is_checked_after_rounding_up():
    # 5003.7 - 5002.75 = 0.95 → w górę do 1.0 = minimum brokera → wchodzi
    p = plan(make_signal(stop_level=D("5002.75")))
    assert p.stop_distance == D("1.0") and p.target_distance == D("1.50")
    # dokładnie minimum brokera też wchodzi
    assert plan(make_signal(stop_level=D("5002.7"))).stop_distance == D("1.0")


def test_plan_carries_the_decision_quote_and_is_frozen():
    p = plan(make_signal())
    assert p.decision_quote is QUOTE
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.size = D("1")  # type: ignore[misc]


def test_plan_is_pure():
    s = make_signal(stop_level=D("5000"))
    assert plan(s, session_index=9, allowed=True) == plan(s, session_index=9, allowed=True)


@pytest.mark.parametrize(
    ("quote_epic", "rules_epic", "cfg_instrument"),
    [("DE40", "US500", "US500"), ("US500", "DE40", "US500"), ("US500", "US500", "DE40")],
)
def test_quote_rules_and_config_must_be_for_the_same_instrument_in_entry_plan(quote_epic, rules_epic, cfg_instrument):
    quote = dataclasses.replace(QUOTE, epic=quote_epic)
    cfg = dataclasses.replace(CFG, instrument=cfg_instrument)
    with pytest.raises(ValueError, match="same epic"):
        entry_plan(make_signal(), quote, make_rules(epic=rules_epic), RISK, cfg)


@pytest.mark.parametrize("bad", [-1, True, 1.0, "3", None])
def test_session_index_must_be_a_non_negative_integer(bad):
    with pytest.raises(ValueError, match="session_index"):
        entry_plan(make_signal(), QUOTE, RULES, RISK, CFG, session_index=bad)  # type: ignore[arg-type]


def test_entry_plan_requires_a_bar_signal():
    with pytest.raises(TypeError, match="BarSignal"):
        entry_plan("BUY", QUOTE, RULES, RISK, CFG)  # type: ignore[arg-type]


def test_size_policy_uses_the_config_fixed_min_size_sessions():
    cfg0 = dataclasses.replace(CFG, fixed_min_size_sessions=0)
    p = entry_plan(
        make_signal(stop_level=D("5000")), QUOTE, RULES, RISK, cfg0, session_index=0, rule_size_allowed=True
    )
    assert p.size == D("0.03")


# ---------------------------------------------------------------------------------------------------------------
# moduł: czysta funkcja, bez wejścia/wyjścia, zegara, brokera, floatów i zaszytych progów
# ---------------------------------------------------------------------------------------------------------------


def test_module_has_no_io_clock_broker_or_float():
    tree = ast.parse(Path(strategy_bars.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "gwstock.config",
        "gwstock.strategy",
        "gwstock.types",
    }
    assert imported <= allowed, f"unexpected imports in strategy_bars.py: {sorted(imported - allowed)}"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert "float" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "now" not in {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def test_module_has_no_hard_coded_thresholds():
    """Progi (n, 0.60, 0.75, 0.25, 1.5, 50) są w `config/lane_us500_b1_m5.toml`; w kodzie wolno tylko 0 i 1."""
    tree = ast.parse(Path(strategy_bars.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    decimal_context = {  # stałe samego kontekstu Decimal (prec, Emin, Emax), nie progi reguły
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Context"
        for inner in ast.walk(node)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings or id(node) in decimal_context:
            continue
        if isinstance(node.value, bool):
            continue
        if isinstance(node.value, int):
            assert node.value in (0, 1), f"hard-coded number {node.value} at line {node.lineno}"
        if isinstance(node.value, str):
            with pytest.raises(ArithmeticError):
                D(node.value)  # napis w kodzie nie może być liczbą dziesiętną (InvalidOperation to ArithmeticError)


# ---------------------------------------------------------------------------------------------------------------
# test krzyżowy z research/candle_strategies.py (źródłem prawdy reguły): kierunek i poziom stopu
# ---------------------------------------------------------------------------------------------------------------

RESEARCH = Path(__file__).resolve().parents[2] / "research" / "candle_strategies.py"
needs_research = pytest.mark.skipif(not RESEARCH.exists(), reason="brak katalogu research/ (obraz testowy)")


@pytest.fixture(scope="module")
def cs():
    spec = importlib.util.spec_from_file_location("candle_strategies_for_bars", RESEARCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Ceny w ósemkach punktu (liczby całkowite): bid, ask i środek są wielokrotnościami 1/8, więc float z badań i Decimal
# silnika liczą dokładnie to samo, a granice progów (0.60, 0.75, 0.25, wybicie o b) da się trafić dokładnie.
def eighths(m: int) -> Decimal:
    return D(m) * D("0.125")


class Series:
    """Seria świec w ósemkach: środek OHLC i połowa spreadu (w ósemkach) dla każdej z czterech cen osobno."""

    def __init__(self, mids: list[tuple[int, int, int, int]], halves: list[tuple[int, int, int, int]]):
        self.mids, self.halves = mids, halves

    def research_bars(self, cs):
        bars = []
        for k, (m, s) in enumerate(zip(self.mids, self.halves)):
            bid = [(x - y) / 8 for x, y in zip(m, s)]
            ask = [(x + y) / 8 for x, y in zip(m, s)]
            bars.append(cs.Bar(T0 + timedelta(minutes=5 * k), *bid, *ask))
        return bars

    def engine_candles(self):
        out = []
        for k, (m, s) in enumerate(zip(self.mids, self.halves)):
            mid = [(eighths(x - y) + eighths(x + y)) / 2 for x, y in zip(m, s)]  # (bid + ask) / 2 jak w adapterze
            out.append(Candle("US500", T0 + timedelta(minutes=5 * k), *mid))
        return out

    def last_spread(self) -> Decimal:
        return eighths(2 * self.halves[-1][3])  # ask_c - bid_c ostatniej świecy (jak `bars[i].ac - bars[i].bc`)


def _prior_walk(r: random.Random, count: int) -> tuple[list[tuple[int, int, int, int]], int]:
    mids, last_close = [], 40000 + r.randint(-800, 800)
    for _ in range(count):
        o = last_close
        c = o + r.randint(-6, 6)
        mids.append((o, max(o, c) + r.randint(0, 6), min(o, c) - r.randint(0, 6), c))
        last_close = c
    return mids, last_close


def random_series(r: random.Random) -> Series:
    total = r.choice([14, 15, 16])
    mids, _ = _prior_walk(r, total - 1)
    window = mids[-12:]
    upper, lower = max(m[1] for m in window), min(m[2] for m in window)
    side = r.choice([1, 1, -1, -1, 0])  # 0: świeca bez związku z zakresem (większość z nich nie daje sygnału)
    rng_e = r.randint(4, 30)
    if side == 1:  # kandydat na long: zamknięcie przy górze zakresu świecy, wybicie blisko progu, korpus 0.4-1.0
        c = upper + r.randint(-2, 14)
        h = c + r.randint(0, max(1, rng_e // 3))
        low = h - rng_e
        o = max(low, c - r.randint(rng_e * 2 // 5, rng_e))
    elif side == -1:
        c = lower - r.randint(-2, 14)
        low = c - r.randint(0, max(1, rng_e // 3))
        h = low + rng_e
        o = min(h, c + r.randint(rng_e * 2 // 5, rng_e))
    else:
        o = mids[-1][3]
        c = o + r.randint(-12, 12)
        h, low = max(o, c) + r.randint(0, 5), min(o, c) - r.randint(0, 5)
    mids.append((o, h, low, c))
    halves = [tuple(r.randint(0, 4) for _ in range(4)) for _ in mids]
    return Series(mids, halves)


def tie_series(r: random.Random) -> Series:
    """Świeca sygnału na granicach: korpus 0.60 i clv 0.75 / 0.25 (zakres 20k ósemek), wybicie o b +-1 ósemkę."""
    total = r.choice([14, 15, 16])
    mids, _ = _prior_walk(r, total - 1)
    window = mids[-12:]
    upper, lower = max(m[1] for m in window), min(m[2] for m in window)
    halves = [tuple(r.randint(0, 4) for _ in range(4)) for _ in mids]
    close_half = 2 * r.randint(0, 4)  # parzysta połowa spreadu zamknięcia: 0.25 x spread to całkowita liczba ósemek
    halves.append((r.randint(0, 4), r.randint(0, 4), r.randint(0, 4), close_half))
    # spread = 2 x close_half ósemek punktu, więc 0.25 x spread = close_half / 2 ósemek; krok stopu 0.25 pkt = 2 ósemki
    b = max(2, close_half // 2)
    k = r.randint(1, 3)
    rng_e = 20 * k
    body = 12 * k + r.choice([-1, 0, 1])
    if r.random() < 0.5:
        c = upper + b + r.choice([-1, 0, 1])
        low = c - (15 * k + r.choice([-1, 0, 1]))
        h, o = low + rng_e, c - body
    else:
        c = lower - b + r.choice([-1, 0, 1])
        low = c - (5 * k + r.choice([-1, 0, 1]))
        h, o = low + rng_e, c + body
    mids.append((o, h, low, c))
    return Series(mids, halves)


def _compare(cs, series_: Series, step: str, counts: dict[str, int]) -> None:
    bars = series_.research_bars(cs)
    candles = series_.engine_candles()
    spread = series_.last_spread()
    last = len(bars) - 1
    expected = cs.signals("B1", CFG.lookback_bars, cs.TF["M5"]["limits"], bars, last, None, None, float(spread))
    got = bar_signal(candles, spread, make_rules(stop_step=step), CFG)
    if expected is None:
        assert got is None, (series_.mids, series_.halves)
        counts["none"] += 1
        return
    side, stop, target, limit = expected
    assert got is not None, (series_.mids, series_.halves)
    assert got.side is (Side.BUY if side == 1 else Side.SELL)
    assert abs(float(got.stop_level) - stop) <= 1e-9, (float(got.stop_level), stop)
    assert float(got.target_r) == target and got.time_limit_min == limit
    counts["long" if side == 1 else "short"] += 1


@needs_research
def test_config_values_equal_the_research_rule(cs):
    assert CFG.lookback_bars == cs.TF["M5"]["n"]
    assert CFG.bar_minutes == cs.TF["M5"]["minutes"]
    assert CFG.time_limit_min == cs.TF["M5"]["limits"]["B1"]


@needs_research
@pytest.mark.parametrize("step", ["0.1", "0.25", "0.5", "0"])
def test_cross_check_with_research_on_random_series(cs, monkeypatch, step):
    """1000 serii na krok stopu: ten sam kierunek i ten sam poziom stopu (tolerancja 1e-9) co `signals` z badań."""
    monkeypatch.setattr(cs, "STEP", float(step))
    r = random.Random(f"bars-random-{step}")
    counts = {"long": 0, "short": 0, "none": 0}
    for _ in range(1000):
        _compare(cs, random_series(r), step, counts)
    assert min(counts.values()) >= 100, counts  # test nie jest pusty: są sygnały obu stron i brak sygnału


@needs_research
def test_cross_check_with_research_on_threshold_ties(cs, monkeypatch):
    """Granice progów (korpus 0.60, clv 0.75 / 0.25, wybicie dokładnie o b): 2000 serii z cenami w ósemkach punktu."""
    monkeypatch.setattr(cs, "STEP", 0.25)
    r = random.Random("bars-ties")
    counts = {"long": 0, "short": 0, "none": 0}
    for _ in range(2000):
        _compare(cs, tie_series(r), "0.25", counts)
    assert min(counts.values()) >= 100, counts

