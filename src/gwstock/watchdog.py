"""Nadzór silnika (spec §9, §13): blokada jednej instancji, tętno, wyzwalacze kill switcha i ograniczanie alarmów.

Watchdog nie handluje i niczego nie zamyka: włącza kill switch MIĘKKI (brak nowych wejść, istniejące pozycje
zarządzane normalnie) i alarmuje. Silnik informuje go jawnymi wywołaniami (`note_*`), bez ukrytych powiązań.

Wyzwalacze (każdy idempotentny: kill switch nie powtarza zapisu ani alarmu tej samej przyczyny):
- QUOTE_SILENCE: w oknie sesji (ustawianym przez silnik) brak ŚWIEŻEGO notowania dłużej niż `stale_quote_s`.
  `Quote.ts` to chwila ostatniej ZMIANY pary (bid, ask), więc zamrożone notowanie też jest ciszą. Cisza liczy się
  od najpóźniejszej z chwil: ostatnie notowanie, początek okna, ustawienie okna (start silnika w środku sesji);
- LOGIN_FAILED: `LOGIN_FAILURE_LIMIT` nieudanych logowań z rzędu (dowolnego rodzaju): brak nowych wejść.
  Blokada logowań (`may_login()` zwraca False na stałe) WYŁĄCZNIE po `LOGIN_FAILURE_LIMIT` odrzuceniach DANYCH
  LOGOWANIA (`note_login_failure(..., credential=True)`, adapter: `BrokerAuthError`): Capital.com może zablokować klucz
  po serii błędnych logowań, więc silnik przestaje próbować do restartu procesu (licznik jest w pamięci: restart daje
  trzy nowe próby). Alarm LOGIN_LOCKOUT odsyła do procedury z runbooka (zatrzymać silnik, naprawić klucz, jedna próba
  pojedynczego logowania, start). Plik `data/LOGIN_RESET` (usuwany przy odczycie) zdejmuje blokadę w działającym
  silniku: to obejście dla wsparcia (równoważne restartowi), nie krok właściciela, więc alarm go nie podaje. Awaria
  transportu, timeout, 5xx i 429 NIE blokują: kolejna próba po 30 s, potem odstęp podwajany do 5 minut, bez końca
  (chwilowa awaria nie może odwołać spłaszczenia o 17:25). Po odrzuceniu danych logowania przerwa 30 s;
- REJECTIONS: `REJECTION_LIMIT` odrzuceń zleceń (wejście, zamknięcie, stop) z rzędu;
- RECONCILE_FAILING: `RECONCILE_FAILURE_LIMIT` przebiegów uzgadniania z wyjątkiem z rzędu (np. historia dłuższa
  niż 14 dni: każdy przebieg rzuca, a MISMATCH_TIMEOUT uzgadniania nigdy by nie wypalił). Każdy błąd jest widoczny
  (alarm ograniczany), a niekompletna historia (HISTORY_AMBIGUOUS, HISTORY_INCONSISTENT, HISTORY_TRUNCATED,
  HISTORY_RANGE_TOO_LARGE) daje od razu alarm ERROR dla właściciela;
- UNKNOWN_BROKER_RESPONSES: licznik adaptera `unknown_close_sources` (zamknięcia o nieznanym źródle) osiąga
  `UNKNOWN_RESPONSES_LIMIT`; każdy wzrost licznika to alarm.

Wiele pasm (projekt §2.8): okno sesji, ostatnie notowanie i liczniki uzgadniania (błędy i pominięcia z rzędu) są per
instrument (`epic=...`; bez niego pasmo dotychczasowe DE40 v1, zachowanie jak dotąd). Notowanie jednego instrumentu
nie karmi straży ciszy innego (A7), a udany przebieg uzgadniania jednego pasma nie zeruje awarii drugiego (A8).
Przyczyny kill switcha są wspólne (portfel, R3); alarmy kolejnych pasm mają nazwę instrumentu w treści i w kluczu.

Tętno: JSON `{ts, mode, state, last_quote_ts, kill_level}` (z kolejnymi pasmami także `lanes`) zapisywany atomowo (plik tymczasowy w tym samym katalogu
+ `os.replace`); nieudany zapis zostawia poprzedni plik i nie przerywa pracy. `health` sprawdza świeżość po `ts`.

Blokada instancji: `fcntl.flock` (LOCK_EX | LOCK_NB) na pliku w katalogu danych; jądro zwalnia ją przy śmierci procesu,
więc nie ma „martwych" blokad. Plik nie jest usuwany przy zwolnieniu (usunięcie otwierałoby wyścig dwóch procesów).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gwstock.clock import Clock
from gwstock.config import RiskConfig
from gwstock.journal import Journal
from gwstock.notify import FileNotifier, Notifier
from gwstock.risk import KillSwitch
from gwstock.serialization import dump
from gwstock.types import KillLevel

_LOG = logging.getLogger("gwstock.watchdog")

# Przyczyny kill switcha nadawane przez Watchdog.
QUOTE_SILENCE = "QUOTE_SILENCE"
LOGIN_FAILED = "LOGIN_FAILED"
REJECTIONS = "REJECTIONS"
RECONCILE_FAILING = "RECONCILE_FAILING"
UNKNOWN_BROKER_RESPONSES = "UNKNOWN_BROKER_RESPONSES"

# Stałe procedur nadzoru (spec §9, §13; nie parametry strategii ani ryzyka).
LOGIN_FAILURE_LIMIT = 3  # nieudane logowania z rzędu → kill miękki; tyle odrzuceń danych logowania → blokada prób
LOGIN_RETRY_PAUSE_S = 30  # przerwa po odrzuceniu danych logowania przed kolejną próbą
LOGIN_BACKOFF_START_S = 30  # przerwa po pierwszej awarii innej niż odrzucenie danych (transport, 5xx, 429)
LOGIN_BACKOFF_MAX_S = 300  # najdłuższa przerwa między próbami przy awarii transportu (bez blokady)
REJECTION_LIMIT = 3  # spec §9: trzy odrzucenia z rzędu
RECONCILE_FAILURE_LIMIT = 3  # przebiegi uzgadniania z wyjątkiem z rzędu (3 × 10 s)
RECONCILE_SKIP_LIMIT = 6  # pominięte przebiegi z rzędu (zagłodzenie uzgadniania): sam alarm
UNKNOWN_RESPONSES_LIMIT = 3  # spec §9: nieznane odpowiedzi brokera ponad próg
ALERT_WINDOW_S = 300  # ten sam alarm najwyżej raz na 5 minut
LOGIN_RESET_FILE = "LOGIN_RESET"  # zdejmuje blokadę logowań (zużywany): obejście dla wsparcia, nie krok właściciela
LOUD_HISTORY_CODES = ("HISTORY_AMBIGUOUS", "HISTORY_INCONSISTENT", "HISTORY_TRUNCATED", "HISTORY_RANGE_TOO_LARGE")

SOURCE_WATCHDOG = "watchdog"


class AlreadyRunning(RuntimeError):
    """Inna instancja silnika trzyma blokadę katalogu danych."""


class InstanceLock:
    """Blokada jednej instancji: `fcntl.flock` na pliku (domyślnie `data/engine.lock`), z pid właściciela w środku."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Bierze blokadę albo rzuca `AlreadyRunning` (z pid właściciela, jeśli da się go odczytać)."""
        if self._fd is not None:
            raise RuntimeError(f"{self._path}: lock already held by this object")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_pid(fd)
            os.close(fd)
            raise AlreadyRunning(
                f"another engine instance holds {self._path}" + (f" (pid {holder})" if holder else "")
            ) from None
        except BaseException:
            os.close(fd)
            raise
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)  # dopiero z blokadą: pid poprzedniego właściciela zostaje, dopóki go trzyma
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Zwalnia blokadę (idempotentne)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _read_pid(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 32).decode("ascii", "replace").strip()
    except OSError:
        return ""
    return text if text.isdigit() else ""


