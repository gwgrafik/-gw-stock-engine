"""Odczyty diagnostyczne próby pasma (`smoke --epic`, zadanie 17): pola celu zysku w surowych odpowiedziach brokera
(C5), wiersze historii transakcji (C8), bezpieczny wydruk wartości i symulacja w FakeBroker (C5d: `set_stop` kasuje
cel, brak pól celu w odpowiedziach, waluta pozycji).

Odczyty są tylko do odczytu i nie należą do protokołu `Broker` (silnik ich nie używa); próba woła je wyłącznie po
nazwie, a ich brak to „nie ustalono”.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.broker.diagnostics import (
    FieldsRead,
    SettlementRow,
    TargetFieldsReport,
    parse_transactions,
    show,
    split_fields,
)
from gwstock.broker.fake import FakeBroker
from gwstock.types import BrokerTimeout, InstrumentRules, Quote, SessionExpired, Side, TargetedEntryRequest

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
D = Decimal
US500 = InstrumentRules("US500", D("0.01"), D("0.01"), D("1"), D("0.05"), D("0.5"), D("0.1"), currency="USD")


def test_split_fields_separates_target_keys_from_the_useful_other_keys():
    target, other = split_fields({
        "profitLevel": D("6810.6"), "limitDistance": None, "profitAmount": D("1"), "dealId": "abc", "upl": D("-0.01"),
        "currency": "USD", "level": D("6800.6"), "stopLevel": D("6795"), "size": D("0.01"), "profit": D("3"),
    })  # fmt: skip
    assert target == {"profitLevel": D("6810.6"), "limitDistance": None, "profitAmount": D("1")}
    assert other == {"currency": "USD", "upl": D("-0.01"), "level": D("6800.6"), "stopLevel": D("6795")}
    assert split_fields(None) == ({}, {}) and split_fields([1]) == ({}, {})


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (D("6810.60"), "6810.60"), (D("1E+1"), "10"), (None, "null"), (True, "true"), ("USD", "USD"),
        ("deal 0123456789", "deal <NUM>"), ("AbCdEf0123456789AbCdEf0123", "<TOKEN>"),
        ("6a1f2c3d-1111-2222-3333-444455556666", "<ID>"), ({"a": 1}, "dict"), ("x" * 90, "x" * 59 + "…"),
    ],
)
def test_show_prints_numbers_plainly_and_masks_identifiers(value, text):
    assert show(value) == text


def test_parse_transactions_keeps_only_what_the_owner_may_see():
    rows = parse_transactions({"transactions": [
        {"dateUtc": "2026-11-02T14:51:00.420", "instrumentName": "US 500", "transactionType": "TRADE",
         "note": "Trade closed", "reference": "123456789", "size": "-0.04", "currency": "EUR", "status": "PROCESSED"},
        {"dateUTC": "2026-11-02T14:51:01", "transactionType": "CONVERSION", "size": "x", "currency": None},
        "junk",
    ]})  # fmt: skip
    assert rows == [
        SettlementRow("TRADE", "EUR", D("-0.04"), datetime(2026, 11, 2, 14, 51, 0, 420000, tzinfo=UTC), "US 500",
                      "Trade closed"),
        SettlementRow("CONVERSION", None, None, datetime(2026, 11, 2, 14, 51, 1, tzinfo=UTC), None, None),
    ]  # fmt: skip
    assert parse_transactions({}) == [] and parse_transactions(None) == []
    assert all("123456789" not in repr(r) for r in rows)  # referencja (powiązanie z dealId, C8) nie trafia nigdzie


@pytest.fixture
def fake(clock) -> FakeBroker:
    return FakeBroker(clock, US500, Quote("US500", D("6800.0"), D("6800.6"), clock.now_utc()), account_id="ACC-1")


async def enter(fake: FakeBroker) -> tuple[str, str]:
    ack = await fake.open_position(
        TargetedEntryRequest("gws-20261102-US500-1-smoke", "US500", Side.BUY, D("0.01"), D("5"), D("7.5"))
    )
    pos = await fake.confirm(ack.deal_reference)
    return pos.deal_id, ack.deal_reference


async def test_fake_reports_the_target_under_the_configured_keys(fake):
    deal, ref = await enter(fake)
    report = await fake.diagnose_target_fields(deal, ref)
    assert isinstance(report, TargetFieldsReport)
    assert report.position == FieldsRead(
        True, {"profitLevel": D("6808.1")},
        {"currency": "USD", "upl": D("-0.006"), "level": D("6800.6"), "stopLevel": D("6795.6")},
    )  # fmt: skip
    assert report.confirm.found and report.confirm.target == {"profitLevel": D("6808.1")}
    fake.target_keys = ("profitDistance", "limitLevel")
    report = await fake.diagnose_target_fields(deal, ref)
    assert report.position.target == {"profitDistance": D("7.5"), "limitLevel": D("6808.1")}
    assert "diagnose_target_fields" in fake.calls


async def test_fake_can_hide_the_target_fields_and_reports_unknown_records(fake):
    deal, ref = await enter(fake)
    fake.target_keys = ()
    report = await fake.diagnose_target_fields(deal, ref)
    assert report.position.found and report.position.target == {} and report.confirm.target == {}
    missing = await fake.diagnose_target_fields("DEAL-9", "REF-9")
    assert missing.position == FieldsRead(False) and missing.confirm == FieldsRead(False)


async def test_fake_set_stop_keeps_the_target_by_default_and_clears_it_when_simulating_c5d(fake):
    deal, ref = await enter(fake)
    await fake.set_stop(deal, D("6795"))
    assert fake.positions[deal].target_level == D("6808.1")
    fake.set_stop_clears_target = True
    await fake.set_stop(deal, D("6795"))
    assert fake.positions[deal].target_level is None and fake.positions[deal].stop_level == D("6795")
    report = await fake.diagnose_target_fields(deal, ref)
    assert report.position.target == {"profitLevel": None}  # klucz jest, cel skasowany (jawny null)
    await fake.set_protection(deal, D("6795"), D("6808"))
    assert fake.positions[deal].target_level == D("6808")


async def test_fake_diagnostics_take_failures_and_the_session_like_other_methods(fake):
    deal, ref = await enter(fake)
    fake.fail_next("diagnose_target_fields", BrokerTimeout("t"))
    with pytest.raises(BrokerTimeout):
        await fake.diagnose_target_fields(deal, ref)
    fake.expire_session()
    with pytest.raises(SessionExpired):
        await fake.diagnose_transactions(T0, T0 + timedelta(minutes=1))


async def test_fake_transactions_are_filtered_by_time(fake, clock):
    inside = SettlementRow("TRADE", "EUR", D("-0.04"), T0 + timedelta(seconds=30), "US 500", "Trade closed")
    outside = SettlementRow("TRADE", "EUR", D("1"), T0 - timedelta(hours=1), "US 500", "Trade closed")
    fake.transactions += [inside, outside]
    assert await fake.diagnose_transactions(T0, T0 + timedelta(minutes=1)) == [inside]
