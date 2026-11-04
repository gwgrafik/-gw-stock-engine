"""Testy skryptu diagnostycznego research/diag_fx.py (waluty, tylko odczyt): parser odpowiedzi na danych syntetycznych,
zbieranie przez wstrzyknięte `get` i przebieg całości na atrapie sesji HTTP. Bez sieci, bez brokera, bez kluczy."""
import ast
import importlib.util
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "research" / "diag_fx.py"
pytestmark = pytest.mark.skipif(not SCRIPT.parent.exists(), reason="brak katalogu research/ (obraz testowy)")

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

# Wszystkie "numery", "identyfikatory" i "sekrety" poniżej są sztuczne. Wyjście skryptu nie może zawierać żadnego z nich.
ACCOUNT_A = "123456789012345678"
ACCOUNT_B = "223456789012345678"
DEAL_1 = "00006d8b-0055-31be-0000-00008a3a2b1f"
DEAL_2 = "00006d8b-0055-31be-0000-00008a3a2c00"
OWNER_NAME = "Jan Kowalski"
CREDS = SimpleNamespace(api_key="fake-key-ABCDEF123456", identifier="fake-user@example.test",
                        api_password="fake-pass-QWERTY987654")
CST = "fake-cst-TOKEN0123456789"
SEC = "fake-sec-TOKEN9876543210"
LEAKS = [ACCOUNT_A, ACCOUNT_B, DEAL_1, DEAL_2, OWNER_NAME, CREDS.api_key, CREDS.identifier, CREDS.api_password, CST, SEC]


@pytest.fixture(scope="module")
def fx():
    name = "diag_fx_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(name, None)


def J(fx, obj):
    """Jak w produkcji: JSON z liczbami zmiennoprzecinkowymi jako Decimal."""
    return fx.parse_json(json.dumps(obj))


# ---------------------------------------------------------------- dane syntetyczne

LOGIN = {
    "accountType": "CFD", "currencyIsoCode": "EUR", "currentAccountId": ACCOUNT_A, "clientId": "987654321",
    "accountInfo": {"balance": 40.12},
    "accounts": [
        {"accountId": ACCOUNT_A, "accountName": OWNER_NAME, "preferred": True, "accountType": "CFD", "currency": "EUR",
         "status": "ENABLED"},
        {"accountId": ACCOUNT_B, "accountName": "demo", "preferred": False, "accountType": "CFD", "currency": "USD"},
    ],
}

MARKET_US500 = {
    "instrument": {
        "epic": "US500", "name": "US 500", "type": "INDICES", "currency": "USD", "lotSize": 1, "marginFactor": 5,
        "marginFactorUnit": "PERCENTAGE", "country": "US", "conversionRate": 0.8597,
        "currencies": [{"code": "USD", "symbol": "$", "baseExchangeRate": 1.1632, "exchangeRate": 0.8597,
                        "isDefault": True}],
        "openingHours": {"mon": ["00:00 - 21:59", "23:05 - 00:00"], "fri": ["00:00 - 21:59"], "sat": [], "zone": "UTC"},
        "overnightFee": {"longRate": -0.0213, "shortRate": 0.0071, "swapChargeTimestamp": 1707948000000,
                         "swapChargeInterval": 1440},
    },
    "dealingRules": {
        "minStepDistance": {"unit": "POINTS", "value": 0.1}, "minDealSize": {"unit": "POINTS", "value": 0.01},
        "maxDealSize": {"unit": "POINTS", "value": 1000}, "minSizeIncrement": {"unit": "POINTS", "value": 0.01},
        "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01},
        "maxStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 90},
        "marketOrderPreference": "AVAILABLE_DEFAULT_ON", "trailingStopsPreference": "NOT_AVAILABLE",
    },
    "snapshot": {"marketStatus": "TRADEABLE", "bid": 6801.2, "offer": 6802.0, "updateTimeUTC": "2026-11-04T11:59:58.100",
                 "marketModes": ["REGULAR"], "delayTime": 0, "scalingFactor": 1, "decimalPlacesFactor": 1},
}

MARKET_EURUSD = {
    "instrument": {"epic": "EURUSD", "name": "EUR/USD", "type": "CURRENCIES", "currency": "USD", "lotSize": 1},
    "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 100}},
    "snapshot": {"marketStatus": "TRADEABLE", "bid": 1.16834, "offer": 1.16842, "updateTimeUTC": "2026-11-04T11:59:58.100"},
}

TRANSACTIONS = {"transactions": [
    {"date": "2026-11-02T11:12:00.420", "dateUtc": "2026-11-02T10:12:00.420", "instrumentName": "US 500",
     "transactionType": "TRADE", "note": "Trade closed", "reference": "123456789", "size": "-1.05", "currency": "USD",
     "status": "PROCESSED"},
    {"dateUtc": "2026-11-02T10:12:01.000", "instrumentName": "US 500", "transactionType": "CONVERSION",
     "note": "Conversion", "reference": "123456790", "size": "-0.90", "currency": "EUR", "status": "PROCESSED"},
    {"dateUTC": "2026-11-02T10:12:02.000", "instrumentName": "US 500", "transactionType": "FX_COMMISSION",
     "note": "FX commission", "reference": "x", "size": "-0.01", "currency": "EUR", "status": "PROCESSED"},
    {"dateUtc": "2026-11-02T09:00:00.000", "instrumentName": "Germany 40", "transactionType": "TRADE",
     "note": "Trade closed", "reference": "555555", "size": "0.40", "currency": "EUR", "status": "PROCESSED"},
    "junk", None, 5,
]}

ACTIVITY = {"activities": [
    {"dateUTC": "2026-11-02T08:05:03.100", "epic": "US500", "dealId": DEAL_1, "source": "USER", "type": "POSITION",
     "status": "ACCEPTED",
     "details": {"dealReference": "o_" + DEAL_1, "currency": "USD", "size": 0.01, "direction": "BUY", "level": 6801.2,
                 "guaranteedStop": False, "stopLevel": 6790.0, "profitLevel": 6830.0}},
    {"dateUTC": "2026-11-02T10:12:00.420", "epic": "US500", "dealId": DEAL_1, "source": "TP", "type": "POSITION",
     "status": "ACCEPTED",
     "details": {"dealReference": "p_" + DEAL_1, "currency": "USD", "size": 0.01, "direction": "SELL", "level": 6830.0}},
    {"dateUTC": "2026-11-02T09:00:00.000", "epic": "DE40", "dealId": DEAL_2, "source": "SL", "type": "POSITION",
     "status": "ACCEPTED", "details": {"dealReference": "p_" + DEAL_2, "currency": "EUR", "direction": "SELL",
                                       "level": 24000.5}},
    None, "x", {"epic": None},
]}

