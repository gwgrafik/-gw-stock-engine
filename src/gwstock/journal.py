"""Dziennik SQLite: lokalne źródło prawdy silnika (surowe zdarzenia, zlecenia, pozycje, ryzyko, kill switch, księga).

Zasady (spec §3, §6, §9, §10):
- jedno połączenie (`check_same_thread=False`) chronione `threading.Lock`; `journal_mode=WAL`, `synchronous=FULL`,
  więc po powrocie z zapisu wiersz jest na dysku (INTENT jest zapisany, zanim cokolwiek wyślemy do brokera);
- każdy zapis przechodzi przez jedną funkcję `_write` (BEGIN IMMEDIATE ... COMMIT, przy błędzie ROLLBACK):
  nieudany zapis wielu instrukcji nie zostawia po sobie nic;
- żaden wyjątek `sqlite3` nie wycieka: błąd zapisu to `JournalWriteError`, błąd odczytu `JournalReadError`,
  uszkodzone dane to `JournalCorruptError`, nieobsługiwany schemat to `JournalSchemaError`;
- `Decimal` zapisany jako TEXT (dokładnie, bez float), czas jako ISO-8601 UTC, enumy po wartości;
- dziennik nie ma zegara: każdy znacznik czasu podaje wywołujący (zegar silnika);
- dziennik nie zna reguł automatu stanów ani strategii: legalność przejść sprawdza `OrderManager`.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from enum import Enum
from typing import Any, TypeVar

from gwstock.types import (
    KillLevel,
    OrderState,
    PositionState,
    Side,
    _require_decimal,
    _require_side,
    _require_utc,
)

SCHEMA_VERSION = 1

T = TypeVar("T")


class JournalError(Exception):
    """Baza wyjątków dziennika; żaden `sqlite3.Error` nie wychodzi poza dziennik."""


class JournalWriteError(JournalError):
    """Zapis nie powiódł się (zablokowana, pełna lub tylko do odczytu baza, naruszone ograniczenie)."""


class JournalReadError(JournalError):
    """Odczyt nie powiódł się."""


class JournalCorruptError(JournalError):
    """Zapisane dane są uszkodzone lub niezgodne z typami (np. zły napis Decimal, nieznany stan)."""


class JournalSchemaError(JournalError):
    """Baza ma nowszą wersję schematu albo nie jest dziennikiem gwstock."""


# --- walidacja ---------------------------------------------------------------------------------


def _check_str(value: object, where: str, *, non_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{where}: expected str, got {type(value).__name__} {value!r}")
    if non_empty and not value:
        raise ValueError(f"{where}: must not be empty")


def _check_utc(value: object, where: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{where}: expected a timezone-aware UTC datetime, got {value!r}")


def _check_decimal(value: object, where: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{where}: expected Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{where}: expected a finite Decimal, got {value!r}")


def _check_day(day: object) -> str:
    # `datetime` jest podklasą `date`, ale klucz dnia z godziną po cichu rozdzieliłby licznik na wiele wierszy
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError(f"expected a date without time, got {type(day).__name__} {day!r}")
    return day.isoformat()


def _require_str(owner: object, name: str, *, optional: bool = False, non_empty: bool = False) -> None:
    value = getattr(owner, name)
    if value is None and optional:
        return
    _check_str(value, f"{type(owner).__name__}.{name}", non_empty=non_empty)


def _require_enum(owner: object, name: str, enum_type: type[Enum], *, optional: bool = False) -> None:
    value = getattr(owner, name)
    if value is None and optional:
        return
    if not isinstance(value, enum_type):
        raise TypeError(f"{type(owner).__name__}.{name}: expected {enum_type.__name__}, got {value!r}")


def _require_positive(owner: object, name: str, *, optional: bool = False) -> None:
    value = getattr(owner, name)
    if value is None and optional:
        return
    if value <= 0:
        raise ValueError(f"{type(owner).__name__}.{name}: must be positive, got {value}")


def _require_utc_optional(owner: object, name: str) -> None:
    if getattr(owner, name) is not None:
        _require_utc(owner, name)


# --- rekordy -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRecord:
    """Zlecenie własne. `order_id` to nasz identyfikator `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}`."""

    order_id: str
    epic: str
    side: Side
    size: Decimal
    stop_distance: Decimal
    state: OrderState
    deal_reference: str | None
    deal_id: str | None
    created_at: datetime
    updated_at: datetime
    note: str = ""

    def __post_init__(self) -> None:
        _require_str(self, "order_id", non_empty=True)
        _require_str(self, "epic", non_empty=True)
        _require_side(self)
        _require_decimal(self, "size")
        _require_positive(self, "size")
        _require_decimal(self, "stop_distance")  # zlecenie wyjścia nie ma stopu, więc 0 jest dozwolone
        if self.stop_distance < 0:
            raise ValueError(f"OrderRecord.stop_distance: must not be negative, got {self.stop_distance}")
        _require_enum(self, "state", OrderState)
        _require_str(self, "deal_reference", optional=True)
        _require_str(self, "deal_id", optional=True)
        _require_utc(self, "created_at")
        _require_utc(self, "updated_at")
        _require_str(self, "note")


@dataclass(frozen=True)
class PositionRecord:
    """Lokalny stan pozycji na instrumencie (jedna na `epic`); pola poza `epic` i `state` są puste dla FLAT."""

    epic: str
    state: PositionState
    deal_id: str | None = None
    side: Side | None = None
    size: Decimal | None = None
    open_level: Decimal | None = None
    stop_level: Decimal | None = None
    opened_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_str(self, "epic", non_empty=True)
        _require_enum(self, "state", PositionState)
        _require_str(self, "deal_id", optional=True)
        _require_enum(self, "side", Side, optional=True)
        _require_decimal(self, "size", optional=True)
        _require_positive(self, "size", optional=True)
        _require_decimal(self, "open_level", optional=True)
        _require_decimal(self, "stop_level", optional=True)
        _require_utc_optional(self, "opened_at")


@dataclass(frozen=True)
class KillState:
    """Stan kill switcha. Domyślnie (nic nigdy nie zapisano): `KillState(KillLevel.NONE, "", None)`.

    `since` to czas ostatniego `set_kill` (także zdjęcia poziomu), nie czas pierwszego wyzwolenia.
    """

    level: KillLevel
    reason: str
    since: datetime | None

    def __post_init__(self) -> None:
        _require_enum(self, "level", KillLevel)
        _require_str(self, "reason")
        _require_utc_optional(self, "since")


_TRADE_DECIMALS = (
    "size", "decision_bid", "decision_ask", "entry_fill", "exit_fill", "gross_pnl",
    "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl",
)
_TRADE_TIMES = ("signal_ts", "submit_ts", "confirm_ts", "exit_ts")


@dataclass(frozen=True)
class TradeRecord:
    """Wiersz księgi (spec §10). Kolejność pól to kolejność kolumn eksportu CSV; `deal_id` trzyma dziennik osobno."""

    instrument: str
    side: Side
    size: Decimal
    decision_bid: Decimal
    decision_ask: Decimal
    entry_fill: Decimal
    exit_fill: Decimal
    gross_pnl: Decimal
    spread_cost: Decimal
    slippage_penalty: Decimal
    financing: Decimal
    fees: Decimal
    net_pnl: Decimal
    signal_ts: datetime
    submit_ts: datetime
    confirm_ts: datetime
    exit_ts: datetime
    latency_ms: int
    exit_reason: str

    def __post_init__(self) -> None:
        _require_str(self, "instrument", non_empty=True)
        _require_side(self)
        for name in _TRADE_DECIMALS:
            _require_decimal(self, name)
        _require_positive(self, "size")
        for name in _TRADE_TIMES:
            _require_utc(self, name)
        if not isinstance(self.latency_ms, int) or isinstance(self.latency_ms, bool):
            raise TypeError(f"TradeRecord.latency_ms: expected int, got {type(self.latency_ms).__name__}")
        if self.latency_ms < 0:
            raise ValueError(f"TradeRecord.latency_ms: must not be negative, got {self.latency_ms}")
        _require_str(self, "exit_reason", non_empty=True)


@dataclass(frozen=True)
class RawEvent:
    """Surowe zdarzenie (odpowiedź brokera lub zdarzenie strukturalne jako JSON) w kolejności `id`."""

    id: int
    source: str
    payload: str
    ts: datetime


# --- kodowanie i dekodowanie wartości --------------------------------------------------------

# Suma musi być dokładna: precyzja 60 cyfr i pułapka na zaokrąglenie zamiast cichej utraty cyfr.
_EXACT = Context(prec=60, traps=[InvalidOperation, Overflow, Inexact])


def _exact_add(a: Decimal, b: Decimal) -> Decimal:
    try:
        return _EXACT.add(a, b)
    except ArithmeticError as exc:
        raise JournalError(f"sum of {a} and {b} is not exactly representable") from exc


def _dec(text: str) -> Decimal:
    """Wczytuje zapisany Decimal; zły napis lub wartość nieskończona to uszkodzenie danych."""
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise JournalCorruptError(f"stored value {text!r} is not a Decimal") from exc
    if not value.is_finite():
        raise JournalCorruptError(f"stored value {text!r} is not finite")
    return value


def _dec_opt(text: str | None) -> Decimal | None:
    return None if text is None else _dec(text)


def _ts(text: str) -> datetime:
    value = datetime.fromisoformat(text)  # ValueError przechodzi do `_decode`
    _check_utc(value, "stored timestamp")
    return value


def _ts_opt(text: str | None) -> datetime | None:
    return None if text is None else _ts(text)


def _decode(kind: str, build: Callable[[tuple], T], row: tuple) -> T:
    try:
        return build(row)
    except JournalCorruptError:
        raise
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise JournalCorruptError(f"corrupt {kind} row: {exc}") from exc


_ORDER_COLS = "order_id, epic, side, size, stop_distance, state, deal_reference, deal_id, created_at, updated_at, note"
_ORDER_MUTABLE = frozenset({"state", "deal_reference", "deal_id", "note", "updated_at"})
_POSITION_COLS = "epic, state, deal_id, side, size, open_level, stop_level, opened_at"
_TRADE_COLS = (
    "instrument, side, size, decision_bid, decision_ask, entry_fill, exit_fill, gross_pnl, spread_cost, "
    "slippage_penalty, financing, fees, net_pnl, signal_ts, submit_ts, confirm_ts, exit_ts, latency_ms, exit_reason"
)
_TRADE_INSERT = (
    f"INSERT INTO trades (deal_id, exit_day, {_TRADE_COLS}) VALUES ({', '.join('?' * 21)}) "
    "ON CONFLICT(deal_id) DO NOTHING"
)  # konflikt tylko na deal_id jest pomijany; naruszenie NOT NULL itp. nadal jest błędem
_DAY_PREFIX = "gws-{:%Y%m%d}-"  # prefiks identyfikatora z `make_client_id` (dzień handlowy, bez znajomości stref)
_ENTRY_SUFFIX = "-entry"  # rola wejścia strategii (ostatni segment identyfikatora)


def _order_from_row(r: tuple) -> OrderRecord:
    return OrderRecord(
        order_id=r[0], epic=r[1], side=Side(r[2]), size=_dec(r[3]), stop_distance=_dec(r[4]),
        state=OrderState(r[5]), deal_reference=r[6], deal_id=r[7], created_at=_ts(r[8]), updated_at=_ts(r[9]),
        note=r[10],
    )


def _position_from_row(r: tuple) -> PositionRecord:
    return PositionRecord(
        epic=r[0], state=PositionState(r[1]), deal_id=r[2], side=None if r[3] is None else Side(r[3]),
        size=_dec_opt(r[4]), open_level=_dec_opt(r[5]), stop_level=_dec_opt(r[6]), opened_at=_ts_opt(r[7]),
    )


def _trade_from_row(r: tuple) -> TradeRecord:
    return TradeRecord(
        instrument=r[0], side=Side(r[1]), size=_dec(r[2]), decision_bid=_dec(r[3]), decision_ask=_dec(r[4]),
        entry_fill=_dec(r[5]), exit_fill=_dec(r[6]), gross_pnl=_dec(r[7]), spread_cost=_dec(r[8]),
        slippage_penalty=_dec(r[9]), financing=_dec(r[10]), fees=_dec(r[11]), net_pnl=_dec(r[12]),
        signal_ts=_ts(r[13]), submit_ts=_ts(r[14]), confirm_ts=_ts(r[15]), exit_ts=_ts(r[16]),
        latency_ms=r[17], exit_reason=r[18],
    )


def _event_from_row(r: tuple) -> RawEvent:
    return RawEvent(id=r[0], source=r[1], payload=r[2], ts=_ts(r[3]))


def _trade_params(t: TradeRecord) -> tuple:
    return (
        t.instrument, t.side.value, str(t.size), str(t.decision_bid), str(t.decision_ask), str(t.entry_fill),
        str(t.exit_fill), str(t.gross_pnl), str(t.spread_cost), str(t.slippage_penalty), str(t.financing),
        str(t.fees), str(t.net_pnl), t.signal_ts.isoformat(), t.submit_ts.isoformat(), t.confirm_ts.isoformat(),
        t.exit_ts.isoformat(), t.latency_ms, t.exit_reason,
    )


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# --- schemat ---------------------------------------------------------------------------------

_SCHEMA = (
    """CREATE TABLE raw_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        payload TEXT NOT NULL,
        ts TEXT NOT NULL)""",
    "CREATE INDEX raw_events_source ON raw_events (source, id)",
    """CREATE TABLE orders (
        order_id TEXT PRIMARY KEY,
        epic TEXT NOT NULL,
        side TEXT NOT NULL,
        size TEXT NOT NULL,
        stop_distance TEXT NOT NULL,
        state TEXT NOT NULL,
        deal_reference TEXT,
        deal_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        note TEXT NOT NULL)""",
    """CREATE TABLE positions (
        epic TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        deal_id TEXT,
        side TEXT,
        size TEXT,
        open_level TEXT,
        stop_level TEXT,
        opened_at TEXT)""",
    "CREATE TABLE realized_pnl (day TEXT PRIMARY KEY, net TEXT NOT NULL)",
    """CREATE TABLE kill_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        level INTEGER NOT NULL,
        reason TEXT NOT NULL,
        since TEXT NOT NULL)""",
    """CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT NOT NULL UNIQUE,
        exit_day TEXT NOT NULL,
        instrument TEXT NOT NULL,
        side TEXT NOT NULL,
        size TEXT NOT NULL,
        decision_bid TEXT NOT NULL,
        decision_ask TEXT NOT NULL,
        entry_fill TEXT NOT NULL,
        exit_fill TEXT NOT NULL,
        gross_pnl TEXT NOT NULL,
        spread_cost TEXT NOT NULL,
        slippage_penalty TEXT NOT NULL,
        financing TEXT NOT NULL,
        fees TEXT NOT NULL,
        net_pnl TEXT NOT NULL,
        signal_ts TEXT NOT NULL,
        submit_ts TEXT NOT NULL,
        confirm_ts TEXT NOT NULL,
        exit_ts TEXT NOT NULL,
        latency_ms INTEGER NOT NULL,
        exit_reason TEXT NOT NULL)""",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


# --- dziennik --------------------------------------------------------------------------------


class Journal:
    """Dziennik SQLite. Metody są synchroniczne (blokujące wywołania sqlite są tu zamierzone).

    Tworzyć wyłącznie przez `Journal.open(path)`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str) -> Journal:
        """Otwiera (lub tworzy) dziennik: WAL, synchronous=FULL, schemat v1.

        Odmawia (bez zmiany pliku) bazy z nowszą wersją schematu lub nie będącej dziennikiem gwstock oraz sytuacji,
        w której nie da się włączyć WAL (np. `:memory:`): dziennik bez trwałości nie ma sensu.
        """
        try:
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # transakcje: ręcznie
        except sqlite3.Error as exc:
            raise JournalError(f"cannot open journal {path!r}: {exc}") from exc
        try:
            cls._setup(conn, path)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @staticmethod
    def _setup(conn: sqlite3.Connection, path: str) -> None:
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise JournalSchemaError(f"{path!r} has schema version {version}, this build supports {SCHEMA_VERSION}")
            if version == 0 and conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0:
                raise JournalSchemaError(f"{path!r} is a non-empty database that is not a gwstock journal")
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise JournalError(f"{path!r}: WAL mode unavailable (got {mode!r}); the journal needs durable storage")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise JournalError(f"{path!r}: could not set synchronous=FULL")
            if version == 0:  # nowa baza: schemat i wersja w jednej transakcji
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    Journal._rollback(conn)
                    raise
        except sqlite3.Error as exc:
            raise JournalError(f"cannot initialise journal {path!r}: {exc}") from exc

    def close(self) -> None:
        """Zamyka połączenie (idempotentne); WAL zostaje zapisany do pliku głównego."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                raise JournalError(f"cannot close journal: {exc}") from exc

    # --- jedyne ścieżki dostępu do bazy ---

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # oryginalny błąd jest ważniejszy; niedomknięta transakcja zablokuje kolejny zapis głośno

    def _write(self, op: Callable[[sqlite3.Connection], T]) -> T:
        """Jedyna ścieżka zapisu: `op` biegnie w jednej transakcji; błąd wycofuje całość."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    result = op(self._conn)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._rollback(self._conn)
                    raise
                return result
            except sqlite3.Error as exc:
                raise JournalWriteError(f"journal write failed: {exc}") from exc

    def _read(self, op: Callable[[sqlite3.Connection], T]) -> T:
        """Jedyna ścieżka odczytu."""
        with self._lock:
            try:
                return op(self._conn)
            except sqlite3.Error as exc:
                raise JournalReadError(f"journal read failed: {exc}") from exc

    def pragma(self, name: str) -> str | int | None:
        """Wartość pragmy tylko do odczytu (np. `journal_mode`, `synchronous`, `user_version`)."""
        if not re.fullmatch(r"[a-z_]+", name):
            raise ValueError(f"pragma: expected a plain name, got {name!r}")

        def op(conn: sqlite3.Connection) -> str | int | None:
            row = conn.execute(f"PRAGMA {name}").fetchone()
            return None if row is None else row[0]

        return self._read(op)

    # --- surowe zdarzenia ---

    def append_raw(self, source: str, payload: str, ts: datetime) -> int:
        """Dopisuje surowe zdarzenie (odpowiedź brokera przed przetworzeniem, zdarzenie strukturalne jako JSON).

        Zwraca rosnące `id`. `payload` może być pusty (pusta odpowiedź też jest faktem).
        """
        _check_str(source, "append_raw.source", non_empty=True)
        _check_str(payload, "append_raw.payload")
        _check_utc(ts, "append_raw.ts")

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO raw_events (source, payload, ts) VALUES (?, ?, ?)", (source, payload, ts.isoformat())
            )
            if cur.lastrowid is None:
                raise JournalWriteError("journal write failed: no row id returned")
            return cur.lastrowid

        return self._write(op)

    def events(self, source: str | None = None) -> list[RawEvent]:
        """Surowe zdarzenia w kolejności `id` (opcjonalnie tylko z danego źródła)."""

        def op(conn: sqlite3.Connection) -> list[tuple]:
            if source is None:
                return conn.execute("SELECT id, source, payload, ts FROM raw_events ORDER BY id").fetchall()
            return conn.execute(
                "SELECT id, source, payload, ts FROM raw_events WHERE source = ? ORDER BY id", (source,)
            ).fetchall()

        return [_decode("raw event", _event_from_row, r) for r in self._read(op)]

    # --- zlecenia ---

    def insert_order(self, o: OrderRecord) -> None:
        """Wstawia nowe zlecenie (np. INTENT). Duplikat `order_id` to `JournalWriteError`."""

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO orders ({_ORDER_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    o.order_id, o.epic, o.side.value, str(o.size), str(o.stop_distance), o.state.value,
                    o.deal_reference, o.deal_id, o.created_at.isoformat(), o.updated_at.isoformat(), o.note,
                ),
            )

        self._write(op)

    def update_order(self, order_id: str, **fields: Any) -> OrderRecord:
        """Zmienia pola zlecenia i zwraca zaktualizowany rekord.

        Dozwolone: `state`, `deal_reference`, `deal_id`, `note`, `updated_at`; `updated_at` jest WYMAGANE (dziennik nie
        ma zegara). ValueError: brak `updated_at` lub inne pole; KeyError: nieznane zlecenie. Legalność przejścia
        stanów sprawdza wywołujący (`OrderManager`).
        """
        unknown = set(fields) - _ORDER_MUTABLE
        if unknown:
            raise ValueError(f"cannot update order fields {sorted(unknown)}; allowed: {sorted(_ORDER_MUTABLE)}")
        if "updated_at" not in fields:
            raise ValueError("updated_at required")

        def op(conn: sqlite3.Connection) -> OrderRecord:
            row = conn.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(order_id)
            new = replace(_decode("order", _order_from_row, row), **fields)  # walidacja w __post_init__
            conn.execute(
                "UPDATE orders SET state = ?, deal_reference = ?, deal_id = ?, note = ?, updated_at = ? "
                "WHERE order_id = ?",
                (new.state.value, new.deal_reference, new.deal_id, new.note, new.updated_at.isoformat(), order_id),
            )
            return new

        return self._write(op)

    def get_order(self, order_id: str) -> OrderRecord | None:
        row = self._read(
            lambda c: c.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        )
        return None if row is None else _decode("order", _order_from_row, row)

    def unresolved_orders(self) -> list[OrderRecord]:
        """Zlecenia wymagające uzgodnienia po restarcie: INTENT, SUBMITTING i UNKNOWN (w kolejności wstawienia).

        INTENT też: wiersz INTENT, który nie przeszedł dalej, oznacza przerwanie między zapisem a wysyłką,
        więc nie wiadomo, czy zlecenie doszło do brokera.
        """
        states = (OrderState.INTENT.value, OrderState.SUBMITTING.value, OrderState.UNKNOWN.value)
        rows = self._read(
            lambda c: c.execute(
                f"SELECT {_ORDER_COLS} FROM orders WHERE state IN (?, ?, ?) ORDER BY rowid", states
            ).fetchall()
        )
        return [_decode("order", _order_from_row, r) for r in rows]

    def count_orders(self, day: date) -> int:
        """Liczba zleceń dnia handlowego (w każdym stanie i roli), po prefiksie `gws-{YYYYMMDD}-` w `order_id`."""
        _check_day(day)
        prefix = _DAY_PREFIX.format(day)
        return self._read(
            lambda c: c.execute(
                "SELECT count(*) FROM orders WHERE substr(order_id, 1, ?) = ?", (len(prefix), prefix)
            ).fetchone()[0]
        )

    def count_entry_orders(self, day: date) -> int:
        """Liczba zleceń WEJŚCIA dnia (rola `entry`, każdy stan): podstawa ENTRIES_PER_DAY bramki ryzyka.

        Zlecenia innych ról (np. `smoke`, próba akceptacyjna L0) zużywają numer kolejny dnia (`count_orders`), ale nie
        dzienne wejście strategii.
        """
        _check_day(day)
        prefix = _DAY_PREFIX.format(day)
        suffix = _ENTRY_SUFFIX
        return self._read(
            lambda c: c.execute(
                "SELECT count(*) FROM orders WHERE substr(order_id, 1, ?) = ? AND substr(order_id, -?) = ?",
                (len(prefix), prefix, len(suffix), suffix),
            ).fetchone()[0]
        )

    def next_seq(self, day: date) -> int:
        """Następny numer kolejny zlecenia dnia (`count_orders(day) + 1`); nie rezerwuje go (jeden wątek decyzyjny)."""
        return self.count_orders(day) + 1

    # --- pozycje ---

    def upsert_position(self, p: PositionRecord) -> None:
        """Zapisuje stan pozycji instrumentu (wstawia lub zastępuje)."""

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO positions ({_POSITION_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(epic) DO UPDATE SET state = excluded.state, deal_id = excluded.deal_id, "
                "side = excluded.side, size = excluded.size, open_level = excluded.open_level, "
                "stop_level = excluded.stop_level, opened_at = excluded.opened_at",
                (
                    p.epic, p.state.value, p.deal_id, None if p.side is None else p.side.value, _opt_str(p.size),
                    _opt_str(p.open_level), _opt_str(p.stop_level),
                    None if p.opened_at is None else p.opened_at.isoformat(),
                ),
            )

        self._write(op)

    def current_position(self, epic: str) -> PositionRecord | None:
        row = self._read(
            lambda c: c.execute(f"SELECT {_POSITION_COLS} FROM positions WHERE epic = ?", (epic,)).fetchone()
        )
        return None if row is None else _decode("position", _position_from_row, row)

    # --- licznik zrealizowanego wyniku (przeżywa restart) ---

    @staticmethod
    def _add_pnl(conn: sqlite3.Connection, day: date, net: Decimal) -> None:
        """Dodaje `net` do wyniku dnia; wywoływane wyłącznie wewnątrz transakcji `_write`."""
        key = _check_day(day)
        row = conn.execute("SELECT net FROM realized_pnl WHERE day = ?", (key,)).fetchone()
        total = _exact_add(_dec(row[0]) if row else Decimal(0), net)
        conn.execute(
            "INSERT INTO realized_pnl (day, net) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET net = excluded.net",
            (key, str(total)),
        )

    def add_realized_pnl(self, exit_day: date, net: Decimal) -> None:
        """Dodaje wynik netto do licznika dnia zamknięcia (klucz: dzień wyjścia, nie wejścia)."""
        _check_day(exit_day)
        _check_decimal(net, "add_realized_pnl.net")
        self._write(lambda c: self._add_pnl(c, exit_day, net))

    def realized_pnl(self, day: date) -> Decimal:
        """Suma wyniku netto zamknięć danego dnia (0, gdy brak)."""
        key = _check_day(day)
        row = self._read(lambda c: c.execute("SELECT net FROM realized_pnl WHERE day = ?", (key,)).fetchone())
        return Decimal(0) if row is None else _dec(row[0])

    def realized_pnl_total(self) -> Decimal:
        """Suma wyniku netto od początku (0, gdy brak)."""
        rows = self._read(lambda c: c.execute("SELECT net FROM realized_pnl ORDER BY day").fetchall())
        total = Decimal(0)
        for (text,) in rows:
            total = _exact_add(total, _dec(text))
        return total

    # --- kill switch ---

    def set_kill(self, level: KillLevel, reason: str, ts: datetime) -> None:
        """Zapisuje poziom kill switcha, przyczynę i czas (również zdjęcie poziomu: `KillLevel.NONE`)."""
        _check_utc(ts, "set_kill.ts")
        state = KillState(level, reason, ts)  # walidacja typów

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO kill_state (id, level, reason, since) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET level = excluded.level, reason = excluded.reason, "
                "since = excluded.since",
                (int(state.level), state.reason, ts.isoformat()),
            )

        self._write(op)

    def kill_state(self) -> KillState:
        """Zapisany stan; gdy nic nie zapisano: `KillState(KillLevel.NONE, "", None)`."""
        row = self._read(lambda c: c.execute("SELECT level, reason, since FROM kill_state WHERE id = 1").fetchone())
        if row is None:
            return KillState(KillLevel.NONE, "", None)
        return _decode("kill state", lambda r: KillState(KillLevel(r[0]), r[1], _ts(r[2])), row)

    # --- księga ---

    def record_trade(self, t: TradeRecord, deal_id: str, exit_day: date) -> bool:
        """Księguje transakcję i dolicza `t.net_pnl` do wyniku `exit_day` w JEDNEJ transakcji (idempotentnie).

        Zwraca True, gdy zaksięgowano; False (bez żadnej zmiany), gdy `deal_id` był już zaksięgowany.
        """
        _check_str(deal_id, "record_trade.deal_id", non_empty=True)
        _check_day(exit_day)

        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(_TRADE_INSERT, (deal_id, exit_day.isoformat(), *_trade_params(t)))
            if cur.rowcount == 0:
                return False
            self._add_pnl(conn, exit_day, t.net_pnl)
            return True

        return self._write(op)

    def has_trade(self, deal_id: str) -> bool:
        """Czy zamknięcie `deal_id` jest już zaksięgowane (tani odczyt przed ponownym księgowaniem)."""
        _check_str(deal_id, "has_trade.deal_id", non_empty=True)
        row = self._read(lambda c: c.execute("SELECT 1 FROM trades WHERE deal_id = ?", (deal_id,)).fetchone())
        return row is not None

    def trades(self) -> list[TradeRecord]:
        """Księga w kolejności zaksięgowania."""
        rows = self._read(lambda c: c.execute(f"SELECT {_TRADE_COLS} FROM trades ORDER BY id").fetchall())
        return [_decode("trade", _trade_from_row, r) for r in rows]

    # --- drobne znaczniki ---

    def get_meta(self, key: str) -> str | None:
        """Mały trwały znacznik (np. `mismatch_since`, `last_closed_check`); None, gdy brak."""
        row = self._read(lambda c: c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone())
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        _check_str(key, "set_meta.key", non_empty=True)
        _check_str(value, "set_meta.value")
        self._write(
            lambda c: c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )
