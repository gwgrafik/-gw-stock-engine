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
from gwstock.journal import JournalReadError, PositionRecord, TradeRecord
from gwstock.risk import KILL, LOSS_PER_TRADE
from gwstock.types import BrokerRejected, KillLevel, OpenPosition, PositionState, Side
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
    daily_candles,
    data_dir,
    day_rules,
    engine_events,
    five_minute_candles,
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


async def test_a_pending_marker_without_a_trade_of_any_lane_is_reported_not_skipped(
    lane_engine, broker, clock, notifier
):
    """F2, pokrewne. Oczekiwane: znacznik `pnl_pending` deala, którego transakcja nie należy do księgi żadnego pasma w
    walucie obcej (tu: US100, instrument bez pasma), nie jest pomijany po cichu: alarm PNL_PENDING_ORPHAN z numerem
    deala i JEDNO zdarzenie silnika na deal w procesie (mimo wielu przebiegów uzgadniania); znacznik zostaje (nic nie
    zgadnięte), wejścia blokuje bramka portfela (PORTFOLIO_PNL_PENDING)."""
    pending_trade(lane_engine.journal, "GHOST-1", "US100")
    stop_engine_at(lane_engine, clock, berlin(DAY, 9, 12))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert any("GHOST-1" in m for m in kill_alerts(notifier, "PNL_PENDING_ORPHAN"))
    (event,) = engine_events(j, "PNL_PENDING_ORPHAN")
    assert event["deal_id"] == "GHOST-1" and event["exit_day"] == PENDING_DAY.isoformat()
    assert j.pnl_pending_deals() == [("GHOST-1", PENDING_DAY)]
    assert j.count_entry_orders(DAY, EPIC) == 0


# --- F3: spłaszczenie DE40 nie czeka na pasma --------------------------------------------------------------------------

DE_FLATTEN = berlin(DAY, 17, 25)  # 16:25 UTC = 11:25 NY


@pytest.mark.parametrize("us_market", [UsMarket(signals=[new_york(DAY, 11, 15)])], ids=["us500-signal-11:15-ny"])
async def test_a_slow_us500_target_repair_does_not_delay_the_de40_flatten_at_1725(lane_engine, broker, clock):
    """F3. Oczekiwane: pozycja US500 (sygnał 11:15 NY, wejście 16:20 UTC) traci cel u brokera o 16:23 UTC; od 16:25
    UTC każde `set_protection` trwa 10 s czasu symulacji i kończy się przekroczeniem czasu (wcześniej szybka odmowa).
    W przebiegu zarządzania z godziną spłaszczenia DE40 (17:25 Berlin) wyjście DE40 idzie zaraz po uzgadnianiu DE40,
    PRZED uzgadnianiem pasm: `close_position` DE40 w ciągu kilku sekund po 16:25 UTC i przed pierwszym wolnym
    `set_protection`, a nie dopiero po nim (kolejność v1 dla DE40: uzgadnianie, potem wyjście)."""
    stripped: list[bool] = []

    def strip_target() -> None:
        if stripped or clock.now_utc() < datetime(2026, 11, 2, 16, 23, tzinfo=UTC):
            return
        stripped.append(True)
        deal = next(d for d, p in broker.positions.items() if p.epic == US)
        p = broker.positions[deal]
        broker.positions[deal] = OpenPosition(p.deal_id, p.epic, p.side, p.size, p.open_level, p.stop_level,
                                              p.opened_at)  # fmt: skip

    clock.hooks.append(strip_target)

    async def slow_protection(deal_id, stop_level, target_level):
        if clock.now_utc() >= DE_FLATTEN:
            clock.advance(10)
            raise TimeoutError("set_protection timed out")
        raise BrokerRejected("error.service.busy")

    broker.set_protection = slow_protection  # type: ignore[method-assign]
    calls = broker_calls(broker, clock, ("close_position", "set_protection"))
    stop_engine_at(lane_engine, clock, DE_FLATTEN + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert stripped
    (de_deal,) = [d for d, t in j.trades_by_deal(EPIC)]
    de_close = [i for i, (_, m, arg) in enumerate(calls) if m == "close_position" and arg == de_deal]
    slow = [i for i, (ts, m, _) in enumerate(calls) if m == "set_protection" and ts >= DE_FLATTEN]
    assert de_close and slow and de_close[0] < slow[0]
    assert timedelta(0) <= calls[de_close[0]][0] - DE_FLATTEN < timedelta(seconds=6)
    assert j.current_position(EPIC).state is P.FLAT


# --- F4: świeca sygnału, która przepadła, zostawia decyzję STALE_SIGNAL ----------------------------------------------

SIGNAL_START = new_york(DAY, 9, 45)  # 14:45 UTC
SIGNAL_END = SIGNAL_START + timedelta(minutes=5)


def stale_records(journal) -> list[dict]:
    return [d for d in lane_decisions(journal) if d["outcome"] == "STALE_SIGNAL"]


async def test_a_signal_candle_that_kept_waiting_past_the_next_candle_is_recorded_stale(lane_engine, broker, clock):
    """F4. Oczekiwane: broker nie zwraca świecy sygnału 14:45 UTC aż do 14:56 (decyzja czeka z powodem
    SIGNAL_CANDLE_MISSING, trzymanym dotąd tylko w pamięci); o 14:55 (koniec następnej świecy) świeca przestaje być
    ważna: JEDNA decyzja LANE_DECISION z wynikiem STALE_SIGNAL, ostatnim powodem czekania i startem świecy w dzienniku
    decyzji; żadnego wejścia US500 z tej świecy (zero zleceń US500)."""
    real = broker.candles_5m

    async def lagging(epic, start, end):
        candles = await real(epic, start, end)
        if epic == US and clock.now_utc() < SIGNAL_END + timedelta(minutes=6):
            candles = [c for c in candles if c.start != SIGNAL_START]
        return candles

    broker.candles_5m = lagging  # type: ignore[method-assign]
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=8))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    (stale,) = stale_records(j)
    assert stale["signal_candle_start_utc"] == SIGNAL_START.isoformat()
    assert stale["why"] == "SIGNAL_CANDLE_MISSING" and stale["mode"] == "live" and stale["day"] == DAY.isoformat()
    assert SIGNAL_END + timedelta(minutes=5) <= datetime.fromisoformat(stale["decided_at"])
    assert datetime.fromisoformat(stale["decided_at"]) < SIGNAL_END + timedelta(minutes=5, seconds=10)
    assert not [d for d in lane_decisions(j) if d["signal_candle_start_utc"] == SIGNAL_START.isoformat()
                and d["outcome"] != "STALE_SIGNAL"]  # fmt: skip
    assert j.count_entry_orders(DAY, US) == 0


