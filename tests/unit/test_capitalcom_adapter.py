"""Testy adaptera Capital.com, część 1: dane logowania, host, sesja i ponowne logowanie, limity zapytań, mapowanie
błędów, rachunki, reguły instrumentu, notowanie i świece. Adapter rozmawia wyłącznie z lokalnym serwerem
`fake_capital_server` (127.0.0.1); prawdziwa sieć nie jest używana.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from gwstock.broker import capitalcom
from gwstock.broker.base import Broker, Capabilities
from gwstock.broker.capitalcom import LIVE_BASE_URL, CapitalComBroker, Credentials, api_root, load_credentials
from gwstock.broker.capitalcom_parse import ParseError, loads
from gwstock.types import (
    AccountInfo,
    BrokerAuthError,
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    Candle,
    InstrumentRules,
    Quote,
    SessionExpired,
)
from tests.unit.fake_capital_server import TEST_API_KEY, TEST_IDENTIFIER, TEST_PASSWORD, candle, stamp

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # = start fixture `clock`
SRC = Path(__file__).resolve().parents[2] / "src"
SECRETS = {
    "api_key": "test-key-ABCDEF123456",
    "identifier": "login-XYZ@example.test",
    "api_password": "test-pass-QWERTY987",
}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def assert_no_float(value, where: str = "value") -> None:
    """Rekurencyjnie: żadnego `float` w dataclassach, słownikach, listach i krotkach."""
    assert not isinstance(value, float), f"float at {where}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            assert_no_float(getattr(value, f.name), f"{where}.{f.name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_float(item, f"{where}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            assert_no_float(item, f"{where}[{i}]")


def assert_no_secret(exc: BaseException, secrets) -> None:
    for e in chain(exc):
        for text in (str(e), repr(e), *map(str, e.args)):
            for secret in secrets:
                assert secret not in text, f"secret leaked into {type(e).__name__}"


# --- host i powierzchnia klasy ---------------------------------------------------------


def test_live_base_url_is_the_documented_live_host():
    assert LIVE_BASE_URL == "https://api-capital.backend-capital.com"
    assert api_root(LIVE_BASE_URL) == "https://api-capital.backend-capital.com/api/v1"


def test_live_host_string_lives_only_in_the_adapter_module():
    hits = sorted(
        p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if "backend-capital.com" in p.read_text("utf-8")
    )
    assert hits == ["gwstock/broker/capitalcom.py"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://api-capital.backend-capital.com/",
        "https://demo-api-capital.backend-capital.com",
        "https://api-capital.backend-capital.com.example.com",
        "http://api-capital.backend-capital.com",
        "http://127.0.0.2:8080",
        "http://127.0.0.1.example.com:8080",
        "http://localhost.:8080",
        "http://user:hunter2@127.0.0.1:8080",
        "http://@127.0.0.1:8080",
        "http://127.0.0.1@example.com",
        "ftp://127.0.0.1:2121",
        "http://127.0.0.1:8080/api/v1",
        "http://127.0.0.1:8080?debug=1",
        "http://127.0.0.1:8080#x",
        "http://localhost:99999",
        "127.0.0.1:8080",
        "",
    ],
)
async def test_refuses_any_host_other_than_live_constant_or_loopback(url, capital_credentials, capital_http, clock):
    with pytest.raises(ValueError) as info:
        CapitalComBroker(capital_credentials, capital_http, clock, base_url=url)
    assert "hunter2" not in str(info.value)  # adres (np. z hasłem w userinfo) nie jest powtarzany w komunikacie
    assert info.value.__context__ is None


@pytest.mark.parametrize(
    ("url", "root"),
    [
        (LIVE_BASE_URL, LIVE_BASE_URL + "/api/v1"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/api/v1"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080/api/v1"),
        ("https://localhost:8443", "https://localhost:8443/api/v1"),
        ("http://[::1]:8080", "http://[::1]:8080/api/v1"),
    ],
)
async def test_accepts_live_constant_and_loopback_test_servers(url, root, capital_credentials, capital_http, clock):
    CapitalComBroker(capital_credentials, capital_http, clock, base_url=url)
    assert api_root(url) == root


async def test_constructor_validates_credentials_and_timeout(capital_credentials, capital_http, clock):
    with pytest.raises(TypeError):
        CapitalComBroker(dict(SECRETS), capital_http, clock, base_url="http://127.0.0.1:1")
    for bad in (0, -1, float("nan"), float("inf"), True, "10"):
        with pytest.raises((TypeError, ValueError)):
            CapitalComBroker(capital_credentials, capital_http, clock, base_url="http://127.0.0.1:1", request_timeout_s=bad)


async def test_capabilities_and_broker_protocol(capital_broker):
    assert capital_broker.capabilities == Capabilities("atomic", None, 600, "positions", True)
    assert isinstance(capital_broker, Broker)


# --- dane logowania ----------------------------------------------------------------------


def write_secrets(tmp_path: Path, content, mode: int = 0o600) -> Path:
    path = tmp_path / "capitalcom.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    path.chmod(mode)
    return path


def test_credentials_repr_and_str_hide_secrets():
    creds = Credentials(**SECRETS, account_id="ACC-FUNDED")
    rebound = dataclasses.replace(creds, account_id=None)
    for text in (repr(creds), str(creds), f"{creds}", repr([creds]), repr(rebound), str({"c": creds})):
        for secret in SECRETS.values():
            assert secret not in text
    assert (creds.api_key, creds.identifier, creds.api_password) == tuple(SECRETS.values())
    assert (creds.account_id, rebound.account_id) == ("ACC-FUNDED", None)
    assert rebound.api_key == SECRETS["api_key"]


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("api_key", "", ValueError),
        ("identifier", "", ValueError),
        ("api_password", "", ValueError),
        ("api_key", 1234567, TypeError),
        ("identifier", None, TypeError),
        ("api_password", b"bytes-secret", TypeError),
        ("api_key", " test-key-ABCDEF123456", ValueError),
        ("api_password", "test-pass-QWERTY987\n", ValueError),
        ("api_key", "key-ABC\r\nX-Injected: 1", ValueError),
        ("identifier", "login\x00XYZ", ValueError),
        ("api_key", "key-ÄBCDEF123456", ValueError),  # nagłówek HTTP: tylko widoczne ASCII
        ("api_key", "key ABCDEF123456", ValueError),
        ("account_id", "", ValueError),
        ("account_id", 42, TypeError),
    ],
)
def test_credentials_reject_invalid_fields_without_echoing_them(name, value, error):
    with pytest.raises(error) as info:
        Credentials(**{**SECRETS, name: value})
    message = str(info.value)
    assert name in message
    if isinstance(value, str) and value.strip():
        assert value.strip() not in message
    assert_no_secret(info.value, SECRETS.values())


def test_load_credentials_reads_exactly_the_three_keys(tmp_path):
    creds = load_credentials(write_secrets(tmp_path, SECRETS))
    assert (creds.api_key, creds.identifier, creds.api_password, creds.account_id) == (*SECRETS.values(), None)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_load_credentials_accepts_owner_only_modes(tmp_path, mode):
    assert load_credentials(write_secrets(tmp_path, SECRETS, mode)).identifier == SECRETS["identifier"]


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o601, 0o620, 0o602, 0o610])
def test_load_credentials_refuses_group_or_other_access(tmp_path, mode):
    path = write_secrets(tmp_path, SECRETS, mode)
    with pytest.raises(ValueError, match="chmod 600") as info:
        load_credentials(path)
    assert_no_secret(info.value, SECRETS.values())
    assert load_credentials(path, check_permissions=False).api_key == SECRETS["api_key"]


SECRET_NAME_KEY = "my-" + SECRETS["api_password"]


@pytest.mark.parametrize(
    "content",
    [
        {k: v for k, v in SECRETS.items() if k != "api_password"},
        {"api_key": SECRETS["api_key"]},
        {**SECRETS, "account_id": "ACC-FUNDED"},  # rachunek związany pochodzi z data/account.json, nie z sekretów
        {**SECRETS, SECRET_NAME_KEY: "x"},  # nazwa nadmiarowego klucza też nie jest powtarzana
        {**SECRETS, "api_key": ""},
        {**SECRETS, "identifier": 12345},
        {**SECRETS, "api_password": None},
        {**SECRETS, "api_key": [SECRETS["api_key"]]},
        {**SECRETS, "api_password": " " + SECRETS["api_password"]},
        [SECRETS["api_key"], SECRETS["identifier"], SECRETS["api_password"]],
        json.dumps(SECRETS["api_password"]),
        "not json " + SECRETS["api_password"],
        json.dumps(SECRETS)[:-5],
        '{"api_key": "%s", "api_key": "b", "identifier": "c", "api_password": "d"}' % SECRETS["api_key"],
        "",
    ],
)
def test_load_credentials_rejects_bad_content_without_echoing_it(tmp_path, content):
    with pytest.raises(ValueError) as info:
        load_credentials(write_secrets(tmp_path, content))
    assert_no_secret(info.value, [*SECRETS.values(), SECRET_NAME_KEY])
    assert info.value.__cause__ is None and info.value.__context__ is None  # np. JSONDecodeError.doc = cały plik


def test_load_credentials_rejects_missing_or_non_regular_files(tmp_path):
    for path in (tmp_path / "missing.json", tmp_path):
        with pytest.raises(ValueError) as info:
            load_credentials(path)
        assert info.value.__context__ is None


# --- sesja: logowanie, rachunek związany, ponowne logowanie ---------------------------------


@pytest.fixture
def server(fake_capital_server):
    return fake_capital_server


@pytest.fixture
def broker(capital_broker):
    return capital_broker


@pytest.fixture
def make_broker(fake_capital_server, capital_credentials, capital_http, clock):
    """Adapter na serwerze testowym z innymi danymi logowania, rachunkiem związanym, adresem lub timeoutem."""

    def make(*, account_id=None, creds=None, base_url=None, **kwargs):
        c = creds or capital_credentials
        if account_id is not None:
            c = dataclasses.replace(c, account_id=account_id)
        return CapitalComBroker(c, capital_http, clock, base_url=base_url or fake_capital_server.base_url, **kwargs)

    return make


def server_secrets(server) -> list[str]:
    return [TEST_API_KEY, TEST_IDENTIFIER, TEST_PASSWORD, *server.issued_tokens]


def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_login_sends_api_key_header_and_plain_password_body(server, broker):
    await broker.login()
    [req] = server.log
    assert (req.method, req.path, req.status) == ("POST", "/session", 200)
    assert {"X-CAP-API-KEY", "Content-Type"} <= req.headers
    assert not {"CST", "X-SECURITY-TOKEN"} & req.headers
    assert req.body == {"identifier": "<redacted>", "password": "<redacted>", "encryptedPassword": False}
    assert req.extra["credentials_ok"] is True


async def test_login_stores_cst_and_security_token_and_sends_them(server, broker):
    await broker.login()
    await broker.keepalive()
    await broker.list_accounts()
    assert server.sequence() == ["POST /session", "GET /ping", "GET /accounts"]
    for req in server.log[1:]:
        assert {"CST", "X-SECURITY-TOKEN"} <= req.headers
        assert "X-CAP-API-KEY" not in req.headers  # klucz API idzie wyłącznie do POST /session
        assert req.auth_ok and req.status == 200


async def test_login_selects_bound_account_once_when_session_account_differs(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")  # sesja startuje na preferowanym ACC-FUNDED
    await broker.login()
    await broker.keepalive()
    assert server.sequence() == ["POST /session", "PUT /session", "GET /session", "GET /ping"]
    assert server.requests("PUT", "/session")[0].body == {"accountId": "ACC-EMPTY"}
    assert (await broker.account()).account_id == "ACC-EMPTY"
    assert server.count("PUT", "/session") == 1


async def test_login_does_not_switch_when_bound_account_is_already_active(server, make_broker):
    broker = make_broker(account_id="ACC-FUNDED")
    await broker.login()
    await broker.keepalive()
    assert server.sequence() == ["POST /session", "GET /ping"]


async def test_login_without_bound_account_never_switches(server, broker):
    await broker.login()
    assert (await broker.account()).account_id == server.preferred_account_id == "ACC-FUNDED"
    assert server.count("PUT", "/session") == 0


async def test_login_fails_closed_when_bound_account_missing(server, make_broker):
    broker = make_broker(account_id="ACC-GONE")
    with pytest.raises(BrokerError) as info:
        await broker.login()
    assert type(info.value) is BrokerError and "ACC-GONE" not in str(info.value)
    assert server.sequence() == ["POST /session"]
    with pytest.raises(SessionExpired):
        await broker.keepalive()
    assert server.sequence() == ["POST /session"]  # żadnego dalszego wywołania na niewłaściwym rachunku


async def test_login_fails_closed_when_account_switch_is_refused(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")
    server.fail_next("/session", 400, {"errorCode": "error.invalid.accountId"}, method="PUT")
    with pytest.raises(BrokerError, match=r"PUT /session.*400 error\.invalid\.accountId"):
        await broker.login()
    with pytest.raises(SessionExpired):
        await broker.keepalive()
    assert server.sequence() == ["POST /session", "PUT /session"]


async def test_login_fails_closed_when_switch_is_not_confirmed_by_get_session(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")
    server.fail_next("/session", 200, {"clientId": "CLIENT-1", "accountId": "ACC-FUNDED"}, method="GET")
    with pytest.raises(BrokerError, match="GET /session"):
        await broker.login()
    with pytest.raises(SessionExpired):
        await broker.keepalive()
    assert server.sequence() == ["POST /session", "PUT /session", "GET /session"]


async def test_account_switch_adopts_tokens_rotated_by_put_session(server, make_broker):
    server.rotate_tokens_on_switch = True  # C18: czy PUT /session zmienia tokeny, jest niepotwierdzone
    broker = make_broker(account_id="ACC-EMPTY")
    await broker.login()
    await broker.keepalive()
    assert [r.auth_ok for r in server.log] == [False, True, True, True]
    assert server.log[-1].status == 200 and server.count("POST", "/session") == 1


async def test_refused_login_is_a_credential_error_not_session_expired(server, make_broker):
    broker = make_broker(creds=Credentials(TEST_API_KEY, TEST_IDENTIFIER, "wrong-password-123"))
    with pytest.raises(BrokerAuthError) as info:
        await broker.login()
    assert isinstance(info.value, BrokerError) and not isinstance(info.value, SessionExpired)
    assert str(info.value) == "login: POST /session: HTTP 401 error.invalid.details"


@pytest.mark.parametrize(
    ("status", "body", "credential"),
    [
        (401, {"errorCode": "error.invalid.details"}, True),
        (403, {"errorCode": "error.forbidden"}, True),
        (400, {"errorCode": "error.null.api.key"}, True),
        (400, {"errorCode": "error.invalid.something"}, False),
        (429, {"errorCode": "error.too-many.requests"}, False),
        (500, None, False),
        (503, {"errorCode": "error.service.unavailable"}, False),
    ],
    ids=["401", "403", "null-api-key", "other-400", "429", "500", "503"],
)
async def test_only_credential_rejections_of_post_session_are_broker_auth_errors(
    server, broker, status, body, credential
):
    # Silnik blokuje dalsze logowania wyłącznie po odrzuceniu danych logowania (ochrona klucza przed blokadą);
    # awaria transportu, przeciążenie i limit zapytań to zwykły BrokerError (ponawiany z odstępem, bez blokady).
    server.fail_next("/session", status, body, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.login()
    assert isinstance(info.value, BrokerAuthError) is credential
    assert not isinstance(info.value, SessionExpired)


@pytest.mark.parametrize(
    ("json_body", "text"),
    [(None, "<html><body>403 Forbidden</body></html>"), (None, ""), ({"message": "blocked"}, None)],
    ids=["html-page", "empty", "json-without-errorCode"],
)
async def test_a_403_without_a_broker_error_code_is_not_a_credential_rejection(server, broker, json_body, text):
    # strona brzegowa / WAF (403 bez errorCode Capital.com) to awaria przejściowa: bez blokady logowań po 3 razach
    server.fail_next("/session", 403, json_body, text=text, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.login()
    assert not isinstance(info.value, BrokerAuthError) and not isinstance(info.value, SessionExpired)


@pytest.mark.parametrize(
    "code",
    [
        "error.too-many.requests",
        "error.service.unavailable",
        "error.maintenance",
        "error.too-many.login.attempts",  # każdy kod z "too-many"
        "error.temporarily.unavailable",  # każdy kod z "unavailable"
        "ERROR.SERVICE.UNAVAILABLE",  # wielkość liter bez znaczenia
    ],
)
async def test_login_403_with_a_transient_error_code_is_not_a_credential_rejection(server, broker, code):
    # przeciążenie, limit albo przerwa techniczna podane jako 403 z errorCode: zwykły BrokerError (ponawiany z
    # odstępem), nie odrzucenie danych logowania, więc bez blokady logowań, która mogłaby wstrzymać wyjścia
    server.fail_next("/session", 403, {"errorCode": code}, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.login()
    assert type(info.value) is BrokerError
    assert str(info.value) == f"login: POST /session: HTTP 403 {code}"
    await broker.login()  # następna próba przechodzi normalnie
    assert server.count("POST", "/session") == 2


@pytest.mark.parametrize(
    ("canned", "match"),
    [
        ({"status": 200, "json": {"currentAccountId": "ACC-FUNDED", "accounts": []}}, "CST"),
        (
            {"status": 200, "json": {"accounts": [{"accountId": "ACC-FUNDED"}]}, "headers": {"CST": "c1", "X-SECURITY-TOKEN": "x1"}},
            "currentAccountId",
        ),
        ({"status": 200, "text": "<html>", "headers": {"CST": "c1", "X-SECURITY-TOKEN": "x1"}}, "malformed"),
    ],
)
async def test_login_response_without_tokens_or_account_is_refused(server, broker, canned, match):
    server.fail_next("/session", method="POST", **canned)
    with pytest.raises(BrokerError, match=match):
        await broker.login()
    with pytest.raises(SessionExpired):
        await broker.keepalive()


async def test_calls_before_login_raise_session_expired_without_network(server, broker):
    for call in (broker.keepalive(), broker.list_accounts(), broker.quote("DE40")):
        with pytest.raises(SessionExpired):
            await call
    assert server.log == []


async def test_logins_are_spaced_by_one_second(server, broker):
    await broker.login()
    await broker.login()
    await broker.keepalive()
    assert [(r.method, r.path, r.at, r.status) for r in server.log] == [
        ("POST", "/session", at(0), 200),
        ("POST", "/session", at(1.0), 200),  # limit brokera: 1 logowanie na sekundę
        ("GET", "/ping", at(1.2), 200),
    ]


async def test_concurrent_logins_share_one_post_session(server, broker):
    await asyncio.gather(broker.login(), broker.login(), broker.login())
    assert server.count("POST", "/session") == 1
    await broker.keepalive()


async def test_401_triggers_one_serialized_relogin_then_retry(server, broker):
    await broker.login()
    server.expire_token()
    await asyncio.gather(broker.keepalive(), broker.list_accounts(), broker.quote("DE40"))
    assert server.count("POST", "/session") == 2  # pierwsze logowanie + JEDNO wspólne ponowne
    for path in ("/ping", "/accounts", "/markets/DE40"):
        assert [r.status for r in server.requests("GET", path)] == [401, 200]


async def test_second_401_after_relogin_raises_session_expired(server, broker):
    await broker.login()
    for _ in range(2):
        server.fail_next("/ping", 401, {"errorCode": "error.invalid.session.token"})
    with pytest.raises(SessionExpired, match="401"):
        await broker.keepalive()
    assert server.sequence() == ["POST /session", "GET /ping", "POST /session", "GET /ping"]
    # sesja porzucona: dalsze wywołania nie wychodzą do brokera, aż silnik sam zaloguje się ponownie
    for call in (broker.keepalive, broker.list_accounts):
        with pytest.raises(SessionExpired, match="not logged in"):
            await call()
    assert len(server.log) == 4
    await broker.login()
    await broker.keepalive()
    assert server.log[-1].status == 200


@pytest.mark.parametrize("failure", ["status", "timeout", "refused"])
async def test_401_with_failing_relogin_raises_session_expired(server, make_broker, failure):
    broker = make_broker(request_timeout_s=0.05)
    await broker.login()
    server.expire_token()
    if failure == "status":
        server.fail_next("/session", 503, {"errorCode": "error.service.unavailable"}, method="POST")
    elif failure == "timeout":
        server.delay_next("/session", 2.0, method="POST")
    else:
        server.password = "test-rotated-by-owner"
    with pytest.raises(SessionExpired) as info:
        await broker.keepalive()
    assert "re-login failed" in str(info.value)
    assert server.sequence() == ["POST /session", "GET /ping", "POST /session"]
    with pytest.raises(SessionExpired):
        await broker.list_accounts()
    assert len(server.log) == 3  # bez sesji nic nie wychodzi do brokera, aż silnik zaloguje się ponownie


async def test_relogin_after_401_rebinds_the_bound_account(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")
    await broker.login()
    server.expire_token()
    await broker.keepalive()
    assert server.sequence() == [
        "POST /session", "PUT /session", "GET /session",
        "GET /ping", "POST /session", "PUT /session", "GET /session", "GET /ping",
    ]  # fmt: skip
    assert (await broker.account()).account_id == "ACC-EMPTY"


async def test_explicit_login_after_failed_relogin_recovers(server, broker):
    await broker.login()
    server.expire_token()
    server.fail_next("/session", 500, {"errorCode": "error.internal"}, method="POST")
    with pytest.raises(SessionExpired):
        await broker.keepalive()
    await broker.login()
    await broker.keepalive()
    assert server.log[-1].status == 200


async def test_keepalive_calls_ping_with_session_tokens(server, broker):
    await broker.login()
    await broker.keepalive()
    ping = server.requests("GET", "/ping")[0]
    assert {"CST", "X-SECURITY-TOKEN"} <= ping.headers and ping.auth_ok
    server.fail_next("/ping", 200, {"status": "DEGRADED"})
    with pytest.raises(BrokerError, match="GET /ping"):
        await broker.keepalive()


# --- limity zapytań (zegar silnika) -----------------------------------------------------------


async def test_requests_are_spaced_200ms_in_call_order(server, broker):
    await broker.login()
    await broker.keepalive()
    await broker.quote("DE40")
    await broker.list_accounts()
    assert [r.at for r in server.log] == [at(0), at(0.2), at(0.4), at(0.6)]


async def test_concurrent_requests_keep_fifo_spacing(broker, capital_sent):
    await broker.login()
    await asyncio.gather(broker.keepalive(), broker.quote("DE40"), broker.list_accounts())
    assert capital_sent == [
        ("POST", "/session", at(0)),
        ("GET", "/ping", at(0.2)),
        ("GET", "/markets/DE40", at(0.4)),
        ("GET", "/accounts", at(0.6)),
    ]


async def test_idle_time_counts_toward_spacing(server, broker, clock):
    await broker.login()
    clock.advance(5)
    await broker.keepalive()
    await broker.keepalive()
    assert [r.at for r in server.log] == [at(0), at(5), at(5.2)]


async def test_order_slots_are_spaced_100ms_independently_of_other_requests(server, make_broker, clock, monkeypatch):
    start = clock.now_utc()
    plain = make_broker()
    await plain._acquire_order_slot()
    await plain._acquire_order_slot()
    assert clock.now_utc() - start == timedelta(seconds=0.1)
    # z wyłączonym limitem ogólnym widać sam limit zleceń (część 2 woła `_request(..., order=True)`)
    monkeypatch.setattr(capitalcom, "GENERAL_MIN_INTERVAL_S", 0.0)
    broker = make_broker()
    await broker.login()
    await broker._request("GET", "/ping", order=True)
    await broker._request("GET", "/ping", order=True)
    await broker._request("GET", "/accounts")
    base = clock.now_utc() - timedelta(seconds=0.1)
    assert [r.at - base for r in server.log] == [timedelta(0), timedelta(0), timedelta(seconds=0.1), timedelta(seconds=0.1)]


async def test_429_is_not_retried_and_pauses_requests(server, broker):
    await broker.login()
    server.fail_next("/markets/DE40", 429, {"errorCode": "error.too-many.requests"})
    with pytest.raises(BrokerError) as info:
        await broker.quote("DE40")
    assert type(info.value) is BrokerError
    assert str(info.value).startswith("GET /markets/DE40: HTTP 429 error.too-many.requests")
    assert server.count("GET", "/markets/DE40") == 1  # bez automatycznej powtórki
    await broker.keepalive()
    assert server.log[-1].at == at(0.2 + capitalcom.RATE_LIMIT_PAUSE_S)


# --- mapowanie błędów ------------------------------------------------------------------------


async def test_timeout_maps_to_broker_timeout(server, make_broker):
    broker = make_broker(request_timeout_s=0.05)
    await broker.login()
    server.delay_next("/markets/DE40", 2.0)
    with pytest.raises(BrokerTimeout) as info:
        await broker.quote("DE40")
    assert str(info.value) == "GET /markets/DE40: no response within 0.05 s"


async def test_connection_error_maps_to_broker_error(make_broker):
    broker = make_broker(base_url=f"http://127.0.0.1:{unused_port()}")
    with pytest.raises(BrokerError) as info:
        await broker.login()
    assert type(info.value) is BrokerError
    assert str(info.value).startswith("login: POST /session: connection failed (")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_maps_to_broker_error_with_status(server, broker, status):
    await broker.login()
    server.fail_next("/accounts", status, text="<html>upstream error</html>")
    with pytest.raises(BrokerError) as info:
        await broker.list_accounts()
    assert type(info.value) is BrokerError and str(info.value) == f"GET /accounts: HTTP {status}"


async def test_4xx_on_get_is_broker_error_with_error_code(server, broker):
    await broker.login()
    server.markets.pop("DE40")
    with pytest.raises(BrokerError) as info:
        await broker.instrument_rules("DE40")
    assert type(info.value) is BrokerError  # nie BrokerRejected: odczyt niczego nie odrzuca
    assert str(info.value) == "GET /markets/DE40: HTTP 404 error.not-found.epic"


async def test_4xx_with_error_code_on_put_is_broker_rejected(server, broker):
    await broker.login()
    server.fail_next("/session", 400, {"errorCode": "error.invalid.accountId"}, method="PUT")
    with pytest.raises(BrokerRejected) as info:
        await broker.select_account("ACC-EMPTY")
    assert info.value.reason == "error.invalid.accountId"


async def test_redirect_is_never_followed(server, broker):
    await broker.login()
    server.fail_next("/accounts", 302, {}, headers={"Location": "/api/v1/ping"})
    with pytest.raises(BrokerError, match="HTTP 302"):
        await broker.list_accounts()
    assert server.count("GET", "/ping") == 0  # tokeny nie idą pod adres z przekierowania


@pytest.mark.parametrize(
    "text",
    ["not json", "", "NaN", '{"accounts": NaN}', '{"accounts": [], "accounts": []}', "[1, 2]", '"text"', "{"],
)
async def test_malformed_json_maps_to_broker_error(server, broker, text):
    await broker.login()
    server.fail_next("/accounts", 200, text=text)
    with pytest.raises(BrokerError, match="GET /accounts: malformed response"):
        await broker.list_accounts()


def test_json_numbers_are_decimal_never_float():
    data = loads(b'{"a": 1.5, "b": [2, 3.0e2, {"c": -0.001}], "d": true, "e": null, "f": "1.5"}')
    assert data == {
        "a": Decimal("1.5"),
        "b": [Decimal("2"), Decimal("3.0e2"), {"c": Decimal("-0.001")}],
        "d": True,
        "e": None,
        "f": "1.5",
    }
    assert_no_float(data)
    assert str(data["a"]) == "1.5" and str(data["b"][2]["c"]) == "-0.001"


@pytest.mark.parametrize("raw", [b"NaN", b'{"a": Infinity}', b'{"a": -Infinity}', b'{"a": 1, "a": 2}', b"{", b"", b"\xff"])
def test_json_loader_rejects_non_finite_duplicate_and_malformed(raw):
    with pytest.raises(ParseError):
        loads(raw)


# --- komunikaty wyjątków nigdy nie niosą sekretów (właściwość dla wielu trybów awarii) ----------


async def _scenario_timeout(server, make_broker):
    b = make_broker(request_timeout_s=0.05)
    await b.login()
    server.delay_next("/ping", 2.0)
    await b.keepalive()


async def _scenario_refused_connection(server, make_broker):
    await make_broker(base_url=f"http://127.0.0.1:{unused_port()}").login()


async def _scenario_http_500(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/accounts", 500, {"errorCode": "error.internal"})
    await b.list_accounts()


async def _scenario_http_429(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/ping", 429, {"errorCode": "error.too-many.requests"})
    await b.keepalive()


async def _scenario_get_404(server, make_broker):
    b = make_broker()
    await b.login()
    await b.quote("NOPE")


async def _scenario_put_rejected(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/session", 400, {"errorCode": "error.invalid.accountId"}, method="PUT")
    await b.select_account("ACC-EMPTY")


async def _scenario_second_401(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/ping", 401, {})
    server.fail_next("/ping", 401, {})
    await b.keepalive()


async def _scenario_relogin_fails(server, make_broker):
    b = make_broker()
    await b.login()
    server.expire_token()
    server.fail_next("/session", 401, {"errorCode": "error.invalid.details"}, method="POST")
    await b.keepalive()


async def _scenario_login_refused(server, make_broker):
    await make_broker(creds=Credentials(TEST_API_KEY, TEST_IDENTIFIER, "other-password-1")).login()


async def _scenario_login_without_tokens(server, make_broker):
    server.fail_next("/session", 200, {"currentAccountId": "ACC-FUNDED"}, method="POST")
    await make_broker().login()


async def _scenario_bound_account_missing(server, make_broker):
    await make_broker(account_id="ACC-GONE").login()


async def _scenario_switch_refused(server, make_broker):
    server.fail_next("/session", 400, {"errorCode": "error.invalid.accountId"}, method="PUT")
    await make_broker(account_id="ACC-EMPTY").login()


async def _scenario_malformed_json(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/accounts", 200, text='{"accounts": NaN}')
    await b.list_accounts()


async def _scenario_error_code_echoes_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    echo = f"error.weird: {TEST_API_KEY} {TEST_PASSWORD} {TEST_IDENTIFIER} {' '.join(server.issued_tokens)}"
    server.fail_next("/accounts", 400, {"errorCode": echo})
    await b.list_accounts()


async def _scenario_rejection_echoes_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/session", 400, {"errorCode": f"error.x {server.issued_tokens[-1]}"}, method="PUT")
    await b.select_account("ACC-EMPTY")


SECRET_SCENARIOS = {name.removeprefix("_scenario_"): fn for name, fn in globals().items() if name.startswith("_scenario_")}


@pytest.mark.parametrize("name", sorted(SECRET_SCENARIOS))
async def test_exception_messages_never_contain_secrets(server, make_broker, name, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BrokerError) as info:
        await SECRET_SCENARIOS[name](server, make_broker)
    assert_no_secret(info.value, server_secrets(server))
    assert info.value.__cause__ is None and info.value.__context__ is None  # brak ukrytych łańcuchów z treścią
    for secret in server_secrets(server):
        assert secret not in caplog.text


# --- rachunki ----------------------------------------------------------------------------------


async def test_list_accounts_parses_id_currency_status_and_balance(server, broker):
    await broker.login()
    server.accounts[0]["status"] = "SUSPENDED"  # status pochodzi z pola API, nie jest zgadywany
    accounts = await broker.list_accounts()
    assert accounts == [
        AccountInfo("ACC-EMPTY", "EUR", "SUSPENDED", Decimal("0.00"), Decimal("0.00"), Decimal("0.00")),
        AccountInfo("ACC-FUNDED", "EUR", "ENABLED", Decimal("40.00"), Decimal("38.71"), Decimal("1.29")),
    ]
    funded = accounts[1]
    assert (str(funded.equity), str(funded.available), str(funded.margin_used)) == ("40.00", "38.71", "1.29")
    assert_no_float(accounts)


async def test_account_is_the_active_session_account(server, broker):
    await broker.login()
    assert (await broker.account()).account_id == "ACC-FUNDED"
    await broker.select_account("ACC-EMPTY")
    active = await broker.account()
    assert (active.account_id, active.equity, active.margin_used) == ("ACC-EMPTY", Decimal("0.00"), Decimal("0.00"))


async def test_select_account_puts_session_once_and_verifies_it(server, broker):
    await broker.login()
    await broker.select_account("ACC-EMPTY")
    assert server.sequence() == ["POST /session", "PUT /session", "GET /session"]
    assert server.requests("PUT", "/session")[0].body == {"accountId": "ACC-EMPTY"}
    await broker.select_account("ACC-EMPTY")  # już aktywny: nic nie wysyła
    assert server.count("PUT", "/session") == 1


async def test_select_unknown_account_raises_broker_error_without_calls(server, broker):
    await broker.login()
    with pytest.raises(BrokerError) as info:
        await broker.select_account("ACC-does-not-exist")
    assert type(info.value) is BrokerError and "ACC-does-not-exist" not in str(info.value)
    assert server.sequence() == ["POST /session"]


async def test_select_account_not_confirmed_drops_the_session(server, broker):
    await broker.login()
    server.fail_next("/session", 200, {"accountId": "ACC-FUNDED"}, method="GET")
    with pytest.raises(BrokerError, match="another account"):
        await broker.select_account("ACC-EMPTY")
    with pytest.raises(SessionExpired):
        await broker.account()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.accounts[1].pop("balance"),
        lambda s: s.accounts[1]["balance"].update(available="38.71"),  # napis zamiast liczby JSON
        lambda s: s.accounts[1]["balance"].update(balance=None),
        lambda s: s.accounts[1].update(accountId="ACC-EMPTY"),  # zdublowany identyfikator
        lambda s: s.accounts[1].update(status=Decimal("1")),
        lambda s: s.accounts[1].pop("currency"),
        lambda s: s.accounts.append("not-an-object"),
    ],
)
async def test_malformed_accounts_are_broker_errors(server, broker, mutate):
    await broker.login()
    mutate(server)
    for call in (broker.list_accounts, broker.account):
        with pytest.raises(BrokerError, match="GET /accounts: unexpected response: accounts"):
            await call()


async def test_account_missing_from_listing_is_a_broker_error(server, broker):
    await broker.login()
    server.accounts.pop(1)
    with pytest.raises(BrokerError, match="active account is not listed"):
        await broker.account()


# --- reguły instrumentu -----------------------------------------------------------------------


def dealing(server, name):
    return server.markets["DE40"]["dealingRules"][name]


def instrument(server):
    return server.markets["DE40"]["instrument"]


async def test_instrument_rules_match_the_live_probe(server, broker):
    await broker.login()
    rules = await broker.instrument_rules("DE40")
    # min. odległość stopu: 0,01% × mid (25780,3 + 25782,3) / 2 = 25781,3 → 2,57813 pkt
    assert rules == InstrumentRules(
        epic="DE40",
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
        value_per_point=Decimal("1"),
        margin_rate=Decimal("0.05"),
        min_stop_distance=Decimal("2.57813"),
        stop_step=Decimal("0.1"),
    )
    assert rules.stop_step != 0
    assert_no_float(rules)
    assert server.sequence()[-1] == "GET /markets/DE40"


async def test_min_stop_distance_in_points_is_taken_as_is(server, broker):
    await broker.login()
    dealing(server, "minStopOrProfitDistance").update(unit="POINTS", value=Decimal("5"))
    assert (await broker.instrument_rules("DE40")).min_stop_distance == Decimal("5")


async def test_zero_step_distance_means_no_stop_rounding(server, broker):
    await broker.login()
    dealing(server, "minStepDistance")["value"] = Decimal("0")
    assert (await broker.instrument_rules("DE40")).stop_step == Decimal("0")


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda s: dealing(s, "minStepDistance").update(unit="PERCENTAGE"), "minStepDistance.unit"),
        (lambda s: dealing(s, "minStopOrProfitDistance").update(unit="AMOUNT"), "minStopOrProfitDistance.unit"),
        (lambda s: dealing(s, "minDealSize").update(unit="PERCENTAGE"), "minDealSize.unit"),
        (lambda s: dealing(s, "minSizeIncrement").update(unit="PERCENTAGE"), "minSizeIncrement.unit"),
        (lambda s: instrument(s).update(marginFactorUnit="POINTS"), "marginFactorUnit"),
        (lambda s: instrument(s).pop("lotSize"), "lotSize"),
        (lambda s: instrument(s).update(lotSize=Decimal("0")), "lotSize"),
        (lambda s: instrument(s).update(lotSize="1"), "lotSize"),
        (lambda s: instrument(s).update(marginFactor=True), "marginFactor"),
        (lambda s: instrument(s).update(marginFactor=Decimal("0")), "marginFactor"),
        (lambda s: instrument(s).update(marginFactor=Decimal("150")), "marginFactor"),
        (lambda s: instrument(s).update(currency="USD"), "currency"),
        (lambda s: dealing(s, "minDealSize").update(value="0.001"), "minDealSize.value"),
        (lambda s: dealing(s, "minDealSize").update(value=Decimal("0")), "minDealSize.value"),
        (lambda s: dealing(s, "minSizeIncrement").update(value=None), "minSizeIncrement.value"),
        (lambda s: dealing(s, "minSizeIncrement").update(value=Decimal("-0.001")), "minSizeIncrement.value"),
        (lambda s: dealing(s, "minStepDistance").update(value=Decimal("-0.1")), "minStepDistance.value"),
        (lambda s: dealing(s, "minStopOrProfitDistance").update(value=Decimal("-1")), "minStopOrProfitDistance.value"),
        (lambda s: s.markets["DE40"]["dealingRules"].update(minStepDistance=Decimal("0.1")), "minStepDistance"),
        (lambda s: s.markets["DE40"].pop("dealingRules"), "dealingRules"),
        (lambda s: s.markets["DE40"].pop("snapshot"), "snapshot"),  # minimum w % wymaga ceny
        (lambda s: s.set_quote("DE40", "25790.0", "25780.0"), "Quote.bid"),
    ],
)
async def test_unexpected_rule_shapes_or_units_are_broker_errors(server, broker, mutate, field):
    await broker.login()
    mutate(server)
    with pytest.raises(BrokerError) as info:
        await broker.instrument_rules("DE40")
    assert type(info.value) is BrokerError
    assert str(info.value).startswith("GET /markets/DE40: unexpected response:") and field in str(info.value)


@pytest.mark.parametrize("epic", ["", "../session", "DE40/../ping", "DE40?x=1", "DE 40", ".", "..", "DE40#", 40, None])
async def test_epic_is_validated_before_any_request(server, broker, epic):
    await broker.login()
    for call in (broker.instrument_rules, broker.quote, lambda e: broker.daily_candles(e, 15)):
        with pytest.raises((ValueError, TypeError)):
            await call(epic)
    with pytest.raises((ValueError, TypeError)):
        await broker.candles_5m(epic, at(0), at(600))
    assert server.sequence() == ["POST /session"]


# --- notowanie --------------------------------------------------------------------------------


async def test_quote_parses_bid_and_offer_as_decimal(server, broker):
    await broker.login()
    q = await broker.quote("DE40")
    assert q == Quote("DE40", Decimal("25780.3"), Decimal("25782.3"), at(0.2))  # pierwszy odczyt: ts = teraz
    assert_no_float(q)


async def test_quote_ts_is_the_moment_the_observed_tuple_last_changed(server, broker, clock):
    await broker.login()
    first = await broker.quote("DE40")  # 0,2
    clock.advance(10)
    unchanged = await broker.quote("DE40")  # 10,2: ta sama krotka → stary ts
    server.set_quote("DE40", "25780.3", "25782.3", update_time="2026-11-02T08:30:10.004")  # zmienia się tylko czas
    by_time = await broker.quote("DE40")  # 10,4
    clock.advance(5)
    server.set_quote("DE40", "25781.0", "25782.3")
    by_bid = await broker.quote("DE40")  # 15,4
    server.set_quote("DE40", "25781.0", "25783.0")
    by_offer = await broker.quote("DE40")  # 15,6
    again = await broker.quote("DE40")  # 15,8: bez zmiany
    assert [q.ts for q in (first, unchanged, by_time, by_bid, by_offer, again)] == [
        at(0.2), at(0.2), at(10.4), at(15.4), at(15.6), at(15.6),
    ]  # fmt: skip
    assert (again.bid, again.ask) == (Decimal("25781.0"), Decimal("25783.0"))


async def test_quote_never_parses_update_time_and_ignores_market_status(server, broker):
    await broker.login()
    server.set_quote("DE40", "25780.3", "25782.3", update_time="not-a-time", market_status="CLOSED")
    q = await broker.quote("DE40")
    assert (q.bid, q.ask, q.ts) == (Decimal("25780.3"), Decimal("25782.3"), at(0.2))


async def test_quote_freshness_is_tracked_per_epic(server, broker, clock):
    server.markets["US100"] = json.loads(json.dumps(server.markets["DE40"], default=str), parse_float=Decimal)
    server.markets["US100"]["snapshot"].update(bid=Decimal("28401.8"), offer=Decimal("28403.6"))
    await broker.login()
    de40 = await broker.quote("DE40")  # 0,2
    clock.advance(30)
    us100 = await broker.quote("US100")  # 30,2: pierwszy odczyt US100
    de40_again = await broker.quote("DE40")  # 30,4: DE40 bez zmian
    assert (de40.ts, us100.ts, de40_again.ts) == (at(0.2), at(30.2), at(0.2))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.set_quote("DE40", None, "25782.3"),
        lambda s: s.set_quote("DE40", "25780.3", None),
        lambda s: s.markets["DE40"]["snapshot"].update(bid=None),
        lambda s: s.markets["DE40"]["snapshot"].update(offer="25782.3"),
        lambda s: s.markets["DE40"]["snapshot"].update(bid=Decimal("0")),
        lambda s: s.markets["DE40"]["snapshot"].update(updateTime=Decimal("1")),
        lambda s: s.markets["DE40"].pop("snapshot"),
    ],
)
async def test_quote_without_usable_bid_or_offer_is_a_broker_error(server, broker, mutate):
    await broker.login()
    mutate(server)
    with pytest.raises(BrokerError, match="GET /markets/DE40: unexpected response: snapshot"):
        await broker.quote("DE40")


async def test_crossed_quote_wraps_the_domain_error_and_keeps_freshness_state(server, broker, clock):
    await broker.login()
    good = await broker.quote("DE40")  # 0,2
    server.set_quote("DE40", "25790.0", "25780.0")
    with pytest.raises(BrokerError) as info:
        await broker.quote("DE40")
    assert type(info.value) is BrokerError
    assert str(info.value) == "GET /markets/DE40: unexpected response: snapshot: invalid Quote.bid"
    assert info.value.__context__ is None
    server.set_quote("DE40", "25780.3", "25782.3")
    clock.advance(1)
    assert (await broker.quote("DE40")).ts == good.ts  # odrzucona migawka nie zmieniła stanu świeżości


# --- świece 5-minutowe i dzienne ------------------------------------------------------------------

OPEN = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)  # 09:00 w Berlinie (czas zimowy)
M5 = timedelta(minutes=5)


def advance_to(clock, moment: datetime) -> None:
    clock.advance((moment - clock.now_utc()).total_seconds())


def prices_reply(*candles) -> dict:
    return {"prices": list(candles), "instrumentType": "INDICES", "tickSize": Decimal("0.1"), "pipPosition": 0}


async def test_candles_5m_request_uses_utc_window_without_zone_suffix(server, broker, clock):
    await broker.login()
    advance_to(clock, OPEN + 2 * M5)
    berlin = ZoneInfo("Europe/Berlin")
    start = OPEN.astimezone(berlin)  # 09:00 w Berlinie = 08:00 UTC; parametr jest w UTC
    await broker.candles_5m("DE40", start, start + 2 * M5)
    [req] = server.requests("GET", "/prices/DE40")
    # `to` = min(end + przesunięcie, teraz − 5 s): nigdy w przyszłości; `max` z obciętego okna (1 pełny slot + zapas)
    assert req.query == {"resolution": "MINUTE_5", "from": "2026-11-02T08:00:00", "to": "2026-11-02T08:09:55", "max": "3"}


async def test_candles_use_snapshot_time_utc_only(server, broker, clock):
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN, local_offset=timedelta(hours=7))])
    advance_to(clock, OPEN + M5)
    [got] = await broker.candles_5m("DE40", OPEN, OPEN + M5)
    assert got.start == OPEN
    # świeca, której czas lokalny wypada w zakresie, a UTC nie: odrzucona po snapshotTimeUTC
    outside = candle(OPEN - timedelta(hours=1), local_offset=timedelta(hours=1))
    assert outside["snapshotTime"] == stamp(OPEN)
    server.fail_next("/prices/DE40", 200, prices_reply(outside))
    assert await broker.candles_5m("DE40", OPEN, OPEN + M5) == []


async def test_candle_without_snapshot_time_utc_is_malformed_even_with_local_time(server, broker, clock):
    await broker.login()
    local_only = candle(OPEN)
    del local_only["snapshotTimeUTC"]
    server.fail_next("/prices/DE40", 200, prices_reply(local_only))
    advance_to(clock, OPEN + M5)
    with pytest.raises(BrokerError, match=r"prices\[0\]\.snapshotTimeUTC: missing"):
        await broker.candles_5m("DE40", OPEN, OPEN + M5)


async def test_candle_prices_are_exact_mids_of_bid_and_ask(server, broker, clock):
    await broker.login()
    c = candle(
        OPEN,
        open=("25780.3", "25782.3"),
        high=("25800.1", "25801.4"),
        low=("25770.05", "25772.0"),
        close=("25790.5", "25792.6"),
    )
    server.set_candles("DE40", "MINUTE_5", [c])
    advance_to(clock, OPEN + M5)
    [got] = await broker.candles_5m("DE40", OPEN, OPEN + M5)
    assert got == Candle("DE40", OPEN, Decimal("25781.3"), Decimal("25800.75"), Decimal("25771.025"), Decimal("25791.55"))
    assert_no_float(got)


@pytest.mark.parametrize(
    ("now", "starts"),
    [
        (OPEN + 2 * M5, [OPEN, OPEN + M5]),  # granica: start + 5 min == teraz → zakończona
        (OPEN + 2 * M5 - timedelta(microseconds=1), [OPEN]),  # 08:05 jeszcze trwa
        (OPEN + M5 - timedelta(microseconds=1), []),
    ],
)
async def test_forming_candle_is_dropped_and_boundary_is_included(server, broker, clock, now, starts):
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN), candle(OPEN + M5), candle(OPEN + 2 * M5)])
    advance_to(clock, now)
    got = await broker.candles_5m("DE40", OPEN, OPEN + 3 * M5)
    assert [c.start for c in got] == starts


async def test_candles_are_filtered_sorted_and_deduplicated(server, broker, clock):
    await broker.login()
    reply = prices_reply(
        candle(OPEN + 2 * M5), candle(OPEN), candle(OPEN + M5), candle(OPEN), candle(OPEN - M5), candle(OPEN + 3 * M5)
    )
    server.fail_next("/prices/DE40", 200, reply)
    advance_to(clock, OPEN + timedelta(minutes=30))
    got = await broker.candles_5m("DE40", OPEN, OPEN + 3 * M5)
    assert [c.start for c in got] == [OPEN, OPEN + M5, OPEN + 2 * M5]


async def test_conflicting_duplicate_candles_are_a_broker_error(server, broker, clock):
    await broker.login()
    server.fail_next("/prices/DE40", 200, prices_reply(candle(OPEN), candle(OPEN, close=("25700.0", "25702.0"))))
    advance_to(clock, OPEN + M5)
    with pytest.raises(BrokerError, match="conflicting"):
        await broker.candles_5m("DE40", OPEN, OPEN + M5)


async def test_empty_five_minute_range_is_valid(server, broker, clock):
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [])
    advance_to(clock, OPEN + M5)
    assert await broker.candles_5m("DE40", OPEN, OPEN + M5) == []
    assert await broker.candles_5m("DE40", OPEN, OPEN) == []  # pusty zakres: bez zapytania
    assert server.count("GET", "/prices/DE40") == 1


async def test_candle_stamp_as_period_end_shifts_start_back_by_one_period(server, broker, clock, monkeypatch):
    monkeypatch.setattr(capitalcom, "CANDLE_STAMP_IS_PERIOD_START", False)
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN + M5), candle(OPEN + 2 * M5), candle(OPEN + 3 * M5)])
    advance_to(clock, OPEN + 2 * M5 + timedelta(seconds=capitalcom.CANDLE_TO_SKEW_MARGIN_S))
    got = await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5)
    assert [c.start for c in got] == [OPEN, OPEN + M5]  # znaczniki 08:05 i 08:10 = końce świec 08:00 i 08:05
    [req] = server.requests("GET", "/prices/DE40")
    assert (req.query["from"], req.query["to"]) == ("2026-11-02T08:05:00", "2026-11-02T08:10:00")  # = teraz − 5 s
    day = datetime(2026, 11, 1, tzinfo=UTC)
    server.set_candles("DE40", "DAY", [candle(day), candle(day + timedelta(days=1))])
    daily = await broker.daily_candles("DE40", 2)
    # znaczniki 1.11 i 2.11 00:00 = końce dni 31.10 i 1.11 (oba zakończone o 08:10 2.11)
    assert [c.start for c in daily] == [day - timedelta(days=1), day]


async def test_candles_5m_reject_naive_datetimes_without_request(server, broker, clock):
    await broker.login()
    with pytest.raises(ValueError):
        await broker.candles_5m("DE40", OPEN.replace(tzinfo=None), OPEN + M5)
    with pytest.raises(ValueError):
        await broker.candles_5m("DE40", OPEN, (OPEN + M5).replace(tzinfo=None))
    clock.advance((OPEN + 1000 * M5 - clock.now_utc()).total_seconds())
    with pytest.raises(ValueError, match="1000"):
        await broker.candles_5m("DE40", OPEN, OPEN + 1000 * M5)
    assert server.count("GET", "/prices/DE40") == 0


@pytest.mark.parametrize(
    "reply",
    [
        {"instrumentType": "INDICES"},
        {"prices": "none"},
        {"prices": [5]},
        prices_reply({k: v for k, v in candle(OPEN).items() if k != "openPrice"}),
        prices_reply({**candle(OPEN), "openPrice": {"bid": Decimal("25780.3"), "ask": None}}),
        prices_reply({**candle(OPEN), "closePrice": {"bid": "25780.3", "ask": Decimal("25782.3")}}),
        prices_reply({**candle(OPEN), "lowPrice": {"bid": Decimal("25782.3"), "ask": Decimal("25780.3")}}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-11-02T08:00:00+01:00"}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-11-02T08:00:00Z"}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-11-02 08:00:00"}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-11-02"}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-13-02T08:00:00"}),
        prices_reply(candle(OPEN, high=("25700.0", "25701.0"), low=("25770.0", "25772.0"))),
    ],
)
async def test_malformed_price_payloads_are_broker_errors(server, broker, clock, reply):
    await broker.login()
    advance_to(clock, OPEN + M5)
    server.fail_next("/prices/DE40", 200, reply)
    with pytest.raises(BrokerError, match="GET /prices/DE40: unexpected response"):
        await broker.candles_5m("DE40", OPEN, OPEN + M5)
    server.fail_next("/prices/DE40", 200, reply)
    with pytest.raises(BrokerError, match="GET /prices/DE40: unexpected response"):
        await broker.daily_candles("DE40", 1)


async def test_candle_timestamps_with_milliseconds_are_accepted(server, broker, clock):
    await broker.login()
    server.fail_next("/prices/DE40", 200, prices_reply({**candle(OPEN), "snapshotTimeUTC": "2026-11-02T08:00:00.000"}))
    advance_to(clock, OPEN + M5)
    [got] = await broker.candles_5m("DE40", OPEN, OPEN + M5)
    assert got.start == OPEN


DAY0 = datetime(2026, 10, 13, tzinfo=UTC)


def days(n: int) -> list[dict]:
    return [candle(DAY0 + timedelta(days=i)) for i in range(n)]


async def test_daily_candles_request_count_plus_two_and_drop_the_forming_day(server, broker):
    await broker.login()
    server.set_candles("DE40", "DAY", days(21))  # 13.10 … 2.11; 2.11 trwa (teraz 2.11 07:30)
    got = await broker.daily_candles("DE40", 15)
    [req] = server.requests("GET", "/prices/DE40")
    assert req.query == {"resolution": "DAY", "max": "17"}
    assert [c.start for c in got] == [DAY0 + timedelta(days=i) for i in range(5, 20)]  # 18.10 … 1.11
    assert_no_float(got)


async def test_daily_candles_need_count_completed_days(server, broker):
    await broker.login()
    server.set_candles("DE40", "DAY", days(21)[-15:])  # 15 świec, ostatnia trwa → 14 zakończonych
    with pytest.raises(BrokerError, match="14 completed daily candles, 15 needed"):
        await broker.daily_candles("DE40", 15)
    server.set_candles("DE40", "DAY", [])
    with pytest.raises(BrokerError, match="0 completed daily candles"):
        await broker.daily_candles("DE40", 15)


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "15", 999])
async def test_daily_candles_validate_count_without_request(server, broker, count):
    await broker.login()
    with pytest.raises((ValueError, TypeError)):
        await broker.daily_candles("DE40", count)
    assert server.count("GET", "/prices/DE40") == 0


async def test_market_data_values_contain_no_float(server, broker, clock):
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN)])
    server.set_candles("DE40", "DAY", days(21))
    advance_to(clock, OPEN + M5)
    values = [
        await broker.account(),
        await broker.list_accounts(),
        await broker.instrument_rules("DE40"),
        await broker.quote("DE40"),
        await broker.candles_5m("DE40", OPEN, OPEN + M5),
        await broker.daily_candles("DE40", 15),
    ]
    assert_no_float(values)


class SteppedClock:
    """Zegar ścienny, który można cofnąć (krok NTP); `sleep` przesuwa czas i zapisuje oczekiwania."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def now_utc(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


