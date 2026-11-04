"""Złote ślady zachowania pasma DE40 v1 (zadanie 4 ze specyfikacji `2026-11-04-multi-lane-design.md`, sekcja 8).

PO CO. Kolejne zadania przebudowują silnik pod wiele pasm (`app.py`, `execution.py`, `reconcile.py`, `journal.py`...),
a pasmo DE40 `orb_index_cfd` v1 ma działać BEZ ZMIANY zachowania (R2). Ten plik nagrywa zachowanie sprzed przebudowy
i porównuje z nim każdy następny stan kodu bajt w bajt: zmiana kolejności wywołań brokera, przyczyny zamknięcia,
zaokrąglenia, treści zdarzenia w dzienniku albo choćby jednego pola w tabeli pokazuje się jako różnica względem wzorca.

CO JEST ŚLADEM (jeden plik `tests/fault/golden/<scenariusz>.json` na scenariusz):
- `runs`: każde `run_day` (etykieta, dzień, czas zegara symulacji na początku i końcu, `DayReport` albo informacja,
  że proces "zginął");
- `broker_calls`: pełna sekwencja `FakeBroker.calls` z czasem zegara symulacji w chwili wywołania i argumentami
  (zapis przez nakładkę na instancji brokera, która tylko dopisuje wpis i woła oryginał; zgodność nazw z `calls` jest
  sprawdzana przy każdym przebiegu);
- `broker_final`: pozycje, które broker trzyma na końcu;
- `journal`: schemat oraz PEŁNA zawartość wszystkich tabel dziennika (`orders`, `positions`, `trades`, `realized_pnl`,
  `kill_state`, `meta`, `raw_events` ze źródłem, ładunkiem i czasem), czytana z dysku osobnym połączeniem tylko do
  odczytu, w kolejności zapisu;
- `alerts`: alarmy wysłane do właściciela (poziom, treść);
- `config_sha256`: skróty plików `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml` (wzorzec obowiązuje tylko dla
  tej konfiguracji: różnica w skrócie od razu wskazuje przyczynę).

DETERMINIZM. Czas pochodzi wyłącznie z `SimClock`, identyfikatory REF-n i DEAL-n z liczników `FakeBroker`, a dane
rynku z `engine_sim`. Śladu nie wolno zanieczyścić niczym zależnym od uruchomienia: ścieżkami bezwzględnymi (test
sprawdza, że w śladzie nie ma katalogu symulacji), czasem ściennym, identyfikatorami procesu, losowością, kolejnością
zbiorów ani wersjami bibliotek. Wzorce zapisano z kodu bez zmian względem commitu f241e18.

ZAPIS WZORCA. Wyłącznie świadomie: `GW_WRITE_GOLDEN=1 python -m pytest tests/fault/test_golden_de40.py`. Test nigdy
nie zapisuje wzorca sam: bez przełącznika brak pliku wzorca jest głośną porażką, a rozbieżność porażką z pierwszą
różniącą się linią. W trybie zapisu test zapisuje plik i kończy się jako POMINIĘTY (nie zielony), więc przebieg z
przełącznikiem nigdy nie udaje weryfikacji; po zapisie uruchom test jeszcze raz bez przełącznika i obejrzyj `git diff`.
Wzorzec wolno przepisać tylko wtedy, gdy zmiana zachowania DE40 jest zamierzona i uzgodniona z właścicielem.

UZBRAJANIE. Symulacja jest uzbrajana wyłącznie przez `engine_sim.arm` (fabryka silnika) w katalogu tymczasowym, w
roli właściciela; ten plik niczego nie uzbraja i nie dotyka `data/` repozytorium.

SCENARIUSZE (źródło w nawiasie to test, którego sytuację nagrywa; ślad biegnie do końca dnia, o ile nie zaznaczono):
- `full_day_long_flat_1725`: wejście BUY o 09:05, spłaszczenie FORCED o 17:25 i raport (test_engine_day);
- `full_day_short_flat_1725`: to samo dla SELL (test_engine_day: short_entry);
- `stop_hit_by_broker`: stop u brokera o 11:00 księgowany raz, reszta dnia bez pozycji (test_engine_day);
- `restart_mid_position`: proces ginie o 11:00 z otwartą pozycją, nowy silnik na tym samym brokerze i dzienniku
  doprowadza dzień do spłaszczenia (test_trade_lifecycle_e2e: test_h);
- `restart_mid_entry_adopt`: proces ginie po przyjęciu zlecenia, przed potwierdzeniem; po restarcie pozycja
  przyjęta bez drugiego wejścia (test_engine_day);
- `no_signal_doji`: dzień bez sygnału, doji (test_engine_day);
- `no_signal_first_candle_missing`: dzień bez sygnału, brak pierwszej świecy do terminu 09:10, przerwany o 09:20
  (test_engine_day);
- `gate_blocked_quote_silence`: cisza notowań od otwarcia, kill miękki i wejście zablokowane przez bramkę, przerwany
  o 09:20 (test_engine_day);
- `dry_run_full_day`: cały dzień w dry_run (decyzja bez zlecenia);
- `dry_run_restart_mid_day`: dry_run z restartem w środku sesji (test_engine_day);
- `hard_kill_emergency_flatten`: plik HARD_STOP o 11:00, awaryjne spłaszczenie co takt z timeoutami zamknięcia
  przez minutę (test_engine_day), przerwany 5 minut po odzyskaniu brokera;
- `market_closed_at_flatten`: zamknięty rynek w chwili spłaszczenia, ponowienie po minucie (test_engine_day).

CZEGO ŚLADY NIE POKRYWAJĄ: adaptera Capital.com (tylko `FakeBroker`), rzeczywistego dysku i fsync (dziennik na tmpfs),
`run_forever`, blokady instancji i tętna, wygasłej sesji i ponownego logowania, kill switcha wywołanego limitami
strat, dni zmiany czasu, skróconych sesji i dni bez sesji, zamkniętego rynku w nocy, awarii zapisu dziennika,
rozmiaru wejścia według reguł po `L1_PASSED`, wielu pozycji i wielu instrumentów, celu zysku.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from gwstock.app import Mode
from gwstock.broker.fake import BROKER_METHODS, FakeBroker
from gwstock.types import BrokerTimeout
from tests.fault.e2e_support import ProcessDied, die
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    FLATTEN_UTC,
    MID,
    OPEN_UTC,
    D,
    Market,
    SimClock,
    at_time,
    berlin,
    broker,
    cfgs,
    clock,
    config_dir,
    data_dir,
    day_rules,
    five_minute_candles,
    journal_path,
    make_engine,
    market,
    runtime,
    sim_root,
    stop_engine_at,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
WRITE_ENV = "GW_WRITE_GOLDEN"
TRACE_FORMAT = 1
CONFIG_FILES = ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml")
UTC = timezone.utc
STOP_LEVEL = MID + 1 - 30  # BUY po ask 25782.3, stop 30 pkt (jak w test_engine_day)
STOP_HIT_UTC = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # 11:00 w Berlinie
CRASH_UTC = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)  # 12:00 w Berlinie: po wejściu 09:05, przed wyjściem 17:25
HARD_AT = STOP_HIT_UTC  # plik HARD_STOP pojawia się o 11:00 w Berlinie


# --- zamiana wartości na drzewo typów JSON -------------------------------------------------------------------------


def plain(value: Any) -> Any:
    """Wartość jako drzewo typów JSON: `Decimal` i `date` jako napisy, `datetime` jako ISO-8601 w UTC, enum po
    wartości, dataclass jako obiekt, krotka jak lista. `float` i nieznany typ to błąd (projekt liczy na `Decimal`)."""
    if isinstance(value, Enum):  # przed str/int: enumy projektu dziedziczą po str albo int
        return plain(value.value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(f"naive datetime in a trace: {value!r}")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(plain(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    raise TypeError(f"cannot put {type(value).__name__} {value!r} in a golden trace")


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical(value: Any) -> str:
    """Tekst JSON w postaci, w jakiej silnik zapisuje ładunki zdarzeń (posortowane klucze, zwarte separatory)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


