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
- po CONFIRMED kolejność: brak stopu → zabezpieczenie; potem obie straże: poślizg wejścia względem notowania decyzji
  (> `slippage_guard_fraction` × stop) i ryzyko FAKTYCZNEJ wielkości i odległości stopu (> `max_loss_per_trade`);
  którakolwiek → zdarzenie i natychmiastowe spłaszczenie; dopiero potem znacznik `fill_guarded:<deal_id>` i zapis
  pomocniczy `entry_by_deal`. Zdarzenia straży, znacznik i zapis pomocniczy są „wg możliwości": ich awaria nie
  zatrzymuje ochrony, tylko kończy się kill switchem miękkim JOURNAL_WRITE_FAILED;
- te same straże dla wypełnienia przyjętego przez uzgadnianie (zgubione potwierdzenie, restart lub awaria dziennika po
  wysyłce): `guard_adopted_fill` po każdym udanym przebiegu uzgadniania (po zabezpieczeniu stopem), dla pozycji OPEN
  bez znacznika `fill_guarded`; z kontekstem wejścia obie straże, bez niego ryzyko wielkości z odległości stopu
  pozycji i wartości punktu brokera. Znacznik (nie wynik uzgadniania) jest kluczem idempotencji: zapisany, gdy żadna
  straż nie zadziałała, albo po spłaszczeniu, które wyprowadziło pozycję z OPEN, więc przerwanie w dowolnym miejscu
  kończy się ponowieniem straży;
- spłaszczenie (`flatten`): OPEN → EXITING (BLOCKED, ENTERING, FLAT: odmowa ze zdarzeniem), jedno zamknięcie; po
  timeoucie lub odmowie NAJPIERW lista pozycji, nigdy druga wysyłka na ślepo: zamknięcie idzie do brokera tylko przy
  przejściu do EXITING albo gdy lista pozycji właśnie potwierdziła, że pozycja nadal jest (ponowienie na pozycji
  EXITING zaczyna się od listy; deal, którego zamknięcie poszło już w tym procesie, także przy dzienniku OPEN po
  nieudanym zapisie EXITING: pamięć wyjść `ExitMemory`, przekazywana wykonawcy odbudowanemu przy zejściu do dry_run);
  zamknięty rynek: EXITING, stop u brokera zostaje, alarm. Zwykłe spłaszczenie zapisuje
  EXITING PRZED zamknięciem (spec §8: po restarcie nie wysyłamy na ślepo), a jego błąd dziennika propaguje się;
- nigdy FLAT bez zaksięgowania: zamknięcie z historii (do `SETTLE_ATTEMPTS` prób) → `book` → FLAT; gdy historia go nie
  pokazuje, pozycja zostaje EXITING i księguje ją później uzgadnianie (`Reconciler(booker=executor)`);
- `book` jest idempotentny (`journal.has_trade` przed czymkolwiek innym; jedyny zapis wyniku to `journal.record_trade`
  po deal_id, który sam dolicza wynik dnia) i nie zawodzi na wiarygodnych danych: każdy błąd budowy faktów lub kosztów
  → rekord zdegradowany, ale zaksięgowany (COST_FALLBACK); błąd dziennika propaguje się (pozycja nie jest FLAT, więc
  kolejny przebieg księguje ponownie);
- przyczyna wyjścia w księdze: STOP, gdy zamknął stop brokera; FORCED, gdy wyjście zainicjował silnik (zapis
  `exit_intent:<deal_id>` tuż przed wysłaniem zamknięcia, nigdy, gdy nic nie wysłano) albo broker wymusił zamknięcie;
  MANUAL, gdy zamknięto wywołaniem bez zamiaru silnika (właściciel);
- kontrole limitów strat (`enforce_loss_limits`, także po każdym księgowaniu, również ponownym): dzień ≤
  −`max_daily_loss` → miękki, ≤ −2 × `max_daily_loss` → twardy, łącznie ≤ −`max_total_drawdown` → twardy; idempotentne,
  więc silnik może je ponowić przy starcie (poziom utracony przez przerwanie wraca z liczników dziennika);
- działania ochronne nie zależą od zapisów dziennika: w procedurze zabezpieczenia zawsze biegną wszystkie próby stopu,
  awaryjne spłaszczenie WYSYŁA zamknięcie nawet wtedy, gdy zapis EXITING zawiedzie (dziennik wg możliwości, alarm,
  kill switch miękki JOURNAL_WRITE_FAILED; uzgadnianie dogoni stan), a zdarzenie PROTECTION_FAILED pisze się dopiero po
  awaryjnym spłaszczeniu;
- pozycja bez stopu: stop (3 próby co 3 s) albo kill switch twardy UNPROTECTED_POSITION i awaryjne spłaszczenie;
  HARD włącza się także wtedy, gdy zabezpieczenie przerwie wyjątek lub anulowanie (wtedy spłaszczenie wykona silnik);
- awaryjne spłaszczenie: no-op przy FLAT; inaczej do `EMERGENCY_ATTEMPTS` prób ze skróconym limitem czasu; pozycja
  BLOCKED tylko przy kill switchu HARD w tej chwili i tylko wtedy, gdy jest dowodnie nasza (zapis ma deal_id, a lista
  brokera właśnie go pokazuje; zapis EXITING z widoku brokera); obcej (bez deal_id) nie dotykamy nigdy; nieczytelny
  dziennik → tylko ostatni NASZ deal tego procesu, który lista brokera właśnie pokazuje (EMERGENCY_FLATTEN_JOURNAL_
  UNREADABLE); bez FLAT → alarm EMERGENCY_FLATTEN_INCOMPLETE (pozycja pod nadzorem, kill switch zostaje HARD do
  decyzji właściciela); alarm JOURNAL_WRITE_FAILED po procedurze zamknięcia mówi, czy zamknięcie poszło do brokera;
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
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
    KillLevel,
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

ROLE_ENTRY = "entry"  # rola identyfikatora wejścia strategii (liczona przez ENTRIES_PER_DAY)
ROLE_SMOKE = "smoke"  # rola identyfikatora próby akceptacyjnej L0 (nie zużywa dziennego wejścia)

META_ENTRY_CTX = "entry_ctx:{}"  # client_id → kontekst wejścia (JSON), zapisany przed wysyłką
META_ENTRY_BY_DEAL = "entry_by_deal:{}"  # deal_id → client_id, zapisany po CONFIRMED (pomocniczy)
META_ENTRY_LAST = "entry_last:{}"  # epic → client_id ostatniego wejścia (pozycja przyjęta przez uzgadnianie)
META_EXIT_INTENT = "exit_intent:{}"  # deal_id → przyczyna wyjścia zainicjowanego przez silnik (wg możliwości)
# deal_id → wynik straży wypełnienia: PASS, przyczyny złączone `+` albo NO_CONTEXT_PASS (klucz idempotencji straży)
META_FILL_GUARDED = "fill_guarded:{}"
FILL_GUARD_PASS = "PASS"
FILL_GUARD_NO_CONTEXT_PASS = "NO_CONTEXT_PASS"

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
OUTCOME_FLAT = "FLAT"  # zamknięte i zaksięgowane (FLAT, albo dziennik dogoni to w uzgadnianiu)
OUTCOME_MARKET_CLOSED = "MARKET_CLOSED"  # rynek zamknięty: stop u brokera zostaje
OUTCOME_STILL_OPEN = "STILL_OPEN"  # zamknięcie zawiodło, lista potwierdza otwartą pozycję
OUTCOME_UNVERIFIED = "UNVERIFIED"  # nie da się odczytać listy pozycji (nic nie wysłano ponownie)
OUTCOME_AWAIT_HISTORY = "AWAIT_HISTORY"  # pozycji nie ma u brokera, zamknięcie jeszcze nie w historii
OUTCOME_BOOKING_FAILED = "BOOKING_FAILED"  # zamknięcie znalezione, księgowanie zawiodło
OUTCOME_REFUSED = "REFUSED"  # pozycja w dzienniku nie nadaje się do spłaszczenia
OUTCOME_NOT_AT_BROKER = "NOT_AT_BROKER"  # BLOCKED z deal_id, którego broker nie pokazuje: nie dotykamy
OUTCOME_UNKNOWN_DEAL = "UNKNOWN_DEAL"  # dziennik nieczytelny, a ten proces nie widział naszego deala: nic nie zamykamy
OUTCOME_CLOSED_UNBOOKED = "CLOSED_UNBOOKED"  # dziennik nieczytelny, nasz deal zamknięty; zaksięguje uzgadnianie
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
_HELD = frozenset({_P.OPEN, _P.EXITING, _P.BLOCKED})  # stany, w których deal_id zapisu dziennika jest naszą pozycją
_ZERO = Decimal("0")
_MILLISECOND = timedelta(milliseconds=1)

