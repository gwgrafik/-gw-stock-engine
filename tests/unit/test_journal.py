"""Testy dziennika SQLite: trwałość (WAL, FULL), atomowość zapisów, niezmienniki księgi i licznika strat."""

import sqlite3
import threading
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.journal import (
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
    """Proxy połączenia SQLite: instrukcja zawierająca `needle` kończy się błędem „database is locked”."""

    def __init__(self, real: sqlite3.Connection, needle: str) -> None:
        self._real = real
        self._needle = needle

    def execute(self, sql, params=()):
        if self._needle in sql:
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


def test_unresolved_orders_are_intent_submitting_and_unknown_only(journal):
    states = [
        OrderState.INTENT,
        OrderState.SUBMITTING,
        OrderState.UNKNOWN,
        OrderState.CONFIRMED,
        OrderState.REJECTED,
        OrderState.NOT_PLACED,
        OrderState.WORKING,
        OrderState.CANCELED,
    ]
    for i, st in enumerate(states, start=1):
        journal.insert_order(make_order(f"gws-20261102-DE40-{i}-entry", state=st))
    unresolved = journal.unresolved_orders()
    assert [o.state for o in unresolved] == [OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN]
    assert [o.order_id for o in unresolved] == [f"gws-20261102-DE40-{i}-entry" for i in (1, 2, 3)]


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
    if column != "state":  # wiersz z nieznanym stanem nie pasuje do filtru unresolved_orders
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
