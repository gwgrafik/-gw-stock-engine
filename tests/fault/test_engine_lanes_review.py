"""Poprawki po niezależnym przeglądzie zadania 12 (silnik z pasmami): testy regresji F1-F6.

Każdy test ma w docstringu OCZEKIWANE zachowanie spisane przed uruchomieniem. Dzień wzorcowy jak w
`test_multi_lane.py`: poniedziałek 2026-11-02 (Berlin UTC+1, Nowy Jork UTC-5); DE40 decyzja 09:05, spłaszczenie 17:25
Berlin (16:25 UTC); US500 świeca sygnału B1 09:45-09:50 NY (14:45-14:50 UTC), wejście 14:50 UTC, zamknięcie NYSE
16:00 NY = 22:00 Berlin. Uzbrajanie wyłącznie przez `engine_sim` (fabryka, katalog tymczasowy); pasmo US500 live tylko z
jawnym warunkiem uzbrojenia pasma testu (`lane_armed`).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.app import Engine, Mode
from gwstock.broker.fake import FakeBroker
from gwstock.config import LANES_FILE, load_lanes
from gwstock.journal import TradeRecord
from gwstock.risk import KILL, LOSS_PER_TRADE
from gwstock.types import KillLevel, PositionState, Side
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    EPIC,
    LANES_TOML,
    US,
    SimClock,
    UsMarket,
    assert_portfolio_books_balance,
    berlin,
    build_lane_broker,
    cfgs,
    config_dir,
    data_dir,
    day_rules,
    engine_events,
    journal_path,
    lane_armed,
    lane_decisions,
    make_engine,
    market,
    new_york,
    payloads,
    runtime,
    sim_root,
    stop_engine_at,
    write_lane_files,
)

D = Decimal
P = PositionState
UTC = timezone.utc
US_ENTRY = new_york(DAY, 9, 50)  # 14:50 UTC


@pytest.fixture
def sim_day() -> date:
    return DAY


@pytest.fixture
def clock(sim_day) -> SimClock:
    return SimClock(berlin(sim_day, 8, 45))


@pytest.fixture
def us_market(sim_day) -> UsMarket:
    return UsMarket(day=sim_day, signals=[new_york(sim_day, 9, 45)])


@pytest.fixture
def broker(clock, day_rules, market, us_market, sim_day) -> FakeBroker:
    return build_lane_broker(clock, day_rules, market, us_market, sim_day)


@pytest.fixture
def lanes(config_dir):
    write_lane_files(config_dir)
    return load_lanes(config_dir)


@pytest.fixture
def lane_engine(make_engine, lanes) -> Engine:
    return make_engine(Mode.LIVE, lanes=lanes, lane_arming=lane_armed)


def gate_blocks(journal) -> list[dict]:
    return [json.loads(e.payload) for e in journal.events("gate_block")]


def broker_calls(broker: FakeBroker, clock: SimClock, methods: tuple[str, ...]) -> list[tuple[datetime, str, str]]:
    """Wywołania `methods` brokera (czas, metoda, instrument lub deal) zapisane PRZED skutkiem u brokera."""
    seen: list[tuple[datetime, str, str]] = []
    for name in methods:
        real = getattr(broker, name)

        async def spy(*args, _real=real, _name=name):
            first = args[0] if args else ""
            seen.append((clock.now_utc(), _name, getattr(first, "epic", first)))
            return await _real(*args)

        setattr(broker, name, spy)
    return seen


# --- F1: limit straty transakcji pasma przed wejściem ----------------------------------------------------------------


async def test_lane_loss_limit_tighter_than_v1_blocks_the_entry_before_any_order(
    make_engine, config_dir, broker, clock, journal_path
):
    """F1. Oczekiwane: `lanes.toml` daje pasmu US500 `max_loss_per_trade = 0.05` EUR (v1: 1.00). Kandydat B1 (0,01 ×
    10,8 pkt × 1 USD po kursie niekorzystnym ≈ 0,10 EUR) leży między limitem pasma a limitem v1, więc bramka pasma
    blokuje go z LOSS_PER_TRADE (min(pasmo, v1)) ZANIM cokolwiek pójdzie do brokera: zero `open_position` i zero
    `close_position` dla US500 (żadnego wejścia zamykanego od razu przez straż wielkości SIZE_MISMATCH_RISK za cenę
    spreadu), zero zleceń US500 w dzienniku, kill switch bez zmian. DE40 v1 handluje normalnie."""
    write_lane_files(config_dir)
    (config_dir / LANES_FILE).write_text(LANES_TOML.replace('max_loss_per_trade = "1.00"', 'max_loss_per_trade = "0.05"'))
    engine = make_engine(Mode.LIVE, lanes=load_lanes(config_dir), lane_arming=lane_armed)
    calls = broker_calls(broker, clock, ("open_position", "close_position"))
    stop_engine_at(engine, clock, US_ENTRY + timedelta(minutes=3))
    await engine.run_day(DAY)
    j = engine.journal
    assert [(m, e) for _, m, e in calls if m == "open_position"] == [("open_position", EPIC)]
    assert not [c for c in calls if c[1] == "close_position"]
    assert j.count_entry_orders(DAY, US) == 0
    us_blocks = [b for b in gate_blocks(j) if b.get("candidate", {}).get("epic") == US]
    assert us_blocks and all(LOSS_PER_TRADE in b["reasons"] for b in us_blocks)
    assert any(d["outcome"] == "ENTER" for d in lane_decisions(j))
    assert not engine_events(j, "SIZE_MISMATCH_RISK")
    assert engine.kill.level() == 0
    assert_portfolio_books_balance(journal_path)


# --- F2: nierozliczona transakcja USD w składzie bez pasm -----------------------------------------------------------

PENDING_DEAL = "DEAL-US-OLD"
PENDING_DAY = date(2026, 10, 30)  # piątek: transakcja US500 z dnia, gdy pasma jeszcze działały


def usd_trade(net: Decimal = D("-0.95"), instrument: str = US) -> TradeRecord:
    t0 = datetime(2026, 10, 30, 14, 50, tzinfo=UTC)
    return TradeRecord(
        instrument, Side.BUY, D("0.01"), D("7589.2"), D("7589.8"), D("7589.8"), D("7494.8"), D("-0.95"), D("0.006"),
        D("0"), D("0"), D("0"), net, t0, t0, t0, t0 + timedelta(minutes=20), 900, "STOP",
    )  # fmt: skip


def pending_trade(journal, deal: str = PENDING_DEAL, instrument: str = US) -> None:
    """Faza 1 księgowania transakcji w USD (wiersz księgi i `pnl_pending:<deal>`), jak zostawiło ją pasmo US500."""
    assert journal.record_trade_pending(usd_trade(instrument=instrument), deal, PENDING_DAY)


def kill_alerts(notifier, code: str) -> list[str]:
    return [m for _, m in notifier.alerts if code in m]


async def test_pending_usd_trade_without_lanes_soft_kills_at_start_and_blocks_the_de40_entry(
    make_engine, broker, clock, notifier
):
    """F2 (rozstrzygnięcie kontrolera). Oczekiwane: skład v1 (brak `config/lanes.toml`), dziennik ze znacznikiem
    `pnl_pending` transakcji US500 → przy starcie dnia kill SOFT z powodem PNL_PENDING_WITHOUT_LANES (nie odmowa
    startu), alarm ERROR z numerem deala i zdarzenie silnika; wejście DE40 o 09:05 zablokowane przez KILL (zero
    `open_position`); znacznik zostaje (silnik bez pasm niczego nie zgaduje i nie rozlicza)."""
    engine = make_engine(Mode.LIVE)
    assert engine.lanes == []
    pending_trade(engine.journal)
    calls = broker_calls(broker, clock, ("open_position",))
    stop_engine_at(engine, clock, berlin(DAY, 9, 12))
    await engine.run_day(DAY)
    j = engine.journal
    assert engine.kill.level() is KillLevel.SOFT and j.kill_state().reason == "PNL_PENDING_WITHOUT_LANES"
    assert any(PENDING_DEAL in m for m in kill_alerts(notifier, "PNL_PENDING_WITHOUT_LANES"))
    (event,) = engine_events(j, "PNL_PENDING_WITHOUT_LANES")
    assert event["deals"] == [PENDING_DEAL]
    assert calls == [] and j.count_entry_orders(DAY) == 0
    assert any(KILL in b["reasons"] for b in gate_blocks(j))
    assert j.pnl_pending_deals() == [(PENDING_DEAL, PENDING_DAY)]


async def test_pending_marker_appearing_after_start_still_blocks_the_next_v1_entry(make_engine, broker, clock):
    """F2: kontrola także tuż przed każdym wejściem v1. Oczekiwane: znacznik pojawia się o 09:02 (po starcie dnia,
    np. dziennik przywrócony z kopii), wejście DE40 o 09:05 zablokowane (kill SOFT PNL_PENDING_WITHOUT_LANES, KILL w
    `gate_block`), zero `open_position`."""
    engine = make_engine(Mode.LIVE)
    calls = broker_calls(broker, clock, ("open_position",))
    added: list[bool] = []

    def add_marker() -> None:
        if not added and clock.now_utc() >= berlin(DAY, 9, 2):
            added.append(True)
            pending_trade(engine.journal)

    clock.hooks.append(add_marker)
    stop_engine_at(engine, clock, berlin(DAY, 9, 12))
    await engine.run_day(DAY)
    assert added and calls == []
    assert engine.journal.kill_state().reason == "PNL_PENDING_WITHOUT_LANES"
    assert any(KILL in b["reasons"] for b in gate_blocks(engine.journal))


async def test_pending_usd_trade_without_lanes_still_manages_and_flattens_the_de40_position(
    make_engine, broker, clock, journal_path
):
    """F2: wyjścia i zarządzanie działają dalej. Oczekiwane: proces v1 wszedł w DE40 o 09:05 i padł; nowy proces
    startuje z niepustym `pnl_pending` (pasma wyłączone) → kill SOFT, ale pozycja DE40 dalej zarządzana i spłaszczona
    o 17:25 (`FORCED`), zaksięgowana raz; licznik dnia zawiera tylko DE40 (kwota USD nie jest zgadywana)."""
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 10))
    await engine.run_day(DAY)
    assert engine.journal.current_position(EPIC).state is P.OPEN
    pending_trade(engine.journal)
    clock.hooks.clear()
    restarted = make_engine.restart(engine, Mode.LIVE)
    report = await restarted.run_day(DAY)
    j = restarted.journal
    assert restarted.kill.level() is KillLevel.SOFT and j.kill_state().reason == "PNL_PENDING_WITHOUT_LANES"
    assert report.final_position_state is P.FLAT
    (de,) = [t for t in j.trades() if t.instrument == EPIC]
    assert de.exit_reason == "FORCED" and timedelta(0) <= de.exit_ts - berlin(DAY, 17, 25) < timedelta(seconds=10)
    assert j.realized_pnl(DAY) == de.net_pnl
    assert j.pnl_pending_deals() == [(PENDING_DEAL, PENDING_DAY)]