# --- nakładka na broker: czas i argumenty każdego wywołania -----------------------------------------------------


class CallLog:
    """Zapisuje (czas zegara, metoda, argumenty) każdego wywołania brokera i woła oryginał bez zmian.

    Nakładka siedzi na instancji `FakeBroker` (nie zmienia `src/`) i niczego nie zmienia w zachowaniu: wpis powstaje
    przed wywołaniem, wynik i wyjątki przechodzą dalej. Zegar symulacji nie biegnie wewnątrz metod brokera, więc czas
    wpisu jest czasem wywołania. Zgodność nazw ze `FakeBroker.calls` sprawdza `verify`.
    """

    def __init__(self, broker: FakeBroker, clock: SimClock) -> None:
        self._broker = broker
        self._clock = clock
        self.entries: list[tuple[datetime, str, tuple[Any, ...], dict[str, Any]]] = []
        for name in sorted(BROKER_METHODS):
            setattr(broker, name, self._wrap(name, getattr(broker, name)))

    def _wrap(self, name: str, real: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def recorded(*args: Any, **kwargs: Any) -> Any:
            self.entries.append((self._clock.now_utc(), name, args, kwargs))
            return await real(*args, **kwargs)

        return recorded

    def verify(self) -> None:
        names = [name for _, name, _, _ in self.entries]
        assert names == self._broker.calls, "the call log and FakeBroker.calls diverged"

    def lines(self) -> list[str]:
        out = []
        for ts, name, args, kwargs in self.entries:
            line = f"{ts.astimezone(UTC).isoformat()} {name}"
            if args:
                line += " " + compact(plain(list(args)))
            if kwargs:
                line += " " + compact(plain(kwargs))
            out.append(line)
        return out


# --- dziennik z dysku --------------------------------------------------------------------------------------------


def event_row(row: tuple[int, str, str, str]) -> dict[str, Any]:
    """Zdarzenie dziennika; ładunek jako obiekt, gdy jego tekst jest dokładnie postacią kanoniczną (wtedy zapis
    obiektu odtwarza tekst bajt w bajt), a w innym razie jako surowy tekst (`payload_text`)."""
    event_id, source, payload, ts = row
    try:
        parsed = json.loads(payload)
    except ValueError:
        return {"id": event_id, "source": source, "ts": ts, "payload_text": payload}
    if canonical(parsed) != payload:
        return {"id": event_id, "source": source, "ts": ts, "payload_text": payload}
    return {"id": event_id, "source": source, "ts": ts, "payload": parsed}


def dump_journal(path: Path) -> dict[str, Any]:
    """Schemat i pełna zawartość wszystkich tabel dziennika w kolejności zapisu (rowid), osobnym połączeniem tylko do
    odczytu; wartości takie, jakie leżą w SQLite (napisy i liczby całkowite)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        out: dict[str, Any] = {}
        out["schema"] = [
            [name, sql]
            for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")
        ]
        listing = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        tables = [name for (name,) in conn.execute(listing + " ORDER BY name")]
        required = {"orders", "positions", "trades", "realized_pnl", "kill_state", "meta", "raw_events"}
        assert required <= set(tables), f"missing journal tables: {sorted(required - set(tables))}"
        for table in tables:
            cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608  (nazwa z sqlite_master)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            if table == "raw_events":
                assert columns == ["id", "source", "payload", "ts"], columns
                out[table] = [event_row(row) for row in rows]
            else:
                out[table] = [dict(zip(columns, row, strict=True)) for row in rows]
        return out
    finally:
        conn.close()


# --- bezstratne kodowanie powtórzeń ---------------------------------------------------------------------------
#
# Dzień to ~12 600 wywołań brokera i ~6 300 zdarzeń dziennika, z czego prawie wszystko to okresowe odpytywanie
# (notowanie co 5 s, pozycje i zlecenia co 10 s), identyczne poza czasem i numerem zdarzenia. Zapis każdego z osobna
# dawałby ~1,8 MB na scenariusz. Kodowanie zastępuje powtórzony blok jednym wpisem
#   {"repeat": n, "every_s": "10", "ids": 2, "block": [pierwsza iteracja, w pełni]}
# (n iteracji; każda następna przesunięta o `every_s` sekund i `ids` numerów zdarzeń). Jest BEZSTRATNE i każdy przebieg
# to sprawdza (`decode(encode(x)) == x` co do znaku), więc usterka kodera kończy się głośną porażką, nigdy po cichu
# przepuszczoną różnicą: dwa różne ślady nie dają tego samego tekstu.

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_US = timedelta(microseconds=1)
MAX_BLOCK = 16  # najdłuższy blok powtórzenia (jeden takt dnia ma 4 wywołania)
MIN_REPEAT = 3  # krótsze powtórzenia zostają zapisane wprost


def iso_to_us(text: Any) -> int | None:
    """Mikrosekundy od epoki dla napisu będącego DOKŁADNIE kanonicznym ISO-8601 w UTC; inaczej None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0) or moment.isoformat() != text:
        return None
    return (moment - EPOCH) // ONE_US


