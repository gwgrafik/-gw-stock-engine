"""Testy adaptera Capital.com, część 2: wejście, potwierdzenie, lista pozycji, stop, zamknięcie, historia zamknięć,
zlecenia oczekujące oraz wspólny kontrakt `BrokerContract` uruchomiony na adapterze.

Adapter rozmawia wyłącznie z lokalnym serwerem `fake_capital_server` (127.0.0.1); prawdziwa sieć nie jest używana.
Czas: zegar testu startuje 2026-11-02 07:30:00 UTC; limiter ogólny (0,2 s) wyznacza chwile kolejnych żądań, więc
logowanie jest w 0,0 s, pierwsze `POST /positions` w 0,2 s, a pozycja ma `createdDateUTC` 07:30:00.200.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.broker import capitalcom, capitalcom_history, capitalcom_parse, capitalcom_trading
from gwstock.broker.capitalcom import CapitalComBroker
from gwstock.broker.capitalcom_history import history_windows, split_window
from gwstock.broker.capitalcom_parse import is_market_closed_code
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    ClosedDeal,
    EntryRequest,
    MarketClosed,
    OpenPosition,
    SessionExpired,
    Side,
    WorkingOrder,
)
from tests.unit.broker_contract import BrokerContract
from tests.unit.fake_capital_server import (
    MARKET_CLOSED_CODE,
    TEST_API_KEY,
    TEST_IDENTIFIER,
    TEST_PASSWORD,
    FakePosition,
    activity,
)
from tests.unit.fake_capital_server import dumps as fake_dumps

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # = start fixture `clock`
BID, OFFER = Decimal("25780.3"), Decimal("25782.3")  # migawka DE40 serwera testowego
DAY = timedelta(days=1)


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


def parse_api(text: str) -> datetime:
    """Parametr `from`/`to` zapytania (UTC bez strefy) → datetime UTC."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def entry(side: Side = Side.BUY, size: str = "0.001", stop: str = "30", epic: str = "DE40") -> EntryRequest:
    return EntryRequest("gws-20261102-DE40-1-entry", epic, side, Decimal(size), Decimal(stop))


@pytest.fixture
def server(fake_capital_server):
    return fake_capital_server


@pytest.fixture
def broker(capital_broker):
    return capital_broker


@pytest.fixture
def make_broker(fake_capital_server, capital_credentials, capital_http, clock):
    """Nowa instancja adaptera (np. „po restarcie”: bez pamięci własnych żądań) na tym samym serwerze."""

    def make(*, account_id=None, **kwargs):
        creds = capital_credentials
        if account_id is not None:
            creds = dataclasses.replace(creds, account_id=account_id)
        return CapitalComBroker(creds, capital_http, clock, base_url=fake_capital_server.base_url, **kwargs)

    return make


def server_secrets(server) -> list[str]:
    return [TEST_API_KEY, TEST_IDENTIFIER, TEST_PASSWORD, *server.issued_tokens]


def since_mark(server) -> int:
    return len(server.log)


def seq(server, mark: int) -> list[str]:
    return server.sequence()[mark:]


async def open_confirmed(broker, side: Side = Side.BUY, **kwargs) -> tuple[str, OpenPosition]:
    ack = await broker.open_position(entry(side, **kwargs))
    pos = await broker.confirm(ack.deal_reference)
    assert pos is not None
    return ack.deal_reference, pos


async def restart(make_broker, clock) -> CapitalComBroker:
    """Nowa instancja adaptera (pusta pamięć), zalogowana; 1 s później (limit logowań na klucz API)."""
    clock.advance(1)
    restarted = make_broker()
    await restarted.login()
    return restarted


def only_position(server) -> FakePosition:
    [pos] = server.positions.values()
    return pos


# --- wspólny kontrakt -------------------------------------------------------------------------------


class TestCapitalComContract(BrokerContract):
    """Ten sam zestaw co `TestFakeBrokerContract`, na adapterze i lokalnym serwerze testowym.

    `expire_session`: adapter po 401 sam loguje się ponownie, więc samo wygaszenie tokenów niczego nie pokaże;
    hook dodatkowo odrzuca NAJBLIŻSZE logowanie (jak broker przy wygasłej sesji, której nie da się odnowić), więc
    adapter musi zgłosić `SessionExpired`, a jawne `login()` potem znowu działa.
    """

    @pytest.fixture
    def broker(self, capital_broker):
        return capital_broker

    @pytest.fixture
    def close_market(self, fake_capital_server):
        return lambda: fake_capital_server.set_market_closed(True)

    @pytest.fixture
    def add_working_order(self, fake_capital_server):
        def add(epic: str, side: Side, size: Decimal, level: Decimal) -> WorkingOrder:
            order_id = fake_capital_server.add_working_order(epic, side.value, size, level)
            return WorkingOrder(order_id, epic, side, size, level)

        return add

    @pytest.fixture
    def reject_next_entry(self, fake_capital_server):
        return fake_capital_server.reject_next_order

    @pytest.fixture
    def expire_session(self, fake_capital_server):
        def expire() -> None:
            fake_capital_server.expire_token()
            fake_capital_server.fail_next("/session", 401, {"errorCode": "error.invalid.details"}, method="POST")

        return expire


# --- open_position -----------------------------------------------------------------------------------


async def test_open_position_sends_stop_distance_atomically_with_exact_decimals(server, broker):
    await broker.login()
    ack = await broker.open_position(entry(Side.BUY, size="0.001", stop="30.0"))
    [post] = server.requests("POST", "/positions")
    assert post.raw == (
        '{"epic":"DE40","direction":"BUY","size":0.001,"guaranteedStop":false,"trailingStop":false,'
        '"stopDistance":30.0}'
    )
    assert {"CST", "X-SECURITY-TOKEN", "Content-Type"} <= post.headers and "X-CAP-API-KEY" not in post.headers
    assert post.at == at(0.2) and post.status == 200
    assert ack.deal_reference.startswith("o_") and ack.deal_reference in server.confirms
    pos = only_position(server)
    assert (pos.direction, pos.size, pos.level, pos.stop_level) == ("BUY", Decimal("0.001"), OFFER, OFFER - 30)


@pytest.mark.parametrize(
    ("size", "stop"),
    [("0.001", "2.6"), ("0.010", "30"), ("1E-3", "30.00"), ("12.345", "2.57813"), ("0.002", "1.5E+1")],
)
async def test_open_position_body_decimals_round_trip_exactly(server, broker, size, stop):
    await broker.login()
    await broker.open_position(entry(Side.SELL, size=size, stop=stop))
    [post] = server.requests("POST", "/positions")
    sent = json.loads(post.raw, parse_float=Decimal, parse_int=Decimal)
    assert f'"size":{format(Decimal(size), "f")},' in post.raw
    assert f'"stopDistance":{format(Decimal(stop), "f")}}}' in post.raw
    assert (sent["size"], sent["stopDistance"]) == (Decimal(size), Decimal(stop))
    assert sent["direction"] == "SELL"


async def test_orders_respect_100ms_spacing(server, make_broker, monkeypatch):
    monkeypatch.setattr(capitalcom, "GENERAL_MIN_INTERVAL_S", 0.0)  # widać sam limit zleceń
    broker = make_broker()
    await broker.login()
    await broker.open_position(entry())
    await broker.open_position(entry())
    assert [r.at for r in server.requests("POST", "/positions")] == [at(0), at(0.1)]


async def test_concurrent_orders_are_spaced_100ms_in_call_order(server, make_broker, monkeypatch, capital_sent):
    monkeypatch.setattr(capitalcom, "GENERAL_MIN_INTERVAL_S", 0.0)
    broker = make_broker()
    await broker.login()
    await asyncio.gather(*(broker.open_position(entry()) for _ in range(3)))
    # chwila WYSYŁKI (FakeClock: uśpienia innych zadań przesuwają czas, zanim żądanie dotrze do serwera)
    assert [t for m, p, t in capital_sent if (m, p) == ("POST", "/positions")] == [at(0), at(0.1), at(0.2)]
    assert server.count("POST", "/positions") == 3


