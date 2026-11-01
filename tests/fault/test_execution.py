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

    def _build(*, live=True, gate=gate, risk=risk_cfg, broker=fake, memory=None):
        args = (mgr, broker, journal, gate, kill, CostModel(cost_cfg), clock, strat_cfg, risk, calendar, notifier)
        extra = {} if memory is None else {"memory": memory}  # pamięć wyjść poprzedniego wykonawcy procesu
        return Executor(*args, **extra) if live is DEFAULT else Executor(*args, live=live, **extra)

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


# --- wejście próby smoke (L0): ta sama ścieżka, inna bramka i rola identyfikatora -----------------------


SMOKE_CID = "gws-20261102-DE40-1-smoke"


async def test_smoke_entry_uses_the_smoke_gate_and_the_smoke_role(executor, signal, rules, journal, fake, clock):
    at(clock, T_ENTRY + timedelta(hours=3))  # 12:06: poza oknem strategii, check_entry by zablokował
    q = Quote("DE40", BID, ASK, clock.now_utc())
    fake.set_quote(q)
    rec = await executor.enter(EntrySignal(Side.BUY, D("30"), D("0.001"), q), rules, DAY, smoke=True)
    assert rec.state is O.CONFIRMED and rec.order_id == SMOKE_CID and rec.deal_id == "DEAL-1"
    assert fake.calls == ["account", "open_position", "confirm"]
    assert journal.get_meta(f"entry_ctx:{SMOKE_CID}") is not None and journal.get_meta("entry_last:DE40") == SMOKE_CID
    assert journal.count_entry_orders(DAY) == 0 and journal.events("gate_block") == []


async def test_smoke_entry_does_not_use_the_daily_entry_and_numbers_after_it(executor, signal, rules, journal, fake):
    rec = await executor.enter(signal, rules, DAY, smoke=True)
    await executor.flatten(position(journal), "SMOKE")
    assert rec.order_id == SMOKE_CID and position(journal).state is P.FLAT
    second = await executor.enter(signal, rules, DAY)  # wejście strategii tego samego dnia nadal możliwe
    assert second.order_id == "gws-20261102-DE40-2-entry" and second.state is O.CONFIRMED


async def test_smoke_entry_is_still_blocked_by_the_kill_switch(executor, signal, rules, journal, fake, kill):
    kill.soft("STOP_FILE")
    assert await executor.enter(signal, rules, DAY, smoke=True) is None
    assert fake.calls == ["account"]
    assert payloads(journal, "gate_block")[0]["reasons"] == [KILL]


@pytest.mark.parametrize("live", [DEFAULT, False])
async def test_smoke_entry_in_dry_run_never_sends(build, signal, rules, journal, fake, live):
    assert await build(live=live).enter(signal, rules, DAY, smoke=True) is None
    assert fake.calls == ["account"]
    [decision] = payloads(journal, "decision")
    assert decision["request"]["client_id"] == SMOKE_CID and journal.count_orders(DAY) == 0


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


async def test_an_orphan_entry_context_without_an_order_row_is_replaced_and_the_entry_proceeds(
    executor, signal, rules, journal, fake, kill
):
    # przerwanie między zapisem kontekstu a wierszem INTENT: nic nie wysłano (INTENT poprzedza wysyłkę), kontekst nasz
    stale = json.dumps({"client_id": CID, "decision_bid": "1", "decision_ask": "2", "submit_ts": "x"})
    journal.set_meta(f"entry_ctx:{CID}", stale)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED and rec.order_id == CID and fake.calls.count("open_position") == 1
    ctx = json.loads(journal.get_meta(f"entry_ctx:{CID}"))
    assert ctx["decision_bid"] == "25780.3" and ctx["decision_ask"] == "25782.3"  # kontekst tego wejścia
    [orphan] = events(journal, "ORPHAN_ENTRY_CONTEXT_REPLACED")
    assert orphan["client_id"] == CID and orphan["previous"] == stale
    assert kill.level() is KillLevel.NONE and journal.trades() == []


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


async def test_a_failing_entry_by_deal_write_never_skips_the_guards(
    executor, quote, rules, journal, fake, kill, monkeypatch
):
    # przegląd T12, test A: wypełnienie 0.0401 (ryzyko 1.0025 > 1.00) i 10 pkt poślizgu, zapis entry_by_deal zawodzi
    fake.fill_size_factor, fake.fill_slippage = D("10.025"), D("10")
    order = []
    real = journal.set_meta

    def set_meta(key, value):
        if key.startswith("entry_by_deal:"):
            order.append("entry_by_deal")
            raise JournalWriteError("SQLITE_BUSY")
        return real(key, value)

    monkeypatch.setattr(journal, "set_meta", set_meta)
    fake.on_call("close_position", lambda: order.append("close_position"))
    rec = await executor.enter(EntrySignal(Side.BUY, D("25"), D("0.004"), quote), rules, DAY)
    assert rec.state is O.CONFIRMED
    assert order == ["close_position", "entry_by_deal"]  # straże i zamknięcie przed zapisem pomocniczym
    assert codes(journal)[:2] == ["SLIPPAGE_GUARD", "SIZE_MISMATCH_RISK"]
    assert position(journal).state is P.FLAT and journal.trades()[0].decision_bid == BID  # kontekst przez wskaźnik
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"


