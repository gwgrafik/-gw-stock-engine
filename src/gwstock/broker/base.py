"""Interfejs `Broker` i deklaracja zdolności adaptera (spec §3).

Rdzeń zna tylko ten interfejs; adaptery (Capital.com, FakeBroker) go implementują.
Błędy sygnalizowane są wyjątkami z `gwstock.types` (wszystkie dziedziczą po `BrokerError`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from gwstock.types import (
    AccountInfo,
    Candle,
    ClosedDeal,
    EntryAck,
    EntryRequest,
    InstrumentRules,
    OpenPosition,
    Quote,
    WorkingOrder,
)


@dataclass(frozen=True)
class Capabilities:
    """`stop_attach`: "atomic" | "after_fill" | "separate"; `idempotency_key`: rodzaj klucza klienta albo None;
    `keepalive_s`: ważność sesji bez aktywności; `reconcile`: prymityw uzgadniania (np. "positions")."""

    stop_attach: str
    idempotency_key: str | None
    keepalive_s: int
    reconcile: str
    is_cfd: bool


@runtime_checkable
class Broker(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    async def login(self) -> None: ...

    async def keepalive(self) -> None: ...

    async def instrument_rules(self, epic: str) -> InstrumentRules: ...

    async def quote(self, epic: str) -> Quote: ...

    async def daily_candles(self, epic: str, count: int) -> list[Candle]: ...

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]: ...

    async def open_position(self, req: EntryRequest) -> EntryAck:
        """Wejście rynkowe ze stopem atomowym; zwraca tylko referencję (potwierdzenie przez `confirm`)."""
        ...

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        """Pozycja otwarta przez zlecenie o tej referencji; None, gdy referencja nieznana lub pozycji już nie ma."""
        ...

    async def list_positions(self) -> list[OpenPosition]: ...

    async def list_working_orders(self) -> list[WorkingOrder]:
        """Wszystkie zlecenia oczekujące aktywnego rachunku (tylko odczyt: v1 ich nie składa ani nie anuluje)."""
        ...

    async def close_position(self, deal_id: str) -> None: ...

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None: ...

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        """Zamknięcia z `ts >= since`."""
        ...

    async def account(self) -> AccountInfo:
        """Aktywny rachunek sesji."""
        ...

    async def list_accounts(self) -> list[AccountInfo]: ...

    async def select_account(self, account_id: str) -> None: ...
