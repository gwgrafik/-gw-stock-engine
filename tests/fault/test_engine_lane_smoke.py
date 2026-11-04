"""Silnik pod próbą pasma (`smoke --epic`, zadanie 17): wykonawca próby i jednorazowe przepuszczenie wejścia próby
przez drugą linię obrony `GuardedBroker` (pasmo bez zamrożonego odcisku), bez zmiany trybu pasma i bez uzbrajania.

Niezmienniki: pasmo zostaje dry_run (wykonawca pasma, powód, `_lane_block` poza próbą); przepuszczenie działa tylko w
bloku `lane_smoke_entry`, tylko dla zlecenia roli `smoke` tego instrumentu i tylko przy procesie live; zwykłe wejście
pasma i wejście po bloku są nadal odrzucane (`LANE_DRY_RUN_NO_ORDERS`). Uzbrojenie wyłącznie `engine_sim.arm`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gwstock.app import Mode
from gwstock.config import load_lanes
from gwstock.types import BrokerRejected, Side, TargetedEntryRequest
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    US,
    UsMarket,
    build_lane_broker,
    cfgs,
    clock,
    config_dir,
    data_dir,
    day_rules,
    journal_path,
    make_engine,
    market,
    runtime,
    sim_root,
    write_lane_files,
)

D = Decimal


@pytest.fixture
def broker(clock, day_rules, market):
    return build_lane_broker(clock, day_rules, market, UsMarket())


@pytest.fixture
def lanes(config_dir):
    return load_lanes(write_lane_files(config_dir))


def request(role: str = "smoke", epic: str = US) -> TargetedEntryRequest:
    return TargetedEntryRequest(f"gws-20261102-{epic}-1-{role}", epic, Side.BUY, D("0.01"), D("5"), D("7.5"))


async def verified(engine) -> None:
    assert await engine.login() and await engine.verify_account() is None


async def test_the_smoke_executor_is_live_but_the_lane_stays_dry_run(make_engine, lanes):
    engine = make_engine(Mode.LIVE, lanes=lanes)  # bez warunku uzbrojenia pasma: odcisk niezamrożony
    lane = engine.lane_for(US)
    assert lane is not None and lane.arming_problem is not None and not lane.executor.live
    executor = engine.smoke_executor(US)
    assert executor.live and executor is not lane.executor and executor.lane.epic == US
    assert executor.lane.uses_target and executor.memory is engine.executor.memory
    assert lane.executor.live is False and engine.lane_for(US) is lane  # tryb pasma bez zmian
    assert engine.lane_for("DE40") is None and engine.lane_for("NOPE") is None


async def test_the_smoke_executor_is_dry_run_in_a_dry_run_process(make_engine, lanes):
    engine = make_engine(Mode.DRY_RUN, lanes=lanes)
    assert engine.smoke_executor(US).live is False


async def test_only_a_smoke_order_inside_the_block_passes_the_lane_guard(make_engine, lanes, broker):
    engine = make_engine(Mode.LIVE, lanes=lanes)
    await verified(engine)
    with pytest.raises(BrokerRejected, match="LANE_DRY_RUN_NO_ORDERS"):
        await engine.broker.open_position(request())  # poza blokiem: jak dotąd
    with engine.lane_smoke_entry(US):
        with pytest.raises(BrokerRejected, match="LANE_DRY_RUN_NO_ORDERS"):
            await engine.broker.open_position(request("entry"))  # zwykłe wejście pasma: nadal odrzucone
        ack = await engine.broker.open_position(request())
    assert ack.deal_reference and broker.calls.count("open_position") == 1
    with pytest.raises(BrokerRejected, match="LANE_DRY_RUN_NO_ORDERS"):
        await engine.broker.open_position(request())  # po bloku: znowu odrzucone
    assert engine._lane_block(US) == "LANE_DRY_RUN_NO_ORDERS"


async def test_the_block_never_opens_a_dry_run_process_or_an_unverified_account(make_engine, lanes, broker):
    engine = make_engine(Mode.DRY_RUN, lanes=lanes)
    with engine.lane_smoke_entry(US):
        with pytest.raises(BrokerRejected, match="DRY_RUN_NO_ORDERS"):
            await engine.broker.open_position(request())
    live = make_engine(Mode.LIVE, lanes=lanes)
    with live.lane_smoke_entry(US):
        with pytest.raises(BrokerRejected, match="ACCOUNT_UNVERIFIED"):
            await live.broker.open_position(request())
    assert "open_position" not in broker.calls


async def test_the_block_is_reset_after_an_exception(make_engine, lanes, broker):
    engine = make_engine(Mode.LIVE, lanes=lanes)
    await verified(engine)
    with pytest.raises(RuntimeError):
        with engine.lane_smoke_entry(US):
            raise RuntimeError("boom")
    with pytest.raises(BrokerRejected, match="LANE_DRY_RUN_NO_ORDERS"):
        await engine.broker.open_position(request())


async def test_the_block_refuses_an_instrument_without_a_lane(make_engine, lanes):
    engine = make_engine(Mode.LIVE, lanes=lanes)
    with pytest.raises(ValueError, match="no enabled lane"):
        with engine.lane_smoke_entry("DE40"):
            pass
    with pytest.raises(ValueError, match="no enabled lane"):
        engine.smoke_executor("NOPE")


async def test_reconcile_lane_once_returns_the_result_and_refresh_fx_feeds_the_rate(make_engine, lanes, broker):
    engine = make_engine(Mode.LIVE, lanes=lanes)
    await verified(engine)
    result = await engine.reconcile_lane_once(US)
    assert result is not None and not result.skipped and result.unmanaged == []
    q = await engine.refresh_fx()
    assert q is not None and q.epic == "EURUSD" and engine._fx.to_account(D("1"), "USD") is not None
    broker.fail_next("quote", BrokerRejected("x"))
    assert await engine.refresh_fx() is None
    broker.fail_next("list_positions", BrokerRejected("x"))
    assert await engine.reconcile_lane_once(US) is None


async def test_without_lanes_the_hooks_have_nothing_to_offer(make_engine):
    engine = make_engine(Mode.LIVE)
    assert engine.lane_for(US) is None and await engine.refresh_fx() is None
    assert await engine.reconcile_lane_once(US) is None