def us_to_iso(us: int) -> str:
    return (EPOCH + timedelta(microseconds=us)).isoformat()


def seconds_text(us: int) -> str:
    whole, rest = divmod(us, 1_000_000)
    return str(whole) if rest == 0 else f"{whole}.{rest:06d}".rstrip("0")


def seconds_us(text: str) -> int:
    return int(Decimal(text) * 1_000_000)


class LineCodec:
    """Wywołania brokera: napis "<ISO> <metoda> [argumenty]"; powtarza się część po czasie, czas rośnie o okres."""

    has_numbers = False

    @staticmethod
    def parse(rep: str) -> tuple[str, int | None, int | None]:
        stamp, _, rest = rep.partition(" ")
        return rest, iso_to_us(stamp), None

    @staticmethod
    def shift(rep: str, dt_us: int, dn: int) -> str:
        stamp, _, rest = rep.partition(" ")
        return f"{us_to_iso(iso_to_us(stamp) + dt_us)} {rest}"


class EventCodec:
    """Zdarzenia dziennika: obiekt z `id` i `ts`; powtarza się reszta, `ts` rośnie o okres, `id` o liczbę zdarzeń."""

    has_numbers = True

    @staticmethod
    def parse(rep: dict[str, Any]) -> tuple[str, int | None, int | None]:
        number = rep["id"] if isinstance(rep["id"], int) and not isinstance(rep["id"], bool) else None
        rest = compact({k: v for k, v in rep.items() if k not in ("id", "ts")})
        return rest, iso_to_us(rep["ts"]), number

    @staticmethod
    def shift(rep: dict[str, Any], dt_us: int, dn: int) -> dict[str, Any]:
        return {
            k: (v + dn if k == "id" else us_to_iso(iso_to_us(v) + dt_us) if k == "ts" else v) for k, v in rep.items()
        }


