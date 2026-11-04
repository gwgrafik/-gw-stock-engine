"""Testy dziennika SQLite: trwałość (WAL, FULL), atomowość zapisów, niezmienniki księgi i licznika strat."""

import sqlite3
import threading
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from gwstock.journal import (
    APPLICATION_ID,
    Journal,
    JournalCorruptError,
    JournalError,
    JournalReadError,
    JournalSchemaError,
    JournalWriteError,
    KillState,
    OrderRecord,
    PositionRecord,
    RawEvent,
    TradeRecord,
)
from gwstock.types import KillLevel, OrderState, PositionState, Side

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)
DAY = date(2026, 11, 2)
NEXT_DAY = date(2026, 11, 3)


def make_order(order_id="gws-20261102-DE40-1-entry", **over) -> OrderRecord:
    fields = dict(
        order_id=order_id,
        epic="DE40",
        side=Side.BUY,
        size=Decimal("0.001"),
        stop_distance=Decimal("20.5"),
        state=OrderState.INTENT,
        deal_reference=None,
        deal_id=None,
        created_at=T0,
        updated_at=T0,
        note="",
    )
    fields.update(over)
    return OrderRecord(**fields)


def make_position(**over) -> PositionRecord:
    fields = dict(
        epic="DE40",
        state=PositionState.OPEN,
        deal_id="DEAL-1",
        side=Side.BUY,
        size=Decimal("0.001"),
        open_level=Decimal("25782.30"),
        stop_level=Decimal("25761.80"),
        opened_at=T0,
    )
    fields.update(over)
    return PositionRecord(**fields)


def make_trade(**over) -> TradeRecord:
    fields = dict(
        instrument="DE40",
        side=Side.BUY,
        size=Decimal("0.001"),
        decision_bid=Decimal("25780.30"),
        decision_ask=Decimal("25782.30"),
        entry_fill=Decimal("25782.30"),
        exit_fill=Decimal("25761.80"),
        gross_pnl=Decimal("-0.0205"),
        spread_cost=Decimal("0.002"),
        slippage_penalty=Decimal("0.0015"),
        financing=Decimal("0"),
        fees=Decimal("0"),
        net_pnl=Decimal("-0.0240"),
        signal_ts=T0,
        submit_ts=T0 + timedelta(seconds=1),
        confirm_ts=T0 + timedelta(seconds=2),
        exit_ts=T0 + timedelta(hours=1),
        latency_ms=2000,
        exit_reason="STOP",
    )
    fields.update(over)
    return TradeRecord(**fields)


def open_journal(path) -> Journal:
    return Journal.open(str(path))


class FailingConn:
    """Proxy połączenia SQLite: instrukcja zawierająca któryś z `needles` kończy się błędem „database is locked”."""

    def __init__(self, real: sqlite3.Connection, *needles: str) -> None:
        self._real = real
        self._needles = needles

    def execute(self, sql, params=()):
        if any(needle in sql for needle in self._needles):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- otwarcie, pragmy, schemat -------------------------------------------------------------


def test_pragmas(tmp_path):
    j = Journal.open(str(tmp_path / "j.sqlite"))
    assert j.pragma("journal_mode") == "wal" and j.pragma("synchronous") == 2  # FULL
    assert j.pragma("user_version") == 1
    j.close()


def test_pragma_rejects_anything_but_a_plain_name(journal):
    with pytest.raises(ValueError):
        journal.pragma("journal_mode=DELETE")
    assert journal.pragma("journal_mode") == "wal"


def test_pragma_reads_only_whitelisted_names(journal):
    expected = {
        "journal_mode": "wal", "synchronous": 2, "user_version": 1, "application_id": APPLICATION_ID,
        "busy_timeout": 5000, "page_size": 4096, "wal_autocheckpoint": 1000,
    }  # fmt: skip
    assert {name: journal.pragma(name) for name in expected} == expected
    # pragmy z efektem ubocznym (checkpoint, optymalizacja, zwolnienie pamięci) i każde inne: odmowa bez wykonania
    for name in ("optimize", "wal_checkpoint", "shrink_memory", "incremental_vacuum", "integrity_check",
                 "query_only", "foreign_keys", "table_info"):  # fmt: skip
        with pytest.raises(ValueError, match="pragma"):
            journal.pragma(name)


def test_open_refuses_database_with_newer_schema_version_and_leaves_it_untouched(tmp_path):
    path = tmp_path / "newer.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.execute("CREATE TABLE future(x)")
    raw.commit()
    raw.close()
    with pytest.raises(JournalSchemaError):
        Journal.open(str(path))
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # nie przełączono na WAL
    raw.close()


def test_open_refuses_foreign_non_empty_database(tmp_path):
    path = tmp_path / "foreign.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE something_else(x)")
    raw.commit()
    raw.close()
    with pytest.raises(JournalSchemaError):
        Journal.open(str(path))


def raw_pragmas(path) -> tuple:
    raw = sqlite3.connect(path)
    try:
        names = ("user_version", "application_id", "journal_mode")
        return tuple(raw.execute(f"PRAGMA {n}").fetchone()[0] for n in names)
    finally:
        raw.close()


def test_open_stamps_the_application_id_on_a_new_journal(tmp_path):
    p = tmp_path / "j.sqlite"
    open_journal(p).close()
    assert raw_pragmas(p) == (1, APPLICATION_ID, "wal")
    assert APPLICATION_ID not in (0, 1)


def test_open_accepts_and_stamps_an_existing_v1_journal_without_application_id(tmp_path):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.append_raw("broker", "kept", T0)
    j.close()
    raw = sqlite3.connect(p)
    raw.execute("PRAGMA application_id = 0")  # dziennik utworzony przez wcześniejszą wersję kodu
    raw.commit()
    raw.close()
    j2 = open_journal(p)
    assert [e.payload for e in j2.events()] == ["kept"]
    j2.close()
    assert raw_pragmas(p) == (1, APPLICATION_ID, "wal")


@pytest.mark.parametrize("version", [0, 1])
def test_open_refuses_a_database_of_another_application_and_leaves_it_untouched(tmp_path, version):
    p = tmp_path / "other.sqlite"
    raw = sqlite3.connect(p)
    raw.execute("PRAGMA application_id = 1234")
    raw.execute(f"PRAGMA user_version = {version}")
    raw.commit()
    raw.close()
    with pytest.raises(JournalSchemaError, match="application_id"):
        Journal.open(str(p))
    assert raw_pragmas(p) == (version, 1234, "delete")  # bez schematu, bez WAL


@pytest.mark.parametrize("version", [-1, -99])
def test_open_refuses_a_negative_schema_version_and_leaves_it_untouched(tmp_path, version):
    p = tmp_path / "odd.sqlite"
    raw = sqlite3.connect(p)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.execute("CREATE TABLE something(x)")
    raw.commit()
    raw.close()
    with pytest.raises(JournalSchemaError):
        Journal.open(str(p))
    assert raw_pragmas(p) == (version, 0, "delete")


def test_open_readonly_on_a_closed_wal_journal_leaves_no_new_files(tmp_path):
    # L26-c: `status`/raport na zamkniętym dzienniku nie mogą zostawić -wal/-shm (innego właściciela niż silnik)
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.append_raw("broker", "kept", T0)
    j.close()
    before = sorted(x.name for x in tmp_path.iterdir())
    assert before == ["j.sqlite"]
    ro = Journal.open_readonly(str(p))
    try:
        assert [e.payload for e in ro.events()] == ["kept"]
        with pytest.raises(JournalWriteError):
            ro.set_meta("x", "y")
    finally:
        ro.close()
    assert sorted(x.name for x in tmp_path.iterdir()) == before
    assert raw_pragmas(p) == (1, APPLICATION_ID, "wal")


def test_open_readonly_sees_committed_rows_of_a_running_writer(tmp_path):
    p = tmp_path / "j.sqlite"
    writer = open_journal(p)
    writer.append_raw("broker", "first", T0)
    ro = Journal.open_readonly(str(p))
    try:
        assert [e.payload for e in ro.events()] == ["first"]
        writer.append_raw("broker", "second", T0)  # silnik pisze dalej: czytelnik widzi zatwierdzone wiersze
        assert [e.payload for e in ro.events()] == ["first", "second"]
        with pytest.raises(JournalWriteError):
            ro.append_raw("broker", "never", T0)
    finally:
        ro.close()
    writer.append_raw("broker", "third", T0)  # czytelnik niczego nie zablokował
    assert len(writer.events()) == 3
    writer.close()


@pytest.mark.parametrize(("app_id", "accepted"), [(0, True), (1234, False)])
def test_open_readonly_checks_the_application_id_and_never_stamps(tmp_path, app_id, accepted):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.append_raw("broker", "kept", T0)
    j.close()
    raw = sqlite3.connect(p)
    raw.execute(f"PRAGMA application_id = {app_id}")  # 0: dziennik v1 sprzed znacznika; inny: cudza baza
    raw.commit()
    raw.close()
    if accepted:
        ro = Journal.open_readonly(str(p))
        assert [e.payload for e in ro.events()] == ["kept"]
        ro.close()
    else:
        with pytest.raises(JournalSchemaError, match="application_id"):
            Journal.open_readonly(str(p))
    assert raw_pragmas(p) == (1, app_id, "wal")  # tylko odczyt: znacznika nie dopisuje


def test_open_refuses_memory_database_because_wal_cannot_be_enabled():
    with pytest.raises(JournalError):
        Journal.open(":memory:")


def test_open_unreachable_path_raises_journal_error(tmp_path):
    with pytest.raises(JournalError):
        Journal.open(str(tmp_path / "missing_dir" / "j.sqlite"))


def test_reopen_existing_database_keeps_schema_and_data(tmp_path):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.append_raw("broker", "{}", T0)
    j.close()
    j2 = open_journal(p)
    assert j2.pragma("user_version") == 1
    assert [e.payload for e in j2.events()] == ["{}"]
    j2.close()


