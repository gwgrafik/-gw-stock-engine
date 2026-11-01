"""Tryby i polecenia właściciela (spec §9, §13): uzbrajanie live, kontrola rachunku przy starcie, dry_run bez zleceń,
`bind-account`, `smoke` (L0), `status`, `health`, rozdział poleceń CLI, leniwy import pre-rejestracji i to, że kod
nigdy nie tworzy plików uzbrojenia (`ARM_LIVE`, `L1_PASSED`).

Ten plik zawiera nazwy plików uzbrojenia wyłącznie jako wzorce wyszukiwania w źródłach (zwolniony w guard_live).
"""

from __future__ import annotations

import ast
import asyncio
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
from gwstock.app import EngineMisconfigured, Mode
from gwstock.broker.fake import FakeBroker, _Rejected
from gwstock.config import RuntimeConfig, load_runtime
from gwstock.journal import Journal, JournalError, JournalReadError, JournalWriteError, OrderRecord, PositionRecord
from gwstock.types import (
    AccountInfo,
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    Candle,
    ClosedDeal,
    KillLevel,
    OpenPosition,
    OrderState,
    PositionState,
    Side,
)
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
    daily_candles,
    clock,
    config_dir,
    data_dir,
    day_rules,
    disarm,
    engine,
    engine_decisions,
    five_minute_candles,
    journal_path,
    make_engine,
    market,
    payloads,
    runtime,
    set_live_allowed,
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


# --- uzbrojenie sprawdzane przy każdym dniu i tuż przed wejściem (C1) ----------------------------------------------


class Died(BaseException):
    """Śmierć procesu w wybranej chwili."""


def die(*_a):
    raise Died()


async def test_arm_file_removed_between_days_stops_entries_and_the_leftover_is_still_flattened(
    make_engine, broker, clock, data_dir, notifier
):
    first = make_engine(Mode.LIVE)
    at_time(broker, clock, berlin(DAY, 11, 0), die)  # pozycja z poniedziałku zostaje otwarta
    with pytest.raises(Died):
        await first.run_day(DAY)
    disarm(data_dir)  # właściciel rozbraja wieczorem
    tuesday = DAY + timedelta(days=1)
    broker.set_daily_candles(daily_candles(tuesday))
    broker.set_candles_5m(five_minute_candles(tuesday, D("25780"), D("25790")))
    clock.advance((berlin(tuesday, 8, 45) - clock.now_utc()).total_seconds())
    second = make_engine.restart(first, Mode.LIVE)
    closes = calls_at(broker, clock, "close_position")
    stop_engine_at(second, clock, berlin(tuesday, 9, 20))
    rep = await second.run_day(tuesday)
    assert second.mode is Mode.DRY_RUN and rep.mode == "dry_run"
    assert broker.calls.count("open_position") == 1  # tylko poniedziałkowe wejście
    assert closes and second.journal.current_position("DE40").state is P.FLAT  # pozostałość spłaszczona
    [(_, message)] = [a for a in alerts(notifier, "MODE_DOWNGRADED")]
    assert "ARM_LIVE removed" in message and "restart" in message
    [event] = second.journal.events("engine")
    assert event.ts == berlin(tuesday, 8, 45)  # już na początku dnia, nie dopiero przed wejściem o 09:05


async def test_arm_file_removed_between_0900_and_0905_means_no_entry(make_engine, broker, clock, data_dir, notifier):
    engine = make_engine(Mode.LIVE)
    at_time(broker, clock, berlin(DAY, 9, 2), lambda: disarm(data_dir))
    closes = calls_at(broker, clock, "close_position")
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert "open_position" not in broker.calls and engine.mode is Mode.DRY_RUN and rep.skip_reason == "DRY_RUN"
    [d] = engine_decisions(engine.journal)
    assert d["outcome"] == "SIGNAL"  # decyzja zapisana, wejście dopiero przed wysyłką zatrzymane
    assert alerts(notifier, "ARM_LIVE removed") and closes == []


async def test_live_allowed_flipped_to_false_stops_the_next_entry_but_the_open_position_is_flattened(
    make_engine, broker, clock, config_dir, notifier
):
    engine = make_engine(Mode.LIVE)
    at_time(broker, clock, berlin(DAY, 12, 0), lambda: set_live_allowed(config_dir, False))
    closes = calls_at(broker, clock, "close_position")
    rep = await engine.run_day(DAY)  # pozycja z 09:05 nadal zarządzana i spłaszczona o 17:25
    assert closes == [berlin(DAY, 17, 25)] and rep.final_position_state is P.FLAT
    tuesday = DAY + timedelta(days=1)
    broker.set_daily_candles(daily_candles(tuesday))
    broker.set_candles_5m(five_minute_candles(tuesday, D("25780"), D("25790")))
    stop_engine_at(engine, clock, berlin(tuesday, 9, 20))
    await engine.run_day(tuesday)
    assert broker.calls.count("open_position") == 1 and engine.mode is Mode.DRY_RUN
    assert alerts(notifier, "live_allowed")


async def test_live_engine_without_a_config_dir_refuses_to_start(make_engine, broker):
    engine = make_engine(Mode.LIVE, config_dir=None)  # live_allowed nie dałoby się sprawdzić ponownie
    with pytest.raises(EngineMisconfigured, match="config_dir"):
        await engine.run_day(DAY)
    assert broker.calls == []


# --- rachunek sprawdzany jeszcze raz tuż przed zleceniem (I1) -------------------------------------------------------


async def test_relogin_inside_the_decision_never_enters_on_an_unverified_account(engine, broker, clock, notifier):
    # sonda P1: ponowne logowanie w trakcie odczytu świec, sesja ląduje na innym (zasilonym) rachunku
    broker.accounts[0] = AccountInfo("ACC-0", "EUR", "ENABLED", D("40"), D("40"), D("0"))
    n = {"c": 0}

    def on_candles():
        n["c"] += 1
        if n["c"] == 1:
            broker.expire_session()
        elif n["c"] == 2:
            broker._active_account_id = "ACC-0"

    broker.on_call("candles_5m", on_candles)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    assert "open_position" not in broker.calls and engine.mode is Mode.DRY_RUN
    assert alerts(notifier, "WRONG_ACTIVE_ACCOUNT")


async def test_account_mismatch_at_the_last_moment_downgrades_and_sends_nothing(engine, broker, clock, notifier):
    def switch():
        if clock.now_utc() >= DECISION_UTC:  # tuż przed zleceniem wykonawca czyta rachunek: sesja na innym
            broker._active_account_id = "ACC-0"

    broker.on_call("account", switch)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    rep = await engine.run_day(DAY)
    assert "open_position" not in broker.calls and engine.mode is Mode.DRY_RUN
    assert alerts(notifier, "ACCOUNT_MISMATCH") and rep.entries == 0


async def test_live_broker_refuses_an_entry_on_an_unverified_session(engine, broker, req):
    with pytest.raises(BrokerRejected, match="ACCOUNT_UNVERIFIED"):  # rachunek jeszcze niesprawdzony w tej sesji
        await engine.broker.open_position(req)
    assert "open_position" not in broker.calls


# --- dry_run: pełna logika, żadnego zlecenia -----------------------------------------------------------------------


async def test_dry_run_never_calls_open_position(make_engine, broker, clock):
    engine = make_engine(Mode.DRY_RUN)
    rep = await engine.run_day(DAY)
    assert "open_position" not in broker.calls and "close_position" not in broker.calls
    assert "set_stop" not in broker.calls
    assert rep.skip_reason == "DRY_RUN" and rep.entries == 0 and rep.mode == "dry_run"
    assert broker.calls.count("candles_5m") >= 1 and "daily_candles" in broker.calls  # dane i decyzja były
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


@pytest.mark.parametrize("content", [{"account_id": "ACC-1", "note": "x"}, {"account_id": "ACC-1", "account": "ACC-0"}])
async def test_bind_account_treats_a_binding_file_with_extra_keys_as_unreadable(unbound, data_dir, content):
    # ruling 4 / addendum 7.3: dodatkowe klucze w data/account.json to nie powiązanie (jak w czytniku silnika)
    (data_dir / "account.json").write_text(json.dumps(content))
    lines: list[str] = []
    owner = Owner(data_dir, "2", "OVERWRITE", "CC-1")  # ten sam rachunek, ale plik trzeba jawnie zastąpić
    assert await cli.bind_account(unbound, owner, data_dir, out=lines.append) == "ACC-1"
    assert "Currently bound: <unreadable>" in lines and "OVERWRITE" in owner.prompts[1]
    assert json.loads((data_dir / "account.json").read_text()) == {"account_id": "ACC-1"}


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


