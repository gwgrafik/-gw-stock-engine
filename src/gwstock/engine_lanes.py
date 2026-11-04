"""Kolejne pasma silnika (zadanie 12, projekt wielu pasm §1.3, §2.1): pasmo świecowe B1/M5 obok pasma DE40 v1.

`BarLane` to stan i kroki jednego pasma `bar_b1_m5` (`config/lanes.toml`) wykonywane przez `app.Engine` w tej samej
korutynie co pasmo DE40 (żadnych zadań w tle, O13). Pasmo ma WŁASNE: instrument, kalendarz (np. NYSE), bramkę
(`risk.BarLaneRiskGate`), wykonawcę (`Executor.for_lane`, cel zysku `TargetedEntryRequest`), uzgadnianie (`Reconciler`
z kluczami per instrument, `legacy_keys=False`), stan dnia, licznik sesji i wejść dnia. Wspólne z portfelem: dziennik,
sesja brokera i limit zapytań (`GuardedBroker` silnika), kill switch, pamięć wyjść (`ExitMemory`), bramka portfelowa.

Decyzja (`lanes.decide_entry`) zapada po zamknięciu każdej świecy M5, której początek leży w oknie sygnałów pasma, w
pierwszym takcie po jej końcu (najpóźniej do końca następnej świecy: później to `STALE_SIGNAL`). Każda decyzja trafia
do dziennika (źródło `decision`, zdarzenie `LANE_DECISION`), także odrzucenie.

Stan dnia (`lanes.DayState`) wg rozstrzygnięć kontrolera zadania 12:
- dzień = data lokalna kalendarza pasma (NY), `DayState.day == LaneSession.day`;
- `entries` = KAŻDE zlecenie wejścia pasma utworzone tego dnia, w dowolnym stanie (`journal.count_entry_orders(day,
  epic)`, jak S3 w `decisions-log.md`). RÓŻNICA względem badań (`research/candle_strategies.run`): badania liczą tylko
  zrealizowane transakcje, więc odrzucone albo niewypełnione wejście zużywa tu limit `max_entries_per_day`
  (konserwatywnie: mniej wejść, nigdy więcej);
- `result_r` = suma wyników ZAKSIĘGOWANYCH transakcji pasma z wyjściem tego dnia, każda w R = wynik w punktach
  (`exit_fill - entry_fill`, ze znakiem kierunku) / odległość stopu z kontekstu wejścia; transakcja bez czytelnej
  odległości stopu liczy się jako `-day_stop_r` (dzień zatrzymany: nie zgadujemy R);
- `position_open`: zapis pozycji pasma inny niż FLAT albo nierozstrzygnięte zlecenie pasma;
- `last_exit_at`: najpóźniejsze wyjście zaksięgowanej transakcji pasma tego dnia.

Wielkość: `session_index` pasma = transakcje instrumentu bez prób smoke; wielkość wg reguł (`rule_size_allowed`)
nigdy dla kolejnego pasma, dopóki nie ma bramki L1 per pasmo (pytanie 7 właściciela): zawsze wielkość minimalna.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from gwstock.calendar import ExchangeCalendar
from gwstock.config import LoadedLane
from gwstock.costs import CostModel
from gwstock.execution import Executor
from gwstock.journal import JournalError
from gwstock.lanes import DayState, LaneSession, exit_due, lane_session
from gwstock.reconcile import Reconciler
from gwstock.risk import BarLaneRiskGate
from gwstock.types import Candle, PositionState, Side

_P = PositionState
LANE_DECISION = "LANE_DECISION"
EXIT_FORCED = "FORCED"


@dataclass
class BarDay:
    """Stan dnia pasma (data lokalna kalendarza pasma)."""

    day: date
    session: LaneSession | None
    open: datetime | None = None
    close: datetime | None = None
    preopen: datetime | None = None
    reconciled: bool = False
    decided: set[datetime] = field(default_factory=set)  # początki świec sygnału już rozstrzygniętych
    waiting: dict[datetime, str] = field(default_factory=dict)  # świeca → powód czekania (ostatni)
    decisions: int = 0


class BarLane:
    """Pasmo `bar_b1_m5` w silniku (patrz docstring modułu). Części buduje `Engine`."""

    def __init__(
        self,
        loaded: LoadedLane,
        calendar: ExchangeCalendar,
        gate: BarLaneRiskGate,
        cost_model: CostModel,
        arming_problem: str | None,
    ) -> None:
        self.loaded = loaded
        self.bar = loaded.bar
        self.epic = loaded.epic
        self.calendar = calendar
        self.gate = gate
        self.cost_model = cost_model
        self.arming_problem = arming_problem  # None: pasmo wolno uzbroić (proces live); inaczej najwyżej dry_run
        self.executor: Executor | None = None
        self.reconciler: Reconciler | None = None
        self.day: BarDay | None = None
        self.next_quote: datetime | None = None
        self.next_manage: datetime | None = None
        self.next_fx: datetime | None = None
        self.last_quote: Any = None
        self.downgraded = False

    @property
    def span(self) -> timedelta:
        return timedelta(minutes=self.bar.bar_minutes)

    @property
    def foreign(self) -> bool:
        return self.executor is not None and self.executor.foreign

    def begin_day(self, day: date, preopen_lead_min: int) -> BarDay:
        """Okna, otwarcie i zamknięcie pasma dnia `day` (ta sama data lokalna co dzień portfela; sesja NYSE mieści się
        w dacie berlińskiej). Wygasły kalendarz: wyjątek (wywołujący: brak handlu pasma i alarm)."""
        if self.day is not None and self.day.day == day:
            return self.day
        session = lane_session(self.bar, self.calendar, day)
        d = BarDay(day, session)
        if session is not None:
            cal = self.calendar
            d.open = cal.local_to_utc(day, cal.open_time(day))
            d.close = cal.local_to_utc(day, cal.close_time(day))
            d.preopen = d.open - timedelta(minutes=preopen_lead_min)
        self.day = d
        return d

    def active(self, now: datetime) -> bool:
        """W oknie pracy pasma (przygotowanie do zamknięcia kalendarza pasma)."""
        d = self.day
        return d is not None and d.preopen is not None and d.close is not None and d.preopen <= now <= d.close

    def due_candle(self, now: datetime) -> datetime | None:
        """Początek świecy sygnału, której decyzja przypada teraz (zakończona, w oknie, jeszcze nierozstrzygnięta)."""
        d = self.day
        if d is None or d.session is None:
            return None
        span = self.span
        grid = int(span.total_seconds())
        epoch = int(now.timestamp())
        end = datetime.fromtimestamp(epoch - epoch % grid, tz=now.tzinfo)  # koniec ostatniej zakończonej świecy
        start = end - span
        if start in d.decided or not d.session.in_signal_window(start) or now >= end + span:
            return None
        return start

    def next_candle_end(self, now: datetime) -> datetime | None:
        """Najbliższy koniec świecy z początkiem w oknie sygnałów (budzenie pętli na decyzję)."""
        d = self.day
        if d is None or d.session is None:
            return None
        span = self.span
        grid = int(span.total_seconds())
        epoch = int(now.timestamp())
        end = datetime.fromtimestamp(epoch - epoch % grid, tz=now.tzinfo) + span
        for opens, closes in d.session.windows:
            if end - span < closes and end - span >= opens:
                return end
            if end - span < opens:
                return opens + span
        return None

    def candle_window(self, start: datetime) -> tuple[datetime, datetime]:
        """Zakres zapytania o świece: świeca sygnału i tyle poprzednich, ile potrzeba decyzji, z zapasem na luki."""
        needed = max(self.bar.lookback_bars + 2, self.bar.atr_period + 1) + self.bar.gap_tolerance_bars + 2
        return start - self.span * needed, start + self.span

    def day_state(self, journal: Any, executor: Executor, now: datetime) -> DayState:
        """`lanes.DayState` pasma z dziennika (rozstrzygnięcia w docstringu modułu). Błąd odczytu: wyjątek."""
        d = self.day
        assert d is not None
        entries = journal.count_entry_orders(d.day, self.epic)
        position = journal.current_position(self.epic)
        position_open = (position is not None and position.state is not _P.FLAT) or any(
            o.epic == self.epic for o in journal.unresolved_orders()
        )
        result_r = Decimal(0)
        last_exit: datetime | None = None
        for deal_id, trade in journal.trades_by_deal(self.epic):
            if self.calendar.to_local(trade.exit_ts).date() != d.day:
                continue
            last_exit = trade.exit_ts if last_exit is None else max(last_exit, trade.exit_ts)
            result_r += self._trade_r(executor, deal_id, trade)
        return DayState(d.day, entries, result_r, position_open, last_exit)

    def _trade_r(self, executor: Executor, deal_id: str, trade: Any) -> Decimal:
        try:
            ctx = executor._context(deal_id, self.epic)  # kontekst wejścia (ta sama droga co księgowanie)
            stop = Decimal(ctx["stop_distance"]) if ctx is not None else None
        except (JournalError, Exception):
            stop = None
        if stop is None or not stop.is_finite() or stop <= 0:
            return -self.bar.day_stop_r  # R nieznane: dzień zatrzymany, nie zgadujemy
        points = trade.exit_fill - trade.entry_fill
        if trade.side is Side.SELL:
            points = -points
        return points / stop

    def session_index(self, journal: Any) -> int:
        """Zakończone sesje pasma dla polityki wielkości: transakcje instrumentu bez prób smoke."""
        return len(journal.trades(self.epic)) - journal.count_smoke_trades(self.epic)

    def exit_reason(self, position: Any, now: datetime) -> str | None:
        """Wyjście zaplanowane pozycji OPEN pasma: limit czasu albo godzina spłaszczenia (`lanes.exit_due`); pozycja bez
        znanego otwarcia, z wcześniejszego dnia albo w dniu bez sesji pasma: spłaszczenie (pasmo nie trzyma nocy)."""
        d = self.day
        opened = position.opened_at
        if opened is None or d is None or d.session is None:
            return EXIT_FORCED
        if self.calendar.to_local(opened).date() < d.day or now < opened:
            return EXIT_FORCED
        return exit_due(opened, now, d.session, self.bar)

    def closed_candles(self, candles: list[Candle], start: datetime, now: datetime) -> list[Candle]:
        """Zakończone świece instrumentu pasma do świecy sygnału włącznie, rosnąco i bez powtórzeń."""
        span = self.span
        seen: dict[datetime, Candle] = {}
        for c in candles:
            if c.epic == self.epic and c.start <= start and c.start + span <= now:
                seen[c.start] = c
        return [seen[k] for k in sorted(seen)]


def lane_epics(primary: str, lanes: list[BarLane]) -> list[str]:
    return [primary, *(lane.epic for lane in lanes)]


def default_lane_arming(_loaded: LoadedLane) -> str | None:
    """Domyślny warunek uzbrojenia kolejnego pasma: pre-rejestracja pasm nie jest jeszcze zamrażana (zadanie 13), więc
    kolejne pasmo pracuje najwyżej jako dry_run (R7, projekt §6.2)."""
    return "LANE_PREREGISTRATION_NOT_FROZEN"


LaneArming = Callable[[LoadedLane], "str | None"]