async def test_spacer_never_waits_longer_than_its_longest_pause_after_the_clock_steps_back():
    clock = SteppedClock(T0)
    spacer = capitalcom._Spacer(clock, 0.2, max_wait_s=1.0)
    await spacer.acquire()
    clock.now -= timedelta(hours=1)  # zegar cofnięty o godzinę: bez limitu każde żądanie czekałoby godzinę
    await spacer.acquire()
    await spacer.acquire()
    assert clock.sleeps == [1.0, 0.2]
    spacer.pause_until(clock.now + timedelta(seconds=1))  # pauza po 429 mieści się w limicie oczekiwania
    await spacer.acquire()
    assert clock.sleeps == [1.0, 0.2, 1.0]


# --- runda poprawek 1: anulowanie, okno świec bez przyszłości, drobne zabezpieczenia -----------------


async def wait_for_request(server, method: str, path: str, count: int = 1) -> None:
    """Czeka (czas rzeczywisty, najwyżej 5 s), aż serwer przyjmie `count` żądań `method path`."""
    for _ in range(500):
        if server.count(method, path) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{method} {path} never arrived")


async def cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("step", ["PUT", "GET"])
async def test_cancelled_select_account_drops_the_session(server, broker, step):
    await broker.login()
    server.delay_next("/session", 2.0, method=step)
    task = asyncio.create_task(broker.select_account("ACC-EMPTY"))
    await wait_for_request(server, step, "/session")
    await cancel(task)  # np. asyncio.wait_for silnika: rachunek sesji u brokera jest teraz niepewny
    assert broker._session_account_id is None
    sent = len(server.log)
    with pytest.raises(SessionExpired, match="not logged in"):
        await broker.account()
    assert len(server.log) == sent  # nic nie wychodzi na niepewnym rachunku, aż do nowego logowania
    await broker.login()
    assert (await broker.account()).account_id == "ACC-FUNDED"


