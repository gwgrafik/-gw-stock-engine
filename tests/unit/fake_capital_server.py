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
  open/close/high/low; filtracja `from`/`to` po `snapshotTimeUTC`, `max` domyślnie 10.

Sterowanie: `log` (metoda, ścieżka, zapytanie, nazwy obecnych nagłówków, treść; hasła i klucze nigdy nie są
zapisywane, treść `POST /session` jest zredagowana), `count(...)`, `expire_token()` (każde następne żądanie
z dotychczasowymi tokenami → 401), `fail_next(...)` (gotowa odpowiedź zamiast obsługi), `delay_next(...)`
(opóźnienie przed odpowiedzią, do wywołania przekroczenia czasu), `accounts`, `set_quote(...)`, `set_candles(...)`.

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
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    status: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Canned:
    status: int
    json: Any = None
    text: str | None = None
    headers: dict[str, str] | None = None


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
        self.login_min_interval = timedelta(seconds=1)
        self._sessions: dict[tuple[str, str], str] = {}
        self._last_login_at: datetime | None = None
        self._failures: dict[tuple[str | None, str], deque[Canned]] = {}
        self._delays: dict[tuple[str | None, str], deque[float]] = {}
        self._routes: list[tuple[str, str, Handler, bool]] = []
        self._seq = itertools.count(1)
        self._closing = asyncio.Event()
        self._server: TestServer | None = None

        self.route("POST", "/session", self._login, auth=False)
        self.route("GET", "/session", self._get_session)
        self.route("PUT", "/session", self._switch_account)
        self.route("DELETE", "/session", self._logout)
        self.route("GET", "/ping", self._ping)
        self.route("GET", "/accounts", self._accounts)
        self.route("GET", "/markets/{epic}", self._market)
        self.route("GET", "/prices/{epic}", self._prices)

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

    def delay_next(self, path: str, seconds: float, *, method: str | None = None) -> None:
        """Najbliższe żądanie na `path` czeka `seconds` (czas rzeczywisty) przed odpowiedzią."""
        self._delays.setdefault((method, path), deque()).append(seconds)

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
            body = await self._read_body(request)
            entry = LoggedRequest(
                method=request.method,
                path=path,
                query=dict(request.query),
                headers=frozenset(h for h in LOGGED_HEADERS if h in request.headers),
                body=self._redact(request.method, path, body),
                at=self.clock.now_utc(),
                auth_ok=self.session_account(request) is not None,
            )
            self.log.append(entry)
            delay = self._pop(self._delays, request.method, path)
            if delay is not None:
                try:
                    await asyncio.wait_for(self._closing.wait(), delay)
                except TimeoutError:
                    pass
            canned = self._pop(self._failures, request.method, path)
            if canned is not None:
                response: web.StreamResponse = self._canned(canned)
            elif auth and not entry.auth_ok:
                response = self.reply({"errorCode": "error.invalid.session.token"}, 401)
            else:
                response = await handler(request, body)
            entry.status = response.status
            if request.method == "POST" and path == "/session":
                entry.extra["credentials_ok"] = body_matches(body, self.identifier, self.password)
            return response

        return wrapped

    @staticmethod
    async def _read_body(request: web.Request) -> Any:
        if not request.can_read_body:
            return None
        raw = await request.read()
        try:
            return json.loads(raw, parse_float=Decimal)
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
        items = [
            c
            for c in self.prices.get((epic, resolution), [])
            if (start is None or c.get("snapshotTimeUTC", "") >= start)
            and (end is None or c.get("snapshotTimeUTC", "") <= end)
        ]
        items = items[:limit] if start is not None else items[-limit:]
        return self.reply({"prices": items, "instrumentType": "INDICES", "tickSize": Decimal("0.1"), "pipPosition": 0})


def body_matches(body: Any, identifier: str, password: str) -> bool:
    return isinstance(body, dict) and body.get("identifier") == identifier and body.get("password") == password
