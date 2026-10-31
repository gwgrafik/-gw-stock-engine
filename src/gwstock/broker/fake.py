"""FakeBroker: broker w pamięci zachowujący się jak Capital.com, z pełnym sterowaniem i wstrzykiwaniem awarii.

Model:
- jeden instrument (`rules.epic`); inny epic → `BrokerRejected("EPIC_NOT_FOUND")`;
- wejście: BUY po ask + `fill_slippage`, SELL po bid − `fill_slippage`; wielkość = żądana × `fill_size_factor`
  (bez zaokrąglania); stop dołączony atomowo: wypełnienie − `stop_distance` (BUY) lub + (SELL);
- referencje "REF-{n}" i identyfikatory pozycji "DEAL-{n}" z osobnych liczników (od 1);
  odrzucone wejście nie zużywa numeru;
- zamknięcie wywołaniem: BUY po bid, SELL po ask (bez poślizgu) → `ClosedDeal` "MANUAL"; `trigger_stop` → "STOP";
- `netting=True`: drugie wejście w tym samym kierunku powiększa istniejącą pozycję (średni poziom ważony wielkością,
  stop bez zmian); bez niego każde wejście to osobna pozycja;
- rachunki: aktywny jest `accounts[0]`, dopóki `select_account` albo `login` z `account_id` nie wybierze innego
  (każdy `login` zaczyna nową sesję); z wpisów `accounts` liczą się id, waluta, status i `equity` (saldo);
  `margin_used` aktywnego = Σ size × open_level × value_per_point × margin_rate, `available` = equity − margin_used;
- zlecenia oczekujące: tylko te dodane przez `add_working_order` (id "WO-{n}", dowolny epic); nie wykonują się
  i nie zajmują depozytu;
- uproszczenia: pozycje są wspólne dla wszystkich rachunków, saldo nie zmienia się po zamknięciu, sesja wygasa
  tylko przez `expire_session()`; testy mogą zmieniać `positions` i `working_orders` bezpośrednio
  (np. obca pozycja, brak stopu).

Kolejność obsługi każdej metody brokera:
1. nazwa metody trafia do `calls`;
2. callbacki `on_call` (mogą zmienić stan brokera, np. `trigger_stop` tuż przed `close_position`);
3. pierwsza zakolejkowana awaria `fail_next` tej metody (FIFO, jedna na wywołanie):
   `applied=False` → wyjątek bez skutku;
4. kontrole brokera: wygasła sesja (`SessionExpired`, poza `login`), zamknięty rynek (`MarketClosed`, tylko
   `open_position` i `close_position`), walidacja (`BrokerRejected`);
5. skutek; awaria z `applied=True` → wyjątek dopiero po skutku (np. timeout po przyjęciu zlecenia).
"""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
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
    WorkingOrder,
)

BROKER_METHODS = frozenset(name for name, member in vars(Broker).items() if inspect.iscoroutinefunction(member))


@dataclass(frozen=True)
class _Failure:
    exc: Exception
    applied: bool