POSITIONS = {"positions": [
    {"position": {"contractSize": 1, "createdDateUTC": "2026-11-04T09:30:03.100", "dealId": DEAL_1,
                  "dealReference": "p_" + DEAL_1, "workingOrderId": DEAL_2, "size": 0.01, "leverage": 20, "upl": -0.04,
                  "direction": "BUY", "level": 6801.2, "currency": "USD", "guaranteedStop": False,
                  "stopLevel": 6790.0, "stopDistance": None, "profitLevel": 6830.0, "profitDistance": None,
                  "profitAmount": None},
     "market": {"epic": "US500", "instrumentType": "INDICES", "lotSize": 1, "bid": 6801.0, "offer": 6801.9,
                "marketStatus": "TRADEABLE"}},
    {"position": {"dealId": DEAL_2, "direction": "SELL", "size": 0.001, "currency": "EUR", "upl": 0.2},
     "market": {"epic": "DE40"}},
    None, "x", {"position": None},
]}

CONFIRM = {"date": "2026-11-02T10:12:00.420", "status": "OPEN", "dealStatus": "ACCEPTED", "epic": "US500",
           "dealReference": "o_" + DEAL_1, "dealId": DEAL_2, "affectedDeals": [{"dealId": DEAL_1, "status": "OPENED"}],
           "level": 6801.2, "size": 0.01, "direction": "BUY", "guaranteedStop": False, "trailingStop": False,
           "stopLevel": 6790.0, "profitLevel": 6830.0, "profit": -0.04, "profitCurrency": "USD"}

SEARCH = {"markets": [
    {"epic": "EURUSD", "instrumentName": "EUR/USD", "instrumentType": "CURRENCIES", "marketStatus": "TRADEABLE",
     "bid": 1.16834, "offer": 1.16842, "updateTimeUTC": "2026-11-04T11:59:58.100"},
    {"epic": "EURUSD_W", "instrumentName": "EUR/USD weekend", "instrumentType": "CURRENCIES"},
    {"epic": "EURGBP", "instrumentName": "EUR/GBP", "instrumentType": "CURRENCIES"},
    None,
]}

JUNK = [None, [], "x", 5, True, {}, [None, 1], {"instrument": None}, {"instrument": []}, {"positions": "x"},
        {"positions": [None, 1, {"position": None}]}, {"transactions": {"a": 1}}, {"activities": [None, "x"]},
        {"markets": "x"}, {"accounts": "x"}]


def full_routes(**over):
    routes = {
        "/markets/US500": (200, MARKET_US500),
        "/history/transactions": (200, TRANSACTIONS),
        "/history/activity": (200, ACTIVITY),
        "/markets/EURUSD": (200, MARKET_EURUSD),
        "/positions": (200, POSITIONS),
    }
    routes.update(over)
    return routes


def make_get(fx, routes, log):
    async def get(path, params=None):
        log.append((path, dict(params or {})))
        route = routes.get(path)
        if route is None and path.startswith("/confirms/"):
            route = (200, CONFIRM)
        if route is None:
            route = (404, {"errorCode": "error.not-found"})
        if callable(route):
            route = route(dict(params or {}))
        status, payload = route
        return status, (J(fx, payload) if payload is not None else None)
    return get


async def no_sleep(_seconds):
    return None


# ---------------------------------------------------------------- konfiguracja

def test_config_defaults(fx):
    cfg = fx.load_config({})
    assert cfg["epic"] == "US500" and cfg["fx_epic"] == "EURUSD" and cfg["search"] == "EURUSD"
    assert cfg["days"] == 14 and cfg["confirms"] == 3
    assert cfg["data_dir"] == "/data" and cfg["out"] == "/data/diag_fx_us500.json"
    assert cfg["key_file"] == "/secrets/capitalcom.json"


def test_config_from_env_is_normalised_and_clamped(fx):
    cfg = fx.load_config({"GW_EPIC": "us100", "GW_DAYS": "999", "GW_CONFIRMS": "99", "GW_DATA_DIR": "/x"})
    assert cfg["epic"] == "US100" and cfg["days"] == 60 and cfg["confirms"] == 5
    assert cfg["out"] == "/x/diag_fx_us100.json"
    low = fx.load_config({"GW_DAYS": "0", "GW_CONFIRMS": "-4"})
    assert low["days"] == 1 and low["confirms"] == 0
    junk = fx.load_config({"GW_DAYS": "abc", "GW_CONFIRMS": ""})
    assert junk["days"] == 14 and junk["confirms"] == 3
    assert fx.load_config({"GW_DIAG_OUT": "/tmp/o.json"})["out"] == "/tmp/o.json"


# ---------------------------------------------------------------- maskowanie i formatowanie

def test_mask_hides_account_numbers_ids_emails_and_tokens(fx):
    assert fx.mask(ACCOUNT_A) == "<NUM>"
    assert fx.mask("konto 123456 ok") == "konto <NUM> ok"
    assert fx.mask(DEAL_1) == "<ID>"
    assert fx.mask("o_" + DEAL_1) == "o_<ID>"
    assert fx.mask("jan.kowalski@example.test") == "<EMAIL>"
    assert fx.mask("AbCdEfGhIjKlMnOpQrStUv123456") == "<TOKEN>"


def test_mask_keeps_prices_decimals_dates_and_names(fx):
    for text in ("25782.3", "1.168345", "123456.78", "-1234567.89", "US500", "US 500", "2026-11-04T11:59:58.100",
                 "AVAILABLE_DEFAULT_ON", "TRAILING_STOPS_NOT_AVAILABLE", "12345", "0.00012345678"):
        assert fx.mask(text) == text


def test_fmt_formats_values_without_surprises(fx):
    assert fx.fmt(Decimal("1E+3")) == "1000"
    assert fx.fmt(Decimal("1.0500")) == "1.0500"
    assert fx.fmt(True) == "true" and fx.fmt(None) == "null"
    assert fx.fmt(1000) == "1000" and fx.fmt(10 ** 12) == "<NUM>"
    assert fx.fmt(["a", Decimal("1.5")]) == "[a, 1.5]"
    assert fx.fmt({"a": 1}) == "{a=1}"
    assert fx.fmt(DEAL_1) == "<ID>"
    long = fx.fmt("x" * 200)
    assert len(long) <= 80 and long.endswith("…")
    assert len(fx.fmt(list(range(50)))) < 120  # listy też są przycinane


