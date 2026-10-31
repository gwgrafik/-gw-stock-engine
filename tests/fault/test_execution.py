"""Testy awarii wykonawcy (spec §5 pkt 7, §8, §9, §10): wejście przez bramkę, straże po potwierdzeniu, zabezpieczenie
stopem, spłaszczenie, awaryjne spłaszczenie i księgowanie.

Niezmienniki pod lupą (prawdziwe pieniądze):
- zamknięta transakcja jest zaksięgowana dokładnie raz (także później przez uzgadnianie), nigdy FLAT bez księgowania;
- po timeoucie lub odmowie zamknięcia najpierw lista pozycji, nigdy druga wysyłka na ślepo;
- pozycja bez stopu dostaje stop albo kill switch twardy i awaryjne spłaszczenie;
- wyjść nie blokuje ani bramka, ani kill switch;
- awaria dziennika przed wysyłką: kill switch miękki i żadnego zlecenia; tryb dry_run nigdy nie składa zlecenia.

Prawdziwe `Journal`, `OrderManager`, `RiskGate`, `KillSwitch`, `CostModel` i `Reconciler` na `FakeBroker`/`FakeClock`.
Zegar startowy w tym pliku: 09:06 w Berlinie (w oknie wejścia). Aktywny rachunek: ACC-1 z saldem 40 EUR.
"""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import gwstock.execution as execution
from gwstock.broker.fake import FakeBroker
from gwstock.clock import FakeClock
from gwstock.costs import CostModel
from gwstock.execution import Executor
from gwstock.journal import JournalReadError, JournalWriteError, OrderRecord, PositionRecord, TradeRecord
from gwstock.orders.manager import EntryBlocked
from gwstock.reconcile import Reconciler
from gwstock.risk import KILL, OUTSIDE_WINDOW, STALE_DATA, RiskGate
from gwstock.strategy import EntrySignal
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    ClosedDeal,
    KillLevel,
    OpenPosition,
    OrderState,
    PositionState,
    Quote,
    SessionExpired,
    Side,
)
from tests.fault.test_lost_trade_regression import accept_then_stop_before_first_check

D = Decimal
O = OrderState
P = PositionState

DAY = date(2026, 11, 2)  # poniedziałek, CET
T_ENTRY = datetime(2026, 11, 2, 8, 6, tzinfo=timezone.utc)  # 09:06 w Berlinie: okno wejścia 09:05-09:10
T_FLATTEN = datetime(2026, 11, 2, 16, 25, tzinfo=timezone.utc)  # 17:25 w Berlinie
OTHER_DAY = date(2026, 10, 30)
CID = "gws-20261102-DE40-1-entry"
BID, ASK = D("25780.3"), D("25782.3")  # fixture `quote`: mid 25781.3, spread 2.0
FILL = ASK  # BUY po ask
STOP = FILL - D("30")
SIZE = D("0.007")
TEST_GUARD_S = 2  # prawdziwy czas: błędna implementacja ma przerwać test, a nie zawiesić sesję pytest


@pytest.fixture
def clock() -> FakeClock:
    """Nadpisuje fixture z conftest: 09:06 w Berlinie, czyli w oknie wejścia."""
    return FakeClock(T_ENTRY)


@pytest.fixture(autouse=True)
def funded_account(fake):
    """Domyślnie aktywny jest ACC-0 z saldem 0 (bramka: LEVERAGE); testy pracują na ACC-1 z saldem 40 EUR."""
    fake.accounts = [a for a in fake.accounts if a.account_id == "ACC-1"]


DEFAULT = object()  # `build(live=DEFAULT)`: konstruktor bez argumentu `live`


@pytest.fixture
def build(mgr, fake, journal, gate, kill, cost_cfg, clock, strat_cfg, risk_cfg, calendar, notifier):
    """Fabryka wykonawcy z podmienionymi częściami (tryb, bramka, konfiguracja ryzyka, broker)."""

    def _build(*, live=True, gate=gate, risk=risk_cfg, broker=fake):
        args = (mgr, broker, journal, gate, kill, CostModel(cost_cfg), clock, strat_cfg, risk, calendar, notifier)
        return Executor(*args) if live is DEFAULT else Executor(*args, live=live)

    return _build


# --- pomocnicze -------------------------------------------------------------------------------------


def payloads(journal, source):
    return [json.loads(e.payload) for e in journal.events(source)]


def events(journal, code=None):
    found = payloads(journal, "event")
    return found if code is None else [e for e in found if e["event"] == code]


def codes(journal):
    return [e["event"] for e in events(journal)]


def alerts(notifier, text):
    return [(level, message) for level, message in notifier.alerts if text in message]


def write_failure(*_args, **_kwargs):
    raise JournalWriteError("disk full")


def fail_nth(monkeypatch, obj, name, n):
    """`obj.name` rzuca JournalWriteError przy n-tym wywołaniu (od 1), pozostałe działają."""
    real = getattr(obj, name)
    count = {"n": 0}

    def wrapper(*args, **kwargs):
        count["n"] += 1
        if count["n"] == n:
            raise JournalWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)


def switchable(monkeypatch, obj, name, exc):
    """`obj.name` rzuca `exc`, dopóki `state["on"]`; potem działa normalnie."""
    real = getattr(obj, name)
    state = {"on": True}

    def wrapper(*args, **kwargs):
        if state["on"]:
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)
    return state


def lag_history(fake, monkeypatch, empty_reads):
    """Historia zamknięć brokera spóźnia się: pierwsze `empty_reads` odczytów zwraca pustą listę."""
    real = fake.closed_since
    seen = {"n": 0}

    async def lagging(since):
        result = await real(since)
        seen["n"] += 1
        return [] if seen["n"] <= empty_reads else result

    monkeypatch.setattr(fake, "closed_since", lagging)


def at(clock, when):
    clock.advance((when - clock.now_utc()).total_seconds())


def times_of(fake, clock, method):
    seen = []
    fake.on_call(method, lambda: seen.append((clock.now_utc() - T_ENTRY).total_seconds()))
    return seen


async def entered(executor, signal, rules, fake):
    """Wejście CONFIRMED (DEAL-1 OPEN, stop STOP); czyści `fake.calls`."""
    rec = await executor.enter(signal, rules, DAY)
    assert rec is not None and rec.state is O.CONFIRMED and rec.deal_id == "DEAL-1"
    fake.calls.clear()
    return rec


def position(journal):
    return journal.current_position("DE40")


def trade(
    *, exit_fill, gross, spread, slippage, net, exit_ts, exit_reason, signal_ts=T_ENTRY, submit_ts=T_ENTRY,
    confirm_ts=T_ENTRY, latency_ms=0, size=SIZE, side=Side.BUY, decision_bid=BID, decision_ask=ASK, entry_fill=FILL,
):
    return TradeRecord(
        "DE40", side, size, decision_bid, decision_ask, entry_fill, exit_fill, D(gross), D(spread), D(slippage),
        D("0.00"), D("0.00"), D(net), signal_ts, submit_ts, confirm_ts, exit_ts, latency_ms, exit_reason,
    )


# Wyjście silnika o 17:25 po bid 25810.3 przy spreadzie 3.0: mid wyjścia 25811.8, mid wejścia 25781.3.
# gross = 30.5 × 0.007 = 0.2135 → 0.21; spread = max(zrealizowany (1.0 + 1.5) × 0.007, model (1.0 + 1.5) × 0.007)
# = 0.0175 → 0.02; poślizg wejścia 0.5 × 0.007 = 0.0035 → 0.00; netto 0.21 − 0.02 − 0.00 = 0.19.
EXIT_BID, EXIT_ASK = D("25810.3"), D("25813.3")
FORCED_GAIN = dict(exit_fill=EXIT_BID, gross="0.21", spread="0.02", slippage="0.00", net="0.19", exit_reason="FORCED")
# Stop po 25752.3 przy spreadzie 2.0: mid wyjścia 25753.3 (BUY wychodzi po bid, mid = wyjście + spread/2).
# gross = −28 × 0.007 = −0.196 → −0.20; spread 0.014 → 0.01; poślizg (0.5 + 1.0) × 0.007 = 0.0105 → 0.01; netto −0.22.
STOP_LOSS = dict(exit_fill=STOP, gross="-0.20", spread="0.01", slippage="0.01", net="-0.22", exit_reason="STOP")


