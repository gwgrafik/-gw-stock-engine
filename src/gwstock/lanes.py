"""Pasma (lanes) silnika: jeden instrument + jedna reguła + własna konfiguracja (projekt wielu pasm, R1).

Dwie części:

1. `LaneSpec`: to, czego wykonawca (`gwstock.execution.Executor`) potrzebuje o paśmie (zadanie 9 projektu).
2. Pasmo świecowe B1/M5 (`bar_b1_m5`, zadanie 11): decyzja wejścia z oknami sesji i filtrami dnia (`decide_entry`),
   okna i godzina spłaszczenia danego dnia (`lane_session`) oraz wyjście zaplanowane (`exit_due`). Czyste funkcje bez
   wejścia/wyjścia, zegara i brokera: stan dnia, notowanie i chwilę podaje wywołujący (silnik, zadanie 12).

Zakres pasma B1/M5 = CAŁA funkcja `run` z `research/candle_strategies.py` (zamrożona hipoteza testu forward
`docs/preregistration/forward-us500-b1-m5-sesje.md`), nie tylko sygnał (`strategy_bars.bar_signal`). Wszystkie progi
pochodzą z `BarRuleConfig` (`config/lane_us500_b1_m5.toml`); w kodzie nie ma żadnej z tych liczb. Zmiana progu to NOWY
wariant z osobną pre-rejestracją.

Kolejność kontroli w `decide_entry` (pierwsza prawdziwa daje odrzucenie; kolejność odpowiada pętli `run`):

 1. `INSUFFICIENT_DATA` dla pustej listy świec; błędy wywołującego (typy, różne instrumenty, niemalejące znaczniki,
    świeca sygnału jeszcze niezakończona w chwili `now`) to wyjątek, nie odrzucenie;
 2. `STALE_SIGNAL`: decyzja później niż o jedną długość świecy po końcu świecy sygnału (wejście ma być na otwarciu
    NASTĘPNEJ świecy; nowa kontrola pasma, w badaniach jej nie ma);
 3. `POSITION_OVERLAP`: trwa pozycja pasma albo świeca sygnału zaczęła się nie później niż ostatnie wyjście
    (badania: po wyjściu pętla wraca dopiero od świecy po tej, w której nastąpiło wyjście);
 4. `INSUFFICIENT_DATA`: mniej niż `max(lookback_bars + 2, atr_period + 1)` świec (zakres, świeca sygnału, świeca
    kontroli ciągłości; ATR potrzebuje `atr_period + 1` zamknięć; to samo co `i < n + 2 or atr is None` w badaniach);
 5. `OUTSIDE_WINDOW`: początek świecy sygnału poza oknami `signal_windows` dnia (badania: `in_window` na początku świecy);
 6. `MAX_ENTRIES_DAY` (`entries >= max_entries_per_day`), potem `DAY_STOP` (`result_r <= -day_stop_r`);
 7. `GAPPED_DATA`: rozpiętość od świecy `lookback_bars + 2` od końca do świecy sygnału większa niż
    `(lookback_bars + 1 + gap_tolerance_bars) x bar_minutes` (sprawdzana dla KAŻDEJ świecy w oknie, także bez sygnału,
    jak w badaniach);
 8. `NO_SIGNAL`: brak sygnału reguły B1 (`strategy_bars.bar_signal`, spread z notowania);
 9. `STOP_TOO_CLOSE`: odległość stopu od ceny wejścia (z założonym poślizgiem) mniejsza niż `min_stop_fraction` ceny;
10. `SLIP_FILTER`: |cena wejścia (z poślizgiem) - zamknięcie świecy sygnału| > `slippage_filter_atr_fraction` x ATR;
11. `COST_OVER_LIMIT`: (spread + 2 x założony poślizg) > `max_round_trip_cost_r` x odległość stopu;
12. plan wielkości (`strategy_bars.entry_plan`): `ENTER` z `BarEntryPlan` albo `STOP_TOO_TIGHT` / `SKIPPED_MIN_SIZE`.

Wszystkie porównania są w dokładnej arytmetyce `Decimal` bez dzielenia (ATR jako suma zakresów przemnożona przez ułamek,
odchylenie razy okres), więc granice rozstrzygają się dokładnie: równo przechodzi, o najmniejszy krok więcej odrzuca.

RÓŻNICE WZGLĘDEM `research/candle_strategies.run` (nie do uniknięcia albo świadome; test krzyżowy
`tests/unit/test_lanes.py` porównuje decyzje przy wyrównanych danych i opisuje, co jest wyrównane):

 a. Spread: badania biorą spread z zamknięcia świecy sygnału (`bars[i].ac - bars[i].bc`) do bufora wybicia i kosztu;
    świece silnika mają tylko ceny mid (`Candle`), więc pasmo bierze spread z notowania w chwili decyzji. Zgadza się, gdy
    spread jest ciągły między zamknięciem świecy a otwarciem następnej (tak jest w danych rzeczywistych).
 b. Cena wejścia: badania wypełniają na otwarciu NASTĘPNEJ ISTNIEJĄCEJ świecy (ask/bid otwarcia + poślizg); pasmo bierze
    notowanie z chwili decyzji tuż po zamknięciu świecy sygnału i dolicza ten sam założony poślizg
    (`assumed_slippage_points`). Przy dziurze w danych tuż po świecy sygnału badania wchodzą później, a pasmo odrzuca
    spóźnioną decyzję (`STALE_SIGNAL`).
 c. R: filtry (`STOP_TOO_CLOSE`, `SLIP_FILTER`, `COST_OVER_LIMIT`) liczą cenę wejścia Z założonym poślizgiem, jak
    badania. Plan (`entry_plan`, zadanie 2) liczy odległość stopu i celu od notowania BEZ poślizgu, bo rzeczywista cena
    wypełnienia jest nieznana przy składaniu zlecenia, a stop i cel to poziomy u brokera; różnica to najwyżej
    założony poślizg w odległości stopu i celu.
 d. Wyjścia po wejściu (stop, cel, limit czasu, luka za stopem) wykonuje broker i silnik, a nie symulacja; limit czasu
    liczy się od `opened_at` pozycji (badania: od początku świecy wejścia; różnica sekund, zob. projekt §3.6).
    `result_r` dnia liczy silnik z rzeczywistych wypełnień (badania: z założonych kosztów).
 e. Zaplanowane wyjście: zamknięcie kalendarza pasma minus `flatten_before_close_min` (NYSE 15:55); badania mają stałe
    15:55. Różnica tylko w skróconych sesjach (np. 13:00 -> 12:55), gdzie badania w ogóle nie wyszłyby przed końcem
    danych. Okna sygnałów są przycinane do godziny spłaszczenia.
 f. Dzień: stan dnia (`DayState`) podaje wywołujący dla dnia lokalnego kalendarza pasma (badania: data lokalna strefy
    sesji początku świecy), a `entries` liczy to, co silnik uzna za wejście (badania: zrealizowane transakcje).
 g. Arytmetyka: badania liczą na `float` (średnia ATR zaokrąglona), pasmo na `Decimal`; granice rozstrzygają się
    dokładnie, więc na wartościach niedwójkowych (np. poślizg 0.15) wynik może różnić się od `float` tylko w przypadku
    remisu co do ostatniego bitu.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow

from gwstock.calendar import ExchangeCalendar
from gwstock.config import BarRuleConfig, RiskConfig, StrategyConfig, parse_signal_windows
from gwstock.strategy import SKIPPED_MIN_SIZE, STOP_TOO_TIGHT, Skip
from gwstock.strategy_bars import BarEntryPlan, BarSignal, bar_signal, entry_plan
from gwstock.types import Candle, InstrumentRules, Quote, Side

__all__ = [
    "LaneSpec", "DayState", "LaneSession", "LaneDecision", "lane_session", "decide_entry", "exit_due",
    "ENTER", "NO_SIGNAL", "INSUFFICIENT_DATA", "OUTSIDE_WINDOW", "STALE_SIGNAL", "POSITION_OVERLAP",
    "MAX_ENTRIES_DAY", "DAY_STOP", "GAPPED_DATA", "STOP_TOO_CLOSE", "SLIP_FILTER", "COST_OVER_LIMIT",
    "STOP_TOO_TIGHT", "SKIPPED_MIN_SIZE", "DECISION_REASONS", "EXIT_TIME_LIMIT", "EXIT_FLATTEN",
]  # fmt: skip

_EPIC = re.compile(r"[A-Za-z0-9]+")  # jak człon instrumentu identyfikatora zlecenia (`orders.ids.make_client_id`)
_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class LaneSpec:
    """Opis pasma dla wykonawcy (projekt §2.2). Wszystkie wartości pochodzą z konfiguracji pasma.

    - `epic`: instrument pasma (najwyżej jedna otwarta pozycja na instrument, R1);
    - `calendar`: kalendarz dnia księgowania (data lokalna to klucz `realized_pnl` i podstawa liczenia nocy
      trzymania); wspólny dla pasm portfela (dzień portfela, projekt §1.3, pytanie 4 właściciela);
    - `slippage_guard_fraction`: straż poślizgu wejścia (ułamek odległości stopu);
    - `max_loss_per_trade`: limit straty jednej transakcji w WALUCIE RACHUNKU (straż wielkości, odległość stopu z limitu);
    - `currency`: waluta instrumentu (ISO 4217) albo None, gdy instrument jest w walucie rachunku (DE40 v1); inna niż
      waluta rachunku wymaga przeliczeń przez `FxView` i dwufazowego księgowania (projekt §7);
    - `uses_target`: pasmo wchodzi z celem zysku dołączonym atomowo (`TargetedEntryRequest`, R4) i zabezpiecza
      pozycję stopem RAZEM z celem (`Broker.set_protection`); pasmo bez celu (DE40 v1) księguje zamknięcie celem
      jak STOP (projekt §3.5: zachowanie v1 bajt w bajt).
    """

    epic: str
    calendar: ExchangeCalendar
    slippage_guard_fraction: Decimal
    max_loss_per_trade: Decimal
    currency: str | None = None
    uses_target: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.epic, str) or not _EPIC.fullmatch(self.epic):
            raise ValueError(f"LaneSpec.epic: expected letters and digits only, got {self.epic!r}")
        if not isinstance(self.calendar, ExchangeCalendar):
            raise TypeError(f"LaneSpec.calendar: expected ExchangeCalendar, got {self.calendar!r}")
        for name in ("slippage_guard_fraction", "max_loss_per_trade"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"LaneSpec.{name}: expected a finite Decimal > 0, got {value!r}")
        if self.currency is not None and (not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency)):
            raise ValueError(f"LaneSpec.currency: expected a 3-letter ISO 4217 code or None, got {self.currency!r}")
        if not isinstance(self.uses_target, bool):
            raise TypeError(f"LaneSpec.uses_target: expected bool, got {self.uses_target!r}")

    @classmethod
    def from_v1(cls, strat: StrategyConfig, risk: RiskConfig, calendar: ExchangeCalendar) -> LaneSpec:
        """Pasmo DE40 `orb_index_cfd` v1 z `strategy_v1.toml` i `risk_v1.toml` (bez celu, w walucie rachunku)."""
        return cls(
            epic=strat.instrument,
            calendar=calendar,
            slippage_guard_fraction=strat.slippage_guard_fraction,
            max_loss_per_trade=risk.max_loss_per_trade,
        )


# ---------------------------------------------------------------------------------------------------------------
# Pasmo świecowe B1/M5: stan dnia, okna sesji, decyzja wejścia, wyjście zaplanowane
# ---------------------------------------------------------------------------------------------------------------

ENTER = "ENTER"
NO_SIGNAL = "NO_SIGNAL"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
STALE_SIGNAL = "STALE_SIGNAL"
POSITION_OVERLAP = "POSITION_OVERLAP"
MAX_ENTRIES_DAY = "MAX_ENTRIES_DAY"
DAY_STOP = "DAY_STOP"
GAPPED_DATA = "GAPPED_DATA"
STOP_TOO_CLOSE = "STOP_TOO_CLOSE"
SLIP_FILTER = "SLIP_FILTER"
COST_OVER_LIMIT = "COST_OVER_LIMIT"
# `STOP_TOO_TIGHT` i `SKIPPED_MIN_SIZE` pochodzą z `strategy_bars.entry_plan` (reeksportowane wyżej).
DECISION_REASONS = frozenset(
    {
        ENTER,
        NO_SIGNAL,
        INSUFFICIENT_DATA,
        OUTSIDE_WINDOW,
        STALE_SIGNAL,
        POSITION_OVERLAP,
        MAX_ENTRIES_DAY,
        DAY_STOP,
        GAPPED_DATA,
        STOP_TOO_CLOSE,
        SLIP_FILTER,
        COST_OVER_LIMIT,
        STOP_TOO_TIGHT,
        SKIPPED_MIN_SIZE,
    }
)

# Przyczyny wyjścia zaplanowanego przez silnik (wartości trafiają do `Executor.flatten(position, reason)`):
EXIT_TIME_LIMIT = "TIME_LIMIT"  # limit czasu pozycji (projekt §3.6)
EXIT_FLATTEN = "FORCED"  # zaplanowane spłaszczenie przed zamknięciem; ta sama przyczyna co `app.FLATTEN_REASON`

# Obliczenia pasma: 60 cyfr znaczących (jak `strategy_bars`), bez zależności od kontekstu otoczenia.
_CALC = Context(
    prec=60,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


def _require_utc(owner: str, name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{owner}.{name}: expected a timezone-aware UTC datetime, got {value!r}")


def _require_plain_date(owner: str, name: str, value: object) -> None:
    # datetime jest podklasą date, ale nie jest datą dnia
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{owner}.{name}: expected a date, got {value!r}")


@dataclass(frozen=True)
class DayState:
    """Stan dnia pasma, który podaje wywołujący (silnik); sama decyzja niczego nie zapamiętuje.

    - `day`: dzień lokalny kalendarza pasma (musi zgadzać się z `LaneSession.day`);
    - `entries`: liczba wejść tego dnia (limit `max_entries_per_day`);
    - `result_r`: suma wyników ZAMKNIĘTYCH transakcji dnia w R (strata ujemna; limit `-day_stop_r`); R transakcji to
      zrealizowany wynik w punktach podzielony przez odległość stopu w punktach z wejścia (jak `pnl / r_pts` w badaniach);
    - `position_open`: trwa pozycja pasma (najwyżej jedna naraz, R1);
    - `last_exit_at`: chwila ostatniego wyjścia z pozycji pasma tego dnia (UTC) albo None. Świeca sygnału, która
      zaczęła się nie później niż ta chwila, nakładałaby się na poprzednią transakcję (jak w badaniach).
    """

    day: date
    entries: int
    result_r: Decimal
    position_open: bool
    last_exit_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_plain_date("DayState", "day", self.day)
        if isinstance(self.entries, bool) or not isinstance(self.entries, int) or self.entries < 0:
            raise ValueError(f"DayState.entries: expected a non-negative integer, got {self.entries!r}")
        if not isinstance(self.result_r, Decimal) or not self.result_r.is_finite():
            raise ValueError(f"DayState.result_r: expected a finite Decimal, got {self.result_r!r}")
        if not isinstance(self.position_open, bool):
            raise TypeError(f"DayState.position_open: expected bool, got {self.position_open!r}")
        if self.last_exit_at is not None:
            _require_utc("DayState", "last_exit_at", self.last_exit_at)


@dataclass(frozen=True)
class LaneSession:
    """Okna sygnałów i godzina spłaszczenia jednego dnia pasma jako chwile UTC (budowane przez `lane_session`).

    `windows`: rozłączne, rosnące przedziały [początek, koniec) UTC, w których musi zaczynać się świeca sygnału; wszystkie
    kończą się nie później niż `flatten_at`. Pusta krotka = dzień bez okien (np. skrócona sesja).
    """

    day: date
    windows: tuple[tuple[datetime, datetime], ...]
    flatten_at: datetime

    def __post_init__(self) -> None:
        _require_plain_date("LaneSession", "day", self.day)
        if not isinstance(self.windows, tuple):
            raise TypeError(f"LaneSession.windows: expected a tuple of (start, end) pairs, got {self.windows!r}")
        previous_end: datetime | None = None
        for window in self.windows:
            try:
                start, end = window
            except (TypeError, ValueError):
                raise TypeError(f"LaneSession.windows: expected (start, end) pairs, got {window!r}") from None
            _require_utc("LaneSession", "windows", start)
            _require_utc("LaneSession", "windows", end)
            if not start < end:
                raise ValueError(f"LaneSession.windows: window must start before it ends, got {start} / {end}")
            if previous_end is not None and start < previous_end:
                raise ValueError(f"LaneSession.windows: windows must be ascending and disjoint, got {self.windows!r}")
            previous_end = end
        _require_utc("LaneSession", "flatten_at", self.flatten_at)
        if previous_end is not None and previous_end > self.flatten_at:
            raise ValueError(f"LaneSession.windows: a window ends after the flatten time {self.flatten_at}")

    def in_signal_window(self, start: datetime) -> bool:
        """Czy świeca sygnału o początku `start` (UTC) zaczyna się w którymś oknie ([początek, koniec))."""
        _require_utc("LaneSession.in_signal_window", "start", start)
        return any(opens <= start < closes for opens, closes in self.windows)


def lane_session(cfg: BarRuleConfig, calendar: ExchangeCalendar, day: date) -> LaneSession | None:
    """Okna sygnałów i spłaszczenie pasma w dniu `day` (data lokalna kalendarza) albo None, gdy to nie dzień sesji.

    Godziny z `cfg` (`signal_windows`, `flatten_before_close_min`) są czasem lokalnym strefy `cfg.session_timezone`,
    która MUSI być strefą kalendarza (inaczej ValueError: dwa źródła strefy nie mogą się rozjechać). Zamknięcie, święta
    i skrócone sesje pochodzą z kalendarza; okno wykraczające poza godzinę spłaszczenia jest przycinane, a okno w całości
    po niej odpada. Wygasły kalendarz to `CalendarExpiredError` (wywołujący zamienia go na brak handlu).
    """
    if cfg.session_timezone != calendar.profile.tz:
        raise ValueError(
            f"lane_session: lane config time zone {cfg.session_timezone!r} differs from the calendar "
            f"{calendar.profile.name} time zone {calendar.profile.tz!r}"
        )
    if not calendar.is_session_day(day):
        return None
    flatten_at = calendar.local_to_utc(day, calendar.flatten_time(day, cfg.flatten_before_close_min))
    windows: list[tuple[datetime, datetime]] = []
    for opens_local, closes_local in parse_signal_windows(cfg.signal_windows):
        opens = calendar.local_to_utc(day, opens_local)
        closes = min(calendar.local_to_utc(day, closes_local), flatten_at)
        if opens < closes:
            windows.append((opens, closes))
    return LaneSession(day, tuple(windows), flatten_at)


@dataclass(frozen=True)
class LaneDecision:
    """Wynik `decide_entry`: `ENTER` z planem albo przyczyna odrzucenia.

    `plan` jest ustawiony wyłącznie przy `ENTER`. `signal` to sygnał reguły B1, jeśli decyzja doszła do jego znalezienia
    (przy `ENTER` i przy odrzuceniach po sygnale: `STOP_TOO_CLOSE`, `SLIP_FILTER`, `COST_OVER_LIMIT`, `STOP_TOO_TIGHT`,
    `SKIPPED_MIN_SIZE`); inaczej None.
    """

    reason: str
    plan: BarEntryPlan | None = None
    signal: BarSignal | None = None

    def __post_init__(self) -> None:
        if self.reason not in DECISION_REASONS:
            raise ValueError(f"LaneDecision.reason: expected one of {sorted(DECISION_REASONS)}, got {self.reason!r}")
        if self.plan is not None and not isinstance(self.plan, BarEntryPlan):
            raise TypeError(f"LaneDecision.plan: expected BarEntryPlan or None, got {self.plan!r}")
        if self.signal is not None and not isinstance(self.signal, BarSignal):
            raise TypeError(f"LaneDecision.signal: expected BarSignal or None, got {self.signal!r}")
        if self.reason == ENTER:
            if self.plan is None or self.signal is None:
                raise ValueError("LaneDecision: ENTER needs both a plan and a signal")
        elif self.plan is not None:
            raise ValueError(f"LaneDecision: a plan is only allowed with ENTER, got {self.reason!r}")

    @property
    def entered(self) -> bool:
        return self.reason == ENTER


def _true_range_sum(candles: Sequence[Candle], period: int) -> Decimal:
    """Suma prawdziwych zakresów TR (ceny mid) `period` ostatnich świec; wymaga `period + 1` świec (poprzednie zamknięcia)."""
    window = candles[-(period + 1) :]
    total = Decimal(0)
    for previous, current in zip(window, window[1:]):
        true_range = max(
            _CALC.subtract(current.high, current.low),
            _CALC.abs(_CALC.subtract(current.high, previous.close)),
            _CALC.abs(_CALC.subtract(current.low, previous.close)),
        )
        total = _CALC.add(total, true_range)
    return total


def decide_entry(
    candles: Sequence[Candle],
    quote: Quote,
    rules: InstrumentRules,
    risk: RiskConfig,
    cfg: BarRuleConfig,
    session: LaneSession,
    state: DayState,
    now: datetime,
    session_index: int = 0,
    rule_size_allowed: bool = False,
) -> LaneDecision:
    """Decyzja wejścia pasma B1/M5 po zamknięciu świecy sygnału (ostatniej na liście); kolejność kontroli i różnice
    względem badań w docstringu modułu.

    - `candles`: ZAKOŃCZONE świece M5 (ceny mid) rosnąco wg `start`; ostatnia to świeca sygnału, starsze dostarczają
      zakres, kontrolę ciągłości i ATR (potrzeba ich `max(lookback_bars + 2, atr_period + 1)`);
    - `quote`: notowanie z chwili decyzji (wejście long po ask, short po bid; spread do bufora i kosztu);
    - `session`, `state`: okna dnia i stan dnia (muszą dotyczyć tego samego dnia);
    - `now`: chwila decyzji (UTC), nie wcześniej niż koniec świecy sygnału; od końca następnej świecy sygnał jest
      nieaktualny (`STALE_SIGNAL`);
    - `session_index`, `rule_size_allowed`: polityka wielkości jak w `strategy_bars.entry_plan`.

    Wyjątek (zły typ, różne instrumenty, niemalejące znaczniki, niezakończona świeca, stan z innego dnia) oznacza błąd
    wywołującego, a nie odrzucenie wejścia.
    """
    if not isinstance(quote, Quote):
        raise TypeError(f"decide_entry: quote: expected Quote, got {type(quote).__name__} {quote!r}")
    if not isinstance(session, LaneSession):
        raise TypeError(f"decide_entry: session: expected LaneSession, got {type(session).__name__} {session!r}")
    if not isinstance(state, DayState):
        raise TypeError(f"decide_entry: state: expected DayState, got {type(state).__name__} {state!r}")
    if isinstance(session_index, bool) or not isinstance(session_index, int) or session_index < 0:
        raise ValueError(f"decide_entry: session_index: expected a non-negative integer, got {session_index!r}")
    _require_utc("decide_entry", "now", now)
    if state.day != session.day:
        raise ValueError(f"decide_entry: state.day {state.day} is not the session day {session.day}")

    lookback = cfg.lookback_bars
    needed = max(lookback + 1 + 1, cfg.atr_period + 1)  # zakres + świeca sygnału + świeca kontroli ciągłości / ATR
    tail = tuple(candles[-needed:])
    for index, candle in enumerate(tail):
        if not isinstance(candle, Candle):
            raise TypeError(f"decide_entry: candles[{index}]: expected Candle, got {type(candle).__name__} {candle!r}")
    epics = {candle.epic for candle in tail} | {quote.epic, rules.epic, cfg.instrument}
    if len(epics) != 1:
        raise ValueError(f"decide_entry: candles, quote, rules and config must be for the same epic, got {sorted(epics)}")
    for earlier, later in zip(tail, tail[1:]):
        if not earlier.start < later.start:
            raise ValueError(
                f"decide_entry: candle starts must be strictly ascending, got {earlier.start.isoformat()} "
                f"followed by {later.start.isoformat()}"
            )
    if not tail:
        return LaneDecision(INSUFFICIENT_DATA)

    bar = timedelta(minutes=cfg.bar_minutes)
    signal_candle = tail[-1]
    end = signal_candle.start + bar
    if now < end:
        raise ValueError(f"decide_entry: the signal candle is not closed yet (ends {end.isoformat()}, now {now.isoformat()})")
    if now >= end + bar:
        return LaneDecision(STALE_SIGNAL)
    if state.position_open or (state.last_exit_at is not None and signal_candle.start <= state.last_exit_at):
        return LaneDecision(POSITION_OVERLAP)
    if len(tail) < needed:
        return LaneDecision(INSUFFICIENT_DATA)
    if not session.in_signal_window(signal_candle.start):
        return LaneDecision(OUTSIDE_WINDOW)
    if state.entries >= cfg.max_entries_per_day:
        return LaneDecision(MAX_ENTRIES_DAY)
    if state.result_r <= -cfg.day_stop_r:
        return LaneDecision(DAY_STOP)
    span = signal_candle.start - tail[-(lookback + 1 + 1)].start
    if span > bar * (lookback + 1 + cfg.gap_tolerance_bars):
        return LaneDecision(GAPPED_DATA)

    spread = _CALC.subtract(quote.ask, quote.bid)
    signal = bar_signal(tail, spread, rules, cfg)
    if signal is None:
        return LaneDecision(NO_SIGNAL)

    slip = cfg.assumed_slippage_points
    if signal.side is Side.BUY:
        entry = _CALC.add(quote.ask, slip)
        risk_points = _CALC.subtract(entry, signal.stop_level)
    else:
        entry = _CALC.subtract(quote.bid, slip)
        risk_points = _CALC.subtract(signal.stop_level, entry)
    if risk_points <= 0 or risk_points < _CALC.multiply(cfg.min_stop_fraction, entry):
        return LaneDecision(STOP_TOO_CLOSE, signal=signal)
    deviation = _CALC.abs(_CALC.subtract(entry, signal_candle.close))
    if _CALC.multiply(deviation, Decimal(cfg.atr_period)) > _CALC.multiply(
        cfg.slippage_filter_atr_fraction, _true_range_sum(tail, cfg.atr_period)
    ):
        return LaneDecision(SLIP_FILTER, signal=signal)
    round_trip_cost = _CALC.add(_CALC.add(spread, slip), slip)  # spread + wejście + wyjście, każde z założonym poślizgiem
    if round_trip_cost > _CALC.multiply(cfg.max_round_trip_cost_r, risk_points):
        return LaneDecision(COST_OVER_LIMIT, signal=signal)

    plan = entry_plan(signal, quote, rules, risk, cfg, session_index=session_index, rule_size_allowed=rule_size_allowed)
    if isinstance(plan, Skip):
        return LaneDecision(plan.reason, signal=signal)
    return LaneDecision(ENTER, plan=plan, signal=signal)


def exit_due(opened_at: datetime, now: datetime, session: LaneSession, cfg: BarRuleConfig) -> str | None:
    """Czy pozycja pasma otwarta o `opened_at` (UTC) ma być teraz zamknięta przez silnik: `EXIT_TIME_LIMIT`, gdy minęło
    `time_limit_min`, `EXIT_FLATTEN`, gdy nadeszła godzina spłaszczenia dnia, inaczej None.

    Przy obu naraz wygrywa limit czasu (tak oznacza je też `run` w badaniach). Pozycja z poprzedniego dnia to sprawa
    silnika (`app._must_flatten`), nie tej funkcji. Wyjść nigdy nie blokuje bramka ryzyka (R4).
    """
    _require_utc("exit_due", "opened_at", opened_at)
    _require_utc("exit_due", "now", now)
    if not isinstance(session, LaneSession):
        raise TypeError(f"exit_due: session: expected LaneSession, got {type(session).__name__} {session!r}")
    if now < opened_at:
        raise ValueError(f"exit_due: now {now.isoformat()} is before opened_at {opened_at.isoformat()}")
    if now - opened_at >= timedelta(minutes=cfg.time_limit_min):
        return EXIT_TIME_LIMIT
    if now >= session.flatten_at:
        return EXIT_FLATTEN
    return None