@pytest.mark.parametrize("step", ["POST", "PUT", "GET"])
async def test_cancelled_login_drops_the_session_including_the_previous_one(server, make_broker, step):
    broker = make_broker(account_id="ACC-EMPTY")
    await broker.login()  # pełna, poprawna sesja na ACC-EMPTY
    server.delay_next("/session", 2.0, method=step)
    task = asyncio.create_task(broker.login())
    await wait_for_request(server, step, "/session", count=2)
    await cancel(task)
    assert (broker._tokens, broker._session_account_id) == (None, None)
    sent = len(server.log)
    with pytest.raises(SessionExpired, match="not logged in"):
        await broker.keepalive()
    assert len(server.log) == sent
    await broker.login()
    assert (await broker.account()).account_id == "ACC-EMPTY"


async def test_relogin_never_sends_other_requests_before_the_switch_is_verified(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")
    await broker.login()
    server.expire_token()
    server.delay_next("/session", 0.3, method="PUT")  # przełączenie w toku, gdy przychodzą inne żądania
    first = asyncio.create_task(broker.keepalive())
    await wait_for_request(server, "PUT", "/session", count=2)
    await asyncio.gather(first, broker.list_accounts(), broker.quote("DE40"))
    verified_at = max(i for i, r in enumerate(server.log) if (r.method, r.path) == ("GET", "/session"))
    for i, req in enumerate(server.log):
        if req.path != "/session" and req.auth_ok:
            assert req.account == "ACC-EMPTY" and i > verified_at, server.sequence()
    assert server.count("POST", "/session") == 2


async def test_opening_range_read_at_0810_never_asks_for_the_future(server, broker, clock):
    assert server.reject_future_to  # serwer testowy odrzuca `to` z przyszłości (wariant ostrożny, C9)
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN), candle(OPEN + M5), candle(OPEN + 2 * M5)])
    advance_to(clock, OPEN + 2 * M5)  # 08:10:00 UTC = 09:10 w Berlinie
    got = await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5)
    assert [c.start for c in got] == [OPEN, OPEN + M5]
    [req] = server.requests("GET", "/prices/DE40")
    assert (req.query["to"], req.status) == ("2026-11-02T08:09:55", 200)  # teraz − margines rozjazdu zegarów