def exit_quote(fake, clock):
    fake.set_quote(Quote("DE40", EXIT_BID, EXIT_ASK, clock.now_utc()))


# --- wejście: bramka, tryb, konto ---------------------------------------------------------------------


async def test_entry_goes_through_risk_gate_and_attaches_stop(executor, signal, rules, journal, fake, kill):
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED and rec.order_id == CID and rec.deal_id == "DEAL-1"
    assert fake.calls == ["account", "open_position", "confirm"]
    assert fake.positions["DEAL-1"].stop_level == ASK - D("30") == STOP
    assert position(journal) == PositionRecord("DE40", P.OPEN, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    assert journal.get_meta("entry_by_deal:DEAL-1") == CID
    assert json.loads(journal.get_meta(f"entry_ctx:{CID}")) == {
        "client_id": CID, "epic": "DE40", "side": "BUY", "size": "0.007", "stop_distance": "30",
        "value_per_point": "1", "decision_bid": "25780.3", "decision_ask": "25782.3",
        "decision_ts": T_ENTRY.isoformat(), "signal_ts": T_ENTRY.isoformat(), "submit_ts": T_ENTRY.isoformat(),
    }  # fmt: skip
    assert kill.level() is KillLevel.NONE
    assert journal.events("gate_block") == [] and codes(journal) == []


async def test_entry_is_blocked_when_kill_active(executor, signal, rules, journal, fake, kill):
    kill.soft("STOP_FILE")
    assert await executor.enter(signal, rules, DAY) is None
    assert fake.calls == ["account"]  # odczyt konta, ani jednego zlecenia
    [block] = payloads(journal, "gate_block")
    assert block["reasons"] == [KILL] and block["day"] == "2026-11-02"
    assert journal.count_orders(DAY) == 0 and journal.get_meta(f"entry_ctx:{CID}") is None
    assert position(journal) is None and kill.reason() == "STOP_FILE"


async def test_gate_block_returns_none_with_a_gate_block_event_and_no_broker_order(
    executor, signal, rules, journal, fake, clock
):
    clock.advance(10 * 60)  # 09:16: po terminie wejścia, notowanie decyzji ma 600 s
    assert await executor.enter(signal, rules, DAY) is None
    assert fake.calls == ["account"]
    [block] = payloads(journal, "gate_block")
    assert block["reasons"] == [OUTSIDE_WINDOW, STALE_DATA]
    candidate = block["candidate"]
    assert candidate["entry_price"] == "25781.3"  # mid notowania decyzji: ta sama baza co wielkość strategii
    assert candidate["quote_age_s"] == 600.0 and candidate["size"] == "0.007" and candidate["stop_distance"] == "30"
    assert journal.count_orders(DAY) == 0 and journal.events("decision") == []


async def test_leverage_is_judged_on_the_decision_mid_like_the_strategy_sizing(executor, rules, fake, clock, journal):
    # sygnał wzorcowy z T11: mid 25000.0 × 0.008 = 200.000 = 5 × 40 (po ask byłoby 200.0072 > 200 i blokada)
    q = Quote("DE40", D("24999.1"), D("25000.9"), clock.now_utc())
    fake.set_quote(q)
    rec = await executor.enter(EntrySignal(Side.BUY, D("30"), D("0.008"), q), rules, DAY)
    assert rec.state is O.CONFIRMED and journal.events("gate_block") == []


@pytest.mark.parametrize("live", [DEFAULT, False, 1, "true"], ids=["default", "false", "one", "string"])
async def test_dry_run_never_calls_open_position(build, signal, rules, journal, fake, live):
    ex = build(live=live)  # tylko dokładnie `True` uzbraja wysyłkę
    assert ex.live is False
    assert await ex.enter(signal, rules, DAY) is None
    assert fake.calls == ["account"]  # sam odczyt konta: zlecenia nie składa nawet menedżer
    [decision] = payloads(journal, "decision")
    assert decision["request"] == {
        "client_id": CID, "epic": "DE40", "side": "BUY", "size": "0.007", "stop_distance": "30",
    }  # fmt: skip
    assert decision["candidate"]["entry_price"] == "25781.3"
    assert journal.count_orders(DAY) == 0 and position(journal) is None
    assert journal.get_meta(f"entry_ctx:{CID}") is None


async def test_dry_run_still_applies_the_gate(build, signal, rules, journal, fake, kill):
    kill.soft("STOP_FILE")
    assert await build(live=False).enter(signal, rules, DAY) is None
    assert payloads(journal, "gate_block")[0]["reasons"] == [KILL] and journal.events("decision") == []


@pytest.mark.parametrize("exc", [BrokerError("HTTP 503"), SessionExpired("expired")], ids=lambda e: type(e).__name__)
async def test_account_unavailable_means_no_entry_and_no_kill(executor, signal, rules, journal, fake, kill, exc):
    fake.fail_next("account", exc)
    assert await executor.enter(signal, rules, DAY) is None
    assert fake.calls == ["account"]
    [event] = events(journal, "ACCOUNT_UNAVAILABLE")
    assert event["error"] == type(exc).__name__
    assert payloads(journal, "broker.account")[0]["error"] == type(exc).__name__  # surowa odpowiedź przed decyzją
    assert kill.level() is KillLevel.NONE and journal.count_orders(DAY) == 0
    assert journal.events("gate_block") == []


# --- wejście: kontekst i identyfikator --------------------------------------------------------------


async def test_entry_context_is_persisted_before_the_send(executor, signal, rules, journal, fake):
    seen = []
    fake.on_call(
        "open_position",
        lambda: seen.append((journal.get_meta(f"entry_ctx:{CID}"), journal.get_meta("entry_last:DE40"))),
    )
    await executor.enter(signal, rules, DAY)
    [(ctx, last)] = seen
    assert ctx is not None and json.loads(ctx)["client_id"] == CID and last == CID


async def test_client_id_sequence_is_per_trading_day(build, risk_cfg, strat_cfg, journal, calendar, clock, kill, rules,
                                                     signal):  # fmt: skip
    for order_id in ("gws-20261102-DE40-1-entry", "gws-20261030-DE40-1-entry", "gws-20261030-DE40-2-entry"):
        t = T_ENTRY - timedelta(hours=1)
        journal.insert_order(OrderRecord(order_id, "DE40", Side.BUY, SIZE, D("30"), O.REJECTED, None, None, t, t))
    gate = RiskGate(replace(risk_cfg, max_entries_per_day=2), strat_cfg, journal, calendar, clock, kill)
    rec = await build(gate=gate).enter(signal, rules, DAY)
    assert rec.order_id == "gws-20261102-DE40-2-entry"  # zlecenia innych dni się nie liczą


async def test_a_seq_gap_never_overwrites_an_existing_entry_context(
    build, risk_cfg, strat_cfg, journal, calendar, clock, kill, rules, signal, fake
):
    taken = "gws-20261102-DE40-2-entry"  # luka: dzień ma 1 zlecenie, ale numer 2 jest już zajęty
    t = T_ENTRY - timedelta(hours=1)
    journal.insert_order(OrderRecord(taken, "DE40", Side.BUY, SIZE, D("30"), O.REJECTED, None, None, t, t))
    journal.set_meta(f"entry_ctx:{taken}", "ORIGINAL")
    gate = RiskGate(replace(risk_cfg, max_entries_per_day=2), strat_cfg, journal, calendar, clock, kill)
    with pytest.raises(EntryBlocked):
        await build(gate=gate).enter(signal, rules, DAY)
    assert journal.get_meta(f"entry_ctx:{taken}") == "ORIGINAL"
    assert "open_position" not in fake.calls and kill.reason() == "ENTRY_EXCEPTION"


# --- wejście: awarie --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "n"), [("append_raw", 1), ("set_meta", 1), ("set_meta", 2)], ids=["raw_account", "context", "pointer"]
)
async def test_journal_write_failure_before_the_send_soft_kills_and_sends_nothing(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch, name, n
):
    fail_nth(monkeypatch, journal, name, n)
    assert await executor.enter(signal, rules, DAY) is None
    assert "open_position" not in fake.calls and journal.count_orders(DAY) == 0
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"
    assert alerts(notifier, "JOURNAL_WRITE_FAILED")