def test_scrub_removes_known_secrets_longest_first_and_ignores_short_ones(fx):
    assert fx.scrub("x fake-secret-token y", ["fake-secret-token"]) == "x <SECRET> y"
    assert fx.scrub("fake-secret-token", ["fake-secret", "fake-secret-token"]) == "<SECRET>"
    assert fx.scrub("abc", ["ab", "", None]) == "abc"


def test_mask_removes_known_secrets_before_pattern_masking_so_no_fragment_survives(fx):
    secret = "fake-key-ABCDEF123456"
    fx._SECRETS[:] = [secret]
    try:
        masked = fx.mask("bad key " + secret + " ok")
    finally:
        fx._SECRETS.clear()
    assert masked == "bad key <SECRET> ok"
    assert fx.mask("bad key " + secret) != "bad key <SECRET>"  # po wyczyszczeniu rejestru zostaje tylko maskowanie wzorcami


def test_config_rejects_unsafe_epics(fx):
    cfg = fx.load_config({"GW_EPIC": "../etc/x", "GW_FX_EPIC": "a b"})
    assert cfg["epic"] == "US500" and cfg["fx_epic"] == "EURUSD" and cfg["out"] == "/data/diag_fx_us500.json"


def test_parse_json_gives_decimals_and_none_on_garbage(fx):
    value = fx.parse_json('{"a": 1.10, "b": 2}')
    assert str(value["a"]) == "1.10" and value["b"] == 2
    for bad in ("not json", "", None, '{"a": NaN}', '{"a": 1e999999999999999999999}', '{"a": Infinity}'):
        assert fx.parse_json(bad) is None


def test_window_ranges_cover_the_period_without_gaps_and_respect_the_span(fx):
    wins = fx.window_ranges(NOW, 3, 1)
    assert wins[-1][1] == "2026-11-04T12:00:00"
    fmt = "%Y-%m-%dT%H:%M:%S"
    parsed = [(datetime.strptime(a, fmt), datetime.strptime(b, fmt)) for a, b in wins]
    for frm, to in parsed:
        assert (to - frm).total_seconds() <= 86400
    for (_, prev_to), (nxt_from, _) in zip(parsed, parsed[1:]):
        assert (prev_to - nxt_from).total_seconds() == 1  # sąsiednie okna zachodzą o 1 s
    assert len(wins) == 3  # dokładnie jedno okno na dobę, bez okna na samą resztę z nakładek
    assert datetime(2026, 11, 1, 12, 0, 0) <= parsed[0][0] <= datetime(2026, 11, 1, 12, 1, 0)  # najstarsze sekundy mogą odpaść
    two_weeks = fx.window_ranges(NOW, 14, 7)
    assert len(two_weeks) == 2 and "2026-10-21T12:00:00" <= two_weeks[0][0] <= "2026-10-21T12:01:00"
    assert fx.window_ranges(NOW, 1, 7) == [("2026-11-03T12:00:00", "2026-11-04T12:00:00")]  # okno nie sięga dalej niż zakres


# ---------------------------------------------------------------- parser: instrument

def test_describe_market_extracts_currency_rules_and_quote(fx):
    info = fx.describe_market(J(fx, MARKET_US500))
    assert info["readable"] is True
    assert (info["epic"], info["name"], info["type"], info["currency"]) == ("US500", "US 500", "INDICES", "USD")
    assert info["lotSize"] == 1 and info["marginFactor"] == 5 and info["marginFactorUnit"] == "PERCENTAGE"
    [cur] = info["currencies"]
    assert cur["code"] == "USD" and cur["exchangeRate"] == Decimal("0.8597") and cur["baseExchangeRate"] == Decimal("1.1632")
    assert cur["isDefault"] is True and cur["symbol"] == "$"
    assert "instrument.currency" in info["currency_fields"] and "instrument.conversionRate" in info["currency_fields"]
    assert info["rules"]["minDealSize"] == {"value": Decimal("0.01"), "unit": "POINTS"}
    assert info["rules"]["minStopOrProfitDistance"]["unit"] == "PERCENTAGE"
    assert info["rules"]["marketOrderPreference"] == {"value": "AVAILABLE_DEFAULT_ON", "unit": None}
    q = info["quote"]
    assert q["bid"] == Decimal("6801.2") and q["offer"] == Decimal("6802.0") and q["spread"] == Decimal("0.8")
    assert q["marketStatus"] == "TRADEABLE" and q["marketModes"] == ["REGULAR"]
    assert q["updateTimeUTC"] == "2026-11-04T11:59:58.100"
    assert info["hours"]["zone"] == "UTC" and info["hours"]["days"]["mon"] == ["00:00 - 21:59", "23:05 - 00:00"]
    assert info["overnight"]["swapChargeInterval"] == 1440


def test_describe_market_uses_updatetime_when_utc_time_is_missing_and_has_no_spread_without_both_sides(fx):
    payload = {"snapshot": {"bid": 1.5, "updateTime": "2026-11-04T12:59:58"}}
    info = fx.describe_market(J(fx, payload))
    assert info["quote"]["updateTimeUTC"] is None and info["quote"]["updateTime"] == "2026-11-04T12:59:58"
    assert info["quote"]["spread"] is None


def test_describe_market_finds_currency_hints_in_unknown_places(fx):
    payload = {"instrument": {"epic": "X", "settlement": {"fxMarkup": 0.004, "other": 1}},
               "snapshot": {"quoteCurrencyRate": 0.9}}
    info = fx.describe_market(J(fx, payload))
    assert info["currency_fields"]["instrument.settlement.fxMarkup"] == Decimal("0.004")
    assert info["currency_fields"]["snapshot.quoteCurrencyRate"] == Decimal("0.9")
    assert "instrument.settlement.other" not in info["currency_fields"]


def test_describe_market_survives_junk_and_currency_list_of_wrong_shape(fx):
    for junk in JUNK:
        info = fx.describe_market(junk)
        assert info["readable"] is False and info["epic"] is None and info["currencies"] == []
    odd = fx.describe_market({"instrument": {"currencies": [None, "USD", {"code": "USD"}, 5]}})
    assert [c["code"] for c in odd["currencies"]] == ["USD"]
    assert fx.describe_market({"instrument": {"currencies": {"code": "USD"}}})["currencies"] == []


# ---------------------------------------------------------------- parser: rachunki