async def test_a_signal_candle_whose_tick_came_after_the_next_candle_is_recorded_stale(lane_engine, broker, clock):
    """F4. Oczekiwane: pętla silnika „zasypia” od 14:49 do 14:56:30 UTC (np. zawieszony odczyt), więc pierwszy takt po
    końcu świecy sygnału 14:45 przychodzi po końcu następnej świecy (14:55): decyzja STALE_SIGNAL bez powodu czekania
    (`why` = None) zamiast ciszy; następna świeca (14:50, zakończona 14:55) jest rozstrzygana normalnie; świece sprzed
    pierwszego taktu decyzji tego procesu NIE dostają wpisu (jedyny STALE to 14:45)."""
    jumped: list[bool] = []

    def stall() -> None:
        if not jumped and clock.now_utc() >= SIGNAL_END - timedelta(minutes=1):
            jumped.append(True)
            clock.advance((SIGNAL_END + timedelta(minutes=6, seconds=30) - clock.now_utc()).total_seconds())

    clock.hooks.append(stall)
    stop_engine_at(lane_engine, clock, SIGNAL_END + timedelta(minutes=9))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    (stale,) = stale_records(j)
    assert stale["signal_candle_start_utc"] == SIGNAL_START.isoformat() and stale["why"] is None
    nxt = (SIGNAL_START + timedelta(minutes=5)).isoformat()
    assert [d["outcome"] for d in lane_decisions(j) if d["signal_candle_start_utc"] == nxt] == ["NO_SIGNAL"]


# --- F5: pasmo dry_run nie zatrzymuje wejść live DE40 przez wspólny kill switch --------------------------------------

TUESDAY = date(2026, 11, 3)


def watchdog_events(journal, code: str) -> list[dict]:
    return [p for p in payloads(journal, "watchdog") if p.get("event") == code]


def freeze_us500(us_market: UsMarket, broker: FakeBroker, when: datetime) -> None:
    us_market.frozen_from = when  # notowanie US500 przestaje się zmieniać (cisza notowań pasma)