async def test_applied_then_dropped_open_is_a_timeout_and_is_never_resent(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    server.apply_then_drop_response("/positions", method="POST")
    with pytest.raises(BrokerTimeout) as info:
        await broker.open_position(entry())
    assert str(info.value) == "POST /positions: no response within 0.1 s"
    assert server.count("POST", "/positions") == 1  # brak automatycznej powtórki
    assert len(server.positions) == 1  # broker wykonał zlecenie mimo braku odpowiedzi
    # adapter nie zna referencji, więc nie ma z czego wyliczyć stopu: None (silnik ustawi stop sam)
    [listed] = await broker.list_positions()
    assert (listed.deal_id, listed.stop_level, listed.stop_derived) == (only_position(server).deal_id, None, False)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_on_open_is_never_retried(server, broker, status):
    await broker.login()
    server.fail_next("/positions", status, {"errorCode": "error.internal"}, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.open_position(entry())
    assert type(info.value) is BrokerError and str(info.value) == f"POST /positions: HTTP {status} error.internal"
    assert server.count("POST", "/positions") == 1 and server.positions == {}


async def test_connection_error_on_open_is_never_retried(server, broker):
    await broker.login()
    await server.close()  # serwer znika w trakcie: połączenie odrzucone
    with pytest.raises(BrokerError) as info:
        await broker.open_position(entry())
    assert type(info.value) is BrokerError and str(info.value).startswith("POST /positions: connection failed (")
    assert server.count("POST", "/positions") <= 1


async def test_429_on_open_is_not_retried(server, broker):
    await broker.login()
    server.fail_next("/positions", 429, {"errorCode": "error.too-many.requests"}, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.open_position(entry())
    assert type(info.value) is BrokerError and "HTTP 429" in str(info.value)
    assert server.count("POST", "/positions") == 1 and server.positions == {}


async def test_401_on_open_relogs_in_once_and_retries_once(server, broker):
    await broker.login()
    server.fail_next("/positions", 401, {"errorCode": "error.invalid.session.token"}, method="POST")
    ack = await broker.open_position(entry())
    assert [r.status for r in server.requests("POST", "/positions")] == [401, 200]
    assert server.count("POST", "/session") == 2
    assert len(server.positions) == 1 and ack.deal_reference in server.confirms


async def test_second_401_on_open_is_session_expired_and_nothing_is_created(server, broker):
    await broker.login()
    for _ in range(2):
        server.fail_next("/positions", 401, {"errorCode": "error.invalid.session.token"}, method="POST")
    with pytest.raises(SessionExpired):
        await broker.open_position(entry())
    assert server.count("POST", "/positions") == 2 and server.positions == {}


async def test_rejection_with_error_code_is_broker_rejected_with_that_reason(server, broker):
    await broker.login()
    server.fail_next("/positions", 400, {"errorCode": "error.invalid.stoploss.minvalue: 25779.7"}, method="POST")
    with pytest.raises(BrokerRejected) as info:
        await broker.open_position(entry())
    assert info.value.reason == "error.invalid.stoploss.minvalue: 25779.7"
    assert server.count("POST", "/positions") == 1


@pytest.mark.parametrize(
    ("size", "stop", "reason"),
    [
        ("0.0005", "30", "error.invalid.size.minvalue: 0.001"),
        ("0.0015", "30", "error.invalid.size.increment"),
        ("0.001", "1.0", "error.invalid.stoploss.minvalue: 2.57813"),
    ],
)
async def test_server_validation_of_size_and_stop_is_broker_rejected(server, broker, size, stop, reason):
    await broker.login()
    with pytest.raises(BrokerRejected) as info:
        await broker.open_position(entry(size=size, stop=stop))
    assert info.value.reason == reason and server.positions == {}


async def test_market_closed_on_open_is_market_closed(server, broker):
    await broker.login()
    server.set_market_closed(True)
    with pytest.raises(MarketClosed) as info:
        await broker.open_position(entry())
    assert str(info.value) == f"POST /positions: HTTP 400 {MARKET_CLOSED_CODE}"
    assert server.positions == {}


@pytest.mark.parametrize(
    ("code", "closed"),
    [
        ("error.market.closed", True),
        ("error.invalid.market.closed", True),
        ("MARKET_CLOSED", True),
        ("error.instrument.market-is-closed", True),
        ("error.position.closed", False),
        ("error.market.unavailable", False),
        ("error.invalid.stoploss.minvalue: 1", False),
    ],
)
async def test_only_error_codes_naming_a_closed_market_are_market_closed(server, broker, code, closed):
    assert is_market_closed_code(code) is closed
    await broker.login()
    server.fail_next("/positions", 400, {"errorCode": code}, method="POST")
    with pytest.raises(BrokerRejected if not closed else MarketClosed) as info:
        await broker.open_position(entry())
    if not closed:
        assert info.value.reason == code


@pytest.mark.parametrize(
    "req",
    [
        entry(epic="DE/40"),
        entry(epic=""),
        entry(size="0"),
        entry(size="-0.001"),
        entry(stop="0"),
        entry(stop="-30"),
    ],
)
async def test_open_position_validates_the_request_before_sending(server, broker, req):
    await broker.login()
    with pytest.raises(ValueError):
        await broker.open_position(req)
    assert server.count("POST", "/positions") == 0


async def test_open_position_requires_an_entry_request(server, broker):
    await broker.login()
    with pytest.raises(TypeError):
        await broker.open_position({"epic": "DE40"})
    assert server.count("POST", "/positions") == 0


@pytest.mark.parametrize(
    "reply",
    [{}, {"dealReference": ""}, {"dealReference": 7}, {"dealReference": "o_a/../b"}, {"dealReference": None}],
)
async def test_open_response_without_a_usable_reference_is_a_broker_error(server, broker, reply):
    await broker.login()
    server.fail_next("/positions", 200, reply, method="POST")
    with pytest.raises(BrokerError) as info:
        await broker.open_position(entry())
    assert type(info.value) is BrokerError and str(info.value).startswith("POST /positions: unexpected response")


# --- confirm -------------------------------------------------------------------------------------------


async def test_confirm_returns_the_fill_snapshot_built_from_confirm_and_position(server, broker):
    await broker.login()
    ack = await broker.open_position(entry(Side.BUY))
    fake = only_position(server)
    mark = since_mark(server)
    pos = await broker.confirm(ack.deal_reference)
    assert pos == OpenPosition(
        fake.deal_id, "DE40", Side.BUY, Decimal("0.001"), OFFER, OFFER - 30, at(0.2), stop_derived=True
    )
    assert pos.deal_id != server.confirms[ack.deal_reference]["dealId"]  # id pozycji, nie zlecenia (C2)
    assert seq(server, mark) == [f"GET /confirms/{ack.deal_reference}", f"GET /positions/{fake.deal_id}"]
    assert_no_float(pos)


async def test_confirm_of_a_sell_derives_the_stop_above_the_fill(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker, Side.SELL, stop="25.5")
    assert (pos.side, pos.open_level, pos.stop_level, pos.stop_derived) == (Side.SELL, BID, BID + Decimal("25.5"), True)


@pytest.mark.parametrize(("keys", "stop", "derived"), [("present", OFFER - 30, False), ("null", None, False)])
async def test_confirm_uses_the_broker_stop_fields_when_they_are_present(server, broker, keys, stop, derived):
    server.position_stop_keys = keys
    await broker.login()
    _, pos = await open_confirmed(broker)
    assert (pos.stop_level, pos.stop_derived) == (stop, derived)


async def test_confirm_never_reads_the_local_date_field(server, broker):
    await broker.login()
    ack = await broker.open_position(entry())
    assert server.confirms[ack.deal_reference]["date"] == "2026-11-02T08:30:00.200"  # lokalne (+1 h)
    assert (await broker.confirm(ack.deal_reference)).opened_at == at(0.2)


async def test_confirm_snapshot_stays_after_the_position_is_closed_without_new_requests(server, broker):
    await broker.login()
    ref, pos = await open_confirmed(broker)
    server.trigger_stop(pos.deal_id, "25751.0")
    mark = since_mark(server)
    assert await broker.confirm(ref) == pos
    assert seq(server, mark) == []  # migawka z pamięci: wynik zlecenia się nie zmienia


async def test_confirm_after_restart_of_a_closed_position_takes_opened_at_from_history(
    server, broker, make_broker, clock
):
    await broker.login()
    ref, pos = await open_confirmed(broker)
    clock.advance(30)
    server.trigger_stop(pos.deal_id, "25751.0")
    restarted = await restart(make_broker, clock)
    mark = since_mark(server)
    snapshot = await restarted.confirm(ref)
    # po restarcie adapter nie zna wysłanej odległości stopu: stop nieznany, nie zgadywany
    assert snapshot == dataclasses.replace(pos, stop_level=None, stop_derived=False)
    assert seq(server, mark) == [f"GET /confirms/{ref}", f"GET /positions/{pos.deal_id}", "GET /history/activity"]
    lookup, history = server.log[-2], server.log[-1]
    start, end = parse_api(history.query["from"]), parse_api(history.query["to"])
    assert history.query == {
        "from": history.query["from"],
        "to": history.query["to"],
        "detailed": "true",
        "dealId": pos.deal_id,
    }
    assert end - start == DAY and lookup.at <= end <= history.at  # jedno okno dobowe aż do chwili odczytu


async def test_confirm_of_a_closed_position_without_its_opening_in_history_is_a_broker_error(
    server, broker, make_broker, clock
):
    await broker.login()
    ref, pos = await open_confirmed(broker)
    server.trigger_stop(pos.deal_id, "25751.0")
    server.activities.clear()
    restarted = await restart(make_broker, clock)
    with pytest.raises(BrokerError) as info:
        await restarted.confirm(ref)
    assert type(info.value) is BrokerError and "opening" in str(info.value)


async def test_confirm_after_restart_of_an_open_position_has_no_derived_stop(server, broker, make_broker, clock):
    await broker.login()
    ref, pos = await open_confirmed(broker)
    restarted = await restart(make_broker, clock)
    expected = dataclasses.replace(pos, stop_level=None, stop_derived=False)
    assert await restarted.confirm(ref) == expected
    assert await restarted.list_positions() == [expected]


async def test_confirm_of_an_unknown_reference_is_none(server, broker):
    await broker.login()
    assert await broker.confirm("o_does-not-exist") is None
    assert server.log[-1].status == 404


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {}),
        (404, {"errorCode": "error.not-found.dealId"}),
        (404, {"errorCode": "error.not-found.dealReference.extra"}),
        (400, {"errorCode": "error.not-found.dealReference"}),
        (400, {"errorCode": "error.invalid.dealReference"}),
        (500, {"errorCode": "error.not-found.dealReference"}),
        (503, None),
    ],
)
async def test_confirm_is_none_only_for_the_documented_404_and_never_rejected_otherwise(server, broker, status, body):
    await broker.login()
    server.fail_next("/confirms/o_ref-1", status, body)
    with pytest.raises(BrokerError) as info:
        await broker.confirm("o_ref-1")
    assert type(info.value) is BrokerError  # nigdy None, nigdy BrokerRejected


async def test_confirm_timeout_is_broker_timeout_not_none(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    server.delay_next("/confirms/o_ref-1", 2.0)
    with pytest.raises(BrokerTimeout):
        await broker.confirm("o_ref-1")


async def test_confirm_of_a_rejected_deal_raises_broker_rejected_with_its_reason(server, broker):
    await broker.login()
    server.reject_next_order("INSUFFICIENT_FUNDS")
    ack = await broker.open_position(entry())
    with pytest.raises(BrokerRejected) as info:
        await broker.confirm(ack.deal_reference)
    assert info.value.reason == "INSUFFICIENT_FUNDS" and server.positions == {}
    with pytest.raises(BrokerRejected):  # nic nie jest zapamiętywane: kolejne potwierdzenie pyta brokera ponownie
        await broker.confirm(ack.deal_reference)
    assert server.count("GET", f"/confirms/{ack.deal_reference}") == 2


@pytest.mark.parametrize(("reason", "expected"), [(None, "REJECTED"), ("", "REJECTED"), (17, "REJECTED")])
async def test_rejected_deal_without_a_text_reason_is_still_rejected(server, broker, reason, expected):
    await broker.login()
    body = {"dealStatus": "REJECTED", "dealReference": "o_ref-1", "status": "REJECTED"}
    if reason is not None:
        body["reason"] = reason
    server.fail_next("/confirms/o_ref-1", 200, body)
    with pytest.raises(BrokerRejected) as info:
        await broker.confirm("o_ref-1")
    assert info.value.reason == expected


def accepted(ref: str = "o_ref-1", **changes) -> dict:
    body = {
        "date": "2026-11-02T08:30:00.200",
        "status": "OPEN",
        "dealStatus": "ACCEPTED",
        "epic": "DE40",
        "dealReference": ref,
        "dealId": "006011e7-0001-54c4-0000-000080560040",
        "affectedDeals": [{"dealId": "006011e7-0001-54c4-0000-000080560042", "status": "OPENED"}],
        "level": OFFER,
        "size": Decimal("0.001"),
        "direction": "BUY",
        "guaranteedStop": False,
        "trailingStop": False,
    }
    for key, value in changes.items():
        if value is ...:
            body.pop(key)
        else:
            body[key] = value
    return body


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"dealStatus": None},
        {"dealStatus": 1},
        {"dealStatus": "PENDING"},
        {"dealStatus": "accepted"},
        accepted(dealStatus="UNKNOWN"),
        accepted(dealStatus="REJECTED", dealReference="o_someone-else"),  # cudze potwierdzenie: nie odrzucenie
        accepted(dealReference="o_someone-else"),
        accepted(affectedDeals=[]),
        accepted(affectedDeals=...),
        accepted(affectedDeals=[{"dealId": "D-1", "status": "AMENDED"}]),  # agregacja z inną pozycją (C15)
        accepted(affectedDeals=[{"dealId": "D-1", "status": "OPENED"}, {"dealId": "D-2", "status": "OPENED"}]),
        accepted(affectedDeals=[{"dealId": "D/../1", "status": "OPENED"}]),
        accepted(level=...),
        accepted(level="25782.3"),
        accepted(level=Decimal("0")),
        accepted(size=Decimal("-0.001")),
        accepted(direction="LONG"),
        accepted(epic=...),
    ],
)
async def test_confirm_with_an_unexpected_status_or_shape_is_a_broker_error_never_a_rejection(server, broker, body):
    await broker.login()
    server.fail_next("/confirms/o_ref-1", 200, body)
    mark = since_mark(server)
    with pytest.raises(BrokerError) as info:
        await broker.confirm("o_ref-1")
    assert type(info.value) is BrokerError and str(info.value).startswith("GET /confirms/o_ref-1")
    assert seq(server, mark) == ["GET /confirms/o_ref-1"]  # odrzucone na samym potwierdzeniu, bez dalszych zgadywań


