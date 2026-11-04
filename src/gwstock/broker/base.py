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
        """Wejście rynkowe ze stopem atomowym; zwraca tylko referencję (potwierdzenie przez `confirm`).

        `TargetedEntryRequest`: także cel zysku atomowo (`target_distance` od ceny wykonania, po stronie zysku);
        potwierdzenie i lista pozycji zwracają wtedy `TargetedOpenPosition`.
        """
        ...

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        """Migawka wykonania zlecenia o tej referencji z chwili wypełnienia.

        Pola: deal_id pozycji, open_level = cena wykonania TEGO zlecenia, size = wielkość potwierdzona dla tego
        zlecenia, stop_level, opened_at = czas wypełnienia. Zwracana także wtedy, gdy pozycja została już
        zamknięta, wybita stopem albo zagregowana z inną. None tylko dla nieznanej referencji.

        Stop przy agregacji (netting z inną pozycją tego samego kierunku): `deal_id` migawki to pozycja zagregowana, a
        `stop_level` to stop, który faktycznie chroni tę pozycję zaraz po wypełnieniu (FakeBroker: stop pierwotnej
        pozycji, bez zmian), więc NIE musi być równy `open_level ∓ stop_distance` żądania tego zlecenia. `size` i
        `open_level` zostają wielkością i ceną wykonania tego zlecenia, nie sumą ani średnią pozycji. Wywołujący nie
        zakłada więc, że stop migawki wynika z własnego żądania. Adapter, który nie potrafi jednoznacznie przypisać
        wypełnienia przy agregacji (Capital.com: potwierdzenie bez jednego wpisu OPENED), rzuca `BrokerError` zamiast
        zwracać niepewną migawkę.

        Zlecenie odrzucone przez brokera po przyjęciu (dealStatus REJECTED): `BrokerRejected(reason)`.
        """
        ...

    async def list_positions(self) -> list[OpenPosition]: ...

    async def list_working_orders(self) -> list[WorkingOrder]:
        """Wszystkie zlecenia oczekujące aktywnego rachunku (tylko odczyt: v1 ich nie składa ani nie anuluje)."""
        ...

    async def close_position(self, deal_id: str) -> None: ...

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None: ...

    async def set_protection(self, deal_id: str, stop_level: Decimal, target_level: Decimal | None) -> None:
        """Ustawia stop i cel zysku pozycji RAZEM (jedno żądanie): pasma z celem (R4, projekt §3.1).

        Wysyła zawsze oba poziomy, bo nie ustalono, czy zmiana samego stopu u Capital.com kasuje cel (C5d).
        `target_level=None`: pozycja bez celu. Poziom zbyt bliski rynku albo cel po stronie straty:
        `BrokerRejected`, a pozycja zostaje bez zmian (oba poziomy albo żaden). DE40 v1 używa wyłącznie `set_stop`.
        """
        ...

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        """WSZYSTKIE zamknięcia z `ts >= since` albo `BrokerError`; nigdy lista częściowa.

        Historia Capital.com sięga jednego dnia na zapytanie, więc adapter dzieli zakres na zapytania obejmujące
        najwyżej dobę i łączy wyniki; zakresu dłuższego niż 14 dni odmawia (`BrokerError`), tak samo jak każdej
        niepełnej odpowiedzi. Wywołujący podają prawdziwy początek i nigdy go nie przycinają: brak zamknięcia na
        liście jest dowodem tylko wtedy, gdy lista obejmuje cały zakres. Wywołujący to uzgadnianie (`Reconciler`),
        menedżer zleceń (rozstrzyganie UNKNOWN przed NOT_PLACED), wykonawca (`Executor._settle` po zamknięciu) i
        polecenie właściciela `reset-position`; każdy z nich traktuje wyjątek jak "nie wiadomo", nie jak "brak".
        """
        ...

    async def account(self) -> AccountInfo:
        """Aktywny rachunek sesji."""
        ...

    async def list_accounts(self) -> list[AccountInfo]: ...

    async def select_account(self, account_id: str) -> None: ...