@pytest.fixture
def dry_lane_engine(make_engine, lanes) -> Engine:
    """Proces live (uzbrojony przez fabrykę), pasmo US500 bez warunku uzbrojenia pasma: dry_run, tylko mierzy."""
    engine = make_engine(Mode.LIVE, lanes=lanes)
    assert engine.executor.live and not engine.lanes[0].executor.live
    return engine


async def test_quote_silence_of_a_dry_run_lane_alarms_and_de40_still_enters_live(
    dry_lane_engine, make_engine, lanes, broker, clock, us_market, notifier
):
    """F5 (rozstrzygnięcie kontrolera). Oczekiwane: pasmo US500 w dry_run ma ciszę notowań w swojej sesji (notowanie
    zamrożone od 14:40 UTC) → alarm QUOTE_SILENCE z nazwą US500 i zdarzenie nadzoru (`kill` = False), ale kill switch
    procesu zostaje NONE; następnego dnia (wtorek) DE40 live wchodzi o 09:05 (`open_position` DE40), czego kill SOFT
    z poniedziałku by zabronił."""
    freeze_us500(us_market, broker, datetime(2026, 11, 2, 14, 40, tzinfo=UTC))
    stop_engine_at(dry_lane_engine, clock, datetime(2026, 11, 2, 15, 10, tzinfo=UTC))
    await dry_lane_engine.run_day(DAY)
    j = dry_lane_engine.journal
    assert dry_lane_engine.kill.level() is KillLevel.NONE and j.kill_state().level is KillLevel.NONE
    assert any(f"QUOTE_SILENCE ({US})" in m for _, m in notifier.alerts)
    events = watchdog_events(j, "QUOTE_SILENCE")
    assert events and all(e["epic"] == US and e["kill"] is False for e in events)
    clock.hooks.clear()
    broker.set_daily_candles(daily_candles(TUESDAY), epic=EPIC)
    broker.set_candles_5m(five_minute_candles(TUESDAY, D("25780"), D("25790")), epic=EPIC)
    clock.advance((berlin(TUESDAY, 8, 45) - clock.now_utc()).total_seconds())
    engine = make_engine.restart(dry_lane_engine, Mode.LIVE, lanes=lanes)  # nowy proces: kill switch z dziennika
    stop_engine_at(engine, clock, berlin(TUESDAY, 9, 8))
    opens = broker_calls(broker, clock, ("open_position",))
    await engine.run_day(TUESDAY)
    assert [arg for _, _, arg in opens] == [EPIC] and engine.journal.count_entry_orders(TUESDAY, EPIC) == 1


async def test_reconcile_failing_of_a_dry_run_lane_alarms_without_the_kill_switch(dry_lane_engine, clock, notifier):
    """F5. Oczekiwane: każde uzgadnianie pasma US500 (dry_run, bez pozycji) rzuca od 14:00 UTC → po trzech przebiegach
    alarm RECONCILE_FAILING z nazwą US500 i zdarzenie nadzoru, bez kill switcha procesu."""
    lane = dry_lane_engine.lanes[0]

    async def broken() -> None:
        raise RuntimeError("lane reconciliation bug")

    lane.reconciler.run_once = broken
    stop_engine_at(dry_lane_engine, clock, datetime(2026, 11, 2, 8, 20, tzinfo=UTC))
    await dry_lane_engine.run_day(DAY)
    assert dry_lane_engine.kill.level() is KillLevel.NONE
    assert any(f"RECONCILE_FAILING ({US})" in m for _, m in notifier.alerts)
    assert watchdog_events(dry_lane_engine.journal, "RECONCILE_FAILING")


async def test_quote_silence_of_a_live_lane_still_soft_kills(lane_engine, broker, clock, us_market, notifier):
    """F5: pasmo live bez zmian. Oczekiwane: cisza notowań US500 (pasmo uzbrojone) → kill SOFT QUOTE_SILENCE."""
    freeze_us500(us_market, broker, datetime(2026, 11, 2, 14, 0, tzinfo=UTC))
    stop_engine_at(lane_engine, clock, datetime(2026, 11, 2, 14, 40, tzinfo=UTC))
    await lane_engine.run_day(DAY)
    assert lane_engine.kill.level() is KillLevel.SOFT and lane_engine.journal.kill_state().reason == "QUOTE_SILENCE"