@pytest.mark.parametrize(
    "other",
    [
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff", "status": "AMENDED"},  # netting z inną pozycją (C15)
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff", "status": "PARTIALLY_CLOSED"},
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff", "status": "FULLY_CLOSED"},
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff", "status": "DELETED"},
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff", "status": None},
        {"dealId": "006011e7-0001-54c4-0000-0000805600ff"},
        "OUR_DEAL_AMENDED",  # drugi wpis naszego dealId
    ],
    ids=["amended", "partially-closed", "fully-closed", "deleted", "null-status", "no-status", "ours-twice"],
)
async def test_open_confirm_with_another_affected_deal_is_not_a_clean_fill(server, broker, other):
    # zlecenie dotknęło także innej pozycji (np. zmniejszyło przeciwną przy wyłączonym hedgingu): to nie jest czyste
    # wykonanie, więc BrokerError (UNKNOWN), a decyzję podejmują rozstrzyganie i uzgadnianie, nie zgadywanie adaptera
    await broker.login()
    ack = await broker.open_position(entry())
    fake = only_position(server)
    confirmation = server.confirms[ack.deal_reference]
    assert confirmation["affectedDeals"] == [{"dealId": fake.deal_id, "status": "OPENED"}]
    extra = {"dealId": fake.deal_id, "status": "AMENDED"} if other == "OUR_DEAL_AMENDED" else other
    confirmation["affectedDeals"].append(extra)
    mark = since_mark(server)
    for _ in range(2):  # nic nie jest zapamiętywane: kolejne potwierdzenie pyta brokera ponownie
        with pytest.raises(BrokerError) as info:
            await broker.confirm(ack.deal_reference)
        assert type(info.value) is BrokerError
        assert str(info.value).startswith(f"GET /confirms/{ack.deal_reference}: unexpected response: affectedDeals")
    assert seq(server, mark) == [f"GET /confirms/{ack.deal_reference}"] * 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epic", "US100"),
        ("direction", "SELL"),
        ("workingOrderId", "006011e7-0001-54c4-0000-0000805600ff"),
        ("size", Decimal("0.002")),  # pozycja większa niż wykonanie: zlecenie dołączyło do innej (C15)
        ("size", Decimal("0.0005")),  # mniejsza: część już zamknięta albo inne wykonanie
    ],
)
async def test_confirm_cross_checks_the_listed_position(server, broker, field, value):
    await broker.login()
    ack = await broker.open_position(entry())
    fake = only_position(server)
    item = server._position_item(fake)
    (item["market"] if field == "epic" else item["position"])[field] = value
    server.fail_next(f"/positions/{fake.deal_id}", 200, item, method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    assert type(info.value) is BrokerError and "does not match the confirmation" in str(info.value)


# Liczba z wykładnikiem poza zakresem `Decimal` (InvalidOperation przy parsowaniu) albo poza zakresem działań (Overflow
# przy pierwszym mnożeniu): wynik zawsze `BrokerError` (zlecenie: UNKNOWN), nigdy surowy wyjątek arytmetyczny.
HUGE_NUMBERS = ["1e99999999999999999999", "-1e-99999999999999999999", "1E+99999999999999999999", "1e999999999"]


@pytest.mark.parametrize("number", HUGE_NUMBERS)
@pytest.mark.parametrize("field", ["size", "level", "stopLevel"])
async def test_position_list_and_snapshot_with_a_huge_number_are_broker_errors(server, broker, number, field):
    server.position_stop_keys = "present"
    await broker.login()
    ack = await broker.open_position(entry())
    fake = only_position(server)
    listing = {"positions": [server._position_item(fake)]}
    marker = Decimal("7.7777")
    listing["positions"][0]["position"][field] = marker
    server.fail_next("/positions", 200, text=fake_dumps(listing).replace("7.7777", number), method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.list_positions()
    assert type(info.value) is BrokerError and info.value.__context__ is None
    item = server._position_item(fake)
    item["position"][field] = marker  # ta sama pozycja przy potwierdzeniu: kontrola krzyżowa `GET /positions/{dealId}`
    server.fail_next(f"/positions/{fake.deal_id}", 200, text=fake_dumps(item).replace("7.7777", number), method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    assert type(info.value) is BrokerError and info.value.__context__ is None


@pytest.mark.parametrize("number", HUGE_NUMBERS)
@pytest.mark.parametrize("field", ["level", "size", "stopLevel"])
async def test_confirm_body_with_a_huge_number_is_a_broker_error(server, broker, number, field):
    server.confirm_stop_keys = True
    await broker.login()
    ack = await broker.open_position(entry())
    payload = dict(server.confirms[ack.deal_reference])
    payload[field] = Decimal("7.7777")
    server.fail_next(f"/confirms/{ack.deal_reference}", 200, text=fake_dumps(payload).replace("7.7777", number))
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    assert type(info.value) is BrokerError and info.value.__context__ is None
    assert (await broker.confirm(ack.deal_reference)).deal_id == only_position(server).deal_id  # nic nie zapamiętano


@pytest.mark.parametrize("listed", [Decimal("0.002"), Decimal("0.0005")])
async def test_confirm_size_mismatch_message_states_both_sizes(server, broker, listed):
    # surowa odpowiedź `GET /positions/{dealId}` nie trafia do dziennika, więc komunikat sam musi pokazać obie wielkości
    # (właściciel czyta je z alarmu przy pierwszym smoke), i nic więcej (żadnych identyfikatorów ani sekretów)
    await broker.login()
    ack = await broker.open_position(entry())
    fake = only_position(server)
    item = server._position_item(fake)
    item["position"]["size"] = listed
    server.fail_next(f"/positions/{fake.deal_id}", 200, item, method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    message = str(info.value)
    assert type(info.value) is BrokerError
    assert message.endswith(f"does not match the confirmation (size: confirm 0.001, position {listed})")
    assert not any(secret in message for secret in server_secrets(server))


async def test_confirm_accepts_a_position_without_working_order_id(server, broker):
    await broker.login()
    ack = await broker.open_position(entry())
    fake = only_position(server)
    item = server._position_item(fake)
    del item["position"]["workingOrderId"]
    server.fail_next(f"/positions/{fake.deal_id}", 200, item, method="GET")
    assert (await broker.confirm(ack.deal_reference)).deal_id == fake.deal_id


@pytest.mark.parametrize("ref", ["", "o_a/b", "..", ".", "o_ref?x=1", "o_ref#x", "o ref", 7, None])
async def test_confirm_refuses_an_unsafe_reference_without_a_request(server, broker, ref):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises((ValueError, TypeError)):
        await broker.confirm(ref)
    assert seq(server, mark) == []


# --- list_positions --------------------------------------------------------------------------------------


async def test_list_positions_matches_the_confirm_snapshot_exactly(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    listed = await broker.list_positions()
    assert listed == [pos] and listed[0].stop_derived is True
    assert_no_float(listed)


@pytest.mark.parametrize(
    ("keys", "stop", "derived"),
    [("absent", OFFER - 30, True), ("present", OFFER - 30, False), ("null", None, False)],
)
async def test_list_positions_stop_fields(server, broker, keys, stop, derived):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.position_stop_keys = keys  # klucze obecne → wartość brokera; jawny null → brak stopu; brak → nasz zapis
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (stop, derived)


async def test_broker_reported_stop_wins_over_our_record(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    only_position(server).stop_level = Decimal("25700.0")  # np. zmieniony ręcznie w aplikacji
    server.position_stop_keys = "present"
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (Decimal("25700.0"), False)


async def test_stop_distance_key_alone_gives_the_stop_from_the_broker_level(server, broker):
    await broker.login()
    ack = await broker.open_position(entry(Side.SELL))
    fake = only_position(server)
    item = server._position_item(fake)
    item["position"]["stopDistance"] = Decimal("30")
    server.fail_next("/positions", 200, {"positions": [item]}, method="GET")
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (BID + 30, False)
    assert ack.deal_reference


async def test_list_positions_after_restart_has_no_record_of_our_stop(server, broker, make_broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    restarted = await restart(make_broker, clock)
    assert await restarted.list_positions() == [dataclasses.replace(pos, stop_level=None, stop_derived=False)]


async def test_list_positions_returns_foreign_positions_of_the_active_account_only(server, broker, clock):
    created = at(-3600)
    server.positions["FOREIGN-1"] = FakePosition(
        "FOREIGN-1", "ORDER-1", "ACC-FUNDED", "US100", "SELL", Decimal("0.01"), Decimal("28401.8"), created, None
    )
    server.positions["OTHER-ACC"] = FakePosition(
        "OTHER-ACC", "ORDER-2", "ACC-EMPTY", "DE40", "BUY", Decimal("1"), OFFER, created, None
    )
    server.markets["US100"] = server.markets["DE40"]
    await broker.login()
    assert await broker.list_positions() == [
        OpenPosition("FOREIGN-1", "US100", Side.SELL, Decimal("0.01"), Decimal("28401.8"), None, created)
    ]


async def test_empty_position_list_is_valid(server, broker):
    await broker.login()
    assert await broker.list_positions() == []


def position_item(**changes) -> dict:
    item = {
        "position": {
            "contractSize": 1,
            "createdDate": "2026-11-02T08:05:03.100",
            "createdDateUTC": "2026-11-02T07:05:03.100",
            "dealId": "D-1",
            "dealReference": "p_D-1",
            "workingOrderId": "W-1",
            "size": Decimal("0.001"),
            "leverage": 20,
            "upl": Decimal("-0.002"),
            "direction": "BUY",
            "level": OFFER,
            "currency": "EUR",
            "guaranteedStop": False,
        },
        "market": {"instrumentName": "Germany 40", "epic": "DE40", "bid": BID, "offer": OFFER},
    }
    for key, value in changes.items():
        part, name = key.split("__")
        if value is ...:
            item[part].pop(name)
        else:
            item[part][name] = value
    return item


def test_position_row_requires_the_stop_level_reported_flag():
    # brak domyślnej wartości: każdy, kto buduje wiersz, musi powiedzieć, czy broker podał `stopLevel` wprost (C6)
    fields = dict(
        deal_id="D-1", working_order_id=None, epic="DE40", side=Side.BUY, size=Decimal("0.001"), level=OFFER,
        opened_at=at(0.2), stop_reported=True, stop_level=OFFER - 30,
    )  # fmt: skip
    with pytest.raises(TypeError, match="stop_level_reported"):
        capitalcom_parse.PositionRow(**fields)
    assert capitalcom_parse.PositionRow(**fields, stop_level_reported=True).stop_level_reported is True


async def test_list_positions_parses_the_documented_fields_exactly(server, broker):
    await broker.login()
    server.fail_next("/positions", 200, {"positions": [position_item()]}, method="GET")
    assert await broker.list_positions() == [
        OpenPosition("D-1", "DE40", Side.BUY, Decimal("0.001"), OFFER, None, datetime(2026, 11, 2, 7, 5, 3, 100000, UTC))
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"positions": None},
        {"positions": {}},
        {"positions": [position_item(market__epic=...)]},
        {"positions": [position_item(position__dealId=...)]},
        {"positions": [position_item(position__dealId="D/1")]},
        {"positions": [position_item(position__direction="LONG")]},
        {"positions": [position_item(position__size="0.001")]},
        {"positions": [position_item(position__size=Decimal("0"))]},
        {"positions": [position_item(position__level=...)]},
        {"positions": [position_item(position__createdDateUTC=...)]},
        {"positions": [position_item(position__createdDateUTC="2026-11-02T07:05:03.100Z")]},
        {"positions": [position_item(position__stopLevel="25750.0")]},
        {"positions": [position_item(position__stopDistance=Decimal("-30"))]},
        {"positions": [position_item(), position_item()]},  # ten sam dealId dwa razy
    ],
)
async def test_malformed_position_lists_are_broker_errors(server, broker, payload):
    await broker.login()
    server.fail_next("/positions", 200, payload, method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.list_positions()
    assert type(info.value) is BrokerError and str(info.value).startswith("GET /positions")


# --- set_stop --------------------------------------------------------------------------------------------


async def test_set_stop_puts_the_level_and_confirms_by_the_returned_reference(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    mark = since_mark(server)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert seq(server, mark) == [
        f"PUT /positions/{pos.deal_id}",
        f"GET /confirms/p_{pos.deal_id}",
        f"GET /positions/{pos.deal_id}",  # kontrola: stop pozycji (gdy broker go pokazuje) równy żądanemu
    ]
    assert server.log[mark].raw == '{"stopLevel":25742.3}'
    assert only_position(server).stop_level == Decimal("25742.3")
    [moved] = await broker.list_positions()
    assert (moved.stop_level, moved.stop_derived) == (Decimal("25742.3"), True)  # z naszego potwierdzonego PUT


async def test_set_stop_on_a_position_we_did_not_open_is_remembered(server, broker, make_broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    restarted = await restart(make_broker, clock)
    await restarted.set_stop(pos.deal_id, Decimal("25742.3"))
    [listed] = await restarted.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (Decimal("25742.3"), True)


async def test_set_stop_of_a_gone_deal_is_position_not_found_without_confirm(server, broker):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises(BrokerRejected) as info:
        await broker.set_stop("D-gone", Decimal("25700"))
    assert info.value.reason == "POSITION_NOT_FOUND"
    assert seq(server, mark) == ["PUT /positions/D-gone"]


async def test_set_stop_too_close_to_the_market_is_rejected_with_the_broker_code(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    with pytest.raises(BrokerRejected) as info:
        await broker.set_stop(pos.deal_id, BID)
    assert info.value.reason == "error.invalid.stoploss.maxvalue: 25777.72187"
    [listed] = await broker.list_positions()
    assert listed.stop_level == pos.stop_level


async def test_set_stop_rejected_on_confirm_keeps_the_old_stop(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "REJECTED", "reason": "STOP_TOO_CLOSE", "dealReference": ref})
    with pytest.raises(BrokerRejected) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert info.value.reason == "STOP_TOO_CLOSE"
    [listed] = await broker.list_positions()
    assert listed.stop_level == pos.stop_level


@pytest.mark.parametrize(
    "failure",
    [
        (404, {"errorCode": "error.not-found.dealReference"}),
        (500, {"errorCode": "error.internal"}),
        (200, {"dealStatus": "PENDING"}),
        (200, {"dealStatus": "ACCEPTED", "dealReference": "p_someone-else"}),
        "timeout",
    ],
)
async def test_set_stop_with_an_unknown_outcome_is_broker_timeout(server, make_broker, failure):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    if failure == "timeout":
        server.delay_next(f"/confirms/{ref}", 2.0)
    else:
        server.fail_next(f"/confirms/{ref}", *failure)
    with pytest.raises(BrokerTimeout) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert "stop outcome unknown" in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    [listed] = await broker.list_positions()
    assert listed.stop_level == pos.stop_level  # wynik nieznany: zapis stopu bez zmian, silnik ponowi


@pytest.mark.parametrize("level", [25742.3, "25742.3", Decimal("NaN"), Decimal("0"), Decimal("-1"), None])
async def test_set_stop_validates_the_level_before_sending(server, broker, level):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises((TypeError, ValueError)):
        await broker.set_stop("D-1", level)
    assert seq(server, mark) == []


async def test_set_stop_market_closed_and_5xx_are_mapped(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/positions/{pos.deal_id}", 400, {"errorCode": MARKET_CLOSED_CODE}, method="PUT")
    with pytest.raises(MarketClosed):
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    server.fail_next(f"/positions/{pos.deal_id}", 503, None, method="PUT")
    with pytest.raises(BrokerError) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert type(info.value) is BrokerError and str(info.value) == f"PUT /positions/{pos.deal_id}: HTTP 503"


# --- close_position ----------------------------------------------------------------------------------------


async def test_close_is_delete_by_deal_id_then_confirm_by_the_returned_reference(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    mark = since_mark(server)
    assert await broker.close_position(pos.deal_id) is None
    assert seq(server, mark) == [
        f"DELETE /positions/{pos.deal_id}",
        f"GET /confirms/p_{pos.deal_id}",
        f"GET /positions/{pos.deal_id}",  # kontrola: pozycji już nie ma (404)
    ]
    delete = server.log[mark]
    assert delete.raw is None and delete.query == {}  # bez treści i bez wielkości: zamknięcie w całości (C22)
    assert server.positions == {} and server.count("POST", "/positions") == 1  # nigdy przeciwne zlecenie


async def test_second_close_of_a_gone_deal_is_position_not_found_and_changes_nothing(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.close_position(pos.deal_id)
    rows, confirms = list(server.activities), dict(server.confirms)
    mark = since_mark(server)
    with pytest.raises(BrokerRejected) as info:
        await broker.close_position(pos.deal_id)
    assert info.value.reason == "POSITION_NOT_FOUND"
    assert seq(server, mark) == [f"DELETE /positions/{pos.deal_id}"]
    assert server.log[-1].status == 404
    assert server.positions == {} and server.activities == rows and server.confirms == confirms
    assert server.count("POST", "/positions") == 1  # żadnej nowej pozycji


async def test_close_rejected_on_confirm_is_broker_rejected(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "REJECTED", "reason": "CLOSE_REFUSED", "dealReference": ref})
    with pytest.raises(BrokerRejected) as info:
        await broker.close_position(pos.deal_id)
    assert info.value.reason == "CLOSE_REFUSED"


@pytest.mark.parametrize(
    "failure",
    [
        (404, {"errorCode": "error.not-found.dealReference"}),
        (500, {"errorCode": "error.internal"}),
        (401, {}),
        (200, {"dealStatus": "PENDING"}),
        (200, {"dealStatus": None}),
        (200, "not json"),
        "timeout",
    ],
)
async def test_close_with_an_unknown_outcome_is_broker_timeout(server, make_broker, failure):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    if failure == "timeout":
        server.delay_next(f"/confirms/{ref}", 2.0)
    elif failure[0] == 401:  # 401, ponowne logowanie odrzucone: SessionExpired w trakcie potwierdzenia
        server.fail_next(f"/confirms/{ref}", 401, {})
        server.fail_next("/session", 401, {"errorCode": "error.invalid.details"}, method="POST")
    elif failure[1] == "not json":
        server.fail_next(f"/confirms/{ref}", 200, text="not json")
    else:
        server.fail_next(f"/confirms/{ref}", *failure)
    with pytest.raises(BrokerTimeout) as info:
        await broker.close_position(pos.deal_id)
    assert "close outcome unknown" in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert server.count("DELETE", f"/positions/{pos.deal_id}") == 1  # nigdy ponownie na ślepo


@pytest.mark.parametrize("reply", [{}, {"dealReference": None}, {"dealReference": "p_a/b"}])
async def test_close_response_without_a_reference_is_an_unknown_outcome(server, broker, reply):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/positions/{pos.deal_id}", 200, reply, method="DELETE")
    with pytest.raises(BrokerTimeout, match="close outcome unknown"):
        await broker.close_position(pos.deal_id)


async def test_applied_then_dropped_close_is_a_timeout_and_is_never_resent(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.apply_then_drop_response(f"/positions/{pos.deal_id}", method="DELETE")
    with pytest.raises(BrokerTimeout):
        await broker.close_position(pos.deal_id)
    assert server.count("DELETE", f"/positions/{pos.deal_id}") == 1 and server.positions == {}


async def test_close_when_the_market_is_closed_is_market_closed_and_the_position_stays(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.set_market_closed(True)
    with pytest.raises(MarketClosed):
        await broker.close_position(pos.deal_id)
    assert list(server.positions) == [pos.deal_id]


@pytest.mark.parametrize("status", [500, 503])
async def test_close_5xx_is_a_broker_error_and_never_retried(server, broker, status):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/positions/{pos.deal_id}", status, None, method="DELETE")
    with pytest.raises(BrokerError) as info:
        await broker.close_position(pos.deal_id)
    assert type(info.value) is BrokerError
    assert server.count("DELETE", f"/positions/{pos.deal_id}") == 1


async def test_close_401_relogs_in_and_deletes_once_more(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/positions/{pos.deal_id}", 401, {"errorCode": "error.invalid.session.token"}, method="DELETE")
    await broker.close_position(pos.deal_id)
    assert [r.status for r in server.requests("DELETE", f"/positions/{pos.deal_id}")] == [401, 200]
    assert server.positions == {}


@pytest.mark.parametrize("deal_id", ["", "D/1", "..", "D?x", 7, None])
async def test_close_refuses_an_unsafe_deal_id_without_a_request(server, broker, deal_id):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises((TypeError, ValueError)):
        await broker.close_position(deal_id)
    assert seq(server, mark) == []


# --- closed_since: okna, kompletność, mapowanie -----------------------------------------------------------


def history_requests(server) -> list[tuple[str, str]]:
    return [(r.query["from"], r.query["to"]) for r in server.requests("GET", "/history/activity")]


async def test_closed_since_splits_a_three_day_range_into_overlapping_windows_of_at_most_one_day(server, broker):
    await broker.login()
    assert await broker.closed_since(T0 - 3 * DAY) == []
    assert history_requests(server) == [
        ("2026-10-30T07:30:00", "2026-10-31T07:30:00"),
        ("2026-10-31T07:29:59", "2026-11-01T07:29:59"),
        ("2026-11-01T07:29:58", "2026-11-02T07:29:58"),
        ("2026-11-02T07:29:57", "2026-11-02T07:30:00"),
    ]
    for r in server.requests("GET", "/history/activity"):
        assert r.query["detailed"] == "true" and set(r.query) == {"from", "to", "detailed"}
        assert parse_api(r.query["to"]) - parse_api(r.query["from"]) <= DAY


def test_history_windows_cover_the_whole_range_with_one_second_overlaps():
    since, now = T0 - timedelta(days=2, seconds=7.25), T0 + timedelta(seconds=0.4)
    windows = history_windows(since, now)
    assert windows[0][0] == T0 - timedelta(days=2, seconds=8) and windows[-1][1] == T0 + timedelta(seconds=1)
    for (a, b), (c, _) in zip(windows, windows[1:]):
        assert b - c == timedelta(seconds=1)
    assert all(b - a <= DAY and a.microsecond == b.microsecond == 0 for a, b in windows)
    assert history_windows(T0, T0) == [(T0, T0 + timedelta(seconds=1))]  # nigdy okno zerowej szerokości


def test_split_window_halves_with_overlap_and_refuses_tiny_windows():
    assert split_window(T0, T0 + DAY) == [(T0, T0 + DAY / 2), (T0 + DAY / 2 - timedelta(seconds=1), T0 + DAY)]
    assert split_window(T0, T0 + timedelta(seconds=4)) == [
        (T0, T0 + timedelta(seconds=2)),
        (T0 + timedelta(seconds=1), T0 + timedelta(seconds=4)),
    ]
    assert split_window(T0, T0 + timedelta(seconds=3)) is None


async def test_closed_since_last_window_reaches_now_and_never_asks_for_the_future(server, broker, clock):
    await broker.login()
    clock.advance(0.4)  # teraz = 07:30:00.4: koniec okna zaokrąglony W GÓRĘ do pełnej sekundy
    await broker.closed_since(T0 - timedelta(hours=1))
    [request] = server.requests("GET", "/history/activity")
    assert request.query["to"] == "2026-11-02T07:30:01" and request.at >= at(1)
    assert request.status == 200  # serwer testowy odrzuca `to` z przyszłości


async def test_closed_since_refuses_more_than_fourteen_days_before_any_request(server, broker):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(days=14, seconds=1))
    assert type(info.value) is BrokerError and str(info.value).startswith("HISTORY_RANGE_TOO_LARGE")
    assert seq(server, mark) == []


async def test_closed_since_serves_exactly_fourteen_days_in_full(server, broker):
    await broker.login()
    server.add_activity(activity("D-old", T0 - timedelta(days=13, hours=23), source="SL", level="25000.0"))
    closed = await broker.closed_since(T0 - timedelta(days=14))
    assert [c.deal_id for c in closed] == ["D-old"]
    assert len(server.requests("GET", "/history/activity")) == 15


@pytest.mark.parametrize("nth", [1, 2, 3, 4])
async def test_a_failing_window_raises_and_never_returns_a_partial_list(server, broker, nth):
    await broker.login()
    for day in range(3):
        server.add_activity(activity(f"D-{day}", T0 - timedelta(days=day, hours=1), source="SL", level="25000.0"))
    server.fail_request("/history/activity", nth, 500, {"errorCode": "error.internal"})
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - 3 * DAY)
    assert type(info.value) is BrokerError and str(info.value) == "GET /history/activity: HTTP 500 error.internal"
    assert len(server.requests("GET", "/history/activity")) == nth  # dalej nie pyta, nic nie zwraca


async def test_a_window_timing_out_mid_way_raises_broker_timeout(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    server.add_activity(activity("D-1", T0 - timedelta(days=1, hours=12), source="SL"))
    server.delay_next("/history/activity", 0.0)  # pierwsze okno odpowiada od razu
    server.delay_next("/history/activity", 2.0)  # drugie nie odpowiada w czasie
    with pytest.raises(BrokerTimeout):
        await broker.closed_since(T0 - 2 * DAY)
    assert len(server.requests("GET", "/history/activity")) == 2


async def test_a_truncated_window_is_split_until_every_part_is_complete(server, broker, monkeypatch):
    monkeypatch.setattr(capitalcom_history, "ACTIVITY_SUSPECT_ROWS", 3)
    server.activity_limit(3)  # odpowiedź z 3 wierszami może być obcięta: adapter dzieli okno
    stamps = [T0 - timedelta(hours=h) for h in (20, 15, 9, 5, 1)]
    for i, moment in enumerate(stamps):
        server.add_activity(activity(f"D-{i}", moment, source="SL", level=f"2500{i}.0"))
    await broker.login()
    closed = await broker.closed_since(T0 - timedelta(hours=23))
    assert [(c.deal_id, c.exit_level, c.ts) for c in closed] == [
        (f"D-{i}", Decimal(f"2500{i}.0"), moment) for i, moment in enumerate(stamps)
    ]
    windows = history_requests(server)
    assert len(windows) > 1 and windows[0] == ("2026-11-01T08:30:00", "2026-11-02T07:30:00")


async def test_a_truncated_window_that_cannot_be_split_raises(server, broker, monkeypatch):
    monkeypatch.setattr(capitalcom_history, "ACTIVITY_SUSPECT_ROWS", 3)
    server.activity_limit(3)
    for i in range(4):  # cztery wiersze w tej samej sekundzie: dzielenie po czasie nie pomoże
        server.add_activity(activity(f"D-{i}", T0 - timedelta(hours=1, milliseconds=-i), source="SL"))
    await broker.login()
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(hours=2))
    assert type(info.value) is BrokerError and str(info.value).startswith("HISTORY_TRUNCATED")


async def test_too_many_history_requests_raise_instead_of_returning_a_partial_list(server, broker, monkeypatch):
    monkeypatch.setattr(capitalcom_history, "ACTIVITY_SUSPECT_ROWS", 1)
    monkeypatch.setattr(capitalcom_history, "MAX_HISTORY_REQUESTS", 5)
    server.activity_limit(1)
    for i in range(8):
        server.add_activity(activity(f"D-{i}", T0 - timedelta(hours=1 + i), source="SL"))
    await broker.login()
    with pytest.raises(BrokerError, match="^HISTORY_TRUNCATED"):
        await broker.closed_since(T0 - timedelta(hours=12))
    assert len(server.requests("GET", "/history/activity")) == 5


async def test_rows_at_a_window_boundary_and_duplicated_rows_count_once(server, broker):
    await broker.login()
    boundary = T0 - DAY - timedelta(seconds=0.5)  # 07:29:59.5: w obu oknach (…07:30:00] i [07:29:59…
    server.add_activity(activity("D-1", boundary, source="SL", level="25001.0"))
    server.add_activity(activity("D-2", T0 - timedelta(hours=2), source="SL", level="25002.0"))
    server.add_activity(activity("D-2", T0 - timedelta(hours=2), source="SL", level="25002.0"))  # duplikat API
    closed = await broker.closed_since(T0 - 2 * DAY)
    assert [(c.deal_id, c.exit_level) for c in closed] == [("D-1", Decimal("25001.0")), ("D-2", Decimal("25002.0"))]
    first, second = (tuple(parse_api(x) for x in w) for w in history_requests(server)[:2])
    assert first[0] <= boundary <= first[1] and second[0] <= boundary <= second[1]  # serwer zwrócił go dwa razy


async def test_distinct_closures_of_one_deal_are_all_kept(server, broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    clock.advance(150)
    server.add_activity(activity(pos.deal_id, at(60), source="USER", level="25790.0", size="0.0005"))  # częściowe
    server.add_activity(activity(pos.deal_id, at(120), source="SL", level="25752.3", size="0.0005"))
    closed = await broker.closed_since(pos.opened_at)
    assert closed == [
        ClosedDeal(pos.deal_id, "DE40", Decimal("25790.0"), "MANUAL", at(60)),
        ClosedDeal(pos.deal_id, "DE40", Decimal("25752.3"), "STOP", at(120)),
    ]  # dwa zamknięcia → uzgadnianie zgłasza AMBIGUOUS_CLOSURE


@pytest.mark.parametrize(
    ("source", "reason", "unknown"),
    [
        ("SL", "STOP", 0),
        ("TP", "TARGET", 0),  # cel zysku: osobna przyczyna (zadanie 7); NIEPOTWIERDZONE, że TP = zadziałanie celu (C7)
        ("USER", "MANUAL", 0),
        ("CLOSE_OUT", "FORCED", 0),
        ("DEALER", "FORCED", 0),
        ("SYSTEM", "FORCED", 0),
        ("NEW_SOURCE", "FORCED", 1),
    ],
)
async def test_close_source_maps_to_the_closed_deal_reason(server, broker, clock, source, reason, unknown):
    await broker.login()
    _, pos = await open_confirmed(broker)
    clock.advance(120)
    assert broker.unknown_close_sources == 0
    fake = server.positions.pop(pos.deal_id)
    server.add_activity(activity(pos.deal_id, at(90), source=source, direction="SELL", level="25760.5", size=fake.size))
    closed = await broker.closed_since(pos.opened_at)
    assert closed == [ClosedDeal(pos.deal_id, "DE40", Decimal("25760.5"), reason, at(90))]
    assert broker.unknown_close_sources == unknown  # nieznane źródło: nigdy "STOP", zawsze licznik dla T14


async def test_closed_deal_uses_the_epic_identifier_utc_time_and_exact_decimal_level(server, broker):
    await broker.login()
    moment = datetime(2026, 11, 2, 7, 12, 34, 567000, UTC)
    row = activity("D-9", moment, source="SL", level="25749.85")
    row["details"]["marketName"] = "Germany 40"
    server.add_activity(row)
    [closed] = await broker.closed_since(T0 - timedelta(hours=1))
    assert closed == ClosedDeal("D-9", "DE40", Decimal("25749.85"), "STOP", moment)
    assert row["date"] == "2026-11-02T08:12:34.567"  # pole lokalne (+1 h) nie jest czytane
    assert_no_float(closed)


async def test_api_close_is_reported_as_manual(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.close_position(pos.deal_id)
    [closed] = await broker.closed_since(pos.opened_at)
    assert (closed.deal_id, closed.epic, closed.reason, closed.exit_level) == (pos.deal_id, "DE40", "MANUAL", BID)


async def test_opening_rows_and_other_activity_types_are_not_closures(server, broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))  # EDIT_STOP_AND_LIMIT
    clock.advance(60)
    for kind in ("SWAP", "WORKING_ORDER", "SYSTEM"):
        server.add_activity(activity(pos.deal_id, at(30), type=kind, source="SYSTEM"))
    for status in ("REJECTED", "MODIFY_REJECT", "CANCEL_REJECT"):  # odrzucona operacja: nic się nie stało
        server.add_activity(activity(pos.deal_id, at(31), status=status, direction="SELL"))
    assert await broker.closed_since(T0) == []
    assert len(server.activities) == 8 and all(parse_api(w[1]) >= at(31) for w in history_requests(server))


@pytest.mark.parametrize("status", ["EXECUTED", "UNKNOWN", "MODIFIED", "CREATED", "SOMETHING_NEW"])
async def test_position_rows_with_an_unexpected_status_raise(server, broker, status):
    await broker.login()
    server.add_activity(activity("D-1", T0 - timedelta(minutes=5), source="SL", status=status))
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(hours=1))
    assert type(info.value) is BrokerError and "status" in str(info.value)


async def test_unknown_activity_types_raise(server, broker):
    await broker.login()
    server.add_activity(activity("D-1", T0 - timedelta(minutes=5), type="TRADE"))
    with pytest.raises(BrokerError, match="type"):
        await broker.closed_since(T0 - timedelta(hours=1))


async def test_rows_before_since_are_not_returned(server, broker):
    await broker.login()
    since = T0 - timedelta(hours=1, seconds=-0.5)  # 06:30:00.5: okno zaczyna się od 06:30:00
    server.add_activity(activity("D-early", T0 - timedelta(hours=1), source="SL"))  # 06:30:00.0 < since
    server.add_activity(activity("D-late", since, source="SL"))
    assert [c.deal_id for c in await broker.closed_since(since)] == ["D-late"]
    assert history_requests(server)[0][0] == "2026-11-02T06:30:00"


async def test_closed_since_validates_since_without_a_request(server, broker):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises(ValueError):
        await broker.closed_since(datetime(2026, 11, 2, 7, 0))
    assert await broker.closed_since(T0 + timedelta(minutes=1)) == []  # początek w przyszłości: nic się nie zamknęło
    assert seq(server, mark) == []


# --- closed_since: rozpoznanie otwarcia i zamknięcia ---------------------------------------------------------


async def test_side_unknown_after_restart_opening_and_api_close_in_range(server, broker, make_broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.close_position(pos.deal_id)
    restarted = await restart(make_broker, clock)
    [closed] = await restarted.closed_since(pos.opened_at - timedelta(minutes=1))
    assert (closed.deal_id, closed.reason, closed.exit_level) == (pos.deal_id, "MANUAL", BID)


async def test_side_unknown_a_stop_out_reveals_the_side_even_without_the_opening(server, broker):
    await broker.login()
    server.add_activity(activity("D-1", T0 - timedelta(days=2), source="USER", direction="BUY", level="25000.0"))
    server.add_activity(activity("D-1", T0 - timedelta(minutes=5), source="SL", direction="SELL", level="24970.0"))
    server.add_activity(activity("D-1", T0 - timedelta(minutes=10), source="USER", direction="SELL", level="24990.0",
                                 size="0.0005"))  # fmt: skip
    closed = await broker.closed_since(T0 - timedelta(hours=1))
    assert [(c.reason, c.exit_level) for c in closed] == [("MANUAL", Decimal("24990.0")), ("STOP", Decimal("24970.0"))]


async def test_lone_user_row_of_a_deal_that_is_not_open_is_ambiguous(server, broker):
    await broker.login()
    server.add_activity(activity("D-1", T0 - timedelta(minutes=5), source="USER", direction="SELL"))
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(hours=1))
    assert type(info.value) is BrokerError and str(info.value).startswith("HISTORY_AMBIGUOUS")
    assert server.count("GET", "/positions") == 1  # zanim zgłosi niejasność, sprawdza otwarte pozycje


async def test_lone_user_row_of_an_open_deal_is_its_opening(server, broker, make_broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)
    restarted = await restart(make_broker, clock)
    assert await restarted.closed_since(T0) == []
    assert server.count("GET", "/positions") == 1  # strona pozycji z listy, nie zgadywana


async def test_two_user_rows_with_the_same_direction_and_no_open_deal_are_ambiguous(server, broker):
    await broker.login()
    for minutes in (10, 5):
        server.add_activity(activity("D-1", T0 - timedelta(minutes=minutes), source="USER", direction="SELL"))
    with pytest.raises(BrokerError, match="^HISTORY_AMBIGUOUS"):
        await broker.closed_since(T0 - timedelta(hours=1))


@pytest.mark.parametrize(
    "rows",
    [
        [("SL", "SELL"), ("CLOSE_OUT", "BUY")],  # dwa zamknięcia systemowe w przeciwnych kierunkach
        [("TP", "BUY"), ("SL", "SELL")],  # take profit i stop w przeciwnych kierunkach
    ],
)
async def test_contradicting_rows_are_inconsistent(server, broker, rows):
    await broker.login()
    for minute, (source, direction) in enumerate(rows):
        server.add_activity(activity("D-1", T0 - timedelta(minutes=10 - minute), source=source, direction=direction))
    with pytest.raises(BrokerError, match="^HISTORY_INCONSISTENT"):
        await broker.closed_since(T0 - timedelta(hours=1))


async def test_known_side_contradicted_by_a_stop_row_is_inconsistent(server, broker, clock):
    await broker.login()
    _, pos = await open_confirmed(broker)  # BUY
    clock.advance(120)
    server.positions.pop(pos.deal_id)
    server.add_activity(activity(pos.deal_id, at(60), source="SL", direction="BUY"))
    with pytest.raises(BrokerError, match="^HISTORY_INCONSISTENT"):
        await broker.closed_since(pos.opened_at)


def activity_reply(*rows) -> dict:
    return {"activities": list(rows)}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("details"),
        lambda r: r["details"].pop("level"),
        lambda r: r["details"].update(level="25750.0"),
        lambda r: r["details"].update(direction="LONG"),
        lambda r: r.update(dateUTC="2026-11-02T07:00:00.000Z"),
        lambda r: r.pop("dateUTC"),
        lambda r: r.pop("dealId"),
        lambda r: r.pop("epic"),
        lambda r: r.pop("type"),
        lambda r: r.update(source=None),
        lambda r: r.update(status=7),
    ],
)
async def test_malformed_activity_rows_are_broker_errors(server, broker, mutate):
    await broker.login()
    row = activity("D-1", T0 - timedelta(minutes=5), source="SL")
    mutate(row)
    server.fail_next("/history/activity", 200, activity_reply(row))
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(hours=1))
    assert type(info.value) is BrokerError and str(info.value).startswith("GET /history/activity")


@pytest.mark.parametrize("payload", [{}, {"activities": None}, {"activities": {}}, {"activities": ["x"]}])
async def test_malformed_activity_payloads_are_broker_errors(server, broker, payload):
    await broker.login()
    server.fail_next("/history/activity", 200, payload)
    with pytest.raises(BrokerError, match="^GET /history/activity"):
        await broker.closed_since(T0 - timedelta(hours=1))


async def test_closures_of_every_epic_are_returned_sorted_by_time(server, broker):
    await broker.login()
    server.add_activity(activity("D-2", T0 - timedelta(minutes=5), source="SL", epic="US100", level="28000.0"))
    server.add_activity(activity("D-1", T0 - timedelta(minutes=50), source="SL", level="25000.0"))
    closed = await broker.closed_since(T0 - timedelta(hours=1))
    assert [(c.deal_id, c.epic) for c in closed] == [("D-1", "DE40"), ("D-2", "US100")]


# --- zlecenia oczekujące ---------------------------------------------------------------------------------


async def test_working_orders_are_parsed_for_the_active_account(server, broker):
    server.markets["US100"] = server.markets["DE40"]
    first = server.add_working_order("DE40", "BUY", Decimal("0.001"), Decimal("25680.3"))
    second = server.add_working_order("US100", "SELL", Decimal("0.01"), Decimal("28500.5"))
    server.add_working_order("DE40", "BUY", Decimal("1"), Decimal("25000"), account="ACC-EMPTY")
    await broker.login()
    orders = await broker.list_working_orders()
    assert orders == [
        WorkingOrder(first, "DE40", Side.BUY, Decimal("0.001"), Decimal("25680.3")),
        WorkingOrder(second, "US100", Side.SELL, Decimal("0.01"), Decimal("28500.5")),
    ]
    assert_no_float(orders)


def working_item(**changes) -> dict:
    data = {"dealId": "W-1", "direction": "BUY", "epic": "DE40", "orderSize": Decimal("1"), "orderLevel": Decimal("20")}
    data.update({k: v for k, v in changes.items() if v is not ...})
    for key in [k for k, v in changes.items() if v is ...]:
        data.pop(key)
    return {"workingOrderData": data, "marketData": {"epic": "DE40"}}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"workingOrders": None},
        {"workingOrders": [{"marketData": {}}]},
        {"workingOrders": [working_item(orderLevel="20")]},
        {"workingOrders": [working_item(orderSize=...)]},
        {"workingOrders": [working_item(direction="LONG")]},
        {"workingOrders": [working_item(dealId=...)]},
        {"workingOrders": [working_item(epic=...)]},
        {"workingOrders": [working_item(), working_item()]},
    ],
)
async def test_malformed_working_orders_are_broker_errors(server, broker, payload):
    await broker.login()
    server.fail_next("/workingorders", 200, payload)
    with pytest.raises(BrokerError) as info:
        await broker.list_working_orders()
    assert type(info.value) is BrokerError and str(info.value).startswith("GET /workingorders")


# --- bez sekretów i bez float na każdej nowej ścieżce ---------------------------------------------------------


def echo(server) -> str:
    return f"error.weird: {TEST_API_KEY} {TEST_PASSWORD} {TEST_IDENTIFIER} {' '.join(server.issued_tokens)}"


async def _scenario_open_rejected_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/positions", 400, {"errorCode": echo(server)}, method="POST")
    await b.open_position(entry())


async def _scenario_open_timeout(server, make_broker):
    b = make_broker(request_timeout_s=0.05)
    await b.login()
    server.apply_then_drop_response("/positions", method="POST")
    await b.open_position(entry())


async def _scenario_confirm_rejected_reason_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.reject_next_order(echo(server))
    ack = await b.open_position(entry())
    await b.confirm(ack.deal_reference)


async def _scenario_confirm_status_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/confirms/o_ref-1", 200, {"dealStatus": server.issued_tokens[-1], "dealReference": "o_ref-1"})
    await b.confirm("o_ref-1")


async def _scenario_positions_malformed(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/positions", 200, {"positions": [{"position": {"dealId": TEST_PASSWORD}}]}, method="GET")
    await b.list_positions()


async def _scenario_set_stop_rejected_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/positions/D-1", 400, {"errorCode": echo(server)}, method="PUT")
    await b.set_stop("D-1", Decimal("25000"))


async def _scenario_close_confirm_rejected_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    _, pos = await open_confirmed(b)
    server.fail_next(f"/confirms/p_{pos.deal_id}", 200, {"dealStatus": "REJECTED", "reason": echo(server)})
    await b.close_position(pos.deal_id)


async def _scenario_close_confirm_failing_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    _, pos = await open_confirmed(b)
    server.fail_next(f"/confirms/p_{pos.deal_id}", 400, {"errorCode": echo(server)})
    await b.close_position(pos.deal_id)


async def _scenario_history_error_echoing_secrets(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_request("/history/activity", 2, 400, {"errorCode": echo(server)})
    await b.closed_since(T0 - 2 * DAY)


async def _scenario_history_ambiguous(server, make_broker):
    b = make_broker()
    await b.login()
    server.add_activity(activity(server.issued_tokens[-1], T0 - timedelta(minutes=5), source="USER"))
    await b.closed_since(T0 - timedelta(hours=1))


async def _scenario_working_orders_malformed(server, make_broker):
    b = make_broker()
    await b.login()
    server.fail_next("/workingorders", 200, {"workingOrders": [working_item(direction=TEST_API_KEY)]})
    await b.list_working_orders()


SECRET_SCENARIOS = {name.removeprefix("_scenario_"): fn for name, fn in globals().items() if name.startswith("_scenario_")}


@pytest.mark.parametrize("name", sorted(SECRET_SCENARIOS))
async def test_exception_messages_on_trading_paths_never_contain_secrets(server, make_broker, name, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BrokerError) as info:
        await SECRET_SCENARIOS[name](server, make_broker)
    secrets = server_secrets(server)
    for e in chain(info.value):
        texts = [str(e), repr(e), *map(str, e.args), str(getattr(e, "reason", ""))]
        for text in texts:
            for secret in secrets:
                assert secret not in text, f"secret leaked into {type(e).__name__}"
    assert info.value.__cause__ is None and info.value.__context__ is None
    # dziennik dostępu SERWERA testowego (`aiohttp.access`) zapisuje wiersz żądania, w którym adapter odsyła brokerowi
    # jego własny dealId (tu celowo równy tokenowi); to nie jest wyjście adaptera, więc go pomijamy
    adapter_logs = "\n".join(r.getMessage() for r in caplog.records if r.name != "aiohttp.access")
    for secret in secrets:
        assert secret not in adapter_logs


async def test_trading_values_contain_no_float(server, broker):
    server.add_working_order("DE40", "BUY", Decimal("0.001"), Decimal("25680.3"))
    await broker.login()
    ack = await broker.open_position(entry())
    pos = await broker.confirm(ack.deal_reference)
    listed = await broker.list_positions()
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    await broker.close_position(pos.deal_id)
    closed = await broker.closed_since(pos.opened_at)
    orders = await broker.list_working_orders()
    for value in (ack, pos, listed, closed, orders):
        assert_no_float(value)


# --- runda samokontroli: wiersze spoza okna, przycinanie po usunięciu sekretów --------------------------------


@pytest.mark.parametrize(
    "moment",
    [
        T0 - timedelta(hours=3),  # przed `from` (np. broker zignorował zakres albo filtruje po czasie lokalnym)
        T0 + timedelta(seconds=1),  # po `to` + 1 s
    ],
)
async def test_rows_outside_the_requested_window_mean_the_filter_was_not_applied(server, broker, moment):
    await broker.login()
    server.fail_next("/history/activity", 200, activity_reply(activity("D-1", moment, type="SWAP", source="SYSTEM")))
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(T0 - timedelta(hours=2))
    assert type(info.value) is BrokerError and "outside the requested window" in str(info.value)


async def test_rows_inside_the_last_second_of_the_window_are_accepted(server, broker):
    await broker.login()
    late = T0 + timedelta(milliseconds=999)  # `to` = 07:30:00, wiersz 07:30:00.999 (włączność sekundy `to`)
    server.fail_next("/history/activity", 200, activity_reply(activity("D-1", late, source="SL")))
    assert [c.ts for c in await broker.closed_since(T0 - timedelta(hours=2))] == [late]


@pytest.mark.parametrize("prefix", [190, 195, 199])
async def test_error_codes_are_cut_only_after_secrets_are_removed(server, broker, prefix):
    await broker.login()
    code = "x" * prefix + TEST_API_KEY  # cięcie na 200 znakach wypadłoby w środku klucza
    server.fail_next("/positions", 400, {"errorCode": code}, method="POST")
    with pytest.raises(BrokerRejected) as info:
        await broker.open_position(entry())
    assert TEST_API_KEY[:4] not in info.value.reason and info.value.reason == ("x" * prefix + "<redacted>")[:200]


async def test_rejection_reasons_are_cut_only_after_secrets_are_removed(server, broker):
    await broker.login()
    server.reject_next_order("y" * 195 + TEST_PASSWORD)
    ack = await broker.open_position(entry())
    with pytest.raises(BrokerRejected) as info:
        await broker.confirm(ack.deal_reference)
    assert TEST_PASSWORD[:4] not in info.value.reason and len(info.value.reason) == 200


async def test_confirm_of_a_closed_position_with_two_opening_rows_is_a_broker_error(server, broker, make_broker, clock):
    await broker.login()
    ref, pos = await open_confirmed(broker)
    clock.advance(10)
    server.add_activity(activity(pos.deal_id, clock.now_utc(), direction="BUY", level="25790.0"))  # np. dołożenie
    server.trigger_stop(pos.deal_id, "25751.0")
    restarted = await restart(make_broker, clock)
    with pytest.raises(BrokerError, match="appears 2 times"):
        await restarted.confirm(ref)


# --- runda poprawek 1 -----------------------------------------------------------------------------------------
# (1) potwierdzenie `p_<dealId>` jest wspólne dla PUT i DELETE: sukces tylko z przypisaniem do NASZEGO deala i kontrolą


async def test_close_with_a_stale_put_confirm_is_an_unknown_outcome(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    # DELETE przyjęty do realizacji, ale bez skutku: ta sama referencja, potwierdzenie nadal pokazuje PUT
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="DELETE")
    mark = since_mark(server)
    with pytest.raises(BrokerTimeout, match="close outcome unknown"):
        await broker.close_position(pos.deal_id)
    assert pos.deal_id in server.positions
    assert seq(server, mark) == [f"DELETE /positions/{pos.deal_id}", f"GET /confirms/p_{pos.deal_id}"]


def close_confirm(deal_id: str, **changes) -> dict:
    body = {
        "status": "CLOSED",
        "dealStatus": "ACCEPTED",
        "epic": "DE40",
        "dealReference": f"p_{deal_id}",
        "dealId": deal_id,
        "affectedDeals": [{"dealId": deal_id, "status": "FULLY_CLOSED"}],
        "level": BID,
        "size": Decimal("0.001"),
        "direction": "SELL",
    }
    body.update(changes)
    return body


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "OPEN"},
        {"affectedDeals": []},
        {"affectedDeals": [{"dealId": "OTHER-DEAL", "status": "FULLY_CLOSED"}]},
        {"affectedDeals": [{"dealId": "{deal}", "status": "AMENDED"}]},
        {"affectedDeals": [{"dealId": "{deal}", "status": "OPENED"}]},
        {"affectedDeals": None},
    ],
)
async def test_close_confirm_must_attribute_the_closure_to_our_deal(server, broker, changes):
    await broker.login()
    _, pos = await open_confirmed(broker)
    if "affectedDeals" in changes and changes["affectedDeals"]:
        changes = {"affectedDeals": [{**d, "dealId": d["dealId"].replace("{deal}", pos.deal_id)} for d in changes["affectedDeals"]]}
    server.fail_next(f"/confirms/p_{pos.deal_id}", 200, close_confirm(pos.deal_id, **changes))
    with pytest.raises(BrokerTimeout, match="close outcome unknown"):
        await broker.close_position(pos.deal_id)


async def test_accepted_close_with_the_position_still_listed_is_an_unknown_outcome(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="DELETE")
    server.fail_next(f"/confirms/p_{pos.deal_id}", 200, close_confirm(pos.deal_id))
    with pytest.raises(BrokerTimeout, match="close outcome unknown") as info:
        await broker.close_position(pos.deal_id)
    assert "still open" in str(info.value) and pos.deal_id in server.positions


@pytest.mark.parametrize(
    "failure", [(500, {"errorCode": "error.internal"}), (404, {}), (404, {"errorCode": "error.other"}), "timeout"]
)
async def test_close_verification_that_fails_is_an_unknown_outcome(server, make_broker, failure):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_confirmed(broker)
    if failure == "timeout":
        server.delay_next(f"/positions/{pos.deal_id}", 2.0, method="GET")
    else:
        server.fail_next(f"/positions/{pos.deal_id}", *failure, method="GET")
    with pytest.raises(BrokerTimeout, match="close outcome unknown") as info:
        await broker.close_position(pos.deal_id)
    assert info.value.__context__ is None and info.value.__cause__ is None


async def test_set_stop_with_a_stale_confirm_reporting_the_old_stop_is_not_success(server, broker):
    server.confirm_stop_keys = True  # potwierdzenie niesie `stopLevel` ([S3])
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")
    with pytest.raises(BrokerError) as info:
        await broker.set_stop(pos.deal_id, Decimal("25732.3"))
    assert type(info.value) is BrokerError and "stopLevel" in str(info.value)
    [listed] = await broker.list_positions()
    assert listed.stop_level == Decimal("25742.3")  # zapis bez zmian


async def test_set_stop_with_a_stale_confirm_is_caught_by_the_position_read(server, broker):
    server.position_stop_keys = "present"
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")
    with pytest.raises(BrokerError) as info:
        await broker.set_stop(pos.deal_id, Decimal("25732.3"))
    assert type(info.value) is BrokerError and "differs from the requested" in str(info.value)


async def test_set_stop_with_nothing_reported_is_accepted_and_remembered(server, broker):
    """Pozostałe ryzyko (C3): bez `stopLevel` w potwierdzeniu i w pozycji nieaktualnego potwierdzenia nie widać."""
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")
    await broker.set_stop(pos.deal_id, Decimal("25732.3"))
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (Decimal("25732.3"), True)


@pytest.mark.parametrize(
    "affected",
    [[], [{"dealId": "OTHER-DEAL", "status": "AMENDED"}], [{"dealId": "{deal}", "status": "FULLY_CLOSED"}], None],
)
async def test_set_stop_confirm_must_name_our_deal_as_amended(server, broker, affected):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    if affected:
        affected = [{**d, "dealId": d["dealId"].replace("{deal}", pos.deal_id)} for d in affected]
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "status": "OPEN",
                                               "affectedDeals": affected})  # fmt: skip
    with pytest.raises(BrokerTimeout, match="stop outcome unknown"):
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    [listed] = await broker.list_positions()
    assert listed.stop_level == pos.stop_level


@pytest.mark.parametrize(
    ("reported", "error"),
    [(Decimal("25742.3"), None), (Decimal("25700.0"), BrokerError), (None, BrokerError)],
)
async def test_set_stop_checks_the_stop_the_position_reports(server, broker, reported, error):
    await broker.login()
    _, pos = await open_confirmed(broker)
    item = server._position_item(only_position(server))
    item["position"]["stopLevel"] = reported
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")  # odczyt kontrolny po PUT
    if error is None:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    else:
        with pytest.raises(error) as info:
            await broker.set_stop(pos.deal_id, Decimal("25742.3"))
        assert type(info.value) is BrokerError


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ((404, {"errorCode": "error.not-found.dealId"}), BrokerRejected),
        ((500, {"errorCode": "error.internal"}), BrokerTimeout),
        ("timeout", BrokerTimeout),
    ],
)
async def test_set_stop_verification_failures(server, make_broker, failure, error):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_confirmed(broker)
    if failure == "timeout":
        server.delay_next(f"/positions/{pos.deal_id}", 2.0, method="GET")
    else:
        server.fail_next(f"/positions/{pos.deal_id}", *failure, method="GET")
    with pytest.raises(error) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    if error is BrokerRejected:
        assert info.value.reason == "POSITION_NOT_FOUND"
    else:
        assert "stop outcome unknown" in str(info.value)


