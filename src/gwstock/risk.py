"""Bramka ryzyka i kill switch (spec §9).

Bramka (`RiskGate`) blokuje WEJŚCIA, które łamią twarde limity; wyjść nie blokuje nigdy (`check_exit` zawsze
przechodzi, nawet przy kill switchu HARD i zepsutym dzienniku). Jest tylko do odczytu: nic nie zapisuje.
`Verdict.reasons` zawiera WSZYSTKIE niespełnione kontrole, w kolejności tabeli ze spec §9 (bez duplikatów).

Zasada nadrzędna: kontrola, której nie da się ocenić (błąd odczytu dziennika, zepsuty zegar lub kalendarz,
brak konta), BLOKUJE swoim powodem. Bramka nigdy nie rzuca wyjątku z powodu złych danych.

Kontrole (próg, granica, powód):

- FORMAT: epic to napis równy instrumentowi z konfiguracji strategii; `side` to `Side`; `size`, `entry_price`,
  `value_per_point`, `margin_rate` to skończone `Decimal` > 0; `stop_distance` to skończony `Decimal` (znak
  ocenia NO_STOP). Cokolwiek innego (też brak obiektu kandydata) to FORMAT. Kontrole zależne od złego pola są
  wtedy pomijane (powód FORMAT już blokuje), więc zły typ nie powoduje wyjątku ani lawiny powodów.
  Obcy instrument jest FORMAT-em, bo v1 handluje jednym instrumentem, a kalendarz i okno dotyczą tylko jego.
- NO_STOP: `stop_distance` jest `None` (brak stopu) albo `<= 0` (odległość stopu to jedyna informacja o stopie:
  zero lub minus to brak stopu albo stop po złej stronie). Brak stopu to więc FORMAT + NO_STOP; stop złego typu
  (np. float, napis, NaN) to samo FORMAT.
- LOSS_PER_TRADE: `size × stop_distance × value_per_point > max_loss_per_trade` (równo przechodzi, o grosz więcej
  blokuje).
- LEVERAGE: `notional = size × entry_price × value_per_point`; `capital = min(virtual_capital, account.equity)`;
  blokuje, gdy `notional / capital > max_effective_leverage` (liczone bez dzielenia: `notional > limit × capital`)
  albo `capital <= 0`. Brak konta (zły typ) też blokuje.
- MARGIN: `required = size × entry_price × value_per_point × margin_rate`; blokuje, gdy
  `required > account.available × (1 − margin_reserve_fraction)` (równo przechodzi).
- ENTRIES_PER_DAY: `journal.count_entry_orders(day) >= max_entries_per_day`; `day` to lokalna data handlowa
  (`calendar.to_local(clock.now_utc()).date()`), a liczą się wszystkie zlecenia WEJŚCIA dnia (rola `entry`) w każdym
  stanie; zlecenie próby `smoke` nie zużywa dziennego wejścia.
- DAILY_LOSS: `loss_so_far = max(0, −journal.realized_pnl(day)) + open_risk`, gdzie `open_risk` to ryzyko pozycji
  instrumentu zapisanej w dzienniku (stan inny niż FLAT): `size × |open_level − stop_level| × value_per_point`,
  a przy nieznanym stopie lub wielkości pełne `max_loss_per_trade`; 0, gdy pozycji nie ma. Blokuje, gdy
  `loss_so_far >= max_daily_loss` ALBO `loss_so_far + ryzyko_kandydata > max_daily_loss`. Zysk dnia niczego nie
  kompensuje (liczy się tylko strata).
- TOTAL_DRAWDOWN: `journal.realized_pnl_total() <= −max_total_drawdown` (równo −12 blokuje).
- NOT_SESSION: lokalny dzień nie jest dniem sesji (weekend, święto) albo kalendarz wygasł
  (`CalendarExpiredError`, spec §8) albo kalendarz zawiódł. Gdy ta kontrola nie przechodzi, OUTSIDE_WINDOW nie
  jest dodawane.
- OUTSIDE_WINDOW: wejście wolno tylko wtedy, gdy `open + range_minutes <= teraz <= open + range_minutes +
  entry_deadline_after_range_min` (granice włącznie: 09:05:00 do 09:10:00 w Berlinie, bez ułamka sekundy
  ponad) ORAZ `teraz < godzina spłaszczenia` (`calendar.flatten_time(day, flatten_before_close_min)`, więc także
  przy skróconej sesji). Wszystkie godziny pochodzą z kalendarza i konfiguracji, porównanie jest w UTC
  (`calendar.local_to_utc`), więc zmiana czasu nie przesuwa okna lokalnie. Okna, którego nie da się policzyć,
  blokuje.
- DUPLICATE: dziennik ma pozycję instrumentu w stanie innym niż FLAT albo jakiekolwiek nierozstrzygnięte
  zlecenie (INTENT, SUBMITTING, UNKNOWN) na tym instrumencie.
- STALE_DATA: `quote_age_s > stale_quote_s` (60 przechodzi, 61 blokuje). Wiek ujemny, NaN, nieskończony, `bool`
  albo nieliczbowy to STALE_DATA (nie da się go ocenić), nie FORMAT.
- KILL: `max(kill.level(), journal.kill_state().level) != NONE`. Poziom w pamięci się liczy, bo zapis kill
  switcha do dziennika mógł się nie udać.

`check_smoke` (próba akceptacyjna L0: jedno minimalne wejście poza oknem strategii, polecenie `smoke`) to te same
kontrole w tej samej kolejności BEZ NOT_SESSION, OUTSIDE_WINDOW i ENTRIES_PER_DAY; pozostałe (KILL, DUPLICATE, FORMAT,
NO_STOP, LOSS_PER_TRADE, LEVERAGE, MARGIN, DAILY_LOSS, TOTAL_DRAWDOWN, STALE_DATA) obowiązują bez zmian.

Kill switch (`KillSwitch`) to awaryjny wyłącznik. Poziom jest w pamięci (decyduje natychmiast) i w dzienniku
(przeżywa restart, `set_kill`), a zdjąć go może wyłącznie jawne `clear`:

- `soft`/`hard` NIGDY nie rzucają: poziom w pamięci jest ustawiany jako pierwszy krok, przed jakimkolwiek odczytem
  i zapisem dziennika (także przed ponowieniem odczytu nieznanego stanu startowego, który na zajętej bazie trwa
  sekundy; `scan_files` sprawdza pliki przed tym odczytem), awaria zapisu do dziennika
  (JournalWriteError lub cokolwiek innego) kończy się alarmem z napisem JOURNAL_WRITE_FAILED, a awaria
  notyfikatora tylko wpisem do logu. Nieudany zapis stanu jest ponawiany przy kolejnym `soft`/`hard`/`scan_files`.
- Idempotencja: zapis do dziennika i alarm tylko gdy poziom rośnie albo przyczyna jest nowa na danym poziomie;
  powtórzenia (Reconciler woła `soft` co 10 s) są tanim no-opem.
- Poziom nigdy nie spada: `soft` przy HARD zostawia HARD (przyczyna trafia raz do audytu `kill.audit`).
- Każda zmiana zostawia wiersz audytu (`journal.append_raw("kill.audit", ...)`), więc historia przyczyn nie ginie
  mimo jednego wiersza stanu.
- `clear(requested_by)` wymaga niepustego `requested_by`, odmawia (ValueError), dopóki leży `data/STOP` lub
  `data/HARD_STOP` (inaczej `scan_files` włączyłby kill switch od razu) albo nie da się tego sprawdzić, a także
  dopóki nie da się odczytać zapisanego stanu (mógł tam być HARD; `clear` sam ponawia odczyt). Alarm i audyt
  zdjęcia podają poziom po scaleniu z dziennikiem.
- Nie da się odczytać zapisanego stanu przy starcie: start od SOFT (`KILL_STATE_UNREADABLE`, nie znamy poprzedniego
  poziomu). Dopóki stanu nie znamy, `soft` nie nadpisuje dziennika (mógł tam być HARD), a każdy alarm niesie znacznik
  NOT_PERSISTED: poziom jest tylko w pamięci. Odczyt jest ponawiany przy każdym `soft`/`hard`/`scan_files`/`clear`; po
  sukcesie zapisany poziom wchodzi do pamięci (HARD wraca, nigdy nie obniżamy), a poziom z pamięci, jeśli jest
  wyższy od zapisanego, trafia do dziennika (wiersz stanu + audyt). HARD zapisuje się od razu (niczego nie obniży).
- `data/HARD_STOP`, którego obecności nie da się sprawdzić (błąd systemu plików inny niż brak pliku), włącza tylko
  SOFT (`STOP_FILE_UNREADABLE`), nie HARD: HARD zamyka pozycje, a niepewny odczyt pliku nie jest na to dość mocnym
  dowodem. Świadomy fail-safe: blokujemy wejścia, nie robimy nieodwracalnych ruchów.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Protocol, runtime_checkable

from gwstock.calendar import ExchangeCalendar
from gwstock.clock import Clock
from gwstock.config import RiskConfig, StrategyConfig
from gwstock.journal import Journal
from gwstock.notify import Notifier
from gwstock.serialization import dump
from gwstock.types import AccountInfo, KillLevel, PositionState, Side

_LOG = logging.getLogger("gwstock.risk")

# Powody blokady (`Verdict.reasons`), w kolejności tabeli ze spec §9.
FORMAT = "FORMAT"
NO_STOP = "NO_STOP"
LOSS_PER_TRADE = "LOSS_PER_TRADE"
LEVERAGE = "LEVERAGE"
MARGIN = "MARGIN"
ENTRIES_PER_DAY = "ENTRIES_PER_DAY"
DAILY_LOSS = "DAILY_LOSS"
TOTAL_DRAWDOWN = "TOTAL_DRAWDOWN"
NOT_SESSION = "NOT_SESSION"
OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
DUPLICATE = "DUPLICATE"
STALE_DATA = "STALE_DATA"
KILL = "KILL"
# Kontrole, których nie stosuje `check_smoke` (próba L0 biegnie poza oknem strategii i nie zużywa wejścia dnia).
_SMOKE_EXEMPT = frozenset({NOT_SESSION, OUTSIDE_WINDOW, ENTRIES_PER_DAY})

# Przyczyny kill switcha nadawane przez sam moduł oraz znacznik awarii zapisu w treści alarmu.
STOP_FILE = "STOP_FILE"
HARD_STOP_FILE = "HARD_STOP_FILE"
STOP_FILE_UNREADABLE = "STOP_FILE_UNREADABLE"
KILL_STATE_UNREADABLE = "KILL_STATE_UNREADABLE"
JOURNAL_WRITE_FAILED = "JOURNAL_WRITE_FAILED"
NOT_PERSISTED = "NOT_PERSISTED"
UNSPECIFIED = "UNSPECIFIED"

# Iloczyny pieniężne liczymy dokładnie: zaokrąglenie (domyślne 28 cyfr) mogłoby zamienić "o grosz więcej" w "równo".
# Pułapka Inexact zamiast cichego zaokrąglenia: wynik nie do policzenia dokładnie blokuje wejście.
_EXACT = Context(prec=80, traps=[InvalidOperation, Overflow, Inexact])
_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True)
class Verdict:
    """Werdykt bramki. `passed` jest prawdą dokładnie wtedy, gdy `reasons` jest puste."""

    passed: bool
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool) or self.passed != (not self.reasons):
            raise ValueError(f"Verdict: passed={self.passed!r} is inconsistent with reasons={self.reasons!r}")


@dataclass(frozen=True)
class EntryCandidate:
    """Kandydat na wejście. NIE waliduje się przy tworzeniu: ocenia go bramka (zły typ to FORMAT, nie wyjątek).

    `quote_age_s`: wiek notowania w sekundach (int, Decimal lub float, np. `timedelta.total_seconds()`).
    """

    epic: str
    side: Side
    size: Decimal
    stop_distance: Decimal
    entry_price: Decimal
    value_per_point: Decimal
    margin_rate: Decimal
    quote_age_s: Decimal | int | float


@runtime_checkable
class KillView(Protocol):
    """Widok kill switcha dla bramki (spełnia go `KillSwitch`): bieżący poziom z pamięci."""

    def level(self) -> KillLevel: ...


def _finite(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _positive(value: object) -> Decimal | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _product(*factors: Decimal) -> Decimal:
    result = factors[0]
    for factor in factors[1:]:
        result = _EXACT.multiply(result, factor)
    return result


def _age_ok(age: object) -> bool:
    """Wiek notowania da się ocenić: nieujemna, skończona liczba (nie `bool`)."""
    if isinstance(age, bool):
        return False
    if isinstance(age, int):
        return age >= 0
    if isinstance(age, float):
        return math.isfinite(age) and age >= 0
    if isinstance(age, Decimal):
        return age.is_finite() and age >= 0
    return False


class RiskGate:
    """Bramka wejść. Wejścia `check_entry`; wyjścia `check_exit` (zawsze przechodzą)."""

    def __init__(
        self,
        cfg: RiskConfig,
        strat: StrategyConfig,
        journal: Journal,
        calendar: ExchangeCalendar,
        clock: Clock,
        kill: KillView | None = None,
    ) -> None:
        self._cfg = cfg
        self._strat = strat
        self._journal = journal
        self._calendar = calendar
        self._clock = clock
        self._kill = kill

    def check_exit(self) -> Verdict:
        """Wyjścia nigdy nie są blokowane: nie zależy od dziennika, zegara, kalendarza ani kill switcha."""
        return Verdict(True, [])

    def check_entry(self, c: EntryCandidate, account: AccountInfo) -> Verdict:
        return self._evaluate(c, account, frozenset())

    def check_smoke(self, c: EntryCandidate, account: AccountInfo) -> Verdict:
        """Wejście próby `smoke` (L0): jak `check_entry`, ale bez NOT_SESSION, OUTSIDE_WINDOW i ENTRIES_PER_DAY."""
        return self._evaluate(c, account, _SMOKE_EXEMPT)

    def _evaluate(self, c: object, account: object, exempt: frozenset[str]) -> Verdict:
        try:
            return self._check_entry(c, account, exempt)
        except Exception:  # siatka bezpieczeństwa: bramka nie rzuca, a niepewny kandydat nie przechodzi
            _LOG.exception("risk gate failed unexpectedly, blocking the entry")
            return Verdict(False, [FORMAT])

    # --- ocena ---

    def _check_entry(self, c: object, account: object, exempt: frozenset[str] = frozenset()) -> Verdict:
        epic, side, size, stop, price, vpp, rate, age = self._fields(c)
        epic_ok = isinstance(epic, str) and epic == self._strat.instrument
        size_d, price_d, vpp_d, rate_d = _positive(size), _positive(price), _positive(vpp), _positive(rate)
        stop_d = _finite(stop)
        format_bad = not (
            epic_ok
            and isinstance(side, Side)
            and size_d is not None
            and stop_d is not None
            and price_d is not None
            and vpp_d is not None
            and rate_d is not None
        )
        now, day = self._today()
        window = self._window(now, day)
        instrument = self._strat.instrument

        def risk_of_candidate() -> Decimal | None:
            if size_d is None or stop_d is None or vpp_d is None:
                return None
            return _product(size_d, stop_d, vpp_d)

        def loss_per_trade() -> bool:
            risk = risk_of_candidate()
            return risk is not None and risk > self._cfg.max_loss_per_trade

        def leverage() -> bool:
            if not isinstance(account, AccountInfo):
                return True
            capital = min(self._cfg.virtual_capital, account.equity)
            if capital <= 0:
                return True
            if size_d is None or price_d is None or vpp_d is None:
                return False
            return _product(size_d, price_d, vpp_d) > _product(self._cfg.max_effective_leverage, capital)

        def margin() -> bool:
            if not isinstance(account, AccountInfo):
                return True
            if size_d is None or price_d is None or vpp_d is None or rate_d is None:
                return False
            required = _product(size_d, price_d, vpp_d, rate_d)
            usable = _product(account.available, _EXACT.subtract(_ONE, self._cfg.margin_reserve_fraction))
            return required > usable

        def entries_per_day() -> bool:
            return day is None or self._journal.count_entry_orders(day) >= self._cfg.max_entries_per_day

        def daily_loss() -> bool:
            if day is None:
                return True
            lost = _EXACT.add(max(_ZERO, _EXACT.minus(self._journal.realized_pnl(day))), self._open_risk(vpp_d))
            if lost >= self._cfg.max_daily_loss:
                return True
            risk = risk_of_candidate()
            return risk is not None and _EXACT.add(lost, max(_ZERO, risk)) > self._cfg.max_daily_loss

        def total_drawdown() -> bool:
            return self._journal.realized_pnl_total() <= _EXACT.minus(self._cfg.max_total_drawdown)

        def duplicate() -> bool:
            position = self._journal.current_position(instrument)
            if position is not None and position.state is not PositionState.FLAT:
                return True
            return any(o.epic == instrument for o in self._journal.unresolved_orders())

        def stale() -> bool:
            return not _age_ok(age) or age > self._cfg.stale_quote_s  # type: ignore[operator]

        def kill_active() -> bool:
            level = KillLevel.NONE if self._kill is None else self._kill.level()
            return max(level, self._journal.kill_state().level) != KillLevel.NONE

        checks: list[tuple[str, Callable[[], bool]]] = [
            (FORMAT, lambda: format_bad),
            (NO_STOP, lambda: stop is None or (stop_d is not None and stop_d <= 0)),
            (LOSS_PER_TRADE, loss_per_trade),
            (LEVERAGE, leverage),
            (MARGIN, margin),
            (ENTRIES_PER_DAY, entries_per_day),
            (DAILY_LOSS, daily_loss),
            (TOTAL_DRAWDOWN, total_drawdown),
            (NOT_SESSION, lambda: window == NOT_SESSION),
            (OUTSIDE_WINDOW, lambda: window == OUTSIDE_WINDOW),
            (DUPLICATE, duplicate),
            (STALE_DATA, stale),
            (KILL, kill_active),
        ]
        reasons = [reason for reason, check in checks if reason not in exempt and self._blocks(reason, check)]
        return Verdict(not reasons, reasons)

    @staticmethod
    def _fields(c: object) -> tuple:
        names = (
            "epic", "side", "size", "stop_distance", "entry_price", "value_per_point", "margin_rate", "quote_age_s",
        )  # fmt: skip
        values = []
        for name in names:
            try:
                values.append(getattr(c, name, None))
            except Exception:  # np. właściwość, która rzuca: pole traktujemy jak brakujące
                values.append(None)
        return tuple(values)

    @staticmethod
    def _blocks(reason: str, check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception as exc:  # nie da się ocenić = blokada
            _LOG.warning("risk check %s could not be evaluated, blocking: %s: %s", reason, type(exc).__name__, exc)
            return True

    def _today(self) -> tuple[datetime | None, object]:
        """Chwila UTC i lokalna data handlowa; `(None, None)`, gdy zegar lub kalendarz zawiodły."""
        try:
            now = self._clock.now_utc()
            return now, self._calendar.to_local(now).date()
        except Exception as exc:
            _LOG.warning("risk gate cannot determine the trading day: %s: %s", type(exc).__name__, exc)
            return None, None

    def _window(self, now: datetime | None, day: object) -> str | None:
        """NOT_SESSION, OUTSIDE_WINDOW albo None (wejście dozwolone czasowo)."""
        if now is None or day is None:
            return NOT_SESSION
        cal, strat = self._calendar, self._strat
        try:
            if not cal.is_session_day(day):  # także CalendarExpiredError (wygasły kalendarz)
                return NOT_SESSION
        except Exception:
            return NOT_SESSION
        try:
            start = datetime.combine(day, cal.open_time(day)) + timedelta(minutes=strat.range_minutes)
            end = start + timedelta(minutes=strat.entry_deadline_after_range_min)
            flatten = datetime.combine(day, cal.flatten_time(day, strat.flatten_before_close_min))
            first = cal.local_to_utc(start.date(), start.time())
            last = cal.local_to_utc(end.date(), end.time())
            flatten_utc = cal.local_to_utc(flatten.date(), flatten.time())
            return None if first <= now <= last and now < flatten_utc else OUTSIDE_WINDOW
        except Exception as exc:
            _LOG.warning("risk gate cannot compute the entry window: %s: %s", type(exc).__name__, exc)
            return OUTSIDE_WINDOW

    def _open_risk(self, vpp: Decimal | None) -> Decimal:
        position = self._journal.current_position(self._strat.instrument)
        if position is None or position.state is PositionState.FLAT:
            return _ZERO
        if position.size is None or position.open_level is None or position.stop_level is None or vpp is None:
            return self._cfg.max_loss_per_trade  # nieznany stop = pełna dozwolona strata
        distance = _EXACT.abs(_EXACT.subtract(position.open_level, position.stop_level))
        return _product(position.size, distance, vpp)


def _marker_present(path: Path) -> bool:
    """Czy plik/łącze istnieje (także zepsute łącze). Inne błędy niż brak pliku propagują: nie wiemy."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _reason_text(reason: object) -> str:
    try:
        text = reason.strip() if isinstance(reason, str) else str(reason).strip()
    except Exception:
        return UNSPECIFIED
    return text or UNSPECIFIED