async def test_fake_server_really_rejects_a_future_to(server, broker, clock):
    await broker.login()
    advance_to(clock, OPEN + 2 * M5)
    params = {"resolution": "MINUTE_5", "from": "2026-11-02T08:00:00", "to": "2026-11-02T08:15:00", "max": "5"}
    with pytest.raises(BrokerError, match="HTTP 400 error.invalid.to"):
        await broker._request("GET", "/prices/DE40", params=params)


@pytest.mark.parametrize(
    ("now", "starts"),
    [
        (OPEN + 2 * M5, [OPEN]),  # świeca 08:05 (znacznik 08:10) przyjdzie w odczycie po upływie marginesu
        (OPEN + 2 * M5 + timedelta(seconds=5), [OPEN, OPEN + M5]),
        (OPEN + 2 * M5 + timedelta(seconds=7, microseconds=500), [OPEN, OPEN + M5]),
    ],
)
async def test_flipped_stamp_semantics_never_asks_for_the_future(server, broker, clock, monkeypatch, now, starts):
    monkeypatch.setattr(capitalcom, "CANDLE_STAMP_IS_PERIOD_START", False)
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN + M5), candle(OPEN + 2 * M5)])  # znaczniki = końce świec
    advance_to(clock, now)
    got = await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5)
    assert [c.start for c in got] == starts
    [req] = server.requests("GET", "/prices/DE40")
    newest = now - timedelta(seconds=capitalcom.CANDLE_TO_SKEW_MARGIN_S)
    assert req.status == 200 and req.query["to"] == newest.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.mark.parametrize(
    ("now", "end", "to"),
    [
        (OPEN + 2 * M5 + timedelta(seconds=30), OPEN + 3 * M5, "2026-11-02T08:10:25"),  # teraz − 5 s
        (OPEN + 2 * M5 + timedelta(seconds=7, microseconds=900_000), OPEN + 3 * M5, "2026-11-02T08:10:02"),
        (OPEN + 6 * M5, OPEN + 2 * M5, "2026-11-02T08:10:00"),  # okno dawno minione: koniec okna bez zmian
        (OPEN + 2 * M5 + timedelta(seconds=5), OPEN + 2 * M5, "2026-11-02T08:10:00"),  # granica marginesu
    ],
)
async def test_candles_to_is_capped_with_a_skew_margin(server, broker, clock, now, end, to):
    # zegar silnika może się spieszyć względem brokera: `to` najwyżej teraz − margines, więc broker nie dostaje pytania
    # o swoją przyszłość (zachowanie przy takim pytaniu nieznane, C9)
    await broker.login()
    advance_to(clock, now)
    await broker.candles_5m("DE40", OPEN, end)
    [req] = server.requests("GET", "/prices/DE40")
    assert (req.query["to"], req.status) == (to, 200)
    assert capitalcom.CANDLE_TO_SKEW_MARGIN_S == 5  # stała procedury, nie parametr strategii


