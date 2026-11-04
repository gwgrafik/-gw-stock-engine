"""FakeBroker: broker w pamięci zachowujący się jak Capital.com, z pełnym sterowaniem i wstrzykiwaniem awarii.

Model:
- instrumenty: `rules` to jedne reguły (jeden instrument, jak w v1) albo lista reguł różnych instrumentów (pasma),
  `quote` to jedno notowanie albo lista z dokładnie jednym notowaniem na instrument; epic spoza reguł →
  `BrokerRejected("EPIC_NOT_FOUND")`; notowania, świece, reguły walidacji, depozyt i stan rynku są per instrument.
  Pierwszy instrument jest "główny": pozycję na instrumencie spoza reguł (wstawioną przez test do `positions`)
  obsługują jego notowanie, reguły i stan rynku (dla jednego instrumentu to dokładnie zachowanie v1);
- wejście: BUY po ask + `fill_slippage`, SELL po bid − `fill_slippage`; wielkość = żądana × `fill_size_factor`
  (bez zaokrąglania); stop dołączony atomowo: wypełnienie − `stop_distance` (BUY) lub + (SELL);
- cel zysku (`TargetedEntryRequest`, R4): dołączony atomowo, wypełnienie + `target_distance` (BUY) lub − (SELL);
  odległość <= 0 albo mniejsza niż `min_stop_distance` instrumentu (wspólne minimum stopu i celu) →
  `BrokerRejected("MIN_TARGET_DISTANCE")` bez skutku; pozycja z celem to `TargetedOpenPosition`, bez celu zwykłe
  `OpenPosition`; `set_stop` zachowuje cel (UPROSZCZENIE: czy Capital.com go kasuje, nie ustalono, C5d);
  `set_protection` ustawia stop i cel naraz (oba albo żaden);
- referencje "REF-{n}" i identyfikatory pozycji "DEAL-{n}" z osobnych liczników (od 1, wspólnych dla instrumentów);
  wejście odrzucone od razu (walidacja, `MarketClosed`) nie zużywa żadnego numeru, odrzucenie odroczone zużywa
  tylko referencję;
- `confirm(ref)` zwraca migawkę wykonania TEGO zlecenia z chwili wypełnienia (deal_id, cena wykonania jako
  open_level, wielkość tego zlecenia, stop i cel obowiązujące pozycję zaraz po wypełnieniu, czas wypełnienia), także
  po zamknięciu, stopie, celu, `set_stop`, `set_protection` i agregacji; None tylko dla nieznanej referencji;
- `reject_next_entry(reason)`: najbliższe wejście, które dotrze do brokera, dostaje referencję, ale nie tworzy
  pozycji, a `confirm` tej referencji rzuca `BrokerRejected(reason)` (jak dealStatus REJECTED);
- świece: zwracane są tylko zakończone (5 min: start + 5 min <= teraz; dzienne: start + 1 dzień <= teraz);
- zamknięcie wywołaniem: BUY po bid, SELL po ask (bez poślizgu) → `ClosedDeal` "MANUAL"; `trigger_stop` → "STOP";
  `trigger_target` → "TARGET" po poziomie celu;
- `simulate_candle(candle, spread)`: świeca (ceny mid) przechodzi przez rynek pozycji swojego instrumentu; BUY patrzy
  na bid = mid − spread/2, SELL na ask = mid + spread/2; stop dotknięty → "STOP" po stopie (luka przez stop: po
  otwarciu świecy), cel dotknięty → "TARGET" po celu; stop i cel w jednej świecy → STOP (R4);
- `netting=True`: drugie wejście w tym samym kierunku na tym samym instrumencie powiększa istniejącą pozycję (średni
  poziom ważony wielkością, stop i cel bez zmian); bez niego każde wejście to osobna pozycja;
- rachunki: aktywny jest `accounts[0]`, dopóki `select_account` albo `login` z `account_id` nie wybierze innego
  (każdy `login` zaczyna nową sesję); z wpisów `accounts` liczą się id, waluta, status i `equity` (saldo);
  `margin_used` aktywnego = Σ size × open_level × value_per_point × margin_rate (reguły instrumentu pozycji, bez
  przeliczania walut), `available` = equity − margin_used;
- zlecenia oczekujące: tylko te dodane przez `add_working_order` (id "WO-{n}", dowolny epic); nie wykonują się
  i nie zajmują depozytu;
- historia zamknięć: `closed_since` zwraca wszystkie zamknięcia od `since` albo rzuca; z `history_limit_days=N`
  zakres dłuższy niż N dni (teraz − since) to `BrokerError` (jak adapter z limitem 14 dni), domyślnie bez limitu;
- uproszczenia: pozycje są wspólne dla wszystkich rachunków, saldo nie zmienia się po zamknięciu, sesja wygasa
  tylko przez `expire_session()`; testy mogą zmieniać `positions` i `working_orders` bezpośrednio
  (np. obca pozycja, brak stopu).

Kolejność obsługi każdej metody brokera:
1. nazwa metody trafia do `calls`;
2. callbacki `on_call` (mogą zmienić stan brokera, np. `trigger_stop` tuż przed `close_position`;
   `once=True` rozbraja callback po pierwszym wywołaniu);
3. pierwsza zakolejkowana awaria `fail_next` tej metody (FIFO, jedna na wywołanie):
   `applied=False` → wyjątek bez skutku;
4. kontrole brokera: wygasła sesja (`SessionExpired`, poza `login`), zamknięty rynek instrumentu (`MarketClosed`,
   tylko `open_position` i `close_position`; `set_market_closed(flag, epic=None)`, None = wszystkie instrumenty),
   walidacja (`BrokerRejected`);
5. skutek; awaria z `applied=True` → wyjątek dopiero po skutku (np. timeout po przyjęciu zlecenia).
"""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal

