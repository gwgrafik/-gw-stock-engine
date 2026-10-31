"""Wspólne testy kontraktowe interfejsu `Broker` (FakeBroker, a w Zadaniu 13 adapter Capital.com).

Klasa nie zaczyna się od `Test`, więc pytest nie zbiera jej samej. Podklasa `Test...(BrokerContract)` nadpisuje:
- fixture `broker`: świeży, niezalogowany adapter, bez otwartych pozycji i zleceń oczekujących,
  z instrumentem `epic` i co najmniej jednym rachunkiem;
- fixture `close_market`: funkcja bez argumentów, która zamyka rynek instrumentu po stronie brokera
  (FakeBroker: `set_market_closed(True)`; adapter: stan lokalnego serwera testowego);
- fixture `add_working_order`: funkcja `(epic, side, size, level) -> WorkingOrder`, która umieszcza zlecenie
  oczekujące po stronie brokera i zwraca to, co broker ma potem raportować; podklasa, która nie umie
  go utworzyć, wywołuje w tej fixture `pytest.skip(powód)` (pomija tylko ten jeden test).
Każdy test sam wywołuje `login()`. Testy nie zależą od zegara silnika: czasy pochodzą od brokera.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest

from gwstock.broker.base import Broker, Capabilities
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    EntryRequest,
    MarketClosed,
    OpenPosition,
    Side,
    WorkingOrder,
)


class BrokerContract:
    epic = "DE40"
    stop_distance = Decimal("30")

    @pytest.fixture
    def broker(self) -> Broker:
        raise NotImplementedError("subclass must override the `broker` fixture")

    @pytest.fixture
    def close_market(self) -> Callable[[], None]:
        raise NotImplementedError("subclass must override the `close_market` fixture")

    @pytest.fixture
    def add_working_order(self) -> Callable[[str, Side, Decimal, Decimal], WorkingOrder]:
        raise NotImplementedError(
            "subclass must override the `add_working_order` fixture "
            "(call pytest.skip(...) in it if the test broker cannot hold a working order)"
        )

    async def _request(self, broker: Broker, side: Side) -> EntryRequest:
        rules = await broker.instrument_rules(self.epic)
        stop = max(self.stop_distance, rules.min_stop_distance)
        return EntryRequest(f"gws-contract-{side.value.lower()}", self.epic, side, rules.min_size, stop)

    async def _open(self, broker: Broker, side: Side) -> tuple[EntryRequest, OpenPosition]:
        req = await self._request(broker, side)
        ack = await broker.open_position(req)
        assert ack.deal_reference
        pos = await broker.confirm(ack.deal_reference)
        assert pos is not None, "confirm() of a fresh entry must return the open position"
        return req, pos

    async def test_login_and_keepalive(self, broker):
        await broker.login()
        await broker.keepalive()
        assert isinstance(broker.capabilities, Capabilities) and broker.capabilities.keepalive_s > 0

    async def test_instrument_rules_and_quote_for_epic(self, broker):
        await broker.login()
        rules = await broker.instrument_rules(self.epic)
        assert rules.epic == self.epic and rules.min_size > 0 and rules.size_step > 0
        q = await broker.quote(self.epic)
        assert q.epic == self.epic and q.bid <= q.ask and q.ts.utcoffset() == timedelta(0)

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    async def test_open_attaches_stop_atomically_at_stop_distance_from_fill(self, broker, side):
        await broker.login()
        req, pos = await self._open(broker, side)
        # pierwsza obserwacja pozycji ma już stop: brak okna bez ochrony
        assert (pos.epic, pos.side, pos.size) == (self.epic, side, req.size)
        if side is Side.BUY:
            assert pos.stop_level == pos.open_level - req.stop_distance
        else:
            assert pos.stop_level == pos.open_level + req.stop_distance
        assert pos.opened_at.utcoffset() == timedelta(0)

    async def test_confirm_of_unknown_reference_returns_none(self, broker):
        await broker.login()
        assert await broker.confirm("REF-does-not-exist") is None

    async def test_list_positions_shows_confirmed_position(self, broker):
        await broker.login()
        _, pos = await self._open(broker, Side.BUY)
        assert await broker.list_positions() == [pos]

    async def test_close_position_empties_list_and_reports_closed_deal(self, broker):
        await broker.login()
        _, pos = await self._open(broker, Side.BUY)
        await broker.close_position(pos.deal_id)
        assert await broker.list_positions() == []
        closed = [d for d in await broker.closed_since(pos.opened_at) if d.deal_id == pos.deal_id]
        assert len(closed) == 1
        assert closed[0].epic == self.epic and closed[0].reason == "MANUAL" and closed[0].ts >= pos.opened_at

    async def test_close_of_unknown_deal_is_rejected(self, broker):
        await broker.login()
        with pytest.raises(BrokerRejected):
            await broker.close_position("DEAL-does-not-exist")

    async def test_market_closed_refuses_open_and_close_but_keeps_reads(self, broker, close_market):
        await broker.login()
        _, pos = await self._open(broker, Side.BUY)
        req = await self._request(broker, Side.SELL)
        close_market()
        with pytest.raises(MarketClosed):
            await broker.open_position(req)
        with pytest.raises(MarketClosed):
            await broker.close_position(pos.deal_id)
        assert await broker.list_positions() == [pos]  # stop u brokera zostaje, pozycja nietknięta
        assert (await broker.quote(self.epic)).epic == self.epic

    async def test_accounts_select_and_active_account_agree(self, broker):
        await broker.login()
        accounts = await broker.list_accounts()
        assert accounts and len({a.account_id for a in accounts}) == len(accounts)
        for listed in accounts:
            await broker.select_account(listed.account_id)
            active = await broker.account()
            assert (active.account_id, active.currency, active.status, active.equity) == (
                listed.account_id,
                listed.currency,
                listed.status,
                listed.equity,
            )
            assert active.available == active.equity - active.margin_used

    async def test_select_unknown_account_raises_broker_error(self, broker):
        await broker.login()
        with pytest.raises(BrokerError):
            await broker.select_account("ACC-does-not-exist")

    async def test_working_orders_are_empty_by_default(self, broker):
        await broker.login()
        assert await broker.list_working_orders() == []

    async def test_working_order_at_broker_is_listed(self, broker, add_working_order):
        await broker.login()
        rules = await broker.instrument_rules(self.epic)
        q = await broker.quote(self.epic)
        order = add_working_order(self.epic, Side.BUY, rules.min_size, q.bid - Decimal("100"))
        assert await broker.list_working_orders() == [order]