def encode_runs(reps: list[Any], codec: Any) -> list[Any]:
    parsed = [codec.parse(rep) for rep in reps]
    out: list[Any] = []
    i, total = 0, len(reps)
    while i < total:
        best: tuple[int, int, int, int, int] | None = None  # (pokrycie, k, powtórzenia, dt, dn)
        for k in range(1, MAX_BLOCK + 1):
            if i + 2 * k > total:
                break
            found = _count_repeats(parsed, i, k, codec.has_numbers)
            if found is not None and found[0] >= MIN_REPEAT and (best is None or found[0] * k > best[0]):
                best = (found[0] * k, k, found[0], found[1], found[2])
        if best is None:
            out.append(reps[i])
            i += 1
            continue
        _, k, count, dt_us, dn = best
        run: dict[str, Any] = {"repeat": count, "every_s": seconds_text(dt_us)}
        if codec.has_numbers:
            run["ids"] = dn
        run["block"] = reps[i : i + k]
        out.append(run)
        i += count * k
    return out


def _count_repeats(
    parsed: list[tuple[str, int | None, int | None]], i: int, k: int, numbers: bool
) -> tuple[int, int, int] | None:
    """(powtórzenia, przesunięcie czasu w µs, przesunięcie numeru) bloku `parsed[i:i+k]`; None: nie powtarza się."""
    block = parsed[i : i + k]
    if any(t is None or (numbers and n is None) for _, t, n in block):
        return None
    first = parsed[i + k]
    if first[1] is None:
        return None
    dt_us = first[1] - block[0][1]
    dn = (first[2] - block[0][2]) if numbers and first[2] is not None else 0
    if dt_us < 0 or (numbers and dn <= 0):
        return None
    count = 1  # iteracje 0..count-1 już pasują; sprawdzana jest iteracja o numerze `count`
    while i + (count + 1) * k <= len(parsed):
        for j, (sig, t, n) in enumerate(block):
            other = parsed[i + count * k + j]
            if other[0] != sig or other[1] != t + count * dt_us or (numbers and other[2] != n + count * dn):
                break
        else:
            count += 1
            continue
        break
    return (count, dt_us, dn) if count > 1 else None


def decode_runs(encoded: list[Any], codec: Any) -> list[Any]:
    out: list[Any] = []
    for item in encoded:
        if isinstance(item, dict) and "repeat" in item and "block" in item:
            dt_us = seconds_us(item["every_s"])
            dn = item.get("ids", 0)
            for m in range(item["repeat"]):
                out.extend(codec.shift(rep, m * dt_us, m * dn) for rep in item["block"])
        else:
            out.append(item)
    return out


def compress(reps: list[Any], codec: Any) -> list[Any]:
    """`encode_runs` z dowodem bezstratności: dekodowanie musi oddać dokładnie te same wpisy (co do znaku)."""
    encoded = encode_runs(reps, codec)
    back = decode_runs(encoded, codec)
    assert [compact(r) for r in back] == [compact(r) for r in reps], "the run-length encoder lost information"
    return encoded


# --- symulacja: zestaw, który dostaje scenariusz ----------------------------------------------------------------