def test_reopen_keeps_wal_and_synchronous_full(tmp_path, monkeypatch):
    # synchronous dotyczy połączenia, nie pliku; ten build SQLite ma domyślnie FULL, więc obniżamy domyślną wartość
    # (jak w buildach z SQLITE_DEFAULT_WAL_SYNCHRONOUS=1), żeby test widział brak jawnego ustawienia
    real_connect = sqlite3.connect

    def lowered_default(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    monkeypatch.setattr(sqlite3, "connect", lowered_default)
    p = tmp_path / "j.sqlite"
    raw = sqlite3.connect(p)
    assert raw.execute("PRAGMA synchronous").fetchone()[0] == 1  # założenie testu działa
    raw.close()
    open_journal(p).close()
    j = open_journal(p)  # ponowne otwarcie istniejącego dziennika
    assert (j.pragma("journal_mode"), j.pragma("synchronous")) == ("wal", 2)
    j._conn = FailingConn(j._conn, "INSERT INTO raw_events", "ROLLBACK")
    with pytest.raises(JournalWriteError):
        j.append_raw("broker", "x", T0)
    assert (j.pragma("journal_mode"), j.pragma("synchronous")) == ("wal", 2)  # połączenie otwarte na nowo w procesie
    j.close()


def test_close_is_idempotent_and_blocks_further_use(tmp_path):
    j = open_journal(tmp_path / "j.sqlite")
    j.close()
    j.close()
    with pytest.raises(JournalWriteError):
        j.append_raw("broker", "{}", T0)
    with pytest.raises(JournalReadError):
        j.events()


# --- surowe zdarzenia ----------------------------------------------------------------------


def test_raw_event_is_stored_before_anything_else(tmp_path):
    j = Journal.open(str(tmp_path / "j.sqlite"))
    first = j.append_raw("broker", '{"a": 1}', T0)
    second = j.append_raw("broker", '{"a": 2}', T0)
    assert 0 < first < second
    j.close()


def test_events_roundtrip_filter_and_order(journal):
    journal.append_raw("broker", "r1", T0)
    journal.append_raw("event", '{"kind": "SLIPPAGE_GUARD"}', T0 + timedelta(seconds=1))
    journal.append_raw("broker", "", T0 + timedelta(seconds=2))  # pusta odpowiedź też jest zdarzeniem
    everything = journal.events()
    assert [e.payload for e in everything] == ["r1", '{"kind": "SLIPPAGE_GUARD"}', ""]
    assert [e.id for e in everything] == sorted(e.id for e in everything)
    assert everything[1] == RawEvent(everything[1].id, "event", '{"kind": "SLIPPAGE_GUARD"}', T0 + timedelta(seconds=1))
    assert [e.payload for e in journal.events("broker")] == ["r1", ""]
    assert journal.events("nonexistent") == []


def test_events_after_id_and_limit_page_through_in_id_order(journal):
    sources = ["broker", "event", "broker", "broker", "event"]
    ids = [journal.append_raw(src, f"p{i}", T0) for i, src in enumerate(sources)]
    assert [e.id for e in journal.events()] == ids  # domyślnie bez zmian: wszystko
    assert [e.id for e in journal.events(after_id=ids[1])] == ids[2:]
    assert [e.id for e in journal.events(limit=2)] == ids[:2]
    assert [e.id for e in journal.events(after_id=ids[0], limit=2)] == ids[1:3]
    assert [e.id for e in journal.events("broker", after_id=ids[0])] == [ids[2], ids[3]]
    assert [e.id for e in journal.events("broker", after_id=ids[2], limit=1)] == [ids[3]]
    assert [e.id for e in journal.events("event", after_id=0, limit=5)] == [ids[1], ids[4]]
    assert journal.events(after_id=ids[-1]) == [] and journal.events("nonexistent", limit=1) == []
    seen, cursor = [], 0  # strony po dwa: każde zdarzenie dokładnie raz
    while page := journal.events(after_id=cursor, limit=2):
        seen += [e.id for e in page]
        cursor = page[-1].id
    assert seen == ids
    with pytest.raises(TypeError):
        journal.events("broker", ids[0])  # after_id i limit tylko z nazwy


@pytest.mark.parametrize(
    "kwargs, error",
    [({"after_id": -1}, ValueError), ({"after_id": 2**63}, ValueError), ({"after_id": True}, TypeError),
     ({"after_id": 1.0}, TypeError), ({"after_id": "1"}, TypeError), ({"limit": 0}, ValueError),
     ({"limit": -1}, ValueError), ({"limit": 2**63}, ValueError), ({"limit": False}, TypeError),
     ({"limit": "2"}, TypeError)],
)  # fmt: skip
def test_events_rejects_a_bad_after_id_or_limit(journal, kwargs, error):
    journal.append_raw("broker", "x", T0)
    with pytest.raises(error):
        journal.events(**kwargs)


def test_append_raw_rejects_naive_timestamp_and_empty_source(journal):
    with pytest.raises(ValueError):
        journal.append_raw("broker", "x", datetime(2026, 11, 2, 7, 30))
    with pytest.raises(ValueError):
        journal.append_raw("", "x", T0)
    assert journal.events() == []


def test_concurrent_appends_get_unique_increasing_ids(journal):
    per_thread = 25
    results: dict[int, list[int]] = {}

    def worker(n: int) -> None:
        results[n] = [journal.append_raw(f"t{n}", f"{n}:{i}", T0) for i in range(per_thread)]

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [i for ids in results.values() for i in ids]
    assert len(all_ids) == len(set(all_ids)) == 8 * per_thread
    assert all(ids == sorted(ids) and len(set(ids)) == per_thread for ids in results.values())
    stored = journal.events()
    assert [e.id for e in stored] == sorted(all_ids)
    assert len({e.payload for e in stored}) == 8 * per_thread


# --- zlecenia ------------------------------------------------------------------------------


def test_order_roundtrip_preserves_exact_decimals_and_types(journal):
    o = make_order(size=Decimal("0.0010"), stop_distance=Decimal("20.50"), note="intent zapisany")
    journal.insert_order(o)
    got = journal.get_order(o.order_id)
    assert got == o
    assert str(got.size) == "0.0010" and str(got.stop_distance) == "20.50"  # dokładny zapis, bez float
    assert got.side is Side.BUY and got.state is OrderState.INTENT
    assert journal.get_order("gws-nope") is None


def test_insert_order_duplicate_id_raises_and_keeps_original(journal):
    journal.insert_order(make_order(note="pierwszy"))
    with pytest.raises(JournalWriteError):
        journal.insert_order(make_order(note="drugi"))
    assert journal.get_order("gws-20261102-DE40-1-entry").note == "pierwszy"


def test_update_order_changes_fields_persists_and_returns_record(tmp_path):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.insert_order(make_order())
    later = T0 + timedelta(seconds=3)
    updated = j.update_order(
        "gws-20261102-DE40-1-entry",
        state=OrderState.CONFIRMED,
        deal_reference="REF-1",
        deal_id="DEAL-1",
        note="ok",
        updated_at=later,
    )
    assert updated == make_order(
        state=OrderState.CONFIRMED, deal_reference="REF-1", deal_id="DEAL-1", note="ok", updated_at=later
    )
    j.close()
    j2 = open_journal(p)
    assert j2.get_order("gws-20261102-DE40-1-entry") == updated
    j2.close()


def test_update_order_requires_updated_at(journal):
    journal.insert_order(make_order())
    with pytest.raises(ValueError, match="updated_at required"):
        journal.update_order("gws-20261102-DE40-1-entry", state=OrderState.SUBMITTING)
    assert journal.get_order("gws-20261102-DE40-1-entry").state is OrderState.INTENT


@pytest.mark.parametrize("field", ["bogus", "epic", "side", "size", "stop_distance", "created_at"])
def test_update_order_rejects_unknown_and_immutable_fields(journal, field):
    journal.insert_order(make_order())
    with pytest.raises(ValueError):
        journal.update_order("gws-20261102-DE40-1-entry", updated_at=T0, **{field: "x"})
    assert journal.get_order("gws-20261102-DE40-1-entry") == make_order()


def test_update_order_unknown_order_raises_key_error(journal):
    with pytest.raises(KeyError):
        journal.update_order("gws-missing", state=OrderState.SUBMITTING, updated_at=T0)


def test_update_order_validates_values_and_changes_nothing_on_error(journal):
    journal.insert_order(make_order())
    with pytest.raises(TypeError):
        journal.update_order("gws-20261102-DE40-1-entry", state="CONFIRMED", updated_at=T0)  # napis zamiast enum
    with pytest.raises(ValueError):
        journal.update_order("gws-20261102-DE40-1-entry", state=OrderState.CONFIRMED, updated_at=datetime(2026, 1, 1))
    assert journal.get_order("gws-20261102-DE40-1-entry") == make_order()


def test_update_order_write_failure_is_wrapped_and_changes_nothing(journal):
    journal.insert_order(make_order())
    journal._conn.execute(
        "CREATE TRIGGER no_update BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(JournalWriteError):
        journal.update_order("gws-20261102-DE40-1-entry", state=OrderState.SUBMITTING, updated_at=T0)
    assert journal.get_order("gws-20261102-DE40-1-entry").state is OrderState.INTENT


TERMINAL_ORDER_STATES = {
    OrderState.CONFIRMED, OrderState.REJECTED, OrderState.NOT_PLACED, OrderState.CANCELED, OrderState.EXPIRED,
}


def test_terminal_order_states_are_exactly_the_states_without_exits_in_the_fsm():
    from gwstock.orders.fsm import ALLOWED_ORDER_TRANSITIONS

    assert {s for s, exits in ALLOWED_ORDER_TRANSITIONS.items() if not exits} == TERMINAL_ORDER_STATES


def test_unresolved_orders_includes_working_partial_and_canceling(journal):
    # każdy stan poza końcowymi (także rozszerzone, nieużywane w v1) blokuje podwójne wejście
    states = [OrderState.CONFIRMED, *OrderState]  # pierwszy wiersz końcowy: kolejność wstawienia, nie stanów
    for i, st in enumerate(states, start=1):
        journal.insert_order(make_order(f"gws-20261102-DE40-{i}-entry", state=st))
    unresolved = journal.unresolved_orders()
    assert [o.state for o in unresolved] == [
        OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN,
        OrderState.WORKING, OrderState.PARTIAL, OrderState.CANCELING,
    ]  # fmt: skip
    assert [o.order_id for o in unresolved] == [
        f"gws-20261102-DE40-{i}-entry" for i, st in enumerate(states, start=1) if st not in TERMINAL_ORDER_STATES
    ]


@pytest.mark.parametrize("bad", ["WAT", "confirmed", "CONFIRMED ", ""])
def test_unresolved_orders_with_an_unknown_state_raises_corrupt(journal, bad):
    journal.insert_order(make_order("gws-20261102-DE40-1-entry", state=OrderState.CONFIRMED))
    journal.insert_order(make_order("gws-20261102-DE40-2-entry", state=OrderState.CONFIRMED))
    journal._conn.execute("UPDATE orders SET state = ? WHERE order_id = 'gws-20261102-DE40-2-entry'", (bad,))
    with pytest.raises(JournalCorruptError):
        journal.unresolved_orders()  # nieczytelny stan nie znika z ochrony przed podwójnym wejściem


def test_unresolved_orders_empty_when_everything_is_settled(journal):
    journal.insert_order(make_order(state=OrderState.CONFIRMED))
    assert journal.unresolved_orders() == []


def test_count_orders_and_next_seq_are_per_trading_day_and_ignore_state(journal):
    assert journal.count_orders(DAY) == 0 and journal.next_seq(DAY) == 1
    journal.insert_order(make_order("gws-20261102-DE40-1-entry", state=OrderState.REJECTED))
    journal.insert_order(make_order("gws-20261102-DE40-2-entry", state=OrderState.NOT_PLACED))
    journal.insert_order(make_order("gws-20261103-DE40-1-entry", state=OrderState.CONFIRMED))
    journal.insert_order(make_order("manual-20261102-x"))  # cudzy prefix nie jest liczony
    assert journal.count_orders(DAY) == 2 and journal.next_seq(DAY) == 3
    assert journal.count_orders(NEXT_DAY) == 1 and journal.next_seq(NEXT_DAY) == 2
    assert journal.count_orders(date(2026, 11, 4)) == 0


def test_count_orders_rejects_datetime_day(journal):
    with pytest.raises(TypeError):
        journal.count_orders(T0)  # datetime jest podklasą date, ale to nie jest dzień


def test_count_entry_orders_counts_only_the_entry_role_of_the_day_in_any_state(journal):
    assert journal.count_entry_orders(DAY) == 0
    journal.insert_order(make_order("gws-20261102-DE40-1-smoke", state=OrderState.CONFIRMED))
    journal.insert_order(make_order("gws-20261102-DE40-2-entry", state=OrderState.REJECTED))
    journal.insert_order(make_order("gws-20261102-DE40-3-entry", state=OrderState.INTENT))
    journal.insert_order(make_order("gws-20261102-DE40-4-reentry", state=OrderState.CONFIRMED))  # inna rola
    journal.insert_order(make_order("gws-20261103-DE40-1-entry", state=OrderState.CONFIRMED))
    journal.insert_order(make_order("manual-20261102-x-entry"))  # cudzy prefix nie jest liczony
    assert journal.count_entry_orders(DAY) == 2
    assert journal.count_entry_orders(NEXT_DAY) == 1
    # numeracja dnia nadal liczy wszystkie role: identyfikator zlecenia próby smoke nie wraca
    assert journal.count_orders(DAY) == 4 and journal.next_seq(DAY) == 5


def test_count_entry_orders_rejects_datetime_day(journal):
    with pytest.raises(TypeError):
        journal.count_entry_orders(T0)


MIXED_ORDER_IDS = [
    "gws-20261102-DE40-1-entry", "gws-20261102-DE40-2-smoke", "gws-20261102-DE40-3-ENTRY", "gws-20261102-DE40-4-xentry",
    "gws-20261102-entry", "gws-20261102-", "gws-20261102-é-entry", "gws-20261102-\x7f", "gws-20261102-DE40-5-entry ",
    "gws-20261102", "gws-20261102.", "gws-20261102.x-entry", "gws-202611020-DE40-1-entry", "gws-20261102/-entry",
    "GWS-20261102-DE40-1-entry",
    "gws-20261101-DE40-1-entry", "gws-20261103-DE40-1-entry", "gws-20261103-DE40-2-entry", "xgws-20261102-DE40-1-entry",
    "manual-20261102-x-entry",
]  # fmt: skip


def test_count_orders_and_entry_orders_give_the_prefix_counts_on_a_mixed_journal(journal):
    for i, order_id in enumerate(MIXED_ORDER_IDS):
        journal.insert_order(make_order(order_id, state=list(OrderState)[i % len(OrderState)]))
    for day in (date(2026, 11, 1), DAY, NEXT_DAY, date(2026, 11, 4)):
        prefix = f"gws-{day:%Y%m%d}-"
        ours = [o for o in MIXED_ORDER_IDS if o.startswith(prefix)]
        assert journal.count_orders(day) == len(ours)
        assert journal.count_entry_orders(day) == len([o for o in ours if o.endswith("-entry")])
    assert journal.count_orders(DAY) == 9 and journal.count_entry_orders(DAY) == 3  # dzień z przypadkami brzegowymi


class RecordingConn:
    """Proxy połączenia SQLite zapisujący wykonane instrukcje (do planu zapytania)."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self.real = real
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self.real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_count_orders_and_entry_orders_search_the_primary_key_instead_of_scanning(journal):
    for order_id in MIXED_ORDER_IDS:
        journal.insert_order(make_order(order_id))
    recording = RecordingConn(journal._conn)
    journal._conn = recording
    journal.count_orders(DAY)
    journal.count_entry_orders(DAY)
    journal._conn = recording.real
    assert len(recording.statements) == 2
    for sql, params in recording.statements:
        plan = " | ".join(row[-1] for row in journal._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
        assert plan.startswith("SEARCH orders USING") and "SCAN" not in plan, plan


# --- pozycje -------------------------------------------------------------------------------


def test_position_upsert_replaces_and_reads_back(journal):
    assert journal.current_position("DE40") is None
    first = make_position(state=PositionState.ENTERING, deal_id=None, open_level=None, stop_level=None, opened_at=None)
    journal.upsert_position(first)
    assert journal.current_position("DE40") == first
    second = make_position()
    journal.upsert_position(second)
    assert journal.current_position("DE40") == second
    assert str(journal.current_position("DE40").open_level) == "25782.30"
    flat = PositionRecord("DE40", PositionState.FLAT, None, None, None, None, None, None)
    journal.upsert_position(flat)
    assert journal.current_position("DE40") == flat
    assert journal.current_position("OTHER") is None


# --- licznik strat -------------------------------------------------------------------------


def test_daily_loss_survives_reopen_and_is_keyed_by_exit_day(tmp_path):
    p = str(tmp_path / "j.sqlite")
    j = Journal.open(p)
    j.add_realized_pnl(date(2026, 11, 2), Decimal("-3.5"))
    j.close()
    j2 = Journal.open(p)
    assert j2.realized_pnl(date(2026, 11, 2)) == Decimal("-3.5")
    assert j2.realized_pnl(date(2026, 11, 3)) == Decimal("0")
    j2.close()


def test_realized_pnl_accumulates_exactly_per_day_and_in_total(journal):
    assert journal.realized_pnl(DAY) == Decimal("0") and journal.realized_pnl_total() == Decimal("0")
    journal.add_realized_pnl(DAY, Decimal("0.1"))
    journal.add_realized_pnl(DAY, Decimal("0.2"))
    journal.add_realized_pnl(NEXT_DAY, Decimal("-1.25"))
    assert journal.realized_pnl(DAY) == Decimal("0.3")  # dokładnie, bez błędu float
    assert journal.realized_pnl(NEXT_DAY) == Decimal("-1.25")
    assert journal.realized_pnl_total() == Decimal("-0.95")


@pytest.mark.parametrize("net", [0.5, "1.0", None, Decimal("NaN"), Decimal("Infinity")])
def test_add_realized_pnl_rejects_non_decimal_or_non_finite(journal, net):
    with pytest.raises((TypeError, ValueError)):
        journal.add_realized_pnl(DAY, net)
    assert journal.realized_pnl_total() == Decimal("0")


def test_add_realized_pnl_rejects_datetime_as_day(journal):
    with pytest.raises(TypeError):
        journal.add_realized_pnl(T0, Decimal("1"))
    assert journal.realized_pnl_total() == Decimal("0")


# --- księga transakcji ---------------------------------------------------------------------


def test_record_trade_books_trade_and_pnl_in_exit_day_and_returns_true(journal):
    t = make_trade()
    assert journal.record_trade(t, "DEAL-1", DAY) is True
    assert journal.trades() == [t]
    assert journal.realized_pnl(DAY) == Decimal("-0.0240")
    assert journal.realized_pnl_total() == Decimal("-0.0240")


def test_record_trade_is_idempotent_per_deal_id(journal):
    t = make_trade()
    assert journal.record_trade(t, "DEAL-1", DAY) is True
    assert journal.record_trade(t, "DEAL-1", DAY) is False
    assert journal.record_trade(make_trade(net_pnl=Decimal("-9")), "DEAL-1", NEXT_DAY) is False  # inny dzień też nie
    assert journal.trades() == [t]
    assert journal.realized_pnl(DAY) == Decimal("-0.0240")
    assert journal.realized_pnl(NEXT_DAY) == Decimal("0")
    assert journal.realized_pnl_total() == Decimal("-0.0240")


def test_has_trade_tells_whether_a_deal_is_already_booked(journal):
    assert journal.has_trade("DEAL-1") is False
    journal.record_trade(make_trade(), "DEAL-1", DAY)
    assert journal.has_trade("DEAL-1") is True
    assert journal.has_trade("DEAL-2") is False


def test_has_trade_rejects_an_empty_deal_id_and_reports_read_failures(journal):
    with pytest.raises(ValueError):
        journal.has_trade("")
    journal.close()
    with pytest.raises(JournalReadError):
        journal.has_trade("DEAL-1")


def test_record_trade_different_deals_accumulate_in_order(journal):
    a, b = make_trade(net_pnl=Decimal("-1.5")), make_trade(net_pnl=Decimal("0.5"), exit_reason="MANUAL")
    assert journal.record_trade(a, "DEAL-1", DAY) and journal.record_trade(b, "DEAL-2", DAY)
    assert journal.trades() == [a, b]  # kolejność wstawienia
    assert journal.realized_pnl(DAY) == Decimal("-1.0")


def test_record_trade_is_atomic_when_pnl_update_fails_for_new_day(journal):
    for ev in ("INSERT", "UPDATE"):
        journal._conn.execute(
            f"CREATE TRIGGER boom_{ev} BEFORE {ev} ON realized_pnl BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(), "DEAL-1", DAY)
    assert journal.trades() == []  # wiersz transakcji wycofany razem z nieudaną aktualizacją pnl
    assert journal.realized_pnl_total() == Decimal("0")
    # po awarii dziennik nie ma otwartej transakcji i działa dalej
    journal._conn.execute("DROP TRIGGER boom_INSERT")
    journal._conn.execute("DROP TRIGGER boom_UPDATE")
    assert journal.record_trade(make_trade(), "DEAL-1", DAY) is True


def test_record_trade_is_atomic_when_pnl_update_fails_for_existing_day(journal):
    journal.add_realized_pnl(DAY, Decimal("-1"))
    journal._conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON realized_pnl BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(), "DEAL-1", DAY)
    assert journal.trades() == []
    assert journal.realized_pnl(DAY) == Decimal("-1")


def test_record_trade_writes_its_flags_in_the_same_transaction(journal):
    # BUG-E2E-2: znacznik wiersza księgi (np. szacunek właściciela) powstaje razem z transakcją albo wcale
    journal._conn.execute("CREATE TRIGGER boom BEFORE INSERT ON meta BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(), "DEAL-1", DAY, flags="ESTIMATED RESOLVED_BY_OWNER")
    assert journal.trades() == [] and journal.has_trade("DEAL-1") is False  # awaria przed COMMIT: ani wiersz...
    assert journal.realized_pnl_total() == Decimal("0") and journal.trade_flags("DEAL-1") is None  # ...ani znacznik
    journal._conn.execute("DROP TRIGGER boom")
    assert journal.record_trade(make_trade(), "DEAL-1", DAY, flags="ESTIMATED RESOLVED_BY_OWNER") is True
    assert journal.trade_flags("DEAL-1") == "ESTIMATED RESOLVED_BY_OWNER"
    # już zaksięgowane: nic się nie zmienia, także znacznik
    assert journal.record_trade(make_trade(), "DEAL-1", DAY, flags="RESOLVED_BY_OWNER") is False
    assert journal.trade_flags("DEAL-1") == "ESTIMATED RESOLVED_BY_OWNER"


def test_trade_flags_are_none_without_flags_and_listed_with_each_trade_in_booking_order(journal):
    a, b, c = make_trade(), make_trade(net_pnl=Decimal("-1")), make_trade(exit_reason="MANUAL")
    assert journal.record_trade(a, "DEAL-1", DAY) is True  # zwykłe księgowanie: bez znacznika
    assert journal.record_trade(b, "DEAL-2", DAY, flags="RESOLVED_BY_OWNER") is True
    assert journal.record_trade(c, "DEAL-3", NEXT_DAY, flags="ESTIMATED RESOLVED_BY_OWNER") is True
    assert journal.trade_flags("DEAL-1") is None and journal.trade_flags("DEAL-9") is None
    # bez argumentu: każda transakcja razem ze swoimi znacznikami, w kolejności `trades()`
    assert journal.trade_flags() == [(a, None), (b, "RESOLVED_BY_OWNER"), (c, "ESTIMATED RESOLVED_BY_OWNER")]
    assert journal.get_meta("trade_flags:DEAL-1") is None


def test_trade_flags_reads_the_ledger_and_its_flags_in_one_statement(journal):
    # m6: dwa osobne odczyty (`trades()`, potem znaczniki) przy piszącym silniku mogłyby się rozjechać
    assert journal.record_trade(make_trade(), "DEAL-1", DAY, flags="ESTIMATED RESOLVED_BY_OWNER") is True
    statements: list[str] = []
    real = journal._conn

    class Counting:
        def execute(self, sql, params=()):
            statements.append(sql)
            return real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real, name)

    journal._conn = Counting()
    try:
        assert journal.trade_flags() == [(make_trade(), "ESTIMATED RESOLVED_BY_OWNER")]
    finally:
        journal._conn = real
    assert len(statements) == 1


@pytest.mark.parametrize("flags", ["estimated", "ESTIMATED  RESOLVED_BY_OWNER", " ESTIMATED", "ESTIMATED\n", 7])
def test_record_trade_rejects_malformed_flags_before_writing(journal, flags):
    with pytest.raises(TypeError if isinstance(flags, int) else ValueError):  # zły typ: TypeError jak reszta dziennika
        journal.record_trade(make_trade(), "DEAL-1", DAY, flags=flags)
    assert journal.trades() == [] and journal.trade_flags() == []


def test_trades_roundtrip_preserves_exact_decimals_and_timestamps(journal):
    t = make_trade(entry_fill=Decimal("25782.30"), financing=Decimal("0E-8"), latency_ms=1234)
    journal.record_trade(t, "DEAL-1", DAY)
    got = journal.trades()[0]
    assert got == t
    assert str(got.entry_fill) == "25782.30" and str(got.financing) == "0E-8"
    assert got.signal_ts == T0 and got.exit_ts == T0 + timedelta(hours=1) and got.latency_ms == 1234


# --- kill switch ---------------------------------------------------------------------------


def test_kill_state_defaults_to_none(journal):
    assert journal.kill_state() == KillState(KillLevel.NONE, "", None)


def test_kill_state_survives_reopen(tmp_path):
    p = str(tmp_path / "j.sqlite")
    j = Journal.open(p)
    j.set_kill(KillLevel.SOFT, "daily loss", T0)
    j.close()
    j2 = Journal.open(p)
    assert j2.kill_state() == KillState(KillLevel.SOFT, "daily loss", T0)
    assert j2.kill_state().level is KillLevel.SOFT
    j2.close()


def test_set_kill_overwrites_level_reason_and_time(journal):
    journal.set_kill(KillLevel.SOFT, "daily loss", T0)
    later = T0 + timedelta(minutes=5)
    journal.set_kill(KillLevel.HARD, "drawdown", later)
    assert journal.kill_state() == KillState(KillLevel.HARD, "drawdown", later)
    journal.set_kill(KillLevel.NONE, "owner reset", later + timedelta(hours=1))  # zdjęcie poziomu to jawna akcja
    assert journal.kill_state() == KillState(KillLevel.NONE, "owner reset", later + timedelta(hours=1))


def test_set_kill_rejects_plain_int_level(journal):
    with pytest.raises(TypeError):
        journal.set_kill(1, "x", T0)
    assert journal.kill_state().level is KillLevel.NONE


# --- meta ----------------------------------------------------------------------------------


def test_meta_get_set_overwrite_and_persist(tmp_path):
    p = str(tmp_path / "j.sqlite")
    j = Journal.open(p)
    assert j.get_meta("mismatch_since") is None
    j.set_meta("mismatch_since", "2026-11-02T07:30:00+00:00")
    j.set_meta("mismatch_since", "2026-11-02T07:31:00+00:00")
    j.set_meta("last_closed_check", "")
    j.close()
    j2 = Journal.open(p)
    assert j2.get_meta("mismatch_since") == "2026-11-02T07:31:00+00:00"
    assert j2.get_meta("last_closed_check") == ""
    j2.close()


def test_meta_rejects_non_string_value(journal):
    with pytest.raises(TypeError):
        journal.set_meta("k", 1)
    assert journal.get_meta("k") is None


# --- trwałość całości po ponownym otwarciu -------------------------------------------------


def test_everything_survives_close_and_reopen(tmp_path):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    order = make_order(state=OrderState.CONFIRMED, deal_reference="REF-1", deal_id="DEAL-1")
    j.insert_order(order)
    position = make_position()
    j.upsert_position(position)
    j.set_kill(KillLevel.HARD, "drawdown", T0)
    j.set_meta("k", "v")
    trade = make_trade()
    j.record_trade(trade, "DEAL-1", DAY)
    j.add_realized_pnl(NEXT_DAY, Decimal("-2"))
    j.append_raw("broker", "raw", T0)
    j.close()

    j2 = open_journal(p)
    assert j2.get_order(order.order_id) == order
    assert j2.current_position("DE40") == position
    assert j2.kill_state() == KillState(KillLevel.HARD, "drawdown", T0)
    assert j2.get_meta("k") == "v"
    assert j2.trades() == [trade]
    assert j2.realized_pnl(DAY) == Decimal("-0.0240")
    assert j2.realized_pnl(NEXT_DAY) == Decimal("-2")
    assert j2.realized_pnl_total() == Decimal("-2.0240")
    assert j2.count_orders(DAY) == 1
    assert [e.payload for e in j2.events()] == ["raw"]
    assert j2.record_trade(trade, "DEAL-1", DAY) is False  # idempotencja przeżywa restart
    j2.close()


# --- awarie zapisu i odczytu ---------------------------------------------------------------


def test_write_failure_raises_journal_write_error(tmp_path, monkeypatch):
    j = Journal.open(str(tmp_path / "j.sqlite"))
    monkeypatch.setattr(j, "_conn", FailingConn(j._conn, "BEGIN"))  # baza zablokowana
    with pytest.raises(JournalWriteError):
        j.append_raw("broker", "{}", T0)
    with pytest.raises(JournalWriteError):
        j.insert_order(make_order())
    with pytest.raises(JournalWriteError):
        j.set_kill(KillLevel.SOFT, "x", T0)
    monkeypatch.undo()
    assert j.events() == [] and j.get_order(make_order().order_id) is None
    assert j.kill_state().level is KillLevel.NONE
    j.close()


def test_mid_transaction_failure_rolls_back_and_wraps(journal, monkeypatch):
    monkeypatch.setattr(journal, "_conn", FailingConn(journal._conn, "INSERT INTO realized_pnl"))
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(), "DEAL-1", DAY)
    monkeypatch.undo()
    assert journal.trades() == [] and journal.realized_pnl_total() == Decimal("0")
    assert journal.record_trade(make_trade(), "DEAL-1", DAY) is True


@pytest.mark.parametrize("mode", ["ABORT", "FAIL", "ROLLBACK"])
def test_a_failure_in_the_middle_of_a_write_rolls_back_all(tmp_path, mode):
    # prawdziwy błąd SQLite w DRUGIEJ instrukcji zapisu (wiersz transakcji już wstawiony), nie odmowa przy BEGIN
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j._conn.execute(f"CREATE TRIGGER boom BEFORE INSERT ON realized_pnl BEGIN SELECT RAISE({mode}, 'boom'); END")
    with pytest.raises(JournalWriteError) as caught:
        j.record_trade(make_trade(), "DEAL-1", DAY)
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    assert j._conn.in_transaction is False
    assert j.trades() == [] and j.has_trade("DEAL-1") is False and j.realized_pnl_total() == Decimal("0")
    raw = sqlite3.connect(p)  # inne połączenie też nic nie widzi
    assert raw.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    raw.close()
    j._conn.execute("DROP TRIGGER boom")
    assert j.record_trade(make_trade(), "DEAL-1", DAY) is True
    j.close()


def test_after_a_failed_rollback_reads_return_only_committed_data(tmp_path):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    booked = make_trade()
    j.record_trade(booked, "DEAL-0", DAY)
    # wiersz transakcji wstawiony, wynik dnia zawodzi, a potem zawodzi też ROLLBACK: transakcja zostaje otwarta
    j._conn = FailingConn(j._conn, "INSERT INTO realized_pnl", "ROLLBACK")
    with pytest.raises(JournalWriteError):
        j.record_trade(make_trade(net_pnl=Decimal("-9")), "DEAL-1", NEXT_DAY)
    # żaden odczyt nie widzi niezatwierdzonego wiersza (połączenie z otwartą transakcją jest porzucone)
    assert j.trades() == [booked] and j.has_trade("DEAL-1") is False
    assert j.realized_pnl(NEXT_DAY) == Decimal("0") and j.realized_pnl_total() == Decimal("-0.0240")
    # nowe połączenie ma te same pragmy i działa dalej, także zapis tej samej transakcji
    assert j.pragma("journal_mode") == "wal" and j.pragma("synchronous") == 2
    assert j.record_trade(make_trade(net_pnl=Decimal("-9")), "DEAL-1", NEXT_DAY) is True
    j.close()
    other = open_journal(p)
    assert [t.net_pnl for t in other.trades()] == [Decimal("-0.0240"), Decimal("-9")]
    assert other.realized_pnl_total() == Decimal("-9.0240")
    other.close()


@pytest.mark.parametrize("replacement", ["missing", "empty"])
def test_a_journal_that_cannot_reopen_after_a_failed_rollback_fails_every_call(tmp_path, replacement):
    p = tmp_path / "j.sqlite"
    j = open_journal(p)
    j.append_raw("broker", "kept", T0)
    j._conn = FailingConn(j._conn, "INSERT INTO orders", "ROLLBACK")
    with pytest.raises(JournalWriteError):
        j.insert_order(make_order())
    aside = tmp_path / "aside.sqlite"
    p.rename(aside)
    if replacement == "empty":
        p.write_bytes(b"")  # ponowne otwarcie nie tworzy schematu na cudzym (pustym) pliku
    for _ in range(2):  # każde wywołanie, nie tylko pierwsze
        with pytest.raises(JournalReadError):
            j.events()
        with pytest.raises(JournalWriteError):
            j.append_raw("broker", "lost", T0)
    assert p.exists() is (replacement == "empty")  # brak pliku: nie utworzono nowego
    if replacement == "empty":
        assert p.read_bytes() == b""
        p.unlink()
    aside.rename(p)
    assert [e.payload for e in j.events()] == ["kept"]  # plik wrócił: następny dostęp otwiera go ponownie
    assert j.get_order(make_order().order_id) is None
    j.close()
    j.close()
    with pytest.raises(JournalReadError):
        j.events()  # jawne zamknięcie nie otwiera ponownie


def test_write_errors_outside_sqlite_are_journal_write_errors(journal):
    huge = make_trade()
    object.__setattr__(huge, "latency_ms", 2**63)  # z pominięciem walidacji rekordu: SQLite nie zwiąże tej liczby
    with pytest.raises(JournalWriteError) as caught:
        journal.record_trade(huge, "DEAL-1", DAY)
    assert isinstance(caught.value.__cause__, OverflowError)
    with pytest.raises(JournalWriteError) as caught:
        journal.append_raw("broker", "\ud800", T0)  # samotny surogat: nie da się go zakodować w UTF-8
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)
    with pytest.raises(JournalWriteError):
        journal.set_meta("k", "\udcff")
    with pytest.raises(JournalWriteError):
        journal.insert_order(make_order(note="\ud800"))
    journal.add_realized_pnl(DAY, Decimal("1E+100"))
    with pytest.raises(JournalWriteError) as caught:
        journal.add_realized_pnl(DAY, Decimal("1E-100"))  # suma nie mieści się dokładnie w 60 cyfrach
    assert type(caught.value.__cause__) is JournalError
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(net_pnl=Decimal("1E-100")), "DEAL-2", DAY)
    # nic nie zapisano (także wiersz transakcji przed nieudanym licznikiem), a dziennik działa dalej
    assert journal.trades() == [] and journal.events() == [] and journal.get_meta("k") is None
    assert journal.get_order(make_order().order_id) is None and journal.realized_pnl(DAY) == Decimal("1E+100")
    assert journal.record_trade(make_trade(), "DEAL-1", NEXT_DAY) is True


def test_latency_ms_is_bounded(journal):
    top = make_trade(latency_ms=2**63 - 1)  # największa liczba, którą SQLite przechowa jako INTEGER
    assert journal.record_trade(top, "DEAL-1", DAY) is True
    assert journal.trades()[0].latency_ms == 2**63 - 1
    with pytest.raises(ValueError, match="latency_ms"):
        make_trade(latency_ms=2**63)


def test_real_sqlite_readonly_failure_is_wrapped_and_nothing_is_written(journal):
    journal._conn.execute("PRAGMA query_only = ON")  # SQLite sam odmawia zapisu: „attempt to write a readonly database”
    with pytest.raises(JournalWriteError) as caught:
        journal.append_raw("broker", "{}", T0)
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    with pytest.raises(JournalWriteError):
        journal.record_trade(make_trade(), "DEAL-1", DAY)
    with pytest.raises(JournalWriteError):
        journal.add_realized_pnl(DAY, Decimal("-1"))
    journal._conn.execute("PRAGMA query_only = OFF")
    assert journal.events() == [] and journal.trades() == [] and journal.realized_pnl_total() == Decimal("0")
    assert journal.append_raw("broker", "{}", T0) > 0  # po awarii dziennik działa dalej


def test_read_failure_raises_journal_read_error(journal):
    journal._conn.execute("DROP TABLE orders")  # prawdziwy błąd SQLite: „no such table”
    with pytest.raises(JournalReadError) as caught:
        journal.get_order("x")
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    with pytest.raises(JournalReadError):
        journal.unresolved_orders()


def test_error_hierarchy():
    for cls in (JournalWriteError, JournalReadError, JournalCorruptError, JournalSchemaError):
        assert issubclass(cls, JournalError)
    assert not issubclass(JournalError, sqlite3.Error)


@pytest.mark.parametrize(
    "column, bad",
    [("size", "abc"), ("size", "NaN"), ("stop_distance", "Infinity"), ("state", "WAT"), ("side", "UP"),
     ("created_at", "yesterday"), ("created_at", "2026-11-02T07:30:00")],
)
def test_corrupt_stored_order_raises_journal_corrupt_error(journal, column, bad):
    journal.insert_order(make_order(state=OrderState.UNKNOWN))
    journal._conn.execute(f"UPDATE orders SET {column} = ?", (bad,))
    with pytest.raises(JournalCorruptError):
        journal.get_order("gws-20261102-DE40-1-entry")
    with pytest.raises(JournalCorruptError):
        journal.unresolved_orders()


def test_corrupt_pnl_text_raises_journal_corrupt_error_and_blocks_further_booking(journal):
    journal.add_realized_pnl(DAY, Decimal("-1"))
    journal._conn.execute("UPDATE realized_pnl SET net = 'oops'")
    with pytest.raises(JournalCorruptError):
        journal.realized_pnl(DAY)
    with pytest.raises(JournalCorruptError):
        journal.realized_pnl_total()
    with pytest.raises(JournalCorruptError):
        journal.record_trade(make_trade(), "DEAL-1", DAY)
    assert journal.trades() == []  # nic nie zapisano na uszkodzonym liczniku


def test_corrupt_kill_level_raises_journal_corrupt_error(journal):
    journal.set_kill(KillLevel.SOFT, "x", T0)
    journal._conn.execute("UPDATE kill_state SET level = 7")
    with pytest.raises(JournalCorruptError):
        journal.kill_state()


# --- walidacja rekordów --------------------------------------------------------------------


def test_records_are_frozen(journal):
    o = make_order()
    with pytest.raises(FrozenInstanceError):
        o.state = OrderState.CONFIRMED  # type: ignore[misc]


def test_order_record_rejects_float_naive_time_and_wrong_enum():
    with pytest.raises(TypeError):
        make_order(size=0.001)
    with pytest.raises(ValueError):
        make_order(created_at=datetime(2026, 11, 2, 7, 30))
    with pytest.raises(ValueError):
        make_order(updated_at=datetime(2026, 11, 2, 8, 30, tzinfo=timezone(timedelta(hours=1))))
    with pytest.raises(TypeError):
        make_order(state="INTENT")
    with pytest.raises(TypeError):
        make_order(side="BUY")
    with pytest.raises(ValueError):
        make_order(size=Decimal("0"))
    with pytest.raises(ValueError):
        make_order(order_id="")
    with pytest.raises(ValueError):
        make_order(stop_distance=Decimal("NaN"))


def test_position_record_rejects_float_and_wrong_types():
    with pytest.raises(TypeError):
        make_position(open_level=25782.3)
    with pytest.raises(TypeError):
        make_position(state="OPEN")
    with pytest.raises(ValueError):
        make_position(opened_at=datetime(2026, 11, 2, 7, 30))


def test_trade_record_rejects_float_naive_time_and_bad_latency():
    with pytest.raises(TypeError):
        make_trade(net_pnl=-0.024)
    with pytest.raises(ValueError):
        make_trade(exit_ts=datetime(2026, 11, 2, 8, 30))
    with pytest.raises(TypeError):
        make_trade(latency_ms=2000.0)
    with pytest.raises(ValueError):
        make_trade(latency_ms=-1)
    with pytest.raises(TypeError):
        make_trade(side="BUY")


def test_trade_record_has_exactly_the_nineteen_ledger_fields_in_csv_order():
    from dataclasses import fields

    assert [f.name for f in fields(TradeRecord)] == [
        "instrument", "side", "size", "decision_bid", "decision_ask", "entry_fill", "exit_fill",
        "gross_pnl", "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl",
        "signal_ts", "submit_ts", "confirm_ts", "exit_ts", "latency_ms", "exit_reason",
    ]  # fmt: skip


def test_record_trade_rejects_empty_deal_id_and_datetime_exit_day(journal):
    with pytest.raises(ValueError):
        journal.record_trade(make_trade(), "", DAY)
    with pytest.raises(TypeError):
        journal.record_trade(make_trade(), "DEAL-1", T0)
    assert journal.trades() == [] and journal.realized_pnl_total() == Decimal("0")


def test_count_smoke_trades_recognises_smoke_entries_by_order_or_entry_pointer(journal):
    # transakcje próby smoke (L0) nie są sesjami strategii: licznik sesji live je pomija
    journal.record_trade(make_trade(), "DEAL-1", DAY)  # wejście strategii
    journal.insert_order(make_order("gws-20261102-DE40-1-entry", state=OrderState.CONFIRMED, deal_id="DEAL-1"))
    journal.record_trade(make_trade(), "DEAL-2", DAY)  # smoke rozpoznany po zleceniu (rola smoke, ten deal_id)
    journal.insert_order(make_order("gws-20261102-DE40-2-smoke", state=OrderState.CONFIRMED, deal_id="DEAL-2"))
    journal.record_trade(make_trade(), "DEAL-3", NEXT_DAY)  # smoke rozpoznany po wskaźniku wykonawcy
    journal.set_meta("entry_by_deal:DEAL-3", "gws-20261103-DE40-1-smoke")
    journal.record_trade(make_trade(), "DEAL-4", NEXT_DAY)  # wskaźnik na wejście strategii
    journal.set_meta("entry_by_deal:DEAL-4", "gws-20261103-DE40-2-entry")
    journal.insert_order(make_order("gws-20261103-DE40-9-smoke", state=OrderState.REJECTED))  # bez transakcji
    assert journal.count_smoke_trades() == 2
    assert len(journal.trades()) == 4


# --- liczniki i księga per instrument (wiele pasm; zadanie 5) -----------------------------------------------------


def seed_orders(journal, rows) -> None:
    """Zlecenia (order_id, stan) z `epic` wziętym z segmentu instrumentu identyfikatora `gws-DZIEŃ-EPIC-SEQ-ROLA`."""
    for order_id, state in rows:
        journal.insert_order(make_order(order_id, epic=order_id.split("-")[2], state=state))


def test_count_entry_orders_with_an_epic_counts_only_that_instrument_in_any_state(journal):
    seed_orders(journal, [
        ("gws-20261102-DE40-1-entry", OrderState.REJECTED),
        ("gws-20261102-US500-2-entry", OrderState.CONFIRMED),
        ("gws-20261102-US500-3-entry", OrderState.NOT_PLACED),
        ("gws-20261102-US500-4-smoke", OrderState.CONFIRMED),  # inna rola: nie jest dziennym wejściem
        ("gws-20261102-DE40-5-smoke", OrderState.CONFIRMED),
        ("gws-20261103-US500-1-entry", OrderState.INTENT),
    ])  # fmt: skip
    assert journal.count_entry_orders(DAY, "DE40") == 1
    assert journal.count_entry_orders(DAY, "US500") == 2
    assert journal.count_entry_orders(DAY, "EURUSD") == 0  # instrument bez wejść
    assert journal.count_entry_orders(NEXT_DAY, "US500") == 1 and journal.count_entry_orders(NEXT_DAY, "DE40") == 0
    # bez instrumentu zachowanie jak dotąd: wszystkie instrumenty razem (także jawne None)
    assert journal.count_entry_orders(DAY) == 3 and journal.count_entry_orders(DAY, None) == 3
    assert journal.count_entry_orders(DAY, "DE40") + journal.count_entry_orders(DAY, "US500") == 3


def test_count_entry_orders_with_an_epic_does_not_mix_instruments_that_share_a_name_prefix(journal):
    seed_orders(journal, [
        ("gws-20261102-US5-1-entry", OrderState.CONFIRMED),
        ("gws-20261102-US500-2-entry", OrderState.CONFIRMED),
        ("gws-20261102-US5000-3-entry", OrderState.CONFIRMED),
        ("gws-20261102-DE40-4-entry", OrderState.CONFIRMED),
        ("gws-20261102-DE400-5-entry", OrderState.CONFIRMED),
        ("gws-20261102-de40-6-entry", OrderState.CONFIRMED),  # wielkość liter ma znaczenie, jak w identyfikatorze
    ])  # fmt: skip
    for epic in ("US5", "US500", "US5000", "DE40", "DE400", "de40"):
        assert journal.count_entry_orders(DAY, epic) == 1, epic
    assert journal.count_entry_orders(DAY, "DE4") == 0 and journal.count_entry_orders(DAY, "S500") == 0
    assert journal.count_entry_orders(DAY) == 6


@pytest.mark.parametrize("epic", ["", "DE40-1", "DE40-", "-DE40", "DE 40", "DE40%", "DE_40", "DE40\n", "é", "DE40."])
def test_count_entry_orders_rejects_an_epic_that_is_not_a_single_identifier_token(journal, epic):
    # "DE40-1" złożone z prefiksu dnia dałoby `gws-20261102-DE40-1-`, czyli policzyłoby wejście nr 1 instrumentu DE40
    seed_orders(journal, [("gws-20261102-DE40-1-entry", OrderState.CONFIRMED)])
    with pytest.raises(ValueError):
        journal.count_entry_orders(DAY, epic)


class EpicStr(str, Enum):
    DE40 = "DE40"


@pytest.mark.parametrize("epic", [40, b"DE40", EpicStr.DE40, ["DE40"]])
def test_count_entry_orders_rejects_an_epic_that_is_not_exactly_a_str(journal, epic):
    with pytest.raises(TypeError):
        journal.count_entry_orders(DAY, epic)  # podklasa str renderuje się w identyfikatorze inaczej niż jej wartość


def test_count_entry_orders_with_an_epic_still_rejects_a_datetime_day(journal):
    with pytest.raises(TypeError):
        journal.count_entry_orders(T0, "DE40")


def test_count_entry_orders_with_an_epic_gives_the_prefix_counts_on_a_mixed_journal(journal):
    ids = [*MIXED_ORDER_IDS, "gws-20261102-US500-6-entry", "gws-20261102-US500-7-entry", "gws-20261103-US500-3-entry",
           "gws-20261102-US500-8-smoke", "gws-20261102-US5-9-entry", "gws-20261102-DE40-10-entry"]  # fmt: skip
    for i, order_id in enumerate(ids):
        journal.insert_order(make_order(order_id, state=list(OrderState)[i % len(OrderState)]))
    for day in (date(2026, 11, 1), DAY, NEXT_DAY, date(2026, 11, 4)):
        for epic in ("DE40", "US500", "US5", "entry", "xgws"):
            prefix = f"gws-{day:%Y%m%d}-{epic}-"
            expected = len([o for o in ids if o.startswith(prefix) and o.endswith("-entry")])
            assert journal.count_entry_orders(day, epic) == expected, (day, epic)
    assert journal.count_entry_orders(DAY, "DE40") == 2 and journal.count_entry_orders(DAY, "US500") == 2


def test_count_entry_orders_with_an_epic_searches_a_narrow_primary_key_range(journal):
    for order_id in [*MIXED_ORDER_IDS, "gws-20261102-US500-6-entry", "gws-20261102-US500-7-entry"]:
        journal.insert_order(make_order(order_id))
    recording = RecordingConn(journal._conn)
    journal._conn = recording
    journal.count_entry_orders(DAY, "US500")
    journal._conn = recording.real
    ((sql, params),) = recording.statements
    # zakres klucza głównego zawężony do prefiksu instrumentu (nie do całego dnia z filtrem po kolumnie epic)
    assert params[:2] == ("gws-20261102-US500-", "gws-20261102-US500.")
    plan = " | ".join(row[-1] for row in journal._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
    assert plan.startswith("SEARCH orders USING") and "order_id>? AND order_id<?" in plan and "SCAN" not in plan, plan
    assert "epic" not in sql  # kolumna `epic` nie ma indeksu: filtr idzie po kluczu głównym


def test_count_smoke_trades_with_an_epic_counts_only_that_instruments_smoke_trades(journal):
    # DE40: DEAL-1 wejście strategii, DEAL-2 smoke po zleceniu, DEAL-3 smoke po wskaźniku wykonawcy
    journal.record_trade(make_trade(), "DEAL-1", DAY)
    journal.insert_order(make_order("gws-20261102-DE40-1-entry", state=OrderState.CONFIRMED, deal_id="DEAL-1"))
    journal.record_trade(make_trade(), "DEAL-2", DAY)
    journal.insert_order(make_order("gws-20261102-DE40-2-smoke", state=OrderState.CONFIRMED, deal_id="DEAL-2"))
    journal.record_trade(make_trade(), "DEAL-3", NEXT_DAY)
    journal.set_meta("entry_by_deal:DEAL-3", "gws-20261103-DE40-1-smoke")
    # US500: DEAL-4 wejście strategii, DEAL-5 smoke po zleceniu, DEAL-6 i DEAL-7 smoke po wskaźniku
    journal.record_trade(make_trade(instrument="US500"), "DEAL-4", DAY)
    journal.insert_order(
        make_order("gws-20261102-US500-3-entry", epic="US500", state=OrderState.CONFIRMED, deal_id="DEAL-4")
    )
    journal.record_trade(make_trade(instrument="US500"), "DEAL-5", DAY)
    journal.insert_order(
        make_order("gws-20261102-US500-4-smoke", epic="US500", state=OrderState.CONFIRMED, deal_id="DEAL-5")
    )
    journal.record_trade(make_trade(instrument="US500"), "DEAL-6", NEXT_DAY)
    journal.set_meta("entry_by_deal:DEAL-6", "gws-20261103-US500-1-smoke")
    journal.record_trade(make_trade(instrument="US500"), "DEAL-7", NEXT_DAY)
    journal.set_meta("entry_by_deal:DEAL-7", "gws-20261103-US500-2-smoke")
    assert journal.count_smoke_trades() == 5 and journal.count_smoke_trades(None) == 5
    assert journal.count_smoke_trades("DE40") == 2
    assert journal.count_smoke_trades("US500") == 3
    assert journal.count_smoke_trades("EURUSD") == 0
    # licznik sesji pasma = jego transakcje bez prób smoke; sesje pasm sumują się do sesji całej księgi
    sessions = {e: len(journal.trades(e)) - journal.count_smoke_trades(e) for e in ("DE40", "US500")}
    assert sessions == {"DE40": 1, "US500": 1}
    assert sum(sessions.values()) == len(journal.trades()) - journal.count_smoke_trades()


def test_trades_with_an_epic_returns_only_that_instrument_in_booking_order(journal):
    a = make_trade(net_pnl=Decimal("-1"))
    b = make_trade(instrument="US500", net_pnl=Decimal("2"))
    c = make_trade(net_pnl=Decimal("3"), exit_reason="MANUAL")
    d = make_trade(instrument="US500", net_pnl=Decimal("-4"), exit_reason="TARGET")
    for i, trade in enumerate((a, b, c, d), start=1):
        journal.record_trade(trade, f"DEAL-{i}", DAY)
    assert journal.trades("DE40") == [a, c]
    assert journal.trades("US500") == [b, d]
    assert journal.trades("EURUSD") == []
    assert journal.trades() == [a, b, c, d] and journal.trades(None) == [a, b, c, d]  # bez instrumentu: jak dotąd


@pytest.mark.parametrize("method", ["trades", "count_smoke_trades"])
def test_trades_and_smoke_count_reject_an_empty_or_non_str_epic_instead_of_returning_everything(journal, method):
    journal.record_trade(make_trade(), "DEAL-1", DAY)
    with pytest.raises(ValueError):
        getattr(journal, method)("")  # pusty napis to nie „wszystkie instrumenty”
    for bad in (40, b"DE40", ["DE40"]):
        with pytest.raises(TypeError):
            getattr(journal, method)(bad)


@pytest.mark.parametrize("call", [
    lambda j: j.trades("DE40"), lambda j: j.count_smoke_trades("DE40"), lambda j: j.count_entry_orders(DAY, "DE40"),
])  # fmt: skip
def test_per_instrument_reads_report_a_closed_journal_as_a_read_error(journal, call):
    journal.close()
    with pytest.raises(JournalReadError):
        call(journal)
