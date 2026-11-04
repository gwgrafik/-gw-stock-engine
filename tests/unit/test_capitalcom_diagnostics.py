"""Odczyty diagnostyczne adaptera Capital.com dla próby pasma (`smoke --epic`, zadanie 17): surowe klucze celu zysku w
`GET /positions/{dealId}` i `GET /confirms/{dealReference}` (C5, C5d) oraz wiersze `GET /history/transactions` (C8).

Tylko lokalny serwer `fake_capital_server` (127.0.0.1); prawdziwa sieć nie jest używana. Odczyty są tylko do odczytu:
żadnego żądania innego niż GET, żadnej zmiany pamięci adaptera, nic z `dealId`, `dealReference` ani `reference` w
wartościach wyników.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.broker.capitalcom import CapitalComBroker
from gwstock.broker.diagnostics import FieldsRead, SettlementRow
from gwstock.types import BrokerError, Side, TargetedEntryRequest

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
OFFER = Decimal("25782.3")


@pytest.fixture
def server(fake_capital_server):
    return fake_capital_server


@pytest.fixture
def broker(fake_capital_server, capital_credentials, capital_http, clock):
    """Adapter z `target_epics={"DE40"}`: DE40 gra tu rolę instrumentu pasma z celem (jak w prawdziwej próbie pasma, w której
    `build_engine` przekazuje adapterowi epiki pasm z celem; dla epików spoza zbioru adapter nie otwiera pozycji z celem)."""
    return CapitalComBroker(
        capital_credentials, capital_http, clock, base_url=fake_capital_server.base_url, target_epics=frozenset({"DE40"})
    )


async def open_targeted(broker) -> tuple[str, str]:
    req = TargetedEntryRequest("gws-20261102-DE40-1-smoke", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                               Decimal("45"))  # fmt: skip
    ack = await broker.open_position(req)
    pos = await broker.confirm(ack.deal_reference)
    return pos.deal_id, ack.deal_reference


def gets_only(server, mark: int) -> bool:
    return all(r.method == "GET" for r in server.log[mark:])


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("present", {"profitLevel": OFFER + 45}),
        ("distance", {"profitDistance": Decimal("45")}),
        ("null", {"profitLevel": None}),
        ("absent", {}),
    ],
)
async def test_target_fields_come_raw_from_the_position_read(server, broker, keys, expected):
    server.position_target_keys = keys
    await broker.login()
    deal, ref = await open_targeted(broker)
    mark = len(server.log)
    report = await broker.diagnose_target_fields(deal, ref)
    assert report.position.found and report.position.target == expected
    assert report.position.other["level"] == OFFER and "upl" in report.position.other
    assert report.confirm.found and report.confirm.target == {}  # potwierdzenie bez `profitLevel` (domyślnie)
    assert gets_only(server, mark)
    assert [f"{r.method} {r.path}" for r in server.log[mark:]] == [
        f"GET /positions/{deal}", f"GET /confirms/{ref}"
    ]


async def test_confirm_target_keys_are_reported_when_the_broker_sends_them(server, broker):
    server.confirm_target_keys = True
    await broker.login()
    deal, ref = await open_targeted(broker)
    report = await broker.diagnose_target_fields(deal, ref)
    assert report.confirm.target == {"profitLevel": OFFER + 45}


async def test_a_closed_position_and_an_unknown_reference_are_not_found_not_errors(server, broker):
    await broker.login()
    deal, ref = await open_targeted(broker)
    await broker.close_position(deal)
    report = await broker.diagnose_target_fields(deal, None)
    assert report.position == FieldsRead(False) and report.confirm == FieldsRead(False)


async def test_a_put_that_clears_the_target_shows_in_the_raw_read(server, broker):
    server.position_target_keys = "present"
    server.put_clears_target = True
    await broker.login()
    deal, ref = await open_targeted(broker)
    await broker.set_stop(deal, OFFER - 30)
    report = await broker.diagnose_target_fields(deal, ref)
    assert report.position.target == {"profitLevel": None}


async def test_transactions_are_read_by_time_window_without_the_reference(server, broker, clock):
    server.transactions = [
        {"date": "2026-11-02T07:30:05", "dateUtc": "2026-11-02T07:30:05.420", "instrumentName": "US 500",
         "transactionType": "TRADE", "note": "Trade closed", "reference": "987654321", "size": "-0.04",
         "currency": "EUR", "status": "PROCESSED"},
        {"date": "2026-11-02T06:00:00", "dateUtc": "2026-11-02T06:00:00", "transactionType": "DEPOSIT", "size": "40",
         "currency": "EUR"},
    ]  # fmt: skip
    await broker.login()
    clock.advance(30)
    mark = len(server.log)
    rows = await broker.diagnose_transactions(T0, T0 + timedelta(hours=1))
    assert rows == [SettlementRow("TRADE", "EUR", Decimal("-0.04"),
                                  datetime(2026, 11, 2, 7, 30, 5, 420000, tzinfo=UTC), "US 500", "Trade closed")]
    [req] = server.log[mark:]
    assert req.method == "GET" and req.path == "/history/transactions"
    assert req.query == {"from": "2026-11-02T07:30:00", "to": "2026-11-02T07:30:30"}  # `to` obcięte do teraz


async def test_a_failed_read_raises_for_the_caller(server, broker):
    await broker.login()
    deal, ref = await open_targeted(broker)
    server.fail_next(f"/positions/{deal}", 500, method="GET")
    with pytest.raises(BrokerError):
        await broker.diagnose_target_fields(deal, ref)