class RateLimitedNotifier:
    """Notyfikator ograniczający powtórzenia: ten sam klucz alarmu najwyżej raz na `window_s` sekund.

    Klucz domyślny to (poziom, treść), więc RÓŻNE alarmy nigdy nie są tłumione; `alert_keyed` łączy alarmy o zmiennej
    treści pod jednym kluczem. Liczba stłumionych trafia do najbliższego wysłanego alarmu tego klucza. Nigdy nie rzuca.
    """

    def __init__(self, inner: Notifier, clock: Clock, window_s: float = ALERT_WINDOW_S) -> None:
        self._inner = inner
        self._clock = clock
        self._window = timedelta(seconds=window_s)
        self._window_s = window_s
        self._last: dict[tuple[str, str], tuple[datetime, int]] = {}

    def alert(self, level: str, message: str) -> None:
        self._emit((level, message), level, message)

    def alert_keyed(self, key: str, level: str, message: str) -> None:
        self._emit((level, f"key:{key}"), level, message)

    def _emit(self, key: tuple[str, str], level: str, message: str) -> None:
        try:
            now = self._clock.now_utc()
            seen = self._last.get(key)
            if seen is not None and now - seen[0] < self._window:
                self._last[key] = (seen[0], seen[1] + 1)
                return
            suppressed = 0 if seen is None else seen[1]
            self._last[key] = (now, 0)
            self._prune(now)
            text = f"{message} (+{suppressed} suppressed in {self._window_s:g} s)" if suppressed else message
            self._inner.alert(level, text)
        except Exception:  # alarm nie jest warunkiem dalszej pracy
            _LOG.warning("alert could not be delivered: %s %s", level, message)

    def _prune(self, now: datetime) -> None:
        if len(self._last) > 512:
            for key in [k for k, (t, n) in self._last.items() if now - t >= self._window and n == 0]:
                del self._last[key]


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime
    armed_at: datetime


