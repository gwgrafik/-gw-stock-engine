"""Testy FakeBroker: realistyczny broker testowy z pełnym sterowaniem (podstawa testów awarii)."""

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.broker.base import Broker
from gwstock.broker.fake import FakeBroker
from gwstock.types import (
    AccountInfo,
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    Candle,
    ClosedDeal,
    EntryRequest,
    MarketClosed,
    Quote,
    SessionExpired,
    Side,
)
from tests.unit.broker_contract import BrokerContract

UTC = timezone.utc
PROTOCOL_METHODS = {name for name, member in vars(Broker).items() if inspect.iscoroutinefunction(member)}


def entry(side: Side = Side.BUY, size: str = "0.007", stop: str = "30", epic: str = "DE40") -> EntryRequest:
    return EntryRequest("gws-20261102-DE40-1-entry", epic, side, Decimal(size), Decimal(stop))


async def open_and_confirm(fake: FakeBroker, req: EntryRequest | None = None):
    ack = await fake.open_position(req or entry())
    return await fake.confirm(ack.deal_reference)


def candle(start: datetime, close: str = "25800") -> Candle:
    return Candle("DE40", start, Decimal("25790"), Decimal("25810"), Decimal("25780"), Decimal(close))


class TestFakeBrokerContract(BrokerContract):
    @pytest.fixture
    def broker(self, fake):
        return fake

    @pytest.fixture
    def close_market(self, fake):
        return lambda: fake.set_market_closed(True)


# --- testy z planu -------------------------------------------------------------


async def test_open_position_attaches_stop_atomically(fake, quote):
    ack = await fake.open_position(
        EntryRequest("gws-20261102-DE40-1-entry", "DE40", Side.BUY, Decimal("0.007"), Decimal("30"))
    )
    pos = await fake.confirm(ack.deal_reference)
    assert pos.stop_level == quote.ask - Decimal("30") and pos.size == Decimal("0.007")