def test_describe_accounts_flags_active_and_bound_accounts_without_ids(fx):
    acc = fx.describe_accounts(J(fx, LOGIN), bound_id=ACCOUNT_B)
    assert acc["count"] == 2 and acc["session_currency"] == "EUR"
    assert [a["currency"] for a in acc["accounts"]] == ["EUR", "USD"]
    assert [a["active"] for a in acc["accounts"]] == [True, False]
    assert [a["bound"] for a in acc["accounts"]] == [False, True]
    assert acc["active_currency"] == "EUR" and acc["bound_currency"] == "USD" and acc["bound_is_active"] is False
    dump = repr(acc)
    assert ACCOUNT_A not in dump and ACCOUNT_B not in dump and OWNER_NAME not in dump


def test_describe_accounts_without_binding_or_with_junk(fx):
    acc = fx.describe_accounts(J(fx, LOGIN))
    assert acc["bound_is_active"] is None and acc["bound_currency"] is None and acc["active_currency"] == "EUR"
    unknown = fx.describe_accounts(J(fx, LOGIN), bound_id="999999999")
    assert unknown["bound_is_active"] is None and unknown["bound_found"] is False
    for junk in JUNK:
        assert fx.describe_accounts(junk)["count"] == 0
    assert fx.describe_accounts(J(fx, {"accounts": [None, 3, {"currency": None}]}))["count"] == 1


# ---------------------------------------------------------------- parser: historia transakcji

def test_parse_and_summarize_transactions(fx):
    rows = fx.parse_transactions(J(fx, TRANSACTIONS))
    assert len(rows) == 4 and rows[0]["type"] == "TRADE" and rows[0]["date_utc"] == "2026-11-02T10:12:00.420"
    assert rows[2]["date_utc"] == "2026-11-02T10:12:02.000"  # wariant nazwy pola dateUTC
    summary = fx.summarize_transactions(rows, ["US500", "US 500"], ["123456789"])
    assert summary["total"] == 4 and summary["currencies"] == ["EUR", "USD"]
    assert summary["fx_counts"]["CONVERSION"] == 1 and summary["fx_counts"]["FX_COMMISSION"] == 1
    assert summary["instrument_total"] == 3 and summary["instrument_currencies"] == ["EUR", "USD"]
    [closed] = summary["instrument_closed"]
    assert closed["currency"] == "USD" and closed["size"] == "-1.05" and closed["link"] == "exact"
    assert {"type": "TRADE", "note": "Trade closed", "currency": "EUR", "status": "PROCESSED", "count": 1} in summary["kinds"]
    assert len(summary["fx_rows"]) == 2


def test_summarize_transactions_other_instrument_and_empty(fx):
    rows = fx.parse_transactions(J(fx, TRANSACTIONS))
    other = fx.summarize_transactions(rows, ["JP225"], [])
    assert other["instrument_total"] == 0 and other["instrument_closed"] == [] and other["fx_counts"]["CONVERSION"] == 1
    for junk in JUNK:
        empty = fx.summarize_transactions(fx.parse_transactions(junk), ["US500"], [])
        assert empty["total"] == 0 and empty["instrument_closed"] == []


def test_link_kind(fx):
    assert fx.link_kind("123456789", ["123456789"]) == "exact"
    assert fx.link_kind("456789", [DEAL_1 + "123456789"]) == "partial"
    assert fx.link_kind("123", ["0001234"]) == "none"  # za krótka, żeby coś znaczyła
    assert fx.link_kind("", ["x"]) == "missing" and fx.link_kind(None, ["x"]) == "missing"
    assert fx.link_kind("zzzzzzzz", []) == "none"


# ---------------------------------------------------------------- parser: historia aktywności

def test_parse_and_summarize_activities(fx):
    rows = fx.parse_activities(J(fx, ACTIVITY))
    assert len(rows) == 4  # słowniki; None i napis odpadają
    summary = fx.summarize_activities(rows, "US500")
    assert summary["total"] == 4 and summary["epic_total"] == 2
    assert summary["by_epic_currency"]["US500"] == {"USD": 2} and summary["by_epic_currency"]["DE40"] == {"EUR": 1}
    [closing] = summary["closing"]
    assert closing["source"] == "TP" and closing["currency"] == "USD" and closing["direction"] == "SELL"
    assert closing["level"] == Decimal("6830.0") and closing["date_utc"] == "2026-11-02T10:12:00.420"
    assert "profitLevel" in summary["detail_keys"] and "stopLevel" in summary["detail_keys"]
    assert {"source": "TP", "type": "POSITION", "status": "ACCEPTED", "currency": "USD", "count": 1} in summary["kinds"]


def test_a_lone_system_closing_row_counts_as_closing_and_junk_is_harmless(fx):
    def row(deal, stamp, source):
        return {"dateUTC": stamp, "epic": "US500", "dealId": deal, "source": source, "type": "POSITION", "status": "ACCEPTED",
                "details": {"currency": "USD", "direction": "SELL", "level": 1.5}}

    rows = fx.parse_activities(J(fx, {"activities": [
        row("d1", "2026-11-02T10:12:00", "SL"), row("d2", "2026-11-02T10:13:00", "USER"),
        row("d3", "2026-11-02T10:14:00", "TP"), row("d4", "2026-11-02T10:15:00", "CLOSE_OUT")]}))
    summary = fx.summarize_activities(rows, "US500")
    assert [c["source"] for c in summary["closing"]] == ["CLOSE_OUT", "TP", "SL"]  # najnowsze pierwsze; samotne USER to otwarcie
    for junk in JUNK:
        empty = fx.summarize_activities(fx.parse_activities(junk), "US500")
        assert empty["total"] == 0 and empty["closing"] == [] and empty["by_epic_currency"] == {}


def test_activity_rows_are_deduplicated_across_overlapping_windows(fx):
    rows = fx.parse_activities(J(fx, ACTIVITY))
    assert len(fx.dedupe(rows + rows)) == len(rows)


def test_pick_references_prefers_the_epic_newest_first_and_deduplicates(fx):
    rows = fx.parse_activities(J(fx, ACTIVITY))
    positions = fx.describe_positions(J(fx, POSITIONS))["items"]
    assert fx.pick_references(rows, positions, "US500", 0) == []
    assert fx.pick_references(rows, positions, "US500", 2) == ["p_" + DEAL_1, "o_" + DEAL_1]
    assert fx.pick_references(rows, positions, "US500", 9) == ["p_" + DEAL_1, "o_" + DEAL_1, "p_" + DEAL_2]
    assert fx.pick_references([], positions, "US500", 3) == ["p_" + DEAL_1]
    assert fx.pick_references([], [], "US500", 3) == []


