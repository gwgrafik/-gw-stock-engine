"""Testy bramki ryzyka: każda kontrola osobno i razem, granice, DST, odporność na złe dane.

Domyślny zegar w tym pliku to 09:06 w Berlinie (w oknie wejścia); testy okna budują własną bramkę o zadanej godzinie.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

import pytest

from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.journal import Journal, JournalReadError, OrderRecord, PositionRecord
from gwstock.risk import (
    DAILY_LOSS,
    DUPLICATE,
    ENTRIES_PER_DAY,
    FORMAT,
    GATE_ERROR,
    KILL,
    LEVERAGE,
    LOSS_PER_TRADE,
    MARGIN,
    NO_STOP,
    NOT_SESSION,
    OUTSIDE_WINDOW,
    STALE_DATA,
    TOTAL_DRAWDOWN,
    EntryCandidate,
    RiskGate,
    Verdict,
)
from gwstock.types import AccountInfo, KillLevel, OrderState, PositionState, Side

CAL = ExchangeCalendar(XETRA)
DAY = date(2026, 11, 2)  # poniedziałek, czas zimowy (CET), dzień sesji
D = Decimal

# kolejność kontroli z tabeli spec §9
TABLE_ORDER = [
    FORMAT, NO_STOP, LOSS_PER_TRADE, LEVERAGE, MARGIN, ENTRIES_PER_DAY, DAILY_LOSS, TOTAL_DRAWDOWN,
    NOT_SESSION, OUTSIDE_WINDOW, DUPLICATE, STALE_DATA, KILL,
]  # fmt: skip


def berlin(day: date, hh: int, mm: int, ss: int = 0, us: int = 0) -> datetime:
    """Chwila UTC odpowiadająca lokalnej godzinie w Berlinie."""
    return CAL.local_to_utc(day, time(hh, mm, ss, us))


@pytest.fixture
def clock() -> FakeClock:
    """Nadpisuje fixture z conftest: 09:06 w Berlinie, czyli w oknie wejścia."""
    return FakeClock(berlin(DAY, 9, 6))


def acct(*, equity: str = "40", available: str = "40") -> AccountInfo:
    return AccountInfo("ACC-1", "EUR", "ENABLED", D(equity), D(available), D("0"))


ACCT = acct()


def cand(**over) -> EntryCandidate:
    """Kandydat, który przechodzi wszystkie kontrole: 0.001 DE40, stop 30 pkt (ryzyko 0,03 EUR)."""
    fields = dict(
        epic="DE40",
        side=Side.BUY,
        size=D("0.001"),
        stop_distance=D("30"),
        entry_price=D("25782.3"),
        value_per_point=D("1"),
        margin_rate=D("0.05"),
        quote_age_s=5,
    )
    fields.update(over)
    return EntryCandidate(**fields)


def build_gate(journal, risk_cfg, strat_cfg, when_utc, *, kill=None, calendar=CAL, strat=None, risk=None):
    clock = FakeClock(when_utc)
    return RiskGate(risk or risk_cfg, strat or strat_cfg, journal, calendar, clock, kill), clock


def order(order_id="gws-20261102-DE40-1-entry", *, epic="DE40", state=OrderState.REJECTED) -> OrderRecord:
    t = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    return OrderRecord(order_id, epic, Side.BUY, D("0.001"), D("30"), state, None, None, t, t)


def position(state=PositionState.OPEN, *, side=Side.BUY, stop="25752.3", size="0.001") -> PositionRecord:
    t = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    if state is PositionState.FLAT:
        return PositionRecord("DE40", state)
    if state is PositionState.ENTERING:
        return PositionRecord("DE40", state)
    return PositionRecord("DE40", state, "DEAL-1", side, D(size), D("25782.3"), None if stop is None else D(stop), t)


def reasons(gate: RiskGate, c=None, account=ACCT) -> list[str]:
    return gate.check_entry(cand() if c is None else c, account).reasons


# --- stałe i typy ---------------------------------------------------------------------------------


def test_reason_constants_are_their_own_names_in_table_order():
    assert TABLE_ORDER == [
        "FORMAT", "NO_STOP", "LOSS_PER_TRADE", "LEVERAGE", "MARGIN", "ENTRIES_PER_DAY", "DAILY_LOSS",
        "TOTAL_DRAWDOWN", "NOT_SESSION", "OUTSIDE_WINDOW", "DUPLICATE", "STALE_DATA", "KILL",
    ]  # fmt: skip


def test_verdict_is_consistent_and_immutable():
    assert Verdict(True, []).passed
    assert not Verdict(False, ["KILL"]).passed
    with pytest.raises(ValueError):
        Verdict(True, ["KILL"])
    with pytest.raises(ValueError):
        Verdict(False, [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        Verdict(True, []).passed = False  # type: ignore[misc]


def test_entry_candidate_is_frozen_but_does_not_validate_so_the_gate_can_judge_it():
    c = cand(size=None, side="BUY")
    assert c.size is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.size = D("1")  # type: ignore[misc]


# --- kandydat, który przechodzi ------------------------------------------------------------------


def test_clean_candidate_in_window_passes(gate):
    v = gate.check_entry(cand(), ACCT)
    assert v.passed and v.reasons == []


# --- FORMAT ----------------------------------------------------------------------------------------

BAD_FIELDS = [
    ("epic", ""), ("epic", "   "), ("epic", None), ("epic", 5), ("epic", "OTHER"),
    ("side", "BUY"), ("side", None),
    ("size", D(0)), ("size", D(-1)), ("size", 0.001), ("size", None), ("size", "0.001"), ("size", D("NaN")),
    ("size", D("Infinity")), ("size", True),
    ("stop_distance", 30), ("stop_distance", 30.0), ("stop_distance", "30"),
    ("stop_distance", D("NaN")), ("stop_distance", D("-Infinity")),
    ("entry_price", D(0)), ("entry_price", D(-1)), ("entry_price", None), ("entry_price", 25782.3),
    ("entry_price", D("NaN")),
    ("value_per_point", D(0)), ("value_per_point", D(-1)), ("value_per_point", None), ("value_per_point", D("NaN")),
    ("margin_rate", D(0)), ("margin_rate", D(-0.05)), ("margin_rate", None), ("margin_rate", 0.05),
    ("margin_rate", D("Infinity")),
]  # fmt: skip


@pytest.mark.parametrize(("name", "value"), BAD_FIELDS, ids=[f"{n}={v!r}" for n, v in BAD_FIELDS])
def test_bad_candidate_field_is_format_and_never_raises(gate, name, value):
    v = gate.check_entry(cand(**{name: value}), ACCT)
    assert not v.passed
    assert v.reasons == [FORMAT]  # nic poza tym: kontrole zależne od złego pola są pomijane, nie wybuchają


@pytest.mark.parametrize("junk", [None, object(), "candidate", {"epic": "DE40"}, 42])
def test_object_that_is_not_a_candidate_is_format(gate, junk):
    v = gate.check_entry(junk, ACCT)
    assert not v.passed
    # brak stopu to też NO_STOP (spec §9: "brak stopu"), a wiek notowania jest wtedy nie do odczytania
    assert v.reasons == [FORMAT, NO_STOP, STALE_DATA]


def test_candidate_for_another_instrument_is_format(gate):
    # v1 handluje wyłącznie instrumentem z konfiguracji strategii (spec §1: tylko DE40)
    assert reasons(gate, cand(epic="US100")) == [FORMAT]


# --- NO_STOP ---------------------------------------------------------------------------------------


def test_missing_stop_is_both_format_and_no_stop(gate):
    # spec §9 podpina "brak stopu" pod NO_STOP; zły TYP stopu (30.0, "30", NaN) zostaje samym FORMAT
    assert reasons(gate, cand(stop_distance=None)) == [FORMAT, NO_STOP]


@pytest.mark.parametrize("stop", ["0", "-5", "-0", "0.000"])
def test_stop_not_positive_is_no_stop(gate, stop):
    assert reasons(gate, cand(stop_distance=D(stop))) == [NO_STOP]


# --- LOSS_PER_TRADE ----------------------------------------------------------------------------------


def test_loss_check_blocks_when_over_limit(gate):
    v = gate.check_entry(cand(size=D("0.2"), stop_distance=D("30"), value_per_point=D("1")), ACCT)
    assert not v.passed and LOSS_PER_TRADE in v.reasons  # 0.2 × 30 = 6 > 1,00


@pytest.mark.parametrize(
    ("size", "stop", "vpp", "blocked"),
    [
        ("0.01", "100", "1", False),  # dokładnie 1,00 = limit: przechodzi
        ("0.01", "101", "1", True),  # 1,01: o grosz za dużo
        ("0.01", "100.01", "1", True),
        ("0.01", "50", "2", False),  # wartość punktu skaluje ryzyko: 0,01 × 50 × 2 = 1,00
        ("0.01", "50.5", "2", True),
        ("0.001", "30", "1", False),
    ],
)
def test_loss_per_trade_boundary(gate, size, stop, vpp, blocked):
    c = cand(size=D(size), stop_distance=D(stop), value_per_point=D(vpp), entry_price=D("1000"))
    assert reasons(gate, c) == ([LOSS_PER_TRADE] if blocked else [])


# --- LEVERAGE --------------------------------------------------------------------------------------


def test_leverage_check(gate):  # notional 25780 × 0.01 = 257,8 > 5 × 40 = 200
    v = gate.check_entry(cand(size=D("0.01"), entry_price=D("25780")), ACCT)
    assert not v.passed and v.reasons == [LEVERAGE]


def test_leverage_at_the_limit_passes_and_a_tick_over_blocks(gate):
    assert reasons(gate, cand(size=D("0.008"), entry_price=D("25000"))) == []  # notional 200,000 = 5 × 40
    assert reasons(gate, cand(size=D("0.008"), entry_price=D("25000.01"))) == [LEVERAGE]


def test_leverage_uses_value_per_point_in_the_notional(gate):
    c = cand(size=D("0.004"), entry_price=D("25000"), value_per_point=D("2"), stop_distance=D("10"))
    assert reasons(gate, c) == []  # 0.004 × 25000 × 2 = 200
    c = cand(size=D("0.0041"), entry_price=D("25000"), value_per_point=D("2"), stop_distance=D("10"))
    assert reasons(gate, c) == [LEVERAGE]


def test_leverage_capital_is_the_smaller_of_virtual_capital_and_equity(gate):
    c = cand(size=D("0.008"), entry_price=D("25000"))  # notional 200
    assert reasons(gate, c, acct(equity="100")) == []  # kapitał = min(40, 100) = 40 -> limit 200
    over = cand(size=D("0.0081"), entry_price=D("25000"))  # notional 202,5: powyżej limitu 200 z kapitału wirtualnego
    assert reasons(gate, over, acct(equity="100")) == [LEVERAGE]  # większe saldo nie podnosi limitu
    assert reasons(gate, c, acct(equity="30")) == [LEVERAGE]  # kapitał = 30 -> limit 150
    assert reasons(gate, c, acct(equity="39.99")) == [LEVERAGE]


@pytest.mark.parametrize("equity", ["0", "-5"])
def test_leverage_blocks_when_capital_is_not_positive(gate, equity):
    assert LEVERAGE in reasons(gate, account=acct(equity=equity))
    # kapitał <= 0 blokuje sam z siebie, także gdy notionalu kandydata nie da się policzyć
    assert reasons(gate, cand(size=None), acct(equity=equity)) == [FORMAT, LEVERAGE]


# --- MARGIN ----------------------------------------------------------------------------------------


def test_margin_boundary_with_reserve(gate):
    c = cand(size=D("0.008"), entry_price=D("25000"))  # wymagany depozyt 200 × 0.05 = 10
    assert reasons(gate, c, acct(available="12.5")) == []  # 12.5 × (1 − 0.20) = 10: równo przechodzi
    assert reasons(gate, c, acct(available="12.49")) == [MARGIN]  # 9,992 < 10
    assert reasons(gate, c, acct(available="0")) == [MARGIN]
    assert reasons(gate, c, acct(available="-3")) == [MARGIN]


def test_margin_uses_the_candidate_margin_rate(gate):
    c = cand(size=D("0.008"), entry_price=D("25000"), margin_rate=D("0.1"))  # 20 wymagane
    assert reasons(gate, c, acct(available="25")) == []  # 25 × 0.8 = 20
    assert reasons(gate, c, acct(available="24.99")) == [MARGIN]


def test_missing_or_wrong_account_blocks_leverage_and_margin(gate):
    assert reasons(gate, account=None) == [LEVERAGE, MARGIN]
    assert reasons(gate, account="ACC-1") == [LEVERAGE, MARGIN]


# --- ENTRIES_PER_DAY -------------------------------------------------------------------------------


def test_one_entry_per_day(gate, journal):
    assert reasons(gate) == []  # 0 zleceń
    journal.insert_order(order())
    assert reasons(gate) == [ENTRIES_PER_DAY]  # po wejściu


@pytest.mark.parametrize("state", [OrderState.REJECTED, OrderState.NOT_PLACED, OrderState.CONFIRMED])
def test_failed_attempt_also_uses_the_daily_entry(gate, journal, state):
    journal.insert_order(order(state=state))
    assert reasons(gate) == [ENTRIES_PER_DAY]


def test_orders_of_another_day_do_not_count(gate, journal):
    journal.insert_order(order("gws-20261030-DE40-1-entry"))
    journal.insert_order(order("gws-20261103-DE40-1-entry"))
    assert reasons(gate) == []


def test_entries_per_day_uses_the_local_trading_date_not_the_utc_date(journal, risk_cfg, strat_cfg):
    # 2026-11-02 23:30 UTC = 2026-11-03 00:30 w Berlinie: dzień handlowy to 3 listopada
    when = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, when)
    journal.insert_order(order("gws-20261102-DE40-1-entry"))
    assert ENTRIES_PER_DAY not in reasons(gate)
    journal.insert_order(order("gws-20261103-DE40-1-entry"))
    assert ENTRIES_PER_DAY in reasons(gate)


def test_max_entries_per_day_comes_from_the_config(journal, risk_cfg, strat_cfg):
    two = dataclasses.replace(risk_cfg, max_entries_per_day=2)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), risk=two)
    journal.insert_order(order("gws-20261102-DE40-1-entry"))
    assert reasons(gate) == []
    journal.insert_order(order("gws-20261102-DE40-2-entry"))
    assert reasons(gate) == [ENTRIES_PER_DAY]


def test_smoke_orders_do_not_use_the_daily_entry(gate, journal):
    journal.insert_order(order("gws-20261102-DE40-1-smoke", state=OrderState.CONFIRMED))
    assert reasons(gate) == []
    journal.insert_order(order("gws-20261102-DE40-2-entry"))
    assert reasons(gate) == [ENTRIES_PER_DAY]


def test_zero_entries_allowed_blocks_everything(journal, risk_cfg, strat_cfg):
    zero = dataclasses.replace(risk_cfg, max_entries_per_day=0)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), risk=zero)
    assert reasons(gate) == [ENTRIES_PER_DAY]


# --- DAILY_LOSS ------------------------------------------------------------------------------------


def test_daily_loss_blocks_after_a_big_loss(gate, journal):
    journal.add_realized_pnl(DAY, D("-10"))
    assert reasons(gate) == [DAILY_LOSS]


def test_daily_loss_boundary_includes_the_risk_of_the_candidate(gate, journal):
    journal.add_realized_pnl(DAY, D("-1.47"))  # 1,47 + 0,03 (kandydat) = 1,50: nie więcej niż limit
    assert reasons(gate) == []
    journal.add_realized_pnl(DAY, D("-0.01"))  # 1,48 + 0,03 = 1,51 > 1,50
    assert reasons(gate) == [DAILY_LOSS]


def test_daily_loss_at_the_limit_blocks_even_when_the_candidate_risk_is_unknown(gate, journal):
    bad = cand(size=None)  # ryzyka kandydata nie da się policzyć: zostaje samo `loss_so_far >= limit`
    journal.add_realized_pnl(DAY, D("-1.49"))
    assert reasons(gate, bad) == [FORMAT]
    journal.add_realized_pnl(DAY, D("-0.01"))  # równo 1,50
    assert reasons(gate, bad) == [FORMAT, DAILY_LOSS]


def test_profit_and_other_days_do_not_count_as_daily_loss(gate, journal):
    journal.add_realized_pnl(DAY, D("5"))
    journal.add_realized_pnl(date(2026, 11, 1), D("-10"))  # wczoraj; łączna strata −10 > −12
    assert reasons(gate) == []


@pytest.mark.parametrize(
    ("side", "stop"), [(Side.BUY, "25752.3"), (Side.SELL, "25812.3")], ids=["long", "short"]
)
def test_daily_loss_includes_the_risk_of_the_open_position(gate, journal, side, stop):
    journal.add_realized_pnl(DAY, D("-1.46"))
    assert reasons(gate) == []  # 1,46 + 0,03 = 1,49
    journal.upsert_position(position(side=side, stop=stop))  # ryzyko otwartej: 0.001 × |25782.3 − stop| = 0,03
    assert reasons(gate) == [DAILY_LOSS, DUPLICATE]  # 1,46 + 0,03 + 0,03 = 1,52


def test_open_position_risk_scales_with_value_per_point(gate, journal):
    journal.upsert_position(position())
    journal.add_realized_pnl(DAY, D("-1.40"))
    assert reasons(gate) == [DUPLICATE]  # 1,40 + 0,03 + 0,03 = 1,46
    # vpp = 2 podwaja ryzyko otwartej i kandydata: 1,40 + 0,06 + 0,06 = 1,52
    c = cand(value_per_point=D("2"), entry_price=D("12000"))
    assert reasons(gate, c) == [DAILY_LOSS, DUPLICATE]


@pytest.mark.parametrize("state", [PositionState.OPEN, PositionState.ENTERING], ids=["no-stop", "entering"])
def test_open_position_with_unknown_stop_counts_as_a_full_max_loss(gate, journal, state):
    journal.upsert_position(position(state, stop=None))
    assert reasons(gate) == [DUPLICATE]  # 1,00 + 0,03 = 1,03 < 1,50
    journal.add_realized_pnl(DAY, D("-0.60"))  # 0,60 + 1,00 (nieznany stop) = 1,60 >= 1,50
    assert reasons(gate) == [DAILY_LOSS, DUPLICATE]


def test_profit_does_not_offset_the_open_risk_or_the_candidate(gate, journal):
    journal.add_realized_pnl(DAY, D("5"))
    journal.upsert_position(position(stop=None))  # nieznany stop: 1,00
    c = cand(size=D("0.01"), stop_distance=D("60"), entry_price=D("1000"))  # ryzyko kandydata 0,60
    assert reasons(gate, c) == [DAILY_LOSS, DUPLICATE]  # 1,00 + 0,60 = 1,60 > 1,50, mimo zysku dnia


def test_open_risk_is_computed_exactly(gate, journal):
    # 30 cyfr znaczących: `abs()` w domyślnym kontekście (28 cyfr) zaokrągliłoby odległość i zgubiło ostatnią cyfrę
    journal.upsert_position(
        PositionRecord(
            "DE40", PositionState.OPEN, "DEAL-1", Side.BUY, D("1"), D("60.0000000000000000000000000001"),
            D("30"), datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc),
        )
    )
    assert gate._open_risk(D("1")) == D("30.0000000000000000000000000001")


def test_flat_position_adds_no_open_risk(gate, journal):
    journal.upsert_position(position(PositionState.FLAT))
    journal.add_realized_pnl(DAY, D("-1.46"))
    assert reasons(gate) == []


# --- TOTAL_DRAWDOWN --------------------------------------------------------------------------------


def test_total_drawdown_blocks_entries(gate, journal):
    journal.add_realized_pnl(date(2026, 10, 30), D("-5"))
    journal.add_realized_pnl(date(2026, 11, 1), D("-7"))  # łącznie −12, a dziś 0
    assert reasons(gate) == [TOTAL_DRAWDOWN]


def test_total_drawdown_just_above_the_limit_passes(gate, journal):
    journal.add_realized_pnl(date(2026, 10, 30), D("-5"))
    journal.add_realized_pnl(date(2026, 11, 1), D("-6.99"))  # −11,99
    assert reasons(gate) == []


def test_total_drawdown_gains_offset_old_losses(gate, journal):
    journal.add_realized_pnl(date(2026, 10, 30), D("-20"))
    journal.add_realized_pnl(date(2026, 11, 1), D("9"))  # −11
    assert reasons(gate) == []


def test_total_drawdown_reached_today_also_trips_the_daily_limit(gate, journal):
    journal.add_realized_pnl(DAY, D("-12"))
    assert reasons(gate) == [DAILY_LOSS, TOTAL_DRAWDOWN]


# --- NOT_SESSION -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day",
    [date(2026, 12, 24), date(2026, 11, 7), date(2026, 11, 8), date(2026, 4, 3), date(2027, 3, 29)],
    ids=["christmas-eve", "saturday", "sunday", "good-friday", "easter-monday-2027"],
)
def test_holiday_or_weekend_is_not_session_and_window_is_not_added(journal, risk_cfg, strat_cfg, day):
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(day, 9, 6))
    assert reasons(gate) == [NOT_SESSION]  # bez OUTSIDE_WINDOW na wierzchu


def test_window_and_holiday(journal, risk_cfg, strat_cfg):
    h, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(date(2026, 12, 24), 9, 6))
    assert NOT_SESSION in reasons(h)
    for hh, mm, ok in ((9, 4, False), (9, 11, False), (9, 6, True)):
        g, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, hh, mm))
        assert (OUTSIDE_WINDOW not in reasons(g)) is ok, (hh, mm)
        assert NOT_SESSION not in reasons(g)


def test_expired_calendar_year_is_not_session(journal, risk_cfg, strat_cfg):
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(date(2028, 1, 3), 9, 6))  # poniedziałek, brak tabeli 2028
    assert reasons(gate) == [NOT_SESSION]
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(date(2028, 1, 8), 9, 6))  # sobota w wygasłym roku
    assert reasons(gate) == [NOT_SESSION]


def test_session_day_uses_the_local_date_not_the_utc_date(journal, risk_cfg, strat_cfg):
    # 2026-12-23 23:30 UTC = 2026-12-24 00:30 w Berlinie (święto), choć data UTC to zwykły dzień
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, datetime(2026, 12, 23, 23, 30, tzinfo=timezone.utc))
    assert reasons(gate) == [NOT_SESSION]


def test_calendar_failure_is_not_session(journal, risk_cfg, strat_cfg):
    class BrokenCalendar(ExchangeCalendar):
        def is_session_day(self, d):
            raise RuntimeError("calendar exploded")

    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), calendar=BrokenCalendar(XETRA))
    assert reasons(gate) == [NOT_SESSION]


# --- OUTSIDE_WINDOW --------------------------------------------------------------------------------

WINDOW_TIMES = [
    ((8, 30), False), ((8, 59, 59), False), ((9, 0), False), ((9, 4, 59, 999999), False),
    ((9, 5), True), ((9, 5, 0, 1), True), ((9, 7, 30), True), ((9, 10), True),
    ((9, 10, 0, 1), False), ((9, 10, 1), False), ((12, 0), False), ((17, 24, 59), False),
    ((17, 25), False), ((17, 30), False), ((23, 0), False),
]  # fmt: skip


@pytest.mark.parametrize(("hms", "allowed"), WINDOW_TIMES, ids=[str(t) for t, _ in WINDOW_TIMES])
def test_entry_window_boundaries_are_inclusive_at_both_ends(journal, risk_cfg, strat_cfg, hms, allowed):
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, *hms))
    assert reasons(gate) == ([] if allowed else [OUTSIDE_WINDOW])


DST_CASES = [
    # 2026-10-23 (piątek, CEST, UTC+2): okno 09:05-09:10 lokalnie = 07:05-07:10 UTC
    (datetime(2026, 10, 23, 7, 5, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 23, 7, 10, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 23, 7, 4, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 23, 7, 10, 1, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 23, 8, 5, tzinfo=timezone.utc), False),  # to 10:05 lokalnie
    # 2026-10-26 (poniedziałek, CET, UTC+1): to samo okno lokalne = 08:05-08:10 UTC
    (datetime(2026, 10, 26, 7, 5, tzinfo=timezone.utc), False),  # to 08:05 lokalnie
    (datetime(2026, 10, 26, 8, 5, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 26, 8, 10, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 26, 8, 10, 1, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 26, 9, 5, tzinfo=timezone.utc), False),  # to 10:05 lokalnie
]


@pytest.mark.parametrize(("when", "allowed"), DST_CASES, ids=[w.isoformat() for w, _ in DST_CASES])
def test_entry_window_follows_the_dst_shift(journal, risk_cfg, strat_cfg, when, allowed):
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, when)
    assert reasons(gate) == ([] if allowed else [OUTSIDE_WINDOW])


def test_window_comes_from_the_strategy_config(journal, risk_cfg, strat_cfg):
    strat = dataclasses.replace(strat_cfg, range_minutes=10, entry_deadline_after_range_min=2)  # 09:10-09:12
    for hms, allowed in (((9, 6), False), ((9, 9, 59), False), ((9, 10), True), ((9, 12), True), ((9, 12, 1), False)):
        gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, *hms), strat=strat)
        assert reasons(gate) == ([] if allowed else [OUTSIDE_WINDOW]), hms


def test_window_never_extends_to_the_flatten_time(journal, risk_cfg, strat_cfg):
    strat = dataclasses.replace(strat_cfg, entry_deadline_after_range_min=600)  # okno do 19:05, spłaszczenie 17:25
    for hms, allowed in (((17, 24, 59), True), ((17, 25), False), ((17, 25, 1), False), ((18, 0), False)):
        gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, *hms), strat=strat)
        assert reasons(gate) == ([] if allowed else [OUTSIDE_WINDOW]), hms


def test_early_close_moves_the_flatten_time(journal, risk_cfg, strat_cfg):
    profile = dataclasses.replace(XETRA, early_closes=MappingProxyType({DAY: time(12, 0)}))  # spłaszczenie 11:55
    strat = dataclasses.replace(strat_cfg, entry_deadline_after_range_min=600)
    cal = ExchangeCalendar(profile)
    for hms, allowed in (((11, 54, 59), True), ((11, 55), False), ((12, 0), False)):
        gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, *hms), strat=strat, calendar=cal)
        assert reasons(gate) == ([] if allowed else [OUTSIDE_WINDOW]), hms


def test_unevaluable_window_config_blocks(journal, risk_cfg, strat_cfg):
    strat = dataclasses.replace(strat_cfg, flatten_before_close_min=600)  # spłaszczenie przed otwarciem
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), strat=strat)
    assert reasons(gate) == [OUTSIDE_WINDOW]


# --- DUPLICATE -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", [PositionState.OPEN, PositionState.ENTERING, PositionState.EXITING, PositionState.BLOCKED]
)
def test_existing_position_is_a_duplicate(gate, journal, state):
    journal.upsert_position(position(state))
    assert DUPLICATE in reasons(gate)


def test_flat_position_is_not_a_duplicate(gate, journal):
    journal.upsert_position(position(PositionState.FLAT))
    assert reasons(gate) == []


@pytest.mark.parametrize("state", [OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN])
def test_unresolved_order_is_a_duplicate(gate, journal, state):
    journal.insert_order(order("gws-20261030-DE40-1-entry", state=state))  # starszy dzień: bez ENTRIES_PER_DAY
    assert reasons(gate) == [DUPLICATE]


@pytest.mark.parametrize("state", [OrderState.CONFIRMED, OrderState.REJECTED, OrderState.NOT_PLACED])
def test_resolved_order_is_not_a_duplicate(gate, journal, state):
    journal.insert_order(order("gws-20261030-DE40-1-entry", state=state))
    assert reasons(gate) == []


def test_unresolved_order_on_another_instrument_is_not_a_duplicate(gate, journal):
    journal.insert_order(order("gws-20261030-US100-1-entry", epic="US100", state=OrderState.UNKNOWN))
    assert reasons(gate) == []


# --- STALE_DATA ------------------------------------------------------------------------------------


@pytest.mark.parametrize("age", [0, 5, 60, D("60"), 60.0, D("59.999")])
def test_fresh_quote_passes(gate, age):
    assert reasons(gate, cand(quote_age_s=age)) == []


@pytest.mark.parametrize(
    "age",
    [
        61, D("61"), 60.001, D("60.001"), -1, D("-0.5"), float("nan"), float("inf"), D("NaN"), D("Infinity"),
        None, "5", True,
    ],  # fmt: skip
    ids=repr,
)
def test_stale_or_unusable_quote_age_blocks(gate, age):
    assert reasons(gate, cand(quote_age_s=age)) == [STALE_DATA]


def test_stale_quote_blocks(gate):
    v = gate.check_entry(cand(quote_age_s=61), ACCT)
    assert not v.passed and STALE_DATA in v.reasons


def test_stale_limit_comes_from_the_config(journal, risk_cfg, strat_cfg):
    strict = dataclasses.replace(risk_cfg, stale_quote_s=10)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), risk=strict)
    assert reasons(gate, cand(quote_age_s=10)) == []
    assert reasons(gate, cand(quote_age_s=11)) == [STALE_DATA]


# --- KILL ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("hard", [False, True])
def test_active_kill_switch_blocks_entries(gate, kill, hard):
    (kill.hard if hard else kill.soft)("test")
    assert reasons(gate) == [KILL]


def test_journal_kill_state_blocks_even_without_a_live_kill_view(journal, risk_cfg, strat_cfg):
    gate, clock = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6))  # kill=None
    assert reasons(gate) == []
    journal.set_kill(KillLevel.SOFT, "from-another-process", clock.now_utc())
    assert reasons(gate) == [KILL]


def test_in_memory_kill_blocks_when_the_journal_write_failed(gate, kill, journal, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal, "set_kill", fail)
    kill.soft("x")
    assert journal.kill_state().level is KillLevel.NONE  # dziennik nic nie wie
    assert reasons(gate) == [KILL]  # a bramka i tak blokuje


def test_kill_view_only_is_enough_to_block(journal, risk_cfg, strat_cfg):
    class View:
        def level(self):
            return KillLevel.HARD

    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), kill=View())
    assert reasons(gate) == [KILL]


def test_cleared_kill_lets_entries_through_again(gate, kill):
    kill.soft("x")
    assert reasons(gate) == [KILL]
    kill.clear("owner")
    assert reasons(gate) == []


def test_unreadable_kill_view_blocks(journal, risk_cfg, strat_cfg):
    class BrokenView:
        def level(self):
            raise RuntimeError("no level")

    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 9, 6), kill=BrokenView())
    assert reasons(gate) == [KILL]


# --- wszystkie powody razem, w kolejności z tabeli ------------------------------------------------


def test_all_failing_reasons_are_collected_in_table_order_on_a_holiday(journal, risk_cfg, strat_cfg, kill):
    xmas_eve = date(2026, 12, 24)
    gate, clock = build_gate(journal, risk_cfg, strat_cfg, berlin(xmas_eve, 9, 6), kill=kill)
    journal.insert_order(order("gws-20261224-DE40-1-entry"))  # ENTRIES_PER_DAY (REJECTED: bez DUPLICATE z zlecenia)
    journal.add_realized_pnl(xmas_eve, D("-10"))  # DAILY_LOSS
    journal.add_realized_pnl(date(2026, 12, 1), D("-5"))  # łącznie −15: TOTAL_DRAWDOWN
    journal.upsert_position(position())  # DUPLICATE
    kill.soft("test")  # KILL
    c = cand(side="BUY", size=D("0.2"), quote_age_s=61)  # FORMAT (zły typ side), strata 6, dźwignia, depozyt, STALE
    v = gate.check_entry(c, acct(equity="40", available="40"))
    assert not v.passed
    assert v.reasons == [
        FORMAT, LOSS_PER_TRADE, LEVERAGE, MARGIN, ENTRIES_PER_DAY, DAILY_LOSS, TOTAL_DRAWDOWN, NOT_SESSION,
        DUPLICATE, STALE_DATA, KILL,
    ]  # fmt: skip


def test_all_failing_reasons_are_collected_in_table_order_outside_the_window(journal, risk_cfg, strat_cfg, kill):
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 8, 0), kill=kill)
    journal.insert_order(order("gws-20261102-DE40-1-entry"))
    journal.add_realized_pnl(DAY, D("-13"))
    journal.upsert_position(position())
    kill.hard("test")
    c = cand(stop_distance=D("0"), size=D("0.2"), quote_age_s=100)
    v = gate.check_entry(c, ACCT)
    assert v.reasons == [
        NO_STOP, LEVERAGE, MARGIN, ENTRIES_PER_DAY, DAILY_LOSS, TOTAL_DRAWDOWN, OUTSIDE_WINDOW, DUPLICATE,
        STALE_DATA, KILL,
    ]  # fmt: skip


# --- odporność: błąd odczytu = brak możliwości oceny = blokada --------------------------------------

JOURNAL_FAULTS = [
    ("count_entry_orders", [ENTRIES_PER_DAY]),
    ("realized_pnl", [DAILY_LOSS]),
    ("realized_pnl_total", [TOTAL_DRAWDOWN]),
    ("current_position", [DAILY_LOSS, DUPLICATE]),  # ryzyko otwartej też nie do policzenia
    ("unresolved_orders", [DUPLICATE]),
    ("kill_state", [KILL]),
]


@pytest.mark.parametrize("exc", [JournalReadError("locked"), RuntimeError("boom")], ids=["journal-error", "unexpected"])
@pytest.mark.parametrize(("method", "expected"), JOURNAL_FAULTS, ids=[m for m, _ in JOURNAL_FAULTS])
def test_journal_read_failure_blocks_the_dependent_check(gate, journal, monkeypatch, method, expected, exc):
    def fail(*a, **k):
        raise exc

    monkeypatch.setattr(journal, method, fail)
    v = gate.check_entry(cand(), ACCT)
    assert not v.passed
    assert v.reasons == expected


def test_clock_failure_blocks_every_day_dependent_check(journal, risk_cfg, strat_cfg):
    class BrokenClock:
        def now_utc(self):
            raise RuntimeError("clock exploded")

    gate = RiskGate(risk_cfg, strat_cfg, journal, CAL, BrokenClock())
    assert reasons(gate) == [ENTRIES_PER_DAY, DAILY_LOSS, NOT_SESSION]


# --- wyjścia i brak skutków ubocznych ---------------------------------------------------------------


def test_exits_are_never_blocked_even_when_kill_is_hard(gate, kill):
    kill.hard("test")
    assert gate.check_entry(cand(), ACCT).reasons == [KILL]
    assert gate.check_exit().passed


def test_exit_verdict_is_a_plain_pass(gate):
    assert gate.check_exit() == Verdict(True, [])


def test_exit_check_touches_nothing(risk_cfg, strat_cfg):
    # żadnego dziennika, kalendarza ani zegara: wyjście nie zależy od niczego, co mogłoby zawieść
    assert RiskGate(risk_cfg, strat_cfg, None, None, None).check_exit() == Verdict(True, [])


def test_exit_passes_with_everything_blocked_and_the_journal_broken(gate, kill, journal, monkeypatch):
    kill.hard("test")
    journal.add_realized_pnl(DAY, D("-100"))
    for name in ("count_orders", "realized_pnl", "kill_state", "current_position"):
        monkeypatch.setattr(journal, name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert gate.check_exit().passed


def test_the_gate_does_not_write_to_the_journal(gate, journal):
    journal.add_realized_pnl(DAY, D("-10"))
    before = (journal.events(), journal.kill_state(), journal.realized_pnl_total(), journal.count_orders(DAY))
    for _ in range(3):
        gate.check_entry(cand(), ACCT)
        gate.check_entry(cand(size=None), None)
        gate.check_exit()
    after = (journal.events(), journal.kill_state(), journal.realized_pnl_total(), journal.count_orders(DAY))
    assert before == after


def test_an_unexpected_gate_error_blocks_with_gate_error_not_format(gate, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(gate, "_window", boom)
    with caplog.at_level(logging.ERROR, logger="gwstock.risk"):
        verdict = gate.check_entry(cand(), ACCT)  # nie rzuca
    # kandydat jest poprawny: FORMAT wskazywałby na złe pola, a zawiodła sama bramka
    assert verdict == Verdict(False, [GATE_ERROR]) and GATE_ERROR == "GATE_ERROR"
    assert "risk gate failed unexpectedly" in caplog.text and "bug" in caplog.text


def test_kill_view_is_optional(journal, risk_cfg, strat_cfg):
    gate = RiskGate(risk_cfg, strat_cfg, journal, CAL, FakeClock(berlin(DAY, 9, 6)))
    assert gate.check_entry(cand(), ACCT).passed


# --- check_smoke: próba L0 (jedno minimalne wejście poza oknem strategii) --------------------------


def smoke_reasons(gate: RiskGate, c=None, account=ACCT) -> list[str]:
    return gate.check_smoke(cand() if c is None else c, account).reasons


def test_smoke_ignores_the_session_the_window_and_the_daily_entry(journal, risk_cfg, strat_cfg):
    saturday = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, saturday)
    journal.insert_order(order("gws-20261107-DE40-1-entry"))
    assert reasons(gate) == [ENTRIES_PER_DAY, NOT_SESSION]
    assert smoke_reasons(gate) == []
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(DAY, 12, 0))  # dzień sesji, poza oknem 09:05-09:10
    journal.insert_order(order("gws-20261102-DE40-1-entry"))
    assert reasons(gate) == [ENTRIES_PER_DAY, OUTSIDE_WINDOW]
    assert smoke_reasons(gate) == []


def test_smoke_keeps_every_other_check_in_table_order(journal, risk_cfg, strat_cfg, kill):
    xmas_eve = date(2026, 12, 24)
    gate, _ = build_gate(journal, risk_cfg, strat_cfg, berlin(xmas_eve, 9, 6), kill=kill)
    journal.insert_order(order("gws-20261224-DE40-1-entry"))
    journal.add_realized_pnl(xmas_eve, D("-10"))
    journal.add_realized_pnl(date(2026, 12, 1), D("-5"))
    journal.upsert_position(position())
    kill.soft("test")
    c = cand(side="BUY", size=D("0.2"), quote_age_s=61)
    v = gate.check_smoke(c, acct(equity="40", available="40"))
    assert not v.passed
    assert v.reasons == [
        FORMAT, LOSS_PER_TRADE, LEVERAGE, MARGIN, DAILY_LOSS, TOTAL_DRAWDOWN, DUPLICATE, STALE_DATA, KILL,
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        (dict(stop_distance=None), [FORMAT, NO_STOP]),
        (dict(stop_distance=D("0")), [NO_STOP]),
        (dict(stop_distance=D("1000.01")), [LOSS_PER_TRADE]),  # 0.001 × 1000.01 = 1.00001 > 1.00
        (dict(quote_age_s=61), [STALE_DATA]),
    ],
    ids=["no-stop-field", "zero-stop", "loss-per-trade", "stale"],
)
def test_smoke_blocks_on_its_own_checks(gate, over, expected):
    assert smoke_reasons(gate, cand(**over)) == expected


def test_smoke_is_blocked_by_the_live_kill_switch_and_by_the_account(gate, kill):
    assert smoke_reasons(gate, account=acct(equity="0", available="0")) == [LEVERAGE, MARGIN]
    kill.soft("x")
    assert smoke_reasons(gate) == [KILL]


def test_smoke_never_raises_on_an_internal_error(gate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(gate, "_today", boom)
    assert gate.check_smoke(cand(), ACCT) == Verdict(False, [GATE_ERROR])