async def test_entry_by_deal_write_failure_without_a_guard_only_soft_kills(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    real = journal.set_meta

    def set_meta(key, value):
        if key.startswith("entry_by_deal:"):
            raise JournalWriteError("SQLITE_BUSY")
        return real(key, value)

    monkeypatch.setattr(journal, "set_meta", set_meta)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED and position(journal).state is P.OPEN and "close_position" not in fake.calls
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"


async def test_a_failing_guard_event_write_still_flattens(executor, quote, rules, journal, fake, kill, monkeypatch):
    fake.fill_slippage = D("10")
    failing_raw(journal, monkeypatch, lambda s, p: s == "event" and "SLIPPAGE_GUARD" in p)
    await executor.enter(EntrySignal(Side.BUY, D("30"), SIZE, quote), rules, DAY)
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"


async def test_both_guards_are_checked_and_the_position_is_closed_once(executor, quote, rules, journal, fake):
    fake.fill_slippage, fake.fill_size_factor = D("10"), D("10.025")
    await executor.enter(EntrySignal(Side.BUY, D("25"), D("0.004"), quote), rules, DAY)
    assert codes(journal)[:2] == ["SLIPPAGE_GUARD", "SIZE_MISMATCH_RISK"]
    assert fake.calls.count("close_position") == 1 and len(journal.trades()) == 1


# --- straże wypełnienia przyjętego przez uzgadnianie (BUG-E2E-1) ------------------------------------------


def make_reconciler(journal, fake, clock, kill, risk_cfg, executor, mgr):
    return Reconciler(journal, fake, clock, kill, risk_cfg, "DE40", booker=executor, entry_gate=lambda: mgr.in_flight)


async def adopted_entry(executor, reconciler, signal, rules, journal, fake, clock, risk_cfg):
    """Wejście przyjęte, odpowiedź zgubiona, lista nieczytelna przez 30 s (UNKNOWN CHECKS_INCOMPLETE, kill miękki
    ENTRY_UNRESOLVED); uzgadnianie przyjmuje pozycję z danymi brokera. Straże wejścia nie biegły; czyści
    `fake.calls`."""
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    for _ in range(3):
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.UNKNOWN and codes(journal) == ["ENTRY_UNRESOLVED"]
    clock.advance(risk_cfg.reconcile_interval_s)
    await reconciler.run_once()
    assert journal.get_order(CID).state is O.CONFIRMED and position(journal).state is P.OPEN
    assert journal.get_meta("fill_guarded:DEAL-1") is None
    fake.calls.clear()


@pytest.fixture
def adopting(executor, mgr, journal, fake, clock, kill, risk_cfg):
    return make_reconciler(journal, fake, clock, kill, risk_cfg, executor, mgr)


async def test_guard_adopted_fill_closes_an_oversize_fill_adopted_by_reconciliation(
    executor, adopting, signal, rules, journal, fake, clock, risk_cfg, notifier
):
    fake.fill_size_factor = D("5")  # 0.035 × 30 × 1 = 1.05 > max_loss_per_trade 1.00
    await adopted_entry(executor, adopting, signal, rules, journal, fake, clock, risk_cfg)
    assert position(journal).size == D("0.035")
    assert await executor.guard_adopted_fill() is True
    [risk] = events(journal, "SIZE_MISMATCH_RISK")
    assert risk["adopted"] is True and D(risk["size"]) == D("0.035") and D(risk["requested"]) == SIZE
    assert D(risk["distance"]) == D("30") and D(risk["risk"]) == D("1.05") and D(risk["limit"]) == D("1.00")
    [guard] = events(journal, "FILL_GUARD_ADOPTED")
    assert guard == {
        "event": "FILL_GUARD_ADOPTED", "deal_id": "DEAL-1", "order_id": CID, "causes": ["SIZE_MISMATCH_RISK"],
        "source": "entry_ctx",
    }  # fmt: skip
    assert events(journal, "SLIPPAGE_GUARD") == []
    assert fake.calls.count("close_position") == 1 and "DEAL-1" not in fake.positions
    [booked] = journal.trades()
    assert booked.exit_reason == "FORCED" and booked.size == D("0.035") and booked.decision_bid == BID
    assert position(journal) == PositionRecord("DE40", P.FLAT)
    [(level, message)] = alerts(notifier, "SIZE_MISMATCH_RISK:")
    assert level == "ERROR" and "(adopted by reconciliation)" in message
    assert journal.get_meta("fill_guarded:DEAL-1") == "SIZE_MISMATCH_RISK"
    assert journal.get_meta("entry_by_deal:DEAL-1") == CID  # księgowanie znajdzie kontekst bezpośrednio


async def test_guard_adopted_fill_closes_a_slipped_fill_using_the_decision_quote_of_the_entry_context(
    executor, adopting, signal, rules, journal, fake, clock, risk_cfg
):
    fake.fill_slippage = D("10")  # wypełnienie 25792.3 przy ask decyzji 25782.3: 10 > 25% × 30
    await adopted_entry(executor, adopting, signal, rules, journal, fake, clock, risk_cfg)
    # rynek doszedł do wypełnienia: liczy się notowanie decyzji z kontekstu, nie bieżące (tu poślizg byłby ujemny)
    fake.set_quote(Quote("DE40", D("25791.3"), D("25793.3"), clock.now_utc()))
    assert await executor.guard_adopted_fill() is True
    [slip] = events(journal, "SLIPPAGE_GUARD")
    assert slip["adopted"] is True and slip["decision_bid"] == "25780.3" and slip["decision_ask"] == "25782.3"
    assert D(slip["open_level"]) == D("25792.3")
    assert D(slip["deviation"]) == D("10") and D(slip["limit"]) == D("7.5")
    assert events(journal, "FILL_GUARD_ADOPTED")[0]["causes"] == ["SLIPPAGE_GUARD"]
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT
    assert journal.trades()[0].exit_reason == "FORCED"
    assert journal.get_meta("fill_guarded:DEAL-1") == "SLIPPAGE_GUARD"


@pytest.mark.parametrize("fill", ["pass", "closed"])
async def test_guard_adopted_fill_is_idempotent(
    executor, adopting, signal, rules, journal, fake, clock, risk_cfg, fill
):
    if fill == "closed":
        fake.fill_size_factor = D("5")
    await adopted_entry(executor, adopting, signal, rules, journal, fake, clock, risk_cfg)
    assert await executor.guard_adopted_fill() is (fill == "closed")
    if fill == "pass":
        assert fake.calls == [] and position(journal).state is P.OPEN  # z kontekstem: bez odczytu brokera
        assert journal.get_meta("fill_guarded:DEAL-1") == "PASS"
        assert events(journal, "FILL_GUARD_ADOPTED")[0]["causes"] == []
    fake.calls.clear()
    seen = len(journal.events())
    assert await executor.guard_adopted_fill() is False
    assert fake.calls == [] and len(journal.events()) == seen


@pytest.mark.parametrize("slip", ["0", "10"], ids=["pass", "slipped"])
async def test_after_submit_marks_the_fill_guarded_so_reconciliation_never_reruns_the_guards(
    executor, signal, rules, journal, fake, slip
):
    fake.fill_slippage = D(slip)
    rec = await executor.enter(signal, rules, DAY)
    assert rec.state is O.CONFIRMED
    assert journal.get_meta("fill_guarded:DEAL-1") == ("PASS" if slip == "0" else "SLIPPAGE_GUARD")
    fake.calls.clear()
    seen = len(journal.events())
    assert await executor.guard_adopted_fill() is False
    assert fake.calls == [] and len(journal.events()) == seen


NO_CTX_POSITION = PositionRecord("DE40", P.OPEN, "DEAL-9", Side.BUY, D("0.010"), D("25700"), D("25670"), T_ENTRY)


@pytest.mark.parametrize("case", ["oversize", "normal", "rules_unreadable", "no_stop"])
async def test_guard_adopted_fill_without_entry_context_checks_the_size_risk_from_broker_rules(
    executor, journal, fake, kill, notifier, case
):
    # pozycja OPEN z deal_id bez kontekstu wejścia (np. kontekst nieczytelny): tylko ryzyko wielkości, wartość punktu
    # od brokera; stop 30 pkt; 0.040 × 30 × 1 = 1.20 > 1.00, 0.010 × 30 × 1 = 0.30
    stored = replace(NO_CTX_POSITION, size=D("0.040") if case == "oversize" else D("0.010"))
    if case == "no_stop":
        stored = replace(stored, stop_level=None)
    journal.upsert_position(stored)
    fake.positions["DEAL-9"] = OpenPosition("DEAL-9", "DE40", Side.BUY, stored.size, D("25700"), stored.stop_level,
                                            T_ENTRY)  # fmt: skip
    if case == "rules_unreadable":
        fake.fail_next("instrument_rules", BrokerError("HTTP 503"))
    fired = await executor.guard_adopted_fill()
    marker = journal.get_meta("fill_guarded:DEAL-9")
    if case == "no_stop":  # stop ustawia procedura zabezpieczenia; straż czeka na następny przebieg
        assert fired is False and fake.calls == [] and marker is None and journal.events() == []
        return
    if case == "rules_unreadable":
        assert fired is False and fake.calls == ["instrument_rules"] and marker is None
        assert [lvl for lvl, _ in alerts(notifier, "FILL_GUARD_UNAVAILABLE DEAL-9")] == ["WARN"]
        assert "close_position" not in fake.calls and position(journal) == stored
        assert await executor.guard_adopted_fill() is False  # następny przebieg ponawia
        assert fake.calls == ["instrument_rules"] * 2 and journal.get_meta("fill_guarded:DEAL-9") == "NO_CONTEXT_PASS"
        return
    [guard] = events(journal, "FILL_GUARD_ADOPTED")
    assert guard["source"] == "broker_rules" and guard["order_id"] is None
    assert events(journal, "SLIPPAGE_GUARD") == []  # bez notowania decyzji poślizgu nie da się sprawdzić
    if case == "normal":
        assert fired is False and fake.calls == ["instrument_rules"] and marker == "NO_CONTEXT_PASS"
        assert guard["causes"] == [] and position(journal) == stored
        return
    assert fired is True and guard["causes"] == ["SIZE_MISMATCH_RISK"]
    [risk] = events(journal, "SIZE_MISMATCH_RISK")
    assert risk["requested"] is None and D(risk["risk"]) == D("1.20") and risk["adopted"] is True
    assert fake.calls[:2] == ["instrument_rules", "close_position"] and fake.calls.count("close_position") == 1
    assert position(journal) == PositionRecord("DE40", P.FLAT) and journal.trades()[0].exit_reason == "FORCED"
    assert marker == "SIZE_MISMATCH_RISK" and journal.get_meta("entry_by_deal:DEAL-9") is None


@pytest.mark.parametrize("stored", ["EXITING", "BLOCKED", "FLAT", "ENTERING", "none", "unresolved_order"])
async def test_guard_adopted_fill_leaves_exiting_blocked_flat_and_entering_alone(executor, journal, fake, stored):
    oversize = replace(NO_CTX_POSITION, size=D("0.040"))
    record = {
        "EXITING": replace(oversize, state=P.EXITING),
        "BLOCKED": replace(oversize, state=P.BLOCKED),
        "FLAT": PositionRecord("DE40", P.FLAT),
        "ENTERING": PositionRecord("DE40", P.ENTERING),
        "none": None,
        "unresolved_order": oversize,  # zlecenie nierozstrzygnięte: najpierw rozstrzyga je uzgadnianie
    }[stored]
    if record is not None:
        journal.upsert_position(record)
    if stored == "unresolved_order":
        journal.insert_order(OrderRecord(CID, "DE40", Side.BUY, SIZE, D("30"), O.UNKNOWN, None, None, T_ENTRY, T_ENTRY))
    fake.positions["DEAL-9"] = OpenPosition("DEAL-9", "DE40", Side.BUY, D("0.040"), D("25700"), D("25670"), T_ENTRY)
    assert await executor.guard_adopted_fill() is False
    assert fake.calls == [] and journal.events() == [] and journal.get_meta("fill_guarded:DEAL-9") is None
    assert position(journal) == record


@pytest.mark.parametrize("failure", ["exiting_write", "position_left_open"])
async def test_guard_adopted_fill_retries_when_the_exiting_write_of_its_flatten_fails(
    executor, adopting, signal, rules, journal, fake, clock, risk_cfg, monkeypatch, failure
):
    fake.fill_size_factor = D("5")
    await adopted_entry(executor, adopting, signal, rules, journal, fake, clock, risk_cfg)
    if failure == "exiting_write":
        failing = switchable(monkeypatch, journal, "upsert_position", JournalWriteError("disk full"))
        with pytest.raises(JournalWriteError):  # zwykłe spłaszczenie: EXITING przed zamknięciem (spec §8)
            await executor.guard_adopted_fill()
        failing["on"] = False
    else:  # spłaszczenie wróciło, a pozycja nadal OPEN (np. odmowa): znacznika nie ma, straż pobiegnie znowu
        real = executor.flatten

        async def left_open(_position, _reason):
            return None

        monkeypatch.setattr(executor, "flatten", left_open)
        assert await executor.guard_adopted_fill() is True
        monkeypatch.setattr(executor, "flatten", real)
    assert "close_position" not in fake.calls and position(journal).state is P.OPEN
    assert journal.get_meta("fill_guarded:DEAL-1") is None  # straż pobiegnie znowu
    assert await executor.guard_adopted_fill() is True
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT
    assert len(journal.trades()) == 1 and journal.get_meta("fill_guarded:DEAL-1") == "SIZE_MISMATCH_RISK"


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
    assert [level for level, _ in alerts(executor._notifier, "STOP_ATTACHED")] == ["INFO"]


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


def failing_raw(journal, monkeypatch, predicate):
    """`append_raw` rzuca JournalWriteError dla zapisów spełniających `predicate(source, payload)`."""
    real = journal.append_raw

    def append_raw(source, payload, ts):
        if predicate(source, payload):
            raise JournalWriteError("disk full")
        return real(source, payload, ts)

    monkeypatch.setattr(journal, "append_raw", append_raw)


@pytest.mark.parametrize("set_stop_failures", [2, 3], ids=["third_attempt_works", "all_fail"])
async def test_protection_runs_every_attempt_even_when_journaling_fails(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch, set_stop_failures
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    for _ in range(set_stop_failures):
        fake.fail_next("set_stop", BrokerError("HTTP 500"))
    failing_raw(journal, monkeypatch, lambda s, p: s == "broker.set_stop" or "STOP_ATTACHED" in p)
    protected = await executor.protect_unprotected(fake.positions["DEAL-1"])
    assert fake.calls.count("set_stop") == 3  # nieudany zapis surowego błędu nie przerywa kolejnych prób
    assert alerts(notifier, "JOURNAL_WRITE_FAILED")
    if set_stop_failures == 2:
        assert protected is True and fake.positions["DEAL-1"].stop_level == STOP
        assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"
    else:
        assert protected is False and kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"
        assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT


async def test_protection_failed_event_write_never_skips_the_emergency_close(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    for _ in range(3):
        fake.fail_next("set_stop", BrokerError("HTTP 500"))
    failing_raw(journal, monkeypatch, lambda s, p: s == "event" and "PROTECTION_FAILED" in p)
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is False
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.FLAT
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"


async def test_cancellation_during_protection_ends_hard_without_claiming_an_emergency_flatten(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    started = asyncio.Event()

    async def hang(_deal_id, _level):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake, "set_stop", hang)
    task = asyncio.create_task(executor.protect_unprotected(fake.positions["DEAL-1"]))
    await asyncio.wait_for(started.wait(), TEST_GUARD_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"  # silnik spłaszczy przy HARD
    assert "close_position" not in fake.calls
    [(level, message)] = alerts(notifier, "UNPROTECTED_POSITION:")
    assert level == "ERROR" and "not run" in message


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


async def test_protection_confirms_a_stop_by_a_list_read_before_going_hard(
    executor, signal, rules, journal, fake, kill, clock, notifier
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    for _ in range(3):  # broker ustawia stop, ale żadna odpowiedź nie dociera w 2 s
        fake.fail_next("set_stop", BrokerTimeout(), applied=True)
    reads = times_of(fake, clock, "list_positions")
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is True
    assert fake.calls == ["set_stop"] * 3 + ["list_positions"] and reads == [6]  # 4 s budżetu zostały na odczyt
    assert kill.level() is KillLevel.NONE and fake.positions["DEAL-1"].stop_level == STOP
    [event] = events(journal, "STOP_ATTACHED")
    assert event["verified_by"] == "list" and D(event["stop_level"]) == STOP
    [listed] = [p for p in payloads(journal, "broker.list_positions") if p.get("context") == "protection"]
    assert listed["response"][0]["deal_id"] == "DEAL-1"  # surowa odpowiedź przed interpretacją
    assert [lvl for lvl, _ in alerts(notifier, "STOP_ATTACHED")] == ["INFO"]


@pytest.mark.parametrize("broker_view", ["no_stop", "derived_stop", "other_level", "list_unreadable", "budget_spent"])
async def test_protection_still_goes_hard_when_the_list_shows_no_or_a_derived_stop(
    executor, signal, rules, journal, fake, kill, clock, broker_view
):
    await entered(executor, signal, rules, fake)
    fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
    for _ in range(3):
        fake.fail_next("set_stop", BrokerTimeout())  # nic nie ustawione
    if broker_view in ("derived_stop", "other_level"):
        level, derived = (STOP, True) if broker_view == "derived_stop" else (STOP - 1, False)

        def broker_stop() -> None:
            fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=level, stop_derived=derived)

        fake.on_call("list_positions", broker_stop, once=True)
    elif broker_view == "list_unreadable":
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
    elif broker_view == "budget_spent":  # każda próba czeka 1.75 s: z budżetu 10 s zostaje 0.5 s (< 1 s)
        fake.on_call("set_stop", lambda: clock.advance(1.75))
    assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is False
    assert kill.level() is KillLevel.HARD and kill.reason() == "UNPROTECTED_POSITION"
    assert fake.calls[:3] == ["set_stop"] * 3 and fake.calls.count("close_position") == 1
    reads = 0 if broker_view == "budget_spent" else 1  # bez budżetu: od razu HARD i awaryjne zamknięcie
    assert fake.calls[3:4 + reads] == ["list_positions"] * reads + ["close_position"]
    assert events(journal, "STOP_ATTACHED") == []


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


async def test_ambiguous_closure_in_the_history_is_not_booked(executor, signal, rules, journal, fake, notifier,
                                                              monkeypatch):  # fmt: skip
    await entered(executor, signal, rules, fake)

    async def twice(_since):
        fake.calls.append("closed_since")
        return [
            ClosedDeal("DEAL-1", "DE40", BID, "MANUAL", T_ENTRY), ClosedDeal("DEAL-1", "DE40", STOP, "STOP", T_ENTRY)
        ]

    monkeypatch.setattr(fake, "closed_since", twice)
    assert await executor.flatten(position(journal), "FORCED") is None
    assert position(journal).state is P.EXITING and journal.trades() == []
    assert events(journal, "AMBIGUOUS_CLOSURE")[0]["count"] == 2 and alerts(notifier, "AMBIGUOUS_CLOSURE")


async def test_a_booking_error_in_settle_leaves_the_position_exiting(executor, signal, rules, journal, fake, notifier):
    await entered(executor, signal, rules, fake)
    journal.set_meta(f"entry_ctx:{CID}", "{}")  # kontekst nieczytelny: rekord zdegradowany potrzebuje brokera
    fake.fail_next("instrument_rules", BrokerError("HTTP 503"))
    assert await executor.flatten(position(journal), "FORCED") is None
    assert position(journal).state is P.EXITING and journal.trades() == []
    assert events(journal, "BOOKING_FAILED")[0]["error"] == "BookingError" and alerts(notifier, "BOOKING_FAILED")


async def test_an_unknown_history_start_leaves_exiting_without_reading_the_history(executor, journal, fake):
    rec = PositionRecord("DE40", P.OPEN, "DEAL-1", Side.BUY, SIZE, FILL, STOP, None)
    journal.upsert_position(rec)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    assert await executor.flatten(rec, "FORCED") is None
    assert fake.calls == ["close_position"] and position(journal).state is P.EXITING
    assert events(journal, "CLOSE_SENT_AWAIT_HISTORY")[0]["why"] == "HISTORY_START_UNKNOWN"


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
                    D("0.00"), D("0.00"), D(net), T_ENTRY, T_ENTRY, T_ENTRY, closed.ts, 0, "MANUAL")
    ]  # fmt: skip  # zamknięcie MANUAL bez zamiaru wyjścia silnika: właściciel
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
    # potwierdzenie przyjętego zlecenia = czas wypełnienia u brokera, nie chwila zapisu przez uzgadnianie
    assert journal.trades() == [trade(**STOP_LOSS, exit_ts=adopted_at, confirm_ts=T_ENTRY, latency_ms=0)]
    assert events(journal, "COST_FALLBACK") == []


async def test_adopted_entry_latency_uses_the_broker_fill_time(
    executor, adopting, signal, rules, journal, fake, clock, risk_cfg
):
    fake.on_call("open_position", lambda: clock.advance(1.5), once=True)  # wypełnienie 1.5 s po sygnale
    await adopted_entry(executor, adopting, signal, rules, journal, fake, clock, risk_cfg)
    filled_at = T_ENTRY + timedelta(seconds=1.5)
    assert position(journal).opened_at == filled_at
    assert (journal.get_order(CID).updated_at - T_ENTRY).total_seconds() > 30  # CONFIRMED zapisane dużo później
    fake.trigger_stop("DEAL-1", STOP)
    clock.advance(risk_cfg.reconcile_interval_s)
    await adopting.run_once()
    [booked] = journal.trades()
    assert booked.confirm_ts == filled_at and booked.latency_ms == 1500  # bez przestoju i rozstrzygania


@pytest.mark.parametrize(("late_s", "uses_quote"), [(60, True), (61, False)], ids=["at_tolerance", "late"])
async def test_late_booking_uses_the_entry_spread_not_the_current_quote(
    executor, signal, rules, journal, fake, clock, late_s, uses_quote
):
    await entered(executor, signal, rules, fake)
    at(clock, T_FLATTEN)
    fake.trigger_stop("DEAL-1", STOP)
    [closed] = await fake.closed_since(T_ENTRY)
    clock.advance(late_s)  # księgowanie po restarcie: bieżące notowanie nie mówi nic o spreadzie przy wyjściu
    fake.set_quote(Quote("DE40", D("25740.3"), D("25750.3"), clock.now_utc()))  # spread 10
    fake.calls.clear()
    await executor.book(position(journal), closed)
    [booked] = journal.trades()
    [event] = events(journal, "TRADE_BOOKED")
    if uses_quote:
        assert fake.calls == ["quote"] and booked.spread_cost > D("0.01")
        assert "EXIT_SPREAD_FROM_ENTRY" not in event["notes"]
        return
    assert fake.calls == []  # spread wyjścia = spread wejścia (2.0), bez odczytu notowania
    assert booked == trade(**STOP_LOSS, exit_ts=T_FLATTEN)
    assert "EXIT_SPREAD_FROM_ENTRY" in event["notes"]


# --- księgowanie przez właściciela (resolve-lost-trade, BUG-E2E-2) -----------------------------------


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL], ids=["buy", "sell"])
async def test_book_estimated_uses_the_worst_case_entry_and_flags_the_trade(executor, journal, fake, notifier, side):
    # wejście przyjęte = wyjście ∓ odległość stopu (najgorsza strata ze stopu), notowanie decyzji = wejście, spready 0
    exit_level = D("25752.3")
    entry = exit_level + 30 if side is Side.BUY else exit_level - 30
    pos = PositionRecord("DE40", P.BLOCKED, "DEAL-9", side, D("0.007"), entry, None, T_ENTRY)
    closed = ClosedDeal("DEAL-9", "DE40", exit_level, "STOP", T_ENTRY + timedelta(hours=1))
    await executor.book(pos, closed, estimated=True)
    # gross −30 × 0.007 = −0.21; poślizg STOP (0.5 + 1.0) × 0.007 = 0.0105 → 0.01; spread 0; netto −0.22
    assert journal.trades() == [
        TradeRecord("DE40", side, D("0.007"), entry, entry, entry, exit_level, D("-0.21"), D("0.00"), D("0.01"),
                    D("0.00"), D("0.00"), D("-0.22"), T_ENTRY, T_ENTRY, T_ENTRY, closed.ts, 0, "STOP")
    ]  # fmt: skip
    assert journal.trade_flags("DEAL-9") == "ESTIMATED RESOLVED_BY_OWNER" and journal.realized_pnl(DAY) == D("-0.22")
    [event] = events(journal, "TRADE_BOOKED")
    assert event["estimated"] is True and event["net_pnl"] == "-0.22" and event["cost_fallback"] is False
    assert events(journal, "COST_FALLBACK") == []  # szacunek to nie awaria kosztów
    [(level, message)] = alerts(notifier, "LOST_TRADE_ESTIMATED")
    assert level == "ERROR" and message.startswith("LOST_TRADE_ESTIMATED DEAL-9: worst-case loss booked")
    assert fake.calls == ["instrument_rules"]  # wartość punktu od brokera: szacunek nie ma kontekstu tego deala


