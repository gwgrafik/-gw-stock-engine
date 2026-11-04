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

Kolejne pasma (zadanie 12, projekt wielu pasm §1.3, §2.1): WYŁĄCZNIE z jawnym `config/lanes.toml` (`enabled = true`;
brak pliku = dokładnie złożenie i zachowanie v1, pilnowane złotymi śladami DE40). Wtedy w tej samej korutynie, w stałej
kolejności, obok DE40 pracują pasma B1/M5 (`engine_lanes.BarLane`): dzień portfela trwa od najwcześniejszego
przygotowania do najpóźniejszego zamknięcia pasm (sesja NYSE do 22:00 czasu berlińskiego), pętla budzi się na najbliższe
zdarzenie wszystkich pasm; wspólne są sesja brokera i limit zapytań, kill switch, dziennik, pamięć wyjść i bramka
portfelowa (`PortfolioGate` ponad bramką pasma, kurs EURUSD brokera `fx.QuoteFxView`, widok pozycji bez pasma); per
pasmo są wykonawca, uzgadnianie (klucze per instrument), bramka (wejścia dnia per instrument), stan dnia, licznik sesji,
straż ciszy notowań, liczniki uzgadniania i zamknięty rynek. HARD zamyka WSZYSTKIE pasma próbami przeplatanymi; pozycja
na instrumencie bez pasma jest tylko zgłaszana (UNMANAGED_POSITION raz na deal) i blokuje wejścia
(PORTFOLIO_UNMANAGED_EXPOSURE), nigdy nie jest zamykana; wyjątek kroku jednego pasma (LANE_STEP_FAILED) nie zatrzymuje
pozostałych ani wyjść. Kolejne pasmo jest live tylko przy procesie live I spełnionym warunku uzbrojenia pasma
(`lane_arming`; domyślnie nigdy: pre-rejestracja pasm to zadanie 13), inaczej dry_run (także `GuardedBroker` po
`req.epic`). Wynik pasma w USD: faza 1 w walucie instrumentu, faza 2 z szacunku kursu brokera (ESTIMATED_FX).

Zatrzymanie (`request_shutdown`, sygnał): koniec wywołań brokera przy najbliższej granicy kroku taktu; otwarta pozycja
zostaje u brokera ze stopem, co trafia do logu. Anulowanie (`CancelledError`) przechodzi dalej po wpisie do logu.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from gwstock.broker.base import Broker, Capabilities
from gwstock.calendar import NYSE, XETRA, ExchangeCalendar
from gwstock.clock import Clock
from gwstock.config import (
    CostConfig,
    LoadedLane,
    PortfolioConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    load_lanes,
    load_portfolio,
    load_runtime,
)
from gwstock.costs import CostModel
from gwstock.engine_lanes import LANE_DECISION, BarLane, LaneArming, default_lane_arming
from gwstock.execution import KILL_CHECK_FAILED, Executor, ExitMemory, emergency_flatten_lanes
from gwstock.fx import ESTIMATED_FX, QuoteFxView
from gwstock.journal import Journal, JournalError, JournalWriteError, PositionRecord
from gwstock.lanes import STALE_SIGNAL, LaneSpec, decide_entry
from gwstock.notify import FileNotifier, Notifier
from gwstock.orders.manager import OrderManager
from gwstock.portfolio import PortfolioGate, PortfolioLane
from gwstock.reconcile import Reconciler, ReconcileResult
from gwstock.risk import JOURNAL_WRITE_FAILED, BarLaneRiskGate, KillSwitch, LaneRiskGate, RiskGate
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
# Skład bez pasm (brak `config/lanes.toml`) z nierozliczoną transakcją w walucie obcej (`pnl_pending`): liczniki strat
# (DAILY_LOSS, TOTAL_DRAWDOWN) nie widzą jej wyniku, a bramka v1 nie czyta znaczników, więc wejścia blokuje kill SOFT.
PNL_PENDING_WITHOUT_LANES = "PNL_PENDING_WITHOUT_LANES"

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
    primary_final: bool = False  # z kolejnymi pasmami: uzgadnianie DE40 po zamknięciu XETRA już było


