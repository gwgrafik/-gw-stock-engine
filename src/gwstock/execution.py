"""Wykonawca (spec §5 pkt 7, §8, §9, §10): wejście przez bramkę ryzyka, straże po potwierdzeniu, zabezpieczenie stopem,
spłaszczenie, awaryjne spłaszczenie i księgowanie zamkniętej transakcji. Spełnia `TradeBooker` z `gwstock.reconcile`.

Niezmienniki (prawdziwe pieniądze):
- `live` inne niż dokładnie `True` (domyślnie, `dry_run`): `enter` ocenia bramkę i zapisuje decyzję (`decision`), ale
  NIGDY nie woła menedżera zleceń ani brokera w sprawie zlecenia (obrona w głąb obok trybu silnika);
- przed wysyłką dziennik trwale ma kontekst wejścia (`entry_ctx:<client_id>`: notowanie decyzji, żądana wielkość, stop,
  wartość punktu, czasy) i wskaźnik ostatniego wejścia (`entry_last:<epic>`); `JournalWriteError` przed wysyłką albo
  z menedżera = kill switch miękki JOURNAL_WRITE_FAILED i brak zlecenia (lub zlecenie zostawione uzgadnianiu);
- wynik `submit_entry` inny niż CONFIRMED/REJECTED/NOT_PLACED albo pozycja BLOCKED = kill switch miękki
  ENTRY_UNRESOLVED (rozstrzyga uzgadnianie lub właściciel, nigdy ponowne wejście); każdy inny wyjątek = kill miękki
  ENTRY_EXCEPTION, alarm i wyjątek dalej; anulowanie (`CancelledError`) przechodzi nietknięte;
- po CONFIRMED: brak stopu → zabezpieczenie (przed czymkolwiek innym), potem obie straże: poślizg wejścia względem
  notowania decyzji (> `slippage_guard_fraction` × stop) i ryzyko FAKTYCZNEJ wielkości i odległości stopu
  (> `max_loss_per_trade`); którakolwiek → zdarzenie, potem natychmiastowe spłaszczenie;
- spłaszczenie (`flatten`): OPEN → EXITING (BLOCKED, ENTERING, FLAT: odmowa ze zdarzeniem), jedno zamknięcie; po
  timeoucie lub odmowie NAJPIERW lista pozycji, nigdy druga wysyłka na ślepo: zamknięcie idzie do brokera tylko przy
  przejściu OPEN → EXITING albo gdy lista pozycji właśnie potwierdziła, że pozycja nadal jest (ponowienie na pozycji
  EXITING zaczyna się od listy); zamknięty rynek: EXITING, stop u brokera zostaje, alarm;
- nigdy FLAT bez zaksięgowania: zamknięcie z historii (do `SETTLE_ATTEMPTS` prób) → `book` → FLAT; gdy historia go nie
  pokazuje, pozycja zostaje EXITING i księguje ją później uzgadnianie (`Reconciler(booker=executor)`);
- `book` jest idempotentny (jedyny zapis wyniku to `journal.record_trade` po deal_id, który sam dolicza wynik dnia)
  i nie zawodzi na wiarygodnych danych: każdy błąd budowy faktów lub kosztów → rekord zdegradowany, ale zaksięgowany
  (COST_FALLBACK); błąd dziennika propaguje się (pozycja nie jest FLAT, więc kolejny przebieg księguje ponownie);
- po każdym udanym `record_trade` (także ponownym, który niczego nie zmienia: domyka okno przerwania między księgowaniem
  a kill switchem) kontrole limitów: dzień ≤ −`max_daily_loss` → miękki, ≤ −2 × `max_daily_loss` → twardy, łącznie
  ≤ −`max_total_drawdown` → twardy;
- pozycja bez stopu: stop (3 próby co 3 s) albo kill switch twardy UNPROTECTED_POSITION i awaryjne spłaszczenie;
  HARD włącza się także wtedy, gdy zabezpieczenie przerwie wyjątek;
- awaryjne spłaszczenie: no-op przy FLAT; inaczej do `EMERGENCY_ATTEMPTS` prób ze skróconym limitem czasu; bez FLAT →
  alarm EMERGENCY_FLATTEN_INCOMPLETE (pozycja pod nadzorem, kill switch zostaje HARD do decyzji właściciela);
- wyjść nie blokuje ani bramka, ani kill switch; awaria notyfikatora nie przerywa przebiegu; każda surowa odpowiedź
  brokera trafia do dziennika przed interpretacją, a każda zmiana stanu pozycji przechodzi przez automat stanów.

Zdarzenia strukturalne: `append_raw("event", {"event": KOD, ...})`; blokada bramki: `gate_block`; decyzja `dry_run`:
`decision`. Czasy w księdze: sygnał = wywołanie `enter`, wysyłka = tuż przed `submit_entry`, potwierdzenie = zapis
CONFIRMED zlecenia (zegar silnika; bez niego czas otwarcia u brokera), opóźnienie = potwierdzenie − sygnał.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, TypeVar

from gwstock.broker.base import Broker
from gwstock.calendar import ExchangeCalendar
from gwstock.clock import Clock
from gwstock.config import RiskConfig, StrategyConfig
from gwstock.costs import CostBreakdown, CostModel, TradeFacts
from gwstock.journal import (
    Journal,
    JournalError,
    JournalReadError,
    JournalWriteError,
    OrderRecord,
    PositionRecord,
    TradeRecord,
)
from gwstock.notify import Notifier
from gwstock.orders.fsm import check_position_transition
from gwstock.orders.ids import make_client_id
from gwstock.orders.manager import HISTORY_MARGIN, EntryBlocked, OrderManager
from gwstock.risk import JOURNAL_WRITE_FAILED, EntryCandidate, KillSwitch, RiskGate
from gwstock.serialization import dump
from gwstock.strategy import EntrySignal
from gwstock.types import (
    BrokerRejected,
    ClosedDeal,
    EntryRequest,
    InstrumentRules,
    MarketClosed,
    OpenPosition,
    OrderState,
    PositionState,
    Side,
)

T = TypeVar("T")
_O = OrderState
_P = PositionState

SOURCE_EVENT = "event"
SOURCE_GATE_BLOCK = "gate_block"
SOURCE_DECISION = "decision"
SOURCE_ACCOUNT = "broker.account"
SOURCE_CLOSE = "broker.close_position"
SOURCE_POSITIONS = "broker.list_positions"
SOURCE_CLOSED = "broker.closed_since"
SOURCE_SET_STOP = "broker.set_stop"
SOURCE_QUOTE = "broker.quote"
SOURCE_RULES = "broker.instrument_rules"

META_ENTRY_CTX = "entry_ctx:{}"  # client_id → kontekst wejścia (JSON), zapisany przed wysyłką
META_ENTRY_BY_DEAL = "entry_by_deal:{}"  # deal_id → client_id, zapisany po CONFIRMED
META_ENTRY_LAST = "entry_last:{}"  # epic → client_id ostatniego wejścia (pozycja przyjęta przez uzgadnianie)

# Przyczyny kill switcha nadawane przez wykonawcę (JOURNAL_WRITE_FAILED z `gwstock.risk`).
ENTRY_EXCEPTION = "ENTRY_EXCEPTION"
ENTRY_UNRESOLVED = "ENTRY_UNRESOLVED"
UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
DAILY_LOSS = "DAILY_LOSS"
DAILY_LOSS_2X = "DAILY_LOSS_2X"
TOTAL_DRAWDOWN = "TOTAL_DRAWDOWN"
KILL_CHECK_FAILED = "KILL_CHECK_FAILED"

COST_FALLBACK = "COST_FALLBACK"
NO_ENTRY_CONTEXT = "NO_ENTRY_CONTEXT"

# Wynik jednej próby spłaszczenia (`_flatten`).
OUTCOME_FLAT = "FLAT"  # zamknięte, zaksięgowane, FLAT
OUTCOME_MARKET_CLOSED = "MARKET_CLOSED"  # rynek zamknięty: stop u brokera zostaje
OUTCOME_STILL_OPEN = "STILL_OPEN"  # zamknięcie zawiodło, lista potwierdza otwartą pozycję
OUTCOME_UNVERIFIED = "UNVERIFIED"  # zamknięcie zawiodło i nie da się sprawdzić listy
OUTCOME_AWAIT_HISTORY = "AWAIT_HISTORY"  # pozycji nie ma u brokera, zamknięcie jeszcze nie w historii
OUTCOME_BOOKING_FAILED = "BOOKING_FAILED"  # zamknięcie znalezione, księgowanie zawiodło (nie dziennik)
OUTCOME_REFUSED = "REFUSED"  # pozycja w dzienniku nie jest OPEN/EXITING z tym deal_id
_EMERGENCY_RETRY = frozenset({OUTCOME_STILL_OPEN, OUTCOME_UNVERIFIED})  # pozycja może być nadal otwarta

# Stałe procedur ze spec §8 i §9 (nie parametry strategii ani ryzyka).
SETTLE_ATTEMPTS = 3  # odczyty historii po zamknięciu
SETTLE_PAUSE_S = 2  # przed każdym odczytem historii
PROTECT_ATTEMPTS = 3
PROTECT_PAUSE_S = 3
# Od pierwszej nieudanej próby do decyzji HARD: 3 + 2 + 3 + 2 = 10 s (spec §9: bez stopu najdłużej 10 s).
PROTECT_CALL_TIMEOUT_S = 2
EMERGENCY_ATTEMPTS = 5
EMERGENCY_PAUSE_S = 1
EMERGENCY_MIN_TIMEOUT_S = 1  # skrócony limit = max(1 s, submit_timeout_s / 2)

_RESOLVED = frozenset({_O.CONFIRMED, _O.REJECTED, _O.NOT_PLACED})
_FLATTENABLE = frozenset({_P.OPEN, _P.EXITING})
_ZERO = Decimal("0")
_MILLISECOND = timedelta(milliseconds=1)


class BookingError(Exception):
    """Nie da się zbudować nawet rekordu zdegradowanego (np. brak wartości punktu, pozycja bez kierunku)."""


class _NoContext(Exception):
    """Brak kontekstu wejścia dla pozycji (obca lub przyjęta bez naszego zapisu): rekord zdegradowany."""


async def _with_timeout(call: Awaitable[T], timeout: float) -> T:
    """Wywołanie brokera z limitem czasu; przekroczenie to `TimeoutError` (niejasność, nie porażka)."""
    return await asyncio.wait_for(call, timeout)


class Executor:
    """Wykonuje wejście, wyjście i spłaszczenie oraz księguje zamknięcia (patrz docstring modułu).

    `live=True` (dokładnie `True`) uzbraja wysyłkę zleceń; domyślnie wykonawca pracuje jak `dry_run`. Jeden wywołujący
    naraz (silnik szereguje wykonawcę i uzgadnianie jedną blokadą).
    """

    def __init__(
        self,
        manager: OrderManager,
        broker: Broker,
        journal: Journal,
        gate: RiskGate,
        kill: KillSwitch,
        cost_model: CostModel,
        clock: Clock,
        strat: StrategyConfig,
        risk: RiskConfig,
        calendar: ExchangeCalendar,
        notifier: Notifier,
        live: bool = False,
    ) -> None:
        if not risk.submit_timeout_s > 0:
            raise ValueError(f"submit_timeout_s must be positive, got {risk.submit_timeout_s!r}")
        self._manager = manager
        self._broker = broker
        self._journal = journal
        self._gate = gate
        self._kill = kill
        self._cost_model = cost_model
        self._clock = clock
        self._strat = strat
        self._risk = risk
        self._calendar = calendar
        self._notifier = notifier
        self._live = live is True
        self._epic = strat.instrument
        self._timeout = risk.submit_timeout_s

    @property
    def live(self) -> bool:
        """Czy wykonawca wysyła zlecenia (tylko przy `live=True` w konstruktorze)."""
        return self._live

    # --- wejście ----------------------------------------------------------------------------------------

    async def enter(self, signal: EntrySignal, rules: InstrumentRules, day: date) -> OrderRecord | None:
        """Wejście z sygnału strategii; `day` to dzień handlowy (identyfikator zlecenia i jego numer kolejny).

        None: bramka zablokowała (`gate_block`), tryb `dry_run` (`decision`), konto niedostępne (ACCOUNT_UNAVAILABLE)
        albo awaria dziennika przed wysyłką lub z menedżera (kill miękki JOURNAL_WRITE_FAILED). Inaczej rekord zlecenia
        z menedżera: CONFIRMED (pozycja mogła zostać od razu spłaszczona przez straż), REJECTED, NOT_PLACED albo
        UNKNOWN (kill miękki ENTRY_UNRESOLVED). Wyjątek inny niż błąd zapisu dziennika: kill miękki ENTRY_EXCEPTION
        i wyjątek dalej; `CancelledError` bez żadnej reakcji.
        """
        try:
            request = await self._prepare(signal, rules, day)
        except JournalWriteError as exc:
            self._journal_failed("before the send", exc)
            return None
        except Exception as exc:
            self._entry_exception("before the send", exc)
            raise
        if request is None:
            return None
        try:
            rec = await self._manager.submit_entry(request)
        except JournalWriteError as exc:  # przed wysyłką nic nie poszło; po niej rozstrzyga uzgadnianie (SUBMITTING)
            self._journal_failed(f"submit_entry {request.client_id}", exc)
            return None
        except Exception as exc:
            self._entry_exception(f"submit_entry {request.client_id}", exc)
            raise
        try:
            await self._after_submit(rec, signal, rules)
        except JournalWriteError as exc:
            self._journal_failed(f"after submit_entry {rec.order_id}", exc)
        except Exception as exc:
            self._entry_exception(f"after submit_entry {rec.order_id}", exc)
            raise
        return rec

    async def _prepare(self, signal: EntrySignal, rules: InstrumentRules, day: date) -> EntryRequest | None:
        """Konto, bramka, identyfikator i kontekst wejścia; żądanie do wysłania albo None (bez wysyłki)."""
        signal_ts = self._now()
        quote = signal.decision_quote
        candidate = EntryCandidate(
            epic=self._epic,
            side=signal.side,
            size=signal.size,
            stop_distance=signal.stop_distance,
            entry_price=quote.mid,  # ta sama baza co wielkość strategii (dźwignia od mid)
            value_per_point=rules.value_per_point,
            margin_rate=rules.margin_rate,
            quote_age_s=(signal_ts - quote.ts).total_seconds(),
        )
        try:
            account = await _with_timeout(self._broker.account(), self._timeout)
        except Exception as exc:
            self._raw_error(SOURCE_ACCOUNT, exc)
            self._alert("WARN", f"ACCOUNT_UNAVAILABLE: no entry ({type(exc).__name__}: {exc})")
            self._event("ACCOUNT_UNAVAILABLE", error=type(exc).__name__, message=str(exc))
            return None
        self._raw(SOURCE_ACCOUNT, response=account)

        verdict = self._gate.check_entry(candidate, account)
        if not verdict.passed:
            block = {"reasons": verdict.reasons, "candidate": candidate, "day": day.isoformat()}
            self._append(SOURCE_GATE_BLOCK, block)
            return None
        client_id = make_client_id(day, self._epic, self._journal.next_seq(day), "entry")
        request = EntryRequest(client_id, self._epic, signal.side, signal.size, signal.stop_distance)
        if not self._live:
            self._append(SOURCE_DECISION, {"request": request, "candidate": candidate, "day": day.isoformat()})
            return None

        ctx_key = META_ENTRY_CTX.format(client_id)
        if self._journal.get_order(client_id) is not None or self._journal.get_meta(ctx_key) is not None:
            # luka w numeracji dnia: cudzego kontekstu nie nadpisujemy (księga tamtego zlecenia by na tym ucierpiała)
            raise EntryBlocked("DUPLICATE_CLIENT_ID", client_id)
        context = {
            "client_id": client_id,
            "epic": self._epic,
            "side": signal.side,
            "size": signal.size,
            "stop_distance": signal.stop_distance,
            "value_per_point": rules.value_per_point,
            "decision_bid": quote.bid,
            "decision_ask": quote.ask,
            "decision_ts": quote.ts,
            "signal_ts": signal_ts,
            "submit_ts": self._now(),
        }
        self._journal.set_meta(ctx_key, dump(context))
        self._journal.set_meta(META_ENTRY_LAST.format(self._epic), client_id)
        return request

    async def _after_submit(self, rec: OrderRecord, signal: EntrySignal, rules: InstrumentRules) -> None:
        """Kontrakt z menedżerem (T7), potem zabezpieczenie i straże po potwierdzeniu."""
        position = self._journal.current_position(self._epic)
        if rec.state not in _RESOLVED or (position is not None and position.state is _P.BLOCKED):
            self._unresolved(rec, position)
            return
        if rec.state is not _O.CONFIRMED:
            return  # REJECTED/NOT_PLACED: zapisane przez menedżera, pozycja FLAT, próba dnia zużyta
        if position is None or position.state is not _P.OPEN or position.deal_id != rec.deal_id:
            self._unresolved(rec, position)
            return
        if position.stop_level is None:  # ochrona najpierw, przed każdym innym zapisem
            if not await self._protect(_as_open(position), signal.stop_distance):
                return  # kill switch twardy i awaryjne spłaszczenie już wykonane
        self._journal.set_meta(META_ENTRY_BY_DEAL.format(rec.deal_id), rec.order_id)
        if self._guard_fill(position, signal, rules):
            await self.flatten(position, "FORCED")

    def _unresolved(self, rec: OrderRecord, position: PositionRecord | None) -> None:
        state = None if position is None else position.state
        self._kill.soft(ENTRY_UNRESOLVED)
        self._alert(
            "ERROR",
            f"{ENTRY_UNRESOLVED}: order {rec.order_id} {rec.state.value} ({rec.note}), position "
            f"{'missing' if state is None else state.value}; reconciliation or the owner decides, no new entry",
        )
        self._event(ENTRY_UNRESOLVED, order_id=rec.order_id, state=rec.state, note=rec.note, position=state)

    def _guard_fill(self, position: PositionRecord, signal: EntrySignal, rules: InstrumentRules) -> list[str]:
        """Straż poślizgu (spec §5 pkt 7) i ryzyko faktycznej wielkości; obie zawsze sprawdzane, zdarzenia przed
        zamknięciem. Zwraca przyczyny natychmiastowego spłaszczenia."""
        quote = signal.decision_quote
        causes: list[str] = []
        if position.side is Side.BUY:
            deviation = position.open_level - quote.ask  # BUY kupuje po ask: gorzej = wyżej
        else:
            deviation = quote.bid - position.open_level  # SELL sprzedaje po bid: gorzej = niżej
        limit = self._strat.slippage_guard_fraction * signal.stop_distance
        if deviation > limit:
            causes.append("SLIPPAGE_GUARD")
            self._event(
                "SLIPPAGE_GUARD", deal_id=position.deal_id, side=position.side, decision_bid=quote.bid,
                decision_ask=quote.ask, open_level=position.open_level, deviation=deviation, limit=limit,
            )  # fmt: skip
        if position.stop_level is not None:
            distance = abs(position.open_level - position.stop_level)
        else:
            distance = signal.stop_distance  # stop ustawiony przed chwilą w tej odległości
        risk = position.size * distance * rules.value_per_point
        if risk > self._risk.max_loss_per_trade:
            causes.append("SIZE_MISMATCH_RISK")
            self._event(
                "SIZE_MISMATCH_RISK", deal_id=position.deal_id, size=position.size, requested=signal.size,
                distance=distance, risk=risk, limit=self._risk.max_loss_per_trade,
            )  # fmt: skip
        if causes:
            self._alert("ERROR", f"{' + '.join(causes)}: closing {position.deal_id} at once")
        return causes

    # --- spłaszczenie -------------------------------------------------------------------------------------

    async def flatten(self, position: PositionRecord | OpenPosition, reason: str) -> ClosedDeal | None:
        """Zamyka NASZĄ pozycję i księguje zamknięcie; zwraca je, gdy pozycja jest już FLAT (zaksięgowana).

        `position`: rekord z dziennika (albo widok brokera); liczą się wyłącznie `epic` i `deal_id`, a źródłem prawdy
        jest bieżący rekord dziennika (musi być OPEN lub EXITING z tym deal_id, inaczej odmowa). `reason` (np. FORCED,
        MANUAL, EMERGENCY) trafia do zdarzeń. None: rynek zamknięty, zamknięcie niepotwierdzone, pozycja nadal otwarta
        albo zamknięcie czeka na historię (pozycja EXITING; ponowić przy następnym takcie albo zaksięguje uzgadnianie).
        Ponowienie na pozycji EXITING najpierw czyta listę pozycji: zniknęła → księgowanie bez zamknięcia, jest →
        zamknięcie, nieczytelna → nic nie wysyłamy. Błąd dziennika propaguje się; kill switch nigdy nie blokuje wyjścia.
        """
        closed, _ = await self._flatten(position, reason, self._timeout)
        return closed

    async def _flatten(
        self, position: PositionRecord | OpenPosition, reason: str, timeout: float, *, verified: bool = False
    ) -> tuple[ClosedDeal | None, str]:
        """Jedna próba spłaszczenia. `verified=True`: wywołujący przed chwilą (ta sama procedura) potwierdził listą,
        że pozycja jest otwarta, więc ponowne zamknięcie nie wymaga kolejnego odczytu."""
        epic, deal_id = position.epic, position.deal_id
        current = self._journal.current_position(epic)
        if current is None or deal_id is None or current.deal_id != deal_id or current.state not in _FLATTENABLE:
            state = "MISSING" if current is None else current.state.value
            self._alert(
                "ERROR" if state == _P.BLOCKED.value else "WARN",
                f"FLATTEN_REFUSED {deal_id}: journal position is {state} "
                f"(deal {None if current is None else current.deal_id}); nothing sent",
            )
            self._event(
                "FLATTEN_REFUSED", deal_id=deal_id, flatten_reason=reason, journal_state=state,
                journal_deal_id=None if current is None else current.deal_id,
            )  # fmt: skip
            return None, OUTCOME_REFUSED
        if current.state is _P.OPEN:
            check_position_transition(_P.OPEN, _P.EXITING)
            current = replace(current, state=_P.EXITING)
            self._journal.upsert_position(current)
        elif not verified:
            # EXITING z wcześniejszej próby (przerwanie, odmowa, nieczytelna lista): wcześniejsze zamknięcie mogło
            # dojść, więc zanim wyślemy kolejne, broker musi potwierdzić, że pozycja nadal jest
            gone, list_error = await self._deal_gone(deal_id, timeout)
            if gone is None:
                self._alert("ERROR", f"FLATTEN_UNVERIFIED {deal_id}: position list unreadable before a retry")
                self._event(
                    "FLATTEN_UNVERIFIED", deal_id=deal_id, flatten_reason=reason, error=list_error, stage="before_retry"
                )
                return None, OUTCOME_UNVERIFIED
            if gone:
                return await self._settle(current, reason, timeout)

        try:
            await _with_timeout(self._broker.close_position(deal_id), timeout)
        except MarketClosed as exc:
            self._raw_error(SOURCE_CLOSE, exc, deal_id=deal_id, flatten_reason=reason)
            self._alert(
                "WARN", f"FLATTEN_BLOCKED_MARKET_CLOSED {deal_id}: the stop at the broker stays, retry after reopening"
            )
            self._event("FLATTEN_BLOCKED_MARKET_CLOSED", deal_id=deal_id, flatten_reason=reason)
            return None, OUTCOME_MARKET_CLOSED
        except Exception as exc:  # timeout (także asyncio), odmowa, wygasła sesja: nie wiemy, czy zamknięto
            self._raw_error(SOURCE_CLOSE, exc, deal_id=deal_id, flatten_reason=reason)
            gone, _ = await self._deal_gone(deal_id, timeout)
            if gone is not True:
                code, outcome = (
                    ("FLATTEN_UNVERIFIED", OUTCOME_UNVERIFIED) if gone is None
                    else ("FLATTEN_STILL_OPEN", OUTCOME_STILL_OPEN)
                )  # fmt: skip
                self._alert("ERROR", f"{code} {deal_id}: close failed ({type(exc).__name__}: {exc}); not resent")
                self._event(
                    code, deal_id=deal_id, flatten_reason=reason, error=type(exc).__name__, message=str(exc),
                    stage="after_close",
                )  # fmt: skip
                return None, outcome
        else:
            self._raw(SOURCE_CLOSE, deal_id=deal_id, flatten_reason=reason, response=None)
        return await self._settle(current, reason, timeout)

    async def _deal_gone(self, deal_id: str, timeout: float) -> tuple[bool | None, str | None]:
        """Lista pozycji zamiast wysyłki na ślepo: (czy deal zniknął, typ błędu odczytu). None: nie da się sprawdzić."""
        try:
            positions = await _with_timeout(self._broker.list_positions(), timeout)
        except Exception as exc:
            self._raw_error(SOURCE_POSITIONS, exc, deal_id=deal_id, context="flatten")
            return None, type(exc).__name__
        self._raw(SOURCE_POSITIONS, deal_id=deal_id, context="flatten", response=positions)
        return not any(p.deal_id == deal_id for p in positions), None

    async def _settle(self, position: PositionRecord, reason: str, timeout: float) -> tuple[ClosedDeal | None, str]:
        """Pozycji nie ma u brokera: zamknięcie z historii → księgowanie → FLAT. Bez zamknięcia zostaje EXITING."""
        deal_id = position.deal_id
        if position.opened_at is None:  # początku historii nie zgadujemy: zostaje uzgadnianiu
            self._await_history(position, reason, "HISTORY_START_UNKNOWN")
            return None, OUTCOME_AWAIT_HISTORY
        since = position.opened_at - HISTORY_MARGIN
        for attempt in range(1, SETTLE_ATTEMPTS + 1):
            await self._clock.sleep(SETTLE_PAUSE_S)
            try:
                closures = await _with_timeout(self._broker.closed_since(since), timeout)
            except Exception as exc:
                self._raw_error(SOURCE_CLOSED, exc, deal_id=deal_id, since=since, attempt=attempt)
                continue
            self._raw(SOURCE_CLOSED, deal_id=deal_id, since=since, attempt=attempt, response=closures)
            found = [c for c in closures if c.deal_id == deal_id and c.epic == position.epic]
            if len(found) > 1:  # nie zgadujemy, które zamknięcie jest prawdziwe: zablokuje uzgadnianie
                self._alert("ERROR", f"AMBIGUOUS_CLOSURE {deal_id}: {len(found)} closures in the history, not booked")
                self._event("AMBIGUOUS_CLOSURE", deal_id=deal_id, count=len(found))
                return None, OUTCOME_AWAIT_HISTORY
            if found:
                closed = found[0]
                try:
                    await self._book(position, closed, timeout)
                except JournalError:
                    raise
                except Exception as exc:
                    self._alert("ERROR", f"BOOKING_FAILED {deal_id}: {type(exc).__name__}: {exc}; stays EXITING")
                    self._event("BOOKING_FAILED", deal_id=deal_id, error=type(exc).__name__, message=str(exc))
                    return None, OUTCOME_BOOKING_FAILED
                self._finish_flat(position, closed, reason)
                return closed, OUTCOME_FLAT
        self._await_history(position, reason, f"NOT_IN_HISTORY_AFTER={SETTLE_ATTEMPTS}")
        return None, OUTCOME_AWAIT_HISTORY

    def _await_history(self, position: PositionRecord, reason: str, why: str) -> None:
        self._alert(
            "WARN",
            f"CLOSE_SENT_AWAIT_HISTORY {position.deal_id}: closed at the broker, closure not in history yet ({why}); "
            "position stays EXITING, reconciliation books it",
        )
        self._event("CLOSE_SENT_AWAIT_HISTORY", deal_id=position.deal_id, flatten_reason=reason, why=why)

    def _finish_flat(self, position: PositionRecord, closed: ClosedDeal, reason: str) -> None:
        """EXITING → FLAT po zaksięgowaniu, tylko gdy dziennik nadal mówi EXITING z tym deal_id."""
        current = self._journal.current_position(position.epic)
        state = None if current is None else current.state
        self._event(
            "FLATTENED", deal_id=position.deal_id, flatten_reason=reason, close_reason=closed.reason,
            exit_level=closed.exit_level, journal_state=state,
        )  # fmt: skip
        if current is not None and current.state is _P.EXITING and current.deal_id == position.deal_id:
            check_position_transition(_P.EXITING, _P.FLAT)
            self._journal.upsert_position(PositionRecord(position.epic, _P.FLAT))
        elif state is not _P.FLAT:  # ktoś zmienił stan w międzyczasie: zostaje uzgadnianiu
            self._alert("WARN", f"FLATTENED {position.deal_id}: booked, journal position is {state}; left as is")

    # --- awaryjne spłaszczenie ----------------------------------------------------------------------------

    async def emergency_flatten(self) -> None:
        """Procedura spłaszczenia ze skróconym limitem czasu, do `EMERGENCY_ATTEMPTS` prób co `EMERGENCY_PAUSE_S`.

        No-op, gdy dziennik nie ma pozycji albo jest FLAT (silnik woła ją przy każdym takcie z kill switchem HARD).
        Ponawia tylko, gdy pozycja może być nadal otwarta (zamknięcie zawiodło); rynek zamknięty, zamknięcie czekające
        na historię i pozycja BLOCKED/ENTERING kończą próby. Bez FLAT: alarm EMERGENCY_FLATTEN_INCOMPLETE.
        """
        timeout = max(EMERGENCY_MIN_TIMEOUT_S, self._risk.submit_timeout_s / 2)
        outcome, attempts = "", 0
        for attempt in range(1, EMERGENCY_ATTEMPTS + 1):
            if attempt > 1:
                await self._clock.sleep(EMERGENCY_PAUSE_S)
            current = self._journal.current_position(self._epic)
            if current is None or current.state is _P.FLAT:
                return
            if current.state not in _FLATTENABLE:  # BLOCKED: decyduje właściciel; ENTERING: rozstrzyga uzgadnianie
                outcome = f"STATE_{current.state.value}"
                break
            attempts = attempt
            # STILL_OPEN: lista sprzed sekundy potwierdziła otwartą pozycję: kolejne zamknięcie bez ponownego odczytu
            _, outcome = await self._flatten(current, "EMERGENCY", timeout, verified=outcome == OUTCOME_STILL_OPEN)
            if outcome == OUTCOME_FLAT:
                return
            if outcome not in _EMERGENCY_RETRY:
                break
        current = self._journal.current_position(self._epic)
        if current is None or current.state is _P.FLAT:
            return
        self._alert(
            "ERROR",
            f"EMERGENCY_FLATTEN_INCOMPLETE: position {current.deal_id} {current.state.value} after {attempts} "
            f"attempt(s) ({outcome}); kept under watch",
        )
        self._event(
            "EMERGENCY_FLATTEN_INCOMPLETE", deal_id=current.deal_id, state=current.state, attempts=attempts,
            outcome=outcome,
        )  # fmt: skip

    # --- zabezpieczenie stopem ----------------------------------------------------------------------------

    async def protect_unprotected(self, position: OpenPosition) -> bool:
        """Ustawia stop pozycji bez stopu (widok brokera: kierunek i poziom otwarcia brokera).

        Odległość: stop z kontekstu wejścia, a bez niego `max_loss_per_trade / (size × wartość_punktu)` w dół do kroku
        stopu brokera (bez kroku: do dokładności ceny otwarcia). True: stop ustawiony. False: kill switch twardy
        UNPROTECTED_POSITION, alarm i awaryjne spłaszczenie (HARD także wtedy, gdy procedurę przerwie wyjątek).
        """
        return await self._protect(position, None)

    async def _protect(self, position: OpenPosition, distance: Decimal | None) -> bool:
        attached = False
        level: Decimal | None = None
        try:
            if distance is None:
                distance = await self._protection_distance(position)
            if distance is not None:
                level = position.open_level - distance if position.side is Side.BUY else position.open_level + distance
                timeout = min(self._timeout, PROTECT_CALL_TIMEOUT_S)
                for attempt in range(1, PROTECT_ATTEMPTS + 1):
                    if attempt > 1:
                        await self._clock.sleep(PROTECT_PAUSE_S)
                    try:
                        await _with_timeout(self._broker.set_stop(position.deal_id, level), timeout)
                    except Exception as exc:  # ponowienie stopu jest bezpieczne (ten sam poziom)
                        self._raw_error(
                            SOURCE_SET_STOP, exc, deal_id=position.deal_id, stop_level=level, attempt=attempt
                        )
                        continue
                    attached = True  # stop jest u brokera: awaria zapisu niżej nie oznacza braku ochrony
                    self._raw(
                        SOURCE_SET_STOP, deal_id=position.deal_id, stop_level=level, attempt=attempt, response=None
                    )
                    self._alert("WARN", f"STOP_ATTACHED {position.deal_id}: stop {level} set after the broker had none")
                    self._event("STOP_ATTACHED", deal_id=position.deal_id, stop_level=level, attempt=attempt)
                    break
        finally:
            if not attached:  # także przy wyjątku i anulowaniu: pozycja bez stopu nigdy nie zostaje bez HARD
                self._kill.hard(UNPROTECTED_POSITION)
                self._alert(
                    "ERROR",
                    f"{UNPROTECTED_POSITION}: {position.deal_id} has no stop at the broker (stop {level}); "
                    "kill switch HARD, emergency flatten",
                )
        if attached:
            return True
        self._event("PROTECTION_FAILED", deal_id=position.deal_id, stop_level=level, distance=distance)
        await self.emergency_flatten()
        return False

    async def _protection_distance(self, position: OpenPosition) -> Decimal | None:
        try:
            ctx = self._context(position.deal_id, position.epic)
            if ctx is not None:
                distance = _ctx_decimal(ctx, "stop_distance")
                if distance > 0:
                    return distance
        except Exception:  # ochrona przede wszystkim: nieczytelny kontekst = odległość z limitu straty
            pass
        try:
            rules = await _with_timeout(
                self._broker.instrument_rules(position.epic), min(self._timeout, PROTECT_CALL_TIMEOUT_S)
            )
        except Exception as exc:
            self._raw_error(SOURCE_RULES, exc, deal_id=position.deal_id, context="protection")
            return None
        self._raw(SOURCE_RULES, deal_id=position.deal_id, context="protection", response=rules)
        return _max_loss_distance(self._risk.max_loss_per_trade, position.size, position.open_level, rules)

    # --- księgowanie --------------------------------------------------------------------------------------

    async def book(self, position: PositionRecord, closed: ClosedDeal) -> None:
        """Księguje zamknięcie naszej pozycji (`TradeBooker`); idempotentny po `closed.deal_id`.

        Nie zmienia stanu pozycji ani zleceń (to robi wywołujący po powrocie). Błąd dziennika propaguje się;
        `BookingError`, gdy nie da się zbudować nawet rekordu zdegradowanego.
        """
        await self._book(position, closed, self._timeout)

    async def _book(self, position: PositionRecord, closed: ClosedDeal, timeout: float) -> None:
        exit_day = self._calendar.to_local(closed.ts).date()
        fallback: str | None = None
        try:
            trade, notes = await self._full_trade(position, closed, timeout)
        except (JournalReadError, JournalWriteError):
            raise  # dziennik leży: księgowanie powtórzy się z pełnym kontekstem
        except Exception as exc:  # brak kontekstu, ujemny spread, złe typy: rekord zdegradowany, ale zaksięgowany
            fallback = NO_ENTRY_CONTEXT if isinstance(exc, _NoContext) else f"{type(exc).__name__}: {exc}"
            try:
                trade, notes = await self._fallback_trade(position, closed, timeout)
            except (JournalReadError, JournalWriteError):
                raise
            except Exception as exc2:
                self._alert("ERROR", f"BOOKING_FAILED {closed.deal_id}: {type(exc2).__name__}: {exc2} ({fallback})")
                raise BookingError(f"cannot book {closed.deal_id}: {type(exc2).__name__}: {exc2}") from exc2
        booked = self._journal.record_trade(trade, closed.deal_id, exit_day)
        triggered = self._enforce_loss_limits(exit_day, announce=booked)  # kill przed zapisami zdarzeń
        if not booked:
            return  # już zaksięgowane (np. ponowny przebieg uzgadniania): w księdze nic się nie zmienia
        if fallback is not None:
            self._alert("WARN", f"{COST_FALLBACK} {closed.deal_id}: degraded ledger row ({fallback}); check the broker")
            self._event(COST_FALLBACK, deal_id=closed.deal_id, reason=fallback)
        self._event(
            "TRADE_BOOKED", deal_id=closed.deal_id, exit_day=exit_day.isoformat(), exit_reason=trade.exit_reason,
            net_pnl=trade.net_pnl, notes=list(notes), cost_fallback=fallback is not None,
        )  # fmt: skip
        for code, detail in triggered:
            self._event(code, **detail)

    async def _full_trade(
        self, position: PositionRecord, closed: ClosedDeal, timeout: float
    ) -> tuple[TradeRecord, tuple[str, ...]]:
        ctx = self._context(closed.deal_id, position.epic)
        if ctx is None:
            raise _NoContext(NO_ENTRY_CONTEXT)
        bid, ask = _ctx_decimal(ctx, "decision_bid"), _ctx_decimal(ctx, "decision_ask")
        vpp = _ctx_decimal(ctx, "value_per_point")
        signal_ts, submit_ts = _ctx_time(ctx, "signal_ts"), _ctx_time(ctx, "submit_ts")
        spread_entry = ask - bid
        spread_exit = await self._exit_spread(position.epic, closed.deal_id, spread_entry, timeout)
        # mid wyjścia nie jest obserwowany: BUY wychodzi po bid (mid = wyjście + spread/2), SELL po ask (− spread/2)
        half = spread_exit / 2
        mid_exit = closed.exit_level + half if position.side is Side.BUY else closed.exit_level - half
        facts = TradeFacts(
            side=position.side, size=position.size, value_per_point=vpp, mid_entry=(bid + ask) / 2, mid_exit=mid_exit,
            entry_fill=position.open_level, exit_fill=closed.exit_level, spread_entry=spread_entry,
            spread_exit=spread_exit, exit_reason=_exit_reason(closed),
            nights_held=self._nights(position.opened_at, closed),
        )  # fmt: skip
        costs = self._cost_model.compute(facts)
        confirm_ts = self._confirm_ts(ctx, closed.deal_id, position, submit_ts)
        trade = _record(position, bid, ask, closed, costs, signal_ts, submit_ts, confirm_ts, facts.exit_reason)
        return trade, costs.notes

    async def _fallback_trade(
        self, position: PositionRecord, closed: ClosedDeal, timeout: float
    ) -> tuple[TradeRecord, tuple[str, ...]]:
        """Rekord zdegradowany: mid wejścia = poziom otwarcia, spready 0, notowanie decyzji = poziom otwarcia."""
        vpp = await self._fallback_vpp(position.epic, closed.deal_id, timeout)
        level = position.open_level
        opened = position.opened_at
        when = opened if opened is not None else closed.ts
        reason = _exit_reason(closed)
        facts = TradeFacts(
            side=position.side, size=position.size, value_per_point=vpp, mid_entry=level, mid_exit=closed.exit_level,
            entry_fill=level, exit_fill=closed.exit_level, spread_entry=_ZERO, spread_exit=_ZERO, exit_reason=reason,
            nights_held=0 if opened is None else self._nights(opened, closed),
        )  # fmt: skip
        costs = self._cost_model.compute(facts)
        return _record(position, level, level, closed, costs, when, when, when, reason), costs.notes

    async def _exit_spread(self, epic: str, deal_id: str, fallback: Decimal, timeout: float) -> Decimal:
        """Spread z bieżącego notowania (najbliższe wyjściu, jakie mamy); bez notowania: spread wejścia."""
        try:
            quote = await _with_timeout(self._broker.quote(epic), timeout)
        except Exception as exc:
            self._raw_error(SOURCE_QUOTE, exc, deal_id=deal_id, context="booking")
            return fallback
        self._raw(SOURCE_QUOTE, deal_id=deal_id, context="booking", response=quote)
        return quote.ask - quote.bid

    async def _fallback_vpp(self, epic: str, deal_id: str, timeout: float) -> Decimal:
        try:
            ctx = self._context(deal_id, epic)
            if ctx is not None:
                vpp = _ctx_decimal(ctx, "value_per_point")
                if vpp > 0:
                    return vpp
        except (JournalReadError, JournalWriteError):
            raise
        except Exception:
            pass
        try:
            rules = await _with_timeout(self._broker.instrument_rules(epic), timeout)
        except Exception as exc:
            self._raw_error(SOURCE_RULES, exc, deal_id=deal_id, context="booking")
            raise
        self._raw(SOURCE_RULES, deal_id=deal_id, context="booking", response=rules)
        return rules.value_per_point

    def _confirm_ts(self, ctx: dict[str, Any], deal_id: str, position: PositionRecord, submit_ts: datetime) -> datetime:
        """Chwila zapisu CONFIRMED zlecenia (zegar silnika); bez niej czas otwarcia u brokera albo wysyłki."""
        order = self._journal.get_order(ctx["client_id"])
        if order is not None and order.state is _O.CONFIRMED and order.deal_id == deal_id:
            return order.updated_at
        return position.opened_at if position.opened_at is not None else submit_ts

    def _nights(self, opened_at: datetime, closed: ClosedDeal) -> int:
        local = self._calendar.to_local
        return max(0, (local(closed.ts).date() - local(opened_at).date()).days)

    def _enforce_loss_limits(self, exit_day: date, *, announce: bool) -> list[tuple[str, dict[str, Any]]]:
        """Kontrole po księgowaniu (spec §9); kill switch nigdy nie rzuca. Zwraca wyzwolone (kod, szczegóły)."""
        try:
            day_pnl = self._journal.realized_pnl(exit_day)
            total = self._journal.realized_pnl_total()
        except Exception as exc:  # nie da się sprawdzić limitów: blokujemy wejścia
            self._kill.soft(KILL_CHECK_FAILED)
            self._alert("ERROR", f"{KILL_CHECK_FAILED}: realized pnl unreadable after booking ({type(exc).__name__})")
            return []
        daily = self._risk.max_daily_loss
        checks = (
            (day_pnl <= -daily, self._kill.soft, DAILY_LOSS, {"day_pnl": day_pnl, "limit": -daily}),
            (day_pnl <= -2 * daily, self._kill.hard, DAILY_LOSS_2X, {"day_pnl": day_pnl, "limit": -2 * daily}),
            (
                total <= -self._risk.max_total_drawdown, self._kill.hard, TOTAL_DRAWDOWN,
                {"total_pnl": total, "limit": -self._risk.max_total_drawdown},
            ),
        )  # fmt: skip
        triggered = []
        for hit, engage, code, detail in checks:
            if not hit:
                continue
            engage(code)
            triggered.append((code, {"exit_day": exit_day.isoformat(), **detail}))
            if announce:
                self._alert("ERROR", f"{code}: {', '.join(f'{k} {v}' for k, v in detail.items())} ({exit_day})")
        return triggered

    # --- kontekst wejścia ---------------------------------------------------------------------------------

    def _context(self, deal_id: str | None, epic: str) -> dict[str, Any] | None:
        """Kontekst wejścia pozycji `deal_id`: przez `entry_by_deal`, a bez niego przez wskaźnik ostatniego wejścia,
        jeśli jego zlecenie ma w dzienniku ten sam deal_id (pozycja przyjęta przez uzgadnianie). None: brak."""
        if deal_id is None:
            return None
        client_id = self._journal.get_meta(META_ENTRY_BY_DEAL.format(deal_id))
        if not client_id:
            last = self._journal.get_meta(META_ENTRY_LAST.format(epic))
            order = self._journal.get_order(last) if last else None
            if order is None or order.deal_id != deal_id or order.epic != epic:
                return None
            client_id = last
        text = self._journal.get_meta(META_ENTRY_CTX.format(client_id))
        if text is None:
            return None
        ctx = json.loads(text)
        if not isinstance(ctx, dict) or ctx.get("client_id") != client_id:
            raise ValueError(f"entry context of {client_id!r} is malformed")
        return ctx

    # --- zapisy i alarmy ----------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock.now_utc()

    def _append(self, source: str, payload: dict[str, Any]) -> None:
        self._journal.append_raw(source, dump(payload), self._now())

    def _raw(self, source: str, **payload: Any) -> None:
        self._append(source, payload)

    def _raw_error(self, source: str, exc: BaseException, **payload: Any) -> None:
        error: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, BrokerRejected):
            error["rejection"] = exc.reason
        self._append(source, {**payload, **error})

    def _event(self, code: str, **details: Any) -> None:
        self._append(SOURCE_EVENT, {"event": code, **details})

    def _alert(self, level: str, message: str) -> None:
        try:
            self._notifier.alert(level, message)
        except Exception:  # alarm nie jest warunkiem dalszej pracy
            pass

    def _journal_failed(self, where: str, exc: BaseException) -> None:
        self._kill.soft(JOURNAL_WRITE_FAILED)
        self._alert("ERROR", f"{JOURNAL_WRITE_FAILED} {where}: {type(exc).__name__}: {exc}; no new entry")

    def _entry_exception(self, where: str, exc: BaseException) -> None:
        self._kill.soft(ENTRY_EXCEPTION)
        self._alert("ERROR", f"{ENTRY_EXCEPTION} {where}: {type(exc).__name__}: {exc}")


def _as_open(rec: PositionRecord) -> OpenPosition:
    """Rekord OPEN z menedżera to dane brokera z potwierdzenia: widok brokera dla zabezpieczenia."""
    return OpenPosition(rec.deal_id, rec.epic, rec.side, rec.size, rec.open_level, rec.stop_level, rec.opened_at)


def _exit_reason(closed: ClosedDeal) -> str:
    return "STOP" if closed.reason == "STOP" else "FORCED"


def _record(
    position: PositionRecord,
    decision_bid: Decimal,
    decision_ask: Decimal,
    closed: ClosedDeal,
    costs: CostBreakdown,
    signal_ts: datetime,
    submit_ts: datetime,
    confirm_ts: datetime,
    exit_reason: str,
) -> TradeRecord:
    latency_ms = max(0, (confirm_ts - signal_ts) // _MILLISECOND)  # zegar brokera może się różnić od naszego
    return TradeRecord(
        instrument=position.epic, side=position.side, size=position.size, decision_bid=decision_bid,
        decision_ask=decision_ask, entry_fill=position.open_level, exit_fill=closed.exit_level,
        gross_pnl=costs.gross_pnl, spread_cost=costs.spread_cost, slippage_penalty=costs.slippage_penalty,
        financing=costs.financing, fees=costs.fees, net_pnl=costs.net_pnl, signal_ts=signal_ts, submit_ts=submit_ts,
        confirm_ts=confirm_ts, exit_ts=closed.ts, latency_ms=latency_ms, exit_reason=exit_reason,
    )  # fmt: skip


def _ctx_decimal(ctx: dict[str, Any], key: str) -> Decimal:
    text = ctx[key]
    if not isinstance(text, str):
        raise ValueError(f"entry context {key}: expected a decimal string, got {text!r}")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError(f"entry context {key}: expected a finite decimal, got {text!r}")
    return value


def _ctx_time(ctx: dict[str, Any], key: str) -> datetime:
    value = datetime.fromisoformat(ctx[key])
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"entry context {key}: expected a UTC timestamp, got {ctx[key]!r}")
    return value


def _max_loss_distance(max_loss: Decimal, size: Decimal, open_level: Decimal, rules: InstrumentRules) -> Decimal | None:
    """Odległość stopu, przy której strata to najwyżej `max_loss`: w dół (dokładnie, na ułamkach) do kroku stopu
    brokera, a bez kroku do dokładności ceny otwarcia. None, gdy nie da się jej wyznaczyć."""
    vpp = rules.value_per_point
    if not (size > 0 and vpp > 0 and max_loss > 0):
        return None
    step = rules.stop_step if rules.stop_step > 0 else Decimal(1).scaleb(open_level.as_tuple().exponent)
    steps = math.floor(Fraction(max_loss) / (Fraction(size) * Fraction(vpp) * Fraction(step)))
    return step * steps if steps > 0 else None