class KillSwitch:
    """Awaryjny wyłącznik: poziom w pamięci i w dzienniku, alarmy, pliki `STOP` i `HARD_STOP`.

    Spełnia `KillView` (dla bramki) oraz `KillSink` z reconcile (`soft`). Szczegóły w dokumentacji modułu.
    """

    def __init__(self, journal: Journal, clock: Clock, data_dir: Path, notifier: Notifier) -> None:
        self._journal = journal
        self._clock = clock
        self._data_dir = Path(data_dir)
        self._notifier = notifier
        self._lock = threading.RLock()
        # Najwyższy poziom zgłoszony przez soft/hard od ostatniego clear(), ustawiany jako PIERWSZY krok, bez blokady
        # `_lock` i przed jakimkolwiek odczytem dziennika (ten może czekać do 5 s na zajętą bazę): `level()` widzi go
        # od razu. `_floor_lock` chroni tylko to przypisanie i nigdy nie jest trzymany przy I/O.
        self._floor_lock = threading.Lock()
        self._floor = KillLevel.NONE
        self._level = KillLevel.NONE
        self._reason = ""
        self._reasons: set[str] = set()  # przyczyny już zapisane na bieżącym poziomie
        self._suppressed: set[str] = set()  # przyczyny soft zgłoszone przy HARD (do audytu, raz)
        self._unsynced = False  # stan w pamięci czeka na utrwalenie w dzienniku
        self._state_unknown = False  # nie umieliśmy odczytać zapisanego stanu przy starcie
        self._restore()

    # --- odczyt ---

    def level(self) -> KillLevel:
        return max(self._level, self._floor)

    def reason(self) -> str:
        return self._reason

    # --- włączanie ---

    def soft(self, reason: str) -> None:
        self._engage(KillLevel.SOFT, reason)

    def hard(self, reason: str) -> None:
        self._engage(KillLevel.HARD, reason)

    def scan_files(self) -> None:
        """`data/HARD_STOP` włącza HARD, `data/STOP` włącza SOFT.

        Pliku, którego obecności nie da się sprawdzić, nie pomijamy: włącza SOFT (`STOP_FILE_UNREADABLE`), także dla
        HARD_STOP (świadomy fail-safe, zob. dokumentacja modułu). Najpierw pliki (HARD z pliku wchodzi do pamięci przed
        jakimkolwiek odczytem dziennika), potem ponowienie odczytu nieznanego stanu startowego i niedokończonego zapisu
        stanu do dziennika.
        """
        for name, engage, reason in (
            ("HARD_STOP", self.hard, HARD_STOP_FILE),
            ("STOP", self.soft, STOP_FILE),
        ):
            try:
                present = _marker_present(self._data_dir / name)
            except Exception as exc:
                _LOG.warning("cannot check %s: %s: %s", name, type(exc).__name__, exc)
                self.soft(STOP_FILE_UNREADABLE)
                continue
            if present:
                engage(reason)
        with self._lock:
            self._recover()
            self._retry_unsynced()

    def clear(self, requested_by: str) -> None:
        """Jawne zdjęcie poziomu. Rzuca ValueError (brak `requested_by`, plik STOP, nieczytelny zapisany stan) i błąd
        zapisu dziennika.

        Najpierw ponawia odczyt nieznanego stanu startowego: dopóki go nie znamy, odmawia (mógł tam być HARD, a
        zdjęcie nadpisałoby go po ciemku). Alarm i audyt podają poziom po scaleniu z dziennikiem ("was HARD", gdy
        odczyt odzyskał zapisany HARD), nie ten, od którego silnik wystartował.
        """
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise ValueError(f"requested_by: a non-empty name of who clears the kill switch, got {requested_by!r}")
        who = requested_by.strip()
        for name in ("HARD_STOP", "STOP"):
            path = self._data_dir / name
            try:
                present = _marker_present(path)
            except Exception as exc:
                raise ValueError(f"cannot verify that {path} is absent ({exc}); refusing to clear") from exc
            if present:
                raise ValueError(
                    f"{path} exists: remove it first, otherwise scan_files() would engage the kill switch again at once"
                )
        with self._lock:
            self._recover()
            if self._state_unknown:
                raise ValueError("the stored kill state cannot be read; refusing to clear (it may be HARD)")
            previous_level, previous_reason = self._level, self._reason  # już scalone z zapisanym stanem
            ts = self._now()
            self._journal.set_kill(KillLevel.NONE, f"CLEARED by {who}", ts)  # błąd: poziom w pamięci zostaje
            self._level, self._reason = KillLevel.NONE, ""
            with self._floor_lock:
                self._floor = KillLevel.NONE
            self._reasons.clear()
            self._suppressed.clear()
            self._unsynced = False
            self._state_unknown = False
            failures = self._audit(
                "CLEAR", KillLevel.NONE, "", previous_level, previous_reason, ts, requested_by=who
            )
            self._alert(
                "CLEAR",
                f"kill switch cleared by {who} (was {previous_level.name}: {previous_reason})" + self._suffix(failures),
            )

    # --- wnętrze ---

    def _restore(self) -> None:
        try:
            state = self._journal.kill_state()
            level, reason = KillLevel(state.level), str(state.reason)
        except Exception as exc:
            # nie znamy zapisanego stanu: start od SOFT (nie ryzykujemy wejścia), HARD niczym nie zastąpimy
            _LOG.error("cannot read the stored kill state: %s: %s", type(exc).__name__, exc)
            self._level, self._reason = KillLevel.SOFT, KILL_STATE_UNREADABLE
            self._reasons = {KILL_STATE_UNREADABLE}
            self._state_unknown = True
            self._unsynced = True  # poziom jest tylko w pamięci
            self._alert(
                "SOFT",
                f"kill switch SOFT at startup: {KILL_STATE_UNREADABLE} ({type(exc).__name__}: {exc})"
                + self._suffix([]),
            )
            return
        if level != KillLevel.NONE:
            self._level, self._reason, self._reasons = level, reason, {reason}

    def _engage(self, level: KillLevel, reason: object) -> None:
        # Najpierw pamięć, zanim czekamy na blokadę i zanim cokolwiek czyta dziennik: od tej chwili bramka blokuje,
        # a HARD obowiązuje wykonawcę, nawet gdy odczyt nieznanego stanu startowego trwa sekundy.
        with self._floor_lock:
            if level > self._floor:
                self._floor = level
        try:
            text = _reason_text(reason)
            with self._lock:
                self._apply(level, text)
        except Exception:  # soft/hard nie rzucają; poziom w pamięci ma zostać
            _LOG.exception("kill switch bookkeeping failed, level %s kept in memory", level.name)
            if level > self._level:
                self._level = level

    def _recover(self) -> None:
        """`_resolve_unknown`, który nigdy nie rzuca (woła się pod blokadą).

        Błąd samego odzyskiwania zostaje w logu, a reszta kroku (sprawdzenie plików STOP, zapis i alarm włączenia,
        ponowienie zapisu) i tak się wykonuje; stan zostaje nieznany, więc kolejna próba przy następnym wywołaniu.
        """
        try:
            self._resolve_unknown()
        except Exception:
            _LOG.exception("recovery of the stored kill state failed; it is retried on the next call")

    def _resolve_unknown(self) -> None:
        """Ponawia odczyt zapisanego stanu, którego nie udało się odczytać przy starcie (woła się pod blokadą).

        Po sukcesie: zapisany poziom nie niższy od bieżącego wchodzi do pamięci (HARD wraca; dziennik już go ma, więc
        bez zapisu); niższy od bieżącego (np. NONE) zostaje zastąpiony poziomem z pamięci, który trafia do dziennika.
        Nigdy nie obniżamy poziomu. Dopóki odczyt zawodzi, nic się nie zmienia, a poziom zostaje tylko w pamięci.
        """
        if not self._state_unknown:
            return
        try:
            state = self._journal.kill_state()
            stored_level, stored_reason = KillLevel(state.level), str(state.reason)
        except Exception:
            return  # nadal nieczytelny: kolejna próba przy następnym soft/hard/scan_files
        self._state_unknown = False
        ts = self._now()
        memory_level, memory_reason = self._level, self._reason
        if stored_level >= memory_level:
            self._level, self._reason, self._reasons = stored_level, stored_reason, {stored_reason}
            self._unsynced = False  # dziennik już ma ten stan
            self._audit("RECOVERED", stored_level, stored_reason, memory_level, memory_reason, ts)
            self._alert(
                stored_level.name,
                f"kill switch {stored_level.name} restored from the journal after an unreadable start: "
                f"{stored_reason} (memory was {memory_level.name}: {memory_reason})",
            )
            return
        self._audit("RECOVERED", memory_level, memory_reason, stored_level, stored_reason, ts)
        self._unsynced = True
        self._retry_unsynced()  # zapis poziomu z pamięci do dziennika (po sukcesie alarm INFO)

    def _apply(self, level: KillLevel, reason: str) -> None:
        self._recover()
        previous_level, previous_reason = self._level, self._reason
        if level < previous_level:  # poziom nie spada: tylko ślad audytowy, raz na przyczynę
            if reason not in self._suppressed:
                self._suppressed.add(reason)
                self._audit("SUPPRESSED", level, reason, previous_level, previous_reason, self._now())
            self._retry_unsynced()
            return
        if level == previous_level and reason in self._reasons:  # powtórzenie: tani no-op
            self._retry_unsynced()
            return

        # Najpierw pamięć: od tej chwili bramka blokuje, cokolwiek stanie się z dziennikiem i alarmem.
        self._level, self._reason = level, reason
        if level > previous_level:
            self._reasons = {reason}
        else:
            self._reasons.add(reason)
        if previous_level == KillLevel.NONE:
            event, text = "ENGAGE", f"kill switch {level.name}: {reason}"
        elif level > previous_level:
            event = "ESCALATE"
            text = f"kill switch escalated {previous_level.name} -> {level.name}: {reason} (was: {previous_reason})"
        else:
            event = "REASON"
            text = f"kill switch {level.name}: additional reason {reason} (was: {previous_reason})"

        ts = self._now()
        failures: list[str] = []
        if self._state_unknown and level < KillLevel.HARD:
            self._unsynced = True  # zapisanego stanu nie znamy (mógł być HARD): niczego nie nadpisujemy
        else:
            try:
                self._journal.set_kill(level, reason, ts)
                self._unsynced = False
                self._state_unknown = False  # zapisany HARD jest nie niższy od niczego, co tam było
            except Exception as exc:
                self._unsynced = True
                failures.append(f"set_kill {type(exc).__name__}: {exc}")
        failures += self._audit(event, level, reason, previous_level, previous_reason, ts)
        self._alert(level.name, text + self._suffix(failures))

    def _retry_unsynced(self) -> None:
        """Ponawia utrwalenie stanu, gdy wcześniejszy zapis zawiódł; po sukcesie jedna informacja, bez spamu."""
        if not self._unsynced:
            return
        if self._state_unknown and self._level < KillLevel.HARD:
            return  # nie nadpisujemy stanu, którego nie znamy
        ts = self._now()
        try:
            self._journal.set_kill(self._level, self._reason, ts)
        except Exception:
            return  # dziennik nadal leży; alarm o tym już poszedł
        self._unsynced = False
        self._audit("PERSISTED", self._level, self._reason, self._level, self._reason, ts)
        self._alert(
            "INFO",
            f"kill switch {self._level.name} ({self._reason}) is now persisted in the journal after an earlier failure",
        )

    def _audit(
        self,
        event: str,
        level: KillLevel,
        reason: str,
        previous_level: KillLevel,
        previous_reason: str,
        ts: datetime,
        **extra: str,
    ) -> list[str]:
        """Dopisuje wiersz audytu; zwraca opisy błędów (puste = zapisano), nigdy nie rzuca."""
        try:
            payload = dump(
                {
                    "event": event,
                    "level": level.name,
                    "reason": reason,
                    "previous_level": previous_level.name,
                    "previous_reason": previous_reason,
                    "ts": ts,
                    **extra,
                }
            )
            self._journal.append_raw("kill.audit", payload, ts)
            return []
        except Exception as exc:
            return [f"audit {type(exc).__name__}: {exc}"]

    def _suffix(self, failures: list[str]) -> str:
        """Znaczniki dla alarmu: JOURNAL_WRITE_FAILED (co zawiodło) i NOT_PERSISTED (poziom tylko w pamięci)."""
        text = f" {JOURNAL_WRITE_FAILED}: ({'; '.join(failures)})" if failures else ""
        if self._unsynced:
            text += f" {NOT_PERSISTED}: the level is held in memory only and is lost on restart until it is written"
        return text

    def _alert(self, level: str, message: str) -> None:
        try:
            self._notifier.alert(level, message)
        except Exception:  # alarm nie jest warunkiem włączenia; ślad zostaje w logu
            _LOG.error("kill switch alert could not be delivered: %s %s", level, message, exc_info=True)

    def _now(self) -> datetime:
        try:
            ts = self._clock.now_utc()
            if isinstance(ts, datetime) and ts.utcoffset() is not None:
                return ts.astimezone(timezone.utc)
        except Exception:
            pass
        return datetime.now(timezone.utc)  # zepsuty lub naiwny zegar nie może zatrzymać kill switcha