# Zapisy „wg możliwości": lista, do której trafia opis nieudanego zapisu (przebieg idzie dalej); None: zapis
# obowiązkowy (błąd dziennika propaguje się).
Failures = list[str] | None


class BookingError(Exception):
    """Nie da się zbudować nawet rekordu zdegradowanego (np. brak wartości punktu, pozycja bez kierunku)."""


class _NoContext(Exception):
    """Brak kontekstu wejścia dla pozycji (obca lub przyjęta bez naszego zapisu): rekord zdegradowany."""


@dataclass
class ExitMemory:
    """Pamięć wyjść TEGO procesu (nie przeżywa restartu; silnik przekazuje ją wykonawcy odbudowanemu przy zejściu do
    dry_run). Uzupełnia dziennik, gdy jego zapis „wg możliwości" zawiódł.

    `closes_sent`: deale, dla których `close_position` poszło do brokera (sukces albo niejasny błąd; odmowa przy
    zamkniętym rynku się nie liczy): kolejna próba zawsze zaczyna od listy pozycji, nawet gdy dziennik nadal mówi OPEN.
    `last_own_deal`: ostatni NASZ deal widziany w dzienniku (OPEN/EXITING/BLOCKED) albo przekazany zabezpieczeniu;
    awaryjne spłaszczenie przy nieczytelnym dzienniku zamyka tylko jego (i tylko gdy broker go pokazuje).
    """

    closes_sent: set[str] = field(default_factory=set)
    last_own_deal: str | None = None


async def _with_timeout(call: Awaitable[T], timeout: float) -> T:
    """Wywołanie brokera z limitem czasu; przekroczenie to `TimeoutError` (niejasność, nie porażka)."""
    return await asyncio.wait_for(call, timeout)


