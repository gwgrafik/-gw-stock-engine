"""Testy awarii menedżera zleceń: wejście, rozstrzyganie UNKNOWN i błędy zapisu dziennika.

Niezmienniki pod lupą (spec §6): wiersz INTENT i stan SUBMITTING są trwałe przed wywołaniem brokera; po każdej
niejasności sprawdzamy listę pozycji, nigdy nie wysyłamy ponownie; surowa odpowiedź brokera trafia do dziennika
przed zmianą stanu, którą uzasadnia; każda zmiana stanu przechodzi przez automat stanów.
"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import gwstock.orders.manager as manager_module
from gwstock.broker.capitalcom_trading import MarketClosedRejection
from gwstock.journal import JournalWriteError, OrderRecord, PositionRecord
from gwstock.orders.fsm import check_order_transition, check_position_transition
from gwstock.orders.manager import HISTORY_MARGIN, EntryBlocked, OrderManager, match_entry_positions
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    OpenPosition,
    OrderState,
    PositionState,
    SessionExpired,
    Side,
)

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)  # = clock.now_utc() na starcie fixture `clock`
FILL = Decimal("25782.3")  # BUY po ask z fixture `quote`
STOP = FILL - Decimal("30")
SKEW_BEYOND_TOLERANCE = timedelta(seconds=6)  # tolerancja zegara brokera to 5 s


def order(journal, req):
    return journal.get_order(req.client_id)


def position(journal):
    return journal.current_position("DE40")


def payloads(journal, source):
    return [json.loads(e.payload) for e in journal.events(source)]


def foreign(deal_id, side=Side.BUY, epic="DE40", opened_at=T0):
    return OpenPosition(deal_id, epic, side, Decimal("0.007"), FILL, STOP, opened_at)


def write_failure(*_args, **_kwargs):
    raise JournalWriteError("disk full")


async def hang_forever():
    await asyncio.Event().wait()


TEST_GUARD_S = 2  # prawdziwy czas: błędna implementacja ma przerwać test, a nie zawiesić całą sesję pytest


# --- ścieżka podstawowa --------------------------------------------------------------------------


async def test_intent_row_exists_before_broker_call(journal, fake, mgr, req):
    seen = {}
    fake.on_call("open_position", lambda: seen.setdefault("state", journal.get_order(req.client_id).state))
    fake.on_call("open_position", lambda: seen.setdefault("position", position(journal).state))
    await mgr.submit_entry(req)
    assert seen["state"] == OrderState.SUBMITTING  # INTENT zapisany i przeszedł w SUBMITTING przed wywołaniem
    assert seen["position"] == PositionState.ENTERING


async def test_confirmed_entry_records_broker_fill_and_opens_position(journal, fake, mgr, req, clock):
    rec = await mgr.submit_entry(req)
    assert rec == order(journal, req)
    assert rec.state == OrderState.CONFIRMED
    assert (rec.deal_reference, rec.deal_id, rec.note) == ("REF-1", "DEAL-1", "")
    assert (rec.created_at, rec.updated_at) == (T0, T0)
    assert position(journal) == PositionRecord(
        "DE40", PositionState.OPEN, "DEAL-1", Side.BUY, Decimal("0.007"), FILL, STOP, T0
    )
    assert fake.calls == ["open_position", "confirm"]  # bez listy pozycji, gdy potwierdzenie jest jednoznaczne
    assert clock.elapsed_since_start() == 0


# --- timeout i inne niejasności: UNKNOWN, lista pozycji, nigdy ponowna wysyłka ----------------------


async def test_timeout_then_position_found_is_adopted_not_duplicated(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and fake.calls.count("open_position") == 1
    assert rec.deal_id == "DEAL-1" and rec.deal_reference is None  # odpowiedź (referencja) nie dotarła
    # kod wyniku na początku, przyczyna UNKNOWN zostaje w notatce
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=positions CAUSE=open_position:BrokerTimeout"
    assert position(journal).state == PositionState.OPEN and position(journal).deal_id == "DEAL-1"
    assert fake.calls.count("list_positions") == 1 and clock.elapsed_since_start() == 10


async def test_timeout_without_position_becomes_not_placed_after_three_checks(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.NOT_PLACED and fake.calls.count("list_positions") == 3
    assert clock.elapsed_since_start() >= 30  # trzy zapytania w 30 s
    assert fake.calls[-1] == "closed_since" and fake.calls.count("closed_since") == 1  # historia przed NOT_PLACED
    assert rec.note == "NOT_FOUND_AFTER_CHECKS=3 CAUSE=open_position:BrokerTimeout"
    assert position(journal).state == PositionState.FLAT
    assert fake.calls.count("open_position") == 1 and fake.positions == {}


OTHER_ERRORS = [SessionExpired("401"), BrokerError("HTTP 502"), RuntimeError("boom")]


@pytest.mark.parametrize("exc", OTHER_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("applied", [True, False], ids=["reached_broker", "lost"])
async def test_any_other_open_error_is_unknown_and_resolved_by_position_list(journal, fake, mgr, req, exc, applied):
    fake.fail_next("open_position", exc, applied=applied)
    rec = await mgr.submit_entry(req)
    assert fake.calls.count("open_position") == 1
    if applied:
        assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1"
        assert position(journal).state == PositionState.OPEN
    else:
        assert rec.state == OrderState.NOT_PLACED and fake.calls.count("list_positions") == 3
        assert position(journal).state == PositionState.FLAT
    [raw] = payloads(journal, "broker.open_position")
    assert raw["error"] == type(exc).__name__ and raw["message"] == str(exc)


async def test_confirm_returning_none_is_resolved_from_position_list(journal, fake, mgr, req, monkeypatch):
    async def unknown_reference(deal_reference):  # jak 404 tuż po POST: broker jeszcze nie zna referencji
        fake.calls.append("confirm")
        return None

    monkeypatch.setattr(fake, "confirm", unknown_reference)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1" and rec.deal_reference == "REF-1"
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=positions CAUSE=confirm:NONE"
    assert fake.calls.count("open_position") == 1


async def test_confirm_timeout_is_resolved_by_asking_confirm_again(journal, fake, mgr, req):
    fake.fail_next("confirm", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and (rec.deal_reference, rec.deal_id) == ("REF-1", "DEAL-1")
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm CAUSE=confirm:BrokerTimeout"
    assert fake.calls.count("open_position") == 1 and fake.calls.count("confirm") == 2


async def test_position_stopped_out_before_resolution_is_still_confirmed_from_snapshot(journal, fake, mgr, req):
    # broker wykonał wejście, ale zanim je potwierdziliśmy, stop zamknął pozycję: lista pozycji jest pusta,
    # a migawka `confirm` nadal mówi, co zlecenie wytworzyło (stratę zaksięguje rekoncyliator, nie NOT_PLACED)
    fake.on_call("confirm", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    fake.fail_next("confirm", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1"
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm CAUSE=confirm:BrokerTimeout"
    assert position(journal).state == PositionState.OPEN  # rekoncyliator wykryje zniknięcie i zaksięguje stop


async def test_snapshot_opened_before_intent_is_accepted_when_the_deal_is_listed(journal, fake, mgr, req, monkeypatch):
    # odchyłka zegara brokera: opened_at sprzed INTENT; pozycja z tym deal_id wisi na liście, więc to nasze wejście
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - SKEW_BEYOND_TOLERANCE)

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    fake.fail_next("confirm", BrokerTimeout())
    def skew_listed_position():  # sama lista nie wystarczy do przejęcia (opened_at < created_at): decyduje migawka
        fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], opened_at=T0 - SKEW_BEYOND_TOLERANCE)

    fake.on_call("list_positions", skew_listed_position, once=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm CAUSE=confirm:BrokerTimeout"


async def test_unverifiable_snapshot_blocks_instead_of_guessing(journal, fake, mgr, req, monkeypatch):
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - SKEW_BEYOND_TOLERANCE)

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    fake.fail_next("confirm", BrokerTimeout())
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CONFIRM_UNVERIFIED CAUSE=confirm:BrokerTimeout"
    assert position(journal).state == PositionState.BLOCKED
    assert fake.calls.count("open_position") == 1


async def test_direct_confirmation_is_not_compared_with_intent_time(journal, fake, mgr, req, monkeypatch):
    # odpowiedź na NASZĄ świeżą referencję: odchyłka zegara brokera nie może zamienić wykonania w UNKNOWN
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - SKEW_BEYOND_TOLERANCE)

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and fake.calls == ["open_position", "confirm"]
    assert position(journal).opened_at == T0 - SKEW_BEYOND_TOLERANCE


async def test_confirmation_contradicting_the_order_blocks(journal, fake, mgr, req, monkeypatch):
    real_confirm = fake.confirm

    async def wrong_side(deal_reference):
        return replace(await real_confirm(deal_reference), side=Side.SELL)

    monkeypatch.setattr(fake, "confirm", wrong_side)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CONFIRM_MISMATCH" and rec.deal_reference == "REF-1"
    assert position(journal).state == PositionState.BLOCKED
    assert fake.calls == ["open_position", "confirm"]  # żadnej ponownej wysyłki, żadnego zgadywania
    [event] = payloads(journal, "order.event")
    assert event["event"] == "CONFIRM_MISMATCH" and event["order_id"] == req.client_id


async def test_rejection_found_while_resolving_is_not_placed(journal, fake, mgr, req):
    fake.reject_next_entry("INSUFFICIENT_FUNDS")
    fake.fail_next("confirm", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.NOT_PLACED
    assert rec.note == "REJECTED_ON_CONFIRM=INSUFFICIENT_FUNDS CAUSE=confirm:BrokerTimeout"
    assert position(journal).state == PositionState.FLAT
    assert fake.calls == ["open_position", "confirm", "confirm", "list_positions", "closed_since"]


def _rejected_in_round_1(fake, *, filled):
    """Potwierdzenie: najpierw timeout (UNKNOWN), w 1. rundzie rozstrzygania REJECTED. `filled`: POST mimo to otworzył
    pozycję (sprzeczne dowody), inaczej broker naprawdę nic nie otworzył."""
    if filled:
        fake.fail_next("confirm", BrokerTimeout())
        fake.fail_next("confirm", BrokerRejected("INSUFFICIENT_FUNDS"))
    else:
        fake.reject_next_entry("INSUFFICIENT_FUNDS")
        fake.fail_next("confirm", BrokerTimeout())


REJECTED_IN_RESOLUTION = {
    "listed": (
        lambda fake: _rejected_in_round_1(fake, filled=True),
        OrderState.UNKNOWN, "REJECTED_BUT_LISTED", PositionRecord("DE40", PositionState.BLOCKED),
    ),
    "stopped_out": (
        lambda fake: (
            _rejected_in_round_1(fake, filled=True),
            fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True),
        ),
        OrderState.UNKNOWN, "UNMATCHED_CLOSED_DEAL", PositionRecord("DE40", PositionState.BLOCKED),
    ),
    "history_unreadable": (
        lambda fake: (_rejected_in_round_1(fake, filled=False), fake.fail_next("closed_since", BrokerTimeout())),
        OrderState.UNKNOWN, "CHECKS_INCOMPLETE", PositionRecord("DE40", PositionState.ENTERING),
    ),
    "list_unreadable_every_round": (
        lambda fake: (
            _rejected_in_round_1(fake, filled=False),
            *(fake.fail_next("list_positions", BrokerTimeout()) for _ in range(3)),
        ),
        OrderState.UNKNOWN, "CHECKS_INCOMPLETE", PositionRecord("DE40", PositionState.ENTERING),
    ),
    "clean": (
        lambda fake: _rejected_in_round_1(fake, filled=False),
        OrderState.NOT_PLACED, "REJECTED_ON_CONFIRM=INSUFFICIENT_FUNDS", PositionRecord("DE40", PositionState.FLAT),
    ),
}


@pytest.mark.parametrize(
    ("setup", "state", "code", "pos"), list(REJECTED_IN_RESOLUTION.values()), ids=list(REJECTED_IN_RESOLUTION)
)
async def test_rejected_on_confirm_during_resolution_checks_the_list_and_history_before_not_placed(
    journal, fake, mgr, req, setup, state, code, pos
):
    # REJECTED na referencję w trakcie rozstrzygania nie jest dowodem braku pozycji: POST mógł ją otworzyć
    setup(fake)
    rec = await mgr.submit_entry(req)
    assert rec.state == state and rec.note == f"{code} CAUSE=confirm:BrokerTimeout"
    assert position(journal) == pos and journal.trades() == []
    assert fake.calls.count("open_position") == 1
    if state is OrderState.NOT_PLACED:  # NOT_PLACED tylko po udanym odczycie listy i czystej historii
        assert fake.calls[-2:] == ["list_positions", "closed_since"]
        assert [p["response"] for p in payloads(journal, "broker.closed_since")] == [[]]


async def test_ambiguous_match_blocks_and_never_guesses(journal, fake, mgr, req):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fake.positions["MANUAL-1"] = foreign("MANUAL-1")  # druga pasująca pozycja (np. ręczna) w tym samym oknie
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.deal_id is None
    assert rec.note == "AMBIGUOUS_MATCH CAUSE=open_position:BrokerTimeout"
    assert position(journal) == PositionRecord("DE40", PositionState.BLOCKED)
    assert fake.calls.count("open_position") == 1
    [event] = payloads(journal, "order.event")
    assert event["event"] == "AMBIGUOUS_MATCH" and sorted(event["deal_ids"]) == ["DEAL-1", "MANUAL-1"]


async def test_positions_opened_before_intent_or_not_matching_are_not_adopted(journal, fake, mgr, req):
    fake.positions["OLD-1"] = foreign("OLD-1", opened_at=T0 - SKEW_BEYOND_TOLERANCE)
    fake.positions["SELL-1"] = foreign("SELL-1", side=Side.SELL)
    fake.positions["US-1"] = foreign("US-1", epic="US500")
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.NOT_PLACED and fake.calls.count("list_positions") == 3
    assert position(journal).state == PositionState.FLAT


async def test_failed_check_never_counts_as_proof_of_absence(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    fake.fail_next("list_positions", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CHECKS_INCOMPLETE CAUSE=open_position:BrokerTimeout"
    assert position(journal).state == PositionState.ENTERING  # rekoncyliator rozstrzygnie, gdy broker odpowie
    assert fake.calls.count("list_positions") == 3 and clock.elapsed_since_start() == 30


async def test_expired_session_leaves_order_unknown_not_not_placed(journal, fake, mgr, req):
    fake.expire_session()  # menedżer nie loguje się sam: brak odczytu to brak wiedzy, a nie brak pozycji
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CHECKS_INCOMPLETE CAUSE=open_position:SessionExpired"
    assert fake.calls == ["open_position"] + ["list_positions"] * 3


async def test_failed_check_followed_by_found_position_is_adopted(journal, fake, mgr, req):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fake.fail_next("list_positions", SessionExpired("401"))
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and fake.calls.count("list_positions") == 2


async def test_asyncio_timeout_of_open_position_is_unknown(journal, fake, clock, risk_cfg, req, monkeypatch):
    real_open = fake.open_position

    async def accepted_but_silent(r):  # broker przyjął zlecenie, odpowiedź nigdy nie wraca
        await real_open(r)
        await hang_forever()

    monkeypatch.setattr(fake, "open_position", accepted_but_silent)
    mgr = OrderManager(journal, fake, clock, replace(risk_cfg, submit_timeout_s=0.05))
    rec = await asyncio.wait_for(mgr.submit_entry(req), TEST_GUARD_S)
    assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1"
    assert fake.calls.count("open_position") == 1
    [raw] = payloads(journal, "broker.open_position")
    assert raw["error"] == "TimeoutError"


async def test_asyncio_timeout_without_fill_is_not_placed(journal, fake, clock, risk_cfg, req, monkeypatch):
    async def silent(r):
        fake.calls.append("open_position")
        await hang_forever()

    monkeypatch.setattr(fake, "open_position", silent)
    mgr = OrderManager(journal, fake, clock, replace(risk_cfg, submit_timeout_s=0.05))
    rec = await asyncio.wait_for(mgr.submit_entry(req), TEST_GUARD_S)
    assert rec.state == OrderState.NOT_PLACED and fake.calls.count("open_position") == 1


@pytest.mark.parametrize(
    ("method", "expected_calls"),
    [("open_position", ["open_position"]), ("confirm", ["open_position", "confirm"])],
)
async def test_cancellation_during_broker_call_leaves_submitting(
    journal, fake, mgr, req, monkeypatch, method, expected_calls
):
    started = asyncio.Event()

    async def slow(*_args):
        fake.calls.append(method)
        started.set()
        await hang_forever()

    monkeypatch.setattr(fake, method, slow)
    task = asyncio.create_task(mgr.submit_entry(req))
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait([task, waiter], timeout=TEST_GUARD_S, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    assert started.is_set(), "submit_entry never reached the broker call"
    assert mgr.in_flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not mgr.in_flight
    assert order(journal, req).state == OrderState.SUBMITTING  # rozstrzygnie uzgadnianie po restarcie
    assert position(journal).state == PositionState.ENTERING
    assert fake.calls == expected_calls
    if method == "confirm":  # referencja przetrwała w surowym zdarzeniu (wiersz 8 tabeli awarii)
        assert payloads(journal, "broker.open_position")[0]["response"] == {"deal_reference": "REF-1"}


async def test_resolve_unknown_accepts_only_unknown_orders(journal, mgr, req):
    with pytest.raises(ValueError, match="UNKNOWN"):
        await mgr.resolve_unknown(unresolved(OrderState.SUBMITTING))


@pytest.mark.parametrize(
    "pos",
    [
        None,
        PositionRecord("DE40", PositionState.FLAT),
        PositionRecord("DE40", PositionState.BLOCKED),
        PositionRecord("DE40", PositionState.OPEN, "DEAL-7", Side.BUY, Decimal("0.007"), FILL, STOP, T0),
        PositionRecord("DE40", PositionState.EXITING, "DEAL-7", Side.BUY, Decimal("0.007"), FILL, STOP, T0),
    ],
    ids=["missing", "FLAT", "BLOCKED", "OPEN", "EXITING"],
)
async def test_resolve_unknown_requires_an_entering_position(journal, fake, mgr, clock, pos):
    # BLOCKED to decyzja uzgadniania lub właściciela; OPEN po awarii między zapisami to już przejęta pozycja
    rec = unresolved(OrderState.UNKNOWN)
    journal.insert_order(rec)
    if pos is not None:
        journal.upsert_position(pos)
    with pytest.raises(ValueError, match="ENTERING"):
        await mgr.resolve_unknown(rec)
    assert clock.elapsed_since_start() == 0 and fake.calls == []  # ani czekania, ani brokera
    assert journal.get_order(rec.order_id) == rec and journal.current_position("DE40") == pos
    assert not mgr.in_flight


@pytest.mark.parametrize("stored", [OrderState.NOT_PLACED, OrderState.CONFIRMED, None], ids=str)
async def test_resolve_unknown_rereads_the_order_and_refuses_a_stale_record(journal, fake, mgr, clock, stored):
    # wywołujący trzyma kopię UNKNOWN, a dziennik już ją rozstrzygnął (albo nie zna): nie wolno pisać po starym stanie
    stale = unresolved(OrderState.UNKNOWN)
    if stored is not None:
        journal.insert_order(replace(stale, state=stored))
    journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
    before = journal.get_order(stale.order_id), journal.current_position("DE40")
    with pytest.raises(ValueError, match="expected UNKNOWN"):
        await mgr.resolve_unknown(stale)
    assert clock.elapsed_since_start() == 0 and fake.calls == []  # ani czekania, ani brokera
    assert (journal.get_order(stale.order_id), journal.current_position("DE40")) == before
    assert not mgr.in_flight


async def test_resolve_unknown_works_on_the_journal_record_not_on_the_passed_copy(journal, fake, mgr, req):
    for _ in range(4):  # potwierdzenie przy wysyłce i w trzech rundach
        fake.fail_next("confirm", BrokerTimeout())
    for _ in range(3):
        fake.fail_next("list_positions", BrokerTimeout())
    first = await mgr.submit_entry(req)  # UNKNOWN CHECKS_INCOMPLETE z referencją REF-1, pozycja ENTERING
    assert first.state == OrderState.UNKNOWN and first.deal_reference == "REF-1"
    assert first.note == "CHECKS_INCOMPLETE CAUSE=confirm:BrokerTimeout"
    assert position(journal).state == PositionState.ENTERING
    fake.calls.clear()
    old_copy = replace(first, deal_reference=None, note="CAUSE=open_position:BrokerTimeout")  # kopia sprzed odpowiedzi
    rec = await mgr.resolve_unknown(old_copy)
    assert rec.state == OrderState.CONFIRMED
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm CAUSE=confirm:BrokerTimeout"
    assert fake.calls[0] == "confirm"  # referencja z dziennika, nie z przekazanej kopii


async def test_second_resolve_after_ambiguous_match_is_refused(journal, fake, mgr, req, clock):
    _ambiguous(fake)
    rec = await mgr.submit_entry(req)
    calls, elapsed = list(fake.calls), clock.elapsed_since_start()
    with pytest.raises(ValueError, match="BLOCKED"):
        await mgr.resolve_unknown(rec)  # nie wolno przejąć ręcznej pozycji wbrew decyzji BLOCKED
    assert fake.calls == calls and clock.elapsed_since_start() == elapsed
    assert order(journal, req) == rec and position(journal).state == PositionState.BLOCKED


async def test_resolve_after_crash_between_writes_never_unbooks_a_filled_trade(journal, fake, mgr):
    # awaria po zapisie pozycji OPEN, przed zapisem CONFIRMED; potem stop zamknął pozycję u brokera
    rec = unresolved(OrderState.UNKNOWN)
    journal.insert_order(rec)
    opened = PositionRecord("DE40", PositionState.OPEN, "DEAL-7", Side.BUY, Decimal("0.007"), FILL, STOP, T0)
    journal.upsert_position(opened)
    with pytest.raises(ValueError, match="OPEN"):
        await mgr.resolve_unknown(rec)
    assert journal.current_position("DE40") == opened and journal.get_order(rec.order_id) == rec
    assert fake.calls == []


async def test_resolve_unknown_resolves_an_unknown_entering_order(journal, fake, mgr, clock):
    rec = unresolved(OrderState.UNKNOWN)
    journal.insert_order(rec)
    journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
    fake.positions["DEAL-7"] = foreign("DEAL-7")
    seen = []
    fake.on_call("list_positions", lambda: seen.append(mgr.in_flight))
    out = await mgr.resolve_unknown(rec)
    assert out.state == OrderState.CONFIRMED and out.deal_id == "DEAL-7"
    assert seen == [True] and not mgr.in_flight


async def test_resolve_unknown_is_refused_while_an_entry_is_in_flight(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    real_list = fake.list_positions
    refused = []

    async def list_and_probe():  # drugi rozstrzygający w trakcie rozstrzygania tego samego wejścia
        if not refused:
            with pytest.raises(RuntimeError, match="in flight"):
                await mgr.resolve_unknown(journal.get_order(req.client_id))
            refused.append(mgr.in_flight)
        return await real_list()

    monkeypatch.setattr(fake, "list_positions", list_and_probe)
    rec = await mgr.submit_entry(req)
    assert refused == [True] and rec.state == OrderState.NOT_PLACED  # odmowa nie zgasiła flagi ani nie zmieniła wyniku
    assert fake.calls.count("list_positions") == 3 and not mgr.in_flight


# --- odrzucenia: stany końcowe z powodem ------------------------------------------------------------


async def test_rejected_entry_is_terminal_and_logged(journal, fake, mgr, req):
    rec = await mgr.submit_entry(replace(req, stop_distance=Decimal("1")))  # < min_stop_distance 2.6
    assert rec.state == OrderState.REJECTED and rec.note == "MIN_STOP_DISTANCE"
    assert position(journal).state == PositionState.FLAT
    assert fake.calls == ["open_position"]
    [raw] = payloads(journal, "broker.open_position")
    assert raw["error"] == "BrokerRejected" and raw["reason"] == "MIN_STOP_DISTANCE"


async def test_deferred_rejection_on_confirm_is_terminal_and_logged(journal, fake, mgr, req):
    fake.reject_next_entry("INSUFFICIENT_FUNDS")  # POST 200 z referencją, dealStatus REJECTED w potwierdzeniu
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.REJECTED and rec.note == "INSUFFICIENT_FUNDS" and rec.deal_reference == "REF-1"
    assert position(journal).state == PositionState.FLAT
    # REJECTED dopiero po pustej liście i czystej historii
    assert fake.calls == ["open_position", "confirm", "list_positions", "closed_since"]
    [ack] = payloads(journal, "broker.open_position")
    [raw] = payloads(journal, "broker.confirm")
    [listed] = payloads(journal, "broker.list_positions")
    [history] = payloads(journal, "broker.closed_since")
    assert ack["response"] == {"deal_reference": "REF-1"}
    assert raw["error"] == "BrokerRejected" and raw["reason"] == "INSUFFICIENT_FUNDS"
    assert listed["response"] == [] and listed["check"] == "rejected_confirm"
    assert history["response"] == [] and history["check"] == "rejected_confirm"
    assert history["since"] == (T0 - HISTORY_MARGIN).isoformat()


def _filled_rejected_and_stopped_before_the_list(fake):
    """Repro z przeglądu G4 (I-1): POST otworzył DEAL-1, potwierdzenie mówi REJECTED, a stop zamyka pozycję tuż przed
    odczytem listy."""
    fake.fail_next("confirm", BrokerRejected("INSUFFICIENT_FUNDS"))
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)


EMPTY_LIST_AFTER_REJECTION = {
    "stopped_out": (
        _filled_rejected_and_stopped_before_the_list,
        OrderState.UNKNOWN, "UNMATCHED_CLOSED_DEAL CAUSE=confirm:BrokerRejected",
        PositionRecord("DE40", PositionState.BLOCKED),
    ),
    "history_unreadable": (
        lambda fake: (fake.reject_next_entry("INSUFFICIENT_FUNDS"), fake.fail_next("closed_since", BrokerTimeout())),
        OrderState.UNKNOWN, "CAUSE=confirm:BrokerRejected_UNVERIFIED", PositionRecord("DE40", PositionState.ENTERING),
    ),
    "clean": (
        lambda fake: fake.reject_next_entry("INSUFFICIENT_FUNDS"),
        OrderState.REJECTED, "INSUFFICIENT_FUNDS", PositionRecord("DE40", PositionState.FLAT),
    ),
}


@pytest.mark.parametrize(
    ("setup", "state", "note", "pos"), list(EMPTY_LIST_AFTER_REJECTION.values()), ids=list(EMPTY_LIST_AFTER_REJECTION)
)
async def test_a_rejected_confirm_with_an_empty_list_reads_the_history_before_rejecting(
    journal, fake, mgr, req, monkeypatch, setup, state, note, pos
):
    # pusta lista nie wystarcza: wejście mogło się wykonać i zostać wybite, zanim przeczytaliśmy listę (jak przed
    # NOT_PLACED); zamknięcie → BLOCKED jak zgubiona transakcja, nieczytelna historia → UNKNOWN, czysta → REJECTED
    setup(fake)
    log = spy_writes(journal, monkeypatch)
    rec = await mgr.submit_entry(req)
    assert rec.state == state and rec.note == note and rec.deal_reference == "REF-1"
    assert position(journal) == pos and journal.trades() == []
    assert fake.calls == ["open_position", "confirm", "list_positions", "closed_since"]
    [history] = payloads(journal, "broker.closed_since")
    assert history["check"] == "rejected_confirm" and history["since"] == (T0 - HISTORY_MARGIN).isoformat()
    raw = ["broker.open_position", "broker.confirm", "broker.list_positions", "broker.closed_since"]
    assert all(set(raw) <= set(sources) for _, _, sources in log[2:])  # surowa historia przed decyzją
    events = payloads(journal, "order.event")
    if state is OrderState.REJECTED:
        assert events == []
    elif pos.state is PositionState.BLOCKED:
        [event] = events
        assert event["event"] == "UNMATCHED_CLOSED_DEAL" and event["deal_ids"] == ["DEAL-1"]
        assert event["reason"] == "INSUFFICIENT_FUNDS"
    else:
        [event] = events
        assert event["event"] == "REJECTION_UNVERIFIED" and event["stage"] == "history"
        assert event["reason"] == "INSUFFICIENT_FUNDS" and history["error"] == "BrokerTimeout"


@pytest.mark.parametrize(
    "exc", [BrokerRejected("INSUFFICIENT_FUNDS"), MarketClosedRejection("error.market.closed")],
    ids=["rejected", "market_closed_rejection"],
)
async def test_a_rejected_confirm_with_our_position_listed_blocks_instead_of_rejecting(
    journal, fake, mgr, req, monkeypatch, exc
):
    # broker przyjął POST i otworzył pozycję, a potwierdzenie mówi REJECTED: dowody sprzeczne, nie zgadujemy
    fake.fail_next("confirm", exc)
    log = spy_writes(journal, monkeypatch)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "REJECTED_BUT_LISTED" and rec.deal_reference == "REF-1"
    assert position(journal) == PositionRecord("DE40", PositionState.BLOCKED)
    assert fake.calls == ["open_position", "confirm", "list_positions"] and list(fake.positions) == ["DEAL-1"]
    [event] = payloads(journal, "order.event")
    assert event["event"] == "REJECTED_BUT_LISTED" and event["deal_ids"] == ["DEAL-1"]
    raw = ["broker.open_position", "broker.confirm", "broker.list_positions", "order.event"]
    assert log[2:] == [("position", PositionState.BLOCKED, raw), ("order", OrderState.UNKNOWN, raw)]


async def test_a_rejected_confirm_with_an_unreadable_position_list_stays_unknown(
    journal, fake, mgr, req, reconciler, clock, kill_sink
):
    fake.reject_next_entry("INSUFFICIENT_FUNDS")
    fake.fail_next("list_positions", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CAUSE=confirm:BrokerRejected_UNVERIFIED"
    assert rec.deal_reference == "REF-1"
    assert position(journal).state == PositionState.ENTERING  # nieudany odczyt nie jest dowodem braku pozycji
    assert fake.calls == ["open_position", "confirm", "list_positions"]  # bez ponownej wysyłki, bez rund menedżera
    [error] = payloads(journal, "broker.list_positions")
    assert error["error"] == "BrokerTimeout" and error["check"] == "rejected_confirm"
    [event] = payloads(journal, "order.event")
    assert event["event"] == "REJECTION_UNVERIFIED" and event["reason"] == "INSUFFICIENT_FUNDS"

    for _ in range(3):  # rozstrzyga uzgadnianie: trzy puste odczyty w 30 s i czysta historia
        clock.advance(15)
        await reconciler.run_once()
    final = order(journal, req)
    assert final.state == OrderState.NOT_PLACED
    assert final.note == "NOT_FOUND_BY_RECONCILER READS=3 CAUSE=confirm:BrokerRejected_UNVERIFIED"
    assert position(journal).state == PositionState.FLAT and fake.calls.count("open_position") == 1


async def test_market_closed_is_rejected_and_position_stays_flat(journal, fake, mgr, req):
    fake.set_market_closed(True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.REJECTED and rec.note == "MARKET_CLOSED"
    assert position(journal).state == PositionState.FLAT
    assert fake.calls == ["open_position"]


# --- wielkość potwierdzona przez brokera ------------------------------------------------------------


@pytest.mark.parametrize("timeout_first", [False, True], ids=["confirmed", "adopted"])
async def test_broker_confirms_different_size_records_actual_size(journal, fake, mgr, req, timeout_first):
    fake.fill_size_factor = Decimal("0.5")
    if timeout_first:
        fake.fail_next("open_position", BrokerTimeout(), applied=True)
    rec = await mgr.submit_entry(req)
    assert journal.current_position("DE40").size == req.size * Decimal("0.5")
    assert rec.state == OrderState.CONFIRMED
    assert rec.note.endswith("FILLED_SIZE=0.0035 REQUESTED=0.007")
    assert order(journal, req).size == req.size  # zlecenie zachowuje wielkość żądaną


# --- surowe odpowiedzi przed przetworzeniem ----------------------------------------------------------


def spy_writes(journal, monkeypatch):
    """Rejestruje, jakie surowe zdarzenia były w dzienniku w chwili każdej zmiany zlecenia i pozycji."""
    log = []
    update_order, upsert_position = journal.update_order, journal.upsert_position

    def on_update(order_id, **fields):
        log.append(("order", fields.get("state"), [e.source for e in journal.events()]))
        return update_order(order_id, **fields)

    def on_upsert(p):
        log.append(("position", p.state, [e.source for e in journal.events()]))
        return upsert_position(p)

    monkeypatch.setattr(journal, "update_order", on_update)
    monkeypatch.setattr(journal, "upsert_position", on_upsert)
    return log


async def test_ack_and_confirmation_are_journaled_before_the_updates_they_justify(journal, fake, mgr, req, monkeypatch):
    log = spy_writes(journal, monkeypatch)
    await mgr.submit_entry(req)
    raw = ["broker.open_position", "broker.confirm"]
    assert log == [
        ("position", PositionState.ENTERING, []),
        ("order", OrderState.SUBMITTING, []),
        ("position", PositionState.OPEN, raw),
        ("order", OrderState.CONFIRMED, raw),
    ]
    [confirm] = payloads(journal, "broker.confirm")
    assert confirm["order_id"] == req.client_id and confirm["deal_reference"] == "REF-1"
    assert confirm["response"]["deal_id"] == "DEAL-1" and confirm["response"]["size"] == "0.007"


async def test_positions_are_journaled_before_adoption(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    log = spy_writes(journal, monkeypatch)
    await mgr.submit_entry(req)
    after_check = ["broker.open_position", "broker.list_positions"]
    assert log[2:] == [
        ("order", OrderState.UNKNOWN, ["broker.open_position"]),
        ("position", PositionState.OPEN, after_check),
        ("order", OrderState.CONFIRMED, after_check),
    ]
    [check] = payloads(journal, "broker.list_positions")
    assert check["attempt"] == 1 and [p["deal_id"] for p in check["response"]] == ["DEAL-1"]


CHECKED = ["broker.open_position"] + ["broker.list_positions"] * 3 + ["broker.closed_since"]
REJECTION_CHECKED = ["broker.open_position", "broker.confirm", "broker.list_positions", "broker.closed_since"]


@pytest.mark.parametrize(
    ("scenario", "expected_tail"),
    [
        (
            lambda fake: fake.reject_next_entry("INSUFFICIENT_FUNDS"),
            [
                ("position", PositionState.FLAT, REJECTION_CHECKED),
                ("order", OrderState.REJECTED, REJECTION_CHECKED),
            ],
        ),
        (
            lambda fake: fake.fail_next("open_position", BrokerTimeout()),
            [
                ("order", OrderState.UNKNOWN, ["broker.open_position"]),
                ("position", PositionState.FLAT, CHECKED),
                ("order", OrderState.NOT_PLACED, CHECKED),
            ],
        ),
    ],
    ids=["rejected", "not_placed"],
)
async def test_position_is_written_before_the_order_leaves_the_unresolved_set(
    journal, fake, mgr, req, monkeypatch, scenario, expected_tail
):
    # zapis zlecenia jest punktem zatwierdzenia: awaria między zapisami zostawia zlecenie nierozstrzygnięte
    scenario(fake)
    log = spy_writes(journal, monkeypatch)
    await mgr.submit_entry(req)
    assert log[2:] == expected_tail


async def test_every_raw_event_carries_engine_clock_time(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    await mgr.submit_entry(req)
    times = [(e.source, e.ts) for e in journal.events()]
    assert times == [
        ("broker.open_position", T0),
        ("broker.list_positions", T0 + timedelta(seconds=10)),
        ("broker.list_positions", T0 + timedelta(seconds=20)),
        ("broker.list_positions", T0 + timedelta(seconds=30)),
        ("broker.closed_since", T0 + timedelta(seconds=30)),
    ]


# --- automat stanów: każda zmiana sprawdzona ----------------------------------------------------------


def _timeout_found(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)


def _timeout_lost(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)


def _ambiguous(fake):
    _timeout_found(fake)
    fake.positions["MANUAL-1"] = foreign("MANUAL-1")


SCENARIOS = {
    "confirmed": lambda fake: None,
    "timeout_found": _timeout_found,
    "timeout_lost": _timeout_lost,
    "rejected": lambda fake: fake.fail_next("open_position", BrokerRejected("MIN_SIZE")),
    "deferred_rejection": lambda fake: fake.reject_next_entry("INSUFFICIENT_FUNDS"),
    "market_closed": lambda fake: fake.set_market_closed(True),
    "ambiguous": _ambiguous,
    "confirm_timeout": lambda fake: fake.fail_next("confirm", BrokerTimeout()),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS.values()), ids=list(SCENARIOS))
async def test_every_state_change_is_checked_by_the_fsm_right_before_it_is_written(
    journal, fake, mgr, req, monkeypatch, scenario
):
    log = []

    def check_order(old, new):
        log.append(("check", "order", old, new))
        check_order_transition(old, new)

    def check_position(old, new):
        log.append(("check", "position", old, new))
        check_position_transition(old, new)

    update_order, upsert_position = journal.update_order, journal.upsert_position

    def on_update(order_id, **fields):
        if "state" in fields:
            log.append(("write", "order", journal.get_order(order_id).state, fields["state"]))
        return update_order(order_id, **fields)

    def on_upsert(p):
        current = journal.current_position(p.epic)
        log.append(("write", "position", PositionState.FLAT if current is None else current.state, p.state))
        return upsert_position(p)

    monkeypatch.setattr(manager_module, "check_order_transition", check_order)
    monkeypatch.setattr(manager_module, "check_position_transition", check_position)
    monkeypatch.setattr(journal, "update_order", on_update)
    monkeypatch.setattr(journal, "upsert_position", on_upsert)
    scenario(fake)
    await mgr.submit_entry(req)
    checks, writes = log[0::2], log[1::2]
    assert len(log) >= 4 and len(checks) == len(writes)
    for check, write in zip(checks, writes):
        assert check[0] == "check" and write[0] == "write" and check[1:] == write[1:]  # ta sama krawędź, tuż przed


# --- ochrona przed podwójnym wejściem (wyłącznie dziennik) --------------------------------------------


def unresolved(state, order_id="gws-20261102-DE40-9-entry", epic="DE40"):
    return OrderRecord(order_id, epic, Side.BUY, Decimal("0.007"), Decimal("30"), state, None, None, T0, T0)


@pytest.mark.parametrize("state", [OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN])
async def test_no_second_entry_while_unknown(journal, fake, mgr, req, state):
    journal.insert_order(unresolved(state))
    with pytest.raises(EntryBlocked) as info:
        await mgr.submit_entry(req)
    assert info.value.reason == "UNRESOLVED_ORDER"
    assert fake.calls == [] and order(journal, req) is None


async def test_second_entry_after_ambiguous_resolution_is_blocked(journal, fake, mgr, req):
    _ambiguous(fake)
    await mgr.submit_entry(req)
    with pytest.raises(EntryBlocked):
        await mgr.submit_entry(replace(req, client_id="gws-20261102-DE40-2-entry"))
    assert fake.calls.count("open_position") == 1


@pytest.mark.parametrize(
    "state", [PositionState.ENTERING, PositionState.OPEN, PositionState.EXITING, PositionState.BLOCKED]
)
async def test_entry_is_blocked_while_position_is_not_flat(journal, fake, mgr, req, state):
    journal.upsert_position(PositionRecord("DE40", state))
    with pytest.raises(EntryBlocked) as info:
        await mgr.submit_entry(req)
    assert info.value.reason == "POSITION_NOT_FLAT"
    assert fake.calls == [] and order(journal, req) is None


async def test_terminal_order_and_flat_position_do_not_block(journal, fake, mgr, req):
    fake.reject_next_entry("INSUFFICIENT_FUNDS")
    assert (await mgr.submit_entry(req)).state == OrderState.REJECTED
    rec = await mgr.submit_entry(replace(req, client_id="gws-20261102-DE40-2-entry"))
    assert rec.state == OrderState.CONFIRMED  # limit wejść dziennych to sprawa bramki ryzyka, nie menedżera


async def test_unresolved_order_on_another_instrument_does_not_block(journal, fake, mgr, req):
    journal.insert_order(unresolved(OrderState.UNKNOWN, epic="US500"))
    assert (await mgr.submit_entry(req)).state == OrderState.CONFIRMED


async def test_client_id_is_never_reused(journal, fake, mgr, req):
    fake.set_market_closed(True)
    await mgr.submit_entry(req)
    fake.set_market_closed(False)
    with pytest.raises(EntryBlocked) as info:
        await mgr.submit_entry(req)
    assert info.value.reason == "DUPLICATE_CLIENT_ID"
    assert fake.calls.count("open_position") == 1


async def test_entry_without_stop_is_never_sent(journal, fake, mgr, req):
    with pytest.raises(ValueError, match="stop"):
        await mgr.submit_entry(replace(req, stop_distance=Decimal("0")))
    assert fake.calls == [] and order(journal, req) is None


def test_submit_timeout_must_be_positive(journal, fake, clock, risk_cfg):
    with pytest.raises(ValueError, match="submit_timeout_s"):
        OrderManager(journal, fake, clock, replace(risk_cfg, submit_timeout_s=0))


# --- błędy zapisu dziennika -----------------------------------------------------------------------


async def test_journal_write_failure_means_no_order_is_sent(journal, fake, mgr, req, monkeypatch):  # Review Focus 3
    monkeypatch.setattr(journal, "insert_order", lambda *_: (_ for _ in ()).throw(JournalWriteError()))
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert "open_position" not in fake.calls
    assert position(journal) is None  # pozycja nadal FLAT (nigdy nie zapisana)


async def test_entering_write_failure_means_no_order_is_sent(journal, fake, mgr, req, monkeypatch):
    monkeypatch.setattr(journal, "upsert_position", write_failure)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == [] and order(journal, req).state == OrderState.INTENT


async def test_submitting_write_failure_means_no_order_is_sent(journal, fake, mgr, req, monkeypatch):
    monkeypatch.setattr(journal, "update_order", write_failure)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == [] and order(journal, req).state == OrderState.INTENT


async def test_write_failure_after_broker_call_propagates_without_retry(journal, fake, mgr, req, monkeypatch):
    update_order = journal.update_order

    def fail_on_confirmed(order_id, **fields):
        if fields.get("state") == OrderState.CONFIRMED:
            raise JournalWriteError("disk full")
        return update_order(order_id, **fields)

    monkeypatch.setattr(journal, "update_order", fail_on_confirmed)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls.count("open_position") == 1 and fake.calls == ["open_position", "confirm"]
    assert order(journal, req).state == OrderState.SUBMITTING  # po restarcie: jak UNKNOWN, przejęcie z listy pozycji
    assert position(journal).state == PositionState.OPEN  # pozycja zapisana przed zamknięciem zlecenia


def fail_raw_once(journal, monkeypatch, source):
    """`append_raw` zawodzi RAZ (dla danego źródła), potem działa: ponowienie lub połknięcie błędu byłoby widać."""
    real = journal.append_raw
    armed = [True]

    def append_raw(src, payload, ts):
        if armed[0] and src == source:
            armed[0] = False
            raise JournalWriteError("disk full (once)")
        return real(src, payload, ts)

    monkeypatch.setattr(journal, "append_raw", append_raw)


async def test_raw_ack_write_failure_stops_before_confirm(journal, fake, mgr, req, monkeypatch):
    fail_raw_once(journal, monkeypatch, "broker.open_position")
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position"]  # odpowiedzi nie przetwarzamy, dopóki nie jest zapisana
    assert order(journal, req).state == OrderState.SUBMITTING and not mgr.in_flight


async def test_raw_error_write_failure_in_the_handler_stops_everything(journal, fake, mgr, req, monkeypatch):
    # wiersz 6a: zapis surowego błędu (wewnątrz `except`) zawodzi → koniec, bez UNKNOWN i bez odczytów
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fail_raw_once(journal, monkeypatch, "broker.open_position")
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position"]
    assert order(journal, req).state == OrderState.SUBMITTING


async def test_raw_positions_write_failure_stops_resolution(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fail_raw_once(journal, monkeypatch, "broker.list_positions")
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position", "list_positions"]  # błąd zapisu to nie nieudany odczyt: brak 2. rundy
    assert order(journal, req).state == OrderState.UNKNOWN and position(journal).state == PositionState.ENTERING


async def test_unknown_write_failure_after_timeout_stops_before_any_check(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    update_order = journal.update_order

    def fail_on_unknown(order_id, **fields):
        if fields.get("state") == OrderState.UNKNOWN:
            raise JournalWriteError("disk full")
        return update_order(order_id, **fields)

    monkeypatch.setattr(journal, "update_order", fail_on_unknown)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position"]
    assert order(journal, req).state == OrderState.SUBMITTING


# --- znaczniki czasu zleceń ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("applied", "final", "seconds"), [(True, OrderState.CONFIRMED, 10), (False, OrderState.NOT_PLACED, 30)]
)
async def test_updated_at_follows_the_engine_clock(journal, fake, mgr, req, monkeypatch, applied, final, seconds):
    fake.fail_next("open_position", BrokerTimeout(), applied=applied)
    written = []
    update_order = journal.update_order

    def on_update(order_id, **fields):
        rec = update_order(order_id, **fields)
        written.append((rec.state, rec.updated_at))
        return rec

    monkeypatch.setattr(journal, "update_order", on_update)
    rec = await mgr.submit_entry(req)
    assert written == [
        (OrderState.SUBMITTING, T0),
        (OrderState.UNKNOWN, T0),
        (final, T0 + timedelta(seconds=seconds)),
    ]
    assert rec.created_at == T0


# --- flaga in_flight (bramka dla uzgadniania) -----------------------------------------------------------


async def test_in_flight_is_true_only_while_an_entry_is_being_submitted(journal, fake, mgr, req):
    seen = []
    fake.fail_next("confirm", BrokerTimeout())
    for method in ("open_position", "confirm", "list_positions"):
        fake.on_call(method, lambda: seen.append(mgr.in_flight))
    assert not mgr.in_flight
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED
    assert seen == [True, True, True, True] and not mgr.in_flight  # open, confirm, confirm (runda 1), lista


@pytest.mark.parametrize("running", ["entry_on_another_epic", "resolution"])
async def test_submit_entry_refuses_while_another_call_is_in_flight(journal, fake, mgr, req, monkeypatch, running):
    # drugi piszący naraz złamałby `in_flight` (jego `finally` zgasiłby flagę pierwszego); ochrona dziennika nie łapie
    # wejścia na innym instrumencie, więc odmowa musi przyjść przed czymkolwiek innym
    other = replace(req, client_id="gws-20261102-US500-2-entry", epic="US500")
    method = "open_position" if running == "entry_on_another_epic" else "list_positions"
    real = getattr(fake, method)
    probes = []

    async def call_and_probe(*args):
        if not probes:
            probes.append("probed")
            with pytest.raises(RuntimeError, match="in flight"):
                await mgr.submit_entry(other)
            probes.append(mgr.in_flight)  # odmowa nie gasi flagi trwającego wywołania
        return await real(*args)

    monkeypatch.setattr(fake, method, call_and_probe)
    if running == "entry_on_another_epic":
        rec = await mgr.submit_entry(req)
    else:
        unknown = unresolved(OrderState.UNKNOWN)
        journal.insert_order(unknown)
        journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
        rec = await mgr.resolve_unknown(unknown)
    assert probes == ["probed", True] and not mgr.in_flight
    assert rec.state in (OrderState.CONFIRMED, OrderState.NOT_PLACED)
    assert journal.get_order(other.client_id) is None and journal.current_position("US500") is None  # nic nie zapisano
    assert fake.calls.count("open_position") == (1 if running == "entry_on_another_epic" else 0)


async def test_in_flight_is_never_set_when_the_guard_refuses(journal, fake, mgr, req):
    journal.insert_order(unresolved(OrderState.UNKNOWN))
    with pytest.raises(EntryBlocked):
        await mgr.submit_entry(req)
    assert not mgr.in_flight


@pytest.mark.parametrize("stage", ["intent", "confirmed"])
async def test_in_flight_is_cleared_after_a_journal_write_error(journal, fake, mgr, req, monkeypatch, stage):
    if stage == "intent":
        monkeypatch.setattr(journal, "insert_order", write_failure)
    else:
        update_order = journal.update_order

        def fail_on_confirmed(order_id, **fields):
            if fields.get("state") == OrderState.CONFIRMED:
                raise JournalWriteError("disk full")
            return update_order(order_id, **fields)

        monkeypatch.setattr(journal, "update_order", fail_on_confirmed)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert not mgr.in_flight


# --- dopasowanie pozycji do wejścia (wspólne z uzgadnianiem) ---------------------------------------------


@pytest.mark.parametrize(
    ("offset_s", "matches"), [(-6, False), (-5, True), (0, True), (3600, True)], ids=["-6s", "-5s", "0s", "+1h"]
)
def test_match_entry_positions_tolerates_five_seconds_of_broker_clock_skew(offset_s, matches):
    pos = foreign("DEAL-1", opened_at=T0 + timedelta(seconds=offset_s))
    assert match_entry_positions(unresolved(OrderState.UNKNOWN), [pos]) == ([pos] if matches else [])


def test_match_entry_positions_requires_same_epic_and_side():
    rec = unresolved(OrderState.UNKNOWN)
    assert match_entry_positions(rec, [foreign("S-1", side=Side.SELL), foreign("U-1", epic="US500")]) == []


def test_match_entry_positions_returns_all_matches_in_broker_order():
    rec = unresolved(OrderState.UNKNOWN)
    listed = [foreign("B-2"), foreign("X-1", side=Side.SELL), foreign("A-1", opened_at=T0 + timedelta(seconds=1))]
    assert match_entry_positions(rec, listed) == [listed[0], listed[2]]
    assert match_entry_positions(rec, ()) == []


async def test_position_reported_slightly_before_intent_is_adopted(journal, fake, mgr, req):
    # zegar brokera 5 s za naszym (albo pełne sekundy): po utracie odpowiedzi to nadal nasze wykonanie
    fake.on_call(
        "list_positions",
        lambda: fake.positions.update(
            {"DEAL-1": replace(fake.positions["DEAL-1"], opened_at=T0 - timedelta(seconds=5))}
        ),
        once=True,
    )
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1"


async def test_snapshot_reported_slightly_before_intent_is_accepted(journal, fake, mgr, req, monkeypatch):
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        return replace(await real_confirm(deal_reference), opened_at=T0 - timedelta(seconds=5))

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    fake.fail_next("confirm", BrokerTimeout())
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)  # nie ma jej już na liście
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.note.startswith("ADOPTED_AFTER_UNKNOWN VIA=confirm")


# --- historia zamknięć przed NOT_PLACED (regresja utraconej transakcji: tests/fault/test_lost_trade_regression.py) --


@pytest.mark.parametrize(
    "exc", [BrokerError("HTTP 502"), BrokerTimeout(), SessionExpired("401"), RuntimeError("boom")],
    ids=lambda e: type(e).__name__,
)
async def test_history_read_failure_keeps_the_order_unknown(journal, fake, mgr, req, exc):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    fake.fail_next("closed_since", exc)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CHECKS_INCOMPLETE CAUSE=open_position:BrokerTimeout"
    assert position(journal).state == PositionState.ENTERING and not mgr.in_flight
    [error] = payloads(journal, "broker.closed_since")
    assert error["error"] == type(exc).__name__ and error["since"] == (T0 - HISTORY_MARGIN).isoformat()
    [event] = payloads(journal, "order.event")
    assert event["event"] == "CHECKS_INCOMPLETE" and event["history_read"] is False


async def test_history_read_hanging_past_the_timeout_keeps_the_order_unknown(
    journal, fake, clock, risk_cfg, req, monkeypatch
):
    async def silent(since):
        fake.calls.append("closed_since")
        await hang_forever()

    monkeypatch.setattr(fake, "closed_since", silent)
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    mgr = OrderManager(journal, fake, clock, replace(risk_cfg, submit_timeout_s=0.05))
    rec = await asyncio.wait_for(mgr.submit_entry(req), TEST_GUARD_S)
    assert rec.state == OrderState.UNKNOWN and rec.note.split()[0] == "CHECKS_INCOMPLETE"
    assert payloads(journal, "broker.closed_since")[0]["error"] == "TimeoutError"


async def test_closure_on_our_epic_blocks_instead_of_not_placed(journal, fake, mgr, req):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "UNMATCHED_CLOSED_DEAL CAUSE=open_position:BrokerTimeout"
    assert position(journal) == PositionRecord("DE40", PositionState.BLOCKED) and journal.trades() == []
    [event] = payloads(journal, "order.event")
    assert event["event"] == "UNMATCHED_CLOSED_DEAL" and event["deal_ids"] == ["DEAL-1"]


async def test_history_is_journaled_before_the_block_it_justifies(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    log = spy_writes(journal, monkeypatch)
    await mgr.submit_entry(req)
    sources = CHECKED + ["order.event"]
    assert log[-2:] == [("position", PositionState.BLOCKED, sources), ("order", None, sources)]


async def test_raw_history_write_failure_stops_before_any_decision(journal, fake, mgr, req, monkeypatch):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    fail_raw_once(journal, monkeypatch, "broker.closed_since")
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position"] + ["list_positions"] * 3 + ["closed_since"]
    assert order(journal, req).state == OrderState.UNKNOWN and position(journal).state == PositionState.ENTERING
    assert not mgr.in_flight


async def test_cancellation_during_history_read_leaves_unknown(journal, fake, mgr, req, monkeypatch):
    started = asyncio.Event()

    async def slow(since):
        fake.calls.append("closed_since")
        started.set()
        await hang_forever()

    monkeypatch.setattr(fake, "closed_since", slow)
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    task = asyncio.create_task(mgr.submit_entry(req))
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait([task, waiter], timeout=TEST_GUARD_S, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    assert started.is_set(), "submit_entry never reached the history read"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order(journal, req).state == OrderState.UNKNOWN and position(journal).state == PositionState.ENTERING
    assert not mgr.in_flight
