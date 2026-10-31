"""Adapter Capital.com (REST, host live): jedyny kod, który rozmawia z brokerem.

Źródła: `docs/ops/capitalcom-api-notes.md` (fakty z dokumentacji, pozycje C1–C26 do potwierdzenia),
`docs/ops/capitalcom-live-probe-2026-10-31.md` i `docs/ops/capitalcom-live-readout-2026-11-01.md` (pomiary DE40).

Zasady:
- host: wyłącznie `LIVE_BASE_URL` albo serwer testowy na pętli zwrotnej (127.0.0.1, localhost, ::1); inny adres
  to `ValueError`, więc dane logowania mogą trafić tylko do brokera albo do lokalnego testu;
- dane logowania (`Credentials`) z pliku sekretów o dokładnie trzech kluczach; nigdy nie występują w `repr`, `str`
  ani w komunikatach wyjątków;
- logowanie `POST /session` jawnym hasłem (`encryptedPassword: false`, TLS); klucz API idzie wyłącznie w nagłówku
  `X-CAP-API-KEY` i wyłącznie do `POST /session`; tokeny `CST` i `X-SECURITY-TOKEN` żyją tylko w pamięci i są
  przejmowane z nagłówków późniejszych odpowiedzi, jeśli broker je tam poda (notatki §1.1, C18);
- rachunek związany (`creds.account_id`): jeśli aktywny rachunek sesji (`currentAccountId`) jest inny, dokładnie
  jedno `PUT /session` tuż po logowaniu, przed jakimkolwiek innym wywołaniem, i kontrola `GET /session`; rachunek
  spoza listy logowania → `BrokerError` bez dalszych wywołań; bez `account_id` adapter niczego nie przełącza;
- jedno logowanie naraz (`asyncio.Lock`): równoległe `login()` i równoległe odpowiedzi 401 dzielą jedno
  `POST /session`; 401 → jedno ponowne logowanie i jedna powtórka żądania; nieudane ponowne logowanie albo drugie
  401 zaraz po nim → `SessionExpired` i sesja PORZUCONA: każde następne żądanie → `SessionExpired` bez wysyłki, aż
  silnik sam wywoła `login()` (ponawianie i ochrona przed blokadą konta należą do Watchdoga, T14), więc nic nie
  zapętla się samo;
- logowanie albo `select_account` przerwane w dowolnym miejscu (błąd, przekroczenie czasu, ANULOWANIE) nie
  zostawia sesji: rachunek sesji u brokera byłby wtedy niepewny;
- limity na zegarze silnika (`Clock`): ≥ 0,2 s między dowolnymi żądaniami (5/s, połowa limitu brokera, spec §13),
  ≥ 1 s między `POST /session`, ≥ 0,1 s między zleceniami (`order=True`, część 2); kolejność FIFO; każde żądanie
  wysyłane z `raise_for_status=False`, więc wstrzyknięta sesja HTTP nie omija obsługi 401 i mapowania błędów;
- HTTP 429 nie jest ponawiany automatycznie ani razu: `BrokerError` i wstrzymanie wszystkich żądań na
  `RATE_LIMIT_PAUSE_S` (spec §13 „przy limitach wstrzymuje zapytania”); o ponowieniu decyduje silnik;
- błędy: przekroczenie czasu → `BrokerTimeout`, błąd połączenia → `BrokerError`, 5xx i 429 → `BrokerError` ze
  statusem, 4xx z `errorCode` na POST/PUT/DELETE → `BrokerRejected(errorCode)`, inne 4xx (w tym każde 4xx na GET)
  → `BrokerError`; przekierowania nie są wykonywane (nagłówki z tokenami nie mogą trafić pod inny adres);
- komunikaty wyjątków zawierają wyłącznie metodę, ścieżkę bez zapytania, status HTTP i `errorCode` brokera (lub
  nazwę pola przy złym kształcie odpowiedzi), nigdy danych logowania, tokenów, nagłówków ani treści żądań;
  dodatkowo każdy komunikat jest czyszczony ze znanych sekretów, a wyjątki adaptera nie mają łańcucha
  (`__cause__`/`__context__`) z obcymi obiektami (menedżer zleceń zapisuje tekst wyjątku w dzienniku);
- JSON wyłącznie z liczbami `Decimal` (`capitalcom_parse.loads`), czas wyłącznie z pól UTC.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import stat
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from gwstock.broker.base import Capabilities
from gwstock.broker.capitalcom_parse import (
    Bar,
    ParseError,
    build,
    dumps,
    loads,
    parse_accounts,
    parse_bars,
    parse_instrument_rules,
    parse_login,
    parse_session_account,
    parse_snapshot,
)
from gwstock.clock import Clock
from gwstock.types import (
    AccountInfo,
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    Candle,
    ClosedDeal,
    EntryAck,
    EntryRequest,
    InstrumentRules,
    OpenPosition,
    Quote,
    SessionExpired,
    WorkingOrder,
)

LIVE_BASE_URL = "https://api-capital.backend-capital.com"
API_PREFIX = "/api/v1"

DEFAULT_REQUEST_TIMEOUT_S = 10.0
"""Całkowity czas jednego żądania (połączenie, wysyłka, odczyt odpowiedzi)."""
GENERAL_MIN_INTERVAL_S = 0.2
"""Odstęp między dowolnymi dwoma żądaniami: 5/s, połowa limitu brokera 10/s (notatki §1.2, spec §13)."""
ORDER_MIN_INTERVAL_S = 0.1
"""Odstęp między zleceniami (`POST /positions`, `POST /workingorders`): limit brokera 1 na 0,1 s (notatki §1.2)."""
LOGIN_MIN_INTERVAL_S = 1.0
"""Odstęp między `POST /session`: limit brokera 1/s na klucz API (notatki §1.2)."""
RATE_LIMIT_PAUSE_S = 1.0
"""Po HTTP 429 żadne żądanie nie wychodzi przez tyle sekund (kod i `Retry-After` poza logowaniem: C20)."""
MAX_PRICE_POINTS = 1000
"""Górny limit parametru `max` w `GET /prices` (notatki §2.18); większy zakres to błąd wywołującego."""

CANDLE_STAMP_IS_PERIOD_START = True
"""Czy `snapshotTimeUTC` świecy to POCZĄTEK jej okresu (True) czy koniec (False). NIEPOTWIERDZONE (C9).

