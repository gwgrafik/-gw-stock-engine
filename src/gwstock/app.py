"""Silnik: korzeń kompozycji i pętla dnia (spec §4, §8, §9, §13).

`Engine` składa dziennik, kill switch, menedżera zleceń, bramkę ryzyka, wykonawcę, uzgadnianie i nadzór, a potem
prowadzi dzień giełdy JEDNĄ korutyną: kolejne takty wykonywane po sobie, bez zadań w tle (poza obsługą sygnału, która
tylko ustawia flagę zatrzymania). Wszystkie sekwencje wobec brokera i dziennika biegną w tej pętli, więc uzgadnianie,
wykonawca i menedżer nigdy się nie przeplatają (flaga `in_flight` menedżera jako bramka uzgadniania zostaje jako obrona
w głąb).

Oś dnia (godziny wyłącznie z kalendarza i konfiguracji, porównania w UTC):
- dzień bez sesji albo wygasły kalendarz → raport `NOT_SESSION`; pozycja lub zlecenie z poprzedniego dnia są nadal
  zarządzane (logowanie, uzgadnianie, spłaszczenie) do FLAT albo do północy;
- przed przygotowaniem (otwarcie − `PREOPEN_LEAD_MIN`) spoczynek: tętno i pliki kill switcha, bez brokera (chyba że
  jest pozostała pozycja);
- takt (co `quote_poll_s`, uzgadnianie co `reconcile_interval_s`, ping co `ping_interval_s`): pliki kill switcha,
  logowanie (w razie potrzeby), kontrola rachunku (live), notowanie → Watchdog, ping, zarządzanie (uzgadnianie,
  stop dla pozycji bez stopu, straże wypełnienia przyjętego przez uzgadnianie, HARD → awaryjne spłaszczenie,
  EXITING → ponowienie, OPEN po godzinie spłaszczenia → spłaszczenie FORCED), dane sygnału (reguły, ATR14), decyzja,
  tętno;
- decyzja po zamknięciu pierwszej świecy (otwarcie + `range_minutes`): świeca 5-minutowa o starcie RÓWNYM otwarciu
  sesji w UTC (nigdy „najbliższa"); brak → ponawianie do terminu (+ `entry_deadline_after_range_min`) i potem
  `NO_SIGNAL_DATA`; start po terminie → `NO_SIGNAL_DATA`. Zdarzenie `decision` (DECISION) zawsze zapisuje świecę
  (start UTC i OHLC), ATR, notowanie, tryb, `session_index`, `rule_size_allowed` i wynik: semantyka stempla świec
  jest niepotwierdzona (L0), więc właściciel porównuje ją z wykresem przed uzbrojeniem;
- spłaszczenie o `calendar.flatten_time(day, flatten_before_close_min)`, ponawiane co takt; po zamknięciu
  uzgadnianie końcowe i `DayReport`.

Logowanie i wygasła sesja: każde wywołanie brokera przez `Engine._call` (dla menedżera, wykonawcy i uzgadniania przez
`GuardedBroker`). `SessionExpired` → (jeśli `watchdog.may_login()`) jedno szeregowane `broker.login()`, potem JEDNO
ponowienie; zawiedzie albo nie wolno logować → wyjątek idzie do obsługi wywołującego. Także wyjścia: zamknięcie, które
trafi na wygasłą sesję, jest ponawiane po zalogowaniu w tym samym takcie. Wyjątek: `open_position` NIE jest ponawiane
(Capital.com nie zna klucza idempotencji): menedżer widzi wyjątek, stan UNKNOWN i rozstrzyga go listą pozycji.

Tryby: `Mode.DRY_RUN` (domyślny) robi wszystko poza zleceniami wejścia (wykonawca bez `live` nigdy nie woła menedżera,
a `GuardedBroker.open_position` w dry_run odmawia jako druga linia obrony); zarządzanie istniejącą pozycją (stop,
spłaszczenie) działa w obu trybach, bo wyjść nie blokujemy nigdy. `Mode.LIVE` wymaga przy każdym starcie dnia: rachunek
związany jest na liście brokera, ENABLED, w EUR, saldo > 0, a aktywny rachunek sesji to właśnie on; inaczej silnik
przechodzi na dry_run do końca procesu i alarmuje. Silnik live bez wykonawcy jako księgującego uzgadniania odmawia
startu (`EngineMisconfigured`): zamknięcie stopem nie zostałoby zaksięgowane.

Zatrzymanie (`request_shutdown`, sygnał): koniec wywołań brokera przy najbliższej granicy kroku taktu; otwarta pozycja
zostaje u brokera ze stopem, co trafia do logu. Anulowanie (`CancelledError`) przechodzi dalej po wpisie do logu.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from gwstock.broker.base import Broker, Capabilities
from gwstock.calendar import ExchangeCalendar
from gwstock.clock import Clock
from gwstock.config import CostConfig, RiskConfig, RuntimeConfig, StrategyConfig, load_runtime
from gwstock.costs import CostModel
from gwstock.execution import KILL_CHECK_FAILED, Executor, ExitMemory
from gwstock.journal import Journal, JournalError, JournalWriteError, PositionRecord
from gwstock.notify import FileNotifier, Notifier
from gwstock.orders.manager import OrderManager
from gwstock.reconcile import Reconciler, ReconcileResult
from gwstock.risk import JOURNAL_WRITE_FAILED, KillSwitch, RiskGate
from gwstock.serialization import dump
from gwstock.strategy import ATR_PERIOD, NO_SIGNAL_DATA, EntrySignal, Skip, atr14, decide
from gwstock.types import (
    AccountInfo,
    BrokerAuthError,
    BrokerError,
    BrokerRejected,
    Candle,
    ClosedDeal,
    EntryAck,
    EntryRequest,
    InstrumentRules,
    KillLevel,
    MarketClosed,
    OpenPosition,
    OrderState,
    PositionState,
    Quote,
    SessionExpired,
    WorkingOrder,
)
from gwstock.watchdog import InstanceLock, RateLimitedNotifier, Watchdog

_LOG = logging.getLogger("gwstock.engine")
T = TypeVar("T")
_P = PositionState


class Mode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


# Stałe pracy silnika (nie parametry strategii ani ryzyka: te są wyłącznie w config/*.toml).
QUOTE_POLL_S = 5  # odczyt notowania przez REST (Deviation D1: bez strumienia WebSocket w v1)
PREOPEN_LEAD_MIN = 10  # przygotowanie (logowanie, uzgadnianie startowe, ATR) przed otwarciem sesji
SLEEP_CHUNK_S = 5  # najdłuższy pojedynczy sen: flaga zatrzymania i tętno najpóźniej co tyle sekund
MIN_SLEEP_S = 0.5  # zabezpieczenie przed pętlą bez upływu czasu (gdyby termin wypadł w przeszłości)
ERROR_PAUSE_S = 60  # przerwa po nieoczekiwanym wyjątku dnia w `run_forever`
ACCOUNT_CURRENCY = "EUR"  # spec §9: rachunek w EUR (DE40 w EUR, bez przewalutowania)
ACCOUNT_STATUS = "ENABLED"
HEARTBEAT_FILE = "heartbeat.json"
LOCK_FILE = "engine.lock"
ALERTS_FILE = "alerts.log"
L1_PASSED_FILE = "L1_PASSED"  # bramka L1: tworzy WYŁĄCZNIE właściciel; silnik tylko sprawdza obecność
ARM_FILE = "ARM_LIVE"  # uzbrojenie live: tworzy WYŁĄCZNIE właściciel; silnik tylko sprawdza obecność
FLATTEN_REASON = "FORCED"
MARKET_CLOSED_RETRY_S = 60  # wyjście przy zamkniętym rynku: kolejna próba najwcześniej po minucie (nie co takt)

# Przyczyny braku wejścia w `DayReport.skip_reason` (obok przyczyn strategii DOJI, STOP_TOO_TIGHT, SKIPPED_MIN_SIZE).
NOT_SESSION = "NOT_SESSION"
NOT_READY = "NOT_READY"  # logowanie, uzgadnianie startowe albo kontrola rachunku nie zdążyły przed terminem
GATE_BLOCKED = "GATE_BLOCKED"  # sygnał zatrzymany przez bramkę ryzyka (powody w zdarzeniu gate_block)
DRY_RUN_DECISION = "DRY_RUN"  # sygnał przeszedł bramkę, ale dry_run nie wysyła zleceń (zdarzenie decision)
ENTRY_NOT_SENT = "ENTRY_NOT_SENT"  # wykonawca nie wysłał (konto niedostępne, awaria dziennika, wyjątek)
SHUTDOWN = "SHUTDOWN"

# Przyczyny kill switcha nadawane przez silnik.
JOURNAL_UNREADABLE = "JOURNAL_UNREADABLE"

SOURCE_DECISION = "decision"
SOURCE_ENGINE = "engine"

# Powody czekania na decyzję, które przed terminem oznaczają NOT_READY (pozostałe: NO_SIGNAL_DATA).
_NOT_READY_WHY = frozenset({"NOT_LOGGED_IN", "STARTUP_RECONCILE_PENDING", "ACCOUNT_UNVERIFIED"})
# Metody brokera, których odrzucenia (BrokerRejected) liczy Watchdog (trzy z rzędu → kill miękki).
_ORDER_METHODS = frozenset({"open_position", "confirm", "close_position", "set_stop"})


def arm_file_exists(data_dir: Path) -> bool:
    """Czy właściciel utworzył plik uzbrojenia (tylko sprawdzenie; błąd odczytu = nie)."""
    try:
        return (Path(data_dir) / ARM_FILE).is_file()
    except OSError:
        return False


class EngineMisconfigured(RuntimeError):
    """Silnik złożony tak, że nie wolno mu startować (np. live bez wykonawcy jako księgującego)."""


@dataclass(frozen=True)
class DayReport:
    """Raport dnia. Pierwsze cztery pola to kontrakt planu; pozostałe to szczegóły dla logu i `status`.

    `entries`: zlecenia wejścia dnia w dzienniku (rola `entry`, każdy stan). `net_pnl`: suma netto transakcji
    zaksięgowanych z dniem wyjścia `day` (None: żadnej). `skip_reason`: dlaczego nie było wejścia (None: było albo
    już wcześniej tego dnia). `interrupted`: dzień przerwany prośbą o zatrzymanie.
    """

    entries: int
    final_position_state: PositionState
    net_pnl: Decimal | None
    skip_reason: str | None
    day: date | None = None
    mode: str = ""
    interrupted: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Times:
    """Godziny dnia sesji w UTC (z kalendarza i konfiguracji strategii)."""

    preopen: datetime
    open: datetime
    range_end: datetime
    deadline: datetime
    flatten: datetime
    close: datetime


@dataclass
class _DayState:
    day: date | None
    times: _Times | None  # None: spoczynek lub dzień bez sesji (każda pozycja OPEN do spłaszczenia)
    started: datetime
    login_gen: int
    idle: bool = False
    account_checked: bool = False
    reconciled: bool = False
    rules: InstrumentRules | None = None
    atr: Decimal | None = None
    decided: bool = False
    skip_reason: str | None = None
    why_waiting: str | None = None
    flatten_kicked: bool = False
    next_quote: datetime | None = None
    next_manage: datetime | None = None
    next_ping: datetime | None = None
    next_data: datetime | None = None
    candles_seen: list[Candle] = field(default_factory=list)  # ostatnia odpowiedź na zapytanie o pierwszą świecę
    candles_followup_at: datetime | None = None  # zdarzenie DECISION_CANDLES (sąsiednie świece, kontrola L0)
    notes: list[str] = field(default_factory=list)


class _SessionGuard:
    """Wywołania brokera z ponownym logowaniem po `SessionExpired` (szeregowanym, z ochroną przed blokadą konta)."""

    def __init__(self, inner: Broker, watchdog: Watchdog, alerts: RateLimitedNotifier, clock: Clock) -> None:
        self._inner = inner
        self._watchdog = watchdog
        self._alerts = alerts
        self._clock = clock
        self._lock = asyncio.Lock()
        self.gen = 0  # liczba udanych logowań w tym procesie
        self.market_closed_at: datetime | None = None  # ostatnie zamknięcie odrzucone przez zamknięty rynek

    async def login(self, cause: str = "start") -> bool:
        """Jawne logowanie (start dnia); True: sesja jest. Nie rzuca (porażkę liczy Watchdog)."""
        async with self._lock:
            return await self._login_locked(cause)

    async def call(self, name: str, fn: Callable[..., Awaitable[T]], *args: Any, retry: bool = True) -> T:
        gen = self.gen
        try:
            try:
                return self._ok(name, await fn(*args))
            except SessionExpired as exc:
                if not await self._relogin(gen, f"{name}: {exc}") or not retry:
                    raise
            return self._ok(name, await fn(*args))  # JEDNO ponowienie; kolejne SessionExpired idzie dalej
        except BrokerRejected as exc:
            if name in _ORDER_METHODS:
                self._watchdog.note_rejection(f"{name}: {exc.reason}")
            raise

    def _ok(self, name: str, result: T) -> T:
        if name in _ORDER_METHODS and (name != "confirm" or result is not None):
            self._watchdog.note_success()
        return result

    async def _relogin(self, gen: int, cause: str) -> bool:
        async with self._lock:
            if self.gen != gen:
                return True  # ktoś zalogował się po starcie tego wywołania: wystarczy ponowić
            return await self._login_locked(cause)

    async def _login_locked(self, cause: str) -> bool:
        if not self._watchdog.may_login():
            self._alerts.alert_keyed(
                "SESSION_NO_RELOGIN",
                "ERROR",
                f"SESSION_EXPIRED ({cause}); no login attempt now (lock-out protection or pause after a failed login)",
            )
            return False
        try:
            await self._inner.login()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # tylko odrzucenie danych logowania liczy się do blokady prób
            credential = isinstance(exc, BrokerAuthError)
            self._watchdog.note_login_failure(f"{type(exc).__name__}: {exc} ({cause})", credential=credential)
            return False
        self._watchdog.note_login_success()
        self.gen += 1
        return True


class GuardedBroker:
    """`Broker` dla menedżera, wykonawcy i uzgadniania: każde wywołanie przez `_SessionGuard` silnika."""

    def __init__(
        self,
        inner: Broker,
        guard: _SessionGuard,
        entry_block: Callable[[], str | None],
        *,
        on_account: Callable[[AccountInfo], None] | None = None,
        on_quote: Callable[[Quote], None] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._entry_block = entry_block
        self._on_account = on_account
        self._on_quote = on_quote
        self._clock = clock

    @property
    def inner(self) -> Broker:
        return self._inner

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    async def login(self) -> None:
        if not await self._guard.login("explicit"):
            raise SessionExpired("login failed or not allowed now")

    async def keepalive(self) -> None:
        return await self._guard.call("keepalive", self._inner.keepalive)

    async def instrument_rules(self, epic: str) -> InstrumentRules:
        return await self._guard.call("instrument_rules", self._inner.instrument_rules, epic)

    async def quote(self, epic: str) -> Quote:
        q = await self._guard.call("quote", self._inner.quote, epic)
        if self._on_quote is not None:  # każde notowanie (także wykonawcy w trakcie wejścia) karmi straż ciszy
            self._on_quote(q)
        return q

    async def daily_candles(self, epic: str, count: int) -> list[Candle]:
        return await self._guard.call("daily_candles", self._inner.daily_candles, epic, count)

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]:
        return await self._guard.call("candles_5m", self._inner.candles_5m, epic, start, end)

    async def open_position(self, req: EntryRequest) -> EntryAck:
        blocked = self._entry_block()  # druga linia obrony: dry_run albo rachunek niesprawdzony w tej sesji
        if blocked is not None:
            raise BrokerRejected(blocked)
        return await self._guard.call("open_position", self._inner.open_position, req, retry=False)

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        return await self._guard.call("confirm", self._inner.confirm, deal_reference)

    async def list_positions(self) -> list[OpenPosition]:
        return await self._guard.call("list_positions", self._inner.list_positions)

    async def list_working_orders(self) -> list[WorkingOrder]:
        return await self._guard.call("list_working_orders", self._inner.list_working_orders)

    async def close_position(self, deal_id: str) -> None:
        try:
            await self._guard.call("close_position", self._inner.close_position, deal_id)
        except MarketClosed:
            if self._clock is not None:
                self._guard.market_closed_at = self._clock.now_utc()
            raise
        self._guard.market_closed_at = None

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        return await self._guard.call("set_stop", self._inner.set_stop, deal_id, stop_level)

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        return await self._guard.call("closed_since", self._inner.closed_since, since)

    async def account(self) -> AccountInfo:
        """Aktywny rachunek sesji; w live porównany z rachunkiem związanym (wykonawca czyta go tuż przed zleceniem)."""
        info = await self._guard.call("account", self._inner.account)
        if self._on_account is not None:
            self._on_account(info)  # niezgodność: zejście do dry_run i BrokerError (żadnego zlecenia)
        return info

    async def list_accounts(self) -> list[AccountInfo]:
        return await self._guard.call("list_accounts", self._inner.list_accounts)

    async def select_account(self, account_id: str) -> None:
        return await self._guard.call("select_account", self._inner.select_account, account_id)


class Engine:
    """Korzeń kompozycji i pętla dnia (patrz docstring modułu)."""

    def __init__(
        self,
        broker: Broker,
        journal: Journal,
        clock: Clock,
        calendar: ExchangeCalendar,
        cfgs: tuple[StrategyConfig, RiskConfig, CostConfig],
        data_dir: Path,
        runtime: RuntimeConfig,
        mode: Mode = Mode.DRY_RUN,
        *,
        notifier: Notifier | None = None,
        reconciler: Reconciler | None = None,
        quote_poll_s: float = QUOTE_POLL_S,
        lock: InstanceLock | None = None,
        config_dir: Path | None = None,
    ) -> None:
        strat, risk, costs = cfgs
        if not quote_poll_s > 0:
            raise ValueError(f"quote_poll_s must be positive, got {quote_poll_s!r}")
        self.journal = journal
        self.clock = clock
        self.calendar = calendar
        self.strat, self.risk, self.costs = strat, risk, costs
        self.data_dir = Path(data_dir)
        self.runtime = runtime
        self._config_dir = Path(config_dir) if config_dir is not None else None
        self._mode = Mode(mode)
        self._epic = strat.instrument
        self._quote_poll_s = quote_poll_s
        base = notifier if notifier is not None else FileNotifier(self.data_dir / ALERTS_FILE, clock)
        self.alerts = RateLimitedNotifier(base, clock)
        self.kill = KillSwitch(journal, clock, self.data_dir, base)  # zmiany poziomu: bez tłumienia
        self.raw_broker = broker
        self.watchdog = Watchdog(
            clock, journal, self.kill, broker, risk, self.data_dir / HEARTBEAT_FILE, notifier=self.alerts
        )
        self._guard = _SessionGuard(broker, self.watchdog, self.alerts, clock)
        self.broker = GuardedBroker(
            broker, self._guard, self._entry_block, on_account=self._check_session_account,
            on_quote=lambda q: self.watchdog.note_quote(q.ts), clock=clock,
        )  # fmt: skip
        self.manager = OrderManager(journal, self.broker, clock, risk)
        self.gate = RiskGate(risk, strat, journal, calendar, clock, kill=self.kill)
        self._cost_model = CostModel(costs)
        self.executor = self._build_executor()
        self._own_reconciler = reconciler is None
        self.reconciler = reconciler if reconciler is not None else self._build_reconciler()
        self._lock = lock if lock is not None else InstanceLock(self.data_dir / LOCK_FILE)
        self._shutdown = False
        self._stop_logged = False
        self._verified_gen = -1  # pokolenie sesji, dla którego sprawdzono rachunek (live)
        self._day_states: dict[date, _DayState] = {}  # stan dnia przeżywa ponowienie dnia w run_forever
        self.last_quote: Quote | None = None
        self.watchdog.set_status(mode=self._mode.value, state="CREATED")

    # --- stan publiczny ---

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown

    def request_shutdown(self) -> None:
        """Czyste zatrzymanie (np. SIGTERM): koniec wywołań brokera przy najbliższej granicy kroku."""
        self._shutdown = True

    def check_wiring(self) -> None:
        """Odmowa startu silnika złożonego niebezpiecznie (live)."""
        if self._mode is not Mode.LIVE:
            return
        if self._config_dir is None:
            raise EngineMisconfigured("live engine without config_dir: live_allowed could not be re-checked")
        if not self.executor.live:
            raise EngineMisconfigured("live engine with an executor that does not send orders")
        if getattr(self.reconciler, "booker", None) is not self.executor:
            raise EngineMisconfigured(
                "live engine without the Executor as the reconciler's booker: a stop-out would go unbooked"
            )

    # --- budowa części ---

    def _build_executor(self, memory: ExitMemory | None = None) -> Executor:
        return Executor(
            self.manager, self.broker, self.journal, self.gate, self.kill, self._cost_model, self.clock, self.strat,
            self.risk, self.calendar, self.alerts, live=self._mode is Mode.LIVE, memory=memory,
        )  # fmt: skip

    def _build_reconciler(self) -> Reconciler:
        return Reconciler(
            self.journal, self.broker, self.clock, self.kill, self.risk, self._epic, booker=self.executor,
            entry_gate=lambda: self.manager.in_flight, notifier=self.alerts,
        )  # fmt: skip

    def _downgrade(self, reason: str) -> None:
        """live → dry_run do końca procesu (nigdy z powrotem); alarm i zdarzenie. Nowy wykonawca dostaje pamięć wyjść
        poprzedniego (wysłane zamknięcia tego procesu: nigdy druga wysyłka na ślepo)."""
        if self._mode is Mode.DRY_RUN:
            return
        self._mode = Mode.DRY_RUN
        self.executor = self._build_executor(self.executor.memory)
        if self._own_reconciler:
            self.reconciler = self._build_reconciler()
        self.watchdog.set_status(mode=self._mode.value)
        _LOG.error("MODE_DOWNGRADED live -> dry_run for the rest of this run: %s", reason)
        self._alert("MODE_DOWNGRADED", "ERROR", f"MODE_DOWNGRADED live -> dry_run for the rest of this run: {reason}")
        self._event("MODE_DOWNGRADED", reason=reason)

    def _armed_problem(self) -> str | None:
        """Czy uzbrojenie nadal trwa: plik właściciela, `live_allowed` i ten sam związany rachunek (z plików)."""
        if not arm_file_exists(self.data_dir):
            return f"{ARM_FILE} removed by the owner"
        if self._config_dir is None:
            return "no config_dir to re-check live_allowed"
        try:
            runtime = load_runtime(self._config_dir, self.data_dir)
        except Exception as exc:  # nieczytelna konfiguracja: nie zgadujemy, rozbrajamy
            return f"runtime configuration unreadable ({type(exc).__name__})"
        if runtime.live_allowed is not True:
            return "live_allowed is no longer true in config/runtime.toml"
        if runtime.account_id != self.runtime.account_id:
            return "the bound account in data/account.json changed"
        return None

    def _check_armed(self) -> None:
        """Na początku każdego dnia i tuż przed wejściem: rozbrojenie przez właściciela → dry_run (na stałe)."""
        if self._mode is not Mode.LIVE:
            return
        problem = self._armed_problem()
        if problem is not None:
            self._downgrade(f"{problem}; re-arming requires a restart of the engine")

    def _entry_block(self) -> str | None:
        """Powód odmowy wysłania wejścia (None: wolno): dry_run albo rachunek niesprawdzony w bieżącej sesji."""
        if self._mode is not Mode.LIVE:
            return "DRY_RUN_NO_ORDERS"
        if self._verified_gen != self._guard.gen:
            return "ACCOUNT_UNVERIFIED"
        return None

    def _check_session_account(self, info: AccountInfo) -> None:
        """Ostatnia kontrola przed zleceniem: aktywny rachunek sesji musi być związanym (live)."""
        bound = self.runtime.account_id
        if self._mode is not Mode.LIVE or bound is None or info.account_id == bound:
            return
        self._downgrade("ACCOUNT_MISMATCH: the session's active account is not the bound one")
        raise BrokerError("ACCOUNT_MISMATCH: the session's active account is not the bound one; no order sent")

    # --- wywołania brokera ---

    async def _call(self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Wywołanie brokera z ponownym logowaniem po wygasłej sesji (jedno ponowienie)."""
        return await self._guard.call(getattr(fn, "__name__", "call"), fn, *args)

    async def login(self) -> bool:
        return await self._guard.login("start")

    async def probe(self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Wywołanie brokera z ponownym logowaniem, ale bez liczenia odrzuceń (sonda `smoke`)."""
        return await self._guard.call("probe", fn, *args)

    def trading_day(self) -> date:
        """Lokalna data handlowa teraz (kalendarz giełdy)."""
        return self._today()

    def session_index(self) -> int:
        """Zakończone sesje live dla polityki wielkości: transakcje w księdze bez prób `smoke` (L0)."""
        return len(self.journal.trades()) - self.journal.count_smoke_trades()

    async def verify_account(self) -> str | None:
        """Kontrola rachunku związanego (spec §9): None = w porządku, inaczej powód. Błąd odczytu → wyjątek."""
        bound = self.runtime.account_id
        if bound is None:
            return "NO_BOUND_ACCOUNT: run bind-account"
        accounts = await self.broker.list_accounts()
        match = [a for a in accounts if a.account_id == bound]
        if len(match) != 1:
            return "BOUND_ACCOUNT_NOT_FOUND among the broker accounts"
        acc = match[0]
        if acc.status != ACCOUNT_STATUS:
            return f"ACCOUNT_NOT_ENABLED (status {acc.status})"
        if acc.currency != ACCOUNT_CURRENCY:
            return f"ACCOUNT_NOT_EUR (currency {acc.currency})"
        if not acc.equity > 0:
            return "ACCOUNT_ZERO_BALANCE"
        active = await self._call(self.raw_broker.account)  # bez haka niezgodności: tu liczy się powód
        if active.account_id != bound:
            return "WRONG_ACTIVE_ACCOUNT: the session is on another account than the bound one"
        self._verified_gen = self._guard.gen  # odczyt rachunku należy do sesji bieżącego pokolenia
        return None

    async def reconcile_once(self) -> ReconcileResult | None:
        """Jeden przebieg uzgadniania z obsługą błędów (Watchdog, kill switch); None przy wyjątku.

        Po udanym przebiegu: stop dla naszych pozycji bez stopu, potem straże wypełnienia przyjętego przez uzgadnianie
        (`Executor.guard_adopted_fill`; najpierw stop, potem straże). Po przebiegu pominiętym i po prośbie o
        zatrzymanie żadnego z tych kroków."""
        try:
            result = await self.reconciler.run_once()
        except asyncio.CancelledError:
            raise
        except JournalWriteError as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert("RECONCILE_JOURNAL", "ERROR", f"{JOURNAL_WRITE_FAILED} in reconciliation: {exc}; managing on")
            return None
        except Exception as exc:
            self.watchdog.note_reconcile_error(type(exc).__name__, str(exc))
            return None
        if result.skipped:  # pominięty: blocked/unprotected nic nie znaczą (zamknięcie mogło być zaksięgowane)
            self.watchdog.note_reconcile_skipped()
            return result
        self.watchdog.note_reconcile_ok()
        for p in result.unprotected:
            if self._shutdown:
                break
            await self._safe("protect_unprotected", self.executor.protect_unprotected, p)
        if not self._shutdown:
            await self._safe("guard_adopted_fill", self.executor.guard_adopted_fill)
        return result

    # --- dzień ---

    async def run_day(self, day: date) -> DayReport:
        """Jeden dzień giełdy (patrz docstring modułu). Wraca po uzgadnianiu końcowym albo po prośbie o zatrzymanie."""
        self.check_wiring()
        self._check_armed()
        try:
            return await self._run_day(day)
        except asyncio.CancelledError:
            self._log_stop("cancelled")
            raise

    async def _run_day(self, day: date) -> DayReport:
        if self._shutdown:
            return self._report(day, SHUTDOWN, interrupted=True)
        self._enforce_limits(day)
        times = self._session_times(day)
        if times is None:
            await self._off_day(day)
            return self._report(day, NOT_SESSION, interrupted=self._shutdown)
        if self._now() < times.preopen:
            await self._idle_until(times.preopen)
            if self._shutdown:
                return self._report(day, SHUTDOWN, interrupted=True)
        st = self._day_states.get(day)
        if st is None or st.times != times:  # ponowienie dnia (wyjątek w run_forever) kontynuuje ten sam stan
            st = _DayState(day, times, self._now(), self._guard.gen)
            self._day_states = {day: st}
        self.watchdog.set_session_window(times.open, times.close)
        try:
            await self._loop(st, times.close)
            if not self._shutdown:
                self.watchdog.set_status(state="FINAL_RECONCILE")
                await self._manage(st, self._now())  # uzgadnianie końcowe (i ponowienie spłaszczenia)
        finally:
            self.watchdog.set_session_window(None, None)
        if self._shutdown:
            self._log_stop("shutdown requested")
        skip = st.skip_reason if st.decided else (SHUTDOWN if self._shutdown else NOT_READY)
        return self._report(day, skip, interrupted=self._shutdown, notes=st.notes)

    async def run_forever(self) -> None:
        """Dzień po dniu do zatrzymania; blokada instancji przez całe życie; zwykły wyjątek dnia nie zabija silnika."""
        self.check_wiring()
        with contextlib.nullcontext() if self._lock.held else self._lock:  # main bierze blokadę przed wszystkim
            _LOG.info("engine started: mode %s, instrument %s", self._mode.value, self._epic)
            try:
                while not self._shutdown:
                    day = self._today()
                    try:
                        report = await self.run_day(day)
                    except EngineMisconfigured:
                        raise
                    except Exception as exc:
                        _LOG.exception("day %s failed", day)
                        self._alert("DAY_FAILED", "ERROR", f"DAY_FAILED {day}: {type(exc).__name__}: {exc}; continuing")
                        await self._pause(ERROR_PAUSE_S)
                        continue
                    _LOG.info("day %s: %s", day, report)
                    if self._shutdown:
                        break
                    await self._idle_until(self._next_day_start(day))
            except asyncio.CancelledError:
                self._log_stop("cancelled")
                raise
            self._log_stop("shutdown requested")

    # --- pętla ---

    async def _loop(self, st: _DayState, end: datetime, *, until_settled: bool = False) -> None:
        first = True
        while not self._shutdown:
            now = self._now()
            if not first and now >= end:
                return
            first = False
            await self._tick(st)
            if self._shutdown:
                return
            if until_settled and not self._needs_management():
                return
            await self._sleep_until(self._next_wake(st, end))

    async def _tick(self, st: _DayState) -> None:
        now = self._now()
        self.watchdog.set_status(state=self._phase(st, now))
        self._safe_sync("kill.scan_files", self.kill.scan_files)
        if st.idle and not self._needs_management():
            await self.watchdog.tick()
            return
        steps: list[Callable[[], Awaitable[None]]] = [
            lambda: self._ensure_login(st),
            lambda: self._ensure_account(st),
            lambda: self._quote_step(st),
            lambda: self._ping_step(st),
            lambda: self._manage_step(st),
            lambda: self._signal_data_step(st),
            lambda: self._decision_step(st),
            lambda: self._candles_followup_step(st),
        ]
        for step in steps:
            if self._shutdown:
                return
            await step()
        await self.watchdog.tick()

    def _next_wake(self, st: _DayState, end: datetime) -> datetime:
        now = self._now()
        candidates = [end, now + timedelta(seconds=SLEEP_CHUNK_S)]
        if not (st.idle and not self._needs_management()):
            candidates += [t for t in (st.next_quote, st.next_manage, st.next_ping, st.next_data) if t is not None]
        t = st.times
        if t is not None:
            if not st.decided:
                candidates += [t.range_end, t.deadline]
            if not st.flatten_kicked:
                candidates.append(t.flatten)
        future = [c for c in candidates if c > now]
        return min(future) if future else now + timedelta(seconds=MIN_SLEEP_S)

    async def _sleep_until(self, when: datetime) -> None:
        delay = (when - self._now()).total_seconds()
        await self.clock.sleep(min(max(delay, MIN_SLEEP_S), SLEEP_CHUNK_S))

    async def _pause(self, seconds: float) -> None:
        until = self._now() + timedelta(seconds=seconds)
        while not self._shutdown and self._now() < until:
            await self.watchdog.tick()
            await self._sleep_until(until)

    async def _idle_until(self, until: datetime) -> None:
        """Spoczynek do `until`: tętno i pliki kill switcha; pozostała pozycja jest zarządzana (spłaszczana)."""
        self.watchdog.set_session_window(None, None)
        st = _DayState(None, None, self._now(), self._guard.gen, idle=True)
        if self._now() < until:
            await self._loop(st, until)

    async def _off_day(self, day: date) -> None:
        """Dzień bez sesji: tylko zarządzanie pozostałą pozycją lub zleceniem, do FLAT albo do północy."""
        self.watchdog.set_session_window(None, None)
        if not self._needs_management():
            self.watchdog.set_status(state="NOT_SESSION")
            await self.watchdog.tick()
            return
        st = _DayState(day, None, self._now(), self._guard.gen)
        await self._loop(st, self._next_day_start(day), until_settled=True)

    # --- kroki taktu ---

    async def _ensure_login(self, st: _DayState) -> None:
        if self._guard.gen == st.login_gen:  # w tym dniu jeszcze nie było udanego logowania
            await self._guard.login("start of day" if st.day is not None else "idle management")

    async def _ensure_account(self, st: _DayState) -> None:
        if st.account_checked or self._guard.gen == st.login_gen:
            return
        if self._mode is Mode.LIVE:
            await self._check_account(st)
            return
        st.account_checked = True  # dry_run: kontrola tylko do wiadomości (lista kontrolna przed uzbrojeniem)
        try:
            problem = await self.verify_account()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.info("dry_run account check unavailable: %s", type(exc).__name__)
            return
        _LOG.info("dry_run account check: %s", problem or "bound account OK (would allow live)")

    async def _check_account(self, st: _DayState) -> bool:
        """Kontrola rachunku (live); porażka rozstrzygająca → dry_run do końca procesu; błąd odczytu → ponowienie."""
        try:
            problem = await self.verify_account()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"ACCOUNT_CHECK_UNAVAILABLE: {type(exc).__name__}: {exc}; no entry yet"
            self._alert("ACCOUNT_CHECK", "WARN", message)
            return False
        if problem is not None:
            self._downgrade(problem)
            return False
        st.account_checked = True
        self._verified_gen = self._guard.gen
        return True

    async def _quote_step(self, st: _DayState) -> None:
        now = self._now()
        if st.idle or (st.next_quote is not None and now < st.next_quote):
            return
        st.next_quote = _next(st.next_quote, now, self._quote_poll_s)
        try:
            q = await self.broker.quote(self._epic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # cisza notowań wyłapie Watchdog; tu tylko ślad w logu
            _LOG.debug("quote failed: %s: %s", type(exc).__name__, exc)
            return
        self.last_quote = q
        self.watchdog.note_quote(q.ts)

    async def _ping_step(self, st: _DayState) -> None:
        now = self._now()
        if st.next_ping is None:
            st.next_ping = now + timedelta(seconds=self.risk.ping_interval_s)
            return
        if now < st.next_ping:
            return
        st.next_ping = _next(st.next_ping, now, self.risk.ping_interval_s)
        try:
            await self.broker.keepalive()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._alert("PING_FAILED", "WARN", f"PING_FAILED: {type(exc).__name__}: {exc}")

    async def _manage_step(self, st: _DayState) -> None:
        now = self._now()
        due = st.next_manage is None or now >= st.next_manage
        kick = st.times is not None and now >= st.times.flatten and not st.flatten_kicked
        if not (due or kick):
            return
        if kick:
            st.flatten_kicked = True
        st.next_manage = _next(st.next_manage, now, self.risk.reconcile_interval_s)
        await self._manage(st, now)

    async def _manage(self, st: _DayState, now: datetime) -> None:
        """Uzgadnianie, ochrona i wyjścia: HARD → awaryjne spłaszczenie; EXITING → ponowienie; OPEN po godzinie
        spłaszczenia (albo poza sesją) → spłaszczenie FORCED. Każdy błąd: alarm i dalej (zarządzanie trwa)."""
        result = await self.reconcile_once()
        if result is not None and not result.skipped:
            st.reconciled = True
        if self._shutdown:
            return
        backed_off = self._market_closed_backoff()
        if self.kill.level() is KillLevel.HARD:
            if not backed_off:
                await self._safe("emergency_flatten", self.executor.emergency_flatten)
            return
        position = self._position()
        if position is None or backed_off:  # zamknięty rynek: stop u brokera chroni, próba co minutę
            return
        if position.state is _P.EXITING or (position.state is _P.OPEN and self._must_flatten(st, position, now)):
            await self._safe("flatten", self.executor.flatten, position, FLATTEN_REASON)

    def _market_closed_backoff(self) -> bool:
        closed_at = self._guard.market_closed_at
        return closed_at is not None and (self._now() - closed_at).total_seconds() < MARKET_CLOSED_RETRY_S

    async def _signal_data_step(self, st: _DayState) -> None:
        t, now = st.times, self._now()
        if t is None or st.decided or now > t.deadline or (st.rules is not None and st.atr is not None):
            return
        if st.next_data is not None and now < st.next_data:
            return
        st.next_data = now + timedelta(seconds=self.risk.reconcile_interval_s)
        await self._load_signal_data(st)

    async def _load_signal_data(self, st: _DayState) -> None:
        try:
            rules = await self.broker.instrument_rules(self._epic)
            daily = await self.broker.daily_candles(self._epic, ATR_PERIOD + 1)
            atr = atr14(daily)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # w tym błąd danych z atr14: brak wejścia, dopóki dane się nie wczytają
            self._alert("SIGNAL_DATA", "WARN", f"SIGNAL_DATA_UNAVAILABLE: {type(exc).__name__}: {exc}; retrying")
            return
        st.rules, st.atr = rules, atr
        st.next_data = None

    async def _decision_step(self, st: _DayState) -> None:
        t, now = st.times, self._now()
        if t is None or st.decided or now < t.range_end:
            return
        if self._entries_today(st.day) > 0:
            st.decided, st.skip_reason = True, None
            st.notes.append("ENTRY_ALREADY_MADE")
            return
        if now > t.deadline:
            why = "STARTED_AFTER_DEADLINE" if st.started > t.deadline else (st.why_waiting or "DEADLINE_PASSED")
            reason = NOT_READY if why in _NOT_READY_WHY else NO_SIGNAL_DATA
            neighbours = [] if why == "STARTED_AFTER_DEADLINE" else await self._neighbour_candles(t)
            self._record_decision(st, reason=reason, why=why, neighbours=neighbours)
            st.decided, st.skip_reason = True, reason
            return
        if self._guard.gen == st.login_gen:
            st.why_waiting = "NOT_LOGGED_IN"
            return
        if not st.reconciled:
            st.why_waiting = "STARTUP_RECONCILE_PENDING"
            return
        if self._mode is Mode.LIVE and (not st.account_checked or self._verified_gen != self._guard.gen):
            await self._check_account(st)  # po ponownym logowaniu: rachunek sprawdzany jeszcze raz przed wejściem
            if self._mode is Mode.LIVE and self._verified_gen != self._guard.gen:
                st.why_waiting = "ACCOUNT_UNVERIFIED"  # błąd odczytu: bez wejścia, dopóki nie sprawdzimy
                return
        if st.rules is None or st.atr is None:
            await self._load_signal_data(st)
            if st.rules is None or st.atr is None:
                st.why_waiting = "SIGNAL_DATA_UNAVAILABLE"
                return
        await self._decide(st, t)

    async def _decide(self, st: _DayState, t: _Times) -> None:
        try:
            candles = await self.broker.candles_5m(self._epic, t.open, t.range_end)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            st.why_waiting = "CANDLES_UNAVAILABLE"
            message = f"CANDLES_UNAVAILABLE: {type(exc).__name__}: {exc}; retrying to the deadline"
            self._alert("CANDLES", "WARN", message)
            return
        st.candles_seen = list(candles)
        first = next((c for c in candles if c.start == t.open), None)  # wyłącznie równość ze startem sesji (UTC)
        if first is None:
            st.why_waiting = "FIRST_CANDLE_MISSING"
            return
        try:
            quote = await self.broker.quote(self._epic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            st.why_waiting = "QUOTE_UNAVAILABLE"
            self._alert("DECISION_QUOTE", "WARN", f"DECISION_QUOTE_UNAVAILABLE: {type(exc).__name__}: {exc}")
            return
        self.last_quote = quote
        self.watchdog.note_quote(quote.ts)
        try:
            refreshed = await self.broker.instrument_rules(self._epic)  # minimalny stop zależy od bieżącej ceny
        except asyncio.CancelledError:
            raise
        except Exception:
            refreshed = None
        rules = refreshed or st.rules
        st.rules = rules
        neighbours = await self._neighbour_candles(t)
        if self._shutdown:  # prośba o zatrzymanie w trakcie odczytów: bez decyzji i bez wejścia
            return
        try:
            session_index = self.session_index()
            rule_size_allowed = (self.data_dir / L1_PASSED_FILE).exists()
            outcome = decide(
                first, st.atr, quote, rules, self.strat, self.risk, session_index=session_index,
                rule_size_allowed=rule_size_allowed,
            )  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # błąd danych lub wywołującego: brak wejścia, alarm
            self._alert("DECIDE_FAILED", "ERROR", f"DECIDE_FAILED: {type(exc).__name__}: {exc}; no entry today")
            self._record_decision(
                st, first=first, quote=quote, reason=NO_SIGNAL_DATA, why="DECIDE_FAILED", neighbours=neighbours
            )
            st.decided, st.skip_reason = True, NO_SIGNAL_DATA
            return
        st.decided = True
        if isinstance(outcome, Skip):
            self._record_decision(
                st, first=first, quote=quote, reason=outcome.reason, session_index=session_index,
                rule_size_allowed=rule_size_allowed, neighbours=neighbours,
            )  # fmt: skip
            st.skip_reason = outcome.reason
            return
        self._record_decision(
            st, first=first, quote=quote, signal=outcome, session_index=session_index,
            rule_size_allowed=rule_size_allowed, neighbours=neighbours,
        )  # fmt: skip
        if self._shutdown:
            st.skip_reason = SHUTDOWN
            return
        await self._enter(st, outcome, rules)

    async def _enter(self, st: _DayState, signal: EntrySignal, rules: InstrumentRules) -> None:
        self._check_armed()  # właściciel mógł rozbroić między 09:00 a 09:05: wtedy wejście tylko jako dry_run
        if self._mode is Mode.LIVE and self._verified_gen != self._guard.gen:
            await self._check_account(st)  # ponowne logowanie w trakcie decyzji: rachunek jeszcze raz przed wysyłką
            if self._mode is Mode.LIVE and self._verified_gen != self._guard.gen:
                st.skip_reason = "ACCOUNT_UNVERIFIED"
                st.notes.append("ENTRY_SKIPPED_ACCOUNT_UNVERIFIED")
                return
        try:
            blocks_before = len(self.journal.events("gate_block"))
        except JournalError:
            blocks_before = -1
        try:
            rec = await self.executor.enter(signal, rules, st.day)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # wykonawca już włączył kill switch miękki (ENTRY_EXCEPTION) i zaalarmował
            _LOG.error("entry failed: %s: %s", type(exc).__name__, exc)
            st.skip_reason = ENTRY_NOT_SENT
            return
        finally:
            self.watchdog.rearm_silence()  # w trakcie długiego wejścia pętla nie odczytywała notowań
        if rec is not None:
            st.skip_reason = None if rec.state is OrderState.CONFIRMED else f"ENTRY_{rec.state.value}"
            return
        try:
            blocked = blocks_before >= 0 and len(self.journal.events("gate_block")) > blocks_before
        except JournalError:
            blocked = False
        if blocked:
            st.skip_reason = GATE_BLOCKED
        elif not self.executor.live:
            st.skip_reason = DRY_RUN_DECISION
        else:
            st.skip_reason = ENTRY_NOT_SENT

    # --- zapisy, alarmy, odczyty ---

    def _record_decision(
        self,
        st: _DayState,
        *,
        first: Candle | None = None,
        quote: Quote | None = None,
        signal: EntrySignal | None = None,
        reason: str | None = None,
        why: str | None = None,
        session_index: int | None = None,
        rule_size_allowed: bool | None = None,
        neighbours: list[Candle] | None = None,
    ) -> None:
        """Zdarzenie `decision` (DECISION): zawsze, także dla braku danych; awaria zapisu → kill miękki.

        Dla kontroli L0 (konwencja stempla niepotwierdzona) zawiera WSZYSTKIE świece z zapytania o pierwszą świecę
        i świecę sprzed otwarcia, każdą ze startem UTC, etykietą czasu lokalnego i OHLC; świecę po pierwszej dopisuje
        zdarzenie DECISION_CANDLES, gdy się zakończy.
        """
        if session_index is None:
            session_index = self._safe_read(self.session_index, -1)
        if rule_size_allowed is None:
            rule_size_allowed = (self.data_dir / L1_PASSED_FILE).exists()
        payload = {
            "event": "DECISION",
            "day": st.day.isoformat() if st.day is not None else None,
            "mode": self._mode.value,
            "first_candle": first,
            "first_candle_start_utc": first.start if first is not None else None,
            "candles_returned": [self._candle_view(c) for c in st.candles_seen],
            "neighbours": [self._candle_view(c) for c in neighbours or ()],
            "atr": st.atr,
            "quote": quote,
            "session_index": session_index,
            "rule_size_allowed": rule_size_allowed,
            "outcome": "SIGNAL" if signal is not None else "SKIP",
            "skip_reason": reason,
            "why": why,
            "signal": (
                {"side": signal.side, "stop_distance": signal.stop_distance, "size": signal.size}
                if signal is not None else None
            ),
            "decided_at": self._now(),
        }  # fmt: skip
        try:
            self.journal.append_raw(SOURCE_DECISION, dump(payload), self._now())
        except Exception as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert("DECISION_JOURNAL", "ERROR", f"{JOURNAL_WRITE_FAILED}: decision not journaled: {exc}")
        if st.times is not None and why != "STARTED_AFTER_DEADLINE":
            span = timedelta(minutes=self.strat.range_minutes)
            st.candles_followup_at = st.times.open + 2 * span  # świeca po pierwszej jest wtedy zakończona
        _LOG.info(
            "decision %s: %s %s (first candle %s)", st.day, payload["outcome"], reason or "",
            first.start.isoformat() if first is not None else "missing",
        )  # fmt: skip

    def _candle_view(self, c: Candle) -> dict[str, Any]:
        local = self.calendar.to_local(c.start)
        return {
            "start": c.start, "local": f"{local:%H:%M} {local.tzname()}", "open": c.open, "high": c.high,
            "low": c.low, "close": c.close,
        }  # fmt: skip

    async def _neighbour_candles(self, t: _Times) -> list[Candle]:
        """Świeca przed otwarciem (kontekst kontroli L0); błąd odczytu nie przeszkadza decyzji."""
        span = timedelta(minutes=self.strat.range_minutes)
        try:
            return await self.broker.candles_5m(self._epic, t.open - span, t.open)
        except asyncio.CancelledError:
            raise
        except Exception:
            return []

    async def _candles_followup_step(self, st: _DayState) -> None:
        """DECISION_CANDLES: świeca przed, pierwsza i po pierwszej, po zakończeniu tej ostatniej (kontrola L0)."""
        t, due = st.times, st.candles_followup_at
        if t is None or due is None or self._now() < due:
            return
        st.candles_followup_at = None
        span = timedelta(minutes=self.strat.range_minutes)
        try:
            candles = await self.broker.candles_5m(self._epic, t.open - span, t.open + 2 * span)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.info("decision candle follow-up unavailable: %s", type(exc).__name__)
            return
        payload = {
            "event": "DECISION_CANDLES", "day": st.day.isoformat() if st.day else None,
            "first_candle_start_utc": t.open, "candles": [self._candle_view(c) for c in candles],
        }  # fmt: skip
        try:
            self.journal.append_raw(SOURCE_DECISION, dump(payload), self._now())
        except Exception as exc:
            _LOG.warning("DECISION_CANDLES not journaled: %s", exc)

    def _event(self, code: str, **details: Any) -> None:
        try:
            self.journal.append_raw(SOURCE_ENGINE, dump({"event": code, **details}), self._now())
        except Exception as exc:
            _LOG.warning("engine event %s not journaled: %s", code, exc)

    def _alert(self, key: str, level: str, message: str) -> None:
        self.alerts.alert_keyed(key, level, message)

    async def _safe(self, what: str, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Krok zarządzania: błąd dziennika → kill miękki; inny wyjątek → alarm; zarządzanie trwa dalej."""
        try:
            return await fn(*args)
        except asyncio.CancelledError:
            raise
        except JournalWriteError as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert(f"{what}:journal", "ERROR", f"{JOURNAL_WRITE_FAILED} during {what}: {exc}; managing on")
        except JournalError as exc:
            self.kill.soft(JOURNAL_UNREADABLE)
            self._alert(f"{what}:journal", "ERROR", f"{JOURNAL_UNREADABLE} during {what}: {exc}; managing on")
        except Exception as exc:
            self._alert(f"{what}:error", "ERROR", f"{what.upper()}_FAILED: {type(exc).__name__}: {exc}")
        return None

    def _safe_sync(self, what: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as exc:
            self._alert(f"{what}:error", "ERROR", f"{what} failed: {type(exc).__name__}: {exc}")

    def _safe_read(self, fn: Callable[[], T], default: T) -> T:
        try:
            return fn()
        except Exception:
            return default

    def _position(self) -> PositionRecord | None:
        try:
            return self.journal.current_position(self._epic)
        except JournalError as exc:
            self.kill.soft(JOURNAL_UNREADABLE)
            self._alert("POSITION_READ", "ERROR", f"{JOURNAL_UNREADABLE}: position unreadable ({exc}); retrying")
            return None

    def _needs_management(self) -> bool:
        """Pozycja inna niż FLAT albo nierozstrzygnięte zlecenie (nieczytelny dziennik: tak, ostrożnie)."""
        try:
            position = self.journal.current_position(self._epic)
            if position is not None and position.state is not _P.FLAT:
                return True
            return any(o.epic == self._epic for o in self.journal.unresolved_orders())
        except Exception:
            return True

    def _entries_today(self, day: date | None) -> int:
        if day is None:
            return 0
        return self._safe_read(lambda: self.journal.count_entry_orders(day), 1)  # nieczytelny: jak po wejściu

    def _enforce_limits(self, day: date) -> None:
        """Limity strat z liczników dziennika: dzień ostatniego księgowania i dzisiaj (idempotentne)."""
        days = {day}
        try:
            trades = self.journal.trades()
            if trades:
                days.add(self.calendar.to_local(max(t.exit_ts for t in trades)).date())
        except Exception as exc:
            self.kill.soft(KILL_CHECK_FAILED)
            self._alert("LIMITS", "ERROR", f"{KILL_CHECK_FAILED}: ledger unreadable at start ({type(exc).__name__})")
        for d in sorted(days):
            self.executor.enforce_loss_limits(d)

    def _session_times(self, day: date) -> _Times | None:
        cal, strat = self.calendar, self.strat
        try:
            if not cal.is_session_day(day):
                return None
            open_local = datetime.combine(day, cal.open_time(day))
            range_end = open_local + timedelta(minutes=strat.range_minutes)
            deadline = range_end + timedelta(minutes=strat.entry_deadline_after_range_min)
            flatten = datetime.combine(day, cal.flatten_time(day, strat.flatten_before_close_min))
            close = datetime.combine(day, cal.close_time(day))

            def utc(local: datetime) -> datetime:
                return cal.local_to_utc(local.date(), local.time())

            return _Times(
                preopen=utc(open_local - timedelta(minutes=PREOPEN_LEAD_MIN)), open=utc(open_local),
                range_end=utc(range_end), deadline=utc(deadline), flatten=utc(flatten), close=utc(close),
            )  # fmt: skip
        except Exception as exc:  # m.in. CalendarExpiredError: wygasły kalendarz = brak handlu
            self._alert("CALENDAR", "ERROR", f"CALENDAR_UNAVAILABLE for {day}: {type(exc).__name__}: {exc}; no trading")
            return None

    def _must_flatten(self, st: _DayState, position: PositionRecord, now: datetime) -> bool:
        """OPEN do spłaszczenia: po godzinie spłaszczenia, poza sesją albo pozycja z wcześniejszego dnia (strategia
        nigdy nie trzyma pozycji przez noc; nieznany czas otwarcia traktujemy jak pozostałość)."""
        if st.times is None or now >= st.times.flatten:
            return True
        if position.opened_at is None:
            return True
        return self.calendar.to_local(position.opened_at).date() < (st.day or self._today())

    def _phase(self, st: _DayState, now: datetime) -> str:
        t = st.times
        if t is None:
            return "IDLE" if st.idle else "NOT_SESSION"
        if now < t.open:
            return "PREOPEN"
        if now < t.range_end:
            return "OPENING_RANGE"
        if not st.decided:
            return "DECISION"
        if now < t.flatten:
            return "MANAGING"
        if now < t.close:
            return "FLATTEN"
        return "CLOSED"

    def _report(
        self, day: date, skip: str | None, *, interrupted: bool = False, notes: list[str] | None = None
    ) -> DayReport:
        try:
            position = self.journal.current_position(self._epic)
            state = _P.FLAT if position is None else position.state
        except Exception:
            state = _P.BLOCKED  # nie wiemy: raport nie może udawać FLAT
        entries = self._safe_read(lambda: self.journal.count_entry_orders(day), -1)
        net = self._safe_read(lambda: self._day_net(day), None)
        return DayReport(entries, state, net, skip, day, self._mode.value, interrupted, tuple(notes or ()))

    def _day_net(self, day: date) -> Decimal | None:
        nets = [t.net_pnl for t in self.journal.trades() if self.calendar.to_local(t.exit_ts).date() == day]
        return sum(nets, Decimal(0)) if nets else None

    def _log_stop(self, why: str) -> None:
        """Wpis do logu przy zatrzymaniu (raz): co zostaje u brokera (pozycja ze stopem) i że start to uzgodni."""
        self.watchdog.set_status(state="STOPPED")
        if self._stop_logged:
            return
        self._stop_logged = True
        try:
            position = self.journal.current_position(self._epic)
        except Exception as exc:
            _LOG.warning("engine stopped (%s); journal unreadable (%s): check the broker positions", why, exc)
            return
        if position is None or position.state is _P.FLAT:
            _LOG.info("engine stopped (%s); no open position", why)
            return
        _LOG.warning(
            "engine stopped (%s); position %s %s left at the broker, protected by its broker stop %s; "
            "the next start reconciles it before anything else", why, position.deal_id, position.state.value,
            position.stop_level,
        )  # fmt: skip

    def _now(self) -> datetime:
        return self.clock.now_utc()

    def _today(self) -> date:
        return self.calendar.to_local(self._now()).date()

    def _next_day_start(self, day: date) -> datetime:
        return self.calendar.local_to_utc(day + timedelta(days=1), time(0, 0))


def _next(previous: datetime | None, now: datetime, interval_s: float) -> datetime:
    """Stały rytm: poprzedni termin + interwał; po zaległości (termin w przeszłości) od teraz."""
    step = timedelta(seconds=interval_s)
    if previous is None:
        return now + step
    upcoming = previous + step
    return upcoming if upcoming > now else now + step
