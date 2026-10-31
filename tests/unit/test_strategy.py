"""Testy strategii ORB (spec §5, §11): każda reguła ma przypięty test z liczbami policzonymi ręcznie.

Konfiguracje `STRAT` i `RISK` są wpisane w teście na stałe (wartości ze spec §5), a osobny test pilnuje, że
`config/*.toml` nadal im odpowiada: zmiana konfiguracji ma wymusić ponowne sprawdzenie ręcznych liczb.

Bazowy scenariusz: świeca 25000 → 25010 (byczka), ATR 300, notowanie 24999.1 / 25000.9 (spread 1.8, mid 25000.0),
DE40 (min 0.001, krok 0.001, 1 EUR/pkt, min. stop 2.6). Stąd:
- stop = 0.10 × 300 = 30.0 (≥ 2.6, ≥ 3 × 1.8 = 5.4);
- limit straty: 1.00 / (30 × 1) = 0.0333…; limit dźwigni: 5 × 40 / (25000 × 1) = 0.008 → wielkość 0.008.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest

from gwstock import strategy
from gwstock.config import RiskConfig, StrategyConfig
from gwstock.strategy import (
    DOJI,
    NO_SIGNAL_DATA,
    SKIPPED_MIN_SIZE,
    STOP_TOO_TIGHT,
    EntrySignal,
    Skip,
    atr14,
    decide,
    round_down_to_step,
    round_up_to_step,
)
from gwstock.types import Candle, InstrumentRules, Quote, Side

D = Decimal

STRAT = StrategyConfig(
    instrument="DE40",
    exchange_profile="XETRA",
    range_minutes=5,
    stop_atr_fraction=D("0.10"),
    min_stop_spread_multiple=D("3"),
    slippage_guard_fraction=D("0.25"),
    flatten_before_close_min=5,
    entry_deadline_after_range_min=5,
    fixed_min_size_sessions=5,
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

OPEN_UTC = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)  # 09:00 w Berlinie (czas zimowy)


def make_candle(o: str, c: str, *, h: str | None = None, low: str | None = None) -> Candle:
    o_, c_ = D(o), D(c)
    return Candle(
        "DE40", OPEN_UTC, o_, D(h) if h is not None else max(o_, c_), D(low) if low is not None else min(o_, c_), c_
    )


def make_quote(bid: str = "24999.1", ask: str = "25000.9") -> Quote:
    return Quote("DE40", D(bid), D(ask), OPEN_UTC + timedelta(minutes=5))


def make_rules(
    *, min_size: str = "0.001", step: str = "0.001", vpp: str = "1", min_stop: str = "2.6", stop_step: str = "0"
) -> InstrumentRules:
    return InstrumentRules("DE40", D(min_size), D(step), D(vpp), D("0.05"), D(min_stop), D(stop_step))


BULL = make_candle("25000", "25010")
BEAR = make_candle("25010", "25000")


def run(
    *,
    first: Candle | None = BULL,
    atr: str = "300",
    quote: Quote | None = None,
    rules: InstrumentRules | None = None,
    strat: StrategyConfig = STRAT,
    risk: RiskConfig = RISK,
    session_index: int = 9,
    rule_size_allowed: bool = True,
):
    """`decide` z bazowym scenariuszem; domyślnie tryb wielkości wg reguł (sesja 9 i zgoda właściciela)."""
    return decide(
        first,
        D(atr),
        quote if quote is not None else make_quote(),
        rules if rules is not None else make_rules(),
        strat,
        risk,
        session_index=session_index,
        rule_size_allowed=rule_size_allowed,
    )


# ---------------------------------------------------------------------------------------------------------------
# zaokrąglanie do kroku
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        ("0.0333333", "0.001", "0.033"),
        ("0.008", "0.001", "0.008"),  # dokładna wielokrotność zostaje
        ("0.0009999", "0.001", "0.000"),  # poniżej kroku → 0
        ("7.74", "0.5", "7.5"),
        ("23", "5", "20"),
        ("0", "0.001", "0.000"),
        ("-0.0005", "0.001", "-0.001"),  # w dół = ku −∞
    ],
)
def test_round_down_to_step_table(value, step, expected):
    result = round_down_to_step(D(value), D(step))
    assert result == D(expected)
    assert isinstance(result, Decimal)


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        ("30.07", "0.1", "30.1"),
        ("30.0", "0.1", "30.0"),  # dokładna wielokrotność zostaje
        ("30.01", "0.1", "30.1"),
        ("2.65", "0.1", "2.7"),
        ("111.110", "0.1", "111.2"),
        ("0", "0.1", "0.0"),
        ("21", "5", "25"),
        ("-0.0004", "0.001", "0.000"),  # w górę = ku +∞
    ],
)
def test_round_up_to_step_table(value, step, expected):
    result = round_up_to_step(D(value), D(step))
    assert result == D(expected)
    assert isinstance(result, Decimal)


def test_round_down_keeps_repeating_decimals_exact():
    third = D(1) / D(30)  # 0.03333333333333333333333333333 (28 cyfr)
    assert round_down_to_step(third, D("0.001")) == D("0.033")


def test_round_down_is_exact_just_below_a_step_boundary():
    # 0.007999…9 (28 dziewiątek) to wciąż mniej niż 0.008; dzielenie w precyzji 28 cyfr dałoby 8 i błędne 0.008
    just_below = D("0.007" + "9" * 28)
    assert round_down_to_step(just_below, D("0.001")) == D("0.007")


def test_round_up_is_exact_just_above_a_step_boundary():
    # 30.000…01 (27 zer) to więcej niż 30.0; dzielenie w precyzji 28 cyfr dałoby 300 i błędne 30.0
    just_above = D("30." + "0" * 27 + "1")
    assert round_up_to_step(just_above, D("0.1")) == D("30.1")


def test_rounding_is_exact_for_huge_values():
    big = D("123456789012345678901234567890.7")
    assert round_down_to_step(big, D(1)) == D("123456789012345678901234567890")
    assert round_up_to_step(big, D(1)) == D("123456789012345678901234567891")


def test_rounding_ignores_the_ambient_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert round_down_to_step(D("0.0333333"), D("0.001")) == D("0.033")
        assert round_up_to_step(D("30.07"), D("0.1")) == D("30.1")


@pytest.mark.parametrize("fn", [round_down_to_step, round_up_to_step])
@pytest.mark.parametrize("step", ["0", "-0.001"])
def test_rounding_rejects_non_positive_step(fn, step):
    with pytest.raises(ValueError, match="step"):
        fn(D("1"), D(step))


@pytest.mark.parametrize("fn", [round_down_to_step, round_up_to_step])
def test_rounding_rejects_float_and_non_finite_input(fn):
    with pytest.raises(TypeError, match="value"):
        fn(0.033, D("0.001"))
    with pytest.raises(TypeError, match="step"):
        fn(D("0.033"), 0.001)
    with pytest.raises(ValueError, match="value"):
        fn(D("NaN"), D("0.001"))
    with pytest.raises(ValueError, match="value"):
        fn(D("Infinity"), D("0.001"))
    with pytest.raises(ValueError, match="step"):
        fn(D("1"), D("Infinity"))


# ---------------------------------------------------------------------------------------------------------------
# ATR14
# ---------------------------------------------------------------------------------------------------------------

DAY0 = datetime(2026, 10, 12, tzinfo=timezone.utc)


def day_candle(i: int, h: str, low: str, c: str) -> Candle:
    """Dzienna świeca nr `i` (kolejne dni od DAY0); otwarcie = zamknięcie, bo ATR go nie używa."""
    return Candle("DE40", DAY0 + timedelta(days=i), D(c), D(h), D(low), D(c))


def build(rows: list[tuple[str, str, str]], offset: int = 0) -> list[Candle]:
    return [day_candle(offset + i, *row) for i, row in enumerate(rows)]


# (H, L, C); komentarz: H−L, |H−Cprev|, |L−Cprev| → TR. Pierwsza świeca daje tylko Cprev = 1000.
GAP_ROWS = [
    ("1000", "1000", "1000"),
    ("1030", "1010", "1020"),  # 20, 30, 10 -> 30  (luka w górę: |H−Cprev| wygrywa)
    ("1025", "1005", "1010"),  # 20,  5, 15 -> 20
    ("1015", "990", "995"),  #   25,  5, 20 -> 25
    ("985", "960", "970"),  #    25, 10, 35 -> 35  (luka w dół: |L−Cprev| wygrywa)
    ("980", "965", "975"),  #    15, 10,  5 -> 15
    ("1000", "990", "995"),  #   10, 25, 15 -> 25
    ("1005", "985", "1000"),  #  20, 10, 10 -> 20
    ("1000", "1000", "1000"),  # 0,  0,  0 ->  0  (zerowy zakres)
    ("1040", "1035", "1038"),  # 5, 40, 35 -> 40
    ("1042", "1030", "1031"),  # 12, 4,  8 -> 12
    ("1031", "1001", "1005"),  # 30, 0, 30 -> 30  (remis zakresu z |L−Cprev|)
    ("1010", "1000", "1008"),  # 10, 5,  5 -> 10
    ("1010", "970", "975"),  #   40, 2, 38 -> 40
    ("995", "980", "990"),  #    15, 20, 5 -> 20
]
# suma TR = 30+20+25+35+15+25+20+0+40+12+30+10+40+20 = 322; 322 / 14 = 23
GAP_ATR = D("23")


def test_atr14_constant_range_of_100_is_100():
    candles = [day_candle(i, "25050", "24950", "25000") for i in range(15)]
    assert atr14(candles) == D("100")


def test_atr14_matches_hand_computed_value_with_gaps():
    candles = build(GAP_ROWS)
    assert len(candles) == 15
    assert atr14(candles) == GAP_ATR


def test_atr14_result_is_decimal_and_does_not_mutate_input():
    candles = build(GAP_ROWS)
    snapshot = list(candles)
    result = atr14(candles)
    assert isinstance(result, Decimal)
    assert candles == snapshot


def test_atr14_uses_only_the_last_15_candles():
    junk = [day_candle(i, "9999", "1", "5000") for i in range(5)]
    candles = junk + build(GAP_ROWS, offset=5)
    assert len(candles) == 20
    assert atr14(candles) == GAP_ATR


def test_atr14_oldest_candle_of_the_window_only_supplies_its_close():
    rows = list(GAP_ROWS)
    rows[0] = ("5000", "10", "1000")  # skrajne H/L, to samo zamknięcie → nic się nie zmienia
    assert atr14(build(rows)) == GAP_ATR


def test_atr14_does_not_round_the_result():
    rows = [("1000", "1000", "1000")] * 14 + [("1001", "1000", "1000")]  # jedno TR = 1, reszta 0 → 1/14
    result = atr14(build(rows))
    assert abs(Fraction(result) - Fraction(1, 14)) < Fraction(1, 10**40)  # co najmniej 40 cyfr, bez zaokrąglania
    assert result != D("0.07")


def test_atr14_ignores_the_ambient_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert atr14(build(GAP_ROWS)) == GAP_ATR


def test_atr14_needs_15_candles():
    with pytest.raises(ValueError, match="15"):
        atr14(build(GAP_ROWS)[:14])
    with pytest.raises(ValueError, match="15"):
        atr14([])


def test_atr14_requires_strictly_ascending_starts():
    candles = build(GAP_ROWS)
    swapped = list(candles)
    swapped[6], swapped[7] = swapped[7], swapped[6]
    with pytest.raises(ValueError, match="ascending"):
        atr14(swapped)
    with pytest.raises(ValueError, match="ascending"):
        atr14(list(reversed(candles)))
    duplicated = list(candles)
    duplicated[7] = duplicated[6]
    with pytest.raises(ValueError, match="ascending"):
        atr14(duplicated)


def test_atr14_checks_ordering_of_the_whole_input_not_only_the_window():
    junk = [day_candle(i, "9999", "1", "5000") for i in range(5)]
    junk[0], junk[1] = junk[1], junk[0]  # nieporządek poza oknem 15 ostatnich
    with pytest.raises(ValueError, match="ascending"):
        atr14(junk + build(GAP_ROWS, offset=5))


def test_atr14_rejects_a_candle_with_high_below_low():
    rows = list(GAP_ROWS)
    rows[5] = ("965", "980", "975")  # zamienione H i L: błąd danych, nie ma co liczyć
    with pytest.raises(ValueError, match="high"):
        atr14(build(rows))


# ---------------------------------------------------------------------------------------------------------------
# decide: plan z briefu
# ---------------------------------------------------------------------------------------------------------------


def test_long_when_first_candle_bullish_and_size_rounds_down():
    quote = make_quote("24999.1", "25000.9")
    s = decide(
        make_candle("25000", "25010"), D("300"), quote, make_rules(), STRAT, RISK, session_index=9,
        rule_size_allowed=True,
    )  # fmt: skip
    assert s.side == Side.BUY and s.stop_distance == D("30.0") and s.size == D("0.008")
    # min(1.00/(30×1)=0.0333, 5×40/(25000×1)=0.008) → 0.008 (krok 0.001)
    assert s.decision_quote is quote


def test_first_sessions_use_minimum_size():
    s = run(session_index=2)  # nawet ze zgodą właściciela: poniżej fixed_min_size_sessions → min_size
    assert s.size == D("0.001") and s.stop_distance == D("30.0") and s.side == Side.BUY


def test_short_when_bearish():
    s = run(first=BEAR)
    assert s.side == Side.SELL and s.stop_distance == D("30.0") and s.size == D("0.008")


def test_short_and_long_get_the_same_stop_and_size():
    assert dataclasses.replace(run(first=BULL), side=Side.SELL) == run(first=BEAR)
    assert dataclasses.replace(run(first=BULL, session_index=0), side=Side.SELL) == run(first=BEAR, session_index=0)


def test_doji_is_skipped():
    assert run(first=make_candle("25000", "25000")) == Skip("DOJI")


def test_doji_is_skipped_whatever_the_range_of_the_candle():
    assert run(first=make_candle("25000", "25000", h="25100", low="24900")) == Skip(DOJI)


def test_missing_first_candle_is_no_signal_data():
    q = make_quote()
    assert decide(None, D("300"), q, make_rules(), STRAT, RISK) == Skip("NO_SIGNAL_DATA")
    assert run(first=None) == Skip(NO_SIGNAL_DATA)


def test_stop_not_smaller_than_broker_minimum():
    s = run(rules=make_rules(min_stop="40"))  # 0.10 × 300 = 30 < 40
    assert s.stop_distance == D("40")
    assert s.size == D("0.008")  # limit dźwigni nadal wiąże (1.00 / 40 = 0.025)


def test_stop_equal_to_broker_minimum_is_unchanged():
    assert run(rules=make_rules(min_stop="30")).stop_distance == D("30")


def test_stop_tighter_than_three_spreads_is_skipped():
    # ATR 30 → stop 3.0 (≥ 2.6) < 3 × 1.8 = 5.4
    assert run(atr="30") == Skip("STOP_TOO_TIGHT")


def test_below_minimum_size_is_skipped():
    # wielkość wg reguł 0.008 < min. 0.01
    assert run(rules=make_rules(min_size="0.01")) == Skip("SKIPPED_MIN_SIZE")


# ---------------------------------------------------------------------------------------------------------------
# polityka wielkości (ruling 1): minimalna, dopóki NIE zachodzi jednocześnie sesja ≥ N i zgoda właściciela
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_index", "rule_size_allowed", "expected"),
    [
        (9, True, "0.008"),  # plan z briefu: obie przesłanki spełnione
        (5, True, "0.008"),  # granica: session_index == fixed_min_size_sessions → reguły
        (4, True, "0.001"),  # granica: o jedną sesję za wcześnie → minimum
        (2, True, "0.001"),
        (0, True, "0.001"),
        (9, False, "0.001"),  # brak zgody właściciela (L1) → minimum, choć sesji dość
        (5, False, "0.001"),
        (100, False, "0.001"),
        (2, False, "0.001"),
    ],
)
def test_size_policy_needs_both_enough_sessions_and_owner_permission(session_index, rule_size_allowed, expected):
    s = run(session_index=session_index, rule_size_allowed=rule_size_allowed)
    assert s.size == D(expected)


def test_defaults_are_the_safe_minimum_size():
    # bez session_index i bez rule_size_allowed: minimum
    s = decide(BULL, D("300"), make_quote(), make_rules(), STRAT, RISK)
    assert s.size == D("0.001")
    # sama liczba sesji nie wystarcza
    s = decide(BULL, D("300"), make_quote(), make_rules(), STRAT, RISK, session_index=50)
    assert s.size == D("0.001")
    # sama zgoda nie wystarcza (domyślnie session_index=0)
    s = decide(BULL, D("300"), make_quote(), make_rules(), STRAT, RISK, rule_size_allowed=True)
    assert s.size == D("0.001")


@pytest.mark.parametrize("truthy", [1, "yes", "True", object(), [1]])
def test_only_an_explicit_true_enables_rule_size(truthy):
    s = decide(
        BULL, D("300"), make_quote(), make_rules(), STRAT, RISK, session_index=9,
        rule_size_allowed=truthy,  # type: ignore[arg-type]
    )  # fmt: skip
    assert s.size == D("0.001")


def test_boundary_follows_the_configured_number_of_sessions():
    strat3 = dataclasses.replace(STRAT, fixed_min_size_sessions=3)
    assert run(strat=strat3, session_index=2).size == D("0.001")
    assert run(strat=strat3, session_index=3).size == D("0.008")
    strat0 = dataclasses.replace(STRAT, fixed_min_size_sessions=0)
    assert run(strat=strat0, session_index=0).size == D("0.008")
    assert run(strat=strat0, session_index=0, rule_size_allowed=False).size == D("0.001")


@pytest.mark.parametrize("bad", [-1, 1.5, "9", True, None])
def test_session_index_must_be_a_non_negative_integer(bad):
    with pytest.raises(ValueError, match="session_index"):
        run(session_index=bad)  # type: ignore[arg-type]


def test_minimum_size_mode_never_skips_for_size():
    # w trybie minimalnym wielkość to rules.min_size, więc SKIPPED_MIN_SIZE nie ma jak zajść
    s = run(rules=make_rules(min_size="0.01"), session_index=2)
    assert s.size == D("0.01")
    assert run(rules=make_rules(min_size="0.01"), rule_size_allowed=False).size == D("0.01")


# ---------------------------------------------------------------------------------------------------------------
# wielkość wg reguł: limit straty vs limit dźwigni (ręcznie)
# ---------------------------------------------------------------------------------------------------------------

QUOTE_26000 = ("25999.1", "26000.9")  # mid 26000.0, spread 1.8


@pytest.mark.parametrize(
    ("label", "atr", "risk_changes", "rule_kwargs", "quote", "expected"),
    [
        # limit dźwigni wiąże: stop 30 → strata 0.0333, dźwignia 0.008
        ("leverage-limited", "300", {}, {}, None, "0.008"),
        # limit straty wiąże: stop 150 → 1/150 = 0.00666… → 0.006 (dźwignia 0.008)
        ("loss-limited", "1500", {}, {}, None, "0.006"),
        # oba limity równe 0.008: stop 125 → 1/125 = 0.008 dokładnie (dokładna wielokrotność nie ginie)
        ("tie", "1250", {}, {}, None, "0.008"),
        # wartość punktu 2: strata 1/(30×2)=0.01666…, dźwignia 5×40/(25000×2)=0.004 → 0.004
        ("vpp2 leverage-limited", "300", {}, {"vpp": "2"}, None, "0.004"),
        # wartość punktu 2, stop 150: strata 1/(150×2)=0.00333…, dźwignia 0.004 → 0.003
        ("vpp2 loss-limited", "1500", {}, {"vpp": "2"}, None, "0.003"),
        # kapitał 100: dźwignia 5×100/25000 = 0.02; strata 0.0333 → 0.02
        ("bigger capital", "300", {"virtual_capital": D("100")}, {}, None, "0.02"),
        # dźwignia 2: 2×40/25000 = 0.0032 → 0.003
        ("lower leverage", "300", {"max_effective_leverage": D("2")}, {}, None, "0.003"),
        # mid 26000: 200/26000 = 0.0076923… → 0.007
        ("higher mid", "300", {}, {}, QUOTE_26000, "0.007"),
        # max strata 0.50, stop 150: 0.5/150 = 0.00333… → 0.003
        ("lower max loss", "1500", {"max_loss_per_trade": D("0.50")}, {}, None, "0.003"),
        # krok 0.005 (min 0.005): min(0.0333, 0.008) = 0.008 → 0.005
        ("coarser step", "300", {}, {"step": "0.005", "min_size": "0.005"}, None, "0.005"),
        # wielkość dokładnie równa minimum nie jest odrzucana
        ("exactly min size", "300", {}, {"min_size": "0.008"}, None, "0.008"),
    ],
)
def test_rule_size_is_the_smaller_of_loss_and_leverage_limits_floored_to_step(
    label, atr, risk_changes, rule_kwargs, quote, expected
):
    q = make_quote(*quote) if quote else None
    s = run(atr=atr, risk=dataclasses.replace(RISK, **risk_changes), rules=make_rules(**rule_kwargs), quote=q)
    assert s.size == D(expected), label


def test_huge_atr_gives_size_below_minimum_and_is_skipped_in_rule_mode():
    # ATR 15000 → stop 1500: 1.00/1500 = 0.000666… → floor do 0.001 = 0.000 < 0.001
    assert run(atr="15000") == Skip("SKIPPED_MIN_SIZE")


def test_huge_atr_in_minimum_size_mode_returns_a_signal_that_the_risk_gate_must_block():
    # DOKUMENTOWANE: tryb minimalny nie sprawdza limitu straty (to robi bramka ryzyka, LOSS_PER_TRADE).
    # Stop 1500 × 0.001 × 1 = 1.50 EUR > max_loss_per_trade 1.00, więc strategia zwraca sygnał, a bramka odrzuca.
    s = run(atr="15000", session_index=2)
    assert s.stop_distance == D("1500") and s.size == D("0.001")
    assert s.size * s.stop_distance * D("1") > RISK.max_loss_per_trade


def test_rule_mode_rejects_non_positive_divisors():
    with pytest.raises(ValueError, match="value_per_point"):
        run(rules=make_rules(vpp="0"))
    with pytest.raises(ValueError, match="mid"):
        run(quote=make_quote("0", "0"))


def test_minimum_size_mode_does_not_divide_so_zero_vpp_does_not_matter():
    assert run(rules=make_rules(vpp="0"), session_index=2).size == D("0.001")


# ---------------------------------------------------------------------------------------------------------------
# spread i minimalna odległość stopu
# ---------------------------------------------------------------------------------------------------------------


def test_stop_equal_to_three_spreads_passes():
    # spread 10 → 3 × 10 = 30; stop 30.0 nie jest mniejszy
    s = run(quote=make_quote("24995", "25005"))
    assert s.stop_distance == D("30.0") and s.size == D("0.008")


def test_stop_just_below_three_spreads_is_skipped():
    # ATR 299.9 → stop 29.99 < 30
    assert run(atr="299.9", quote=make_quote("24995", "25005")) == Skip("STOP_TOO_TIGHT")
    # spread 10.10 → 30.30 > 30.0
    assert run(quote=make_quote("24994.95", "25005.05")) == Skip(STOP_TOO_TIGHT)


def test_zero_spread_quote_is_accepted():
    s = run(quote=make_quote("25000", "25000"))
    assert s.stop_distance == D("30.0") and s.size == D("0.008")
    # przy zerowym spreadzie liczy się już tylko minimalny stop brokera
    assert run(atr="30", quote=make_quote("25000", "25000")).stop_distance == D("3.0")


def test_crossed_quote_cannot_reach_the_strategy():
    with pytest.raises(ValueError, match="crossed"):
        make_quote("25001", "25000")


def test_spread_multiple_comes_from_the_config():
    strat = dataclasses.replace(STRAT, min_stop_spread_multiple=D("20"))  # 20 × 1.8 = 36 > 30
    assert run(strat=strat) == Skip(STOP_TOO_TIGHT)
    strat = dataclasses.replace(STRAT, min_stop_spread_multiple=D("16"))  # 16 × 1.8 = 28.8 ≤ 30
    assert run(strat=strat).stop_distance == D("30.0")


def test_stop_fraction_comes_from_the_config():
    strat = dataclasses.replace(STRAT, stop_atr_fraction=D("0.20"))
    assert run(strat=strat).stop_distance == D("60.0")


def test_zero_stop_is_too_tight_not_a_division_by_zero():
    strat = dataclasses.replace(STRAT, stop_atr_fraction=D("0"))
    assert run(strat=strat, rules=make_rules(min_stop="0"), quote=make_quote("25000", "25000")) == Skip(STOP_TOO_TIGHT)


# ---------------------------------------------------------------------------------------------------------------
# krok stopu (ruling 2)
# ---------------------------------------------------------------------------------------------------------------


def test_stop_is_rounded_up_to_the_stop_step():
    s = run(atr="300.7", rules=make_rules(stop_step="0.1"))  # 0.10 × 300.7 = 30.07 → 30.1
    assert s.stop_distance == D("30.1")


def test_stop_that_is_already_a_multiple_of_the_step_is_unchanged():
    s = run(atr="300", rules=make_rules(stop_step="0.1"))  # 30.00 → 30.0
    assert s.stop_distance == D("30.0")
    assert run(atr="300.1", rules=make_rules(stop_step="0.1")).stop_distance == D("30.1")  # 30.01 → 30.1


def test_zero_stop_step_means_no_rounding():
    s = run(atr="300.7", rules=make_rules(stop_step="0"))
    assert s.stop_distance == D("30.07")


def test_broker_minimum_is_applied_before_the_step_rounding():
    # ATR 10 → 1.0 < min. stop 2.65 → 2.65 → w górę do kroku 0.1 → 2.7 (spread 0.8 → 2.4 ≤ 2.7)
    s = run(atr="10", rules=make_rules(min_stop="2.65", stop_step="0.1"), quote=make_quote("24999.6", "25000.4"))
    assert s.stop_distance == D("2.7")


def test_sizing_uses_the_rounded_stop_so_risk_never_grows():
    # kapitał 1000 → dźwignia (0.2) nie wiąże; ATR 1111.1 → stop 111.11 → 111.2 przy kroku 0.1
    risk = dataclasses.replace(RISK, virtual_capital=D("1000"))
    rounded = run(atr="1111.1", risk=risk, rules=make_rules(stop_step="0.1"))
    assert rounded.stop_distance == D("111.2")
    assert rounded.size == D("0.008")  # 1/111.2 = 0.008992… (z 111.11 wyszłoby 0.009: ryzyko 1.0000 > 1.00 przy 111.2)
    unrounded = run(atr="1111.1", risk=risk, rules=make_rules(stop_step="0"))
    assert unrounded.stop_distance == D("111.11")
    assert unrounded.size == D("0.009")
    assert rounded.size * rounded.stop_distance <= RISK.max_loss_per_trade


def test_spread_check_uses_the_rounded_stop():
    # ATR 299.5 → 29.95 → 30.0 (krok 0.1); spread 10 → próg 30.0: zaokrąglony stop przechodzi, surowy by odpadł
    quote = make_quote("24995", "25005")
    assert run(atr="299.5", quote=quote, rules=make_rules(stop_step="0.1")).stop_distance == D("30.0")
    assert run(atr="299.5", quote=quote, rules=make_rules(stop_step="0")) == Skip(STOP_TOO_TIGHT)


# ---------------------------------------------------------------------------------------------------------------
# kolejność kontroli i złe dane
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("atr", ["0", "-5", "-0.0001"])
def test_non_positive_atr_is_no_signal_data(atr):
    assert run(atr=atr) == Skip("NO_SIGNAL_DATA")


@pytest.mark.parametrize("atr", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_atr_is_no_signal_data(atr):
    assert run(atr=atr) == Skip("NO_SIGNAL_DATA")


@pytest.mark.parametrize("atr", [300.0, 300, "300", None])
def test_atr_must_be_a_decimal(atr):
    with pytest.raises(TypeError, match="atr"):
        decide(BULL, atr, make_quote(), make_rules(), STRAT, RISK)  # type: ignore[arg-type]


def test_check_order_data_then_doji_then_stop_then_size():
    doji = make_candle("25000", "25000")
    assert run(first=None, atr="0") == Skip(NO_SIGNAL_DATA)
    assert run(first=doji, atr="0") == Skip(NO_SIGNAL_DATA)  # zły ATR przed dojī
    assert run(first=doji, atr="30") == Skip(DOJI)  # doji przed kontrolą stopu
    # stop za ciasny i wielkość poniżej minimum jednocześnie → STOP_TOO_TIGHT
    assert run(atr="30", rules=make_rules(min_size="0.01")) == Skip(STOP_TOO_TIGHT)
    assert run(atr="30", session_index=2) == Skip(STOP_TOO_TIGHT)  # też w trybie minimalnym


def test_direction_comes_only_from_close_versus_open():
    # długie knoty nie zmieniają kierunku ani wyniku
    wicks_up = make_candle("25000", "25001", h="26000", low="24999")
    wicks_down = make_candle("25000", "25001", h="25001", low="24000")
    plain = make_candle("25000", "25001")
    assert run(first=wicks_up) == run(first=wicks_down) == run(first=plain)
    assert run(first=plain).side == Side.BUY
    assert run(first=make_candle("25000", "24999", h="26000", low="24000")).side == Side.SELL


# ---------------------------------------------------------------------------------------------------------------
# niezmienniki, czystość, typy wyniku
# ---------------------------------------------------------------------------------------------------------------


def test_decide_is_pure_calling_twice_gives_equal_results():
    args = (BULL, D("300"), make_quote(), make_rules(), STRAT, RISK)
    for kwargs in ({}, {"session_index": 9, "rule_size_allowed": True}):
        assert decide(*args, **kwargs) == decide(*args, **kwargs)
    assert decide(None, D("300"), make_quote(), make_rules(), STRAT, RISK) == decide(
        None, D("300"), make_quote(), make_rules(), STRAT, RISK
    )


def test_decide_ignores_the_ambient_decimal_context():
    kwargs = dict(
        atr="1111.1", rules=make_rules(stop_step="0.1"), risk=dataclasses.replace(RISK, virtual_capital=D("1000"))
    )
    expected = run(**kwargs)
    with localcontext() as ctx:
        ctx.prec = 4
        got = run(**kwargs)
    assert got == expected and isinstance(got, EntrySignal)


def test_signal_fields_are_exact_decimals_never_floats():
    s = run(atr="300.7", rules=make_rules(stop_step="0.1"))
    assert isinstance(s.stop_distance, Decimal) and isinstance(s.size, Decimal)
    assert isinstance(s.side, Side)


@pytest.mark.parametrize("stop_step", ["0", "0.1", "0.5"])
@pytest.mark.parametrize("session_index", [2, 9])
def test_signal_invariants_over_a_grid_of_atr_values(stop_step, session_index):
    rules = make_rules(stop_step=stop_step)
    quote = make_quote()
    checked = 0
    for atr_int in range(60, 3000, 37):
        atr = D(atr_int) / D(10) + D("0.03")  # także „brzydkie” wartości
        s = run(atr=str(atr), rules=rules, quote=quote, session_index=session_index)
        if isinstance(s, Skip):
            assert s.reason in {STOP_TOO_TIGHT, SKIPPED_MIN_SIZE}
            continue
        checked += 1
        stop, size = Fraction(s.stop_distance), Fraction(s.size)
        assert stop >= Fraction(rules.min_stop_distance)
        assert stop >= Fraction(STRAT.stop_atr_fraction) * Fraction(atr)  # zaokrąglenie tylko w górę
        assert stop >= 3 * Fraction(quote.spread)
        if rules.stop_step > 0:
            assert (stop / Fraction(rules.stop_step)).denominator == 1
        assert size >= Fraction(rules.min_size)
        assert (size / Fraction(rules.size_step)).denominator == 1
        if session_index >= STRAT.fixed_min_size_sessions:
            assert size * stop <= Fraction(RISK.max_loss_per_trade)
            assert size * Fraction(quote.mid) <= Fraction(RISK.max_effective_leverage) * Fraction(RISK.virtual_capital)
        else:
            assert s.size == rules.min_size
    assert checked > 10  # siatka faktycznie ćwiczy ścieżkę sygnału


# ---------------------------------------------------------------------------------------------------------------
# EntrySignal i Skip
# ---------------------------------------------------------------------------------------------------------------


def test_skip_reasons_are_exactly_the_four_documented_ones():
    for reason in ("NO_SIGNAL_DATA", "DOJI", "STOP_TOO_TIGHT", "SKIPPED_MIN_SIZE"):
        assert Skip(reason).reason == reason
    assert (NO_SIGNAL_DATA, DOJI, STOP_TOO_TIGHT, SKIPPED_MIN_SIZE) == (
        "NO_SIGNAL_DATA", "DOJI", "STOP_TOO_TIGHT", "SKIPPED_MIN_SIZE",
    )  # fmt: skip
    with pytest.raises(ValueError, match="reason"):
        Skip("TOO_SMALL")
    with pytest.raises(ValueError, match="reason"):
        Skip("doji")


def test_skip_and_entry_signal_are_frozen():
    skip = Skip("DOJI")
    with pytest.raises(dataclasses.FrozenInstanceError):
        skip.reason = "NO_SIGNAL_DATA"  # type: ignore[misc]
    signal = run()
    with pytest.raises(dataclasses.FrozenInstanceError):
        signal.size = D("1")  # type: ignore[misc]


def test_entry_signal_validates_its_fields():
    q = make_quote()
    ok = EntrySignal(Side.BUY, D("30"), D("0.008"), q)
    assert ok.decision_quote is q
    with pytest.raises(TypeError, match="side"):
        EntrySignal("BUY", D("30"), D("0.008"), q)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="stop_distance"):
        EntrySignal(Side.BUY, 30.0, D("0.008"), q)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="size"):
        EntrySignal(Side.BUY, D("30"), 0.008, q)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="stop_distance"):
        EntrySignal(Side.BUY, D("0"), D("0.008"), q)
    with pytest.raises(ValueError, match="size"):
        EntrySignal(Side.BUY, D("30"), D("0"), q)
    with pytest.raises(ValueError, match="size"):
        EntrySignal(Side.BUY, D("30"), D("NaN"), q)
    with pytest.raises(TypeError, match="decision_quote"):
        EntrySignal(Side.BUY, D("30"), D("0.008"), None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------------------
# konfiguracja i granice modułu
# ---------------------------------------------------------------------------------------------------------------


def test_pinned_configs_match_the_toml_files(strat_cfg, risk_cfg):
    # jeśli to padnie, ręczne liczby w tym pliku trzeba przeliczyć od nowa
    assert strat_cfg == STRAT
    assert risk_cfg == RISK


def test_real_config_reproduces_the_hand_verified_numbers(strat_cfg, risk_cfg):
    q = make_quote()
    s = decide(BULL, D("300"), q, make_rules(), strat_cfg, risk_cfg, session_index=9, rule_size_allowed=True)
    assert (s.side, s.stop_distance, s.size) == (Side.BUY, D("30.0"), D("0.008"))
    assert decide(BULL, D("30"), q, make_rules(), strat_cfg, risk_cfg, session_index=9, rule_size_allowed=True) == Skip(
        "STOP_TOO_TIGHT"
    )  # 3.0 < 5.4


def test_strategy_module_has_no_io_clock_or_float():
    tree = ast.parse(Path(strategy.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {"__future__", "dataclasses", "decimal", "fractions", "math", "gwstock.config", "gwstock.types"}
    assert imported <= allowed, f"unexpected imports in strategy.py: {sorted(imported - allowed)}"
    floats = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert not floats, "float literal in strategy.py"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "float" not in names
