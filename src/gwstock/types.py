"""Typy domenowe wspólne dla rdzenia i adapterów brokera.

Zasady (kontrolowane przy konstrukcji, żeby błąd adaptera nie przeszedł po cichu):
- kwoty, ceny, wielkości i ułamki to skończone `Decimal` (float jest odrzucany);
- znaczniki czasu są świadome strefy i w UTC;
- wszystkie dataclasses są niemutowalne.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, IntEnum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(str, Enum):
    INTENT = "INTENT"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NOT_PLACED = "NOT_PLACED"
    WORKING = "WORKING"
    PARTIAL = "PARTIAL"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class PositionState(str, Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    BLOCKED = "BLOCKED"


class KillLevel(IntEnum):
    NONE = 0
    SOFT = 1
    HARD = 2


# Przyczyny zamknięcia: stop u brokera, zamknięcie wywołaniem (silnik lub ręcznie), wymuszone przez brokera.
CLOSE_REASONS = frozenset({"STOP", "MANUAL", "FORCED"})


def _require_decimal(owner: object, name: str, *, optional: bool = False) -> None:
    value = getattr(owner, name)
    if value is None and optional:
        return
    where = f"{type(owner).__name__}.{name}"
    if not isinstance(value, Decimal):
        raise TypeError(f"{where}: expected Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{where}: expected a finite Decimal, got {value!r}")


def _require_utc(owner: object, name: str) -> None:
    value = getattr(owner, name)
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{type(owner).__name__}.{name}: expected a timezone-aware UTC datetime, got {value!r}")


def _require_side(owner: object) -> None:
    side = getattr(owner, "side")
    if not isinstance(side, Side):
        raise TypeError(f"{type(owner).__name__}.side: expected Side, got {side!r}")


@dataclass(frozen=True)
class Quote:
    """Notowanie instrumentu.

    `ts` to chwila (na zegarze silnika), w której obserwowana para (bid, ask) ostatnio się ZMIENIŁA,
    a nie chwila odczytu: zamrożone notowanie starzeje się, więc uruchamia straż ciszy notowań.
    """

    epic: str
    bid: Decimal
    ask: Decimal
    ts: datetime

    def __post_init__(self) -> None:
        _require_decimal(self, "bid")
        _require_decimal(self, "ask")
        _require_utc(self, "ts")
        if self.bid > self.ask:
            raise ValueError(f"Quote.bid: crossed quote, bid {self.bid} > ask {self.ask}")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class Candle:
    """Świeca o cenach mid; `start` to początek świecy w UTC."""

    epic: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        _require_utc(self, "start")
        for name in ("open", "high", "low", "close"):
            _require_decimal(self, name)


@dataclass(frozen=True)
class InstrumentRules:
    """Reguły instrumentu od brokera.

    `stop_step` to krok odległości stopu w punktach (DE40: `minStepDistance` = 0,1; hipoteza, że dotyczy stopu,
    do potwierdzenia na próbie `smoke`). 0 oznacza brak kroku, czyli stop nie jest zaokrąglany.
    """

    epic: str
    min_size: Decimal
    size_step: Decimal
    value_per_point: Decimal
    margin_rate: Decimal
    min_stop_distance: Decimal
    stop_step: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("min_size", "size_step", "value_per_point", "margin_rate", "min_stop_distance", "stop_step"):
            _require_decimal(self, name)
        if self.stop_step < 0:
            raise ValueError(f"InstrumentRules.stop_step: expected >= 0, got {self.stop_step}")


@dataclass(frozen=True)
class OpenPosition:
    """Pozycja otwarta u brokera. `stop_level=None` oznacza brak stopu.

    `stop_derived=True`: broker nie podał stopu, a adapter wyliczył go z własnego żądania (poziom ∓ stopDistance).
    """

    deal_id: str
    epic: str
    side: Side
    size: Decimal
    open_level: Decimal
    stop_level: Decimal | None
    opened_at: datetime
    stop_derived: bool = False

    def __post_init__(self) -> None:
        _require_side(self)
        _require_decimal(self, "size")
        _require_decimal(self, "open_level")
        _require_decimal(self, "stop_level", optional=True)
        _require_utc(self, "opened_at")


@dataclass(frozen=True)
class WorkingOrder:
    """Zlecenie oczekujące u brokera (LIMIT/STOP); v1 ich nie składa, tylko wykrywa obce."""

    order_id: str
    epic: str
    side: Side
    size: Decimal
    level: Decimal

    def __post_init__(self) -> None:
        _require_side(self)
        _require_decimal(self, "size")
        _require_decimal(self, "level")


@dataclass(frozen=True)
class ClosedDeal:
    deal_id: str
    epic: str
    exit_level: Decimal
    reason: str
    ts: datetime

    def __post_init__(self) -> None:
        _require_decimal(self, "exit_level")
        _require_utc(self, "ts")
        if self.reason not in CLOSE_REASONS:
            raise ValueError(f"ClosedDeal.reason: expected one of {sorted(CLOSE_REASONS)}, got {self.reason!r}")


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    currency: str
    status: str
    equity: Decimal
    available: Decimal
    margin_used: Decimal

    def __post_init__(self) -> None:
        for name in ("equity", "available", "margin_used"):
            _require_decimal(self, name)


@dataclass(frozen=True)
class EntryRequest:
    """Wejście rynkowe ze stopem dołączonym atomowo (`stop_distance` w punktach od ceny wykonania)."""

    client_id: str
    epic: str
    side: Side
    size: Decimal
    stop_distance: Decimal

    def __post_init__(self) -> None:
        _require_side(self)
        _require_decimal(self, "size")
        _require_decimal(self, "stop_distance")


@dataclass(frozen=True)
class EntryAck:
    deal_reference: str


class BrokerError(Exception):
    """Błąd po stronie brokera lub połączenia (baza wszystkich wyjątków brokera)."""


class BrokerTimeout(BrokerError):
    """Brak odpowiedzi w czasie: NIE wiadomo, czy broker wykonał operację."""


class BrokerRejected(BrokerError):
    """Broker odrzucił operację; `reason` to kod przyczyny."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SessionExpired(BrokerError):
    """Sesja wygasła: potrzebne ponowne logowanie."""


class MarketClosed(BrokerError):
    """Rynek instrumentu jest zamknięty: zlecenia (wejścia i wyjścia) niemożliwe."""