async def test_a_dry_run_lane_with_a_real_position_is_not_measure_only(dry_lane_engine):
    """F5, strona ostrożna. Oczekiwane: pasmo dry_run z pozycją inną niż FLAT w dzienniku (np. po rozbrojeniu pasma
    z otwartą pozycją) nie jest „tylko mierzące”: jego cisza i awarie uzgadniania dalej włączają kill SOFT."""
    assert dry_lane_engine._lane_measure_only(US) is True
    assert dry_lane_engine._lane_measure_only(EPIC) is False
    dry_lane_engine.journal.upsert_position(PositionRecord(US, P.OPEN, "DEAL-X", Side.BUY, D("0.01"), D("7590"),
                                                           D("7580"), datetime(2026, 11, 2, 7, 0, tzinfo=UTC)))
    assert dry_lane_engine._lane_measure_only(US) is False


async def test_journal_unreadable_in_a_dry_run_lane_still_soft_kills(dry_lane_engine, clock, monkeypatch):
    """F5: wspólny dziennik. Oczekiwane: błąd odczytu dziennika w kroku pasma dry_run → kill SOFT JOURNAL_UNREADABLE
    jak dotąd."""

    async def unreadable(*_a, **_k):
        raise JournalReadError("database disk image is malformed")

    monkeypatch.setattr(dry_lane_engine, "_bar_quote_step", unreadable)
    stop_engine_at(dry_lane_engine, clock, datetime(2026, 11, 2, 8, 0, tzinfo=UTC))
    await dry_lane_engine.run_day(DAY)
    assert dry_lane_engine.kill.level() is KillLevel.SOFT
    assert dry_lane_engine.journal.kill_state().reason == "JOURNAL_UNREADABLE"


# --- F6: run_forever z pasmami (22:00 → północ, pozostałość po 22:00, start nocą) -----------------------------------

TRADING_CALLS = ("login", "quote", "list_positions", "closed_since", "candles_5m", "open_position", "close_position")


def call_times(broker: FakeBroker, clock: SimClock) -> list[tuple[datetime, str]]:
    seen: list[tuple[datetime, str]] = []
    for name in TRADING_CALLS:
        broker.on_call(name, lambda _n=name: seen.append((clock.now_utc(), _n)))
    return seen


def tuesday_de40_data(broker: FakeBroker) -> None:
    broker.set_daily_candles(daily_candles(TUESDAY), epic=EPIC)
    broker.set_candles_5m(five_minute_candles(TUESDAY, D("25780"), D("25790")), epic=EPIC)


def trades_by_epic(journal) -> dict[str, list]:
    out: dict[str, list] = {}
    for _deal, t in journal.trades_by_deal():
        out.setdefault(t.instrument, []).append(t)
    return out


async def test_run_forever_with_lanes_crosses_2200_and_midnight_into_the_next_session(
    lane_engine, broker, clock, journal_path
):
    """F6. Oczekiwane: `run_forever` od poniedziałku 08:45 Berlin: oba pasma handlują w poniedziałek (DE40 09:05-17:25,
    US500 14:50-15:40 UTC), dzień portfela kończy się z zamknięciem NYSE (22:00 Berlin), potem spoczynek BEZ wywołań
    brokera do północy i dalej do przygotowania wtorku (08:50 Berlin), we wtorek pasmo US500 pracuje na dniu wtorku, a
    DE40 wchodzi o 09:05 wtorku; poniedziałkowe wyniki rozliczone (USD w EUR), księga = liczniki."""
    swapped: list[bool] = []

    def tuesday_data() -> None:
        if not swapped and clock.now_utc() >= datetime(2026, 11, 2, 22, 0, tzinfo=UTC):
            swapped.append(True)
            tuesday_de40_data(broker)

    clock.hooks.append(tuesday_data)
    seen = call_times(broker, clock)
    stop_engine_at(lane_engine, clock, berlin(TUESDAY, 9, 8))
    await lane_engine.run_forever()
    j = lane_engine.journal
    by_epic = trades_by_epic(j)
    assert len(by_epic[US]) == 1 and len(by_epic[EPIC]) == 1  # poniedziałek; wtorkowa pozycja DE40 jeszcze otwarta
    assert j.count_entry_orders(TUESDAY, EPIC) == 1 and j.current_position(EPIC).state is P.OPEN
    assert lane_engine.lanes[0].day is not None and lane_engine.lanes[0].day.day == TUESDAY
    night = [(ts, m) for ts, m in seen if new_york(DAY, 16, 0) + timedelta(minutes=1) < ts < berlin(TUESDAY, 8, 50)]
    assert night == [] and any(ts >= berlin(TUESDAY, 8, 50) for ts, _ in seen)
    assert j.pnl_pending_deals() == [] and lane_engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


