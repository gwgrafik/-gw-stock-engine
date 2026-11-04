"""Bramki pasm w silniku z pasmami (zadanie 12, projekt §2.3, A3): wejścia dnia liczone per instrument, a pasmo B1/M5
ma własne okno (od pierwszej świecy sygnału do godziny spłaszczenia pasma) i własny limit wejść z pliku pasma.

Bramka v1 (`RiskGate`) zostaje bez zmian: liczy wejścia wszystkich instrumentów (zachowanie DE40 v1 bajt w bajt).
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.calendar import NYSE, XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import load_bar_rule_config
from gwstock.journal import OrderRecord
from gwstock.risk import (
    ENTRIES_PER_DAY,
    FORMAT,
    NOT_SESSION,
    OUTSIDE_WINDOW,
    BarLaneRiskGate,
    EntryCandidate,
    LaneRiskGate,
    RiskGate,
)
from gwstock.types import AccountInfo, OrderState, Side

D = Decimal
DAY = date(2026, 11, 2)
XC = ExchangeCalendar(XETRA)
NC = ExchangeCalendar(NYSE)
BAR = load_bar_rule_config(Path("config/lane_us500_b1_m5.toml"))
ACCT = AccountInfo("ACC-1", "EUR", "ENABLED", D("40"), D("40"), D("0"))


def entry(journal, epic: str, seq: int, day: date = DAY) -> None:
    ts = datetime(2026, 11, 2, 8, 5, tzinfo=timezone.utc)
    oid = f"gws-{day:%Y%m%d}-{epic}-{seq}-entry"
    journal.insert_order(OrderRecord(oid, epic, Side.BUY, D("0.01"), D("5"), OrderState.CONFIRMED, "R", "D", ts, ts))


def cand(epic: str, **over) -> EntryCandidate:
    fields = dict(
        epic=epic, side=Side.BUY, size=D("0.01"), stop_distance=D("5"), entry_price=D("7583"),
        value_per_point=D("1"), margin_rate=D("0.05"), quote_age_s=1,
    )  # fmt: skip
    fields.update(over)
    return EntryCandidate(**fields)


def ny(hh: int, mm: int, day: date = DAY) -> datetime:
    return NC.local_to_utc(day, time(hh, mm))


def test_the_v1_gate_still_counts_entries_of_every_instrument(journal, risk_cfg, strat_cfg, kill):
    entry(journal, "US500", 1)
    clock = FakeClock(XC.local_to_utc(DAY, time(9, 6)))
    gate = RiskGate(risk_cfg, strat_cfg, journal, XC, clock, kill)
    assert ENTRIES_PER_DAY in gate.check_entry(cand("DE40", entry_price=D("25781"), value_per_point=D("1"),
                                                     size=D("0.001"), stop_distance=D("30")), ACCT).reasons


def test_a_lane_gate_counts_only_its_own_instrument(journal, risk_cfg, strat_cfg, kill):
    entry(journal, "US500", 1)
    clock = FakeClock(XC.local_to_utc(DAY, time(9, 6)))
    gate = LaneRiskGate(risk_cfg, strat_cfg, journal, XC, clock, kill)
    c = cand("DE40", entry_price=D("25781"), size=D("0.001"), stop_distance=D("30"))
    assert gate.check_entry(c, ACCT).passed
    entry(journal, "DE40", 2)
    assert ENTRIES_PER_DAY in gate.check_entry(c, ACCT).reasons


def gate_at(journal, risk_cfg, kill, when: datetime) -> BarLaneRiskGate:
    return BarLaneRiskGate(risk_cfg, BAR, journal, NC, FakeClock(when), kill)


@pytest.fixture
def bar_gate(journal, risk_cfg, kill):
    return gate_at(journal, risk_cfg, kill, ny(9, 50)), None


def test_bar_gate_window_runs_from_the_first_signal_candle_close_to_the_lane_flatten(journal, risk_cfg, kill):
    def reasons(when):
        return gate_at(journal, risk_cfg, kill, when).check_entry(cand("US500"), ACCT).reasons

    assert reasons(ny(9, 50)) == []  # 09:50 NY: świeca 09:45-09:50 zakończona
    assert OUTSIDE_WINDOW in reasons(ny(9, 49))
    assert OUTSIDE_WINDOW in reasons(ny(12, 0))  # po oknie porannym (ostatnia świeca 11:40-11:45, do 11:55)
    assert reasons(ny(15, 34)) == []
    assert OUTSIDE_WINDOW in reasons(ny(15, 55))  # godzina spłaszczenia pasma


def test_bar_gate_has_no_session_on_a_nyse_holiday(journal, risk_cfg, kill):
    thanksgiving = date(2026, 11, 26)
    gate = BarLaneRiskGate(risk_cfg, BAR, journal, NC, FakeClock(ny(10, 0, thanksgiving)), kill)
    assert NOT_SESSION in gate.check_entry(cand("US500"), ACCT).reasons


def test_bar_gate_limits_entries_per_ny_day_by_the_lane_file_and_per_instrument(bar_gate, journal):
    gate, _ = bar_gate
    entry(journal, "DE40", 1)
    for seq in range(2, 2 + BAR.max_entries_per_day - 1):
        entry(journal, "US500", seq)
    assert gate.check_entry(cand("US500"), ACCT).passed  # 9 wejść US500 (+1 DE40) < 10
    entry(journal, "US500", 99)
    assert ENTRIES_PER_DAY in gate.check_entry(cand("US500"), ACCT).reasons


def test_bar_gate_refuses_another_instrument(bar_gate):
    gate, _ = bar_gate
    assert FORMAT in gate.check_entry(cand("DE40"), ACCT).reasons


def test_bar_gate_never_blocks_an_exit(bar_gate):
    gate, _ = bar_gate
    assert gate.check_exit().passed


def test_bar_gate_refuses_a_calendar_of_another_time_zone(journal, risk_cfg, kill):
    with pytest.raises(ValueError):
        BarLaneRiskGate(risk_cfg, BAR, journal, XC, FakeClock(ny(10, 0)), kill)
