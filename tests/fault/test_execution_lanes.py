"""Testy wykonawcy świadomego pasma (projekt wielu pasm, zadanie 9): `LaneSpec`, pamięć wyjść per instrument, przyczyna
TARGET, limit czasu pozycji, wejście i zabezpieczenie z celem, przeliczenia walut (`FxView`), dwufazowe księgowanie
i przeplatane awaryjne spłaszczenie wielu pasm.

Niezmienniki pod lupą (prawdziwe pieniądze):
- pasmo DE40 opisane `LaneSpec` zachowuje się bajt w bajt jak wykonawca v1 (te same wywołania brokera i zapisy);
- awaryjne spłaszczenie przy nieczytelnym dzienniku zamyka własny deal KAŻDEGO pasma i nigdy deala innego pasma;
- zamknięcie celem w wyścigu z limitem czasu jest TARGET, nie FORCED; pasmo bez celu księguje cel jak STOP;
- pasmo z celem nigdy nie otwiera pozycji bez celu po cichu i zabezpiecza stop RAZEM z celem (`set_protection`);
- ryzyko w walucie instrumentu jest sprawdzane w walucie rachunku po kursie niekorzystnym; brak kursu przy wejściu
  blokuje, przy straży po wypełnieniu zamyka (chyba że parytet jest górnym ograniczeniem);
- wynik w walucie obcej nigdy nie trafia do licznika EUR przed rozliczeniem (faza 2), i trafia tam dokładnie raz;
- wyjść nie blokuje ani bramka, ani kill switch.

Zegar startowy jak w `test_execution.py`: 09:06 w Berlinie (w oknie wejścia). Rachunek ACC-1 z saldem 40 EUR.
"""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.broker.fake import FakeBroker
from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import load_config
from gwstock.costs import CostModel
from gwstock.execution import Executor, ExitMemory
from gwstock.journal import Journal, JournalReadError, PositionRecord
from gwstock.lanes import LaneSpec
from gwstock.orders.manager import OrderManager
from gwstock.risk import KillSwitch, RiskGate
from gwstock.strategy import EntrySignal
from gwstock.strategy_bars import BarEntryPlan
from gwstock.types import (
    ClosedDeal,
    InstrumentRules,
    KillLevel,
    OpenPosition,
    OrderState,
    PositionState,
    Quote,
    Side,
)
from tests.conftest import RecordingNotifier

D = Decimal
O = OrderState
P = PositionState

DAY = date(2026, 11, 2)
T_ENTRY = datetime(2026, 11, 2, 8, 6, tzinfo=timezone.utc)  # 09:06 w Berlinie
DE_CID = "gws-20261102-DE40-1-entry"
US_CID = "gws-20261102-US500-1-entry"
US_RULES = InstrumentRules("US500", D("0.01"), D("0.01"), D("1"), D("0.05"), D("0.5"), D("0.1"), "USD")
US_BID, US_ASK = D("7583.0"), D("7583.6")
US_FILL = US_ASK  # BUY po ask
US_STOP_D, US_TARGET_D = D("10"), D("15")
US_STOP, US_TARGET = US_FILL - US_STOP_D, US_FILL + US_TARGET_D


class StubFx:
    """`FxView` testu: kwota USD × `factor` = EUR po kursie niekorzystnym; `factor=None`: brak ważnego kursu."""

    account_currency = "EUR"

    def __init__(self, factor: Decimal | None = D("0.95"), par_upper: bool = True) -> None:
        self.factor = factor
        self.par_upper = par_upper
        self.asked: list[tuple[Decimal, str]] = []

    def to_account(self, amount: Decimal, currency: str) -> Decimal | None:
        self.asked.append((amount, currency))
        assert currency == "USD"
        return None if self.factor is None else amount * self.factor

    def par_is_upper_bound(self, currency: str) -> bool:
        assert currency == "USD"
        return self.par_upper


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T_ENTRY)


@pytest.fixture
def us_quote(clock) -> Quote:
    return Quote("US500", US_BID, US_ASK, clock.now_utc())