async def test_run_forever_started_after_2200_flattens_both_leftovers_at_once(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """F6 (pozostałość US500 po 22:00). Oczekiwane: proces padł o 14:52 UTC z pozycjami DE40 i US500; nowy proces
    startuje o 22:30 Berlin (po końcu dnia portfela) przez `run_forever`: obie pozostałości spłaszczone `FORCED` w
    ciągu minuty od startu (pasma nie trzymają nocy), US500 rozliczone w EUR z kursu brokera (ESTIMATED_FX), żadnego
    nowego wejścia w poniedziałek; potem spoczynek do wtorku 08:50 bez wywołań brokera."""
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert j.current_position(EPIC).state is P.OPEN and j.current_position(US).state is P.OPEN
    clock.hooks.clear()
    start = berlin(DAY, 22, 30)
    clock.advance((start - clock.now_utc()).total_seconds())
    engine = make_engine.restart(lane_engine, Mode.LIVE, lanes=lanes, lane_arming=lane_armed)
    seen = call_times(broker, clock)
    stop_engine_at(engine, clock, berlin(TUESDAY, 8, 45))
    await engine.run_forever()
    j = engine.journal
    by_epic = trades_by_epic(j)
    for epic in (EPIC, US):
        (t,) = by_epic[epic]
        assert t.exit_reason == "FORCED" and timedelta(0) <= t.exit_ts - start < timedelta(minutes=1)
    (us_deal,) = [d for d, _ in j.trades_by_deal(US)]
    assert j.pnl_pending_deals() == [] and j.trade_fx(us_deal).source == "ESTIMATED_FX"
    assert j.count_entry_orders(DAY, EPIC) == 1 and j.count_entry_orders(DAY, US) == 1
    late = [(ts, m) for ts, m in seen if start + timedelta(minutes=2) < ts]
    assert late == [] and any(m == "close_position" for _, m in seen)
    assert_portfolio_books_balance(journal_path)


@pytest.mark.parametrize(
    "start", [berlin(DAY, 23, 30), berlin(TUESDAY, 3, 0), berlin(TUESDAY, 8, 54)], ids=["23:30", "03:00", "08:54"]
)
async def test_run_forever_started_at_night_waits_for_the_next_session_and_trades_it(
    make_engine, lanes, broker, clock, start
):
    """F6 (start procesu po 22:00 i przed 08:55). Oczekiwane: bez pozycji; proces startuje nocą (23:30 Berlin w
    poniedziałek, 03:00 albo 08:54 we wtorek): co najwyżej uzgadnianie startowe zaraz po starcie (poniedziałek 23:30:
    jeden takt dnia, który już się skończył), potem spoczynek bez wywołań brokera do przygotowania wtorku (08:50) i
    normalny wtorek: DE40 wchodzi o 09:05 wtorku, pasmo US500 na dniu wtorku, żadnego wejścia w poniedziałek."""
    clock.advance((start - clock.now_utc()).total_seconds())
    tuesday_de40_data(broker)
    engine = make_engine(Mode.LIVE, lanes=lanes, lane_arming=lane_armed)
    seen = call_times(broker, clock)
    opens = broker_calls(broker, clock, ("open_position",))
    stop_engine_at(engine, clock, berlin(TUESDAY, 9, 8))
    await engine.run_forever()
    j = engine.journal
    assert [arg for _, _, arg in opens] == [EPIC]
    assert timedelta(0) <= opens[0][0] - berlin(TUESDAY, 9, 5) < timedelta(seconds=10)
    assert j.count_entry_orders(DAY) == 0 and j.count_entry_orders(TUESDAY, EPIC) == 1
    assert engine.lanes[0].day is not None and engine.lanes[0].day.day == TUESDAY
    idle = [(ts, m) for ts, m in seen if start + timedelta(minutes=1) < ts < berlin(TUESDAY, 8, 50)]
    assert idle == [] and any(ts >= berlin(TUESDAY, 8, 50) for ts, _ in seen)