@pytest.mark.parametrize("when", ["before_send", "after_send"])
async def test_journal_write_error_from_the_manager_soft_kills_and_returns_none(
    executor, signal, rules, journal, fake, kill, monkeypatch, when
):
    if when == "before_send":
        monkeypatch.setattr(journal, "insert_order", write_failure)
    else:
        real = journal.update_order

        def update_order(order_id, **fields):
            if fields.get("state") is O.CONFIRMED:
                raise JournalWriteError("disk full")
            return real(order_id, **fields)

        monkeypatch.setattr(journal, "update_order", update_order)
    assert await executor.enter(signal, rules, DAY) is None
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"
    assert fake.calls.count("open_position") == (0 if when == "before_send" else 1)
    if when == "after_send":  # dziennik mówi SUBMITTING: uzgadnianie przyjmie pozycję, żadnej drugiej wysyłki
        assert journal.get_order(CID).state is O.SUBMITTING and "close_position" not in fake.calls


@pytest.mark.parametrize("scenario", ["lost_trade_blocked", "checks_incomplete"])
async def test_unresolved_entry_soft_kills_and_never_reenters(
    executor, signal, rules, journal, fake, kill, notifier, scenario
):
    if scenario == "lost_trade_blocked":
        accept_then_stop_before_first_check(fake)
        expected = ("UNMATCHED_CLOSED_DEAL", P.BLOCKED)
    else:
        fake.fail_next("open_position", BrokerTimeout(), applied=False)
        for _ in range(3):
            fake.fail_next("list_positions", BrokerError("HTTP 502"))
        expected = ("CHECKS_INCOMPLETE", P.ENTERING)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.UNKNOWN and rec.note.split()[0] == expected[0]
    assert position(journal).state is expected[1]
    assert kill.level() is KillLevel.SOFT and kill.reason() == "ENTRY_UNRESOLVED"
    [event] = events(journal, "ENTRY_UNRESOLVED")
    assert event["order_id"] == CID and event["state"] == "UNKNOWN" and event["position"] == expected[1].value
    assert alerts(notifier, "ENTRY_UNRESOLVED")
    assert fake.calls.count("open_position") == 1 and journal.trades() == []


@pytest.mark.parametrize(
    ("state", "stored"),
    [(O.CONFIRMED, "blocked"), (O.CONFIRMED, "other_deal"), (O.CONFIRMED, "missing"), (O.REJECTED, "blocked"),
     (O.NOT_PLACED, "blocked")],
)  # fmt: skip
async def test_an_order_without_our_open_position_still_soft_kills(
    executor, mgr, signal, rules, journal, fake, kill, monkeypatch, state, stored
):
    # kontrakt z T7: liczy się też stan pozycji (BLOCKED), nie tylko stan zlecenia
    async def submitted(req):
        if stored == "blocked":
            journal.upsert_position(PositionRecord("DE40", P.BLOCKED))
        elif stored == "other_deal":
            journal.upsert_position(PositionRecord("DE40", P.OPEN, "DEAL-2", Side.BUY, SIZE, FILL, STOP, T_ENTRY))
        deal_id = "DEAL-1" if state is O.CONFIRMED else None
        return OrderRecord(
            req.client_id, "DE40", req.side, req.size, req.stop_distance, state, "REF-1", deal_id, T_ENTRY, T_ENTRY
        )

    monkeypatch.setattr(mgr, "submit_entry", submitted)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is state
    assert kill.level() is KillLevel.SOFT and kill.reason() == "ENTRY_UNRESOLVED"
    assert fake.calls == ["account"] and journal.get_meta("entry_by_deal:DEAL-1") is None


@pytest.mark.parametrize(
    "exc", [RuntimeError("boom"), EntryBlocked("POSITION_NOT_FLAT", "DE40 OPEN")], ids=lambda e: type(e).__name__
)
async def test_any_other_exception_from_the_manager_soft_kills_and_is_reraised(
    executor, mgr, signal, rules, kill, notifier, monkeypatch, exc
):
    async def boom(_req):
        raise exc

    monkeypatch.setattr(mgr, "submit_entry", boom)
    with pytest.raises(type(exc)):
        await executor.enter(signal, rules, DAY)
    assert kill.level() is KillLevel.SOFT and kill.reason() == "ENTRY_EXCEPTION"
    assert alerts(notifier, "ENTRY_EXCEPTION")


async def test_cancellation_propagates_untouched(executor, signal, rules, journal, fake, kill, notifier, monkeypatch):
    started = asyncio.Event()

    async def hang(_req):
        fake.calls.append("open_position")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake, "open_position", hang)
    task = asyncio.create_task(executor.enter(signal, rules, DAY))
    await asyncio.wait_for(started.wait(), TEST_GUARD_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kill.level() is KillLevel.NONE and notifier.alerts == []
    assert journal.get_order(CID).state is O.SUBMITTING  # rozstrzygnie uzgadnianie po restarcie


# --- wejście: straże po potwierdzeniu --------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "slip", "closes"),
    [
        (Side.BUY, "10", True),  # spec: 10 pkt gorzej przy stopie 30 (> 7.5)
        (Side.BUY, "7.5", False),  # równo 25% stopu: zostaje
        (Side.BUY, "7.6", True),
        (Side.SELL, "7.5", False),
        (Side.SELL, "7.6", True),
    ],
)
async def test_slippage_guard_closes_position_when_fill_deviates_over_25pct_of_stop(
    executor, quote, rules, journal, fake, kill, side, slip, closes
):
    fake.fill_slippage = D(slip)
    seen = []
    fake.on_call("close_position", lambda: seen.append(codes(journal)))
    rec = await executor.enter(EntrySignal(side, D("30"), SIZE, quote), rules, DAY)
    assert rec.state is O.CONFIRMED
    if not closes:
        assert "close_position" not in fake.calls and position(journal).state is P.OPEN
        assert events(journal, "SLIPPAGE_GUARD") == []
        return
    assert fake.calls.count("close_position") == 1 and seen == [["SLIPPAGE_GUARD"]]  # zdarzenie przed zamknięciem
    [event] = events(journal, "SLIPPAGE_GUARD")
    assert D(event["deviation"]) == D(slip) and D(event["limit"]) == D("7.5")
    assert event["decision_bid"] == "25780.3" and event["decision_ask"] == "25782.3"
    assert position(journal) == PositionRecord("DE40", P.FLAT)
    [booked] = journal.trades()
    assert booked.exit_reason == "FORCED" and kill.level() is KillLevel.NONE


async def test_slippage_guard_books_the_closed_trade_with_its_costs(executor, signal, rules, journal, fake):
    # wypełnienie 25792.3, wyjście po bid 25780.3; mid wejścia i wyjścia 25781.3 → gross 0.00;
    # spread zrealizowany (11.0 + 1.0) × 0.007 = 0.084 → 0.08 (> model 0.014); netto −0.08
    fake.fill_slippage = D("10")
    await executor.enter(signal, rules, DAY)
    assert journal.trades() == [
        trade(entry_fill=D("25792.3"), exit_fill=BID, gross="0.00", spread="0.08", slippage="0.00", net="-0.08",
              exit_ts=T_ENTRY, exit_reason="FORCED")
    ]  # fmt: skip
    assert journal.realized_pnl(DAY) == D("-0.08")


