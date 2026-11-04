"""Wydruk i przebieg próby `smoke` DE40 (bez `--epic`) zamrożone PRZED dodaniem próby pasma (`smoke --epic`).

Oczekiwane linie i kolejność wywołań brokera nagrano z kodu sprzed zmiany (zadanie 17); każda różnica znaczy, że
próba DE40 przestała być identyczna. Scenariusze: sukces, wstrzymanie, dry_run, drugie zamknięcie przyjęte, kill switch
oraz silnik z włączonym `config/lanes.toml` (próba DE40 zatrzymana bramką portfelową jak przed zmianą).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import gwstock.cli as cli
from gwstock.app import Mode
from gwstock.config import load_lanes
from gwstock.types import Candle
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    D,
    UsMarket,
    berlin,
    broker,
    build_lane_broker,
    cfgs,
    clock,
    config_dir,
    daily_candles,
    data_dir,
    day_rules,
    engine,
    five_minute_candles,
    journal_path,
    make_engine,
    market,
    runtime,
    sim_root,
    write_lane_files,
)

ACCOUNT = "PASS [1] bound account found, ENABLED, EUR, balance > 0, active in this session"
ENTRY = (
    "PASS [2] entry accepted: deal DEAL-1, open level 25782.3, stop level 25752.3, size 0.001, stop reported by the API"
)
VISIBLE = "PASS [3] position visible in list_positions with a stop"
FLATTEN = "PASS [4] flatten through the executor (DELETE by dealId)"
BOOKED = (
    "PASS [5] position gone at the broker and the trade booked: gross 0.00, spread 0.00, slippage 0.00, fees 0.00, "
    "financing 0.00, net 0.00, exit FORCED"
)
CANDLES = [
    f"INFO [7] 5m candle start 2026-11-02T10:{mm}:00+00:00 UTC (11:{mm} CET): open 25781 high 25784 low 25779 close 25782"
    for mm in ("45", "50", "55")
] + [
    "INFO [7] compare these candles with the Capital.com 5-minute chart (mid prices): the bar starting at 09:00 "
    "Europe/Berlin must be the 09:00-09:05 bar; arm only after this matches",
    "PASS [7] completed 5-minute candles readable",
    "INFO [8] history latency: the closure appeared in closed_since 2.0 s after the close was sent",
]
PASS_LINES = [
    ACCOUNT, ENTRY, VISIBLE, FLATTEN, BOOKED, "PASS [6] second close of DEAL-1 rejected with POSITION_NOT_FOUND",
    *CANDLES, "PASS smoke: 7/7 checks passed",
]  # fmt: skip
PASS_CALLS = [
    "login", "list_accounts", "account", "list_positions", "list_working_orders", "instrument_rules", "quote",
    "daily_candles", "account", "open_position", "confirm", "list_positions", "close_position", "closed_since", "quote",
    "list_positions", "close_position", "candles_5m",
]  # fmt: skip
HOLD_LINES = [
    ACCOUNT, ENTRY, VISIBLE,
    *(f"INFO [3] holding {s} s more: look at the position and its stop in the Capital.com app" for s in (60, 45, 30, 15)),
    "INFO [3] hold finished: flattening now", FLATTEN, BOOKED,
    "PASS [6] second close of DEAL-1 rejected with POSITION_NOT_FOUND", *CANDLES, "PASS smoke: 7/7 checks passed",
]  # fmt: skip
SECOND_LINES = [
    ACCOUNT, ENTRY, VISIBLE, FLATTEN, BOOKED,
    "FAIL [6] LOUD: a second close of DEAL-1 gave accepted (!) instead of POSITION_NOT_FOUND; the emergency path relies "
    "on it. Positions on DE40 now: []",
    *CANDLES, "FAIL smoke: 6/7 checks passed",
]  # fmt: skip
DRY_LINES = [
    "FAIL smoke runs only in live mode (live_allowed, data/ARM_LIVE, bound account, pre-registration); the engine is in "
    "dry_run, nothing was sent"
]
KILL_LINES = [ACCOUNT, "FAIL [2] entry not confirmed: not sent (gate: KILL)"]
LANES_LINES = [ACCOUNT, "FAIL [2] entry not confirmed: not sent (gate: PORTFOLIO_UNMANAGED_EXPOSURE)"]
LANES_CALLS = [
    "login", "list_accounts", "account", "list_positions", "list_working_orders", "instrument_rules", "quote",
    "daily_candles", "account",
]  # fmt: skip


@pytest.fixture
def smoke_clock(clock, broker):
    clock.advance((berlin(DAY, 12, 0) - clock.now_utc()).total_seconds())  # poza oknem strategii
    noon = berlin(DAY, 11, 30)
    flat = (D("25781"), D("25784"), D("25779"), D("25782"))
    broker.set_candles_5m(Candle("DE40", noon + timedelta(minutes=5 * k), *flat) for k in range(6))
    return clock


async def test_de40_smoke_output_and_broker_calls_are_unchanged(engine, broker, smoke_clock):
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 0
    assert lines == PASS_LINES
    assert broker.calls == PASS_CALLS


async def test_de40_smoke_hold_output_is_unchanged(engine, broker, smoke_clock):
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append, hold_s=60) == 0
    assert lines == HOLD_LINES


async def test_de40_smoke_dry_run_refusal_is_unchanged(make_engine, broker, smoke_clock):
    lines: list[str] = []
    assert await cli.smoke(make_engine(Mode.DRY_RUN), out=lines.append) == 1
    assert lines == DRY_LINES and broker.calls == []


async def test_de40_smoke_second_close_accepted_output_is_unchanged(engine, broker, smoke_clock, monkeypatch):
    real = broker.close_position
    seen = {"n": 0}

    async def close(deal_id):
        seen["n"] += 1
        return await real(deal_id) if seen["n"] == 1 else None

    monkeypatch.setattr(broker, "close_position", close)
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert lines == SECOND_LINES


async def test_de40_smoke_kill_switch_output_is_unchanged(engine, broker, smoke_clock):
    engine.kill.soft("STOP_FILE")
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert lines == KILL_LINES


async def test_de40_smoke_with_lanes_enabled_is_unchanged(make_engine, clock, day_rules, market, config_dir):
    clock.advance((berlin(DAY, 12, 0) - clock.now_utc()).total_seconds())
    fake = build_lane_broker(clock, day_rules, market, UsMarket())
    noon = berlin(DAY, 11, 30)
    flat = (D("25781"), D("25784"), D("25779"), D("25782"))
    fake.set_candles_5m([Candle("DE40", noon + timedelta(minutes=5 * k), *flat) for k in range(6)], epic="DE40")
    engine = make_engine(Mode.LIVE, broker=fake, lanes=load_lanes(write_lane_files(config_dir)))
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert lines == LANES_LINES and fake.calls == LANES_CALLS


def test_smoke_without_epic_dispatches_to_the_de40_probe(monkeypatch, tmp_path):
    seen: list[tuple] = []

    async def fake_smoke_engine(paths, hold_s=0, *rest, **kw):
        seen.append((hold_s, rest, kw))
        return 0

    monkeypatch.setattr(cli, "_smoke_engine", fake_smoke_engine)
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path))
    assert cli.main(["smoke"]) == 0
    assert cli.main(["smoke", "--hold", "30"]) == 0
    assert seen == [(0, (), {}), (30, (), {})]
