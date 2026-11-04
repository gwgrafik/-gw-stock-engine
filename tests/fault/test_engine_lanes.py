"""Silnik z wieloma pasmami (zadanie 12, projekt wielu pasm §1.3, §2.1, §2.8, §5.4): DE40 v1 i pasmo B1/M5 na US500
w JEDNEJ korutynie, dzień portfela obejmujący sesję NYSE, bramka portfelowa, kurs EURUSD brokera, pozycje bez pasma,
cel zysku, limit czasu pozycji, awaryjne spłaszczenie wszystkich pasm i izolacja błędów pasma.

Dzień wzorcowy: poniedziałek 2026-11-02 (Berlin UTC+1, Nowy Jork UTC-5). DE40: decyzja 09:05, spłaszczenie 17:25
Berlin. US500: świeca sygnału B1 09:45-09:50 NY (14:45-14:50 UTC), wejście 14:50 UTC, limit czasu 50 min (15:40 UTC),
spłaszczenie pasma 15:55 NY, zamknięcie 16:00 NY = 22:00 Berlin. Uzbrajanie wyłącznie `engine_sim.arm` (fabryka) w
katalogu tymczasowym; pasmo US500 live tylko z jawnym warunkiem uzbrojenia pasma testu (`lane_arming`).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.app import Engine, EngineMisconfigured, Mode
from gwstock.broker.fake import FakeBroker
from gwstock.config import LANES_FILE, load_lanes
from gwstock.portfolio import PORTFOLIO_UNMANAGED_EXPOSURE
from gwstock.risk import LaneRiskGate, RiskGate
from gwstock.types import (
    BrokerRejected,
    Candle,
    EntryRequest,
    InstrumentRules,
    KillLevel,
    OpenPosition,
    PositionState,
    Quote,
    Side,
    TargetedEntryRequest,
)
from gwstock.watchdog import QUOTE_SILENCE
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    cfgs,
    clock,
    config_dir,
    daily_candles,
    disarm,
    data_dir,
    day_rules,
    five_minute_candles,
    journal_path,
    make_engine,
    market,
    payloads,
    runtime,
    sim_root,
    stop_engine_at,
)

D = Decimal
P = PositionState
UTC = timezone.utc
US = "US500"
US_RULES = InstrumentRules(US, D("0.01"), D("0.01"), D("1"), D("0.05"), D("0.5"), D("0.1"), "USD")
FX_RULES = InstrumentRules("EURUSD", D("100"), D("100"), D("0.0001"), D("0.0333"), D("0"), D("0"), None)
SIGNAL_START = datetime(2026, 11, 2, 14, 45, tzinfo=UTC)  # 09:45 NY
SIGNAL_END = SIGNAL_START + timedelta(minutes=5)
NY_CLOSE = datetime(2026, 11, 2, 21, 0, tzinfo=UTC)  # 16:00 NY = 22:00 Berlin
LANES_TOML = """enabled = true

