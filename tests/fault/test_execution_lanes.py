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
from gwstock.execution import Executor, ExitMemory, emergency_flatten_lanes
from gwstock.journal import Journal, JournalReadError, PositionRecord
from gwstock.lanes import LaneSpec
from gwstock.orders.manager import OrderManager
from gwstock.risk import KillSwitch, RiskGate
from gwstock.strategy import EntrySignal
from gwstock.strategy_bars import BarEntryPlan
from gwstock.types import (
    BrokerError,
    ClosedDeal,
    InstrumentRules,
    KillLevel,
    OpenPosition,
    OrderState,
    PositionState,
    Quote,
    Side,
    TargetedEntryRequest,
    TargetedOpenPosition,
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


# --- przyczyna wyjścia: TARGET przed zamiarem silnika; limit czasu pozycji -------------------------------


@pytest.fixture
def ut(lane_build, us_gate, calendar) -> Executor:
    """Pasmo z celem w walucie rachunku (bez przeliczeń): przyczyna wyjścia i limit czasu bez dwufazowej księgi."""
    return lane_build(LaneSpec("US500", calendar, D("0.25"), D("1.00"), uses_target=True), us_gate)


def hold_targeted(fake, journal, deal_id="DEAL-T"):
    pos = TargetedOpenPosition(deal_id, "US500", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY, target_level=US_TARGET)
    fake.positions[deal_id] = pos
    journal.upsert_position(PositionRecord("US500", P.OPEN, deal_id, Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY))
    return pos


async def test_time_limit_flatten_racing_the_target_books_target_not_forced(ut, fake, journal, kill):
    hold_targeted(fake, journal)
    fake.on_call("close_position", lambda: fake.trigger_target("DEAL-T"), once=True)  # cel zamyka w chwili DELETE
    closed = await ut.flatten(journal.current_position("US500"), "TIME_LIMIT")
    assert closed is not None and closed.reason == "TARGET" and closed.exit_level == US_TARGET
    assert fake.calls.count("close_position") == 1  # po odmowie lista pozycji, nigdy druga wysyłka
    assert journal.get_meta("exit_intent:DEAL-T") == "TIME_LIMIT"  # zamiar silnika był zapisany
    [row] = journal.trades()
    assert row.exit_reason == "TARGET" and row.exit_fill == US_TARGET
    [flat] = events(journal, "FLATTENED")
    assert flat["flatten_reason"] == "TIME_LIMIT" and flat["close_reason"] == "TARGET"
    assert events(journal, "TRADE_BOOKED")[0]["exit_reason"] == "TARGET"
    assert journal.current_position("US500") == PositionRecord("US500", P.FLAT)
    assert kill.level() is KillLevel.NONE


async def test_time_limit_flatten_is_booked_forced_with_the_time_limit_reason(ut, fake, journal):
    hold_targeted(fake, journal)
    closed = await ut.flatten(journal.current_position("US500"), "TIME_LIMIT")
    assert closed is not None and closed.reason == "MANUAL"  # zamknięcie wywołaniem u brokera
    assert fake.calls == ["close_position", "closed_since", "instrument_rules"]  # bez kontekstu: reguły brokera
    [row] = journal.trades()
    assert row.exit_reason == "FORCED"  # wyjście zainicjował silnik (limit czasu pozycji)
    [flat] = events(journal, "FLATTENED")
    assert flat["flatten_reason"] == "TIME_LIMIT" and flat["close_reason"] == "MANUAL"
    assert journal.get_meta("exit_intent:DEAL-T") == "TIME_LIMIT"


async def test_time_limit_flatten_is_never_blocked_by_the_kill_switch(ut, fake, journal, kill):
    hold_targeted(fake, journal)
    kill.hard("TEST")
    assert await ut.flatten(journal.current_position("US500"), "TIME_LIMIT") is not None
    assert fake.positions == {} and journal.trades()[0].exit_reason == "FORCED"


@pytest.mark.parametrize(
    ("uses_target", "broker_reason", "intent", "expected"),
    [
        (True, "STOP", True, "STOP"),
        (True, "TARGET", True, "TARGET"),  # wyścig limitu czasu z celem: przyczyna brokera przed zamiarem silnika
        (True, "TARGET", False, "TARGET"),
        (True, "MANUAL", True, "FORCED"),
        (True, "MANUAL", False, "MANUAL"),
        (True, "FORCED", False, "FORCED"),
        (False, "TARGET", False, "STOP"),  # pasmo bez celu (DE40 v1): cel ustawiony ręcznie księguje się jak STOP
        (False, "TARGET", True, "STOP"),
        (False, "STOP", True, "STOP"),
        (False, "MANUAL", True, "FORCED"),
        (False, "MANUAL", False, "MANUAL"),
    ],
)
async def test_exit_reason_order_is_stop_then_target_then_engine_intent_then_broker(
    lane_build, us_gate, calendar, journal, uses_target, broker_reason, intent, expected
):
    ex = lane_build(LaneSpec("US500", calendar, D("0.25"), D("1.00"), uses_target=uses_target), us_gate)
    record = PositionRecord("US500", P.EXITING, "DEAL-T", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    journal.upsert_position(record)
    if intent:
        journal.set_meta("exit_intent:DEAL-T", "TIME_LIMIT")
    await ex.book(record, ClosedDeal("DEAL-T", "US500", US_TARGET, broker_reason, T_ENTRY))
    assert journal.trades()[0].exit_reason == expected


async def test_target_exit_costs_no_extra_stop_slippage_in_the_ledger(lane_build, us_gate, calendar, journal):
    ex = lane_build(LaneSpec("US500", calendar, D("0.25"), D("1.00"), uses_target=True), us_gate)
    record = PositionRecord("US500", P.EXITING, "DEAL-T", Side.BUY, D("1"), US_FILL, US_STOP, T_ENTRY)
    await ex.book(record, ClosedDeal("DEAL-T", "US500", US_TARGET, "TARGET", T_ENTRY))
    await ex.book(replace(record, deal_id="DEAL-S"), ClosedDeal("DEAL-S", "US500", US_STOP, "STOP", T_ENTRY))
    target, stop = journal.trades()
    assert target.slippage_penalty == D("0.50")  # tylko poślizg wejścia 0,5 pkt × 1
    assert stop.slippage_penalty == D("1.50")  # wejście 0,5 + stop 1,0


# --- wejście i zabezpieczenie z celem zysku ---------------------------------------------------------------


def spy_requests(fake, monkeypatch):
    sent = []
    real = fake.open_position

    async def open_position(req):
        sent.append(req)
        return await real(req)

    monkeypatch.setattr(fake, "open_position", open_position)
    return sent


def strip_stop_on_confirm(fake, monkeypatch):
    """Broker przyjął wejście BEZ stopu (i bez celu): potwierdzenie i lista pozycji nie mają ochrony."""
    real = fake.confirm

    async def confirm(ref):
        snapshot = await real(ref)
        plain = OpenPosition(*(getattr(snapshot, f) for f in ("deal_id", "epic", "side", "size", "open_level")),
                             None, snapshot.opened_at)  # fmt: skip
        fake.positions[snapshot.deal_id] = plain
        return plain

    monkeypatch.setattr(fake, "confirm", confirm)


async def test_target_lane_enters_with_a_targeted_request_and_the_target_attached_atomically(
    ut, fake, journal, plan, kill, monkeypatch
):
    sent = spy_requests(fake, monkeypatch)
    rec = await ut.enter(plan, US_RULES, DAY)
    assert rec.state is O.CONFIRMED and rec.order_id == US_CID and rec.deal_id == "DEAL-1"
    assert fake.calls == ["account", "open_position", "confirm"]
    [req] = sent
    assert isinstance(req, TargetedEntryRequest) and req.target_distance == US_TARGET_D and req.epic == "US500"
    pos = fake.positions["DEAL-1"]
    assert isinstance(pos, TargetedOpenPosition) and pos.stop_level == US_STOP and pos.target_level == US_TARGET
    ctx = json.loads(journal.get_meta(f"entry_ctx:{US_CID}"))
    assert ctx["target_distance"] == "15" and ctx["stop_distance"] == "10" and ctx["epic"] == "US500"
    assert "currency" not in ctx  # pasmo w walucie rachunku: bez pól walutowych
    assert journal.get_meta("entry_last:US500") == US_CID and kill.level() is KillLevel.NONE


async def test_target_lane_never_opens_a_position_without_a_target(ut, fake, journal, notifier, us_quote, kill):
    signal = EntrySignal(Side.BUY, US_STOP_D, D("0.01"), us_quote)  # sygnał bez celu
    assert await ut.enter(signal, US_RULES, DAY) is None
    assert fake.calls == [] and fake.positions == {}  # nic nie poszło do brokera, nawet odczyt konta
    [refused] = events(journal, "ENTRY_REFUSED")
    assert refused["reason"] == "TARGET_MISSING" and refused["epic"] == "US500"
    assert [level for level, _ in alerts(notifier, "TARGET_MISSING")] == ["ERROR"]
    assert journal.count_orders(DAY) == 0 and journal.get_meta(f"entry_ctx:{US_CID}") is None
    assert kill.level() is KillLevel.NONE


async def test_a_lane_without_a_target_refuses_a_plan_with_one(de, fake, journal, notifier, quote, rules):
    plan = BarEntryPlan(Side.BUY, D("0.007"), D("30"), D("45"), quote)
    assert await de.enter(plan, rules, DAY) is None
    assert fake.calls == [] and journal.count_orders(DAY) == 0
    assert events(journal, "ENTRY_REFUSED")[0]["reason"] == "TARGET_NOT_SUPPORTED"
    assert alerts(notifier, "TARGET_NOT_SUPPORTED")


async def test_target_lane_in_dry_run_records_the_targeted_request(lane_build, us_gate, calendar, plan, fake, journal):
    ex = lane_build(LaneSpec("US500", calendar, D("0.25"), D("1.00"), uses_target=True), us_gate, live=False)
    assert await ex.enter(plan, US_RULES, DAY) is None
    assert fake.calls == ["account"]
    [decision] = payloads(journal, "decision")
    assert decision["request"]["target_distance"] == "15" and decision["request"]["client_id"] == US_CID


async def test_target_lane_protects_with_stop_and_target_together(ut, fake, journal, plan, kill, monkeypatch, notifier):
    strip_stop_on_confirm(fake, monkeypatch)
    rec = await ut.enter(plan, US_RULES, DAY)
    assert rec.state is O.CONFIRMED
    assert fake.calls == ["account", "open_position", "confirm", "set_protection"]  # nigdy sam set_stop
    pos = fake.positions["DEAL-1"]
    assert pos.stop_level == US_STOP and pos.target_level == US_TARGET
    [event] = events(journal, "STOP_ATTACHED")
    assert D(event["stop_level"]) == US_STOP and D(event["target_level"]) == US_TARGET
    [raw] = payloads(journal, "broker.set_protection")
    assert D(raw["stop_level"]) == US_STOP and D(raw["target_level"]) == US_TARGET
    assert kill.level() is KillLevel.NONE and journal.current_position("US500").state is P.OPEN


async def test_protect_unprotected_on_a_target_lane_keeps_the_target_from_the_entry(ut, fake, journal, plan, kill):
    await ut.enter(plan, US_RULES, DAY)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    fake.calls.clear()
    assert await ut.protect_unprotected(fake.positions["DEAL-1"]) is True
    assert fake.calls == ["set_protection"]
    assert fake.positions["DEAL-1"].stop_level == US_STOP and fake.positions["DEAL-1"].target_level == US_TARGET


async def test_protect_unprotected_keeps_the_target_the_broker_reports(ut, fake, journal, kill):
    pos = TargetedOpenPosition("DEAL-T", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY, target_level=US_TARGET)
    fake.positions["DEAL-T"] = pos
    assert await ut.protect_unprotected(pos) is True  # bez kontekstu: odległość z limitu straty, cel brokera zostaje
    assert fake.calls == ["instrument_rules", "set_protection"]
    assert fake.positions["DEAL-T"].target_level == US_TARGET and fake.positions["DEAL-T"].stop_level is not None


async def test_protection_without_a_known_target_still_sets_the_stop_and_alarms(ut, fake, journal, kill, notifier):
    pos = OpenPosition("DEAL-T", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    fake.positions["DEAL-T"] = pos
    assert await ut.protect_unprotected(pos) is True  # ochrona przede wszystkim
    assert fake.calls == ["instrument_rules", "set_protection"] and fake.positions["DEAL-T"].stop_level is not None
    assert [level for level, _ in alerts(notifier, "TARGET_UNAVAILABLE")] == ["WARN"]
    assert kill.level() is KillLevel.NONE


async def test_failed_protection_of_a_target_lane_goes_hard_and_flattens(ut, fake, journal, plan, kill, clock):
    await ut.enter(plan, US_RULES, DAY)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    for _ in range(3):
        fake.fail_next("set_protection", BrokerError("HTTP 500"))
    fake.calls.clear()
    assert await ut.protect_unprotected(fake.positions["DEAL-1"]) is False
    assert fake.calls[:3] == ["set_protection"] * 3 and "set_stop" not in fake.calls
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"
    assert fake.calls.count("close_position") == 1 and fake.positions == {}


async def test_a_rejected_target_never_blocks_the_stop(ut, fake, journal, plan, kill, clock, notifier):
    await ut.enter(plan, US_RULES, DAY)
    # cena podeszła pod cel bliżej niż minimum brokera: cel zostanie odrzucony (MIN_TARGET_DISTANCE), stop nie
    fake.set_quote(Quote("US500", US_TARGET - D("0.2"), US_TARGET + D("0.4"), clock.now_utc()))
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    fake.calls.clear()
    assert await ut.protect_unprotected(fake.positions["DEAL-1"]) is True
    assert fake.calls == ["set_protection", "set_protection"]  # z celem (odmowa), potem sam stop
    assert fake.positions["DEAL-1"].stop_level == US_STOP and fake.positions["DEAL-1"].target_level is None
    assert kill.level() is KillLevel.NONE
    [(level, message)] = alerts(notifier, "TARGET_REJECTED")
    assert level == "WARN" and "MIN_TARGET_DISTANCE" in message and "C5d" in message
    raws = payloads(journal, "broker.set_protection")
    assert raws[0]["rejection"] == "MIN_TARGET_DISTANCE" and D(raws[0]["target_level"]) == US_TARGET
    assert raws[1]["target_level"] is None and raws[1]["attempt"] == 2
    [attached] = events(journal, "STOP_ATTACHED")
    assert attached["target_level"] is None and attached["attempt"] == 2


async def test_ensure_target_sets_stop_and_target_together_for_an_untargeted_position(ut, fake, journal, plan, notifier):
    await ut.enter(plan, US_RULES, DAY)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    fake.calls.clear()
    assert await ut.ensure_target(fake.positions["DEAL-1"]) is True
    assert fake.calls == ["set_protection"]
    assert fake.positions["DEAL-1"].target_level == US_TARGET and fake.positions["DEAL-1"].stop_level == US_STOP
    assert events(journal, "TARGET_ATTACHED")[0]["deal_id"] == "DEAL-1"
    assert [level for level, _ in alerts(notifier, "TARGET_ATTACHED")] == ["INFO"]


async def test_ensure_target_does_nothing_when_not_needed_or_not_possible(ut, de, fake, journal, plan, notifier):
    await ut.enter(plan, US_RULES, DAY)
    fake.calls.clear()
    assert await ut.ensure_target(fake.positions["DEAL-1"]) is True  # cel już jest
    stopless = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    assert await ut.ensure_target(stopless) is False  # najpierw stop (zabezpieczenie)
    plain = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    assert await de.ensure_target(plain) is False  # pasmo bez celu, obcy instrument
    foreign = OpenPosition("DEAL-9", "US500", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    assert await ut.ensure_target(foreign) is False  # bez kontekstu wejścia celu nie zgadujemy
    assert fake.calls == [] and [level for level, _ in alerts(notifier, "TARGET_UNAVAILABLE DEAL-9")] == ["WARN"]


async def test_ensure_target_failure_is_an_alarm_never_a_kill(ut, fake, journal, plan, notifier, kill):
    await ut.enter(plan, US_RULES, DAY)
    plain = OpenPosition("DEAL-1", "US500", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    fake.positions["DEAL-1"] = plain
    fake.fail_next("set_protection", BrokerError("HTTP 500"))
    fake.fail_next("set_protection", BrokerError("HTTP 500"))
    assert await ut.ensure_target(plain) is False
    assert await ut.ensure_target(plain) is False
    assert fake.positions["DEAL-1"].stop_level == US_STOP  # stop nietknięty
    assert len(alerts(notifier, "TARGET_ATTACH_FAILED")) == 1  # raz na deal
    assert len(events(journal, "TARGET_ATTACH_FAILED")) == 2
    assert kill.level() is KillLevel.NONE


# --- waluta obca: przeliczenia przez FxView (projekt §7.3) -------------------------------------------------


async def test_foreign_entry_is_judged_in_the_account_currency_and_keeps_both_values(us, fx, fake, journal, plan):
    rec = await us.enter(plan, US_RULES, DAY)
    assert rec.state is O.CONFIRMED and fake.calls == ["account", "open_position", "confirm"]
    assert (D("1"), "USD") in fx.asked  # wartość punktu przeliczona przed bramką
    ctx = json.loads(journal.get_meta(f"entry_ctx:{US_CID}"))
    assert ctx["value_per_point"] == "1" and ctx["currency"] == "USD"  # księga w walucie instrumentu
    assert ctx["value_per_point_account"] == "0.95"


async def test_foreign_entry_over_the_loss_limit_only_after_conversion_is_blocked(us, fx, fake, journal, us_quote):
    fx.factor = D("2")  # 1 USD = 2 EUR (kurs niekorzystny): 0,60 USD ryzyka to 1,20 EUR > 1,00 EUR
    plan = BarEntryPlan(Side.BUY, D("0.01"), D("60"), US_TARGET_D, us_quote)  # nominał 151,67 EUR < 200 EUR
    assert await us.enter(plan, US_RULES, DAY) is None
    assert fake.calls == ["account"]
    [block] = payloads(journal, "gate_block")
    assert block["reasons"] == ["LOSS_PER_TRADE"] and block["candidate"]["value_per_point"] == "2"


@pytest.mark.parametrize("par_upper", [True, False])
async def test_foreign_entry_without_a_valid_rate_is_blocked_before_any_broker_call(
    us, fx, fake, journal, plan, notifier, kill, par_upper
):
    fx.factor, fx.par_upper = None, par_upper  # parytet nigdy nie wystarcza do wejścia
    assert await us.enter(plan, US_RULES, DAY) is None
    assert fake.calls == [] and journal.count_orders(DAY) == 0
    [block] = payloads(journal, "gate_block")
    assert block["reasons"] == ["FX_UNAVAILABLE"] and block["currency"] == "USD" and block["epic"] == "US500"
    assert alerts(notifier, "FX_UNAVAILABLE") and kill.level() is KillLevel.NONE


@pytest.mark.parametrize("bad", [D("-1"), D("NaN"), 1.0, "0.95", RuntimeError("feed down")], ids=repr)
async def test_a_broken_fx_view_counts_as_no_rate(us, fx, fake, journal, plan, monkeypatch, bad):
    def broken(amount, currency):
        if isinstance(bad, Exception):
            raise bad
        return bad

    monkeypatch.setattr(fx, "to_account", broken)
    assert await us.enter(plan, US_RULES, DAY) is None
    assert payloads(journal, "gate_block")[0]["reasons"] == ["FX_UNAVAILABLE"] and fake.calls == []


async def test_size_guard_in_usd_closes_when_the_eur_risk_is_over_the_limit(us, fx, fake, journal, plan, notifier):
    fx.factor = D("1.2")
    fake.fill_size_factor = D("9")  # broker wypełnił 0,09 zamiast 0,01: 0,90 USD ryzyka = 1,08 EUR > 1,00 EUR
    rec = await us.enter(plan, US_RULES, DAY)
    assert rec.state is O.CONFIRMED
    [risk] = events(journal, "SIZE_MISMATCH_RISK")
    assert D(risk["risk"]) == D("1.080") and D(risk["risk_instrument"]) == D("0.90") and risk["currency"] == "USD"
    assert risk["fx"] == "RATE" and D(risk["limit"]) == D("1.00")
    assert fake.positions == {} and fake.calls.count("close_position") == 1
    assert journal.get_meta("fill_guarded:DEAL-1") == "SIZE_MISMATCH_RISK"


async def test_size_guard_in_usd_passes_when_the_eur_risk_is_within_the_limit(us, fx, fake, journal, plan):
    fake.fill_size_factor = D("9")  # 0,90 USD × 0,95 = 0,855 EUR ≤ 1,00 EUR (bez przeliczenia też przechodzi)
    await us.enter(plan, US_RULES, DAY)
    assert events(journal, "SIZE_MISMATCH_RISK") == [] and "DEAL-1" in fake.positions
    assert journal.get_meta("fill_guarded:DEAL-1") == "PASS"


@pytest.mark.parametrize(("par_upper", "closed"), [(True, False), (False, True)])
async def test_size_guard_without_a_rate_uses_par_only_as_an_upper_bound_else_closes(
    us, fx, fake, journal, plan, par_upper, closed
):
    fake.fill_size_factor = D("9")  # 0,90 USD: w parytecie poniżej 1,00 EUR
    fx.par_upper = par_upper
    fake.on_call("confirm", lambda: setattr(fx, "factor", None))  # kurs znika po wysłaniu wejścia
    await us.enter(plan, US_RULES, DAY)
    assert ("DEAL-1" not in fake.positions) is closed
    found = events(journal, "SIZE_MISMATCH_RISK")
    if closed:
        assert found[0]["fx"] == "UNAVAILABLE" and found[0]["risk"] is None
    else:
        assert found == [] and journal.get_meta("fill_guarded:DEAL-1") == "PASS"


async def test_adopted_usd_fill_without_context_is_guarded_in_eur(us, fx, fake, journal):
    fx.factor = D("1.2")
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.09"), US_FILL, US_STOP)  # 0,90 USD = 1,08 EUR
    assert await us.guard_adopted_fill() is True
    assert fake.calls[0] == "instrument_rules" and "DEAL-B" not in fake.positions
    assert D(events(journal, "SIZE_MISMATCH_RISK")[0]["risk"]) == D("1.080")


@pytest.mark.parametrize(
    ("factor", "par_upper", "expected"),
    [
        (D("1.25"), True, US_FILL - D("80.0")),  # 1,00 EUR / (0,01 × 1,25 EUR/pkt) = 80 pkt
        (None, True, US_FILL - D("100.0")),  # bez kursu, parytet górnym ograniczeniem: 100 pkt (ciaśniej niż prawda)
    ],
)
async def test_protection_distance_from_the_loss_limit_is_converted(us, fx, fake, kill, factor, par_upper, expected):
    fx.factor, fx.par_upper = factor, par_upper
    pos = OpenPosition("DEAL-B", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    fake.positions["DEAL-B"] = pos
    assert await us.protect_unprotected(pos) is True
    assert fake.positions["DEAL-B"].stop_level == expected and kill.level() is KillLevel.NONE


async def test_protection_distance_without_a_rate_or_a_par_bound_goes_hard(us, fx, fake, journal, kill):
    fx.factor, fx.par_upper = None, False
    pos = OpenPosition("DEAL-B", "US500", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY)
    fake.positions["DEAL-B"] = pos
    journal.upsert_position(PositionRecord("US500", P.OPEN, "DEAL-B", Side.BUY, D("0.01"), US_FILL, None, T_ENTRY))
    assert await us.protect_unprotected(pos) is False
    assert kill.level() is KillLevel.HARD and "set_protection" not in fake.calls and fake.positions == {}


# --- waluta obca: dwufazowe księgowanie (projekt §7.4) ------------------------------------------------------


async def closed_by_target_racing_the_time_limit(us, fake, journal, plan):
    await us.enter(plan, US_RULES, DAY)
    fake.on_call("close_position", lambda: fake.trigger_target("DEAL-1"), once=True)
    closed = await us.flatten(journal.current_position("US500"), "TIME_LIMIT")
    assert closed is not None and closed.reason == "TARGET"
    return closed


async def test_foreign_close_is_booked_pending_without_touching_the_eur_counter(us, fake, journal, plan, kill):
    await closed_by_target_racing_the_time_limit(us, fake, journal, plan)
    [row] = journal.trades()
    # w USD: mid wejścia 7583.3, mid wyjścia 7598.6 + 0.3 = 7598.9; brutto 15.6 × 0.01 = 0.156 → 0.16;
    # spread 0.006 → 0.01; poślizg wejścia 0.5 × 0.01 = 0.005 → 0.01 (bez dodatku stopu); netto 0.14 USD
    assert row.exit_reason == "TARGET" and row.net_pnl == D("0.14") and row.slippage_penalty == D("0.01")
    assert journal.realized_pnl(DAY) == D("0") and journal.realized_pnl_total() == D("0")  # USD nie trafia do EUR
    assert journal.pnl_pending_deals() == [("DEAL-1", DAY)]
    [booked] = events(journal, "TRADE_BOOKED")
    assert booked["pnl_pending"] is True and booked["currency"] == "USD" and booked["exit_reason"] == "TARGET"
    assert journal.current_position("US500") == PositionRecord("US500", P.FLAT)
    record = PositionRecord("US500", P.EXITING, "DEAL-1", Side.BUY, D("0.01"), US_FILL, US_STOP, T_ENTRY)
    await us.book(record, (await fake.closed_since(T_ENTRY))[0])  # ponowne księgowanie: bez zmian
    assert len(journal.trades()) == 1 and journal.realized_pnl(DAY) == D("0")
    assert journal.pnl_pending_deals() == [("DEAL-1", DAY)] and kill.level() is KillLevel.NONE


async def test_settling_a_foreign_trade_adds_the_eur_amount_exactly_once(us, fake, journal, plan):
    await closed_by_target_racing_the_time_limit(us, fake, journal, plan)
    assert us.settle_foreign_pnl("DEAL-1", D("0.12"), source="HISTORY_TRANSACTIONS", rate=D("1.1667")) is True
    assert journal.realized_pnl(DAY) == D("0.12") and journal.pnl_pending_deals() == []
    fx = journal.trade_fx("DEAL-1")
    assert fx.net_eur == D("0.12") and fx.source == "HISTORY_TRANSACTIONS" and fx.rate == D("1.1667")
    [settled] = events(journal, "TRADE_SETTLED")
    assert settled["deal_id"] == "DEAL-1" and D(settled["net_eur"]) == D("0.12") and settled["exit_day"] == "2026-11-02"
    assert us.settle_foreign_pnl("DEAL-1", D("0.12"), source="HISTORY_TRANSACTIONS") is False  # drugi raz: nic
    assert journal.realized_pnl(DAY) == D("0.12") and len(events(journal, "TRADE_SETTLED")) == 1


async def test_settling_a_foreign_loss_enforces_the_loss_limits(us, fake, journal, plan, kill, notifier):
    await closed_by_target_racing_the_time_limit(us, fake, journal, plan)
    assert kill.level() is KillLevel.NONE
    assert us.settle_foreign_pnl("DEAL-1", D("-1.60"), source="ESTIMATED_FX", rate=D("1.08")) is True
    assert kill.level() is KillLevel.SOFT and kill.reason() == "DAILY_LOSS"  # −1,60 ≤ −1,50 EUR
    assert events(journal, "DAILY_LOSS")[0]["exit_day"] == "2026-11-02"
    assert alerts(notifier, "DAILY_LOSS")


async def test_a_foreign_lane_refuses_its_next_entry_while_a_trade_awaits_settlement(us, fake, journal, plan, us_quote):
    await closed_by_target_racing_the_time_limit(us, fake, journal, plan)
    fake.calls.clear()
    plan2 = BarEntryPlan(Side.BUY, D("0.01"), US_STOP_D, US_TARGET_D, us_quote)
    assert await us.enter(plan2, US_RULES, DAY) is None
    assert fake.calls == []  # licznik EUR niepełny: bez wejścia, nic do brokera
    assert payloads(journal, "gate_block")[-1]["reasons"] == ["PNL_PENDING"]


def test_settlement_is_only_for_a_foreign_lane(de, ut):
    for ex in (de, ut):
        with pytest.raises(ValueError, match="foreign"):
            ex.settle_foreign_pnl("DEAL-1", D("1"), source="HISTORY_TRANSACTIONS")


async def test_domestic_lanes_still_book_straight_into_the_eur_counter(ut, fake, journal):
    hold_targeted(fake, journal)
    fake.trigger_target("DEAL-T")
    await ut.flatten(journal.current_position("US500"), "FORCED")
    assert journal.pnl_pending_deals() == [] and journal.realized_pnl(DAY) == journal.trades()[0].net_pnl != D("0")


# --- awaryjne spłaszczenie wielu pasm: próby przeplatane (projekt §5.4, załącznik A15) ------------------------


def close_log(fake, clock, monkeypatch, failing=()):
    """Zapis (deal, sekundy od startu) każdej próby zamknięcia; deale z `failing` zawsze dostają błąd brokera."""
    log = []
    real = fake.close_position
    start = clock.now_utc()

    async def close_position(deal_id):
        log.append((deal_id, (clock.now_utc() - start).total_seconds()))
        if deal_id in failing:
            fake.calls.append("close_position")
            raise BrokerError("HTTP 500")
        return await real(deal_id)

    monkeypatch.setattr(fake, "close_position", close_position)
    return log


async def test_emergency_flatten_of_two_lanes_interleaves_the_attempts(de, us, fake, journal, kill, clock, monkeypatch):
    hold(fake, journal, "DE40", "DEAL-A", Side.BUY, D("0.007"), D("25782.3"), D("25752.3"))
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.01"), US_FILL, US_STOP)
    kill.hard("TEST")
    log = close_log(fake, clock, monkeypatch, failing={"DEAL-A"})  # DE40 nie da się zamknąć
    await emergency_flatten_lanes([de, us])
    assert log[:2] == [("DEAL-A", 0.0), ("DEAL-B", 0.0)]  # US500 dostaje próbę w pierwszej rundzie, nie po 30 s
    assert [deal for deal, _ in log] == ["DEAL-A", "DEAL-B"] + ["DEAL-A"] * 4  # 5 prób DE40, jedno zamknięcie US500
    assert "DEAL-B" not in fake.positions and "DEAL-A" in fake.positions
    assert journal.current_position("US500") == PositionRecord("US500", P.FLAT)
    assert events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")[0]["deal_id"] == "DEAL-A"
    assert kill.level() is KillLevel.HARD


async def test_emergency_flatten_of_two_lanes_with_an_unreadable_journal_interleaved(
    de, us, fake, journal, kill, monkeypatch
):
    await two_held_positions(de, us, fake, journal, kill)
    unreadable_positions(journal, monkeypatch)
    await emergency_flatten_lanes([de, us])
    assert fake.positions == {}
    assert fake.calls == ["list_positions", "close_position", "list_positions", "close_position"]


async def test_emergency_flatten_lanes_refuses_two_executors_of_one_instrument(de, lane_build, de_lane, gate):
    with pytest.raises(ValueError, match="DE40"):
        await emergency_flatten_lanes([de, lane_build(de_lane, gate)])


async def test_a_failing_lane_does_not_stop_the_emergency_flatten_of_the_other(
    de, us, fake, journal, kill, monkeypatch
):
    hold(fake, journal, "DE40", "DEAL-A", Side.BUY, D("0.007"), D("25782.3"), D("25752.3"))
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.01"), US_FILL, US_STOP)
    kill.hard("TEST")
    real = journal.current_position

    def broken_for_de40(epic):
        if epic == "DE40":
            raise RuntimeError("bug")
        return real(epic)

    monkeypatch.setattr(journal, "current_position", broken_for_de40)
    with pytest.raises(RuntimeError, match="bug"):
        await emergency_flatten_lanes([de, us])
    assert "DEAL-B" not in fake.positions  # US500 zamknięte mimo błędu pasma DE40; błąd i tak wychodzi dalej


# --- wyjścia nigdy nie czekają; straż poślizgu z konfiguracji pasma ------------------------------------------


async def test_exits_of_a_foreign_lane_never_wait_for_a_rate_or_a_settlement(us, fx, fake, journal, plan, kill):
    await closed_by_target_racing_the_time_limit(us, fake, journal, plan)  # transakcja czeka na rozliczenie
    hold(fake, journal, "US500", "DEAL-B", Side.BUY, D("0.01"), US_FILL, US_STOP)
    fx.factor, fx.par_upper = None, False  # ani kursu, ani parytetu
    kill.hard("TEST")
    closed = await us.flatten(journal.current_position("US500"), "TIME_LIMIT")
    assert closed is not None and "DEAL-B" not in fake.positions
    assert journal.pnl_pending_deals() == [("DEAL-1", DAY), ("DEAL-B", DAY)]


async def test_slippage_guard_uses_the_fraction_of_the_lane(lane_build, us_gate, calendar, fake, journal, us_quote):
    lane = LaneSpec("US500", calendar, D("0.10"), D("1.00"), uses_target=True)  # 10% z 10 pkt = 1 pkt
    ex = lane_build(lane, us_gate)
    fake.fill_slippage = D("1.5")  # gorzej o 1,5 pkt: ponad 1 pkt pasma (przy 0,25 z DE40 byłoby w normie)
    await ex.enter(BarEntryPlan(Side.BUY, D("0.01"), US_STOP_D, US_TARGET_D, us_quote), US_RULES, DAY)
    [slip] = events(journal, "SLIPPAGE_GUARD")
    assert D(slip["limit"]) == D("1.00") and D(slip["deviation"]) == D("1.5")
    assert fake.positions == {}


async def test_de40_protection_records_keep_exactly_the_v1_fields(de, fake, journal):
    """Pasmo bez celu: zapisy zabezpieczenia bez żadnych pól celu (bajty dziennika DE40 v1)."""
    pos = OpenPosition("DEAL-X", "DE40", Side.BUY, D("0.007"), D("25782.3"), None, T_ENTRY)
    fake.positions["DEAL-X"] = pos
    assert await de.protect_unprotected(pos) is True
    assert fake.calls == ["instrument_rules", "set_stop"]
    [raw] = payloads(journal, "broker.set_stop")
    assert set(raw) == {"deal_id", "stop_level", "attempt", "response"}
    [event] = events(journal, "STOP_ATTACHED")
    assert set(event) == {"event", "deal_id", "stop_level", "attempt", "verified_by"}
    assert journal.events("broker.set_protection") == []