@dataclass
class Sim:
    """Wszystko, czego scenariusz potrzebuje: te same fixtures co testy `test_engine_day.py`, plus dziennik wywołań."""

    make_engine: Any
    broker: FakeBroker
    clock: SimClock
    market: Market
    data_dir: Path
    journal_path: Path
    notifier: Any
    log: CallLog
    runs: list[dict[str, Any]] = field(default_factory=list)

    def engine(self, mode: Mode = Mode.LIVE) -> Any:
        return self.make_engine(mode)

    def restart(self, old: Any, mode: Mode = Mode.LIVE) -> Any:
        """Nowy silnik z dziennika otwartego z dysku (stary dziennik zamknięty), ten sam broker i zegar."""
        return self.make_engine.restart(old, mode)

    async def run_day(self, label: str, engine: Any, day: date = DAY) -> None:
        """`engine.run_day(day)` z zapisem w śladzie; `ProcessDied` (symulowane kill -9) jest tu oczekiwanym końcem."""
        record: dict[str, Any] = {"label": label, "day": day, "started": self.clock.now_utc()}
        try:
            report = await engine.run_day(day)
        except ProcessDied:
            record["outcome"] = "PROCESS_DIED"
        else:
            record["outcome"] = "REPORT"
            record["report"] = report
        record["finished"] = self.clock.now_utc()
        self.runs.append(plain(record))


@dataclass(frozen=True)
class Scenario:
    name: str
    run: Callable[[Sim], Awaitable[None]]


SCENARIOS: list[Scenario] = []


def scenario(name: str) -> Callable[[Callable[[Sim], Awaitable[None]]], Callable[[Sim], Awaitable[None]]]:
    def register(fn: Callable[[Sim], Awaitable[None]]) -> Callable[[Sim], Awaitable[None]]:
        assert all(s.name != name for s in SCENARIOS), f"duplicate scenario {name}"
        SCENARIOS.append(Scenario(name, fn))
        return fn

    return register


@scenario("full_day_long_flat_1725")
async def _full_day_long(sim: Sim) -> None:
    await sim.run_day("day", sim.engine(Mode.LIVE))


@scenario("full_day_short_flat_1725")
async def _full_day_short(sim: Sim) -> None:
    sim.broker.set_candles_5m(five_minute_candles(DAY, D("25790"), D("25780")))  # świeca spadkowa: SELL
    sim.market.late_mid = MID - 20
    await sim.run_day("day", sim.engine(Mode.LIVE))


@scenario("stop_hit_by_broker")
async def _stop_hit(sim: Sim) -> None:
    engine = sim.engine(Mode.LIVE)
    at_time(sim.broker, sim.clock, STOP_HIT_UTC, lambda: sim.broker.trigger_stop("DEAL-1", STOP_LEVEL))
    await sim.run_day("day", engine)


@scenario("restart_mid_position")
async def _restart_mid_position(sim: Sim) -> None:
    first = sim.engine(Mode.LIVE)
    at_time(sim.broker, sim.clock, CRASH_UTC, die)  # proces ginie w trakcie zarządzania pozycją
    await sim.run_day("first", first)
    second = sim.restart(first, Mode.LIVE)
    await sim.run_day("second", second)


@scenario("restart_mid_entry_adopt")
async def _restart_mid_entry(sim: Sim) -> None:
    first = sim.engine(Mode.LIVE)
    sim.broker.on_call("confirm", die, once=True)  # zlecenie przyjęte u brokera, proces ginie przed potwierdzeniem
    await sim.run_day("first", first)
    second = sim.restart(first, Mode.LIVE)
    await sim.run_day("second", second)


@scenario("no_signal_doji")
async def _no_signal_doji(sim: Sim) -> None:
    sim.broker.set_candles_5m(five_minute_candles(DAY, D("25785"), D("25785")))
    await sim.run_day("day", sim.engine(Mode.LIVE))


@scenario("no_signal_first_candle_missing")
async def _no_signal_missing_candle(sim: Sim) -> None:
    sim.broker.set_candles_5m(five_minute_candles(DAY, D("25780"), D("25790"), include_first=False))
    engine = sim.engine(Mode.LIVE)
    stop_engine_at(engine, sim.clock, berlin(DAY, 9, 20))
    await sim.run_day("day", engine)


@scenario("gate_blocked_quote_silence")
async def _gate_blocked(sim: Sim) -> None:
    sim.market.frozen_from = OPEN_UTC  # od 09:00 para (bid, ask) stoi w miejscu
    engine = sim.engine(Mode.LIVE)
    stop_engine_at(engine, sim.clock, berlin(DAY, 9, 20))
    await sim.run_day("day", engine)


@scenario("dry_run_full_day")
async def _dry_run_full_day(sim: Sim) -> None:
    await sim.run_day("day", sim.engine(Mode.DRY_RUN))


@scenario("dry_run_restart_mid_day")
async def _dry_run_restart(sim: Sim) -> None:
    first = sim.engine(Mode.DRY_RUN)
    stop_engine_at(first, sim.clock, berlin(DAY, 11, 0))  # po otwarciu zakresu, decyzji i terminie wejścia
    await sim.run_day("first", first)
    second = sim.restart(first, Mode.DRY_RUN)
    await sim.run_day("second", second)