Dokumentacja tego nie rozstrzyga; True wynika z jednego przykładu (pierwsza świeca ma czas równy `from`, notatki
§2.18.1). Pomyłka wybrałaby świecę sprzed otwarcia zamiast pierwszej, dlatego to bramka L0 właściciela: w `dry_run`
silnik zapisuje początek UTC i OHLC wybranej świecy, a właściciel porównuje je z wykresem 5-minutowym DE40 w aplikacji
Capital.com dla 09:00 czasu Berlina. Stała jest czytana wyłącznie w `_stamp_shift` (jedno miejsce): False oznacza
start = znacznik − okres świecy (5 minut albo 1 dzień), a okno zapytania przesuwa się o ten sam okres.
"""

SECRET_KEYS = ("api_key", "identifier", "api_password")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PART_TWO = "Task 13 part 2"
_EPIC = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
_FIVE_MINUTES = timedelta(minutes=5)
_ONE_DAY = timedelta(days=1)
_SECRET_MIN_LEN = 4  # krótszych wartości nie da się sensownie wyciąć z komunikatu (zniszczyłoby tekst)
_MAX_ERROR_CODE_LEN = 200


def api_root(base_url: str) -> str:
    """Korzeń REST (`<base>/api/v1`) dla dozwolonego adresu; każdy inny adres to `ValueError`.

    Dozwolone: dokładnie `LIVE_BASE_URL` albo http(s) na 127.0.0.1, localhost lub ::1 (serwer testowy), bez
    danych użytkownika, ścieżki, zapytania i fragmentu. Komunikat nie powtarza adresu (mógłby nieść hasło).
    """
    if not isinstance(base_url, str):
        raise TypeError("base_url: expected a string")
    if base_url == LIVE_BASE_URL:
        return LIVE_BASE_URL + API_PREFIX
    allowed = False
    try:
        parts = urlsplit(base_url)
        parts.port  # noqa: B018 - waliduje port (poza zakresem → ValueError)
        allowed = (
            parts.scheme in ("http", "https")
            and parts.hostname in _LOOPBACK_HOSTS
            and "@" not in parts.netloc
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
            and "#" not in base_url
            and "?" not in base_url
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise ValueError("base_url: only LIVE_BASE_URL or a loopback test server (127.0.0.1, localhost, ::1) is allowed")
    return base_url.rstrip("/") + API_PREFIX


# --- dane logowania ------------------------------------------------------------------------


def _check_secret(name: str, value: object) -> None:
    """Komunikaty podają tylko nazwę pola i rodzaj błędu, nigdy wartość."""
    if not isinstance(value, str):
        raise TypeError(f"Credentials.{name}: expected a string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"Credentials.{name}: must not be empty")
    if value != value.strip():
        raise ValueError(f"Credentials.{name}: has leading or trailing whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"Credentials.{name}: contains control characters")
    if name == "api_key" and not all(33 <= ord(ch) <= 126 for ch in value):
        # klucz idzie w nagłówku HTTP: znak spoza ASCII dałby UnicodeEncodeError z kluczem w `args`
        raise ValueError(f"Credentials.{name}: expected visible ASCII characters only")


@dataclass(frozen=True, repr=False)
class Credentials:
    """Klucz API, identyfikator i hasło klucza API oraz (opcjonalnie) rachunek związany.

    Sekrety nie trafiają do `repr`/`str`. `account_id` nie pochodzi z pliku sekretów: aplikacja podaje go z
    `data/account.json` przez `dataclasses.replace` (polecenie `bind-account` pracuje z `account_id=None`).
    """

    api_key: str = field(repr=False)
    identifier: str = field(repr=False)
    api_password: str = field(repr=False)
    account_id: str | None = None

    def __post_init__(self) -> None:
        for name in SECRET_KEYS:
            _check_secret(name, getattr(self, name))
        if self.account_id is not None:
            if not isinstance(self.account_id, str):
                raise TypeError(f"Credentials.account_id: expected a string or None, got {type(self.account_id).__name__}")
            if not self.account_id.strip():
                raise ValueError("Credentials.account_id: must not be empty")

    def __repr__(self) -> str:
        bound = "None" if self.account_id is None else "<set>"
        return f"Credentials(api_key=<redacted>, identifier=<redacted>, api_password=<redacted>, account_id={bound})"

    __str__ = __repr__


def load_credentials(path: Path, *, check_permissions: bool = True) -> Credentials:
    """Czyta plik sekretów: obiekt JSON z dokładnie kluczami `api_key`, `identifier`, `api_password` (napisy).

    Plik musi być zwykłym plikiem bez dostępu dla grupy i innych (chmod 600), chyba że `check_permissions=False`.
    Każdy problem to `ValueError` bez treści pliku i bez łańcucha wyjątków (np. `JSONDecodeError.doc` niesie cały
    dokument), a `account_id` jest zawsze None.
    """
    path = Path(path)
    raw, problem = _read_secret_file(path, check_permissions)
    if problem is None:
        data, problem = _decode_secret_json(raw)
    if problem is None:
        problem = _check_secret_structure(data)
    if problem is None:
        creds, problem = _build_credentials(data)
    if problem is not None:
        raise ValueError(f"secrets file {path}: {problem}")
    return creds


def _read_secret_file(path: Path, check_permissions: bool) -> tuple[str | None, str | None]:
    try:
        info = path.stat()
    except OSError as exc:
        return None, f"cannot access it ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    mode = stat.S_IMODE(info.st_mode)
    if check_permissions and mode & 0o077:
        return None, f"mode {mode:04o} lets group or others access it; expected owner-only access (chmod 600)"
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read it ({type(exc).__name__})"


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate key")
        result[key] = value
    return result


def _decode_secret_json(raw: str | None) -> tuple[object, str | None]:
    try:
        return json.loads(raw or "", object_pairs_hook=_unique_object), None
    except _DuplicateKey:
        return None, "a key appears more than once"
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON (line {exc.lineno}, column {exc.colno})"
    except (ValueError, RecursionError):
        return None, "not valid JSON"


def _check_secret_structure(data: object) -> str | None:
    expected = ", ".join(SECRET_KEYS)
    if not isinstance(data, dict):
        return f"expected a JSON object with the keys {expected}"
    missing = [key for key in SECRET_KEYS if key not in data]
    if missing:
        return f"missing keys: {', '.join(missing)}"
    extra = sum(1 for key in data if key not in SECRET_KEYS)
    if extra:
        return f"{extra} unexpected key(s); only {expected} are allowed (the bound account comes from data/account.json)"
    for key in SECRET_KEYS:
        if not isinstance(data[key], str):
            return f"key {key}: expected a string"
    return None


def _build_credentials(data: dict) -> tuple[Credentials | None, str | None]:
    try:
        return Credentials(**{key: data[key] for key in SECRET_KEYS}), None
    except (TypeError, ValueError) as exc:
        return None, str(exc)  # komunikaty Credentials nie zawierają wartości


# --- adapter ---


@dataclass(frozen=True)
class _Tokens:
    cst: str = field(repr=False)
    security: str = field(repr=False)


@dataclass(frozen=True)
class _Response:
    status: int
    cst: str | None = field(repr=False)
    security: str | None = field(repr=False)
    body: bytes = field(repr=False)


class _Spacer:
    """Minimalny odstęp między kolejnymi zgodami na zegarze silnika; czekający są obsługiwani w kolejności FIFO.

    Oczekiwanie nigdy nie przekracza `max_wait_s` (najdłuższej uprawnionej przerwy: odstępu albo pauzy po 429):
    zegar silnika to czas ścienny, a jego cofnięcie (krok NTP, wznowienie maszyny) wstrzymałoby inaczej wszystkie
    żądania, także ochronne, na cały czas cofnięcia.
    """

    def __init__(self, clock: Clock, interval_s: float, *, max_wait_s: float | None = None) -> None:
        self._clock = clock
        self._interval = timedelta(seconds=interval_s)
        self._max_wait_s = max(interval_s, max_wait_s or 0.0)
        self._next: datetime | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            # pętla: pauza (`pause_until`) podniesiona w trakcie czekania też musi zostać odczekana
            while self._next is not None:
                wait = (self._next - self._clock.now_utc()).total_seconds()
                if wait <= 0:
                    break
                if wait <= self._max_wait_s:
                    await self._clock.sleep(wait)
                    continue
                # zegar cofnął się: odczekaj najdłuższą uprawnioną przerwę; jeśli termin nadal jest nierealnie
                # daleko (nie pauza podniesiona w międzyczasie), licz od nowa od bieżącej chwili
                await self._clock.sleep(self._max_wait_s)
                if (self._next - self._clock.now_utc()).total_seconds() > self._max_wait_s:
                    break
            self._next = self._clock.now_utc() + self._interval

    def pause_until(self, moment: datetime) -> None:
        if self._next is None or moment > self._next:
            self._next = moment


def _valid_token(value: str | None) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 4096 and all(33 <= ord(ch) <= 126 for ch in value)


def _error_code(body: bytes) -> str | None:
    """`errorCode` z treści błędu `{"errorCode": "..."}`; tylko znaki drukowalne, przycięty."""
    try:
        payload = loads(body)
    except ParseError:
        return None
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    if not isinstance(code, str):
        return None
    code = "".join(ch for ch in code if ch.isprintable()).strip()[:_MAX_ERROR_CODE_LEN]
    return code or None


def _with_code(code: str | None) -> str:
    return f" {code}" if code else ""


def _check_epic(epic: object) -> str:
    """Epic trafia do ścieżki URL, więc tylko bezpieczne znaki (bez `/`, `?`, `#` i ścieżek `.`/`..`)."""
    if not isinstance(epic, str):
        raise TypeError(f"epic: expected a string, got {type(epic).__name__}")
    if not _EPIC.fullmatch(epic):
        raise ValueError(f"epic: expected letters, digits, '.', '_' or '-' (e.g. 'DE40'), got {epic!r:.40}")
    return epic


def _utc_arg(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name}: expected a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _api_time(moment: datetime) -> str:
    """Parametr `from`/`to`: UTC bez strefy i bez ułamków (`YYYY-MM-DDTHH:MM:SS`); filtracja po `snapshotTimeUTC`."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _stamp_shift(period: timedelta) -> timedelta:
    """Jedyne miejsce, które czyta `CANDLE_STAMP_IS_PERIOD_START`: znacznik świecy = jej początek + przesunięcie."""
    return timedelta(0) if CANDLE_STAMP_IS_PERIOD_START else period


