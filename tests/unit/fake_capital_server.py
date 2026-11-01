"""Lokalny serwer testowy udający REST API Capital.com (`aiohttp.web` na 127.0.0.1, bez sieci zewnętrznej).

Kształty odpowiedzi według `docs/ops/capitalcom-api-notes.md` (§2.2–2.9, §2.16, §2.18) i odczytu live
`docs/ops/capitalcom-live-probe-2026-10-31.md`:
- `POST /session`: nagłówki `CST` i `X-SECURITY-TOKEN`, treść JSON z `Content-Type: text/plain` (jak w dokumentacji),
  `currentAccountId` = rachunek preferowany, `accounts[]` w kształcie z §2.4; limit 1 logowania na sekundę zegara
  testu (szybsze logowanie → 429 `error.too-many.requests`);
- `PUT /session`, `GET /session`, `DELETE /session`, `GET /ping`, `GET /accounts` (kształt §2.8);
- `GET /markets/{epic}`: `instrument` (lotSize, marginFactor, marginFactorUnit, openingHours w UTC), `dealingRules`
  z obiektami `{unit, value}`, `snapshot` z `updateTime` bez strefy i BEZ `updateTimeUTC` (jak w odczycie live);
- `GET /prices/{epic}`: świece z `snapshotTime` (czas lokalny CET) i `snapshotTimeUTC`, bid/ask osobno dla
  open/close/high/low; filtracja `from`/`to` po `snapshotTimeUTC`, `max` domyślnie 10; z `reject_future_to=True`
  (domyślnie) `to` późniejsze niż zegar testu → 400 `error.invalid.to` (zachowanie brokera nieznane, C9: serwer
  testowy zakłada wariant ostrożny, więc testy dowodzą, że adapter nigdy nie pyta o przyszłość).

Część handlowa (Zadanie 13, część 2), kształty wg notatek §2.10–§2.15, §2.19 i oficjalnej specyfikacji [S1]
(nota przy każdym endpoincie poniżej):
- `POST /positions`: tylko `{"dealReference": "o_<uuid>"}`; pozycja z `dealId` RÓŻNYM od identyfikatora zlecenia
  (jak w przykładach: pozycja = zlecenie + 2), wypełnienie BUY po `offer`, SELL po `bid`, stop = wypełnienie ∓
  `stopDistance`; walidacja wielkości i stopu wg reguł DE40; rynek zamknięty → 400 `MARKET_CLOSED_CODE`;
- `GET /confirms/{ref}`: migawka ACCEPTED (`dealId` zlecenia, `affectedDeals[0]` = pozycja `OPENED`) zostaje bez
  zmian także po zamknięciu pozycji; REJECTED z `reason`; nieznana referencja → 404 `error.not-found.dealReference`;
  pole `date` celowo w czasie LOKALNYM (UTC + 1 h), więc adapter, który by go użył, dostałby zły czas;
- `GET /positions`, `GET /positions/{dealId}`: kształt §2.11; klucze stopu domyślnie NIEOBECNE (jak w oficjalnych
  przykładach), `position_stop_keys` = "present" albo "null" je dodaje;
- `PUT /positions/{dealId}` (stop) i `DELETE /positions/{dealId}`: `{"dealReference": "p_<dealId>"}` (ta sama
  referencja dla obu operacji, jak w [S1]); gone deal → 404 `error.not-found.dealId` bez żadnego skutku;
- `GET /history/activity`: wiersze `{date, dateUTC, epic, dealId, source, type, status, details}` od najnowszych,
  `details` tylko z `detailed=true`, `from`/`to` najwyżej 1 dzień (inaczej 400 `error.invalid.from`), `to` w
  przyszłości → 400 `error.invalid.to` (wariant ostrożny), opcjonalny limit liczby wierszy (`activity_limit`);
- `GET /workingorders`: kształt `workingOrderData` + `marketData` z [S1] (zlecenia dodane przez `add_working_order`).

Sterowanie: `log` (metoda, ścieżka, zapytanie, nazwy obecnych nagłówków, treść; hasła i klucze nigdy nie są
zapisywane, treść `POST /session` jest zredagowana; `raw` = surowy tekst treści poza `/session`), `count(...)`,
`expire_token()` (każde następne żądanie z dotychczasowymi tokenami → 401), `fail_next(...)` (gotowa odpowiedź
zamiast obsługi), `fail_request(...)` (to samo dla n-tego kolejnego żądania), `delay_next(...)` (opóźnienie przed
odpowiedzią, do wywołania przekroczenia czasu), `apply_then_drop_response(...)` (skutek następuje, a klient nie
dostaje odpowiedzi), `reject_next_order(...)`, `set_market_closed(...)`, `trigger_stop(...)`, `add_activity(...)`,
`activity_limit(...)`, `add_working_order(...)`, `accounts`, `set_quote(...)`, `set_candles(...)`.

Rozszerzanie (Zadanie 13, część 2): nowy endpoint rejestruje się wywołaniem `self.route(method, path, handler)`
(ścieżka względem `/api/v1`, wzorce `aiohttp`, np. `"/positions/{deal_id}"`) w `__init__` podklasy albo tej klasy,
zawsze PRZED `start()`. Handler ma postać `async def h(request, body) -> web.Response` (`body` to sparsowany JSON
albo None) i zwraca `self.reply(payload, status)`. Log, `fail_next`, `delay_next` i kontrola tokenów (`auth=True`)
działają dla niego automatycznie; stan sesji: `self.session_account(request)`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import secrets
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from gwstock.clock import Clock

API = "/api/v1"
TEST_API_KEY = "test-api-key-7f3a9c41d2"
TEST_IDENTIFIER = "owner-login@example.test"
TEST_PASSWORD = "s3cr3t-api-pa55word"

# Nagłówki, których OBECNOŚĆ (nigdy wartość) trafia do logu żądań.
LOGGED_HEADERS = ("X-CAP-API-KEY", "CST", "X-SECURITY-TOKEN", "Content-Type")
RESOLUTIONS = frozenset({"MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR", "HOUR_4", "DAY", "WEEK"})
# Kod odmowy przy zamkniętym rynku: NIEUDOKUMENTOWANY (notatki §1.4, C14b). Serwer testowy używa napisu, który
# nazywa zamknięty rynek; adapter rozpoznaje każdy kod zawierający "market" i "closed".
MARKET_CLOSED_CODE = "error.market.closed"
LOCAL_OFFSET = timedelta(hours=1)  # pola lokalne (`date`, `createdDate`) w CET: celowo różne od UTC
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DEC_MARK = "\x00dec:"
_DEC_TOKEN = re.compile(r'"\\u0000dec:([^"\\]+)"')

Handler = Callable[[web.Request, Any], Awaitable[web.StreamResponse]]


def dumps(payload: Any) -> str:
    """JSON z liczbami `Decimal` zapisanymi dokładnie (jak liczby JSON brokera), bez pośrednictwa `float`."""

    def prep(value: Any) -> Any:
        if isinstance(value, Decimal):
            return f"{_DEC_MARK}{value}"
        if isinstance(value, dict):
            return {k: prep(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [prep(v) for v in value]
        if isinstance(value, float):
            raise TypeError("fake server payloads use Decimal, not float")
        return value

    return _DEC_TOKEN.sub(r"\1", json.dumps(prep(payload), ensure_ascii=False))


def stamp(moment: datetime) -> str:
    """Znacznik w formacie API: `YYYY-MM-DDTHH:MM:SS`, bez strefy."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def stamp_ms(moment: datetime) -> str:
    """Znacznik z milisekundami, jak w odpowiedziach pozycji i historii: `YYYY-MM-DDTHH:MM:SS.mmm`, bez strefy."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def parse_stamp(value: str) -> datetime:
    """Odwrotność `stamp`/`stamp_ms` (UTC)."""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def activity(
    deal_id: str,
    utc: datetime,
    *,
    source: str = "USER",
    direction: str = "SELL",
    level: str | Decimal = "25750.0",
    size: str | Decimal = "0.001",
    type: str = "POSITION",
    status: str = "ACCEPTED",
    epic: str = "DE40",
) -> dict[str, Any]:
    """Wiersz `GET /history/activity` z `details` (kształt z przykładu [S1] "Filter list by dealId", notatki §2.19)."""
    return {
        "date": stamp_ms(utc + LOCAL_OFFSET),
        "dateUTC": stamp_ms(utc),
        "epic": epic,
        "dealId": deal_id,
        "source": source,
        "type": type,
        "status": status,
        "details": {
            "dealReference": f"p_{deal_id}",
            "marketName": "Germany 40",
            "currency": "EUR",
            "size": Decimal(size),
            "direction": direction,
            "level": Decimal(level),
            "guaranteedStop": False,
        },
    }


def candle(
    utc: datetime,
    open: tuple[str, str] = ("25780.3", "25782.3"),
    high: tuple[str, str] = ("25800.1", "25801.4"),
    low: tuple[str, str] = ("25770.0", "25772.0"),
    close: tuple[str, str] = ("25790.5", "25792.5"),
    *,
    local_offset: timedelta = timedelta(hours=1),
    volume: int = 187,
) -> dict[str, Any]:
    """Świeca w kształcie `GET /prices` (pary bid/ask); `snapshotTime` = czas lokalny (CET: UTC + 1 h)."""

    def price(pair: tuple[str, str]) -> dict[str, Decimal]:
        return {"bid": Decimal(pair[0]), "ask": Decimal(pair[1])}

    return {
        "snapshotTime": stamp(utc + local_offset),
        "snapshotTimeUTC": stamp(utc),
        "openPrice": price(open),
        "closePrice": price(close),
        "highPrice": price(high),
        "lowPrice": price(low),
        "lastTradedVolume": volume,
    }


def de40_market() -> dict[str, Any]:
    """`GET /markets/DE40` z regułami zmierzonymi na koncie live (probe 2026-10-31, readout 2026-11-01)."""
    week = ["00:00 - 21:00", "21:05 - 00:00"]
    return {
        "instrument": {
            "epic": "DE40",
            "symbol": "DE40",
            "expiry": "-",
            "name": "Germany 40",
            "lotSize": Decimal("1"),
            "type": "INDICES",
            "guaranteedStopAllowed": True,
            "streamingPricesAvailable": True,
            "currency": "EUR",
            "marginFactor": Decimal("5"),
            "marginFactorUnit": "PERCENTAGE",
            "openingHours": {
                "mon": week,
                "tue": week,
                "wed": week,
                "thu": week,
                "fri": ["00:00 - 21:00"],
                "sat": [],
                "sun": ["22:00 - 00:00"],
                "zone": "UTC",
            },
            "country": "DE",
            "overnightFee": {
                "longRate": Decimal("-0.0123"),
                "shortRate": Decimal("-0.0042"),
                "swapChargeTimestamp": 1793649600000,
                "swapChargeInterval": 1440,
            },
        },
        "dealingRules": {
            "minStepDistance": {"unit": "POINTS", "value": Decimal("0.1")},
            "minDealSize": {"unit": "POINTS", "value": Decimal("0.001")},
            "maxDealSize": {"unit": "POINTS", "value": Decimal("1000")},
            "minSizeIncrement": {"unit": "POINTS", "value": Decimal("0.001")},
            "minGuaranteedStopDistance": {"unit": "PERCENTAGE", "value": Decimal("0.1")},
            "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": Decimal("0.01")},
            "maxStopOrProfitDistance": {"unit": "PERCENTAGE", "value": Decimal("100")},
            "marketOrderPreference": "AVAILABLE_DEFAULT_ON",
            "trailingStopsPreference": "NOT_AVAILABLE",
        },
        "snapshot": {
            "marketStatus": "TRADEABLE",
            "netChange": Decimal("12.3"),
            "percentageChange": Decimal("0.05"),
            "updateTime": "2026-11-02T08:29:59.512",
            "delayTime": 0,
            "bid": Decimal("25780.3"),
            "offer": Decimal("25782.3"),
            "high": Decimal("25810.0"),
            "low": Decimal("25700.1"),
            "decimalPlacesFactor": 1,
            "scalingFactor": 1,
            "marketModes": ["REGULAR"],
        },
    }


def default_accounts() -> list[dict[str, Any]]:
    """Dwa rachunki CFD w EUR, oba ENABLED; preferowany jest ten z saldem (jak w odczycie live)."""

    def account(account_id: str, preferred: bool, balance: str, available: str) -> dict[str, Any]:
        return {
            "accountId": account_id,
            "accountName": f"CFD {account_id}",
            "status": "ENABLED",
            "accountType": "CFD",
            "preferred": preferred,
            "balance": {
                "balance": Decimal(balance),
                "deposit": Decimal(balance),
                "profitLoss": Decimal("0.00"),
                "available": Decimal(available),
            },
            "currency": "EUR",
            "symbol": "€",
        }

    return [account("ACC-EMPTY", False, "0.00", "0.00"), account("ACC-FUNDED", True, "40.00", "38.71")]


@dataclass
class LoggedRequest:
    """Jedno żądanie, które dotarło do serwera. Nigdy nie zawiera wartości klucza, hasła ani tokenów."""

    method: str
    path: str  # względem /api/v1, bez zapytania
    query: dict[str, str]
    headers: frozenset[str]  # nazwy obecnych nagłówków z LOGGED_HEADERS
    body: Any
    at: datetime  # zegar testu w chwili przyjęcia
    auth_ok: bool  # czy para tokenów należała do żywej sesji
    account: str | None = None  # rachunek sesji tej pary tokenów w chwili przyjęcia (None: brak sesji)
    status: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: str | None = None  # surowy tekst treści (np. dokładny zapis liczb); nigdy dla `/session`


@dataclass(frozen=True)
class Canned:
    status: int
    json: Any = None
    text: str | None = None
    headers: dict[str, str] | None = None


@dataclass
class FakePosition:
    """Otwarta pozycja po stronie serwera (pola w jednostkach API)."""

    deal_id: str
    order_id: str  # `workingOrderId` pozycji = `dealId` potwierdzenia otwarcia (notatki §2.15, C2)
    account: str
    epic: str
    direction: str
    size: Decimal
    level: Decimal
    created: datetime
    stop_level: Decimal | None


class FakeCapitalServer:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.api_key = TEST_API_KEY
        self.identifier = TEST_IDENTIFIER
        self.password = TEST_PASSWORD
        self.accounts: list[dict[str, Any]] = default_accounts()
        self.markets: dict[str, dict[str, Any]] = {"DE40": de40_market()}
        self.prices: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.log: list[LoggedRequest] = []
        self.issued_tokens: list[str] = []
        self.rotate_tokens_on_switch = False
        self.reject_future_to = True
        self.login_min_interval = timedelta(seconds=1)
        self._sessions: dict[tuple[str, str], str] = {}
        self._last_login_at: datetime | None = None
        self._failures: dict[tuple[str | None, str], deque[Canned]] = {}
        self._delays: dict[tuple[str | None, str], deque[float]] = {}
        self._routes: list[tuple[str, str, Handler, bool]] = []
        self._seq = itertools.count(1)
        self._closing = asyncio.Event()
        self._server: TestServer | None = None
        # część handlowa
        self.positions: dict[str, FakePosition] = {}
        self.confirms: dict[str, dict[str, Any]] = {}
        self.activities: list[dict[str, Any]] = []  # kolejność zdarzeń; odpowiedź podaje je od najnowszych
        self.working_orders: list[tuple[str, dict[str, Any]]] = []  # (rachunek, element odpowiedzi)
        # klucze stopu w GET /positions: "absent" (jak w przykładach [S1]) | "present" | "null" | "when-set" (klucz
        # tylko przy pozycji, która ma stop: tak mogłoby wyglądać API, które pomija klucz przy braku stopu)
        self.position_stop_keys = "absent"
        self.confirm_stop_keys = False  # `stopLevel` w potwierdzeniach (pole znane tylko z [S3], notatki §2.15)
        self.hedging_mode = False
        self.market_closed = False
        self.max_activities: int | None = None
        self._deal_seq = itertools.count(0x80560040, 3)
        self._rejections: deque[str] = deque()
        self._drops: dict[tuple[str | None, str], deque[bool]] = {}
        self._nth_failures: list[list[Any]] = []  # [metoda | None, ścieżka, pozostało do pominięcia, Canned]

        self.route("POST", "/session", self._login, auth=False)
        self.route("GET", "/session", self._get_session)
        self.route("PUT", "/session", self._switch_account)
        self.route("DELETE", "/session", self._logout)
        self.route("GET", "/ping", self._ping)
        self.route("GET", "/accounts", self._accounts)
        self.route("GET", "/markets/{epic}", self._market)
        self.route("GET", "/prices/{epic}", self._prices)
        self.route("POST", "/positions", self._open_position)
        self.route("GET", "/positions", self._list_positions)
        self.route("GET", "/positions/{deal_id}", self._get_position)
        self.route("PUT", "/positions/{deal_id}", self._update_position)
        self.route("DELETE", "/positions/{deal_id}", self._close_position)
        self.route("GET", "/confirms/{ref}", self._confirm)
        self.route("GET", "/history/activity", self._activity)
        self.route("GET", "/workingorders", self._working_orders)

    # --- cykl życia ---------------------------------------------------------------

    def route(self, method: str, path: str, handler: Handler, *, auth: bool = True) -> None:
        if self._server is not None:
            raise RuntimeError("register routes before start()")
        self._routes.append((method, path, handler, auth))

    async def start(self) -> None:
        app = web.Application()
        for method, path, handler, auth in self._routes:
            app.router.add_route(method, API + path, self._wrap(handler, auth))
        self._server = TestServer(app, host="127.0.0.1", handler_cancellation=True)
        await self._server.start_server()

    async def close(self) -> None:
        self._closing.set()
        if self._server is not None:
            await self._server.close()

    @property
    def base_url(self) -> str:
        assert self._server is not None and self._server.port is not None
        return f"http://127.0.0.1:{self._server.port}"

    # --- sterowanie testowe ---------------------------------------------------------

    def fail_next(
        self,
        path: str,
        status: int = 500,
        json: Any = None,
        *,
        text: str | None = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
    ) -> None:
        """Najbliższe żądanie na `path` (metoda dowolna albo `method`) dostaje tę odpowiedź zamiast obsługi (FIFO)."""
        self._failures.setdefault((method, path), deque()).append(Canned(status, json, text, headers))

    def fail_request(
        self, path: str, nth: int, status: int = 500, json: Any = None, *, method: str | None = None
    ) -> None:
        """`nth`-te (od 1) kolejne żądanie na `path` dostaje tę odpowiedź zamiast obsługi; wcześniejsze przechodzą."""
        if nth < 1:
            raise ValueError("nth: expected >= 1")
        self._nth_failures.append([method, path, nth - 1, Canned(status, json)])

    def delay_next(self, path: str, seconds: float, *, method: str | None = None) -> None:
        """Najbliższe żądanie na `path` czeka `seconds` (czas rzeczywisty) przed odpowiedzią."""
        self._delays.setdefault((method, path), deque()).append(seconds)

    def apply_then_drop_response(self, path: str, *, method: str | None = None) -> None:
        """Najbliższe żądanie na `path` zostaje WYKONANE (skutek po stronie serwera), ale odpowiedź nie wychodzi:
        serwer czeka, aż klient zrezygnuje (przekroczenie czasu po stronie klienta)."""
        self._drops.setdefault((method, path), deque()).append(True)

    def reject_next_order(self, reason: str) -> None:
        """Najbliższe `POST /positions` dostaje referencję, nie tworzy pozycji, a potwierdzenie ma dealStatus
        REJECTED z tym `reason` (kształt pola `reason`: [S3], notatki §2.15, C4)."""
        self._rejections.append(reason)

    def set_market_closed(self, flag: bool) -> None:
        """Zamknięty rynek: `POST /positions` i `DELETE /positions/{dealId}` → 400 `MARKET_CLOSED_CODE`."""
        self.market_closed = flag

    def activity_limit(self, n: int | None) -> None:
        """Najwyżej `n` wierszy w jednej odpowiedzi `GET /history/activity` (nadmiar obcięty po cichu)."""
        self.max_activities = n

    def add_activity(self, row: dict[str, Any]) -> None:
        """Dopisuje gotowy wiersz historii (kształt jak `activity(...)`)."""
        self.activities.append(row)

    def trigger_stop(self, deal_id: str, level: str | Decimal, *, source: str = "SL") -> None:
        """Broker zamyka pozycję sam (stop: `source="SL"`; inne źródła np. `CLOSE_OUT`) po poziomie `level`."""
        pos = self.positions.pop(deal_id)
        self.add_activity(
            activity(
                deal_id,
                self.clock.now_utc(),
                source=source,
                direction=_opposite(pos.direction),
                level=Decimal(level),
                size=pos.size,
                epic=pos.epic,
            )
        )

    def add_working_order(
        self, epic: str, direction: str, size: Decimal, level: Decimal, *, account: str | None = None
    ) -> str:
        """Zlecenie oczekujące (LIMIT) rachunku `account` (domyślnie preferowanego); zwraca jego `dealId`."""
        order_id = self._new_deal_id()
        now = self.clock.now_utc()
        item = {
            "workingOrderData": {
                "dealId": order_id,
                "direction": direction,
                "epic": epic,
                "orderSize": size,
                "leverage": 20,
                "orderLevel": level,
                "timeInForce": "GOOD_TILL_CANCELLED",
                "createdDate": stamp_ms(now + LOCAL_OFFSET),
                "createdDateUTC": stamp_ms(now),
                "guaranteedStop": False,
                "orderType": "LIMIT",
                "trailingStop": False,
                "currencyCode": "EUR",
            },
            "marketData": self._market_summary(epic),
        }
        self.working_orders.append((account or self.preferred_account_id, item))
        return order_id

    def expire_token(self) -> None:
        """Wszystkie dotychczasowe sesje wygasają: następne żądanie z ich tokenami dostaje 401."""
        self._sessions.clear()

    def set_quote(
        self,
        epic: str,
        bid: str | None,
        offer: str | None,
        *,
        update_time: str | None = None,
        market_status: str | None = None,
    ) -> None:
        """Zmienia migawkę `GET /markets/{epic}`; None przy bid/offer usuwa pole z odpowiedzi."""
        snapshot = self.markets[epic]["snapshot"]
        for key, value in (("bid", bid), ("offer", offer)):
            if value is None:
                snapshot.pop(key, None)
            else:
                snapshot[key] = Decimal(value)
        if update_time is not None:
            snapshot["updateTime"] = update_time
        if market_status is not None:
            snapshot["marketStatus"] = market_status

    def set_candles(self, epic: str, resolution: str, candles: list[dict[str, Any]]) -> None:
        """Świece zwracane przez `GET /prices/{epic}?resolution=...` w podanej kolejności."""
        self.prices[(epic, resolution)] = list(candles)

    def requests(self, method: str | None = None, path: str | None = None) -> list[LoggedRequest]:
        return [r for r in self.log if (method is None or r.method == method) and (path is None or r.path == path)]

    def count(self, method: str, path: str) -> int:
        return len(self.requests(method, path))

    def counters(self) -> Counter[tuple[str, str]]:
        return Counter((r.method, r.path) for r in self.log)

    def sequence(self) -> list[str]:
        """Kolejność żądań jako napisy `"METODA /ścieżka"`."""
        return [f"{r.method} {r.path}" for r in self.log]

    @property
    def preferred_account_id(self) -> str:
        return next(a["accountId"] for a in self.accounts if a.get("preferred"))

    def session_account(self, request: web.Request) -> str | None:
        pair = (request.headers.get("CST", ""), request.headers.get("X-SECURITY-TOKEN", ""))
        return self._sessions.get(pair)

    def reply(self, payload: Any, status: int = 200, **kwargs: Any) -> web.Response:
        return web.Response(text=dumps(payload), status=status, content_type="application/json", **kwargs)

    # --- mechanika -------------------------------------------------------------------

    def _wrap(self, handler: Handler, auth: bool) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            path = request.path[len(API) :]
            raw_body = await request.read() if request.can_read_body else None
            body = self._parse_body(raw_body)
            entry = LoggedRequest(
                method=request.method,
                path=path,
                query=dict(request.query),
                headers=frozenset(h for h in LOGGED_HEADERS if h in request.headers),
                body=self._redact(request.method, path, body),
                at=self.clock.now_utc(),
                auth_ok=self.session_account(request) is not None,
                account=self.session_account(request),
                raw=None if path == "/session" or raw_body is None else raw_body.decode("utf-8", "replace"),
            )
            self.log.append(entry)
            delay = self._pop(self._delays, request.method, path)
            if delay is not None:
                try:
                    await asyncio.wait_for(self._closing.wait(), delay)
                except TimeoutError:
                    pass
            canned = self._pop(self._failures, request.method, path) or self._pop_nth(request.method, path)
            if canned is not None:
                response: web.StreamResponse = self._canned(canned)
            elif auth and not entry.auth_ok:
                response = self.reply({"errorCode": "error.invalid.session.token"}, 401)
            else:
                response = await handler(request, body)
                if self._pop(self._drops, request.method, path):
                    entry.extra["dropped"] = True
                    await asyncio.wait_for(self._closing.wait(), 30)  # skutek już nastąpił; odpowiedź nie wyjdzie
            entry.status = response.status
            if request.method == "POST" and path == "/session":
                entry.extra["credentials_ok"] = body_matches(body, self.identifier, self.password)
            return response

        return wrapped

    @staticmethod
    def _parse_body(raw: bytes | None) -> Any:
        if raw is None:
            return None
        try:
            return json.loads(raw, parse_float=Decimal, parse_int=Decimal)  # każda liczba JSON (także `30`)
        except ValueError:
            return {"<unparseable>": True}

    @staticmethod
    def _redact(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path == "/session" and isinstance(body, dict):
            return {k: ("<redacted>" if k in ("identifier", "password") else v) for k, v in body.items()}
        return body

    @staticmethod
    def _pop(table: dict[tuple[str | None, str], deque], method: str, path: str) -> Any:
        for key in ((method, path), (None, path)):
            queue = table.get(key)
            if queue:
                return queue.popleft()
        return None

    def _pop_nth(self, method: str, path: str) -> Canned | None:
        for item in list(self._nth_failures):
            if item[1] == path and item[0] in (None, method):
                if item[2] == 0:
                    self._nth_failures.remove(item)
                    return item[3]
                item[2] -= 1
        return None

    def _canned(self, canned: Canned) -> web.Response:
        if canned.text is not None:
            return web.Response(
                text=canned.text, status=canned.status, content_type="application/json", headers=canned.headers
            )
        return self.reply(canned.json, canned.status, headers=canned.headers)

    def _new_tokens(self) -> tuple[str, str]:
        n = next(self._seq)
        cst, xst = f"CST-{n:03d}-{secrets.token_hex(8)}", f"XST-{n:03d}-{secrets.token_hex(8)}"
        self.issued_tokens += [cst, xst]
        return cst, xst

    def _find_account(self, account_id: Any) -> dict[str, Any] | None:
        return next((a for a in self.accounts if a["accountId"] == account_id), None)

    # --- endpointy ---------------------------------------------------------------------

    async def _login(self, request: web.Request, body: Any) -> web.Response:
        key = request.headers.get("X-CAP-API-KEY")
        if key is None:
            return self.reply({"errorCode": "error.null.api.key"}, 400)
        now = self.clock.now_utc()
        too_fast = self._last_login_at is not None and now - self._last_login_at < self.login_min_interval
        self._last_login_at = now
        if too_fast:
            return self.reply({"errorCode": "error.too-many.requests"}, 429)
        if key != self.api_key or not body_matches(body, self.identifier, self.password):
            return self.reply({"errorCode": "error.invalid.details"}, 401)
        if body.get("encryptedPassword") is not False:
            return self.reply({"errorCode": "error.invalid.details"}, 401)
        account = self._find_account(self.preferred_account_id)
        assert account is not None
        cst, xst = self._new_tokens()
        self._sessions[(cst, xst)] = account["accountId"]
        payload = {
            "accountType": "CFD",
            "accountInfo": account["balance"],
            "currencyIsoCode": account["currency"],
            "currencySymbol": account["symbol"],
            "currentAccountId": account["accountId"],
            "streamingHost": "wss://streaming.invalid/",
            "accounts": [
                {
                    "accountId": a["accountId"],
                    "accountName": a["accountName"],
                    "preferred": a["preferred"],
                    "accountType": a["accountType"],
                    "currency": a["currency"],
                    "symbol": a["symbol"],
                    "balance": a["balance"],
                }
                for a in self.accounts
            ],
            "clientId": "CLIENT-1",
            "timezoneOffset": 1,
            "hasActiveDemoAccounts": False,
            "hasActiveLiveAccounts": True,
            "trailingStopsEnabled": False,
        }
        # dokumentacja opisuje treść logowania jako text/plain, mimo że to JSON (notatki §1.3)
        return web.Response(
            text=dumps(payload), content_type="text/plain", headers={"CST": cst, "X-SECURITY-TOKEN": xst}
        )

    async def _get_session(self, request: web.Request, body: Any) -> web.Response:
        account_id = self.session_account(request)
        account = self._find_account(account_id)
        return self.reply(
            {
                "clientId": "CLIENT-1",
                "accountId": account_id,
                "timezoneOffset": 1,
                "locale": "en",
                "currency": account["currency"] if account else "EUR",
                "streamEndpoint": "wss://streaming.invalid/",
            }
        )

    async def _switch_account(self, request: web.Request, body: Any) -> web.Response:
        target = body.get("accountId") if isinstance(body, dict) else None
        if self._find_account(target) is None:
            return self.reply({"errorCode": "error.invalid.accountId"}, 400)
        pair = (request.headers["CST"], request.headers["X-SECURITY-TOKEN"])
        headers = None
        if self.rotate_tokens_on_switch:
            del self._sessions[pair]
            pair = self._new_tokens()
            headers = {"CST": pair[0], "X-SECURITY-TOKEN": pair[1]}
        self._sessions[pair] = target
        payload = {
            "trailingStopsEnabled": False,
            "dealingEnabled": True,
            "hasActiveDemoAccounts": False,
            "hasActiveLiveAccounts": True,
        }
        return self.reply(payload, headers=headers)

    async def _logout(self, request: web.Request, body: Any) -> web.Response:
        self._sessions.pop((request.headers["CST"], request.headers["X-SECURITY-TOKEN"]), None)
        return self.reply({"status": "SUCCESS"})

    async def _ping(self, request: web.Request, body: Any) -> web.Response:
        return self.reply({"status": "OK"})

    async def _accounts(self, request: web.Request, body: Any) -> web.Response:
        return self.reply({"accounts": self.accounts})

    async def _market(self, request: web.Request, body: Any) -> web.Response:
        market = self.markets.get(request.match_info["epic"])
        if market is None:
            return self.reply({"errorCode": "error.not-found.epic"}, 404)
        return self.reply(market)

    async def _prices(self, request: web.Request, body: Any) -> web.Response:
        epic = request.match_info["epic"]
        if epic not in self.markets:
            return self.reply({"errorCode": "error.not-found.epic"}, 404)
        query = request.query
        resolution = query.get("resolution", "MINUTE")
        if resolution not in RESOLUTIONS:
            return self.reply({"errorCode": "error.invalid.resolution"}, 400)
        try:
            limit = int(query.get("max", "10"))
        except ValueError:
            return self.reply({"errorCode": "error.invalid.max"}, 400)
        if not 1 <= limit <= 1000:
            return self.reply({"errorCode": "error.invalid.max"}, 400)
        start, end = query.get("from"), query.get("to")
        for name, value in (("from", start), ("to", end)):
            if value is not None and not _STAMP.fullmatch(value):
                return self.reply({"errorCode": f"error.invalid.{name}"}, 400)
        if self.reject_future_to and end is not None:
            if datetime.strptime(end, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) > self.clock.now_utc():
                return self.reply({"errorCode": "error.invalid.to"}, 400)
        items = [
            c
            for c in self.prices.get((epic, resolution), [])
            if (start is None or c.get("snapshotTimeUTC", "") >= start)
            and (end is None or c.get("snapshotTimeUTC", "") <= end)
        ]
        items = items[:limit] if start is not None else items[-limit:]
        return self.reply({"prices": items, "instrumentType": "INDICES", "tickSize": Decimal("0.1"), "pipPosition": 0})

    # --- część handlowa ----------------------------------------------------------------

    def _new_deal_id(self) -> str:
        return self._deal_id_at(next(self._deal_seq))

    @staticmethod
    def _deal_id_at(n: int) -> str:
        return f"006011e7-0001-54c4-0000-{n:012x}"

    def _snapshot(self, epic: str) -> dict[str, Any]:
        return self.markets[epic]["snapshot"]

    def _market_summary(self, epic: str) -> dict[str, Any]:
        """Obiekt `market` / `marketData` z [S1] (§2.11): skrót instrumentu z bieżącym notowaniem."""
        market = self.markets[epic]
        snap = market["snapshot"]
        now = self.clock.now_utc()
        return {
            "instrumentName": market["instrument"]["name"],
            "expiry": "-",
            "marketStatus": "CLOSED" if self.market_closed else snap.get("marketStatus", "TRADEABLE"),
            "epic": epic,
            "symbol": market["instrument"]["symbol"],
            "instrumentType": market["instrument"]["type"],
            "lotSize": market["instrument"]["lotSize"],
            "high": snap["high"],
            "low": snap["low"],
            "percentageChange": snap["percentageChange"],
            "netChange": snap["netChange"],
            "bid": snap["bid"],
            "offer": snap["offer"],
            "updateTime": stamp_ms(now + LOCAL_OFFSET),
            "updateTimeUTC": stamp_ms(now),
            "delayTime": 0,
            "streamingPricesAvailable": True,
            "scalingFactor": 1,
            "marketModes": ["REGULAR"],
        }

    def _min_stop(self, epic: str) -> Decimal:
        """Minimalna odległość stopu w punktach: `minStopOrProfitDistance` (0,01%) × mid (notatki §2.16.1)."""
        snap = self._snapshot(epic)
        rule = self.markets[epic]["dealingRules"]["minStopOrProfitDistance"]
        return rule["value"] / 100 * (snap["bid"] + snap["offer"]) / 2

    def _position_item(self, pos: FakePosition) -> dict[str, Any]:
        """Element `GET /positions` (§2.11; [S1] "All positions"); klucze stopu wg `position_stop_keys`."""
        snap = self._snapshot(pos.epic)
        price = snap["bid"] if pos.direction == "BUY" else snap["offer"]
        upl = (price - pos.level) * pos.size * (1 if pos.direction == "BUY" else -1)
        position: dict[str, Any] = {
            "contractSize": 1,
            "createdDate": stamp_ms(pos.created + LOCAL_OFFSET),
            "createdDateUTC": stamp_ms(pos.created),
            "dealId": pos.deal_id,
            "dealReference": f"p_{pos.deal_id}",
            "workingOrderId": pos.order_id,
            "size": pos.size,
            "leverage": 20,
            "upl": upl,
            "direction": pos.direction,
            "level": pos.level,
            "currency": "EUR",
            "guaranteedStop": False,
        }
        if self.position_stop_keys == "present" or (self.position_stop_keys == "when-set" and pos.stop_level):
            position["trailingStop"] = False
            position["stopLevel"] = pos.stop_level
        elif self.position_stop_keys == "null":
            position["trailingStop"] = False
            position["stopLevel"] = None
        return {"position": position, "market": self._market_summary(pos.epic)}

    def _confirm_payload(self, ref: str, pos: FakePosition, *, status: str, deal_status: str = "ACCEPTED",
                         affected: str = "OPENED", direction: str | None = None,
                         level: Decimal | None = None) -> dict[str, Any]:  # fmt: skip
        """Potwierdzenie (§2.15; [S1] "Position/Order confirmation"); `date` celowo lokalne (C19)."""
        payload = {
            "date": stamp_ms(self.clock.now_utc() + LOCAL_OFFSET),
            "status": status,
            "dealStatus": deal_status,
            "epic": pos.epic,
            "dealReference": ref,
            "dealId": pos.order_id if affected == "OPENED" else pos.deal_id,
            "affectedDeals": [{"dealId": pos.deal_id, "status": affected}],
            "level": pos.level if level is None else level,
            "size": pos.size,
            "direction": direction or pos.direction,
            "guaranteedStop": False,
            "trailingStop": False,
        }
        if self.confirm_stop_keys and affected in ("OPENED", "AMENDED"):
            payload["stopLevel"] = pos.stop_level
        return payload

    def _own_position(self, request: web.Request) -> FakePosition | None:
        pos = self.positions.get(request.match_info["deal_id"])
        return pos if pos is not None and pos.account == self.session_account(request) else None

    async def _open_position(self, request: web.Request, body: Any) -> web.Response:
        """`POST /positions` (§2.10): odpowiedź WYŁĄCZNIE `{"dealReference": "o_..."}`."""
        if self.market_closed:
            return self.reply({"errorCode": MARKET_CLOSED_CODE}, 400)
        if not isinstance(body, dict):
            return self.reply({"errorCode": "error.invalid.request"}, 400)
        epic, direction, size = body.get("epic"), body.get("direction"), body.get("size")
        if epic not in self.markets:
            return self.reply({"errorCode": "error.not-found.epic"}, 404)
        if direction not in ("BUY", "SELL"):
            return self.reply({"errorCode": "error.invalid.direction"}, 400)
        rules = self.markets[epic]["dealingRules"]
        minimum, step = rules["minDealSize"]["value"], rules["minSizeIncrement"]["value"]
        # kody błędów wielkości są nieudokumentowane (C14): serwer testowy ma własne
        if not isinstance(size, Decimal) or size < minimum:
            return self.reply({"errorCode": f"error.invalid.size.minvalue: {minimum}"}, 400)
        if (size - minimum) % step:
            return self.reply({"errorCode": "error.invalid.size.increment"}, 400)
        distance = body.get("stopDistance")
        if distance is not None and (not isinstance(distance, Decimal) or distance < self._min_stop(epic)):
            return self.reply({"errorCode": f"error.invalid.stoploss.minvalue: {self._min_stop(epic)}"}, 400)
        account = self.session_account(request)
        if not self.hedging_mode and any(
            p.account == account and p.epic == epic and p.direction != direction for p in self.positions.values()
        ):  # bez hedgingu przeciwne wejście zmniejszałoby pozycję (C15): tego serwer testowy nie symuluje
            return self.reply({"errorCode": "error.fake.netting-not-simulated"}, 400)
        ref = f"o_{uuid.uuid4()}"
        snap = self._snapshot(epic)
        fill = snap["offer"] if direction == "BUY" else snap["bid"]
        stop = None if distance is None else (fill - distance if direction == "BUY" else fill + distance)
        n = next(self._deal_seq)  # pozycja = zlecenie + 2 (relacja z przykładów [S1], notatki §2.15)
        order_id, deal_id = self._deal_id_at(n), self._deal_id_at(n + 2)
        pos = FakePosition(deal_id, order_id, account, epic, direction, size, fill, self.clock.now_utc(), stop)
        if self._rejections:
            reason = self._rejections.popleft()
            payload = self._confirm_payload(ref, pos, status="REJECTED", deal_status="REJECTED")
            payload.update(affectedDeals=[], reason=reason)
            self.confirms[ref] = payload
            self.add_activity(activity(pos.deal_id, pos.created, direction=direction, level=fill, size=size,
                                       epic=epic, status="REJECTED"))  # fmt: skip
            return self.reply({"dealReference": ref})
        self.positions[pos.deal_id] = pos
        self.confirms[ref] = self._confirm_payload(ref, pos, status="OPEN")
        self.add_activity(activity(pos.deal_id, pos.created, direction=direction, level=fill, size=size, epic=epic))
        return self.reply({"dealReference": ref})

    async def _list_positions(self, request: web.Request, body: Any) -> web.Response:
        """`GET /positions` (§2.11): wszystkie pozycje AKTYWNEGO rachunku sesji."""
        account = self.session_account(request)
        items = [self._position_item(p) for p in self.positions.values() if p.account == account]
        return self.reply({"positions": items})

    async def _get_position(self, request: web.Request, body: Any) -> web.Response:
        """`GET /positions/{dealId}` (§2.12): ten sam kształt co element listy; zamknięta → 404."""
        pos = self._own_position(request)
        if pos is None:
            return self.reply({"errorCode": "error.not-found.dealId"}, 404)
        return self.reply(self._position_item(pos))

    async def _update_position(self, request: web.Request, body: Any) -> web.Response:
        """`PUT /positions/{dealId}` ze `stopLevel` (§2.13): `{"dealReference": "p_<dealId>"}`."""
        pos = self._own_position(request)
        if pos is None:
            return self.reply({"errorCode": "error.not-found.dealId"}, 404)
        level = body.get("stopLevel") if isinstance(body, dict) else None
        if not isinstance(level, Decimal):
            return self.reply({"errorCode": "error.invalid.stoploss"}, 400)
        snap, minimum = self._snapshot(pos.epic), self._min_stop(pos.epic)
        if pos.direction == "BUY" and level > snap["bid"] - minimum:
            return self.reply({"errorCode": f"error.invalid.stoploss.maxvalue: {snap['bid'] - minimum}"}, 400)
        if pos.direction == "SELL" and level < snap["offer"] + minimum:
            return self.reply({"errorCode": f"error.invalid.stoploss.minvalue: {snap['offer'] + minimum}"}, 400)
        pos.stop_level = level
        ref = f"p_{pos.deal_id}"
        self.confirms[ref] = self._confirm_payload(ref, pos, status="OPEN", affected="AMENDED")
        self.add_activity(activity(pos.deal_id, self.clock.now_utc(), direction=pos.direction, level=pos.level,
                                   size=pos.size, epic=pos.epic, type="EDIT_STOP_AND_LIMIT"))  # fmt: skip
        return self.reply({"dealReference": ref})

    async def _close_position(self, request: web.Request, body: Any) -> web.Response:
        """`DELETE /positions/{dealId}` (§2.14): całość pozycji po cenie rynkowej; zniknięta → 404 bez skutku."""
        if self.market_closed:
            return self.reply({"errorCode": MARKET_CLOSED_CODE}, 400)
        pos = self._own_position(request)
        if pos is None:
            return self.reply({"errorCode": "error.not-found.dealId"}, 404)
        snap = self._snapshot(pos.epic)
        exit_level = snap["bid"] if pos.direction == "BUY" else snap["offer"]
        del self.positions[pos.deal_id]
        ref = f"p_{pos.deal_id}"
        # kształt potwierdzenia zamknięcia jest nieopisany (C3): status i affectedDeals to założenie serwera testowego
        self.confirms[ref] = self._confirm_payload(
            ref, pos, status="CLOSED", affected="FULLY_CLOSED", direction=_opposite(pos.direction), level=exit_level
        )
        self.add_activity(activity(pos.deal_id, self.clock.now_utc(), direction=_opposite(pos.direction),
                                   level=exit_level, size=pos.size, epic=pos.epic))  # fmt: skip
        return self.reply({"dealReference": ref})

    async def _confirm(self, request: web.Request, body: Any) -> web.Response:
        """`GET /confirms/{dealReference}` (§2.15): migawka trwała, nieznana referencja → 404."""
        payload = self.confirms.get(request.match_info["ref"])
        if payload is None:
            return self.reply({"errorCode": "error.not-found.dealReference"}, 404)
        return self.reply(payload)

    async def _activity(self, request: web.Request, body: Any) -> web.Response:
        """`GET /history/activity` (§2.19): filtr po `dateUTC`, zakres najwyżej 1 dzień, od najnowszych."""
        query, now = request.query, self.clock.now_utc()
        bounds: dict[str, datetime] = {}
        for name in ("from", "to"):
            if name in query:
                if not _STAMP.fullmatch(query[name]):
                    return self.reply({"errorCode": f"error.invalid.{name}"}, 400)
                bounds[name] = parse_stamp(query[name])
        if bounds:
            start = bounds.get("from", bounds.get("to", now) - timedelta(days=1))
            end = bounds.get("to", start + timedelta(days=1))
            if end < start or end - start > timedelta(days=1):
                return self.reply({"errorCode": "error.invalid.from"}, 400)
            if "to" in bounds and end > now:
                return self.reply({"errorCode": "error.invalid.to"}, 400)
        else:
            try:
                period = int(query.get("lastPeriod", "600"))
            except ValueError:
                return self.reply({"errorCode": "error.invalid.lastPeriod"}, 400)
            if not 0 < period <= 86400:
                return self.reply({"errorCode": "error.invalid.lastPeriod"}, 400)
            start, end = now - timedelta(seconds=period), now
        deal_id = query.get("dealId")
        rows = [
            r for r in self.activities
            if start <= parse_stamp(r["dateUTC"]) <= end and (deal_id is None or r.get("dealId") == deal_id)
        ]  # fmt: skip
        rows.sort(key=lambda r: r["dateUTC"], reverse=True)
        if query.get("detailed") != "true":
            rows = [{k: v for k, v in r.items() if k != "details"} for r in rows]
        if self.max_activities is not None:
            rows = rows[: self.max_activities]
        return self.reply({"activities": rows})

    async def _working_orders(self, request: web.Request, body: Any) -> web.Response:
        """`GET /workingorders` ([S1] "All working orders"): zlecenia oczekujące AKTYWNEGO rachunku."""
        account = self.session_account(request)
        return self.reply({"workingOrders": [item for owner, item in self.working_orders if owner == account]})


def _opposite(direction: str) -> str:
    return "SELL" if direction == "BUY" else "BUY"


def body_matches(body: Any, identifier: str, password: str) -> bool:
    return isinstance(body, dict) and body.get("identifier") == identifier and body.get("password") == password