@pytest.mark.parametrize(
    ("factor", "closes"), [("10", False), ("10.025", True), ("0.5", False)], ids=["at_limit", "over", "smaller"]
)
async def test_size_mismatch_risk_closes_a_fill_larger_than_the_risk_allows(
    executor, quote, rules, journal, fake, factor, closes
):
    # żądane 0.004 przy stopie 25 (ryzyko 0.10); broker potwierdza 0.004 × factor: 0.040 × 25 = 1.00 (równo limit)
    fake.fill_size_factor = D(factor)
    rec = await executor.enter(EntrySignal(Side.BUY, D("25"), D("0.004"), quote), rules, DAY)
    assert rec.state is O.CONFIRMED
    if not closes:
        assert "close_position" not in fake.calls and events(journal, "SIZE_MISMATCH_RISK") == []
        return
    [event] = events(journal, "SIZE_MISMATCH_RISK")
    assert D(event["size"]) == D("0.0401") and D(event["requested"]) == D("0.004")
    assert D(event["distance"]) == D("25") and D(event["risk"]) == D("1.0025") and D(event["limit"]) == D("1.00")
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT
    [booked] = journal.trades()
    assert booked.size == D("0.0401")


async def test_both_guards_are_checked_and_the_position_is_closed_once(executor, quote, rules, journal, fake):
    fake.fill_slippage, fake.fill_size_factor = D("10"), D("10.025")
    await executor.enter(EntrySignal(Side.BUY, D("25"), D("0.004"), quote), rules, DAY)
    assert codes(journal)[:2] == ["SLIPPAGE_GUARD", "SIZE_MISMATCH_RISK"]
    assert fake.calls.count("close_position") == 1 and len(journal.trades()) == 1


# --- zabezpieczenie stopem ---------------------------------------------------------------------------


def strip_stop_on_confirm(fake, monkeypatch):
    """Broker przyjął wejście BEZ stopu: potwierdzenie i lista pozycji nie mają stop_level."""
    real = fake.confirm

    async def confirm(ref):
        snapshot = await real(ref)
        if snapshot is None:
            return None
        fake.positions[snapshot.deal_id] = replace(fake.positions[snapshot.deal_id], stop_level=None)
        return replace(snapshot, stop_level=None)

    monkeypatch.setattr(fake, "confirm", confirm)