class Executor:
    """Wykonuje wejście, wyjście i spłaszczenie oraz księguje zamknięcia (patrz docstring modułu).

    `live=True` (dokładnie `True`) uzbraja wysyłkę zleceń; domyślnie wykonawca pracuje jak `dry_run`. Jeden wywołujący
    naraz (silnik szereguje wykonawcę i uzgadnianie jedną blokadą). `memory`: pamięć wyjść poprzedniego wykonawcy tego
    samego procesu (silnik przy zejściu do dry_run); domyślnie pusta.
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
        *,
        memory: ExitMemory | None = None,
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
        self._memory = memory if memory is not None else ExitMemory()
        self._closes_started = 0  # wywołania `close_position` w tym wykonawcy (czy procedura cokolwiek wysłała)

    @property
    def live(self) -> bool:
        """Czy wykonawca wysyła zlecenia (tylko przy `live=True` w konstruktorze)."""
        return self._live

    @property
    def memory(self) -> ExitMemory:
        """Pamięć wyjść tego procesu (do przekazania wykonawcy odbudowanemu przez silnik)."""
        return self._memory

    # --- wejście ----------------------------------------------------------------------------------------

    async def enter(
        self, signal: EntrySignal, rules: InstrumentRules, day: date, *, smoke: bool = False
    ) -> OrderRecord | None:
        """Wejście z sygnału strategii; `day` to dzień handlowy (identyfikator zlecenia i jego numer kolejny).

        `smoke=True`: próba akceptacyjna L0 (polecenie `smoke`): ta sama ścieżka, ale bramka `check_smoke` (bez okna,
        sesji i limitu wejść dnia) i rola identyfikatora `smoke`, więc zlecenie nie zużywa dziennego wejścia strategii.
        Tryb `dry_run` także tu nigdy nie wysyła zlecenia.

        None: bramka zablokowała (`gate_block`), tryb `dry_run` (`decision`), konto niedostępne (ACCOUNT_UNAVAILABLE)
        albo awaria dziennika przed wysyłką lub z menedżera (kill miękki JOURNAL_WRITE_FAILED). Inaczej rekord zlecenia
        z menedżera: CONFIRMED (pozycja mogła zostać od razu spłaszczona przez straż), REJECTED, NOT_PLACED albo
        UNKNOWN (kill miękki ENTRY_UNRESOLVED). Wyjątek inny niż błąd zapisu dziennika: kill miękki ENTRY_EXCEPTION
        i wyjątek dalej; `CancelledError` bez żadnej reakcji.
        """
        try:
            request = await self._prepare(signal, rules, day, smoke)
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

    async def _prepare(
        self, signal: EntrySignal, rules: InstrumentRules, day: date, smoke: bool = False
    ) -> EntryRequest | None:
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

        verdict = self._gate.check_smoke(candidate, account) if smoke else self._gate.check_entry(candidate, account)
        if not verdict.passed:
            block = {"reasons": verdict.reasons, "candidate": candidate, "day": day.isoformat()}
            if smoke:
                block["smoke"] = True
            self._append(SOURCE_GATE_BLOCK, block)
            return None
        role = ROLE_SMOKE if smoke else ROLE_ENTRY
        client_id = make_client_id(day, self._epic, self._journal.next_seq(day), role)
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
        """Kontrakt z menedżerem (T7), potem ochrona: zabezpieczenie, straże i ich spłaszczenie; zapisy pomocnicze na
        końcu i wg możliwości (ich awaria = kill miękki, nigdy pominięta ochrona)."""
        position = self._journal.current_position(self._epic)
        if rec.state not in _RESOLVED or (position is not None and position.state is _P.BLOCKED):
            self._unresolved(rec, position)
            return
        if rec.state is not _O.CONFIRMED:
            return  # REJECTED/NOT_PLACED: zapisane przez menedżera, pozycja FLAT, próba dnia zużyta
        if position is None or position.state is not _P.OPEN or position.deal_id != rec.deal_id:
            self._unresolved(rec, position)
            return
        self._remember_deal(position)
        if position.stop_level is None:
            if not await self._protect(_as_open(position), signal.stop_distance):
                return  # kill switch twardy i awaryjne spłaszczenie już wykonane
        failures: list[str] = []
        quote = signal.decision_quote
        closes_before: int | None = None
        try:
            causes = self._guard_values(
                position, quote.bid, quote.ask, signal.stop_distance, rules.value_per_point, signal.size, failures
            )
            if causes:
                closes_before = self._closes_started
                await self.flatten(position, "FORCED")
            self._mark_fill_guarded(position, causes, FILL_GUARD_PASS, failures)
            self._set_meta(META_ENTRY_BY_DEAL.format(rec.deal_id), rec.order_id, best_effort=failures)
        finally:
            self._report_journal_failures(failures, f"entry {rec.order_id}", closes_before=closes_before)

    async def guard_adopted_fill(self) -> bool:
        """Straże po wypełnieniu (poślizg, ryzyko faktycznej wielkości) dla pozycji, której wejścia nie sprawdził
        `enter` (przyjętej przez uzgadnianie); silnik woła ją po każdym udanym przebiegu uzgadniania, po zabezpieczeniu.

        Nic nie robi (żadnego wywołania brokera), gdy pozycja w dzienniku nie jest OPEN z deal_id, ma już znacznik
        `fill_guarded`, albo na instrumencie jest zlecenie nierozstrzygnięte (najpierw uzgadnianie). Z kontekstem
        wejścia: notowanie decyzji, żądana wielkość, odległość stopu i wartość punktu z kontekstu. Bez niego (albo
        z nieczytelnym): tylko ryzyko wielkości z odległości stopu pozycji i wartości punktu brokera; pozycja bez stopu
        czeka na zabezpieczenie, nieczytelne reguły brokera → alarm FILL_GUARD_UNAVAILABLE i ponowienie w następnym
        przebiegu. Zadziałała straż → spłaszczenie FORCED. True: straż zadziałała i spłaszczenie ruszyło. Błąd zapisu
        obowiązkowego spłaszczenia (EXITING) propaguje się; znacznika wtedy nie ma, więc straż pobiegnie znowu.
        """
        position = self._journal.current_position(self._epic)
        self._remember_deal(position)  # każdy przebieg: ostatni nasz deal dla awaryjnego spłaszczenia bez dziennika
        if position is None or position.state is not _P.OPEN or position.deal_id is None:
            return False
        deal_id = position.deal_id
        if self._journal.get_meta(META_FILL_GUARDED.format(deal_id)) is not None:
            return False
        if any(o.epic == self._epic for o in self._journal.unresolved_orders()):
            return False  # pozycja może jeszcze należeć do zlecenia bez deal_id: kontekst nie byłby pewny
        failures: list[str] = []
        closes_before: int | None = None
        try:
            found = self._guard_context(deal_id, position.epic)
            if found is not None:
                order_id, ctx = found
                causes = self._guard_values(
                    position, ctx["decision_bid"], ctx["decision_ask"], ctx["stop_distance"], ctx["value_per_point"],
                    ctx["size"], failures, adopted=True,
                )  # fmt: skip
                source, passed = "entry_ctx", FILL_GUARD_PASS
            else:
                if position.stop_level is None:
                    return False  # ochrona stopem najpierw (zabezpieczenie); straż w następnym przebiegu
                order_id = self._entry_client_id(deal_id, position.epic)
                vpp = await self._guard_rules_vpp(position, failures)
                if vpp is None:
                    return False
                causes = self._guard_values(position, None, None, None, vpp, None, failures, adopted=True)
                source, passed = "broker_rules", FILL_GUARD_NO_CONTEXT_PASS
            self._event(
                "FILL_GUARD_ADOPTED", best_effort=failures, deal_id=deal_id, order_id=order_id, causes=causes,
                source=source,
            )  # fmt: skip
            if causes:
                closes_before = self._closes_started
                await self.flatten(position, "FORCED")
            self._mark_fill_guarded(position, causes, passed, failures)
            if found is not None:
                self._set_meta(META_ENTRY_BY_DEAL.format(deal_id), order_id, best_effort=failures)
            return bool(causes)
        finally:
            self._report_journal_failures(failures, f"fill guard of {deal_id}", closes_before=closes_before)

    def _guard_context(self, deal_id: str, epic: str) -> tuple[str, dict[str, Decimal]] | None:
        """(client_id, wartości) z kontekstu wejścia pozycji; nieczytelny albo bez sensu = brak (None). Błąd dziennika
        propaguje się."""
        try:
            ctx = self._context(deal_id, epic)
            if ctx is None:
                return None
            keys = ("decision_bid", "decision_ask", "stop_distance", "value_per_point", "size")
            values = {key: _ctx_decimal(ctx, key) for key in keys}
        except JournalError:
            raise
        except Exception:  # zły JSON, brak pola, zły typ: jak brak kontekstu
            return None
        if not (values["stop_distance"] > 0 and values["value_per_point"] > 0 and values["size"] > 0):
            return None
        return ctx["client_id"], values

    async def _guard_rules_vpp(self, position: PositionRecord, failures: list[str]) -> Decimal | None:
        """Wartość punktu od brokera dla straży bez kontekstu (limit czasu jak w zabezpieczeniu); None: alarm."""
        deal_id = position.deal_id
        try:
            rules = await _with_timeout(
                self._broker.instrument_rules(position.epic), min(self._timeout, PROTECT_CALL_TIMEOUT_S)
            )
        except Exception as exc:
            self._raw_error(SOURCE_RULES, exc, best_effort=failures, deal_id=deal_id, context="fill_guard")
            self._alert(
                "WARN",
                f"FILL_GUARD_UNAVAILABLE {deal_id}: instrument rules unreadable ({type(exc).__name__}: {exc}); the "
                "size risk of the adopted fill is checked on the next reconciliation pass",
            )
            return None
        self._raw(SOURCE_RULES, best_effort=failures, deal_id=deal_id, context="fill_guard", response=rules)
        if not rules.value_per_point > 0:
            self._alert("WARN", f"FILL_GUARD_UNAVAILABLE {deal_id}: value per point {rules.value_per_point}; retrying")
            return None
        return rules.value_per_point

    def _mark_fill_guarded(
        self, position: PositionRecord, causes: list[str], passed: str, failures: list[str]
    ) -> None:
        """Znacznik `fill_guarded` (wg możliwości): bez przyczyny od razu; po zadziałaniu straży tylko wtedy, gdy
        spłaszczenie wyprowadziło pozycję z OPEN (inaczej, także przy nieczytelnym dzienniku, straż pobiegnie znowu)."""
        deal_id = position.deal_id
        if causes:
            try:
                current = self._journal.current_position(position.epic)
            except JournalError:
                return
            if current is not None and current.state is _P.OPEN and current.deal_id == deal_id:
                return
        value = "+".join(causes) if causes else passed
        self._set_meta(META_FILL_GUARDED.format(deal_id), value, best_effort=failures)

    def _unresolved(self, rec: OrderRecord, position: PositionRecord | None) -> None:
        state = None if position is None else position.state
        self._kill.soft(ENTRY_UNRESOLVED)
        self._alert(
            "ERROR",
            f"{ENTRY_UNRESOLVED}: order {rec.order_id} {rec.state.value} ({rec.note}), position "
            f"{'missing' if state is None else state.value}; reconciliation or the owner decides, no new entry",
        )
        self._event(ENTRY_UNRESOLVED, order_id=rec.order_id, state=rec.state, note=rec.note, position=state)

    def _guard_values(
        self,
        position: PositionRecord,
        decision_bid: Decimal | None,
        decision_ask: Decimal | None,
        stop_distance: Decimal | None,
        value_per_point: Decimal,
        requested_size: Decimal | None,
        failures: list[str],
        *,
        adopted: bool = False,
    ) -> list[str]:
        """Straż poślizgu (spec §5 pkt 7; tylko ze znanym notowaniem decyzji i odległością stopu) i ryzyko faktycznej
        wielkości (zawsze); obie sprawdzane, zdarzenia (wg możliwości) przed zamknięciem. Odległość dla ryzyka: stop
        pozycji, a bez niego `stop_distance` (stop ustawiony przed chwilą w tej odległości). `adopted`: wypełnienie
        przyjęte przez uzgadnianie. Zwraca przyczyny natychmiastowego spłaszczenia."""
        causes: list[str] = []
        if decision_bid is not None and decision_ask is not None and stop_distance is not None:
            if position.side is Side.BUY:
                deviation = position.open_level - decision_ask  # BUY kupuje po ask: gorzej = wyżej
            else:
                deviation = decision_bid - position.open_level  # SELL sprzedaje po bid: gorzej = niżej
            limit = self._strat.slippage_guard_fraction * stop_distance
            if deviation > limit:
                causes.append("SLIPPAGE_GUARD")
                self._event(
                    "SLIPPAGE_GUARD", best_effort=failures, deal_id=position.deal_id, side=position.side,
                    decision_bid=decision_bid, decision_ask=decision_ask, open_level=position.open_level,
                    deviation=deviation, limit=limit, adopted=adopted,
                )  # fmt: skip
        if position.stop_level is not None:
            distance = abs(position.open_level - position.stop_level)
        elif stop_distance is not None:
            distance = stop_distance
        else:  # wywołujący gwarantuje stop albo odległość: straży ryzyka nigdy nie pomijamy po cichu
            raise ValueError(f"fill guard of {position.deal_id}: neither a stop level nor a stop distance")
        risk = position.size * distance * value_per_point
        if risk > self._risk.max_loss_per_trade:
            causes.append("SIZE_MISMATCH_RISK")
            self._event(
                "SIZE_MISMATCH_RISK", best_effort=failures, deal_id=position.deal_id, size=position.size,
                requested=requested_size, distance=distance, risk=risk, limit=self._risk.max_loss_per_trade,
                adopted=adopted,
            )  # fmt: skip
        if causes:
            where = " (adopted by reconciliation)" if adopted else ""
            self._alert("ERROR", f"{' + '.join(causes)}: closing {position.deal_id} at once{where}")
        return causes

    # --- spłaszczenie -------------------------------------------------------------------------------------

    async def flatten(self, position: PositionRecord | OpenPosition, reason: str) -> ClosedDeal | None:
        """Zamyka NASZĄ pozycję i księguje zamknięcie; zwraca je, gdy pozycja jest już FLAT (zaksięgowana).

        `position`: rekord z dziennika (albo widok brokera); liczą się wyłącznie `epic` i `deal_id`, a źródłem prawdy
        jest bieżący rekord dziennika (musi być OPEN lub EXITING z tym deal_id, inaczej odmowa; BLOCKED zamyka tylko
        awaryjne spłaszczenie). `reason` (np. FORCED, MANUAL, EMERGENCY) trafia do zdarzeń i do `exit_intent`.
        None: rynek zamknięty, zamknięcie niepotwierdzone, pozycja nadal otwarta albo zamknięcie czeka na historię
        (pozycja EXITING; ponowić przy następnym takcie albo zaksięguje uzgadnianie). Ponowienie na pozycji EXITING
        (albo OPEN, gdy zamknięcie tego deala poszło już w tym procesie) najpierw czyta listę pozycji: zniknęła →
        księgowanie bez zamknięcia, jest → zamknięcie, nieczytelna → nic nie wysyłamy. Błąd zapisu obowiązkowego
        (EXITING, surowe odpowiedzi, księga) propaguje się; kill switch nigdy nie blokuje wyjścia.
        """
        failures: list[str] = []
        closes_before = self._closes_started
        try:
            closed, _ = await self._flatten(position, reason, self._timeout, failures=failures)
        finally:
            self._report_journal_failures(failures, f"flatten {position.deal_id}", closes_before=closes_before)
        return closed

    async def _flatten(
        self,
        position: PositionRecord | OpenPosition,
        reason: str,
        timeout: float,
        *,
        failures: list[str],
        verified: bool = False,
        emergency: bool = False,
    ) -> tuple[ClosedDeal | None, str]:
        """Jedna próba spłaszczenia.

        `failures`: dokąd trafiają nieudane zapisy wg możliwości. `verified=True`: ta sama procedura przed chwilą
        potwierdziła listą, że pozycja jest otwarta (bez ponownego odczytu). `emergency=True`: każdy zapis dziennika
        jest wg możliwości (zamknięcie idzie do brokera także przy awarii dziennika), a pozycję BLOCKED wolno zamknąć,
        gdy jest dowodnie nasza (deal_id na liście brokera).
        """
        must: Failures = failures if emergency else None
        epic, deal_id = position.epic, position.deal_id
        current = self._journal.current_position(epic)
        allowed = _FLATTENABLE | {_P.BLOCKED} if emergency else _FLATTENABLE
        if current is None or deal_id is None or current.deal_id != deal_id or current.state not in allowed:
            state = "MISSING" if current is None else current.state.value
            self._alert(
                "ERROR" if state == _P.BLOCKED.value else "WARN",
                f"FLATTEN_REFUSED {deal_id}: journal position is {state} "
                f"(deal {None if current is None else current.deal_id}); nothing sent",
            )
            self._event(
                "FLATTEN_REFUSED", best_effort=must, deal_id=deal_id, flatten_reason=reason, journal_state=state,
                journal_deal_id=None if current is None else current.deal_id,
            )  # fmt: skip
            return None, OUTCOME_REFUSED
        self._remember_deal(current)

        if current.state is _P.BLOCKED:
            # Tylko awaryjnie, tylko przy kill switchu HARD w tej chwili i tylko nasz deal, który broker teraz pokazuje;
            # zapis EXITING z widoku brokera (to jest pozycja, którą zamykamy i księgujemy, np. inna wielkość po
            # SIZE_MISMATCH).
            if self._kill.level() is not KillLevel.HARD:
                self._alert("ERROR", f"FLATTEN_REFUSED {deal_id}: journal position is BLOCKED and the kill switch is "
                                     "not HARD; nothing sent")  # fmt: skip
                self._event(
                    "FLATTEN_REFUSED", best_effort=must, deal_id=deal_id, flatten_reason=reason,
                    journal_state=current.state, journal_deal_id=deal_id, why="NOT_HARD",
                )  # fmt: skip
                return None, OUTCOME_REFUSED
            read_ok, at_broker, error = await self._find_at_broker(deal_id, timeout, must)
            if not read_ok:
                self._alert("ERROR", f"FLATTEN_UNVERIFIED {deal_id}: BLOCKED, position list unreadable; not touched")
                self._event(
                    "FLATTEN_UNVERIFIED", best_effort=must, deal_id=deal_id, flatten_reason=reason, error=error,
                    stage="blocked",
                )  # fmt: skip
                return None, OUTCOME_UNVERIFIED
            if at_broker is None:
                self._alert("ERROR", f"BLOCKED_NOT_AT_BROKER {deal_id}: not listed by the broker; left to the owner")
                self._event("BLOCKED_NOT_AT_BROKER", best_effort=must, deal_id=deal_id, flatten_reason=reason)
                return None, OUTCOME_NOT_AT_BROKER
            self._event(
                "BLOCKED_EMERGENCY_FLATTEN", best_effort=must, deal_id=deal_id, journal=current, broker=at_broker
            )
            current = self._to_exiting(current, _from_broker(_P.EXITING, at_broker), must)
        else:
            sent_before = deal_id in self._memory.closes_sent
            if current.state is _P.OPEN:
                current = self._to_exiting(current, replace(current, state=_P.EXITING), must)
                recheck = sent_before and not verified  # dziennik mówił OPEN, choć zamknięcie już poszło (zapis zawiódł)
            else:
                recheck = not verified
            if recheck:
                # EXITING z wcześniejszej próby (przerwanie, odmowa, nieczytelna lista) albo zamknięcie wysłane już
                # w tym procesie: mogło dojść, więc zanim wyślemy kolejne, broker musi potwierdzić, że pozycja nadal jest
                read_ok, at_broker, error = await self._find_at_broker(deal_id, timeout, must)
                if not read_ok:
                    self._alert("ERROR", f"FLATTEN_UNVERIFIED {deal_id}: position list unreadable before a retry")
                    self._event(
                        "FLATTEN_UNVERIFIED", best_effort=must, deal_id=deal_id, flatten_reason=reason, error=error,
                        stage="before_retry",
                    )  # fmt: skip
                    return None, OUTCOME_UNVERIFIED
                if at_broker is None:
                    return await self._settle(current, reason, timeout, must)

        sent_before = self._before_close(deal_id, reason, failures)
        try:
            await _with_timeout(self._broker.close_position(deal_id), timeout)
        except MarketClosed as exc:
            if not sent_before:  # odmowa zamkniętego rynku: to zamknięcie na pewno nie doszło
                self._memory.closes_sent.discard(deal_id)
            self._raw_error(SOURCE_CLOSE, exc, best_effort=must, deal_id=deal_id, flatten_reason=reason)
            self._alert(
                "WARN", f"FLATTEN_BLOCKED_MARKET_CLOSED {deal_id}: the stop at the broker stays, retry after reopening"
            )
            self._event("FLATTEN_BLOCKED_MARKET_CLOSED", best_effort=must, deal_id=deal_id, flatten_reason=reason)
            return None, OUTCOME_MARKET_CLOSED
        except Exception as exc:  # timeout (także asyncio), odmowa, wygasła sesja: nie wiemy, czy zamknięto
            self._raw_error(SOURCE_CLOSE, exc, best_effort=must, deal_id=deal_id, flatten_reason=reason)
            read_ok, at_broker, _ = await self._find_at_broker(deal_id, timeout, must)
            if not read_ok or at_broker is not None:
                code, outcome = (
                    ("FLATTEN_UNVERIFIED", OUTCOME_UNVERIFIED) if not read_ok
                    else ("FLATTEN_STILL_OPEN", OUTCOME_STILL_OPEN)
                )  # fmt: skip
                self._alert("ERROR", f"{code} {deal_id}: close failed ({type(exc).__name__}: {exc}); not resent")
                self._event(
                    code, best_effort=must, deal_id=deal_id, flatten_reason=reason, error=type(exc).__name__,
                    message=str(exc), stage="after_close",
                )  # fmt: skip
                return None, outcome
        else:
            self._raw(SOURCE_CLOSE, best_effort=must, deal_id=deal_id, flatten_reason=reason, response=None)
        return await self._settle(current, reason, timeout, must)

    def _before_close(self, deal_id: str, reason: str, failures: list[str]) -> bool:
        """Tuż przed `close_position` (i tylko wtedy): pamięć wysłanych zamknięć (także anulowanie w trakcie to
        niejasność), licznik wysyłek i zamiar wyjścia silnika w dzienniku (wg możliwości). Zwraca, czy zamknięcie tego
        deala poszło już wcześniej w tym procesie."""
        sent_before = deal_id in self._memory.closes_sent
        self._memory.closes_sent.add(deal_id)
        self._closes_started += 1
        self._set_meta(META_EXIT_INTENT.format(deal_id), reason, best_effort=failures)
        return sent_before

    def _remember_deal(self, record: PositionRecord | None) -> None:
        """Ostatni NASZ deal (zapis dziennika z deal_id w stanie OPEN/EXITING/BLOCKED) dla awaryjnego spłaszczenia przy
        nieczytelnym dzienniku."""
        if record is not None and record.deal_id is not None and record.state in _HELD:
            self._memory.last_own_deal = record.deal_id

    def _to_exiting(self, current: PositionRecord, target: PositionRecord, must: Failures) -> PositionRecord:
        """Przejście do EXITING (automat stanów); w trybie awaryjnym zapis wg możliwości (zwraca stan docelowy)."""
        check_position_transition(current.state, _P.EXITING)
        self._write(must, lambda: self._journal.upsert_position(target))
        return target

    async def _find_at_broker(
        self, deal_id: str, timeout: float, must: Failures
    ) -> tuple[bool, OpenPosition | None, str | None]:
        """Lista pozycji zamiast wysyłki na ślepo: (odczyt udany, pozycja brokera z tym deal_id lub None, błąd)."""
        try:
            positions = await _with_timeout(self._broker.list_positions(), timeout)
        except Exception as exc:
            self._raw_error(SOURCE_POSITIONS, exc, best_effort=must, deal_id=deal_id, context="flatten")
            return False, None, type(exc).__name__
        self._raw(SOURCE_POSITIONS, best_effort=must, deal_id=deal_id, context="flatten", response=positions)
        return True, next((p for p in positions if p.deal_id == deal_id), None), None

    async def _settle(
        self, position: PositionRecord, reason: str, timeout: float, must: Failures
    ) -> tuple[ClosedDeal | None, str]:
        """Pozycji nie ma u brokera: zamknięcie z historii → księgowanie → FLAT. Bez zamknięcia zostaje EXITING."""
        deal_id = position.deal_id
        if position.opened_at is None:  # początku historii nie zgadujemy: zostaje uzgadnianiu
            self._await_history(position, reason, "HISTORY_START_UNKNOWN", must)
            return None, OUTCOME_AWAIT_HISTORY
        since = position.opened_at - HISTORY_MARGIN
        for attempt in range(1, SETTLE_ATTEMPTS + 1):
            await self._clock.sleep(SETTLE_PAUSE_S)
            try:
                closures = await _with_timeout(self._broker.closed_since(since), timeout)
            except Exception as exc:
                self._raw_error(SOURCE_CLOSED, exc, best_effort=must, deal_id=deal_id, since=since, attempt=attempt)
                continue
            self._raw(SOURCE_CLOSED, best_effort=must, deal_id=deal_id, since=since, attempt=attempt, response=closures)
            found = [c for c in closures if c.deal_id == deal_id and c.epic == position.epic]
            if len(found) > 1:  # nie zgadujemy, które zamknięcie jest prawdziwe: zablokuje uzgadnianie
                self._alert("ERROR", f"AMBIGUOUS_CLOSURE {deal_id}: {len(found)} closures in the history, not booked")
                self._event("AMBIGUOUS_CLOSURE", best_effort=must, deal_id=deal_id, count=len(found))
                return None, OUTCOME_AWAIT_HISTORY
            if found:
                closed = found[0]
                try:
                    await self._book(position, closed, timeout)
                except JournalError as exc:
                    if must is None:
                        raise
                    must.append(f"book {type(exc).__name__}: {exc}")  # awaryjnie: zamknięte, zaksięguje uzgadnianie
                    self._alert("ERROR", f"BOOKING_FAILED {deal_id}: journal {type(exc).__name__}; stays as is")
                    return None, OUTCOME_BOOKING_FAILED
                except Exception as exc:
                    self._alert("ERROR", f"BOOKING_FAILED {deal_id}: {type(exc).__name__}: {exc}; stays EXITING")
                    self._event(
                        "BOOKING_FAILED", best_effort=must, deal_id=deal_id, error=type(exc).__name__, message=str(exc)
                    )
                    return None, OUTCOME_BOOKING_FAILED
                self._finish_flat(position, closed, reason, must)
                return closed, OUTCOME_FLAT
        self._await_history(position, reason, f"NOT_IN_HISTORY_AFTER={SETTLE_ATTEMPTS}", must)
        return None, OUTCOME_AWAIT_HISTORY

    def _await_history(self, position: PositionRecord, reason: str, why: str, must: Failures) -> None:
        self._alert(
            "WARN",
            f"CLOSE_SENT_AWAIT_HISTORY {position.deal_id}: closed at the broker, closure not in history yet ({why}); "
            "position stays EXITING, reconciliation books it",
        )
        self._event(
            "CLOSE_SENT_AWAIT_HISTORY", best_effort=must, deal_id=position.deal_id, flatten_reason=reason, why=why
        )

    def _finish_flat(self, position: PositionRecord, closed: ClosedDeal, reason: str, must: Failures) -> None:
        """EXITING → FLAT po zaksięgowaniu, tylko gdy dziennik nadal mówi EXITING z tym deal_id."""
        current = self._journal.current_position(position.epic)
        state = None if current is None else current.state
        self._event(
            "FLATTENED", best_effort=must, deal_id=position.deal_id, flatten_reason=reason, close_reason=closed.reason,
            exit_level=closed.exit_level, journal_state=state,
        )  # fmt: skip
        if current is not None and current.state is _P.EXITING and current.deal_id == position.deal_id:
            check_position_transition(_P.EXITING, _P.FLAT)
            self._write(must, lambda: self._journal.upsert_position(PositionRecord(position.epic, _P.FLAT)))
        elif state is not _P.FLAT:  # ktoś zmienił stan w międzyczasie albo zapis EXITING zawiódł: dogoni uzgadnianie
            self._alert("WARN", f"FLATTENED {position.deal_id}: booked, journal position is {state}; left as is")

    # --- awaryjne spłaszczenie ----------------------------------------------------------------------------

    async def emergency_flatten(self) -> None:
        """Procedura spłaszczenia ze skróconym limitem czasu, do `EMERGENCY_ATTEMPTS` prób co `EMERGENCY_PAUSE_S`.

        No-op, gdy dziennik nie ma pozycji albo jest FLAT (silnik woła ją przy każdym takcie z kill switchem HARD).
        Zapisy dziennika są wg możliwości: zamknięcie idzie do brokera także wtedy, gdy zapis EXITING zawiedzie (alarm,
        kill switch miękki JOURNAL_WRITE_FAILED). Pozycja BLOCKED: tylko nasz deal_id, który lista brokera właśnie
        pokazuje; obcej nie dotykamy. Ponawia tylko, gdy pozycja może być nadal otwarta (zamknięcie zawiodło albo lista
        była nieczytelna); rynek zamknięty, zamknięcie czekające na historię, BLOCKED spoza brokera i ENTERING kończą
        próby. Bez FLAT: alarm EMERGENCY_FLATTEN_INCOMPLETE. Nieczytelny dziennik: zamknięcie tylko NASZEGO ostatniego
        deala tego procesu i tylko wtedy, gdy lista brokera go właśnie pokazuje (alarm
        EMERGENCY_FLATTEN_JOURNAL_UNREADABLE; zaksięguje uzgadnianie, gdy dziennik wróci); nieznanego nie zamykamy.
        """
        timeout = max(EMERGENCY_MIN_TIMEOUT_S, self._risk.submit_timeout_s / 2)
        failures: list[str] = []
        outcome, attempts = "", 0
        closes_before = self._closes_started
        try:
            for attempt in range(1, EMERGENCY_ATTEMPTS + 1):
                if attempt > 1:
                    await self._clock.sleep(EMERGENCY_PAUSE_S)
                try:
                    current = self._journal.current_position(self._epic)
                except JournalError as exc:  # ochrona nie czeka na dziennik
                    attempts = attempt
                    outcome = await self._flatten_without_journal(exc, timeout, failures)
                    if outcome == OUTCOME_CLOSED_UNBOOKED:
                        return
                    if outcome not in _EMERGENCY_RETRY:
                        break
                    continue
                if current is None or current.state is _P.FLAT:
                    return
                if current.state not in _FLATTENABLE | {_P.BLOCKED} or current.deal_id is None:
                    outcome = f"STATE_{current.state.value}"  # BLOCKED bez deal_id (obca) albo ENTERING: nie ruszamy
                    break
                attempts = attempt
                # STILL_OPEN: lista sprzed sekundy potwierdziła otwartą pozycję, więc zamknięcie bez kolejnego odczytu
                _, outcome = await self._flatten(
                    current, "EMERGENCY", timeout, failures=failures, verified=outcome == OUTCOME_STILL_OPEN,
                    emergency=True,
                )  # fmt: skip
                if outcome == OUTCOME_FLAT:
                    return
                if outcome not in _EMERGENCY_RETRY:
                    break
            try:
                current = self._journal.current_position(self._epic)
            except JournalError:
                deal_id, state = self._memory.last_own_deal, "UNKNOWN"  # dziennik nieczytelny: nie wiemy, nie FLAT
            else:
                if current is None or current.state is _P.FLAT:
                    return
                deal_id, state = current.deal_id, current.state.value
            self._alert(
                "ERROR",
                f"EMERGENCY_FLATTEN_INCOMPLETE: position {deal_id} {state} after {attempts} attempt(s) ({outcome}); "
                "kept under watch",
            )
            self._event(
                "EMERGENCY_FLATTEN_INCOMPLETE", best_effort=failures, deal_id=deal_id, state=state, attempts=attempts,
                outcome=outcome,
            )  # fmt: skip
        finally:
            self._report_journal_failures(failures, "emergency flatten", closes_before=closes_before)

    async def _flatten_without_journal(self, error: JournalError, timeout: float, failures: list[str]) -> str:
        """Jedna próba awaryjna przy nieczytelnym dzienniku: tylko ostatni NASZ deal tego procesu (widziany w dzienniku
        albo przekazany zabezpieczeniu), tylko gdy lista brokera go właśnie pokazuje (nigdy wysyłka na ślepo); każdy
        zapis wg możliwości, bez księgowania (zrobi je uzgadnianie z dziennika, który wróci)."""
        why = f"{type(error).__name__}: {error}"
        code = "EMERGENCY_FLATTEN_JOURNAL_UNREADABLE"
        deal_id = self._memory.last_own_deal
        if deal_id is None:
            self._alert(
                "ERROR", f"{code}: journal unreadable ({why}) and no own deal seen in this process; nothing closed, "
                "check the broker positions",
            )  # fmt: skip
            return OUTCOME_UNKNOWN_DEAL
        read_ok, at_broker, list_error = await self._find_at_broker(deal_id, timeout, failures)
        if not read_ok:
            self._alert("ERROR", f"{code}: journal unreadable ({why}), position list unreadable ({list_error}); "
                                 f"nothing sent for {deal_id}")  # fmt: skip
            return OUTCOME_UNVERIFIED
        if at_broker is None:
            self._alert("ERROR", f"{code}: journal unreadable ({why}); our deal {deal_id} is not listed by the broker; "
                                 "nothing closed, check the broker positions")  # fmt: skip
            return OUTCOME_NOT_AT_BROKER
        sent_before = self._before_close(deal_id, "EMERGENCY", failures)
        try:
            await _with_timeout(self._broker.close_position(deal_id), timeout)
        except MarketClosed as exc:
            if not sent_before:
                self._memory.closes_sent.discard(deal_id)
            self._raw_error(SOURCE_CLOSE, exc, best_effort=failures, deal_id=deal_id, flatten_reason="EMERGENCY")
            self._alert("ERROR", f"{code}: journal unreadable ({why}); {deal_id} not closed, market closed (the stop "
                                 "at the broker stays)")  # fmt: skip
            return OUTCOME_MARKET_CLOSED
        except Exception as exc:  # nie wiemy, czy zamknięto: kolejna próba zaczyna od listy
            self._raw_error(SOURCE_CLOSE, exc, best_effort=failures, deal_id=deal_id, flatten_reason="EMERGENCY")
            self._alert("ERROR", f"{code}: journal unreadable ({why}); close of {deal_id} failed "
                                 f"({type(exc).__name__}: {exc}); listed again before any retry")  # fmt: skip
            return OUTCOME_UNVERIFIED
        self._raw(SOURCE_CLOSE, best_effort=failures, deal_id=deal_id, flatten_reason="EMERGENCY", response=None)
        self._alert("ERROR", f"{code}: {deal_id} closed at the broker with the journal unreadable ({why}); "
                             "reconciliation books it once the journal is readable")  # fmt: skip
        return OUTCOME_CLOSED_UNBOOKED

    # --- zabezpieczenie stopem ----------------------------------------------------------------------------

    async def protect_unprotected(self, position: OpenPosition) -> bool:
        """Ustawia stop pozycji bez stopu (widok brokera: kierunek i poziom otwarcia brokera).

        Odległość: stop z kontekstu wejścia, a bez niego `max_loss_per_trade / (size × wartość_punktu)` w dół do kroku
        stopu brokera (bez kroku: do dokładności ceny otwarcia). Zawsze wszystkie próby (dziennik wg możliwości).
        True: stop ustawiony. False: kill switch twardy UNPROTECTED_POSITION, alarm i awaryjne spłaszczenie, dopiero
        potem zdarzenie PROTECTION_FAILED. Wyjątek lub anulowanie przed potwierdzonym stopem: HARD i wyjątek dalej
        (awaryjne spłaszczenie wykona silnik, który woła je przy każdym takcie z HARD).
        """
        return await self._protect(position, None)

    async def _protect(self, position: OpenPosition, distance: Decimal | None) -> bool:
        # nasza pozycja (z potwierdzenia wejścia albo z uzgadniania: deal z dziennika u brokera)
        self._memory.last_own_deal = position.deal_id
        failures: list[str] = []
        attached = False
        level: Decimal | None = None
        where = f"protection of {position.deal_id}"
        try:
            if distance is None:
                distance = await self._protection_distance(position, failures)
            if distance is not None:
                level = position.open_level - distance if position.side is Side.BUY else position.open_level + distance
                attached = await self._attach_stop(position, level, failures)
        except BaseException as exc:  # także anulowanie: pozycja bez stopu nigdy nie zostaje bez HARD
            if not attached:
                self._kill.hard(UNPROTECTED_POSITION)
                self._alert(
                    "ERROR",
                    f"{UNPROTECTED_POSITION}: {position.deal_id} has no stop at the broker; protection interrupted "
                    f"({type(exc).__name__}); kill switch HARD, emergency flatten not run here (the engine runs it)",
                )
            self._report_journal_failures(failures, where)
            raise
        if attached:
            self._report_journal_failures(failures, where)
            return True
        self._kill.hard(UNPROTECTED_POSITION)
        self._alert(
            "ERROR",
            f"{UNPROTECTED_POSITION}: {position.deal_id} has no stop at the broker (stop {level}); kill switch HARD, "
            "emergency flatten",
        )
        try:
            await self.emergency_flatten()  # przed zdarzeniem: ochrona nie czeka na zapis dziennika
        finally:
            self._event(
                "PROTECTION_FAILED", best_effort=failures, deal_id=position.deal_id, stop_level=level, distance=distance
            )
            self._report_journal_failures(failures, where)
        return False

    async def _attach_stop(self, position: OpenPosition, level: Decimal, failures: list[str]) -> bool:
        """Do `PROTECT_ATTEMPTS` prób `set_stop` (ponowienie tego samego poziomu jest bezpieczne); zapisy wg możliwości,
        więc awaria dziennika nie skraca procedury."""
        timeout = min(self._timeout, PROTECT_CALL_TIMEOUT_S)
        for attempt in range(1, PROTECT_ATTEMPTS + 1):
            if attempt > 1:
                await self._clock.sleep(PROTECT_PAUSE_S)
            try:
                await _with_timeout(self._broker.set_stop(position.deal_id, level), timeout)
            except Exception as exc:
                self._raw_error(
                    SOURCE_SET_STOP, exc, best_effort=failures, deal_id=position.deal_id, stop_level=level,
                    attempt=attempt,
                )  # fmt: skip
                continue
            self._raw(
                SOURCE_SET_STOP, best_effort=failures, deal_id=position.deal_id, stop_level=level, attempt=attempt,
                response=None,
            )  # fmt: skip
            self._alert("INFO", f"STOP_ATTACHED {position.deal_id}: stop {level} set after the broker had none")
            self._event(
                "STOP_ATTACHED", best_effort=failures, deal_id=position.deal_id, stop_level=level, attempt=attempt
            )
            return True
        return False

    async def _protection_distance(self, position: OpenPosition, failures: list[str]) -> Decimal | None:
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
            self._raw_error(SOURCE_RULES, exc, best_effort=failures, deal_id=position.deal_id, context="protection")
            return None
        self._raw(SOURCE_RULES, best_effort=failures, deal_id=position.deal_id, context="protection", response=rules)
        return _max_loss_distance(self._risk.max_loss_per_trade, position.size, position.open_level, rules)

    # --- księgowanie --------------------------------------------------------------------------------------

    async def book(self, position: PositionRecord, closed: ClosedDeal) -> None:
        """Księguje zamknięcie naszej pozycji (`TradeBooker`); idempotentny po `closed.deal_id`.

        Już zaksięgowane: tylko ponowne nałożenie limitów strat, bez żadnego odczytu brokera. Nie zmienia stanu pozycji
        ani zleceń (to robi wywołujący po powrocie). Błąd dziennika propaguje się; `BookingError`, gdy nie da się
        zbudować nawet rekordu zdegradowanego.
        """
        await self._book(position, closed, self._timeout)

    async def _book(self, position: PositionRecord, closed: ClosedDeal, timeout: float) -> None:
        exit_day = self._calendar.to_local(closed.ts).date()
        if self._journal.has_trade(closed.deal_id):
            self._enforce_loss_limits(exit_day, announce=False)  # domyka okno przerwania między księgą a kill switchem
            return
        exit_reason = self._exit_reason(closed)
        fallback: str | None = None
        try:
            trade, notes = await self._full_trade(position, closed, exit_reason, timeout)
        except (JournalReadError, JournalWriteError):
            raise  # dziennik leży: księgowanie powtórzy się z pełnym kontekstem
        except Exception as exc:  # brak kontekstu, ujemny spread, złe typy: rekord zdegradowany, ale zaksięgowany
            fallback = NO_ENTRY_CONTEXT if isinstance(exc, _NoContext) else f"{type(exc).__name__}: {exc}"
            try:
                trade, notes = await self._fallback_trade(position, closed, exit_reason, timeout)
            except (JournalReadError, JournalWriteError):
                raise
            except Exception as exc2:
                self._alert("ERROR", f"BOOKING_FAILED {closed.deal_id}: {type(exc2).__name__}: {exc2} ({fallback})")
                raise BookingError(f"cannot book {closed.deal_id}: {type(exc2).__name__}: {exc2}") from exc2
        booked = self._journal.record_trade(trade, closed.deal_id, exit_day)
        triggered = self._enforce_loss_limits(exit_day, announce=booked)  # kill przed zapisami zdarzeń
        if not booked:
            return  # zaksięgował ktoś w międzyczasie: w księdze nic się nie zmienia
        if fallback is not None:
            self._alert("WARN", f"{COST_FALLBACK} {closed.deal_id}: degraded ledger row ({fallback}); check the broker")
            self._event(COST_FALLBACK, deal_id=closed.deal_id, reason=fallback)
        self._event(
            "TRADE_BOOKED", deal_id=closed.deal_id, exit_day=exit_day.isoformat(), exit_reason=trade.exit_reason,
            net_pnl=trade.net_pnl, notes=list(notes), cost_fallback=fallback is not None,
        )  # fmt: skip
        for code, detail in triggered:
            self._event(code, **detail)

    def _exit_reason(self, closed: ClosedDeal) -> str:
        """STOP: stop brokera; FORCED: wyjście silnika (zapisany zamiar) albo wymuszenie brokera; MANUAL: zamknięcie
        wywołaniem bez zamiaru silnika (właściciel)."""
        if closed.reason == "STOP":
            return "STOP"
        if self._journal.get_meta(META_EXIT_INTENT.format(closed.deal_id)) is not None:
            return "FORCED"
        return closed.reason

    async def _full_trade(
        self, position: PositionRecord, closed: ClosedDeal, exit_reason: str, timeout: float
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
            spread_exit=spread_exit, exit_reason=exit_reason,
            nights_held=self._nights(position.opened_at, closed),
        )  # fmt: skip
        costs = self._cost_model.compute(facts)
        confirm_ts = self._confirm_ts(ctx, closed.deal_id, position, submit_ts)
        trade = _record(position, bid, ask, closed, costs, signal_ts, submit_ts, confirm_ts, exit_reason)
        return trade, costs.notes

    async def _fallback_trade(
        self, position: PositionRecord, closed: ClosedDeal, exit_reason: str, timeout: float
    ) -> tuple[TradeRecord, tuple[str, ...]]:
        """Rekord zdegradowany: mid wejścia = poziom otwarcia, spready 0, notowanie decyzji = poziom otwarcia."""
        vpp = await self._fallback_vpp(position.epic, closed.deal_id, timeout)
        level = position.open_level
        opened = position.opened_at
        when = opened if opened is not None else closed.ts
        facts = TradeFacts(
            side=position.side, size=position.size, value_per_point=vpp, mid_entry=level, mid_exit=closed.exit_level,
            entry_fill=level, exit_fill=closed.exit_level, spread_entry=_ZERO, spread_exit=_ZERO,
            exit_reason=exit_reason, nights_held=0 if opened is None else self._nights(opened, closed),
        )  # fmt: skip
        costs = self._cost_model.compute(facts)
        return _record(position, level, level, closed, costs, when, when, when, exit_reason), costs.notes

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

    # --- limity strat (spec §9) -----------------------------------------------------------------------------

    def enforce_loss_limits(self, day: date) -> None:
        """Nakłada kill switch z liczników dziennika: wynik dnia `day` ≤ −`max_daily_loss` → miękki DAILY_LOSS,
        ≤ −2 × `max_daily_loss` → twardy DAILY_LOSS_2X, wynik łączny ≤ −`max_total_drawdown` → twardy TOTAL_DRAWDOWN.

        Idempotentne (kill switch nie powtarza zapisu ani alarmu tej samej przyczyny), nigdy nie rzuca. Silnik woła je
        przy starcie: poziom utracony przez przerwanie między księgowaniem a zapisem kill switcha wraca z liczników.
        Nieczytelne liczniki → miękki KILL_CHECK_FAILED.
        """
        self._enforce_loss_limits(day, announce=False)

    def _enforce_loss_limits(self, exit_day: date, *, announce: bool) -> list[tuple[str, dict[str, Any]]]:
        """Kontrole limitów; kill switch nigdy nie rzuca. `announce`: alarm wykonawcy ze szczegółami (nowe księgowanie).
        Zwraca wyzwolone (kod, szczegóły) dla zdarzeń."""
        try:
            day_pnl = self._journal.realized_pnl(exit_day)
            total = self._journal.realized_pnl_total()
        except Exception as exc:  # nie da się sprawdzić limitów: blokujemy wejścia
            self._kill.soft(KILL_CHECK_FAILED)
            self._alert("ERROR", f"{KILL_CHECK_FAILED}: realized pnl unreadable ({type(exc).__name__})")
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

    def _entry_client_id(self, deal_id: str | None, epic: str) -> str | None:
        """Identyfikator wejścia pozycji `deal_id`: z `entry_by_deal`, a bez niego wskaźnik ostatniego wejścia, jeśli
        jego zlecenie ma w dzienniku ten sam deal_id (pozycja przyjęta przez uzgadnianie). None: brak."""
        if deal_id is None:
            return None
        client_id = self._journal.get_meta(META_ENTRY_BY_DEAL.format(deal_id))
        if client_id:
            return client_id
        last = self._journal.get_meta(META_ENTRY_LAST.format(epic))
        order = self._journal.get_order(last) if last else None
        if order is None or order.deal_id != deal_id or order.epic != epic:
            return None
        return last

    def _context(self, deal_id: str | None, epic: str) -> dict[str, Any] | None:
        """Kontekst wejścia pozycji `deal_id` (przez `_entry_client_id`). None: brak; zły kontekst: ValueError."""
        client_id = self._entry_client_id(deal_id, epic)
        if client_id is None:
            return None
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

    @staticmethod
    def _write(best_effort: Failures, write: Callable[[], object]) -> None:
        """Zapis obowiązkowy (`best_effort=None`: błąd propaguje się) albo wg możliwości (błąd dziennika dopisany do
        listy, przebieg idzie dalej)."""
        if best_effort is None:
            write()
            return
        try:
            write()
        except JournalError as exc:
            best_effort.append(f"{type(exc).__name__}: {exc}")

    def _append(self, source: str, payload: dict[str, Any], *, best_effort: Failures = None) -> None:
        self._write(best_effort, lambda: self._journal.append_raw(source, dump(payload), self._now()))

    def _raw(self, source: str, *, best_effort: Failures = None, **payload: Any) -> None:
        self._append(source, payload, best_effort=best_effort)

    def _raw_error(self, source: str, exc: BaseException, *, best_effort: Failures = None, **payload: Any) -> None:
        error: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, BrokerRejected):
            error["rejection"] = exc.reason
        self._append(source, {**payload, **error}, best_effort=best_effort)

    def _event(self, code: str, *, best_effort: Failures = None, **details: Any) -> None:
        self._append(SOURCE_EVENT, {"event": code, **details}, best_effort=best_effort)

    def _set_meta(self, key: str, value: str, *, best_effort: Failures = None) -> None:
        self._write(best_effort, lambda: self._journal.set_meta(key, value))

    def _report_journal_failures(self, failures: list[str], where: str, *, closes_before: int | None = None) -> None:
        """Po działaniu ochronnym: zapisy wg możliwości, które zawiodły → kill switch miękki i alarm.

        `closes_before`: stan licznika wysyłek `close_position` na początku procedury, która miała zamknąć pozycję;
        alarm mówi wtedy, czy zamknięcie poszło do brokera (zapis obowiązkowy mógł je przerwać przed wysyłką)."""
        if not failures:
            return
        self._kill.soft(JOURNAL_WRITE_FAILED)
        more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
        if closes_before is None:
            outcome = "broker actions were carried out regardless"
        elif self._closes_started > closes_before:
            outcome = "the close was sent to the broker regardless"
        else:
            outcome = "the close was NOT sent (the next management pass retries it)"
        self._alert("ERROR", f"{JOURNAL_WRITE_FAILED} during {where}: {failures[0]}{more}; {outcome}")

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


def _from_broker(state: PositionState, p: OpenPosition) -> PositionRecord:
    return PositionRecord(p.epic, state, p.deal_id, p.side, p.size, p.open_level, p.stop_level, p.opened_at)


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
