"""Wsparcie testów E2E cyklu życia transakcji (`tests/fault/test_trade_lifecycle_e2e.py`).

Podział na to, co przeżywa restart silnika, i to, co ginie razem z procesem:

- `World` (przeżywa): broker `FakeBroker` (jego pozycje, zamknięcia, potwierdzenia i kolejka awarii są stanem BROKERA,
  niezależnym od silnika), zegar `FakeClock`, pliki (dziennik SQLite, katalog `data/` z plikiem alarmów) i konfiguracja
  wczytana z `config/` (wartości limitów nigdy nie są tu przepisywane);
- `Stack` (ginie): `Journal`, `KillSwitch`, `OrderManager`, `RiskGate`, `Executor`, `Reconciler` i notyfikator plikowy.

`Rig.restart()` (wymóg B): zamyka dziennik, sprawdza, że stare połączenie naprawdę nie działa, porzuca stary stos
i po `gc.collect()` sprawdza słabymi referencjami, że ŻADEN obiekt poprzedniej instancji nie przeżył (nic z pamięci
nie przechodzi przez restart), a potem buduje nowy stos z dziennika otwartego z dysku i sprawdza, że nowe obiekty są
połączone wyłącznie z nowym dziennikiem i z TYM SAMYM brokerem (wymóg C). `stop()`/`start()` rozdzielają te kroki,
gdy coś ma się wydarzyć u brokera, kiedy silnika nie ma.

`read_disk` czyta stan z dysku OSOBNYM połączeniem tylko do odczytu (zlecenia, pozycja, księga, liczniki wyniku, kill
switch, zdarzenia, znaczniki), `alerts` czyta plik `data/alerts.log` (`FileNotifier`, jak w produkcji). Asercje testów
opierają się na tym, nie na atrybutach w pamięci.

Awarie dziennika: `FaultyConnection` podstawiony za połączenie sqlite3 dziennika (`install_faults`) liczy transakcje
zapisu (każde `BEGIN IMMEDIATE` to jeden punkt zapisu), nazywa je pierwszą instrukcją zmieniającą dane i wstrzykuje
`sqlite3.OperationalError` w wybranym punkcie, przed wybraną instrukcją albo tuż po niej wykonuje akcję (np. obraz
katastrofy `crash_image` i śmierć procesu `ProcessDied`). Kod `src/` nie jest zmieniany.
"""

from __future__ import annotations

import gc
import json
import shutil
import sqlite3
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from gwstock.app import Engine, Mode
from gwstock.broker.fake import FakeBroker
from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import CostConfig, RiskConfig, StrategyConfig
from gwstock.costs import CostModel
from gwstock.execution import Executor
from gwstock.journal import Journal, JournalError, KillState, OrderRecord, PositionRecord, TradeRecord
from gwstock.notify import FileNotifier
from gwstock.orders.manager import OrderManager
from gwstock.reconcile import Reconciler, ReconcileResult
from gwstock.risk import EntryCandidate, KillSwitch, RiskGate
from gwstock.strategy import EntrySignal
from gwstock.types import InstrumentRules, KillLevel, PositionState, Quote, Side

D = Decimal
CENT = D("0.01")  # rozdzielczość księgi (koszty i wynik zaokrąglane do 0,01 EUR), nie parametr ryzyka
EPIC = "DE40"
DAY = date(2026, 11, 2)  # poniedziałek, CET: dzień sesji XETRA
EARLIER_DAY = date(2026, 10, 30)  # piątek przed nim: dzień wcześniejszych strat (tylko licznik łączny)
T_ENTRY = datetime(2026, 11, 2, 8, 6, tzinfo=timezone.utc)  # 09:06 w Berlinie: w oknie wejścia 09:05-09:10
T_FLATTEN = datetime(2026, 11, 2, 16, 25, tzinfo=timezone.utc)  # 17:25 w Berlinie
MID = D("25781.3")
HALF_SPREAD = D("1.0")
BID, ASK = MID - HALF_SPREAD, MID + HALF_SPREAD
SIZE = D("0.007")  # ryzyko 0,007 × 30 × 1 = 0,21 EUR
STOP_DISTANCE = D("30")
FILL = ASK  # BUY po ask (bez poślizgu)
STOP_LEVEL = FILL - STOP_DISTANCE
DEAL = "DEAL-1"
CID = "gws-20261102-DE40-1-entry"
ALERTS_FILE = "alerts.log"
RULES = InstrumentRules(EPIC, D("0.001"), D("0.001"), D("1"), D("0.05"), D("2.6"))