async def test_smoke_shows_prominently_an_entry_adopted_after_unknown_without_failing(engine, broker, smoke_clock):
    # potwierdzenie milczy dwa razy: menedżer przyjmuje wejście z listy pozycji (ADOPTED_AFTER_UNKNOWN VIA=positions),
    # tak samo jak przy fałszywej niezgodności wielkości w potwierdzeniu; właściciel musi to zobaczyć
    for _ in range(2):
        broker.fail_next("confirm", BrokerTimeout())
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 0, lines
    note = engine.journal.get_order("gws-20261102-DE40-1-smoke").note
    assert "ADOPTED_AFTER_UNKNOWN VIA=positions" in note
    [warning] = [line for line in lines if line.startswith("WARN [2]")]
    assert "wejście przyjęte przez uzgadnianie, nie przez potwierdzenie" in warning and note in warning
    assert lines[-1].startswith("PASS smoke:") and "WARN [2]" in lines[-1]


async def test_smoke_without_an_adopted_entry_prints_no_adoption_warning(engine, broker, smoke_clock):
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 0
    assert not any(line.startswith("WARN") for line in lines) and "WARN" not in lines[-1]


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


async def test_smoke_skips_the_second_close_when_the_position_is_not_confirmed_gone(engine, broker, smoke_clock):
    def timing_out():
        raise BrokerTimeout("DELETE /positions: timeout")

    broker.on_call("close_position", timing_out)
    lines: list[str] = []
    assert await cli.smoke(engine, out=lines.append) == 1
    assert any(line.startswith("FAIL [4]") for line in lines) and any(line.startswith("FAIL [5]") for line in lines)
    assert any(line.startswith("INFO [6] skipped: position not confirmed gone") for line in lines)
    assert not any(line.startswith(("PASS [6]", "FAIL [6]")) for line in lines)


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
    make_engine, broker, clock, data_dir, config_dir, monkeypatch
):
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    make_engine.journal_path = data_dir / "journal.sqlite"  # układ produkcyjny: dziennik w data/
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    lines: list[str] = []
    assert cli.status(config_dir, data_dir, out=lines.append, now=clock.now_utc()) == 0
    text = "\n".join(lines)
    assert "mode (files): dry_run (no frozen pre-registration fingerprint" in text  # reszta uzbrojenia jest
    assert "kill switch: NONE" in text and "position DE40: OPEN deal DEAL-1 BUY size 0.001" in text
    assert "unresolved orders: 0" in text and "heartbeat: 5 s ago, engine mode live, state MANAGING" in text
    assert "last decision 2026-11-02: SIGNAL" in text and "first candle 2026-11-02T08:00:00+00:00" in text
    assert "trades today (2026-11-02): 0" in text


def corrupt_order_row(path: Path) -> None:
    """Wiersz zlecenia z nieznanym stanem (np. po ręcznej edycji): `unresolved_orders` rzuca JournalCorruptError."""
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO orders VALUES ('gws-20261102-US500-1-entry', 'US500', 'BUY', '1', '1', 'WAT', NULL, NULL, "
        "'2026-11-02T08:05:00+00:00', '2026-11-02T08:05:00+00:00', '')"
    )
    raw.commit()
    raw.close()


@pytest.mark.parametrize("broken", ["order_row", "kill_state"])
async def test_status_prints_unreadable_for_a_corrupt_part_and_shows_the_rest(
    make_engine, broker, clock, data_dir, config_dir, monkeypatch, broken
):
    # addendum 7b: jeden uszkodzony wiersz (dowolny instrument) nie może urwać całego `status`
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    make_engine.journal_path = data_dir / "journal.sqlite"
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    engine.kill.soft("STOP_FILE")
    if broken == "order_row":
        corrupt_order_row(data_dir / "journal.sqlite")
    else:
        corrupt_kill_state(data_dir)
    lines: list[str] = []
    assert cli.status(config_dir, data_dir, out=lines.append, now=clock.now_utc()) == 0
    text = "\n".join(lines)
    part = "unresolved orders: UNREADABLE (JournalCorruptError" if broken == "order_row" else (
        "kill switch: UNREADABLE (JournalCorruptError")  # fmt: skip
    assert part in text and "journal repair" in text
    assert "position DE40: OPEN deal DEAL-1" in text and "last decision 2026-11-02: SIGNAL" in text
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
        monkeypatch.setitem(cli.COMMANDS, name, lambda paths, args, name=name: seen.append((name, paths)) or 7)
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
    def busy(paths, args):
        raise AlreadyRunning("another engine instance holds data/engine.lock (pid 1)")

    def bad(paths, args):
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


def test_run_takes_the_instance_lock_before_anything_else(monkeypatch, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    monkeypatch.setenv("GW_CONFIG_DIR", str(tmp_path / "missing-config"))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["run"]) == 3  # blokada przed konfiguracją, dziennikiem i siecią
    assert not (data / "journal.sqlite").exists()


def test_main_maps_journal_errors_to_an_exit_code_without_a_traceback(monkeypatch, capsys):
    def broken(paths, args):
        raise JournalError("cannot open journal 'data/journal.sqlite': disk I/O error")

    monkeypatch.setitem(cli.COMMANDS, "run", broken)
    assert cli.main(["run"]) == 2
    err = capsys.readouterr().err
    assert "JournalError" in err and "Traceback" not in err


async def test_status_opens_the_journal_read_only(make_engine, broker, clock, data_dir, monkeypatch):
    make_engine.journal_path = data_dir / "journal.sqlite"
    engine = make_engine(Mode.LIVE)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)

    def no_writable_open(path):
        raise AssertionError("status must not open the journal for writing")

    monkeypatch.setattr(Journal, "open", no_writable_open)
    lines: list[str] = []
    assert cli.status(Path("config"), data_dir, out=lines.append, now=clock.now_utc()) == 0
    assert any(line.startswith("position DE40: OPEN") for line in lines)
    ro = Journal.open_readonly(str(data_dir / "journal.sqlite"))
    try:
        with pytest.raises(JournalWriteError):
            ro.set_meta("x", "y")
        assert ro.current_position("DE40").state is P.OPEN
    finally:
        ro.close()


# --- polecenia operatorskie: clear-kill, reset-position, smoke --hold, licznik sesji -------------------------------


