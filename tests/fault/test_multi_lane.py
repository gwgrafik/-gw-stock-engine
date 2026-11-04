"""Testy odporności silnika z wieloma pasmami (zadanie 14, projekt wielu pasm §9): DE40 v1 i pasmo B1/M5 na US500.

Każdy scenariusz ma w docstringu OCZEKIWANE zachowanie spisane przed uruchomieniem z projektu
(`docs/superpowers/specs/2026-11-04-multi-lane-design.md`, sekcje 1, 3, 4, 5, 6, 9, Załącznik A) i z `CLAUDE.md`
(wyjść nigdy nie blokuje bramka ryzyka; kod niczego nie uzbraja). Niezmienniki sprawdzane w każdym scenariuszu, gdzie
mają sens: księga portfela zgadza się z licznikami (`assert_portfolio_books_balance`), brak wejścia przy kill switchu,
żaden FLAT bez księgowania, żadna druga wysyłka zamknięcia bez odczytu listy pozycji.

Dzień wzorcowy: poniedziałek 2026-11-02 (Berlin UTC+1, Nowy Jork UTC-5). DE40: decyzja 09:05, spłaszczenie 17:25 Berlin
(16:25 UTC). US500: świeca sygnału B1 09:45-09:50 NY (14:45-14:50 UTC), wejście 14:50 UTC, limit czasu 50 min (15:40
UTC), spłaszczenie pasma 15:55 NY (20:55 UTC), zamknięcie NYSE 16:00 NY (21:00 UTC = 22:00 Berlin). Obie pozycje są
otwarte jednocześnie między 14:50 a 15:40 UTC. Uzbrajanie wyłącznie przez `engine_sim` (fabryka, katalog tymczasowy).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.app import Engine, Mode
from gwstock.broker.fake import FakeBroker
from gwstock.config import load_lanes
from gwstock.journal import JournalReadError
from gwstock.portfolio import PORTFOLIO_PNL_PENDING, PORTFOLIO_UNMANAGED_EXPOSURE
from gwstock.reconcile import CLOSURE_NOT_IN_HISTORY
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    KillLevel,
    OpenPosition,
    PositionState,
    Quote,
    Side,
    TargetedEntryRequest,
)
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    EPIC,
    FX,
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
    disarm,
    five_minute_candles,
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
US_TIME_LIMIT = US_ENTRY + timedelta(minutes=50)  # 15:40 UTC
DE_FLATTEN = berlin(DAY, 17, 25)  # 16:25 UTC
NY_CLOSE = new_york(DAY, 16, 0)  # 21:00 UTC


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


def restart(make_engine, old: Engine, lanes, clock: SimClock, **kw) -> Engine:
    """Nowy proces na tym samym dzienniku i brokerze (nic z pamięci poprzedniej instancji)."""
    clock.hooks.clear()
    return make_engine.restart(old, Mode.LIVE, lanes=lanes, lane_arming=kw.pop("lane_arming", lane_armed), **kw)


def deal_of(broker: FakeBroker, epic: str) -> str:
    (deal,) = [d for d, p in broker.positions.items() if p.epic == epic]
    return deal


def trades_by_epic(journal) -> dict[str, list]:
    out: dict[str, list] = {}
    for _deal, t in journal.trades_by_deal():
        out.setdefault(t.instrument, []).append(t)
    return out


def close_calls(broker: FakeBroker) -> list[tuple[datetime, str]]:
    """Każde `close_position` (czas, deal) zapisane przed skutkiem u brokera."""
    seen: list[tuple[datetime, str]] = []
    real = broker.close_position

    async def spy(deal_id: str) -> None:
        seen.append((broker._clock.now_utc(), deal_id))
        await real(deal_id)

    broker.close_position = spy  # type: ignore[method-assign]
    return seen


def open_calls(broker: FakeBroker) -> list[tuple[datetime, str]]:
    seen: list[tuple[datetime, str]] = []
    real = broker.open_position

    async def spy(req):
        seen.append((broker._clock.now_utc(), req.epic))
        return await real(req)

    broker.open_position = spy  # type: ignore[method-assign]
    return seen


def at(clock: SimClock, when: datetime, action) -> list[bool]:
    """Akcja raz, po pierwszym śnie silnika, który dociera do `when` (zdarzenie u brokera w trakcie pracy)."""
    done: list[bool] = []

    def hook() -> None:
        if not done and clock.now_utc() >= when:
            done.append(True)
            action()

    clock.hooks.append(hook)
    return done


def reconcile_events(journal) -> list[dict]:
    return payloads(journal, "reconcile.event")


def gate_blocks(journal) -> list[dict]:
    return [json.loads(e.payload) for e in journal.events("gate_block")]


def pending_blocked(block: dict) -> bool:
    """Wejście zatrzymane rozliczeniem w toku: powód bramki pasma (`PNL_PENDING`) albo portfela."""
    reasons = [*block.get("reasons", []), *block.get("portfolio_reasons", [])]
    return "PNL_PENDING" in reasons or PORTFOLIO_PNL_PENDING in reasons


# --- 1, 2: restart w trakcie dwóch pozycji ------------------------------------------------------------------------


async def test_restart_with_two_open_positions_books_target_and_stop_per_instrument(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Scenariusz 9.2.1. Oczekiwane (projekt §3.3, §9.2.1, A1): DE40 OPEN ze stopem i US500 OPEN ze stopem i celem;
    proces zatrzymany; w przerwie US500 zamknięty celem, DE40 wybity stopem; po restarcie oba zaksięgowane z przyczyną
    z historii brokera (US500 `TARGET`, DE40 `STOP`), oba FLAT, brak BLOCKED i brak kill switcha, znacznik zamknięć
    per instrument (`closed_marker` dla DE40, `closed_marker:US500` dla US500), licznik dnia portfela = wynik DE40 +
    rozliczenie US500 w EUR; żadnego `close_position` (to broker zamknął)."""
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert j.current_position(EPIC).state is P.OPEN and j.current_position(US).state is P.OPEN
    us_deal, de_deal = deal_of(broker, US), deal_of(broker, EPIC)
    assert broker.positions[us_deal].target_level is not None
    clock.advance(60)
    broker.trigger_target(us_deal)
    clock.advance(60)
    de_stop = broker.positions[de_deal].stop_level
    broker.trigger_stop(de_deal, de_stop)
    clock.advance(60)
    closes = close_calls(broker)
    engine = restart(make_engine, lane_engine, lanes, clock)
    stop_engine_at(engine, clock, clock.now_utc() + timedelta(seconds=40))
    await engine.run_day(DAY)
    j = engine.journal
    by_epic = trades_by_epic(j)
    assert [t.exit_reason for t in by_epic[US]] == ["TARGET"]
    assert [t.exit_reason for t in by_epic[EPIC]] == ["STOP"]
    assert j.current_position(EPIC).state is P.FLAT and j.current_position(US).state is P.FLAT
    assert engine.kill.level() is KillLevel.NONE
    assert j.get_meta("closed_marker") is not None and j.get_meta(f"closed_marker:{US}") is not None
    assert closes == []
    assert j.pnl_pending_deals() == []
    assert_portfolio_books_balance(journal_path)