async def test_set_stop_confirm_carrying_a_different_stop_level_is_a_broker_error(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "stopLevel": BID - 50,
                                               "affectedDeals": [{"dealId": pos.deal_id, "status": "AMENDED"}]})  # fmt: skip
    with pytest.raises(BrokerError) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert type(info.value) is BrokerError and "stopLevel" in str(info.value)


# (2) historia: deal z samotnym wierszem USER w zakresie → wyszukanie jego otwarcia wstecz (filtr dealId, okna ≤ 1 doba)


async def test_foreign_deal_closed_while_the_engine_was_down_is_found_by_a_deal_lookback(server, broker, make_broker, clock):
    # obca pozycja otwarta 3 h przed `since`, zamknięta przez właściciela po `since`, gdy silnik nie działał
    server.add_activity(activity("F-1", T0 - timedelta(hours=3), source="USER", direction="BUY", epic="US100"))
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL", epic="US100",
                                 level="28000.0"))  # fmt: skip
    # nasza pozycja: otwarta po `since`, wybita stopem
    server.add_activity(activity("OUR-1", T0 - timedelta(minutes=39), source="USER", direction="BUY"))
    server.add_activity(activity("OUR-1", T0 - timedelta(minutes=10), source="SL", direction="SELL", level="25700.0"))
    restarted = await restart(make_broker, clock)
    since = T0 - timedelta(minutes=40)
    for _ in range(3):  # za każdym razem, nie tylko raz
        closed = await restarted.closed_since(since)
        assert closed == [
            ClosedDeal("F-1", "US100", Decimal("28000.0"), "MANUAL", T0 - timedelta(minutes=20)),
            ClosedDeal("OUR-1", "DE40", Decimal("25700.0"), "STOP", T0 - timedelta(minutes=10)),
        ]
        clock.advance(60)
    lookbacks = [r for r in server.requests("GET", "/history/activity") if r.query.get("dealId") == "F-1"]
    assert lookbacks and all(parse_api(r.query["to"]) - parse_api(r.query["from"]) <= DAY for r in lookbacks)
    assert all(r.query["detailed"] == "true" for r in lookbacks)