def _assemble_candles(
    epic: str, bars: list[Bar], period: timedelta, shift: timedelta, now: datetime, on_grid: bool
) -> list[Candle]:
    """Świece z odczytanych słupków: start = znacznik − przesunięcie, tylko zakończone, rosnąco, bez duplikatów.

    `on_grid`: znacznik musi leżeć na siatce okresu (5 min: pełne 5 minut, zero sekund); inaczej `ParseError`
    (np. znacznik `.999` końca okresu nie może przejść po cichu). Sprzeczne duplikaty → `ParseError`. Arytmetyka
    poza zakresem (`OverflowError`) przechodzi do `_parse`, który zamienia ją na `BrokerError`.
    """
    by_start: dict[datetime, Candle] = {}
    for i, bar in enumerate(bars):
        if on_grid and (bar.stamp - bar.stamp.replace(hour=0, minute=0, second=0, microsecond=0)) % period:
            raise ParseError(f"prices[{i}].snapshotTimeUTC: not on a {period.seconds // 60}-minute boundary")
        start = bar.stamp - shift
        candle = build(Candle, "prices", epic, start, bar.open, bar.high, bar.low, bar.close)
        if by_start.setdefault(start, candle) != candle:
            raise ParseError(f"prices[{i}]: conflicting candles for one period")
    return [by_start[s] for s in sorted(by_start) if s + period <= now]