# ---------------------------------------------------------------- parser: pozycje i potwierdzenia

def test_split_profit_fields(fx):
    target, result = fx.split_profit_fields({"profitLevel": 1, "limitDistance": 2, "profit": 3, "upl": 4, "stopLevel": 5,
                                             "profitCurrency": "USD", "profitAmount": None})
    assert target == {"profitLevel": 1, "limitDistance": 2, "profitAmount": None}
    assert result == {"profit": 3, "upl": 4, "profitCurrency": "USD"}
    assert fx.split_profit_fields("junk") == ({}, {})


def test_describe_positions_reports_target_fields_even_when_null(fx):
    info = fx.describe_positions(J(fx, POSITIONS))
    assert info["count"] == 2 and len(info["items"]) == 2
    first, second = info["items"]
    assert first["epic"] == "US500" and first["currency"] == "USD" and first["upl"] == Decimal("-0.04")
    assert first["direction"] == "BUY" and first["size"] == Decimal("0.01")
    assert first["target_fields"] == {"profitLevel": Decimal("6830.0"), "profitDistance": None, "profitAmount": None}
    assert first["stop_fields"] == {"stopLevel": Decimal("6790.0"), "stopDistance": None}
    assert first["result_fields"] == {"upl": Decimal("-0.04")}
    assert "profitLevel" in first["position_keys"] and "market_keys" in first
    assert second["target_fields"] == {} and second["epic"] == "DE40"
    assert info["target_keys"] == ["profitAmount", "profitDistance", "profitLevel"]
    assert first["deal_reference"] == "p_" + DEAL_1


def test_describe_positions_empty_and_junk(fx):
    assert fx.describe_positions(J(fx, {"positions": []}))["count"] == 0
    for junk in JUNK:
        info = fx.describe_positions(junk)
        assert info["count"] == 0 and info["items"] == [] and info["target_keys"] == []


def test_describe_confirm(fx):
    info = fx.describe_confirm(J(fx, CONFIRM))
    assert info["status"] == "OPEN" and info["deal_status"] == "ACCEPTED" and info["affected"] == 1
    assert info["target_fields"] == {"profitLevel": Decimal("6830.0")}
    assert info["result_fields"] == {"profit": Decimal("-0.04"), "profitCurrency": "USD"}
    assert {"profitLevel", "profit", "profitCurrency", "stopLevel"} <= set(info["keys"])
    for junk in JUNK:
        empty = fx.describe_confirm(junk)
        assert isinstance(empty["keys"], list) and empty["target_fields"] == {} and empty["result_fields"] == {}
        assert empty["affected"] == 0
    assert fx.describe_confirm(None)["keys"] == [] and fx.describe_confirm([1])["keys"] == []


# ---------------------------------------------------------------- para walutowa

def test_parse_search_and_pick_fx_epic(fx):
    rows = fx.parse_search(J(fx, SEARCH))
    assert len(rows) == 3 and rows[0]["epic"] == "EURUSD" and rows[0]["bid"] == Decimal("1.16834")
    assert fx.pick_fx_epic(rows) == "EURUSD"
    assert fx.pick_fx_epic([r for r in rows if r["epic"] != "EURUSD"]) == "EURUSD_W"  # bez dokładnego epiku: najkrótszy z EURUSD
    by_name = [{"epic": "CS.D.EURUSD.CFD.IP", "name": "EUR/USD", "type": "CURRENCIES"}]
    assert fx.pick_fx_epic(by_name) == "CS.D.EURUSD.CFD.IP"
    by_epic = [{"epic": "EURUSDX", "name": "Euro / US Dollar", "type": "CURRENCIES"}]
    assert fx.pick_fx_epic(by_epic) == "EURUSDX"
    assert fx.pick_fx_epic([{"epic": "GBPUSD", "name": "GBP/USD", "type": "CURRENCIES"}]) is None
    assert fx.pick_fx_epic([{"epic": "EURUSD3L", "name": "EUR USD 3x", "type": "SHARES"}]) is None
    assert fx.pick_fx_epic([]) is None
    for junk in JUNK:
        assert fx.pick_fx_epic(fx.parse_search(junk)) is None


def test_pick_fx_epic_prefers_the_exact_epic_then_the_exact_name_then_the_shortest_epic(fx):
    rows = [{"epic": "EURUSD_W", "name": "EUR/USD weekend", "type": "CURRENCIES"},
            {"epic": "EURUSD", "name": "x", "type": "CURRENCIES"}]
    assert fx.pick_fx_epic(rows) == "EURUSD"
    rows = [{"epic": "AAA_EURUSD_LONG", "name": "other", "type": "CURRENCIES"},
            {"epic": "BBB", "name": "EUR/USD", "type": "CURRENCIES"}]
    assert fx.pick_fx_epic(rows) == "BBB"
    rows = [{"epic": "EURUSD_LONGER", "name": "o1", "type": "CURRENCIES"}, {"epic": "EURUSD_W", "name": "o2", "type": "CURRENCIES"}]
    assert fx.pick_fx_epic(rows) == "EURUSD_W"


def test_quote_age(fx):
    assert fx.quote_age_s("2026-11-04T11:59:58.100", NOW) == Decimal("1.9")
    assert fx.quote_age_s("2026-11-04T11:59:58", NOW) == Decimal("2")
    assert fx.quote_age_s("2026-11-04T11:59:58Z", NOW) == Decimal("2")
    assert fx.quote_age_s("2026-11-04T12:00:05", NOW) == Decimal("-5")  # zegar brokera w przód: wartość ujemna jest jawna
    for bad in (None, "", "junk", 5, "2026-13-45T00:00:00"):
        assert fx.quote_age_s(bad, NOW) is None


def test_load_bound_account_is_tolerant(fx, tmp_path):
    path = tmp_path / "account.json"
    assert fx.load_bound_account(path) is None
    path.write_text(json.dumps({"account_id": ACCOUNT_A}))
    assert fx.load_bound_account(path) == ACCOUNT_A
    for bad in ("not json", "[]", json.dumps({"account_id": 5}), json.dumps({"other": "x"}), ""):
        path.write_text(bad)
        assert fx.load_bound_account(path) is None


