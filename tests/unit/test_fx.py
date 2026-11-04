"""Jeden widok przeliczeń walut (`gwstock.fx`, zadanie 12; projekt wielu pasm §7.3): kurs WYŁĄCZNIE z notowania pary
brokera (`portfolio.toml`: `fx_quote_epic`, `fx_quote_max_age_s`), strona niekorzystna, nigdy kurs wymyślony."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock import execution, portfolio
from gwstock.clock import FakeClock
from gwstock.fx import FxView, QuoteFxView
from gwstock.types import Quote

D = Decimal
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def fx(clock) -> QuoteFxView:
    return QuoteFxView("EURUSD", 60, clock)


def eurusd(bid: str, ask: str, ts: datetime = T0) -> Quote:
    return Quote("EURUSD", D(bid), D(ask), ts)


def test_one_protocol_is_shared_by_the_executor_and_the_portfolio_gate(fx):
    assert execution.FxView is FxView and portfolio.FxView is FxView
    assert isinstance(fx, FxView)


def test_no_quote_means_no_rate_never_a_guess(fx):
    assert fx.to_account(D("10"), "USD") is None
    assert fx.estimate(D("-1"), "USD") is None
    assert fx.par_is_upper_bound("USD") is False


def test_amounts_convert_on_the_unfavourable_side(fx):
    fx.note_quote(eurusd("1.0800", "1.0802"))
    # 10 USD -> EUR po bid (większa kwota): ryzyko i wartość punktu nigdy zaniżone
    assert fx.to_account(D("10.8"), "USD") == D("10")
    assert fx.to_account(D("1"), "USD") > D("1") / D("1.0802")
    assert fx.to_account(D("5"), "EUR") == D("5")  # waluta rachunku: bez przeliczenia
    assert fx.to_account(D("5"), "GBP") is None  # innej pary nie znamy


def test_estimate_of_a_result_is_conservative_for_losses_and_gains(fx):
    fx.note_quote(eurusd("1.0000", "1.2500"))
    assert fx.estimate(D("-1.25"), "USD") == (D("-1.25"), D("1.0000"))  # strata po bid: większa strata
    assert fx.estimate(D("1.25"), "USD") == (D("1.00"), D("1.2500"))  # zysk po ask: mniejszy zysk
    assert fx.estimate(D("-0.001"), "USD") == (D("-0.01"), D("1.0000"))  # grosze w dół: strata nie zaniżona
    assert fx.estimate(D("0.019"), "USD") == (D("0.01"), D("1.2500"))
    assert fx.estimate(D("2"), "EUR") == (D("2"), None)


def test_a_stale_quote_is_no_rate_but_still_tells_the_parity_bound(fx, clock):
    fx.note_quote(eurusd("1.08", "1.09"))
    clock.advance(60)
    assert fx.to_account(D("1"), "USD") is not None  # równo 60 s przechodzi
    clock.advance(1)
    assert fx.to_account(D("1"), "USD") is None and fx.estimate(D("-1"), "USD") is None
    assert fx.par_is_upper_bound("USD") is True  # ostatni znany bid >= 1: 1 USD <= 1 EUR
    fx.note_quote(eurusd("0.99", "1.01", clock.now_utc()))
    assert fx.par_is_upper_bound("USD") is False


def test_quotes_of_other_instruments_and_bad_quotes_are_ignored(fx):
    fx.note_quote(Quote("US500", D("7583"), D("7584"), T0))
    fx.note_quote(eurusd("0", "1.08"))  # bid 0: kursu z tego nie ma
    assert fx.to_account(D("1"), "USD") is None


def test_quote_from_the_future_is_not_trusted(fx):
    fx.note_quote(eurusd("1.08", "1.09", T0 + timedelta(seconds=5)))
    assert fx.to_account(D("1"), "USD") is None


def test_pair_must_quote_the_account_currency(clock):
    with pytest.raises(ValueError):
        QuoteFxView("USDJPY", 60, clock)
    with pytest.raises(ValueError):
        QuoteFxView("EURUSD", 0, clock)
    assert QuoteFxView("EURUSD", 60, clock).account_currency == "EUR"
