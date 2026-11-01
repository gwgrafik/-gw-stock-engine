"""Regresja: wejście przyjęte przez brokera, odpowiedź zgubiona, pozycja wybita stopem przed pierwszym sprawdzeniem.

Błąd (potwierdzony): trzy puste odczyty listy pozycji dawały NOT_PLACED i pozycję FLAT bez zajrzenia do historii
zamknięć; prawdziwa transakcja zamknęła się u brokera, w księdze zero, alarmu brak, a uzgadnianie nie miało już czego
uzgadniać (zlecenie końcowe, pozycja FLAT). Teraz przed NOT_PLACED menedżer czyta historię zamknięć od INTENT −
`HISTORY_MARGIN`: zamknięcie na naszym instrumencie → UNKNOWN UNMATCHED_CLOSED_DEAL i pozycja BLOCKED (bez księgowania:
cena wejścia i wielkość nieznane, odtwarza właściciel z historii Capital.com); nieudany odczyt historii → UNKNOWN
CHECKS_INCOMPLETE, pozycja ENTERING i dalej prowadzi uzgadnianie. Nigdy NOT_PLACED bez czystej historii.

Prawdziwe `Journal`, `OrderManager` i `Reconciler` na `FakeBroker` i `FakeClock`, z atrapą kill switcha.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.journal import Journal, PositionRecord
from gwstock.orders.manager import HISTORY_MARGIN, EntryBlocked, OrderManager
from gwstock.reconcile import Reconciler
from gwstock.types import BrokerError, BrokerTimeout, ClosedDeal, OpenPosition, OrderState, PositionState, Side
from tests.conftest import RecordingKillSink

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)  # = clock.now_utc() na starcie fixture `clock`
FILL = Decimal("25782.3")
STOP = FILL - Decimal("30")
CAUSE = "CAUSE=open_position:BrokerTimeout"
SECOND_ID = "gws-20261102-DE40-2-entry"


def accept_then_stop_before_first_check(fake):
    """Dokładnie scenariusz z reprodukcji: przyjęte, odpowiedź zgubiona, stop przed pierwszym odczytem listy."""
    fake.fail_next("open_position", BrokerTimeout(), applied=True)

    def stop_out():
        for deal_id in list(fake.positions):
            fake.trigger_stop(deal_id, STOP)

    fake.on_call("list_positions", stop_out, once=True)


def payloads(journal, source):
    return [json.loads(e.payload) for e in journal.events(source)]


def spy_states(journal, monkeypatch):
    """Każdy zapisany stan zlecenia i pozycji (menedżer i uzgadnianie piszą przez ten sam dziennik)."""
    written = {"order": [], "position": []}
    update_order, upsert_position = journal.update_order, journal.upsert_position

    def on_update(order_id, **fields):
        rec = update_order(order_id, **fields)
        written["order"].append(rec.state)
        return rec

    def on_upsert(p):
        written["position"].append(p.state)
        return upsert_position(p)

    monkeypatch.setattr(journal, "update_order", on_update)
    monkeypatch.setattr(journal, "upsert_position", on_upsert)
    return written


def assert_blocked_lost_trade(journal, req):
    rec = journal.get_order(req.client_id)
    assert rec.state is OrderState.UNKNOWN and rec.note.split()[0] == "UNMATCHED_CLOSED_DEAL"
    assert journal.current_position("DE40") == PositionRecord("DE40", PositionState.BLOCKED)
    assert journal.trades() == [] and journal.realized_pnl_total() == 0  # nic nie zaksięgowano (dane nieznane)


@pytest.fixture
def reopen():
    """Otwiera dziennik z pliku jak po restarcie procesu; zamyka wszystkie otwarte na końcu testu."""
    opened = []

    def _open(path):
        j = Journal.open(str(path))
        opened.append(j)
        return j

    yield _open
    for j in opened:
        j.close()


def reconciler_for(journal, fake, clock, kill_sink, risk_cfg, mgr):
    return Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40", entry_gate=lambda: mgr.in_flight)


# --- a. dokładna reprodukcja ----------------------------------------------------------------------------


async def test_lost_ack_and_stop_out_before_first_check_is_blocked_not_not_placed(journal, fake, mgr, req, monkeypatch):
    accept_then_stop_before_first_check(fake)
    written = spy_states(journal, monkeypatch)
    rec = await mgr.submit_entry(req)
    assert rec.state is OrderState.UNKNOWN and rec.note == f"UNMATCHED_CLOSED_DEAL {CAUSE}"
    assert_blocked_lost_trade(journal, req)
    assert OrderState.NOT_PLACED not in written["order"] and PositionState.FLAT not in written["position"]
    assert fake.calls == ["open_position"] + ["list_positions"] * 3 + ["closed_since"]
    assert not mgr.in_flight
    [history] = payloads(journal, "broker.closed_since")
    assert history["since"] == (T0 - HISTORY_MARGIN).isoformat()
    assert [c["deal_id"] for c in history["response"]] == ["DEAL-1"]
    [event] = payloads(journal, "order.event")
    assert event["event"] == "UNMATCHED_CLOSED_DEAL" and event["deal_ids"] == ["DEAL-1"]


# --- b. historia nieczytelna: nigdy NOT_PLACED; uzgadnianie kończy, gdy historia wróci ---------------------


@pytest.mark.parametrize("exc", [BrokerError("HTTP 502"), BrokerTimeout()], ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("stopped_out", [True, False], ids=["stopped_out", "never_placed"])
async def test_unreadable_history_never_gives_not_placed_and_the_reconciler_finishes_later(
    journal, fake, mgr, req, clock, kill_sink, risk_cfg, monkeypatch, exc, stopped_out
):
    if stopped_out:
        accept_then_stop_before_first_check(fake)
    else:
        fake.fail_next("open_position", BrokerTimeout(), applied=False)
    fake.fail_next("closed_since", exc)
    written = spy_states(journal, monkeypatch)
    rec = await mgr.submit_entry(req)
    assert rec.state is OrderState.UNKNOWN and rec.note == f"CHECKS_INCOMPLETE {CAUSE}"
    assert journal.current_position("DE40").state is PositionState.ENTERING
    [error] = payloads(journal, "broker.closed_since")
    assert error["error"] == type(exc).__name__ and error["since"] == (T0 - HISTORY_MARGIN).isoformat()

    reconciler = reconciler_for(journal, fake, clock, kill_sink, risk_cfg, mgr)
    for _ in range(2):  # licznik pustych odczytów uzgadniania: 1, 2 (bez historii)
        clock.advance(risk_cfg.reconcile_interval_s)
        await reconciler.run_once()
    fake.fail_next("closed_since", exc)
    fake.fail_next("closed_since", exc)
    for _ in range(2):  # trzeci pusty odczyt wymaga historii, a ta nadal nieczytelna: przebieg przerwany
        clock.advance(risk_cfg.reconcile_interval_s)
        with pytest.raises(type(exc)):
            await reconciler.run_once()
        assert journal.get_order(req.client_id).state is OrderState.UNKNOWN
        assert journal.current_position("DE40").state is PositionState.ENTERING
    assert OrderState.NOT_PLACED not in written["order"] and PositionState.FLAT not in written["position"]

    clock.advance(risk_cfg.reconcile_interval_s)
    await reconciler.run_once()  # historia znów czytelna
    final = journal.get_order(req.client_id)
    if stopped_out:
        assert_blocked_lost_trade(journal, req)
        assert "UNMATCHED_CLOSED_DEAL" in kill_sink.calls
    else:
        assert final.state is OrderState.NOT_PLACED and final.note.split()[0] == "NOT_FOUND_BY_RECONCILER"
        assert journal.current_position("DE40").state is PositionState.FLAT
    assert fake.calls.count("open_position") == 1


# --- c. restart w każdym ważnym punkcie --------------------------------------------------------------------


async def test_blocked_lost_trade_survives_restarts_and_keeps_raising_the_alarm(
    tmp_path, journal, fake, mgr, req, clock, risk_cfg, reopen
):
    accept_then_stop_before_first_check(fake)
    await mgr.submit_entry(req)
    path = tmp_path / "journal.sqlite"  # plik dziennika z fixture `journal`
    journal.close()

    # restart 1: zaraz po powrocie menedżera
    j1 = reopen(path)
    assert_blocked_lost_trade(j1, req)
    mgr1 = OrderManager(j1, fake, clock, risk_cfg)
    with pytest.raises(EntryBlocked):
        await mgr1.submit_entry(replace(req, client_id=SECOND_ID))
    with pytest.raises(ValueError, match="BLOCKED"):
        await mgr1.resolve_unknown(j1.get_order(req.client_id))
    kills = RecordingKillSink()
    result = await reconciler_for(j1, fake, clock, kills, risk_cfg, mgr1).run_once()
    assert result.blocked and any(n.startswith("BLOCKED_AWAITING_OWNER") for n in result.notes)
    assert kills.calls == []  # rozbieżność dopiero zauważona; alarm natychmiastowy daje wykonawca (T12)
    j1.close()

    # restart 2: między przebiegami uzgadniania, po przekroczeniu tolerancji rozbieżności
    clock.advance(risk_cfg.mismatch_tolerance_s + 1)
    j2 = reopen(path)
    mgr2 = OrderManager(j2, fake, clock, risk_cfg)
    reconciler = reconciler_for(j2, fake, clock, kills, risk_cfg, mgr2)
    assert (await reconciler.run_once()).blocked
    assert kills.calls == ["MISMATCH_TIMEOUT"]
    clock.advance(risk_cfg.reconcile_interval_s)
    assert (await reconciler.run_once()).blocked
    assert kills.calls == ["MISMATCH_TIMEOUT", "MISMATCH_TIMEOUT"]  # powtarzany co przebieg (odbiorca idempotentny)
    j2.close()

    # restart 3: stan nadal ten sam
    j3 = reopen(path)
    assert_blocked_lost_trade(j3, req)
    assert fake.calls.count("open_position") == 1


class ProcessDied(BaseException):
    """Śmierć procesu (kill -9) w wybranym miejscu: nie jest `Exception`, więc żadna siatka w kodzie jej nie połknie."""


def die_after_history_raw(journal, monkeypatch):
    real = journal.append_raw

    def append_raw(source, payload, ts):
        row = real(source, payload, ts)
        if source == "broker.closed_since":
            raise ProcessDied("killed right after the raw history record")
        return row

    monkeypatch.setattr(journal, "append_raw", append_raw)


def die_on_position_write(journal, monkeypatch, *, after):
    real = journal.upsert_position

    def upsert_position(p):
        if p.state is PositionState.BLOCKED and not after:
            raise ProcessDied("killed right before the BLOCKED write")
        real(p)
        if p.state is PositionState.BLOCKED:
            raise ProcessDied("killed right after the BLOCKED write, before the order note")

    monkeypatch.setattr(journal, "upsert_position", upsert_position)


CRASH_POINTS = {
    "after_the_history_raw_record": die_after_history_raw,
    "before_the_block_write": lambda j, mp: die_on_position_write(j, mp, after=False),
    "after_the_block_write": lambda j, mp: die_on_position_write(j, mp, after=True),
}


@pytest.mark.parametrize("crash", list(CRASH_POINTS))
async def test_a_crash_after_the_history_read_before_the_block_still_ends_blocked_after_restart(
    tmp_path, journal, fake, mgr, req, clock, risk_cfg, reopen, monkeypatch, crash
):
    # menedżer zapisał surową historię (z zamknięciem DEAL-1), a proces zginął przed (albo w trakcie) zapisu BLOCKED
    accept_then_stop_before_first_check(fake)
    CRASH_POINTS[crash](journal, monkeypatch)
    with pytest.raises(ProcessDied):
        await mgr.submit_entry(req)
    assert not mgr.in_flight
    path = tmp_path / "journal.sqlite"  # plik dziennika z fixture `journal`
    journal.close()

    # restart: na dysku zlecenie UNKNOWN i pozycja ENTERING (albo już BLOCKED), surowa historia jest, księgi nie ma
    j1 = reopen(path)
    [history] = payloads(j1, "broker.closed_since")
    assert [c["deal_id"] for c in history["response"]] == ["DEAL-1"]
    assert j1.get_order(req.client_id).state is OrderState.UNKNOWN and j1.trades() == []
    expected = PositionState.BLOCKED if crash == "after_the_block_write" else PositionState.ENTERING
    assert j1.current_position("DE40").state is expected
    written = spy_states(j1, monkeypatch)
    kills = RecordingKillSink()
    mgr1 = OrderManager(j1, fake, clock, risk_cfg)
    reconciler = reconciler_for(j1, fake, clock, kills, risk_cfg, mgr1)
    for _ in range(3):  # trzy puste odczyty po ≥ 30 s od INTENT, potem historia (zamknięcie na DE40)
        clock.advance(risk_cfg.reconcile_interval_s)
        await reconciler.run_once()
    clock.advance(risk_cfg.mismatch_tolerance_s + 1)
    assert (await reconciler.run_once()).blocked
    j1.close()

    # restart 2: transakcja nadal zablokowana do decyzji właściciela, nigdy NOT_PLACED ani FLAT
    j2 = reopen(path)
    rec = j2.get_order(req.client_id)
    assert rec.state is OrderState.UNKNOWN
    assert j2.current_position("DE40") == PositionRecord("DE40", PositionState.BLOCKED)
    assert j2.trades() == [] and j2.realized_pnl_total() == 0
    assert OrderState.NOT_PLACED not in written["order"] and PositionState.FLAT not in written["position"]
    if crash == "after_the_block_write":  # BLOCKED już zapisane: uzgadnianie czeka na właściciela, nie dopisuje kodu
        assert "MISMATCH_TIMEOUT" in kills.calls
    else:
        assert_blocked_lost_trade(j2, req)  # uzgadnianie samo doszło do UNMATCHED_CLOSED_DEAL
        assert kills.calls[:1] == ["UNMATCHED_CLOSED_DEAL"] and "MISMATCH_TIMEOUT" in kills.calls
    assert fake.calls.count("open_position") == 1


# --- d. wiele przebiegów z pustą listą pozycji nie zdejmuje blokady --------------------------------------


async def test_reconciliation_never_clears_the_block_while_the_closure_is_unmatched(
    journal, fake, mgr, req, clock, kill_sink, risk_cfg, monkeypatch
):
    accept_then_stop_before_first_check(fake)
    await mgr.submit_entry(req)
    written = spy_states(journal, monkeypatch)
    reconciler = reconciler_for(journal, fake, clock, kill_sink, risk_cfg, mgr)
    for _ in range(5):
        clock.advance(risk_cfg.reconcile_interval_s * 3)
        result = await reconciler.run_once()
        assert result.blocked and not result.skipped
    assert fake.positions == {} and [c.deal_id for c in await fake.closed_since(T0)] == ["DEAL-1"]
    assert_blocked_lost_trade(journal, req)
    assert written["order"] == [] and written["position"] == []  # ani FLAT, ani NOT_PLACED, ani nic innego
    assert "MISMATCH_TIMEOUT" in kill_sink.calls


# --- e. kontrola: naprawdę nie złożone -----------------------------------------------------------------------


async def test_genuinely_not_placed_reads_history_once_after_the_third_check(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    seen = []
    real_closed_since = fake.closed_since

    async def closed_since(since):
        seen.append((since, list(fake.calls)))
        return await real_closed_since(since)

    monkeypatch.setattr(fake, "closed_since", closed_since)
    rec = await mgr.submit_entry(req)
    assert rec.state is OrderState.NOT_PLACED and rec.note == f"NOT_FOUND_AFTER_CHECKS=3 {CAUSE}"
    assert journal.current_position("DE40").state is PositionState.FLAT
    assert seen == [(T0 - HISTORY_MARGIN, ["open_position"] + ["list_positions"] * 3)]
    assert fake.calls.count("closed_since") == 1


# --- f. tylko zamknięcia na naszym instrumencie i z okna od INTENT się liczą ----------------------------------


def _other_epic_closure(fake):
    fake.positions["US-1"] = OpenPosition("US-1", "US500", Side.BUY, Decimal("0.1"), Decimal("6800"), None, T0)
    fake.trigger_stop("US-1", Decimal("6790"))  # ClosedDeal na US500, ts = T0


def _old_closure_from_a_lax_adapter(fake, monkeypatch):
    old = ClosedDeal("OLD-1", "DE40", STOP, "STOP", T0 - HISTORY_MARGIN - timedelta(seconds=1))
    real_closed_since = fake.closed_since

    async def closed_since(since):  # adapter zwraca więcej, niż prosiliśmy
        return [old, *await real_closed_since(since)]

    monkeypatch.setattr(fake, "closed_since", closed_since)


@pytest.mark.parametrize("setup", ["other_epic", "older_than_window"])
async def test_closures_elsewhere_or_before_the_window_do_not_block_not_placed(
    journal, fake, mgr, req, monkeypatch, setup
):
    if setup == "other_epic":
        _other_epic_closure(fake)
    else:
        _old_closure_from_a_lax_adapter(fake, monkeypatch)
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    rec = await mgr.submit_entry(req)
    assert rec.state is OrderState.NOT_PLACED
    assert journal.current_position("DE40").state is PositionState.FLAT
    [history] = payloads(journal, "broker.closed_since")
    assert len(history["response"]) == 1  # obca odpowiedź zapisana w dzienniku, ale nie zablokowała wyniku
