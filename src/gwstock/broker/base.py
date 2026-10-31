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

    async def daily_candles(self, epic: str, count: int) -> list[Candle]:
        """Ostatnie `count` świec dziennych, od najstarszej. Tylko zakończone: świeca w trakcie nigdy nie wraca."""
        ...

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]:
        """Świece 5-minutowe z `start <= c.start < end`, od najstarszej.

        Tylko zakończone (c.start + 5 min <= teraz): świeca w trakcie formowania nigdy nie jest zwracana.
        """
        ...

    async def open_position(self, req: EntryRequest) -> EntryAck:
        """Wejście rynkowe ze stopem atomowym; zwraca tylko referencję (potwierdzenie przez `confirm`)."""
        ...

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        """Migawka wykonania zlecenia o tej referencji z chwili wypełnienia.

        Pola: deal_id pozycji, open_level = cena wykonania TEGO zlecenia, size = wielkość potwierdzona dla tego
        zlecenia, stop_level, opened_at = czas wypełnienia. Zwracana także wtedy, gdy pozycja została już
        zamknięta, wybita stopem albo zagregowana z inną. None tylko dla nieznanej referencji.
        Zlecenie odrzucone przez brokera po przyjęciu (dealStatus REJECTED): `BrokerRejected(reason)`.
        """
        ...

    async def list_positions(self) -> list[OpenPosition]: ...

    async def list_working_orders(self) -> list[WorkingOrder]:
        """Wszystkie zlecenia oczekujące aktywnego rachunku (tylko odczyt: v1 ich nie składa ani nie anuluje)."""
        ...

    async def close_position(self, deal_id: str) -> None: ...

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None: ...

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        """WSZYSTKIE zamknięcia z `ts >= since` albo `BrokerError`; nigdy lista częściowa.

        Historia Capital.com sięga jednego dnia na zapytanie, więc adapter dzieli zakres na zapytania obejmujące
        najwyżej dobę i łączy wyniki; zakresu dłuższego niż 14 dni odmawia (`BrokerError`), tak samo jak każdej
        niepełnej odpowiedzi. Wywołujący (uzgadnianie) podaje prawdziwy początek i nigdy go nie przycina: brak
        zamknięcia na liście jest dowodem tylko wtedy, gdy lista obejmuje cały zakres.
        """
        ...

    async def account(self) -> AccountInfo:
        """Aktywny rachunek sesji."""
        ...

    async def list_accounts(self) -> list[AccountInfo]: ...

    async def select_account(self, account_id: str) -> None: ...