class FakeBroker:
    capabilities = Capabilities(
        stop_attach="atomic", idempotency_key=None, keepalive_s=600, reconcile="positions", is_cfd=True
    )

    def __init__(
        self,
        clock: Clock,
        rules: InstrumentRules,
        quote: Quote,
        account_id: str | None = None,
        *,
        netting: bool = False,
    ) -> None:
        self._clock = clock
        self._rules = rules
        self._require_own_data(quote.epic)
        self._quote = quote
        self._quote_fn: Callable[[datetime], Quote] | None = None
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
        self._references: dict[str, str] = {}
        self._ref_seq = 0
        self._deal_seq = 0
        self._order_seq = 0
        self._failures: dict[str, deque[_Failure]] = {}
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self._session_expired = False
        self._market_closed = False
        self._active_account_id: str | None = None  # None: rachunek domyślny nowej sesji (accounts[0])
        self._daily: list[Candle] = []
        self._candles_5m: list[Candle] = []

    # --- sterowanie testowe (nie trafia do `calls`) -----------------------------

    def set_quote(self, q: Quote) -> None:
        """Stałe notowanie (zapisane tak, jak podane); wyłącza `set_quote_fn`."""
        self._require_own_data(q.epic)
        self._quote = q
        self._quote_fn = None

    def set_quote_fn(self, fn: Callable[[datetime], Quote]) -> None:
        """Notowanie wyliczane z zegara: `quote()` i wypełnienia używają `fn(clock.now_utc())`."""
        self._quote_fn = fn

    def set_daily_candles(self, candles: Iterable[Candle]) -> None:
        self._daily = self._sorted_own(candles)

    def set_candles_5m(self, candles: Iterable[Candle]) -> None:
        self._candles_5m = self._sorted_own(candles)

    def fail_next(self, method: str, exc: Exception, applied: bool = False) -> None:
        """Kolejkuje awarię najbliższego wywołania `method` (FIFO).

        `applied=True`: skutek u brokera następuje, a klient i tak widzi wyjątek (np. timeout po przyjęciu).
        """
        self._require_method(method)
        self._failures.setdefault(method, deque()).append(_Failure(exc, applied))

    def on_call(self, method: str, callback: Callable[[], None]) -> None:
        """Callback przy każdym wywołaniu `method`, tuż przed jej skutkiem (po zapisie w `calls`)."""
        self._require_method(method)
        self._callbacks.setdefault(method, []).append(callback)

    def trigger_stop(self, deal_id: str, level: Decimal) -> None:
        """Stop u brokera wykonał się po `level` (przy luce może być gorszy niż stop_level)."""
        pos = self.positions.pop(deal_id, None)
        if pos is None:
            raise ValueError(f"trigger_stop: no open position {deal_id!r}")
        self._closed.append(ClosedDeal(deal_id, pos.epic, level, "STOP", self._clock.now_utc()))

    def add_working_order(self, epic: str, side: Side, size: Decimal, level: Decimal) -> WorkingOrder:
        """Umieszcza zlecenie oczekujące u brokera (np. obce, złożone ręcznie); dowolny instrument."""
        order = WorkingOrder(f"WO-{self._order_seq + 1}", epic, side, size, level)  # błędne dane nie zużywają id
        self._order_seq += 1
        self.working_orders.append(order)
        return order

    def expire_session(self) -> None:
        """Do następnego `login()` każda metoda brokera (także `keepalive`) rzuca `SessionExpired`."""
        self._session_expired = True

    def set_market_closed(self, flag: bool) -> None:
        """Zamknięty rynek: `open_position` i `close_position` rzucają `MarketClosed`; odczyty działają."""
        self._market_closed = flag

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
            rules = self._rules
        return rules

    async def quote(self, epic: str) -> Quote:
        with self._call("quote"):
            self._require_epic(epic)
            q = self._current_quote()
        return q

    async def daily_candles(self, epic: str, count: int) -> list[Candle]:
        with self._call("daily_candles"):
            self._require_epic(epic)
            if count < 1:
                raise ValueError(f"daily_candles: count must be positive, got {count}")
            candles = self._daily[-count:]
        return candles

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]:
        with self._call("candles_5m"):
            self._require_epic(epic)
            candles = [c for c in self._candles_5m if start <= c.start < end]
        return candles

    async def open_position(self, req: EntryRequest) -> EntryAck:
        with self._call("open_position"):
            self._require_epic(req.epic)
            self._require_market_open()
            self._validate_entry(req)
            target = self._netting_target(req)
            q = self._current_quote()
            if req.side is Side.BUY:
                fill = q.ask + self.fill_slippage
                stop = fill - req.stop_distance
            else:
                fill = q.bid - self.fill_slippage
                stop = fill + req.stop_distance
            size = req.size * self.fill_size_factor
            self._ref_seq += 1
            reference = f"REF-{self._ref_seq}"
            if target is None:
                self._deal_seq += 1
                deal_id = f"DEAL-{self._deal_seq}"
                self.positions[deal_id] = OpenPosition(
                    deal_id, req.epic, req.side, size, fill, stop, self._clock.now_utc()
                )
            else:
                deal_id = target.deal_id
                total = target.size + size
                level = (target.size * target.open_level + size * fill) / total
                self.positions[deal_id] = replace(target, size=total, open_level=level)
            self._references[reference] = deal_id
        return EntryAck(reference)

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        with self._call("confirm"):
            deal_id = self._references.get(deal_reference)
            pos = self.positions.get(deal_id) if deal_id is not None else None
        return pos

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
            self._require_market_open()
            pos = self._require_position(deal_id)
            q = self._current_quote()
            exit_level = q.bid if pos.side is Side.BUY else q.ask
            del self.positions[deal_id]
            self._closed.append(ClosedDeal(deal_id, pos.epic, exit_level, "MANUAL", self._clock.now_utc()))

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        with self._call("set_stop"):
            pos = self._require_position(deal_id)
            q = self._current_quote()
            min_distance = self._rules.min_stop_distance
            if pos.side is Side.BUY:
                allowed = stop_level <= q.bid - min_distance
            else:
                allowed = stop_level >= q.ask + min_distance
            if not allowed:
                raise BrokerRejected("MIN_STOP_DISTANCE")
            self.positions[deal_id] = replace(pos, stop_level=stop_level)

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        with self._call("closed_since"):
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
        for callback in list(self._callbacks.get(method, ())):
            callback()
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

    def _require_own_data(self, epic: str) -> None:
        if epic != self._rules.epic:
            raise ValueError(f"FakeBroker simulates only {self._rules.epic!r}, got data for {epic!r}")

    def _sorted_own(self, candles: Iterable[Candle]) -> list[Candle]:
        result = sorted(candles, key=lambda c: c.start)
        for c in result:
            self._require_own_data(c.epic)
        return result

    def _require_epic(self, epic: str) -> None:
        if epic != self._rules.epic:
            raise BrokerRejected("EPIC_NOT_FOUND")

    def _require_market_open(self) -> None:
        if self._market_closed:
            raise MarketClosed(f"market {self._rules.epic} is closed")

    def _require_position(self, deal_id: str) -> OpenPosition:
        pos = self.positions.get(deal_id)
        if pos is None:
            raise BrokerRejected("POSITION_NOT_FOUND")
        return pos

    def _validate_entry(self, req: EntryRequest) -> None:
        r = self._rules
        if req.size < r.min_size:
            raise BrokerRejected("MIN_SIZE")
        if (req.size - r.min_size) % r.size_step != 0:
            raise BrokerRejected("SIZE_STEP")
        if req.stop_distance <= 0 or req.stop_distance < r.min_stop_distance:
            raise BrokerRejected("MIN_STOP_DISTANCE")

    def _netting_target(self, req: EntryRequest) -> OpenPosition | None:
        if not self._netting:
            return None
        target = next((p for p in self.positions.values() if p.epic == req.epic), None)
        if target is not None and target.side is not req.side:
            raise NotImplementedError("FakeBroker does not simulate netting of an opposite-side entry")
        return target

    def _current_quote(self) -> Quote:
        if self._quote_fn is None:
            return self._quote
        q = self._quote_fn(self._clock.now_utc())
        self._require_own_data(q.epic)
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
            r = self._rules
            margin = sum(
                (p.size * p.open_level * r.value_per_point * r.margin_rate for p in self.positions.values()),
                Decimal(0),
            )
        return AccountInfo(acc.account_id, acc.currency, acc.status, acc.equity, acc.equity - margin, margin)