def test_write_raw_writes_numbers_as_numbers_and_reports_failures(fx, tmp_path):
    path = tmp_path / "raw.json"
    message = fx.write_raw(path, {"US500": J(fx, MARKET_US500)})
    assert str(path) in message
    data = json.loads(path.read_text())
    assert data["US500"]["snapshot"]["bid"] == 6801.2 and data["US500"]["instrument"]["currencies"][0]["code"] == "USD"
    failed = fx.write_raw(tmp_path, {"x": 1})  # katalog zamiast pliku
    assert "nie zapisano" in failed and "IsADirectoryError" in failed


# ---------------------------------------------------------------- zbieranie (wstrzyknięte get)

async def test_collect_requests_only_read_endpoints_in_order(fx):
    log = []
    cfg = fx.load_config({"GW_DAYS": "2", "GW_CONFIRMS": "2"})
    report = await fx.collect(make_get(fx, full_routes(), log), cfg, NOW, J(fx, LOGIN), ACCOUNT_A)
    paths = [p for p, _ in log]
    n_act = len(fx.window_ranges(NOW, 2, 1))
    assert paths[0] == "/markets/US500" and paths[1] == "/history/transactions"
    assert paths[2:2 + n_act] == ["/history/activity"] * n_act
    assert paths[2 + n_act:] == ["/markets/EURUSD", "/positions", "/confirms/p_" + DEAL_1, "/confirms/o_" + DEAL_1]
    assert all(p.startswith(("/markets", "/history/", "/positions", "/confirms/")) for p in paths)
    assert report["aborted"] is None and report["accounts"]["active_currency"] == "EUR"
    assert report["market"]["info"]["currency"] == "USD" and report["fx"]["epic"] == "EURUSD"
    assert report["fx"]["info"]["quote"]["bid"] == Decimal("1.16834")
    assert report["fx"]["age_s"] == Decimal("1.9")
    assert report["positions"]["info"]["count"] == 2
    assert [c["status"] for c in report["confirms"]] == [200, 200]
    assert report["confirms"][0]["info"]["target_fields"] == {"profitLevel": Decimal("6830.0")}
    assert set(report["raw"]) == {"US500", "EURUSD"}
    assert report["requests"] == len(log)
    # parametry zapytań o historię: zakres, szczegóły, filtr po instrumencie
    first_act = next(p for path, p in log if path == "/history/activity")
    assert first_act["detailed"] == "true" and first_act["filter"] == "epic==US500" and "from" in first_act and "to" in first_act
    tx = next(p for path, p in log if path == "/history/transactions")
    assert set(tx) == {"from", "to"}