@scenario("hard_kill_emergency_flatten")
async def _hard_kill(sim: Sim) -> None:
    engine = sim.engine(Mode.LIVE)
    reopen = HARD_AT + timedelta(minutes=1)  # koniec timeoutów zamknięcia

    def timing_out() -> None:
        if HARD_AT <= sim.clock.now_utc() < reopen:
            raise BrokerTimeout("DELETE /positions: timeout")

    at_time(sim.broker, sim.clock, HARD_AT, lambda: (sim.data_dir / "HARD_STOP").write_text(""))
    sim.broker.on_call("close_position", timing_out)
    stop_engine_at(engine, sim.clock, reopen + timedelta(minutes=5))
    await sim.run_day("day", engine)


@scenario("market_closed_at_flatten")
async def _market_closed_at_flatten(sim: Sim) -> None:
    engine = sim.engine(Mode.LIVE)
    at_time(sim.broker, sim.clock, FLATTEN_UTC - timedelta(seconds=1), lambda: sim.broker.set_market_closed(True))
    at_time(
        sim.broker, sim.clock, FLATTEN_UTC + timedelta(seconds=45), lambda: sim.broker.set_market_closed(False),
        method="list_positions",
    )  # fmt: skip
    await sim.run_day("day", engine)


# --- budowa śladu, wzorzec, porównanie -------------------------------------------------------------------------


def config_hashes() -> dict[str, str]:
    return {name: hashlib.sha256(Path("config", name).read_bytes()).hexdigest() for name in CONFIG_FILES}


def build_trace(name: str, sim: Sim) -> dict[str, Any]:
    sim.log.verify()
    calls = sim.log.lines()
    journal = dump_journal(sim.journal_path)
    events = journal["raw_events"]
    journal["raw_events"] = compress(events, EventCodec)
    per_method: dict[str, int] = {}
    for _, method, _, _ in sim.log.entries:
        per_method[method] = per_method.get(method, 0) + 1
    return {
        "format": TRACE_FORMAT,
        "scenario": name,
        "config_sha256": config_hashes(),
        "runs": sim.runs,
        "clock_end": plain(sim.clock.now_utc()),
        "counts": {"broker_calls": len(calls), "raw_events": len(events)},
        "call_counts": dict(sorted(per_method.items())),
        "broker_calls": compress(calls, LineCodec),
        "broker_final": {"positions": plain(sorted(sim.broker.positions.values(), key=lambda p: p.deal_id))},
        "journal": journal,
        "alerts": plain([list(a) for a in sim.notifier.alerts]),
    }


def render(trace: Mapping[str, Any]) -> str:
    """Tekst pliku wzorca: poprawny JSON, jeden element listy w linii (czytelny diff), klucze w ustalonej kolejności."""
    out: list[str] = []
    _emit("", trace, 0, out, "")
    return "\n".join(out) + "\n"


def _emit(prefix: str, value: Any, level: int, out: list[str], trailing: str) -> None:
    pad = " " * level
    if isinstance(value, dict) and value:
        out.append(f"{pad}{prefix}{{")
        items = list(value.items())
        for i, (key, item) in enumerate(items):
            _emit(f"{compact(key)}: ", item, level + 1, out, "," if i < len(items) - 1 else "")
        out.append(f"{pad}}}{trailing}")
    elif isinstance(value, list) and value:
        out.append(f"{pad}{prefix}[")
        for i, item in enumerate(value):
            out.append(f"{pad} {compact(item)}{',' if i < len(value) - 1 else ''}")
        out.append(f"{pad}]{trailing}")
    else:
        out.append(f"{pad}{prefix}{compact(value)}{trailing}")


class GoldenMismatch(AssertionError):
    """Ślad różni się od wzorca albo wzorca brak."""


def writing_requested() -> bool:
    return os.environ.get(WRITE_ENV) == "1"


def first_difference(expected: str, actual: str) -> str:
    """Opis pierwszej różniącej się linii z kilkoma liniami kontekstu (bez pełnego diffu: ślad ma tysiące linii)."""
    want, got = expected.split("\n"), actual.split("\n")
    index = next((i for i, (a, b) in enumerate(zip(want, got, strict=False)) if a != b), min(len(want), len(got)))
    lo = max(0, index - 3)

    def window(lines: list[str]) -> str:
        return "\n".join(f"  {n + 1:>6}: {line[:240]}" for n, line in enumerate(lines[lo : index + 6], start=lo))

    return (
        f"pierwsza różnica w linii {index + 1} (wzorzec ma {len(want)} linii, ślad {len(got)})\n"
        f"--- wzorzec\n{window(want)}\n--- ślad\n{window(got)}"
    )


