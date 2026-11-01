"""Polecenia właściciela: `python -m gwstock` (silnik), `bind-account`, `smoke`, `status`, `health` (spec §9, §13).

Uzbrajanie (spec §9): tryb `live` tylko gdy JEDNOCZEŚNIE `live_allowed = true` w `config/runtime.toml`, istnieje
`data/ARM_LIVE` (tworzy go wyłącznie właściciel; kod nigdy), rachunek jest związany (`data/account.json` z
`bind-account`) i odcisk konfiguracji równa się zamrożonemu w `data/preregistration.json` (klucz `fingerprint`).
Inaczej `dry_run`. Odcisk liczy moduł `gwstock.prereg` (Zadanie 15), importowany leniwie: brak modułu, funkcji albo
pliku z zamrożonym odciskiem = None = `dry_run`. Silnik przy każdym starcie dnia sprawdza jeszcze rachunek u brokera
(`Engine.verify_account`) i w razie problemu zostaje w `dry_run`.

Zmienne środowiskowe: `GW_CONFIG_DIR` (domyślnie `config`), `GW_DATA_DIR` (domyślnie `data`),
`GW_CAPITALCOM_KEY_FILE` (domyślnie `/secrets/capitalcom.json`). Sekretów nie ma w logu ani w wyjściu poleceń;
saldo rachunków `bind-account` wypisuje wyłącznie na terminal właściciela.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import json
import logging
import math
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from gwstock.app import (
    ACCOUNT_CURRENCY,
    ACCOUNT_STATUS,
    ARM_FILE,
    HEARTBEAT_FILE,
    LOCK_FILE,
    Engine,
    Mode,
    arm_file_exists,
)
from gwstock.config import RuntimeConfig, StrategyConfig, load_config, load_runtime
from gwstock.journal import JournalError
from gwstock.strategy import EntrySignal, atr14, round_up_to_step
from gwstock.types import BrokerRejected, OrderState, PositionState, Side
from gwstock.watchdog import AlreadyRunning, InstanceLock, heartbeat_age_s, read_heartbeat

__all__ = [
    "Mode", "BindAccountError", "resolve_mode", "mode_decision", "bind_account", "smoke", "status", "health", "main",
]  # fmt: skip

_LOG = logging.getLogger("gwstock.cli")

ENV_CONFIG_DIR = "GW_CONFIG_DIR"
ENV_DATA_DIR = "GW_DATA_DIR"
ENV_KEY_FILE = "GW_CAPITALCOM_KEY_FILE"
DEFAULT_CONFIG_DIR = "config"
DEFAULT_DATA_DIR = "data"
DEFAULT_KEY_FILE = "/secrets/capitalcom.json"

PREREG_FILE = "preregistration.json"
ACCOUNT_FILE = "account.json"
JOURNAL_FILE = "journal.sqlite"
HEALTH_MAX_AGE_S = 120
# Wejścia odcisku pre-rejestracji (ruling: bez runtime.toml, który zmienia właściciel).
PREREG_CONFIG_FILES = ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml")
PREREG_ADAPTER = "capitalcom-live"
PREREG_INSTRUMENT_CLASS = "index-cfd"
OVERWRITE_WORD = "OVERWRITE"
SMOKE_SETTLE_ATTEMPTS = 10
SMOKE_SETTLE_PAUSE_S = 3
SMOKE_CANDLE_LOOKBACK_MIN = 30


# --- tryb ---------------------------------------------------------------------------------------------------


def mode_decision(
    runtime: RuntimeConfig, arm_file_exists: bool, config_fingerprint: str | None, frozen_fingerprint: str | None
) -> tuple[Mode, list[str]]:
    """Tryb i brakujące warunki uzbrojenia (pusta lista = wszystkie spełnione, `live`)."""
    missing = []
    if runtime.live_allowed is not True:
        missing.append("live_allowed is not true in config/runtime.toml")
    if arm_file_exists is not True:
        missing.append(f"data/{ARM_FILE} does not exist (only the owner creates it)")
    if not runtime.account_id:
        missing.append("no bound account in data/account.json (run bind-account)")
    if frozen_fingerprint is None:
        missing.append(f"no frozen pre-registration fingerprint in data/{PREREG_FILE}")
    elif config_fingerprint is None:
        missing.append("the configuration fingerprint cannot be computed (gwstock.prereg unavailable)")
    elif config_fingerprint != frozen_fingerprint:
        missing.append("the configuration fingerprint differs from the frozen pre-registration")
    return (Mode.DRY_RUN if missing else Mode.LIVE), missing


def resolve_mode(
    runtime: RuntimeConfig, arm_file_exists: bool, config_fingerprint: str | None, frozen_fingerprint: str | None
) -> Mode:
    """`Mode.LIVE` tylko przy wszystkich czterech warunkach uzbrojenia (spec §9); inaczej `Mode.DRY_RUN`."""
    return mode_decision(runtime, arm_file_exists, config_fingerprint, frozen_fingerprint)[0]


def config_fingerprint(config_dir: Path) -> str | None:
    """Odcisk bieżącej konfiguracji z `gwstock.prereg` (leniwy import); None, gdy modułu lub funkcji nie ma.

    Oczekiwany interfejs Zadania 15: `config_fingerprint(config_dir) -> str` albo
    `fingerprint(paths, adapter, instrument_class) -> str`. Każdy błąd = None (`dry_run`).
    """
    try:
        prereg = importlib.import_module("gwstock.prereg")
    except ImportError:
        return None
    try:
        direct = getattr(prereg, "config_fingerprint", None)
        if callable(direct):
            value = direct(Path(config_dir))
        else:
            paths = [Path(config_dir) / name for name in PREREG_CONFIG_FILES]
            value = prereg.fingerprint(paths, PREREG_ADAPTER, PREREG_INSTRUMENT_CLASS)
    except Exception as exc:
        _LOG.warning("configuration fingerprint unavailable: %s", type(exc).__name__)
        return None
    return value if isinstance(value, str) and value else None


def frozen_fingerprint(data_dir: Path) -> str | None:
    """Zamrożony odcisk z `data/preregistration.json` (klucz `fingerprint`); None: brak albo nieczytelny."""
    try:
        data = json.loads((Path(data_dir) / PREREG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("fingerprint") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def runtime_mode(config_dir: Path, data_dir: Path, runtime: RuntimeConfig) -> tuple[Mode, list[str]]:
    """Tryb z plików (bez brokera): runtime.toml, ARM_LIVE, account.json, odcisk pre-rejestracji."""
    return mode_decision(
        runtime, arm_file_exists(data_dir), config_fingerprint(config_dir), frozen_fingerprint(data_dir)
    )


# --- bind-account ---------------------------------------------------------------------------------------------


class BindAccountError(Exception):
    """`bind-account` przerwane: nic nie zapisano."""


def _read_binding(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("account_id") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return "<unreadable>"
    return value if isinstance(value, str) and value else "<unreadable>"


def _write_binding(path: Path, account_id: str) -> None:
    """Zapis atomowy z uprawnieniami 600: plik tymczasowy w tym samym katalogu, fsync, `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps({"account_id": account_id}) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


async def bind_account(
    broker: Any, ask: Callable[[str], str], data_dir: Path, out: Callable[[str], None] = print
) -> str:
    """Wybór rachunku przez właściciela (spec §9, pytanie 19); zwraca związany identyfikator.

    Logowanie bez związanego rachunku, lista rachunków (id, waluta, status, saldo; tylko na terminalu), wybór NUMEREM,
    odmowa rachunku z saldem 0, w walucie innej niż EUR albo nie ENABLED, potwierdzenie ostatnimi 4 znakami
    identyfikatora, jawne OVERWRITE przy zmianie istniejącego powiązania. Zapis `data/account.json` (600) dopiero po
    wszystkich potwierdzeniach. Każde przerwanie: `BindAccountError`, plik bez zmian.
    """
    path = Path(data_dir) / ACCOUNT_FILE
    existing = _read_binding(path)
    await broker.login()
    accounts = await broker.list_accounts()
    if not accounts:
        raise BindAccountError("the login lists no accounts; nothing written")
    out("Accounts of this login (shown only on this terminal; nothing is written yet):")
    for i, a in enumerate(accounts, start=1):
        out(
            f"  {i}) {a.account_id}  currency {a.currency}  status {a.status}  balance {a.equity}  "
            f"available {a.available}"
        )
    out("  (the account type is not exposed by the API adapter; the Capital.com app shows it)")
    if existing is not None:
        out(f"Currently bound: {existing}")
    answer = ask(f"Number of the account to bind (1-{len(accounts)}): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(accounts):
        raise BindAccountError(f"{answer!r} is not a number from the list; nothing written")
    chosen = accounts[int(answer) - 1]
    problems = []
    if chosen.status != ACCOUNT_STATUS:
        problems.append(f"status {chosen.status} (must be {ACCOUNT_STATUS})")
    if chosen.currency != ACCOUNT_CURRENCY:
        problems.append(f"currency {chosen.currency} (must be {ACCOUNT_CURRENCY})")
    if not chosen.equity > 0:
        problems.append("balance 0 (the engine trades only a funded account)")
    if problems:
        raise BindAccountError(f"account {answer} cannot be bound: {', '.join(problems)}; nothing written")
    if existing is not None and existing != chosen.account_id:
        word = ask(f"Another account is bound. Type {OVERWRITE_WORD} to replace the binding: ").strip()
        if word != OVERWRITE_WORD:
            raise BindAccountError("the existing binding is kept; nothing written")
    tail = chosen.account_id[-4:]
    if ask("Type the last 4 characters of the chosen account id to confirm: ").strip() != tail:
        raise BindAccountError("the confirmation does not match the chosen account; nothing written")
    _write_binding(path, chosen.account_id)
    out(f"Bound account {chosen.account_id} written to {path} (mode 600).")
    return chosen.account_id


# --- smoke (L0) --------------------------------------------------------------------------------------------------


def smoke_stop_distance(rules: Any, atr: Decimal | None, strat: StrategyConfig) -> Decimal:
    """Stop próby: stop strategii dla bieżącego ATR (bez ATR: 2 × minimalny stop), w górę do kroku stopu."""
    if atr is not None and atr > 0:
        distance = max(strat.stop_atr_fraction * atr, rules.min_stop_distance)
    else:
        distance = rules.min_stop_distance * 2
    if rules.stop_step > 0:
        distance = round_up_to_step(distance, rules.stop_step)
    return distance


async def smoke(engine: Engine, out: Callable[[str], None] = print) -> int:
    """Próba akceptacyjna L0 (tylko `live`): jedno minimalne wejście ze stopem, natychmiastowe zamknięcie i kontrole.

    Kroki PASS/FAIL/INFO (bez sekretów): rachunek, wejście, widoczność pozycji, spłaszczenie, księga, drugie zamknięcie
    (`POSITION_NOT_FOUND`, założenie ścieżki awaryjnej wykonawcy), ostatnie świece 5-minutowe do porównania z wykresem
    (semantyka stempla, L0) i opóźnienie historii. Kod wyjścia 0 tylko, gdy każdy krok PASS przeszedł.
    """
    results: list[bool] = []

    def step(n: int, ok: bool, text: str) -> bool:
        out(f"{'PASS' if ok else 'FAIL'} [{n}] {text}")
        results.append(ok)
        return ok

    def info(n: int, text: str) -> None:
        out(f"INFO [{n}] {text}")

    if engine.mode is not Mode.LIVE:
        out("FAIL smoke runs only in live mode (live_allowed, data/ARM_LIVE, bound account, pre-registration); "
            "the engine is in dry_run, nothing was sent")  # fmt: skip
        return 1
    engine.check_wiring()
    epic, journal, clock = engine.strat.instrument, engine.journal, engine.clock
    day = engine.trading_day()

    # [1] rachunek
    if not await engine.login():
        step(1, False, "login failed")
        return 1
    try:
        problem = await engine.verify_account()
    except Exception as exc:
        step(1, False, f"account unreadable ({type(exc).__name__})")
        return 1
    if problem is not None:
        step(1, False, f"account check: {problem}")
        return 1
    step(1, True, "bound account found, ENABLED, EUR, balance > 0, active in this session")

    engine.kill.scan_files()
    result = await engine.reconcile_once()
    position = journal.current_position(epic)
    unresolved = [o for o in journal.unresolved_orders() if o.epic == epic]
    busy = (position is not None and position.state is not PositionState.FLAT) or bool(unresolved)
    if result is None or result.skipped or busy:
        step(2, False, "smoke refuses: reconciliation failed or a position/unresolved order exists on the instrument")
        return 1

    # [2] wejście
    try:
        rules = await engine.broker.instrument_rules(epic)
        quote = await engine.broker.quote(epic)
    except Exception as exc:
        step(2, False, f"rules/quote unavailable ({type(exc).__name__}: {exc})")
        return 1
    try:
        atr = atr14(await engine.broker.daily_candles(epic, 15))
    except Exception:
        atr = None
        info(2, "ATR unavailable: stop = 2 x the broker minimum")
    stop = smoke_stop_distance(rules, atr, engine.strat)
    signal_ = EntrySignal(Side.BUY, stop, rules.min_size, quote)
    rec = await engine.executor.enter(signal_, rules, day, smoke=True)
    if rec is None or rec.state is not OrderState.CONFIRMED:
        blocks = journal.events("gate_block")
        why = json.loads(blocks[-1].payload).get("reasons") if rec is None and blocks else None
        step(2, False, f"entry not confirmed: {rec.state.value if rec else 'not sent'}"
             + (f" (gate: {', '.join(why)})" if why else ""))  # fmt: skip
        if rec is not None and rec.state not in (OrderState.REJECTED, OrderState.NOT_PLACED):
            info(2, "the entry outcome is unresolved: a position may exist with its broker stop; check the Capital.com "
                 "app and start the engine, whose startup reconciliation adopts or blocks it before anything else")
        return 1
    deal_id = rec.deal_id
    position = journal.current_position(epic)
    at_broker = await _find_position(engine, deal_id)
    derived = at_broker.stop_derived if at_broker is not None else None
    step(
        2, position is not None and position.state is PositionState.OPEN,
        f"entry accepted: deal {deal_id}, open level {position.open_level if position else '?'}, stop level "
        f"{position.stop_level if position else '?'}, size {position.size if position else '?'}, stop "
        f"{'derived from our request' if derived else 'reported by the API' if derived is False else 'unknown'}",
    )  # fmt: skip
    if derived:
        info(2, "the API did not show the stop: verify the stop in the Capital.com app before arming")

    # [3] widoczność
    visible = at_broker is not None and at_broker.stop_level is not None
    step(3, visible, "position visible in list_positions with a stop")

    # [4] spłaszczenie wykonawcą (DELETE po dealId)
    closed_at = clock.now_utc()
    if position is not None and position.state is PositionState.OPEN:
        await engine.executor.flatten(position, "SMOKE")
    for _ in range(SMOKE_SETTLE_ATTEMPTS):
        current = journal.current_position(epic)
        if current is None or current.state is PositionState.FLAT:
            break
        await clock.sleep(SMOKE_SETTLE_PAUSE_S)
        await engine.reconcile_once()
        current = journal.current_position(epic)
        if current is not None and current.state is PositionState.EXITING:
            await engine.executor.flatten(current, "SMOKE")
    current = journal.current_position(epic)
    step(4, current is None or current.state is PositionState.FLAT, "flatten through the executor (DELETE by dealId)")

    # [5] pozycja znikła i transakcja zaksięgowana
    gone = await _find_position(engine, deal_id) is None
    booked = journal.has_trade(deal_id)
    costs = ""
    if booked:
        t = journal.trades()[-1]
        costs = (f": gross {t.gross_pnl}, spread {t.spread_cost}, slippage {t.slippage_penalty}, fees {t.fees}, "
                 f"financing {t.financing}, net {t.net_pnl}, exit {t.exit_reason}")  # fmt: skip
    step(5, gone and booked, f"position gone at the broker and the trade booked{costs}")

    # [6] drugie zamknięcie: ma być POSITION_NOT_FOUND (założenie ścieżki awaryjnej wykonawcy). Tylko gdy pozycji
    # na pewno nie ma: inaczej „drugie" zamknięcie byłoby pierwszym zamknięciem pozycji, która nadal jest otwarta.
    if gone:
        await _second_close_check(engine, deal_id, step, out, results)
    else:
        info(6, "skipped: position not confirmed gone (steps 4/5 failed); the engine's reconciliation handles it")

    # [7] ostatnie świece 5-minutowe: stempel do porównania z wykresem (L0)
    now = clock.now_utc()
    try:
        candles = (await engine.broker.candles_5m(epic, now - timedelta(minutes=SMOKE_CANDLE_LOOKBACK_MIN), now))[-3:]
    except Exception as exc:
        candles = []
        info(7, f"candles unavailable ({type(exc).__name__}: {exc})")
    for c in candles:
        local = engine.calendar.to_local(c.start)
        info(7, f"5m candle start {c.start.isoformat()} UTC ({local:%H:%M} {local.tzname()}): open {c.open} high "
             f"{c.high} low {c.low} close {c.close}")  # fmt: skip
    info(7, "compare these candles with the Capital.com 5-minute chart (mid prices): the bar starting at 09:00 "
         "Europe/Berlin must be the 09:00-09:05 bar; arm only after this matches")  # fmt: skip
    step(7, bool(candles), "completed 5-minute candles readable")

    # [8] opóźnienie historii
    latency = _history_latency(journal, deal_id, closed_at)
    info(8, f"history latency: the closure appeared in closed_since {latency:.1f} s after the close was sent"
         if latency is not None else "history latency: not measured (no closed_since read showed the closure)")

    passed = all(results)
    out(f"{'PASS' if passed else 'FAIL'} smoke: {sum(results)}/{len(results)} checks passed")
    return 0 if passed else 1


async def _second_close_check(
    engine: Engine, deal_id: str, step: Callable[[int, bool, str], bool], out: Callable[[str], None], results: list
) -> None:
    epic = engine.strat.instrument
    outcome, ok = "accepted (!)", False
    try:
        await engine.probe(engine.raw_broker.close_position, deal_id)
    except BrokerRejected as exc:
        outcome, ok = f"BrokerRejected({exc.reason})", exc.reason == "POSITION_NOT_FOUND"
    except Exception as exc:
        outcome = f"{type(exc).__name__}: {exc}"
    if ok:
        step(6, True, f"second close of {deal_id} rejected with POSITION_NOT_FOUND")
        return
    leftovers = [p for p in (await _positions(engine)) if p.epic == epic]
    engine.kill.soft("SMOKE_SECOND_CLOSE_UNEXPECTED")
    engine.alerts.alert("ERROR", f"SMOKE_SECOND_CLOSE_UNEXPECTED: {outcome}; positions on {epic}: {leftovers}")
    now_open = [(p.deal_id, p.side.value, str(p.size)) for p in leftovers]
    out(f"FAIL [6] LOUD: a second close of {deal_id} gave {outcome} instead of POSITION_NOT_FOUND; the emergency "
        f"path relies on it. Positions on {epic} now: {now_open}")  # fmt: skip
    results.append(False)


async def _positions(engine: Engine) -> list:
    try:
        return await engine.broker.list_positions()
    except Exception:
        return []


async def _find_position(engine: Engine, deal_id: str | None):
    return next((p for p in await _positions(engine) if p.deal_id == deal_id), None)


def _history_latency(journal: Any, deal_id: str, since: datetime) -> float | None:
    sent = None
    for e in journal.events("broker.close_position"):
        data = json.loads(e.payload)
        if data.get("deal_id") == deal_id and "error" not in data and e.ts >= since:
            sent = e.ts
            break
    if sent is None:
        return None
    for e in journal.events("broker.closed_since"):
        if e.ts < sent:
            continue
        response = json.loads(e.payload).get("response") or []
        if any(isinstance(c, dict) and c.get("deal_id") == deal_id for c in response):
            return (e.ts - sent).total_seconds()
    return None


# --- status, health ------------------------------------------------------------------------------------------------


def status(config_dir: Path, data_dir: Path, out: Callable[[str], None] = print, now: datetime | None = None) -> int:
    """Stan tylko do odczytu: tryb z plików i powód, kill switch, pozycja i zlecenia, tętno, ostatnia decyzja,
    dzisiejsze transakcje. Niczego nie zapisuje (dziennik otwierany tylko, gdy istnieje)."""
    from gwstock.calendar import XETRA, ExchangeCalendar
    from gwstock.journal import Journal

    config_dir, data_dir = Path(config_dir), Path(data_dir)
    now = now or datetime.now(timezone.utc)
    try:
        strat = load_config(config_dir)[0]
        epic = strat.instrument
    except Exception as exc:
        out(f"config: unreadable ({type(exc).__name__}: {exc})")
        epic = "DE40"
    try:
        runtime = load_runtime(config_dir, data_dir)
        mode, missing = runtime_mode(config_dir, data_dir, runtime)
        out(f"mode (files): {mode.value} ({'; '.join(missing) if missing else 'all arming conditions met'})")
        out("  the running engine also checks the bound account at the broker at each start and may stay in dry_run")
    except Exception as exc:
        out(f"mode (files): dry_run (runtime/account file unreadable: {type(exc).__name__}: {exc})")
    beat = read_heartbeat(data_dir / HEARTBEAT_FILE)
    age = heartbeat_age_s(data_dir / HEARTBEAT_FILE, now)
    if age is None:
        out("heartbeat: none")
    else:
        out(f"heartbeat: {age:.0f} s ago, engine mode {beat.get('mode')}, state {beat.get('state')}, kill "
            f"{beat.get('kill_level')}, last quote {beat.get('last_quote_ts')}")  # fmt: skip
    path = data_dir / JOURNAL_FILE
    if not path.exists():
        out("journal: none yet")
        return 0
    journal = Journal.open_readonly(str(path))  # status nigdy nie pisze (sqlite mode=ro)
    try:
        ks = journal.kill_state()
        out(f"kill switch: {ks.level.name}" + (f" ({ks.reason}, since {ks.since.isoformat()})" if ks.since else ""))
        pos = journal.current_position(epic)
        if pos is None or pos.state is PositionState.FLAT:
            out(f"position {epic}: FLAT")
        else:
            out(f"position {epic}: {pos.state.value} deal {pos.deal_id} {pos.side.value if pos.side else '?'} size "
                f"{pos.size} open {pos.open_level} stop {pos.stop_level}")  # fmt: skip
        orders = journal.unresolved_orders()
        out(f"unresolved orders: {len(orders)}")
        for o in orders:
            out(f"  {o.order_id} {o.state.value} {o.side.value} {o.size} ({o.note})")
        decisions = [json.loads(e.payload) for e in journal.events("decision")]
        decisions = [d for d in decisions if d.get("event") == "DECISION"]
        if decisions:
            d = decisions[-1]
            candle = d.get("first_candle") or {}
            ohlc = " ".join(f"{k[0].upper()} {candle.get(k)}" for k in ("open", "high", "low", "close"))
            out(f"last decision {d.get('day')}: {d.get('outcome')} {d.get('skip_reason') or ''} mode {d.get('mode')} "
                f"first candle {candle.get('start')} {ohlc}")  # fmt: skip
        else:
            out("last decision: none")
        calendar = ExchangeCalendar(XETRA)
        today = calendar.to_local(now).date()
        todays = [t for t in journal.trades() if calendar.to_local(t.exit_ts).date() == today]
        out(f"trades today ({today}): {len(todays)}")
        for t in todays:
            out(f"  {t.side.value} {t.size} entry {t.entry_fill} exit {t.exit_fill} net {t.net_pnl} ({t.exit_reason})")
    finally:
        journal.close()
    return 0


def health(data_dir: Path, now: datetime | None = None, max_age_s: float = HEALTH_MAX_AGE_S) -> int:
    """Healthcheck Dockera: 0, gdy tętno jest świeższe niż `max_age_s`; inaczej 1."""
    age = heartbeat_age_s(Path(data_dir) / HEARTBEAT_FILE, now or datetime.now(timezone.utc))
    return 0 if age is not None and math.isfinite(age) and -max_age_s < age < max_age_s else 1


# --- main ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    key_file: Path


def paths_from_env(env: dict[str, str] | None = None) -> Paths:
    env = os.environ if env is None else env
    return Paths(
        Path(env.get(ENV_CONFIG_DIR, DEFAULT_CONFIG_DIR)),
        Path(env.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)),
        Path(env.get(ENV_KEY_FILE, DEFAULT_KEY_FILE)),
    )


@dataclass(frozen=True)
class RunContext:
    cfgs: tuple
    runtime: RuntimeConfig
    mode: Mode
    missing: list[str]
    creds: Any


def prepare_run(paths: Paths) -> RunContext:
    """Konfiguracja, tryb z plików i dane logowania z `account_id` z `data/account.json` (bez sieci)."""
    from gwstock.broker.capitalcom import load_credentials

    cfgs = load_config(paths.config_dir)
    runtime = load_runtime(paths.config_dir, paths.data_dir)
    mode, missing = runtime_mode(paths.config_dir, paths.data_dir, runtime)
    creds = dataclasses.replace(load_credentials(paths.key_file), account_id=runtime.account_id)
    return RunContext(cfgs, runtime, mode, missing, creds)


def build_engine(
    ctx: RunContext,
    http: Any,
    journal: Any,
    clock: Any,
    data_dir: Path,
    *,
    config_dir: Path | None = None,
    lock: InstanceLock | None = None,
) -> Engine:
    """Silnik na prawdziwym adapterze Capital.com (wyłącznie host live) i kalendarzu XETRA.

    `config_dir`: silnik live ponownie sprawdza w nim `live_allowed` (na początku dnia i przed wejściem).
    `lock`: blokada instancji wzięta już przez polecenie (przed dziennikiem i siecią).
    """
    from gwstock.broker.capitalcom import LIVE_BASE_URL, CapitalComBroker
    from gwstock.calendar import XETRA, ExchangeCalendar

    broker = CapitalComBroker(ctx.creds, http, clock, base_url=LIVE_BASE_URL)
    return Engine(
        broker, journal, clock, ExchangeCalendar(XETRA), ctx.cfgs, data_dir, ctx.runtime, ctx.mode,
        config_dir=config_dir, lock=lock,
    )  # fmt: skip


def _log_mode(ctx: RunContext) -> None:
    if ctx.mode is Mode.LIVE:
        _LOG.info("mode: live (all four arming conditions met; the bound account is checked at the broker next)")
    else:
        _LOG.info("mode: dry_run, no orders will be sent: %s", "; ".join(ctx.missing))


async def _with_engine(paths: Paths, action: Callable[[Engine], Any], *, lock: InstanceLock | None = None) -> int:
    import aiohttp

    from gwstock.clock import SystemClock
    from gwstock.journal import Journal

    ctx = prepare_run(paths)
    _log_mode(ctx)
    clock = SystemClock()
    async with aiohttp.ClientSession() as http:
        journal = Journal.open(str(paths.data_dir / JOURNAL_FILE))
        try:
            engine = build_engine(ctx, http, journal, clock, paths.data_dir, config_dir=paths.config_dir, lock=lock)
            return await action(engine)
        finally:
            journal.close()


async def _run_engine(paths: Paths) -> int:
    async def run(engine: Engine) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, engine.request_shutdown)
        await engine.run_forever()
        return 0

    with InstanceLock(paths.data_dir / LOCK_FILE) as lock:  # blokada przed konfiguracją, dziennikiem i siecią
        return await _with_engine(paths, run, lock=lock)


async def _smoke_engine(paths: Paths) -> int:
    with InstanceLock(paths.data_dir / LOCK_FILE) as lock:  # nigdy obok działającego silnika: blokada najpierw
        return await _with_engine(paths, smoke, lock=lock)


async def _bind(paths: Paths) -> int:
    import aiohttp

    from gwstock.broker.capitalcom import LIVE_BASE_URL, CapitalComBroker, load_credentials
    from gwstock.clock import SystemClock

    creds = dataclasses.replace(load_credentials(paths.key_file), account_id=None)
    with InstanceLock(paths.data_dir / LOCK_FILE):
        async with aiohttp.ClientSession() as http:
            broker = CapitalComBroker(creds, http, SystemClock(), base_url=LIVE_BASE_URL)
            try:
                await bind_account(broker, input, paths.data_dir)
            except BindAccountError as exc:
                print(f"bind-account aborted: {exc}")
                return 2
    return 0


def _cmd_run(paths: Paths, args: argparse.Namespace) -> int:
    return asyncio.run(_run_engine(paths))


def _cmd_smoke(paths: Paths, args: argparse.Namespace) -> int:
    return asyncio.run(_smoke_engine(paths))


def _cmd_bind(paths: Paths, args: argparse.Namespace) -> int:
    return asyncio.run(_bind(paths))


def _cmd_status(paths: Paths, args: argparse.Namespace) -> int:
    return status(paths.config_dir, paths.data_dir)


def _cmd_health(paths: Paths, args: argparse.Namespace) -> int:
    return health(paths.data_dir)


COMMANDS: dict[str, Callable[[Paths, argparse.Namespace], int]] = {
    "run": _cmd_run,
    "bind-account": _cmd_bind,
    "smoke": _cmd_smoke,
    "status": _cmd_status,
    "health": _cmd_health,
}

_HELP = {
    "run": "run the engine (default without a command)",
    "bind-account": "choose the trading account (owner, interactive)",
    "smoke": "L0 acceptance probe: one minimal entry with a stop, closed at once (live only)",
    "status": "read-only state: mode, kill switch, position, orders, heartbeat, last decision, today's trades",
    "health": "exit 0 if the heartbeat is fresher than 120 s (Docker healthcheck)",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gwstock", description="gw-stock-engine (DE40 CFD, Capital.com)")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name in COMMANDS:
        sub.add_parser(name, help=_HELP[name])
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command not in ("health", "status"):
        logging.basicConfig(
            level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    paths = paths_from_env()
    try:
        return COMMANDS[command](paths, args)
    except AlreadyRunning as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (ValueError, OSError, JournalError) as exc:  # konfiguracja, sekrety, account.json, dziennik: bez sekretów
        print(f"cannot run {command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