@pytest.mark.parametrize(
    ("size", "exit_level"),
    [(D("0.007"), D("25782.3")), (D("0.007"), D("25790.0")), (D("0.001"), D("25780.3"))],
    ids=["flat", "profit", "loss_rounds_to_zero"],
)
async def test_book_estimated_refuses_a_non_negative_gross(executor, journal, fake, notifier, size, exit_level):
    pos = PositionRecord("DE40", P.BLOCKED, "DEAL-9", Side.BUY, size, D("25782.3"), None, T_ENTRY)
    closed = ClosedDeal("DEAL-9", "DE40", exit_level, "MANUAL", T_ENTRY + timedelta(hours=1))
    with pytest.raises(execution.BookingError, match="estimate"):  # zysku (ani zera) z szacunku nigdy nie księgujemy
        await executor.book(pos, closed, estimated=True)
    assert journal.trades() == [] and journal.realized_pnl_total() == 0 and journal.trade_flags("DEAL-9") is None
    assert events(journal, "TRADE_BOOKED") == [] and alerts(notifier, "LOST_TRADE_ESTIMATED") == []
    assert alerts(notifier, "BOOKING_FAILED DEAL-9")[0][0] == "ERROR"


async def test_book_by_the_owner_flags_an_exact_trade_without_the_estimate_marks(executor, journal, notifier):
    await executor.book(seeded_position(journal), SEEDED_CLOSE, by_owner=True)
    [booked] = journal.trades()
    assert booked.entry_fill == D("25721") and booked.net_pnl == D("-0.21")  # dokładnie, z kontekstem wejścia
    assert journal.trade_flags("DEAL-7") == "RESOLVED_BY_OWNER"
    [event] = events(journal, "TRADE_BOOKED")
    assert "estimated" not in event and alerts(notifier, "LOST_TRADE_ESTIMATED") == []


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