from gwstock.broker.base import Broker, Capabilities
from gwstock.clock import Clock
from gwstock.types import (
    AccountInfo,
    BrokerError,
    BrokerRejected,
    Candle,
    ClosedDeal,
    EntryAck,
    EntryRequest,
    InstrumentRules,
    MarketClosed,
    OpenPosition,
    Quote,
    SessionExpired,
    Side,
    TargetedOpenPosition,
    WorkingOrder,
)

BROKER_METHODS = frozenset(name for name, member in vars(Broker).items() if inspect.iscoroutinefunction(member))
_FIVE_MINUTES = timedelta(minutes=5)
_ONE_DAY = timedelta(days=1)
_TRIGGER_REASONS = frozenset({"STOP", "FORCED"})


@dataclass(frozen=True)
class _Failure:
    exc: Exception
    applied: bool


@dataclass(frozen=True, eq=False)
class _Callback:
    fn: Callable[[], None]
    once: bool


@dataclass(frozen=True)
class _Rejected:
    reason: str


class FakeBroker:
    capabilities = Capabilities(
        stop_attach="atomic", idempotency_key=None, keepalive_s=600, reconcile="positions", is_cfd=True
    )

    def __init__(
        self,
        clock: Clock,
        rules: InstrumentRules | Sequence[InstrumentRules],
        quote: Quote | Sequence[Quote],
        account_id: str | None = None,
        *,
        netting: bool = False,
        history_limit_days: int | None = None,
    ) -> None:
        if history_limit_days is not None and (
            isinstance(history_limit_days, bool) or not isinstance(history_limit_days, int) or history_limit_days <= 0
        ):
            raise ValueError(f"history_limit_days: expected a positive integer or None, got {history_limit_days!r}")
        self._history_limit = None if history_limit_days is None else timedelta(days=history_limit_days)
        self._clock = clock
        self._instruments = _instruments(rules)
        self._primary = next(iter(self._instruments))
        self._quotes = self._initial_quotes(quote)
        self._quote_fns: dict[str, Callable[[datetime], Quote]] = {}
        self._account_id = account_id
        self._netting = netting
        self.fill_size_factor = Decimal(1)
        self.fill_slippage = Decimal(0)
        self.accounts: list[AccountInfo] = [
            AccountInfo("ACC-0", "EUR", "ENABLED", Decimal(0), Decimal(0), Decimal(0)),
            AccountInfo("ACC-1", "EUR", "ENABLED", Decimal(40), Decimal(40), Decimal(0)),
        ]
        self.positions: dict[str, OpenPosition] = {}
        self.working_orders: list[WorkingOrder] = []
        self.calls: list[str] = []
        self._closed: list[ClosedDeal] = []
        self._confirms: dict[str, OpenPosition | _Rejected] = {}
        self._pending_rejections: deque[str] = deque()
        self._ref_seq = 0
        self._deal_seq = 0
        self._order_seq = 0
        self._failures: dict[str, deque[_Failure]] = {}
        self._callbacks: dict[str, list[_Callback]] = {}
        self._session_expired = False
        self._closed_markets: set[str] = set()
        self._active_account_id: str | None = None  # None: rachunek domyślny nowej sesji (accounts[0])
        self._daily: dict[str, list[Candle]] = {}
        self._candles_5m: dict[str, list[Candle]] = {}

    # Pola modelu jednego instrumentu, czytane przez istniejące testy (np. `test_modes.py`): instrument główny.

    @property
    def _rules(self) -> InstrumentRules:
        return self._instruments[self._primary]

    @property
    def _quote(self) -> Quote:
        return self._quotes[self._primary]

    @property
    def _quote_fn(self) -> Callable[[datetime], Quote] | None:
        return self._quote_fns.get(self._primary)

    # --- sterowanie testowe (nie trafia do `calls`) -----------------------------

    def set_quote(self, q: Quote) -> None:
        """Stałe notowanie instrumentu `q.epic` (zapisane tak, jak podane); wyłącza jego `set_quote_fn`."""
        self._require_own_data(q.epic)
        self._quotes[q.epic] = q
        self._quote_fns.pop(q.epic, None)

    def set_quote_fn(self, fn: Callable[[datetime], Quote], epic: str | None = None) -> None:
        """Notowanie wyliczane z zegara: `quote()` i wypełnienia instrumentu używają `fn(clock.now_utc())`.

        `epic=None` tylko przy jednym instrumencie; przy kilku trzeba wskazać, którego notowania dotyczy funkcja.
        """
        if epic is None:
            if len(self._instruments) != 1:
                raise ValueError(f"set_quote_fn: epic is required with several instruments {sorted(self._instruments)}")
            epic = self._primary
        self._require_own_data(epic)
        self._quote_fns[epic] = fn

    def set_daily_candles(self, candles: Iterable[Candle], epic: str | None = None) -> None:
        """Świece dzienne; `epic=None` zastępuje świece wszystkich instrumentów, `epic`: tylko tego instrumentu."""
        self._daily = self._store_candles(self._daily, candles, epic)

    def set_candles_5m(self, candles: Iterable[Candle], epic: str | None = None) -> None:
        """Świece 5-minutowe; `epic=None` zastępuje świece wszystkich instrumentów, `epic`: tylko tego instrumentu."""
        self._candles_5m = self._store_candles(self._candles_5m, candles, epic)

    def fail_next(self, method: str, exc: Exception, applied: bool = False) -> None:
        """Kolejkuje awarię najbliższego wywołania `method` (FIFO).

        `applied=True`: skutek u brokera następuje, a klient i tak widzi wyjątek (np. timeout po przyjęciu).
        """
        self._require_method(method)
        self._failures.setdefault(method, deque()).append(_Failure(exc, applied))

    def on_call(self, method: str, callback: Callable[[], None], once: bool = False) -> None:
        """Callback przy wywołaniu `method`, tuż przed jej skutkiem (po zapisie w `calls`).

        Domyślnie przy każdym wywołaniu; `once=True`: tylko przy pierwszym, potem rozbrojony.
        """
        self._require_method(method)
        self._callbacks.setdefault(method, []).append(_Callback(callback, once))

    def reject_next_entry(self, reason: str) -> None:
        """Odrzucenie odroczone (FIFO): wejście dostanie referencję, a `confirm` rzuci `BrokerRejected(reason)`."""
        self._pending_rejections.append(reason)

    def trigger_stop(self, deal_id: str, level: Decimal, reason: str = "STOP") -> None:
        """Broker zamknął pozycję po `level`.

        `reason`: "STOP" (stop u brokera; przy luce gorzej niż stop_level) albo "FORCED" (wymuszenie przez brokera).
        Zamknięcie celem to `trigger_target`.
        """
        if reason not in _TRIGGER_REASONS:
            raise ValueError(f"trigger_stop: reason must be one of {sorted(_TRIGGER_REASONS)}, got {reason!r}")
        pos = self.positions.pop(deal_id, None)
        if pos is None:
            raise ValueError(f"trigger_stop: no open position {deal_id!r}")
        self._closed.append(ClosedDeal(deal_id, pos.epic, level, reason, self._clock.now_utc()))

    def trigger_target(self, deal_id: str) -> None:
        """Broker zamknął pozycję jej celem zysku: `ClosedDeal` "TARGET" po `target_level` (zlecenie z limitem)."""
        pos = self.positions.get(deal_id)
        if pos is None:
            raise ValueError(f"trigger_target: no open position {deal_id!r}")
        if pos.target_level is None:
            raise ValueError(f"trigger_target: position {deal_id!r} has no target")
        self._book(pos, pos.target_level, "TARGET")

    def simulate_candle(self, candle: Candle, spread: Decimal = Decimal(0)) -> list[ClosedDeal]:
        """Świeca `candle` (ceny mid) przeszła przez rynek: zamyka pozycje instrumentu, których stop lub cel dotknęła.

        BUY patrzy na bid (mid − spread/2), SELL na ask (mid + spread/2). Stop dotknięty: "STOP" po stopie, a gdy
        świeca otworzyła się już za stopem (luka), po cenie otwarcia. Cel dotknięty: "TARGET" po celu. Stop i cel w
        tej samej świecy: STOP (R4: kolejność wewnątrz świecy nieznana, więc wariant ostrożny). Zwraca zamknięcia
        w kolejności pozycji; to sterowanie testowe, nie wywołanie brokera (nie trafia do `calls`).
        """
        self._require_own_data(candle.epic)
        if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
            raise ValueError(f"simulate_candle: spread must be a finite Decimal >= 0, got {spread!r}")
        half = spread / 2
        closed: list[ClosedDeal] = []
        for pos in [p for p in self.positions.values() if p.epic == candle.epic]:
            stop, goal = pos.stop_level, pos.target_level
            if pos.side is Side.BUY:  # pozycja BUY zamyka się po bid
                open_, high, low = candle.open - half, candle.high - half, candle.low - half
                stop_exit = min(stop, open_) if stop is not None and low <= stop else None
                target_hit = goal is not None and high >= goal
            else:  # pozycja SELL zamyka się po ask
                open_, high, low = candle.open + half, candle.high + half, candle.low + half
                stop_exit = max(stop, open_) if stop is not None and high >= stop else None
                target_hit = goal is not None and low <= goal
            if stop_exit is not None:  # także gdy cel dotknięty w tej samej świecy (R4)
                closed.append(self._book(pos, stop_exit, "STOP"))
            elif target_hit:
                closed.append(self._book(pos, goal, "TARGET"))
        return closed

    def add_working_order(self, epic: str, side: Side, size: Decimal, level: Decimal) -> WorkingOrder:
        """Umieszcza zlecenie oczekujące u brokera (np. obce, złożone ręcznie); dowolny instrument."""
        order = WorkingOrder(f"WO-{self._order_seq + 1}", epic, side, size, level)  # błędne dane nie zużywają id
        self._order_seq += 1
        self.working_orders.append(order)
        return order

    def expire_session(self) -> None:
        """Do następnego `login()` każda metoda brokera (także `keepalive`) rzuca `SessionExpired`."""
        self._session_expired = True

    def set_market_closed(self, flag: bool, epic: str | None = None) -> None:
        """Zamknięty rynek: `open_position` i `close_position` instrumentu rzucają `MarketClosed`; odczyty działają.

        `epic=None`: wszystkie instrumenty naraz (także te wcześniej zamknięte osobno); `epic`: tylko ten instrument.
        """
        if epic is not None:
            self._require_own_data(epic)
        epics = set(self._instruments) if epic is None else {epic}
        if flag:
            self._closed_markets |= epics
        else:
            self._closed_markets -= epics

    # --- Broker ------------------------------------------------------------------

    async def login(self) -> None:
        with self._call("login"):
            if self._account_id is not None and self._find_account(self._account_id) is None:
                raise BrokerError(f"bound account {self._account_id!r} not found among broker accounts")
            self._session_expired = False
            self._active_account_id = self._account_id

    async def keepalive(self) -> None:
        with self._call("keepalive"):
            pass

    async def instrument_rules(self, epic: str) -> InstrumentRules:
        with self._call("instrument_rules"):
            self._require_epic(epic)
            rules = self._instruments[epic]
        return rules

    async def quote(self, epic: str) -> Quote:
        with self._call("quote"):
            self._require_epic(epic)
            q = self._current_quote(epic)
        return q

    async def daily_candles(self, epic: str, count: int) -> list[Candle]:
        with self._call("daily_candles"):
            self._require_epic(epic)
            if count < 1:
                raise ValueError(f"daily_candles: count must be positive, got {count}")
            now = self._clock.now_utc()
            candles = [c for c in self._daily.get(epic, []) if c.start + _ONE_DAY <= now][-count:]
        return candles

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]:
        with self._call("candles_5m"):
            self._require_epic(epic)
            now = self._clock.now_utc()
            own = self._candles_5m.get(epic, [])
            candles = [c for c in own if start <= c.start < end and c.start + _FIVE_MINUTES <= now]
        return candles

    async def open_position(self, req: EntryRequest) -> EntryAck:
        with self._call("open_position"):
            self._require_epic(req.epic)
            self._require_market_open(req.epic)
            self._validate_entry(req)
            target = self._netting_target(req)
            self._ref_seq += 1
            reference = f"REF-{self._ref_seq}"
            if self._pending_rejections:
                self._confirms[reference] = _Rejected(self._pending_rejections.popleft())
            else:
                self._confirms[reference] = self._fill(req, target)
        return EntryAck(reference)

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        with self._call("confirm"):
            outcome = self._confirms.get(deal_reference)
            if isinstance(outcome, _Rejected):
                raise BrokerRejected(outcome.reason)
        return outcome

    async def list_positions(self) -> list[OpenPosition]:
        with self._call("list_positions"):
            positions = list(self.positions.values())
        return positions

    async def list_working_orders(self) -> list[WorkingOrder]:
        with self._call("list_working_orders"):
            orders = list(self.working_orders)
        return orders

    async def close_position(self, deal_id: str) -> None:
        with self._call("close_position"):
            known = self.positions.get(deal_id)
            if known is not None:
                self._require_market_open(self._sim_epic(known.epic))
            elif self._closed_markets >= set(self._instruments):
                # nieznany deal przy wszystkich rynkach zamkniętych: jak w v1 rynek sprawdzany przed pozycją
                self._require_market_open(self._primary)
            pos = self._require_position(deal_id)
            q = self._current_quote(self._sim_epic(pos.epic))
            exit_level = q.bid if pos.side is Side.BUY else q.ask
            self._book(pos, exit_level, "MANUAL")

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        with self._call("set_stop"):
            pos = self._require_position(deal_id)
            if not self._stop_allowed(pos, stop_level):
                raise BrokerRejected("MIN_STOP_DISTANCE")
            self.positions[deal_id] = replace(pos, stop_level=stop_level, stop_derived=False)

    async def set_protection(self, deal_id: str, stop_level: Decimal, target_level: Decimal | None) -> None:
        with self._call("set_protection"):
            pos = self._require_position(deal_id)
            if not self._stop_allowed(pos, stop_level):
                raise BrokerRejected("MIN_STOP_DISTANCE")
            if target_level is not None and not self._target_allowed(pos, target_level):
                raise BrokerRejected("MIN_TARGET_DISTANCE")
            self.positions[deal_id] = _protected(pos, stop_level, target_level)

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        with self._call("closed_since"):
            if self._history_limit is not None:  # rozpiętość tylko przy limicie: bez limitu nie ma czego odejmować
                span = self._clock.now_utc() - since
                if span > self._history_limit:
                    raise BrokerError(
                        f"closed_since: requested history of {span} exceeds the limit {self._history_limit}"
                    )
            closed = [d for d in self._closed if d.ts >= since]
        return closed

    async def account(self) -> AccountInfo:
        with self._call("account"):
            active_id = self._active_id()
            acc = self._find_account(active_id) if active_id is not None else None
            if acc is None:
                raise BrokerError(f"active account {active_id!r} not found")
            info = self._view(acc, active_id)
        return info

    async def list_accounts(self) -> list[AccountInfo]:
        with self._call("list_accounts"):
            active_id = self._active_id()
            accounts = [self._view(acc, active_id) for acc in self.accounts]
        return accounts

    async def select_account(self, account_id: str) -> None:
        with self._call("select_account"):
            if self._find_account(account_id) is None:
                raise BrokerError(f"unknown account {account_id!r}")
            self._active_account_id = account_id

    # --- mechanika -----------------------------------------------------------------

    @contextmanager
    def _call(self, method: str) -> Iterator[None]:
        self.calls.append(method)
        callbacks = self._callbacks.get(method, [])
        for callback in list(callbacks):
            if callback.once:
                callbacks.remove(callback)
            callback.fn()
        queue = self._failures.get(method)
        failure = queue.popleft() if queue else None
        if failure is not None and not failure.applied:
            raise failure.exc
        if self._session_expired and method != "login":
            raise SessionExpired(f"{method}: session expired, login required")
        yield
        if failure is not None:
            raise failure.exc

    @staticmethod
    def _require_method(method: str) -> None:
        if method not in BROKER_METHODS:
            raise ValueError(f"{method!r} is not a Broker method; expected one of {sorted(BROKER_METHODS)}")

    def _initial_quotes(self, quote: Quote | Sequence[Quote]) -> dict[str, Quote]:
        listed = [quote] if isinstance(quote, Quote) else list(quote)
        result: dict[str, Quote] = {}
        for q in listed:
            if not isinstance(q, Quote):
                raise TypeError(f"FakeBroker: expected Quote, got {q!r}")
            self._require_own_data(q.epic)
            if q.epic in result:
                raise ValueError(f"FakeBroker: two quotes for {q.epic!r}")
            result[q.epic] = q
        missing = sorted(set(self._instruments) - set(result))
        if missing:
            raise ValueError(f"FakeBroker: no quote for {missing}")
        return result

    def _require_own_data(self, epic: str) -> None:
        if epic not in self._instruments:
            raise ValueError(f"FakeBroker simulates only {sorted(self._instruments)}, got data for {epic!r}")

    def _store_candles(
        self, store: dict[str, list[Candle]], candles: Iterable[Candle], epic: str | None
    ) -> dict[str, list[Candle]]:
        """Nowy magazyn świec: `epic=None` zastępuje wszystko, `epic` tylko świece tego instrumentu."""
        if epic is not None:
            self._require_own_data(epic)
        ordered = sorted(candles, key=lambda c: c.start)
        for c in ordered:
            self._require_own_data(c.epic)
            if epic is not None and c.epic != epic:
                raise ValueError(f"FakeBroker: a candle of {c.epic!r} passed as candles of {epic!r}")
        result = {} if epic is None else {k: v for k, v in store.items() if k != epic}
        for c in ordered:
            result.setdefault(c.epic, []).append(c)
        return result

    def _require_epic(self, epic: str) -> None:
        if epic not in self._instruments:
            raise BrokerRejected("EPIC_NOT_FOUND")

    def _sim_epic(self, epic: str) -> str:
        """Instrument, którego rynek obsługuje pozycję: własny albo główny (pozycja spoza reguł, dodana przez test)."""
        return epic if epic in self._instruments else self._primary

    def _require_market_open(self, epic: str) -> None:
        if epic in self._closed_markets:
            raise MarketClosed(f"market {epic} is closed")

    def _require_position(self, deal_id: str) -> OpenPosition:
        pos = self.positions.get(deal_id)
        if pos is None:
            raise BrokerRejected("POSITION_NOT_FOUND")
        return pos

    def _stop_allowed(self, pos: OpenPosition, stop_level: Decimal) -> bool:
        """Stop po stronie straty, co najmniej o minimum instrumentu od ceny zamknięcia (BUY: bid, SELL: ask)."""
        epic = self._sim_epic(pos.epic)
        q = self._current_quote(epic)
        min_distance = self._instruments[epic].min_stop_distance
        if pos.side is Side.BUY:
            return stop_level <= q.bid - min_distance
        return stop_level >= q.ask + min_distance

    def _target_allowed(self, pos: OpenPosition, target_level: Decimal) -> bool:
        """Cel po stronie zysku, co najmniej o minimum instrumentu od ceny zamknięcia (BUY: bid, SELL: ask)."""
        epic = self._sim_epic(pos.epic)
        q = self._current_quote(epic)
        min_distance = self._instruments[epic].min_stop_distance
        if pos.side is Side.BUY:
            return target_level >= q.bid + min_distance
        return target_level <= q.ask - min_distance

    def _validate_entry(self, req: EntryRequest) -> None:
        r = self._instruments[req.epic]
        if req.size < r.min_size:
            raise BrokerRejected("MIN_SIZE")
        if (req.size - r.min_size) % r.size_step != 0:
            raise BrokerRejected("SIZE_STEP")
        if req.stop_distance <= 0 or req.stop_distance < r.min_stop_distance:
            raise BrokerRejected("MIN_STOP_DISTANCE")
        goal = req.target_distance
        if goal is not None and (goal <= 0 or goal < r.min_stop_distance):
            raise BrokerRejected("MIN_TARGET_DISTANCE")

    def _fill(self, req: EntryRequest, target: OpenPosition | None) -> OpenPosition:
        """Wykonuje wejście i zwraca migawkę wykonania tego zlecenia (wynik `confirm`)."""
        q = self._current_quote(req.epic)
        if req.side is Side.BUY:
            fill = q.ask + self.fill_slippage
            stop = fill - req.stop_distance
            goal = None if req.target_distance is None else fill + req.target_distance
        else:
            fill = q.bid - self.fill_slippage
            stop = fill + req.stop_distance
            goal = None if req.target_distance is None else fill - req.target_distance
        size = req.size * self.fill_size_factor
        now = self._clock.now_utc()
        if target is None:
            self._deal_seq += 1
            plain = OpenPosition(f"DEAL-{self._deal_seq}", req.epic, req.side, size, fill, stop, now)
            snapshot = plain if goal is None else _protected(plain, stop, goal)
            self.positions[snapshot.deal_id] = snapshot
            return snapshot
        total = target.size + size
        level = (target.size * target.open_level + size * fill) / total
        self.positions[target.deal_id] = replace(target, size=total, open_level=level)
        # agregacja: stop i cel pozycji się nie zmieniają, więc migawka podaje ochronę, która faktycznie chroni pozycję
        plain = OpenPosition(target.deal_id, req.epic, req.side, size, fill, target.stop_level, now)
        if target.target_level is None:
            return plain
        return _protected(plain, target.stop_level, target.target_level, target_derived=target.target_derived)

    def _netting_target(self, req: EntryRequest) -> OpenPosition | None:
        if not self._netting:
            return None
        target = next((p for p in self.positions.values() if p.epic == req.epic), None)
        if target is not None and target.side is not req.side:
            raise NotImplementedError("FakeBroker does not simulate netting of an opposite-side entry")
        return target

    def _book(self, pos: OpenPosition, exit_level: Decimal, reason: str) -> ClosedDeal:
        """Zamknięcie pozycji u brokera: znika z listy, a zamknięcie trafia do historii."""
        del self.positions[pos.deal_id]
        deal = ClosedDeal(pos.deal_id, pos.epic, exit_level, reason, self._clock.now_utc())
        self._closed.append(deal)
        return deal

    def _current_quote(self, epic: str) -> Quote:
        fn = self._quote_fns.get(epic)
        if fn is None:
            return self._quotes[epic]
        q = fn(self._clock.now_utc())
        self._require_own_data(q.epic)
        if q.epic != epic:
            raise ValueError(f"FakeBroker: the quote function of {epic!r} returned a quote for {q.epic!r}")
        return q

    def _find_account(self, account_id: str) -> AccountInfo | None:
        return next((a for a in self.accounts if a.account_id == account_id), None)

    def _active_id(self) -> str | None:
        if self._active_account_id is not None:
            return self._active_account_id
        return self.accounts[0].account_id if self.accounts else None

    def _view(self, acc: AccountInfo, active_id: str | None) -> AccountInfo:
        margin = Decimal(0)
        if acc.account_id == active_id:
            margin = sum((self._margin(p) for p in self.positions.values()), Decimal(0))
        return AccountInfo(acc.account_id, acc.currency, acc.status, acc.equity, acc.equity - margin, margin)

    def _margin(self, p: OpenPosition) -> Decimal:
        r = self._instruments[self._sim_epic(p.epic)]
        return p.size * p.open_level * r.value_per_point * r.margin_rate


def _instruments(rules: InstrumentRules | Sequence[InstrumentRules]) -> dict[str, InstrumentRules]:
    listed = [rules] if isinstance(rules, InstrumentRules) else list(rules)
    if not listed:
        raise ValueError("FakeBroker: at least one InstrumentRules is required")
    result: dict[str, InstrumentRules] = {}
    for r in listed:
        if not isinstance(r, InstrumentRules):
            raise TypeError(f"FakeBroker: expected InstrumentRules, got {r!r}")
        if r.epic in result:
            raise ValueError(f"FakeBroker: duplicate rules for {r.epic!r}")
        result[r.epic] = r
    return result


def _protected(
    pos: OpenPosition, stop_level: Decimal | None, target_level: Decimal | None, *, target_derived: bool = False
) -> OpenPosition:
    """Ta sama pozycja z nową ochroną (stop podany przez brokera, nie wyliczony): `TargetedOpenPosition` z celem,
    zwykłe `OpenPosition` bez celu."""
    base = {f.name: getattr(pos, f.name) for f in fields(OpenPosition)}
    base.update(stop_level=stop_level, stop_derived=False)
    if target_level is None:
        return OpenPosition(**base)
    return TargetedOpenPosition(**base, target_level=target_level, target_derived=target_derived)
