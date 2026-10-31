"""Menedżer zleceń wejścia (spec §6): INTENT → SUBMITTING → broker → CONFIRMED / REJECTED / UNKNOWN.

Niezmienniki (prawdziwe pieniądze):
- przed wywołaniem brokera dziennik trwale zawiera wiersz zlecenia w stanie SUBMITTING i pozycję ENTERING;
  `JournalWriteError` na tym etapie przerywa wejście, zanim cokolwiek pójdzie do brokera;
- zlecenie wysyłamy najwyżej raz: po każdej niejasności (timeout, wygasła sesja, pusta odpowiedź, nieznany wyjątek)
  stan to UNKNOWN i patrzymy na potwierdzenie i listę pozycji brokera; Capital.com nie zna klucza idempotencji,
  więc ponowna wysyłka mogłaby otworzyć drugą pozycję;
- ochrona przed podwójnym wejściem opiera się wyłącznie na dzienniku (nierozstrzygnięte zlecenie albo pozycja
  inna niż FLAT na tym instrumencie blokuje wejście);
- każda surowa odpowiedź brokera (także wyjątek) trafia do `append_raw` przed zmianą stanu, którą uzasadnia;
- każda zmiana stanu przechodzi przez `check_order_transition` / `check_position_transition`;
- kolejność zapisów po odpowiedzi brokera: najpierw pozycja, na końcu zlecenie. Wiersz zlecenia jest punktem
  zatwierdzenia: dopóki nie zapisano stanu końcowego, zlecenie zostaje nierozstrzygnięte (SUBMITTING/UNKNOWN),
  więc uzgadnianie po restarcie zawsze je znajdzie;
- `JournalWriteError` po wywołaniu brokera propaguje się bez ponowienia i bez kolejnego wywołania brokera
  (dziennik mówi wtedy SUBMITTING lub UNKNOWN, a uzgadnianie traktuje to jak UNKNOWN);
- anulowanie zadania w trakcie wywołania brokera zostawia SUBMITTING (lub UNKNOWN) i jest przekazywane dalej.

Rozstrzyganie UNKNOWN: do `UNKNOWN_CHECKS` rund co `UNKNOWN_CHECK_INTERVAL_S` (zegar silnika). W rundzie, gdy znamy
referencję, najpierw `confirm`, potem zawsze `list_positions`. Pozycja pasuje do zlecenia, gdy zgadza się epic,
kierunek i `opened_at >= created_at` (wielkość nie: broker może potwierdzić inną, zapisujemy faktyczną).
Migawka `confirm` (odpowiedź na NASZĄ referencję) jest przyjmowana, gdy epic i kierunek są zgodne oraz
`opened_at >= created_at` albo jej deal_id jest na liście pozycji; sprzeczna (inny epic lub kierunek) albo
niesprawdzalna przy udanym odczycie listy to BLOCKED. NOT_PLACED tylko po `UNKNOWN_CHECKS` udanych odczytach listy
bez dopasowania; nieudany odczyt nigdy nie jest dowodem braku pozycji (wtedy zlecenie zostaje UNKNOWN).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

from gwstock.broker.base import Broker
from gwstock.clock import Clock
from gwstock.config import RiskConfig
from gwstock.journal import Journal, OrderRecord, PositionRecord
from gwstock.orders.fsm import check_order_transition, check_position_transition
from gwstock.serialization import dump
from gwstock.types import (
    BrokerRejected,
    EntryRequest,
    MarketClosed,
    OpenPosition,
    OrderState,
    PositionState,
)

# Stałe protokołu UNKNOWN ze spec §6 ("po trzech zapytaniach w 30 s"), nie parametry strategii ani ryzyka.
UNKNOWN_CHECKS = 3
UNKNOWN_CHECK_INTERVAL_S = 10

SOURCE_OPEN = "broker.open_position"
SOURCE_CONFIRM = "broker.confirm"
SOURCE_POSITIONS = "broker.list_positions"
SOURCE_EVENT = "order.event"

T = TypeVar("T")


class EntryBlocked(Exception):
    """Wejście odrzucone, zanim cokolwiek zapisano lub wysłano.

    `reason`: UNRESOLVED_ORDER, POSITION_NOT_FLAT albo DUPLICATE_CLIENT_ID; `detail`: czego dotyczy.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class OrderManager:
    """Składa wejścia i rozstrzyga zlecenia UNKNOWN. Jeden wywołujący naraz (wątek decyzyjny silnika)."""

    def __init__(self, journal: Journal, broker: Broker, clock: Clock, cfg: RiskConfig) -> None:
        if not cfg.submit_timeout_s > 0:
            raise ValueError(f"submit_timeout_s must be positive, got {cfg.submit_timeout_s!r}")
        self._journal = journal
        self._broker = broker
        self._clock = clock
        self._cfg = cfg

    # --- wejście --------------------------------------------------------------------------------

    async def submit_entry(self, req: EntryRequest) -> OrderRecord:
        """Składa wejście; zwraca rekord CONFIRMED, REJECTED lub NOT_PLACED (UNKNOWN, gdy nie da się rozstrzygnąć).

        `EntryBlocked` i `ValueError` (brak stopu): nic nie zapisano i nic nie wysłano. `JournalWriteError` przed
        wysyłką: nic nie wysłano; po wysyłce: zlecenie zostaje w dzienniku SUBMITTING/UNKNOWN i rozstrzyga je
        uzgadnianie. W żadnym przypadku wywołujący nie ponawia wejścia.
        """
        if not req.stop_distance > 0:
            raise ValueError(f"entry {req.client_id!r} without a stop is never sent: stop_distance {req.stop_distance}")
        self._guard(req)
        now = self._clock.now_utc()
        order = OrderRecord(
            req.client_id, req.epic, req.side, req.size, req.stop_distance, OrderState.INTENT, None, None, now, now
        )
        self._journal.insert_order(order)
        self._move_position(PositionRecord(req.epic, PositionState.ENTERING))
        order = self._transition(order, OrderState.SUBMITTING)
        # Od tej chwili zlecenie mogło dotrzeć do brokera: dalej tylko CONFIRMED, REJECTED albo UNKNOWN.
        try:
            ack = await self._call(self._broker.open_position(req))
        except BrokerRejected as exc:
            self._record_error(SOURCE_OPEN, order, exc)
            return self._reject(order, exc.reason)
        except MarketClosed as exc:
            self._record_error(SOURCE_OPEN, order, exc)
            return self._reject(order, "MARKET_CLOSED")
        except Exception as exc:  # timeout (także asyncio), wygasła sesja, nieznany błąd: nie wiadomo, czy przyjęto
            self._record_error(SOURCE_OPEN, order, exc)
            return await self._unknown(order, f"CAUSE=open_position:{type(exc).__name__}")
        self._record(SOURCE_OPEN, order, response=ack)
        ref = ack.deal_reference
        try:
            snapshot = await self._call(self._broker.confirm(ref))
        except BrokerRejected as exc:  # odrzucenie odroczone: POST przyjęty, dealStatus REJECTED
            self._record_error(SOURCE_CONFIRM, order, exc, deal_reference=ref)
            return self._reject(order, exc.reason, deal_reference=ref)
        except Exception as exc:
            self._record_error(SOURCE_CONFIRM, order, exc, deal_reference=ref)
            return await self._unknown(order, f"CAUSE=confirm:{type(exc).__name__}", deal_reference=ref)
        self._record(SOURCE_CONFIRM, order, deal_reference=ref, response=snapshot)
        if snapshot is None:  # broker jeszcze nie zna referencji: niejasne, nie ponawiamy wysyłki
            return await self._unknown(order, "CAUSE=confirm:NONE", deal_reference=ref)
        if _contradicts(order, snapshot):
            return self._block(order, "CONFIRM_MISMATCH", deal_reference=ref, deal_ids=[snapshot.deal_id])
        # Bez porównania opened_at z created_at: to odpowiedź na naszą świeżą referencję, a odchyłka zegara
        # brokera nie może zamienić wykonania w UNKNOWN.
        return self._confirm(order, snapshot, "", deal_reference=ref)

    # --- rozstrzyganie UNKNOWN ------------------------------------------------------------------

    async def resolve_unknown(self, order: OrderRecord) -> OrderRecord:
        """Rozstrzyga zlecenie UNKNOWN wyłącznie odczytami brokera (nigdy ponowną wysyłką).

        Wynik: CONFIRMED (pozycja OPEN z faktycznymi danymi brokera), NOT_PLACED (pozycja FLAT) albo UNKNOWN z notatką
        AMBIGUOUS_MATCH / CONFIRM_MISMATCH / CONFIRM_UNVERIFIED (pozycja BLOCKED: decyduje uzgadnianie lub właściciel)
        lub CHECKS_INCOMPLETE (pozycja ENTERING: brak udanych odczytów, rozstrzygnie uzgadnianie).
        """
        if order.state is not OrderState.UNKNOWN:
            raise ValueError(f"resolve_unknown: order {order.order_id!r} is {order.state.value}, expected UNKNOWN")
        clean_checks = 0
        for attempt in range(1, UNKNOWN_CHECKS + 1):
            await self._clock.sleep(UNKNOWN_CHECK_INTERVAL_S)
            snapshot: OpenPosition | None = None
            if order.deal_reference is not None:
                try:
                    snapshot = await self._call(self._broker.confirm(order.deal_reference))
                except BrokerRejected as exc:
                    self._record_error(SOURCE_CONFIRM, order, exc, attempt=attempt)
                    return self._not_placed(order, f"REJECTED_ON_CONFIRM={exc.reason}")
                except Exception as exc:
                    self._record_error(SOURCE_CONFIRM, order, exc, attempt=attempt)
                else:
                    self._record(SOURCE_CONFIRM, order, attempt=attempt, response=snapshot)
            positions: list[OpenPosition] | None
            try:
                positions = await self._call(self._broker.list_positions())
            except Exception as exc:
                self._record_error(SOURCE_POSITIONS, order, exc, attempt=attempt)
                positions = None
            else:
                self._record(SOURCE_POSITIONS, order, attempt=attempt, response=positions)

            if snapshot is not None:
                if _contradicts(order, snapshot):
                    return self._block(order, "CONFIRM_MISMATCH", deal_ids=[snapshot.deal_id])
                listed = positions is not None and any(p.deal_id == snapshot.deal_id for p in positions)
                if snapshot.opened_at >= order.created_at or listed:
                    return self._confirm(order, snapshot, "ADOPTED_AFTER_UNKNOWN VIA=confirm")
                if positions is not None:
                    return self._block(order, "CONFIRM_UNVERIFIED", deal_ids=[snapshot.deal_id])
                continue  # migawki nie da się sprawdzić w tej rundzie: to nie jest dowód braku pozycji
            if positions is None:
                continue
            matches = [p for p in positions if _matches(order, p)]
            if len(matches) == 1:
                return self._confirm(order, matches[0], "ADOPTED_AFTER_UNKNOWN VIA=positions")
            if len(matches) > 1:
                return self._block(order, "AMBIGUOUS_MATCH", deal_ids=[p.deal_id for p in matches])
            clean_checks += 1
        if clean_checks == UNKNOWN_CHECKS:
            return self._not_placed(order, f"NOT_FOUND_AFTER_CHECKS={UNKNOWN_CHECKS}")
        self._event(order, "CHECKS_INCOMPLETE", clean_checks=clean_checks)
        return self._annotate(order, "CHECKS_INCOMPLETE")

    # --- ochrona przed podwójnym wejściem --------------------------------------------------------

    def _guard(self, req: EntryRequest) -> None:
        """Wyłącznie dziennik (nigdy broker): identyfikator, nierozstrzygnięte zlecenia i stan pozycji instrumentu."""
        if self._journal.get_order(req.client_id) is not None:
            raise EntryBlocked("DUPLICATE_CLIENT_ID", req.client_id)
        for o in self._journal.unresolved_orders():
            if o.epic == req.epic:
                raise EntryBlocked("UNRESOLVED_ORDER", f"{o.order_id} {o.state.value}")
        pos = self._journal.current_position(req.epic)
        if pos is not None and pos.state is not PositionState.FLAT:
            raise EntryBlocked("POSITION_NOT_FLAT", f"{req.epic} {pos.state.value}")

    # --- wyniki (pozycja najpierw, zlecenie na końcu) ------------------------------------------

    async def _unknown(self, order: OrderRecord, cause: str, **fields: Any) -> OrderRecord:
        order = self._transition(order, OrderState.UNKNOWN, note=cause, **fields)
        return await self.resolve_unknown(order)

    def _reject(self, order: OrderRecord, reason: str, **fields: Any) -> OrderRecord:
        self._move_position(PositionRecord(order.epic, PositionState.FLAT))
        return self._transition(order, OrderState.REJECTED, note=reason, **fields)

    def _not_placed(self, order: OrderRecord, note: str) -> OrderRecord:
        self._move_position(PositionRecord(order.epic, PositionState.FLAT))
        return self._transition(order, OrderState.NOT_PLACED, note=note)

    def _confirm(self, order: OrderRecord, pos: OpenPosition, note: str, **fields: Any) -> OrderRecord:
        """Pozycja OPEN z FAKTYCZNYMI danymi brokera (wielkość, poziomy, czas), potem zlecenie CONFIRMED."""
        self._move_position(
            PositionRecord(
                pos.epic, PositionState.OPEN, pos.deal_id, pos.side, pos.size, pos.open_level, pos.stop_level,
                pos.opened_at,
            )
        )
        if pos.size != order.size:
            note = f"{note} FILLED_SIZE={pos.size} REQUESTED={order.size}".strip()
        return self._transition(order, OrderState.CONFIRMED, deal_id=pos.deal_id, note=note, **fields)

    def _block(self, order: OrderRecord, note: str, *, deal_ids: list[str], **fields: Any) -> OrderRecord:
        """Sprzeczne dowody: pozycja BLOCKED, zlecenie zostaje UNKNOWN (decyduje uzgadnianie lub właściciel)."""
        self._event(order, note, deal_ids=deal_ids)
        self._move_position(PositionRecord(order.epic, PositionState.BLOCKED))
        if order.state is OrderState.UNKNOWN:
            return self._annotate(order, note, **fields)
        return self._transition(order, OrderState.UNKNOWN, note=note, **fields)

    # --- zapisy -----------------------------------------------------------------------------------

    def _transition(self, order: OrderRecord, new: OrderState, **fields: Any) -> OrderRecord:
        check_order_transition(order.state, new)
        return self._journal.update_order(order.order_id, state=new, updated_at=self._clock.now_utc(), **fields)

    def _annotate(self, order: OrderRecord, note: str, **fields: Any) -> OrderRecord:
        """Zmiana pól bez zmiany stanu (to nie jest przejście automatu)."""
        return self._journal.update_order(order.order_id, note=note, updated_at=self._clock.now_utc(), **fields)

    def _move_position(self, new: PositionRecord) -> None:
        current = self._journal.current_position(new.epic)
        check_position_transition(PositionState.FLAT if current is None else current.state, new.state)
        self._journal.upsert_position(new)

    def _record(self, source: str, order: OrderRecord, **payload: Any) -> None:
        self._journal.append_raw(source, dump({"order_id": order.order_id, **payload}), self._clock.now_utc())

    def _record_error(self, source: str, order: OrderRecord, exc: BaseException, **payload: Any) -> None:
        error: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, BrokerRejected):
            error["reason"] = exc.reason
        self._record(source, order, **payload, **error)

    def _event(self, order: OrderRecord, event: str, **payload: Any) -> None:
        self._record(SOURCE_EVENT, order, event=event, **payload)

    async def _call(self, call: Awaitable[T]) -> T:
        """Wywołanie brokera z limitem `submit_timeout_s` (przekroczenie to `TimeoutError`, czyli niejasność)."""
        return await asyncio.wait_for(call, self._cfg.submit_timeout_s)


def _matches(order: OrderRecord, pos: OpenPosition) -> bool:
    return pos.epic == order.epic and pos.side is order.side and pos.opened_at >= order.created_at


def _contradicts(order: OrderRecord, snapshot: OpenPosition) -> bool:
    return snapshot.epic != order.epic or snapshot.side is not order.side