@pytest.fixture
def fake(clock, rules, quote, us_quote) -> FakeBroker:
    """Dwa instrumenty (DE40 główny, US500 w USD); nadpisuje fixture z conftest (menedżer używa tego brokera)."""
    broker = FakeBroker(clock, [rules, US_RULES], [quote, us_quote])
    broker.accounts = [a for a in broker.accounts if a.account_id == "ACC-1"]
    return broker


@pytest.fixture
def fx() -> StubFx:
    return StubFx()


@pytest.fixture
def memory() -> ExitMemory:
    return ExitMemory()


@pytest.fixture
def de_lane(strat_cfg, risk_cfg, calendar) -> LaneSpec:
    return LaneSpec.from_v1(strat_cfg, risk_cfg, calendar)


@pytest.fixture
def us_lane(calendar) -> LaneSpec:
    return LaneSpec("US500", calendar, D("0.25"), D("1.00"), currency="USD", uses_target=True)


@pytest.fixture
def us_gate(risk_cfg, strat_cfg, journal, calendar, clock, kill) -> RiskGate:
    return RiskGate(risk_cfg, replace(strat_cfg, instrument="US500"), journal, calendar, clock, kill)


@pytest.fixture
def lane_build(mgr, fake, journal, kill, cost_cfg, clock, risk_cfg, notifier, memory):
    def _build(lane, gate, *, fx=None, live=True):
        return Executor.for_lane(
            mgr, fake, journal, gate, kill, CostModel(cost_cfg), clock, lane, risk_cfg, notifier, live=live,
            memory=memory, fx=fx,
        )  # fmt: skip

    return _build


@pytest.fixture
def de(lane_build, de_lane, gate) -> Executor:
    return lane_build(de_lane, gate)


@pytest.fixture
def us(lane_build, us_lane, us_gate, fx) -> Executor:
    return lane_build(us_lane, us_gate, fx=fx)


@pytest.fixture
def plan(us_quote) -> BarEntryPlan:
    """Plan B1 na US500: BUY 0,01, stop 10 pkt, cel 15 pkt (ryzyko 0,10 USD)."""
    return BarEntryPlan(Side.BUY, D("0.01"), US_STOP_D, US_TARGET_D, us_quote)


def payloads(journal, source):
    return [json.loads(e.payload) for e in journal.events(source)]


def events(journal, code=None):
    found = payloads(journal, "event")
    return found if code is None else [e for e in found if e["event"] == code]


def alerts(notifier, text):
    return [(level, message) for level, message in notifier.alerts if text in message]


def hold(fake, journal, epic, deal_id, side, size, level, stop, target=None):
    """Pozycja OPEN pasma u brokera i w dzienniku (bez wejścia przez wykonawcę)."""
    pos = OpenPosition(deal_id, epic, side, size, level, stop, T_ENTRY)
    fake.positions[deal_id] = pos
    journal.upsert_position(PositionRecord(epic, P.OPEN, deal_id, side, size, level, stop, T_ENTRY))
    return pos


# --- LaneSpec ----------------------------------------------------------------------------------------


def test_lane_spec_from_v1_takes_the_de40_values_from_the_v1_configs(strat_cfg, risk_cfg, calendar):
    lane = LaneSpec.from_v1(strat_cfg, risk_cfg, calendar)
    assert lane == LaneSpec("DE40", calendar, D("0.25"), D("1.00"))
    assert lane.currency is None and lane.uses_target is False and lane.calendar is calendar


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epic", "US-500"), ("epic", ""), ("epic", 5), ("calendar", None),
        ("slippage_guard_fraction", D("0")), ("slippage_guard_fraction", 0.25), ("max_loss_per_trade", D("-1")),
        ("max_loss_per_trade", D("NaN")), ("currency", "usd"), ("currency", "US"), ("uses_target", 1),
    ],
)  # fmt: skip
def test_lane_spec_rejects_bad_values(us_lane, field, value):
    with pytest.raises((ValueError, TypeError), match=field):
        replace(us_lane, **{field: value})