_P = PositionState


class ProcessDied(BaseException):
    """Śmierć procesu (kill -9) w wybranym miejscu: nie jest `Exception`, więc żadna siatka w kodzie jej nie połknie."""


def die(*_args: Any, **_kwargs: Any) -> None:
    raise ProcessDied("process killed here")


# --- świat: to, co przeżywa restart --------------------------------------------------------------------------------


@dataclass
class Market:
    """Notowanie rynku: bieżący mid i połowa spreadu; zawsze świeże (`ts` = teraz), więc bramka nie widzi ciszy."""

    mid: Decimal = MID
    half: Decimal = HALF_SPREAD

    def quote(self, now: datetime) -> Quote:
        return Quote(EPIC, self.mid - self.half, self.mid + self.half, now)


@dataclass
class World:
    root: Path
    clock: FakeClock
    broker: FakeBroker
    market: Market
    strat: StrategyConfig
    risk: RiskConfig
    costs: CostConfig
    calendar: ExchangeCalendar
    data_dir: Path
    journal_path: Path

    @property
    def alerts_path(self) -> Path:
        return self.data_dir / ALERTS_FILE

    def at(self, when: datetime) -> None:
        """Zegar na `when` (tylko do przodu)."""
        self.clock.advance((when - self.clock.now_utc()).total_seconds())


def make_world(root: Path, cfgs: tuple[StrategyConfig, RiskConfig, CostConfig], *, start: datetime = T_ENTRY) -> World:
    """Nowy świat w `root`: broker z aktywnym rachunkiem ACC-1 (EUR, saldo 40), notowanie bid 25780.3 / ask 25782.3."""
    root.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    clock = FakeClock(start)
    market = Market()
    broker = FakeBroker(clock, RULES, market.quote(start))
    broker.set_quote_fn(market.quote)
    broker.accounts = [a for a in broker.accounts if a.account_id == "ACC-1"]  # ACC-0 ma saldo 0 (bramka: LEVERAGE)
    strat, risk, costs = cfgs
    return World(root, clock, broker, market, strat, risk, costs, ExchangeCalendar(XETRA), data_dir,
                 root / "journal.sqlite")  # fmt: skip


def entry_signal() -> EntrySignal:
    """Sygnał BUY: stop 30 pkt, wielkość 0.007; notowanie decyzji z 09:06 (bid 25780.3, ask 25782.3)."""
    return EntrySignal(Side.BUY, STOP_DISTANCE, SIZE, Quote(EPIC, BID, ASK, T_ENTRY))


def entry_candidate(world: World) -> EntryCandidate:
    """Kandydat jak w `Executor._prepare` dla `entry_signal()`, ze świeżym notowaniem (wiek 0 s)."""
    return EntryCandidate(EPIC, Side.BUY, SIZE, STOP_DISTANCE, MID, RULES.value_per_point, RULES.margin_rate, 0)


# --- stos: to, co ginie z procesem ---------------------------------------------------------------------------------