def check_golden(name: str, text: str, directory: Path = GOLDEN_DIR, actual_dir: Path | None = None) -> None:
    """Porównuje `text` z wzorcem bajt w bajt; zapis wzorca TYLKO przy `GW_WRITE_GOLDEN=1` (test jest wtedy pominięty)."""
    path = directory / f"{name}.json"
    actual = text.encode("utf-8")
    if writing_requested():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(actual)
        pytest.skip(f"wzorzec zapisany: {path.name}; uruchom test ponownie bez {WRITE_ENV} i obejrzyj git diff")
    if not path.is_file():
        raise GoldenMismatch(
            f"brak wzorca {path}. Test nigdy nie zapisuje go sam; świadomy zapis: {WRITE_ENV}=1 python -m pytest "
            f"tests/fault/test_golden_de40.py (tylko na kodzie, którego zachowanie ma być wzorcem)."
        )
    expected = path.read_bytes()
    if actual != expected:
        where = ""
        if actual_dir is not None:
            dump = actual_dir / f"{name}.actual.json"
            dump.write_bytes(actual)
            where = f"\nzapisany ślad do porównania: {dump}"
        raise GoldenMismatch(
            f"ślad {name} różni się od wzorca {path.name}: zachowanie DE40 v1 się zmieniło.\n"
            f"{first_difference(expected.decode('utf-8'), text)}{where}"
        )


# --- testy ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def sim(make_engine, broker, clock, market, data_dir, journal_path, notifier) -> Sim:
    """Zestaw dla scenariusza; nakładka na broker powstaje przed jakimkolwiek silnikiem."""
    return Sim(make_engine, broker, clock, market, data_dir, journal_path, notifier, CallLog(broker, clock))


@pytest.mark.parametrize("name", [s.name for s in SCENARIOS])
async def test_de40_behaviour_matches_the_golden_trace(name, sim, sim_root, tmp_path):
    [chosen] = [s for s in SCENARIOS if s.name == name]
    await chosen.run(sim)
    text = render(build_trace(name, sim))
    for leak in (str(sim_root), str(tmp_path), str(Path.cwd()), "/dev/shm", "gwstock-sim-"):
        assert leak not in text, f"the trace contains a run-dependent path fragment: {leak}"
    check_golden(name, text, actual_dir=tmp_path)


def test_every_scenario_has_a_golden_file_and_every_golden_file_a_scenario():
    if writing_requested():
        pytest.skip(f"tryb zapisu wzorców ({WRITE_ENV}=1)")
    expected = {f"{s.name}.json" for s in SCENARIOS}
    present = {p.name for p in GOLDEN_DIR.glob("*") if p.is_file()} if GOLDEN_DIR.is_dir() else set()
    assert present == expected, f"brakujące: {sorted(expected - present)}, nadmiarowe: {sorted(present - expected)}"