async def test_enforce_loss_limits_reapplies_a_kill_lost_in_a_crash(
    tmp_path, executor, journal, kill, clock, data_dir, notifier, monkeypatch, build, mgr, fake, gate, cost_cfg,
    strat_cfg, risk_cfg, calendar,
):
    journal.add_realized_pnl(DAY, D("-1.29"))
    monkeypatch.setattr(journal, "set_kill", write_failure)  # kill switch włącza się tylko w pamięci
    await executor.book(seeded_position(journal), SEEDED_CLOSE)
    assert kill.level() is KillLevel.SOFT and journal.kill_state().level is KillLevel.NONE
    journal.close()  # przerwanie procesu: poziom z pamięci przepada

    from gwstock.journal import Journal
    from gwstock.risk import KillSwitch

    reopened = Journal.open(str(tmp_path / "journal.sqlite"))
    try:
        fresh_kill = KillSwitch(reopened, clock, data_dir, notifier)
        assert fresh_kill.level() is KillLevel.NONE
        fresh = Executor(mgr, fake, reopened, gate, fresh_kill, CostModel(cost_cfg), clock, strat_cfg, risk_cfg,
                         calendar, notifier, live=True)  # fmt: skip
        fresh.enforce_loss_limits(DAY)
        assert fresh_kill.level() is KillLevel.SOFT and fresh_kill.reason() == "DAILY_LOSS"
        assert reopened.kill_state().level is KillLevel.SOFT  # tym razem utrwalone
        audit = len(reopened.events("kill.audit"))
        fresh.enforce_loss_limits(DAY)  # idempotentne: bez nowego wiersza audytu
        assert len(reopened.events("kill.audit")) == audit and fresh_kill.reason() == "DAILY_LOSS"
    finally:
        reopened.close()