async def test_candles_read_right_after_the_range_still_gets_its_candles(server, broker, clock):
    await broker.login()
    server.set_candles("DE40", "MINUTE_5", [candle(OPEN), candle(OPEN + M5)])
    advance_to(clock, OPEN + M5 + timedelta(seconds=3))  # okno [08:05, 08:10) po obcięciu do 08:04:58 jest puste
    assert await broker.candles_5m("DE40", OPEN + M5, OPEN + 2 * M5) == []
    assert server.count("GET", "/prices/DE40") == 0  # bez zapytania
    advance_to(clock, OPEN + 2 * M5)  # 08:10:00: `to` = 08:09:55, znaczniki 08:00 i 08:05 (początki świec) mieszczą się
    got = await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5)
    assert [c.start for c in got] == [OPEN, OPEN + M5]
    [req] = server.requests("GET", "/prices/DE40")
    assert (req.query["to"], req.query["max"]) == ("2026-11-02T08:09:55", "3")


@pytest.mark.parametrize("now", [T0, OPEN + 10 * M5, OPEN + 1000 * M5])
async def test_point_limit_is_checked_on_the_requested_window(server, broker, clock, now):
    # limit 1000 świec dotyczy żądanego okna [start, end), nie okna obciętego do teraz: wynik nie zależy od zegara
    await broker.login()
    advance_to(clock, now)
    with pytest.raises(ValueError, match="1001 candles, more than 1000"):
        await broker.candles_5m("DE40", OPEN, OPEN + 999 * M5)
    assert server.count("GET", "/prices/DE40") == 0
    await broker.candles_5m("DE40", OPEN, OPEN + 998 * M5)  # 998 slotów + zapas 2 = dokładnie 1000: dozwolone
    if now == T0:
        assert server.count("GET", "/prices/DE40") == 0  # okno w przyszłości: nic nie wysłano
    else:
        [req] = server.requests("GET", "/prices/DE40")
        assert int(req.query["max"]) <= 1000


