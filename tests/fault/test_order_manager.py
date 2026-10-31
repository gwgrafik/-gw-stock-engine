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
from gwstock.journal import JournalWriteError, OrderRecord, PositionRecord
from gwstock.orders.fsm import check_order_transition, check_position_transition
from gwstock.orders.manager import EntryBlocked, OrderManager
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
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=positions"
    assert position(journal).state == PositionState.OPEN and position(journal).deal_id == "DEAL-1"
    assert fake.calls.count("list_positions") == 1 and clock.elapsed_since_start() == 10


async def test_timeout_without_position_becomes_not_placed_after_three_checks(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.NOT_PLACED and fake.calls.count("list_positions") == 3
    assert clock.elapsed_since_start() >= 30  # trzy zapytania w 30 s
    assert rec.note == "NOT_FOUND_AFTER_CHECKS=3"
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
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=positions"
    assert fake.calls.count("open_position") == 1


async def test_confirm_timeout_is_resolved_by_asking_confirm_again(journal, fake, mgr, req):
    fake.fail_next("confirm", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and (rec.deal_reference, rec.deal_id) == ("REF-1", "DEAL-1")
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm"
    assert fake.calls.count("open_position") == 1 and fake.calls.count("confirm") == 2


async def test_position_stopped_out_before_resolution_is_still_confirmed_from_snapshot(journal, fake, mgr, req):
    # broker wykonał wejście, ale zanim je potwierdziliśmy, stop zamknął pozycję: lista pozycji jest pusta,
    # a migawka `confirm` nadal mówi, co zlecenie wytworzyło (stratę zaksięguje rekoncyliator, nie NOT_PLACED)
    fake.on_call("confirm", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    fake.fail_next("confirm", BrokerTimeout())
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.deal_id == "DEAL-1"
    assert rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm"
    assert position(journal).state == PositionState.OPEN  # rekoncyliator wykryje zniknięcie i zaksięguje stop


async def test_snapshot_opened_before_intent_is_accepted_when_the_deal_is_listed(journal, fake, mgr, req, monkeypatch):
    # odchyłka zegara brokera: opened_at sprzed INTENT; pozycja z tym deal_id wisi na liście, więc to nasze wejście
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - timedelta(seconds=1))

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    fake.fail_next("confirm", BrokerTimeout())
    def skew_listed_position():  # sama lista nie wystarczy do przejęcia (opened_at < created_at): decyduje migawka
        fake.positions["DEAL-1"] = replace(fake.positions["DEAL-1"], opened_at=T0 - timedelta(seconds=1))

    fake.on_call("list_positions", skew_listed_position, once=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and rec.note == "ADOPTED_AFTER_UNKNOWN VIA=confirm"


async def test_unverifiable_snapshot_blocks_instead_of_guessing(journal, fake, mgr, req, monkeypatch):
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - timedelta(seconds=1))

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    fake.fail_next("confirm", BrokerTimeout())
    fake.on_call("list_positions", lambda: fake.trigger_stop("DEAL-1", STOP), once=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CONFIRM_UNVERIFIED"
    assert position(journal).state == PositionState.BLOCKED
    assert fake.calls.count("open_position") == 1


async def test_direct_confirmation_is_not_compared_with_intent_time(journal, fake, mgr, req, monkeypatch):
    # odpowiedź na NASZĄ świeżą referencję: odchyłka zegara brokera nie może zamienić wykonania w UNKNOWN
    real_confirm = fake.confirm

    async def skewed_confirm(deal_reference):
        snap = await real_confirm(deal_reference)
        return replace(snap, opened_at=T0 - timedelta(seconds=1))

    monkeypatch.setattr(fake, "confirm", skewed_confirm)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and fake.calls == ["open_position", "confirm"]
    assert position(journal).opened_at == T0 - timedelta(seconds=1)


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
    assert rec.state == OrderState.NOT_PLACED and rec.note == "REJECTED_ON_CONFIRM=INSUFFICIENT_FUNDS"
    assert position(journal).state == PositionState.FLAT
    assert fake.calls.count("open_position") == 1


async def test_ambiguous_match_blocks_and_never_guesses(journal, fake, mgr, req):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    fake.positions["MANUAL-1"] = foreign("MANUAL-1")  # druga pasująca pozycja (np. ręczna) w tym samym oknie
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "AMBIGUOUS_MATCH" and rec.deal_id is None
    assert position(journal) == PositionRecord("DE40", PositionState.BLOCKED)
    assert fake.calls.count("open_position") == 1
    [event] = payloads(journal, "order.event")
    assert event["event"] == "AMBIGUOUS_MATCH" and sorted(event["deal_ids"]) == ["DEAL-1", "MANUAL-1"]


async def test_positions_opened_before_intent_or_not_matching_are_not_adopted(journal, fake, mgr, req):
    fake.positions["OLD-1"] = foreign("OLD-1", opened_at=T0 - timedelta(seconds=1))
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
    assert rec.state == OrderState.UNKNOWN and rec.note == "CHECKS_INCOMPLETE"
    assert position(journal).state == PositionState.ENTERING  # rekoncyliator rozstrzygnie, gdy broker odpowie
    assert fake.calls.count("list_positions") == 3 and clock.elapsed_since_start() == 30


async def test_expired_session_leaves_order_unknown_not_not_placed(journal, fake, mgr, req):
    fake.expire_session()  # menedżer nie loguje się sam: brak odczytu to brak wiedzy, a nie brak pozycji
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.UNKNOWN and rec.note == "CHECKS_INCOMPLETE"
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


async def test_cancellation_during_broker_call_leaves_submitting(journal, fake, mgr, req, monkeypatch):
    started = asyncio.Event()

    async def slow(r):
        fake.calls.append("open_position")
        started.set()
        await hang_forever()

    monkeypatch.setattr(fake, "open_position", slow)
    task = asyncio.create_task(mgr.submit_entry(req))
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait([task, waiter], timeout=TEST_GUARD_S, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    assert started.is_set(), "submit_entry never reached the broker call"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order(journal, req).state == OrderState.SUBMITTING  # rozstrzygnie uzgadnianie po restarcie
    assert position(journal).state == PositionState.ENTERING
    assert fake.calls == ["open_position"]


async def test_resolve_unknown_accepts_only_unknown_orders(journal, mgr, req):
    with pytest.raises(ValueError, match="UNKNOWN"):
        await mgr.resolve_unknown(unresolved(OrderState.SUBMITTING))


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
    assert fake.calls == ["open_position", "confirm"]
    [ack] = payloads(journal, "broker.open_position")
    [raw] = payloads(journal, "broker.confirm")
    assert ack["response"] == {"deal_reference": "REF-1"}
    assert raw["error"] == "BrokerRejected" and raw["reason"] == "INSUFFICIENT_FUNDS"


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


CHECKED = ["broker.open_position"] + ["broker.list_positions"] * 3


@pytest.mark.parametrize(
    ("scenario", "expected_tail"),
    [
        (
            lambda fake: fake.reject_next_entry("INSUFFICIENT_FUNDS"),
            [
                ("position", PositionState.FLAT, ["broker.open_position", "broker.confirm"]),
                ("order", OrderState.REJECTED, ["broker.open_position", "broker.confirm"]),
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


async def test_raw_ack_write_failure_stops_before_confirm(journal, fake, mgr, req, monkeypatch):
    monkeypatch.setattr(journal, "append_raw", write_failure)
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert fake.calls == ["open_position"]  # odpowiedzi nie przetwarzamy, dopóki nie jest zapisana
    assert order(journal, req).state == OrderState.SUBMITTING


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