async def test_timeout_after_acceptance_leaves_position_open(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    with pytest.raises(BrokerTimeout):
        await fake.open_position(entry())
    assert len(await fake.list_positions()) == 1


async def test_stop_trigger_removes_position_and_reports_closed_deal(fake, clock):
    t0 = clock.now_utc()
    pos = await fake.confirm((await fake.open_position(entry())).deal_reference)
    clock.advance(60)
    fake.trigger_stop(pos.deal_id, Decimal("25749.8"))
    assert await fake.list_positions() == []
    assert await fake.closed_since(t0) == [
        ClosedDeal(pos.deal_id, "DE40", Decimal("25749.8"), "STOP", clock.now_utc())
    ]


# --- wypełnienie, stop i wielkość ------------------------------------------------


async def test_sell_fills_at_bid_with_stop_above_fill(fake):
    pos = await open_and_confirm(fake, entry(Side.SELL))
    assert (pos.side, pos.open_level, pos.stop_level) == (Side.SELL, Decimal("25780.3"), Decimal("25810.3"))


@pytest.mark.parametrize(
    ("side", "fill", "stop"),
    [(Side.BUY, "25783.8", "25753.8"), (Side.SELL, "25778.8", "25808.8")],
)
async def test_fill_slippage_is_adverse_and_stop_follows_the_fill(fake, side, fill, stop):
    fake.fill_slippage = Decimal("1.5")
    pos = await open_and_confirm(fake, entry(side))
    assert (pos.open_level, pos.stop_level) == (Decimal(fill), Decimal(stop))


async def test_fill_size_factor_scales_confirmed_and_listed_size_without_rounding(fake):
    fake.fill_size_factor = Decimal("0.5")
    pos = await open_and_confirm(fake)
    assert pos.size == Decimal("0.0035")
    assert [p.size for p in await fake.list_positions()] == [Decimal("0.0035")]


async def test_fill_uses_quote_current_at_fill_time(fake):
    fake.set_quote_fn(lambda now: Quote("DE40", Decimal("25700.0"), Decimal("25701.0"), now))
    pos = await open_and_confirm(fake)
    assert (pos.open_level, pos.stop_level) == (Decimal("25701.0"), Decimal("25671.0"))


@pytest.mark.parametrize(
    ("req", "reason"),
    [
        (entry(size="0.0005"), "MIN_SIZE"),
        (entry(size="0.0015"), "SIZE_STEP"),
        (entry(stop="2.5"), "MIN_STOP_DISTANCE"),
        (entry(stop="0"), "MIN_STOP_DISTANCE"),
        (entry(epic="US100"), "EPIC_NOT_FOUND"),
    ],
    ids=["below-min-size", "off-step", "stop-too-close", "no-stop", "unknown-epic"],
)
async def test_invalid_entry_is_rejected_without_effect(fake, req, reason):
    with pytest.raises(BrokerRejected) as info:
        await fake.open_position(req)
    assert info.value.reason == reason and fake.positions == {}
    assert (await fake.open_position(entry())).deal_reference == "REF-1"  # odrzucenie nie zużywa referencji


async def test_minimum_stop_distance_is_accepted(fake, quote):
    pos = await open_and_confirm(fake, entry(stop="2.6"))
    assert pos.stop_level == quote.ask - Decimal("2.6")


# --- identyfikatory i potwierdzenia ---------------------------------------------


async def test_references_and_deal_ids_are_deterministic(fake):
    acks = [await fake.open_position(entry()) for _ in range(2)]
    assert [a.deal_reference for a in acks] == ["REF-1", "REF-2"]
    assert list(fake.positions) == ["DEAL-1", "DEAL-2"]
    assert (await fake.confirm("REF-2")).deal_id == "DEAL-2"


async def test_confirm_returns_none_once_position_is_gone(fake):
    ack = await fake.open_position(entry())
    await fake.close_position("DEAL-1")
    assert await fake.confirm(ack.deal_reference) is None


# --- wstrzykiwanie awarii --------------------------------------------------------


async def test_failure_not_applied_has_no_effect_and_consumes_no_ids(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    with pytest.raises(BrokerTimeout):
        await fake.open_position(entry())
    assert fake.positions == {} and fake.calls == ["open_position"]
    pos = await open_and_confirm(fake)
    assert pos.deal_id == "DEAL-1"


async def test_failure_applied_leaves_exactly_one_confirmable_position(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    with pytest.raises(BrokerTimeout):
        await fake.open_position(entry())
    assert list(fake.positions) == ["DEAL-1"]
    assert (await fake.confirm("REF-1")).deal_id == "DEAL-1"


async def test_applied_failure_on_close_still_closes(fake):
    await open_and_confirm(fake)
    fake.fail_next("close_position", BrokerTimeout(), applied=True)
    with pytest.raises(BrokerTimeout):
        await fake.close_position("DEAL-1")
    assert await fake.list_positions() == []


async def test_queued_failures_are_consumed_fifo_one_per_call(fake):
    fake.fail_next("open_position", BrokerTimeout())
    fake.fail_next("open_position", BrokerRejected("MIN_STOP_DISTANCE"))
    with pytest.raises(BrokerTimeout):
        await fake.open_position(entry())
    with pytest.raises(BrokerRejected) as info:
        await fake.open_position(entry())
    assert info.value.reason == "MIN_STOP_DISTANCE"
    assert (await fake.open_position(entry())).deal_reference == "REF-1"
    assert fake.calls.count("open_position") == 3 and len(fake.positions) == 1


async def test_failure_is_scoped_to_its_method(fake):
    fake.fail_next("close_position", BrokerTimeout())
    pos = await open_and_confirm(fake)
    with pytest.raises(BrokerTimeout):
        await fake.close_position(pos.deal_id)
    assert await fake.list_positions() == [pos]


async def test_on_call_runs_before_effect_so_stop_can_win_the_race(fake, clock):
    t0 = clock.now_utc()
    pos = await open_and_confirm(fake)
    fake.on_call("close_position", lambda: fake.trigger_stop(pos.deal_id, Decimal("25752.3")))
    with pytest.raises(BrokerRejected) as info:
        await fake.close_position(pos.deal_id)
    assert info.value.reason == "POSITION_NOT_FOUND"
    assert await fake.closed_since(t0) == [ClosedDeal(pos.deal_id, "DE40", Decimal("25752.3"), "STOP", t0)]


async def test_on_call_sees_call_recorded_and_runs_even_if_call_then_fails(fake):
    seen = []
    fake.on_call("open_position", lambda: seen.append(list(fake.calls)))
    fake.fail_next("open_position", BrokerTimeout())
    with pytest.raises(BrokerTimeout):
        await fake.open_position(entry())
    assert seen == [["open_position"]]


@pytest.mark.parametrize("method", ["open_postion", "set_quote", "capabilities"])
def test_controls_refuse_names_that_are_not_broker_methods(fake, method):
    # literówka w teście nie może po cichu wyłączyć wstrzykniętej awarii
    with pytest.raises(ValueError, match=method):
        fake.fail_next(method, BrokerTimeout())
    with pytest.raises(ValueError, match=method):
        fake.on_call(method, lambda: None)


# --- zamknięcia -----------------------------------------------------------------


async def test_close_of_unknown_deal_is_rejected_as_position_not_found(fake):
    with pytest.raises(BrokerRejected) as info:
        await fake.close_position("DEAL-404")
    assert info.value.reason == "POSITION_NOT_FOUND"


async def test_close_fills_buy_at_bid_and_sell_at_ask_as_manual_deals(fake, clock):
    t0 = clock.now_utc()
    await open_and_confirm(fake, entry(Side.BUY))
    await open_and_confirm(fake, entry(Side.SELL))
    clock.advance(120)
    fake.set_quote(Quote("DE40", Decimal("25800.1"), Decimal("25802.1"), clock.now_utc()))
    await fake.close_position("DEAL-1")
    await fake.close_position("DEAL-2")
    t1 = clock.now_utc()
    assert await fake.closed_since(t0) == [
        ClosedDeal("DEAL-1", "DE40", Decimal("25800.1"), "MANUAL", t1),
        ClosedDeal("DEAL-2", "DE40", Decimal("25802.1"), "MANUAL", t1),
    ]


async def test_closed_since_includes_its_boundary_and_excludes_earlier_deals(fake, clock):
    await open_and_confirm(fake)
    await open_and_confirm(fake)
    await fake.close_position("DEAL-1")
    t1 = clock.now_utc()
    clock.advance(10)
    fake.trigger_stop("DEAL-2", Decimal("25750"))
    t2 = clock.now_utc()
    assert [d.deal_id for d in await fake.closed_since(t1)] == ["DEAL-1", "DEAL-2"]
    assert [d.deal_id for d in await fake.closed_since(t1 + timedelta(microseconds=1))] == ["DEAL-2"]
    assert [d.deal_id for d in await fake.closed_since(t2)] == ["DEAL-2"]
    assert await fake.closed_since(t2 + timedelta(seconds=1)) == []


async def test_trigger_stop_of_unknown_deal_is_refused(fake):
    with pytest.raises(ValueError, match="DEAL-404"):
        fake.trigger_stop("DEAL-404", Decimal("25750"))


# --- stop -----------------------------------------------------------------------


async def test_set_stop_moves_stop_of_open_position(fake):
    await open_and_confirm(fake)
    await fake.set_stop("DEAL-1", Decimal("25777.7"))  # bid 25780.3 − min 2.6: granica dozwolona
    assert fake.positions["DEAL-1"].stop_level == Decimal("25777.7")


@pytest.mark.parametrize(
    ("side", "level"),
    [(Side.BUY, "25777.8"), (Side.BUY, "25790"), (Side.SELL, "25784.8"), (Side.SELL, "25770")],
    ids=["buy-too-close", "buy-wrong-side", "sell-too-close", "sell-wrong-side"],
)
async def test_set_stop_too_close_or_on_wrong_side_is_rejected(fake, side, level):
    pos = await open_and_confirm(fake, entry(side))
    with pytest.raises(BrokerRejected) as info:
        await fake.set_stop(pos.deal_id, Decimal(level))
    assert info.value.reason == "MIN_STOP_DISTANCE" and fake.positions[pos.deal_id] == pos


async def test_set_stop_of_unknown_deal_is_rejected(fake):
    with pytest.raises(BrokerRejected) as info:
        await fake.set_stop("DEAL-404", Decimal("25700"))
    assert info.value.reason == "POSITION_NOT_FOUND"


# --- sesja i stan rynku ---------------------------------------------------------

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
CALLS = {
    "keepalive": lambda b: b.keepalive(),
    "instrument_rules": lambda b: b.instrument_rules("DE40"),
    "quote": lambda b: b.quote("DE40"),
    "daily_candles": lambda b: b.daily_candles("DE40", 15),
    "candles_5m": lambda b: b.candles_5m("DE40", T0, T0 + timedelta(minutes=5)),
    "open_position": lambda b: b.open_position(entry()),
    "confirm": lambda b: b.confirm("REF-1"),
    "list_positions": lambda b: b.list_positions(),
    "close_position": lambda b: b.close_position("DEAL-1"),
    "set_stop": lambda b: b.set_stop("DEAL-1", Decimal("25700")),
    "closed_since": lambda b: b.closed_since(T0),
    "account": lambda b: b.account(),
    "list_accounts": lambda b: b.list_accounts(),
    "select_account": lambda b: b.select_account("ACC-1"),
}


def test_session_table_covers_every_broker_method_but_login():
    assert set(CALLS) | {"login"} == PROTOCOL_METHODS


@pytest.mark.parametrize("method", sorted(CALLS))
async def test_expired_session_refuses_every_call_until_login(fake, method):
    await open_and_confirm(fake)  # DEAL-1 istnieje, żeby close/set_stop po logowaniu miały na czym działać
    fake.expire_session()
    with pytest.raises(SessionExpired):
        await CALLS[method](fake)
    assert fake.calls[-1] == method and list(fake.positions) == ["DEAL-1"]
    await fake.login()
    await CALLS[method](fake)


async def test_market_closed_refuses_orders_until_reopened(fake):
    pos = await open_and_confirm(fake)
    fake.set_market_closed(True)
    with pytest.raises(MarketClosed):
        await fake.open_position(entry())
    with pytest.raises(MarketClosed):
        await fake.close_position(pos.deal_id)
    assert list(fake.positions) == ["DEAL-1"]
    fake.set_market_closed(False)
    assert (await fake.open_position(entry())).deal_reference == "REF-2"
    await fake.close_position(pos.deal_id)
    assert list(fake.positions) == ["DEAL-2"]


async def test_reads_keep_working_while_market_closed(fake):
    fake.set_daily_candles([candle(T0 - timedelta(days=1))])
    fake.set_market_closed(True)
    assert len(await fake.daily_candles("DE40", 15)) == 1
    assert await fake.candles_5m("DE40", T0, T0) == []
    assert (await fake.account()).account_id == "ACC-0"
    await fake.keepalive()


# --- dane: świece i notowania ------------------------------------------------------


async def test_daily_candles_returns_last_count_in_time_order(fake):
    days = [T0 - timedelta(days=n) for n in (1, 3, 2)]
    fake.set_daily_candles([candle(d) for d in days])
    last_two = await fake.daily_candles("DE40", 2)
    assert [c.start for c in last_two] == [T0 - timedelta(days=2), T0 - timedelta(days=1)]
    assert len(await fake.daily_candles("DE40", 30)) == 3


async def test_daily_candles_refuses_non_positive_count(fake):
    fake.set_daily_candles([candle(T0 - timedelta(days=1))])
    with pytest.raises(ValueError, match="count"):
        await fake.daily_candles("DE40", 0)


async def test_candles_5m_returns_half_open_range_in_time_order(fake):
    starts = [datetime(2026, 11, 2, 8, m, tzinfo=UTC) for m in (10, 0, 5, 15)]
    fake.set_candles_5m([candle(s) for s in starts])
    start, end = datetime(2026, 11, 2, 8, 0, tzinfo=UTC), datetime(2026, 11, 2, 8, 10, tzinfo=UTC)
    got = await fake.candles_5m("DE40", start, end)
    assert [c.start.minute for c in got] == [0, 5]


async def test_reads_for_another_instrument_are_rejected(fake):
    reads = (
        lambda: fake.quote("US100"),
        lambda: fake.instrument_rules("US100"),
        lambda: fake.daily_candles("US100", 1),
        lambda: fake.candles_5m("US100", T0, T0 + timedelta(minutes=5)),
    )
    for read in reads:
        with pytest.raises(BrokerRejected) as info:
            await read()
        assert info.value.reason == "EPIC_NOT_FOUND"


def test_controls_refuse_data_for_another_instrument(fake, quote):
    with pytest.raises(ValueError, match="US100"):
        fake.set_quote(Quote("US100", quote.bid, quote.ask, quote.ts))
    with pytest.raises(ValueError, match="US100"):
        fake.set_candles_5m([Candle("US100", T0, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))])


async def test_quote_fn_derives_quote_from_engine_clock_and_set_quote_clears_it(fake, clock, quote):
    def moving(now: datetime) -> Quote:
        minutes = Decimal(int((now - T0).total_seconds() // 60))
        return Quote("DE40", Decimal("25780.3") + minutes, Decimal("25782.3") + minutes, now)

    fake.set_quote_fn(moving)
    clock.advance(180)
    expected = Quote("DE40", Decimal("25783.3"), Decimal("25785.3"), T0 + timedelta(minutes=3))
    assert await fake.quote("DE40") == expected
    fake.set_quote(quote)
    clock.advance(60)
    assert await fake.quote("DE40") == quote


# --- rachunki -------------------------------------------------------------------


async def test_default_accounts_put_unfunded_account_first_and_active(fake):
    assert await fake.list_accounts() == [
        AccountInfo("ACC-0", "EUR", "ENABLED", Decimal("0"), Decimal("0"), Decimal("0")),
        AccountInfo("ACC-1", "EUR", "ENABLED", Decimal("40"), Decimal("40"), Decimal("0")),
    ]
    assert (await fake.account()).account_id == "ACC-0"


async def test_login_selects_bound_account(clock, rules, quote):
    fake = FakeBroker(clock, rules, quote, account_id="ACC-1")
    assert (await fake.account()).account_id == "ACC-0"
    await fake.login()
    funded = AccountInfo("ACC-1", "EUR", "ENABLED", Decimal("40"), Decimal("40"), Decimal("0"))
    assert await fake.account() == funded


async def test_login_with_unknown_bound_account_fails_closed(clock, rules, quote):
    fake = FakeBroker(clock, rules, quote, account_id="ACC-9")
    with pytest.raises(BrokerError, match="ACC-9"):
        await fake.login()


async def test_new_session_without_bound_account_starts_on_default_account(fake):
    await fake.select_account("ACC-1")
    await fake.login()
    assert (await fake.account()).account_id == "ACC-0"


async def test_margin_used_and_available_follow_open_positions(fake):
    await fake.select_account("ACC-1")
    await open_and_confirm(fake, entry(Side.BUY, size="0.007"))  # 0.007 × 25782.3 × 1 × 0.05 = 9.023805
    await open_and_confirm(fake, entry(Side.SELL, size="0.002"))  # 0.002 × 25780.3 × 1 × 0.05 = 2.57803
    active = await fake.account()
    assert (active.equity, active.margin_used, active.available) == (
        Decimal("40"),
        Decimal("11.601835"),
        Decimal("28.398165"),
    )
    assert await fake.list_accounts() == [
        AccountInfo("ACC-0", "EUR", "ENABLED", Decimal("0"), Decimal("0"), Decimal("0")),
        active,
    ]
    await fake.close_position("DEAL-1")
    assert (await fake.account()).margin_used == Decimal("2.57803")


# --- agregacja pozycji (netting) -----------------------------------------------------


async def test_without_netting_second_entry_opens_separate_position(fake):
    await open_and_confirm(fake)
    await open_and_confirm(fake)
    assert [(p.deal_id, p.size) for p in await fake.list_positions()] == [
        ("DEAL-1", Decimal("0.007")),
        ("DEAL-2", Decimal("0.007")),
    ]


async def test_netting_second_entry_increases_existing_position(clock, rules, quote):
    fake = FakeBroker(clock, rules, quote, netting=True)
    await open_and_confirm(fake, entry(size="0.007"))  # ask 25782.3, stop 25752.3
    fake.set_quote(Quote("DE40", Decimal("25790.3"), Decimal("25792.3"), clock.now_utc()))
    ack = await fake.open_position(entry(size="0.003"))
    # (0.007 × 25782.3 + 0.003 × 25792.3) / 0.010 = 25785.3
    [pos] = await fake.list_positions()
    assert (pos.deal_id, pos.size, pos.open_level, pos.stop_level) == (
        "DEAL-1",
        Decimal("0.010"),
        Decimal("25785.3"),
        Decimal("25752.3"),
    )
    assert ack.deal_reference == "REF-2" and await fake.confirm("REF-2") == pos


async def test_netting_opposite_entry_is_not_simulated(clock, rules, quote):
    fake = FakeBroker(clock, rules, quote, netting=True)
    await open_and_confirm(fake)
    with pytest.raises(NotImplementedError):
        await fake.open_position(entry(Side.SELL))


# --- zgodność z interfejsem ---------------------------------------------------------


def test_fake_implements_every_broker_method_with_protocol_signature(fake):
    assert isinstance(fake, Broker)
    for name in PROTOCOL_METHODS:
        impl = getattr(FakeBroker, name)
        assert inspect.iscoroutinefunction(impl), name
        assert list(inspect.signature(impl).parameters) == list(inspect.signature(getattr(Broker, name)).parameters)


async def test_calls_record_broker_methods_in_order_but_not_controls(fake, quote):
    await fake.login()
    fake.set_quote(quote)
    await fake.quote("DE40")
    pos = await open_and_confirm(fake)
    fake.trigger_stop(pos.deal_id, Decimal("25750"))
    await fake.list_positions()
    assert fake.calls == ["login", "quote", "open_position", "confirm", "list_positions"]