async def test_deal_lookback_goes_back_day_by_day_within_the_cap(server, broker):
    server.add_activity(activity("F-1", T0 - timedelta(days=5), source="USER", direction="BUY"))
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL", level="25001.0"))
    await broker.login()
    closed = await broker.closed_since(T0 - timedelta(hours=1))
    assert [(c.deal_id, c.reason, c.exit_level) for c in closed] == [("F-1", "MANUAL", Decimal("25001.0"))]
    lookbacks = [(r.query["from"], r.query["to"]) for r in server.requests("GET", "/history/activity")
                 if r.query.get("dealId") == "F-1"]  # fmt: skip
    assert lookbacks[0] == ("2026-11-01T06:30:00", "2026-11-02T06:30:00")  # najpierw doba tuż przed `since`
    assert len(lookbacks) == 5  # dzień po dniu wstecz, aż do otwarcia sprzed 5 dni
    assert all(parse_api(t) - parse_api(f) <= DAY for f, t in lookbacks)


def lookback_requests(server, deal_id: str = "F-1") -> list[tuple[str, str]]:
    return [(r.query["from"], r.query["to"]) for r in server.requests("GET", "/history/activity")
            if r.query.get("dealId") == deal_id]  # fmt: skip


async def test_lookback_results_before_since_are_cached_and_bounded(server, broker, clock, monkeypatch):
    server.add_activity(activity("F-1", T0 - timedelta(days=5), source="USER", direction="BUY"))
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL", level="25001.0"))
    await broker.login()
    since = T0 - timedelta(hours=1)
    expected = [ClosedDeal("F-1", "DE40", Decimal("25001.0"), "MANUAL", T0 - timedelta(minutes=20))]
    assert await broker.closed_since(since) == expected
    first = lookback_requests(server)
    assert len(first) == 5  # dzień po dniu wstecz, aż do otwarcia sprzed 5 dni
    clock.advance(60)
    assert await broker.closed_since(since) == expected
    # okna w całości przed `since` (ich historia się już nie zmienia) z pamięci; doba tuż przed `since` (koniec okna
    # = `since`) zawsze od nowa, bo opóźniony wiersz mógłby się w niej jeszcze pojawić
    assert lookback_requests(server)[5:] == [first[0]]
    assert all(window_end < since for *_, window_end in broker._lookback_cache)
    await broker.select_account("ACC-EMPTY")  # historia należy do rachunku: pamięć innego rachunku nie obowiązuje
    assert await broker.closed_since(since) == expected  # (historia serwera testowego jest wspólna dla rachunków)
    assert len(lookback_requests(server)) == 5 + 1 + 5
    # ograniczenie: najwyżej LOOKBACK_CACHE_LIMIT okien (najstarsze wypadają i są czytane ponownie)
    assert capitalcom_trading.LOOKBACK_CACHE_LIMIT == 64
    monkeypatch.setattr(capitalcom_trading, "LOOKBACK_CACHE_LIMIT", 2)
    restarted = CapitalComBroker(broker._creds, broker._session, clock, base_url=server.base_url)
    clock.advance(1)
    await restarted.login()
    assert await restarted.closed_since(since) == expected
    assert len(restarted._lookback_cache) == 2
    mark = len(lookback_requests(server))
    assert await restarted.closed_since(since) == expected
    assert len(lookback_requests(server)) - mark == 5 and len(restarted._lookback_cache) == 2