async def test_window_entirely_in_the_future_sends_nothing(server, broker, clock):
    await broker.login()  # teraz 07:30, okno 08:00–08:10
    assert await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5) == []
    assert server.count("GET", "/prices/DE40") == 0


class YieldingClock:
    """Zegar testowy, którego `sleep` oddaje sterowanie pętli (FakeClock tego nie robi)."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def now_utc(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)
        self.now += timedelta(seconds=seconds)


async def test_spacer_honours_a_pause_raised_while_a_request_is_already_waiting():
    clock = YieldingClock(T0)
    spacer = capitalcom._Spacer(clock, 0.2, max_wait_s=1.0)
    await spacer.acquire()  # T0
    waiting = asyncio.create_task(spacer.acquire())  # będzie czekać do T0 + 0,2
    await asyncio.sleep(0)
    assert clock.sleeps == [0.2]
    spacer.pause_until(T0 + timedelta(seconds=1))  # 429 zauważone przez inne żądanie w trakcie tego czekania
    await waiting
    assert clock.now == T0 + timedelta(seconds=1) and clock.sleeps == [0.2, 0.8]


class FrozenClock:
    """Zegar, którego `sleep` oddaje sterowanie pętli, ale czasu nie przesuwa (np. źle napisany zegar testowy)."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def now_utc(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