class CapitalComBroker:
    """Adapter `Broker` dla Capital.com (część 1 z 2: sesja, rachunki, dane rynkowe; patrz docstring modułu)."""

    capabilities = Capabilities(
        stop_attach="atomic", idempotency_key=None, keepalive_s=600, reconcile="positions", is_cfd=True
    )

    def __init__(
        self,
        creds: Credentials,
        session: aiohttp.ClientSession,
        clock: Clock,
        base_url: str = LIVE_BASE_URL,
        *,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        if not isinstance(creds, Credentials):
            raise TypeError(f"creds: expected Credentials, got {type(creds).__name__}")
        self._api = api_root(base_url)
        if (
            isinstance(request_timeout_s, bool)
            or not isinstance(request_timeout_s, (int, float))
            or not 0 < request_timeout_s < math.inf
        ):
            raise ValueError("request_timeout_s: expected a positive finite number of seconds")
        self._creds = creds
        self._session = session
        self._clock = clock
        self._timeout_s = float(request_timeout_s)
        self._general = _Spacer(clock, GENERAL_MIN_INTERVAL_S, max_wait_s=RATE_LIMIT_PAUSE_S)
        self._orders = _Spacer(clock, ORDER_MIN_INTERVAL_S)
        self._logins = _Spacer(clock, LOGIN_MIN_INTERVAL_S)
        self._auth_lock = asyncio.Lock()
        # stan sesji: podmieniany wyłącznie pod `_auth_lock` (logowanie) albo przez `_adopt_tokens`/`_drop_session`
        self._tokens: _Tokens | None = None
        self._session_account_id: str | None = None
        self._login_accounts: dict[str, str | None] = {}  # rachunki z ostatniego logowania: id → waluta
        self._login_seq = 0  # liczba zakończonych prób logowania (udanych i nieudanych)
        self._last_login_error: str | None = None
        self._seen_tokens: deque[str] = deque(maxlen=64)  # do czyszczenia komunikatów także ze starych tokenów
        # świeżość notowań: epic → (ostatnio obserwowana krotka, chwila jej ostatniej zmiany na zegarze silnika)
        self._quote_marks: dict[str, tuple[tuple, datetime]] = {}

    # --- sesja ---------------------------------------------------------------------------------

    async def login(self) -> None:
        """Nowa sesja (`POST /session`) i, jeśli trzeba, jednorazowe przełączenie na rachunek związany.

        Równoległe wywołania (i ponowne logowanie po 401) dzielą jedną próbę. Porażka → `BrokerError`, a adapter
        zostaje bez sesji (każde żądanie → `SessionExpired` bez wysyłki), aż kolejne `login()` się uda.
        """
        seen = self._login_seq
        async with self._auth_lock:
            if self._login_seq == seen:
                await self._login_locked()
                return
            failure = None if self._tokens is not None else (self._last_login_error or "the session was dropped")
        if failure is not None:  # wspólna próba zawiodła albo jej sesję już porzucono: logowanie się nie udało
            raise self._error(BrokerError, f"login: no session after the concurrent login attempt ({failure})")

    async def keepalive(self) -> None:
        """`GET /ping` z tokenami sesji (sesja żyje 10 minut od ostatniego użycia; silnik woła co 5 minut)."""
        payload = await self._request("GET", "/ping")
        if payload.get("status") != "OK":
            raise self._error(BrokerError, "GET /ping: unexpected status in the response")

    async def _login_locked(self) -> None:
        try:
            tokens, account_id, accounts = await self._open_session()
        except BaseException as exc:  # także anulowanie w trakcie POST/PUT/GET: nie zostaje żadna sesja, ani stara
            self._drop_session()
            interrupted = f"login interrupted ({type(exc).__name__})"
            self._last_login_error = str(exc) if isinstance(exc, BrokerError) else interrupted
            self._login_seq += 1
            raise
        self._tokens = tokens
        self._session_account_id = account_id
        self._login_accounts = accounts
        self._last_login_error = None
        self._login_seq += 1

    async def _open_session(self) -> tuple[_Tokens, str, dict[str, str | None]]:
        where = "login: POST /session"
        await self._logins.acquire()
        body = {"identifier": self._creds.identifier, "password": self._creds.api_password, "encryptedPassword": False}
        resp = await self._send(where, "POST", "/session", {"X-CAP-API-KEY": self._creds.api_key}, body=body)
        if resp.status != 200:
            raise self._status_error(where, resp)
        tokens = self._tokens_from(resp)
        if tokens is None:
            raise self._error(BrokerError, f"{where}: response without valid CST/X-SECURITY-TOKEN headers")
        # treść jest JSON-em mimo `Content-Type: text/plain` w dokumentacji (notatki §1.3, C19b)
        info = self._parse(where, parse_login, self._decode(where, resp))
        bound = self._creds.account_id
        if bound is None or bound == info.current_account_id:
            return tokens, info.current_account_id, info.account_currencies
        if bound not in info.account_currencies:
            raise self._error(
                BrokerError,
                f"{where}: the bound account is not among the {len(info.account_currencies)} accounts of this login; "
                "nothing else was sent",
            )
        return await self._switch_after_login(tokens, bound), bound, info.account_currencies

    async def _switch_after_login(self, tokens: _Tokens, bound: str) -> _Tokens:
        """Jedyne dozwolone przełączenie rachunku: tuż po logowaniu, przed każdym innym wywołaniem (spec §13)."""
        where = "login: PUT /session"
        resp = await self._send(where, "PUT", "/session", self._auth_headers(tokens), body={"accountId": bound})
        if resp.status != 200:
            raise self._status_error(where, resp)
        self._decode(where, resp)
        tokens = self._tokens_from(resp, fallback=tokens)  # C18: czy tokeny zmieniają się po PUT, niepotwierdzone
        where = "login: GET /session"
        resp = await self._send(where, "GET", "/session", self._auth_headers(tokens))
        if resp.status != 200:
            raise self._status_error(where, resp)
        if self._parse(where, parse_session_account, self._decode(where, resp)) != bound:
            raise self._error(BrokerError, f"{where}: the session still reports another account after the switch")
        return tokens

    def _drop_session(self) -> None:
        self._tokens = None
        self._session_account_id = None
        self._login_accounts = {}

    async def _relogin_after_401(self, where: str, used: _Tokens) -> None:
        """Jedno wspólne ponowne logowanie: tylko jeśli nikt nie podmienił tokenów, którymi wysłano żądanie."""
        failure: str | None = None
        async with self._auth_lock:
            if self._tokens is used:
                try:
                    await self._login_locked()
                except BrokerError as exc:
                    failure = f"{where}: HTTP 401 and the re-login failed ({type(exc).__name__}: {exc})"
            elif self._tokens is None:
                failure = f"{where}: HTTP 401 and no session is left ({self._last_login_error or 'session dropped'})"
        if failure is not None:
            raise self._error(SessionExpired, failure)

    # --- rachunki ----------------------------------------------------------------------------------

    async def account(self) -> AccountInfo:
        """Aktywny rachunek sesji (z logowania albo po `select_account`), odczytany z `GET /accounts`."""
        accounts = self._parse("GET /accounts", parse_accounts, await self._request("GET", "/accounts"))
        active = self._session_account_id
        match = [a for a in accounts if a.account_id == active]
        if active is None or len(match) != 1:
            raise self._error(BrokerError, "GET /accounts: the session's active account is not listed")
        return match[0]

    async def list_accounts(self) -> list[AccountInfo]:
        """Wszystkie rachunki klienta (`GET /accounts`), w kolejności brokera."""
        return self._parse("GET /accounts", parse_accounts, await self._request("GET", "/accounts"))

    async def select_account(self, account_id: str) -> None:
        """Przełącza sesję na rachunek (`PUT /session` raz, kontrola `GET /session`); tylko dla `bind-account`.

        Rachunek spoza listy logowania → `BrokerError` bez wysyłki; rachunek już aktywny → nic nie wysyła (broker
        mógłby odrzucić przełączenie na ten sam rachunek). Przy związanym rachunku (`creds.account_id`) inny rachunek
        → `BrokerError` bez wysyłki (ponowne logowanie i tak przełączyłoby z powrotem). Każde wyjście po wysłaniu `PUT`
        inne niż potwierdzony sukces (także anulowanie) porzuca sesję, bo rachunek sesji jest wtedy niepewny
        (następne żądania → `SessionExpired`, aż do nowego logowania).
        """
        if not isinstance(account_id, str):
            raise TypeError(f"account_id: expected a string, got {type(account_id).__name__}")
        if self._creds.account_id is not None and account_id != self._creds.account_id:
            raise self._error(
                BrokerError,
                "select_account: refused, this adapter has a bound account (creds.account_id) and the next re-login "
                "would switch back to it; nothing was sent",
            )
        if self._tokens is None:
            raise self._error(SessionExpired, "select_account: not logged in (login required)")
        if account_id not in self._login_accounts:
            raise self._error(
                BrokerError,
                f"select_account: unknown account (not among the {len(self._login_accounts)} accounts of this login); "
                "nothing was sent",
            )
        if account_id == self._session_account_id:
            return
        verified = False
        try:
            await self._request("PUT", "/session", body={"accountId": account_id})
            active = self._parse("GET /session", parse_session_account, await self._request("GET", "/session"))
            if active != account_id:
                raise self._error(BrokerError, "select_account: GET /session reports another account after the switch")
            self._session_account_id = account_id
            verified = True
        finally:
            if not verified:  # każde inne wyjście po wysłaniu PUT (błąd, timeout, anulowanie): rachunek niepewny
                self._drop_session()

    # --- transport -------------------------------------------------------------------------------

    async def _acquire_order_slot(self) -> None:
        """Hak części 2: zlecenia (`POST /positions`, `POST /workingorders`) co najmniej co 0,1 s."""
        await self._orders.acquire()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        order: bool = False,
    ) -> dict[str, Any]:
        """Żądanie z tokenami sesji; zwraca obiekt JSON odpowiedzi 2xx albo rzuca wyjątek z `gwstock.types`.

        401 → jedno wspólne ponowne logowanie i jedna powtórka; drugie 401 → `SessionExpired` i porzucenie sesji.
        Nigdy więcej prób.
        """
        where = f"{method} {path}"
        resp, used = await self._send_authed(method, path, params, body, order)
        if resp.status == 401:
            await self._relogin_after_401(where, used)
            resp, used = await self._send_authed(method, path, params, body, order)
            if resp.status == 401:
                failure = f"{where}: HTTP 401 again right after a fresh login; session dropped"
                if self._tokens is used:
                    self._drop_session()
                    self._last_login_error = failure
                raise self._error(SessionExpired, failure)
        self._adopt_tokens(resp, used)
        if 200 <= resp.status < 300:
            return self._decode(where, resp)
        raise self._status_error(where, resp, rejectable=method != "GET")

    async def _send_authed(
        self, method: str, path: str, params: dict[str, str] | None, body: dict[str, Any] | None, order: bool
    ) -> tuple[_Response, _Tokens]:
        if order:
            await self._acquire_order_slot()
        await self._general.acquire()
        tokens = self._tokens  # odczyt PO odczekaniu limitu: w międzyczasie mogło się zakończyć logowanie
        if tokens is None:
            raise self._error(SessionExpired, f"{method} {path}: not logged in (login required)")
        return await self._transmit(f"{method} {path}", method, path, self._auth_headers(tokens), params, body), tokens

    async def _send(
        self, where: str, method: str, path: str, headers: dict[str, str], *, body: dict[str, Any] | None = None
    ) -> _Response:
        """Żądanie kroku logowania: bez ścieżki 401 → ponowne logowanie (blokada sesji jest już zajęta)."""
        await self._general.acquire()
        return await self._transmit(where, method, path, headers, None, body)

    async def _transmit(
        self,
        where: str,
        method: str,
        path: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        body: dict[str, Any] | None,
    ) -> _Response:
        """Jedno żądanie HTTP bez przekierowań, z całkowitym limitem czasu; wyjątki bez łańcucha i bez sekretów."""
        sent = {"Accept": "application/json", **headers}
        data = None
        if body is not None:
            data = dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        failure: tuple[type[BrokerError], str]
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._session.request(
                    method,
                    self._api + path,
                    params=params,
                    data=data,
                    headers=sent,
                    allow_redirects=False,
                    raise_for_status=False,
                ) as resp:
                    raw = await resp.read()
                    return _Response(resp.status, resp.headers.get("CST"), resp.headers.get("X-SECURITY-TOKEN"), raw)
        except TimeoutError:  # przed ClientError: limity czasu aiohttp dziedziczą po obu
            failure = (BrokerTimeout, f"{where}: no response within {self._timeout_s:g} s")
        except aiohttp.ClientError as exc:
            failure = (BrokerError, f"{where}: connection failed ({type(exc).__name__})")
        raise self._error(*failure)

    def _auth_headers(self, tokens: _Tokens) -> dict[str, str]:
        return {"CST": tokens.cst, "X-SECURITY-TOKEN": tokens.security}

    def _tokens_from(self, resp: _Response, fallback: _Tokens | None = None) -> _Tokens | None:
        cst = resp.cst if _valid_token(resp.cst) else None
        security = resp.security if _valid_token(resp.security) else None
        if fallback is None:
            tokens = _Tokens(cst, security) if cst and security else None
        else:
            tokens = _Tokens(cst or fallback.cst, security or fallback.security)
        if tokens is not None:
            self._seen_tokens.extend((tokens.cst, tokens.security))
        return tokens

    def _adopt_tokens(self, resp: _Response, used: _Tokens) -> None:
        """Tokeny z nagłówków odpowiedzi (notatki §1.1); tylko gdy sesja nie zmieniła się w trakcie żądania."""
        if self._tokens is not used or (resp.cst is None and resp.security is None):
            return
        fresh = self._tokens_from(resp, fallback=used)
        if fresh != used:
            self._tokens = fresh

    def _decode(self, where: str, resp: _Response) -> dict[str, Any]:
        try:
            payload = loads(resp.body)
        except ParseError as exc:
            problem = str(exc)
        else:
            if isinstance(payload, dict):
                return payload
            problem = "expected a JSON object"
        raise self._error(BrokerError, f"{where}: malformed response ({problem}; HTTP {resp.status})")

    def _parse(self, where: str, parser, *args: Any) -> Any:
        try:
            return parser(*args)
        except ParseError as exc:
            problem = str(exc)
        except ArithmeticError as exc:  # np. saldo 9E+999999 (decimal.Overflow) albo data poza zakresem
            problem = f"a number or time is out of range ({type(exc).__name__})"
        raise self._error(BrokerError, f"{where}: unexpected response: {problem}")

    def _status_error(self, where: str, resp: _Response, *, rejectable: bool = False) -> BrokerError:
        code = _error_code(resp.body)
        if resp.status == 429:
            self._general.pause_until(self._clock.now_utc() + timedelta(seconds=RATE_LIMIT_PAUSE_S))
            return self._error(
                BrokerError,
                f"{where}: HTTP 429{_with_code(code)} (rate limited; not retried, requests paused "
                f"{RATE_LIMIT_PAUSE_S:g} s)",
            )
        if rejectable and 400 <= resp.status < 500 and code:
            return self._error(BrokerRejected, code)
        return self._error(BrokerError, f"{where}: HTTP {resp.status}{_with_code(code)}")

    def _error(self, cls: type[BrokerError], message: str) -> BrokerError:
        """Jedyna droga tworzenia wyjątków brokera: komunikat bez znanych sekretów."""
        secrets = [self._creds.api_key, self._creds.identifier, self._creds.api_password, *self._seen_tokens]
        for secret in sorted(set(secrets), key=len, reverse=True):
            if len(secret) >= _SECRET_MIN_LEN:
                message = message.replace(secret, "<redacted>")
        return cls(message)

    # --- część 2 (Zadanie 13b) --------------------------------------------------------------------

    async def open_position(self, req: EntryRequest) -> EntryAck:
        raise NotImplementedError(_PART_TWO)

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        raise NotImplementedError(_PART_TWO)

    async def list_positions(self) -> list[OpenPosition]:
        raise NotImplementedError(_PART_TWO)

    async def close_position(self, deal_id: str) -> None:
        raise NotImplementedError(_PART_TWO)

    async def set_stop(self, deal_id: str, stop_level) -> None:
        raise NotImplementedError(_PART_TWO)

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        raise NotImplementedError(_PART_TWO)

    async def list_working_orders(self) -> list[WorkingOrder]:
        raise NotImplementedError(_PART_TWO)

    # --- dane rynkowe -------------------------------------------------------------------------------

    async def instrument_rules(self, epic: str) -> InstrumentRules:
        """Reguły z `GET /markets/{epic}` (bez pamięci podręcznej: minimum stopu zależy od bieżącej ceny).

        Jednostki inne niż udokumentowane, brak pola albo waluta instrumentu różna od waluty rachunku sesji →
        `BrokerError` (adapter niczego nie zgaduje). Szczegóły mapowania: `capitalcom_parse.parse_instrument_rules`.
        """
        path = f"/markets/{_check_epic(epic)}"
        payload = await self._request("GET", path)
        currency = self._login_accounts.get(self._session_account_id) if self._session_account_id else None
        return self._parse(f"GET {path}", parse_instrument_rules, epic, payload, currency)

    async def quote(self, epic: str) -> Quote:
        """Notowanie z `GET /markets/{epic}` (`snapshot.bid`, `snapshot.offer`).

        `ts` = chwila zegara silnika, w której obserwowana krotka (bid, offer, napis `updateTime`, napis
        `updateTimeUTC` jeśli jest) ostatnio się ZMIENIŁA; pierwszy odczyt epiku → teraz. Czas brokera nie jest
        parsowany (`updateTime` jest lokalny i bez strefy), więc zamrożona migawka starzeje się i uruchamia straż
        ciszy notowań. `marketStatus` nie jest częścią interfejsu v1, więc stan inny niż TRADEABLE nie jest błędem.
        """
        path = f"/markets/{_check_epic(epic)}"
        where = f"GET {path}"
        snapshot = self._parse(where, parse_snapshot, await self._request("GET", path))
        now = self._clock.now_utc()
        previous = self._quote_marks.get(epic)
        changed_at = previous[1] if previous is not None and previous[0] == snapshot.marker else now
        quote = self._parse(where, build, Quote, "snapshot", epic, snapshot.bid, snapshot.offer, changed_at)
        self._quote_marks[epic] = (snapshot.marker, changed_at)  # dopiero po udanej budowie notowania
        return quote

    async def candles_5m(self, epic: str, start: datetime, end: datetime) -> list[Candle]:
        """Zakończone świece 5-minutowe z `start <= c.start < end`, rosnąco (`GET /prices?resolution=MINUTE_5`).

        Szukane znaczniki to półotwarte [start + przesunięcie, end + przesunięcie), więc `from` = start + przesunięcie
        i `to` = end + przesunięcie wystarczają bez względu na to, czy broker traktuje `to` włącznie (C9). `to` jest
        dodatkowo obcięte do teraz (pełne sekundy): pytanie o przyszłość mogłoby zostać odrzucone, a zakończona świeca
        ma znacznik ≤ teraz − 5 min (≤ teraz przy znaczniku = koniec okresu; przy wyłącznym `to` świeca o znaczniku
        równym dokładnie teraz przyjdzie w następnym odczycie). Okno puste po obcięciu → [] bez zapytania. Wynik jest
        filtrowany lokalnie po `snapshotTimeUTC`; pusta lista jest poprawna (świec jeszcze nie ma).
        """
        path = f"/prices/{_check_epic(epic)}"
        start, end = _utc_arg("start", start), _utc_arg("end", end)
        if start >= end:
            return []
        shift = _stamp_shift(_FIVE_MINUTES)
        first = (start + shift).replace(microsecond=0)
        last = min(end + shift, self._clock.now_utc()).replace(microsecond=0)
        if last < first:
            return []
        points = (last - first) // _FIVE_MINUTES + 2
        if points > MAX_PRICE_POINTS:
            raise ValueError(f"candles_5m: the range needs {points} candles, more than {MAX_PRICE_POINTS} per request")
        params = {"resolution": "MINUTE_5", "from": _api_time(first), "to": _api_time(last), "max": str(points)}
        payload = await self._request("GET", path, params=params)
        candles = self._candles(path, epic, payload, _FIVE_MINUTES, on_grid=True)
        return [c for c in candles if start <= c.start < end]

    async def daily_candles(self, epic: str, count: int) -> list[Candle]:
        """Ostatnie `count` ZAKOŃCZONYCH świec dziennych, rosnąco (`GET /prices?resolution=DAY&max=count+2`).

        Dzień trwający (start + 1 dzień > teraz) jest odrzucany; mniej niż `count` zakończonych dni → `BrokerError`
        (ATR14 potrzebuje 15). Granica doby świecy DE40 i pomijanie weekendów: C9.
        """
        path = f"/prices/{_check_epic(epic)}"
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"daily_candles: count must be an int, got {type(count).__name__}")
        if not 1 <= count <= MAX_PRICE_POINTS - 2:
            raise ValueError(f"daily_candles: count must be between 1 and {MAX_PRICE_POINTS - 2}, got {count}")
        params = {"resolution": "DAY", "max": str(count + 2)}
        # granica doby świecy DE40 jest nieznana (C9), więc znaczników dziennych nie sprawdzamy względem siatki
        candles = self._candles(path, epic, await self._request("GET", path, params=params), _ONE_DAY, on_grid=False)
        if len(candles) < count:
            raise self._error(BrokerError, f"GET {path}: only {len(candles)} completed daily candles, {count} needed")
        return candles[-count:]

    def _candles(
        self, path: str, epic: str, payload: dict[str, Any], period: timedelta, *, on_grid: bool
    ) -> list[Candle]:
        """Świece zakończone (start + okres <= teraz), posortowane, bez duplikatów (patrz `_assemble_candles`)."""
        where = f"GET {path}"
        bars = self._parse(where, parse_bars, payload)
        now = self._clock.now_utc()
        return self._parse(where, _assemble_candles, epic, bars, period, _stamp_shift(period), now, on_grid)
