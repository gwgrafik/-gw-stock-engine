"""Testy pasma B1/M5 (`lanes`): okna sesji, decyzja wejścia z filtrami dnia i wyjście zaplanowane (projekt wielu pasm,
zadanie 11).

Liczby policzone ręcznie. Scenariusz bazowy (US500, czas zimowy w Nowym Jorku od 1.11.2026, dzień 5.11.2026):
12 świec po 5000 (zakres 4999-5001, TR = 2) i świeca sygnału o początku 10:40 czasu NY (15:40 UTC), czyli 15 świec;
koniec świecy sygnału i chwila decyzji to 10:45 (15:45 UTC). Reguły instrumentu: krok stopu 0.1, min. stop 1.0,
min. wielkość 0.01. Notowanie z decyzji ma spread 0.4, więc bufor wybicia b = max(0.1, 0.25 x 0.4) = 0.1, a koszt rundy
= spread + 2 x 0.15 (założony poślizg) = 0.7.

Long: świeca (5000.4; 5004; 5000; 5003.6): zakres 4, korpus 3.2 (0.80), clv 0.90, zamknięcie > 5001.1; stop = 5000;
TR świecy sygnału = 4, więc suma 14 zakresów = 13 x 2 + 4 = 30 (ATR14 = 30 / 14). Notowanie bid 5003.8 / ask 5004.2:
cena wejścia po założonym poślizgu 5004.35, odległość stopu 4.35, odchylenie od zamknięcia 0.75.
Short: świeca (4999.6; 5000; 4996; 4996.4), stop = 5000, notowanie bid 4996.2 / ask 4996.6: cena wejścia 4996.05, 3.95.

Test krzyżowy z `research/candle_strategies.run` jest w drugiej połowie pliku.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock import lanes
from gwstock.calendar import NYSE, CalendarExpiredError, ExchangeCalendar
from gwstock.config import BarRuleConfig, RiskConfig, load_bar_rule_config
from gwstock.lanes import (
    COST_OVER_LIMIT,
    DAY_STOP,
    DECISION_REASONS,
    ENTER,
    EXIT_FLATTEN,
    EXIT_TIME_LIMIT,
    GAPPED_DATA,
    INSUFFICIENT_DATA,
    MAX_ENTRIES_DAY,
    NO_SIGNAL,
    OUTSIDE_WINDOW,
    POSITION_OVERLAP,
    SLIP_FILTER,
    STALE_SIGNAL,
    STOP_TOO_CLOSE,
    DayState,
    LaneDecision,
    LaneSession,
    decide_entry,
    exit_due,
    lane_session,
)
from gwstock.strategy import SKIPPED_MIN_SIZE, STOP_TOO_TIGHT
from gwstock.strategy_bars import BarEntryPlan, BarSignal
from gwstock.types import Candle, InstrumentRules, Quote, Side

D = Decimal
UTC = timezone.utc

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
    session_timezone="America/New_York",
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
RULES = InstrumentRules("US500", D("0.01"), D("0.01"), D("1"), D("0.05"), D("1.0"), D("0.1"))
CAL = ExchangeCalendar(NYSE)
DAY = date(2026, 11, 5)
SIGNAL_START = datetime(2026, 11, 5, 15, 40, tzinfo=UTC)  # 10:40 w Nowym Jorku (EST)
SESSION = lane_session(CFG, CAL, DAY)
FRESH = DayState(day=DAY, entries=0, result_r=D("0"), position_open=False)

LONG = ("5000.4", "5004", "5000", "5003.6")  # (o, h, l, c)
SHORT = ("4999.6", "5000", "4996", "4996.4")
FLAT_BAR = ("5000", "5001", "4999", "5000")
LONG_QUOTE = ("5003.8", "5004.2")  # (bid, ask)
SHORT_QUOTE = ("4996.2", "4996.6")


def build(
    ohlc: tuple[str, str, str, str] = LONG,
    *,
    total: int = 15,
    signal_start: datetime = SIGNAL_START,
    back: list[int] | None = None,
    epic: str = "US500",
) -> list[Candle]:
    """`total - 1` płaskich świec i świeca sygnału na końcu; `back` to minuty przed początkiem świecy sygnału."""
    offsets = back if back is not None else [5 * (total - 1 - k) for k in range(total)]
    assert len(offsets) == total and offsets[-1] == 0
    out = []
    for k, minutes in enumerate(offsets):
        o, h, low, c = ohlc if k == total - 1 else FLAT_BAR
        out.append(Candle(epic, signal_start - timedelta(minutes=minutes), D(o), D(h), D(low), D(c)))
    return out


def quote(prices: tuple[str, str] = LONG_QUOTE, *, epic: str = "US500", ts: datetime | None = None) -> Quote:
    return Quote(epic, D(prices[0]), D(prices[1]), ts or SIGNAL_START + timedelta(minutes=5))


def end_of(candles: list[Candle], cfg: BarRuleConfig = CFG) -> datetime:
    return candles[-1].start + timedelta(minutes=cfg.bar_minutes)


def decide(candles=None, q=None, *, cfg=CFG, state=FRESH, session=SESSION, now=None, rules=RULES, **kw) -> LaneDecision:
    candles = candles if candles is not None else build()
    return decide_entry(
        candles,
        q if q is not None else quote(),
        rules,
        RISK,
        cfg,
        session,
        state,
        now if now is not None else end_of(candles, cfg),
        **kw,
    )


def reason(*args, **kw) -> str:
    return decide(*args, **kw).reason


# ---------------------------------------------------------------------------------------------------------------
# konfiguracja pasma w pliku = wartości przypięte w tym teście
# ---------------------------------------------------------------------------------------------------------------


def test_config_file_matches_the_values_pinned_in_this_test():
    assert load_bar_rule_config(Path("config") / "lane_us500_b1_m5.toml") == CFG


# ---------------------------------------------------------------------------------------------------------------
# lane_session: okna sygnałów i godzina spłaszczenia jako chwile UTC
# ---------------------------------------------------------------------------------------------------------------


def utc(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_session_in_winter_time_has_utc_windows_and_flatten_from_the_calendar():
    assert SESSION == LaneSession(
        day=DAY,
        windows=((utc(2026, 11, 5, 14, 45), utc(2026, 11, 5, 16, 45)), (utc(2026, 11, 5, 19, 0), utc(2026, 11, 5, 20, 30))),
        flatten_at=utc(2026, 11, 5, 20, 55),  # 15:55 w Nowym Jorku
    )


def test_session_in_summer_time_shifts_by_the_zone_not_by_a_fixed_offset():
    s = lane_session(CFG, CAL, date(2026, 10, 29))  # czwartek, EDT (UTC-4)
    assert s is not None
    assert s.windows == (
        (utc(2026, 10, 29, 13, 45), utc(2026, 10, 29, 15, 45)),
        (utc(2026, 10, 29, 18, 0), utc(2026, 10, 29, 19, 30)),
    )
    assert s.flatten_at == utc(2026, 10, 29, 19, 55)


def test_session_on_the_day_of_the_clock_change_uses_the_offset_of_that_day():
    # 2.11.2026 (poniedziałek) to pierwszy dzień sesji po zmianie czasu w USA (1.11): już EST
    s = lane_session(CFG, CAL, date(2026, 11, 2))
    assert s is not None and s.windows[0][0] == utc(2026, 11, 2, 14, 45) and s.flatten_at == utc(2026, 11, 2, 20, 55)


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 11, 7), date(2026, 11, 8), date(2026, 12, 25)])
def test_no_session_on_holidays_and_weekends(day):
    assert lane_session(CFG, CAL, day) is None


def test_early_close_day_moves_the_flatten_and_drops_windows_after_it():
    s = lane_session(CFG, CAL, date(2026, 11, 27))  # dzień po Thanksgiving, zamknięcie 13:00
    assert s is not None
    assert s.flatten_at == utc(2026, 11, 27, 17, 55)  # 12:55 w Nowym Jorku
    assert s.windows == ((utc(2026, 11, 27, 14, 45), utc(2026, 11, 27, 16, 45)),)  # okno 14:00-15:30 po zamknięciu odpada


def test_a_window_crossing_the_flatten_is_clipped_to_it():
    cfg = dataclasses.replace(CFG, signal_windows="09:45-13:30")
    s = lane_session(cfg, CAL, date(2026, 11, 27))
    assert s is not None and s.windows == ((utc(2026, 11, 27, 14, 45), utc(2026, 11, 27, 17, 55)),)


def test_session_windows_and_flatten_come_from_the_config():
    cfg = dataclasses.replace(CFG, signal_windows="10:00-10:30", flatten_before_close_min=30)
    s = lane_session(cfg, CAL, DAY)
    assert s is not None
    assert s.windows == ((utc(2026, 11, 5, 15, 0), utc(2026, 11, 5, 15, 30)),)
    assert s.flatten_at == utc(2026, 11, 5, 20, 30)


def test_session_requires_the_calendar_zone_to_match_the_lane_zone():
    cfg = dataclasses.replace(CFG, session_timezone="Europe/Berlin")
    with pytest.raises(ValueError, match="time zone"):
        lane_session(cfg, CAL, DAY)


def test_session_of_an_expired_calendar_raises_instead_of_pretending_a_day():
    with pytest.raises(CalendarExpiredError):
        lane_session(CFG, CAL, date(2028, 1, 4))


def test_session_day_must_be_a_plain_date():
    with pytest.raises((TypeError, ValueError)):
        lane_session(CFG, CAL, datetime(2026, 11, 5, 12, 0))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start", "inside"),
    [
        (utc(2026, 11, 5, 14, 40), False),  # 09:40
        (utc(2026, 11, 5, 14, 45), True),  # 09:45, początek okna włącznie
        (utc(2026, 11, 5, 16, 40), True),  # 11:40, ostatnia świeca okna
        (utc(2026, 11, 5, 16, 45), False),  # 11:45, koniec okna wyłącznie
        (utc(2026, 11, 5, 18, 55), False),  # 13:55
        (utc(2026, 11, 5, 19, 0), True),  # 14:00
        (utc(2026, 11, 5, 20, 25), True),  # 15:25, ostatnia świeca drugiego okna
        (utc(2026, 11, 5, 20, 30), False),  # 15:30
        (utc(2026, 11, 5, 21, 0), False),  # 16:00
        (utc(2026, 11, 4, 15, 40), False),  # ta sama godzina dzień wcześniej: okna dotyczą tego dnia
    ],
)
def test_in_signal_window_is_half_open_per_window(start, inside):
    assert SESSION.in_signal_window(start) is inside


def test_in_signal_window_rejects_naive_and_non_utc_times():
    with pytest.raises(ValueError):
        SESSION.in_signal_window(datetime(2026, 11, 5, 15, 40))
    with pytest.raises(ValueError):
        SESSION.in_signal_window(datetime(2026, 11, 5, 10, 40, tzinfo=timezone(timedelta(hours=-5))))


def test_session_validates_its_fields():
    w = ((utc(2026, 11, 5, 14, 45), utc(2026, 11, 5, 16, 45)),)
    flat = utc(2026, 11, 5, 20, 55)
    LaneSession(DAY, w, flat)
    LaneSession(DAY, (), flat)  # dzień bez okien (po przycięciu) jest poprawny
    for bad in (
        dict(day=datetime(2026, 11, 5)),
        dict(windows=((utc(2026, 11, 5, 16, 45), utc(2026, 11, 5, 14, 45)),)),  # koniec przed początkiem
        dict(windows=((utc(2026, 11, 5, 14, 45), utc(2026, 11, 5, 14, 45)),)),
        dict(windows=(w[0], (utc(2026, 11, 5, 16, 0), utc(2026, 11, 5, 17, 0)))),  # nakładanie
        dict(windows=((datetime(2026, 11, 5, 14, 45), utc(2026, 11, 5, 16, 45)),)),  # data bez strefy
        dict(windows=((utc(2026, 11, 5, 19, 0), utc(2026, 11, 5, 21, 0)),)),  # okno kończy się po spłaszczeniu
        dict(flatten_at=datetime(2026, 11, 5, 20, 55)),
        dict(windows=[w[0]]),  # lista zamiast krotki
    ):
        kwargs = dict(day=DAY, windows=w, flatten_at=flat) | bad
        with pytest.raises((TypeError, ValueError)):
            LaneSession(**kwargs)


# ---------------------------------------------------------------------------------------------------------------
# DayState i LaneDecision
# ---------------------------------------------------------------------------------------------------------------


def test_day_state_validates_its_fields():
    DayState(DAY, 0, D("0"), False)
    DayState(DAY, 10, D("-3.5"), True, utc(2026, 11, 5, 15, 0))
    for bad in (
        dict(day=datetime(2026, 11, 5)),
        dict(entries=-1),
        dict(entries=True),
        dict(entries=1.0),
        dict(result_r=0.5),
        dict(result_r=D("NaN")),
        dict(result_r=D("Infinity")),
        dict(position_open=1),
        dict(last_exit_at=datetime(2026, 11, 5, 15, 0)),
        dict(last_exit_at="2026-11-05"),
    ):
        kwargs = dict(day=DAY, entries=0, result_r=D("0"), position_open=False) | bad
        with pytest.raises((TypeError, ValueError)):
            DayState(**kwargs)


def test_decision_reasons_are_the_documented_set():
    assert DECISION_REASONS == {
        ENTER,
        NO_SIGNAL,
        INSUFFICIENT_DATA,
        OUTSIDE_WINDOW,
        STALE_SIGNAL,
        POSITION_OVERLAP,
        MAX_ENTRIES_DAY,
        DAY_STOP,
        GAPPED_DATA,
        STOP_TOO_CLOSE,
        SLIP_FILTER,
        COST_OVER_LIMIT,
        STOP_TOO_TIGHT,
        SKIPPED_MIN_SIZE,
    }


def test_decision_invariants():
    plan = decide().plan
    assert isinstance(plan, BarEntryPlan)
    LaneDecision(ENTER, plan=plan, signal=decide().signal)
    LaneDecision(NO_SIGNAL)
    for bad in (
        dict(reason="BOGUS"),
        dict(reason=ENTER),  # wejście bez planu
        dict(reason=NO_SIGNAL, plan=plan),  # plan przy odrzuceniu
        dict(reason=ENTER, plan="x"),
        dict(reason=NO_SIGNAL, signal="x"),
    ):
        with pytest.raises((TypeError, ValueError)):
            LaneDecision(**bad)


# ---------------------------------------------------------------------------------------------------------------
# decide_entry: wejście
# ---------------------------------------------------------------------------------------------------------------


def test_long_entry_with_hand_computed_numbers():
    d = decide()
    assert d.reason == ENTER
    assert d.signal == BarSignal(Side.BUY, D("5000"), D("1.5"), 50, SIGNAL_START + timedelta(minutes=5))
    q = quote()
    # odległość stopu liczona od notowania BEZ założonego poślizgu: ask 5004.2 - 5000 = 4.2; cel 1.5 x 4.2
    assert d.plan == BarEntryPlan(Side.BUY, D("0.01"), D("4.2"), D("6.3"), q)
    assert d.entered


def test_short_entry_with_hand_computed_numbers():
    c = build(SHORT)
    d = decide(c, quote(SHORT_QUOTE))
    assert d.reason == ENTER
    assert d.signal == BarSignal(Side.SELL, D("5000"), D("1.5"), 50, SIGNAL_START + timedelta(minutes=5))
    assert d.plan == BarEntryPlan(Side.SELL, D("0.01"), D("3.8"), D("5.7"), quote(SHORT_QUOTE))


def test_the_plan_stop_distance_is_rounded_up_to_the_broker_step():
    d = decide(q=quote(("5003.77", "5004.17")))  # ask - stop = 4.17 -> 4.2; r z poślizgiem 4.32, wszystkie filtry przechodzą
    assert d.reason == ENTER and d.plan.stop_distance == D("4.2") and d.plan.target_distance == D("6.3")


def test_size_policy_is_passed_through_to_the_plan():
    big = decide(session_index=5, rule_size_allowed=True).plan
    small = decide(session_index=5, rule_size_allowed=False).plan
    assert small.size == D("0.01")
    # 1.00 EUR / (4.2 pkt x 1 EUR) = 0.238 -> 0.23; limit dźwigni 5 x 40 / (5004.2 x 1 EUR) = 0.0399 -> 0.03
    assert big.size == D("0.03")


def test_a_plan_below_the_broker_minimum_stop_is_rejected_with_the_entry_plan_reason():
    # filtry przechodzą, ale odległość stopu od notowania 4.2 jest mniejsza od minimum brokera 10
    d = decide(rules=dataclasses.replace(RULES, min_stop_distance=D("10")))
    assert d.reason == STOP_TOO_TIGHT and d.plan is None and d.signal is not None


def test_size_below_the_minimum_is_rejected_with_the_entry_plan_reason():
    d = decide(session_index=5, rule_size_allowed=True, rules=dataclasses.replace(RULES, min_size=D("0.5")))
    assert d.reason == SKIPPED_MIN_SIZE and d.plan is None and d.signal is not None


# ---------------------------------------------------------------------------------------------------------------
# decide_entry: sygnał
# ---------------------------------------------------------------------------------------------------------------


def test_no_signal_for_a_candle_inside_the_channel():
    d = decide(build(FLAT_BAR))
    assert d.reason == NO_SIGNAL and d.plan is None and d.signal is None and not d.entered


def test_signal_buffer_uses_the_spread_of_the_quote():
    # spread 8.0 -> b = 2.0, wybicie wymaga zamknięcia > 5003; 5003.6 ją przechodzi (sygnał jest), ale cena wejścia
    # 5008.35 leży 4.75 od zamknięcia (> 0.5 ATR), więc dalej odrzuca filtr poślizgu
    assert reason(q=quote(("5000.2", "5008.2"))) == SLIP_FILTER
    # spread 16 -> b = 4.0, zamknięcie 5003.6 < 5005: brak sygnału (zanim filtry cokolwiek ocenią)
    assert reason(q=quote(("5000.2", "5016.2"))) == NO_SIGNAL


# ---------------------------------------------------------------------------------------------------------------
# decide_entry: filtry (granice: równo przechodzi, o najmniejszy krok więcej odrzuca)
# ---------------------------------------------------------------------------------------------------------------


def test_stop_too_close_at_the_threshold_passes_and_one_tick_closer_rejects():
    cfg = dataclasses.replace(CFG, min_stop_fraction=D("0.001"))
    # stop = niżej z (low, upper - b); low 4999.34565 -> r = 5004.35 - 4999.34565 = 5.00435 = 0.001 x 5004.35 (równo)
    tie = build(("5000.5", "5004", "4999.34565", "5003.6"))
    assert reason(tie, cfg=cfg) == ENTER
    closer = build(("5000.5", "5004", "4999.34566", "5003.6"))  # r = 5.00434
    assert reason(closer, cfg=cfg) == STOP_TOO_CLOSE


@pytest.mark.parametrize(
    ("ask", "expected"),
    [
        ("5001.2", STOP_TOO_CLOSE),  # entry 5001.35, r = 0.45 < 0.50014
        ("5001.24", STOP_TOO_CLOSE),  # entry 5001.39, r = 0.49 < 0.50014
        ("5001.26", ENTER),  # entry 5001.41, r = 0.51 >= 0.50014
        ("5001.3", ENTER),
    ],
)
def test_stop_too_close_on_a_tiny_breakout_with_the_configured_fraction(ask, expected):
    tiny = build(("5001.0", "5001.25", "5001.0", "5001.2"))
    cfg = dataclasses.replace(CFG, max_round_trip_cost_r=D("100"), slippage_filter_atr_fraction=D("1"))
    bid = D(ask) - D("0.2")  # spread 0.2: b = max(0.1, 0.05) = 0.1
    d = decide(tiny, q=quote((str(bid), ask)), cfg=cfg, rules=dataclasses.replace(RULES, min_stop_distance=D("0")))
    assert d.reason == expected


def test_stop_too_close_fraction_comes_from_the_config():
    tiny = build(("5001.0", "5001.25", "5001.0", "5001.2"))
    loose = dataclasses.replace(
        CFG, max_round_trip_cost_r=D("100"), slippage_filter_atr_fraction=D("1"), min_stop_fraction=D("0.00001")
    )
    strict = dataclasses.replace(loose, min_stop_fraction=D("0.001"))
    q = quote(("5001.06", "5001.26"))  # entry 5001.41, r = 0.51
    rules = dataclasses.replace(RULES, min_stop_distance=D("0"))
    assert decide(tiny, q=q, cfg=loose, rules=rules).reason == ENTER
    assert decide(tiny, q=q, cfg=strict, rules=rules).reason == STOP_TOO_CLOSE  # 0.001 x 5001.41 = 5.0 > 0.51


def test_slip_filter_at_the_threshold_passes_and_one_tick_further_rejects_for_a_long():
    cfg = dataclasses.replace(CFG, slippage_filter_atr_fraction=D("0.7"))  # 0.7 x 30 / 14 = 1.5 dokładnie
    # entry = ask + 0.15; |entry - close 5003.6| = 1.5 gdy ask = 5004.95
    assert reason(q=quote(("5004.55", "5004.95")), cfg=cfg) == ENTER
    assert reason(q=quote(("5004.56", "5004.96")), cfg=cfg) == SLIP_FILTER


def test_slip_filter_is_symmetric_for_an_entry_below_the_close():
    cfg = dataclasses.replace(CFG, slippage_filter_atr_fraction=D("0.7"))
    # entry 5002.1 (ask 5001.95): |5002.1 - 5003.6| = 1.5 równo; koszt zrelaksowany, żeby SLIP był jedynym filtrem
    relaxed = dataclasses.replace(cfg, max_round_trip_cost_r=D("100"))
    assert reason(q=quote(("5001.55", "5001.95")), cfg=relaxed) == ENTER
    assert reason(q=quote(("5001.54", "5001.94")), cfg=relaxed) == SLIP_FILTER


def test_slip_filter_for_a_short_uses_the_bid_minus_the_assumed_slippage():
    cfg = dataclasses.replace(CFG, slippage_filter_atr_fraction=D("0.7"))
    # entry = bid - 0.15; |entry - close 4996.4| = 1.5 gdy bid = 4995.05 (entry 4994.9)
    assert reason(build(SHORT), q=quote(("4995.05", "4995.45")), cfg=cfg) == ENTER
    assert reason(build(SHORT), q=quote(("4995.04", "4995.44")), cfg=cfg) == SLIP_FILTER


def test_slip_filter_uses_the_atr_of_the_configured_period():
    # okres 1: ATR = TR świecy sygnału = 4; 0.5 x 4 = 2.0 dokładnie gdy |entry - close| = 2.0 (ask = 5005.45)
    cfg = dataclasses.replace(CFG, atr_period=1)
    assert reason(q=quote(("5005.05", "5005.45")), cfg=cfg) == ENTER
    assert reason(q=quote(("5005.06", "5005.46")), cfg=cfg) == SLIP_FILTER
    # okres 14 (ATR = 30 / 14, próg 1.0714): to samo notowanie jest odrzucone, a 5004.4 przechodzi (0.9 + 0.15 -> 0.95 <= 1.07)
    assert reason(q=quote(("5005.05", "5005.45"))) == SLIP_FILTER


def test_atr_averages_the_true_ranges_of_the_last_n_candles_only():
    cfg = dataclasses.replace(CFG, slippage_filter_atr_fraction=D("0.7"))  # granica 1.5 przy ATR = 30 / 14
    c = build()
    edge = quote(("5004.56", "5004.96"))  # odchylenie 1.51: przy ATR = 30 / 14 odrzucone
    assert reason(c, q=edge, cfg=cfg) == SLIP_FILTER
    # z najstarszej świecy liczy się tylko zamknięcie (poprzednie zamknięcie dla pierwszego zakresu), więc jej rozstrzał
    # nie zmienia ATR
    wide_oldest = dataclasses.replace(c[0], high=D("5100"), low=D("4900"))
    assert reason([wide_oldest] + c[1:], q=edge, cfg=cfg) == SLIP_FILTER
    # druga najstarsza świeca jest już w oknie 14 zakresów: jej TR = 200 podnosi ATR i próg, więc to samo notowanie przechodzi
    wide_second = dataclasses.replace(c[1], high=D("5100"), low=D("4900"))
    assert reason([c[0], wide_second] + c[2:], q=edge, cfg=cfg) == ENTER


def test_cost_filter_at_the_threshold_passes_and_one_tick_cheaper_rejects():
    # cost = 0.4 + 2 x 0.15 = 0.7; r = ask + 0.15 - 5000; 0.7 = 0.2 x 3.5 gdy ask = 5003.35
    assert reason(q=quote(("5002.95", "5003.35"))) == ENTER
    assert reason(q=quote(("5002.94", "5003.34"))) == COST_OVER_LIMIT


def test_cost_filter_for_a_short():
    # cost 0.7, r = 5000 - (bid - 0.15); 0.7 = 0.2 x 3.5 gdy bid = 4996.65 (entry 4996.5, r = 3.5)
    assert reason(build(SHORT), q=quote(("4996.65", "4997.05"))) == ENTER
    assert reason(build(SHORT), q=quote(("4996.66", "4997.06"))) == COST_OVER_LIMIT


def test_cost_limit_and_assumed_slippage_come_from_the_config():
    q = quote(("5002.94", "5003.34"))
    assert reason(q=q) == COST_OVER_LIMIT
    assert reason(q=q, cfg=dataclasses.replace(CFG, max_round_trip_cost_r=D("0.21"))) == ENTER
    # bez założonego poślizgu koszt to sam spread 0.4 <= 0.2 x 3.34 = 0.668
    assert reason(q=q, cfg=dataclasses.replace(CFG, assumed_slippage_points=D("0"))) == ENTER


def test_filters_run_in_the_documented_order():
    # zbyt bliski stop wygrywa ze wszystkim po nim
    tiny = build(("5001.0", "5001.25", "5001.0", "5001.2"))
    assert reason(tiny, q=quote(("5001.0", "5001.2"))) == STOP_TOO_CLOSE
    # filtr poślizgu wygrywa z kosztem: entry 5001.15 (ask 5001.0): odchylenie od zamknięcia 2.45 > 1.07, r = 1.15, więc
    # koszt 0.7 > 0.2 x 1.15 też odrzuca
    assert reason(q=quote(("5000.6", "5001.0"))) == SLIP_FILTER


# ---------------------------------------------------------------------------------------------------------------
# decide_entry: dane, okno, limity dnia, nakładanie
# ---------------------------------------------------------------------------------------------------------------


def test_fewer_than_fifteen_candles_are_insufficient_data_and_fifteen_decide():
    assert reason(build(total=14)) == INSUFFICIENT_DATA  # ATR14 potrzebuje 15 świec (14 zakresów i poprzednie zamknięcia)
    assert reason(build(total=15)) == ENTER
    assert reason(build(total=40)) == ENTER  # starsze świece są ignorowane


def test_an_empty_candle_list_is_insufficient_data():
    d = decide_entry([], quote(), RULES, RISK, CFG, SESSION, FRESH, SIGNAL_START + timedelta(minutes=5))
    assert d.reason == INSUFFICIENT_DATA


def test_insufficient_data_follows_the_configured_lookback_and_atr_period():
    assert reason(build(total=14), cfg=dataclasses.replace(CFG, atr_period=1)) == ENTER  # n + 2 = 14 świec wystarcza
    assert reason(build(total=13), cfg=dataclasses.replace(CFG, atr_period=1)) == INSUFFICIENT_DATA
    assert reason(build(total=16), cfg=dataclasses.replace(CFG, atr_period=15)) == ENTER  # 15 zakresów = 16 świec
    assert reason(build(total=15), cfg=dataclasses.replace(CFG, atr_period=15)) == INSUFFICIENT_DATA


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (utc(2026, 11, 5, 14, 40), OUTSIDE_WINDOW),  # 09:40
        (utc(2026, 11, 5, 14, 45), ENTER),  # 09:45
        (utc(2026, 11, 5, 16, 40), ENTER),  # 11:40, koniec świecy 11:45 = koniec okna: początek świecy decyduje
        (utc(2026, 11, 5, 16, 45), OUTSIDE_WINDOW),  # 11:45
        (utc(2026, 11, 5, 18, 55), OUTSIDE_WINDOW),  # 13:55
        (utc(2026, 11, 5, 19, 0), ENTER),  # 14:00
        (utc(2026, 11, 5, 20, 25), ENTER),  # 15:25
        (utc(2026, 11, 5, 20, 30), OUTSIDE_WINDOW),  # 15:30
    ],
)
def test_signal_window_is_decided_by_the_start_of_the_signal_candle(start, expected):
    c = build(signal_start=start)
    q = quote(ts=start + timedelta(minutes=5))
    assert reason(c, q=q) == expected


def test_signal_window_in_summer_time():
    s = lane_session(CFG, CAL, date(2026, 10, 29))
    start = utc(2026, 10, 29, 13, 40)  # 09:40 EDT
    assert reason(build(signal_start=start), session=s, state=DayState(date(2026, 10, 29), 0, D("0"), False)) == OUTSIDE_WINDOW
    start = utc(2026, 10, 29, 13, 45)  # 09:45 EDT
    assert reason(build(signal_start=start), session=s, state=DayState(date(2026, 10, 29), 0, D("0"), False)) == ENTER


def test_a_day_without_windows_never_enters():
    s = LaneSession(DAY, (), utc(2026, 11, 5, 20, 55))
    assert reason(session=s) == OUTSIDE_WINDOW


def test_entry_limit_per_day_at_the_boundary():
    assert reason(state=DayState(DAY, 9, D("0"), False)) == ENTER
    assert reason(state=DayState(DAY, 10, D("0"), False)) == MAX_ENTRIES_DAY
    assert reason(state=DayState(DAY, 11, D("0"), False)) == MAX_ENTRIES_DAY


def test_entry_limit_per_day_comes_from_the_config():
    cfg = dataclasses.replace(CFG, max_entries_per_day=2)
    assert reason(cfg=cfg, state=DayState(DAY, 1, D("0"), False)) == ENTER
    assert reason(cfg=cfg, state=DayState(DAY, 2, D("0"), False)) == MAX_ENTRIES_DAY


def test_day_stop_at_the_boundary():
    assert reason(state=DayState(DAY, 1, D("-2.99"), False)) == ENTER
    assert reason(state=DayState(DAY, 1, D("-3.0"), False)) == DAY_STOP  # równo -3,0 R kończy dzień (<=)
    assert reason(state=DayState(DAY, 1, D("-3.01"), False)) == DAY_STOP
    assert reason(state=DayState(DAY, 1, D("12"), False)) == ENTER  # zysk nie kończy dnia


def test_day_stop_comes_from_the_config_as_an_absolute_value():
    cfg = dataclasses.replace(CFG, day_stop_r=D("1.5"))
    assert reason(cfg=cfg, state=DayState(DAY, 1, D("-1.49"), False)) == ENTER
    assert reason(cfg=cfg, state=DayState(DAY, 1, D("-1.5"), False)) == DAY_STOP


def test_entry_limit_is_checked_before_the_day_stop():
    assert reason(state=DayState(DAY, 10, D("-9"), False)) == MAX_ENTRIES_DAY


def test_an_open_position_blocks_the_next_entry():
    assert reason(state=DayState(DAY, 1, D("0"), True)) == POSITION_OVERLAP


def test_a_signal_candle_that_started_before_or_at_the_last_exit_is_an_overlap():
    start = SIGNAL_START
    assert reason(state=DayState(DAY, 1, D("0"), False, start)) == POSITION_OVERLAP  # wyjście równo w początku świecy
    assert reason(state=DayState(DAY, 1, D("0"), False, start + timedelta(minutes=4))) == POSITION_OVERLAP  # w trakcie
    assert reason(state=DayState(DAY, 1, D("0"), False, start - timedelta(microseconds=1))) == ENTER  # tuż przed
    assert reason(state=DayState(DAY, 1, D("0"), False, start - timedelta(minutes=5))) == ENTER


def back_with_hole(after_index: int, missing: int, total: int = 15) -> list[int]:
    """Minuty przed świecą sygnału dla `total` świec; między świecą `after_index` a następną brakuje `missing` pozycji."""
    grid = [5 * (total - 1 - k) for k in range(total)]
    return [g + 5 * missing if k <= after_index else g for k, g in enumerate(grid)]


def test_precedence_of_rejection_reasons_follows_the_documented_order():
    out_of_window = utc(2026, 11, 5, 14, 40)  # 09:40
    # nakładanie przed wszystkim
    assert reason(build(signal_start=out_of_window), state=DayState(DAY, 10, D("-9"), True)) == POSITION_OVERLAP
    # za mało danych przed oknem
    assert reason(build(total=14, signal_start=out_of_window)) == INSUFFICIENT_DATA
    # okno przed limitami dnia
    assert reason(build(signal_start=out_of_window), state=DayState(DAY, 10, D("-9"), False)) == OUTSIDE_WINDOW
    # limity przed luką w danych
    holed = build(back=back_with_hole(5, 3))
    assert reason(holed, state=DayState(DAY, 10, D("0"), False)) == MAX_ENTRIES_DAY
    assert reason(holed, state=DayState(DAY, 1, D("-3"), False)) == DAY_STOP
    # luka przed sygnałem: świeca bez sygnału, ale z dziurą w danych, to GAPPED_DATA (jak w badaniach)
    assert reason(build(FLAT_BAR, back=back_with_hole(5, 3))) == GAPPED_DATA
    assert reason(holed) == GAPPED_DATA


@pytest.mark.parametrize(
    ("after_index", "missing", "expected"),
    [
        (1, 0, ENTER),  # bez dziury: 14 ostatnich świec obejmuje 13 odstępów = 65 min
        (1, 1, ENTER),  # 70 min
        (1, 2, ENTER),  # 75 min = (n + 1 + 2) x 5: równo przechodzi
        (1, 3, GAPPED_DATA),  # 80 min
        (5, 2, ENTER),  # dziura w środku zakresu
        (5, 3, GAPPED_DATA),
        (13, 2, ENTER),  # dziura tuż przed świecą sygnału
        (13, 3, GAPPED_DATA),
        (0, 6, ENTER),  # dziura starsza niż 14 ostatnich świec nie liczy się
    ],
)
def test_gapped_data_tolerates_two_missing_candles_in_the_span_of_the_last_fourteen(after_index, missing, expected):
    assert reason(build(back=back_with_hole(after_index, missing))) == expected


def test_gap_tolerance_comes_from_the_config():
    c = build(back=back_with_hole(5, 3))
    assert reason(c) == GAPPED_DATA
    assert reason(c, cfg=dataclasses.replace(CFG, gap_tolerance_bars=3)) == ENTER
    assert reason(build(back=back_with_hole(5, 1)), cfg=dataclasses.replace(CFG, gap_tolerance_bars=0)) == GAPPED_DATA
    assert reason(build(), cfg=dataclasses.replace(CFG, gap_tolerance_bars=0)) == ENTER


# ---------------------------------------------------------------------------------------------------------------
# decide_entry: chwila decyzji i błędy wywołującego
# ---------------------------------------------------------------------------------------------------------------


def test_the_signal_candle_must_be_closed_at_the_time_of_the_decision():
    c = build()
    with pytest.raises(ValueError, match="not closed"):
        decide(c, now=end_of(c) - timedelta(microseconds=1))
    assert reason(c, now=end_of(c)) == ENTER


def test_the_decision_is_stale_after_one_more_candle_has_closed():
    c = build()
    assert reason(c, now=end_of(c) + timedelta(minutes=5) - timedelta(microseconds=1)) == ENTER
    assert reason(c, now=end_of(c) + timedelta(minutes=5)) == STALE_SIGNAL


def test_staleness_follows_the_configured_bar_length():
    c = build()
    cfg = dataclasses.replace(CFG, bar_minutes=10)  # świeca 10-minutowa: koniec 10:50, nieaktualne od 11:00
    assert reason(c, cfg=cfg, now=SIGNAL_START + timedelta(minutes=19)) == ENTER
    assert reason(c, cfg=cfg, now=SIGNAL_START + timedelta(minutes=20)) == STALE_SIGNAL


def test_naive_or_non_utc_decision_time_is_an_error():
    with pytest.raises(ValueError):
        decide(now=datetime(2026, 11, 5, 15, 45))
    with pytest.raises(ValueError):
        decide(now=datetime(2026, 11, 5, 10, 45, tzinfo=timezone(timedelta(hours=-5))))


def test_state_must_be_for_the_session_day():
    with pytest.raises(ValueError, match="day"):
        decide(state=DayState(date(2026, 11, 4), 0, D("0"), False))


@pytest.mark.parametrize("which", ["quote", "rules", "cfg", "candle"])
def test_instrument_mismatch_is_an_error_not_a_rejection(which):
    kw = {}
    c = build()
    if which == "quote":
        kw["q"] = quote(epic="US100")
    elif which == "rules":
        kw["rules"] = dataclasses.replace(RULES, epic="US100")
    elif which == "cfg":
        kw["cfg"] = dataclasses.replace(CFG, instrument="US100")
    else:
        c = build(epic="US100")
    with pytest.raises(ValueError, match="same"):
        decide(c, **kw)


def test_candle_starts_must_be_strictly_ascending_over_the_whole_used_tail():
    c = build()
    swapped = c[:]
    swapped[1], swapped[2] = swapped[2], swapped[1]  # w ogonie ATR, poza zakresem sygnału... ale nadal w używanych 15
    with pytest.raises(ValueError, match="ascending"):
        decide(swapped)
    dup = c[:]
    dup[3] = dup[4]
    with pytest.raises(ValueError, match="ascending"):
        decide(dup)


def test_argument_types_are_checked():
    c = build()
    with pytest.raises(TypeError):
        decide_entry(c, "quote", RULES, RISK, CFG, SESSION, FRESH, end_of(c))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        decide_entry(c, quote(), RULES, RISK, CFG, "session", FRESH, end_of(c))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        decide_entry(c, quote(), RULES, RISK, CFG, SESSION, "state", end_of(c))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        decide_entry(c + ["x"], quote(), RULES, RISK, CFG, SESSION, FRESH, end_of(c))  # type: ignore[list-item]
    with pytest.raises(ValueError):
        decide(session_index=-1)


def test_decide_entry_is_pure_and_does_not_modify_its_input():
    c = build()
    before = list(c)
    first = decide(c)
    assert c == before
    assert decide(c) == first
    assert decide(tuple(c)) == first


# ---------------------------------------------------------------------------------------------------------------
# exit_due: limit czasu i spłaszczenie
# ---------------------------------------------------------------------------------------------------------------

OPENED = utc(2026, 11, 5, 15, 45)  # 10:45 NY


def test_no_exit_before_the_limit_and_before_the_flatten():
    assert exit_due(OPENED, OPENED, SESSION, CFG) is None
    assert exit_due(OPENED, OPENED + timedelta(minutes=49, seconds=59), SESSION, CFG) is None


def test_time_limit_at_the_boundary():
    assert exit_due(OPENED, OPENED + timedelta(minutes=50), SESSION, CFG) == EXIT_TIME_LIMIT
    assert exit_due(OPENED, OPENED + timedelta(minutes=51), SESSION, CFG) == EXIT_TIME_LIMIT


def test_time_limit_comes_from_the_config():
    cfg = dataclasses.replace(CFG, time_limit_min=10)
    assert exit_due(OPENED, OPENED + timedelta(minutes=9, seconds=59), SESSION, cfg) is None
    assert exit_due(OPENED, OPENED + timedelta(minutes=10), SESSION, cfg) == EXIT_TIME_LIMIT


def test_scheduled_flatten_at_the_boundary():
    late_open = utc(2026, 11, 5, 20, 30)  # 15:30
    assert exit_due(late_open, SESSION.flatten_at - timedelta(microseconds=1), SESSION, CFG) is None
    assert exit_due(late_open, SESSION.flatten_at, SESSION, CFG) == EXIT_FLATTEN
    assert exit_due(late_open, SESSION.flatten_at + timedelta(minutes=10), SESSION, CFG) == EXIT_FLATTEN


def test_time_limit_wins_over_the_flatten_when_both_are_due():
    open_ = utc(2026, 11, 5, 19, 30)  # 14:30, po 85 min jest 15:55
    assert exit_due(open_, SESSION.flatten_at, SESSION, CFG) == EXIT_TIME_LIMIT


def test_exit_due_on_an_early_close_day_flattens_before_the_early_close():
    s = lane_session(CFG, CAL, date(2026, 11, 27))
    assert s is not None
    assert exit_due(utc(2026, 11, 27, 17, 30), utc(2026, 11, 27, 17, 55), s, CFG) == EXIT_FLATTEN  # 12:55, nie 15:55


def test_exit_due_validates_its_arguments():
    with pytest.raises(ValueError):
        exit_due(datetime(2026, 11, 5, 15, 45), OPENED, SESSION, CFG)
    with pytest.raises(ValueError):
        exit_due(OPENED, datetime(2026, 11, 5, 15, 45), SESSION, CFG)
    with pytest.raises(ValueError, match="before"):
        exit_due(OPENED, OPENED - timedelta(seconds=1), SESSION, CFG)
    with pytest.raises(TypeError):
        exit_due(OPENED, OPENED, "session", CFG)  # type: ignore[arg-type]


def test_exit_reasons_are_distinct_and_the_scheduled_one_matches_the_engine_flatten_reason():
    assert EXIT_TIME_LIMIT == "TIME_LIMIT"  # `Executor.flatten(position, "TIME_LIMIT")`, projekt §3.6
    assert EXIT_FLATTEN == "FORCED"  # jak `app.FLATTEN_REASON` zaplanowanego spłaszczenia
    assert EXIT_TIME_LIMIT != EXIT_FLATTEN


# ---------------------------------------------------------------------------------------------------------------
# higiena modułu: czysty, bez I/O, zegara, brokera, floatów i liczb zaszytych w kodzie
# ---------------------------------------------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(Path(lanes.__file__).read_text(encoding="utf-8"))


def test_module_has_no_io_clock_broker_or_float():
    tree = _tree()
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
        "re",
        "gwstock.calendar",
        "gwstock.config",
        "gwstock.strategy",
        "gwstock.strategy_bars",
        "gwstock.types",
    }
    assert imported <= allowed, f"unexpected imports in lanes.py: {sorted(imported - allowed)}"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert "float" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & {"now", "utcnow", "today", "sleep"}


def test_module_has_no_hard_coded_thresholds():
    """Progi (10 wejść, -3 R, 0,0001, 0,5, 0,20, 0,15, ATR14, okna 09:45-11:45 i 14:00-15:30, 50 min) są w
    `config/lane_us500_b1_m5.toml`; w kodzie wolno tylko 0 i 1 oraz napisy, które nie są liczbami."""
    tree = _tree()
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