def foreign_deal_history(server) -> list[ClosedDeal]:
    """F-1: otwarcie 5 dni temu, zamknięcie ręczne 20 minut temu (sprzed `since` = teraz − 1 h wyszukiwanie sięga
    dzień po dniu wstecz); zwraca oczekiwany wynik `closed_since`."""
    server.add_activity(activity("F-1", T0 - timedelta(days=5), source="USER", direction="BUY"))
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL", level="25001.0"))
    return [ClosedDeal("F-1", "DE40", Decimal("25001.0"), "MANUAL", T0 - timedelta(minutes=20))]


async def test_lookback_window_read_during_an_account_switch_is_not_cached(server, broker, monkeypatch):
    # rachunek sesji może się zmienić W TRAKCIE żądania (ponowne logowanie po 401): wiersze nie wiadomo, z którego
    # rachunku przyszły, więc nie zapisują się pod żadnym kluczem (ani starym, ani nowym)
    expected = foreign_deal_history(server)
    await broker.login()
    original = broker._activity_rows
    lookbacks = []

    async def rows_then_switch(windows, budget, **kwargs):
        rows = await original(windows, budget, **kwargs)
        if kwargs.get("deal_id"):
            lookbacks.append(windows[0])
            if len(lookbacks) == 2:  # drugie okno wyszukiwania (pierwsze nigdy nie jest zapisywane)
                broker._session_account_id = "ACC-EMPTY"
        return rows

    monkeypatch.setattr(broker, "_activity_rows", rows_then_switch)
    assert await broker.closed_since(T0 - timedelta(hours=1)) == expected
    assert len(lookbacks) == 5
    assert {key[0] for key in broker._lookback_cache} == {"ACC-EMPTY"}  # nic pod kluczem rachunku sprzed zmiany
    assert [key[2:] for key in broker._lookback_cache] == [window for window in lookbacks[2:]]  # tylko okna 3–5