async def test_enforce_loss_limits_on_a_quiet_day_changes_nothing(executor, journal, kill):
    executor.enforce_loss_limits(DAY)
    assert kill.level() is KillLevel.NONE and journal.events() == []


async def test_unreadable_pnl_after_booking_soft_kills(executor, journal, kill, monkeypatch):
    def locked():
        raise JournalReadError("database is locked")

    monkeypatch.setattr(journal, "realized_pnl_total", locked)
    await executor.book(seeded_position(journal), SEEDED_CLOSE)  # zaksięgowane: limitów nie da się sprawdzić
    assert len(journal.trades()) == 1
    assert kill.level() is KillLevel.SOFT and kill.reason() == "KILL_CHECK_FAILED"


async def test_rebooking_an_already_booked_deal_reads_nothing_from_the_broker(executor, journal, fake):
    pos = PositionRecord("DE40", P.EXITING, "DEAL-9", Side.BUY, D("0.010"), D("25700.0"), None, T_ENTRY)
    closed = ClosedDeal("DEAL-9", "DE40", D("25720.0"), "MANUAL", T_ENTRY + timedelta(hours=1))
    await executor.book(pos, closed)  # COST_FALLBACK: wartość punktu od brokera
    fake.calls.clear()
    fake.fail_next("instrument_rules", BrokerError("HTTP 503"))
    await executor.book(pos, closed)  # np. uzgadnianie po restarcie: no-op, mimo że broker nie odpowiada
    assert fake.calls == [] and len(journal.trades()) == 1