async def test_restart_books_de40_closed_later_after_us500_closed_earlier_with_lagging_de40_history(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Scenariusz 9.2.2 (regresja A1: globalny `closed_marker`). Oczekiwane: US500 zamknięty celem WCZEŚNIEJ, DE40
    stopem PÓŹNIEJ, a historia DE40 spóźnia się o 30 s, więc US500 księguje się pierwszy; zaksięgowanie US500 nie
    przesuwa początku historii DE40 (znacznik per instrument), DE40 księguje się, gdy jego historia dogoni, oba FLAT,
    bez MISMATCH_TIMEOUT (opóźnienie < `mismatch_tolerance_s` 60 s) i bez kill switcha."""
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    us_deal, de_deal = deal_of(broker, US), deal_of(broker, EPIC)
    broker.trigger_target(us_deal)
    clock.advance(120)
    broker.trigger_stop(de_deal, broker.positions[de_deal].stop_level)
    real = broker.closed_since

    async def de40_lagging(since: datetime):
        now = clock.now_utc()
        return [c for c in await real(since) if c.epic != EPIC or c.ts + timedelta(seconds=30) <= now]

    broker.closed_since = de40_lagging  # type: ignore[method-assign]
    engine = restart(make_engine, lane_engine, lanes, clock)
    stop_engine_at(engine, clock, clock.now_utc() + timedelta(seconds=90))
    await engine.run_day(DAY)
    j = engine.journal
    by_epic = trades_by_epic(j)
    assert [t.exit_reason for t in by_epic.get(US, [])] == ["TARGET"]
    assert [t.exit_reason for t in by_epic.get(EPIC, [])] == ["STOP"]
    assert j.current_position(EPIC).state is P.FLAT and j.current_position(US).state is P.FLAT
    assert engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


async def test_restart_with_two_open_positions_then_the_engine_closes_both_on_their_own_schedules(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Restart w trakcie dwóch pozycji, wariant bez zamknięcia u brokera. Oczekiwane (projekt §3.6, §9.2.1): po
    restarcie o 14:55 UTC obie pozycje przyjęte z dziennika (OPEN), bez nowego wejścia; US500 zamknięta przez silnik z
    limitu czasu liczonego od otwarcia u brokera (15:40 UTC, nie od restartu), DE40 o 17:25; obie `FORCED`, każda
    zamknięta jednym `close_position`, licznik wejść dnia każdego instrumentu = 1."""
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    clock.advance(120)
    closes = close_calls(broker)
    opens = open_calls(broker)
    engine = restart(make_engine, lane_engine, lanes, clock)
    report = await engine.run_day(DAY)
    j = engine.journal
    assert opens == []
    by_epic = trades_by_epic(j)
    (us,), (de,) = by_epic[US], by_epic[EPIC]
    assert us.exit_reason == "FORCED" and timedelta(0) <= us.exit_ts - US_TIME_LIMIT < timedelta(seconds=10)
    assert de.exit_reason == "FORCED" and timedelta(0) <= de.exit_ts - DE_FLATTEN < timedelta(seconds=10)
    assert len(closes) == 2 and len({d for _, d in closes}) == 2
    assert j.count_entry_orders(DAY, US) == 1 and j.count_entry_orders(DAY, EPIC) == 1
    assert report.final_position_state is P.FLAT and engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


async def test_a_lane_left_unarmed_after_a_restart_still_manages_its_open_position(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Projekt §6.3: pasmo bez odcisku jest dry_run, ale jego pozycje (np. z wcześniejszego uzbrojenia) są zarządzane;
    wyjścia nigdy nie zależą od trybu. Oczekiwane: po restarcie z domyślnym warunkiem uzbrojenia (pasmo US500 dry_run)
    pozycja US500 zamknięta z limitu czasu o 15:40 UTC i zaksięgowana, zero nowych wejść US500, DE40 live."""
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    opens = open_calls(broker)
    clock.hooks.clear()
    engine = make_engine.restart(lane_engine, Mode.LIVE, lanes=lanes)  # bez warunku uzbrojenia pasma
    assert not engine.lanes[0].executor.live and engine.executor.live
    stop_engine_at(engine, clock, US_TIME_LIMIT + timedelta(minutes=2))
    await engine.run_day(DAY)
    j = engine.journal
    (us,) = trades_by_epic(j)[US]
    assert us.exit_reason == "FORCED" and timedelta(0) <= us.exit_ts - US_TIME_LIMIT < timedelta(seconds=10)
    assert j.current_position(US).state is P.FLAT and opens == []
    assert_portfolio_books_balance(journal_path)


async def test_restart_inside_the_signal_candle_window_after_a_rejected_entry_does_not_send_it_again(
    lane_engine, make_engine, lanes, broker, clock
):
    """Odrzucone wejście i restart w oknie tej samej świecy sygnału. Oczekiwane (spec S3 w `decisions-log.md`:
    odrzucenie zużywa wejście; projekt §3.4: „REJECTED, wejście zużyte”; badania: jedna decyzja na świecę sygnału):
    broker odrzuca wejście US500 z sygnału 09:45 NY; proces pada i wraca o 14:52 UTC, gdy ta świeca jest jeszcze
    „do decyzji” (do 14:55); po restarcie ta sama świeca NIE daje drugiego zlecenia (jedno `open_position` US500)."""
    at(clock, US_ENTRY - timedelta(minutes=1), lambda: broker.reject_next_entry("error.invalid.stoploss.minvalue"))
    opens = open_calls(broker)
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(seconds=30))
    await lane_engine.run_day(DAY)
    assert [e for _, e in opens].count(US) == 1
    assert lane_engine.journal.current_position(US).state is P.FLAT  # odrzucone: brak pozycji
    clock.advance(60)
    engine = restart(make_engine, lane_engine, lanes, clock)
    stop_engine_at(engine, clock, US_ENTRY + timedelta(minutes=6))
    await engine.run_day(DAY)
    assert [e for _, e in opens].count(US) == 1, opens
    assert engine.journal.count_entry_orders(DAY, US) == 1


async def test_an_uncertain_us500_entry_is_adopted_with_stop_and_target_and_exits_on_time(
    lane_engine, broker, clock, journal_path
):
    """Wejście US500 w stanie niepewnym (UNKNOWN: odpowiedź `open_position` zgubiona, zlecenie przyjęte). Oczekiwane
    (spec v1 §7, projekt §3.4): brak drugiej wysyłki; pozycja rozstrzygnięta listą brokera i przyjęta (OPEN) ze stopem
    i celem z wejścia; wyjście z limitu czasu liczonego od otwarcia u brokera; zaksięgowana; DE40 nietknięty."""
    real_open = broker.open_position
    first: list[bool] = []

    async def lost_reply(req):
        if req.epic == US and not first:
            first.append(True)
            await real_open(req)  # broker przyjął i wypełnił
            raise BrokerTimeout("reply lost")
        return await real_open(req)

    broker.open_position = lost_reply  # type: ignore[method-assign]
    opens = open_calls(broker)
    stop_engine_at(lane_engine, clock, US_TIME_LIMIT + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert [e for _, e in opens].count(US) == 1
    (us,) = trades_by_epic(j)[US]
    assert us.exit_reason == "FORCED" and us.exit_ts - US_TIME_LIMIT < timedelta(seconds=30)
    assert j.current_position(US).state is P.FLAT and j.current_position(EPIC).state is P.OPEN
    assert_portfolio_books_balance(journal_path)


# --- 3, 12: rozbieżność brokera dla jednej z dwóch pozycji ------------------------------------------------------------


async def test_size_mismatch_on_us500_blocks_it_and_soft_kills_while_de40_is_managed_and_flattened(
    lane_engine, broker, clock, journal_path
):
    """Scenariusz 9.2.3 (i 9.2.12: ręczna transakcja właściciela agregująca się z naszą pozycją US500). Oczekiwane
    (projekt §5.2, §5.3): US500 z inną wielkością u brokera → US500 BLOCKED (`SIZE_MISMATCH`) i kill SOFT; pozycja
    US500 nie jest ruszana przez silnik (BLOCKED nie spłaszcza się z limitu czasu; zostaje stop u brokera, sprawa
    właściciela); DE40 OPEN dalej zarządzany: spłaszczony o 17:25 i zaksięgowany; następnego dnia nowe wejście DE40
    zablokowane wspólnym kill switchem (bramka `KILL`) do `clear-kill`."""

    def owner_adds() -> None:
        deal = deal_of(broker, US)
        broker.positions[deal] = replace(broker.positions[deal], size=broker.positions[deal].size * 2)

    at(clock, datetime(2026, 11, 2, 15, 0, tzinfo=UTC), owner_adds)
    closes = close_calls(broker)
    report = await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.kill.level() is KillLevel.SOFT
    assert j.current_position(US).state is P.BLOCKED
    assert any(e.get("event") == "SIZE_MISMATCH" for e in reconcile_events(j))
    us_deal = deal_of(broker, US)
    assert all(d != us_deal for _, d in closes)  # pozycja BLOCKED nigdy nie zamykana przez silnik bez HARD
    assert report.final_position_state is P.FLAT
    de = trades_by_epic(j)[EPIC]
    assert len(de) == 1 and de[0].exit_reason == "FORCED" and de[0].exit_ts - DE_FLATTEN < timedelta(seconds=10)
    assert US not in trades_by_epic(j)
    assert_portfolio_books_balance(journal_path)
    # następny dzień: wejście DE40 zablokowane wspólnym kill switchem
    tuesday = date(2026, 11, 3)
    broker.set_daily_candles(daily_candles(tuesday), epic=EPIC)
    broker.set_candles_5m(five_minute_candles(tuesday, D("25780"), D("25790")), epic=EPIC)
    stop_engine_at(lane_engine, clock, berlin(tuesday, 9, 12))
    await lane_engine.run_day(tuesday)
    assert j.count_entry_orders(tuesday, EPIC) == 0
    assert any("KILL" in b["reasons"] for b in gate_blocks(j))


async def test_owner_hedge_position_on_us500_is_foreign_and_does_not_stop_de40_management(
    lane_engine, broker, clock, journal_path
):
    """Scenariusz 9.2.12 (wariant z osobną pozycją, `hedgingMode`). Oczekiwane (projekt §5.2): druga pozycja na
    instrumencie pasma obok naszej → `FOREIGN_POSITION`, US500 BLOCKED, kill SOFT; pozycja właściciela nigdy nie
    dotknięta; DE40 zarządzany i spłaszczony o 17:25."""

    def owner_opens() -> None:
        broker.positions["OWN-US"] = OpenPosition(
            "OWN-US", US, Side.SELL, D("0.05"), D("7590"), D("7600"), clock.now_utc()
        )

    at(clock, datetime(2026, 11, 2, 15, 0, tzinfo=UTC), owner_opens)
    closes = close_calls(broker)
    stop_engine_at(lane_engine, clock, DE_FLATTEN + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.kill.level() is KillLevel.SOFT
    assert j.current_position(US).state is P.BLOCKED
    assert any(e.get("event") == "FOREIGN_POSITION" for e in reconcile_events(j))
    assert "OWN-US" in broker.positions and all(d != "OWN-US" for _, d in closes)
    assert [t.exit_reason for t in trades_by_epic(j)[EPIC]] == ["FORCED"]
    assert_portfolio_books_balance(journal_path)


# --- 4, 5: zamknięcie przez cel -----------------------------------------------------------------------------------


async def test_target_close_is_booked_as_target_without_any_close_call(lane_engine, broker, clock, journal_path):
    """Zamknięcie celem w trakcie pracy (projekt §3.3). Oczekiwane: pozycja US500 znika u brokera, uzgadnianie
    pasma znajduje zamknięcie w historii i księguje `TARGET` (tylko poślizg wejścia, bez dodatkowego poślizgu wyjścia
    stopem), FLAT, bez kill switcha,
    bez żadnego `close_position` dla tego deala; rozliczenie EUR z kursu brokera (ESTIMATED_FX)."""
    closes = close_calls(broker)
    at(clock, datetime(2026, 11, 2, 15, 0, tzinfo=UTC), lambda: broker.trigger_target(deal_of(broker, US)))
    stop_engine_at(lane_engine, clock, datetime(2026, 11, 2, 15, 2, tzinfo=UTC))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    ((deal, trade),) = j.trades_by_deal(US)
    # sam poślizg wejścia 0,5 pkt x 0,01 (bez dodatkowego 1,0 pkt wyjścia stopem: to dałoby 0,02)
    assert trade.exit_reason == "TARGET" and trade.slippage_penalty == D("0.01")
    assert j.current_position(US).state is P.FLAT and lane_engine.kill.level() is KillLevel.NONE
    assert all(d != deal for _, d in closes)
    assert j.trade_fx(deal) is not None
    assert_portfolio_books_balance(journal_path)


async def test_target_close_with_history_lagging_90s_blocks_then_books_target(lane_engine, broker, clock, journal_path):
    """Scenariusz 9.2.4. Oczekiwane (projekt §3.4): US500 znika z listy, a zamknięcie widać w historii dopiero po 90 s:
    BLOCKED `LOCAL_POSITION_MISSING`, po `mismatch_tolerance_s` (60 s) kill SOFT `MISMATCH_TIMEOUT`, księgowanie po
    pojawieniu się historii z przyczyną `TARGET`, FLAT, bez `CLOSURE_NOT_IN_HISTORY` (silnik nie wysłał zamknięcia);
    DE40 nietknięty (OPEN do 17:25)."""
    real = broker.closed_since

    async def us_lagging(since: datetime):
        now = clock.now_utc()
        return [c for c in await real(since) if c.epic != US or c.ts + timedelta(seconds=90) <= now]

    broker.closed_since = us_lagging  # type: ignore[method-assign]
    hit = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    at(clock, hit, lambda: broker.trigger_target(deal_of(broker, US)))
    stop_engine_at(lane_engine, clock, hit + timedelta(minutes=4))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    codes = [e.get("event") for e in reconcile_events(j)]
    assert "LOCAL_POSITION_MISSING" in codes and "MISMATCH_TIMEOUT" in codes
    assert CLOSURE_NOT_IN_HISTORY not in codes
    assert lane_engine.kill.level() is KillLevel.SOFT
    (trade,) = trades_by_epic(j)[US]
    assert trade.exit_reason == "TARGET"
    assert j.current_position(US).state is P.FLAT
    assert j.current_position(EPIC).state is P.OPEN
    assert_portfolio_books_balance(journal_path)


async def test_time_limit_close_racing_the_target_is_booked_as_target_without_a_second_send(
    lane_engine, broker, clock, journal_path
):
    """Scenariusz 9.2.5. Oczekiwane (projekt §3.3, §3.4, A11): w chwili `DELETE` z limitu czasu (50 min) cel zamyka
    pozycję; broker odpowiada `POSITION_NOT_FOUND`; wykonawca rozstrzyga listą i historią: brak drugiej wysyłki
    zamknięcia, przyczyna `TARGET` (nie `FORCED`), FLAT, bez kill switcha."""
    seen: list[tuple[datetime, str]] = []
    real = broker.close_position

    async def racing(deal_id: str) -> None:
        seen.append((clock.now_utc(), deal_id))
        pos = broker.positions.get(deal_id)
        if pos is not None and pos.epic == US:
            broker.trigger_target(deal_id)  # cel wygrywa wyścig z naszym DELETE
        await real(deal_id)

    broker.close_position = racing  # type: ignore[method-assign]
    stop_engine_at(lane_engine, clock, US_TIME_LIMIT + timedelta(minutes=3))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    ((deal, trade),) = j.trades_by_deal(US)
    assert [d for _, d in seen].count(deal) == 1
    assert seen[0][0] >= US_TIME_LIMIT
    assert trade.exit_reason == "TARGET"
    assert j.current_position(US).state is P.FLAT and lane_engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


# --- 6, 7, 8: kill switch twardy przy dwóch pozycjach -----------------------------------------------------------------

HARD_AT = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # obie pozycje otwarte


@pytest.mark.parametrize(
    "us_market", [UsMarket(signals=[new_york(DAY, 9, 45), new_york(DAY, 14, 0)])], ids=["second-us500-signal"]
)
async def test_hard_kill_with_two_positions_flattens_both_fast_and_stays_until_clear_kill(
    lane_engine, make_engine, lanes, broker, clock, data_dir, journal_path
):
    """Scenariusz 9.2.6. Oczekiwane (projekt §5.4, spec §9): `data/HARD_STOP` przy otwartych DE40 i US500 → obie
    pozycje zamknięte w pierwszym takcie zarządzania (kilka sekund), obie zaksięgowane `FORCED`, HARD zostaje (także
    po restarcie procesu) do `clear-kill`, zero nowych wejść, choć US500 daje drugi sygnał o 14:00 NY."""
    at(clock, HARD_AT, lambda: (data_dir / "HARD_STOP").write_text(""))
    closes = close_calls(broker)
    opens = open_calls(broker)
    stop_engine_at(lane_engine, clock, HARD_AT + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.kill.level() is KillLevel.HARD
    assert {d for _, d in closes} == {t for t, _ in j.trades_by_deal()}  # każdy zamknięty deal zaksięgowany
    assert len(closes) == 2 and all(ts - HARD_AT < timedelta(seconds=15) for ts, _ in closes)
    assert {t.instrument: t.exit_reason for t in j.trades()} == {EPIC: "FORCED", US: "FORCED"}
    (data_dir / "HARD_STOP").unlink()  # plik zdjęty, ale poziom HARD trwa w dzienniku do clear-kill
    engine = restart(make_engine, lane_engine, lanes, clock)
    await engine.run_day(DAY)
    assert engine.kill.level() is KillLevel.HARD
    assert [e for _, e in opens] == [EPIC, US]  # tylko dwa wejścia sprzed HARD
    assert any(d["outcome"] == "ENTER" for d in lane_decisions(engine.journal)[1:])  # drugi sygnał był
    assert engine.journal.count_entry_orders(DAY, US) == 1
    assert_portfolio_books_balance(journal_path)


async def test_hard_kill_with_two_positions_and_an_unreadable_journal_closes_both_own_deals_only(
    lane_engine, broker, clock, data_dir, monkeypatch
):
    """Scenariusz 9.2.7 (regresja A9: `last_own_deal` per instrument). Oczekiwane (projekt §2.2, §5.4): HARD przy
    otwartych DE40 i US500 i nieczytelnym dzienniku → zamknięte oba NASZE deale tego procesu (po jednym na instrument),
    tylko te, które broker listuje; pozycja właściciela na US100 (bez pasma) nietknięta; alarm
    EMERGENCY_FLATTEN_JOURNAL_UNREADABLE."""
    def owner_opens() -> None:  # po obu wejściach (pozycja bez pasma blokuje każde wejście)
        broker.positions["OWN-1"] = OpenPosition("OWN-1", "US100", Side.BUY, D("1"), D("20000"), D("19900"),
                                                 clock.now_utc())  # fmt: skip

    at(clock, HARD_AT - timedelta(minutes=2), owner_opens)
    closes = close_calls(broker)
    j = lane_engine.journal

    def break_journal() -> None:
        def unreadable(_epic):
            raise JournalReadError("disk gone")

        monkeypatch.setattr(j, "current_position", unreadable)
        (data_dir / "HARD_STOP").write_text("")

    at(clock, HARD_AT, break_journal)
    deals: dict[str, str] = {}
    at(clock, HARD_AT - timedelta(seconds=1), lambda: deals.update({EPIC: deal_of(broker, EPIC), US: deal_of(broker, US)}))
    stop_engine_at(lane_engine, clock, HARD_AT + timedelta(seconds=30))
    await lane_engine.run_day(DAY)
    assert {d for _, d in closes} == {deals[EPIC], deals[US]}
    assert len(closes) == 2
    assert "OWN-1" in broker.positions and not any(p.epic in (EPIC, US) for p in broker.positions.values())
    assert lane_engine.kill.level() is KillLevel.HARD


async def test_hard_kill_with_de40_close_failing_gives_us500_its_first_attempt_at_once(
    lane_engine, broker, clock, data_dir, notifier
):
    """Scenariusz 9.2.8 (regresja A15). Oczekiwane (projekt §5.4): zamknięcie DE40 ciągle zawodzi (BrokerError), a
    US500 dostaje pierwszą próbę w tej samej rundzie (próby przeplatane), nie po ok. 30 s procedury DE40; US500
    zaksięgowany `FORCED`; DE40 po wyczerpaniu prób z alarmem EMERGENCY_FLATTEN_INCOMPLETE, stop u brokera zostaje."""
    seen: list[tuple[datetime, str]] = []
    real = broker.close_position

    async def de40_failing(deal_id: str) -> None:
        seen.append((clock.now_utc(), deal_id))
        pos = broker.positions.get(deal_id)
        if pos is not None and pos.epic == EPIC:
            raise BrokerError("gateway timeout")
        await real(deal_id)

    broker.close_position = de40_failing  # type: ignore[method-assign]
    at(clock, HARD_AT, lambda: (data_dir / "HARD_STOP").write_text(""))
    stop_engine_at(lane_engine, clock, HARD_AT + timedelta(seconds=20))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    de_deal = j.current_position(EPIC).deal_id
    first_de = next(ts for ts, d in seen if d == de_deal)
    first_us = next(ts for ts, d in seen if d != de_deal)
    assert first_us - first_de < timedelta(seconds=2)
    assert [t.exit_reason for t in trades_by_epic(j)[US]] == ["FORCED"]
    assert j.current_position(US).state is P.FLAT
    assert j.current_position(EPIC).state is not P.FLAT
    assert any("EMERGENCY_FLATTEN_INCOMPLETE" in m for _, m in notifier.alerts)


# --- 9: zamknięty rynek jednego instrumentu -----------------------------------------------------------------------


@pytest.mark.parametrize("us_market", [UsMarket(signals=[new_york(DAY, 14, 0)])], ids=["us500-signal-14:00-ny"])
async def test_a_closed_de40_market_at_its_flatten_does_not_hold_back_the_us500_lane(lane_engine, broker, clock,
                                                                                    journal_path):
    """Scenariusz 9.2.9, wariant (regresja A6). Oczekiwane: rynek DE40 zamknięty od 17:20 do 21:30 Berlin (16:20-20:30
    UTC): spłaszczenie DE40 o 17:25 odrzucone, DE40 EXITING i ponowienia co minutę (stop u brokera zostaje); w tym
    czasie pasmo US500 wchodzi po sygnale 14:00 NY (19:05 UTC) i wychodzi z limitu czasu o 19:55 UTC bez zwłoki,
    bez wstrzymania kluczem DE40; po otwarciu rynku DE40 zamknięty i zaksięgowany `FORCED`."""
    reopen = datetime(2026, 11, 2, 20, 30, tzinfo=UTC)
    at(clock, datetime(2026, 11, 2, 16, 20, tzinfo=UTC), lambda: broker.set_market_closed(True, epic=EPIC))
    at(clock, reopen, lambda: broker.set_market_closed(False, epic=EPIC))
    us_held: list[object] = []
    closes = close_calls(broker)
    broker.on_call("close_position", lambda: us_held.append(lane_engine._guard.market_closed.get(US)))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    (us_trade,) = trades_by_epic(j)[US]
    us_exit_due = new_york(DAY, 14, 5) + timedelta(minutes=50)
    assert timedelta(0) <= us_trade.exit_ts - us_exit_due < timedelta(seconds=10)
    assert all(v is None for v in us_held)
    (de_trade,) = trades_by_epic(j)[EPIC]
    assert de_trade.exit_reason == "FORCED" and timedelta(0) <= de_trade.exit_ts - reopen < timedelta(minutes=2)
    de_tries = [ts for ts, d in closes if d != j.trades_by_deal(US)[0][0]]
    assert len(de_tries) >= 200 and all(b - a >= timedelta(seconds=55) for a, b in zip(de_tries, de_tries[1:]))
    assert j.current_position(EPIC).state is P.FLAT and j.current_position(US).state is P.FLAT
    assert_portfolio_books_balance(journal_path)


@pytest.mark.parametrize("us_market", [UsMarket(signals=[new_york(DAY, 15, 20)])], ids=["us500-signal-15:20-ny"])
async def test_us500_closed_market_at_its_flatten_is_carried_overnight_and_flattened_after_berlin_midnight(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Scenariusz 9.2.9 i dzień portfela po północy. Oczekiwane (projekt §1.3, §3.6, A6, A14): pozycja US500 z sygnału
    15:20 NY (wejście 20:25 UTC), rynek US500 zamknięty od 20:50 UTC; o 15:55 NY (20:55 UTC) spłaszczenie odrzucone,
    EXITING, ponowienia co minutę także po końcu dnia portfela (22:00 Berlin) w spoczynku i po północy Berlina w nowym
    dniu portfela (wtorek); po otwarciu rynku o 23:30 UTC (00:30 Berlin wtorek, 18:30 NY poniedziałek) zamknięcie
    `FORCED`; dzień księgowania to data berlińska wyjścia (wtorek, projekt §1.3): znacznik `pnl_pending` z dniem
    wtorkowym, a po rozliczeniu (najpóźniej w pierwszym takcie przygotowania wtorku, bo spoczynek bez pozycji niczego
    nie zarządza; wejścia do tego czasu i tak blokuje rozliczenie w toku) kwota EUR w liczniku wtorku; poniedziałkowy
    licznik zawiera tylko DE40; po północy pasmo pracuje na sesji NYSE wtorku (nie poniedziałku)."""
    reopen = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    tuesday = date(2026, 11, 3)
    at(clock, datetime(2026, 11, 2, 20, 50, tzinfo=UTC), lambda: broker.set_market_closed(True, epic=US))
    at(clock, reopen, lambda: broker.set_market_closed(False, epic=US))
    stop_engine_at(lane_engine, clock, reopen + timedelta(minutes=5))
    await lane_engine.run_forever()
    j = lane_engine.journal
    (us_trade,) = trades_by_epic(j)[US]
    assert us_trade.exit_reason == "FORCED" and timedelta(0) <= us_trade.exit_ts - reopen < timedelta(minutes=2)
    (us_deal,) = [d for d, t in j.trades_by_deal(US)]
    assert lane_engine.lanes[0].day is not None and lane_engine.lanes[0].day.day == tuesday
    assert j.current_position(US).state is P.FLAT
    pending = dict(j.pnl_pending_deals())
    if pending:  # jeszcze w spoczynku: rozliczy przygotowanie wtorku
        assert pending == {us_deal: tuesday}
        engine = restart(make_engine, lane_engine, lanes, clock)
        stop_engine_at(engine, clock, berlin(tuesday, 8, 52))
        await engine.run_day(tuesday)
        j = engine.journal
        assert j.count_entry_orders(tuesday, EPIC) == 0 and j.count_entry_orders(tuesday, US) == 0
    assert j.pnl_pending_deals() == [] and j.trade_fx(us_deal) is not None
    (de_trade,) = trades_by_epic(j)[EPIC]
    assert j.realized_pnl(DAY) == de_trade.net_pnl
    assert j.realized_pnl(tuesday) == j.trade_fx(us_deal).net_eur
    assert_portfolio_books_balance(journal_path)


# --- 11: pozycja właściciela na instrumencie bez pasma ----------------------------------------------------------------


async def test_owner_position_without_a_lane_blocks_entries_of_both_lanes_and_is_never_touched(
    lane_engine, broker, clock, journal_path
):
    """Scenariusz 9.2.11. Oczekiwane (projekt §5.1, rozstrzygnięcie zadania 12): pozycja właściciela na US100 (bez
    pasma) → zdarzenie UNMANAGED_POSITION raz na deal w procesie, żadne wywołanie brokera zmieniające tę pozycję
    (`close_position`, `set_stop`, `set_protection`), nie trafia do tabeli `positions`, wejścia OBU pasm zablokowane
    (`PORTFOLIO_UNMANAGED_EXPOSURE`), choć oba mają sygnał."""
    broker.positions["OWN-1"] = OpenPosition("OWN-1", "US100", Side.BUY, D("1"), D("20000"), D("19900"),
                                             clock.now_utc())  # fmt: skip
    touched: list[str] = []
    for name in ("close_position", "set_stop", "set_protection"):
        real = getattr(broker, name)

        async def spy(deal_id, *args, _real=real, _name=name):
            touched.append(f"{_name}:{deal_id}")
            return await _real(deal_id, *args)

        setattr(broker, name, spy)
    stop_engine_at(lane_engine, clock, US_ENTRY + timedelta(minutes=5))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert "OWN-1" in broker.positions and not any("OWN-1" in t for t in touched)
    assert len(engine_events(j, "UNMANAGED_POSITION")) == 1
    assert all(p.epic != "US100" for p in j.positions())
    assert j.count_entry_orders(DAY, EPIC) == 0 and j.count_entry_orders(DAY, US) == 0
    blocks = gate_blocks(j)
    assert len(blocks) >= 2 and all(PORTFOLIO_UNMANAGED_EXPOSURE in b["portfolio_reasons"] for b in blocks)
    assert any(d["outcome"] == "ENTER" for d in lane_decisions(j))
    assert_portfolio_books_balance(journal_path)


# --- 13, brak kursu EURUSD przy wyjściu: wynik w USD bez kwoty EUR -------------------------------------------------


@pytest.mark.parametrize(
    "us_market", [UsMarket(signals=[new_york(DAY, 9, 45), new_york(DAY, 14, 0)])], ids=["second-us500-signal"]
)
async def test_usd_result_without_a_rate_blocks_both_lanes_never_the_exit_and_settles_when_the_rate_returns(
    lane_engine, broker, clock, journal_path, sim_day
):
    """Scenariusz 9.2.13 i „brak kursu EURUSD przy wyjściu”. Oczekiwane (projekt §7.3, §7.4, CLAUDE.md): kurs EURUSD
    przestaje się zmieniać po wejściu US500 (nieważny po 60 s); wyjście z limitu czasu idzie bez kursu; wynik US500
    zostaje w fazie 1 (`pnl_pending`, nic nie zgadnięte, licznik EUR bez tej kwoty); drugi sygnał US500 (14:00 NY)
    zablokowany rozliczeniem w toku (`PNL_PENDING` bramki pasma albo `PORTFOLIO_PNL_PENDING`); następnego dnia wejście
    DE40 też zablokowane tym powodem; gdy
    kurs wraca, rozliczenie atomowe (ESTIMATED_FX) do dnia wyjścia (poniedziałek), znacznik znika, księga = liczniki."""
    state = {"frozen": US_ENTRY + timedelta(minutes=1)}

    def fx(now: datetime) -> Quote:
        ts = now if state["frozen"] is None else min(now, state["frozen"])
        return Quote(FX, D("1.0800"), D("1.0802"), ts)

    broker.set_quote_fn(fx, epic=FX)
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert j.current_position(US).state is P.FLAT
    ((deal, monday),) = j.pnl_pending_deals()
    assert monday == DAY and j.trade_fx(deal) is None
    (de_trade,) = trades_by_epic(j)[EPIC]
    assert j.realized_pnl(DAY) == de_trade.net_pnl
    assert j.count_entry_orders(DAY, US) == 1
    assert any(b.get("epic") == US and pending_blocked(b) for b in gate_blocks(j))
    # wtorek: wejście DE40 zablokowane, potem kurs wraca i transakcja się rozlicza
    tuesday = date(2026, 11, 3)
    broker.set_daily_candles(daily_candles(tuesday), epic=EPIC)
    broker.set_candles_5m(five_minute_candles(tuesday, D("25780"), D("25790")), epic=EPIC)
    blocks_before = len(gate_blocks(j))
    at(clock, berlin(tuesday, 9, 30), lambda: state.update(frozen=None))
    stop_engine_at(lane_engine, clock, berlin(tuesday, 9, 35))
    await lane_engine.run_day(tuesday)
    assert j.count_entry_orders(tuesday, EPIC) == 0
    assert any(pending_blocked(b) for b in gate_blocks(j)[blocks_before:])
    assert j.pnl_pending_deals() == []
    fx_rec = j.trade_fx(deal)
    assert fx_rec is not None and fx_rec.source == "ESTIMATED_FX"
    assert j.realized_pnl(DAY) == de_trade.net_pnl + fx_rec.net_eur
    assert_portfolio_books_balance(journal_path)


# --- 14: limit dzienny portfela przekroczony sumą dwóch pasm -------------------------------------------------------


async def test_daily_loss_reached_only_by_the_sum_of_two_lanes_soft_kills(lane_engine, broker, clock, journal_path,
                                                                         notifier):
    """Scenariusz 9.2.14. Oczekiwane (projekt §4, spec §9 S9, `risk_v1.toml` max_daily_loss 1,50 EUR): DE40 wybity
    stopem z luką (strata ok. 0,90 EUR, pod limitem: bez kill switcha), potem US500 wybity stopem z luką (strata ok.
    0,90 EUR po przeliczeniu, sama pod limitem); suma dnia portfela ≤ −1,50 → kill SOFT DAILY_LOSS (nie HARD, bo
    > −3,00) dopiero po rozliczeniu US500 w EUR; wpis do licznika poniedziałku; księga = liczniki."""
    def gap_de40() -> None:
        deal = deal_of(broker, EPIC)
        broker.trigger_stop(deal, broker.positions[deal].open_level - 900)

    def gap_us500() -> None:
        deal = deal_of(broker, US)
        broker.trigger_stop(deal, broker.positions[deal].open_level - 97)

    kills: list[tuple[datetime, KillLevel]] = []
    at(clock, datetime(2026, 11, 2, 14, 55, tzinfo=UTC), gap_de40)
    at(clock, datetime(2026, 11, 2, 14, 56, tzinfo=UTC), lambda: kills.append((clock.now_utc(), lane_engine.kill.level())))
    at(clock, datetime(2026, 11, 2, 15, 0, tzinfo=UTC), gap_us500)
    stop_engine_at(lane_engine, clock, datetime(2026, 11, 2, 15, 2, tzinfo=UTC))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    by_epic = trades_by_epic(j)
    (de,), (us,) = by_epic[EPIC], by_epic[US]
    assert de.exit_reason == "STOP" and us.exit_reason == "STOP"
    assert kills == [(kills[0][0], KillLevel.NONE)]  # sama strata DE40 pod limitem
    (deal,) = [d for d, t in j.trades_by_deal(US)]
    us_eur = j.trade_fx(deal).net_eur
    assert D("-1.50") < de.net_pnl < 0 and D("-1.50") < us_eur < 0
    assert j.realized_pnl(DAY) == de.net_pnl + us_eur <= D("-1.50")
    assert lane_engine.kill.level() is KillLevel.SOFT
    assert j.kill_state().reason == "DAILY_LOSS"
    assert_portfolio_books_balance(journal_path)


TIGHT_PORTFOLIO = (Path("config") / "portfolio.toml").read_text().replace(
    'max_open_risk = "1.50"', 'max_open_risk = "1.00"'
).replace('max_daily_loss = "1.50"', 'max_daily_loss = "1.00"')


@pytest.mark.parametrize(
    ("de_points", "us_points", "level", "reason"),
    [(600, 62, KillLevel.SOFT, "DAILY_LOSS"), (1040, 106, KillLevel.HARD, "DAILY_LOSS_2X")],
    ids=["sum-over-portfolio-limit", "sum-over-twice-the-portfolio-limit"],
)
async def test_kill_switch_uses_the_tighter_portfolio_daily_loss_limit(
    make_engine, config_dir, broker, clock, journal_path, de_points, us_points, level, reason
):
    """Scenariusz 9.2.14 z limitem portfela OSTRZEJSZYM od `risk_v1.toml` (dozwolone przez `load_portfolio`):
    `max_daily_loss` portfela 1,00 EUR, v1 1,50 EUR. Oczekiwane (projekt §4.4: „wspólny limit dnia portfela (kill: jak
    S9, progi z tego pliku)”): wynik dnia portfela ≤ −1,00 → kill SOFT DAILY_LOSS, ≤ −2,00 → HARD DAILY_LOSS_2X, a nie
    dopiero przy −1,50 / −3,00 z progów v1; bramka portfela i tak blokuje wejścia od −1,00, ale bez kill switcha
    HARD nie zamyka pozycji i nie zostaje do `clear-kill`."""
    write_lane_files(config_dir, TIGHT_PORTFOLIO)
    engine = make_engine(Mode.LIVE, lanes=load_lanes(config_dir), lane_arming=lane_armed)

    def gap(epic: str, points: int) -> None:
        deal = deal_of(broker, epic)
        broker.trigger_stop(deal, broker.positions[deal].open_level - points)

    at(clock, datetime(2026, 11, 2, 14, 55, tzinfo=UTC), lambda: gap(EPIC, de_points))
    at(clock, datetime(2026, 11, 2, 15, 0, tzinfo=UTC), lambda: gap(US, us_points))
    stop_engine_at(engine, clock, datetime(2026, 11, 2, 15, 2, tzinfo=UTC))
    await engine.run_day(DAY)
    j = engine.journal
    (us_deal,) = [d for d, _t in j.trades_by_deal(US)]
    total = j.realized_pnl(DAY)
    assert total == trades_by_epic(j)[EPIC][0].net_pnl + j.trade_fx(us_deal).net_eur
    bound = D("-1.00") if level is KillLevel.SOFT else D("-2.00")
    assert bound - D("0.5") < total <= bound  # pod progiem portfela, nad odpowiednim progiem v1
    assert engine.kill.level() is level and j.kill_state().reason == reason
    assert_portfolio_books_balance(journal_path)


# --- 16, 17: uzbrojenie pasma i rozbrojenie ---------------------------------------------------------------------------


async def test_a_lane_without_a_frozen_fingerprint_is_dry_run_next_to_a_live_de40(make_engine, lanes, broker, clock,
                                                                                 journal_path):
    """Scenariusz 9.2.16. Oczekiwane (R7, projekt §6.2, §6.3): proces live (uzbrojony przez właściciela), pasmo US500
    bez zamrożonego odcisku (domyślny warunek uzbrojenia pasma) → pasmo pracuje jako dry_run: decyzje ENTER zapisane
    z trybem `dry_run`, zero `open_position` dla US500 (także druga linia obrony `GuardedBroker` po `req.epic`), zero
    zleceń US500 w dzienniku; DE40 handluje live (wejście i spłaszczenie 17:25)."""
    engine = make_engine(Mode.LIVE, lanes=lanes)
    engine.check_wiring()
    opens = open_calls(broker)
    report = await engine.run_day(DAY)
    j = engine.journal
    assert engine.mode is Mode.LIVE and engine.executor.live and not engine.lanes[0].executor.live
    assert [e for _, e in opens] == [EPIC]
    assert report.entries == 1 and [t.instrument for t in j.trades()] == [EPIC]
    assert j.count_entry_orders(DAY, US) == 0
    decided = lane_decisions(j)
    assert any(d["outcome"] == "ENTER" for d in decided) and all(d["mode"] == "dry_run" for d in decided)
    with pytest.raises(BrokerRejected, match="LANE_DRY_RUN"):
        await engine.broker.open_position(
            TargetedEntryRequest("gws-20261102-US500-9-entry", US, Side.BUY, D("0.01"), D("10"), D("15"))
        )
    assert [e for _, e in opens] == [EPIC]
    assert_portfolio_books_balance(journal_path)


@pytest.mark.parametrize(
    "us_market", [UsMarket(signals=[new_york(DAY, 9, 45), new_york(DAY, 14, 0)])], ids=["second-us500-signal"]
)
async def test_disarming_with_two_open_positions_keeps_managing_both_and_sends_no_new_entry(
    lane_engine, broker, clock, data_dir, journal_path
):
    """Scenariusz 9.2.17. Oczekiwane (projekt §6.2, K8, CLAUDE.md): właściciel usuwa plik uzbrojenia o 15:00 UTC przy
    otwartych DE40 i US500 → przy najbliższej kontroli (przed wejściem) wszystkie pasma dry_run do końca procesu,
    zdarzenie MODE_DOWNGRADED; obie pozycje dalej zarządzane: US500 zamknięta z limitu czasu (15:40 UTC), DE40 o
    17:25, obie zaksięgowane; drugi sygnał US500 (14:00 NY) to decyzja ENTER bez zlecenia (rozbrojenie wykryte
    kontrolą tuż przed wejściem; kolejne decyzje w trybie dry_run)."""
    at(clock, HARD_AT, lambda: disarm(data_dir))
    opens = open_calls(broker)
    report = await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.mode is Mode.DRY_RUN and not lane_engine.lanes[0].executor.live
    assert engine_events(j, "MODE_DOWNGRADED")
    assert [e for _, e in opens] == [EPIC, US]
    by_epic = trades_by_epic(j)
    (us,), (de,) = by_epic[US], by_epic[EPIC]
    assert us.exit_reason == "FORCED" and timedelta(0) <= us.exit_ts - US_TIME_LIMIT < timedelta(seconds=10)
    assert de.exit_reason == "FORCED" and timedelta(0) <= de.exit_ts - DE_FLATTEN < timedelta(seconds=10)
    late = [d for d in lane_decisions(j) if d["outcome"] == "ENTER" and d["signal_candle_start_utc"] > HARD_AT.isoformat()]
    # tryb w zapisie decyzji to tryb W CHWILI decyzji (jak DECISION v1); rozbrojenie wykrywa kontrola tuż przed
    # wejściem, więc zaraz po decyzji jest MODE_DOWNGRADED, a zlecenia nie ma
    (downgraded,) = [e for e in j.events("engine") if json.loads(e.payload).get("event") == "MODE_DOWNGRADED"]
    assert len(late) == 1 and late[0]["decided_at"] == downgraded.ts.isoformat()
    later = [d for d in lane_decisions(j) if d["decided_at"] > downgraded.ts.isoformat()]
    assert later and all(d["mode"] == "dry_run" for d in later)
    assert j.count_entry_orders(DAY, US) == 1 and report.final_position_state is P.FLAT
    assert j.current_position(US).state is P.FLAT
    assert_portfolio_books_balance(journal_path)


# --- izolacja awarii jednego pasma ------------------------------------------------------------------------------------


async def test_a_failing_us500_reconciliation_is_counted_per_lane_and_never_blocks_any_exit(
    lane_engine, broker, clock, notifier, journal_path
):
    """Izolacja awarii pasma (regresja A8, projekt §2.8). Oczekiwane: od 15:00 UTC każdy przebieg uzgadniania US500
    rzuca; udane przebiegi DE40 w tych samych taktach NIE zerują licznika US500, więc po 3 nieudanych przebiegach
    kill SOFT RECONCILE_FAILING z nazwą US500; wyjścia trwają: US500 zamknięta z limitu czasu (15:40 UTC) i
    zaksięgowana przez wykonawcę, DE40 spłaszczony o 17:25."""
    lane = lane_engine.lanes[0]

    async def broken() -> None:
        raise RuntimeError("lane reconciliation bug")

    at(clock, HARD_AT, lambda: setattr(lane.reconciler, "run_once", broken))
    stop_engine_at(lane_engine, clock, DE_FLATTEN + timedelta(minutes=2))
    await lane_engine.run_day(DAY)
    j = lane_engine.journal
    assert lane_engine.kill.level() is KillLevel.SOFT and j.kill_state().reason == "RECONCILE_FAILING"
    assert any("RECONCILE_FAILING" in m and US in m for _, m in notifier.alerts)
    by_epic = trades_by_epic(j)
    (us,), (de,) = by_epic[US], by_epic[EPIC]
    assert timedelta(0) <= us.exit_ts - US_TIME_LIMIT < timedelta(seconds=10)
    assert timedelta(0) <= de.exit_ts - DE_FLATTEN < timedelta(seconds=10)
    assert j.current_position(US).state is P.FLAT and j.current_position(EPIC).state is P.FLAT
    assert_portfolio_books_balance(journal_path)


# --- 18: święta i zmiana czasu ------------------------------------------------------------------------------------

THANKSGIVING_EVE = date(2026, 11, 25)
THANKSGIVING = date(2026, 11, 26)


@pytest.mark.parametrize("sim_day", [THANKSGIVING_EVE])
@pytest.mark.parametrize(
    "us_market",
    [UsMarket(day=THANKSGIVING_EVE, signals=[new_york(THANKSGIVING_EVE, 9, 45), new_york(THANKSGIVING_EVE, 14, 0)])],
    ids=["two-signals"],
)
async def test_nyse_holiday_on_a_xetra_session_day_us500_only_flattens_its_leftover(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Scenariusz 9.2.18 (święto NYSE w dniu sesji XETRA: Thanksgiving 26.11.2026). Oczekiwane (projekt §1.3, §3.6):
    pozycja US500 z wieczoru 25.11 (proces padł po wejściu 14:05 NY) → 26.11 pasmo US500 nie ma sesji: żadnej
    decyzji ani wejścia US500, pozostałość spłaszczona `FORCED` w pierwszym takcie zarządzania (pasmo nie trzyma
    pozycji przez noc); DE40 handluje normalnie (wejście 09:05, spłaszczenie 17:25); dzień portfela kończy się z
    zamknięciem XETRA (17:30 Berlin), bez ciszy notowań US500 (brak okna sesji US500)."""
    stop_engine_at(lane_engine, clock, new_york(THANKSGIVING_EVE, 14, 10))
    await lane_engine.run_day(THANKSGIVING_EVE)
    j = lane_engine.journal
    assert j.current_position(US).state is P.OPEN and j.current_position(EPIC).state is P.FLAT
    clock.advance((berlin(THANKSGIVING, 7, 0) - clock.now_utc()).total_seconds())
    broker.set_daily_candles(daily_candles(THANKSGIVING), epic=EPIC)
    broker.set_candles_5m(five_minute_candles(THANKSGIVING, D("25780"), D("25790")), epic=EPIC)
    decisions_before = len(lane_decisions(j))
    engine = restart(make_engine, lane_engine, lanes, clock)
    report = await engine.run_day(THANKSGIVING)
    j = engine.journal
    us_trades = trades_by_epic(j)[US]
    assert len(us_trades) == 2 and us_trades[-1].exit_reason == "FORCED"
    assert us_trades[-1].exit_ts - berlin(THANKSGIVING, 7, 0) < timedelta(seconds=30)
    assert len(lane_decisions(j)) == decisions_before
    assert j.count_entry_orders(THANKSGIVING, US) == 0
    assert report.entries == 1 and report.final_position_state is P.FLAT
    (de,) = [t for t in trades_by_epic(j)[EPIC] if t.exit_ts.date() == THANKSGIVING]
    assert timedelta(0) <= de.exit_ts - berlin(THANKSGIVING, 17, 25) < timedelta(seconds=10)
    assert berlin(THANKSGIVING, 17, 30) <= clock.now_utc() < berlin(THANKSGIVING, 17, 35)
    assert engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


CHRISTMAS_EVE_BEFORE = date(2026, 12, 23)
CHRISTMAS_EVE = date(2026, 12, 24)


@pytest.mark.parametrize("sim_day", [CHRISTMAS_EVE_BEFORE])
async def test_xetra_holiday_on_an_nyse_session_day_de40_only_flattens_its_leftover(
    lane_engine, make_engine, lanes, broker, clock, journal_path
):
    """Scenariusz 9.2.18 odwrotnie (święto XETRA w dniu sesji NYSE: Wigilia 24.12.2026, NYSE skrócona do 13:00 NY).
    Oczekiwane: pozycja DE40 z 23.12 (proces padł po wejściu) → 24.12 DE40 bez sesji: żadnej decyzji DE40 (raport
    NOT_SESSION, 0 wejść), pozostałość spłaszczona `FORCED` w pierwszym takcie zarządzania; US500 handluje: sygnał
    09:45 NY, wejście 14:50 UTC, wyjście z limitu czasu 15:40 UTC; drugie okno sygnałów (14:00-15:30 NY) odpada, bo
    spłaszczenie pasma to 12:55 NY; dzień portfela kończy się z zamknięciem NYSE o 13:00 NY (18:00 UTC); bez ciszy
    notowań DE40 (brak okna sesji DE40)."""
    stop_engine_at(lane_engine, clock, berlin(CHRISTMAS_EVE_BEFORE, 9, 12))
    await lane_engine.run_day(CHRISTMAS_EVE_BEFORE)
    j = lane_engine.journal
    assert j.current_position(EPIC).state is P.OPEN
    start = berlin(CHRISTMAS_EVE, 7, 0)
    clock.advance((start - clock.now_utc()).total_seconds())
    us24 = UsMarket(day=CHRISTMAS_EVE, signals=[new_york(CHRISTMAS_EVE, 9, 45)])
    broker.set_quote_fn(us24.quote, epic=US)
    broker.set_candles_5m(us24.candles(), epic=US)
    engine = restart(make_engine, lane_engine, lanes, clock)
    report = await engine.run_day(CHRISTMAS_EVE)
    j = engine.journal
    assert report.skip_reason == "NOT_SESSION" and report.entries == 0 and report.final_position_state is P.FLAT
    (de,) = trades_by_epic(j)[EPIC]
    assert de.exit_reason == "FORCED" and de.exit_ts - start < timedelta(seconds=30)
    (us,) = trades_by_epic(j)[US]
    entry = new_york(CHRISTMAS_EVE, 9, 50)
    assert timedelta(0) <= us.submit_ts - entry < timedelta(seconds=10)
    assert timedelta(0) <= us.exit_ts - (entry + timedelta(minutes=50)) < timedelta(seconds=10)
    decided = [d for d in lane_decisions(j) if d["day"] == CHRISTMAS_EVE.isoformat()]
    assert decided and all(d["signal_candle_start_utc"] < new_york(CHRISTMAS_EVE, 12, 0).isoformat() for d in decided)
    assert new_york(CHRISTMAS_EVE, 13, 0) <= clock.now_utc() < new_york(CHRISTMAS_EVE, 13, 5)
    assert engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)


EU_ONLY_DST = date(2026, 10, 27)  # Berlin już CET (UTC+1, od 25.10), Nowy Jork jeszcze EDT (UTC-4, do 1.11)


@pytest.mark.parametrize("sim_day", [EU_ONLY_DST])
async def test_the_week_between_the_eu_and_us_clock_changes_shifts_the_nyse_session_by_one_hour(
    lane_engine, broker, clock, journal_path
):
    """Zmiana czasu UE 25.10 vs USA 1.11 (wtorek 27.10.2026). Oczekiwane (kalendarze ze strefą, projekt §9.1): DE40
    09:05 Berlin = 08:05 UTC, spłaszczenie 16:25 UTC; NYSE 09:30-16:00 EDT = 13:30-20:00 UTC (14:30-21:00 Berlin):
    świeca sygnału 09:45 NY = 13:45 UTC, wejście US500 13:50 UTC, limit czasu 14:40 UTC; pętla dnia kończy się z
    zamknięciem NYSE o 20:00 UTC (nie 21:00); obie pozycje otwarte jednocześnie; ta sama data berlińska."""
    opens = open_calls(broker)
    report = await lane_engine.run_day(EU_ONLY_DST)
    j = lane_engine.journal
    us_entry = datetime(2026, 10, 27, 13, 50, tzinfo=UTC)
    assert [e for _, e in opens] == [EPIC, US]
    assert timedelta(0) <= opens[0][0] - datetime(2026, 10, 27, 8, 5, tzinfo=UTC) < timedelta(seconds=10)
    assert timedelta(0) <= opens[1][0] - us_entry < timedelta(seconds=10)
    by_epic = trades_by_epic(j)
    (us,), (de,) = by_epic[US], by_epic[EPIC]
    assert timedelta(0) <= us.exit_ts - (us_entry + timedelta(minutes=50)) < timedelta(seconds=10)
    assert timedelta(0) <= de.exit_ts - datetime(2026, 10, 27, 16, 25, tzinfo=UTC) < timedelta(seconds=10)
    (entered,) = [d for d in lane_decisions(j) if d["outcome"] == "ENTER"]
    assert entered["signal_candle_start_utc"] == datetime(2026, 10, 27, 13, 45, tzinfo=UTC).isoformat()
    assert datetime(2026, 10, 27, 20, 0, tzinfo=UTC) <= clock.now_utc() < datetime(2026, 10, 27, 20, 5, tzinfo=UTC)
    assert report.final_position_state is P.FLAT and lane_engine.kill.level() is KillLevel.NONE
    assert_portfolio_books_balance(journal_path)