@pytest.mark.parametrize("name", [s.name for s in SCENARIOS])
def test_golden_files_are_in_the_canonical_form_and_carry_the_current_config(name):
    if writing_requested():
        pytest.skip(f"tryb zapisu wzorców ({WRITE_ENV}=1)")
    text = (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert render(parsed) == text, "the golden file was edited by hand (not in the canonical form)"
    assert parsed["format"] == TRACE_FORMAT and parsed["scenario"] == name
    assert parsed["config_sha256"] == config_hashes(), "config/*_v1.toml changed: the golden trace is for other config"
    assert "\r" not in text


# --- sam mechanizm porównania: nigdy nie zapisuje wzorca po cichu --------------------------------------------------


def test_check_golden_never_writes_without_the_switch_and_fails_loudly_on_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv(WRITE_ENV, raising=False)
    with pytest.raises(GoldenMismatch, match="brak wzorca"):
        check_golden("x", "{}\n", directory=tmp_path)
    assert list(tmp_path.iterdir()) == []  # nic nie zapisano
    for not_the_switch in ("0", "", "true", "yes", "11"):
        monkeypatch.setenv(WRITE_ENV, not_the_switch)
        with pytest.raises(GoldenMismatch, match="brak wzorca"):
            check_golden("x", "{}\n", directory=tmp_path)
        assert list(tmp_path.iterdir()) == []


def test_check_golden_compares_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.delenv(WRITE_ENV, raising=False)
    (tmp_path / "x.json").write_bytes(b'{\n "a": 1\n}\n')
    check_golden("x", '{\n "a": 1\n}\n', directory=tmp_path)  # identyczny: przechodzi
    for other in ('{\n "a": 2\n}\n', '{\n "a": 1\n}', '{\n "a": 1 \n}\n', '{\r\n "a": 1\r\n}\r\n', ""):
        with pytest.raises(GoldenMismatch, match="różni się od wzorca"):
            check_golden("x", other, directory=tmp_path, actual_dir=tmp_path)
    assert (tmp_path / "x.json").read_bytes() == b'{\n "a": 1\n}\n'  # wzorzec nietknięty


def test_check_golden_writes_only_with_the_switch_and_then_skips(tmp_path, monkeypatch):
    monkeypatch.setenv(WRITE_ENV, "1")
    target = tmp_path / "new" / "golden"
    with pytest.raises(pytest.skip.Exception):
        check_golden("x", "{}\n", directory=target)
    assert (target / "x.json").read_bytes() == b"{}\n"


def test_render_is_valid_json_and_stable():
    trace = {"a": [1, {"b": "ł"}], "c": {"d": [], "e": {}, "f": None}, "g": "x"}
    text = render(trace)
    assert json.loads(text) == trace and render(json.loads(text)) == text
    assert text.isascii() and text.endswith("\n") and "\r" not in text


def polling_lines(ticks: int, start: datetime = datetime(2026, 11, 2, 7, 50, tzinfo=UTC)) -> list[str]:
    lines = []
    for m in range(ticks):
        t = start + timedelta(seconds=10 * m)
        lines += [
            f"{t.isoformat()} quote " + '["DE40"]',
            f"{t.isoformat()} list_positions",
            f"{t.isoformat()} list_working_orders",
            f"{(t + timedelta(seconds=5)).isoformat()} quote " + '["DE40"]',
        ]
    return lines


def test_run_length_encoding_folds_the_polling_blocks_and_loses_nothing():
    lines = polling_lines(40)
    encoded = compress(lines, LineCodec)
    assert len(encoded) == 1 and encoded[0]["repeat"] == 40 and encoded[0]["every_s"] == "10"
    assert encoded[0]["block"] == lines[:4] and decode_runs(encoded, LineCodec) == lines
    broken = list(lines)
    broken[77] = broken[77].replace("list_positions", "list_working_orders")  # jedna zmieniona kolejność w środku
    assert compress(broken, LineCodec) != encoded
    assert decode_runs(compress(broken, LineCodec), LineCodec) == broken
    short = polling_lines(2)  # poniżej MIN_REPEAT bloków: zapis wprost
    assert compress(short, LineCodec) == short


def test_run_length_encoding_never_folds_what_it_cannot_shift_exactly():
    lines = [f"2026-11-02T07:50:{s:02d}Z quote" for s in range(0, 50, 5)]  # "Z" zamiast +00:00: nie kanoniczny czas
    assert compress(lines, LineCodec) == lines
    base = datetime(2026, 11, 2, 7, tzinfo=UTC)
    irregular = [f"{(base + timedelta(seconds=s)).isoformat()} ping" for s in (0, 5, 11, 16)]
    assert compress(irregular, LineCodec) == irregular  # odstępy nierówne: brak okresu


def test_run_length_encoding_round_trips_random_sequences_of_both_kinds():
    import random

    rng = random.Random(20261104)  # ziarno stałe: ten sam zestaw przy każdym uruchomieniu
    start = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    folded = 0
    for _ in range(300):
        lines, events = [], []
        t, number = start, 1
        for _ in range(rng.randint(0, 25)):
            methods = ["quote", "list_positions", "close_position", "keepalive"]
            block = [rng.choice(methods) for _ in range(rng.randint(1, 5))]
            step = timedelta(seconds=rng.choice([1, 5, 10]), microseconds=rng.choice([0, 0, 250000]))
            for _ in range(rng.randint(1, 8)):
                for method in block:
                    lines.append(f"{t.isoformat()} {method}")
                    events.append({"id": number, "source": method, "ts": t.isoformat(), "payload": {"n": 1}})
                    number += 1
                t += step
            if rng.random() < 0.3:  # zdarzenie spoza okresu
                lines.append(f"{t.isoformat()} odd")
                events.append({"id": number, "source": "odd", "ts": t.isoformat(), "payload": {"n": rng.randint(0, 3)}})
                number += rng.randint(1, 3)
        folded += len(compress(lines, LineCodec)) < len(lines)
        encoded = compress(events, EventCodec)
        folded += len(encoded) < len(events)
        assert [compact(e) for e in decode_runs(encoded, EventCodec)] == [compact(e) for e in events]
    assert folded > 200  # kodowanie realnie skraca zapis, nie tylko oddaje wejście


def test_event_row_keeps_the_payload_text_exactly():
    canonical_text = '{"a":"1","b":[1,2]}'
    assert event_row((1, "s", canonical_text, "t"))["payload"] == {"a": "1", "b": [1, 2]}
    assert canonical(event_row((1, "s", canonical_text, "t"))["payload"]) == canonical_text
    for odd in ('{"b":1,"a":2}', '{"a": 1}', "not json"):
        row = event_row((1, "s", odd, "t"))
        assert row["payload_text"] == odd and "payload" not in row
