"""Testy uzgadniania z brokerem (spec §7): wiersze tabeli §7, stany po przerwaniu między zapisami i awarie odczytu.

Niezmienniki pod lupą (prawdziwe pieniądze):
- uzgadnianie tylko CZYTA u brokera: nigdy nie zamyka pozycji, nie zmienia stopu i nie anuluje zlecenia;
- każda odpowiedź brokera trafia do dziennika (`append_raw`) przed decyzją, którą uzasadnia;
- nieudany odczyt przerywa przebieg bez zmiany stanu i nie jest dowodem braku pozycji;
- pozycji, której nie da się udowodnić jako naszej, nie przyjmujemy: BLOCKED i kill switch miękki;
- zamknięcie księgujemy przed przejściem do FLAT i najwyżej raz na przebieg; drugi przebieg niczego nie zmienia.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.journal import JournalWriteError, OrderRecord, PositionRecord
from gwstock.reconcile import ReconcileResult, Reconciler
from gwstock.types import BrokerTimeout, ClosedDeal, OpenPosition, OrderState, PositionState, SessionExpired, Side

O = OrderState
P = PositionState

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)  # = clock.now_utc() na starcie fixture `clock`
EPIC = "DE40"
OID = "gws-20261102-DE40-1-entry"
SIZE = Decimal("0.007")
FILL = Decimal("25782.3")  # BUY po ask z fixture `quote`
STOP = FILL - Decimal("30")
EMPTY_READS = f"unknown_empty_reads:{OID}"
READS = ["list_positions", "list_working_orders"]  # przebieg bez potrzeby sięgania do historii
READ_ONLY = {"list_positions", "list_working_orders", "closed_since"}
SECONDS = timedelta(seconds=1)


# --- pomocnicze -------------------------------------------------------------------------------------


def seed_order(journal, state, *, created_at=T0, order_id=OID, epic=EPIC, side=Side.BUY):
    rec = OrderRecord(order_id, epic, side, SIZE, Decimal("30"), state, None, None, created_at, created_at)
    journal.insert_order(rec)
    return rec


def held(state=P.OPEN, deal_id="DEAL-1", *, side=Side.BUY, size=SIZE, stop=STOP, opened_at=T0):
    """Nasza pozycja w dzienniku (pola jak po potwierdzeniu brokera)."""
    return PositionRecord(EPIC, state, deal_id, side, size, FILL, stop, opened_at)


def at_broker(deal_id="DEAL-1", *, side=Side.BUY, size=SIZE, stop=STOP, opened_at=T0, epic=EPIC):
    return OpenPosition(deal_id, epic, side, size, FILL, stop, opened_at)


def put(fake, *positions):
    for p in positions:
        fake.positions[p.deal_id] = p


def entering(journal):
    journal.upsert_position(PositionRecord(EPIC, P.ENTERING))


def position(journal):
    return journal.current_position(EPIC)


def order(journal, order_id=OID):
    return journal.get_order(order_id)


def payloads(journal, source):
    return [json.loads(e.payload) for e in journal.events(source)]


def snapshot(journal):
    """Stan dziennika poza surowymi zdarzeniami: zlecenie, pozycja, znaczniki, księga."""
    meta = {k: journal.get_meta(k) for k in ("mismatch_since", "closed_marker", EMPTY_READS)}
    return order(journal), position(journal), meta, journal.trades()


def has_note(result, code):
    return any(note.split()[0] == code for note in result.notes)


def write_failure(*_args, **_kwargs):
    raise JournalWriteError("disk full")


def spy_states(journal, monkeypatch):
    """Zapisuje kolejne stany zapisywane przez `update_order` (None: zmiana bez zmiany stanu)."""
    states = []
    real = journal.update_order

    def spy(order_id, **fields):
        states.append(fields.get("state"))
        return real(order_id, **fields)

    monkeypatch.setattr(journal, "update_order", spy)
    return states


# --- §7 wiersz 1: pozycja u brokera, a my mamy UNKNOWN lub SUBMITTING → przyjmij jako własną ---------


async def test_position_adopted_when_local_unknown(journal, fake, mgr, req, reconciler, kill_sink):
    fake.fill_size_factor = Decimal(2)  # broker wypełnił więcej, niż prosiliśmy: przyjmujemy faktyczną wielkość
    fake.fail_next("open_position", BrokerTimeout(), applied=True)  # pozycja powstała, odpowiedź zginęła
    for _ in range(3):
        fake.fail_next("list_positions", SessionExpired("401"))  # menedżer nie może sprawdzić listy
    assert (await mgr.submit_entry(req)).state is O.UNKNOWN
    assert position(journal).state is P.ENTERING
    fake.calls.clear()

    result = await reconciler.run_once()

    broker = fake.positions["DEAL-1"]
    o = order(journal)
    assert o.state is O.CONFIRMED and o.deal_id == "DEAL-1"
    assert o.note == (
        "ADOPTED_BY_RECONCILER VIA=positions FILLED_SIZE=0.014 REQUESTED=0.007 CAUSE=open_position:BrokerTimeout"
    )
    assert position(journal) == PositionRecord(
        EPIC, P.OPEN, "DEAL-1", broker.side, broker.size, broker.open_level, broker.stop_level, broker.opened_at
    )
    assert broker.size == Decimal("0.014")
    assert not result.blocked and not result.skipped and kill_sink.calls == []
    assert fake.calls == READS  # tylko odczyty
    assert has_note(result, "ADOPTED_BY_RECONCILER")


async def test_submitting_is_recovered_to_unknown_then_adopted(journal, fake, reconciler, kill_sink, monkeypatch):
    seed_order(journal, O.SUBMITTING)
    entering(journal)
    put(fake, at_broker(opened_at=T0 + SECONDS))
    states = spy_states(journal, monkeypatch)

    result = await reconciler.run_once()

    assert states == [O.UNKNOWN, O.CONFIRMED]  # nigdy SUBMITTING -> CONFIRMED z pominięciem UNKNOWN
    o = order(journal)
    assert o.state is O.CONFIRMED and o.deal_id == "DEAL-1"
    assert o.note == "ADOPTED_BY_RECONCILER VIA=positions RECOVERED_FROM_SUBMITTING"
    assert position(journal) == held(opened_at=T0 + SECONDS)
    assert not result.blocked and kill_sink.calls == []


@pytest.mark.parametrize("pos", [PositionRecord(EPIC, P.ENTERING), None], ids=["entering", "no_position_row"])
async def test_intent_is_abandoned_before_send(journal, fake, reconciler, kill_sink, pos):
    seed_order(journal, O.INTENT)
    if pos is not None:
        journal.upsert_position(pos)

    result = await reconciler.run_once()

    assert order(journal).state is O.NOT_PLACED and order(journal).note == "ABANDONED_BEFORE_SEND"
    assert position(journal) in (PositionRecord(EPIC, P.FLAT), None)
    assert not result.blocked and kill_sink.calls == [] and fake.calls == READS


async def test_abandoned_intent_with_broker_position_is_foreign_not_adopted(journal, fake, reconciler, kill_sink):
    seed_order(journal, O.INTENT)  # INTENT: nic nie poszło do brokera, więc pozycja nie może być nasza
    entering(journal)
    put(fake, at_broker(opened_at=T0 + SECONDS))

    result = await reconciler.run_once()

    assert order(journal).state is O.NOT_PLACED
    assert position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["FOREIGN_POSITION"] and fake.calls == READS


async def test_submitting_without_position_becomes_not_placed_after_three_empty_reads(
    journal, fake, reconciler, clock, kill_sink
):
    seed_order(journal, O.SUBMITTING)
    entering(journal)
    for reads in (1, 2):
        result = await reconciler.run_once()
        assert order(journal).state is O.UNKNOWN and journal.get_meta(EMPTY_READS) == str(reads)
        assert position(journal).state is P.ENTERING and not result.blocked
        clock.advance(15)

    result = await reconciler.run_once()  # trzecie puste zapytanie, 30 s po INTENT

    o = order(journal)
    assert o.state is O.NOT_PLACED
    assert o.note == "NOT_FOUND_BY_RECONCILER READS=3 RECOVERED_FROM_SUBMITTING"
    assert position(journal) == PositionRecord(EPIC, P.FLAT)
    assert fake.calls.count("closed_since") == 1  # historia sprawdzana tylko przed ostatecznym NOT_PLACED
    assert set(fake.calls) <= READ_ONLY
    assert not result.blocked and kill_sink.calls == []


async def test_three_quick_empty_reads_are_not_enough_before_30_s(journal, fake, reconciler, clock):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    for _ in range(3):
        await reconciler.run_once()
        clock.advance(1)
    assert order(journal).state is O.UNKNOWN and journal.get_meta(EMPTY_READS) == "3"

    clock.advance(30)
    await reconciler.run_once()

    assert order(journal).state is O.NOT_PLACED and order(journal).note == "NOT_FOUND_BY_RECONCILER READS=4"


async def test_ambiguous_match_blocks_and_keeps_order_unknown(journal, fake, reconciler, kill_sink):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    first, second = at_broker("DEAL-1", opened_at=T0 + SECONDS), at_broker("DEAL-2", opened_at=T0 + 2 * SECONDS)
    put(fake, first, second)

    result = await reconciler.run_once()

    assert order(journal).state is O.UNKNOWN and order(journal).note == "AMBIGUOUS_MATCH"
    assert position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["AMBIGUOUS_MATCH"]
    assert result.unprotected == [] and fake.calls == READS

    again = await reconciler.run_once()  # BLOCKED z nierozstrzygniętym zleceniem: decyduje właściciel

    assert order(journal).state is O.UNKNOWN and position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert again.blocked and has_note(again, "BLOCKED_AWAITING_OWNER")
    assert fake.positions == {"DEAL-1": first, "DEAL-2": second} and set(fake.calls) <= READ_ONLY


async def test_multiple_unresolved_orders_block_without_adopting(journal, fake, reconciler, kill_sink):
    seed_order(journal, O.UNKNOWN)
    seed_order(journal, O.UNKNOWN, order_id="gws-20261102-DE40-2-entry")
    entering(journal)
    put(fake, at_broker(opened_at=T0 + SECONDS))

    result = await reconciler.run_once()

    assert order(journal).state is O.UNKNOWN and order(journal, "gws-20261102-DE40-2-entry").state is O.UNKNOWN
    assert position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["MULTIPLE_UNRESOLVED_ORDERS"] and fake.calls == READS


async def test_unmatched_closed_deal_blocks_instead_of_not_placed(journal, fake, reconciler, clock, kill_sink):
    """Potwierdzenie zginęło, a pozycja otworzyła się i wybił ją stop, zanim ktokolwiek ją zobaczył."""
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    put(fake, at_broker(opened_at=T0 + SECONDS))
    clock.advance(2)
    fake.trigger_stop("DEAL-1", STOP)
    for _ in range(2):
        await reconciler.run_once()
        clock.advance(14)

    result = await reconciler.run_once()  # trzecie puste zapytanie: historia pokazuje zamknięcie na DE40

    assert order(journal).state is O.UNKNOWN and order(journal).note.split()[0] == "UNMATCHED_CLOSED_DEAL"
    assert position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["UNMATCHED_CLOSED_DEAL"] and result.closed == []


# --- stany po przerwaniu między zapisami menedżera (pozycja zapisana, zlecenie jeszcze nie) -------------


async def test_crash_between_writes_submitting_order_with_open_position_is_confirmed(
    journal, fake, reconciler, kill_sink, monkeypatch
):
    seed_order(journal, O.SUBMITTING)
    journal.upsert_position(held())
    put(fake, at_broker())
    states = spy_states(journal, monkeypatch)

    result = await reconciler.run_once()

    assert states == [O.UNKNOWN, O.CONFIRMED]
    o = order(journal)
    assert o.state is O.CONFIRMED and o.deal_id == "DEAL-1"
    assert o.note == "ADOPTED_BY_RECONCILER VIA=journal RECOVERED_FROM_SUBMITTING"
    assert position(journal) == held()  # OPEN zostaje OPEN (bez niedozwolonego OPEN -> OPEN)
    assert not result.blocked and kill_sink.calls == [] and fake.calls == READS


async def test_crash_between_writes_open_position_already_stopped_out_is_confirmed_and_booked(
    journal, fake, reconciler, booker, clock
):
    seed_order(journal, O.UNKNOWN)
    journal.upsert_position(held())
    put(fake, at_broker())
    clock.advance(60)
    fake.trigger_stop("DEAL-1", STOP)

    result = await reconciler.run_once()

    closed = ClosedDeal("DEAL-1", EPIC, STOP, "STOP", T0 + 60 * SECONDS)
    assert order(journal).state is O.CONFIRMED and order(journal).deal_id == "DEAL-1"
    assert booker.calls == [(held(), closed)] and result.closed == [closed]
    assert position(journal) == PositionRecord(EPIC, P.FLAT) and not result.blocked


async def test_unknown_order_with_flat_position_is_not_placed_without_touching_position(
    journal, fake, reconciler, clock, monkeypatch
):
    seed_order(journal, O.UNKNOWN)
    journal.upsert_position(PositionRecord(EPIC, P.FLAT))  # menedżer zapisał FLAT, zlecenia już nie
    upserts = []
    monkeypatch.setattr(journal, "upsert_position", upserts.append)
    for _ in range(3):
        result = await reconciler.run_once()
        clock.advance(15)

    assert order(journal).state is O.NOT_PLACED and order(journal).note.split()[0] == "NOT_FOUND_BY_RECONCILER"
    assert upserts == [] and position(journal) == PositionRecord(EPIC, P.FLAT) and not result.blocked


async def test_unknown_order_with_flat_position_and_broker_position_is_blocked_not_adopted(
    journal, fake, reconciler, kill_sink
):
    seed_order(journal, O.UNKNOWN)
    journal.upsert_position(PositionRecord(EPIC, P.FLAT))  # menedżer uznał, że wejścia nie ma: sprzeczność
    put(fake, at_broker(opened_at=T0 + SECONDS))

    result = await reconciler.run_once()

    assert order(journal).state is O.UNKNOWN
    assert position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["FOREIGN_POSITION"] and fake.calls == READS


# --- §7 wiersz 2: pozycja u brokera bez śladu w naszych zleceniach → BLOCKED, kill miękki, nie dotykamy -----


async def test_foreign_position_blocks_and_triggers_soft_kill(journal, fake, reconciler, kill_sink):
    foreign = at_broker("DEAL-9", opened_at=T0 - timedelta(hours=1))
    put(fake, foreign)

    result = await reconciler.run_once()

    assert result.blocked and position(journal) == PositionRecord(EPIC, P.BLOCKED)
    assert kill_sink.calls == ["FOREIGN_POSITION"] and has_note(result, "FOREIGN_POSITION")
    assert fake.calls == READS and fake.positions == {"DEAL-9": foreign}  # brak close_position i set_stop
    assert result.unprotected == [] and result.closed == []


async def test_foreign_position_is_never_closed_or_modified(journal, fake, reconciler, clock, kill_sink):
    foreign = at_broker("DEAL-9", stop=None, opened_at=T0 - timedelta(hours=1))  # nawet bez stopu
    put(fake, foreign)
    results = []
    for _ in range(4):
        results.append(await reconciler.run_once())
        clock.advance(40)

    assert set(fake.calls) <= READ_ONLY and fake.positions == {"DEAL-9": foreign}
    assert all(r.blocked and r.unprotected == [] for r in results)
    assert "MISMATCH_TIMEOUT" in kill_sink.calls and set(kill_sink.calls) == {"FOREIGN_POSITION", "MISMATCH_TIMEOUT"}


async def test_extra_position_on_our_epic_while_open_blocks(journal, fake, reconciler, kill_sink):
    journal.upsert_position(held())
    put(fake, at_broker(), at_broker("DEAL-9", opened_at=T0 - timedelta(hours=1)))

    result = await reconciler.run_once()

    assert position(journal) == held(P.BLOCKED)  # nasza pozycja zostaje w zapisie, decyduje właściciel
    assert result.blocked and kill_sink.calls == ["FOREIGN_POSITION"] and fake.calls == READS


async def test_blocked_foreign_position_clears_when_it_vanishes(journal, fake, reconciler, kill_sink):
    put(fake, at_broker("DEAL-9", opened_at=T0 - timedelta(hours=1)))
    assert (await reconciler.run_once()).blocked
    del fake.positions["DEAL-9"]

    result = await reconciler.run_once()

    assert not result.blocked and position(journal) == PositionRecord(EPIC, P.FLAT)
    assert kill_sink.calls == ["FOREIGN_POSITION"]  # kill switch zostaje: zdejmuje go tylko właściciel
    assert not journal.get_meta("mismatch_since")


async def test_positions_and_orders_on_other_epics_are_ignored(journal, fake, reconciler, kill_sink):
    other = "gws-20261102-US500-1-entry"
    put(fake, at_broker("DEAL-5", epic="US500", stop=None))
    fake.add_working_order("US500", Side.SELL, Decimal("1"), Decimal("6000"))
    seed_order(journal, O.UNKNOWN, order_id=other, epic="US500")

    result = await reconciler.run_once()

    assert result == ReconcileResult(blocked=False, notes=[], closed=[], unprotected=[])
    assert order(journal, other).state is O.UNKNOWN and position(journal) is None
    assert kill_sink.calls == [] and fake.calls == READS


# --- §7 wiersz 3: pozycja lokalna, której nie ma u brokera → historia; jest: zaksięguj; brak: BLOCKED -------


async def test_local_position_missing_at_broker_books_stop(journal, fake, mgr, req, reconciler, booker, clock, kill_sink):
    await mgr.submit_entry(req)
    opened = position(journal)
    assert opened.state is P.OPEN
    clock.advance(600)
    fake.trigger_stop("DEAL-1", STOP - Decimal("2"))  # luka: wykonanie gorsze niż stop
    fake.calls.clear()

    result = await reconciler.run_once()

    closed = ClosedDeal("DEAL-1", EPIC, STOP - Decimal("2"), "STOP", T0 + 600 * SECONDS)
    assert result.closed == [closed] and result.closed[0].reason == "STOP"
    assert booker.calls == [(opened, closed)]
    assert position(journal) == PositionRecord(EPIC, P.FLAT)
    assert not result.blocked and kill_sink.calls == []
    assert fake.calls == ["list_positions", "list_working_orders", "closed_since"]
    assert payloads(journal, "broker.closed_since")[-1]["since"] == (T0 - timedelta(minutes=1)).isoformat()


async def test_closed_position_without_booker_still_goes_flat(journal, fake, clock, kill_sink, risk_cfg):
    reconciler = Reconciler(journal, fake, clock, kill_sink, risk_cfg, EPIC)
    journal.upsert_position(held())
    put(fake, at_broker())
    fake.trigger_stop("DEAL-1", STOP)

    result = await reconciler.run_once()

    assert len(result.closed) == 1 and position(journal) == PositionRecord(EPIC, P.FLAT)


async def test_local_position_missing_without_activity_is_blocked(journal, fake, reconciler, booker, kill_sink):
    journal.upsert_position(held())

    result = await reconciler.run_once()

    assert position(journal) == held(P.BLOCKED)  # zamknięcia nie wymyślamy
    assert result.blocked and has_note(result, "LOCAL_POSITION_MISSING")
    assert result.closed == [] and booker.calls == []
    assert kill_sink.calls == []  # kill switch dopiero po tolerancji (rozbieżność oczekująca)
    assert journal.get_meta("mismatch_since") == T0.isoformat()


async def test_missing_position_found_later_in_history_is_booked_from_blocked(journal, fake, reconciler, booker, clock):
    journal.upsert_position(held())
    await reconciler.run_once()  # broker nie pokazuje pozycji ani zamknięcia (opóźniona historia)
    assert position(journal) == held(P.BLOCKED)
    put(fake, at_broker())
    clock.advance(20)
    fake.trigger_stop("DEAL-1", STOP)

    result = await reconciler.run_once()

    closed = ClosedDeal("DEAL-1", EPIC, STOP, "STOP", T0 + 20 * SECONDS)
    assert result.closed == [closed] and booker.calls == [(held(P.BLOCKED), closed)]
    assert position(journal) == PositionRecord(EPIC, P.FLAT) and not result.blocked
    assert not journal.get_meta("mismatch_since")


async def test_blocked_position_recovers_to_open_when_broker_lists_it_again(journal, fake, reconciler, kill_sink):
    journal.upsert_position(held())
    await reconciler.run_once()  # chwilowo brak na liście
    put(fake, at_broker())

    result = await reconciler.run_once()

    assert position(journal) == held() and not result.blocked and kill_sink.calls == []
    assert not journal.get_meta("mismatch_since")


async def test_ambiguous_closure_history_blocks_without_booking(journal, fake, reconciler, booker, kill_sink):
    journal.upsert_position(held())
    for _ in range(2):  # dwa zamknięcia z tym samym deal_id: nie zgadujemy, które jest prawdziwe
        put(fake, at_broker())
        fake.trigger_stop("DEAL-1", STOP)

    result = await reconciler.run_once()

    assert position(journal) == held(P.BLOCKED) and booker.calls == [] and result.closed == []
    assert result.blocked and kill_sink.calls == ["AMBIGUOUS_CLOSURE"]


async def test_stale_history_for_other_epics_and_deals_is_ignored(journal, fake, reconciler, booker):
    journal.upsert_position(held())
    put(fake, at_broker("DEAL-1", epic="US500"), at_broker("DEAL-7"))  # ten sam deal_id na innym instrumencie
    fake.trigger_stop("DEAL-1", Decimal("6000"))
    fake.trigger_stop("DEAL-7", STOP)

    result = await reconciler.run_once()

    assert result.closed == [] and booker.calls == []
    assert position(journal) == held(P.BLOCKED) and has_note(result, "LOCAL_POSITION_MISSING")


async def test_local_open_position_without_deal_id_is_blocked_not_cleared(journal, fake, reconciler, booker):
    journal.upsert_position(PositionRecord(EPIC, P.OPEN))  # nie da się jej sprawdzić ani zaksięgować

    result = await reconciler.run_once()

    assert position(journal) == PositionRecord(EPIC, P.BLOCKED) and result.blocked and booker.calls == []


async def test_closed_marker_advances_and_bounds_the_next_history_read(journal, fake, reconciler, clock):
    journal.upsert_position(held())
    put(fake, at_broker())
    clock.advance(600)
    fake.trigger_stop("DEAL-1", STOP)
    await reconciler.run_once()
    marker = T0 + 600 * SECONDS
    assert journal.get_meta("closed_marker") == marker.isoformat()

    journal.upsert_position(held(deal_id="DEAL-2", opened_at=T0 + 300 * SECONDS))  # zapis sprzed znacznika
    await reconciler.run_once()

    assert payloads(journal, "broker.closed_since")[-1]["since"] == (marker - timedelta(minutes=1)).isoformat()
    assert journal.get_meta("closed_marker") == marker.isoformat()  # bez zamknięcia znacznik stoi


# --- §7 wiersz 4: różnica wielkości lub kierunku → BLOCKED, alarm ---------------------------------------


async def test_size_mismatch_blocks(journal, fake, reconciler, kill_sink):
    journal.upsert_position(held())
    bigger = at_broker(size=Decimal("0.014"))
    put(fake, bigger)

    result = await reconciler.run_once()

    assert position(journal) == held(P.BLOCKED)  # dziennik zachowuje nasz zapis do decyzji właściciela
    assert result.blocked and kill_sink.calls == ["SIZE_MISMATCH"]
    assert fake.calls == READS and fake.positions == {"DEAL-1": bigger}


async def test_side_mismatch_blocks(journal, fake, reconciler, kill_sink):
    journal.upsert_position(held())
    opposite = at_broker(side=Side.SELL, stop=FILL + Decimal("30"))
    put(fake, opposite)

    result = await reconciler.run_once()

    assert position(journal) == held(P.BLOCKED)
    assert result.blocked and kill_sink.calls == ["SIDE_MISMATCH"]
    assert fake.calls == READS and fake.positions == {"DEAL-1": opposite}


# --- §7 wiersz 5: zlecenie oczekujące na naszym instrumencie → alarm, anulowanie tylko przez właściciela -----


async def test_working_order_on_our_epic_is_flagged(journal, fake, reconciler, kill_sink):
    ours = fake.add_working_order(EPIC, Side.BUY, SIZE, Decimal("25000"))
    fake.add_working_order("US500", Side.SELL, Decimal("1"), Decimal("6000"))

    result = await reconciler.run_once()

    assert result.working_orders == [ours] and has_note(result, "FOREIGN_WORKING_ORDER")
    assert kill_sink.calls == ["FOREIGN_WORKING_ORDER"]
    assert fake.calls == READS and len(fake.working_orders) == 2  # nic nie anulowano
    assert not result.blocked


# --- §7 wiersz 6: pozycja bez stopu → zgłoszenie (stop ustawia wykonawca, nie uzgadnianie) ---------------


async def test_unprotected_position_is_reported(journal, fake, reconciler, kill_sink):
    journal.upsert_position(held())
    naked = at_broker(stop=None)
    put(fake, naked)

    result = await reconciler.run_once()

    assert result.unprotected == [naked] and has_note(result, "UNPROTECTED")
    assert fake.calls == READS  # brak set_stop
    assert position(journal) == held(stop=None)  # dziennik odzwierciedla brokera
    assert not result.blocked and kill_sink.calls == []


# --- §7 akapit końcowy: nierozstrzygnięta rozbieżność > 60 s → kill switch miękki --------------------------


async def test_mismatch_older_than_60s_triggers_soft_kill(journal, fake, reconciler, kill_sink, clock):
    journal.upsert_position(held())
    await reconciler.run_once()
    clock.advance(60)
    await reconciler.run_once()
    assert kill_sink.calls == []  # równo 60 s mieści się w tolerancji

    clock.advance(1)
    result = await reconciler.run_once()

    assert kill_sink.calls == ["MISMATCH_TIMEOUT"] and has_note(result, "MISMATCH_TIMEOUT")
    assert journal.get_meta("mismatch_since") == T0.isoformat()


async def test_mismatch_since_is_set_while_pending_and_cleared_when_resolved(journal, fake, reconciler, clock, kill_sink):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    assert journal.get_meta("mismatch_since") is None
    await reconciler.run_once()
    assert journal.get_meta("mismatch_since") == T0.isoformat()
    clock.advance(15)
    await reconciler.run_once()
    assert journal.get_meta("mismatch_since") == T0.isoformat()  # czas liczony od pierwszego wykrycia
    clock.advance(15)

    await reconciler.run_once()

    assert order(journal).state is O.NOT_PLACED
    assert not journal.get_meta("mismatch_since") and kill_sink.calls == []


# --- idempotencja i księgowanie ---------------------------------------------------------------------------


async def test_second_run_after_adoption_changes_nothing(journal, fake, reconciler, kill_sink):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    put(fake, at_broker(opened_at=T0 + SECONDS))
    await reconciler.run_once()
    before = snapshot(journal)

    result = await reconciler.run_once()

    assert snapshot(journal) == before and result.notes == [] and kill_sink.calls == []


async def test_second_run_after_booking_books_nothing(journal, fake, reconciler, booker):
    journal.upsert_position(held())
    put(fake, at_broker())
    fake.trigger_stop("DEAL-1", STOP)
    assert len((await reconciler.run_once()).closed) == 1
    before = snapshot(journal)

    result = await reconciler.run_once()

    assert result.closed == [] and len(booker.calls) == 1 and snapshot(journal) == before


async def test_booker_failure_propagates_and_position_stays_open(journal, fake, reconciler, booker):
    journal.upsert_position(held())
    put(fake, at_broker())
    fake.trigger_stop("DEAL-1", STOP)
    booker.fail = RuntimeError("ledger down")

    with pytest.raises(RuntimeError, match="ledger down"):
        await reconciler.run_once()

    assert position(journal) == held() and journal.get_meta("closed_marker") is None  # bez FLAT bez księgowania
    booker.fail = None
    result = await reconciler.run_once()
    assert position(journal) == PositionRecord(EPIC, P.FLAT) and len(result.closed) == 1
    assert booker.calls[0] == booker.calls[1] and len(booker.calls) == 2  # ponowienie jest idempotentne po stronie księgi


# --- bramka wejścia, awarie odczytu i zapisu ------------------------------------------------------------


class Untouchable:
    """Dziennik, którego nie wolno dotknąć: każda metoda kończy test błędem."""

    def __getattr__(self, name):
        raise AssertionError(f"journal.{name} touched while an entry is in flight")


async def test_entry_gate_skips_without_touching_broker_or_journal(journal, fake, clock, kill_sink, risk_cfg):
    put(fake, at_broker("DEAL-9"))
    skipping = Reconciler(Untouchable(), fake, clock, kill_sink, risk_cfg, EPIC, entry_gate=lambda: True)

    result = await skipping.run_once()

    assert result.skipped and not result.blocked and result.closed == [] and result.unprotected == []
    assert fake.calls == [] and kill_sink.calls == []

    running = Reconciler(journal, fake, clock, kill_sink, risk_cfg, EPIC, entry_gate=lambda: False)
    assert not (await running.run_once()).skipped and fake.calls == READS


@pytest.mark.parametrize("method", ["list_positions", "list_working_orders", "closed_since"])
async def test_broker_error_mid_read_leaves_journal_untouched(journal, fake, reconciler, clock, kill_sink, method):
    seed_order(journal, O.UNKNOWN, created_at=T0 - timedelta(minutes=1))
    entering(journal)
    for _ in range(2):
        await reconciler.run_once()
    before = snapshot(journal)
    assert before[2][EMPTY_READS] == "2"
    fake.fail_next(method, SessionExpired("401"))  # trzeci odczyt (ostatni przed NOT_PLACED) się nie udaje

    with pytest.raises(SessionExpired):
        await reconciler.run_once()

    assert snapshot(journal) == before and kill_sink.calls == []  # nieudany odczyt nie jest pustym odczytem
    await reconciler.run_once()
    assert order(journal).state is O.NOT_PLACED and order(journal).note == "NOT_FOUND_BY_RECONCILER READS=3"


async def test_history_read_failure_neither_books_nor_blocks(journal, fake, reconciler, booker):
    journal.upsert_position(held())
    fake.fail_next("closed_since", SessionExpired("401"))

    with pytest.raises(SessionExpired):
        await reconciler.run_once()

    assert position(journal) == held() and booker.calls == [] and journal.get_meta("mismatch_since") is None


async def test_journal_write_failure_propagates_before_any_state_change(
    journal, fake, reconciler, kill_sink, monkeypatch
):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    put(fake, at_broker("DEAL-9", opened_at=T0 - timedelta(hours=1)))
    monkeypatch.setattr(journal, "append_raw", write_failure)

    with pytest.raises(JournalWriteError):
        await reconciler.run_once()

    assert order(journal).state is O.UNKNOWN and position(journal).state is P.ENTERING
    assert kill_sink.calls == [] and fake.calls == ["list_positions"]  # nic dalej po nieudanym zapisie odpowiedzi


async def test_soft_kill_survives_a_failing_state_write(journal, fake, reconciler, kill_sink, monkeypatch):
    put(fake, at_broker("DEAL-9", opened_at=T0 - timedelta(hours=1)))
    monkeypatch.setattr(journal, "upsert_position", write_failure)

    with pytest.raises(JournalWriteError):
        await reconciler.run_once()

    assert kill_sink.calls == ["FOREIGN_POSITION"]  # alarm przed zapisami: awaria dziennika go nie połyka
    assert position(journal) is None and fake.calls == READS


async def test_broker_responses_are_journaled_before_any_decision(journal, fake, clock, risk_cfg, monkeypatch):
    seed_order(journal, O.UNKNOWN)
    entering(journal)
    put(fake, at_broker("DEAL-1", opened_at=T0 + SECONDS), at_broker("DEAL-2", opened_at=T0 + SECONDS))
    seen = []

    def sources():
        return {e.source for e in journal.events()}

    for name in ("upsert_position", "update_order", "set_meta"):
        real = getattr(journal, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            seen.append((_name, sources()))
            return _real(*args, **kwargs)

        monkeypatch.setattr(journal, name, spy)

    class Sink:
        def soft(self, reason):
            seen.append(("kill", sources()))

    await Reconciler(journal, fake, clock, Sink(), risk_cfg, EPIC).run_once()

    assert {name for name, _ in seen} == {"kill", "upsert_position", "update_order", "set_meta"}
    for name, present in seen:
        assert {"broker.list_positions", "broker.list_working_orders"} <= present, name
    decisions = payloads(journal, "reconcile.event")
    assert decisions and decisions[0]["event"] == "AMBIGUOUS_MATCH"
    raw_ids = [e.id for e in journal.events("broker.list_positions")]
    assert max(raw_ids) < min(e.id for e in journal.events("reconcile.event"))