class Watchdog:
    """Nadzór: notyfikacje od silnika (`note_*`), `tick()` co takt (cisza notowań, licznik adaptera, tętno)."""

    def __init__(
        self,
        clock: Clock,
        journal: Journal,
        kill: KillSwitch,
        broker: object,
        risk: RiskConfig,
        heartbeat_path: Path,
        *,
        notifier: Notifier | None = None,
        reset_path: Path | None = None,
    ) -> None:
        self._clock = clock
        self._journal = journal
        self._kill = kill
        self._broker = broker
        self._risk = risk
        self._heartbeat = Path(heartbeat_path)
        self._notifier: Notifier = (
            notifier if notifier is not None else FileNotifier(self._heartbeat.parent / "alerts.log", clock)
        )
        self._reset_path = Path(reset_path) if reset_path is not None else self._heartbeat.parent / LOGIN_RESET_FILE
        # Okna sesji, ostatnie notowania i liczniki uzgadniania per instrument (projekt wielu pasm §2.8, A7, A8).
        # Klucz None: pasmo dotychczasowe (DE40 v1, wywołania bez `epic`); klucz `epic`: kolejne pasmo.
        self._windows: dict[str | None, _Window] = {}
        self._last_quotes: dict[str | None, datetime] = {}
        self._reconcile_errors_by: dict[str | None, int] = {}
        self._reconcile_skips_by: dict[str | None, int] = {}
        self._mode = ""
        self._state = ""
        self._login_failures = 0  # porażki z rzędu (dowolne): kill miękki przy limicie
        self._auth_failures = 0  # odrzucenia danych logowania od ostatniego sukcesu: blokada przy limicie
        self._transient_failures = 0  # awarie transportu z rzędu: wykładniczy odstęp
        self._last_login_failure: datetime | None = None
        self._login_pause_s = 0.0
        self._rejections = 0
        self._unknown_seen = 0

    # --- stan dla tętna i okno sesji ---

    @property
    def last_quote_ts(self) -> datetime | None:
        """Ostatnie notowanie pasma dotychczasowego (wywołania bez `epic`)."""
        return self._last_quotes.get(None)

    def last_quote_of(self, epic: str | None) -> datetime | None:
        """Ostatnie notowanie instrumentu `epic` (None: pasmo dotychczasowe)."""
        return self._last_quotes.get(epic)

    @property
    def login_failures(self) -> int:
        return self._login_failures

    def set_status(self, *, mode: str | None = None, state: str | None = None) -> None:
        if mode is not None:
            self._mode = mode
        if state is not None:
            self._state = state

    def set_session_window(self, start: datetime | None, end: datetime | None, *, epic: str | None = None) -> None:
        """Okno, w którym obowiązuje straż ciszy notowań (UTC); None wyłącza straż (poza sesją).

        `epic`: okno instrumentu kolejnego pasma; bez niego okno pasma dotychczasowego. Okna są niezależne: notowanie
        jednego instrumentu nigdy nie karmi straży innego (A7)."""
        if start is None or end is None:
            self._windows.pop(epic, None)
            return
        self._windows[epic] = _Window(start, end, self._now())

    def rearm_silence(self) -> None:
        """Cisza notowań liczona od teraz we WSZYSTKICH oknach (po długim wejściu, w trakcie którego pętla nie
        odczytywała notowań żadnego pasma)."""
        now = self._now()
        for key, w in list(self._windows.items()):
            self._windows[key] = _Window(w.start, w.end, now)

    # --- notyfikacje od silnika ---

    def note_quote(self, ts: datetime, *, epic: str | None = None) -> None:
        """Udany odczyt notowania; `ts` to chwila ostatniej zmiany pary (bid, ask) (`Quote.ts`). `epic`: instrument
        kolejnego pasma (bez niego: pasmo dotychczasowe)."""
        last = self._last_quotes.get(epic)
        if last is None or ts > last:
            self._last_quotes[epic] = ts

    def note_login_failure(self, detail: str = "", *, credential: bool = False) -> None:
        """Nieudane logowanie. `credential=True`: broker odrzucił dane logowania (liczy się do blokady prób)."""
        self._login_failures += 1
        self._last_login_failure = self._now()
        n = self._login_failures
        if credential:
            self._auth_failures += 1
            self._login_pause_s = LOGIN_RETRY_PAUSE_S
        else:
            self._transient_failures += 1
            self._login_pause_s = min(
                LOGIN_BACKOFF_START_S * 2 ** (self._transient_failures - 1), LOGIN_BACKOFF_MAX_S
            )
        kind = "credentials rejected" if credential else f"retry in {self._login_pause_s:g} s"
        self._alert_keyed("LOGIN_FAILED", "ERROR", f"LOGIN_FAILED {n} in a row ({kind}): {detail}")
        if n >= LOGIN_FAILURE_LIMIT:
            self._kill.soft(LOGIN_FAILED)
        if credential and self._auth_failures >= LOGIN_FAILURE_LIMIT:
            n = self._auth_failures
            self._alert_keyed(
                "LOGIN_LOCKOUT",
                "ERROR",
                f"LOGIN_LOCKOUT: {n} credential rejections; no further login attempts (key lock-out protection); "
                "positions keep their broker stops; follow the runbook (section 9, LOGIN_FAILED): stop the engine, "
                "fix and check the key first, then one single-login probe, then restart",
            )
            self._event("LOGIN_LOCKOUT", failures=n, detail=detail)

    def note_login_success(self) -> None:
        self._login_failures = 0
        self._auth_failures = 0
        self._transient_failures = 0
        self._last_login_failure = None
        self._login_pause_s = 0.0

    def may_login(self) -> bool:
        """Czy wolno wywołać `broker.login()` teraz (blokada po serii porażek, przerwa po porażce)."""
        self._consume_reset()
        if self._auth_failures >= LOGIN_FAILURE_LIMIT:
            return False
        if self._last_login_failure is not None:
            if (self._now() - self._last_login_failure).total_seconds() < self._login_pause_s:
                return False
        return True

    def note_rejection(self, detail: str = "") -> None:
        self._rejections += 1
        if self._rejections >= REJECTION_LIMIT:
            self._kill.soft(REJECTIONS)
            self._alert_keyed(
                "REJECTIONS", "ERROR", f"REJECTIONS: {self._rejections} broker rejections in a row (last: {detail})"
            )

    def note_success(self) -> None:
        self._rejections = 0

    def note_reconcile_error(self, exc_type: str, message: str = "", *, epic: str | None = None) -> None:
        """Przebieg uzgadniania z wyjątkiem. `epic`: uzgadnianie kolejnego pasma (licznik tylko tego pasma, A8)."""
        n = self._reconcile_errors_by.get(epic, 0) + 1
        self._reconcile_errors_by[epic] = n
        key, lane = _keyed("RECONCILE_ERROR", epic), _lane_text(epic)
        self._alert_keyed(key, "WARN", f"RECONCILE_ERROR{lane} {n} in a row: {exc_type}: {message}")
        for code in LOUD_HISTORY_CODES:
            if code in message:
                self._alert_keyed(
                    _keyed(code, epic),
                    "ERROR",
                    f"{code}{lane}: the broker history cannot be read completely ({exc_type}: {message}); closures "
                    "cannot be booked until it can; the owner checks the Capital.com activity",
                )
        if n >= RECONCILE_FAILURE_LIMIT:
            self._kill.soft(RECONCILE_FAILING)
            message = f"{RECONCILE_FAILING}{lane}: {n} reconciliation passes failed in a row ({exc_type})"
            self._alert_keyed(_keyed(RECONCILE_FAILING, epic), "ERROR", message)

    def note_reconcile_ok(self, *, epic: str | None = None) -> None:
        """Udany przebieg uzgadniania pasma `epic` (None: dotychczasowe): zeruje liczniki TYLKO tego pasma."""
        self._reconcile_errors_by[epic] = 0
        self._reconcile_skips_by[epic] = 0

    def note_reconcile_skipped(self, *, epic: str | None = None) -> None:
        n = self._reconcile_skips_by.get(epic, 0) + 1
        self._reconcile_skips_by[epic] = n
        if n >= RECONCILE_SKIP_LIMIT:
            self._alert_keyed(
                _keyed("RECONCILE_STARVED", epic), "WARN",
                f"RECONCILE_STARVED{_lane_text(epic)}: {n} reconciliation passes skipped",
            )  # fmt: skip

    # --- takt ---

    async def tick(self) -> None:
        """Cisza notowań, licznik nieznanych odpowiedzi adaptera i zapis tętna. Nigdy nie rzuca."""
        now = self._now()
        try:
            self._check_silence(now)
        except Exception as exc:  # nadzór nie może przerwać pętli silnika
            _LOG.warning("quote silence check failed: %s: %s", type(exc).__name__, exc)
        try:
            self._check_unknown_sources()
        except Exception as exc:
            _LOG.warning("unknown response check failed: %s: %s", type(exc).__name__, exc)
        self._write_heartbeat(now)

    def _check_silence(self, now: datetime) -> None:
        """Każde okno osobno (kolejność stała: dotychczasowe, potem instrumenty alfabetycznie); przyczyna kill switcha
        wspólna (QUOTE_SILENCE, kill miękki portfela, R3), alarm z nazwą instrumentu kolejnego pasma."""
        for epic in sorted(self._windows, key=lambda k: (k is not None, k or "")):
            w = self._windows[epic]
            if not w.start <= now <= w.end:
                continue
            ref = max(w.start, w.armed_at)
            last = self._last_quotes.get(epic)
            if last is not None and last > ref:
                ref = last
            silent = (now - ref).total_seconds()
            if silent > self._risk.stale_quote_s:
                self._kill.soft(QUOTE_SILENCE)
                message = (
                    f"{QUOTE_SILENCE}{_lane_text(epic)}: no fresh quote for {silent:.0f} s "
                    f"(limit {self._risk.stale_quote_s} s)"
                )
                self._alert_keyed(_keyed(QUOTE_SILENCE, epic), "ERROR", message)

    def _check_unknown_sources(self) -> None:
        count = getattr(self._broker, "unknown_close_sources", None)
        if isinstance(count, bool) or not isinstance(count, int) or count <= self._unknown_seen:
            return
        self._unknown_seen = count
        self._alert_keyed(
            "UNKNOWN_CLOSE_SOURCE",
            "WARN",
            f"UNKNOWN_CLOSE_SOURCE: {count} closure(s) with an unknown source in the broker history (booked as FORCED)",
        )
        if count >= UNKNOWN_RESPONSES_LIMIT:
            self._kill.soft(UNKNOWN_BROKER_RESPONSES)

    def _write_heartbeat(self, now: datetime) -> None:
        payload = {
            "ts": now,
            "mode": self._mode,
            "state": self._state,
            "last_quote_ts": self._last_quotes.get(None),
            "kill_level": _level_name(self._kill),
        }
        lanes = sorted(k for k in set(self._windows) | set(self._last_quotes) if k is not None)
        if lanes:  # tylko z kolejnymi pasmami: tętno jednego pasma bez zmian (`cli.health` czyta `ts`)
            payload["lanes"] = {epic: {"last_quote_ts": self._last_quotes.get(epic)} for epic in lanes}
        tmp =self._heartbeat.with_name(self._heartbeat.name + ".tmp")
        try:
            tmp.write_text(dump(payload) + "\n", encoding="ascii")
            os.replace(tmp, self._heartbeat)
        except Exception as exc:
            _LOG.warning("heartbeat not written: %s: %s", type(exc).__name__, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # --- wnętrze ---

    def _consume_reset(self) -> None:
        """`data/LOGIN_RESET`: obejście dla wsparcia (jak restart: liczniki od zera), nie krok właściciela."""
        try:
            present = self._reset_path.exists()
        except OSError:
            return
        if not present:
            return
        try:
            self._reset_path.unlink()
        except OSError as exc:  # nieusuwalny plik nie może zdejmować blokady w kółko
            _LOG.warning("cannot consume %s: %s", self._reset_path, exc)
            return
        was = self._auth_failures
        self._login_failures = 0
        self._auth_failures = 0
        self._transient_failures = 0
        self._last_login_failure = None
        self._login_pause_s = 0.0
        self._alert_keyed(
            "LOGIN_LOCKOUT_RESET", "INFO", f"LOGIN_LOCKOUT_RESET by the owner ({was} failures forgotten); logins resume"
        )

    def _alert_keyed(self, key: str, level: str, message: str) -> None:
        try:
            keyed = getattr(self._notifier, "alert_keyed", None)
            if keyed is not None:
                keyed(key, level, message)
            else:
                self._notifier.alert(level, message)
        except Exception:
            _LOG.warning("alert could not be delivered: %s %s", level, message)

    def _event(self, code: str, **details: Any) -> None:
        try:
            self._journal.append_raw(SOURCE_WATCHDOG, dump({"event": code, **details}), self._now())
        except Exception as exc:  # ślad wg możliwości; kill switch i alarm już poszły
            _LOG.warning("watchdog event %s not journaled: %s", code, exc)

    def _now(self) -> datetime:
        return self._clock.now_utc()


def _keyed(code: str, epic: str | None) -> str:
    """Klucz alarmu: dotychczasowy dla pasma bez `epic`, z instrumentem dla kolejnego pasma (osobny budżet)."""
    return code if epic is None else f"{code}:{epic}"


def _lane_text(epic: str | None) -> str:
    return "" if epic is None else f" ({epic})"


def _level_name(kill: KillSwitch) -> str:
    try:
        return KillLevel(kill.level()).name
    except Exception:
        return "UNKNOWN"


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    """Zawartość tętna albo None (brak pliku, nieczytelny JSON, nie obiekt)."""
    try:
        data = json.loads(Path(path).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_age_s(path: Path, now: datetime) -> float | None:
    """Wiek tętna w sekundach wg pola `ts` (UTC) albo None, gdy pliku nie ma lub jest nieczytelny."""
    data = read_heartbeat(path)
    if data is None or not isinstance(data.get("ts"), str):
        return None
    try:
        ts = datetime.fromisoformat(data["ts"])
    except ValueError:
        return None
    if ts.utcoffset() is None:
        return None
    return (now - ts).total_seconds()