async def test_lookback_cache_stores_only_windows_that_ended_long_before_now(server, broker, monkeypatch):
    # bezpieczeństwo pamięci wynika z warunku przy zapisie, nie z budowy okien: okno kończące się sekundę przed `since`
    # (świeże dane, opóźniony wiersz jeszcze może dojść) nie może trafić do pamięci
    expected = foreign_deal_history(server)
    since = T0 - timedelta(hours=1)
    near = (since - DAY - timedelta(seconds=1), since - timedelta(seconds=1))  # kończy się 1 s przed `since`
    far = (T0 - 5 * DAY - timedelta(hours=1), T0 - 5 * DAY + timedelta(hours=1))  # obejmuje otwarcie z T0 − 5 dni
    await broker.login()
    monkeypatch.setattr(capitalcom_history, "lookback_windows", lambda s, cap: [near, far])
    assert await broker.closed_since(since) == expected
    assert [key[2:] for key in broker._lookback_cache] == [far]


async def test_a_failed_lookback_window_is_not_cached_as_empty(server, broker):
    # nieudany odczyt okna (tu: HTTP 500 na czwartym zapytaniu = trzecie okno wstecz) nie zostawia w pamięci pustego
    # wyniku, który ukryłby otwarcie deala przy następnym odczycie; okna odczytane wcześniej zostają w pamięci
    expected = foreign_deal_history(server)
    await broker.login()
    since = T0 - timedelta(hours=1)
    server.fail_request("/history/activity", 4, 500)
    with pytest.raises(BrokerError, match="HTTP 500"):
        await broker.closed_since(since)
    sent = lookback_requests(server)
    assert len(sent) == 3 and len(broker._lookback_cache) == 1  # okno 2 (odczytane w całości) tak, nieudane okno 3 nie
    [(_, _, cached_from, cached_to)] = broker._lookback_cache
    assert (capitalcom_history.api_time(cached_from), capitalcom_history.api_time(cached_to)) == sent[1]
    assert await broker.closed_since(since) == expected  # trzecie okno czytane od nowa, otwarcie znalezione
    assert len(broker._lookback_cache) == 4 and all(len(r) <= 1 for r in broker._lookback_cache.values())
    assert len(lookback_requests(server)) == 3 + 1 + 3  # okno 1 i okna 3–5 od nowa, okno 2 z pamięci


async def test_lookback_cache_key_includes_the_whole_window(server, broker):
    # to samo `from` z innym końcem (okno przycięte do granicy 14 dni przy innym `since`) i ten sam koniec z innym
    # początkiem to różne wpisy: wynik jednego okna nie podmienia wyniku drugiego
    await broker.login()
    start = T0 - 3 * DAY
    short, long = (start, start + timedelta(hours=1)), (start, start + timedelta(hours=12))
    early = (start - timedelta(hours=6), start + timedelta(hours=1))
    server.add_activity(activity("F-1", start + timedelta(hours=6), source="USER", direction="BUY"))
    server.add_activity(activity("F-1", start - timedelta(hours=3), source="USER", direction="BUY"))
    now, budget = broker._clock.now_utc(), capitalcom_trading._Budget(10)
    for _ in range(2):  # drugi obieg z pamięci musi dać te same, osobne wyniki
        assert await broker._lookback_rows("F-1", short, T0, now, budget) == ()
        [late] = await broker._lookback_rows("F-1", long, T0, now, budget)
        [first] = await broker._lookback_rows("F-1", early, T0, now, budget)
        assert (first.ts, late.ts) == (start - timedelta(hours=3), start + timedelta(hours=6))
    assert len(broker._lookback_cache) == 3 and budget.left == 7


async def test_lookback_cache_never_stores_a_window_that_reaches_since(server, broker):
    # `since` dość stare, by każde okno spełniało warunek wieku: okno kończące się w `since` (doba tuż przed zakresem
    # głównym) i tak jest czytane od nowa, okno w całości przed `since` trafia do pamięci
    await broker.login()
    since = T0 - 3 * DAY
    now, budget = broker._clock.now_utc(), capitalcom_trading._Budget(10)
    reaching = (since - DAY, since)
    before = (since - 2 * DAY + timedelta(seconds=1), since - DAY + timedelta(seconds=1))
    for window in (reaching, before):
        await broker._lookback_rows("F-1", window, since, now, budget)
    assert [key[2:] for key in broker._lookback_cache] == [before]


async def test_lookback_cache_hits_do_not_use_the_request_budget(server, broker):
    await broker.login()
    window = (T0 - 3 * DAY, T0 - 2 * DAY)
    server.add_activity(activity("F-1", T0 - timedelta(days=2, hours=12), source="USER", direction="BUY"))
    now, budget = broker._clock.now_utc(), capitalcom_trading._Budget(3)
    first = await broker._lookback_rows("F-1", window, T0, now, budget)
    assert budget.left == 2 and len(first) == 1
    mark = since_mark(server)
    for _ in range(5):
        assert await broker._lookback_rows("F-1", window, T0, now, budget) == first
    assert budget.left == 2 and seq(server, mark) == []  # trafienia: bez zapytania i bez zużycia limitu


async def test_deal_lookback_that_exhausts_the_request_budget_raises(server, broker, monkeypatch):
    monkeypatch.setattr(capitalcom_history, "MAX_HISTORY_REQUESTS", 4)
    server.add_activity(activity("F-1", T0 - timedelta(days=5), source="USER", direction="BUY"))
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL"))
    server.add_activity(activity("D-2", T0 - timedelta(minutes=10), source="SL", direction="SELL"))
    await broker.login()
    with pytest.raises(BrokerError, match="^HISTORY_TRUNCATED"):
        await broker.closed_since(T0 - timedelta(hours=1))
    assert len(server.requests("GET", "/history/activity")) == 4


async def test_deal_lookback_stops_at_the_fourteen_day_cap(server, broker):
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL"))
    await broker.login()
    with pytest.raises(BrokerError, match="^HISTORY_AMBIGUOUS"):
        await broker.closed_since(T0 - timedelta(hours=1))
    lookbacks = [r for r in server.requests("GET", "/history/activity") if r.query.get("dealId") == "F-1"]
    earliest = min(parse_api(r.query["from"]) for r in lookbacks)
    assert earliest >= T0 - timedelta(days=14) - timedelta(seconds=1) and len(lookbacks) == 14


