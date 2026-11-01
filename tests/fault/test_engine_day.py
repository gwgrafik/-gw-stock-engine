"""Symulowane dni silnika (spec §4, §8, §9, §13) na `FakeBroker` i `FakeClock`, z prawdziwym dziennikiem, menedżerem,
uzgadnianiem, wykonawcą, bramką i kill switchem.

Niezmienniki pod lupą (prawdziwe pieniądze):
- uzgadnianie startowe przed jakimkolwiek wejściem; najwyżej jedno wejście dziennie, także po restarcie;
- pierwsza świeca to wyłącznie świeca o starcie równym otwarciu sesji w UTC; jej brak = NO_SIGNAL_DATA;
- spłaszczenie o 17:25 (godzina z kalendarza), ponawiane co takt; po starcie po tej godzinie od razu spłaszczenie;
- wygasła sesja: ponowne logowanie i ponowienie (także zamknięcia), z ochroną przed blokadą konta;
- kill switch HARD: awaryjne spłaszczenie co takt; EXITING: ponawianie co takt; awaria dziennika nie zatrzymuje
  zarządzania; zatrzymanie zostawia pozycję chronioną stopem u brokera.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType

import pytest

from gwstock.app import Engine, EngineMisconfigured, Mode
from gwstock.calendar import XETRA, ExchangeCalendar, ExchangeProfile
from gwstock.journal import JournalReadError, JournalWriteError, PositionRecord, TradeRecord
from gwstock.reconcile import Reconciler
from gwstock.types import BrokerError, BrokerTimeout, KillLevel, MarketClosed, OrderState, PositionState, Side
from gwstock.watchdog import AlreadyRunning, InstanceLock
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    CAL,
    CLOSE_UTC,
    DAY,
    DEADLINE_UTC,
    DECISION_UTC,
    FLATTEN_UTC,
    MID,
    OPEN_UTC,
    D,
    Market,
    at_time,
    berlin,
    broker,
    calls_at,
    cfgs,
    clock,
    daily_candles,
    data_dir,
    day_rules,
    engine,
    engine_decisions,
    five_minute_candles,
    journal_path,
    make_engine,
    market,
    payloads,
    runtime,
    sim_root,
    stop_engine_at,
    with_risk,
)

P = PositionState
STOP_LEVEL = MID + 1 - 30  # BUY po ask 25782.3, stop 30 pkt


class ProcessDied(BaseException):
    """Symulacja śmierci procesu (kill -9) w wybranej chwili: nie jest `Exception`, więc nic jej nie połyka."""


def die(*_a):
    raise ProcessDied()


def position(engine):
    return engine.journal.current_position("DE40")


def kill_reasons(journal) -> list[str]:
    return [p["reason"] for p in payloads(journal, "kill.audit") if p["event"] in ("ENGAGE", "ESCALATE", "REASON")]


def alerts(notifier, text):
    return [(level, message) for level, message in notifier.alerts if text in message]


def trade(net="0.10", exit_ts=None, side=Side.BUY):
    t = exit_ts or datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    return TradeRecord(
        "DE40", side, D("0.001"), MID - 1, MID + 1, MID + 1, MID + 40, D("0.04"), D("0.00"), D("0.00"), D("0.00"),
        D("0.00"), D(net), t, t, t, t, 0, "FORCED",
    )  # fmt: skip


def book_trades(journal, count, net="0.10"):
    for i in range(count):
        exit_ts = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc) + timedelta(days=i)
        journal.record_trade(trade(net, exit_ts), f"OLD-{i}", CAL.to_local(exit_ts).date())


# --- dzień wzorcowy -----------------------------------------------------------------------------------------


async def test_full_day_long_entry_flat_at_1725_and_report(engine, broker, clock):
    entries = calls_at(broker, clock, "open_position")
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)
    assert rep.entries == 1 and rep.final_position_state is P.FLAT and rep.net_pnl is not None
    assert rep.skip_reason is None and rep.day == DAY and rep.mode == "live"
    assert entries == [DECISION_UTC]  # decyzja dokładnie o 09:05:00, po zamknięciu pierwszej świecy
    assert closes == [FLATTEN_UTC]  # spłaszczenie o 17:25:00 (kalendarz.flatten_time, nie stała)
    [t] = engine.journal.trades()
    assert t.side is Side.BUY and t.size == D("0.001") and t.entry_fill == MID + 1 and t.exit_fill == MID + 30 - 1
    assert t.exit_reason == "FORCED" and t.gross_pnl == D("0.03") and rep.net_pnl == t.net_pnl
    assert clock.now_utc() >= CLOSE_UTC and engine.kill.level() is KillLevel.NONE


async def test_startup_reconcile_runs_before_any_entry_and_the_stop_is_attached(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    calls = broker.calls
    assert calls.index("list_positions") < calls.index("open_position")
    assert calls.index("login") < calls.index("list_positions")
    assert calls.count("open_position") == 1


async def test_short_entry_is_booked_with_its_result(make_engine, broker, market):
    broker.set_candles_5m(five_minute_candles(DAY, D("25790"), D("25780")))  # świeca spadkowa: SELL
    market.late_mid = MID - 20
    engine = make_engine(Mode.LIVE)
    rep = await engine.run_day(DAY)
    [t] = engine.journal.trades()
    assert t.side is Side.SELL and t.entry_fill == MID - 1 and t.exit_fill == MID - 20 + 1
    assert t.gross_pnl == D("0.02") and rep.net_pnl == t.net_pnl and rep.final_position_state is P.FLAT


async def test_doji_first_candle_means_no_entry(make_engine, broker):
    broker.set_candles_5m(five_minute_candles(DAY, D("25785"), D("25785")))
    engine = make_engine(Mode.LIVE)
    rep = await engine.run_day(DAY)
    assert rep.skip_reason == "DOJI" and rep.entries == 0 and rep.net_pnl is None
    assert "open_position" not in broker.calls and "close_position" not in broker.calls
    [decision] = engine_decisions(engine.journal)
    assert decision["outcome"] == "SKIP" and decision["skip_reason"] == "DOJI"


# --- kalendarz: święto, skrócona sesja ----------------------------------------------------------------------


async def test_no_entry_on_holiday_and_on_early_close_after_flatten(make_engine, broker, clock):
    xmas = date(2026, 12, 24)
    clock.advance((berlin(xmas, 8, 45) - clock.now_utc()).total_seconds())
    engine = make_engine(Mode.LIVE)
    rep = await engine.run_day(xmas)
    assert rep.skip_reason == "NOT_SESSION" and rep.entries == 0 and rep.final_position_state is P.FLAT
    assert broker.calls == []  # dziennik FLAT: w dzień bez sesji żadnego wywołania brokera
    # skrócona sesja (profil testowy: 2026-12-29 do 14:00): start po spłaszczeniu 13:55 → brak wejścia
    early = date(2026, 12, 29)
    profile = replace(XETRA, early_closes=MappingProxyType({early: time(14, 0)}))
    clock.advance((berlin(early, 13, 56) - clock.now_utc()).total_seconds())
    later = make_engine(Mode.LIVE, calendar=ExchangeCalendar(profile))
    broker.set_daily_candles(daily_candles(early))
    broker.set_candles_5m(five_minute_candles(early, D("25780"), D("25790")))
    rep = await later.run_day(early)
    assert rep.skip_reason == "NO_SIGNAL_DATA" and rep.entries == 0
    assert "open_position" not in broker.calls


async def test_early_close_day_flattens_five_minutes_before_the_early_close(make_engine, broker, clock):
    early = DAY
    profile = replace(XETRA, early_closes=MappingProxyType({early: time(14, 0)}))
    engine = make_engine(Mode.LIVE, calendar=ExchangeCalendar(profile))
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(early)
    assert closes == [berlin(early, 13, 55)]
    assert rep.entries == 1 and rep.final_position_state is P.FLAT and rep.net_pnl is not None
    assert clock.now_utc() < berlin(early, 14, 5)  # dzień kończy się po skróconym zamknięciu, nie o 17:30


# --- restarty -----------------------------------------------------------------------------------------------


async def test_restart_mid_entry_adopts_position_without_duplicate(make_engine, broker, clock):
    first = make_engine(Mode.LIVE)
    broker.on_call("confirm", die, once=True)  # zlecenie przyjęte u brokera, proces ginie przed potwierdzeniem
    with pytest.raises(ProcessDied):
        await first.run_day(DAY)
    assert first.journal.unresolved_orders()[0].state is OrderState.SUBMITTING
    second = make_engine.restart(first, Mode.LIVE)  # nowy dziennik z dysku, nowe obiekty, TEN SAM broker
    assert second is not first and second.journal is not first.journal
    rep = await second.run_day(DAY)
    assert broker.calls.count("open_position") == 1  # żadnego drugiego wejścia
    assert rep.entries == 1 and rep.final_position_state is P.FLAT and rep.skip_reason is None
    [t] = second.journal.trades()
    assert t.exit_reason == "FORCED" and rep.net_pnl == t.net_pnl
    assert second.journal.get_order("gws-20261102-DE40-1-entry").state is OrderState.CONFIRMED


async def test_start_after_flatten_time_with_open_position_flattens_and_does_not_enter(make_engine, broker, clock):
    first = make_engine(Mode.LIVE)
    at_time(broker, clock, datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc), lambda: die())
    with pytest.raises(ProcessDied):
        await first.run_day(DAY)
    assert position(first).state is P.OPEN
    clock.advance((berlin(DAY, 17, 40) - clock.now_utc()).total_seconds())  # restart o 17:40 w Berlinie
    second = make_engine.restart(first, Mode.LIVE)
    closes = calls_at(broker, clock, "close_position")
    rep = await second.run_day(DAY)
    assert closes == [berlin(DAY, 17, 40)]  # od razu, w pierwszym takcie
    assert broker.calls.count("open_position") == 1 and rep.final_position_state is P.FLAT
    assert rep.entries == 1 and len(second.journal.trades()) == 1


async def test_stop_hit_by_broker_is_booked_once(engine, broker, clock):
    at_time(broker, clock, datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
            lambda: broker.trigger_stop("DEAL-1", STOP_LEVEL))  # fmt: skip
    stop_engine_at(engine, clock, datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc))  # 360 uzgodnień po stopie
    rep = await engine.run_day(DAY)
    [t] = engine.journal.trades()
    assert t.exit_reason == "STOP" and t.exit_fill == STOP_LEVEL and rep.net_pnl == t.net_pnl < 0
    assert "close_position" not in broker.calls and rep.final_position_state is P.FLAT
    assert engine.journal.realized_pnl(DAY) == t.net_pnl  # policzone dokładnie raz


# --- pierwsza świeca ----------------------------------------------------------------------------------------


async def test_missing_first_candle_at_0910_means_no_entry_and_logged(make_engine, broker, clock):
    broker.set_candles_5m(five_minute_candles(DAY, D("25780"), D("25790"), include_first=False))
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    reads = calls_at(broker, clock, "candles_5m")
    rep = await engine.run_day(DAY)
    assert rep.skip_reason == "NO_SIGNAL_DATA" and rep.entries == 0
    assert "open_position" not in broker.calls
    assert reads[0] == DECISION_UTC and reads[-1] == DEADLINE_UTC  # czekamy na świecę do 09:10 włącznie
    [decision] = engine_decisions(engine.journal)
    assert decision["why"] == "FIRST_CANDLE_MISSING" and decision["first_candle"] is None
    assert decision["skip_reason"] == "NO_SIGNAL_DATA"


async def test_engine_started_after_the_entry_deadline_has_no_signal_data(make_engine, broker, clock):
    clock.advance((berlin(DAY, 9, 11) - clock.now_utc()).total_seconds())
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert rep.skip_reason == "NO_SIGNAL_DATA" and rep.entries == 0
    assert "open_position" not in broker.calls and "candles_5m" not in broker.calls
    [decision] = engine_decisions(engine.journal)
    assert decision["why"] == "STARTED_AFTER_DEADLINE"


async def test_a_candle_stamped_at_another_minute_is_never_taken_as_the_first(make_engine, broker, clock):
    # świeca o starcie 09:00:01 lokalnie (przesunięty stempel) nie jest świecą 09:00-09:05: wybór tylko po równości
    candles = five_minute_candles(DAY, D("25780"), D("25790"), include_first=False)
    odd = replace(candles[1], start=OPEN_UTC + timedelta(seconds=1))
    broker.set_candles_5m([*candles, odd])
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert rep.skip_reason == "NO_SIGNAL_DATA" and "open_position" not in broker.calls


async def test_decision_event_records_first_candle_start_and_ohlc(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    [d] = engine_decisions(engine.journal)
    assert d["first_candle"] == {
        "epic": "DE40", "start": "2026-11-02T08:00:00+00:00", "open": "25780", "high": "25795", "low": "25775",
        "close": "25790",
    }  # fmt: skip
    assert d["first_candle_start_utc"] == "2026-11-02T08:00:00+00:00"
    assert d["atr"] == "300" and d["mode"] == "live" and d["session_index"] == 0 and d["rule_size_allowed"] is False
    assert d["quote"]["bid"] == "25780.3" and d["quote"]["ask"] == "25782.3"
    assert d["outcome"] == "SIGNAL" and d["signal"] == {"side": "BUY", "stop_distance": "30.0", "size": "0.001"}
    assert d["decided_at"] == DECISION_UTC.isoformat()


@pytest.mark.parametrize(
    ("sessions", "l1", "size"),
    [(4, False, "0.001"), (4, True, "0.001"), (5, False, "0.001"), (5, True, "0.007")],
    ids=["4-sessions", "4-sessions-L1", "5-sessions", "5-sessions-L1"],
)
async def test_first_five_sessions_use_minimum_size_then_rule_size(
    make_engine, broker, clock, data_dir, sessions, l1, size
):
    engine = make_engine(Mode.LIVE)
    book_trades(engine.journal, sessions)
    if l1:
        (data_dir / "L1_PASSED").write_text("")  # tworzy WYŁĄCZNIE właściciel
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    [d] = engine_decisions(engine.journal)
    assert d["session_index"] == sessions and d["rule_size_allowed"] is l1
    assert d["signal"]["size"] == size
    assert broker.positions["DEAL-1"].size == D(size)


# --- notowania, sesja, ping ---------------------------------------------------------------------------------


async def test_quote_silence_61s_triggers_soft_kill_and_blocks_entry(make_engine, broker, clock, market):
    market.frozen_from = OPEN_UTC  # od 09:00 para (bid, ask) stoi w miejscu
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert "QUOTE_SILENCE" in kill_reasons(engine.journal) and engine.kill.level() is KillLevel.SOFT
    assert "open_position" not in broker.calls and rep.skip_reason == "GATE_BLOCKED"
    [block] = payloads(engine.journal, "gate_block")
    assert "KILL" in block["reasons"] and "STALE_DATA" in block["reasons"]


async def test_session_expiry_triggers_relogin_then_retry(engine, broker, clock):
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    at_time(broker, clock, ten, broker.expire_session)
    quotes = calls_at(broker, clock, "quote")
    stop_engine_at(engine, clock, ten + timedelta(minutes=30))
    rep = await engine.run_day(DAY)
    assert broker.calls.count("login") == 2  # start + jedno ponowne logowanie
    assert quotes.count(ten) == 2  # odczyt, który trafił na wygasłą sesję, ponowiony od razu
    assert engine.kill.level() is KillLevel.NONE and rep.final_position_state is P.OPEN


async def test_session_expiry_mid_flatten_relogs_and_retries_the_close_on_the_same_tick(engine, broker, clock):
    at_time(broker, clock, FLATTEN_UTC, broker.expire_session, method="close_position")
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)
    assert closes == [FLATTEN_UTC, FLATTEN_UTC]  # pierwsze: SessionExpired, drugie po zalogowaniu, ten sam takt
    i = broker.calls.index("close_position")
    assert broker.calls[i : i + 3] == ["close_position", "login", "close_position"]
    assert rep.final_position_state is P.FLAT and len(engine.journal.trades()) == 1


async def test_relogin_failing_three_times_soft_kills_and_stops_calling_login(make_engine, broker, clock, notifier):
    broker.set_candles_5m(five_minute_candles(DAY, D("25785"), D("25785")))  # doji: bez pozycji
    engine = make_engine(Mode.LIVE)
    logins = calls_at(broker, clock, "login")
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def broken_login():
        if clock.now_utc() >= ten:
            raise BrokerError("login: POST /session: HTTP 401 error.invalid.details")

    broker.on_call("login", broken_login)
    at_time(broker, clock, ten, broker.expire_session)
    stop_engine_at(engine, clock, ten + timedelta(hours=1))
    rep = await engine.run_day(DAY)
    assert logins[1:] == [ten, ten + timedelta(seconds=30), ten + timedelta(seconds=60)]  # trzy próby i koniec
    assert "LOGIN_FAILED" in kill_reasons(engine.journal) and alerts(notifier, "LOGIN_LOCKOUT")
    assert rep.final_position_state is P.FLAT


async def test_entry_hitting_an_expired_session_is_never_resent(engine, broker, clock):
    # wysyłka trafia na wygasłą sesję: logujemy się ponownie, ale wejścia NIE ponawiamy (brak klucza idempotencji);
    # menedżer ma UNKNOWN i rozstrzyga je listą pozycji i historią
    at_time(broker, clock, DECISION_UTC, broker.expire_session, method="open_position")
    stop_engine_at(engine, clock, berlin(DAY, 9, 30))
    rep = await engine.run_day(DAY)
    assert broker.calls.count("open_position") == 1 and broker.positions == {}
    assert broker.calls.count("login") == 2
    assert engine.journal.get_order("gws-20261102-DE40-1-entry").state is OrderState.NOT_PLACED
    assert rep.entries == 1 and rep.final_position_state is P.FLAT


async def test_no_entry_without_a_successful_startup_reconcile(engine, broker, clock):
    def broken():
        if clock.now_utc() < berlin(DAY, 9, 12):
            raise BrokerError("GET /positions: HTTP 503")

    broker.on_call("list_working_orders", broken)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert rep.skip_reason == "NOT_READY" and "open_position" not in broker.calls
    [decision] = engine_decisions(engine.journal)
    assert decision["why"] == "STARTUP_RECONCILE_PENDING"


async def test_position_left_from_an_earlier_day_is_flattened_at_once(make_engine, broker, clock):
    first = make_engine(Mode.LIVE)
    at_time(broker, clock, datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc), lambda: die())
    with pytest.raises(ProcessDied):
        await first.run_day(DAY)
    tuesday = date(2026, 11, 3)
    clock.advance((berlin(tuesday, 9, 1) - clock.now_utc()).total_seconds())  # start w trakcie sesji wtorku
    second = make_engine.restart(first, Mode.LIVE)
    closes = calls_at(broker, clock, "close_position")
    stop_engine_at(second, clock, berlin(tuesday, 9, 3))
    rep = await second.run_day(tuesday)
    assert closes == [berlin(tuesday, 9, 1)]  # nie czekamy do 17:25: strategia nie trzyma pozycji przez noc
    assert rep.final_position_state is P.FLAT and len(second.journal.trades()) == 1


async def test_engine_pings_every_300s_during_session(engine, broker, clock):
    pings = calls_at(broker, clock, "keepalive")
    stop_engine_at(engine, clock, datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
    await engine.run_day(DAY)
    assert len(pings) == 25  # 08:55 ... 10:55 w Berlinie
    assert all((b - a).total_seconds() == 300 for a, b in zip(pings, pings[1:]))
    assert pings[0] == berlin(DAY, 8, 55)  # 5 minut po przygotowaniu o 08:50


# --- kill switch, EXITING, awarie ----------------------------------------------------------------------------


async def test_kill_hard_runs_emergency_flatten_every_tick(engine, broker, clock, data_dir):
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    at_time(broker, clock, ten, lambda: (broker.set_market_closed(True), (data_dir / "HARD_STOP").write_text("")))
    reopen = ten + timedelta(minutes=1)
    at_time(broker, clock, reopen, lambda: broker.set_market_closed(False), method="list_positions")
    closes = calls_at(broker, clock, "close_position")
    stop_engine_at(engine, clock, reopen + timedelta(minutes=5))
    rep = await engine.run_day(DAY)
    assert engine.kill.level() is KillLevel.HARD
    attempts = [c for c in closes if c <= reopen]
    assert len(attempts) >= 6 and all((b - a).total_seconds() <= 11 for a, b in zip(attempts, attempts[1:]))
    assert rep.final_position_state is P.FLAT and len(engine.journal.trades()) == 1
    assert closes[-1] < reopen + timedelta(seconds=30)


async def test_exiting_position_is_retried_every_tick(engine, broker, clock):
    for _ in range(4):
        broker.fail_next("close_position", BrokerTimeout("DELETE /positions: timeout"))
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)
    assert len(closes) == 5 and closes[0] == FLATTEN_UTC
    assert all(10 <= (b - a).total_seconds() <= 11 for a, b in zip(closes, closes[1:]))
    assert rep.final_position_state is P.FLAT and len(engine.journal.trades()) == 1


async def test_final_pass_after_the_close_retries_the_exit_once_more(engine, broker, clock):
    for _ in range(30):  # każde ponowienie 17:25:00-17:29:50 kończy się timeoutem
        broker.fail_next("close_position", BrokerTimeout("DELETE /positions: timeout"))
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)
    assert len(closes) == 31 and closes[-1] == CLOSE_UTC  # uzgadnianie końcowe o 17:30 ponawia wyjście
    assert rep.final_position_state is P.FLAT and len(engine.journal.trades()) == 1


async def test_market_closed_at_flatten_keeps_exiting_and_retries_until_reopen(engine, broker, clock):
    at_time(broker, clock, FLATTEN_UTC - timedelta(seconds=1), lambda: broker.set_market_closed(True))
    at_time(broker, clock, FLATTEN_UTC + timedelta(seconds=45), lambda: broker.set_market_closed(False),
            method="list_positions")  # fmt: skip
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)
    assert len(closes) >= 4 and rep.final_position_state is P.FLAT


async def test_reconcile_exceptions_alarm_and_soft_kill_after_threshold(engine, broker, clock, notifier):
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    failing = {"until": ten + timedelta(minutes=10)}

    def broken():
        if ten <= clock.now_utc() < failing["until"]:
            raise BrokerError("GET /history/activity: HISTORY_RANGE_TOO_LARGE")

    broker.on_call("list_working_orders", broken)
    at_time(broker, clock, ten + timedelta(minutes=5), lambda: broker.trigger_stop("DEAL-1", STOP_LEVEL))
    stop_engine_at(engine, clock, ten + timedelta(minutes=30))
    rep = await engine.run_day(DAY)
    assert "RECONCILE_FAILING" in kill_reasons(engine.journal)
    assert alerts(notifier, "HISTORY_RANGE_TOO_LARGE")
    # 60 nieudanych przebiegów w 10 minut, ale alarm nie co 10 s: ten sam klucz najwyżej raz na 5 minut
    assert len([a for a in alerts(notifier, "RECONCILE_ERROR") if a[0] == "WARN"]) == 2
    assert broker.calls.count("list_working_orders") >= 60 + 6 * 15  # ponawiane co takt, nie porzucone
    [t] = engine.journal.trades()  # zarządzanie trwa: stop z czasu awarii zaksięgowany, gdy uzgadnianie wróciło
    assert t.exit_reason == "STOP" and rep.final_position_state is P.FLAT


async def test_journal_write_failure_mid_day_soft_kills_but_management_continues(engine, broker, clock, monkeypatch):
    journal = engine.journal
    real = journal.append_raw
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    stop_at = ten + timedelta(minutes=30)

    def flaky(source, payload, ts):
        if ten <= clock.now_utc() < stop_at:
            raise JournalWriteError("disk full")
        return real(source, payload, ts)

    monkeypatch.setattr(journal, "append_raw", flaky)
    at_time(broker, clock, ten + timedelta(minutes=5), lambda: broker.trigger_stop("DEAL-1", STOP_LEVEL))
    stop_engine_at(engine, clock, stop_at + timedelta(minutes=5))
    rep = await engine.run_day(DAY)
    assert engine.kill.level() is KillLevel.SOFT and engine.kill.reason() == "JOURNAL_WRITE_FAILED"
    assert journal.kill_state().reason == "JOURNAL_WRITE_FAILED"  # stan kill switcha zapisany mimo awarii zdarzeń
    [t] = journal.trades()  # stop w trakcie awarii zaksięgowany po jej końcu
    assert t.exit_reason == "STOP" and rep.final_position_state is P.FLAT


async def test_journal_read_failure_mid_day_soft_kills_and_management_resumes(engine, broker, clock, monkeypatch):
    journal = engine.journal
    real = journal.current_position
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    back = ten + timedelta(minutes=10)

    def flaky(epic):
        if ten <= clock.now_utc() < back:
            raise JournalReadError("database is locked")
        return real(epic)

    monkeypatch.setattr(journal, "current_position", flaky)
    at_time(broker, clock, ten + timedelta(minutes=2), lambda: broker.trigger_stop("DEAL-1", STOP_LEVEL))
    stop_engine_at(engine, clock, back + timedelta(minutes=2))
    rep = await engine.run_day(DAY)
    assert engine.kill.level() is KillLevel.SOFT and "JOURNAL_UNREADABLE" in kill_reasons(journal)
    [t] = journal.trades()
    assert t.exit_reason == "STOP" and rep.final_position_state is P.FLAT


def test_guarded_broker_covers_the_whole_broker_protocol():
    import inspect

    from gwstock.app import GuardedBroker
    from gwstock.broker.base import Broker

    protocol = {n for n, m in vars(Broker).items() if inspect.iscoroutinefunction(m)}
    guarded = {n for n, m in vars(GuardedBroker).items() if inspect.iscoroutinefunction(m)}
    assert protocol == guarded  # nowa metoda brokera nie może ominąć ponownego logowania


async def test_unprotected_position_gets_a_stop(engine, broker, clock):
    eleven = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
    at_time(broker, clock, eleven, lambda: broker.positions.update(
        {"DEAL-1": replace(broker.positions["DEAL-1"], stop_level=None)}))  # fmt: skip
    seen = {}
    at_time(broker, clock, eleven + timedelta(minutes=1),
            lambda: seen.setdefault("stop", broker.positions["DEAL-1"].stop_level))  # fmt: skip
    stop_engine_at(engine, clock, eleven + timedelta(minutes=2))
    rep = await engine.run_day(DAY)
    assert broker.calls.count("set_stop") == 1 and seen["stop"] == STOP_LEVEL
    assert engine.kill.level() is KillLevel.NONE and rep.final_position_state is P.OPEN


async def test_startup_restores_loss_limit_kills_from_the_journal(make_engine, broker, clock):
    engine = make_engine(Mode.LIVE)
    yesterday_exit = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)  # piątek
    engine.journal.record_trade(trade("-3.10", yesterday_exit), "LOSS-1", date(2026, 10, 30))  # kill zgubiony w awarii
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert engine.kill.level() is KillLevel.HARD and "DAILY_LOSS_2X" in kill_reasons(engine.journal)
    assert "open_position" not in broker.calls


# --- zatrzymanie, anulowanie, dzień bez sesji -----------------------------------------------------------------


async def test_shutdown_flag_stops_cleanly_with_a_position_left_protected(engine, broker, clock, caplog):
    eleven = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
    seen = {}

    def shut():
        engine.request_shutdown()
        seen["calls"] = len(broker.calls)

    at_time(broker, clock, eleven, shut)
    with caplog.at_level(logging.INFO, logger="gwstock.engine"):
        rep = await engine.run_day(DAY)
    assert rep.interrupted and rep.final_position_state is P.OPEN
    assert len(broker.calls) == seen["calls"]  # po prośbie ani jednego wywołania brokera
    assert broker.positions["DEAL-1"].stop_level == STOP_LEVEL and clock.now_utc() < eleven + timedelta(seconds=10)
    assert any("DEAL-1" in r.getMessage() and "stop" in r.getMessage() for r in caplog.records)


async def test_cancellation_propagates_and_leaves_the_position_protected(engine, broker, clock, caplog):
    def cancel():
        raise asyncio.CancelledError()

    at_time(broker, clock, datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc), cancel)
    with pytest.raises(asyncio.CancelledError):
        await engine.run_day(DAY)
    assert position(engine).state is P.OPEN and broker.positions["DEAL-1"].stop_level == STOP_LEVEL


async def test_leftover_position_on_a_non_session_day_is_flattened(make_engine, broker, clock):
    first = make_engine(Mode.LIVE)
    at_time(broker, clock, datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc), lambda: die())
    with pytest.raises(ProcessDied):
        await first.run_day(DAY)
    saturday = date(2026, 11, 7)
    clock.advance((berlin(saturday, 10, 0) - clock.now_utc()).total_seconds())
    second = make_engine.restart(first, Mode.LIVE)
    rep = await second.run_day(saturday)
    assert rep.skip_reason == "NOT_SESSION" and rep.final_position_state is P.FLAT
    assert "close_position" in broker.calls and len(second.journal.trades()) == 1


async def test_live_engine_without_booker_refuses_to_start(make_engine, broker, clock, risk_cfg, kill_sink):
    probe = make_engine(Mode.LIVE)
    bare = Reconciler(probe.journal, broker, clock, kill_sink, risk_cfg, "DE40")  # bez księgującego
    engine = make_engine(Mode.LIVE, journal=probe.journal, reconciler=bare)
    with pytest.raises(EngineMisconfigured, match="booker"):
        await engine.run_day(DAY)
    with pytest.raises(EngineMisconfigured):
        await engine.run_forever()
    assert broker.calls == []
    dry = make_engine(Mode.DRY_RUN, journal=probe.journal, reconciler=bare)  # dry_run: nic do księgowania
    stop_engine_at(dry, clock, berlin(DAY, 9, 0))
    await dry.run_day(DAY)


# --- run_forever ----------------------------------------------------------------------------------------------


async def test_run_forever_runs_consecutive_days_and_holds_the_lock(engine, broker, clock, data_dir):
    tuesday = date(2026, 11, 3)
    broker.set_daily_candles(daily_candles(DAY) + daily_candles(tuesday, count=1))
    later = five_minute_candles(tuesday, D("25780"), D("25790"))
    broker.set_candles_5m(five_minute_candles(DAY, D("25780"), D("25790")) + later)
    seen = {}

    def check_lock():
        try:
            InstanceLock(data_dir / "engine.lock").acquire()
        except AlreadyRunning:
            seen["held"] = True

    at_time(broker, clock, datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc), check_lock)
    stop_engine_at(engine, clock, berlin(tuesday, 9, 20))
    await engine.run_forever()
    assert seen["held"]
    days = [d["day"] for d in engine_decisions(engine.journal)]
    assert days == ["2026-11-02", "2026-11-03"]
    assert len(engine.journal.trades()) == 1  # poniedziałek spłaszczony i zaksięgowany
    assert engine.journal.get_order("gws-20261103-DE40-1-entry").state is OrderState.CONFIRMED  # wtorek: nowe wejście
    lock = InstanceLock(data_dir / "engine.lock")  # zwolniona po zatrzymaniu
    lock.acquire()
    lock.release()


async def test_run_forever_survives_an_exception_in_a_day(engine, broker, clock, notifier, monkeypatch):
    real = engine.run_day
    state = {"n": 0}

    async def flaky(day):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("bug in the day")
        return await real(day)

    monkeypatch.setattr(engine, "run_day", flaky)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_forever()
    assert state["n"] == 2 and alerts(notifier, "bug in the day")
    assert len(engine_decisions(engine.journal)) == 1


async def test_run_forever_refuses_a_second_instance(engine, broker, data_dir):
    with InstanceLock(data_dir / "engine.lock"):
        with pytest.raises(AlreadyRunning):
            await engine.run_forever()
    assert broker.calls == []