class _SessionGuard:
    """Wywołania brokera z ponownym logowaniem po `SessionExpired` (szeregowanym, z ochroną przed blokadą konta)."""

    def __init__(self, inner: Broker, watchdog: Watchdog, alerts: RateLimitedNotifier, clock: Clock) -> None:
        self._inner = inner
        self._watchdog = watchdog
        self._alerts = alerts
        self._clock = clock
        self._lock = asyncio.Lock()
        self.gen = 0  # liczba udanych logowań w tym procesie
        # Ostatnie zamknięcie odrzucone przez zamknięty rynek, per instrument (A6): klucz None to pasmo DE40 v1
        # (`market_closed_at`), klucz `epic` kolejne pasmo; zamknięty rynek jednego pasma nie wstrzymuje innego.
        self.market_closed: dict[str | None, datetime] = {}

    @property
    def market_closed_at(self) -> datetime | None:
        return self.market_closed.get(None)

    @market_closed_at.setter
    def market_closed_at(self, when: datetime | None) -> None:
        if when is None:
            self.market_closed.pop(None, None)
        else:
            self.market_closed[None] = when

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
        lane_block: Callable[[str], str | None] | None = None,
        close_key: Callable[[str], str | None] | None = None,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._entry_block = entry_block
        self._on_account = on_account
        self._on_quote = on_quote
        self._clock = clock
        self._lane_block = lane_block  # silnik z pasmami: tryb pasma po `req.epic` (druga linia obrony R7)
        self._close_key = close_key  # silnik z pasmami: klucz zamkniętego rynku zamykanego deala (per instrument)

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
        if blocked is None and self._lane_block is not None:
            blocked = self._lane_block(req.epic)  # pasmo bez uzbrojenia: najwyżej dry_run (R7)
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
        key = None if self._close_key is None else self._close_key(deal_id)
        try:
            await self._guard.call("close_position", self._inner.close_position, deal_id)
        except MarketClosed:
            if self._clock is not None:
                self._guard.market_closed[key] = self._clock.now_utc()
            raise
        self._guard.market_closed.pop(key, None)

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        return await self._guard.call("set_stop", self._inner.set_stop, deal_id, stop_level)

    async def set_protection(self, deal_id: str, stop_level: Decimal, target_level: Decimal | None) -> None:
        # Zadanie 6: tylko przekazanie (jak `set_stop`), żeby nowa metoda protokołu nie omijała ponownego logowania;
        # DE40 v1 jej nie wywołuje. Użycie przez silnik pasm: zadania 9 i 12.
        return await self._guard.call("set_protection", self._inner.set_protection, deal_id, stop_level, target_level)

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
        lanes: Sequence[LoadedLane] | None = None,
        lane_arming: LaneArming | None = None,
        portfolio: PortfolioConfig | None = None,
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
        extra = self._load_extra_lanes(lanes, strat, risk)
        self.lanes: list[BarLane] = []  # kolejne pasma (stała kolejność z config/lanes.toml); puste: tylko DE40 v1
        self._lane_by_epic: dict[str, BarLane] = {}
        lanes_mode = bool(extra)
        self.broker = GuardedBroker(
            broker, self._guard, self._entry_block, on_account=self._check_session_account,
            on_quote=self._on_quote, clock=clock,
            lane_block=self._lane_block if lanes_mode else None, close_key=self._close_key if lanes_mode else None,
        )  # fmt: skip
        self.manager = OrderManager(journal, self.broker, clock, risk)
        # Bramka pasma DE40: v1 bez zmian; z kolejnymi pasmami wejścia dnia liczone per instrument (A3).
        gate_cls = LaneRiskGate if lanes_mode else RiskGate
        self.gate = gate_cls(risk, strat, journal, calendar, clock, kill=self.kill)
        self._cost_model = CostModel(costs)
        self._fx: QuoteFxView | None = None
        self._portfolio: PortfolioGate | None = None
        self._extra_epics: list[str] = []
        self._next_fx: datetime | None = None  # kolejny odczyt notowania pary walutowej (kurs, `QuoteFxView`)
        self._unmanaged_epics: frozenset[str] | None = None  # None: jeszcze żadnego udanego odczytu listy brokera
        self._unmanaged_seen: set[str] = set()
        self._lane_arming: LaneArming = lane_arming if lane_arming is not None else default_lane_arming
        self._pending_reported: frozenset[str] = frozenset()  # deale PNL_PENDING_WITHOUT_LANES już w zdarzeniu
        self._orphan_pending_seen: set[str] = set()  # deale PNL_PENDING_ORPHAN już zgłoszone w tym procesie
        if lanes_mode:
            self._setup_portfolio(extra, portfolio)
        self.executor = self._build_executor()
        self._own_reconciler = reconciler is None
        self.reconciler = reconciler if reconciler is not None else self._build_reconciler()
        for loaded in extra:
            self._add_lane(loaded)
        self._lock = lock if lock is not None else InstanceLock(self.data_dir / LOCK_FILE)
        self._shutdown = False
        self._stop_logged = False
        self._verified_gen = -1  # pokolenie sesji, dla którego sprawdzono rachunek (live)
        self._day_states: dict[date, _DayState] = {}  # stan dnia przeżywa ponowienie dnia w run_forever
        self.last_quote: Quote | None = None
        self.last_quotes: dict[str, Quote] = {}  # ostatnie notowanie per instrument (pasma; DE40 także `last_quote`)
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
        for lane in self.lanes:  # każde pasmo: księgujący uzgadniania to wykonawca tego pasma
            if getattr(lane.reconciler, "booker", None) is not lane.executor:
                raise EngineMisconfigured(f"lane {lane.epic}: the reconciler's booker is not the lane executor")

    # --- budowa części ---

    def _build_executor(self, memory: ExitMemory | None = None) -> Executor:
        if self._portfolio is None:  # jedno pasmo DE40: dokładnie złożenie v1
            return Executor(
                self.manager, self.broker, self.journal, self.gate, self.kill, self._cost_model, self.clock,
                self.strat, self.risk, self.calendar, self.alerts, live=self._mode is Mode.LIVE, memory=memory,
            )  # fmt: skip
        return Executor(
            self.manager, self.broker, self.journal, self.gate, self.kill, self._cost_model, self.clock, self.strat,
            self.risk, self.calendar, self.alerts, live=self._mode is Mode.LIVE, memory=memory,
            portfolio=self._portfolio,
        )  # fmt: skip

    def _build_reconciler(self) -> Reconciler:
        if self._portfolio is None:
            return Reconciler(
                self.journal, self.broker, self.clock, self.kill, self.risk, self._epic, booker=self.executor,
                entry_gate=lambda: self.manager.in_flight, notifier=self.alerts,
            )  # fmt: skip
        return Reconciler(
            self.journal, self.broker, self.clock, self.kill, self.risk, self._epic, booker=self.executor,
            entry_gate=lambda: self.manager.in_flight, notifier=self.alerts, legacy_keys=True,
            lane_epics=self._lane_epics(),
        )  # fmt: skip

    # --- kolejne pasma (config/lanes.toml; zadanie 12) ---

    def _load_extra_lanes(
        self, lanes: Sequence[LoadedLane] | None, strat: StrategyConfig, risk: RiskConfig
    ) -> tuple[LoadedLane, ...]:
        """Kolejne pasma: jawnie podane albo z `config_dir/lanes.toml` (brak pliku albo `enabled = false`: żadnych,
        silnik dokładnie jak v1). Zły plik pasm to odmowa startu (`EngineMisconfigured`), nigdy cicha praca bez niego."""
        if lanes is None:
            if self._config_dir is None:
                return ()
            try:
                lanes = load_lanes(self._config_dir) or ()
            except Exception as exc:
                raise EngineMisconfigured(f"lanes configuration refused: {type(exc).__name__}: {exc}") from exc
        lanes = tuple(lanes)
        epics = [lane.epic for lane in lanes]
        if strat.instrument in epics or len(set(epics)) != len(epics):
            raise EngineMisconfigured(f"lanes {epics}: one lane per instrument, none on {strat.instrument} (R1)")
        for lane in lanes:
            if lane.cfg.max_loss_per_trade > risk.max_loss_per_trade:
                raise EngineMisconfigured(f"lane {lane.epic}: max_loss_per_trade above risk_v1.toml")
        return lanes

    def _setup_portfolio(self, extra: tuple[LoadedLane, ...], cfg: PortfolioConfig | None) -> None:
        """Bramka portfelowa (R3), wspólny kurs walut i widok ekspozycji bez pasma (tylko z kolejnymi pasmami)."""
        if cfg is None:
            if self._config_dir is None:
                raise EngineMisconfigured("lanes need config/portfolio.toml (no config_dir)")
            try:
                cfg = load_portfolio(self._config_dir, self.risk)
            except Exception as exc:
                raise EngineMisconfigured(f"portfolio configuration refused: {type(exc).__name__}: {exc}") from exc
        self._fx = QuoteFxView(cfg.fx_quote_epic, cfg.fx_quote_max_age_s, self.clock, ACCOUNT_CURRENCY)
        lanes = [PortfolioLane(self._epic, ACCOUNT_CURRENCY, self.risk.max_loss_per_trade)]
        lanes += [PortfolioLane(lane.epic, lane.cfg.currency, lane.cfg.max_loss_per_trade) for lane in extra]
        self._portfolio = PortfolioGate(cfg, lanes, self.journal, self.calendar, self.clock, fx=self._fx, exposure=self)
        self._extra_epics = [lane.epic for lane in extra]

    def _lane_epics(self) -> list[str]:
        return [self._epic, *self._extra_epics]

    def _add_lane(self, loaded: LoadedLane) -> None:
        profiles = {NYSE.name: NYSE, XETRA.name: XETRA}
        calendar = ExchangeCalendar(profiles[loaded.cfg.exchange_profile])
        gate = BarLaneRiskGate(
            self.risk, loaded.bar, self.journal, calendar, self.clock, kill=self.kill,
            max_loss_per_trade=loaded.cfg.max_loss_per_trade,
        )  # fmt: skip
        try:
            problem = self._lane_arming(loaded)
        except Exception as exc:  # nie wiadomo, czy wolno: najwyżej dry_run
            problem = f"LANE_ARMING_CHECK_FAILED ({type(exc).__name__})"
        lane = BarLane(loaded, calendar, gate, CostModel(loaded.costs), problem)
        self.lanes.append(lane)
        self._lane_by_epic[lane.epic] = lane
        self._build_lane_parts(lane)

    def _lane_spec(self, lane: BarLane) -> LaneSpec:
        currency = None if lane.loaded.cfg.currency == ACCOUNT_CURRENCY else lane.loaded.cfg.currency
        # kalendarz księgowania = kalendarz portfela (dzień `realized_pnl` jak dziś: data berlińska, projekt §1.3)
        return LaneSpec(
            lane.epic, self.calendar, lane.loaded.cfg.slippage_guard_fraction, lane.loaded.cfg.max_loss_per_trade,
            currency=currency, uses_target=True,
        )  # fmt: skip

    def _build_lane_parts(self, lane: BarLane) -> None:
        """Wykonawca i uzgadnianie pasma; live tylko przy procesie live I uzbrojonym paśmie (R7)."""
        live = self._mode is Mode.LIVE and lane.arming_problem is None and not lane.downgraded
        lane.executor = Executor.for_lane(
            self.manager, self.broker, self.journal, lane.gate, self.kill, lane.cost_model, self.clock,
            self._lane_spec(lane), self.risk, self.alerts, live=live, memory=self.executor.memory, fx=self._fx,
            portfolio=self._portfolio,
        )  # fmt: skip
        lane.reconciler = Reconciler(
            self.journal, self.broker, self.clock, self.kill, self.risk, lane.epic, booker=lane.executor,
            entry_gate=lambda: self.manager.in_flight, notifier=self.alerts, legacy_keys=False,
            lane_epics=self._lane_epics(), targeted=True,
        )  # fmt: skip

    def _on_quote(self, q: Quote) -> None:
        """Każde notowanie z `GuardedBroker.quote`: straż ciszy WŁASNEGO instrumentu (A7), kurs pary walutowej."""
        if not self.lanes:
            self.watchdog.note_quote(q.ts)  # jedno pasmo: dokładnie jak v1
            return
        if q.epic == self._epic:
            self.watchdog.note_quote(q.ts)
        elif q.epic in self._lane_by_epic:
            self.watchdog.note_quote(q.ts, epic=q.epic)
            self.last_quotes[q.epic] = q
        if self._fx is not None and q.epic == self._fx.pair_epic:
            self._fx.note_quote(q)

    def _lane_block(self, epic: str) -> str | None:
        """Druga linia obrony po `req.epic` (silnik z pasmami): pasmo bez uzbrojenia nigdy nie wysyła wejścia."""
        if epic == self._epic:
            return None
        lane = self._lane_by_epic.get(epic)
        if lane is None:
            return "NO_LANE_FOR_INSTRUMENT"
        if lane.executor is None or not lane.executor.live:
            return "LANE_DRY_RUN_NO_ORDERS"
        return None

    def _close_key(self, deal_id: str) -> str | None:
        """Klucz zamkniętego rynku dla zamykanego deala: None dla DE40 v1, `epic` kolejnego pasma, "?" nieznany."""
        try:
            for epic in self._lane_epics():
                position = self.journal.current_position(epic)
                if position is not None and position.deal_id == deal_id:
                    return None if epic == self._epic else epic
        except Exception:
            pass
        for epic, deal in self.executor.memory.last_own_deal_by_epic.items():
            if deal == deal_id:
                return None if epic == self._epic else epic
        return "?"

    def unmanaged_epics(self) -> frozenset[str]:
        """`portfolio.ExposureView`: instrumenty bez pasma z pozycją wg ostatniego udanego odczytu listy brokera.
        Przed pierwszym udanym odczytem: wyjątek (bramka portfelowa blokuje, nie zgadujemy)."""
        if self._unmanaged_epics is None:
            raise RuntimeError("no successful broker position read yet")
        return self._unmanaged_epics

    def _downgrade(self, reason: str) -> None:
        """live → dry_run do końca procesu (nigdy z powrotem); alarm i zdarzenie. Nowy wykonawca dostaje pamięć wyjść
        poprzedniego (wysłane zamknięcia tego procesu: nigdy druga wysyłka na ślepo)."""
        if self._mode is Mode.DRY_RUN:
            return
        self._mode = Mode.DRY_RUN
        self.executor = self._build_executor(self.executor.memory)
        if self._own_reconciler:
            self.reconciler = self._build_reconciler()
        for lane in self.lanes:  # rozbrojenie globalne: wszystkie pasma najwyżej dry_run (projekt §6.2)
            self._build_lane_parts(lane)
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

    def _check_pending_without_lanes(self) -> None:
        """Skład bez pasm (dokładnie v1) z transakcją czekającą na rozliczenie w EUR (`journal.pnl_pending_deals()`,
        zostawioną przez wyłączone już pasmo w walucie obcej): bramka v1 nie czyta znaczników, a liczniki strat dnia i
        łączne nie zawierają tego wyniku, więc kill SOFT PNL_PENDING_WITHOUT_LANES z alarmem i zdarzeniem (raz na zbiór
        deali w procesie) zamiast odmowy startu. Wyjścia i zarządzanie pozycjami działają dalej; rozstrzyga właściciel
        (runbook: `resolve-lost-trade` / `clear-kill`; rozliczenie wykona silnik z włączonym `config/lanes.toml`).
        Wołane na początku każdego dnia i tuż przed każdym wejściem v1; bez znaczników nic nie zapisuje (ślady DE40
        bez zmian). Nieczytelne znaczniki: kill SOFT JOURNAL_UNREADABLE (nie wiemy, czy liczniki są pełne)."""
        if self.lanes:  # z pasmami blokuje bramka portfelowa (PORTFOLIO_PNL_PENDING), a rozlicza `_settle_foreign`
            return
        try:
            pending = self.journal.pnl_pending_deals()
        except Exception as exc:
            self.kill.soft(JOURNAL_UNREADABLE)
            self._alert(
                "PNL_PENDING:journal", "ERROR",
                f"{JOURNAL_UNREADABLE}: settlement markers unreadable ({type(exc).__name__}: {exc}); no new entries",
            )  # fmt: skip
            return
        if not pending:
            return
        deals = [deal for deal, _ in pending]
        self.kill.soft(PNL_PENDING_WITHOUT_LANES)
        self._alert(
            PNL_PENDING_WITHOUT_LANES, "ERROR",
            f"{PNL_PENDING_WITHOUT_LANES}: trade(s) {', '.join(deals)} await their settlement in {ACCOUNT_CURRENCY} "
            "but config/lanes.toml is off, so the loss counters miss them; no new entries (kill SOFT), exits and "
            "position management go on; the owner decides (runbook: resolve-lost-trade / clear-kill, or enable the "
            "lanes again so the engine settles them)",
        )  # fmt: skip
        if frozenset(deals) != self._pending_reported:
            self._pending_reported = frozenset(deals)
            self._event(PNL_PENDING_WITHOUT_LANES, deals=deals, exit_days=[day.isoformat() for _, day in pending])

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
        """Zakończone sesje live pasma DE40 dla polityki wielkości: transakcje instrumentu w księdze bez prób `smoke`
        (L0). Per instrument (A4): transakcje innych pasm nie przyspieszają wielkości wg reguł DE40; z jednym pasmem
        wynik równy dotychczasowemu (cała księga to DE40)."""
        return len(self.journal.trades(self._epic)) - self.journal.count_smoke_trades(self._epic)

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
        self._check_pending_without_lanes()
        try:
            if self.lanes:
                return await self._run_day_lanes(day)
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

    async def _run_day_lanes(self, day: date) -> DayReport:
        """Dzień portfela z kolejnymi pasmami (projekt §1.3): od najwcześniejszego przygotowania do najpóźniejszego
        zamknięcia pasm z sesją tego dnia (DE40 09:00-17:30, NYSE 15:30-22:00 czasu berlińskiego), JEDNA korutyna.
        Pasmo bez sesji danego dnia (święto) tylko zarządza pozostałością. Raport dnia dotyczy pasma DE40."""
        if self._shutdown:
            return self._report(day, SHUTDOWN, interrupted=True)
        self._enforce_limits(day)
        times = self._session_times(day)
        spans = [(times.preopen, times.close)] if times is not None else []
        for lane in self.lanes:
            try:
                d = lane.begin_day(day, PREOPEN_LEAD_MIN)
            except Exception as exc:  # m.in. wygasły kalendarz pasma: brak handlu pasma
                lane.day = None
                self._alert(f"CALENDAR:{lane.epic}", "ERROR", f"CALENDAR_UNAVAILABLE {lane.epic} for {day}: {exc}")
                continue
            if d.preopen is not None and d.close is not None:
                spans.append((d.preopen, d.close))
        if not spans:
            await self._off_day(day)
            return self._report(day, NOT_SESSION, interrupted=self._shutdown)
        start, end = min(a for a, _ in spans), max(b for _, b in spans)
        if self._now() < start:
            await self._idle_until(start)
            if self._shutdown:
                return self._report(day, SHUTDOWN, interrupted=True)
        st = self._day_states.get(day)
        if st is None or st.times != times:
            st = _DayState(day, times, self._now(), self._guard.gen)
            self._day_states = {day: st}
        if times is not None:
            self.watchdog.set_session_window(times.open, times.close)
        for lane in self.lanes:
            if lane.day is not None and lane.day.open is not None:
                self.watchdog.set_session_window(lane.day.open, lane.day.close, epic=lane.epic)
        try:
            await self._loop(st, end)
            if not self._shutdown:
                self.watchdog.set_status(state="FINAL_RECONCILE")
                await self._manage_lanes(st, self._now(), True, list(self.lanes))  # uzgadnianie końcowe
        finally:
            self._clear_windows()
        if self._shutdown:
            self._log_stop("shutdown requested")
        if times is None:
            skip: str | None = NOT_SESSION
        else:
            skip = st.skip_reason if st.decided else (SHUTDOWN if self._shutdown else NOT_READY)
        notes = list(st.notes) + [f"{lane.epic}: {lane.day.decisions if lane.day else 0} decisions" for lane in self.lanes]
        return self._report(day, skip, interrupted=self._shutdown, notes=notes)

    def _clear_windows(self) -> None:
        self.watchdog.set_session_window(None, None)
        for lane in self.lanes:
            self.watchdog.set_session_window(None, None, epic=lane.epic)

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
        if self.lanes:
            steps = self._lane_steps(st)
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
            if self._next_fx is not None:
                candidates.append(self._next_fx)
            for lane in self.lanes:  # budzenie na najbliższe zdarzenie spośród WSZYSTKICH pasm
                candidates += [t for t in (lane.next_quote, lane.next_manage) if t is not None]
                if not st.idle:
                    candidates += [t for t in (lane.next_candle_end(now), self._lane_flatten_at(lane)) if t]
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
        self._clear_windows()
        st = _DayState(None, None, self._now(), self._guard.gen, idle=True)
        if self._now() < until:
            await self._loop(st, until)

    async def _off_day(self, day: date) -> None:
        """Dzień bez sesji: tylko zarządzanie pozostałą pozycją lub zleceniem, do FLAT albo do północy."""
        self._clear_windows()
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

    def _market_closed_backoff(self, key: str | None = None) -> bool:
        """Zamknięcie odrzucone przez zamknięty rynek mniej niż minutę temu, dla instrumentu `key` (None: DE40 v1)."""
        closed_at = self._guard.market_closed.get(key)
        return closed_at is not None and (self._now() - closed_at).total_seconds() < MARKET_CLOSED_RETRY_S

    # --- kroki taktu z kolejnymi pasmami (zadanie 12, projekt §1.3) ---

    def _lane_steps(self, st: _DayState) -> list[Callable[[], Awaitable[None]]]:
        """Kolejność taktu z pasmami: wspólne (logowanie, rachunek), notowania pasm w stałej kolejności i kurs pary,
        ping, zarządzanie wszystkich pasm (uzgadnianie, ochrona, HARD → awaryjne spłaszczenie wszystkich, wyjścia),
        dane sygnału i decyzje pasm. Krok jednego pasma, który rzuci, nie zatrzymuje pozostałych (`_lane_safe`)."""
        epic = self._epic
        steps: list[Callable[[], Awaitable[None]]] = [
            lambda: self._ensure_login(st),
            lambda: self._ensure_account(st),
            lambda: self._lane_safe(epic, "quote", self._primary_quote_step, st),
        ]
        steps += [lambda ln=lane: self._lane_safe(ln.epic, "quote", self._bar_quote_step, st, ln) for lane in self.lanes]
        steps += [
            lambda: self._lane_safe("FX", "quote", self._fx_quote_step, st),
            lambda: self._ping_step(st),
            lambda: self._lanes_manage_step(st),
            lambda: self._lane_safe(epic, "signal_data", self._signal_data_step, st),
            lambda: self._lane_safe(epic, "decision", self._decision_step, st),
            lambda: self._lane_safe(epic, "candles_followup", self._candles_followup_step, st),
        ]
        steps += [
            lambda ln=lane: self._lane_safe(ln.epic, "decision", self._bar_decision_step, st, ln) for lane in self.lanes
        ]
        return steps

    async def _lane_safe(self, epic: str, what: str, fn: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """Izolacja pasma (rozstrzygnięcie d): wyjątek kroku jednego pasma → zdarzenie LANE_STEP_FAILED i alarm, a
        pozostałe pasma, wyjścia i spłaszczenia biegną dalej; błąd dziennika → kill miękki jak w `_safe`."""
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except JournalWriteError as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert(f"{epic}:{what}:journal", "ERROR", f"{JOURNAL_WRITE_FAILED} in {epic} {what}: {exc}")
        except JournalError as exc:
            self.kill.soft(JOURNAL_UNREADABLE)
            self._alert(f"{epic}:{what}:journal", "ERROR", f"{JOURNAL_UNREADABLE} in {epic} {what}: {exc}")
        except Exception as exc:
            _LOG.exception("lane %s step %s failed", epic, what)
            self._alert(
                f"LANE_STEP_FAILED:{epic}:{what}", "ERROR",
                f"LANE_STEP_FAILED {epic} {what}: {type(exc).__name__}: {exc}; other lanes and all exits go on",
            )  # fmt: skip
            self._event("LANE_STEP_FAILED", epic=epic, step=what, error=type(exc).__name__, message=str(exc))

    async def _primary_quote_step(self, st: _DayState) -> None:
        """Notowanie DE40: w jego sesji jak v1; po zamknięciu XETRA i w dniu bez jego sesji tylko przy pozostałości."""
        t, now = st.times, self._now()
        if (t is None or now > t.close) and not self._epic_needs_management(self._epic):
            return
        await self._quote_step(st)

    async def _bar_quote_step(self, st: _DayState, lane: BarLane) -> None:
        now = self._now()
        if not lane.active(now) and not self._epic_needs_management(lane.epic):
            return
        if lane.next_quote is not None and now < lane.next_quote:
            return
        lane.next_quote = _next(lane.next_quote, now, self._quote_poll_s)
        try:
            q = await self.broker.quote(lane.epic)  # straż ciszy pasma karmi `_on_quote` (klucz instrumentu)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # cisza notowań pasma wyłapie Watchdog
            _LOG.debug("quote %s failed: %s: %s", lane.epic, type(exc).__name__, exc)
            return
        lane.last_quote = q

    async def _fx_quote_step(self, st: _DayState) -> None:
        """Kurs pary walutowej (`portfolio.toml`: `fx_quote_epic`) co `quote_poll_s`, gdy któreś pasmo w walucie obcej
        jest w sesji albo ma pozycję, albo transakcja czeka na rozliczenie w EUR."""
        fx, now = self._fx, self._now()
        if fx is None:
            return
        need = any(
            lane.foreign and (lane.active(now) or self._epic_needs_management(lane.epic)) for lane in self.lanes
        )
        if not need:
            try:
                need = bool(self.journal.pnl_pending_deals())
            except Exception:
                need = True
        if not need or (self._next_fx is not None and now < self._next_fx):
            return
        self._next_fx = _next(self._next_fx, now, self._quote_poll_s)
        try:
            await self.broker.quote(fx.pair_epic)  # kurs trafia do `QuoteFxView` przez `_on_quote`
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # brak kursu = brak wejścia pasma w walucie obcej, nigdy blokada wyjścia
            _LOG.debug("fx quote %s failed: %s: %s", fx.pair_epic, type(exc).__name__, exc)

    async def _lanes_manage_step(self, st: _DayState) -> None:
        now = self._now()
        due = st.next_manage is None or now >= st.next_manage
        kick = st.times is not None and now >= st.times.flatten and not st.flatten_kicked
        primary = due or kick
        if primary and st.times is not None and now > st.times.close and st.primary_final:
            primary = self._epic_needs_management(self._epic)  # po zamknięciu XETRA DE40 tylko przy pozostałości
        lanes = [lane for lane in self.lanes if lane.next_manage is None or now >= lane.next_manage]
        if not primary and not lanes:
            return
        if primary:
            if kick:
                st.flatten_kicked = True
            st.next_manage = _next(st.next_manage, now, self.risk.reconcile_interval_s)
        for lane in lanes:
            lane.next_manage = _next(lane.next_manage, now, self.risk.reconcile_interval_s)
        await self._manage_lanes(st, now, primary, lanes)

    async def _manage_lanes(self, st: _DayState, now: datetime, primary: bool, lanes: list[BarLane]) -> None:
        """Zarządzanie portfela: uzgadnianie pasm z terminem (DE40 jak v1, potem kolejne pasma w stałej kolejności),
        potem HARD → awaryjne spłaszczenie WSZYSTKICH pasm z próbami przeplatanymi (`emergency_flatten_lanes`), a bez
        HARD wyjścia każdego pasma (EXITING → ponowienie, DE40 po godzinie spłaszczenia, pasmo B1: limit czasu pozycji
        i godzina spłaszczenia pasma). Zamknięty rynek wstrzymuje tylko swój instrument (A6). Wyjść nie blokuje ani
        bramka, ani kurs walut; błąd jednego pasma nie zatrzymuje drugiego.

        Kolejność DE40 jak w v1 (`_manage`): uzgadnianie DE40, a bez HARD od razu jego wyjście (EXITING albo godzina
        spłaszczenia 17:25), dopiero potem uzgadnianie kolejnych pasm (ochrona, cel, rozliczenie walut). Wolny krok
        pasma (np. przekroczenie czasu `set_protection` US500) nie opóźnia spłaszczenia DE40 (przegląd zadania 12, F3)."""
        if primary:
            result = await self.reconcile_once()
            if result is not None and not result.skipped:
                st.reconciled = True
                self._note_unmanaged(result)
            if st.times is not None and now >= st.times.close:
                st.primary_final = True
            if self._shutdown:
                return
            if self.kill.level() is not KillLevel.HARD and not self._market_closed_backoff():
                position = self._position()
                if position is not None and (
                    position.state is _P.EXITING
                    or (position.state is _P.OPEN and self._must_flatten(st, position, now))
                ):
                    await self._safe("flatten", self.executor.flatten, position, FLATTEN_REASON)
        for lane in lanes:
            if self._shutdown:
                return
            await self._reconcile_lane(lane)
        if self._shutdown:
            return
        if self.kill.level() is KillLevel.HARD:
            executors = [self.executor] if not self._market_closed_backoff() else []
            executors += [lane.executor for lane in self.lanes if not self._market_closed_backoff(lane.epic)]
            if executors:
                await self._safe("emergency_flatten", emergency_flatten_lanes, executors)
            return
        for lane in lanes:
            if self._shutdown:
                return
            await self._safe(f"lane_exit_{lane.epic}", self._lane_exit, lane, now)

    async def _reconcile_lane(self, lane: BarLane) -> None:
        """Jeden przebieg uzgadniania pasma (klucze per instrument) z jego reakcjami: stop dla pozycji bez stopu,
        straże wypełnienia, cel dla pozycji bez celu (`ensure_target`, raz na przebieg; porażka to alarm, nie HARD),
        pozycje bez pasma, rozliczenie wyniku w walucie obcej. Nigdy nie rzuca (poza anulowaniem)."""
        epic = lane.epic
        try:
            result = await lane.reconciler.run_once()
        except asyncio.CancelledError:
            raise
        except JournalWriteError as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert(f"RECONCILE_JOURNAL:{epic}", "ERROR", f"{JOURNAL_WRITE_FAILED} in reconciliation of {epic}: {exc}")
            return
        except Exception as exc:
            self.watchdog.note_reconcile_error(type(exc).__name__, str(exc), epic=epic)
            return
        if result.skipped:
            self.watchdog.note_reconcile_skipped(epic=epic)
            return
        self.watchdog.note_reconcile_ok(epic=epic)
        if lane.day is not None:
            lane.day.reconciled = True
        self._note_unmanaged(result)
        for p in result.unprotected:
            if self._shutdown:
                return
            await self._safe(f"protect_unprotected_{epic}", lane.executor.protect_unprotected, p)
        if self._shutdown:
            return
        await self._safe(f"guard_adopted_fill_{epic}", lane.executor.guard_adopted_fill)
        for p in {q.deal_id: q for q in result.untargeted}.values():
            if self._shutdown:
                return
            await self._safe(f"ensure_target_{epic}", lane.executor.ensure_target, p)
        if lane.foreign and not self._shutdown:
            await self._safe(f"settle_{epic}", self._settle_foreign, lane)

    async def _lane_exit(self, lane: BarLane, now: datetime) -> None:
        """Wyjście pasma: EXITING → ponowienie; OPEN po limicie czasu (`lanes.exit_due` → TIME_LIMIT) albo o godzinie
        spłaszczenia pasma (FORCED), także pozycja z wcześniejszego dnia; zamknięty rynek: próba co minutę."""
        if self._market_closed_backoff(lane.epic):
            return
        position = self.journal.current_position(lane.epic)
        if position is None:
            return
        if position.state is _P.EXITING:
            await lane.executor.flatten(position, FLATTEN_REASON)
        elif position.state is _P.OPEN:
            reason = lane.exit_reason(position, now)
            if reason is not None:
                await lane.executor.flatten(position, reason)

    def _note_unmanaged(self, result: ReconcileResult) -> None:
        """Pozycje brokera na instrumentach BEZ pasma (projekt §5.1, rozstrzygnięcie c): tylko zdarzenie i alarm raz
        na deal w procesie; nigdy ich nie dotykamy. Blokadę wszystkich wejść daje bramka portfelowa
        (PORTFOLIO_UNMANAGED_EXPOSURE) z tego widoku."""
        positions = list(result.unmanaged)
        self._unmanaged_epics = frozenset(p.epic for p in positions)
        for p in positions:
            if p.deal_id in self._unmanaged_seen:
                continue
            self._unmanaged_seen.add(p.deal_id)
            self._event(
                "UNMANAGED_POSITION", deal_id=p.deal_id, epic=p.epic, side=p.side, size=p.size,
                open_level=p.open_level, stop_level=p.stop_level,
            )  # fmt: skip
            self._alert(
                f"UNMANAGED_POSITION:{p.deal_id}", "WARN",
                f"UNMANAGED_POSITION {p.deal_id} on {p.epic} (no lane): never touched by the engine; every new entry "
                "is blocked while it is open (PORTFOLIO_UNMANAGED_EXPOSURE)",
            )  # fmt: skip

    def _settle_foreign(self, lane: BarLane) -> None:
        """Faza 2 księgowania pasma w walucie obcej (projekt §7.4, wariant z szacunkiem): wynik w EUR z kursu pary
        brokera po stronie niekorzystnej (`QuoteFxView.estimate`), źródło ESTIMATED_FX; brak ważnego kursu → zostaje
        `pnl_pending` (wejścia zablokowane) i alarm. Kursu nigdy nie zgadujemy."""
        fx = self._fx
        pending = self.journal.pnl_pending_deals()
        if fx is None or not pending:
            return
        trades = dict(self.journal.trades_by_deal(lane.epic))
        currency = lane.loaded.cfg.currency
        for deal_id, exit_day in pending:
            trade = trades.get(deal_id)
            if trade is None:
                self._check_pending_owner(deal_id, exit_day)
                continue
            estimate = fx.estimate(trade.net_pnl, currency)
            if estimate is None:
                self._alert(
                    f"PNL_PENDING_NO_RATE:{lane.epic}", "WARN",
                    f"PNL_PENDING {deal_id}: no valid {fx.pair_epic} rate to settle {trade.net_pnl} {currency}; "
                    "every entry stays blocked until it is settled",
                )  # fmt: skip
                continue
            eur, rate = estimate
            lane.executor.settle_foreign_pnl(deal_id, eur, source=ESTIMATED_FX, rate=rate)

    def _check_pending_owner(self, deal_id: str, exit_day: date) -> None:
        """Znacznik `pnl_pending` bez transakcji w księdze pasma, które właśnie rozlicza: zwykle należy do innego pasma
        w walucie obcej (ono go rozliczy). Gdy transakcji nie ma w księdze ŻADNEGO takiego pasma (np. pasmo usunięte
        z `config/lanes.toml` albo instrument bez pasma), nikt go nie rozliczy: alarm PNL_PENDING_ORPHAN i zdarzenie
        (raz na deal w procesie), nigdy ciche pominięcie. Wejścia i tak blokuje bramka portfelowa (PORTFOLIO_PNL_PENDING);
        kwoty nie zgadujemy, rozstrzyga właściciel."""
        if deal_id in self._orphan_pending_seen:
            return
        owners = [ln.epic for ln in self.lanes if ln.foreign]
        if any(deal_id in dict(self.journal.trades_by_deal(epic)) for epic in owners):
            return
        self._orphan_pending_seen.add(deal_id)
        self._alert(
            f"PNL_PENDING_ORPHAN:{deal_id}", "ERROR",
            f"PNL_PENDING_ORPHAN {deal_id} (exit day {exit_day}): awaits its settlement in {ACCOUNT_CURRENCY}, but no "
            f"foreign-currency lane ({', '.join(owners) or 'none'}) has this trade in its ledger, so nothing settles "
            "it; every entry stays blocked (PORTFOLIO_PNL_PENDING) until the owner resolves it",
        )  # fmt: skip
        self._event("PNL_PENDING_ORPHAN", deal_id=deal_id, exit_day=exit_day.isoformat(), lanes=owners)

    async def _bar_decision_step(self, st: _DayState, lane: BarLane) -> None:
        """Decyzja pasma B1/M5 po zamknięciu świecy w oknie sygnałów (patrz `engine_lanes`)."""
        now, d = self._now(), lane.day
        if st.idle or d is None or d.session is None:
            return
        if d.watch_from is None:
            d.watch_from = now
        for stale in lane.stale_candles(now):  # świeca przepadła: wpis zamiast cichego zniknięcia (F4)
            d.decided.add(stale)
            d.decisions += 1
            self._record_lane_stale(lane, stale, d.waiting.pop(stale, None))
        start = lane.due_candle(now)
        if start is None:
            return
        if self._guard.gen == st.login_gen:
            d.waiting[start] = "NOT_LOGGED_IN"
            return
        if not d.reconciled:
            d.waiting[start] = "STARTUP_RECONCILE_PENDING"
            return
        if lane.executor.live and self._verified_gen != self._guard.gen:
            await self._check_account(st)
            if self._verified_gen != self._guard.gen:
                d.waiting[start] = "ACCOUNT_UNVERIFIED"
                return
        lo, hi = lane.candle_window(start)
        try:
            candles = await self.broker.candles_5m(lane.epic, lo, hi)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            d.waiting[start] = "CANDLES_UNAVAILABLE"
            self._alert(f"CANDLES:{lane.epic}", "WARN", f"CANDLES_UNAVAILABLE {lane.epic}: {type(exc).__name__}: {exc}")
            return
        closed = lane.closed_candles(candles, start, now)
        if not closed or closed[-1].start != start:
            d.waiting[start] = "SIGNAL_CANDLE_MISSING"  # ponowienie w kolejnym takcie, najdłużej do STALE_SIGNAL
            return
        try:
            quote = await self.broker.quote(lane.epic)
            rules = await self.broker.instrument_rules(lane.epic)  # minimalny stop zależy od bieżącej ceny
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # bez decyzji, ponowienie w kolejnym takcie (najdłużej do STALE_SIGNAL)
            d.waiting[start] = "QUOTE_OR_RULES_UNAVAILABLE"
            self._alert(f"DECISION_DATA:{lane.epic}", "WARN", f"DECISION_DATA_UNAVAILABLE {lane.epic}: {exc}")
            return
        if self._shutdown:
            return
        if lane.entry_ordered_for(self.journal, start):  # po restarcie: ta świeca już dała zlecenie (także odrzucone)
            d.decided.add(start)
            return
        executor = lane.executor
        state = lane.day_state(self.journal, executor, now)
        session_index = lane.session_index(self.journal)
        d.decided.add(start)  # od tej chwili ta świeca jest rozstrzygnięta (także przy błędzie decyzji)
        d.decisions += 1
        try:
            decision = decide_entry(
                closed, quote, rules, self.risk, lane.bar, d.session, state, now, session_index=session_index,
                rule_size_allowed=False,
            )  # fmt: skip
        except Exception as exc:
            self._alert(f"DECIDE_FAILED:{lane.epic}", "ERROR", f"DECIDE_FAILED {lane.epic}: {type(exc).__name__}: {exc}")
            self._record_lane_decision(lane, start, closed, quote, state, session_index, None, repr(exc))
            return
        self._record_lane_decision(lane, start, closed, quote, state, session_index, decision, None)
        if decision.entered and not self._shutdown:
            await self._bar_enter(st, lane, decision, rules)

    async def _bar_enter(self, st: _DayState, lane: BarLane, decision: Any, rules: InstrumentRules) -> None:
        self._check_armed()  # rozbrojenie procesu: wszystkie pasma dry_run
        self._check_lane_arming(lane)  # rozbrojenie pasma: to pasmo dry_run
        if lane.executor.live and self._verified_gen != self._guard.gen:
            await self._check_account(st)
            if self._verified_gen != self._guard.gen:
                return
        assert lane.day is not None
        try:
            await lane.executor.enter(decision.plan, rules, lane.day.day)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # wykonawca już włączył kill switch miękki (ENTRY_EXCEPTION) i zaalarmował
            _LOG.error("entry %s failed: %s: %s", lane.epic, type(exc).__name__, exc)
        finally:
            self.watchdog.rearm_silence()

    def _check_lane_arming(self, lane: BarLane) -> None:
        """Przed wejściem pasma live: warunek uzbrojenia pasma nadal spełniony; inaczej to pasmo dry_run na stałe."""
        if lane.executor is None or not lane.executor.live:
            return
        try:
            problem = self._lane_arming(lane.loaded)
        except Exception as exc:
            problem = f"LANE_ARMING_CHECK_FAILED ({type(exc).__name__})"
        if problem is None:
            return
        lane.arming_problem, lane.downgraded = problem, True
        self._build_lane_parts(lane)
        message = f"LANE_DOWNGRADED {lane.epic} live -> dry_run for the rest of this run: {problem}"
        self._alert(f"LANE_DOWNGRADED:{lane.epic}", "ERROR", message)
        self._event("LANE_DOWNGRADED", epic=lane.epic, reason=problem)

    def _record_lane_decision(
        self, lane: BarLane, start: datetime, candles: list[Candle], quote: Quote, state: Any, session_index: int,
        decision: Any, error: str | None,
    ) -> None:  # fmt: skip
        plan = None if decision is None else decision.plan
        signal = None if decision is None else decision.signal
        payload = {
            "event": LANE_DECISION, "epic": lane.epic, "day": state.day.isoformat(),
            "mode": "live" if lane.executor.live else "dry_run", "signal_candle_start_utc": start,
            "signal_candle": candles[-1], "candles_used": len(candles), "quote": quote,
            "state": {
                "entries": state.entries, "result_r": state.result_r, "position_open": state.position_open,
                "last_exit_at": state.last_exit_at,
            },
            "session_index": session_index, "outcome": "ERROR" if decision is None else decision.reason,
            "error": error,
            "signal": None if signal is None else {"side": signal.side, "stop_level": signal.stop_level},
            "plan": None if plan is None else {
                "side": plan.side, "size": plan.size, "stop_distance": plan.stop_distance,
                "target_distance": plan.target_distance,
            },
            "decided_at": self._now(),
        }  # fmt: skip
        try:
            self.journal.append_raw(SOURCE_DECISION, dump(payload), self._now())
        except Exception as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert(f"DECISION_JOURNAL:{lane.epic}", "ERROR", f"{JOURNAL_WRITE_FAILED}: lane decision not journaled: {exc}")

    def _record_lane_stale(self, lane: BarLane, start: datetime, why: str | None) -> None:
        """Decyzja STALE_SIGNAL (`lanes.STALE_SIGNAL`) dla świecy sygnału, która przepadła: czekała (ostatni powód z
        `BarDay.waiting`, np. SIGNAL_CANDLE_MISSING, NOT_LOGGED_IN) albo jej pierwszy takt przyszedł po końcu następnej
        świecy (`why` = None). Bez wejścia; awaria zapisu → kill miękki jak przy każdej decyzji pasma."""
        d = lane.day
        assert d is not None
        payload = {
            "event": LANE_DECISION, "epic": lane.epic, "day": d.day.isoformat(),
            "mode": "live" if lane.executor is not None and lane.executor.live else "dry_run",
            "signal_candle_start_utc": start, "outcome": STALE_SIGNAL, "why": why, "error": None,
            "decided_at": self._now(),
        }  # fmt: skip
        try:
            self.journal.append_raw(SOURCE_DECISION, dump(payload), self._now())
        except Exception as exc:
            self.kill.soft(JOURNAL_WRITE_FAILED)
            self._alert(f"DECISION_JOURNAL:{lane.epic}", "ERROR", f"{JOURNAL_WRITE_FAILED}: lane decision not journaled: {exc}")

    def _lane_flatten_at(self, lane: BarLane) -> datetime | None:
        d = lane.day
        return None if d is None or d.session is None else d.session.flatten_at

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
        self._check_pending_without_lanes()  # znacznik mógł się pojawić po starcie dnia: wejście zablokuje KILL
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
        """Pozycja inna niż FLAT albo nierozstrzygnięte zlecenie KTÓREGOKOLWIEK pasma (nieczytelny dziennik: tak)."""
        return any(self._epic_needs_management(epic) for epic in [self._epic, *self._lane_by_epic])

    def _epic_needs_management(self, epic: str) -> bool:
        try:
            position = self.journal.current_position(epic)
            if position is not None and position.state is not _P.FLAT:
                return True
            return any(o.epic == epic for o in self.journal.unresolved_orders())
        except Exception:
            return True

    def _entries_today(self, day: date | None) -> int:
        if day is None:
            return 0
        if self.lanes:  # wejścia dnia per instrument (A3): wejście innego pasma nie zużywa wejścia DE40
            return self._safe_read(lambda: self.journal.count_entry_orders(day, self._epic), 1)
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
        if self.lanes:  # z kolejnymi pasmami raport dotyczy pasma DE40 (wynik US500 jest w innej walucie)
            entries = self._safe_read(lambda: self.journal.count_entry_orders(day, self._epic), -1)
        else:
            entries = self._safe_read(lambda: self.journal.count_entry_orders(day), -1)
        net = self._safe_read(lambda: self._day_net(day), None)
        return DayReport(entries, state, net, skip, day, self._mode.value, interrupted, tuple(notes or ()))

    def _day_net(self, day: date) -> Decimal | None:
        trades = self.journal.trades(self._epic) if self.lanes else self.journal.trades()
        nets = [t.net_pnl for t in trades if self.calendar.to_local(t.exit_ts).date() == day]
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