class Typed:
    """Atrapa terminala: kolejne odpowiedzi właściciela; zapisuje pytania."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


def soft_killed_journal(data_dir: Path, clock, reason="QUOTE_SILENCE", level="soft") -> None:
    """Dziennik z kill switchem (jak po dniu silnika) i kilkoma alarmami w alerts.log."""
    from gwstock.notify import FileNotifier
    from gwstock.risk import KillSwitch

    journal = Journal.open(str(data_dir / "journal.sqlite"))
    try:
        kill = KillSwitch(journal, clock, data_dir, FileNotifier(data_dir / "alerts.log", clock))
        getattr(kill, level)(reason)
    finally:
        journal.close()


def test_clear_kill_prints_the_state_and_alerts_and_clears_after_the_typed_confirmation(data_dir, clock):
    soft_killed_journal(data_dir, clock)
    lines: list[str] = []
    owner = Typed("CLEAR")
    assert cli.clear_kill(data_dir, "wlasciciel", owner, out=lines.append, clock=clock) == 0
    text = "\n".join(lines)
    assert "kill switch: SOFT (QUOTE_SILENCE" in text and "last alerts:" in text
    assert "kill switch SOFT: QUOTE_SILENCE" in text  # ostatnie alarmy z alerts.log
    assert "cleared by wlasciciel (was SOFT: QUOTE_SILENCE)" in text and "CLEAR" in owner.prompts[0]
    journal = Journal.open(str(data_dir / "journal.sqlite"))
    try:
        assert journal.kill_state().level is KillLevel.NONE
        audit = [json.loads(e.payload) for e in journal.events("kill.audit")]
        assert audit[-1]["event"] == "CLEAR" and audit[-1]["requested_by"] == "wlasciciel"
    finally:
        journal.close()


@pytest.mark.parametrize("answer", ["clear", "yes", ""])
def test_clear_kill_without_the_exact_confirmation_changes_nothing(data_dir, clock, answer):
    soft_killed_journal(data_dir, clock, level="hard", reason="HARD_STOP_FILE")
    assert cli.clear_kill(data_dir, "wlasciciel", Typed(answer), out=lambda _: None, clock=clock) == 2
    journal = Journal.open(str(data_dir / "journal.sqlite"))
    try:
        assert journal.kill_state().level is KillLevel.HARD
    finally:
        journal.close()


def test_clear_kill_refuses_when_nothing_is_engaged_or_a_stop_file_exists(data_dir, clock):
    lines: list[str] = []
    assert cli.clear_kill(data_dir, "wlasciciel", Typed(), out=lines.append, clock=clock) == 1  # brak dziennika
    soft_killed_journal(data_dir, clock)
    (data_dir / "STOP").write_text("")
    lines.clear()
    owner = Typed()
    assert cli.clear_kill(data_dir, "wlasciciel", owner, out=lines.append, clock=clock) == 1
    assert any("data/STOP" in line for line in lines) and owner.prompts == []  # odmowa przed pytaniem
    (data_dir / "STOP").unlink()
    assert cli.clear_kill(data_dir, "wlasciciel", Typed("CLEAR"), out=lambda _: None, clock=clock) == 0
    lines.clear()
    assert cli.clear_kill(data_dir, "wlasciciel", Typed(), out=lines.append, clock=clock) == 1
    assert any("NONE" in line for line in lines)


def test_clear_kill_shows_the_stored_state_when_the_first_read_failed_and_reports_the_merged_level(
    data_dir, clock, monkeypatch
):
    # addendum 7.1: zapisany HARD, pierwszy odczyt stanu zawodzi raz: właściciel nie może zobaczyć „SOFT
    # (KILL_STATE_UNREADABLE)", wpisać CLEAR i po cichu zdjąć HARD
    soft_killed_journal(data_dir, clock, level="hard", reason="HARD_STOP_FILE")
    real = Journal.kill_state
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JournalReadError("database is locked")
        return real(self)

    monkeypatch.setattr(Journal, "kill_state", flaky)
    lines: list[str] = []
    assert cli.clear_kill(data_dir, "wlasciciel", Typed("CLEAR"), out=lines.append, clock=clock) == 0
    [state] = [line for line in lines if line.startswith("kill switch: ")]
    assert state.startswith("kill switch: HARD (HARD_STOP_FILE, since ")
    assert "cleared by wlasciciel (was HARD: HARD_STOP_FILE)" in lines[-1]


def corrupt_kill_state(data_dir: Path) -> None:
    import sqlite3

    raw = sqlite3.connect(data_dir / "journal.sqlite")
    raw.execute("UPDATE kill_state SET level = 7")  # poziom spoza KillLevel: każdy odczyt to JournalCorruptError
    raw.commit()
    raw.close()


def test_clear_kill_refuses_a_permanently_unreadable_kill_state_and_says_only_a_repair_helps(data_dir, clock):
    # addendum 7.2: uszkodzony wiersz stanu: clear() odmawia zawsze (fail closed), a CLI mówi dlaczego i co dalej
    soft_killed_journal(data_dir, clock, level="hard", reason="HARD_STOP_FILE")
    corrupt_kill_state(data_dir)
    lines: list[str] = []
    owner = Typed()
    assert cli.clear_kill(data_dir, "wlasciciel", owner, out=lines.append, clock=clock) == 1
    assert owner.prompts == [] and lines[-1].startswith("odmowa: zapisanego stanu kill switcha nie da się odczytać")
    assert "JournalCorruptError" in lines[-1] and "naprawa dziennika" in lines[-1] and "nic nie zmieniono" in lines[-1]
    import sqlite3

    raw = sqlite3.connect(data_dir / "journal.sqlite")
    assert raw.execute("SELECT level FROM kill_state").fetchone() == (7,)  # nic nie nadpisano
    raw.close()


def test_clear_kill_command_refuses_while_the_engine_runs(monkeypatch, tmp_path, capsys):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["clear-kill", "--by", "wlasciciel"]) == 3
    assert "stop the engine first" in capsys.readouterr().err


def test_clear_kill_and_reset_position_require_a_name():
    for argv in (["clear-kill"], ["reset-position"], ["clear-kill", "--by", "  "], ["reset-position", "--by", ""]):
        with pytest.raises(SystemExit) as exc:  # audyt musi wiedzieć, kto to zrobił
            cli.main(argv)
        assert exc.value.code == 2


async def blocked_position(engine, broker, clock):
    """Pozycja BLOCKED w dzienniku przy pustym brokerze (np. właściciel zamknął ją ręcznie w aplikacji)."""
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    del broker.positions["DEAL-1"]  # zniknęła poza silnikiem; w dzienniku zostaje BLOCKED do decyzji właściciela
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED))


async def test_reset_position_without_a_closure_in_history_needs_the_deal_id_typed(engine, broker, clock):
    await blocked_position(engine, broker, clock)  # pozycji nie ma u brokera, zamknięcia nie ma w historii
    lines: list[str] = []
    owner = Typed("DEAL-1")
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 0
    assert engine.journal.current_position("DE40").state is P.FLAT
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["by"] == "wlasciciel" and audit["previous"]["state"] == "BLOCKED"
    assert audit["unbooked_deal"] == "DEAL-1" and audit["history"] == "NO_CLOSURE" and audit["acknowledged"] == "DEAL-1"
    assert "list_positions" in broker.calls and "list_working_orders" in broker.calls and "closed_since" in broker.calls
    text = "\n".join(lines)
    assert "not booked" in text and "side BUY" in text and "size 0.001" in text and "opened at" in text
    assert "ACC-" not in text  # bez identyfikatorów rachunków w wyjściu
    assert "DEAL-1" in owner.prompts[0]


@pytest.mark.parametrize("answer", ["RESET", "deal-1", "DEAL-2", ""])
async def test_reset_position_without_the_exact_deal_id_changes_nothing(engine, broker, clock, answer):
    await blocked_position(engine, broker, clock)
    assert await cli.reset_position(engine, "wlasciciel", Typed(answer), out=lambda _: None) == 2
    assert engine.journal.current_position("DE40").state is P.BLOCKED
    assert payloads(engine.journal, "owner.reset") == []


async def test_reset_position_refuses_when_the_closure_is_bookable_from_history(engine, broker, clock):
    # sonda R3: pozycja BLOCKED, a broker ma jej zamknięcie w historii: reset zgubiłby księgowanie na zawsze
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    broker.trigger_stop("DEAL-1", MID + 1 - 30)  # zamknięcie stopem zapisane w historii brokera
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED))
    clock.advance(3 * 3600)  # właściciel resetuje kilka godzin później: historia od otwarcia, nie od teraz
    lines: list[str] = []
    owner = Typed()
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 1
    assert owner.prompts == [] and payloads(engine.journal, "owner.reset") == []
    assert engine.journal.current_position("DE40").state is P.BLOCKED and engine.journal.trades() == []
    assert any("start the engine" in line and "book" in line for line in lines)
    result = await engine.reconcile_once()  # kontrola: uzgadnianie silnika rzeczywiście je księguje
    assert result is not None and len(engine.journal.trades()) == 1


async def test_reset_position_refuses_when_the_history_cannot_be_read(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    broker.fail_next("closed_since", BrokerError("GET /history/activity: HISTORY_RANGE_TOO_LARGE"))
    lines: list[str] = []
    owner = Typed()
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 1
    assert owner.prompts == [] and any("history" in line and "HISTORY_RANGE_TOO_LARGE" in line for line in lines)
    assert engine.journal.current_position("DE40").state is P.BLOCKED


async def test_reset_position_with_an_ambiguous_history_still_needs_the_deal_id(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    broker.trigger_stop("DEAL-1", MID + 1 - 30)
    broker._closed.append(replace(broker._closed[-1], exit_level=MID))  # dwa zamknięcia tego samego deal_id
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED))
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed("DEAL-1"), out=lines.append) == 0
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["history"] == "AMBIGUOUS_2_CLOSURES" and audit["unbooked_deal"] == "DEAL-1"


async def test_reset_position_with_unknown_open_time_uses_the_closed_marker_and_refuses_a_bookable_closure(
    engine, broker, clock
):
    # h1: czas otwarcia nieznany, ale znacznik ostatniego zaksięgowanego zamknięcia jest: historia od znacznika
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    opened = engine.journal.current_position("DE40").opened_at
    broker.trigger_stop("DEAL-1", MID + 1 - 30)  # zamknięcie do zaksięgowania przez silnik
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED, opened_at=None))
    engine.journal.set_meta("closed_marker", (opened - timedelta(hours=1)).isoformat())  # poprzednia transakcja
    lines: list[str] = []
    owner = Typed("DEAL-1")
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 1
    assert owner.prompts == [] and payloads(engine.journal, "owner.reset") == []
    assert engine.journal.current_position("DE40").state is P.BLOCKED and engine.journal.trades() == []
    assert any("is in the broker history" in line for line in lines)


async def test_reset_position_start_unknown_only_without_open_time_and_marker_needs_the_deal_id(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), opened_at=None))
    lines: list[str] = []
    before = broker.calls.count("closed_since")
    assert await cli.reset_position(engine, "wlasciciel", Typed("DEAL-1"), out=lines.append) == 0
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["history"] == "START_UNKNOWN" and audit["acknowledged"] == "DEAL-1"
    assert broker.calls.count("closed_since") == before  # bez początku zakresu: żadnego odczytu historii
    assert any("cannot be looked up" in line for line in lines)


async def test_reset_position_older_than_the_history_window_says_so_and_needs_the_deal_id(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    clock.advance(15 * 86400)  # właściciel wraca po dwóch tygodniach: historia brokera (14 dni) tego już nie obejmuje
    before = broker.calls.count("closed_since")
    lines: list[str] = []
    owner = Typed("DEAL-1")
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 0
    assert broker.calls.count("closed_since") == before  # bez odczytu, który i tak by odmówił
    assert any("more than 14 days ago: the broker history no longer covers it" in line for line in lines)
    assert not any("try again later" in line for line in lines) and "DEAL-1" in owner.prompts[0]
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["history"] == "HISTORY_OUT_OF_RANGE" and audit["acknowledged"] == "DEAL-1"


async def test_reset_position_with_an_ambiguous_history_lists_and_audits_every_closure(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    broker.trigger_stop("DEAL-1", MID + 1 - 30)
    broker._closed.append(replace(broker._closed[-1], exit_level=MID, reason="MANUAL"))  # dwa zamknięcia DEAL-1
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED))
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed("DEAL-1"), out=lines.append) == 0
    first, second = broker._closed[-2:]
    for c in (first, second):
        assert any(f"closure of DEAL-1: {c.reason} at {c.exit_level}, {c.ts.isoformat()}" in line for line in lines)
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["history"] == "AMBIGUOUS_2_CLOSURES"
    expected = [("STOP", str(MID + 1 - 30)), ("MANUAL", str(MID))]
    assert [(c["reason"], c["exit_level"]) for c in audit["closures"]] == expected


async def test_reset_position_without_a_deal_id_needs_the_no_deal_id_acknowledgement(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), deal_id=None))
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed("RESET"), out=lines.append) == 2
    assert engine.journal.current_position("DE40").state is P.BLOCKED
    assert any("no deal id" in line.lower() for line in lines)
    assert await cli.reset_position(engine, "wlasciciel", Typed("NO-DEAL-ID"), out=lambda _: None) == 0
    [audit] = payloads(engine.journal, "owner.reset")
    assert audit["unbooked_deal"] is None and audit["acknowledged"] == "NO-DEAL-ID"
    assert engine.journal.current_position("DE40").state is P.FLAT


async def test_reset_position_refuses_while_the_broker_shows_a_position(engine, broker, clock):
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=P.BLOCKED))
    lines: list[str] = []
    owner = Typed()
    assert await cli.reset_position(engine, "wlasciciel", owner, out=lines.append) == 1
    assert any("DEAL-1" in line for line in lines) and owner.prompts == []
    assert engine.journal.current_position("DE40").state is P.BLOCKED


async def test_reset_position_refuses_with_a_working_order_on_the_instrument(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    broker.add_working_order("DE40", Side.BUY, D("0.001"), D("25000"))
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1
    assert any("working order" in line for line in lines)
    assert engine.journal.current_position("DE40").state is P.BLOCKED


async def test_reset_position_refuses_unresolved_orders_and_engine_managed_states(engine, broker, clock):
    await blocked_position(engine, broker, clock)
    t = clock.now_utc()
    engine.journal.insert_order(OrderRecord(
        "gws-20261102-DE40-2-entry", "DE40", Side.BUY, D("0.001"), D("30"), OrderState.UNKNOWN, None, None, t, t,
    ))  # fmt: skip
    lines: list[str] = []
    before = len(broker.calls)
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1
    assert any("unresolved" in line and "reconcile" in line for line in lines)
    assert broker.calls[before:] == []  # odmowa z dziennika, zanim zapytamy brokera


@pytest.mark.parametrize("state", [P.OPEN, P.EXITING])
async def test_reset_position_refuses_open_or_exiting_which_the_engine_books_itself(engine, broker, clock, state):
    await blocked_position(engine, broker, clock)
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), state=state))
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1
    assert engine.journal.current_position("DE40").state is state
    assert any("start the engine" in line for line in lines)


async def test_reset_position_refuses_when_already_flat_or_on_the_wrong_account(engine, broker, clock):
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1  # brak pozycji
    await blocked_position(engine, broker, clock)
    broker.on_call("account", lambda: setattr(broker, "_active_account_id", "ACC-0"))  # sesja na innym rachunku
    lines.clear()
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1
    assert any("bound account" in line for line in lines)


def test_reset_position_command_takes_the_instance_lock_first(monkeypatch, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    monkeypatch.setenv("GW_CONFIG_DIR", str(tmp_path / "missing"))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["reset-position", "--by", "wlasciciel"]) == 3
    assert not (data / "journal.sqlite").exists()


# --- resolve-lost-trade (BUG-E2E-2): zgubiona transakcja, decyzja właściciela na dowodach brokera -----------------

BROKER_READS = frozenset({
    "login", "account", "list_positions", "list_working_orders", "closed_since", "confirm", "instrument_rules", "quote",
})  # fmt: skip
LOST_CID = "gws-20261102-DE40-1-entry"


def lose_the_ack(broker, *, reference_known=False, close=None):
    """Wejście przyjęte u brokera, odpowiedź zgubiona, pozycja zamknięta przed pierwszym odczytem listy (domyślnie jej
    stopem). `reference_known`: POST odpowiedział, ale `confirm` milczy przez całe rozstrzyganie menedżera
    (CAUSE=confirm), więc dane wejścia broker poda dopiero później."""
    if reference_known:
        for _ in range(4):  # potwierdzenie po wysyłce + trzy rundy rozstrzygania
            broker.fail_next("confirm", BrokerTimeout())
    else:
        broker.fail_next("open_position", BrokerTimeout(), applied=True)
    sent = {"v": False}
    broker.on_call("open_position", lambda: sent.update(v=True))

    def close_it():
        if sent["v"]:
            sent["v"] = False
            for deal_id in list(broker.positions):
                if close is None:
                    broker.trigger_stop(deal_id, broker.positions[deal_id].stop_level)
                else:
                    close(deal_id)

    broker.on_call("list_positions", close_it)


async def lost_trade(engine, broker, clock, **kw):
    """Stan z raportu BUG-E2E-2: zlecenie UNKNOWN UNMATCHED_CLOSED_DEAL, pozycja BLOCKED bez pól, broker pusty."""
    lose_the_ack(broker, **kw)
    stop_engine_at(engine, clock, berlin(DAY, 9, 20))
    await engine.run_day(DAY)
    [order] = engine.journal.unresolved_orders()
    assert order.state is OrderState.UNKNOWN and order.note.split()[0] == "UNMATCHED_CLOSED_DEAL", order
    assert engine.journal.current_position("DE40") == PositionRecord("DE40", P.BLOCKED)
    assert engine.journal.trades() == [] and broker.positions == {}
    return order


def journal_rows(path: Path) -> dict:
    """Cały stan dziennika poza audytem `owner.resolve` (odmowa i przerwanie zapisują tylko ten wiersz)."""
    import sqlite3

    raw = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = {t: raw.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()  # noqa: S608 (stałe nazwy tabel)
                for t in ("orders", "positions", "trades", "realized_pnl", "kill_state", "meta")}  # fmt: skip
        rows["raw_events"] = raw.execute(
            "SELECT source, payload FROM raw_events WHERE source != 'owner.resolve' ORDER BY id"
        ).fetchall()
    finally:
        raw.close()
    return rows


def resolve_audit(journal) -> list[dict]:
    return payloads(journal, "owner.resolve")


async def test_resolve_lost_trade_books_the_worst_case_loss_when_the_broker_knows_no_entry(
    engine, broker, clock, journal_path
):
    order = await lost_trade(engine, broker, clock)
    [closure] = await broker.closed_since(order.created_at)
    lines: list[str] = []
    owner = Typed("DEAL-1", "SZACUNEK")
    before = len(broker.calls)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 0, lines
    assert owner.prompts == [cli.DEAL_PROMPT, cli.ESTIMATE_PROMPT]
    j = engine.journal
    # wejście przyjęte = wyjście + 30 pkt (stop strategii); brutto −30 × 0.001 = −0.03; poślizg STOP 0.0015 → 0.00
    [booked] = j.trades()
    assert (booked.entry_fill, booked.exit_fill, booked.size, booked.exit_reason) == (
        closure.exit_level + 30, closure.exit_level, D("0.001"), "STOP"
    )
    assert booked.decision_bid == booked.decision_ask == booked.entry_fill  # szacunek: bez notowania decyzji
    assert booked.gross_pnl == D("-0.03") and booked.net_pnl == D("-0.03") and j.realized_pnl(DAY) == D("-0.03")
    assert j.trade_flags("DEAL-1") == "ESTIMATED RESOLVED_BY_OWNER"
    rec = j.get_order(LOST_CID)
    assert rec.state is OrderState.CONFIRMED and rec.deal_id == "DEAL-1"
    assert rec.note == (
        "RESOLVED_BY_OWNER BOOKED=ESTIMATED CLOSURE=DEAL-1 BY=wlasciciel CAUSE=open_position:BrokerTimeout"
    )
    assert j.current_position("DE40") == PositionRecord("DE40", P.FLAT) and j.unresolved_orders() == []
    assert j.get_meta("closed_marker") == closure.ts.isoformat()
    assert j.get_meta("entry_by_deal:DEAL-1") is None  # szacunek nie udaje prawdziwego kontekstu wejścia
    booking, done = resolve_audit(j)
    assert (booking["phase"], booking["source"], booking["chosen"], booking["by"]) == (
        "BOOKING", "ESTIMATE", "DEAL-1", "wlasciciel"
    )
    assert booking["estimate"]["assumed_entry"] == str(closure.exit_level + 30)
    assert booking["acknowledged"] == ["DEAL-1", "SZACUNEK"] and len(booking["closures"]) == 1
    assert done == {"event": "OWNER_RESOLVE_LOST_TRADE", "phase": "DONE", "by": "wlasciciel", "deal_id": "DEAL-1",
                    "order_id": LOST_CID, "net_pnl": "-0.03", "booked": "ESTIMATED"}  # fmt: skip
    assert set(broker.calls[before:]) <= BROKER_READS and "open_position" not in broker.calls[before:]
    text = "\n".join(lines)
    assert "  DEAL-1  STOP  poziom" in text and "NAJGORSZA możliwa strata ze stopu (SZACUNEK)" in text
    assert "zaksięgowano transakcję DEAL-1 (SZACUNEK): wynik netto -0.03 EUR, dzień 2026-11-02" in text
    assert "kill switch nadal SOFT" in text and "ACC-" not in text
    assert engine.kill.level() is KillLevel.SOFT  # kill switch zdejmuje właściciel osobno (clear-kill)


async def test_resolve_lost_trade_books_exactly_from_the_confirm_when_the_reference_is_known(engine, broker, clock):
    broker.fill_slippage = D("2")  # prawdziwe wypełnienie 2 pkt gorsze od ask: szacunek by tego nie wiedział
    order = await lost_trade(engine, broker, clock, reference_known=True)
    assert order.deal_reference == "REF-1" and "CAUSE=confirm:BrokerTimeout" in order.note
    later = (clock.now_utc() + timedelta(minutes=1)).isoformat()
    engine.journal.set_meta("closed_marker", later)  # znacznik późniejszy niż to zamknięcie: zostaje (max)
    lines: list[str] = []
    owner = Typed("DEAL-1")
    before = len(broker.calls)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 0, lines
    assert owner.prompts == [cli.DEAL_PROMPT]  # dokładne dane: bez drugiego potwierdzenia
    snapshot = await broker.confirm("REF-1")
    j = engine.journal
    [booked] = j.trades()
    assert booked.entry_fill == snapshot.open_level == MID + 1 + 2 and booked.size == snapshot.size
    assert (booked.decision_bid, booked.decision_ask) == (MID - 1, MID + 1)  # notowanie decyzji z kontekstu wejścia
    assert j.trade_flags("DEAL-1") == "RESOLVED_BY_OWNER"
    assert j.get_meta("entry_by_deal:DEAL-1") == LOST_CID
    assert j.get_order(LOST_CID).note.startswith("RESOLVED_BY_OWNER BOOKED=EXACT CLOSURE=DEAL-1 BY=wlasciciel")
    assert j.current_position("DE40").state is P.FLAT and j.get_meta("closed_marker") == later
    assert resolve_audit(j)[0]["source"] == "CONFIRM" and resolve_audit(j)[0]["estimate"] is None
    assert "confirm" in broker.calls[before:] and set(broker.calls[before:]) <= BROKER_READS
    assert any("dane wejścia z potwierdzenia brokera: kierunek BUY" in line for line in lines)
    assert any("(DOKŁADNIE z potwierdzenia)" in line for line in lines)


async def test_resolve_lost_trade_estimates_when_the_broker_answers_the_reference_with_a_rejection(
    engine, broker, clock
):
    # potwierdzenie stale odpowiada REJECTED (np. odrzucenie, a potem pusta lista i zamknięcie w historii): dokładnych
    # danych wejścia nie będzie nigdy, więc szacunek najgorszej straty (z drugim potwierdzeniem), a nie wieczna odmowa
    await lost_trade(engine, broker, clock, reference_known=True)
    broker._confirms["REF-1"] = _Rejected("REJECTED_SAFETY")
    lines: list[str] = []
    owner = Typed("DEAL-1", "SZACUNEK")
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 0, lines
    assert owner.prompts == [cli.DEAL_PROMPT, cli.ESTIMATE_PROMPT]
    assert engine.journal.trade_flags("DEAL-1") == "ESTIMATED RESOLVED_BY_OWNER"
    assert any("odrzucenie (REJECTED_SAFETY)" in line for line in lines)
    booking = resolve_audit(engine.journal)[0]
    assert booking["source"] == "ESTIMATE" and booking["confirm"] == "REJECTED: REJECTED_SAFETY"


async def test_resolve_lost_trade_never_books_a_profit_from_an_estimate(engine, broker, clock):
    def close_by_hand_with_a_profit(deal_id):  # właściciel zamknął w aplikacji 50 pkt nad wejściem
        broker.positions.pop(deal_id)
        broker._closed.append(ClosedDeal(deal_id, "DE40", MID + 50, "MANUAL", clock.now_utc()))

    await lost_trade(engine, broker, clock, close=close_by_hand_with_a_profit)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lambda _: None) == 0
    [booked] = engine.journal.trades()
    assert booked.exit_fill == MID + 50 and booked.entry_fill == MID + 80 and booked.exit_reason == "MANUAL"
    assert booked.gross_pnl == D("-0.03") and booked.net_pnl < 0  # nadal najgorsza strata ze stopu, nie zysk


async def test_resolve_lost_trade_ignores_a_closure_already_booked_as_another_trade(engine, broker, clock):
    order = await lost_trade(engine, broker, clock)
    # zamknięcie poprzedniej transakcji tuż przed INTENT (w zapasie HISTORY_MARGIN), już w księdze: to nie ta
    earlier = ClosedDeal("DEAL-0", "DE40", MID, "MANUAL", order.created_at - timedelta(seconds=30))
    broker._closed.insert(0, earlier)
    assert engine.journal.record_trade(trade_like(earlier), "DEAL-0", DAY)
    lines: list[str] = []
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lines.append) == 0
    assert any(line.startswith("  DEAL-0  MANUAL") and "już w księdze jako inna transakcja" in line for line in lines)
    assert len(engine.journal.trades()) == 2 and engine.journal.trade_flags("DEAL-0") is None


def trade_like(closed):
    from gwstock.journal import TradeRecord

    t = closed.ts
    return TradeRecord("DE40", Side.BUY, D("0.001"), MID, MID, MID, closed.exit_level, D("0.00"), D("0.00"), D("0.00"),
                       D("0.00"), D("0.00"), D("0.00"), t, t, t, t, 0, "MANUAL")  # fmt: skip


def add_unknown_order(engine, clock, order_id="gws-20261102-DE40-2-entry"):
    t = clock.now_utc()
    engine.journal.insert_order(OrderRecord(
        order_id, "DE40", Side.BUY, D("0.001"), D("30"), OrderState.UNKNOWN, None, None, t, t,
    ))  # fmt: skip


def edit_position(engine, **over):
    engine.journal.upsert_position(replace(engine.journal.current_position("DE40"), **over))


def foreign_position(clock):
    return OpenPosition("DEAL-5", "DE40", Side.BUY, D("0.001"), MID, MID - 30, clock.now_utc())


def contradicting_confirm(broker, **over):
    broker._confirms["REF-1"] = replace(broker._confirms["REF-1"], **over)


REFUSALS = {
    # przypadek: (przygotowanie po zgubionej transakcji, zgubiona z referencją?, odpowiedzi, fragment komunikatu)
    "no_lost_trade": (None, False, (), "nie ma zgubionej transakcji"),
    "two_unresolved_orders": (lambda e, b, c: add_unknown_order(e, c), False, (), "niewyjaśnionych zleceń: 2"),
    "submitting": (
        lambda e, b, c: e.journal.update_order(LOST_CID, state=OrderState.SUBMITTING, updated_at=c.now_utc()), False,
        (), "jest SUBMITTING",
    ),
    "entering": (lambda e, b, c: edit_position(e, state=P.ENTERING), False, (), "pozycja jest ENTERING"),
    "blocked_with_a_deal_id": (lambda e, b, c: edit_position(e, deal_id="DEAL-1"), False, (), "z deal id DEAL-1"),
    "blocked_with_fields": (
        lambda e, b, c: edit_position(e, side=Side.BUY, size=D("0.001")), False, (), "BLOCKED z polami pozycji"
    ),
    "no_bound_account": (
        lambda e, b, c: setattr(e, "runtime", RuntimeConfig(True, None)), False, (), "brak związanego rachunku"
    ),
    "wrong_account": (
        lambda e, b, c: b.on_call("account", lambda: setattr(b, "_active_account_id", "ACC-0")), False, (),
        "nie jest na związanym rachunku",
    ),
    "broker_unreadable": (
        lambda e, b, c: b.fail_next("list_positions", BrokerError("HTTP 502")), False, (), "brokera nie da się odczytać"
    ),
    "position_at_broker": (
        lambda e, b, c: b.positions.update({"DEAL-5": foreign_position(c)}), False, (),
        "broker nadal pokazuje pozycję na DE40",
    ),
    "working_order": (
        lambda e, b, c: b.add_working_order("DE40", Side.BUY, D("0.001"), D("25000")), False, (),
        "zlecenie oczekujące u brokera (WO-1)",
    ),
    "history_unreadable": (
        lambda e, b, c: b.fail_next("closed_since", BrokerError("HISTORY_TRUNCATED")), False, (),
        "historii brokera nie da się odczytać (BrokerError: HISTORY_TRUNCATED)",
    ),
    "older_than_14_days": (lambda e, b, c: c.advance(15 * 86400), False, (), "starsze niż 14 dni"),
    "no_closure_but_one_was_seen": (
        lambda e, b, c: b._closed.clear(), False, (), "historia może się spóźniać",
    ),
    "two_closures_without_entry_data": (
        lambda e, b, c: b._closed.append(ClosedDeal("DEAL-7", "DE40", MID, "MANUAL", c.now_utc())), False,
        ("DEAL-1",), "nie da się udowodnić, które jest tą transakcją",
    ),
    "rules_unreadable": (
        lambda e, b, c: b.fail_next("instrument_rules", BrokerError("HTTP 502")), False, ("DEAL-1",),
        "wartości punktu od brokera nie da się odczytać",
    ),
    "confirm_unreadable": (
        lambda e, b, c: b.fail_next("confirm", BrokerTimeout()), True, ("DEAL-1",),
        "potwierdzenia brokera nie da się teraz odczytać (BrokerTimeout)",
    ),
    "confirm_contradicts": (
        lambda e, b, c: contradicting_confirm(b, side=Side.SELL), True, ("DEAL-1",),
        "potwierdzenie brokera wskazuje DEAL-1 SELL DE40",
    ),
    "confirm_names_another_deal": (
        lambda e, b, c: contradicting_confirm(b, deal_id="DEAL-9"), True, ("DEAL-1",),
        "wskazuje DEAL-9 BUY DE40, a nie wpisane zamknięcie DEAL-1",
    ),
    "value_per_point_mismatch": (
        lambda e, b, c: e.journal.set_meta(
            f"entry_ctx:{LOST_CID}",
            json.dumps({**json.loads(e.journal.get_meta(f"entry_ctx:{LOST_CID}")), "value_per_point": "2"}),
        ), False, ("DEAL-1",), "wartość punktu w kontekście wejścia (2) różni się od reguł brokera (1)",
    ),
}


@pytest.mark.parametrize("case", list(REFUSALS))
async def test_resolve_lost_trade_refuses_and_changes_nothing(engine, broker, clock, journal_path, case):
    setup, reference_known, answers, text = REFUSALS[case]
    if case != "no_lost_trade":
        await lost_trade(engine, broker, clock, reference_known=reference_known)
    if setup is not None:
        setup(engine, broker, clock)
    rows = journal_rows(journal_path)
    before = len(broker.calls)
    lines: list[str] = []
    owner = Typed(*answers)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 1, lines
    assert lines[-1].startswith("odmowa: ") and lines[-1].endswith("; nic nie zmieniono") and text in lines[-1]
    assert journal_rows(journal_path) == rows  # stan dziennika bez zmian: tylko wiersz audytu odmowy
    [audit] = resolve_audit(engine.journal)
    assert audit["phase"] == "REFUSED" and audit["reason"] in lines[-1] and audit["by"] == "wlasciciel"
    assert set(broker.calls[before:]) <= BROKER_READS
    assert len(owner.prompts) == len(answers)


@pytest.mark.parametrize(
    "answers", [("DEAL-2",), ("deal-1",), ("",), ("DEAL-1", "szacunek"), ("DEAL-1", "")],
    ids=["other_deal", "lower_case", "empty", "szacunek_lower_case", "no_szacunek"],
)  # fmt: skip
async def test_resolve_lost_trade_aborts_on_a_wrong_deal_id_or_missing_szacunek(
    engine, broker, clock, journal_path, answers
):
    await lost_trade(engine, broker, clock)
    rows = journal_rows(journal_path)
    lines: list[str] = []
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed(*answers), out=lines.append) == 2
    assert lines[-1].startswith("przerwano: ") and lines[-1].endswith("nic nie zmieniono")
    assert journal_rows(journal_path) == rows
    assert [a["phase"] for a in resolve_audit(engine.journal)] == ["ABORTED"]


async def unseen_unknown_order(engine, clock):
    """UNKNOWN + BLOCKED bez pól, ale bez żadnego śladu pozycji ani zamknięcia u brokera w dzienniku."""
    clock.advance(10 * 60)
    add_unknown_order(engine, clock, LOST_CID)
    engine.journal.upsert_position(PositionRecord("DE40", P.ENTERING))
    engine.journal.upsert_position(PositionRecord("DE40", P.BLOCKED))
    clock.advance(5 * 60)


async def test_resolve_lost_trade_marks_an_order_not_placed_when_the_broker_and_its_history_show_nothing(
    engine, broker, clock
):
    await unseen_unknown_order(engine, clock)
    lines: list[str] = []
    owner = Typed("NIEWYKONANE")
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 0, lines
    assert owner.prompts == [cli.NOT_PLACED_PROMPT.format(order=LOST_CID, epic="DE40")]
    j = engine.journal
    rec = j.get_order(LOST_CID)
    assert rec.state is OrderState.NOT_PLACED and rec.note == "RESOLVED_BY_OWNER NOT_PLACED_NO_CLOSURE BY=wlasciciel"
    assert j.current_position("DE40") == PositionRecord("DE40", P.FLAT) and j.trades() == []
    assert [a["phase"] for a in resolve_audit(j)] == ["NOT_PLACED", "DONE"]
    assert "closed_since" in broker.calls and any("NOT_PLACED (RESOLVED_BY_OWNER)" in line for line in lines)


async def test_resolve_lost_trade_not_placed_needs_the_typed_word(engine, broker, clock, journal_path):
    await unseen_unknown_order(engine, clock)
    rows = journal_rows(journal_path)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("RESET"), out=lambda _: None) == 2
    assert journal_rows(journal_path) == rows


async def test_resolve_lost_trade_rerun_after_a_crash_after_booking_finishes_without_booking_twice(
    engine, broker, clock, monkeypatch
):
    await lost_trade(engine, broker, clock)
    real_book = engine.executor.book

    async def book_then_die(*args, **kwargs):
        await real_book(*args, **kwargs)
        raise Died()

    monkeypatch.setattr(engine.executor, "book", book_then_die)
    with pytest.raises(Died):
        await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lambda _: None)
    j = engine.journal
    assert len(j.trades()) == 1 and j.get_order(LOST_CID).state is OrderState.UNKNOWN
    monkeypatch.setattr(engine.executor, "book", real_book)
    lines: list[str] = []
    owner = Typed("DEAL-1")
    assert await cli.resolve_lost_trade(engine, "wlasciciel", owner, out=lines.append) == 0, lines
    assert owner.prompts == [cli.DEAL_PROMPT]  # już zaksięgowane: bez ponownego szacunku
    assert len(j.trades()) == 1 and j.realized_pnl(DAY) == j.trades()[0].net_pnl
    assert j.get_order(LOST_CID).note.startswith("RESOLVED_BY_OWNER BOOKED=ESTIMATED CLOSURE=DEAL-1")
    assert j.current_position("DE40").state is P.FLAT
    assert [a["phase"] for a in resolve_audit(j)] == ["BOOKING", "RESUMED", "DONE"]
    assert any("już zaksięgowana" in line for line in lines)


async def test_a_crash_after_the_order_write_is_healed_by_reconciliation_without_rebooking(
    engine, broker, clock, monkeypatch
):
    await lost_trade(engine, broker, clock)
    j = engine.journal
    real_update = j.update_order

    def update_then_die(order_id, **fields):
        result = real_update(order_id, **fields)
        if fields.get("state") is OrderState.CONFIRMED:
            raise Died()
        return result

    monkeypatch.setattr(j, "update_order", update_then_die)
    with pytest.raises(Died):
        await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lambda _: None)
    monkeypatch.setattr(j, "update_order", real_update)
    assert j.get_order(LOST_CID).state is OrderState.CONFIRMED and j.current_position("DE40").state is P.BLOCKED
    clock.advance(engine.risk.reconcile_interval_s)
    result = await engine.reconcile_once()
    assert result is not None and not result.blocked and result.closed == []
    assert j.current_position("DE40") == PositionRecord("DE40", P.FLAT) and len(j.trades()) == 1


async def test_resolve_lost_trade_applies_the_loss_limit_of_the_closure_day(engine, broker, clock):
    await lost_trade(engine, broker, clock)
    engine.journal.add_realized_pnl(DAY, D("-2.97"))  # wcześniejsze straty tego dnia; szacunek −0.03 → −3.00
    lines: list[str] = []
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lines.append) == 0
    assert engine.journal.realized_pnl(DAY) == D("-3.00")
    assert engine.kill.level() is KillLevel.HARD and engine.journal.kill_state().reason == "DAILY_LOSS_2X"
    assert "kill switch nadal HARD (DAILY_LOSS_2X)" in lines[-1]


async def test_reset_position_refusal_for_an_unknown_order_points_to_resolve_lost_trade(engine, broker, clock):
    await lost_trade(engine, broker, clock)
    lines: list[str] = []
    assert await cli.reset_position(engine, "wlasciciel", Typed(), out=lines.append) == 1
    assert "UNMATCHED_CLOSED_DEAL" in lines[-1] and "use resolve-lost-trade (runbook, section 8.5)" in lines[-1]


def test_resolve_lost_trade_command_takes_the_instance_lock_first(monkeypatch, tmp_path, capsys):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    monkeypatch.setenv("GW_CONFIG_DIR", str(tmp_path / "missing"))
    with InstanceLock(data / "engine.lock"):
        assert cli.main(["resolve-lost-trade", "--by", "wlasciciel"]) == 3
    assert not (data / "journal.sqlite").exists() and "stop the engine first" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["resolve-lost-trade"])  # bez --by: audyt musi wiedzieć, kto to zrobił
    assert exc.value.code == 2


async def test_status_marks_a_trade_booked_from_an_estimate(make_engine, broker, clock, data_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    make_engine.journal_path = data_dir / "journal.sqlite"
    engine = make_engine(Mode.LIVE)
    await lost_trade(engine, broker, clock)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lambda _: None) == 0
    lines: list[str] = []
    assert cli.status(Path("config"), data_dir, out=lines.append, now=clock.now_utc()) == 0
    [trade_line] = [line for line in lines if line.startswith("  BUY 0.001")]
    assert trade_line.endswith("(STOP) [ESTIMATED]")


async def test_status_lists_every_owner_estimate_also_on_a_later_day_from_one_ledger_read(
    make_engine, broker, clock, data_dir, monkeypatch
):
    # m6/m7: resolve-lost-trade bywa uruchamiane dzień później; szacunek musi być widoczny, a księga i znaczniki
    # czytane jednym odczytem (bez osobnego `trades()`, które przy piszącym silniku mogłoby się rozjechać)
    monkeypatch.setitem(sys.modules, "gwstock.prereg", None)
    make_engine.journal_path = data_dir / "journal.sqlite"
    engine = make_engine(Mode.LIVE)
    await lost_trade(engine, broker, clock)
    assert await cli.resolve_lost_trade(engine, "wlasciciel", Typed("DEAL-1", "SZACUNEK"), out=lambda _: None) == 0
    clock.advance(86400)

    def no_separate_ledger_read(self):
        raise AssertionError("status reads the ledger only through trade_flags()")

    monkeypatch.setattr(Journal, "trades", no_separate_ledger_read)
    lines: list[str] = []
    assert cli.status(Path("config"), data_dir, out=lines.append, now=clock.now_utc()) == 0
    assert "trades today (2026-11-03): 0" in lines
    assert "owner estimates in the ledger: 1 (excluded from cost measurement; check them at Capital.com)" in lines
    [line] = [x for x in lines if x.startswith("  2026-11-02 BUY 0.001")]
    assert line.endswith("(STOP) [ESTIMATED]")
    assert any(x.startswith("live sessions (trades without smoke): 1 (including 1 owner estimate)") for x in lines)


async def test_smoke_hold_keeps_managing_and_counts_down_before_the_flatten(engine, broker, smoke_clock):
    lines: list[str] = []
    closes = calls_at(broker, smoke_clock, "close_position")
    start = smoke_clock.now_utc()
    assert await cli.smoke(engine, out=lines.append, hold_s=60) == 0
    countdown = [line for line in lines if line.startswith("INFO [3] holding")]
    assert [c.split("holding ")[1].split(" s")[0] for c in countdown] == ["60", "45", "30", "15"]
    assert (closes[0] - start).total_seconds() >= 60  # spłaszczenie dopiero po wstrzymaniu
    held = [c for c in broker.calls if c in ("list_positions", "quote")]
    assert held.count("list_positions") >= 6 and held.count("quote") >= 12  # uzgadnianie i notowania w trakcie


async def test_smoke_hold_flattens_at_once_when_in_doubt(engine, broker, smoke_clock, data_dir):
    at_time(broker, smoke_clock, smoke_clock.now_utc() + timedelta(seconds=20),
            lambda: (data_dir / "STOP").write_text(""))  # fmt: skip
    closes = calls_at(broker, smoke_clock, "close_position")
    start = smoke_clock.now_utc()
    lines: list[str] = []
    await cli.smoke(engine, out=lines.append, hold_s=300)
    assert (closes[0] - start).total_seconds() < 60  # wątpliwość (kill switch): bez czekania do końca
    assert any("hold stopped" in line for line in lines)


LEFT_OPEN = "left open with its stop; start the engine (it reconciles and flattens at 17:25) or close it"


def interrupt(exc):
    def raise_it():
        raise exc

    return raise_it


@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt], ids=["cancelled", "keyboard"])
async def test_smoke_hold_interrupted_still_closes_the_probe_position(engine, broker, smoke_clock, exc):
    # sonda H1: anulowanie (SIGINT pod asyncio.run) w trakcie wstrzymania zostawiało pozycję otwartą bez słowa
    at_time(broker, smoke_clock, smoke_clock.now_utc() + timedelta(seconds=20), interrupt(exc()))
    lines: list[str] = []
    with pytest.raises(exc):
        await cli.smoke(engine, out=lines.append, hold_s=300)
    assert broker.positions == {} and "close_position" in broker.calls  # zamknięcie spróbowane i udane
    assert any("hold interrupted" in line for line in lines) and not any(LEFT_OPEN in line for line in lines)


async def test_smoke_hold_interrupted_and_unable_to_close_says_so_loudly(engine, broker, smoke_clock):
    def cancel_with_a_closed_market():
        broker.set_market_closed(True)
        raise asyncio.CancelledError()

    at_time(broker, smoke_clock, smoke_clock.now_utc() + timedelta(seconds=20), cancel_with_a_closed_market)
    lines: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await cli.smoke(engine, out=lines.append, hold_s=300)
    assert "DEAL-1" in broker.positions  # stop u brokera nadal chroni
    assert any("deal DEAL-1 " + LEFT_OPEN in line for line in lines)


async def test_smoke_hold_stop_flag_from_a_signal_ends_the_hold_and_flattens(engine, broker, smoke_clock):
    at_time(broker, smoke_clock, smoke_clock.now_utc() + timedelta(seconds=20), engine.request_shutdown)
    closes = calls_at(broker, smoke_clock, "close_position")
    start = smoke_clock.now_utc()
    lines: list[str] = []
    await cli.smoke(engine, out=lines.append, hold_s=300)
    assert (closes[0] - start).total_seconds() < 60 and broker.positions == {}
    assert any("hold stopped: stop requested" in line for line in lines)


async def test_smoke_hold_journal_error_still_closes_the_position_at_the_broker(
    engine, broker, smoke_clock, monkeypatch
):
    # sonda H2: błąd dziennika w trakcie wstrzymania; zamknięcie nie może od niego zależeć
    journal = engine.journal
    real = journal.current_position
    broken_from = smoke_clock.now_utc() + timedelta(seconds=20)

    def flaky(epic):
        if smoke_clock.now_utc() >= broken_from:
            raise JournalReadError("database is locked")
        return real(epic)

    monkeypatch.setattr(journal, "current_position", flaky)
    lines: list[str] = []
    with pytest.raises(JournalReadError):
        await cli.smoke(engine, out=lines.append, hold_s=300)
    assert broker.positions == {}  # zamknięte u brokera mimo dziennika; uzgadnianie silnika zaksięguje je później
    assert any("hold interrupted (JournalReadError)" in line for line in lines)


async def test_smoke_journal_read_failure_right_after_the_hold_still_closes_the_probe_position(
    engine, broker, smoke_clock, monkeypatch
):
    # h2: odczyt dziennika tuż po wstrzymaniu (stan po nim) zawodzi: pozycja próby nie może zostać otwarta bez słowa
    journal = engine.journal
    real_position, real_hold = journal.current_position, cli._hold
    after_hold = {"v": False}

    async def hold(*args, **kwargs):
        await real_hold(*args, **kwargs)
        after_hold["v"] = True

    def position(epic):
        if after_hold["v"]:
            raise JournalReadError("database is locked")
        return real_position(epic)

    monkeypatch.setattr(cli, "_hold", hold)
    monkeypatch.setattr(journal, "current_position", position)
    lines: list[str] = []
    with pytest.raises(JournalReadError):
        await cli.smoke(engine, out=lines.append, hold_s=30)
    assert broker.positions == {} and "close_position" in broker.calls
    assert any("hold interrupted (JournalReadError)" in line for line in lines)


async def test_smoke_close_after_interrupt_survives_a_second_cancellation_and_waits_for_the_close(
    engine, broker, smoke_clock, monkeypatch
):
    # h4: drugie Ctrl-C w trakcie zamykania pozycji próby: czekamy na zamknięcie, zanim wypiszemy wynik
    outer = asyncio.current_task()
    at_time(broker, smoke_clock, smoke_clock.now_utc() + timedelta(seconds=20), interrupt(asyncio.CancelledError()))
    real_close = broker.close_position

    async def slow_close(deal_id):
        outer.cancel()  # drugie przerwanie, gdy zamknięcie jest w drodze
        for _ in range(5):
            await asyncio.sleep(0)
        await real_close(deal_id)

    monkeypatch.setattr(broker, "close_position", slow_close)
    lines: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await cli.smoke(engine, out=lines.append, hold_s=300)
    assert broker.positions == {}
    assert any("closed after the interrupt" in line for line in lines) and not any(LEFT_OPEN in line for line in lines)
    assert outer.uncancel() == 0  # jedno przerwanie zgłoszone, jedno połknięte w oczekiwaniu na zamknięcie


async def test_smoke_first_signal_restores_the_default_handlers(engine):
    # h5: pierwszy sygnał ustawia flagę zatrzymania, a drugi ma już zwykłą obsługę (zawieszona próba da się zatrzymać)
    import signal

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    with cli._stop_on_signals(engine):
        assert all(signal.getsignal(sig) is not before[sig] for sig in before)  # obsługa pętli zdarzeń
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
        assert engine.shutdown_requested
        assert {sig: signal.getsignal(sig) for sig in before} == before  # już po pierwszym sygnale
    assert {sig: signal.getsignal(sig) for sig in before} == before


async def test_smoke_installs_stop_handlers_for_sigterm_and_sigint(engine):
    import os
    import signal

    with cli._stop_on_signals(engine):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)  # handler pętli zdarzeń, nie domyślne zakończenie procesu
    assert engine.shutdown_requested


async def test_the_smoke_command_runs_the_probe_with_the_stop_handlers_installed(engine, monkeypatch, tmp_path):
    import os
    import signal

    async def fake_with_engine(paths, action, *, lock=None):
        return await action(engine)

    async def fake_smoke(eng, out=print, hold_s=0):
        installed = signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None)
        if not installed:  # bez obsługi sygnał zakończyłby proces testów: tylko zgłaszamy brak
            return 1
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
        return 0 if eng.shutdown_requested and hold_s == 30 else 1

    monkeypatch.setattr(cli, "_with_engine", fake_with_engine)
    monkeypatch.setattr(cli, "smoke", fake_smoke)
    paths = cli.Paths(tmp_path / "c", tmp_path / "d", tmp_path / "k.json")
    (tmp_path / "d").mkdir()
    assert await cli._smoke_engine(paths, 30) == 0


@pytest.mark.parametrize("value", ["-1", "601", "x"])
def test_smoke_hold_is_limited_to_600_seconds(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["smoke", "--hold", value])
    assert exc.value.code == 2


async def test_smoke_trades_do_not_count_as_live_sessions(make_engine, broker, smoke_clock, clock, data_dir):
    make_engine.journal_path = data_dir / "journal.sqlite"
    engine = make_engine(Mode.LIVE)
    assert await cli.smoke(engine, out=lambda _: None) == 0
    j = engine.journal
    assert len(j.trades()) == 1 and j.count_smoke_trades() == 1
    assert engine.session_index() == 0
    lines: list[str] = []
    assert cli.status(Path("config"), engine.data_dir, out=lines.append, now=clock.now_utc()) == 0
    assert any("live sessions (trades without smoke): 0" in line for line in lines)


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