async def test_collect_searches_for_the_pair_when_the_direct_epic_is_unknown(fx):
    log = []
    routes = full_routes(**{"/markets/EURUSD": (404, {"errorCode": "error.not-found.epic"}),
                            "/markets": (200, {"markets": [{"epic": "CS.D.EURUSD.CFD.IP", "instrumentName": "EUR/USD",
                                                             "instrumentType": "CURRENCIES"}]}),
                            "/markets/CS.D.EURUSD.CFD.IP": (200, MARKET_EURUSD)})
    report = await fx.collect(make_get(fx, routes, log), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    assert ("/markets", {"searchTerm": "EURUSD"}) in log and "/markets/CS.D.EURUSD.CFD.IP" in [p for p, _ in log]
    assert report["fx"]["epic"] == "CS.D.EURUSD.CFD.IP" and report["fx"]["direct_status"] == 404
    assert report["fx"]["candidates"][0]["epic"] == "CS.D.EURUSD.CFD.IP"


async def test_collect_does_not_refetch_the_same_epic_and_keeps_the_quote_from_the_search_row(fx):
    log = []
    routes = full_routes(**{"/markets/EURUSD": (404, {"errorCode": "error.not-found.epic"}), "/markets": (200, SEARCH)})
    report = await fx.collect(make_get(fx, routes, log), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    assert [p for p, _ in log].count("/markets/EURUSD") == 1
    assert report["fx"]["epic"] == "EURUSD" and report["fx"]["info"] is None
    assert report["fx"]["candidate"]["bid"] == Decimal("1.16834") and report["fx"]["status"] == 404
    text = "\n".join(fx.render(report))
    assert "z wyszukiwania" in text and "bid=1.16834" in text


async def test_collect_reports_when_no_pair_epic_is_found(fx):
    routes = full_routes(**{"/markets/EURUSD": (404, {"errorCode": "error.not-found.epic"}),
                            "/markets": (200, {"markets": []})})
    report = await fx.collect(make_get(fx, routes, []), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    assert report["fx"]["epic"] is None and report["fx"]["info"] is None


async def test_collect_falls_back_to_unfiltered_history_when_the_filter_is_rejected(fx):
    log = []

    def activity(params):
        return (400, {"errorCode": "error.invalid.filter"}) if "filter" in params else (200, ACTIVITY)

    cfg = fx.load_config({"GW_DAYS": "2"})
    report = await fx.collect(make_get(fx, full_routes(**{"/history/activity": activity}), log), cfg, NOW, None, None)
    acts = [p for path, p in log if path == "/history/activity"]
    assert "filter" in acts[0] and all("filter" not in p for p in acts[1:])
    assert len(acts) == len(fx.window_ranges(NOW, 2, 1)) + 1  # pierwsze okno powtórzone bez filtra
    assert report["activity"]["filter"] == "rejected" and report["activity"]["summary"]["epic_total"] > 0


async def test_collect_stops_at_rate_limit_or_auth_errors(fx):
    for code in (429, 401, 403):
        log = []
        routes = full_routes(**{"/history/transactions": (code, {"errorCode": "error.too-many.requests"})})
        report = await fx.collect(make_get(fx, routes, log), fx.load_config({}), NOW, None, None)
        assert [p for p, _ in log] == ["/markets/US500", "/history/transactions"]
        assert str(code) in report["aborted"]


async def test_collect_survives_failed_sections_and_missing_confirms(fx):
    log = []
    routes = {"/markets/US500": (404, {"errorCode": "error.not-found.epic"}),
              "/history/transactions": (500, None), "/history/activity": (200, {"activities": "x"}),
              "/positions": (200, {"positions": []})}
    report = await fx.collect(make_get(fx, routes, log), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    assert report["market"]["status"] == 404 and report["market"]["error"] == "error.not-found.epic"
    assert report["market"]["info"] is None and report["transactions"]["statuses"] == [500]
    assert report["activity"]["summary"]["total"] == 0
    assert report["positions"]["info"]["count"] == 0 and report["confirms"] == []
    assert not any(p.startswith("/confirms") for p, _ in log)


async def test_collect_records_a_missing_confirm_without_aborting(fx):
    routes = full_routes(**{"/confirms/p_" + DEAL_1: (404, {"errorCode": "error.not-found.dealReference"})})
    report = await fx.collect(make_get(fx, routes, []), fx.load_config({"GW_DAYS": "1", "GW_CONFIRMS": "2"}), NOW, None, None)
    assert report["confirms"][0]["status"] == 404 and report["confirms"][0]["info"] is None
    assert report["confirms"][0]["error"] == "error.not-found.dealReference"
    assert report["confirms"][1]["status"] == 200 and report["aborted"] is None


async def test_collect_with_zero_confirms_requests_none(fx):
    log = []
    await fx.collect(make_get(fx, full_routes(), log), fx.load_config({"GW_DAYS": "1", "GW_CONFIRMS": "0"}), NOW, None, None)
    assert not any(p.startswith("/confirms") for p, _ in log)


# ---------------------------------------------------------------- wydruk

async def full_report(fx, routes=None, **env):
    cfg = fx.load_config({"GW_DAYS": "2", "GW_CONFIRMS": "2", **env})
    return await fx.collect(make_get(fx, routes or full_routes(), []), cfg, NOW, J(fx, LOGIN), ACCOUNT_A)


async def test_render_prints_the_facts_and_nothing_secret(fx):
    text = "\n".join(fx.render(await full_report(fx)))
    for expected in ("tylko odczyt", "waluta instrumentu=USD", "exchangeRate=0.8597", "minDealSize=0.01 POINTS",
                     "bid=6801.2", "spread=0.8", "CONVERSION: 1", "FX_COMMISSION: 1", "profitLevel", "profitDistance",
                     "profitCurrency", "EURUSD", "bid=1.16834", "różne: tak", "PODSUMOWANIE", "AVAILABLE_DEFAULT_ON",
                     "waluta rachunku", "EUR"):
        assert expected in text, expected
    for leak in LEAKS:
        assert leak not in text, leak


async def test_render_lists_all_keys_not_just_the_first_few(fx):
    wide = {"instrument": {f"key{i:02d}": i for i in range(30)} | {"epic": "US500", "currency": "USD"}}
    report = await fx.collect(make_get(fx, full_routes(**{"/markets/US500": (200, wide)}), []), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    text = "\n".join(fx.render(report))
    assert "key00" in text and "key29" in text


async def test_summary_shows_the_currency_of_the_closed_trade_not_of_the_conversion_rows(fx):
    text = "\n".join(fx.render(await full_report(fx)))
    line = next(x for x in text.splitlines() if x.startswith("  zamknięte transakcje US500"))
    assert "waluty: USD" in line and "EUR" not in line
    assert "-1.05" in line  # kwota najnowszego zamknięcia


async def test_render_does_not_print_the_swap_timestamp_as_a_masked_number(fx):
    text = "\n".join(fx.render(await full_report(fx)))
    assert "swapChargeTimestamp" not in text and "<NUM>" not in text


async def test_render_says_plainly_when_the_history_cannot_settle_the_currency(fx):
    routes = full_routes(**{"/history/transactions": (200, {"transactions": []}), "/history/activity": (200, {"activities": []}),
                            "/positions": (200, {"positions": []})})
    text = "\n".join(fx.render(await full_report(fx, routes)))
    assert "NIE rozstrzyga" in text and "brak otwartych pozycji" in text


async def test_render_handles_a_report_where_everything_failed(fx):
    report = await fx.collect(make_get(fx, {}, []), fx.load_config({"GW_DAYS": "1"}), NOW, None, None)
    text = "\n".join(fx.render(report))
    assert "404" in text and "PODSUMOWANIE" in text
    assert "przerwano" not in text


async def test_render_mentions_an_aborted_run(fx):
    routes = full_routes(**{"/history/transactions": (429, {"errorCode": "error.too-many.requests"})})
    text = "\n".join(fx.render(await full_report(fx, routes)))
    assert "przerwano" in text and "429" in text


# ---------------------------------------------------------------- przebieg całości na atrapie sesji

class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(payload) if payload is not None else ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body


class FakeSession:
    """Atrapa sesji aiohttp: zapisuje każde żądanie (metoda, ścieżka, nagłówki, ciało), nic nie wysyła."""

    def __init__(self, routes, login=None, boom=None):
        self.routes = routes
        self.login = login if login is not None else FakeResponse(200, LOGIN, {"CST": CST, "X-SECURITY-TOKEN": SEC})
        self.boom = boom
        self.calls = []

    def _route(self, path):
        status, payload = self.routes.get(path) or (404, {"errorCode": "error.not-found"})
        return FakeResponse(status, payload)

    def post(self, url, headers=None, json=None, **kw):
        self.calls.append(("POST", url.split("/api/v1")[1], headers, json))
        return self.login

    def get(self, url, headers=None, params=None, **kw):
        path = url.split("/api/v1")[1]
        self.calls.append(("GET", path, headers, params))
        if self.boom and path == "/positions":
            raise RuntimeError(self.boom)
        if path.startswith("/confirms/"):
            return FakeResponse(200, CONFIRM)
        return self._route(path)

    def delete(self, url, headers=None, **kw):
        self.calls.append(("DELETE", url.split("/api/v1")[1], headers, None))
        return FakeResponse(200, {"status": "SUCCESS"})


def run_cfg(fx, tmp_path, **env):
    return fx.load_config({"GW_DATA_DIR": str(tmp_path), "GW_DAYS": "1", "GW_CONFIRMS": "1", **env})


async def test_run_logs_in_once_reads_only_and_logs_out(fx, tmp_path):
    (tmp_path / "account.json").write_text(json.dumps({"account_id": ACCOUNT_A}))
    session, lines = FakeSession(full_routes()), []
    code = await fx.run(session, "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep)
    assert code == 0
    methods = [(m, p) for m, p, _, _ in session.calls]
    assert methods[0] == ("POST", "/session") and methods[-1] == ("DELETE", "/session")
    assert [m for m, _ in methods].count("POST") == 1
    assert all(m == "GET" for m, _ in methods[1:-1]) and len(methods) > 5
    # klucz API tylko przy logowaniu; tokeny sesji w zapytaniach odczytu
    assert session.calls[0][2] == {"X-CAP-API-KEY": CREDS.api_key}
    assert session.calls[0][3] == {"identifier": CREDS.identifier, "password": CREDS.api_password, "encryptedPassword": False}
    assert all(c[2] == {"CST": CST, "X-SECURITY-TOKEN": SEC} for c in session.calls[1:])
    text = "\n".join(lines)
    for leak in LEAKS:
        assert leak not in text, leak
    assert "waluta instrumentu=USD" in text and "rachunek związany z silnikiem" in text
    raw = json.loads(Path(run_cfg(fx, tmp_path)["out"]).read_text())
    assert set(raw) == {"US500", "EURUSD"}
    assert all(leak not in json.dumps(raw) for leak in (ACCOUNT_A, OWNER_NAME, DEAL_1))  # surowy zapis tylko danych rynkowych


async def test_run_stops_after_a_failed_login_without_retrying(fx, tmp_path):
    session = FakeSession(full_routes(), login=FakeResponse(401, {"errorCode": "error.invalid.details"}))
    lines = []
    assert await fx.run(session, "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep) == 1
    assert [(m, p) for m, p, _, _ in session.calls] == [("POST", "/session")]
    assert "logowanie nieudane" in "\n".join(lines) and "401" in "\n".join(lines)


async def test_run_stops_when_the_login_gives_no_tokens(fx, tmp_path):
    session = FakeSession(full_routes(), login=FakeResponse(200, LOGIN, {}))
    lines = []
    assert await fx.run(session, "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep) == 1
    assert [(m, p) for m, p, _, _ in session.calls] == [("POST", "/session")]
    assert "tokenów" in "\n".join(lines)


async def test_run_logs_out_and_leaks_nothing_when_a_request_blows_up(fx, tmp_path):
    session = FakeSession(full_routes(), boom="boom with " + CREDS.api_password + " and " + ACCOUNT_A)
    lines = []
    code = await fx.run(session, "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep)
    assert code == 1 and session.calls[-1][:2] == ("DELETE", "/session")
    text = "\n".join(lines)
    assert "RuntimeError" in text and "boom" not in text
    for leak in LEAKS:
        assert leak not in text, leak


async def test_run_reports_when_the_session_account_differs_from_the_bound_one(fx, tmp_path):
    (tmp_path / "account.json").write_text(json.dumps({"account_id": ACCOUNT_B}))
    lines = []
    await fx.run(FakeSession(full_routes()), "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep)
    text = "\n".join(lines)
    assert "UWAGA" in text and "różni się" in text
    for leak in LEAKS:
        assert leak not in text, leak


async def test_run_scrubs_secrets_that_a_broker_error_might_echo(fx, tmp_path):
    echoed = full_routes(**{"/markets/US500": (400, {"errorCode": "bad key " + CREDS.api_key + " for " + CST})})
    lines = []
    await fx.run(FakeSession(echoed), "http://test.invalid/api/v1", CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep)
    text = "\n".join(lines)
    assert CREDS.api_key not in text and CST not in text and "<SECRET>" in text
    assert "fake-key" not in text and "fake-cst" not in text  # także żadnego fragmentu po maskowaniu wzorcami


async def test_run_works_with_a_real_aiohttp_client_against_a_loopback_server(fx, tmp_path):
    """Ten sam przebieg, ale z prawdziwym klientem aiohttp i serwerem na 127.0.0.1 (nie broker): treść JSON z
    `Content-Type: text/plain` (jak u brokera), parametry zapytań, nagłówki sesji i wylogowanie."""
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    seen = []

    def reply(payload, status=200, headers=None):
        return web.Response(text=json.dumps(payload), status=status, content_type="text/plain", headers=headers or {})

    async def login(request):
        seen.append(("POST", request.path, None, await request.json()))
        return reply(LOGIN, headers={"CST": CST, "X-SECURITY-TOKEN": SEC})

    async def logout(request):
        seen.append(("DELETE", request.path, request.headers.get("CST"), None))
        return reply({"status": "SUCCESS"})

    def getter(payload):
        async def handler(request):
            seen.append(("GET", request.path, request.headers.get("CST"), dict(request.query)))
            return reply(payload)
        return handler

    app = web.Application()
    app.router.add_post("/api/v1/session", login)
    app.router.add_delete("/api/v1/session", logout)
    app.router.add_get("/api/v1/markets/US500", getter(MARKET_US500))
    app.router.add_get("/api/v1/markets/EURUSD", getter(MARKET_EURUSD))
    app.router.add_get("/api/v1/history/transactions", getter(TRANSACTIONS))
    app.router.add_get("/api/v1/history/activity", getter(ACTIVITY))
    app.router.add_get("/api/v1/positions", getter(POSITIONS))
    app.router.add_get("/api/v1/confirms/{ref}", getter(CONFIRM))
    lines = []
    async with TestServer(app) as server:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            code = await fx.run(session, str(server.make_url("/api/v1")), CREDS, run_cfg(fx, tmp_path), lines.append, NOW, no_sleep)
    assert code == 0
    assert seen[0][:2] == ("POST", "/api/v1/session") and seen[-1][:2] == ("DELETE", "/api/v1/session")
    assert seen[0][3]["encryptedPassword"] is False
    reads = seen[1:-1]
    assert reads and all(m == "GET" and cst == CST for m, _, cst, _ in reads)
    activity = next(q for m, path, _, q in reads if path.endswith("/history/activity"))
    assert activity["detailed"] == "true" and activity["filter"] == "epic==US500" and {"from", "to"} <= set(activity)
    text = "\n".join(lines)
    assert "waluta instrumentu=USD" in text and "profitLevel" in text
    for leak in LEAKS:
        assert leak not in text, leak


# ---------------------------------------------------------------- niezmienniki źródła (tylko odczyt)

def _http_calls():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.setdefault(node.func.attr, []).append(node)
    return calls


def test_source_has_exactly_one_login_one_logout_and_no_other_writes():
    calls = _http_calls()
    for method in ("put", "patch", "request", "head", "options", "ws_connect"):
        assert method not in calls, method
    [login] = calls["post"]
    [logout] = calls["delete"]
    assert "/session" in ast.unparse(login.args[0]) and "/session" in ast.unparse(logout.args[0])


def test_source_never_names_trading_or_arming_endpoints():
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("/workingorders", "/orders", "ARM_LIVE", "L1_PASSED", "live_allowed", "/accounts/preferences"):
        assert forbidden not in source, forbidden


def test_header_documents_the_run_command():
    head = SCRIPT.read_text(encoding="utf-8").split('"""')[1]
    assert "docker compose -f docker/compose.yml run --rm -T -e GW_EPIC=US500 gw-stock-engine python - < research/diag_fx.py" in head
    assert "TYLKO ODCZYT" in head