def test_executor_takes_either_a_lane_or_the_v1_configs(mgr, fake, journal, gate, kill, cost_cfg, clock, strat_cfg,
                                                       risk_cfg, calendar, notifier, de_lane):  # fmt: skip
    args = (mgr, fake, journal, gate, kill, CostModel(cost_cfg), clock)
    with pytest.raises(ValueError, match="lane"):
        Executor(*args, strat_cfg, risk_cfg, calendar, notifier, lane=de_lane)
    with pytest.raises(ValueError, match="strat"):
        Executor(*args, None, risk_cfg, None, notifier)


def test_a_foreign_currency_lane_requires_an_fx_view(lane_build, us_lane, us_gate):
    with pytest.raises(ValueError, match="FxView"):
        lane_build(us_lane, us_gate, fx=None)
    with pytest.raises(ValueError, match="currency"):
        lane_build(us_lane, us_gate, fx=type("Fx", (StubFx,), {"account_currency": "eur"})())


def test_a_lane_in_the_account_currency_needs_no_conversion(lane_build, us_gate, calendar, fx):
    lane = LaneSpec("US500", calendar, D("0.25"), D("1.00"), currency="EUR")
    assert lane_build(lane, us_gate, fx=fx).foreign is False
    assert lane_build(LaneSpec("US500", calendar, D("0.25"), D("1.00")), us_gate).foreign is False


# --- DE40 przez LaneSpec: bajt w bajt jak wykonawca v1 ------------------------------------------------


async def _de40_day(tmp_path, name, clock_start, use_lane):
    """Pełny przebieg wykonawcy DE40 na świeżym dzienniku: wejście, ponowne zabezpieczenie, spłaszczenie, drugie
    wejście bez stopu i awaryjne spłaszczenie. Zwraca wywołania brokera i całą treść dziennika."""
    strat, risk, costs = load_config(Path("config"))
    risk = replace(risk, max_entries_per_day=2)
    clock = FakeClock(clock_start)
    calendar = ExchangeCalendar(XETRA)
    rules = InstrumentRules("DE40", D("0.001"), D("0.001"), D("1"), D("0.05"), D("2.6"))
    q = Quote("DE40", D("25780.3"), D("25782.3"), clock.now_utc())
    fake = FakeBroker(clock, rules, q)
    fake.accounts = [a for a in fake.accounts if a.account_id == "ACC-1"]
    data = tmp_path / name
    data.mkdir()
    journal = Journal.open(str(data / "journal.sqlite"))
    notifier = RecordingNotifier()
    kill = KillSwitch(journal, clock, data, notifier)
    gate = RiskGate(risk, strat, journal, calendar, clock, kill)
    mgr = OrderManager(journal, fake, clock, risk)
    args = (mgr, fake, journal, gate, kill, CostModel(costs), clock)
    if use_lane:
        ex = Executor.for_lane(*args, LaneSpec.from_v1(strat, risk, calendar), risk, notifier, live=True)
    else:
        ex = Executor(*args, strat, risk, calendar, notifier, live=True)
    await ex.enter(EntrySignal(Side.BUY, D("30"), D("0.007"), q), rules, DAY)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    await ex.protect_unprotected(fake.positions["DEAL-1"])
    clock.advance(60)
    fake.set_quote(Quote("DE40", D("25810.3"), D("25813.3"), clock.now_utc()))
    await ex.flatten(journal.current_position("DE40"), "FORCED")
    q2 = Quote("DE40", D("25790.3"), D("25792.3"), clock.now_utc())
    fake.set_quote(q2)
    await ex.enter(EntrySignal(Side.SELL, D("1000"), D("0.007"), q2), rules, DAY)  # straż wielkości zamyka
    kill.hard("TEST")
    rows = journal.current_position("DE40")
    await ex.emergency_flatten()
    result = (
        list(fake.calls),
        [(e.source, e.payload, e.ts) for e in journal.events()],
        journal.trades(),
        rows,
        journal.current_position("DE40"),
        journal.realized_pnl(DAY),
        list(notifier.alerts),
    )
    journal.close()
    return result


