"""Uzgadnianie z brokerem (spec §7): broker jest źródłem prawdy, a dziennik dogania jego stan dla NASZEGO instrumentu.

Przebieg `run_once` (zawsze w tej kolejności):
1. bramka wejścia: gdy wejście jest w toku (`entry_gate()` zwraca True), przebieg nie STARTUJE (bez żadnego
   odczytu i zapisu; pozycja pojawiająca się w trakcie wejścia wyglądałaby na obcą);
2. odczyty: migawka dziennika (zlecenia nierozstrzygnięte i pozycja instrumentu, znaczniki), potem broker:
   `list_positions`, `list_working_orders` i tylko w razie potrzeby `closed_since`; każda odpowiedź brokera trafia
   do `append_raw` (serializacja `dump`) przed interpretacją, a wyjątek odczytu do `broker.error` przed propagacją.
   `closed_since` dostaje PRAWDZIWY początek (chwila odniesienia − `HISTORY_MARGIN`, bez przycinania do doby);
   kontrakt brokera: cała historia od `since` albo `BrokerError` (adapter dzieli zakres na zapytania po dobie,
   limit 14 dni), więc niepełne okno historii przerywa przebieg i nigdy nie staje się dowodem braku zamknięcia
   (zlecenie UNKNOWN nie przejdzie wtedy w NOT_PLACED, zamknięcie nie zostanie pominięte);
   Wyjątek brokera (`BrokerError`, w tym `SessionExpired`) albo dziennika (`JournalWriteError`) przerywa przebieg,
   zanim zmieni się jakikolwiek stan: nieudany odczyt nigdy nie jest dowodem braku pozycji ani pustym odczytem;
3. decyzja według macierzy niżej, w całości przed pierwszym zapisem stanu;
4. ponowne sprawdzenie (bez `await` między nim a pierwszym zapisem, więc atomowe w asyncio): bramka wejścia,
   zlecenia nierozstrzygnięte i pozycja instrumentu muszą być takie jak w migawce; inaczej wynik `skipped=True`
   bez żadnego zapisu, alarmu i zmiany znaczników (wejście lub wyjście zmieniło dziennik w trakcie odczytu
   brokera; plan opiera się na nieaktualnej migawce). Bramka nie pozwala więc przebiegowi wystartować w trakcie
   wejścia, a ponowne sprawdzenie nie pozwala mu zapisać ponad wejściem lub wyjściem, które zaszło w trakcie;
   silnik (Zadanie 14) dodatkowo szereguje oba kierunki jedną blokadą;
5. kill switch miękki (przed zapisami: awaria dziennika nie połknie alarmu), zdarzenia `reconcile.event` (ślad
   decyzji zostaje także przy błędzie księgowania), księgowanie zamknięcia (jedyny zapis z `await`; idempotentny po
   `deal_id`, po nim migawka jest sprawdzana jeszcze raz: zmiana → `SKIPPED_STATE_CHANGED_AFTER_BOOKING` z
   zaksięgowanym zamknięciem w `closed`), zapisy: naprawa SUBMITTING -> UNKNOWN i potwierdzenie z dziennika,
   licznik pustych odczytów, pozycja,
   zlecenia zamykane na podstawie pozycji (wiersz zlecenia na końcu, jak w menedżerze), znacznik historii,
   znacznik rozbieżności. Przerwanie między zapisami zostawia stan, który kolejny przebieg rozpoznaje i dokańcza.

Macierz (J = pozycja w dzienniku, B = pozycje brokera na naszym instrumencie, D = deal_id zapisany w J):
- INTENT: nic nie wysłano → NOT_PLACED ("ABANDONED_BEFORE_SEND"); SUBMITTING → UNKNOWN, dalej jak UNKNOWN;
- UNKNOWN, J z D (OPEN/EXITING/BLOCKED; przerwanie między zapisami menedżera) → CONFIRMED(D), dalej macierz pozycji;
- UNKNOWN, J ENTERING, jedno dopasowanie `match_entry_positions` → J OPEN z faktycznymi danymi brokera, CONFIRMED;
  więcej dopasowań → BLOCKED + kill AMBIGUOUS_MATCH; brak dopasowania przy niepustym B (albo J FLAT i niepuste B:
  sprzeczność z decyzją menedżera) → BLOCKED + kill FOREIGN_POSITION;
- UNKNOWN, B puste: licznik pustych odczytów; po 3 i co najmniej 30 s od INTENT: historia od INTENT − 1 min;
  zamknięcie na naszym instrumencie → BLOCKED + kill UNMATCHED_CLOSED_DEAL, inaczej NOT_PLACED (J ENTERING → FLAT);
- UNKNOWN, J BLOCKED bez D: decyduje właściciel; więcej niż jedno zlecenie UNKNOWN → BLOCKED + kill;
- brak zlecenia, J bez naszej pozycji: niepuste B → BLOCKED (bez pól) + kill FOREIGN_POSITION, nigdy nie dotykamy;
  puste B → FLAT (także samoczynne BLOCKED → FLAT, gdy obca zniknęła); J OPEN/EXITING albo BLOCKED z polami
  pozycji, ale bez D → BLOCKED do decyzji właściciela (nie da się sprawdzić ani zaksięgować);
- D w B: zgodne kierunek i wielkość → odśwież pola (OPEN/EXITING bez zmiany stanu, BLOCKED → OPEN); różne →
  BLOCKED z polami dziennika + kill SIDE_MISMATCH / SIZE_MISMATCH; brak stopu → `unprotected` (stop ustawia wykonawca);
- D poza B: historia od max(znacznik, otwarcie) − 1 min (bez obu: brak odczytu, BLOCKED); jedno zamknięcie D →
  zaksięguj, FLAT; więcej → BLOCKED + kill AMBIGUOUS_CLOSURE; brak → BLOCKED (zamknięcia nie wymyślamy);
- inna pozycja w B obok D → BLOCKED + kill FOREIGN_POSITION; zlecenie oczekujące na naszym instrumencie →
  `working_orders` + kill FOREIGN_WORKING_ORDER (nigdy nie anulujemy);
- rozbieżność oczekująca (zlecenie nierozstrzygnięte albo J BLOCKED) dłużej niż `mismatch_tolerance_s` → kill
  MISMATCH_TIMEOUT (znacznik `mismatch_since` w dzienniku).

Niezmienniki (prawdziwe pieniądze):
- uzgadnianie tylko CZYTA u brokera: nie zamyka pozycji, nie ustawia stopu, nie anuluje zleceń;
- pozycji, której nie da się udowodnić jako naszej (nie wynika z naszego zlecenia ani z zapisu w dzienniku),
  nigdy nie przyjmujemy: BLOCKED i kill switch miękki; sprzeczne dowody tak samo;
- BLOCKED z nierozstrzygniętym zleceniem albo z polami pozycji bez deal_id rozstrzyga właściciel; pozostałe BLOCKED
  uzgadnianie zdejmuje samo, gdy broker i dziennik znów się zgadzają; kill switch zostaje do decyzji właściciela;
- zamknięcie księgujemy przed przejściem do FLAT (błąd księgowania zostawia pozycję i powtarza się w następnym
  przebiegu), zamknięcia nigdy nie wymyślamy;
- każda zmiana stanu przechodzi przez `check_order_transition` / `check_position_transition`; samo odświeżenie pól
  pozycji bez zmiany stanu nie jest przejściem automatu.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, TypeVar

from gwstock.broker.base import Broker
from gwstock.clock import Clock
from gwstock.config import RiskConfig
from gwstock.journal import Journal, OrderRecord, PositionRecord
from gwstock.orders.fsm import check_order_transition, check_position_transition
from gwstock.orders.manager import HISTORY_MARGIN, UNKNOWN_CHECK_INTERVAL_S, UNKNOWN_CHECKS, match_entry_positions
from gwstock.serialization import dump
from gwstock.types import BrokerRejected, ClosedDeal, OpenPosition, OrderState, PositionState, WorkingOrder

SOURCE_POSITIONS = "broker.list_positions"
SOURCE_WORKING_ORDERS = "broker.list_working_orders"
SOURCE_CLOSED = "broker.closed_since"
SOURCE_EVENT = "reconcile.event"
SOURCE_ERROR = "broker.error"  # wyjątek odczytu brokera (typ i treść), zapisany przed propagacją

META_MISMATCH_SINCE = "mismatch_since"  # ISO-8601 pierwszego wykrycia rozbieżności oczekującej; "" = brak
META_CLOSED_MARKER = "closed_marker"  # ISO-8601 czasu ostatniego zaksięgowanego zamknięcia
META_EMPTY_READS = "unknown_empty_reads:{}"  # liczba kolejnych udanych odczytów bez pozycji dla zlecenia UNKNOWN

# Protokół UNKNOWN ze spec §6 ("po trzech zapytaniach w 30 s jej nie ma"): te same stałe co w menedżerze zleceń.
EMPTY_READS_TO_NOT_PLACED = UNKNOWN_CHECKS
MIN_UNKNOWN_AGE = timedelta(seconds=UNKNOWN_CHECKS * UNKNOWN_CHECK_INTERVAL_S)
# `HISTORY_MARGIN` (zapas początku zakresu historii na różnicę zegarów) jest jedną stałą z `gwstock.orders.manager`,
# wspólną dla menedżera i uzgadniania. Początku nigdy nie przycinamy: `Broker.closed_since` zwraca całą historię od
# `since` albo rzuca wyjątek.

RECOVERED_FROM_SUBMITTING = "RECOVERED_FROM_SUBMITTING"

_O = OrderState
_P = PositionState
_HELD = frozenset({_P.OPEN, _P.EXITING, _P.BLOCKED})  # stany, w których zapisany deal_id jest naszą pozycją

T = TypeVar("T")


class KillSink(Protocol):
    """Odbiorca alarmu: kill switch miękki (spełnia go `KillSwitch`); wielokrotne wywołanie jest nieszkodliwe."""

    def soft(self, reason: str) -> None: ...


class TradeBooker(Protocol):
    """Księguje zamknięcie naszej pozycji (spełnia go wykonawca); idempotentny po `closed.deal_id`."""

    async def book(self, position: PositionRecord, closed: ClosedDeal) -> None: ...


@dataclass(frozen=True)
class ReconcileResult:
    """Wynik przebiegu.

    `blocked`: pozycja instrumentu jest po przebiegu BLOCKED. `notes`: kody decyzji ze szczegółami (pierwsze słowo
    to kod). `closed`: zamknięcia naszej pozycji przetworzone w tym przebiegu. `unprotected`: NASZE pozycje u brokera
    bez stopu (widok brokera: kierunek i poziomy brokera). `working_orders`: zlecenia oczekujące na naszym
    instrumencie. `skipped`: przebieg nie zapisał stanu, bo wejście było w toku (`SKIPPED_ENTRY_IN_FLIGHT`) albo
    dziennik zmienił się w trakcie odczytu brokera (`SKIPPED_STATE_CHANGED`); pozostałe pola są wtedy puste.
    Wyjątek: `SKIPPED_STATE_CHANGED_AFTER_BOOKING`: zamknięcie ZOSTAŁO zaksięgowane (jest w `closed`), a zmianę
    wykryto dopiero po księgowaniu, więc reszta zapisów (FLAT, znaczniki) została pominięta.
    """

    blocked: bool
    notes: list[str]
    closed: list[ClosedDeal]
    unprotected: list[OpenPosition]
    skipped: bool = False
    working_orders: list[WorkingOrder] = field(default_factory=list)


@dataclass(frozen=True)
class _OrderWrite:
    """Zapis zlecenia: `order` to stan PRZED zapisem; `new=None` zmienia tylko notatkę (bez przejścia)."""

    order: OrderRecord
    new: OrderState | None
    note: str
    deal_id: str | None = None


@dataclass
class _Plan:
    """Decyzje jednego przebiegu, wyliczone w całości przed pierwszym zapisem stanu."""

    notes: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)
    orders_before: list[_OrderWrite] = field(default_factory=list)  # przed pozycją (nie zależą od niej)
    orders_after: list[_OrderWrite] = field(default_factory=list)  # po pozycji (wiersz zlecenia na końcu)
    position: PositionRecord | None = None  # stan docelowy; None: bez zmiany
    book: tuple[PositionRecord, ClosedDeal] | None = None
    empty_reads: tuple[str, int] | None = None
    mismatch_since: str | None = None  # None: bez zmiany
    closed: list[ClosedDeal] = field(default_factory=list)
    unprotected: list[OpenPosition] = field(default_factory=list)
    resolved: set[str] = field(default_factory=set)

    def record(self, code: str, **details: Any) -> None:
        self.events.append({"event": code, **details})
        self.notes.append(" ".join([code, *(f"{k}={_text(v)}" for k, v in details.items())]))

    def kill(self, reason: str) -> None:
        if reason not in self.kills:
            self.kills.append(reason)


class Reconciler:
    """Uzgadnia dziennik z brokerem dla jednego instrumentu (`epic`); patrz docstring modułu."""

    def __init__(
        self,
        journal: Journal,
        broker: Broker,
        clock: Clock,
        kill: KillSink,
        cfg: RiskConfig,
        epic: str,
        booker: TradeBooker | None = None,
        entry_gate: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(epic, str) or not epic:
            raise ValueError(f"epic: expected a non-empty instrument name, got {epic!r}")
        self._journal = journal
        self._broker = broker
        self._clock = clock
        self._kill = kill
        self._cfg = cfg
        self._epic = epic
        self._booker = booker
        self._entry_gate = entry_gate

    @property
    def booker(self) -> TradeBooker | None:
        """Księgujący zamknięcia (tylko odczyt): silnik w trybie live sprawdza, że to jego wykonawca."""
        return self._booker

    async def run_once(self) -> ReconcileResult:
        """Jeden przebieg uzgadniania. Wyjątki brokera i dziennika z odczytów propagują się bez zmiany stanu.

        `skipped=True`: wejście w toku (przed startem albo przed zapisami) albo dziennik zmienił się w trakcie
        odczytu brokera; nic nie zapisano poza surowymi odpowiedziami brokera (wyjątek: zmiana wykryta dopiero po
        księgowaniu; zamknięcie jest wtedy zaksięgowane i zwrócone w `closed`, a księga jest idempotentna po `deal_id`).
        """
        if self._gate_closed():
            return _skipped("SKIPPED_ENTRY_IN_FLIGHT")

        # 1. odczyty: migawka dziennika, potem broker (surowa odpowiedź zapisana przed interpretacją)
        orders, stored = self._snapshot()
        current = stored or PositionRecord(self._epic, _P.FLAT)
        positions = await self._read("list_positions", self._broker.list_positions())
        self._raw(SOURCE_POSITIONS, {"response": positions})
        working = await self._read("list_working_orders", self._broker.list_working_orders())
        self._raw(SOURCE_WORKING_ORDERS, {"response": working})
        mine = [p for p in positions if p.epic == self._epic]
        working_ours = [w for w in working if w.epic == self._epic]

        # 2. decyzja (bez zapisów stanu)
        plan = _Plan()
        live = self._plan_unresolved(plan, orders)
        ours, generic, final_check = self._plan_live_order(plan, live, current, mine)
        closures = await self._read_history(plan, ours if generic else None, mine, final_check)
        if final_check is not None:
            self._plan_final_check(plan, final_check, current, closures)
        if generic:
            self._plan_position(plan, current, ours, mine, closures)
        if working_ours:
            plan.kill("FOREIGN_WORKING_ORDER")
            plan.record("FOREIGN_WORKING_ORDER", order_ids=[w.order_id for w in working_ours])
        final = plan.position or current
        unresolved_left = any(o.order_id not in plan.resolved for o in live)
        self._plan_mismatch(plan, pending=unresolved_left or final.state is _P.BLOCKED)

        # 3. ponowne sprawdzenie migawki, potem alarm i zapisy (bez `await` od sprawdzenia do pierwszego zapisu)
        if self._gate_closed():
            return _skipped("SKIPPED_ENTRY_IN_FLIGHT")
        if self._snapshot() != (orders, stored):
            return _skipped("SKIPPED_STATE_CHANGED")
        if not await self._apply(plan, current, (orders, stored)):
            return ReconcileResult(False, ["SKIPPED_STATE_CHANGED_AFTER_BOOKING"], plan.closed, [], skipped=True)
        return ReconcileResult(
            final.state is _P.BLOCKED, plan.notes, plan.closed, plan.unprotected, working_orders=working_ours
        )

    def _gate_closed(self) -> bool:
        return self._entry_gate is not None and bool(self._entry_gate())

    def _snapshot(self) -> tuple[list[OrderRecord], PositionRecord | None]:
        """Zlecenia nierozstrzygnięte i zapisana pozycja NASZEGO instrumentu (to, na czym opiera się plan)."""
        orders = [o for o in self._journal.unresolved_orders() if o.epic == self._epic]
        return orders, self._journal.current_position(self._epic)

    async def _read(self, call: str, pending: Awaitable[T]) -> T:
        """Odczyt brokera; wyjątek trafia do `broker.error` (typ i treść) i propaguje się bez innych zapisów."""
        try:
            return await pending
        except Exception as exc:
            error: dict[str, Any] = {"call": call, "error": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, BrokerRejected):
                error["reason"] = exc.reason
            self._raw(SOURCE_ERROR, error)
            raise

    # --- decyzje: zlecenia ----------------------------------------------------------------------

    def _plan_unresolved(self, plan: _Plan, orders: list[OrderRecord]) -> list[OrderRecord]:
        """INTENT -> NOT_PLACED (nic nie wysłano), SUBMITTING -> UNKNOWN; zwraca zlecenia UNKNOWN (stan po naprawie)."""
        live = []
        for o in orders:
            if o.state is _O.INTENT:
                plan.orders_after.append(_OrderWrite(o, _O.NOT_PLACED, _note("ABANDONED_BEFORE_SEND", _carry(o.note))))
                plan.resolved.add(o.order_id)
                plan.record("ABANDONED_BEFORE_SEND", order_id=o.order_id)
            elif o.state is _O.SUBMITTING:
                note = _note(RECOVERED_FROM_SUBMITTING, _carry(o.note))
                plan.orders_before.append(_OrderWrite(o, _O.UNKNOWN, note))
                plan.record(RECOVERED_FROM_SUBMITTING, order_id=o.order_id)
                live.append(replace(o, state=_O.UNKNOWN, note=note))
            else:
                live.append(o)
        return live

    def _plan_live_order(
        self, plan: _Plan, live: list[OrderRecord], current: PositionRecord, mine: list[OpenPosition]
    ) -> tuple[PositionRecord | None, bool, OrderRecord | None]:
        """Zlecenie UNKNOWN na naszym instrumencie. Zwraca (nasza pozycja, czy dalej macierz pozycji, zlecenie do
        ostatecznego sprawdzenia historii przed NOT_PLACED)."""
        ours = _ours(current)
        if not live:
            return ours, True, None
        if len(live) > 1:  # ochrona przed podwójnym wejściem zawiodła: nie zgadujemy, które jest czyje
            plan.position = replace(current, state=_P.BLOCKED)
            plan.kill("MULTIPLE_UNRESOLVED_ORDERS")
            plan.record("MULTIPLE_UNRESOLVED_ORDERS", order_ids=[o.order_id for o in live])
            return None, False, None
        order = live[0]
        if ours is not None:
            # Menedżer zapisał pozycję z danych brokera, a przerwanie przyszło przed zapisem zlecenia.
            note = _note("ADOPTED_BY_RECONCILER", "VIA=journal", _carry(order.note))
            plan.orders_before.append(_OrderWrite(order, _O.CONFIRMED, note, ours.deal_id))
            plan.resolved.add(order.order_id)
            plan.record("ADOPTED_BY_RECONCILER", order_id=order.order_id, deal_id=ours.deal_id, via="journal")
            return ours, True, None
        if current.state is _P.BLOCKED:
            plan.record("BLOCKED_AWAITING_OWNER", order_id=order.order_id)
            return None, False, None
        matches = match_entry_positions(order, mine) if current.state is _P.ENTERING else []
        if len(matches) == 1:
            pos = matches[0]
            filled = f"FILLED_SIZE={pos.size} REQUESTED={order.size}" if pos.size != order.size else ""
            note = _note("ADOPTED_BY_RECONCILER", "VIA=positions", filled, _carry(order.note))
            plan.orders_after.append(_OrderWrite(order, _O.CONFIRMED, note, pos.deal_id))
            plan.resolved.add(order.order_id)
            plan.record("ADOPTED_BY_RECONCILER", order_id=order.order_id, deal_id=pos.deal_id, via="positions")
            return _from_broker(_P.OPEN, pos), True, None
        if len(matches) > 1:
            plan.position = PositionRecord(self._epic, _P.BLOCKED)
            plan.kill("AMBIGUOUS_MATCH")
            plan.record("AMBIGUOUS_MATCH", order_id=order.order_id, deal_ids=[p.deal_id for p in matches])
            self._annotate(plan, order, "AMBIGUOUS_MATCH")
            return None, False, None
        if mine:
            # Pozycja, która nie pasuje do zlecenia (albo dziennik po decyzji menedżera mówi FLAT): nie nasza.
            plan.position = PositionRecord(self._epic, _P.BLOCKED)
            plan.kill("FOREIGN_POSITION")
            plan.record(
                "FOREIGN_POSITION", deal_ids=[p.deal_id for p in mine], order_id=order.order_id,
                journal=current.state,
            )
            return None, False, None
        reads = self._empty_reads(order) + 1
        plan.empty_reads = (META_EMPTY_READS.format(order.order_id), reads)
        old_enough = self._clock.now_utc() - order.created_at >= MIN_UNKNOWN_AGE
        if reads >= EMPTY_READS_TO_NOT_PLACED and old_enough:
            return None, False, order
        plan.record("UNKNOWN_NOT_FOUND", order_id=order.order_id, reads=reads)
        return None, False, None

    def _plan_final_check(
        self, plan: _Plan, order: OrderRecord, current: PositionRecord, closures: list[ClosedDeal]
    ) -> None:
        """Ostatni pusty odczyt: NOT_PLACED, chyba że historia pokazuje zamknięcie na naszym instrumencie po INTENT
        (wejście mogło się wykonać i zostać wybite, zanim ktokolwiek zobaczył pozycję)."""
        earliest = order.created_at - HISTORY_MARGIN
        suspicious = [c for c in closures if c.epic == self._epic and c.ts >= earliest]
        if suspicious:
            plan.position = PositionRecord(self._epic, _P.BLOCKED)
            plan.kill("UNMATCHED_CLOSED_DEAL")
            plan.record("UNMATCHED_CLOSED_DEAL", order_id=order.order_id, deal_ids=[c.deal_id for c in suspicious])
            self._annotate(plan, order, "UNMATCHED_CLOSED_DEAL")
            return
        reads = plan.empty_reads[1] if plan.empty_reads else EMPTY_READS_TO_NOT_PLACED
        if current.state is _P.ENTERING:
            plan.position = PositionRecord(self._epic, _P.FLAT)
        note = _note("NOT_FOUND_BY_RECONCILER", f"READS={reads}", _carry(order.note))
        plan.orders_after.append(_OrderWrite(order, _O.NOT_PLACED, note))
        plan.resolved.add(order.order_id)
        plan.record("NOT_FOUND_BY_RECONCILER", order_id=order.order_id, reads=reads)

    def _annotate(self, plan: _Plan, order: OrderRecord, code: str) -> None:
        if order.note.split()[:1] != [code]:  # ponowny przebieg nie dopisuje kodu drugi raz
            plan.orders_after.append(_OrderWrite(order, None, _note(code, _carry(order.note))))

    # --- decyzje: pozycja -----------------------------------------------------------------------

    def _plan_position(
        self,
        plan: _Plan,
        current: PositionRecord,
        ours: PositionRecord | None,
        mine: list[OpenPosition],
        closures: list[ClosedDeal],
    ) -> None:
        """Macierz §7 dla pozycji, gdy nie ma zlecenia w toku albo zostało przyjęte (`ours`: nasza pozycja)."""
        if ours is None:
            # Pozycja bez deal_id (OPEN/EXITING, albo już BLOCKED z polami pozycji): nie da się jej sprawdzić ani
            # zaksięgować, więc zostaje BLOCKED do decyzji właściciela (nigdy samo do FLAT).
            if current.state in (_P.OPEN, _P.EXITING) or (current.state is _P.BLOCKED and _has_fields(current)):
                plan.position = replace(current, state=_P.BLOCKED)
                plan.record("LOCAL_POSITION_WITHOUT_DEAL", journal=current.state)
                if mine:
                    plan.kill("FOREIGN_POSITION")
                    plan.record("FOREIGN_POSITION", deal_ids=[p.deal_id for p in mine])
            elif mine:
                plan.position = PositionRecord(self._epic, _P.BLOCKED)
                plan.kill("FOREIGN_POSITION")
                plan.record("FOREIGN_POSITION", deal_ids=[p.deal_id for p in mine], journal=current.state)
            else:
                plan.position = PositionRecord(self._epic, _P.FLAT)
                if current.state is not _P.FLAT:
                    plan.record("POSITION_CLEARED", journal=current.state)
            return

        at_broker = next((p for p in mine if p.deal_id == ours.deal_id), None)
        others = [p for p in mine if p.deal_id != ours.deal_id]
        if at_broker is not None:
            if at_broker.side is not ours.side:
                target = replace(ours, state=_P.BLOCKED)  # dziennik zachowuje nasz zapis do decyzji właściciela
                plan.kill("SIDE_MISMATCH")
                plan.record("SIDE_MISMATCH", deal_id=ours.deal_id, journal=ours.side, broker=at_broker.side)
            elif at_broker.size != ours.size:
                target = replace(ours, state=_P.BLOCKED)
                plan.kill("SIZE_MISMATCH")
                plan.record("SIZE_MISMATCH", deal_id=ours.deal_id, journal=ours.size, broker=at_broker.size)
            else:
                state = ours.state if ours.state in (_P.OPEN, _P.EXITING) else _P.OPEN
                target = _from_broker(state, at_broker)
                if current.state is _P.BLOCKED:
                    plan.record("BLOCK_CLEARED", deal_id=ours.deal_id)
            if at_broker.stop_level is None:
                plan.unprotected.append(at_broker)
                plan.record("UNPROTECTED", deal_id=at_broker.deal_id)
        else:
            found = [c for c in closures if c.deal_id == ours.deal_id and c.epic == self._epic]
            if len(found) == 1:
                closed = found[0]
                plan.book = (ours, closed)
                plan.closed.append(closed)
                target = PositionRecord(self._epic, _P.FLAT)
                plan.record(
                    "CLOSURE_FOUND", deal_id=closed.deal_id, reason=closed.reason, exit_level=closed.exit_level,
                    ts=closed.ts, booked=self._booker is not None,
                )
            elif found:
                target = replace(ours, state=_P.BLOCKED)
                plan.kill("AMBIGUOUS_CLOSURE")
                plan.record("AMBIGUOUS_CLOSURE", deal_id=ours.deal_id, count=len(found))
            else:
                target = replace(ours, state=_P.BLOCKED)  # zamknięcia nie wymyślamy: rozbieżność oczekująca
                plan.record("LOCAL_POSITION_MISSING", deal_id=ours.deal_id)
        if others:
            # zachowuje pola naszej pozycji; po zaksięgowaniu (FLAT bez pól) BLOCKED bez pól zdejmie się sam
            target = replace(target, state=_P.BLOCKED)
            plan.kill("FOREIGN_POSITION")
            plan.record("FOREIGN_POSITION", deal_ids=[p.deal_id for p in others], ours=ours.deal_id)
        plan.position = target

    def _plan_mismatch(self, plan: _Plan, *, pending: bool) -> None:
        """Rozbieżność oczekująca (zlecenie nierozstrzygnięte lub pozycja BLOCKED) dłużej niż tolerancja: kill miękki."""
        text = self._journal.get_meta(META_MISMATCH_SINCE)
        since = datetime.fromisoformat(text) if text else None
        now = self._clock.now_utc()
        if not pending:
            if since is not None:
                plan.mismatch_since = ""
            return
        if since is None:
            plan.mismatch_since = now.isoformat()
        elif now - since > timedelta(seconds=self._cfg.mismatch_tolerance_s):
            plan.kill("MISMATCH_TIMEOUT")
            plan.record("MISMATCH_TIMEOUT", since=since)

    # --- odczyty pomocnicze ---------------------------------------------------------------------

    async def _read_history(
        self, plan: _Plan, ours: PositionRecord | None, mine: list[OpenPosition], final_check: OrderRecord | None
    ) -> list[ClosedDeal]:
        """`closed_since` tylko gdy potrzebne: nasza pozycja zniknęła z listy albo zlecenie UNKNOWN ma zostać
        NOT_PLACED. Jeden odczyt od najwcześniejszego PRAWDZIWEGO początku (bez przycinania): niepełną historię
        broker zgłasza wyjątkiem, który przerywa przebieg przed jakąkolwiek zmianą stanu."""
        starts = []
        if ours is not None and not any(p.deal_id == ours.deal_id for p in mine):
            start = self._history_start(ours)
            if start is None:  # nie znamy początku: bez odczytu zamknięcia nie ma dowodu, pozycja zostanie BLOCKED
                plan.record("HISTORY_START_UNKNOWN", deal_id=ours.deal_id)
            else:
                starts.append(start)
        if final_check is not None:
            starts.append(final_check.created_at - HISTORY_MARGIN)
        if not starts:
            return []
        since = min(starts)
        closures = await self._read("closed_since", self._broker.closed_since(since))
        self._raw(SOURCE_CLOSED, {"since": since, "response": closures})
        return closures

    def _history_start(self, ours: PositionRecord) -> datetime | None:
        """Późniejsza z dwóch dolnych granic (znacznik ostatniego zamknięcia, otwarcie pozycji) minus zapas: zamknięcie
        tej pozycji nie może być wcześniejsze. None, gdy nie znamy żadnej z nich."""
        marker = self._journal.get_meta(META_CLOSED_MARKER)
        bounds = [t for t in (datetime.fromisoformat(marker) if marker else None, ours.opened_at) if t is not None]
        return max(bounds) - HISTORY_MARGIN if bounds else None

    def _empty_reads(self, order: OrderRecord) -> int:
        text = self._journal.get_meta(META_EMPTY_READS.format(order.order_id))
        return int(text) if text else 0

    # --- zapisy ---------------------------------------------------------------------------------

    async def _apply(
        self, plan: _Plan, current: PositionRecord, snapshot: tuple[list[OrderRecord], PositionRecord | None]
    ) -> bool:
        """Alarm i zapisy planu. Wywoływane tuż po sprawdzeniu migawki (bez `await` od sprawdzenia do zdarzeń); jedyny
        `await` to księgowanie, po którym migawka jest sprawdzana ponownie (False: zmieniła się, reszta zapisów
        pominięta; zamknięcie jest zaksięgowane, a księga idempotentna)."""
        for reason in plan.kills:
            self._kill.soft(reason)
        for event in plan.events:  # ślad decyzji przed księgowaniem: zostaje także przy jego błędzie
            self._raw(SOURCE_EVENT, event)
        if plan.book is not None and self._booker is not None:
            await self._booker.book(*plan.book)  # błąd: pozycja zostaje, następny przebieg ponowi
            if self._gate_closed() or self._snapshot() != snapshot:
                return False
        for write in plan.orders_before:
            self._write_order(write)
        if plan.empty_reads is not None:
            key, reads = plan.empty_reads
            self._journal.set_meta(key, str(reads))
        if plan.position is not None:
            self._move_position(current, plan.position)
        for write in plan.orders_after:
            self._write_order(write)
        if plan.closed:
            marker = self._journal.get_meta(META_CLOSED_MARKER)
            latest = max(c.ts for c in plan.closed)
            if not marker or datetime.fromisoformat(marker) < latest:
                self._journal.set_meta(META_CLOSED_MARKER, latest.isoformat())
        if plan.mismatch_since is not None:
            self._journal.set_meta(META_MISMATCH_SINCE, plan.mismatch_since)
        return True

    def _write_order(self, write: _OrderWrite) -> None:
        fields: dict[str, Any] = {"note": write.note, "updated_at": self._clock.now_utc()}
        if write.new is not None:
            check_order_transition(write.order.state, write.new)
            fields["state"] = write.new
        if write.deal_id is not None:
            fields["deal_id"] = write.deal_id
        self._journal.update_order(write.order.order_id, **fields)

    def _move_position(self, current: PositionRecord, target: PositionRecord) -> None:
        """Przejście automatu albo samo odświeżenie pól (ten sam stan); brak różnicy: brak zapisu."""
        if target == current:
            return
        if target.state is not current.state:
            check_position_transition(current.state, target.state)
        self._journal.upsert_position(target)

    def _raw(self, source: str, payload: dict[str, Any]) -> None:
        self._journal.append_raw(source, dump(payload), self._clock.now_utc())


def _skipped(code: str) -> ReconcileResult:
    return ReconcileResult(False, [code], [], [], skipped=True)


def _has_fields(rec: PositionRecord) -> bool:
    """Zapis zawiera dane pozycji (kierunek lub wielkość), czyli coś, czego nie wolno po cichu wyzerować."""
    return rec.side is not None or rec.size is not None


def _ours(current: PositionRecord) -> PositionRecord | None:
    """Nasza pozycja z dziennika: zapisany deal_id w stanie OPEN/EXITING/BLOCKED (zawsze z danych brokera)."""
    return current if current.state in _HELD and current.deal_id is not None else None


def _from_broker(state: PositionState, p: OpenPosition) -> PositionRecord:
    return PositionRecord(p.epic, state, p.deal_id, p.side, p.size, p.open_level, p.stop_level, p.opened_at)


def _carry(note: str) -> str:
    """Przyczyny przenoszone do kolejnej notatki zlecenia: tokeny `CAUSE=...` i RECOVERED_FROM_SUBMITTING."""
    return " ".join(t for t in note.split() if t.startswith("CAUSE=") or t == RECOVERED_FROM_SUBMITTING)


def _note(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(_text(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(getattr(value, "value", value))