# (3) stop wyliczony nie może udawać ochrony


async def test_once_stop_keys_were_seen_a_position_without_them_has_no_stop(server, broker):
    server.position_stop_keys = "when-set"  # API pokazuje klucz tylko przy pozycji ze stopem
    await broker.login()
    _, pos = await open_confirmed(broker)
    assert (pos.stop_level, pos.stop_derived) == (OFFER - 30, False)
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))  # adapter ma teraz własny zapis stopu
    only_position(server).stop_level = None  # właściciel usunął stop w aplikacji: klucz znika z odpowiedzi
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (None, False)  # nie nasz zapis, który udawałby ochronę


async def test_stop_keys_seen_in_a_list_also_disable_derivation_for_other_positions(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)  # klucze nieobecne: stop wyliczony
    item = server._position_item(only_position(server))
    other = position_item(position__dealId="D-2", position__stopLevel=Decimal("25700.0"))
    server.fail_next("/positions", 200, {"positions": [item, other]}, method="GET")
    first = await broker.list_positions()
    assert [(p.deal_id, p.stop_level, p.stop_derived) for p in first] == [
        (pos.deal_id, None, False),  # w tej samej odpowiedzi klucze widać przy innej pozycji
        ("D-2", Decimal("25700.0"), False),
    ]
    [again] = await broker.list_positions()
    assert (again.stop_level, again.stop_derived) == (None, False)


@pytest.mark.parametrize(
    ("confirm_stop", "expected"),
    [("match", None), ("other", BrokerError), ("null", BrokerError)],
)
async def test_confirm_stop_level_is_cross_checked_against_our_derived_stop(server, broker, confirm_stop, expected):
    await broker.login()
    ack = await broker.open_position(entry())
    payload = dict(server.confirms[ack.deal_reference])
    payload["stopLevel"] = {"match": OFFER - 30, "other": OFFER - 25, "null": None}[confirm_stop]
    server.fail_next(f"/confirms/{ack.deal_reference}", 200, payload)
    if expected is None:
        pos = await broker.confirm(ack.deal_reference)
        assert (pos.stop_level, pos.stop_derived) == (OFFER - 30, True)
    else:
        with pytest.raises(expected) as info:
            await broker.confirm(ack.deal_reference)
        assert type(info.value) is BrokerError and "stopLevel" in str(info.value)


async def test_confirm_stop_level_without_our_record_is_the_reported_stop(server, broker, make_broker, clock):
    server.confirm_stop_keys = True
    await broker.login()
    ref, pos = await open_confirmed(broker)
    restarted = await restart(make_broker, clock)
    snapshot = await restarted.confirm(ref)
    assert (snapshot.stop_level, snapshot.stop_derived) == (OFFER - 30, False)


class TestCapitalComContractWithStopKeys(TestCapitalComContract):
    """Kontrakt także wtedy, gdy broker pokazuje klucze stopu: asercje stopu porównują wartość brokera, nie nasz zapis."""

    @pytest.fixture
    def broker(self, capital_broker, fake_capital_server):
        fake_capital_server.position_stop_keys = "present"
        fake_capital_server.confirm_stop_keys = True
        return capital_broker


async def test_stop_keys_mode_reports_the_brokers_stop_not_ours(server, broker):
    server.position_stop_keys = "present"
    await broker.login()
    _, pos = await open_confirmed(broker)
    only_position(server).stop_level = Decimal("25600.0")  # broker ma inny stop niż nasze wyliczenie
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.stop_derived) == (Decimal("25600.0"), False)


# poprawki drobne


async def test_overflowing_fill_level_is_a_broker_error(server, broker):
    await broker.login()
    ack = await broker.open_position(entry())
    payload = dict(server.confirms[ack.deal_reference])
    payload["level"] = Decimal("1E+1000000")
    server.fail_next(f"/confirms/{ack.deal_reference}", 200, payload)
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    assert type(info.value) is BrokerError and "out of range" in str(info.value)


async def test_unknown_close_source_is_counted_once_per_closure(server, broker):
    server.add_activity(activity("D-1", T0 - timedelta(minutes=10), source="USER", direction="BUY"))
    server.add_activity(activity("D-1", T0 - timedelta(minutes=5), source="NEW_SOURCE", direction="SELL"))
    await broker.login()
    for _ in range(3):
        await broker.closed_since(T0 - timedelta(hours=1))
    assert broker.unknown_close_sources == 1
    server.add_activity(activity("D-2", T0 - timedelta(minutes=4), source="NEW_SOURCE", direction="SELL"))
    await broker.closed_since(T0 - timedelta(hours=1))
    assert broker.unknown_close_sources == 2


async def test_market_closed_reason_on_an_opening_confirm_is_both_rejected_and_market_closed(server, broker):
    await broker.login()
    server.reject_next_order("error.market.closed")
    ack = await broker.open_position(entry())
    with pytest.raises(MarketClosed) as info:
        await broker.confirm(ack.deal_reference)
    # nadal odrzucenie: menedżer zleceń kończy wejście jako REJECTED (nie UNKNOWN)
    assert isinstance(info.value, BrokerRejected) and info.value.reason == "error.market.closed"


async def test_market_closed_reason_on_a_close_confirm_is_market_closed(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": ref}, method="DELETE")  # odmowa: bez skutku
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "REJECTED", "reason": "MARKET_CLOSED", "dealReference": ref})
    with pytest.raises(MarketClosed) as info:
        await broker.close_position(pos.deal_id)
    assert isinstance(info.value, BrokerRejected) and pos.deal_id in server.positions


def deal_row(deal: str, minutes: int, source: str, side: Side, level: str = "25000") -> capitalcom_history.DealRow:
    ts = T0 + timedelta(minutes=minutes)
    return capitalcom_history.DealRow(deal, "DE40", ts, source, side, Decimal(level), (deal, ts, source, side))


def test_a_row_of_the_inferred_side_after_the_supposed_closure_is_inconsistent():
    # otwarcie przez API z innym źródłem niż USER (gdyby C7 było błędne), zamknięcie USER, strona nieznana
    rows = [deal_row("D", 0, "DEALER", Side.BUY, "25782.3"), deal_row("D", 5, "USER", Side.SELL, "25800")]
    found = capitalcom_history.classify(rows, {})
    assert found.closures == [] and found.conflicting == {"D"}


async def test_non_user_opening_after_restart_is_inconsistent_not_booked(server, broker):
    server.add_activity(activity("D", T0 - timedelta(minutes=30), source="DEALER", direction="BUY", level="25782.3"))
    server.add_activity(activity("D", T0 - timedelta(minutes=25), source="USER", direction="SELL", level="25800.0"))
    await broker.login()
    with pytest.raises(BrokerError, match="^HISTORY_INCONSISTENT"):
        await broker.closed_since(T0 - timedelta(hours=1))


async def test_close_with_a_non_accepted_status_is_unknown_even_when_attributed(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    server.fail_next(f"/confirms/p_{pos.deal_id}", 200, close_confirm(pos.deal_id, dealStatus="PENDING"))
    with pytest.raises(BrokerTimeout, match="unexpected dealStatus"):
        await broker.close_position(pos.deal_id)  # DELETE wykonany, ale status potwierdzenia nie mówi „przyjęte”


async def test_set_stop_with_a_non_accepted_status_is_unknown_even_when_attributed(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "PENDING", "dealReference": ref,
                                               "affectedDeals": [{"dealId": pos.deal_id, "status": "AMENDED"}]})  # fmt: skip
    with pytest.raises(BrokerTimeout, match="unexpected dealStatus"):
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))


async def test_confirm_after_stop_keys_were_seen_reports_a_missing_key_as_no_stop(server, broker):
    await broker.login()
    foreign = position_item(position__dealId="D-9", position__stopLevel=Decimal("25700.0"))
    server.fail_next("/positions", 200, {"positions": [foreign]}, method="GET")
    await broker.list_positions()  # broker pokazał już klucze stopu (przy innej pozycji)
    _, pos = await open_confirmed(broker)  # nasza pozycja bez klucza: według brokera bez stopu
    assert (pos.stop_level, pos.stop_derived) == (None, False)


# --- runda poprawek 2 -----------------------------------------------------------------------------------------


async def seen_stop_keys(server, broker) -> None:
    """Broker pokazał już klucze stopu (przy obcej pozycji): od tej chwili brak klucza znaczy brak stopu."""
    foreign = position_item(position__dealId="D-9", position__stopLevel=Decimal("25700.0"))
    server.fail_next("/positions", 200, {"positions": [foreign]}, method="GET")
    await broker.list_positions()


async def test_set_stop_without_the_stop_key_after_keys_were_seen_is_an_unknown_outcome(server, broker):
    server.position_stop_keys = "when-set"
    await broker.login()
    _, pos = await open_confirmed(broker)  # klucze widoczne: pozycja pokazuje swój stop
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    only_position(server).stop_level = None  # właściciel usunął stop: klucz znika
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")  # bez skutku
    with pytest.raises(BrokerTimeout, match="stop outcome unknown") as info:
        await broker.set_stop(pos.deal_id, Decimal("25732.3"))
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert broker._deal_stops[pos.deal_id] == Decimal("25742.3")  # nic nie zapisano z nieudanej próby
    assert only_position(server).stop_level is None


async def test_confirm_stop_level_does_not_mask_a_missing_position_stop_after_keys_were_seen(server, broker):
    server.position_stop_keys = "when-set"
    server.confirm_stop_keys = True
    await broker.login()
    await seen_stop_keys(server, broker)
    ack = await broker.open_position(entry())
    only_position(server).stop_level = None  # pozycja bez stopu (w trybie when-set: bez klucza)
    snapshot = await broker.confirm(ack.deal_reference)
    assert (snapshot.stop_level, snapshot.stop_derived) == (None, False)  # wykonawca zabezpieczy pozycję


@pytest.mark.parametrize("keys_seen", [False, True])
async def test_confirm_of_a_position_closed_before_the_read_keeps_the_derived_stop(server, broker, keys_seen):
    await broker.login()
    if keys_seen:
        await seen_stop_keys(server, broker)
    ack = await broker.open_position(entry())
    [deal_id] = list(server.positions)
    server.trigger_stop(deal_id, "25752.3")  # wybita stopem przed naszym odczytem
    snapshot = await broker.confirm(ack.deal_reference)
    # pozycji już nie ma: reguła „brak klucza = brak stopu” nie ma tu zastosowania (nie ma czego chronić)
    assert (snapshot.stop_level, snapshot.stop_derived) == (OFFER - 30, True)


@pytest.mark.parametrize("keys_seen", [False, True])
async def test_set_stop_accepts_a_reply_with_only_stop_distance(server, broker, keys_seen):
    await broker.login()
    if keys_seen:
        await seen_stop_keys(server, broker)
    _, pos = await open_confirmed(broker)
    item = server._position_item(only_position(server))
    item["position"]["stopDistance"] = BID - Decimal("25742.3")  # odległość od notowania, nie od wykonania (C6)
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")
    await broker.set_stop(pos.deal_id, Decimal("25742.3"))  # porównujemy tylko zgłoszony `stopLevel`
    assert only_position(server).stop_level == Decimal("25742.3")


async def test_set_stop_with_a_null_stop_distance_is_a_broker_error(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    item = server._position_item(only_position(server))
    item["position"]["stopDistance"] = None  # broker mówi: brak stopu
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.set_stop(pos.deal_id, Decimal("25742.3"))
    assert type(info.value) is BrokerError


async def test_a_lookback_reply_with_another_deal_means_the_filter_was_not_honoured(server, broker):
    since = T0 - timedelta(hours=1)
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL", level="25001.0"))
    rows = [
        activity("X-9", since - timedelta(hours=2), source="USER", direction="SELL"),
        activity("F-1", since - timedelta(hours=3), source="USER", direction="BUY"),
    ]
    server.fail_request("/history/activity", 2, 200, {"activities": rows}, method="GET")  # drugie żądanie = wstecz
    await broker.login()
    with pytest.raises(BrokerError) as info:
        await broker.closed_since(since)
    assert type(info.value) is BrokerError and "filter not honoured" in str(info.value)
    assert server.requests("GET", "/history/activity")[1].query["dealId"] == "F-1"


async def test_a_lookback_row_without_a_deal_id_is_rejected_too(server, broker):
    since = T0 - timedelta(hours=1)
    server.add_activity(activity("F-1", T0 - timedelta(minutes=20), source="USER", direction="SELL"))
    row = activity("F-1", since - timedelta(hours=3), source="SYSTEM", type="SWAP")
    del row["dealId"]
    server.fail_request("/history/activity", 2, 200, {"activities": [row]}, method="GET")
    await broker.login()
    with pytest.raises(BrokerError, match="filter not honoured"):
        await broker.closed_since(since)