@pytest.mark.parametrize(
    ("reason", "intent", "expected"),
    [("MANUAL", None, "MANUAL"), ("FORCED", None, "FORCED"), ("MANUAL", "FORCED", "FORCED"),
     ("STOP", "FORCED", "STOP")],
    ids=["owner_close", "broker_forced", "engine_exit", "stop_during_engine_exit"],
)  # fmt: skip
async def test_exit_reason_tells_engine_exits_from_owner_and_broker_closes(executor, journal, reason, intent, expected):
    if intent is not None:
        journal.set_meta("exit_intent:DEAL-9", intent)
    pos = PositionRecord("DE40", P.EXITING, "DEAL-9", Side.BUY, D("0.010"), D("25700.0"), None, T_ENTRY)
    await executor.book(pos, ClosedDeal("DEAL-9", "DE40", D("25720.0"), reason, T_ENTRY + timedelta(hours=1)))
    assert [t.exit_reason for t in journal.trades()] == [expected]


async def test_flatten_records_the_exit_intent_before_the_close(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    seen = []
    fake.on_call("close_position", lambda: seen.append(journal.get_meta("exit_intent:DEAL-1")))
    await executor.flatten(position(journal), "FORCED")
    assert seen == ["FORCED"] and journal.trades()[0].exit_reason == "FORCED"


async def test_a_failing_exit_intent_write_never_blocks_the_flatten(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    await entered(executor, signal, rules, fake)
    real = journal.set_meta

    def set_meta(key, value):
        if key.startswith("exit_intent:"):
            raise JournalWriteError("disk full")
        return real(key, value)

    monkeypatch.setattr(journal, "set_meta", set_meta)
    closed = await executor.flatten(position(journal), "FORCED")
    assert closed.reason == "MANUAL" and position(journal).state is P.FLAT
    assert journal.get_meta("exit_intent:DEAL-1") is None
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"


@pytest.mark.parametrize("via", ["flatten", "emergency_with_a_dead_journal", "after_a_downgrade"])
async def test_engine_exit_booked_in_the_same_process_is_forced_even_when_the_exit_intent_write_failed(
    build, executor, signal, rules, journal, fake, kill, monkeypatch, via
):
    await entered(executor, signal, rules, fake)
    real = journal.set_meta

    def set_meta(key, value):
        if key.startswith("exit_intent:"):
            raise JournalWriteError("disk full")
        return real(key, value)

    monkeypatch.setattr(journal, "set_meta", set_meta)
    booker = executor
    if via == "flatten":
        await executor.flatten(position(journal), "FORCED")
    else:
        kill.hard("HARD_STOP_FILE")
        monkeypatch.setattr(journal, "upsert_position", write_failure)  # EXITING i FLAT przepadają
        monkeypatch.setattr(journal, "record_trade", write_failure)  # księgowanie dopiero później
        await executor.emergency_flatten()
        assert "DEAL-1" not in fake.positions and journal.trades() == []
        monkeypatch.undo()  # dysk wrócił; zapis zamiaru nadal nie trafił do dziennika
        assert journal.get_meta("exit_intent:DEAL-1") is None
        if via == "after_a_downgrade":  # silnik buduje nowego wykonawcę z pamięcią poprzedniego
            booker = build(live=False, memory=executor.memory)
        [closed] = await fake.closed_since(T_ENTRY)
        await booker.book(position(journal), closed)  # np. uzgadnianie w tym samym procesie
    [booked] = journal.trades()
    assert booked.exit_reason == "FORCED"  # zamiar silnika z pamięci procesu (po restarcie: MANUAL, decisions-log)


async def test_a_new_process_without_the_exit_intent_books_an_engine_close_as_manual(build, executor, journal):
    # pamięć procesu nie przeżywa restartu: bez zapisu w dzienniku zamknięcie wywołaniem to MANUAL (decyzja O-3)
    executor.memory.exit_intents["DEAL-9"] = "EMERGENCY"
    fresh = build()
    pos = PositionRecord("DE40", P.EXITING, "DEAL-9", Side.BUY, D("0.010"), D("25700.0"), None, T_ENTRY)
    await fresh.book(pos, ClosedDeal("DEAL-9", "DE40", D("25720.0"), "MANUAL", T_ENTRY + timedelta(hours=1)))
    assert [t.exit_reason for t in journal.trades()] == ["MANUAL"]


@pytest.mark.parametrize("case", ["refused", "blocked_not_at_broker", "unreadable_list"])
async def test_exit_intent_is_written_only_right_before_a_close_is_sent(executor, journal, fake, kill, case):
    ours = PositionRecord("DE40", P.OPEN, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    if case == "refused":
        journal.upsert_position(PositionRecord("DE40", P.FLAT))
        assert await executor.flatten(ours, "FORCED") is None
    elif case == "blocked_not_at_broker":
        journal.upsert_position(replace(ours, state=P.BLOCKED))
        kill.hard("HARD_STOP_FILE")
        await executor.emergency_flatten()
    else:
        journal.upsert_position(replace(ours, state=P.EXITING))
        fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
        assert await executor.flatten(ours, "FORCED") is None
    assert "close_position" not in fake.calls
    assert journal.get_meta("exit_intent:DEAL-1") is None  # nic nie wysłano: żadnego zamiaru wyjścia w dzienniku


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


@pytest.mark.parametrize("case", ["foreign", "not_at_broker", "list_unreadable"])
async def test_emergency_flatten_leaves_a_blocked_position_that_is_not_provably_ours(
    executor, journal, fake, kill, notifier, case
):
    kill.hard("HARD_STOP_FILE")
    stored = PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, None, T_ENTRY)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, None, T_ENTRY)
    if case == "foreign":
        stored = PositionRecord("DE40", P.BLOCKED)  # obca pozycja: bez deal_id
    elif case == "not_at_broker":
        del fake.positions["DEAL-1"]
    else:
        for _ in range(5):
            fake.fail_next("list_positions", BrokerError("HTTP 502"))
    journal.upsert_position(stored)
    await executor.emergency_flatten()
    assert "close_position" not in fake.calls and position(journal) == stored
    assert fake.calls == ([] if case == "foreign" else ["list_positions"] * (5 if case == "list_unreadable" else 1))
    [event] = events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")
    assert event["state"] == "BLOCKED"
    assert alerts(notifier, "EMERGENCY_FLATTEN_INCOMPLETE") and kill.level() is KillLevel.HARD


async def test_emergency_flatten_closes_our_blocked_deal_when_the_broker_lists_it(executor, journal, fake, kill):
    # uzgadnianie zostawiło nasz deal BLOCKED (SIZE_MISMATCH: dziennik 0.007, broker 0.008); kill switch HARD
    journal.upsert_position(PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY))
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, D("0.008"), FILL, STOP, T_ENTRY)
    kill.hard("DAILY_LOSS_2X")
    seen = []
    fake.on_call("close_position", lambda: seen.append(position(journal).state))
    await executor.emergency_flatten()
    # bez kontekstu wejścia (pozycja z uzgadniania): wartość punktu do rekordu zdegradowanego od brokera
    assert fake.calls == ["list_positions", "close_position", "closed_since", "instrument_rules"]
    assert seen == [P.EXITING] and position(journal) == PositionRecord("DE40", P.FLAT)
    [booked] = journal.trades()
    assert booked.size == D("0.008") and booked.exit_fill == BID  # księga z widoku brokera: to zamknęliśmy
    [event] = events(journal, "BLOCKED_EMERGENCY_FLATTEN")
    assert D(event["journal"]["size"]) == SIZE and D(event["broker"]["size"]) == D("0.008")