async def test_confirmed_entry_without_stop_gets_the_stop_from_the_entry(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    strip_stop_on_confirm(fake, monkeypatch)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED
    assert fake.calls == ["account", "open_position", "confirm", "set_stop"]
    assert fake.positions["DEAL-1"].stop_level == STOP  # wypełnienie − odległość stopu sygnału
    [event] = events(journal, "STOP_ATTACHED")
    assert event["deal_id"] == "DEAL-1" and D(event["stop_level"]) == STOP and event["attempt"] == 1
    assert kill.level() is KillLevel.NONE and position(journal).state is P.OPEN


async def test_unprotected_position_gets_stop_else_hard_kill(executor, signal, rules, journal, fake, kill, clock):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is True
    assert fake.positions["DEAL-1"].stop_level == STOP  # odległość z kontekstu wejścia (30 pkt)
    assert kill.level() is KillLevel.NONE

    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    for _ in range(3):
        fake.fail_next("set_stop", BrokerError("HTTP 500"))
    fake.calls.clear()
    start = (clock.now_utc() - T_ENTRY).total_seconds()
    tries = times_of(fake, clock, "set_stop")
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is False
    assert [t - start for t in tries] == [0, 3, 6]  # 3 próby co 3 s: decyzja HARD w 10 s (spec §9)
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"
    assert fake.calls[:3] == ["set_stop"] * 3 and fake.calls.count("close_position") == 1  # awaryjne spłaszczenie
    assert position(journal) == PositionRecord("DE40", P.FLAT) and len(journal.trades()) == 1
    assert events(journal, "PROTECTION_FAILED")[0]["deal_id"] == "DEAL-1"


async def test_protection_retries_then_succeeds_without_kill(executor, signal, rules, fake, kill, clock):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    fake.fail_next("set_stop", BrokerTimeout())
    fake.fail_next("set_stop", BrokerRejected("TEMPORARY"))
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is True
    assert fake.calls == ["set_stop"] * 3 and kill.level() is KillLevel.NONE
    assert fake.positions["DEAL-1"].stop_level == STOP


async def test_a_journal_failure_during_protection_still_ends_hard(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    fake.fail_next("set_stop", BrokerError("HTTP 500"))
    real = journal.append_raw

    def append_raw(source, payload, ts):
        if source == "broker.set_stop":
            raise JournalWriteError("disk full")
        return real(source, payload, ts)

    monkeypatch.setattr(journal, "append_raw", append_raw)
    with pytest.raises(JournalWriteError):
        await executor.protect_unprotected(fake.positions["DEAL-1"])
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"  # nigdy bez stopu i bez HARD


@pytest.mark.parametrize(
    ("side", "size", "stop_step", "expected"),
    [
        (Side.BUY, "0.010", "0", D("25681.3")),  # 1.00 / 0.010 = 100 pkt
        (Side.SELL, "0.010", "0", D("25881.3")),
        (Side.BUY, "0.007", "0", D("25638.5")),  # 142.857… w dół do kroku ceny 0.1 → 142.8 (ryzyko 0.9996 ≤ 1.00)
        (Side.BUY, "0.007", "0.5", D("25638.8")),  # krok stopu brokera 0.5 → 142.5
    ],
)
async def test_protection_without_an_entry_context_uses_the_max_loss_distance(
    build, clock, rules, quote, kill, side, size, stop_step, expected
):
    broker = FakeBroker(clock, replace(rules, stop_step=D(stop_step)), quote)
    pos = OpenPosition("DEAL-X", "DE40", side, D(size), D("25781.3"), None, T_ENTRY)
    broker.positions["DEAL-X"] = pos
    assert await build(broker=broker).protect_unprotected(pos) is True
    assert broker.positions["DEAL-X"].stop_level == expected
    assert broker.calls == ["instrument_rules", "set_stop"] and kill.level() is KillLevel.NONE


async def test_protection_without_any_distance_ends_hard(build, fake, kill, journal):
    fake.fail_next("instrument_rules", BrokerError("HTTP 500"))
    pos = OpenPosition("DEAL-X", "DE40", Side.BUY, SIZE, FILL, None, T_ENTRY)
    fake.positions["DEAL-X"] = pos
    assert await build().protect_unprotected(pos) is False
    assert "set_stop" not in fake.calls and kill.level() is KillLevel.HARD
    assert kill.reason() == "UNPROTECTED_POSITION"


# --- spłaszczenie ------------------------------------------------------------------------------------


async def test_flatten_closes_and_books_trade_with_costs(executor, signal, rules, journal, fake, clock, kill):
    fake.on_call("account", lambda: clock.advance(1), once=True)  # wysyłka 1 s po sygnale
    fake.on_call("confirm", lambda: clock.advance(0.25), once=True)  # potwierdzenie 0.25 s po wysyłce
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    exit_quote(fake, clock)
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed == ClosedDeal("DEAL-1", "DE40", EXIT_BID, "MANUAL", T_FLATTEN)
    assert fake.calls == ["close_position", "closed_since", "quote"]
    assert journal.trades() == [
        trade(**FORCED_GAIN, exit_ts=T_FLATTEN, submit_ts=T_ENTRY + timedelta(seconds=1),
              confirm_ts=T_ENTRY + timedelta(seconds=1.25), latency_ms=1250)
    ]  # fmt: skip
    assert journal.realized_pnl(DAY) == D("0.19") and journal.realized_pnl_total() == D("0.19")
    assert position(journal) == PositionRecord("DE40", P.FLAT)
    assert codes(journal) == ["TRADE_BOOKED", "FLATTENED"] and kill.level() is KillLevel.NONE
    [history] = payloads(journal, "broker.closed_since")  # od otwarcia (wypełnienie o T+1 s) minus zapas 1 min
    assert history["since"] == (T_ENTRY + timedelta(seconds=1) - timedelta(minutes=1)).isoformat()
    assert history["attempt"] == 1


async def test_flatten_moves_the_position_to_exiting_before_the_close(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    seen = []
    fake.on_call("close_position", lambda: seen.append(position(journal).state))
    await executor.flatten(position(journal), "FORCED")
    assert seen == [P.EXITING]


async def test_flatten_when_stop_already_hit_books_stop_not_second_close(executor, signal, rules, journal, fake, clock):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.on_call("close_position", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed == ClosedDeal("DEAL-1", "DE40", STOP, "STOP", T_FLATTEN)
    assert fake.calls == ["close_position", "list_positions", "closed_since", "quote"]  # jedno zamknięcie
    assert journal.trades() == [trade(**STOP_LOSS, exit_ts=T_FLATTEN)]
    assert position(journal).state is P.FLAT
    sources = [e.source for e in journal.events() if e.source.startswith("broker.")]  # surowe odpowiedzi po kolei
    assert sources[-4:] == ["broker.close_position", "broker.list_positions", "broker.closed_since", "broker.quote"]


async def test_flatten_during_market_closed_keeps_stop_and_retries(
    executor, signal, rules, journal, fake, clock, notifier
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.set_market_closed(True)
    assert await executor.flatten(position(journal), "FORCED") is None
    assert fake.calls == ["close_position"]  # bez listy, bez ponowienia
    assert position(journal).state is P.EXITING and fake.positions["DEAL-1"].stop_level == STOP
    assert events(journal, "FLATTEN_BLOCKED_MARKET_CLOSED")[0]["deal_id"] == "DEAL-1"
    assert alerts(notifier, "FLATTEN_BLOCKED_MARKET_CLOSED")

    fake.set_market_closed(False)
    clock.advance(10)
    fake.calls.clear()
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed.reason == "MANUAL" and position(journal).state is P.FLAT and len(journal.trades()) == 1
    assert fake.calls[:2] == ["list_positions", "close_position"]  # ponowienie: najpierw potwierdzenie, że pozycja jest


@pytest.mark.parametrize("applied", [True, False], ids=["applied", "not_applied"])
async def test_flatten_timeout_reconciles_instead_of_resending(
    executor, signal, rules, journal, fake, clock, notifier, applied
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.fail_next("close_position", BrokerTimeout(), applied=applied)
    closed = await executor.flatten(position(journal), "FORCED")
    if applied:
        assert closed == ClosedDeal("DEAL-1", "DE40", BID, "MANUAL", T_FLATTEN)
        assert fake.calls == ["close_position", "list_positions", "closed_since", "quote"]
        assert position(journal).state is P.FLAT and len(journal.trades()) == 1
        return
    assert closed is None and fake.calls == ["close_position", "list_positions"]  # pozycja jest: nie wysyłamy drugi raz
    assert position(journal).state is P.EXITING and journal.trades() == []
    [event] = events(journal, "FLATTEN_STILL_OPEN")
    assert event["error"] == "BrokerTimeout" and event["flatten_reason"] == "FORCED"
    assert alerts(notifier, "FLATTEN_STILL_OPEN")
    clock.advance(10)  # następny takt silnika: ponowienie po ponownym potwierdzeniu, że pozycja jest
    fake.calls.clear()
    assert (await executor.flatten(position(journal), "FORCED")).reason == "MANUAL"
    assert fake.calls[:2] == ["list_positions", "close_position"] and len(journal.trades()) == 1


async def test_flatten_hanging_close_is_verified_not_resent(
    build, risk_cfg, signal, rules, journal, fake, clock, monkeypatch
):
    executor = build(risk=replace(risk_cfg, submit_timeout_s=0.05))
    await entered(executor, signal, rules, fake)
    real_close = fake.close_position

    async def applied_then_silent(deal_id):
        await real_close(deal_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(fake, "close_position", applied_then_silent)
    closed = await asyncio.wait_for(executor.flatten(position(journal), "FORCED"), TEST_GUARD_S)
    assert closed.reason == "MANUAL" and fake.calls.count("close_position") == 1
    assert payloads(journal, "broker.close_position")[-1]["error"] == "TimeoutError"
    assert position(journal).state is P.FLAT and len(journal.trades()) == 1


async def test_flatten_rejection_with_the_position_still_listed_keeps_exiting(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    fake.fail_next("close_position", BrokerRejected("MARKET_RULES"))
    assert await executor.flatten(position(journal), "FORCED") is None
    assert fake.calls == ["close_position", "list_positions"] and position(journal).state is P.EXITING
    [raw] = [p for p in payloads(journal, "broker.close_position") if "error" in p]
    assert raw["error"] == "BrokerRejected" and raw["rejection"] == "MARKET_RULES"


async def test_flatten_that_cannot_verify_keeps_exiting_and_alarms(executor, signal, rules, journal, fake, notifier):
    await entered(executor, signal, rules, fake)
    fake.expire_session()  # zamknięcie i lista pozycji rzucają SessionExpired
    assert await executor.flatten(position(journal), "FORCED") is None
    assert fake.calls == ["close_position", "list_positions"] and position(journal).state is P.EXITING
    assert events(journal, "FLATTEN_UNVERIFIED")[0]["error"] == "SessionExpired"
    assert alerts(notifier, "FLATTEN_UNVERIFIED")[0][0] == "ERROR"


@pytest.mark.parametrize("broker_state", ["gone", "listed", "unreadable"])
async def test_a_retry_on_an_exiting_position_checks_the_broker_before_closing_again(
    executor, signal, rules, journal, fake, notifier, broker_state
):
    # np. restart po przerwaniu w trakcie zamknięcia: dziennik EXITING, nie wiemy, czy zamknięcie doszło
    await entered(executor, signal, rules, fake)
    journal.upsert_position(replace(position(journal), state=P.EXITING))
    if broker_state == "gone":
        await fake.close_position("DEAL-1")
        fake.calls.clear()
    elif broker_state == "unreadable":
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
    closed = await executor.flatten(position(journal), "FORCED")
    if broker_state == "unreadable":
        assert closed is None and fake.calls == ["list_positions"]  # bez potwierdzenia nie wysyłamy drugi raz
        assert position(journal).state is P.EXITING
        assert events(journal, "FLATTEN_UNVERIFIED")[0]["error"] == "BrokerError"
        assert alerts(notifier, "FLATTEN_UNVERIFIED")
        return
    expected = ["list_positions"] + (["close_position"] if broker_state == "listed" else [])
    assert fake.calls == expected + ["closed_since", "quote"]
    assert closed.reason == "MANUAL" and position(journal).state is P.FLAT and len(journal.trades()) == 1


@pytest.mark.parametrize("mode", ["empty", "error"])
async def test_flatten_history_lagging_books_on_the_third_attempt(
    executor, signal, rules, journal, fake, clock, monkeypatch, mode
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    if mode == "empty":
        lag_history(fake, monkeypatch, empty_reads=2)
    else:
        fake.fail_next("closed_since", BrokerError("HTTP 502"))
        fake.fail_next("closed_since", BrokerTimeout())
    reads = times_of(fake, clock, "closed_since")
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed == ClosedDeal("DEAL-1", "DE40", BID, "MANUAL", T_FLATTEN)
    flatten_s = (T_FLATTEN - T_ENTRY).total_seconds()
    assert [r - flatten_s for r in reads] == [2, 4, 6]  # 3 próby, 2 s przed każdą
    assert len(journal.trades()) == 1 and position(journal).state is P.FLAT


async def test_history_that_never_shows_leaves_exiting_and_the_real_reconciler_books_it_later(
    executor, mgr, signal, rules, journal, fake, clock, kill, risk_cfg, notifier, monkeypatch
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    exit_quote(fake, clock)
    lag_history(fake, monkeypatch, empty_reads=3)
    assert await executor.flatten(position(journal), "FORCED") is None
    assert position(journal).state is P.EXITING and journal.trades() == []  # nigdy FLAT bez księgowania
    assert fake.calls == ["close_position"] + ["closed_since"] * 3
    assert events(journal, "CLOSE_SENT_AWAIT_HISTORY")[0]["deal_id"] == "DEAL-1"
    assert alerts(notifier, "CLOSE_SENT_AWAIT_HISTORY")

    reconciler = Reconciler(journal, fake, clock, kill, risk_cfg, "DE40", booker=executor,
                            entry_gate=lambda: mgr.in_flight)  # fmt: skip
    clock.advance(risk_cfg.reconcile_interval_s)
    result = await reconciler.run_once()
    assert result.closed == [ClosedDeal("DEAL-1", "DE40", EXIT_BID, "MANUAL", T_FLATTEN)]
    assert position(journal) == PositionRecord("DE40", P.FLAT)
    assert journal.trades() == [trade(**FORCED_GAIN, exit_ts=T_FLATTEN)]
    assert journal.realized_pnl(DAY) == D("0.19")
    clock.advance(risk_cfg.reconcile_interval_s)
    assert (await reconciler.run_once()).closed == [] and len(journal.trades()) == 1


@pytest.mark.parametrize("journal_state", ["missing", "FLAT", "ENTERING", "BLOCKED", "other_deal"])
async def test_flatten_refuses_a_position_that_is_not_open_or_exiting(executor, journal, fake, notifier, journal_state):
    ours = PositionRecord("DE40", P.OPEN, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    stored = {
        "missing": None,
        "FLAT": PositionRecord("DE40", P.FLAT),
        "ENTERING": PositionRecord("DE40", P.ENTERING),
        "BLOCKED": replace(ours, state=P.BLOCKED),
        "other_deal": replace(ours, deal_id="DEAL-2"),
    }[journal_state]
    if stored is not None:
        journal.upsert_position(stored)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    assert await executor.flatten(ours, "FORCED") is None
    assert fake.calls == [] and position(journal) == stored
    [event] = events(journal, "FLATTEN_REFUSED")
    assert event["deal_id"] == "DEAL-1" and event["journal_state"] == ("MISSING" if stored is None else stored.state)
    assert alerts(notifier, "FLATTEN_REFUSED")


@pytest.mark.parametrize("level", ["soft", "hard"])
async def test_an_active_kill_never_blocks_flatten(executor, signal, rules, journal, fake, kill, level):
    await entered(executor, signal, rules, fake)
    getattr(kill, level)("HARD_STOP_FILE")
    closed = await executor.flatten(position(journal), "MANUAL")
    assert closed.reason == "MANUAL" and position(journal).state is P.FLAT and len(journal.trades()) == 1


async def test_flatten_accepts_the_brokers_view_of_the_position(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    closed = await executor.flatten(fake.positions["DEAL-1"], "MANUAL")  # OpenPosition: liczy się epic i deal_id
    assert closed.reason == "MANUAL" and position(journal).state is P.FLAT


@pytest.mark.parametrize("meanwhile", ["flat", "blocked"])
async def test_settle_never_overwrites_a_position_changed_meanwhile(
    executor, signal, rules, journal, fake, notifier, meanwhile
):
    await entered(executor, signal, rules, fake)
    changed = (
        PositionRecord("DE40", P.FLAT) if meanwhile == "flat"
        else PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    )  # fmt: skip
    fake.on_call("closed_since", lambda: journal.upsert_position(changed), once=True)
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed.reason == "MANUAL" and len(journal.trades()) == 1  # zaksięgowane
    assert position(journal) == changed  # BLOCKED zostaje decyzją uzgadniania lub właściciela
    assert bool(alerts(notifier, "left as is")) is (meanwhile == "blocked")


async def test_a_closure_of_the_same_deal_on_another_instrument_is_not_ours(executor, signal, rules, journal, fake,
                                                                            monkeypatch):  # fmt: skip
    await entered(executor, signal, rules, fake)

    async def closed_since(_since):
        fake.calls.append("closed_since")
        return [ClosedDeal("DEAL-1", "US500", D("6800"), "MANUAL", T_ENTRY)]

    monkeypatch.setattr(fake, "closed_since", closed_since)
    assert await executor.flatten(position(journal), "FORCED") is None
    assert position(journal).state is P.EXITING and journal.trades() == []


# --- księgowanie --------------------------------------------------------------------------------------


async def test_book_is_idempotent(executor, signal, rules, journal, fake, clock):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.trigger_stop("DEAL-1", STOP)
    [closed] = await fake.closed_since(T_ENTRY)
    pos = position(journal)
    await executor.book(pos, closed)
    await executor.book(pos, closed)  # np. uzgadnianie po restarcie między księgowaniem a FLAT
    assert journal.trades() == [trade(**STOP_LOSS, exit_ts=T_FLATTEN)]
    assert journal.realized_pnl(DAY) == D("-0.22")  # wynik doliczony raz
    assert len(events(journal, "TRADE_BOOKED")) == 1


@pytest.mark.parametrize(
    ("side", "gross", "net"), [(Side.BUY, "0.20", "0.19"), (Side.SELL, "-0.20", "-0.21")], ids=["buy", "sell"]
)
async def test_book_without_entry_context_books_a_cost_fallback_record(
    executor, journal, fake, notifier, side, gross, net
):
    # pozycja przyjęta lub obca, bez kontekstu: mid wejścia = poziom otwarcia, spready 0, decyzja = poziom otwarcia
    # gross = ±(25720.0 − 25700.0) × 0.010 = ±0.20; poślizg 0.5 × 0.010 = 0.005 → 0.01
    pos = PositionRecord("DE40", P.EXITING, "DEAL-9", side, D("0.010"), D("25700.0"), None, T_ENTRY)
    closed = ClosedDeal("DEAL-9", "DE40", D("25720.0"), "MANUAL", T_ENTRY + timedelta(hours=1))
    await executor.book(pos, closed)
    level = D("25700.0")
    assert journal.trades() == [
        TradeRecord("DE40", side, D("0.010"), level, level, level, D("25720.0"), D(gross), D("0.00"), D("0.01"),
                    D("0.00"), D("0.00"), D(net), T_ENTRY, T_ENTRY, T_ENTRY, closed.ts, 0, "FORCED")
    ]  # fmt: skip
    assert journal.realized_pnl(DAY) == D(net)
    [event] = events(journal, "COST_FALLBACK")
    assert event["deal_id"] == "DEAL-9" and event["reason"] == "NO_ENTRY_CONTEXT"
    assert fake.calls == ["instrument_rules"]  # wartość punktu od brokera, bo kontekstu brak
    assert alerts(notifier, "COST_FALLBACK")[0][0] == "WARN"


def crossed_quote():
    """Notowanie z bid > ask (błąd adaptera), zbudowane z pominięciem walidacji konstruktora."""
    q = object.__new__(Quote)
    for name, value in (("epic", "DE40"), ("bid", D("25790.3")), ("ask", D("25788.3")), ("ts", T_FLATTEN)):
        object.__setattr__(q, name, value)
    return q


@pytest.mark.parametrize("where", ["entry_context", "exit_quote"])
async def test_crossed_quote_books_a_cost_fallback_record(
    executor, signal, rules, journal, fake, clock, monkeypatch, where
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.trigger_stop("DEAL-1", STOP)
    [closed] = await fake.closed_since(T_ENTRY)
    if where == "entry_context":
        ctx = json.loads(journal.get_meta(f"entry_ctx:{CID}"))
        ctx["decision_bid"] = "25790.3"  # bid > ask 25782.3: ujemny spread wejścia
        journal.set_meta(f"entry_ctx:{CID}", json.dumps(ctx))
    else:

        async def quote(_epic):
            return crossed_quote()

        monkeypatch.setattr(fake, "quote", quote)
    await executor.book(position(journal), closed)
    # gross = (25752.3 − 25782.3) × 0.007 = −0.21; spread 0; poślizg STOP 1.5 × 0.007 = 0.0105 → 0.01; netto −0.22
    assert journal.trades() == [
        trade(decision_bid=FILL, decision_ask=FILL, exit_fill=STOP, gross="-0.21", spread="0.00", slippage="0.01",
              net="-0.22", exit_ts=T_FLATTEN, exit_reason="STOP")
    ]  # fmt: skip
    [event] = events(journal, "COST_FALLBACK")
    assert ("spread_entry" if where == "entry_context" else "spread_exit") in event["reason"]
    assert "instrument_rules" not in fake.calls  # wartość punktu z kontekstu wejścia


async def test_another_positions_entry_context_is_never_borrowed(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)  # wskaźnik ostatniego wejścia: CID z DEAL-1
    pos = PositionRecord("DE40", P.EXITING, "DEAL-9", Side.BUY, D("0.010"), D("25700.0"), None, T_ENTRY)
    await executor.book(pos, ClosedDeal("DEAL-9", "DE40", D("25720.0"), "MANUAL", T_FLATTEN))
    [booked] = journal.trades()
    assert booked.decision_bid == booked.decision_ask == D("25700.0")  # nie notowanie decyzji z wejścia DEAL-1
    assert events(journal, "COST_FALLBACK")[0]["reason"] == "NO_ENTRY_CONTEXT"


async def test_a_journal_failure_while_building_the_facts_propagates_instead_of_a_fallback(
    executor, signal, rules, journal, fake, clock, monkeypatch
):
    await entered(executor, signal, rules, fake)
    fake.trigger_stop("DEAL-1", STOP)
    [closed] = await fake.closed_since(T_ENTRY)
    real = journal.append_raw

    def append_raw(source, payload, ts):
        if source == "broker.quote":
            raise JournalWriteError("disk full")
        return real(source, payload, ts)

    monkeypatch.setattr(journal, "append_raw", append_raw)
    with pytest.raises(JournalWriteError):
        await executor.book(position(journal), closed)
    assert journal.trades() == []  # żadnego zdegradowanego rekordu w miejsce pełnego: księgowanie się powtórzy


async def test_booking_failure_propagates_and_the_position_is_never_flat_unbooked(
    executor, mgr, signal, rules, journal, fake, clock, kill, risk_cfg, monkeypatch
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    failing = switchable(monkeypatch, journal, "record_trade", JournalWriteError("disk full"))
    with pytest.raises(JournalWriteError):
        await executor.flatten(position(journal), "FORCED")
    assert position(journal).state is P.EXITING and journal.trades() == [] and journal.realized_pnl(DAY) == 0
    assert events(journal, "FLATTENED") == []

    failing["on"] = False  # dziennik wrócił: księguje uzgadnianie, bo pozycja nie jest FLAT
    reconciler = Reconciler(journal, fake, clock, kill, risk_cfg, "DE40", booker=executor,
                            entry_gate=lambda: mgr.in_flight)  # fmt: skip
    clock.advance(risk_cfg.reconcile_interval_s)
    result = await reconciler.run_once()
    assert [c.deal_id for c in result.closed] == ["DEAL-1"] and position(journal).state is P.FLAT
    assert len(journal.trades()) == 1


async def test_position_adopted_by_the_reconciler_is_booked_with_its_entry_context(
    executor, mgr, signal, rules, journal, fake, clock, kill, risk_cfg
):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)  # przyjęte, odpowiedź zgubiona
    for _ in range(3):
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.UNKNOWN and kill.reason() == "ENTRY_UNRESOLVED"
    reconciler = Reconciler(journal, fake, clock, kill, risk_cfg, "DE40", booker=executor,
                            entry_gate=lambda: mgr.in_flight)  # fmt: skip
    clock.advance(risk_cfg.reconcile_interval_s)
    await reconciler.run_once()
    adopted_at = clock.now_utc()
    assert journal.get_order(CID).state is O.CONFIRMED and position(journal).deal_id == "DEAL-1"
    assert journal.get_meta("entry_by_deal:DEAL-1") is None  # wykonawca nie widział CONFIRMED

    fake.trigger_stop("DEAL-1", STOP)
    clock.advance(risk_cfg.reconcile_interval_s)
    await reconciler.run_once()
    assert journal.trades() == [
        trade(**STOP_LOSS, exit_ts=adopted_at, confirm_ts=adopted_at,
              latency_ms=int((adopted_at - T_ENTRY).total_seconds() * 1000))
    ]  # fmt: skip
    assert events(journal, "COST_FALLBACK") == []


# --- kontrole limitów strat po księgowaniu -----------------------------------------------------------


def seeded_position(journal):
    """Pozycja BUY 0.010 z kontekstem wejścia: decyzja 25719/25721, wypełnienie 25721."""
    cid = "gws-20261102-DE40-7-entry"
    ctx = {
        "client_id": cid, "epic": "DE40", "side": "BUY", "size": "0.010", "stop_distance": "30",
        "value_per_point": "1", "decision_bid": "25719", "decision_ask": "25721",
        "decision_ts": T_ENTRY.isoformat(), "signal_ts": T_ENTRY.isoformat(), "submit_ts": T_ENTRY.isoformat(),
    }  # fmt: skip
    journal.set_meta(f"entry_ctx:{cid}", json.dumps(ctx))
    journal.set_meta("entry_by_deal:DEAL-7", cid)
    return PositionRecord("DE40", P.EXITING, "DEAL-7", Side.BUY, D("0.010"), D("25721"), D("25691"), T_ENTRY)


# Wyjście po bid 25701 przy spreadzie 2.0 (fixture `quote`): mid wyjścia 25702, mid wejścia 25720.
# gross = −18 × 0.010 = −0.18; spread (1 + 1) × 0.010 = 0.02; poślizg 0.005 → 0.01; netto −0.21.
SEEDED_CLOSE = ClosedDeal("DEAL-7", "DE40", D("25701"), "MANUAL", T_FLATTEN)


@pytest.mark.parametrize(
    ("day_before", "earlier", "level", "reason", "triggered"),
    [
        ("-1.28", "0", KillLevel.NONE, "", []),  # dzień −1.49
        ("-1.29", "0", KillLevel.SOFT, "DAILY_LOSS", ["DAILY_LOSS"]),  # dzień −1.50
        ("-2.78", "0", KillLevel.SOFT, "DAILY_LOSS", ["DAILY_LOSS"]),  # dzień −2.99
        ("-2.79", "0", KillLevel.HARD, "DAILY_LOSS_2X", ["DAILY_LOSS", "DAILY_LOSS_2X"]),  # dzień −3.00
        ("0", "-11.78", KillLevel.NONE, "", []),  # łącznie −11.99
        ("0", "-11.79", KillLevel.HARD, "TOTAL_DRAWDOWN", ["TOTAL_DRAWDOWN"]),  # łącznie −12.00
    ],
)
async def test_post_booking_loss_limits_engage_the_kill_switch_at_exact_boundaries(
    executor, journal, kill, notifier, day_before, earlier, level, reason, triggered
):
    journal.add_realized_pnl(DAY, D(day_before))
    journal.add_realized_pnl(OTHER_DAY, D(earlier))
    await executor.book(seeded_position(journal), SEEDED_CLOSE)
    [booked] = journal.trades()
    assert booked.net_pnl == D("-0.21") and booked.decision_bid == D("25719")
    assert journal.realized_pnl(DAY) == D(day_before) - D("0.21")
    assert kill.level() is level and kill.reason() == reason
    assert [e["event"] for e in events(journal) if e["event"] in {"DAILY_LOSS", "DAILY_LOSS_2X", "TOTAL_DRAWDOWN"}] == (
        triggered
    )
    for code in triggered:
        assert alerts(notifier, f"{code}:")


async def test_total_drawdown_triggers_hard_kill(executor, signal, rules, journal, fake, clock, kill):
    journal.add_realized_pnl(OTHER_DAY, D("-11.78"))
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.on_call("close_position", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    await executor.flatten(position(journal), "FORCED")  # strata −0.22: łącznie −12.00
    assert journal.realized_pnl_total() == D("-12.00")
    assert kill.level() is KillLevel.HARD and kill.reason() == "TOTAL_DRAWDOWN"


class ProcessDied(Exception):
    """Symulacja przerwania procesu w danym miejscu (osobny typ: nie myli się z żadnym wyjątkiem kodu)."""


async def test_rebooking_after_a_crash_still_enforces_the_loss_limits(executor, journal, kill, monkeypatch):
    journal.add_realized_pnl(DAY, D("-1.29"))
    pos = seeded_position(journal)
    dropped = switchable(monkeypatch, kill, "soft", ProcessDied("between the booking and the kill"))
    with pytest.raises(ProcessDied):
        await executor.book(pos, SEEDED_CLOSE)  # zaksięgowane, ale kill switch nie zdążył
    assert len(journal.trades()) == 1 and kill.level() is KillLevel.NONE
    dropped["on"] = False
    await executor.book(pos, SEEDED_CLOSE)  # po restarcie uzgadnianie księguje jeszcze raz (no-op w księdze)
    assert len(journal.trades()) == 1 and journal.realized_pnl(DAY) == D("-1.50")
    assert kill.level() is KillLevel.SOFT and kill.reason() == "DAILY_LOSS"


async def test_unreadable_pnl_after_booking_soft_kills(executor, journal, kill, monkeypatch):
    def locked():
        raise JournalReadError("database is locked")

    monkeypatch.setattr(journal, "realized_pnl_total", locked)
    await executor.book(seeded_position(journal), SEEDED_CLOSE)  # zaksięgowane: limitów nie da się sprawdzić
    assert len(journal.trades()) == 1
    assert kill.level() is KillLevel.SOFT and kill.reason() == "KILL_CHECK_FAILED"


async def test_record_trade_failure_propagates_from_book_without_any_event(executor, journal, kill, monkeypatch):
    monkeypatch.setattr(journal, "record_trade", write_failure)
    with pytest.raises(JournalWriteError):
        await executor.book(seeded_position(journal), SEEDED_CLOSE)
    assert events(journal, "TRADE_BOOKED") == [] and kill.level() is KillLevel.NONE


# --- awaryjne spłaszczenie -----------------------------------------------------------------------------


@pytest.mark.parametrize("stored", ["none", "flat"])
async def test_emergency_flatten_is_a_noop_when_flat(executor, journal, fake, notifier, stored):
    if stored == "flat":
        journal.upsert_position(PositionRecord("DE40", P.FLAT))
    await executor.emergency_flatten()
    await executor.emergency_flatten()
    assert fake.calls == [] and codes(journal) == [] and notifier.alerts == []


async def test_emergency_flatten_closes_books_and_keeps_the_kill_hard(executor, signal, rules, journal, fake, kill):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    await executor.emergency_flatten()
    assert position(journal).state is P.FLAT and len(journal.trades()) == 1
    assert kill.level() is KillLevel.HARD  # HARD zostaje do decyzji właściciela
    fake.calls.clear()
    await executor.emergency_flatten()  # silnik woła to przy każdym takcie z HARD: teraz no-op
    assert fake.calls == []


async def test_emergency_flatten_retries_then_alerts(executor, signal, rules, journal, fake, clock, notifier):
    await entered(executor, signal, rules, fake)
    for _ in range(5):
        fake.fail_next("close_position", BrokerTimeout())
    tries = times_of(fake, clock, "close_position")
    await executor.emergency_flatten()
    assert tries == [0, 1, 2, 3, 4]  # 5 prób co 1 s, każda po sprawdzeniu, że pozycja nadal jest
    assert fake.calls == ["close_position", "list_positions"] * 5
    assert position(journal).state is P.EXITING and journal.trades() == []
    [event] = events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")
    assert event["attempts"] == 5 and event["outcome"] == "STILL_OPEN" and event["state"] == "EXITING"
    assert alerts(notifier, "EMERGENCY_FLATTEN_INCOMPLETE")[0][0] == "ERROR"


async def test_emergency_flatten_of_an_exiting_position_verifies_before_closing(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    journal.upsert_position(replace(position(journal), state=P.EXITING))  # zamknięcie sprzed restartu: nie wiadomo
    fake.fail_next("list_positions", BrokerError("HTTP 502"))
    fake.fail_next("list_positions", BrokerError("HTTP 502"))
    await executor.emergency_flatten()
    # próba 1 i 2: lista nieczytelna, nic nie wysłano; próba 3: lista potwierdza pozycję, dopiero wtedy zamknięcie
    assert fake.calls[:4] == ["list_positions", "list_positions", "list_positions", "close_position"]
    assert position(journal).state is P.FLAT and len(journal.trades()) == 1


async def test_emergency_flatten_retries_until_flat(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    fake.fail_next("close_position", BrokerTimeout())
    fake.fail_next("close_position", SessionExpired("expired"))
    await executor.emergency_flatten()
    assert fake.calls.count("close_position") == 3 and position(journal).state is P.FLAT
    assert len(journal.trades()) == 1 and events(journal, "EMERGENCY_FLATTEN_INCOMPLETE") == []


@pytest.mark.parametrize(("submit", "emergency"), [(10, 5), (3, 1.5), (1, 1)])
async def test_emergency_flatten_uses_shortened_timeouts(
    build, risk_cfg, signal, rules, journal, fake, monkeypatch, submit, emergency
):
    executor = build(risk=replace(risk_cfg, submit_timeout_s=submit))
    await entered(executor, signal, rules, fake)
    seen = []
    real = execution._with_timeout

    async def spy(call, timeout):
        seen.append(timeout)
        return await real(call, timeout)

    monkeypatch.setattr(execution, "_with_timeout", spy)
    fake.fail_next("close_position", BrokerTimeout())
    await executor.emergency_flatten()
    assert len(seen) >= 4 and set(seen) == {emergency}  # zamknięcie, lista, zamknięcie, historia, notowanie
    assert position(journal).state is P.FLAT


async def test_flatten_uses_the_full_submit_timeout(executor, signal, rules, journal, fake, risk_cfg, monkeypatch):
    await entered(executor, signal, rules, fake)
    seen = []
    real = execution._with_timeout

    async def spy(call, timeout):
        seen.append(timeout)
        return await real(call, timeout)

    monkeypatch.setattr(execution, "_with_timeout", spy)
    await executor.flatten(position(journal), "FORCED")
    assert seen and set(seen) == {risk_cfg.submit_timeout_s}


async def test_emergency_flatten_stops_retrying_while_the_market_is_closed(
    executor, signal, rules, journal, fake, notifier
):
    await entered(executor, signal, rules, fake)
    fake.set_market_closed(True)
    await executor.emergency_flatten()
    assert fake.calls == ["close_position"] and position(journal).state is P.EXITING
    assert fake.positions["DEAL-1"].stop_level == STOP
    [event] = events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")
    assert event["outcome"] == "MARKET_CLOSED" and event["attempts"] == 1


async def test_emergency_flatten_leaves_a_blocked_position_to_the_owner(executor, journal, fake, notifier):
    journal.upsert_position(PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, None, T_ENTRY))
    await executor.emergency_flatten()
    assert fake.calls == [] and position(journal).state is P.BLOCKED
    [event] = events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")
    assert event["state"] == "BLOCKED" and event["attempts"] == 0
    assert alerts(notifier, "EMERGENCY_FLATTEN_INCOMPLETE")


# --- różne ----------------------------------------------------------------------------------------------


async def test_a_failing_notifier_never_breaks_the_flow(executor, signal, rules, journal, fake, notifier):
    notifier.fail = RuntimeError("alert channel down")
    fake.fill_slippage = D("10")
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED and position(journal).state is P.FLAT and len(journal.trades()) == 1
    assert alerts(notifier, "SLIPPAGE_GUARD")  # alarm próbowano wysłać


def test_executor_refuses_a_non_positive_submit_timeout(build, risk_cfg):
    with pytest.raises(ValueError, match="submit_timeout_s"):
        build(risk=replace(risk_cfg, submit_timeout_s=0))