@dataclass(eq=False)
class Stack:
    journal: Journal
    notifier: FileNotifier
    kill: KillSwitch
    manager: OrderManager
    gate: RiskGate
    executor: Executor
    reconciler: Reconciler

    def parts(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def boot(world: World, path: Path) -> Stack:
    """Składa stos jak silnik (`Engine.__init__`), na dzienniku otwartym z pliku `path`; wykonawca w trybie live."""
    journal = Journal.open(str(path))
    notifier = FileNotifier(world.alerts_path, world.clock)
    kill = KillSwitch(journal, world.clock, world.data_dir, notifier)
    manager = OrderManager(journal, world.broker, world.clock, world.risk)
    gate = RiskGate(world.risk, world.strat, journal, world.calendar, world.clock, kill)
    executor = Executor(
        manager, world.broker, journal, gate, kill, CostModel(world.costs), world.clock, world.strat, world.risk,
        world.calendar, notifier, live=True,
    )  # fmt: skip
    reconciler = Reconciler(
        journal, world.broker, world.clock, kill, world.risk, EPIC, booker=executor,
        entry_gate=lambda: manager.in_flight,
    )  # fmt: skip
    return Stack(journal, notifier, kill, manager, gate, executor, reconciler)


def assert_fresh_wiring(stack: Stack, world: World) -> None:
    """Każda część stosu wskazuje wyłącznie na obiekty TEGO stosu oraz na wspólny broker i zegar."""
    expected = {
        Journal: stack.journal, KillSwitch: stack.kill, OrderManager: stack.manager, Executor: stack.executor,
        RiskGate: stack.gate, FakeBroker: world.broker, FakeClock: world.clock,
    }  # fmt: skip
    for name, part in stack.parts().items():
        for attr, value in vars(part).items():
            for cls, current in expected.items():
                if isinstance(value, cls) and value is not current:
                    raise AssertionError(f"{name}.{attr} is a {cls.__name__} that does not belong to the new stack")
    assert stack.journal.pragma("journal_mode") == "wal"


class Rig:
    """Stos silnika na świecie `world`; jedyna silna referencja do stosu (testy sięgają po części przez `rig.s`)."""

    def __init__(self, world: World, path: Path | None = None) -> None:
        self.world = world
        self.path = path or world.journal_path
        self.restarts = 0
        self._stack: Stack | None = boot(world, self.path)

    @property
    def s(self) -> Stack:
        if self._stack is None:
            raise AssertionError("the engine is down (stopped and not started again)")
        return self._stack

    @property
    def running(self) -> bool:
        return self._stack is not None

    def stop(self) -> None:
        """Koniec procesu: dziennik zamknięty, wszystkie obiekty porzucone (sprawdzone słabymi referencjami)."""
        old = self._stack
        if old is None:
            raise AssertionError("the engine is already down")
        self._stack = None
        refs = {name: weakref.ref(obj) for name, obj in old.parts().items()}
        old.journal.close()
        try:
            old.journal.get_meta("restart-probe")
        except JournalError:
            pass
        else:
            raise AssertionError("the previous journal connection still works after close()")
        del old
        gc.collect()
        alive = sorted(name for name, ref in refs.items() if ref() is not None)
        if alive:
            raise AssertionError(f"objects of the previous engine instance survived the restart: {alive}")

    def start(self, path: Path | None = None) -> None:
        """Nowy proces: dziennik otwarty z dysku (opcjonalnie z innego pliku, np. obrazu katastrofy), nowe obiekty."""
        if self._stack is not None:
            raise AssertionError("the engine is already running")
        if path is not None:
            self.path = path
        self._stack = boot(self.world, self.path)
        self.restarts += 1
        assert_fresh_wiring(self._stack, self.world)

    def restart(self, path: Path | None = None) -> None:
        self.stop()
        self.start(path)

    def close(self) -> None:
        if self._stack is not None:
            self._stack.journal.close()
            self._stack = None


# --- kroki silnika na poziomie części ----------------------------------------------------------------------------


async def reconcile(rig: Rig, *, advance: bool = True) -> ReconcileResult:
    """Jeden przebieg uzgadniania co `reconcile_interval_s`, z zabezpieczeniem pozycji bez stopu (jak `Engine`)."""
    if advance:
        rig.world.clock.advance(rig.world.risk.reconcile_interval_s)
    result = await rig.s.reconciler.run_once()
    if not result.skipped:
        for p in result.unprotected:
            await rig.s.executor.protect_unprotected(p)
    return result


async def manage(rig: Rig, flatten_at: datetime = T_FLATTEN) -> ReconcileResult:
    """Lustro `Engine._manage`: uzgadnianie, potem HARD → awaryjne spłaszczenie, EXITING → ponowienie, OPEN po
    godzinie spłaszczenia → spłaszczenie FORCED."""
    result = await reconcile(rig)
    if rig.s.kill.level() is KillLevel.HARD:
        await rig.s.executor.emergency_flatten()
        return result
    position = rig.s.journal.current_position(EPIC)
    if position is not None and (
        position.state is _P.EXITING or (position.state is _P.OPEN and rig.world.clock.now_utc() >= flatten_at)
    ):
        await rig.s.executor.flatten(position, "FORCED")
    return result


# --- stan na dysku -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskState:
    """Migawka dziennika odczytana z dysku osobnym połączeniem tylko do odczytu."""

    orders: list[OrderRecord]
    position: PositionRecord | None
    trades: list[TradeRecord]
    trade_rows: list[tuple[str, str, str]]  # (deal_id, exit_day, net_pnl) w kolejności księgowania
    pnl: dict[date, Decimal]  # wiersze `realized_pnl` (dzień wyjścia → suma netto)
    pnl_total: Decimal
    kill: KillState
    events: list[tuple[str, dict[str, Any]]]  # (źródło, ładunek JSON) w kolejności zapisu
    meta: dict[str, str]

    def order(self, order_id: str) -> OrderRecord:
        found = [o for o in self.orders if o.order_id == order_id]
        assert len(found) == 1, f"order {order_id} not on disk: {[o.order_id for o in self.orders]}"
        return found[0]

    def payloads(self, source: str) -> list[dict[str, Any]]:
        return [p for s, p in self.events if s == source]

    def codes(self, source: str = "event") -> list[str]:
        return [p.get("event") for p in self.payloads(source)]

    def kill_audit(self, *events: str) -> list[dict[str, Any]]:
        rows = self.payloads("kill.audit")
        return [r for r in rows if r["event"] in events] if events else rows


def read_disk(path: Path) -> DiskState:
    journal = Journal.open_readonly(str(path))
    raw = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        ids = [r[0] for r in raw.execute("SELECT order_id FROM orders ORDER BY rowid")]
        pnl = {date.fromisoformat(d): Decimal(n) for d, n in raw.execute("SELECT day, net FROM realized_pnl")}
        trade_rows = [tuple(r) for r in raw.execute("SELECT deal_id, exit_day, net_pnl FROM trades ORDER BY id")]
        meta = dict(raw.execute("SELECT key, value FROM meta").fetchall())
        events = []
        for e in journal.events():
            try:
                payload = json.loads(e.payload)
            except ValueError:
                payload = {"raw": e.payload}
            events.append((e.source, payload if isinstance(payload, dict) else {"value": payload}))
        return DiskState(
            orders=[journal.get_order(i) for i in ids], position=journal.current_position(EPIC),
            trades=journal.trades(), trade_rows=trade_rows, pnl=pnl, pnl_total=journal.realized_pnl_total(),
            kill=journal.kill_state(), events=events, meta=meta,
        )  # fmt: skip
    finally:
        raw.close()
        journal.close()


def alerts(world: World) -> list[str]:
    """Linie pliku alarmów `data/alerts.log` (jeden alarm na linię)."""
    path = world.alerts_path
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def assert_books_balance(disk: DiskState, seeds: dict[date, Decimal] | None = None) -> None:
    """Księga i liczniki wyniku zgadzają się co do grosza: licznik dnia = zasiew + suma netto transakcji tego dnia
    wyjścia; każdy deal zaksięgowany najwyżej raz; licznik łączny = suma liczników dni."""
    seeds = seeds or {}
    deal_ids = [row[0] for row in disk.trade_rows]
    assert len(deal_ids) == len(set(deal_ids)) == len(disk.trades), disk.trade_rows
    expected: dict[date, Decimal] = dict(seeds)
    for _deal, day, net in disk.trade_rows:
        key = date.fromisoformat(day)
        expected[key] = expected.get(key, Decimal(0)) + Decimal(net)
    assert disk.pnl == expected, (disk.pnl, expected)
    assert disk.pnl_total == sum(expected.values(), Decimal(0))
    for trade, (_deal, _day, net) in zip(disk.trades, disk.trade_rows, strict=True):
        assert trade.net_pnl == Decimal(net)
        assert trade.net_pnl == (
            trade.gross_pnl - trade.spread_cost - trade.slippage_penalty - trade.financing - trade.fees
        )


def crash_image(src: Path, dest_dir: Path) -> Path:
    """Obraz plików dziennika w tej chwili (baza i WAL, bajt w bajt), jak po kill -9: SQLite odtworzy z niego tylko
    zatwierdzone transakcje. Zwraca ścieżkę dziennika w obrazie."""
    dest_dir.mkdir(parents=True)
    for suffix in ("", "-wal"):
        source = Path(f"{src}{suffix}")
        if source.exists():
            shutil.copyfile(source, dest_dir / f"{src.name}{suffix}")
    return dest_dir / src.name


# --- awarie dziennika ----------------------------------------------------------------------------------------------


def _label(sql: str, params: Any) -> str | None:
    """Nazwa punktu zapisu z pierwszej instrukcji zmieniającej dane (np. `raw:broker.account`, `order:SUBMITTING`)."""
    words = sql.split()
    verb = words[0].upper()
    if verb == "INSERT":
        table = words[2]
    elif verb == "UPDATE":
        table = words[1]
    else:
        return None
    params = tuple(params or ())
    if table == "raw_events":
        return f"raw:{params[0]}"
    if table == "meta":
        return f"meta:{str(params[0]).split(':')[0]}"
    if table == "orders":
        return f"order:{params[5] if verb == 'INSERT' else params[0]}"
    if table == "positions":
        return f"position:{params[1]}"
    return table


@dataclass
class _Rule:
    prefix: str
    action: Callable[[], None]
    before: bool


@dataclass
class FaultyConnection:
    """Pośrednik połączenia sqlite3 dziennika: liczy transakcje zapisu i wstrzykuje awarie (patrz docstring modułu)."""

    conn: sqlite3.Connection
    labels: list[str] = field(default_factory=list)  # nazwa każdej rozpoczętej transakcji zapisu (punkt zapisu)
    fail_at: set[int] = field(default_factory=set)  # numery (od 1) transakcji, których BEGIN zawodzi
    fail_from: int | None = None  # od tego numeru zawodzi każda transakcja (pełny dysk)
    rules: list[_Rule] = field(default_factory=list)
    _labelled: bool = True

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        if sql == "BEGIN IMMEDIATE":
            n = len(self.labels) + 1
            self.labels.append("?")
            self._labelled = False
            if n in self.fail_at or (self.fail_from is not None and n >= self.fail_from):
                raise sqlite3.OperationalError(f"disk I/O error (injected at write {n})")
        elif not self._labelled:
            label = _label(sql, params)
            if label is not None:
                self.labels[-1] = label
                self._labelled = True
        for rule in [r for r in self.rules if r.before and sql.startswith(r.prefix)]:
            self.rules.remove(rule)
            rule.action()
        cursor = self.conn.execute(sql, params)
        for rule in [r for r in self.rules if not r.before and sql.startswith(r.prefix)]:
            self.rules.remove(rule)
            rule.action()
        return cursor

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def close(self) -> None:
        self.conn.close()

    @property
    def writes(self) -> int:
        return len(self.labels)

    def fail_before(self, prefix: str) -> None:
        """Najbliższa instrukcja zaczynająca się od `prefix` rzuca błąd dysku, zanim się wykona (raz)."""

        def boom() -> None:
            raise sqlite3.OperationalError(f"disk I/O error (injected before {prefix!r})")

        self.rules.append(_Rule(prefix, boom, before=True))

    def after(self, prefix: str, action: Callable[[], None]) -> None:
        """Tuż po najbliższej instrukcji zaczynającej się od `prefix` (w otwartej transakcji) wykonaj `action` (raz)."""
        self.rules.append(_Rule(prefix, action, before=False))


def install_faults(journal: Journal) -> FaultyConnection:
    faulty = FaultyConnection(journal._conn)  # test podmienia wykonanie instrukcji połączenia, nie kod `src/`
    journal._conn = faulty  # type: ignore[assignment]
    return faulty


# --- broker: pomocnicze sterowanie ---------------------------------------------------------------------------------


def lag_history(broker: FakeBroker, clock: FakeClock, seconds: float) -> None:
    """Historia zamknięć brokera się spóźnia: zamknięcie widać dopiero `seconds` po jego czasie (stan brokera, więc
    przeżywa restart silnika)."""
    real = broker.closed_since
    delay = timedelta(seconds=seconds)

    async def lagging(since: datetime):  # noqa: ANN202
        closures = await real(since)
        now = clock.now_utc()
        return [c for c in closures if c.ts + delay <= now]

    broker.closed_since = lagging  # type: ignore[method-assign]


def calls_with_index(broker: FakeBroker, method: str) -> list[int]:
    """Pozycje kolejnych wywołań `method` w `broker.calls` (indeks wpisu tego wywołania)."""
    seen: list[int] = []
    broker.on_call(method, lambda: seen.append(len(broker.calls) - 1))
    return seen


def crash_after(obj: object, name: str, *, when: Callable[..., bool] = lambda *a, **k: True) -> None:
    """`obj.name` wykonuje się normalnie (np. zatwierdza transakcję), a zaraz potem proces ginie (gdy `when`)."""
    real = getattr(obj, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if when(*args, **kwargs):
            die()
        return result

    setattr(obj, name, wrapper)


def crash_before(obj: object, name: str, when: Callable[..., bool]) -> None:
    """Proces ginie tuż przed wywołaniem `obj.name`, gdy `when(argumenty)`; inaczej wywołanie przechodzi."""
    real = getattr(obj, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if when(*args, **kwargs):
            die()
        return real(*args, **kwargs)

    setattr(obj, name, wrapper)


def crash_on_call(obj: object, name: str, n: int) -> None:
    """Proces ginie przy n-tym wywołaniu `obj.name` (od 1), zanim się wykona."""
    real = getattr(obj, name)
    count = {"n": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        count["n"] += 1
        if count["n"] == n:
            die()
        return real(*args, **kwargs)

    setattr(obj, name, wrapper)


# --- silnik ------------------------------------------------------------------------------------------------------


class EngineRig:
    """`Engine` budowany przez `EngineFactory` z `tests/fault/engine_sim.py` (ta sama symulacja uzbrojenia, ten sam
    broker i zegar), ale z dziennikiem trzymanym tylko tutaj: restart zamyka dziennik, porzuca silnik (sprawdzone
    słabymi referencjami do silnika i wszystkich jego części) i buduje NOWY silnik z dziennika otwartego z dysku."""

    def __init__(self, factory: Any, path: Path) -> None:
        self.factory = factory
        self.path = path
        self._engine: Engine | None = self._build()

    def _build(self) -> Engine:
        return self.factory(Mode.LIVE, journal=Journal.open(str(self.path)))

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise AssertionError("the engine is down")
        return self._engine

    def restart(self, path: Path | None = None) -> None:
        old = self._engine
        assert old is not None
        self._engine = None
        names = ("journal", "kill", "manager", "gate", "executor", "reconciler", "watchdog", "broker", "alerts")
        refs = {"engine": weakref.ref(old), **{n: weakref.ref(getattr(old, n)) for n in names}}
        old.journal.close()
        del old
        gc.collect()
        alive = sorted(name for name, ref in refs.items() if ref() is not None)
        if alive:
            raise AssertionError(f"objects of the previous engine survived the restart: {alive}")
        if path is not None:
            self.path = path
        self._engine = self._build()
        engine = self._engine
        for part in (engine.kill, engine.manager, engine.gate, engine.executor, engine.reconciler):
            journals = [v for v in vars(part).values() if isinstance(v, Journal)]
            assert journals and all(j is engine.journal for j in journals)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.journal.close()
            self._engine = None