@pytest.mark.parametrize("via", ["emergency", "protection"])
async def test_the_emergency_close_is_sent_even_when_the_exiting_write_fails(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch, via
):
    await entered(executor, signal, rules, fake)
    monkeypatch.setattr(journal, "upsert_position", write_failure)
    if via == "emergency":
        kill.hard("HARD_STOP_FILE")
        await executor.emergency_flatten()
    else:
        fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], stop_level=None)
        for _ in range(3):
            fake.fail_next("set_stop", BrokerError("HTTP 500"))
        assert await executor.protect_unprotected(fake.positions["DEAL-1"]) is False
    assert fake.calls.count("close_position") == 1 and "DEAL-1" not in fake.positions  # ekspozycja zamknięta
    assert len(journal.trades()) == 1  # księgowanie działa; zapisy pozycji zawiodły
    assert position(journal).state is P.OPEN  # dziennik dogoni uzgadnianie (OPEN, deal zniknął, zamknięcie)
    assert alerts(notifier, "JOURNAL_WRITE_FAILED") and kill.level() is KillLevel.HARD


@pytest.mark.parametrize("after_close", ["unreadable", "listed"])
async def test_emergency_flatten_never_resends_a_close_without_a_list_read_when_the_exiting_write_failed(
    executor, signal, rules, journal, fake, kill, monkeypatch, after_close
):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    switchable(monkeypatch, journal, "upsert_position", JournalWriteError("disk full"))  # EXITING przepada
    if after_close == "unreadable":
        fake.fail_next("close_position", BrokerTimeout(), applied=True)  # zamknięte, odpowiedź zgubiona
        fake.fail_next("list_positions", BrokerError("HTTP 502"))  # próba 1: nie wiadomo, czy zamknięto
    else:
        fake.fail_next("close_position", BrokerTimeout(), applied=False)  # lista po nim pokazuje pozycję
    await executor.emergency_flatten()
    if after_close == "unreadable":
        # dziennik nadal mówi OPEN, ale zamknięcie już poszło: próba 2 zaczyna od listy (pozycji nie ma), nie od wysyłki
        assert fake.calls[:3] == ["close_position", "list_positions", "list_positions"]
        assert fake.calls.count("close_position") == 1
    else:  # lista sprzed sekundy potwierdziła pozycję: próba 2 zamyka bez kolejnego odczytu
        assert fake.calls[:3] == ["close_position", "list_positions", "close_position"]
    assert "DEAL-1" not in fake.positions and len(journal.trades()) == 1


@pytest.mark.parametrize("next_tick", ["emergency", "flatten"])
async def test_a_close_sent_on_an_earlier_tick_is_never_resent_blind(
    executor, signal, rules, journal, fake, kill, monkeypatch, next_tick
):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    failing = switchable(monkeypatch, journal, "upsert_position", JournalWriteError("disk full"))
    fake.fail_next("close_position", BrokerTimeout(), applied=False)  # nie zamknięte, odpowiedź zgubiona
    for _ in range(5):
        fake.fail_next("list_positions", BrokerError("HTTP 502"))  # ten takt nic nie potwierdzi
    await executor.emergency_flatten()
    assert fake.calls.count("close_position") == 1 and position(journal).state is P.OPEN  # EXITING nie zapisane
    failing["on"] = False  # dysk wrócił; następny takt
    fake.calls.clear()
    if next_tick == "emergency":
        await executor.emergency_flatten()
    else:
        await executor.flatten(position(journal), "FORCED")
    # zamknięcie wysłane wcześniej w tym procesie: najpierw lista (pozycja nadal jest), dopiero potem zamknięcie
    assert fake.calls[:2] == ["list_positions", "close_position"] and fake.calls.count("close_position") == 1
    assert position(journal) == PositionRecord("DE40", P.FLAT) and len(journal.trades()) == 1


async def test_a_close_refused_by_a_closed_market_is_not_remembered_as_sent(executor, signal, rules, journal, fake):
    await entered(executor, signal, rules, fake)
    fake.set_market_closed(True)
    assert await executor.flatten(position(journal), "FORCED") is None  # EXITING: następna próba i tak zacznie od listy
    fake.set_market_closed(False)
    journal.upsert_position(replace(position(journal), state=P.OPEN))  # np. naprawa właściciela; zamknięcie nie poszło
    fake.calls.clear()
    await executor.flatten(position(journal), "FORCED")
    assert fake.calls[:1] == ["close_position"]  # zamknięty rynek to odmowa, nie niejasność: bez zbędnej listy


async def test_journal_failure_alert_says_the_close_was_not_sent_when_a_mandatory_write_aborted_it(
    executor, quote, rules, journal, fake, kill, notifier, monkeypatch
):
    fake.fill_slippage = D("10")  # straż poślizgu chce zamknąć
    failing_raw(journal, monkeypatch, lambda s, p: s == "event" and "SLIPPAGE_GUARD" in p)  # wg możliwości: zawodzi
    real = journal.upsert_position

    def upsert_position(rec):
        if rec.state is P.EXITING:
            raise JournalWriteError("disk full")  # obowiązkowy EXITING przerywa spłaszczenie przed zamknięciem
        return real(rec)

    monkeypatch.setattr(journal, "upsert_position", upsert_position)
    rec = await executor.enter(EntrySignal(Side.BUY, D("30"), SIZE, quote), rules, DAY)
    assert rec.state is O.CONFIRMED and "close_position" not in fake.calls
    [(level, message)] = alerts(notifier, "JOURNAL_WRITE_FAILED during entry")
    assert level == "ERROR" and "the close was NOT sent" in message and "regardless" not in message
    assert kill.level() is KillLevel.SOFT and kill.reason() == "JOURNAL_WRITE_FAILED"


async def test_journal_failure_alert_after_a_sent_close_says_it_was_sent(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch
):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    monkeypatch.setattr(journal, "upsert_position", write_failure)
    await executor.emergency_flatten()
    assert fake.calls.count("close_position") == 1
    [(level, message)] = alerts(notifier, "JOURNAL_WRITE_FAILED during emergency flatten")
    assert level == "ERROR" and "the close was sent to the broker regardless" in message


@pytest.mark.parametrize("level", ["none", "soft"])
async def test_emergency_flatten_leaves_a_blocked_position_alone_unless_the_kill_switch_is_hard(
    executor, journal, fake, kill, notifier, level
):
    stored = PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    journal.upsert_position(stored)
    fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, D("0.008"), FILL, STOP, T_ENTRY)
    if level == "soft":
        kill.soft("SIZE_MISMATCH")
    await executor.emergency_flatten()  # np. kill switch zdjęty między decyzją silnika a wykonaniem
    assert fake.calls == [] and position(journal) == stored and "DEAL-1" in fake.positions
    [refused] = events(journal, "FLATTEN_REFUSED")
    assert refused["why"] == "NOT_HARD" and refused["journal_state"] == "BLOCKED" and refused["deal_id"] == "DEAL-1"
    assert alerts(notifier, "FLATTEN_REFUSED DEAL-1")