[[lane]]
rule = "bar_b1_m5"
rule_file = "lane_us500_b1_m5.toml"
exchange_profile = "NYSE"
currency = "USD"
slippage_guard_fraction = "0.25"
max_loss_per_trade = "1.00"
costs_file = "costs_v1.toml"
"""


def us_quote_at(now: datetime) -> Quote:
    mid = D("7580") if now < SIGNAL_END else D("7589.5")
    return Quote(US, mid - D("0.3"), mid + D("0.3"), now)


def fx_quote_at(now: datetime) -> Quote:
    return Quote("EURUSD", D("1.0800"), D("1.0802"), now)


def us_candles() -> list[Candle]:
    """Świece M5 US500 13:00-21:00 UTC: płasko (zakres 4), jedna świeca wybicia B1 o 14:45 UTC, potem płasko wyżej."""
    out = []
    t = datetime(2026, 11, 2, 13, 0, tzinfo=UTC)
    while t < NY_CLOSE:
        if t < SIGNAL_START:
            out.append(Candle(US, t, D("7580"), D("7582"), D("7578"), D("7580")))
        elif t == SIGNAL_START:
            out.append(Candle(US, t, D("7580"), D("7590"), D("7579"), D("7589.5")))
        else:
            out.append(Candle(US, t, D("7590"), D("7591"), D("7589"), D("7590")))
        t += timedelta(minutes=5)
    return out


@pytest.fixture
def broker(clock, day_rules, market) -> FakeBroker:
    now = clock.now_utc()
    fake = FakeBroker(
        clock, [day_rules, US_RULES, FX_RULES], [market.quote(now), us_quote_at(now), fx_quote_at(now)],
        account_id="ACC-1",
    )  # fmt: skip
    fake.set_quote_fn(market.quote, epic="DE40")
    fake.set_quote_fn(us_quote_at, epic=US)
    fake.set_quote_fn(fx_quote_at, epic="EURUSD")
    fake.set_daily_candles(daily_candles(DAY), epic="DE40")
    fake.set_candles_5m(five_minute_candles(DAY, D("25780"), D("25790")), epic="DE40")
    fake.set_candles_5m(us_candles(), epic=US)
    return fake


@pytest.fixture
def lanes_dir(config_dir) -> Path:
    for name in ("lane_us500_b1_m5.toml", "portfolio.toml"):
        shutil.copy(Path("config") / name, config_dir / name)
    (config_dir / LANES_FILE).write_text(LANES_TOML)
    return config_dir


@pytest.fixture
def lanes(lanes_dir):
    return load_lanes(lanes_dir)


def armed(_lane) -> None:
    return None  # warunek uzbrojenia pasma testu (zadanie 13 dostarczy prawdziwy): pasmo wolno uzbroić


@pytest.fixture
def lane_engine(make_engine, lanes) -> Engine:
    return make_engine(Mode.LIVE, lanes=lanes, lane_arming=armed)


def events(journal, code=None):
    found = [p for src in ("engine", "event") for p in payloads(journal, src)]
    return found if code is None else [e for e in found if e.get("event") == code]


def lane_decisions(journal):
    return [p for p in payloads(journal, "decision") if p.get("event") == "LANE_DECISION"]


# --- złożenie ---------------------------------------------------------------------------------------------


def test_without_a_lanes_file_the_engine_is_the_v1_composition(make_engine):
    engine = make_engine(Mode.LIVE)
    assert engine.lanes == [] and type(engine.gate) is RiskGate and engine._portfolio is None


def test_the_lanes_file_enables_the_lane_but_never_arms_it(make_engine, lanes_dir):
    engine = make_engine(Mode.LIVE)  # bez warunku uzbrojenia pasma: domyślnie pre-rejestracja niezamrożona
    (lane,) = engine.lanes
    assert lane.epic == US and isinstance(engine.gate, LaneRiskGate)
    assert engine.executor.live and not lane.executor.live and lane.arming_problem
    engine.check_wiring()


def test_a_broken_lanes_file_refuses_to_start(make_engine, lanes_dir):
    (lanes_dir / LANES_FILE).write_text(LANES_TOML.replace('rule = "bar_b1_m5"', 'rule = "x"'))
    with pytest.raises(EngineMisconfigured):
        make_engine(Mode.LIVE)


async def test_a_dry_run_lane_never_reaches_the_broker_with_an_entry(make_engine, lanes_dir, broker):
    engine = make_engine(Mode.LIVE)
    assert await engine.login() and await engine.verify_account() is None  # DE40 mógłby już wysyłać
    with pytest.raises(BrokerRejected, match="LANE_DRY_RUN"):
        await engine.broker.open_position(TargetedEntryRequest("gws-20261102-US500-1-entry", US, Side.BUY,
                                                               D("0.01"), D("10"), D("15")))
    with pytest.raises(BrokerRejected, match="NO_LANE"):
        await engine.broker.open_position(EntryRequest("gws-20261102-EURUSD-1-entry", "EURUSD", Side.BUY,
                                                       D("100"), D("0.01")))
    assert "open_position" not in broker.calls


# --- dzień portfela z dwoma pasmami ---------------------------------------------------------------------------


async def test_two_lanes_trade_in_one_coroutine_over_the_nyse_session(lane_engine, broker, clock):
    seen: list[tuple[datetime, str]] = []
    broker.on_call("open_position", lambda: seen.append((clock.now_utc(), "open")))
    report = await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert clock.now_utc() >= NY_CLOSE  # pętla dnia nie śpi po 17:30: trwa do zamknięcia NYSE
    assert report.entries == 1  # wejście DE40 (licznik per instrument: wejście US500 go nie zużywa)
    us_orders = [o for o in (j.get_order(f"gws-20261102-{US}-{n}-entry") for n in range(1, 4)) if o is not None]
    assert len(us_orders) == 1 and j.count_entry_orders(DAY, US) == 1 and j.count_entry_orders(DAY, "DE40") == 1
    trades = dict(j.trades_by_deal())
    by_epic = {t.instrument: t for t in trades.values()}
    assert set(by_epic) == {"DE40", US}
    assert by_epic[US].exit_reason == "FORCED"
    flattened = [e for e in events(j, "FLATTENED") if e.get("flatten_reason") == "TIME_LIMIT"]
    assert len(flattened) == 1  # limit czasu 50 min (`lanes.exit_due`)
    (us_deal,) = [d for d, t in trades.items() if t.instrument == US]
    fx = j.trade_fx(us_deal)
    assert fx is not None and fx.source == "ESTIMATED_FX" and fx.rate in (D("1.0800"), D("1.0802"))
    assert j.pnl_pending_deals() == []
    assert j.current_position(US).state is P.FLAT and j.current_position("DE40").state is P.FLAT
    # oba pasma miały pozycję jednocześnie (DE40 09:05-17:25, US500 14:50-15:40 UTC)
    assert len(seen) == 2 and timedelta(0) <= seen[1][0] - SIGNAL_END < timedelta(seconds=10)
    entered = [d for d in lane_decisions(j) if d["outcome"] == "ENTER"]
    assert len(entered) == 1 and entered[0]["signal_candle_start_utc"] == SIGNAL_START.isoformat()
    assert all(d["mode"] == "live" for d in lane_decisions(j))


async def test_the_lane_enters_with_stop_and_target_together(lane_engine, broker, clock):
    sent: list = []
    original = broker.open_position

    async def spy(req):
        sent.append(req)
        return await original(req)

    broker.open_position = spy
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    us_req = [r for r in sent if r.epic == US]
    assert len(us_req) == 1 and isinstance(us_req[0], TargetedEntryRequest)
    pos = next(p for p in broker.positions.values() if p.epic == US)
    assert pos.stop_level is not None and pos.target_level is not None


async def test_a_dry_run_lane_decides_and_records_but_sends_nothing(make_engine, lanes, broker, clock):
    engine = make_engine(Mode.LIVE, lanes=lanes)  # pasmo nieuzbrojone
    stop_engine_at(engine, clock, SIGNAL_END + timedelta(minutes=2))
    await engine.run_day(DAY)
    decided = lane_decisions(engine.journal)
    assert any(d["outcome"] == "ENTER" and d["mode"] == "dry_run" for d in decided)
    assert engine.journal.count_entry_orders(DAY, US) == 0
    assert not any(p.epic == US for p in broker.positions.values())


# --- pozycje bez pasma, cel, HARD, zamknięty rynek, cisza, izolacja ----------------------------------------------


async def test_an_owner_position_without_a_lane_is_reported_once_never_touched_and_blocks_entries(
    lane_engine, broker, clock
):
    broker.positions["OWN-1"] = OpenPosition("OWN-1", "US100", Side.BUY, D("1"), D("20000"), D("19900"),
                                             clock.now_utc())
    stop_engine_at(lane_engine, clock, datetime(2026, 11, 2, 8, 20, tzinfo=UTC))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert len(events(j, "UNMANAGED_POSITION")) == 1
    assert "OWN-1" in broker.positions  # nigdy nie zamknięta
    blocks = [json.loads(e.payload) for e in j.events("gate_block")]
    assert blocks and PORTFOLIO_UNMANAGED_EXPOSURE in blocks[0]["portfolio_reasons"]
    assert j.count_entry_orders(DAY, "DE40") == 0


async def test_a_lane_position_without_a_target_gets_one_and_a_failure_is_only_an_alarm(
    lane_engine, make_engine, lanes, broker, clock, notifier
):
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    deal = next(d for d, p in broker.positions.items() if p.epic == US)
    pos = broker.positions[deal]
    broker.positions[deal] = OpenPosition(pos.deal_id, pos.epic, pos.side, pos.size, pos.open_level, pos.stop_level,
                                          pos.opened_at)  # cel zniknął u brokera
    broker.fail_next("set_protection", BrokerRejected("error.invalid.takeprofit"))
    clock.hooks.clear()
    restarted = make_engine.restart(lane_engine, Mode.LIVE, lanes=lanes, lane_arming=armed)
    stop_engine_at(restarted, clock, clock.now_utc() + timedelta(seconds=25))
    await restarted.run_day(DAY)
    assert any("TARGET_ATTACH_FAILED" in m for _, m in notifier.alerts)
    assert restarted.kill.level() is not KillLevel.HARD
    assert broker.calls.count("set_protection") == 2  # raz na przebieg zarządzania: porażka, potem sukces
    assert broker.positions[deal].target_level is not None  # stop i cel razem


async def test_hard_kill_flattens_both_lanes(lane_engine, broker, clock, data_dir):
    def hard() -> None:
        if clock.now_utc() >= SIGNAL_END + timedelta(minutes=2):
            (data_dir / "HARD_STOP").write_text("")

    clock.hooks.append(hard)
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=5))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.kill.level() is KillLevel.HARD
    assert j.current_position("DE40").state is P.FLAT and j.current_position(US).state is P.FLAT
    assert {t.instrument for t in j.trades()} == {"DE40", US}
    assert all(t.exit_reason == "FORCED" for t in j.trades())


async def test_a_closed_us500_market_does_not_hold_back_the_de40_flatten(lane_engine, broker, clock):
    def close_us_market() -> None:
        if clock.now_utc() >= SIGNAL_END + timedelta(minutes=1):
            broker.set_market_closed(True, epic=US)  # po wejściu: US500 nie da się zamknąć, DE40 tak

    clock.hooks.append(close_us_market)
    report = await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert report.final_position_state is P.FLAT
    de = [t for t in j.trades() if t.instrument == "DE40"]
    flatten = datetime(2026, 11, 2, 16, 25, tzinfo=UTC)  # 17:25 Berlin
    assert len(de) == 1 and de[0].exit_ts - flatten < timedelta(seconds=10)  # bez minuty zwłoki po odmowie US500
    assert j.current_position(US).state is P.EXITING  # US500: stop u brokera zostaje, ponowienia co minutę


async def test_disarming_mid_day_downgrades_every_lane_and_no_lane_entry_follows(lane_engine, broker, clock, data_dir):
    done: list[bool] = []

    def owner_disarms() -> None:
        if not done and clock.now_utc() >= SIGNAL_START:
            done.append(True)
            disarm(data_dir)  # właściciel usuwa plik uzbrojenia (engine_sim)

    clock.hooks.append(owner_disarms)
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    assert lane_engine.mode is Mode.DRY_RUN and not lane_engine.lanes[0].executor.live
    assert lane_engine.journal.count_entry_orders(DAY, US) == 0
    assert any(d["outcome"] == "ENTER" for d in lane_decisions(lane_engine.journal))  # decyzja jest, zlecenia nie
    assert events(lane_engine.journal, "MODE_DOWNGRADED")


async def test_quote_silence_on_us500_alone_soft_kills_and_names_the_instrument(lane_engine, broker, clock, notifier):
    frozen = us_quote_at(datetime(2026, 11, 2, 14, 0, tzinfo=UTC))
    broker.set_quote_fn(lambda now: Quote(US, frozen.bid, frozen.ask, frozen.ts), epic=US)
    stop_engine_at(lane_engine, clock, datetime(2026, 11, 2, 14, 40, tzinfo=UTC))
    await lane_engine.run_day(DAY)
    assert lane_engine.kill.level() is KillLevel.SOFT
    assert any(QUOTE_SILENCE in m and US in m for _, m in notifier.alerts)


async def test_an_exception_in_one_lane_does_not_stop_the_other_lane_or_the_exits(lane_engine, broker, clock,
                                                                                  monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("lane bug")

    monkeypatch.setattr(lane_engine, "_bar_decision_step", boom)
    report = await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert events(j, "LANE_STEP_FAILED")
    assert report.entries == 1 and report.final_position_state is P.FLAT  # DE40 wszedł i został spłaszczony
    assert [t.instrument for t in j.trades()] == ["DE40"]


async def test_session_index_is_per_instrument(lane_engine, broker, clock):
    await lane_engine.run_day(DAY)
    assert lane_engine.session_index() == 1  # tylko transakcja DE40
    assert lane_engine.lanes[0].session_index(lane_engine.journal) == 1


async def test_no_valid_fx_rate_blocks_the_lane_entry(lane_engine, broker, clock):
    broker.set_quote_fn(lambda now: Quote("EURUSD", D("1.08"), D("1.0802"), now - timedelta(hours=1)), epic="EURUSD")
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert j.count_entry_orders(DAY, US) == 0
    blocks = [json.loads(e.payload) for e in j.events("gate_block")]
    assert any("FX_UNAVAILABLE" in r for b in blocks for r in b["reasons"])


async def test_losing_the_fx_rate_never_blocks_the_exit_and_keeps_the_result_pending(lane_engine, broker, clock,
                                                                                    notifier):
    frozen = SIGNAL_END + timedelta(minutes=1)  # po wejściu kurs EURUSD przestaje się zmieniać (nieważny po 60 s)
    broker.set_quote_fn(lambda now: Quote("EURUSD", D("1.08"), D("1.0802"), min(now, frozen)), epic="EURUSD")
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert j.current_position(US).state is P.FLAT  # wyjście z limitu czasu poszło bez kursu
    (deal, _), = j.pnl_pending_deals()  # wynik w USD czeka na kurs: nic nie zgadnięto
    assert j.trade_fx(deal) is None
    assert any("PNL_PENDING" in m and "no valid EURUSD rate" in m for _, m in notifier.alerts)