async def test_spacer_does_not_spin_with_a_clock_that_never_advances():
    clock = FrozenClock(T0)
    spacer = capitalcom._Spacer(clock, 0.2, max_wait_s=1.0)
    await spacer.acquire()
    await asyncio.wait_for(spacer.acquire(), timeout=1.0)  # sen, który nie przesunął zegara, kończy czekanie
    assert clock.sleeps == [0.2]
    clock.now -= timedelta(seconds=0.1)  # sen, po którym zegar się cofnął, też niczego nie przesunął do przodu
    real_sleep = clock.sleep

    async def sleep_stepping_back(seconds: float) -> None:
        await real_sleep(seconds)
        clock.now -= timedelta(seconds=0.01)

    clock.sleep = sleep_stepping_back
    await asyncio.wait_for(spacer.acquire(), timeout=1.0)
    assert clock.sleeps == [0.2, pytest.approx(0.3)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.accounts[1]["balance"].update(balance=Decimal("9E+999999"), available=Decimal("-9E+999999")),
    ],
)
async def test_out_of_range_account_numbers_are_broker_errors(server, broker, mutate):
    await broker.login()
    mutate(server)
    for call in (broker.list_accounts, broker.account):
        with pytest.raises(BrokerError, match="GET /accounts: unexpected response") as info:
            await call()
        assert type(info.value) is BrokerError and info.value.__context__ is None