def unreadable_journal(journal, monkeypatch, *, from_call=1, until_call=None):
    """`current_position` rzuca JournalReadError od wywołania nr `from_call` (od 1) do `until_call` włącznie."""
    real = journal.current_position
    count = {"n": 0}

    def broken(epic):
        count["n"] += 1
        if count["n"] >= from_call and (until_call is None or count["n"] <= until_call):
            raise JournalReadError("database disk image is malformed")
        return real(epic)

    monkeypatch.setattr(journal, "current_position", broken)


@pytest.mark.parametrize("seen_via", ["entry", "reconciliation_pass", "protection", "emergency_attempt"])
async def test_emergency_flatten_closes_our_last_known_deal_when_the_journal_cannot_be_read(
    build, executor, mgr, signal, rules, journal, fake, kill, clock, risk_cfg, notifier, monkeypatch, seen_via
):
    ours = PositionRecord("DE40", P.OPEN, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    if seen_via == "entry":
        ex = executor
        await entered(ex, signal, rules, fake)  # wykonawca widział DEAL-1 w dzienniku po potwierdzeniu
    else:  # nowy proces: dziennik ma DEAL-1, wykonawca widzi go dopiero w swojej procedurze
        ex = build()
        journal.upsert_position(ours)
        journal.set_meta("fill_guarded:DEAL-1", "PASS")
        fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
        if seen_via == "reconciliation_pass":
            assert await ex.guard_adopted_fill() is False  # silnik woła ją po każdym przebiegu uzgadniania
        elif seen_via == "protection":
            assert await ex.protect_unprotected(fake.positions["DEAL-1"]) is True
    kill.hard("HARD_STOP_FILE")
    fake.calls.clear()
    if seen_via == "emergency_attempt":  # próba 1 czyta dziennik; zamknięcie i lista zawodzą; potem dziennik pada
        fake.fail_next("close_position", BrokerTimeout())
        fake.fail_next("list_positions", BrokerError("HTTP 502"))
        unreadable_journal(journal, monkeypatch, from_call=3)  # odczyt pętli i odczyt w `_flatten` się udają
    else:
        unreadable_journal(journal, monkeypatch)
    await ex.emergency_flatten()
    expected = ["list_positions", "close_position"]
    if seen_via == "emergency_attempt":
        expected = ["close_position", "list_positions"] + expected  # i tak najpierw lista, nigdy ponowienie na ślepo
    assert fake.calls == expected and "DEAL-1" not in fake.positions
    [(level, message)] = [a for a in alerts(notifier, "EMERGENCY_FLATTEN_JOURNAL_UNREADABLE") if "closed" in a[1]]
    assert level == "ERROR" and "DEAL-1 closed" in message
    monkeypatch.undo()  # dziennik znów czytelny: uzgadnianie księguje zamknięcie
    assert position(journal).state in (P.OPEN, P.EXITING) and journal.trades() == []
    clock.advance(risk_cfg.reconcile_interval_s)
    await make_reconciler(journal, fake, clock, kill, risk_cfg, ex, mgr).run_once()
    assert len(journal.trades()) == 1 and position(journal) == PositionRecord("DE40", P.FLAT)


async def test_a_failed_close_without_a_journal_is_listed_again_before_any_retry(
    executor, signal, rules, journal, fake, kill, monkeypatch
):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    fake.fail_next("close_position", BrokerTimeout(), applied=True)  # zamknięte, odpowiedź zgubiona
    unreadable_journal(journal, monkeypatch, until_call=1)  # próba 1 bez dziennika, próba 2 już z dziennikiem (OPEN)
    await executor.emergency_flatten()
    assert fake.calls[:3] == ["list_positions", "close_position", "list_positions"]  # przed ponowieniem lista
    assert fake.calls.count("close_position") == 1 and len(journal.trades()) == 1
    assert position(journal) == PositionRecord("DE40", P.FLAT)


@pytest.mark.parametrize("seen", ["nothing", "another_deal"])
async def test_emergency_flatten_with_an_unreadable_journal_never_closes_a_deal_it_has_not_seen(
    build, executor, signal, rules, journal, fake, kill, notifier, monkeypatch, seen
):
    if seen == "nothing":  # nowy proces: wykonawca nie widział jeszcze żadnej pozycji
        ex = build()
        fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    else:  # widział DEAL-1, a broker pokazuje tylko DEAL-2 (nie nasz)
        ex = executor
        await entered(ex, signal, rules, fake)
        del fake.positions["DEAL-1"]
        fake.positions["DEAL-2"] = OpenPosition("DEAL-2", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    kill.hard("HARD_STOP_FILE")
    unreadable_journal(journal, monkeypatch)
    await ex.emergency_flatten()
    assert "close_position" not in fake.calls and len(fake.positions) == 1
    assert fake.calls == ([] if seen == "nothing" else ["list_positions"])
    [(level, _)] = alerts(notifier, "EMERGENCY_FLATTEN_JOURNAL_UNREADABLE")
    assert level == "ERROR" and alerts(notifier, "EMERGENCY_FLATTEN_INCOMPLETE")


async def test_a_booking_journal_failure_never_aborts_the_emergency_flatten(
    executor, signal, rules, journal, fake, kill, notifier, monkeypatch
):
    await entered(executor, signal, rules, fake)
    kill.hard("HARD_STOP_FILE")
    monkeypatch.setattr(journal, "record_trade", write_failure)
    await executor.emergency_flatten()  # bez wyjątku: ekspozycja zamknięta, księgowanie zrobi uzgadnianie
    assert fake.calls.count("close_position") == 1 and "DEAL-1" not in fake.positions
    assert position(journal).state is P.EXITING and journal.trades() == []
    assert events(journal, "EMERGENCY_FLATTEN_INCOMPLETE")[0]["outcome"] == "BOOKING_FAILED"
    assert alerts(notifier, "JOURNAL_WRITE_FAILED during emergency flatten")


async def test_normal_flatten_never_closes_before_the_exiting_write(
    executor, signal, rules, journal, fake, monkeypatch
):
    await entered(executor, signal, rules, fake)
    monkeypatch.setattr(journal, "upsert_position", write_failure)
    with pytest.raises(JournalWriteError):
        await executor.flatten(position(journal), "FORCED")
    assert "close_position" not in fake.calls  # spec §8: EXITING przed zamknięciem (po restarcie bez ślepej wysyłki)


@pytest.mark.parametrize("stored", ["missing", "blocked_with_deal"])
async def test_normal_flatten_still_refuses_blocked(executor, journal, fake, kill, stored):
    # "missing": broker nie pokazuje DEAL-1; "blocked_with_deal": pokazuje go; zwykłe spłaszczenie odmawia w obu, także
    # przy kill switchu HARD (BLOCKED zamyka wyłącznie awaryjne spłaszczenie)
    ours = PositionRecord("DE40", P.BLOCKED, "DEAL-1", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    journal.upsert_position(ours)
    if stored == "blocked_with_deal":
        fake.positions["DEAL-1"] = OpenPosition("DEAL-1", "DE40", Side.BUY, SIZE, FILL, STOP, T_ENTRY)
    kill.hard("HARD_STOP_FILE")
    assert await executor.flatten(ours, "FORCED") is None
    assert fake.calls == [] and position(journal) == ours
    assert ("DEAL-1" in fake.positions) is (stored == "blocked_with_deal")
    assert events(journal, "FLATTEN_REFUSED")[0]["journal_state"] == "BLOCKED"


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
