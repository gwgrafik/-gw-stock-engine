"""Wspólne testy kontraktowe interfejsu `Broker` (FakeBroker, a w Zadaniu 13 adapter Capital.com).

Klasa nie zaczyna się od `Test`, więc pytest nie zbiera jej samej. Podklasa `Test...(BrokerContract)` nadpisuje:
- fixture `broker`: świeży, niezalogowany adapter, bez otwartych pozycji i zleceń oczekujących,
  z instrumentem `epic` i co najmniej jednym rachunkiem;
- fixture `close_market`: funkcja bez argumentów, która zamyka rynek instrumentu po stronie brokera
  (FakeBroker: `set_market_closed(True)`; adapter: stan lokalnego serwera testowego);
- fixture `add_working_order`: funkcja `(epic, side, size, level) -> WorkingOrder`, która umieszcza zlecenie
  oczekujące po stronie brokera i zwraca to, co broker ma potem raportować;
- fixture `reject_next_entry`: funkcja `(reason) -> None`: broker przyjmie najbliższe wejście (zwróci referencję),
  ale go nie wykona, a potwierdzenie pokaże odrzucenie z tym powodem;
- fixture `expire_session`: funkcja bez argumentów, po której broker traktuje sesję jako wygasłą.
- fixture `supports_target` (zadanie 6): zwraca True, gdy adapter obsługuje cel zysku (`TargetedEntryRequest`,
  `set_protection`); domyślnie `pytest.skip`, więc testy celu są pomijane w adapterze, który jeszcze go nie ma
  (Capital.com do zadania 7). Podklasa włącza je, nadpisując fixture;
- fixture `trigger_target` (zadanie 6): funkcja `(deal_id) -> None`, po której broker zamknął pozycję celem zysku
  po poziomie celu (FakeBroker: `trigger_target`; adapter: stan lokalnego serwera testowego); domyślnie `pytest.skip`.
Hooki `add_working_order`, `reject_next_entry` i `expire_session` są opcjonalne: podklasa, która nie umie danego
stanu wytworzyć (np. adapter z przezroczystym ponownym logowaniem), wywołuje w tej fixture `pytest.skip(powód)`,
co pomija tylko test, który jej używa.
Każdy test sam wywołuje `login()`. Testy nie zależą od zegara silnika: czasy pochodzą od brokera.
Porównania `list_positions() == [pos]` wymagają, żeby `confirm` i `list_positions` budowały `OpenPosition`
identycznie (te same pola, także `stop_derived`) dla świeżo otwartej, niezmienionej pozycji.
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
    SessionExpired,
    Side,
    TargetedEntryRequest,
    WorkingOrder,
)


class BrokerContract:
    epic = "DE40"
    stop_distance = Decimal("30")
    target_distance = Decimal("45")

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

    @pytest.fixture
    def reject_next_entry(self) -> Callable[[str], None]:
        raise NotImplementedError(
            "subclass must override the `reject_next_entry` fixture "
            "(call pytest.skip(...) in it if the test broker cannot reject a deal after acknowledging it)"
        )

    @pytest.fixture
    def expire_session(self) -> Callable[[], None]:
        raise NotImplementedError(
            "subclass must override the `expire_session` fixture "
            "(call pytest.skip(...) in it if the adapter cannot surface an expired session)"
        )

    @pytest.fixture
    def supports_target(self) -> bool:
        pytest.skip("this broker does not support a profit target yet (Capital.com: task 7)")

    @pytest.fixture
    def trigger_target(self) -> Callable[[str], None]:
        pytest.skip("this test broker cannot close a position by its profit target yet (Capital.com: task 7)")

    async def _request(self, broker: Broker, side: Side) -> EntryRequest:
        rules = await broker.instrument_rules(self.epic)
        stop = max(self.stop_distance, rules.min_stop_distance)
        return EntryRequest(f"gws-contract-{side.value.lower()}", self.epic, side, rules.min_size, stop)

    async def _open(self, broker: Broker, side: Side) -> tuple[EntryRequest, str, OpenPosition]:
        req = await self._request(broker, side)
        ack = await broker.open_position(req)
        assert ack.deal_reference
        pos = await broker.confirm(ack.deal_reference)
        assert pos is not None, "confirm() of a fresh entry must return the open position"
        return req, ack.deal_reference, pos

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
        req, _, pos = await self._open(broker, side)
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

    async def test_confirm_after_close_still_returns_the_fill_snapshot(self, broker):
        await broker.login()
        _, ref, pos = await self._open(broker, Side.BUY)
        await broker.close_position(pos.deal_id)
        assert await broker.confirm(ref) == pos

    async def test_rejected_deal_is_acknowledged_then_confirm_raises(self, broker, reject_next_entry):
        await broker.login()
        req = await self._request(broker, Side.BUY)
        reject_next_entry("REJECTED_BY_CONTRACT_TEST")
        ack = await broker.open_position(req)
        with pytest.raises(BrokerRejected) as info:
            await broker.confirm(ack.deal_reference)
        assert info.value.reason == "REJECTED_BY_CONTRACT_TEST"
        assert await broker.list_positions() == []

    async def test_list_positions_shows_confirmed_position(self, broker):
        await broker.login()
        _, _, pos = await self._open(broker, Side.BUY)
        assert await broker.list_positions() == [pos]

    async def test_close_position_empties_list_and_reports_closed_deal(self, broker):
        await broker.login()
        _, _, pos = await self._open(broker, Side.BUY)
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
        _, _, pos = await self._open(broker, Side.BUY)
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

    async def test_set_stop_moves_stop_of_open_position(self, broker):
        await broker.login()
        _, _, pos = await self._open(broker, Side.BUY)
        wider = pos.stop_level - Decimal("10")  # dalej od rynku: zawsze dozwolone
        await broker.set_stop(pos.deal_id, wider)
        [moved] = await broker.list_positions()
        assert (moved.deal_id, moved.stop_level) == (pos.deal_id, wider)

    async def test_set_stop_of_unknown_deal_is_rejected(self, broker):
        await broker.login()
        with pytest.raises(BrokerRejected):
            await broker.set_stop("DEAL-does-not-exist", Decimal("1000"))

    async def test_expired_session_raises_session_expired_until_login(self, broker, expire_session):
        await broker.login()
        expire_session()
        with pytest.raises(SessionExpired):
            await broker.list_positions()
        await broker.login()
        assert await broker.list_positions() == []

    # --- cel zysku (zadanie 6; adapter Capital.com włącza w zadaniu 7) ------------------------------

    async def _targeted_request(self, broker: Broker, side: Side) -> TargetedEntryRequest:
        req = await self._request(broker, side)
        rules = await broker.instrument_rules(self.epic)
        target = max(self.target_distance, rules.min_stop_distance)
        return TargetedEntryRequest(req.client_id, req.epic, req.side, req.size, req.stop_distance, target)

    async def _open_targeted(self, broker: Broker, side: Side) -> tuple[TargetedEntryRequest, OpenPosition]:
        req = await self._targeted_request(broker, side)
        ack = await broker.open_position(req)
        pos = await broker.confirm(ack.deal_reference)
        assert pos is not None, "confirm() of a fresh entry must return the open position"
        return req, pos

    async def test_plain_entry_has_no_target(self, broker):
        await broker.login()
        _, _, pos = await self._open(broker, Side.BUY)
        assert (pos.target_level, pos.target_derived) == (None, False)

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    async def test_open_attaches_stop_and_target_atomically(self, broker, supports_target, side):
        await broker.login()
        req, pos = await self._open_targeted(broker, side)
        sign = 1 if side is Side.BUY else -1
        assert pos.stop_level == pos.open_level - sign * req.stop_distance
        assert pos.target_level == pos.open_level + sign * req.target_distance  # cel po stronie zysku
        assert await broker.list_positions() == [pos]

    async def test_target_closer_than_broker_minimum_is_rejected_without_position(self, broker, supports_target):
        await broker.login()
        req = await self._targeted_request(broker, Side.BUY)
        rules = await broker.instrument_rules(self.epic)
        too_close = TargetedEntryRequest(
            req.client_id, req.epic, req.side, req.size, req.stop_distance, rules.min_stop_distance / 2
        )
        with pytest.raises(BrokerRejected):  # odmowa przy wysłaniu albo przy potwierdzeniu (dealStatus REJECTED)
            ack = await broker.open_position(too_close)
            await broker.confirm(ack.deal_reference)
        assert await broker.list_positions() == []

    async def test_set_protection_moves_stop_and_target_together(self, broker, supports_target):
        await broker.login()
        _, pos = await self._open_targeted(broker, Side.BUY)
        wider_stop = pos.stop_level - Decimal("10")  # dalej od rynku: zawsze dozwolone
        farther_target = pos.target_level + Decimal("10")
        await broker.set_protection(pos.deal_id, wider_stop, farther_target)
        [moved] = await broker.list_positions()
        assert (moved.deal_id, moved.stop_level, moved.target_level) == (pos.deal_id, wider_stop, farther_target)

    async def test_set_protection_adds_target_to_untargeted_position(self, broker, supports_target):
        await broker.login()
        _, _, pos = await self._open(broker, Side.SELL)
        target = pos.open_level - self.target_distance
        await broker.set_protection(pos.deal_id, pos.stop_level, target)
        [moved] = await broker.list_positions()
        assert (moved.stop_level, moved.target_level) == (pos.stop_level, target)

    async def test_set_protection_with_target_on_the_loss_side_is_rejected_and_changes_nothing(
        self, broker, supports_target
    ):
        await broker.login()
        _, pos = await self._open_targeted(broker, Side.BUY)
        with pytest.raises(BrokerRejected):
            await broker.set_protection(pos.deal_id, pos.stop_level - Decimal("10"), pos.open_level - Decimal("100"))
        assert await broker.list_positions() == [pos]

    async def test_set_protection_of_unknown_deal_is_rejected(self, broker, supports_target):
        await broker.login()
        with pytest.raises(BrokerRejected):
            await broker.set_protection("DEAL-does-not-exist", Decimal("1000"), Decimal("2000"))

    async def test_target_hit_closes_position_and_reports_target_reason(self, broker, supports_target, trigger_target):
        await broker.login()
        _, pos = await self._open_targeted(broker, Side.BUY)
        trigger_target(pos.deal_id)
        assert await broker.list_positions() == []
        closed = [d for d in await broker.closed_since(pos.opened_at) if d.deal_id == pos.deal_id]
        assert len(closed) == 1
        assert (closed[0].epic, closed[0].reason, closed[0].exit_level) == (self.epic, "TARGET", pos.target_level)
