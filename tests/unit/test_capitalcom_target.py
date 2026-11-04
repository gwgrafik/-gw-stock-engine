"""Testy adaptera Capital.com, część 3: cel zysku (zadanie 7, R4, projekt §3.1): `profitDistance` przy wejściu,
cel pozycji w potwierdzeniu i na liście, `set_protection`, zamknięcie przez cel.

Adapter rozmawia wyłącznie z lokalnym serwerem `fake_capital_server` (127.0.0.1); prawdziwa sieć nie jest używana.
Wspólne pomocnicze funkcje (`entry`, `open_confirmed`, `position_item`, ...) pochodzą z `test_capitalcom_trading`.
Czas: zegar testu startuje 2026-11-02 07:30:00 UTC (jak w części 2).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from gwstock.broker import capitalcom_parse
from gwstock.broker.capitalcom import CapitalComBroker
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    MarketClosed,
    OpenPosition,
    Side,
    TargetedEntryRequest,
    TargetedOpenPosition,
)
from tests.unit.fake_capital_server import MARKET_CLOSED_CODE
from tests.unit.test_capitalcom_trading import (
    BID,
    OFFER,
    assert_no_float,
    at,
    entry,
    only_position,
    open_confirmed,
    position_item,
    restart,
    seq,
    since_mark,
)


@pytest.fixture
def server(fake_capital_server):
    return fake_capital_server


@pytest.fixture
def broker(capital_broker):
    return capital_broker


@pytest.fixture
def make_broker(fake_capital_server, capital_credentials, capital_http, clock):
    """Nowa instancja adaptera (np. „po restarcie”: bez pamięci własnych żądań) na tym samym serwerze."""

    def make(**kwargs):
        return CapitalComBroker(
            capital_credentials, capital_http, clock, base_url=fake_capital_server.base_url, **kwargs
        )

    return make


# =============================================================================================================
# Cel zysku (zadanie 7, R4, projekt §3.1). Nazwy pól celu w odpowiedziach brokera (`profitLevel`, `profitDistance`),
# przypisanie `TP` i skutek `PUT` bez `profitLevel` (C5d) są NIEPOTWIERDZONE na rachunku (research/diag_fx.py);
# serwer testowy (`fake_capital_server`) przyjmuje je jako założenia z notatek API §2.10–§2.15, §2.19.
# =============================================================================================================


def targeted(
    side: Side = Side.BUY, size: str = "0.001", stop: str = "30", target: str = "45", epic: str = "DE40"
) -> TargetedEntryRequest:
    return TargetedEntryRequest("gws-20261102-DE40-1-entry", epic, side, Decimal(size), Decimal(stop), Decimal(target))


async def open_targeted(broker, side: Side = Side.BUY, **kwargs) -> tuple[str, OpenPosition]:
    ack = await broker.open_position(targeted(side, **kwargs))
    pos = await broker.confirm(ack.deal_reference)
    assert pos is not None
    return ack.deal_reference, pos


PLAIN_BUY_BODY = (
    '{"epic":"DE40","direction":"BUY","size":0.001,"guaranteedStop":false,"trailingStop":false,"stopDistance":30.0}'
)
PLAIN_SELL_BODY = (
    '{"epic":"DE40","direction":"SELL","size":0.010,"guaranteedStop":false,"trailingStop":false,"stopDistance":25.5}'
)


@pytest.mark.parametrize(
    ("req", "body"),
    [
        (entry(Side.BUY, size="0.001", stop="30.0"), PLAIN_BUY_BODY),
        (entry(Side.SELL, size="0.010", stop="25.5"), PLAIN_SELL_BODY),
    ],
)
async def test_plain_entry_body_is_byte_identical_and_has_no_profit_key(server, broker, req, body):
    await broker.login()
    await broker.open_position(req)
    [post] = server.requests("POST", "/positions")
    assert post.raw == body  # DE40 v1: ciało żądania bez zmian co do bajta (R2)
    assert "profit" not in post.raw.lower()


async def test_targeted_entry_adds_only_profit_distance_after_stop_distance(server, broker):
    await broker.login()
    await broker.open_position(targeted(Side.BUY, size="0.001", stop="30.0", target="45.5"))
    [post] = server.requests("POST", "/positions")
    assert post.raw == PLAIN_BUY_BODY[:-1] + ',"profitDistance":45.5}'
    assert post.raw.startswith(PLAIN_BUY_BODY[:-1])  # część wspólna z wejściem bez celu jest ta sama


@pytest.mark.parametrize("target", ["45", "1.5E+1", "12.345", "3.50", "2.57813"])
async def test_profit_distance_decimals_round_trip_exactly(server, broker, target):
    await broker.login()
    await broker.open_position(targeted(Side.SELL, target=target, stop="30"))
    [post] = server.requests("POST", "/positions")
    sent = json.loads(post.raw, parse_float=Decimal, parse_int=Decimal)
    assert post.raw.endswith(f'"profitDistance":{format(Decimal(target), "f")}}}')
    assert sent["profitDistance"] == Decimal(target)


async def test_targeted_entry_never_sends_profit_level_or_amount_or_stop_level(server, broker):
    await broker.login()
    await broker.open_position(targeted())
    [post] = server.requests("POST", "/positions")
    assert sorted(post.body) == sorted(
        ["epic", "direction", "size", "guaranteedStop", "trailingStop", "stopDistance", "profitDistance"]
    )


@pytest.mark.parametrize("target", ["0", "-1", "-0.5"])
async def test_targeted_entry_validates_the_target_distance_before_sending(server, broker, target):
    await broker.login()
    with pytest.raises(ValueError, match="target_distance"):
        await broker.open_position(targeted(target=target))
    assert server.count("POST", "/positions") == 0


@pytest.mark.parametrize("bad", [None, 45.0, "45"])
async def test_a_targeted_request_without_a_usable_target_is_never_sent_as_a_plain_entry(server, broker, bad):
    # zamrożony dataclass da się obejść: adapter i tak nie otwiera cicho pozycji bez celu
    req = targeted()
    object.__setattr__(req, "target_distance", bad)
    await broker.login()
    with pytest.raises((TypeError, ValueError), match="target_distance"):
        await broker.open_position(req)
    assert server.count("POST", "/positions") == 0 and server.positions == {}


async def test_a_rejected_target_is_never_retried_without_the_target(server, broker):
    await broker.login()
    server.fail_next("/positions", 400, {"errorCode": "error.invalid.takeprofit.maxvalue: 25783.0"}, method="POST")
    with pytest.raises(BrokerRejected) as info:
        await broker.open_position(targeted())
    assert info.value.reason == "error.invalid.takeprofit.maxvalue: 25783.0"
    assert server.count("POST", "/positions") == 1 and server.positions == {}


async def test_target_closer_than_the_broker_minimum_is_rejected_by_the_server_without_a_position(server, broker):
    await broker.login()
    with pytest.raises(BrokerRejected) as info:
        await broker.open_position(targeted(target="1.0"))
    assert info.value.reason == "error.invalid.takeprofit.maxvalue: 2.57813"
    assert server.positions == {}


async def test_the_resend_after_401_still_carries_the_target(server, broker):
    await broker.login()
    server.fail_next("/positions", 401, {"errorCode": "error.invalid.session.token"}, method="POST")
    await broker.open_position(targeted(target="45.5"))
    first, second = server.requests("POST", "/positions")
    assert (first.status, second.status) == (401, 200)
    assert first.raw == second.raw and '"profitDistance":45.5' in second.raw
    assert only_position(server).target_level == OFFER + Decimal("45.5")


async def test_a_timeout_on_a_targeted_open_is_never_resent(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    server.apply_then_drop_response("/positions", method="POST")
    with pytest.raises(BrokerTimeout):
        await broker.open_position(targeted())
    assert server.count("POST", "/positions") == 1 and len(server.positions) == 1


# --- confirm i list_positions z celem -----------------------------------------------------------------------


async def test_confirm_of_a_targeted_entry_is_a_targeted_position_with_a_derived_target(server, broker):
    await broker.login()
    ack = await broker.open_position(targeted(Side.BUY))
    fake = only_position(server)
    mark = since_mark(server)
    pos = await broker.confirm(ack.deal_reference)
    assert type(pos) is TargetedOpenPosition
    assert pos == TargetedOpenPosition(
        fake.deal_id, "DE40", Side.BUY, Decimal("0.001"), OFFER, OFFER - 30, at(0.2), True, OFFER + 45, True
    )
    assert seq(server, mark) == [f"GET /confirms/{ack.deal_reference}", f"GET /positions/{fake.deal_id}"]
    assert_no_float(pos)


async def test_confirm_of_a_targeted_sell_derives_the_target_below_the_fill(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker, Side.SELL, stop="25.5", target="40.5")
    assert (pos.open_level, pos.stop_level, pos.target_level, pos.target_derived) == (
        BID, BID + Decimal("25.5"), BID - Decimal("40.5"), True,
    )  # fmt: skip


async def test_confirm_of_a_plain_entry_stays_a_plain_open_position(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    assert type(pos) is OpenPosition and (pos.target_level, pos.target_derived) == (None, False)
    [listed] = await broker.list_positions()
    assert type(listed) is OpenPosition


@pytest.mark.parametrize(
    ("keys", "side", "expected_level"),
    [
        ("present", Side.BUY, OFFER + 45),
        ("when-set", Side.BUY, OFFER + 45),
        ("distance", Side.BUY, OFFER + 45),  # sam `profitDistance`: poziom wykonania + odległość (BUY plus)
        ("distance", Side.SELL, BID - 45),  # SELL minus
    ],
)
async def test_confirm_uses_the_broker_target_fields_when_they_are_present(server, broker, keys, side, expected_level):
    server.position_target_keys = keys
    await broker.login()
    _, pos = await open_targeted(broker, side)
    assert type(pos) is TargetedOpenPosition
    assert (pos.target_level, pos.target_derived) == (expected_level, False)


async def test_confirm_shows_the_target_of_the_broker_not_ours(server, broker):
    server.position_target_keys = "present"
    await broker.login()
    ack = await broker.open_position(targeted())
    only_position(server).target_level = OFFER + 50  # broker ma inny cel niż nasze wyliczenie
    pos = await broker.confirm(ack.deal_reference)
    assert (pos.target_level, pos.target_derived) == (OFFER + 50, False)


async def test_a_null_target_key_means_no_target_and_is_not_replaced_by_our_record(server, broker):
    server.position_target_keys = "null"  # broker pokazuje klucz z jawnym null: pozycja bez celu
    await broker.login()
    ack = await broker.open_position(targeted())
    pos = await broker.confirm(ack.deal_reference)
    # uczciwa migawka: cel, o który prosiliśmy, nie istnieje (silnik zobaczy brak celu i zareaguje), żadnego udawania
    assert type(pos) is OpenPosition and (pos.target_level, pos.target_derived) == (None, False)


async def test_confirm_target_level_in_the_confirmation_matching_our_request_is_accepted(server, broker):
    server.confirm_target_keys = True
    await broker.login()
    ack = await broker.open_position(targeted())
    pos = await broker.confirm(ack.deal_reference)
    assert (pos.target_level, pos.target_derived) == (OFFER + 45, True)  # zgodny z wyliczeniem


@pytest.mark.parametrize("reported", [OFFER + 44, OFFER - 45, None])
async def test_confirm_with_a_target_level_differing_from_our_request_is_a_broker_error(server, broker, reported):
    await broker.login()
    ack = await broker.open_position(targeted())
    payload = dict(server.confirms[ack.deal_reference])
    payload["profitLevel"] = reported
    server.fail_next(f"/confirms/{ack.deal_reference}", 200, payload)
    with pytest.raises(BrokerError) as info:
        await broker.confirm(ack.deal_reference)
    assert type(info.value) is BrokerError and "profitLevel" in str(info.value)


async def seen_target_keys(server, broker) -> None:
    """Broker pokazał już klucze celu (przy obcej pozycji): od tej chwili brak klucza znaczy brak celu."""
    foreign = position_item(position__dealId="D-9", position__profitLevel=Decimal("25900.0"))
    server.fail_next("/positions", 200, {"positions": [foreign]}, method="GET")
    await broker.list_positions()


async def test_confirm_after_target_keys_were_seen_reports_a_missing_key_as_no_target(server, broker):
    await broker.login()
    await seen_target_keys(server, broker)
    _, pos = await open_targeted(broker)  # nasza pozycja bez klucza celu: według brokera bez celu
    assert type(pos) is OpenPosition and (pos.target_level, pos.target_derived) == (None, False)
    assert pos.stop_derived  # stop (klucze stopu nigdy nie wystąpiły) nadal wyliczony: reguły są niezależne


@pytest.mark.parametrize("keys_seen", [False, True])
async def test_confirm_of_a_closed_by_target_position_still_returns_the_derived_target(server, broker, keys_seen):
    await broker.login()
    if keys_seen:
        await seen_target_keys(server, broker)
    ack = await broker.open_position(targeted())
    created = only_position(server).created
    server.trigger_target(only_position(server).deal_id)  # cel zadziałał, zanim zapytaliśmy o potwierdzenie
    pos = await broker.confirm(ack.deal_reference)
    assert type(pos) is TargetedOpenPosition
    assert (pos.stop_level, pos.target_level, pos.stop_derived, pos.target_derived) == (OFFER - 30, OFFER + 45, True, True)
    assert pos.opened_at == created  # z wiersza otwarcia w historii


async def test_confirm_without_our_record_uses_the_reported_target_and_otherwise_none(
    server, broker, make_broker, clock
):
    await broker.login()
    ref, _ = await open_targeted(broker)
    restarted = await restart(make_broker, clock)
    assert (await restarted.confirm(ref)).target_level is None  # brak kluczy i brak zapisu: nie wiadomo
    server.position_target_keys = "present"
    again = await restart(make_broker, clock)
    snapshot = await again.confirm(ref)
    assert (type(snapshot), snapshot.target_level, snapshot.target_derived) == (
        TargetedOpenPosition, OFFER + 45, False,
    )  # fmt: skip


async def test_list_positions_returns_the_derived_target_from_our_record(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    assert await broker.list_positions() == [pos]
    [listed] = await broker.list_positions()
    assert type(listed) is TargetedOpenPosition and (listed.target_level, listed.target_derived) == (OFFER + 45, True)


async def test_list_positions_after_a_restart_has_no_target_when_the_broker_does_not_show_it(
    server, broker, make_broker, clock
):
    await broker.login()
    await open_targeted(broker)
    restarted = await restart(make_broker, clock)
    [listed] = await restarted.list_positions()
    assert type(listed) is OpenPosition and (listed.target_level, listed.target_derived) == (None, False)


async def test_list_positions_shows_the_target_of_the_broker_not_ours(server, broker):
    server.position_target_keys = "present"
    await broker.login()
    await open_targeted(broker)
    only_position(server).target_level = OFFER + 60
    [listed] = await broker.list_positions()
    assert (listed.target_level, listed.target_derived) == (OFFER + 60, False)


async def test_once_target_keys_were_seen_a_position_without_them_has_no_target(server, broker):
    server.position_target_keys = "when-set"  # API pokazuje klucz tylko przy pozycji z celem
    await broker.login()
    _, pos = await open_targeted(broker)
    assert (pos.target_level, pos.target_derived) == (OFFER + 45, False)
    only_position(server).target_level = None  # właściciel usunął cel w aplikacji: klucz znika z odpowiedzi
    [listed] = await broker.list_positions()
    assert type(listed) is OpenPosition and (listed.target_level, listed.target_derived) == (None, False)


async def test_target_keys_seen_in_a_list_also_disable_derivation_for_other_positions(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)  # klucze nieobecne: cel wyliczony z naszego zapisu
    item = server._position_item(only_position(server))
    other = position_item(position__dealId="D-2", position__profitLevel=Decimal("25900.0"))
    server.fail_next("/positions", 200, {"positions": [item, other]}, method="GET")
    first = await broker.list_positions()
    assert [(p.deal_id, p.target_level, p.target_derived) for p in first] == [
        (pos.deal_id, None, False),  # w tej samej odpowiedzi klucze celu widać przy innej pozycji
        ("D-2", Decimal("25900.0"), False),
    ]
    [again] = await broker.list_positions()
    assert (again.target_level, again.target_derived) == (None, False)


async def test_a_manual_target_set_by_the_owner_on_a_plain_position_is_reported(server, broker):
    server.position_target_keys = "when-set"
    await broker.login()
    await open_confirmed(broker)  # wejście bez celu (DE40 v1)
    only_position(server).target_level = OFFER + 100  # właściciel dodał cel ręcznie (projekt §3.5)
    [listed] = await broker.list_positions()
    assert type(listed) is TargetedOpenPosition and listed.target_level == OFFER + 100 and not listed.target_derived


async def test_closing_a_position_forgets_its_target_record(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    assert pos.deal_id in broker._deal_targets
    await broker.close_position(pos.deal_id)
    assert pos.deal_id not in broker._deal_targets


# --- PositionRow i parsowanie pól celu ---------------------------------------------------------------------


def test_position_row_requires_the_target_flags():
    # brak wartości domyślnych: każdy, kto buduje wiersz, mówi wprost, czy broker pokazał klucze celu
    fields = dict(
        deal_id="D-1", working_order_id=None, epic="DE40", side=Side.BUY, size=Decimal("0.001"), level=OFFER,
        opened_at=at(0.2), stop_reported=True, stop_level=OFFER - 30, stop_level_reported=True,
    )  # fmt: skip
    with pytest.raises(TypeError, match="target_reported"):
        capitalcom_parse.PositionRow(**fields)
    row = capitalcom_parse.PositionRow(**fields, target_reported=True, target_level=None, target_level_reported=False)
    assert (row.target_reported, row.target_level, row.target_level_reported) == (True, None, False)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, (False, None, False)),
        ({"position__profitLevel": Decimal("25827.3")}, (True, Decimal("25827.3"), True)),
        ({"position__profitLevel": None}, (True, None, True)),
        ({"position__profitDistance": Decimal("45")}, (True, OFFER + 45, False)),  # BUY: poziom + odległość
        ({"position__profitDistance": None}, (True, None, False)),
        (
            {"position__profitLevel": Decimal("25830.0"), "position__profitDistance": Decimal("45")},
            (True, Decimal("25830.0"), True),  # `profitLevel` wygrywa z `profitDistance`
        ),
        ({"position__profitLevel": None, "position__profitDistance": Decimal("45")}, (True, None, True)),
    ],
)
def test_parse_position_reads_the_target_like_the_stop(changes, expected):
    row = capitalcom_parse.parse_position(position_item(**changes))
    assert (row.target_reported, row.target_level, row.target_level_reported) == expected


def test_parse_position_target_distance_of_a_sell_is_below_the_level():
    item = position_item(position__direction="SELL", position__profitDistance=Decimal("40.5"))
    assert capitalcom_parse.parse_position(item).target_level == OFFER - Decimal("40.5")


@pytest.mark.parametrize(
    "changes",
    [
        {"position__profitLevel": "25827.3"},
        {"position__profitLevel": Decimal("0")},
        {"position__profitLevel": Decimal("-1")},
        {"position__profitLevel": True},
        {"position__profitDistance": "45"},
        {"position__profitDistance": Decimal("0")},
        {"position__profitDistance": Decimal("-3")},
    ],
)
def test_parse_position_rejects_a_malformed_target(changes):
    with pytest.raises(capitalcom_parse.ParseError, match="profit"):
        capitalcom_parse.parse_position(position_item(**changes))


async def test_list_positions_with_a_malformed_target_is_a_broker_error(server, broker):
    await broker.login()
    server.fail_next("/positions", 200, {"positions": [position_item(position__profitLevel="x")]}, method="GET")
    with pytest.raises(BrokerError, match="profitLevel"):
        await broker.list_positions()


def test_parse_opening_reads_the_profit_level_of_the_confirmation():
    base = {
        "affectedDeals": [{"dealId": "D-1", "status": "OPENED"}], "dealId": "W-1", "epic": "DE40",
        "direction": "BUY", "size": Decimal("0.001"), "level": OFFER,
    }  # fmt: skip
    plain = capitalcom_parse.parse_opening(dict(base))
    assert (plain.target_reported, plain.target_level) == (False, None)
    shown = capitalcom_parse.parse_opening({**base, "profitLevel": Decimal("25827.3")})
    assert (shown.target_reported, shown.target_level) == (True, Decimal("25827.3"))
    null = capitalcom_parse.parse_opening({**base, "profitLevel": None})
    assert (null.target_reported, null.target_level) == (True, None)
    with pytest.raises(capitalcom_parse.ParseError, match="profitLevel"):
        capitalcom_parse.parse_opening({**base, "profitLevel": Decimal("-1")})


# --- set_protection: stop i cel w jednym PUT -----------------------------------------------------------------

NEW_STOP = Decimal("25742.3")  # dalej od rynku niż stop wejścia (OFFER - 30 = 25752.3): zawsze dozwolone
NEW_TARGET = Decimal("25837.3")  # dalej niż cel wejścia (OFFER + 45 = 25827.3)


async def test_set_protection_sends_both_levels_in_one_put_and_checks_the_confirmation_and_the_position(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    mark = since_mark(server)
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert seq(server, mark) == [
        f"PUT /positions/{pos.deal_id}",
        f"GET /confirms/p_{pos.deal_id}",
        f"GET /positions/{pos.deal_id}",  # odczyt kontrolny: oba poziomy
    ]
    assert server.log[mark].raw == '{"stopLevel":25742.3,"profitLevel":25837.3}'  # JEDEN PUT z oboma poziomami
    assert (only_position(server).stop_level, only_position(server).target_level) == (NEW_STOP, NEW_TARGET)
    [moved] = await broker.list_positions()
    assert type(moved) is TargetedOpenPosition
    assert (moved.stop_level, moved.stop_derived, moved.target_level, moved.target_derived) == (
        NEW_STOP, True, NEW_TARGET, True,
    )  # fmt: skip


async def test_set_protection_sends_exactly_one_put(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert server.count("PUT", f"/positions/{pos.deal_id}") == 1


async def test_set_protection_without_a_target_sends_only_the_stop_level(server, broker):
    await broker.login()
    _, pos = await open_confirmed(broker)
    mark = since_mark(server)
    await broker.set_protection(pos.deal_id, NEW_STOP, None)
    assert server.log[mark].raw == '{"stopLevel":25742.3}'
    [moved] = await broker.list_positions()
    assert type(moved) is OpenPosition and (moved.stop_level, moved.target_level) == (NEW_STOP, None)


async def test_set_protection_with_a_none_target_forgets_our_target_record(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, None)
    assert pos.deal_id not in broker._deal_targets


async def test_set_stop_still_sends_only_the_stop_level(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    mark = since_mark(server)
    await broker.set_stop(pos.deal_id, NEW_STOP)
    assert server.log[mark].raw == '{"stopLevel":25742.3}'  # DE40 v1: set_stop bez zmian


async def test_set_stop_after_a_put_that_clears_the_target_leaves_the_position_untargeted(server, broker):
    # C5d w najgorszym wariancie (NIEPOTWIERDZONE): PUT bez `profitLevel` kasuje cel; adapter pokazuje prawdę brokera
    server.put_clears_target = True
    server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_stop(pos.deal_id, NEW_STOP)
    [listed] = await broker.list_positions()
    assert type(listed) is OpenPosition and listed.target_level is None


async def test_set_protection_keeps_the_target_even_if_a_put_without_it_would_clear_it(server, broker):
    server.put_clears_target = True
    server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, pos.target_level)
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.target_level) == (NEW_STOP, pos.target_level)


async def test_set_protection_on_a_position_we_did_not_open_is_remembered(server, broker, make_broker, clock):
    await broker.login()
    _, pos = await open_targeted(broker)
    restarted = await restart(make_broker, clock)
    await restarted.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    [listed] = await restarted.list_positions()
    assert (listed.stop_level, listed.stop_derived, listed.target_level, listed.target_derived) == (
        NEW_STOP, True, NEW_TARGET, True,
    )  # fmt: skip


async def test_set_protection_of_a_gone_deal_is_position_not_found_without_confirm(server, broker):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises(BrokerRejected) as info:
        await broker.set_protection("D-gone", NEW_STOP, NEW_TARGET)
    assert info.value.reason == "POSITION_NOT_FOUND"
    assert seq(server, mark) == ["PUT /positions/D-gone"]


async def test_a_target_on_the_loss_side_is_rejected_and_neither_level_changes(server, broker):
    server.position_stop_keys = server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_targeted(broker)
    with pytest.raises(BrokerRejected) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, BID - 100)
    assert info.value.reason.startswith("error.invalid.takeprofit.minvalue:")
    assert (only_position(server).stop_level, only_position(server).target_level) == (pos.stop_level, pos.target_level)
    assert await broker.list_positions() == [pos]


async def test_a_stop_too_close_to_the_market_is_rejected_and_the_target_does_not_change_either(server, broker):
    server.position_stop_keys = server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_targeted(broker)
    with pytest.raises(BrokerRejected) as info:
        await broker.set_protection(pos.deal_id, BID, NEW_TARGET)
    assert info.value.reason.startswith("error.invalid.stoploss.maxvalue:")
    assert (only_position(server).stop_level, only_position(server).target_level) == (pos.stop_level, pos.target_level)


async def test_set_protection_rejected_on_confirm_keeps_our_record(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "REJECTED", "reason": "TARGET_TOO_CLOSE", "dealReference": ref})
    with pytest.raises(BrokerRejected) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert info.value.reason == "TARGET_TOO_CLOSE"
    assert await broker.list_positions() == [pos]


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
async def test_set_protection_with_an_unknown_outcome_is_broker_timeout(server, make_broker, failure):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    if failure == "timeout":
        server.delay_next(f"/confirms/{ref}", 2.0)
    else:
        server.fail_next(f"/confirms/{ref}", *failure)
    with pytest.raises(BrokerTimeout) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert "protection outcome unknown" in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.target_level) == (pos.stop_level, pos.target_level)  # zapis bez zmian


@pytest.mark.parametrize(
    "affected",
    [[], [{"dealId": "OTHER-DEAL", "status": "AMENDED"}], [{"dealId": "{deal}", "status": "FULLY_CLOSED"}], None],
)
async def test_set_protection_confirm_must_name_our_deal_as_amended(server, broker, affected):
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    if affected:
        affected = [{**d, "dealId": d["dealId"].replace("{deal}", pos.deal_id)} for d in affected]
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "status": "OPEN",
                                               "affectedDeals": affected})  # fmt: skip
    with pytest.raises(BrokerTimeout, match="protection outcome unknown"):
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert await broker.list_positions() == [pos]


@pytest.mark.parametrize("reported", [OFFER + 44, None, Decimal("25837.4")])
async def test_set_protection_confirm_carrying_a_different_target_is_a_broker_error(server, broker, reported):
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "profitLevel": reported,
                                               "affectedDeals": [{"dealId": pos.deal_id, "status": "AMENDED"}]})  # fmt: skip
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert type(info.value) is BrokerError and "profitLevel" in str(info.value)
    assert await broker.list_positions() == [pos]


async def test_set_protection_confirm_carrying_a_different_stop_is_a_broker_error(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "stopLevel": BID - 50,
                                               "profitLevel": NEW_TARGET,
                                               "affectedDeals": [{"dealId": pos.deal_id, "status": "AMENDED"}]})  # fmt: skip
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert type(info.value) is BrokerError and "stopLevel" in str(info.value)


async def test_set_protection_confirm_with_both_levels_matching_is_accepted(server, broker):
    server.confirm_stop_keys = server.confirm_target_keys = True
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert server.confirms[f"p_{pos.deal_id}"]["profitLevel"] == NEW_TARGET


async def test_set_protection_with_a_none_target_rejects_a_confirmation_that_still_shows_a_target(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    ref = f"p_{pos.deal_id}"
    server.fail_next(f"/confirms/{ref}", 200, {"dealStatus": "ACCEPTED", "dealReference": ref, "profitLevel": pos.target_level,
                                               "affectedDeals": [{"dealId": pos.deal_id, "status": "AMENDED"}]})  # fmt: skip
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, None)
    assert type(info.value) is BrokerError and "profitLevel" in str(info.value)


@pytest.mark.parametrize(
    ("stop", "target", "error"),
    [
        (NEW_STOP, NEW_TARGET, None),
        (Decimal("25700.0"), NEW_TARGET, BrokerError),  # stop pozycji inny niż żądany
        (NEW_STOP, Decimal("25900.0"), BrokerError),  # cel pozycji inny niż żądany
        (NEW_STOP, None, BrokerError),  # broker mówi: pozycja bez celu
        (None, NEW_TARGET, BrokerError),  # broker mówi: pozycja bez stopu
    ],
)
async def test_set_protection_checks_both_levels_the_position_reports(server, broker, stop, target, error):
    await broker.login()
    _, pos = await open_targeted(broker)
    item = server._position_item(only_position(server))
    item["position"]["stopLevel"] = stop
    item["position"]["profitLevel"] = target
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")  # odczyt kontrolny po PUT
    if error is None:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    else:
        with pytest.raises(error) as info:
            await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
        assert type(info.value) is BrokerError


async def test_set_protection_accepts_a_position_reply_with_only_profit_distance(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    item = server._position_item(only_position(server))
    item["position"]["profitDistance"] = Decimal("99.9")  # odległość z nieznanego punktu odniesienia (C6)
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)  # samego `profitDistance` nie porównujemy
    assert only_position(server).target_level == NEW_TARGET


async def test_set_protection_with_a_null_profit_distance_is_a_broker_error(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    item = server._position_item(only_position(server))
    item["position"]["profitDistance"] = None  # broker mówi: brak celu
    server.fail_next(f"/positions/{pos.deal_id}", 200, item, method="GET")
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert type(info.value) is BrokerError


async def test_set_protection_without_a_target_rejects_a_position_that_still_has_one(server, broker):
    server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_targeted(broker)
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, None)  # nie umiemy skasować celu: nie udajemy sukcesu
    assert type(info.value) is BrokerError and "still has a target" in str(info.value)


async def test_set_protection_without_a_target_accepts_a_position_without_one(server, broker):
    server.position_target_keys = "present"
    await broker.login()
    _, pos = await open_confirmed(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, None)
    assert only_position(server).target_level is None


async def test_set_protection_without_the_target_key_after_keys_were_seen_is_an_unknown_outcome(server, broker):
    server.position_target_keys = "when-set"
    await broker.login()
    _, pos = await open_targeted(broker)  # klucze widoczne: pozycja pokazuje swój cel
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    only_position(server).target_level = None  # właściciel usunął cel: klucz znika
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")  # bez skutku
    with pytest.raises(BrokerTimeout, match="protection outcome unknown") as info:
        await broker.set_protection(pos.deal_id, Decimal("25732.3"), Decimal("25847.3"))
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert broker._deal_stops[pos.deal_id] == NEW_STOP and broker._deal_targets[pos.deal_id] == NEW_TARGET
    assert only_position(server).target_level is None  # nic nie zapisano z nieudanej próby


async def test_set_protection_without_the_stop_key_after_keys_were_seen_is_an_unknown_outcome(server, broker):
    server.position_stop_keys = "when-set"
    await broker.login()
    _, pos = await open_targeted(broker)
    only_position(server).stop_level = None  # właściciel usunął stop
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")  # bez skutku
    with pytest.raises(BrokerTimeout, match="protection outcome unknown"):
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ((404, {"errorCode": "error.not-found.dealId"}), BrokerRejected),
        ((500, {"errorCode": "error.internal"}), BrokerTimeout),
        ("timeout", BrokerTimeout),
    ],
)
async def test_set_protection_verification_failures(server, make_broker, failure, error):
    broker = make_broker(request_timeout_s=0.2)
    await broker.login()
    _, pos = await open_targeted(broker)
    if failure == "timeout":
        server.delay_next(f"/positions/{pos.deal_id}", 2.0, method="GET")
    else:
        server.fail_next(f"/positions/{pos.deal_id}", *failure, method="GET")
    with pytest.raises(error) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    if error is BrokerRejected:
        assert info.value.reason == "POSITION_NOT_FOUND"
    else:
        assert "protection outcome unknown" in str(info.value)
    assert broker._deal_targets[pos.deal_id] == pos.target_level  # nic nie zapisano z nieudanej próby


async def test_set_protection_with_nothing_reported_is_accepted_and_remembered(server, broker):
    """Pozostałe ryzyko (C3): bez poziomów w potwierdzeniu i w pozycji nieaktualnego potwierdzenia nie widać."""
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    server.fail_next(f"/positions/{pos.deal_id}", 200, {"dealReference": f"p_{pos.deal_id}"}, method="PUT")  # bez skutku
    await broker.set_protection(pos.deal_id, Decimal("25732.3"), Decimal("25847.3"))  # nieaktualne potwierdzenie
    [listed] = await broker.list_positions()
    assert (listed.stop_level, listed.target_level, listed.target_derived) == (
        Decimal("25732.3"), Decimal("25847.3"), True,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("stop", "target", "exc"),
    [
        (25742.3, NEW_TARGET, TypeError),
        ("25742.3", NEW_TARGET, TypeError),
        (None, NEW_TARGET, TypeError),
        (Decimal("NaN"), NEW_TARGET, ValueError),
        (Decimal("0"), NEW_TARGET, ValueError),
        (Decimal("-1"), NEW_TARGET, ValueError),
        (NEW_STOP, 25837.3, TypeError),
        (NEW_STOP, "25837.3", TypeError),
        (NEW_STOP, Decimal("NaN"), ValueError),
        (NEW_STOP, Decimal("Infinity"), ValueError),
        (NEW_STOP, Decimal("0"), ValueError),
        (NEW_STOP, Decimal("-1"), ValueError),
    ],
)
async def test_set_protection_validates_both_levels_before_sending(server, broker, stop, target, exc):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises(exc):
        await broker.set_protection("D-1", stop, target)
    assert seq(server, mark) == []


@pytest.mark.parametrize("deal_id", ["", "a/b", "..", "D 1", 5, None])
async def test_set_protection_validates_the_deal_id_before_sending(server, broker, deal_id):
    await broker.login()
    mark = since_mark(server)
    with pytest.raises((TypeError, ValueError)):
        await broker.set_protection(deal_id, NEW_STOP, NEW_TARGET)
    assert seq(server, mark) == []


async def test_set_protection_market_closed_and_5xx_are_mapped(server, broker):
    await broker.login()
    _, pos = await open_targeted(broker)
    server.fail_next(f"/positions/{pos.deal_id}", 400, {"errorCode": MARKET_CLOSED_CODE}, method="PUT")
    with pytest.raises(MarketClosed):
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    server.fail_next(f"/positions/{pos.deal_id}", 503, None, method="PUT")
    with pytest.raises(BrokerError) as info:
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert type(info.value) is BrokerError and str(info.value) == f"PUT /positions/{pos.deal_id}: HTTP 503"


async def test_a_put_is_never_resent_after_an_unknown_outcome(server, make_broker):
    broker = make_broker(request_timeout_s=0.1)
    await broker.login()
    _, pos = await open_targeted(broker)
    server.apply_then_drop_response(f"/positions/{pos.deal_id}", method="PUT")
    with pytest.raises(BrokerTimeout):
        await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)
    assert server.count("PUT", f"/positions/{pos.deal_id}") == 1  # ponowienie należy do silnika, nie do adaptera


# --- zamknięcie przez cel -------------------------------------------------------------------------------------


async def test_a_target_hit_is_reported_as_a_target_closure_at_the_target_level(server, broker, clock):
    await broker.login()
    _, pos = await open_targeted(broker)
    clock.advance(60)
    server.trigger_target(pos.deal_id)
    assert await broker.list_positions() == []
    [closed] = await broker.closed_since(pos.opened_at)
    assert (closed.deal_id, closed.epic, closed.exit_level, closed.reason) == (
        pos.deal_id, "DE40", pos.target_level, "TARGET",
    )  # fmt: skip
    assert at(60) <= closed.ts <= at(61) and broker.unknown_close_sources == 0


async def test_a_stop_hit_on_a_targeted_position_is_still_a_stop(server, broker, clock):
    await broker.login()
    _, pos = await open_targeted(broker)
    clock.advance(60)
    server.trigger_stop(pos.deal_id, pos.stop_level)
    [closed] = await broker.closed_since(pos.opened_at)
    assert (closed.reason, closed.exit_level) == ("STOP", pos.stop_level)


async def test_closing_by_our_delete_after_a_set_protection_is_manual(server, broker, clock):
    await broker.login()
    _, pos = await open_targeted(broker)
    await broker.set_protection(pos.deal_id, NEW_STOP, NEW_TARGET)  # wiersz EDIT_STOP_AND_LIMIT jest pomijany
    clock.advance(30)
    await broker.close_position(pos.deal_id)
    [closed] = await broker.closed_since(pos.opened_at)
    assert closed.reason == "MANUAL"