async def test_out_of_range_rule_numbers_are_broker_errors(server, broker):
    await broker.login()
    dealing(server, "minStopOrProfitDistance")["value"] = Decimal("9E+999999")  # % × mid przekracza zakres
    with pytest.raises(BrokerError, match="GET /markets/DE40: unexpected response") as info:
        await broker.instrument_rules("DE40")
    assert type(info.value) is BrokerError


@pytest.mark.parametrize(
    "reply",
    [
        prices_reply({**candle(OPEN), "openPrice": {"bid": Decimal("9E+999999"), "ask": Decimal("9E+999999")}}),
        prices_reply({**candle(OPEN), "snapshotTimeUTC": "9999-12-31T00:00:00"}),  # start + 1 dzień poza zakresem
    ],
)
async def test_out_of_range_candle_numbers_and_times_are_broker_errors(server, broker, reply):
    await broker.login()
    server.fail_next("/prices/DE40", 200, reply)
    with pytest.raises(BrokerError, match="GET /prices/DE40: unexpected response") as info:
        await broker.daily_candles("DE40", 1)
    assert type(info.value) is BrokerError


async def test_session_with_raise_for_status_cannot_bypass_relogin(server, capital_credentials, clock):
    async with aiohttp.ClientSession(raise_for_status=True) as http:
        broker = CapitalComBroker(capital_credentials, http, clock, base_url=server.base_url)
        await broker.login()
        server.expire_token()
        await broker.keepalive()
        server.markets.pop("DE40")
        with pytest.raises(BrokerError, match="HTTP 404 error.not-found.epic"):
            await broker.quote("DE40")
    assert server.count("POST", "/session") == 2


async def test_waiting_login_raises_when_the_shared_session_is_gone(broker, monkeypatch):
    original = broker._login_locked

    async def login_then_dropped() -> None:
        await original()
        broker._drop_session()  # np. nieudane select_account tuż po logowaniu, zanim czekający to sprawdzi

    monkeypatch.setattr(broker, "_login_locked", login_then_dropped)
    results = await asyncio.gather(broker.login(), broker.login(), return_exceptions=True)
    assert results[0] is None and isinstance(results[1], BrokerError)
    with pytest.raises(SessionExpired):
        await broker.keepalive()


@pytest.mark.parametrize("leader_ends", ["cancelled", "failed"])
async def test_a_waiting_login_retries_when_the_shared_attempt_was_cancelled(server, broker, leader_ends):
    server.delay_next("/session", 2.0 if leader_ends == "cancelled" else 0.3, method="POST")
    if leader_ends == "failed":
        server.fail_next("/session", 503, {"errorCode": "error.service.unavailable"}, method="POST")
    leader = asyncio.create_task(broker.login())
    await wait_for_request(server, "POST", "/session")
    follower = asyncio.create_task(broker.login())
    await asyncio.sleep(0)  # czekający już czeka na wspólną próbę (blokada logowania)
    if leader_ends == "cancelled":
        await cancel(leader)  # np. asyncio.wait_for silnika: anulowanie to nie nieudane logowanie
        await follower  # więc czekający loguje się sam, jeden raz
        assert server.count("POST", "/session") == 2
        await broker.keepalive()
        assert server.log[-1].status == 200
        await asyncio.gather(broker.login(), broker.login())  # po udanej próbie znów jedno wspólne logowanie
        assert server.count("POST", "/session") == 3
    else:
        with pytest.raises(BrokerError, match="503"):
            await leader
        with pytest.raises(BrokerError, match="concurrent login attempt"):
            await follower  # wspólna próba naprawdę zawiodła: bez własnego logowania (ochrona przed blokadą)
        assert server.count("POST", "/session") == 1


async def test_select_account_refuses_any_account_but_the_bound_one(server, make_broker):
    broker = make_broker(account_id="ACC-EMPTY")
    await broker.login()
    sent = len(server.log)
    with pytest.raises(BrokerError, match="bound account") as info:
        await broker.select_account("ACC-FUNDED")  # kolejne ponowne logowanie i tak wróciłoby na ACC-EMPTY
    assert type(info.value) is BrokerError and "ACC-FUNDED" not in str(info.value)
    await broker.select_account("ACC-EMPTY")  # związany i aktywny: nic nie wysyła
    assert len(server.log) == sent
    assert (await broker.account()).account_id == "ACC-EMPTY"


@pytest.mark.parametrize("stamp_utc", ["2026-11-02T08:00:00.999", "2026-11-02T08:02:00", "2026-11-02T08:00:30"])
async def test_five_minute_stamps_off_the_five_minute_grid_are_broker_errors(server, broker, clock, stamp_utc):
    await broker.login()
    server.fail_next("/prices/DE40", 200, prices_reply({**candle(OPEN), "snapshotTimeUTC": stamp_utc}))
    advance_to(clock, OPEN + 2 * M5)
    with pytest.raises(BrokerError, match="5-minute boundary"):
        await broker.candles_5m("DE40", OPEN, OPEN + 2 * M5)
