"""Tryby i polecenia właściciela (spec §9, §13): uzbrajanie live, kontrola rachunku przy starcie, dry_run bez zleceń,
`bind-account`, `smoke` (L0), `status`, `health`, rozdział poleceń CLI, leniwy import pre-rejestracji i to, że kod
nigdy nie tworzy plików uzbrojenia (`ARM_LIVE`, `L1_PASSED`).

Ten plik zawiera nazwy plików uzbrojenia wyłącznie jako wzorce wyszukiwania w źródłach (zwolniony w guard_live).
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import gwstock.cli as cli
from gwstock.app import Mode
from gwstock.broker.fake import FakeBroker
from gwstock.config import RuntimeConfig, load_runtime
from gwstock.journal import Journal
from gwstock.types import AccountInfo, BrokerError, BrokerRejected, Candle, KillLevel, OrderState, PositionState
from gwstock.watchdog import AlreadyRunning, InstanceLock
from tests.fault.engine_sim import (  # noqa: F401  (fixtures)
    DAY,
    DECISION_UTC,
    MID,
    D,
    at_time,
    berlin,
    broker,
    calls_at,
    cfgs,
    clock,
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
)

P = PositionState
SRC = Path(__file__).resolve().parents[2] / "src"
ARMING_NAMES = ("ARM" + "_LIVE", "L1" + "_PASSED")  # sklejone, żeby ten plik nie zawierał gotowej nazwy w kodzie


SECRETS = json.dumps({"api_key": "KEYKEYKEY1", "identifier": "owner@example.com", "api_password": "pw-secret"})


def alerts(notifier, text):
    return [(level, message) for level, message in notifier.alerts if text in message]


# --- uzbrajanie: wszystkie cztery warunki naraz --------------------------------------------------------------

ARMED = dict(
    runtime=RuntimeConfig(True, "ACC-1"), arm_file_exists=True, config_fingerprint="f1", frozen_fingerprint="f1"
)


def test_live_requires_arm_file_live_allowed_bound_account_and_matching_fingerprint():
    assert cli.resolve_mode(**ARMED) is Mode.LIVE
    assert cli.mode_decision(**ARMED) == (Mode.LIVE, [])


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        (dict(runtime=RuntimeConfig(False, "ACC-1")), "live_allowed"),
        (dict(arm_file_exists=False), "ARM_LIVE"),
        (dict(runtime=RuntimeConfig(True, None)), "bind-account"),
        (dict(frozen_fingerprint=None), "no frozen pre-registration"),
        (dict(config_fingerprint=None), "cannot be computed"),
        (dict(config_fingerprint="f2"), "differs"),
    ],
    ids=["live_allowed", "arm_file", "account", "frozen_missing", "prereg_unavailable", "fingerprint_mismatch"],
)
def test_any_missing_arming_condition_means_dry_run_with_the_reason(over, reason):
    mode, missing = cli.mode_decision(**{**ARMED, **over})
    assert mode is Mode.DRY_RUN and cli.resolve_mode(**{**ARMED, **over}) is Mode.DRY_RUN
    assert len(missing) == 1 and reason in missing[0]


def test_truthy_values_that_are_not_true_never_arm():
    assert cli.resolve_mode(**{**ARMED, "arm_file_exists": 1}) is Mode.DRY_RUN
    assert cli.resolve_mode(**{**ARMED, "runtime": RuntimeConfig(1, "ACC-1")}) is Mode.DRY_RUN


def write_prereg(data_dir: Path, fingerprint: str) -> None:
    (data_dir / "preregistration.json").write_text(json.dumps({"fingerprint": fingerprint}))


@pytest.fixture
def armed_dirs(tmp_path):
    config_dir, data = tmp_path / "config", tmp_path / "data"
    config_dir.mkdir()
    data.mkdir()
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml"):
        (config_dir / name).write_text(Path("config", name).read_text())
    (config_dir / "runtime.toml").write_text("live_allowed = true\n")
    (data / "account.json").write_text(json.dumps({"account_id": "ACC-1"}))
    (data / ARMING_NAMES[0]).write_text("")  # w teście tworzy go „właściciel"
    return config_dir, data


def fake_prereg(monkeypatch, value="fp-123"):
    module = types.ModuleType("gwstock.prereg")
    module.fingerprint = lambda paths, adapter, instrument_class: f"{value}" if adapter == "capitalcom-live" else "?"
    monkeypatch.setitem(sys.modules, "gwstock.prereg", module)
    return module


def test_prereg_module_missing_means_dry_run_lazy_fallback(armed_dirs, monkeypatch):
    config_dir, data = armed_dirs
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)  # import → ImportError (Zadanie 15 jeszcze nie istnieje)
    write_prereg(data, "fp-123")
    assert cli.config_fingerprint(config_dir) is None
    mode, missing = cli.runtime_mode(config_dir, data, load_runtime(config_dir, data))
    assert mode is Mode.DRY_RUN and "cannot be computed" in missing[0]


def test_prereg_fingerprint_matching_the_frozen_file_arms(armed_dirs, monkeypatch):
    config_dir, data = armed_dirs
    fake_prereg(monkeypatch)
    write_prereg(data, "fp-123")
    assert cli.runtime_mode(config_dir, data, load_runtime(config_dir, data)) == (Mode.LIVE, [])
    write_prereg(data, "fp-other")
    assert cli.runtime_mode(config_dir, data, load_runtime(config_dir, data))[0] is Mode.DRY_RUN
    (data / "preregistration.json").write_text("{broken")
    assert cli.frozen_fingerprint(data) is None


def test_prereg_failure_or_bad_value_is_none(armed_dirs, monkeypatch):
    config_dir, _ = armed_dirs
    module = fake_prereg(monkeypatch)
    module.fingerprint = lambda *a: (_ for _ in ()).throw(OSError("unreadable config"))
    assert cli.config_fingerprint(config_dir) is None
    module.fingerprint = lambda *a: 123
    assert cli.config_fingerprint(config_dir) is None
    module.config_fingerprint = lambda config_dir: "direct"  # wygodniejszy interfejs, jeśli T15 go doda
    assert cli.config_fingerprint(config_dir) == "direct"


def test_prepare_run_logs_nothing_secret_and_uses_the_bound_account(armed_dirs, monkeypatch, tmp_path):
    config_dir, data = armed_dirs
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    key = tmp_path / "capitalcom.json"
    key.write_text(SECRETS)
    key.chmod(0o600)
    ctx = cli.prepare_run(cli.Paths(config_dir, data, key))
    assert ctx.mode is Mode.DRY_RUN and ctx.creds.account_id == "ACC-1"
    assert "KEYKEYKEY1" not in repr(ctx) and "pw-secret" not in repr(ctx)


async def test_build_engine_uses_the_live_host_only(armed_dirs, monkeypatch, tmp_path, clock):
    import aiohttp

    from gwstock.broker.capitalcom import LIVE_BASE_URL, CapitalComBroker, api_root

    config_dir, data = armed_dirs
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    key = tmp_path / "capitalcom.json"
    key.write_text(SECRETS)
    key.chmod(0o600)
    ctx = cli.prepare_run(cli.Paths(config_dir, data, key))
    journal = Journal.open(str(tmp_path / "j.sqlite"))
    try:
        async with aiohttp.ClientSession() as http:
            engine = cli.build_engine(ctx, http, journal, clock, data)  # bez żadnego żądania sieciowego
            assert isinstance(engine.raw_broker, CapitalComBroker) and engine.raw_broker._api == api_root(LIVE_BASE_URL)
            assert engine.mode is Mode.DRY_RUN
    finally:
        journal.close()


# --- kontrola rachunku u brokera przy starcie (live) ---------------------------------------------------------------


def set_account(broker: FakeBroker, account_id: str, **over) -> None:
    broker.accounts = [replace(a, **over) if a.account_id == account_id else a for a in broker.accounts]


@pytest.mark.parametrize(
    ("change", "reason", "skip"),
    [
        # saldo 0: także bramka (LEVERAGE, MARGIN) zatrzymuje sygnał w dry_run
        (lambda b: set_account(b, "ACC-1", equity=D("0"), available=D("0")), "ACCOUNT_ZERO_BALANCE", "GATE_BLOCKED"),
        (lambda b: set_account(b, "ACC-1", currency="USD"), "ACCOUNT_NOT_EUR", "DRY_RUN"),
        (lambda b: set_account(b, "ACC-1", status="SUSPENDED"), "ACCOUNT_NOT_ENABLED", "DRY_RUN"),
    ],
    ids=["zero_balance", "non_eur", "not_enabled"],
)
async def test_start_with_zero_balance_or_non_eur_account_stays_in_dry_run(
    make_engine, broker, clock, notifier, change, reason, skip
):
    change(broker)
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert engine.mode is Mode.DRY_RUN and rep.mode == "dry_run" and rep.skip_reason == skip
    assert "open_position" not in broker.calls
    assert any(reason in m for _, m in alerts(notifier, "MODE_DOWNGRADED"))
    [d] = engine_decisions(engine.journal)
    assert d["mode"] == "dry_run" and d["outcome"] == "SIGNAL"  # dalej pełna logika, bez zlecenia


async def test_bound_account_missing_at_the_broker_stays_in_dry_run(make_engine, broker, clock, notifier):
    engine = make_engine(Mode.LIVE, runtime=RuntimeConfig(True, "ACC-9"), broker=FakeBroker(
        clock, broker._rules, broker._quote))  # fmt: skip
    engine.raw_broker.set_quote_fn(broker._quote_fn)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert engine.mode is Mode.DRY_RUN and alerts(notifier, "BOUND_ACCOUNT_NOT_FOUND")


async def test_wrong_active_account_in_the_session_stays_in_dry_run(make_engine, clock, day_rules, market, notifier):
    unbound = FakeBroker(clock, day_rules, market.quote(clock.now_utc()))  # sesja ląduje na ACC-0, związany ACC-1
    unbound.set_quote_fn(market.quote)
    engine = make_engine(Mode.LIVE, broker=unbound)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert engine.mode is Mode.DRY_RUN and alerts(notifier, "WRONG_ACTIVE_ACCOUNT")
    assert "open_position" not in unbound.calls


async def test_account_reverified_after_a_relogin_before_the_entry(engine, broker, clock, notifier):
    at_time(broker, clock, berlin(DAY, 8, 59), broker.expire_session)  # ponowne logowanie przed decyzją
    at_time(broker, clock, berlin(DAY, 9, 0), lambda: setattr(broker, "_active_account_id", "ACC-0"),
            method="list_positions")  # fmt: skip
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert engine.mode is Mode.DRY_RUN and alerts(notifier, "WRONG_ACTIVE_ACCOUNT")
    assert "open_position" not in broker.calls


async def test_unreadable_account_delays_the_entry_but_never_trades_unverified(engine, broker, clock, notifier):
    def broken():
        if clock.now_utc() < berlin(DAY, 9, 7):
            raise BrokerError("GET /accounts: HTTP 503")

    broker.on_call("list_accounts", broken)
    entries = calls_at(broker, clock, "open_position")
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert entries == [berlin(DAY, 9, 7)] and engine.mode is Mode.LIVE  # dopiero po udanej kontroli
    assert alerts(notifier, "ACCOUNT_CHECK_UNAVAILABLE")


async def test_healthy_account_keeps_live_and_enters(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert engine.mode is Mode.LIVE and rep.entries == 1 and broker.calls.count("open_position") == 1


# --- dry_run: pełna logika, żadnego zlecenia -----------------------------------------------------------------------


async def test_dry_run_never_calls_open_position(make_engine, broker, clock):
    engine = make_engine(Mode.DRY_RUN)
    rep = await engine.run_day(DAY)
    assert "open_position" not in broker.calls and "close_position" not in broker.calls
    assert "set_stop" not in broker.calls
    assert rep.skip_reason == "DRY_RUN" and rep.entries == 0 and rep.mode == "dry_run"
    assert broker.calls.count("candles_5m") == 1 and "daily_candles" in broker.calls  # dane i decyzja były
    [d] = engine_decisions(engine.journal)
    assert d["mode"] == "dry_run" and d["outcome"] == "SIGNAL"
    assert [p for p in payloads(engine.journal, "decision") if "request" in p]  # zapis decyzji wykonawcy


async def test_dry_run_checks_the_account_for_information_only(make_engine, broker, clock, caplog):
    import logging

    broker.accounts = [replace(a, equity=D("0")) for a in broker.accounts]  # w live: zejście do dry_run
    engine = make_engine(Mode.DRY_RUN)
    stop_engine_at(engine, clock, berlin(DAY, 9, 0))
    with caplog.at_level(logging.INFO, logger="gwstock.engine"):
        await engine.run_day(DAY)
    assert broker.calls.count("list_accounts") == 1 and engine.mode is Mode.DRY_RUN
    assert any("dry_run account check: ACCOUNT_ZERO_BALANCE" in r.getMessage() for r in caplog.records)


async def test_dry_run_broker_refuses_an_entry_even_if_asked(make_engine, broker, req):
    engine = make_engine(Mode.DRY_RUN)
    with pytest.raises(BrokerRejected, match="DRY_RUN_NO_ORDERS"):
        await engine.broker.open_position(req)
    assert "open_position" not in broker.calls


# --- bind-account ----------------------------------------------------------------------------------------------


class Owner:
    """Atrapa terminala właściciela: kolejne odpowiedzi; zapisuje pytania i sprawdza, że nic nie zapisano wcześniej."""

    def __init__(self, data_dir: Path, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.data_dir = data_dir
        self.file_at_prompt: list[bool] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.file_at_prompt.append((self.data_dir / "account.json").exists())
        return self.answers.pop(0)


@pytest.fixture
def unbound(clock, day_rules, market):
    return FakeBroker(clock, day_rules, market.quote(clock.now_utc()))


async def test_bind_account_lists_accounts_and_requires_explicit_confirmation(unbound, data_dir):
    lines: list[str] = []
    owner = Owner(data_dir, "2", "CC-1")
    assert await cli.bind_account(unbound, owner, data_dir, out=lines.append) == "ACC-1"
    text = "\n".join(lines)
    assert "1) ACC-0" in text and "2) ACC-1" in text and "currency EUR" in text and "balance 40" in text
    assert owner.file_at_prompt == [False, False]  # przed każdym pytaniem pliku nie było
    path = data_dir / "account.json"
    assert json.loads(path.read_text()) == {"account_id": "ACC-1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_runtime(Path("config"), data_dir).account_id == "ACC-1"
    assert unbound.calls[:2] == ["login", "list_accounts"]
    assert not any(p.name.endswith(".tmp") for p in data_dir.iterdir())


async def test_bind_account_rejects_zero_balance(unbound, data_dir):
    with pytest.raises(cli.BindAccountError, match="balance 0"):
        await cli.bind_account(unbound, Owner(data_dir, "1", "CC-0"), data_dir, out=lambda _: None)
    assert not (data_dir / "account.json").exists()


@pytest.mark.parametrize(
    ("over", "text"), [(dict(currency="USD"), "currency USD"), (dict(status="DISABLED"), "status DISABLED")]
)
async def test_bind_account_rejects_non_eur_and_disabled(unbound, data_dir, over, text):
    set_account(unbound, "ACC-1", **over)
    with pytest.raises(cli.BindAccountError, match=text):
        await cli.bind_account(unbound, Owner(data_dir, "2", "CC-1"), data_dir, out=lambda _: None)
    assert not (data_dir / "account.json").exists()


@pytest.mark.parametrize("answers", [("2", "CC-0"), ("2", ""), ("x",), ("3",), ("0",)])
async def test_bind_account_writes_nothing_without_a_valid_choice_and_confirmation(unbound, data_dir, answers):
    with pytest.raises(cli.BindAccountError):
        await cli.bind_account(unbound, Owner(data_dir, *answers), data_dir, out=lambda _: None)
    assert not (data_dir / "account.json").exists()


async def test_bind_account_needs_overwrite_to_replace_an_existing_binding(unbound, data_dir):
    unbound.accounts.append(AccountInfo("ACC-2", "EUR", "ENABLED", D("15"), D("15"), D("0")))
    (data_dir / "account.json").write_text(json.dumps({"account_id": "ACC-1"}))
    with pytest.raises(cli.BindAccountError, match="kept"):
        await cli.bind_account(unbound, Owner(data_dir, "3", "yes"), data_dir, out=lambda _: None)
    assert json.loads((data_dir / "account.json").read_text()) == {"account_id": "ACC-1"}
    owner = Owner(data_dir, "3", "OVERWRITE", "CC-2")
    assert await cli.bind_account(unbound, owner, data_dir, out=lambda _: None) == "ACC-2"
    assert json.loads((data_dir / "account.json").read_text()) == {"account_id": "ACC-2"}
    assert "OVERWRITE" in owner.prompts[1]


async def test_bind_account_rebinding_the_same_account_needs_only_the_confirmation(unbound, data_dir):
    (data_dir / "account.json").write_text(json.dumps({"account_id": "ACC-1"}))
    owner = Owner(data_dir, "2", "CC-1")
    assert await cli.bind_account(unbound, owner, data_dir, out=lambda _: None) == "ACC-1"
    assert len(owner.prompts) == 2


# --- smoke (L0) -----------------------------------------------------------------------------------------------------


@pytest.fixture
def smoke_clock(clock, broker):
    clock.advance((berlin(DAY, 12, 0) - clock.now_utc()).total_seconds())  # poza oknem strategii
    noon = berlin(DAY, 11, 30)
    flat = (D("25781"), D("25784"), D("25779"), D("25782"))
    broker.set_candles_5m(Candle("DE40", noon + timedelta(minutes=5 * k), *flat) for k in range(6))
    return clock


async def test_smoke_opens_minimum_size_with_stop_and_closes_immediately(engine, broker, smoke_clock):
    lines: list[str] = []
    code = await cli.smoke(engine, out=lines.append)
    text = "\n".join(lines)
    assert code == 0, text
    for n in range(1, 8):
        assert f"PASS [{n}]" in text
    assert "FAIL" not in text
    assert "deal DEAL-1" in text and "size 0.001" in text and "reported by the API" in text
    assert "INFO [7] 5m candle start 2026-11-02T10:45:00+00:00 UTC (11:45 CET): open 25781" in text
    assert "INFO [7] 5m candle start 2026-11-02T10:55:00+00:00 UTC (11:55 CET)" in text  # trzy ostatnie zakończone
    assert "INFO [8] history latency: the closure appeared in closed_since 2.0 s" in text
    assert broker.calls.count("open_position") == 1
    assert broker.calls.count("close_position") == 2  # spłaszczenie + celowe drugie zamknięcie
    j = engine.journal
    assert j.get_order("gws-20261102-DE40-1-smoke").state is OrderState.CONFIRMED
    assert j.count_entry_orders(DAY) == 0  # próba nie zużywa dziennego wejścia strategii
    [t] = j.trades()
    assert t.size == D("0.001") and j.current_position("DE40").state is P.FLAT
    assert engine.kill.level() is KillLevel.NONE


async def test_smoke_flags_a_derived_stop_for_manual_verification(engine, broker, smoke_clock):
    def mark_derived():
        for deal_id, pos in list(broker.positions.items()):
            broker.positions[deal_id] = replace(pos, stop_derived=True)

    broker.on_call("list_positions", mark_derived)
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 0
    assert any("verify the stop in the Capital.com app" in line for line in lines)


@pytest.mark.parametrize("second", ["accepted", "other_error", "other_rejection"])
async def test_smoke_second_close_that_is_not_position_not_found_is_a_loud_fail(
    engine, broker, smoke_clock, notifier, second, monkeypatch
):
    real = broker.close_position
    state = {"n": 0}

    async def close(deal_id):
        state["n"] += 1
        if state["n"] == 1:
            return await real(deal_id)
        if second == "other_error":
            raise BrokerError("HTTP 500")
        if second == "other_rejection":
            raise BrokerRejected("error.invalid.dealId")  # odrzucone, ale nie z powodu braku pozycji
        return None  # „przyjęte" mimo braku pozycji: założenie ścieżki awaryjnej nie trzyma

    monkeypatch.setattr(broker, "close_position", close)
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert any(line.startswith("FAIL [6] LOUD") for line in lines)
    assert alerts(notifier, "SMOKE_SECOND_CLOSE_UNEXPECTED") and engine.kill.level() is KillLevel.SOFT


async def test_smoke_refuses_in_dry_run(make_engine, broker, smoke_clock):
    engine = make_engine(Mode.DRY_RUN)
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert "dry_run" in lines[0] and broker.calls == []


async def test_smoke_refuses_with_an_open_position(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)  # wejście strategii: DEAL-1 OPEN
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert any("smoke refuses" in line for line in lines) and broker.calls.count("open_position") == 1


async def test_smoke_is_blocked_by_the_kill_switch(engine, broker, smoke_clock):
    engine.kill.soft("STOP_FILE")
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert any("KILL" in line for line in lines) and "open_position" not in broker.calls


def test_smoke_stop_uses_the_strategy_stop_or_twice_the_minimum(day_rules, strat_cfg):
    assert cli.smoke_stop_distance(day_rules, D("300"), strat_cfg) == D("30.0")
    assert cli.smoke_stop_distance(day_rules, D("10"), strat_cfg) == D("2.6")  # 1.0 < minimum 2.6
    assert cli.smoke_stop_distance(day_rules, None, strat_cfg) == D("5.2")
    odd = replace(day_rules, min_stop_distance=D("2.57813"))
    assert cli.smoke_stop_distance(odd, None, strat_cfg) == D("5.2")  # 5.15626 w górę do kroku 0.1


# --- status i health -------------------------------------------------------------------------------------------------


async def test_status_shows_mode_kill_position_orders_heartbeat_decision_and_trades(
    make_engine, broker, clock, data_dir, sim_root, monkeypatch
):
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    make_engine.journal_path = data_dir / "journal.sqlite"  # układ produkcyjny: dziennik w data/
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    lines: list[str] = []
    config_dir = sim_root / "config"
    config_dir.mkdir()
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml", "runtime.toml"):
        (config_dir / name).write_text(Path("config", name).read_text())
    assert cli.status(config_dir, data_dir, out=lines.append, now=clock.now_utc()) == 0
    text = "\n".join(lines)
    assert "mode (files): dry_run" in text and "live_allowed is not true" in text
    assert "kill switch: NONE" in text and "position DE40: OPEN deal DEAL-1 BUY size 0.001" in text
    assert "unresolved orders: 0" in text and "heartbeat: 5 s ago, engine mode live, state MANAGING" in text
    assert "last decision 2026-11-02: SIGNAL" in text and "first candle 2026-11-02T08:00:00+00:00" in text
    assert "trades today (2026-11-02): 0" in text


def test_status_without_a_journal_does_not_create_one(data_dir, tmp_path):
    lines: list[str] = []
    assert cli.status(Path("config"), data_dir, out=lines.append) == 0
    assert "journal: none yet" in lines and not (data_dir / "journal.sqlite").exists()


async def test_health_reports_heartbeat_freshness(engine, data_dir, clock):
    now = clock.now_utc()
    assert cli.health(data_dir, now=now) == 1  # brak tętna
    await engine.watchdog.tick()
    assert cli.health(data_dir, now=now + timedelta(seconds=119)) == 0
    assert cli.health(data_dir, now=now + timedelta(seconds=120)) == 1
    (data_dir / "heartbeat.json").write_text("garbage")
    assert cli.health(data_dir, now=now) == 1


# --- CLI ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "command"),
    [([], "run"), (["run"], "run"), (["bind-account"], "bind-account"), (["smoke"], "smoke"), (["status"], "status"),
     (["health"], "health")],
)  # fmt: skip
def test_cli_dispatches_each_command(monkeypatch, argv, command, tmp_path):
    seen = []
    monkeypatch.setenv("GW_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GW_CAPITALCOM_KEY_FILE", str(tmp_path / "k.json"))
    for name in cli.COMMANDS:
        monkeypatch.setitem(cli.COMMANDS, name, lambda paths, name=name: seen.append((name, paths)) or 7)
    assert cli.main(argv) == 7
    [(name, paths)] = seen
    assert name == command
    assert paths == cli.Paths(tmp_path / "c", tmp_path / "d", tmp_path / "k.json")


def test_cli_defaults_and_unknown_command(monkeypatch):
    for var in ("GW_CONFIG_DIR", "GW_DATA_DIR", "GW_CAPITALCOM_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    assert cli.paths_from_env() == cli.Paths(Path("config"), Path("data"), Path("/secrets/capitalcom.json"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["trade-now"])
    assert exc.value.code == 2


def test_cli_maps_a_second_instance_and_bad_files_to_exit_codes(monkeypatch, capsys):
    def busy(paths):
        raise AlreadyRunning("another engine instance holds data/engine.lock (pid 1)")

    def bad(paths):
        raise ValueError("secrets file /secrets/capitalcom.json: missing key api_key")

    monkeypatch.setitem(cli.COMMANDS, "run", busy)
    assert cli.main(["run"]) == 3
    monkeypatch.setitem(cli.COMMANDS, "run", bad)
    assert cli.main(["run"]) == 2
    assert "missing key" in capsys.readouterr().err


def test_python_m_gwstock_is_import_safe_and_health_works_end_to_end(tmp_path):
    env = {**os.environ, "GW_DATA_DIR": str(tmp_path), "PYTHONPATH": str(SRC)}
    imported = subprocess.run(
        [sys.executable, "-c", "import gwstock.__main__, gwstock.cli, gwstock.app, gwstock.watchdog"],
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert imported.returncode == 0 and imported.stdout == "" and imported.stderr == ""
    unhealthy = subprocess.run([sys.executable, "-m", "gwstock", "health"], env=env, capture_output=True, timeout=60)
    assert unhealthy.returncode == 1
    beat = {"ts": datetime.now(timezone.utc).isoformat(), "mode": "dry_run", "state": "IDLE", "last_quote_ts": None,
            "kill_level": "NONE"}  # fmt: skip
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat))
    healthy = subprocess.run([sys.executable, "-m", "gwstock", "health"], env=env, capture_output=True, timeout=60)
    assert healthy.returncode == 0


def test_cli_holds_the_instance_lock_for_smoke_and_bind(monkeypatch, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    monkeypatch.setenv("GW_CAPITALCOM_KEY_FILE", str(tmp_path / "missing.json"))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["bind-account"]) in (2, 3)  # nigdy nie dochodzi do brokera
    key = tmp_path / "k.json"
    key.write_text(SECRETS)
    key.chmod(0o600)
    monkeypatch.setenv("GW_CAPITALCOM_KEY_FILE", str(key))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["bind-account"]) == 3
        assert cli.main(["smoke"]) == 3  # blokada przed konfiguracją, dziennikiem i siecią
    assert not (data / "journal.sqlite").exists()


# --- kod nigdy nie tworzy plików uzbrojenia --------------------------------------------------------------------------

WRITE_CALLS = frozenset({
    "write_text", "write_bytes", "touch", "open", "mkdir", "makedirs", "symlink_to", "hardlink_to", "rename",
    "replace", "link", "symlink", "copy", "copy2", "copyfile", "copytree", "move", "mknod", "mkfifo", "fdopen",
})  # fmt: skip
# Sprawdzenie obecności: wynik to bool, nie ścieżka (nie przenosi „skażenia" i jest dozwolone).
CHECK_CALLS = frozenset({"exists", "is_file", "is_dir", "is_symlink", "lstat", "stat", "_marker_present"})


def arming_writes(source: str) -> list[int]:
    """Numery linii, w których wyrażenie oparte na nazwie pliku uzbrojenia trafia do operacji zapisu/tworzenia.

    Śledzi stałe z tą nazwą i zmienne z nich zbudowane (w obrębie modułu), a potem każde ich użycie w górę drzewa:
    użycie wewnątrz wywołania zapisującego (`write_text`, `touch`, `open`, `os.open`, `rename`, ...) to naruszenie.
    Dozwolone jest tylko sprawdzanie (np. `.exists()`, `.is_file()`).
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    tainted: set[str] = set()

    def is_marker(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return any(name in node.value for name in ARMING_NAMES)
        return isinstance(node, ast.Name) and node.id in tainted

    def call_name(call: ast.Call) -> str:
        func = call.func
        return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""

    def checked(node: ast.AST, stop: ast.AST) -> bool:
        """Czy marker leży wewnątrz wywołania sprawdzającego (do węzła `stop`)."""
        cur = node
        while cur is not stop and cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Call) and call_name(cur) in CHECK_CALLS:
                return True
        return False

    changed = True
    while changed:  # propagacja: NAZWA = ... (wyrażenie z markerem, które nie jest tylko sprawdzeniem)
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if any(is_marker(n) and not checked(n, node) for n in ast.walk(node.value)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name) and n.id not in tainted:
                                tainted.add(n.id)
                                changed = True
    bad = []
    for node in ast.walk(tree):
        if not is_marker(node):
            continue
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Call):
                name = call_name(cur)
                if name in CHECK_CALLS:
                    break
                if name in WRITE_CALLS:
                    bad.append(node.lineno)
                    break
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                break
    return bad


