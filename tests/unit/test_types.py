"""Testy typów domenowych: pochodne notowania i kontrola granic (Decimal, UTC, poprawne wartości)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from gwstock.types import (
    AccountInfo,
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    Candle,
    ClosedDeal,
    EntryRequest,
    InstrumentRules,
    MarketClosed,
    OpenPosition,
    Quote,
    SessionExpired,
    Side,
    WorkingOrder,
)

TS = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)


def test_quote_mid_and_spread_are_exact_decimals():
    q = Quote("DE40", Decimal("25780.3"), Decimal("25782.3"), TS)
    assert q.mid == Decimal("25781.3")
    assert q.spread == Decimal("2.0")


def test_quote_rejects_float_prices():
    with pytest.raises(TypeError, match="bid"):
        Quote("DE40", 25780.3, Decimal("25782.3"), TS)


@pytest.mark.parametrize(
    "ts",
    [datetime(2026, 11, 2, 7, 30), datetime(2026, 11, 2, 8, 30, tzinfo=ZoneInfo("Europe/Berlin"))],
    ids=["naive", "berlin"],
)
def test_quote_requires_utc_timestamp(ts):
    with pytest.raises(ValueError, match="ts"):
        Quote("DE40", Decimal("1"), Decimal("2"), ts)


def test_quote_rejects_crossed_bid_and_ask():
    # zamienione pola bid/ask dałyby ujemny spread i fałszywie przepuściły filtr spreadu
    with pytest.raises(ValueError, match="bid"):
        Quote("DE40", Decimal("25782.3"), Decimal("25780.3"), TS)


def test_candle_requires_decimal_prices_and_utc_start():
    with pytest.raises(TypeError, match="close"):
        Candle("DE40", TS, Decimal("1"), Decimal("2"), Decimal("1"), 1.5)
    with pytest.raises(ValueError, match="start"):
        Candle("DE40", datetime(2026, 11, 2, 8, 0), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"))


def test_open_position_allows_missing_stop_and_stop_is_not_derived_by_default():
    pos = OpenPosition("DEAL-1", "DE40", Side.BUY, Decimal("0.001"), Decimal("25782.3"), None, TS)
    assert pos.stop_level is None and pos.stop_derived is False


def test_open_position_rejects_plain_string_side_and_float_stop():
    with pytest.raises(TypeError, match="side"):
        OpenPosition("DEAL-1", "DE40", "BUY", Decimal("0.001"), Decimal("1"), Decimal("1"), TS)
    with pytest.raises(TypeError, match="stop_level"):
        OpenPosition("DEAL-1", "DE40", Side.BUY, Decimal("0.001"), Decimal("1"), 0.5, TS)


@pytest.mark.parametrize("reason", ["STOP", "MANUAL", "FORCED"])
def test_closed_deal_accepts_known_reasons(reason):
    assert ClosedDeal("DEAL-1", "DE40", Decimal("25750"), reason, TS).reason == reason


def test_closed_deal_rejects_unknown_reason():
    # nieznana przyczyna zamknięcia nie może przejść jako zgadnięta wartość
    with pytest.raises(ValueError, match="reason"):
        ClosedDeal("DEAL-1", "DE40", Decimal("25750"), "SL", TS)


def test_money_fields_reject_non_decimal_and_non_finite_values():
    with pytest.raises(TypeError, match="size"):
        EntryRequest("gws-1", "DE40", Side.BUY, 0.007, Decimal("30"))
    with pytest.raises(ValueError, match="stop_distance"):
        EntryRequest("gws-1", "DE40", Side.BUY, Decimal("0.007"), Decimal("NaN"))
    with pytest.raises(TypeError, match="equity"):
        AccountInfo("ACC-1", "EUR", "ENABLED", 40, Decimal("40"), Decimal("0"))
    with pytest.raises(TypeError, match="margin_rate"):
        InstrumentRules("DE40", Decimal("0.001"), Decimal("0.001"), Decimal("1"), 0.05, Decimal("2.6"))


def test_broker_exceptions_share_base_and_rejection_keeps_reason():
    rejected = BrokerRejected("POSITION_NOT_FOUND")
    assert rejected.reason == "POSITION_NOT_FOUND" and "POSITION_NOT_FOUND" in str(rejected)
    for exc in (BrokerTimeout(), rejected, SessionExpired(), MarketClosed()):
        assert isinstance(exc, BrokerError)


def test_working_order_keeps_decimal_size_and_level():
    order = WorkingOrder("WO-1", "DE40", Side.SELL, Decimal("0.001"), Decimal("25900"))
    assert (order.side, order.size, order.level) == (Side.SELL, Decimal("0.001"), Decimal("25900"))


def test_working_order_rejects_plain_string_side_float_size_and_non_finite_level():
    with pytest.raises(TypeError, match="side"):
        WorkingOrder("WO-1", "DE40", "SELL", Decimal("0.001"), Decimal("25900"))
    with pytest.raises(TypeError, match="size"):
        WorkingOrder("WO-1", "DE40", Side.SELL, 0.001, Decimal("25900"))
    with pytest.raises(ValueError, match="level"):
        WorkingOrder("WO-1", "DE40", Side.SELL, Decimal("0.001"), Decimal("Infinity"))
