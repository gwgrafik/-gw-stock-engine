"""Próba pasma z celem (`smoke --epic US500`, zadanie 17) na FakeBroker wielu instrumentów (DE40, US500, EURUSD).

OCZEKIWANE zachowanie (spisane przed uruchomieniem z polecenia zadania 17, projektu wielu pasm §3, §7, §7.5 i CLAUDE.md):
- warunki: proces live i pasmo włączone w `config/lanes.toml`; zamrożony odcisk pasma niepotrzebny, a tryb pasma po
  próbie bez zmian (wykonawca pasma dry_run, ten sam powód), żadnych plików uzbrojenia ani `live_allowed`;
- jedno wejście minimalną wielkością z celem i stopem (rola smoke: nie zużywa wejść dnia ani sesji pasma), bramka
  portfelowa i kurs jak przy wejściu pasma (brak kursu = brak próby, nic do brokera);
- kroki [1]-[10] PASS/FAIL, [11] INFO, blok USTALENIA O BROKERZE z POTWIERDZONE / ROZBIEŻNE / NIE USTALONO dla C5, C5d,
  C7, C8; w wydruku nigdy identyfikator deala, referencji ani rachunku;
- każda awaria w trakcie kończy się spłaszczeniem; pozycja, której nie da się zamknąć (zamknięty rynek), daje głośną
  linię; drugie zamknięcie ma dać POSITION_NOT_FOUND.

Dzień wzorcowy: poniedziałek 2026-11-02, próba o 16:00 UTC (11:00 Nowy Jork, 17:00 Berlin). US500: mid 7589,5 (bid
7589,2, ask 7589,8), świece M5 płaskie (zakres 2) od 14:50 UTC, więc ATR14 = 2: stop 2,0, cel 1,5 × 2 = 3,0.
Uzbrajanie wyłącznie `engine_sim.arm` (fabryka, katalog tymczasowy).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import gwstock.cli as cli
import gwstock.smoke_lane as smoke_lane_mod
from gwstock.app import Mode
from gwstock.broker.diagnostics import SettlementRow
from gwstock.config import load_lanes
from gwstock.fx import ESTIMATED_FX
from gwstock.smoke_lane import probe_distances, smoke_lane
from gwstock.types import BrokerRejected, BrokerTimeout, KillLevel, OrderState, PositionState, Quote
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    FX,
    US,
    SimClock,
    UsMarket,
    at_time,
    build_lane_broker,
    calls_at,
    cfgs,
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
UTC = timezone.utc
PROBE_AT = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
P = PositionState


@pytest.fixture
def clock() -> SimClock:
    return SimClock(PROBE_AT)


@pytest.fixture
def broker(clock, day_rules, market):
    return build_lane_broker(clock, day_rules, market, UsMarket())


@pytest.fixture
def lanes(config_dir):
    return load_lanes(write_lane_files(config_dir))


@pytest.fixture
def engine(make_engine, lanes):
    return make_engine(Mode.LIVE, lanes=lanes)  # pasmo bez zamrożonego odcisku: dry_run


def eur_trade_row(broker, clock, amount="-0.01", currency="EUR"):
    def add():
        broker.transactions.append(
            SettlementRow("TRADE", currency, D(amount), clock.now_utc(), "US 500", "Trade closed")
        )

    broker.on_call("close_position", add, once=True)


async def run(engine, hold_s=None) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await smoke_lane(engine, US, out=lines.append, hold_s=hold_s)
    return code, lines


def no_identifiers(lines: list[str]) -> None:
    text = "\n".join(lines)
    for token in ("DEAL-", "REF-", "ACC-", "gws-"):
        assert token not in text, token


def finding(lines: list[str], key: str) -> str:
    [line] = [x for x in lines if x.startswith(f"{key} ") and ":" in x and not x.startswith("    ")]
    return line


def block(lines: list[str]) -> list[str]:
    start = next(i for i, x in enumerate(lines) if x.startswith("USTALENIA O BROKERZE"))
    return lines[start:]


# --- odległości z konfiguracji pasma ---------------------------------------------------------------------------------


def test_probe_distances_come_from_the_lane_file_and_the_broker_rules(lanes):
    from tests.fault.engine_sim import US_RULES

    bar = lanes[0].bar
    q = Quote(US, D("7589.2"), D("7589.8"), PROBE_AT)
    assert probe_distances(US_RULES, q, D("2"), bar) == (D("2.0"), D("3.0"))
    assert probe_distances(US_RULES, q, D("2.64"), bar) == (D("2.7"), D("4.0"))  # cel w dół do kroku: 4,05 → 4,0
    # bez ATR: 2 × max(minimum brokera 0,5; 0,0001 × 7589,5) = 1,5179 → 1,6 w górę do kroku; cel 2,4
    assert probe_distances(US_RULES, q, None, bar) == (D("1.6"), D("2.4"))
    assert probe_distances(US_RULES, q, D("0.1"), bar) == (D("0.8"), D("1.2"))  # nie mniej niż 0,01% ceny


# --- przebieg wzorcowy -----------------------------------------------------------------------------------------------


async def test_the_lane_probe_passes_every_step_and_settles_every_finding(engine, broker, clock, data_dir, config_dir):
    broker.fill_slippage = D("0.1")  # wykonanie 7589,9 ≠ notowanie 7589,8: punkt odniesienia celu rozróżnialny
    eur_trade_row(broker, clock, "-0.01")
    lane = engine.lane_for(US)
    reason = lane.arming_problem
    before = sorted(p.name for p in data_dir.iterdir())
    runtime_text = (config_dir / "runtime.toml").read_text()
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    for n in range(1, 11):
        assert any(x.startswith(f"PASS [{n}]") for x in lines), n
    assert not any(x.startswith("FAIL") for x in lines)
    assert lines[lines.index(block(lines)[0]) - 1] == "PASS smoke --epic US500: 10/10 checks passed"
    assert "INFO [3] holding 20 s more: look at the position and its stop and target in the Capital.com app" in lines
    entry = next(x for x in lines if x.startswith("PASS [2] entry accepted"))
    assert "size 0.01, open level 7589.9, stop level 7587.9" in entry and "target level 7592.9" in entry
    assert "requested stop distance 2.0, target distance 3.0" in entry
    assert finding(lines, "C5").startswith("C5  POTWIERDZONE")
    assert finding(lines, "C5d").startswith("C5d POTWIERDZONE: PUT z samym stopLevel ZACHOWAŁ cel")
    assert finding(lines, "C7").startswith("C7  POTWIERDZONE: zamknięcie przez API (DELETE) ma w historii źródło USER")
    assert finding(lines, "C8").startswith("C8  POTWIERDZONE: wynik rozliczony w EUR: -0.01 EUR; szacunek ESTIMATED_FX")
    assert any("punkt odniesienia profitDistance: cena wykonania" in x for x in lines)
    no_identifiers(lines)
    # broker: jedno wejście z celem, ochrona obu poziomów dwa razy, sam stop raz, wszystko zamknięte
    assert broker.calls.count("open_position") == 1 and broker.positions == {}
    assert broker.calls.count("set_protection") == 2 and broker.calls.count("set_stop") == 1
    assert broker.calls.count("close_position") == 2  # spłaszczenie + celowe drugie zamknięcie
    j = engine.journal
    order = j.get_order("gws-20261102-US500-1-smoke")
    assert order is not None and order.state is OrderState.CONFIRMED
    assert j.count_entry_orders(DAY, US) == 0 and j.count_smoke_trades(US) == 1 and lane.session_index(j) == 0
    [(deal, trade)] = j.trades_by_deal(US)
    fx = j.trade_fx(deal)
    assert fx is not None and fx.source == ESTIMATED_FX and j.pnl_pending_deals() == []
    assert j.current_position(US).state is P.FLAT and engine.kill.level() is KillLevel.NONE
    # tryb pasma bez zmian, nic nie uzbrojone
    assert engine.lane_for(US) is lane and lane.executor.live is False and lane.arming_problem == reason
    created = {p.name for p in data_dir.iterdir()} - set(before)
    assert created <= {"alerts.log", "heartbeat.json"}, created  # żadnych plików uzbrojenia
    assert not (data_dir / ("L1" + "_PASSED")).exists()
    assert (config_dir / "runtime.toml").read_text() == runtime_text


async def test_the_findings_block_names_each_assumption_explicitly(engine, broker, clock):
    code, lines = await run(engine)
    assert code == 0
    tail = block(lines)
    assert tail[0].startswith("USTALENIA O BROKERZE (US500, waluta instrumentu USD, rachunek EUR")
    keys = [x.split()[0] for x in tail[1:] if not x.startswith("    ")]
    assert keys == ["C5", "C5d", "C7", "C8"]
    for x in tail[1:]:
        if not x.startswith("    "):
            assert x.split()[1].rstrip(":") in ("POTWIERDZONE", "ROZBIEŻNE", "NIE")
    # wykonanie = notowanie decyzji: punktu odniesienia nie da się tu rozróżnić (uczciwie: NIE USTALONO)
    assert finding(lines, "C5").startswith("C5  NIE USTALONO")
    assert finding(lines, "C8").startswith("C8  NIE USTALONO: brak wiersza TRADE")
    no_identifiers(lines)


async def test_the_default_hold_is_twenty_seconds_and_short_holds_are_raised(engine, broker, clock):
    opened = calls_at(broker, clock, "open_position")
    closes = calls_at(broker, clock, "close_position")
    code, lines = await run(engine, hold_s=0)
    assert code == 0
    assert "INFO [0] hold raised from 0 s to 15 s so the target fields can be seen" in lines
    assert (closes[0] - opened[0]).total_seconds() >= 15


# --- warunki ---------------------------------------------------------------------------------------------------------


async def test_dry_run_process_refuses_and_sends_nothing(make_engine, lanes, broker):
    engine = make_engine(Mode.DRY_RUN, lanes=lanes)
    code, lines = await run(engine)
    assert code == 1 and "dry_run" in lines[0] and broker.calls == []


async def test_an_instrument_without_an_enabled_lane_is_refused(make_engine, broker):
    engine = make_engine(Mode.LIVE)  # bez config/lanes.toml
    code, lines = await run(engine)
    assert code == 1 and lines == [
        "FAIL smoke --epic US500: no enabled lane for US500 in config/lanes.toml; nothing was sent"
    ]
    lines = []
    assert await smoke_lane(engine, "DE40", out=lines.append) == 1
    assert "run smoke without --epic" in lines[0] and broker.calls == []


async def test_no_fx_quote_means_no_probe(engine, broker):
    def no_rate(now):
        raise BrokerRejected("EPIC_NOT_FOUND")

    broker.set_quote_fn(no_rate, epic=FX)
    code, lines = await run(engine)
    assert code == 1 and "open_position" not in broker.calls
    assert any(x.startswith("FAIL [2] no valid quote of the portfolio FX pair") for x in lines)
    assert finding(lines, "C5").startswith("C5  NIE USTALONO")


async def test_a_stale_fx_rate_is_blocked_by_the_gate_before_the_broker(engine, broker, clock):
    broker.set_quote_fn(lambda now: Quote(FX, D("1.08"), D("1.0802"), now - timedelta(hours=1)), epic=FX)
    code, lines = await run(engine)
    assert code == 1 and "open_position" not in broker.calls
    assert any("FX_UNAVAILABLE" in x for x in lines if x.startswith("FAIL [2]"))


async def test_the_kill_switch_blocks_the_probe(engine, broker):
    engine.kill.soft("STOP_FILE")
    code, lines = await run(engine)
    assert code == 1 and "open_position" not in broker.calls
    assert any("KILL" in x for x in lines if x.startswith("FAIL [2]"))


async def test_an_open_lane_position_refuses_the_probe(engine, broker):
    from gwstock.types import OpenPosition, Side

    broker.positions["X-1"] = OpenPosition("X-1", US, Side.BUY, D("0.01"), D("7589"), D("7580"), PROBE_AT)
    code, lines = await run(engine)
    assert code == 1 and "open_position" not in broker.calls
    assert any("smoke refuses" in x for x in lines)


# --- awarie u brokera ------------------------------------------------------------------------------------------------


async def test_a_refused_target_rejects_the_entry_and_leaves_nothing_open(engine, broker):
    broker.reject_next_entry("error.invalid.takeprofit.minvalue: 7590.5")
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    [fail] = [x for x in lines if x.startswith("FAIL [2]")]
    assert "REJECTED" in fail and "takeprofit" in fail
    assert engine.journal.current_position(US) is None or engine.journal.current_position(US).state is P.FLAT
    no_identifiers(lines)


async def test_a_closed_market_at_entry_sends_nothing_that_stays_open(engine, broker):
    broker.set_market_closed(True, epic=US)
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    assert any(x.startswith("FAIL [2] entry not confirmed") for x in lines)


async def test_missing_target_fields_are_reported_as_a_divergence(engine, broker):
    broker.target_keys = ()  # API nie pokazuje celu ani w pozycji, ani w potwierdzeniu
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    assert finding(lines, "C5").startswith("C5  ROZBIEŻNE")
    assert any("GET /positions/{dealId}: brak kluczy celu" in x for x in lines)
    assert finding(lines, "C5d").startswith("C5d NIE USTALONO: API nie pokazuje celu")
    # cel o krok bliżej (7592,8 → 7592,7): prawdziwa zmiana dla PUT; API go nie pokazuje, więc tylko opis
    assert any("target 7592.7 (not shown by the API" in x for x in lines if x.startswith("PASS [5]"))
    assert broker.positions == {}


async def test_other_target_key_names_are_a_divergence(engine, broker):
    broker.target_keys = ("limitLevel",)
    code, lines = await run(engine)
    assert finding(lines, "C5").startswith("C5  ROZBIEŻNE")
    assert any("inne niż profitLevel/profitDistance" in x for x in lines)
    assert broker.positions == {}


async def test_set_stop_clearing_the_target_is_found_and_the_target_restored(engine, broker):
    broker.set_stop_clears_target = True
    seen: list = []
    broker.on_call("set_protection", lambda: seen.append(dict(broker.positions)))
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    assert finding(lines, "C5d").startswith("C5d ROZBIEŻNE: PUT z samym stopLevel SKASOWAŁ cel")
    restore = seen[-1]
    [pos] = restore.values()
    assert pos.target_level is None  # tuż przed przywróceniem: cel skasowany przez set_stop
    assert any(x.startswith("PASS [6]") and "target after it: removed" in x for x in lines)
    assert broker.positions == {}


async def test_a_refused_protection_change_fails_the_step_and_still_flattens(engine, broker):
    broker.fail_next("set_protection", BrokerRejected("error.invalid.stoploss.maxvalue"))
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    assert any(x.startswith("FAIL [5] set_protection") for x in lines)
    assert any(x.startswith("PASS [7]") for x in lines)
    no_identifiers(lines)


async def test_a_closed_market_at_the_flatten_is_loud_and_the_stop_stays(engine, broker):
    broker.on_call("close_position", lambda: broker.set_market_closed(True, epic=US), once=True)
    code, lines = await run(engine)
    assert code == 1
    assert any(x.startswith("FAIL [7] LOUD: the probe position left open with its stop") for x in lines)
    [pos] = broker.positions.values()
    assert pos.stop_level is not None
    no_identifiers(lines)


async def test_an_unexpected_error_mid_probe_flattens_and_fails(engine, broker, monkeypatch):
    async def boom(self, before):
        raise RuntimeError("bug in the probe")

    monkeypatch.setattr(smoke_lane_mod._Probe, "_c5d_step", boom)
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    assert any("probe interrupted (RuntimeError)" in x for x in lines)
    assert any(x.startswith("INFO [7] the probe position closed after the interrupt") for x in lines)
    assert lines[-1].startswith("C8") or any(x.startswith("USTALENIA") for x in lines)


@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt], ids=["cancelled", "keyboard"])
async def test_an_interrupt_during_the_hold_closes_the_probe_position(engine, broker, clock, exc):
    def raise_it():
        raise exc()

    at_time(broker, clock, PROBE_AT + timedelta(seconds=10), raise_it)
    lines: list[str] = []
    with pytest.raises(exc):
        await smoke_lane(engine, US, out=lines.append)
    assert broker.positions == {} and "close_position" in broker.calls
    assert any(x.startswith("USTALENIA O BROKERZE") for x in lines)
    no_identifiers(lines)


async def test_a_target_hit_during_the_hold_confirms_the_tp_source(engine, broker, clock):
    def hit():
        for deal in list(broker.positions):
            broker.trigger_target(deal)

    at_time(broker, clock, PROBE_AT + timedelta(seconds=8), hit)
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    assert finding(lines, "C7").startswith("C7  POTWIERDZONE: cel zamknął pozycję: źródło TP")
    assert any(x.startswith("INFO [4] diagnostics skipped") for x in lines)
    [(_, trade)] = engine.journal.trades_by_deal(US)
    assert trade.exit_reason == "TARGET"


async def test_an_entry_adopted_after_a_silent_confirmation_is_shown_and_still_closed(engine, broker):
    for _ in range(2):
        broker.fail_next("confirm", BrokerTimeout())
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    assert any(x.startswith("WARN [2]: entry adopted by reconciliation") for x in lines)
    assert "WARN [2]" in lines[lines.index(block(lines)[0]) - 1] and broker.positions == {}
    no_identifiers(lines)


async def test_an_entry_with_an_unknown_outcome_never_leaves_a_position_open(engine, broker):
    def blind_after_the_send():  # wysyłka przyjęta, ale ani potwierdzenie, ani lista pozycji nie odpowiadają
        for _ in range(4):
            broker.fail_next("confirm", BrokerTimeout("GET /confirms: timeout"))
            broker.fail_next("list_positions", BrokerTimeout("GET /positions: timeout"))

    broker.on_call("open_position", blind_after_the_send, once=True)
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}, "\n".join(lines)
    assert any(x.startswith("FAIL [2] entry not confirmed: UNKNOWN") for x in lines)
    assert "INFO [2] the entry was adopted by reconciliation and closed again; the probe stops here" in lines
    assert not any("LOUD" in x for x in lines)
    no_identifiers(lines)


async def test_a_usd_settlement_is_a_divergence(engine, broker, clock):
    eur_trade_row(broker, clock, "-0.01", "USD")
    code, lines = await run(engine)
    assert finding(lines, "C8").startswith("C8  ROZBIEŻNE: wynik rozliczony w walucie instrumentu")


async def test_a_second_close_that_is_accepted_is_loud(engine, broker, monkeypatch, notifier):
    real = broker.close_position
    seen = {"n": 0}

    async def close(deal_id):
        seen["n"] += 1
        return await real(deal_id) if seen["n"] == 1 else None

    monkeypatch.setattr(broker, "close_position", close)
    code, lines = await run(engine)
    assert code == 1 and engine.kill.level() is KillLevel.SOFT
    assert any(x.startswith("FAIL [10] LOUD: a second close gave accepted (!)") for x in lines)
    no_identifiers(lines)


# --- polecenie -------------------------------------------------------------------------------------------------------


def test_the_epic_option_dispatches_to_the_lane_probe(monkeypatch, tmp_path):
    seen: list[tuple] = []

    async def fake_smoke_engine(paths, hold_s=0, *rest, **kw):
        seen.append((hold_s, rest, kw))
        return 0

    monkeypatch.setattr(cli, "_smoke_engine", fake_smoke_engine)
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path))
    assert cli.main(["smoke", "--epic", "US500"]) == 0
    assert cli.main(["smoke", "--epic", "US500", "--hold", "45"]) == 0
    assert seen == [(None, (), {"epic": "US500"}), (45, (), {"epic": "US500"})]


@pytest.mark.parametrize("value", ["US 500", "-x", ""])
def test_a_malformed_epic_is_refused(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["smoke", "--epic", value])
    assert exc.value.code == 2


async def test_the_smoke_command_runs_the_lane_probe_with_the_stop_handlers(engine, monkeypatch, tmp_path):
    import signal

    async def fake_with_engine(paths, action, *, lock=None):
        return await action(engine)

    async def fake_lane(eng, epic, out=print, hold_s=None):
        installed = signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None)
        return 0 if installed and epic == US and hold_s == 30 else 1

    monkeypatch.setattr(cli, "_with_engine", fake_with_engine)
    monkeypatch.setattr(smoke_lane_mod, "smoke_lane", fake_lane)
    paths = cli.Paths(tmp_path / "c", tmp_path / "d", tmp_path / "k.json")
    (tmp_path / "d").mkdir()
    assert await cli._smoke_engine(paths, 30, epic=US) == 0


# --- przerwanie w trakcie wejścia (przegląd S1) ----------------------------------------------------------------------
#
# OCZEKIWANE: przerwanie (CancelledError, KeyboardInterrupt) albo błąd dziennika między wysłaniem wejścia a końcem
# `_enter` (także w `_resolve_unknown` i w jego spłaszczeniu) kończy się zamknięciem pozycji próby albo głośną linią
# LOUD, a wyjątek BaseException idzie dalej; po pierwszym Ctrl-C/SIGTERM (`shutdown_requested`) zlecenie nie wychodzi.


def raiser(exc):
    def raise_it():
        raise exc()

    return raise_it


def blind_after_the_send(broker):
    """Wysyłka przyjęta, ale ani potwierdzenie, ani lista pozycji nie odpowiadają (wynik wejścia UNKNOWN)."""

    def blind():
        for _ in range(4):
            broker.fail_next("confirm", BrokerTimeout("GET /confirms: timeout"))
            broker.fail_next("list_positions", BrokerTimeout("GET /positions: timeout"))

    broker.on_call("open_position", blind, once=True)


@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt], ids=["cancelled", "keyboard"])
async def test_an_interrupt_at_the_confirm_right_after_the_send_closes_the_probe_position(engine, broker, exc):
    broker.on_call("confirm", raiser(exc), once=True)
    lines: list[str] = []
    with pytest.raises(exc):
        await smoke_lane(engine, US, out=lines.append)
    assert "open_position" in broker.calls and "close_position" in broker.calls, "\n".join(lines)
    assert broker.positions == {}, "\n".join(lines)
    assert f"FAIL [2] entry interrupted ({exc.__name__}); resolving the entry outcome now" in lines
    assert "INFO [2] the entry was adopted by reconciliation and closed again; the probe stops here" in lines
    assert not any("LOUD" in x for x in lines) and any(x.startswith("USTALENIA O BROKERZE") for x in lines)
    no_identifiers(lines)


async def test_an_interrupted_entry_that_cannot_be_resolved_is_loud(engine, broker):
    def blind():
        for _ in range(40):
            broker.fail_next("list_positions", BrokerTimeout("GET /positions: timeout"))
        raise asyncio.CancelledError()

    broker.on_call("confirm", blind, once=True)
    lines: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await smoke_lane(engine, US, out=lines.append)
    assert "close_position" not in broker.calls  # nic na ślepo
    [loud] = [x for x in lines if "LOUD" in x]
    assert loud.startswith("FAIL [2] LOUD: the entry outcome is unknown"), loud
    assert "check the Capital.com app" in loud
    no_identifiers(lines)


async def test_a_stop_request_before_the_entry_sends_nothing(engine, broker):
    engine.request_shutdown()
    code, lines = await run(engine)
    assert code == 1 and "open_position" not in broker.calls
    assert "FAIL [2] stop requested (signal) before the entry: nothing was sent" in lines


async def test_an_interrupt_while_resolving_an_unknown_entry_still_closes_the_position(engine, broker):
    blind_after_the_send(broker)
    broker.on_call("close_position", raiser(asyncio.CancelledError), once=True)  # spłaszczenie po przyjęciu przerwane
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}, "\n".join(lines)
    assert any(x.startswith("INFO [2] the probe position closed after the interrupt") for x in lines), "\n".join(lines)
    no_identifiers(lines)


async def test_a_journal_error_while_resolving_an_unknown_entry_still_closes_the_position(engine, broker, monkeypatch):
    from gwstock.journal import JournalWriteError

    blind_after_the_send(broker)

    async def broken(self):
        raise JournalWriteError("disk full")

    monkeypatch.setattr(smoke_lane_mod._Probe, "_flatten_position", broken)
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}, "\n".join(lines)
    assert any(x.startswith("INFO [2] the probe position closed after the interrupt") for x in lines), "\n".join(lines)
    no_identifiers(lines)


# --- wątpliwość w trakcie wstrzymania (przegląd S2) ------------------------------------------------------------------
#
# OCZEKIWANE: wstrzymanie przerwane wątpliwością (pierwszy sygnał zatrzymania, kill switch także HARD) pomija
# diagnostykę [4]-[6] (żadnych zmian ochrony ani odczytów diagnostycznych) i spłaszcza od razu (FAIL [4]); przy
# zatrzymaniu na żądanie po spłaszczeniu pomija też odczyty historii transakcji [11] (C8 NIE USTALONO).


async def test_a_stop_request_during_the_hold_skips_the_diagnostics_and_flattens_at_once(engine, broker, clock):
    opened = calls_at(broker, clock, "open_position")
    closes = calls_at(broker, clock, "close_position")
    at_time(broker, clock, PROBE_AT + timedelta(seconds=8), engine.request_shutdown)
    code, lines = await run(engine)
    assert code == 1, "\n".join(lines)
    for method in ("diagnose_target_fields", "set_protection", "set_stop", "diagnose_transactions"):
        assert method not in broker.calls, method
    assert broker.positions == {} and (closes[0] - opened[0]).total_seconds() < 15
    assert "FAIL [4] diagnostics skipped: hold stopped (stop requested (signal)); flattening at once" in lines
    assert any(x.startswith("PASS [7]") for x in lines)
    assert "INFO [11] skipped: stop requested; read the transaction history in the Capital.com app" in lines
    assert finding(lines, "C5d").startswith("C5d NIE USTALONO")
    assert finding(lines, "C8").startswith("C8  NIE USTALONO")
    no_identifiers(lines)


async def test_a_hard_kill_during_the_hold_skips_the_diagnostics_and_flattens_at_once(engine, broker, clock):
    at_time(broker, clock, PROBE_AT + timedelta(seconds=8), lambda: engine.kill.hard("HARD_STOP_FILE"))
    code, lines = await run(engine)
    assert code == 1, "\n".join(lines)
    for method in ("diagnose_target_fields", "set_protection", "set_stop"):
        assert method not in broker.calls, method
    assert broker.positions == {}
    assert any(x.startswith("FAIL [4] diagnostics skipped: hold stopped (kill switch HARD") for x in lines), lines


# --- uczciwość ustalenia C5d (przegląd S3) ---------------------------------------------------------------------------
#
# OCZEKIWANE: `set_stop` w [6] wysyła stop o jeden krok BLIŻEJ ceny (prawdziwa zmiana dla PUT; nie dalej od ceny), gdy
# nadal spełnia minimalną odległość brokera, a przywrócenie wraca do odczytanego poziomu; bez przesunięcia ustalenie
# mówi wprost „dla PUT bez zmiany stopu”. Punkt odniesienia celu to świeży odczyt tuż przed `set_stop`; po nieudanym
# [5] C5d to NIE USTALONO. Nieudane przywrócenie celu: [7] i tak zamyka, a linia LOUD nie twierdzi, że cel został.


def recording(monkeypatch, broker, method):
    """Zapisuje argumenty wywołań `method` brokera (`calls` trzyma tylko nazwy)."""
    real = getattr(broker, method)
    seen: list[tuple] = []

    async def wrapper(*args):
        seen.append(args)
        return await real(*args)

    monkeypatch.setattr(broker, method, wrapper)
    return seen


async def test_set_stop_moves_the_stop_one_step_closer_and_the_restore_puts_it_back(engine, broker, monkeypatch):
    stops = recording(monkeypatch, broker, "set_stop")
    protections = recording(monkeypatch, broker, "set_protection")
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    [(_, moved)] = stops
    assert moved == D("7587.9")  # stop 7587,8 o krok 0,1 bliżej ceny (BUY: w górę)
    assert protections[-1][1] == D("7587.8")  # przywrócony odczytany poziom
    assert finding(lines, "C5d") == "C5d POTWIERDZONE: PUT z samym stopLevel ZACHOWAŁ cel (stop zmieniony o krok)"
    assert any(x.startswith("PASS [6] set_stop with the stop only 7587.9 (one step closer than 7587.8)")
               for x in lines), "\n".join(lines)  # fmt: skip


async def test_an_unchanged_stop_is_named_in_the_c5d_finding(engine, broker, monkeypatch):
    monkeypatch.setattr(smoke_lane_mod._Probe, "_closer_stop", lambda self, stop, rules, quote: stop)
    stops = recording(monkeypatch, broker, "set_stop")
    code, lines = await run(engine)
    assert code == 0, "\n".join(lines)
    assert stops[0][1] == D("7587.8")
    assert finding(lines, "C5d") == "C5d POTWIERDZONE: dla PUT bez zmiany stopu: PUT z samym stopLevel ZACHOWAŁ cel"
    assert any(x.startswith("PASS [6] set_stop with the stop only 7587.8 (unchanged") for x in lines)


async def test_the_stop_is_not_moved_closer_than_the_broker_minimum(engine, broker, monkeypatch):
    from gwstock.types import Side

    rules = await broker.instrument_rules(US)
    probe = smoke_lane_mod._Probe(engine, US, lambda _: None)
    probe.side = Side.BUY
    q = Quote(US, D("7589.2"), D("7589.8"), PROBE_AT)
    assert probe._closer_stop(D("7587.8"), rules, q) == D("7587.9")
    assert probe._closer_stop(D("7588.6"), rules, q) == D("7588.6")  # 7588,7 byłoby 0,5 od bid: za blisko
    assert probe._closer_stop(D("7588.5"), rules, q) == D("7588.6")  # 0,6 od bid: minimum 0,5 + krok
    probe.side = Side.SELL
    assert probe._closer_stop(D("7591.8"), rules, q) == D("7591.7")
    assert probe._closer_stop(D("7590.4"), rules, q) == D("7590.4")


async def test_a_set_stop_timeout_leaves_c5d_unknown_and_restores_both_levels(engine, broker, monkeypatch):
    broker.fail_next("set_stop", BrokerTimeout("PUT timeout"))
    protections = recording(monkeypatch, broker, "set_protection")
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    assert finding(lines, "C5d") == "C5d NIE USTALONO: set_stop z samym stopem nieudane (BrokerTimeout: PUT timeout)"
    assert any(x.startswith("FAIL [6]") and "set_protection restored: stop 7587.8 (confirmed" in x for x in lines)
    assert len(protections) == 2 and protections[-1][1:] == (D("7587.8"), D("7592.7"))
    no_identifiers(lines)


async def test_a_protection_change_accepted_despite_a_timeout_never_reads_as_a_changed_target(engine, broker):
    broker.fail_next("set_protection", BrokerTimeout("PUT timeout"), applied=True)  # u brokera: cel o krok bliżej
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    assert any(x.startswith("FAIL [5] set_protection") for x in lines)
    c5d = finding(lines, "C5d")
    assert c5d.startswith("C5d NIE USTALONO: krok [5] nieudany"), c5d
    # punkt odniesienia to świeży odczyt tuż przed set_stop (7592,7), nie poziom sprzed nieudanego [5] (7592,8)
    assert any(x.startswith("PASS [6]") and "target after it: kept" in x for x in lines), "\n".join(lines)
    assert any("cel po zmianie stopu: 7592.7 (przed: 7592.7)" in x for x in lines), "\n".join(lines)


def fail_the_target_restore(broker):
    """Drugi `set_protection` (przywrócenie celu w [6]) odrzucony; `set_stop` przed nim kasuje cel."""
    seen = {"n": 0}

    def second_fails():
        seen["n"] += 1
        if seen["n"] == 2:
            broker.fail_next("set_protection", BrokerRejected("error.invalid.takeprofit"))

    broker.set_stop_clears_target = True
    broker.on_call("set_protection", second_fails)


async def test_a_failed_target_restore_still_flattens_the_position_left_with_its_stop_only(engine, broker):
    fail_the_target_restore(broker)
    left: list = []
    broker.on_call("close_position", lambda: left.extend(broker.positions.values()), once=True)
    code, lines = await run(engine)
    assert code == 1 and broker.positions == {}
    [pos] = left  # tuż przed zamknięciem: stop jest, celu nie ma
    assert pos.stop_level is not None and pos.target_level is None
    assert any(x.startswith("FAIL [6]") and "set_protection restored: failed" in x for x in lines)
    assert any(x.startswith("PASS [7]") for x in lines)


async def test_a_failed_target_restore_and_a_failed_flatten_say_the_target_may_be_gone(engine, broker):
    fail_the_target_restore(broker)
    broker.on_call("close_position", lambda: broker.set_market_closed(True, epic=US), once=True)
    code, lines = await run(engine)
    assert code == 1
    [loud] = [x for x in lines if x.startswith("FAIL [7] LOUD")]
    assert "(and target)" not in loud and "its target may be gone" in loud, loud
    [pos] = broker.positions.values()
    assert pos.stop_level == D("7587.9")  # stop z [6] (o krok bliżej ceny) zostaje; cel mogło przywrócić uzgadnianie
    no_identifiers(lines)


async def test_an_interrupt_after_set_stop_never_claims_the_target(engine, broker):
    broker.set_stop_clears_target = True
    seen = {"n": 0}

    def interrupt_the_restore():
        seen["n"] += 1
        if seen["n"] == 2:
            broker.set_market_closed(True, epic=US)  # zamknięcie po przerwaniu też się nie uda
            raise asyncio.CancelledError()

    broker.on_call("set_protection", interrupt_the_restore)
    lines: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await smoke_lane(engine, US, out=lines.append)
    [loud] = [x for x in lines if "LOUD" in x]
    assert loud.startswith("FAIL [7] LOUD: the probe position left open with its stop (its target may be gone"), loud


# --- jawny null w polach celu (przegląd S4) --------------------------------------------------------------------------
#
# OCZEKIWANE: `profitLevel: null` (jawny null) nie jest potwierdzeniem C5 ani w `GET /confirms`, ani w `GET /positions`:
# wynik NIE USTALONO (null widać w linii szczegółów), nigdy POTWIERDZONE; brak klucza zostaje ROZBIEŻNE jak dotąd.


def null_target_in(monkeypatch, broker, where):
    from dataclasses import replace as dc_replace

    real = broker.diagnose_target_fields

    async def nulled(deal_id, ref):
        report = await real(deal_id, ref)
        part = getattr(report, where)
        if part.found:
            part = dc_replace(part, target={"profitLevel": None})
        return dc_replace(report, **{where: part})

    monkeypatch.setattr(broker, "diagnose_target_fields", nulled)


async def test_an_explicit_null_target_in_the_confirm_is_not_a_confirmation(engine, broker, monkeypatch):
    broker.fill_slippage = D("0.1")  # punkt odniesienia rozróżnialny: bez nulla C5 byłoby POTWIERDZONE
    null_target_in(monkeypatch, broker, "confirm")
    code, lines = await run(engine)
    assert finding(lines, "C5").startswith("C5  NIE USTALONO"), finding(lines, "C5")
    assert "    - GET /confirms/{dealReference}: klucze celu profitLevel=null (jawny null: nie potwierdza celu)" in lines


async def test_an_explicit_null_target_in_the_position_is_not_a_confirmation(engine, broker, monkeypatch):
    broker.fill_slippage = D("0.1")
    null_target_in(monkeypatch, broker, "position")
    code, lines = await run(engine)
    assert finding(lines, "C5").startswith("C5  NIE USTALONO"), finding(lines, "C5")
    assert "    - GET /positions/{dealId}: klucze celu profitLevel=null (jawny null: nie potwierdza celu)" in lines


# --- logi próby bez identyfikatorów (przegląd S5) --------------------------------------------------------------------
#
# OCZEKIWANE: logi, które próba pasma pisze w tym samym terminalu co wydruk (ostrzeżenia ścieżki zamknięcia po
# przerwaniu, z tekstem wyjątku brokera), nie zawierają identyfikatora deala ani referencji; stderr też nie.


async def test_the_probe_logs_carry_no_deal_identifiers(engine, broker, clock, monkeypatch, caplog, capsys):
    import logging

    async def failing_close(deal_id):
        broker.calls.append("close_position")
        raise BrokerTimeout(f"DELETE /positions/{deal_id}: timeout")

    def interrupt():
        monkeypatch.setattr(broker, "close_position", failing_close)
        raise asyncio.CancelledError()

    at_time(broker, clock, PROBE_AT + timedelta(seconds=10), interrupt)
    caplog.set_level(logging.DEBUG)
    lines: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await smoke_lane(engine, US, out=lines.append)
    assert any(x.startswith("FAIL [7] LOUD") for x in lines)
    assert "smoke direct close failed: BrokerTimeout: DELETE /positions/<deal>: timeout" in caplog.text
    err = capsys.readouterr().err
    for token in ("DEAL-", "REF-"):
        assert token not in caplog.text, caplog.text
        assert token not in err
    no_identifiers(lines)
