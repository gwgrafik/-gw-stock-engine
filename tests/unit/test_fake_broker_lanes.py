"""Testy FakeBroker dla pasm (zadanie 6): wiele instrumentów, zamknięty rynek per instrument, cel zysku i symulacja
świecy z konwencją R4 "stop i cel w jednej świecy: STOP".

Zachowanie dla jednego instrumentu (konstruktor z jednym `rules`) sprawdzają `test_fake_broker.py` i złote ślady DE40.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.broker.fake import FakeBroker
from gwstock.types import (
    BrokerRejected,
    BrokerTimeout,
    Candle,
    ClosedDeal,
    EntryRequest,
    InstrumentRules,
    MarketClosed,
    OpenPosition,
    Quote,
    Side,
    TargetedEntryRequest,
    TargetedOpenPosition,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # = clock.now_utc() na starcie fixture `clock`
D = Decimal

US500 = InstrumentRules(
    "US500", D("0.01"), D("0.01"), D("1"), D("0.05"), D("0.5"), D("0.1"), currency="USD"
)  # fmt: skip


@pytest.fixture
def us_quote(clock):
    return Quote("US500", D("6800.0"), D("6800.6"), clock.now_utc())


@pytest.fixture
def multi(clock, rules, quote, us_quote) -> FakeBroker:
    return FakeBroker(clock, [rules, US500], [quote, us_quote])


def de40(side=Side.BUY, stop="30", target=None, size="0.001") -> EntryRequest:
    if target is None:
        return EntryRequest("gws-20261102-DE40-1-entry", "DE40", side, D(size), D(stop))
    return TargetedEntryRequest("gws-20261102-DE40-1-entry", "DE40", side, D(size), D(stop), D(target))


def us500(side=Side.BUY, stop="4", target=None, size="0.01") -> EntryRequest:
    if target is None:
        return EntryRequest("gws-20261102-US500-1-entry", "US500", side, D(size), D(stop))
    return TargetedEntryRequest("gws-20261102-US500-1-entry", "US500", side, D(size), D(stop), D(target))


async def open_and_confirm(fake: FakeBroker, req: EntryRequest) -> OpenPosition:
    ack = await fake.open_position(req)
    return await fake.confirm(ack.deal_reference)


def bar(epic: str, o: str, h: str, low: str, c: str, start: datetime = T0) -> Candle:
    return Candle(epic, start, D(o), D(h), D(low), D(c))


# --- konstruktor ---------------------------------------------------------------------------------


async def test_single_rules_constructor_keeps_working_and_list_of_one_is_equivalent(clock, rules, quote):
    one = FakeBroker(clock, rules, quote)
    listed = FakeBroker(clock, [rules], [quote])
    assert await one.instrument_rules("DE40") == await listed.instrument_rules("DE40") == rules
    assert await one.quote("DE40") == await listed.quote("DE40") == quote
    # prywatne pola używane przez istniejące testy (`test_modes.py`) zostają dla jednego instrumentu
    assert (one._rules, one._quote, one._quote_fn) == (rules, quote, None)


async def test_multi_instrument_rules_and_quotes_per_epic(multi, rules, quote, us_quote):
    assert await multi.instrument_rules("DE40") == rules
    assert await multi.instrument_rules("US500") == US500
    assert await multi.quote("DE40") == quote
    assert await multi.quote("US500") == us_quote
    with pytest.raises(BrokerRejected) as info:
        await multi.quote("US100")
    assert info.value.reason == "EPIC_NOT_FOUND"


@pytest.mark.parametrize(
    "case",
    ["empty", "duplicate-epic", "missing-quote", "foreign-quote", "two-quotes-one-epic", "not-rules"],
)
def test_constructor_refuses_inconsistent_instruments(clock, rules, quote, us_quote, case):
    args = {
        "empty": ([], []),
        "duplicate-epic": ([rules, rules], [quote]),
        "missing-quote": ([rules, US500], [quote]),
        "foreign-quote": ([rules], [us_quote]),
        "two-quotes-one-epic": ([rules, US500], [quote, quote, us_quote]),
        "not-rules": ([rules, "US500"], [quote, us_quote]),
    }[case]
    with pytest.raises((TypeError, ValueError)):
        FakeBroker(clock, *args)


# --- wejścia i zamknięcia na dwóch instrumentach -------------------------------------------------


async def test_entries_on_two_instruments_fill_at_own_quotes_with_shared_id_counters(multi, quote, us_quote):
    a = await open_and_confirm(multi, de40())
    b = await open_and_confirm(multi, us500(side=Side.SELL))
    assert (a.deal_id, a.epic, a.open_level, a.stop_level) == ("DEAL-1", "DE40", quote.ask, quote.ask - 30)
    assert (b.deal_id, b.epic, b.open_level, b.stop_level) == ("DEAL-2", "US500", us_quote.bid, us_quote.bid + 4)
    assert list(multi.positions) == ["DEAL-1", "DEAL-2"]


async def test_entry_validation_uses_rules_of_its_own_instrument(multi):
    # 0,001 to minimum DE40, ale poniżej minimum US500 (0,01); stop 1 pkt spełnia minimum US500 (0,5), nie DE40 (2,6)
    with pytest.raises(BrokerRejected) as info:
        await multi.open_position(us500(size="0.001"))
    assert info.value.reason == "MIN_SIZE"
    with pytest.raises(BrokerRejected) as info:
        await multi.open_position(de40(stop="1"))
    assert info.value.reason == "MIN_STOP_DISTANCE"
    assert (await multi.open_position(us500(stop="1"))).deal_reference == "REF-1"


async def test_close_uses_the_quote_of_the_position_instrument(multi, us_quote):
    pos = await open_and_confirm(multi, us500())
    await multi.close_position(pos.deal_id)
    [deal] = await multi.closed_since(T0)
    assert (deal.epic, deal.exit_level, deal.reason) == ("US500", us_quote.bid, "MANUAL")


async def test_margin_sums_positions_with_rules_of_each_instrument(multi, clock, rules, quote, us_quote):
    multi_acc = FakeBroker(clock, [rules, US500], [quote, us_quote], account_id="ACC-1")
    await multi_acc.login()
    a = await open_and_confirm(multi_acc, de40())
    b = await open_and_confirm(multi_acc, us500())
    expected = a.size * a.open_level * D("1") * D("0.05") + b.size * b.open_level * D("1") * D("0.05")
    acc = await multi_acc.account()
    assert (acc.margin_used, acc.available) == (expected, D("40") - expected)


async def test_netting_aggregates_only_the_same_instrument(clock, rules, quote, us_quote):
    fake = FakeBroker(clock, [rules, US500], [quote, us_quote], netting=True)
    await open_and_confirm(fake, de40())
    await open_and_confirm(fake, us500())
    await open_and_confirm(fake, de40())
    assert {d: p.epic for d, p in fake.positions.items()} == {"DEAL-1": "DE40", "DEAL-2": "US500"}
    assert fake.positions["DEAL-1"].size == D("0.002")


# --- notowania i świece per instrument -----------------------------------------------------------


async def test_set_quote_replaces_only_its_instrument(multi, quote, us_quote, clock):
    moved = Quote("US500", D("6810.0"), D("6810.6"), clock.now_utc())
    multi.set_quote(moved)
    assert await multi.quote("US500") == moved and await multi.quote("DE40") == quote


async def test_quote_fn_per_instrument_and_ambiguous_epic_is_refused(multi, quote):
    def us(now: datetime) -> Quote:
        return Quote("US500", D("6900"), D("6901"), now)

    with pytest.raises(ValueError, match="epic"):
        multi.set_quote_fn(us)  # dwa instrumenty: bez `epic` nie wiadomo, którego dotyczy
    multi.set_quote_fn(us, epic="US500")
    assert (await multi.quote("US500")).bid == D("6900") and await multi.quote("DE40") == quote
    multi.set_quote_fn(us, epic="DE40")  # funkcja zwraca notowanie innego instrumentu: błąd testu, nie cicha zamiana
    with pytest.raises(ValueError, match="US500"):
        await multi.quote("DE40")
    with pytest.raises(ValueError, match="US100"):
        multi.set_quote_fn(us, epic="US100")


async def test_candles_are_kept_per_instrument(multi, clock):
    c_de = bar("DE40", "25790", "25810", "25780", "25800", T0 - timedelta(minutes=10))
    c_us = bar("US500", "6800", "6805", "6795", "6801", T0 - timedelta(minutes=10))
    multi.set_candles_5m([c_us, c_de])
    multi.set_daily_candles([replace(c_de, start=T0 - timedelta(days=1)), replace(c_us, start=T0 - timedelta(days=1))])
    assert await multi.candles_5m("US500", T0 - timedelta(hours=1), T0) == [c_us]
    assert await multi.candles_5m("DE40", T0 - timedelta(hours=1), T0) == [c_de]
    assert [c.epic for c in await multi.daily_candles("US500", 5)] == ["US500"]
    multi.set_candles_5m([], epic="US500")  # z `epic`: zastępuje tylko ten instrument
    assert await multi.candles_5m("US500", T0 - timedelta(hours=1), T0) == []
    assert await multi.candles_5m("DE40", T0 - timedelta(hours=1), T0) == [c_de]
    with pytest.raises(ValueError, match="US500"):
        multi.set_candles_5m([c_us], epic="DE40")
    with pytest.raises(ValueError, match="US100"):
        multi.set_candles_5m([replace(c_us, epic="US100")])


# --- zamknięty rynek per instrument ---------------------------------------------------------------


async def test_market_closed_for_one_instrument_leaves_the_other_tradable(multi):
    a = await open_and_confirm(multi, de40())
    b = await open_and_confirm(multi, us500())
    multi.set_market_closed(True, "US500")
    with pytest.raises(MarketClosed, match="US500"):
        await multi.open_position(us500())
    with pytest.raises(MarketClosed, match="US500"):
        await multi.close_position(b.deal_id)
    await multi.close_position(a.deal_id)  # DE40 otwarty: spłaszczenie DE40 nie czeka na US500
    assert (await multi.open_position(de40())).deal_reference == "REF-3"
    multi.set_market_closed(False, "US500")
    await multi.close_position(b.deal_id)
    assert list(multi.positions) == ["DEAL-3"]


async def test_market_closed_without_epic_applies_to_every_instrument(multi):
    multi.set_market_closed(True, "US500")
    multi.set_market_closed(True)
    for req in (de40(), us500()):
        with pytest.raises(MarketClosed):
            await multi.open_position(req)
    multi.set_market_closed(False)  # bez `epic`: otwiera wszystkie, także zamknięty osobno US500
    for req in (de40(), us500()):
        await multi.open_position(req)


def test_market_closed_for_unknown_instrument_is_a_test_error(multi):
    with pytest.raises(ValueError, match="US100"):
        multi.set_market_closed(True, "US100")


async def test_close_of_unknown_deal_reports_market_closed_only_when_every_market_is_closed(multi):
    multi.set_market_closed(True, "US500")
    with pytest.raises(BrokerRejected) as info:
        await multi.close_position("DEAL-404")
    assert info.value.reason == "POSITION_NOT_FOUND"
    multi.set_market_closed(True)
    with pytest.raises(MarketClosed):  # jak dotąd dla jednego instrumentu: rynek sprawdzany przed pozycją
        await multi.close_position("DEAL-404")


# --- cel przy otwarciu ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_entry_with_target_attaches_it_atomically_on_the_profit_side(multi, us_quote, side):
    pos = await open_and_confirm(multi, us500(side=side, target="6"))
    fill = us_quote.ask if side is Side.BUY else us_quote.bid
    sign = 1 if side is Side.BUY else -1
    assert isinstance(pos, TargetedOpenPosition)
    assert (pos.open_level, pos.stop_level, pos.target_level) == (fill, fill - sign * 4, fill + sign * 6)
    assert pos.target_derived is False and multi.positions[pos.deal_id] == pos


async def test_entry_with_target_respects_fill_slippage(multi, us_quote):
    multi.fill_slippage = D("0.3")
    pos = await open_and_confirm(multi, us500(target="6"))
    assert (pos.open_level, pos.target_level) == (us_quote.ask + D("0.3"), us_quote.ask + D("6.3"))


@pytest.mark.parametrize("target", ["0", "-1", "0.4"], ids=["zero", "negative", "below-min"])
async def test_target_closer_than_minimum_is_rejected_without_using_a_number(multi, target):
    with pytest.raises(BrokerRejected) as info:
        await multi.open_position(us500(target=target))
    assert info.value.reason == "MIN_TARGET_DISTANCE" and multi.positions == {}
    assert (await multi.open_position(us500(target="0.5"))).deal_reference == "REF-1"  # minimum wystarczy


async def test_plain_entry_and_set_stop_keep_plain_open_position_type(fake):
    pos = await open_and_confirm(fake, de40())
    await fake.set_stop(pos.deal_id, pos.stop_level - 10)
    assert type(pos) is OpenPosition and type(fake.positions[pos.deal_id]) is OpenPosition


async def test_netting_snapshot_reports_the_protection_of_the_aggregated_position(clock, rules, quote):
    fake = FakeBroker(clock, rules, quote, netting=True)
    first = await open_and_confirm(fake, de40(target="45"))
    second = await open_and_confirm(fake, de40(stop="50", target="90"))
    assert second.deal_id == first.deal_id
    assert (second.stop_level, second.target_level) == (first.stop_level, first.target_level)
    assert fake.positions[first.deal_id].target_level == first.target_level


async def test_confirm_snapshot_keeps_the_target_after_protection_changes(multi):
    ack = await multi.open_position(us500(target="6"))
    pos = await multi.confirm(ack.deal_reference)
    await multi.set_protection(pos.deal_id, pos.stop_level - 1, pos.target_level + 1)
    assert await multi.confirm(ack.deal_reference) == pos


# --- set_protection i set_stop przy celu --------------------------------------------------------


async def test_set_stop_keeps_the_target(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    await multi.set_stop(pos.deal_id, pos.stop_level - 1)
    moved = multi.positions[pos.deal_id]
    assert (moved.stop_level, moved.target_level) == (pos.stop_level - 1, pos.target_level)


async def test_set_protection_with_none_target_removes_it(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    await multi.set_protection(pos.deal_id, pos.stop_level, None)
    moved = multi.positions[pos.deal_id]
    assert type(moved) is OpenPosition and moved.target_level is None and moved.stop_level == pos.stop_level


async def test_set_protection_clears_derived_flags(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    multi.positions[pos.deal_id] = replace(pos, stop_derived=True, target_derived=True)
    await multi.set_protection(pos.deal_id, pos.stop_level, pos.target_level)
    moved = multi.positions[pos.deal_id]
    assert (moved.stop_derived, moved.target_derived) == (False, False)


@pytest.mark.parametrize(
    ("side", "stop_shift", "target_shift", "reason"),
    [
        # BUY: bid 6800.0, stop 6796.6, cel 6806.6; SELL: ask 6800.6, stop 6804.0, cel 6794.0; minimum 0,5 pkt
        (Side.BUY, "3.2", "1", "MIN_STOP_DISTANCE"),  # stop 6799.8: 0,2 pkt pod bid
        (Side.BUY, "-1", "-6.4", "MIN_TARGET_DISTANCE"),  # cel 6800.2: 0,2 pkt nad bid
        (Side.SELL, "-3.2", "-1", "MIN_STOP_DISTANCE"),  # stop 6800.8: 0,2 pkt nad ask
        (Side.SELL, "1", "6.4", "MIN_TARGET_DISTANCE"),  # cel 6800.4: 0,2 pkt pod ask
    ],
)
async def test_set_protection_validates_both_levels_and_changes_nothing_on_refusal(
    multi, us_quote, side, stop_shift, target_shift, reason
):
    pos = await open_and_confirm(multi, us500(side=side, target="6"))
    stop = pos.stop_level + D(stop_shift)
    target = pos.target_level + D(target_shift)
    with pytest.raises(BrokerRejected) as info:
        await multi.set_protection(pos.deal_id, stop, target)
    assert info.value.reason == reason and multi.positions[pos.deal_id] == pos


async def test_set_protection_accepts_levels_exactly_at_the_minimum_distance(multi, us_quote):
    pos = await open_and_confirm(multi, us500(target="6"))
    stop, target = us_quote.bid - D("0.5"), us_quote.bid + D("0.5")
    await multi.set_protection(pos.deal_id, stop, target)
    assert (multi.positions[pos.deal_id].stop_level, multi.positions[pos.deal_id].target_level) == (stop, target)


async def test_set_protection_is_recorded_and_honours_failure_injection(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    multi.fail_next("set_protection", BrokerTimeout(), applied=True)
    with pytest.raises(BrokerTimeout):
        await multi.set_protection(pos.deal_id, pos.stop_level - 1, pos.target_level + 1)
    assert multi.positions[pos.deal_id].target_level == pos.target_level + 1  # skutek nastąpił mimo wyjątku
    assert multi.calls[-1] == "set_protection"


async def test_set_protection_works_while_market_is_closed_like_set_stop(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    multi.set_market_closed(True)
    await multi.set_protection(pos.deal_id, pos.stop_level - 1, pos.target_level + 1)
    assert multi.positions[pos.deal_id].stop_level == pos.stop_level - 1


# --- trigger_target -------------------------------------------------------------------------------


async def test_trigger_target_closes_at_target_with_reason_target_and_is_not_a_broker_call(multi, clock):
    pos = await open_and_confirm(multi, us500(side=Side.SELL, target="6"))
    clock.advance(60)
    calls = list(multi.calls)
    multi.trigger_target(pos.deal_id)
    assert multi.calls == calls and multi.positions == {}
    assert await multi.closed_since(T0) == [
        ClosedDeal(pos.deal_id, "US500", pos.target_level, "TARGET", T0 + timedelta(minutes=1))
    ]


async def test_trigger_target_refuses_unknown_deal_and_position_without_target(multi):
    pos = await open_and_confirm(multi, us500())
    with pytest.raises(ValueError, match="DEAL-404"):
        multi.trigger_target("DEAL-404")
    with pytest.raises(ValueError, match="target"):
        multi.trigger_target(pos.deal_id)
    assert list(multi.positions) == [pos.deal_id]


async def test_trigger_stop_cannot_book_target_reason(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    with pytest.raises(ValueError, match="TARGET"):
        multi.trigger_stop(pos.deal_id, pos.target_level, reason="TARGET")  # cel tylko przez trigger_target


# --- symulacja świecy (R4) -----------------------------------------------------------------------


async def test_candle_touching_only_the_stop_closes_long_at_stop(multi):
    pos = await open_and_confirm(multi, us500(target="6"))  # fill 6800.6, stop 6796.6, cel 6806.6
    closed = multi.simulate_candle(bar("US500", "6800", "6801", "6796.6", "6797"))
    assert [(d.deal_id, d.reason, d.exit_level) for d in closed] == [(pos.deal_id, "STOP", D("6796.6"))]
    assert multi.positions == {} and await multi.closed_since(T0) == closed


async def test_candle_touching_only_the_target_closes_long_at_target(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    closed = multi.simulate_candle(bar("US500", "6801", "6806.6", "6797", "6805"))
    assert [(d.reason, d.exit_level) for d in closed] == [("TARGET", pos.target_level)]


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_candle_touching_stop_and_target_books_stop(multi, side):
    """R4: stop i cel w jednej świecy to STOP (konserwatywnie; kolejność wewnątrz świecy jest nieznana)."""
    pos = await open_and_confirm(multi, us500(side=side, target="6"))
    low, high = min(pos.stop_level, pos.target_level) - 1, max(pos.stop_level, pos.target_level) + 1
    closed = multi.simulate_candle(bar("US500", "6800.3", str(high), str(low), "6800.3"))
    assert [(d.reason, d.exit_level) for d in closed] == [("STOP", pos.stop_level)]


async def test_candle_touching_only_the_target_closes_short_at_target(multi):
    pos = await open_and_confirm(multi, us500(side=Side.SELL, target="6"))  # fill 6800.0, stop 6804.0, cel 6794.0
    closed = multi.simulate_candle(bar("US500", "6799", "6800", "6794", "6795"))
    assert [(d.reason, d.exit_level) for d in closed] == [("TARGET", D("6794.0"))]
    assert pos.target_level == D("6794.0")


async def test_candle_gapping_through_the_stop_exits_at_the_open(multi):
    await open_and_confirm(multi, us500())  # stop 6796.6
    [deal] = multi.simulate_candle(bar("US500", "6790", "6791", "6785", "6788"))
    assert (deal.reason, deal.exit_level) == ("STOP", D("6790"))
    pos = await open_and_confirm(multi, us500(side=Side.SELL))  # stop 6804.0
    [deal] = multi.simulate_candle(bar("US500", "6810", "6812", "6809", "6811"))
    assert (deal.deal_id, deal.reason, deal.exit_level) == (pos.deal_id, "STOP", D("6810"))


async def test_candle_spread_moves_the_closing_side(multi):
    """Świeca to ceny mid; pozycja BUY zamyka się po bid = mid − spread/2, SELL po ask = mid + spread/2."""
    await open_and_confirm(multi, us500())  # stop BUY 6796.6
    # mid low 6797.0 nie dotyka stopu, ale bid low = 6797.0 − 0.5 = 6796.5 już tak
    assert multi.simulate_candle(bar("US500", "6800", "6801", "6797", "6800")) == []
    [deal] = multi.simulate_candle(bar("US500", "6800", "6801", "6797", "6800"), spread=D("1"))
    assert (deal.reason, deal.exit_level) == ("STOP", D("6796.6"))


async def test_candle_moves_only_positions_of_its_instrument_and_keeps_order(multi):
    de = await open_and_confirm(multi, de40())
    a = await open_and_confirm(multi, us500())
    b = await open_and_confirm(multi, us500(target="2"))
    calls = list(multi.calls)
    closed = multi.simulate_candle(bar("US500", "6800", "6803", "6790", "6795"))
    assert [d.deal_id for d in closed] == [a.deal_id, b.deal_id] and list(multi.positions) == [de.deal_id]
    assert multi.calls == calls  # sterowanie testowe, nie wywołanie brokera


async def test_candle_ignores_missing_protection_and_quiet_bars(multi):
    pos = await open_and_confirm(multi, us500(target="6"))
    multi.positions[pos.deal_id] = OpenPosition(*[getattr(pos, n) for n in ("deal_id", "epic", "side", "size")],
                                                pos.open_level, None, pos.opened_at)  # fmt: skip
    assert multi.simulate_candle(bar("US500", "6800", "6900", "6700", "6800")) == []
    assert list(multi.positions) == [pos.deal_id]


def test_candle_of_unknown_instrument_or_negative_spread_is_a_test_error(multi):
    with pytest.raises(ValueError, match="US100"):
        multi.simulate_candle(bar("US100", "1", "1", "1", "1"))
    with pytest.raises(ValueError, match="spread"):
        multi.simulate_candle(bar("US500", "1", "1", "1", "1"), spread=D("-1"))