async def test_de40_lane_spec_is_byte_identical_to_the_v1_executor(tmp_path):
    v1 = await _de40_day(tmp_path, "v1", T_ENTRY, use_lane=False)
    lane = await _de40_day(tmp_path, "lane", T_ENTRY, use_lane=True)
    assert lane == v1
    assert "close_position" in v1[0] and v1[2]  # przebieg rzeczywiście coś zamknął i zaksięgował


# --- pamięć wyjść per instrument: awaryjne spłaszczenie przy nieczytelnym dzienniku ---------------------


def unreadable_positions(journal, monkeypatch):
    def broken(_epic):
        raise JournalReadError("database is locked")

    monkeypatch.setattr(journal, "current_position", broken)


async def two_held_positions(de, us, fake, journal, kill):
    """DE40 DEAL-A i US500 DEAL-B otwarte; każdy wykonawca widział swój deal (przebieg straży przy HARD)."""
    hold(fake, journal, "DE40", "DEAL-A", Side.BUY, D("0.007"), D("25782.3"), D("25752.3"))
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.01"), US_FILL, US_STOP)
    kill.hard("TEST")
    assert await de.guard_adopted_fill() is False and await us.guard_adopted_fill() is False
    fake.calls.clear()


async def test_exit_memory_keeps_the_last_own_deal_per_instrument(de, us, fake, journal, kill, memory):
    await two_held_positions(de, us, fake, journal, kill)
    assert memory.last_own_deal_by_epic == {"DE40": "DEAL-A", "US500": "DEAL-B"}


async def test_emergency_flatten_of_two_lanes_with_an_unreadable_journal_closes_both_own_deals(
    de, us, fake, journal, kill, notifier, monkeypatch
):
    await two_held_positions(de, us, fake, journal, kill)
    unreadable_positions(journal, monkeypatch)
    await de.emergency_flatten()
    await us.emergency_flatten()
    assert fake.positions == {}  # obie pozycje zamknięte: każde pasmo zamknęło SWÓJ deal
    assert fake.calls == ["list_positions", "close_position", "list_positions", "close_position"]
    assert [d.deal_id for d in await fake.closed_since(T_ENTRY)] == ["DEAL-A", "DEAL-B"]
    assert len(alerts(notifier, "EMERGENCY_FLATTEN_JOURNAL_UNREADABLE: DEAL-A closed")) == 1
    assert len(alerts(notifier, "EMERGENCY_FLATTEN_JOURNAL_UNREADABLE: DEAL-B closed")) == 1
    assert kill.level() is KillLevel.HARD


async def test_a_lane_never_closes_the_deal_of_another_lane_with_an_unreadable_journal(
    de, us, fake, journal, kill, notifier, monkeypatch
):
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.01"), US_FILL, US_STOP)
    kill.hard("TEST")
    await us.guard_adopted_fill()  # tylko pasmo US500 widziało swój deal
    fake.calls.clear()
    unreadable_positions(journal, monkeypatch)
    await de.emergency_flatten()
    assert fake.calls == [] and "DEAL-B" in fake.positions  # DE40 nie zna własnego deala: nic nie zamyka
    assert alerts(notifier, "no own deal seen in this process")
    await us.emergency_flatten()
    assert fake.positions == {} and fake.calls == ["list_positions", "close_position"]


async def test_protection_remembers_the_deal_under_its_own_instrument(fake, memory, lane_build, gate, de_lane):
    de = lane_build(de_lane, gate)
    pos = OpenPosition("DEAL-X", "DE40", Side.BUY, D("0.007"), D("25782.3"), None, T_ENTRY)
    fake.positions["DEAL-X"] = pos
    assert await de.protect_unprotected(pos) is True
    assert memory.last_own_deal_by_epic == {"DE40": "DEAL-X"}