def test_engine_code_never_creates_arm_file():
    sources = sorted(SRC.rglob("*.py"))
    assert len(sources) > 20
    offenders = {str(p.relative_to(SRC)): arming_writes(p.read_text()) for p in sources}
    assert {k: v for k, v in offenders.items() if v} == {}
    mentions = [p.name for p in sources if any(n in p.read_text() for n in ARMING_NAMES)]
    assert "cli.py" in mentions and "app.py" in mentions  # skaner naprawdę widzi miejsca, które tylko sprawdzają


@pytest.mark.parametrize(
    "snippet",
    [
        "from pathlib import Path\n(Path('data') / 'ARM_LIVE').touch()\n",
        "import os\nARM = 'ARM_LIVE'\nfd = os.open('data/' + ARM, os.O_CREAT)\n",
        "F = 'L1_PASSED'\ndef f(d):\n    p = d / F\n    p.write_text('')\n",
        "with open('data/L1_PASSED', 'w') as fh:\n    pass\n",
    ],
    ids=["touch", "os-open", "write-text-via-variable", "open-for-write"],
)
def test_the_arming_scanner_catches_creation(snippet):
    snippet = snippet.replace("ARM_LIVE", ARMING_NAMES[0]).replace("L1_PASSED", ARMING_NAMES[1])
    assert arming_writes(snippet)


def test_the_arming_scanner_allows_checking():
    snippet = f"from pathlib import Path\nok = (Path('data') / '{ARMING_NAMES[0]}').exists()\n"
    assert arming_writes(snippet) == []
