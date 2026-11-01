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
import contextlib
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
    ALERTS_FILE,
    ARM_FILE,
    HEARTBEAT_FILE,
    L1_PASSED_FILE,
    LOCK_FILE,
    Engine,
    Mode,
    arm_file_exists,
)
from gwstock.config import RuntimeConfig, StrategyConfig, load_config, load_runtime
from gwstock.execution import FLAGS_BY_OWNER, META_ENTRY_BY_DEAL, META_ENTRY_CTX, SOURCE_EVENT, BookingError
from gwstock.journal import JournalError, PositionRecord
from gwstock.orders.fsm import check_order_transition, check_position_transition
from gwstock.orders.manager import SOURCE_EVENT as SOURCE_ORDER_EVENT
from gwstock.reconcile import SOURCE_EVENT as SOURCE_RECONCILE_EVENT
from gwstock.serialization import dump
from gwstock.strategy import EntrySignal, atr14, round_up_to_step
from gwstock.types import BrokerRejected, KillLevel, OrderState, PositionState, Side
from gwstock.watchdog import AlreadyRunning, InstanceLock, heartbeat_age_s, read_heartbeat

__all__ = [
    "Mode", "BindAccountError", "resolve_mode", "mode_decision", "bind_account", "smoke", "status", "health", "main",
    "clear_kill", "reset_position", "resolve_lost_trade",
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
SMOKE_HOLD_MAX_S = 600  # `smoke --hold`: najdłużej 10 minut z otwartą pozycją próby
SMOKE_HOLD_COUNTDOWN_S = 15
SMOKE_HOLD_TICK_S = 5
SMOKE_CLOSE_WAIT_SUBMITS = 3  # zamknięcie pozycji próby po przerwaniu: czekamy najwyżej 3 × submit_timeout_s
ADOPTED_AFTER_UNKNOWN = "ADOPTED_AFTER_UNKNOWN"  # notatka menedżera: wejście przyjęte z listy/potwierdzenia po UNKNOWN
ADOPTED_WARNING = "WARN [2]"
CLEAR_WORD = "CLEAR"
RESET_WORD = "RESET"
NO_DEAL_ID_WORD = "NO-DEAL-ID"
SOURCE_OWNER_RESET = "owner.reset"
# resolve-lost-trade: komunikaty dla właściciela po polsku, słowa potwierdzeń w ASCII
SOURCE_OWNER_RESOLVE = "owner.resolve"
RESOLVE_EVENT = "OWNER_RESOLVE_LOST_TRADE"
NOTHING_CHANGED = "nic nie zmieniono"
DEAL_PROMPT = "Wpisz identyfikator zamknięcia (deal id) z alarmu UNMATCHED_CLOSED_DEAL, które jest tą transakcją: "
ESTIMATE_WORD = "SZACUNEK"
ESTIMATE_PROMPT = "Wpisz SZACUNEK, aby zaksięgować oszacowaną stratę: "  # runbook cytuje dosłownie
NOT_PLACED_WORD = "NIEWYKONANE"
NOT_PLACED_PROMPT = (
    "Wpisz NIEWYKONANE, aby uznać zlecenie {order} za niewykonane (NOT_PLACED) i ustawić pozycję {epic} na FLAT: "
)
LEFT_OPEN_HINT = (
    "left open with its stop; start the engine (it reconciles and flattens at 17:25) or close it in the Capital.com app"
)


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


async def smoke(engine: Engine, out: Callable[[str], None] = print, hold_s: int = 0) -> int:
    """Próba akceptacyjna L0 (tylko `live`): jedno minimalne wejście ze stopem, natychmiastowe zamknięcie i kontrole.

    Kroki PASS/FAIL/INFO (bez sekretów): rachunek, wejście, widoczność pozycji, spłaszczenie, księga, drugie zamknięcie
    (`POSITION_NOT_FOUND`, założenie ścieżki awaryjnej wykonawcy), ostatnie świece 5-minutowe do porównania z wykresem
    (semantyka stempla, L0) i opóźnienie historii. Kod wyjścia 0 tylko, gdy każdy krok PASS przeszedł.

    `hold_s` (0-600): po sprawdzeniu pozycji tyle sekund normalnego nadzoru (uzgadnianie, notowania, tętno), żeby
    właściciel obejrzał pozycję i jej stop w aplikacji; każda wątpliwość (kill switch, pozycja nie OPEN, nieudane
    uzgadnianie) kończy wstrzymanie i spłaszcza od razu.
    """
    if isinstance(hold_s, bool) or not isinstance(hold_s, int) or not 0 <= hold_s <= SMOKE_HOLD_MAX_S:
        raise ValueError(f"hold_s: expected an integer from 0 to {SMOKE_HOLD_MAX_S}, got {hold_s!r}")
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
    adopted = ADOPTED_AFTER_UNKNOWN in rec.note.split()
    if adopted:  # nie FAIL: wejście jest nasze i zabezpieczone, ale właściciel musi obejrzeć odpowiedź potwierdzenia
        out(f"{ADOPTED_WARNING}: wejście przyjęte przez uzgadnianie, nie przez potwierdzenie — sprawdź w "
            f"dzienniku/aplikacji (notatka zlecenia: {rec.note}); surowa odpowiedź potwierdzenia jest w dzienniku "
            "(broker.confirm); runbook, punkt 5")  # fmt: skip
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
    if hold_s > 0 and visible and position is not None and position.state is PositionState.OPEN:
        try:
            await _hold(engine, deal_id, hold_s, info)
            position = journal.current_position(epic)  # stan po wstrzymaniu (np. wybicie stopem w jego trakcie)
        except GeneratorExit:  # zamykana korutyna nie może już czekać
            raise
        except BaseException as exc:  # anulowanie, Ctrl-C, sygnał, błąd: pozycja próby nie może zostać bez słowa
            info(3, f"hold interrupted ({type(exc).__name__}); closing the probe position now")
            await _close_after_interrupt(engine, deal_id, out)
            raise

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
    flat = current is None or current.state is PositionState.FLAT
    step(4, flat, "flatten through the executor (DELETE by dealId)")
    if not flat and await _still_at_broker(engine, deal_id) is not False:
        out(f"FAIL [4] LOUD: deal {deal_id} {LEFT_OPEN_HINT}")

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
    note = f" ({ADOPTED_WARNING}: entry adopted after UNKNOWN, read it above)" if adopted else ""
    out(f"{'PASS' if passed else 'FAIL'} smoke: {sum(results)}/{len(results)} checks passed{note}")
    return 0 if passed else 1


async def _hold(engine: Engine, deal_id: str | None, hold_s: int, info: Callable[[int, str], None]) -> None:
    """Wstrzymanie z otwartą pozycją próby: zwykły nadzór; wątpliwość → koniec wstrzymania (spłaszczenie od razu)."""
    clock, epic = engine.clock, engine.strat.instrument
    start = clock.now_utc()
    end = start + timedelta(seconds=hold_s)
    next_print, next_reconcile = start, start
    while True:
        now = clock.now_utc()
        remaining = (end - now).total_seconds()
        if remaining <= 0:
            info(3, "hold finished: flattening now")
            return
        if now >= next_print:
            info(3, f"holding {remaining:.0f} s more: look at the position and its stop in the Capital.com app")
            next_print += timedelta(seconds=SMOKE_HOLD_COUNTDOWN_S)
        doubt = None
        engine.kill.scan_files()
        if now >= next_reconcile:
            next_reconcile += timedelta(seconds=engine.risk.reconcile_interval_s)
            result = await engine.reconcile_once()
            if result is None or result.skipped or result.blocked:
                doubt = "reconciliation failed or blocked"
        try:
            await engine.broker.quote(epic)  # karmi straż ciszy notowań
        except Exception as exc:
            doubt = doubt or f"quote unavailable ({type(exc).__name__})"
        await engine.watchdog.tick()
        position = engine.journal.current_position(epic)
        if position is None or position.state is not PositionState.OPEN or position.deal_id != deal_id:
            doubt = doubt or f"journal position is {position.state.value if position else 'missing'}"
        if engine.kill.level() is not KillLevel.NONE:
            doubt = doubt or f"kill switch {engine.kill.level().name} ({engine.kill.reason()})"
        if engine.shutdown_requested:  # SIGTERM/SIGINT w trakcie wstrzymania: zamknąć teraz, nie po czasie
            doubt = doubt or "stop requested (signal)"
        if doubt is not None:
            info(3, f"hold stopped: {doubt}; flattening at once")
            return
        await clock.sleep(min(SMOKE_HOLD_TICK_S, remaining))


async def _still_at_broker(engine: Engine, deal_id: str | None) -> bool | None:
    """Czy broker nadal pokazuje deal (None: lista nieczytelna, czyli nie wiadomo)."""
    try:
        positions = await engine.probe(engine.raw_broker.list_positions)
    except BaseException:  # także kolejne anulowanie: nie wiemy
        return None
    return any(p.deal_id == deal_id for p in positions)


async def _close_after_interrupt(engine: Engine, deal_id: str | None, out: Callable[[str], None]) -> None:
    """Po przerwaniu wstrzymania: zamknięcie pozycji próby osłonięte przed kolejnym anulowaniem (biegnie jako osobne
    zadanie, a kolejne przerwania nie kończą czekania na nie, najwyżej `SMOKE_CLOSE_WAIT_SUBMITS` × submit_timeout_s);
    dopiero potem linia wyniku, a gdy się nie udało, głośna linia, co zostało u brokera i co zrobić.

    Najpierw procedura awaryjna wykonawcy (dziennik wg możliwości); jeśli broker nadal pokazuje deal (na przykład
    dziennik leży), jedno bezpośrednie zamknięcie po dealId: to nasza pozycja próby, a zamknięcie bez dziennika
    uzgadnianie silnika zaksięguje później z historii. Bez czytelnej listy pozycji nic nie wysyłamy na ślepo.
    """

    async def attempt() -> None:
        try:
            await engine.executor.emergency_flatten()
        except Exception as exc:
            _LOG.warning("smoke emergency close via the executor failed: %s: %s", type(exc).__name__, exc)
        if await _still_at_broker(engine, deal_id):
            try:
                await engine.probe(engine.raw_broker.close_position, deal_id)
            except Exception as exc:
                _LOG.warning("smoke direct close failed: %s: %s", type(exc).__name__, exc)

    task = asyncio.ensure_future(attempt())
    loop = asyncio.get_running_loop()
    wait_s = SMOKE_CLOSE_WAIT_SUBMITS * engine.risk.submit_timeout_s
    deadline = loop.time() + wait_s
    while not task.done():  # kolejne przerwania nie kończą czekania: wynik dopiero po zamknięciu (albo po limicie)
        remaining = deadline - loop.time()
        if remaining <= 0:
            _LOG.warning("smoke close after an interrupt still running after %s s; reporting now", wait_s)
            break
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.TimeoutError:
            _LOG.warning("smoke close after an interrupt still running after %s s; reporting now", wait_s)
            break
        except BaseException as exc:  # np. drugie Ctrl-C: zamknięcie biegnie dalej, czekamy na nie
            _LOG.warning("smoke close after an interrupt was itself interrupted: %s; still waiting for the close",
                         type(exc).__name__)  # fmt: skip
    if await _still_at_broker(engine, deal_id) is not False:
        out(f"FAIL [4] LOUD: deal {deal_id} {LEFT_OPEN_HINT}")
    else:
        out(f"INFO [4] deal {deal_id} closed after the interrupt; the engine's reconciliation books it if needed")


@contextlib.contextmanager
def _stop_on_signals(engine: Engine):
    """Pierwszy SIGTERM/SIGINT ustawia flagę zatrzymania silnika (wstrzymanie `smoke` ją sprawdza) zamiast przerywać
    proces i od razu przywraca poprzednią obsługę obu sygnałów: drugi sygnał działa zwykle (zawieszoną próbę da się
    zatrzymać)."""
    loop = asyncio.get_running_loop()
    previous: dict[signal.Signals, Any] = {}

    def restore() -> None:
        for sig, handler in list(previous.items()):
            loop.remove_signal_handler(sig)
            if handler is not None:
                signal.signal(sig, handler)
            del previous[sig]

    def first_signal() -> None:
        engine.request_shutdown()
        restore()

    for sig in (signal.SIGTERM, signal.SIGINT):
        handler = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, first_signal)
        except (NotImplementedError, RuntimeError, ValueError):  # np. nie w głównym wątku: zostaje domyślna obsługa
            continue
        previous[sig] = handler
    try:
        yield
    finally:
        restore()


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
        sessions = len(journal.trades()) - journal.count_smoke_trades()
        l1 = (data_dir / L1_PASSED_FILE).exists()
        out(f"live sessions (trades without smoke): {sessions}; {L1_PASSED_FILE}: {'yes' if l1 else 'no'}")
        calendar = ExchangeCalendar(XETRA)
        today = calendar.to_local(now).date()
        booked = list(zip(journal.trades(), journal.trade_flags(), strict=True))
        todays = [(t, flags) for t, flags in booked if calendar.to_local(t.exit_ts).date() == today]
        out(f"trades today ({today}): {len(todays)}")
        for t, flags in todays:
            mark = " [ESTIMATED]" if "ESTIMATED" in (flags or "").split() else ""  # szacunek właściciela, nie pomiar
            out(f"  {t.side.value} {t.size} entry {t.entry_fill} exit {t.exit_fill} net {t.net_pnl} ({t.exit_reason})"
                + mark)  # fmt: skip
    finally:
        journal.close()
    return 0


def health(data_dir: Path, now: datetime | None = None, max_age_s: float = HEALTH_MAX_AGE_S) -> int:
    """Healthcheck Dockera: 0, gdy tętno jest świeższe niż `max_age_s`; inaczej 1."""
    age = heartbeat_age_s(Path(data_dir) / HEARTBEAT_FILE, now or datetime.now(timezone.utc))
    return 0 if age is not None and math.isfinite(age) and -max_age_s < age < max_age_s else 1


# --- polecenia operatorskie: clear-kill, reset-position -----------------------------------------------------------


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def clear_kill(
    data_dir: Path, by: str, ask: Callable[[str], str], out: Callable[[str], None] = print, clock: Any = None
) -> int:
    """Zdjęcie kill switcha przez właściciela (silnik zatrzymany: blokada instancji bierze wywołujący).

    Odmowa (1): brak dziennika albo poziom NONE, istniejący `data/STOP` lub `data/HARD_STOP`. Wypisuje poziom,
    przyczynę i ostatnie alarmy; bez wpisanego `CLEAR` nic nie zmienia (2). `KillSwitch.clear` zapisuje stan NONE
    i wiersz audytu `kill.audit` (CLEAR, kto). 0: zdjęty.
    """
    from gwstock.clock import SystemClock
    from gwstock.journal import Journal
    from gwstock.notify import FileNotifier
    from gwstock.risk import KillSwitch

    data_dir = Path(data_dir)
    path = data_dir / JOURNAL_FILE
    if not path.exists():
        out("kill switch: NONE (no journal yet); nothing to clear")
        return 1
    clock = clock if clock is not None else SystemClock()
    journal = Journal.open(str(path))
    try:
        kill = KillSwitch(journal, clock, data_dir, FileNotifier(data_dir / ALERTS_FILE, clock))
        level, reason = kill.level(), kill.reason()
        if level is KillLevel.NONE:
            out("kill switch: NONE; nothing to clear")
            return 1
        present = [name for name in ("HARD_STOP", "STOP") if (data_dir / name).exists()]
        if present:
            out(f"refused: data/{present[0]} exists; remove it first, otherwise the engine engages the kill switch "
                "again at once")  # fmt: skip
            return 1
        since = journal.kill_state().since
        out(f"kill switch: {level.name} ({reason}" + (f", since {since.isoformat()})" if since else ")"))
        out("last alerts:")
        for line in _tail(data_dir / ALERTS_FILE, 10):
            out(f"  {line}")
        if ask(f"Type {CLEAR_WORD} to clear the kill switch: ").strip() != CLEAR_WORD:
            out("aborted: the kill switch stays as it is")
            return 2
        try:
            kill.clear(requested_by=by)
        except ValueError as exc:
            out(f"refused: {exc}")
            return 1
        out(f"kill switch cleared by {by.strip()} (was {level.name}: {reason}); start the engine")
        return 0
    finally:
        journal.close()


async def reset_position(engine: Engine, by: str, ask: Callable[[str], str], out: Callable[[str], None] = print) -> int:
    """Wyczyszczenie lokalnego stanu pozycji (BLOCKED/ENTERING → FLAT) przez właściciela, gdy broker jest pusty.

    Odmowa (1): pozycja już FLAT; nierozstrzygnięte zlecenie w dzienniku (rozstrzyga uzgadnianie silnika); pozycja
    OPEN/EXITING (silnik sam ją uzgadnia i księguje: reset mógłby pominąć zaksięgowanie prawdziwego zamknięcia);
    brak powiązanego rachunku, sesja na innym rachunku, broker nieczytelny; broker pokazuje pozycję albo zlecenie
    oczekujące na instrumencie; historia zamknięć nieczytelna (każdy błąd); zamknięcie deal_id JEST w historii (jedno):
    wtedy księguje je uzgadnianie silnika, a reset by je zgubił na zawsze (bez deal_id w dzienniku nikt go już nie
    znajdzie). Początek historii jak w uzgadnianiu (`reconcile.history_start`); starszy niż okno historii brokera: bez
    odczytu (HISTORY_OUT_OF_RANGE). Potwierdzenie (inaczej 2, nic nie zmienione): przy deal_id bez zamknięcia w historii
    (albo historii niejednoznacznej, z każdym zamknięciem wypisanym) właściciel wpisuje DEAL ID jako jawne przyjęcie, że
    transakcja nie zostanie zaksięgowana; przy BLOCKED z polami pozycji, ale bez deal_id, wpisuje `NO-DEAL-ID`; w
    pozostałych stanach `RESET`. Zdarzenie audytowe `owner.reset` (kto, poprzedni stan, niezaksięgowany deal, wynik
    historii, zamknięcia tego deala, potwierdzenie) przed zapisem FLAT przez automat stanów. Wyjście nie zawiera
    identyfikatorów rachunków. 0: wyczyszczone.
    """
    journal, epic = engine.journal, engine.strat.instrument
    position = journal.current_position(epic)
    if position is None or position.state is PositionState.FLAT:
        out(f"position {epic}: FLAT; nothing to reset")
        return 1
    unresolved = [o.order_id for o in journal.unresolved_orders() if o.epic == epic]
    if unresolved:
        out(f"refused: unresolved orders in the journal ({', '.join(unresolved)}): start the engine and let it "
            "reconcile them; if the alarm says UNMATCHED_CLOSED_DEAL and the broker shows nothing, use "
            "resolve-lost-trade (runbook, section 8.5)")  # fmt: skip
        return 1
    if position.state in (PositionState.OPEN, PositionState.EXITING):
        out(f"refused: position {epic} is {position.state.value}: the engine reconciles and books it itself; start "
            "the engine (reset-position is for BLOCKED)")  # fmt: skip
        return 1
    bound = engine.runtime.account_id
    if bound is None:
        out("refused: no bound account (run bind-account)")
        return 1
    if not await engine.login():
        out("refused: login failed")
        return 1
    try:
        active = await engine.probe(engine.raw_broker.account)
        positions = await engine.probe(engine.raw_broker.list_positions)
        working = await engine.probe(engine.raw_broker.list_working_orders)
    except Exception as exc:
        out(f"refused: the broker cannot be read ({type(exc).__name__}: {exc})")
        return 1
    if active.account_id != bound:
        out("refused: the session is not on the bound account; nothing checked, nothing changed")
        return 1
    mine = [p for p in positions if p.epic == epic]
    if mine:
        deals = ", ".join(f"{p.deal_id} {p.side.value} {p.size}" for p in mine)
        out(f"refused: the broker still shows a position on {epic} ({deals}); decide about it in the Capital.com app")
        return 1
    orders = [w.order_id for w in working if w.epic == epic]
    if orders:
        out(f"refused: a working order on {epic} exists at the broker ({', '.join(orders)}); cancel it in the app")
        return 1
    deal_id = position.deal_id
    history, closures = "NOT_APPLICABLE", []
    if deal_id is not None:
        checked = await _reset_history_check(engine, position, out)
        if checked is None:
            return 1
        history, closures = checked
    out(f"journal position {epic}: {position.state.value} deal {deal_id} side "
        f"{position.side.value if position.side else '?'} size {position.size} open level {position.open_level} "
        f"stop {position.stop_level} opened at {position.opened_at.isoformat() if position.opened_at else '?'}")
    out(f"broker: no position and no working order on {epic} on the bound account")
    has_fields = position.side is not None or position.size is not None or position.open_level is not None
    if deal_id is not None:
        if not journal.has_trade(deal_id):
            out(f"WARNING: deal {deal_id} is not booked in the ledger and its closure is not bookable from the broker "
                f"history ({history}): its result will NOT be in the loss limits; check the Capital.com activity")
        word, prompt = deal_id, f"Type the deal id {deal_id} to accept that it stays unbooked and set FLAT: "
    elif position.state is PositionState.BLOCKED and has_fields:
        out("WARNING: the journal position has position fields but no deal id: it cannot be matched with the broker "
            "history or booked; check the Capital.com activity")  # fmt: skip
        word, prompt = NO_DEAL_ID_WORD, f"Type {NO_DEAL_ID_WORD} to accept it and set the journal position FLAT: "
    else:
        word, prompt = RESET_WORD, f"Type {RESET_WORD} to set the journal position FLAT: "
    typed = ask(prompt).strip()
    if typed != word:
        out("aborted: nothing changed")
        return 2
    now = engine.clock.now_utc()
    journal.append_raw(
        SOURCE_OWNER_RESET,
        dump({"event": "OWNER_RESET", "by": by.strip(), "previous": position, "unbooked_deal": deal_id,
              "history": history, "closures": closures, "acknowledged": typed, "broker_positions": [],
              "working_orders": []}),  # fmt: skip
        now,
    )
    check_position_transition(position.state, PositionState.FLAT)
    journal.upsert_position(PositionRecord(epic, PositionState.FLAT))
    out(f"position {epic} set FLAT by {by.strip()} (was {position.state.value}); start the engine")
    return 0


async def _reset_history_check(
    engine: Engine, position: PositionRecord, out: Callable[[str], None]
) -> tuple[str, list] | None:
    """Historia zamknięć deal_id pozycji BLOCKED (pełna albo wyjątek) od tego samego początku, co uzgadnianie:
    `reconcile.history_start` (późniejszy z: znacznik ostatniego zaksięgowanego zamknięcia, otwarcie; minus
    HISTORY_MARGIN).

    None: odmowa (już wypisana): zamknięcie do zaksięgowania przez silnik albo historia nieczytelna. Inaczej (wynik do
    audytu, zamknięcia tego deala): `NO_CLOSURE`, `AMBIGUOUS_<n>_CLOSURES` (każde wypisane), `START_UNKNOWN` (bez czasu
    otwarcia i bez znacznika nie da się jej sprawdzić), `HISTORY_OUT_OF_RANGE` (początek dawniej niż okno historii
    brokera, 14 dni: odczyt i tak by odmówił, a ponowienie niczego nie zmieni).
    """
    from gwstock.broker.capitalcom_history import HISTORY_MAX_RANGE
    from gwstock.reconcile import history_start

    deal_id, epic = position.deal_id, position.epic
    start = history_start(engine.journal, position)
    if start is None:
        out(f"note: neither the open time of deal {deal_id} nor a booked closure is known, so its closure cannot be "
            "looked up in the history")  # fmt: skip
        return "START_UNKNOWN", []
    if engine.clock.now_utc() - start > HISTORY_MAX_RANGE:
        out(f"note: deal {deal_id} opened more than {HISTORY_MAX_RANGE.days} days ago: the broker history no longer "
            "covers it, so its closure cannot be looked up")  # fmt: skip
        return "HISTORY_OUT_OF_RANGE", []
    try:
        closures = await engine.probe(engine.raw_broker.closed_since, start)
    except Exception as exc:
        out(f"refused: the broker history cannot be read ({type(exc).__name__}: {exc}); nothing changed, try again "
            "later")  # fmt: skip
        return None
    found = [c for c in closures if c.deal_id == deal_id and c.epic == epic]
    if len(found) == 1:
        c = found[0]
        out(f"refused: the closure of deal {deal_id} is in the broker history ({c.reason} at {c.exit_level}, "
            f"{c.ts.isoformat()}): the engine books it; start the engine and let its reconciliation book it, then "
            "run reset-position again only if the position is still BLOCKED")  # fmt: skip
        return None
    for c in found:  # niejednoznaczna historia: każde zamknięcie na ekranie i w audycie
        out(f"closure of {deal_id}: {c.reason} at {c.exit_level}, {c.ts.isoformat()}")
    return (f"AMBIGUOUS_{len(found)}_CLOSURES" if found else "NO_CLOSURE"), found


# --- resolve-lost-trade (BUG-E2E-2) ---------------------------------------------------------------------------


async def resolve_lost_trade(
    engine: Engine, by: str, ask: Callable[[str], str], out: Callable[[str], None] = print
) -> int:
    """Rozstrzygnięcie „zgubionej transakcji" przez właściciela (silnik zatrzymany: blokadę bierze wywołujący).

    Stan: jedno zlecenie UNKNOWN na instrumencie i pozycja BLOCKED bez deal_id i bez pól (np. UNMATCHED_CLOSED_DEAL:
    broker przyjął wejście, odpowiedź zginęła, a stop zamknął pozycję, zanim silnik ją zobaczył). Uzgadnianie czeka
    wtedy na właściciela, a `reset-position` odmawia. Zawsze na dowodach brokera (tylko ODCZYTY), nigdy na samym słowie
    właściciela:

    (i) zamknięcie w pełnej historii brokera od INTENT − HISTORY_MARGIN → księgowanie jedyną ścieżką
        (`Executor.book` → `Journal.record_trade`): DOKŁADNIE z `confirm(ref)` (znacznik RESOLVED_BY_OWNER), a gdy
        broker nie zna wejścia (brak referencji, `confirm` zwraca None albo odrzucenie) SZACUNEK najgorszej straty ze
        stopu (wejście = wyjście ± odległość stopu, znacznik ESTIMATED, dodatkowe potwierdzenie `SZACUNEK`; zysku
        z szacunku nigdy nie księgujemy), i to tylko przy jednym możliwym zamknięciu; chwilowy błąd `confirm` to odmowa
        (dokładne dane mogą istnieć);
    (ii) broker bez pozycji i zlecenia, historia pełna i bez zamknięcia, a dziennik nie zapisał dla zlecenia żadnej
        pozycji ani zamknięcia u brokera → zlecenie NOT_PLACED, pozycja FLAT po wpisaniu `NIEWYKONANE`;
    (iii) wszystko inne → odmowa z dokładnym powodem.

    Kolejność zapisów po księgowaniu (każde przerwanie jest bezpieczne): księga (idempotentna; ponowne uruchomienie po
    przerwaniu tylko dokańcza zapisy), zlecenie UNKNOWN → CONFIRMED (deal_id, notatka RESOLVED_BY_OWNER ...), pozycja
    BLOCKED → FLAT (przerwanie przed tym zapisem dokańcza uzgadnianie), znacznik historii `closed_marker`. Kill switcha
    nie dotyka. Audyt `owner.resolve` w każdym przypadku (odmowa i przerwanie wg możliwości); bez identyfikatorów
    rachunków. Kody: 0 rozstrzygnięte, 1 odmowa (stan bez zmian), 2 przerwane przy pytaniu (stan bez zmian).
    """
    return await _LostTrade(engine, by, ask, out).run()


class _LostTrade:
    """Jedno uruchomienie `resolve-lost-trade` (patrz `resolve_lost_trade`)."""

    def __init__(self, engine: Engine, by: str, ask: Callable[[str], str], out: Callable[[str], None]) -> None:
        self.engine, self.ask, self.out = engine, ask, out
        self.journal, self.epic, self.clock = engine.journal, engine.strat.instrument, engine.clock
        self.who = by.strip()

    # --- wynik i audyt ---

    def audit(self, phase: str, *, mandatory: bool = True, **details: Any) -> None:
        payload = {"event": RESOLVE_EVENT, "phase": phase, "by": self.who, **details}
        try:
            self.journal.append_raw(SOURCE_OWNER_RESOLVE, dump(payload), self.clock.now_utc())
        except JournalError:
            if mandatory:
                raise
            _LOG.warning("resolve-lost-trade: audit row %s not written", phase, exc_info=True)

    def refuse(self, code: str, text: str) -> int:
        self.out(f"odmowa: {text}; {NOTHING_CHANGED}")
        self.audit("REFUSED", mandatory=False, code=code, reason=text)
        return 1

    def abort(self, text: str = "") -> int:
        self.out(f"przerwano: {text + '; ' if text else ''}{NOTHING_CHANGED}")
        self.audit("ABORTED", mandatory=False, reason=text)
        return 2

    # --- przebieg ---

    async def run(self) -> int:
        from gwstock.broker.capitalcom_history import HISTORY_MAX_RANGE
        from gwstock.orders.manager import HISTORY_MARGIN

        journal, epic, engine = self.journal, self.epic, self.engine

        # 1. dziennik: dokładnie ten stan (rozpoznany po stanach, nie po treści notatki)
        orders = [o for o in journal.unresolved_orders() if o.epic == epic]
        position = journal.current_position(epic) or PositionRecord(epic, PositionState.FLAT)
        if len(orders) != 1:
            return self.refuse(
                "NOTHING_TO_RESOLVE",
                "w dzienniku nie ma zgubionej transakcji do rozstrzygnięcia (potrzebne dokładnie jedno zlecenie "
                f"UNKNOWN na {epic} i pozycja BLOCKED bez deal id; niewyjaśnionych zleceń: {len(orders)})",
            )
        order = orders[0]
        if order.state is not OrderState.UNKNOWN or position.state is not PositionState.BLOCKED or position.deal_id:
            deal = f" z deal id {position.deal_id}" if position.deal_id else ""
            return self.refuse(
                "ENGINE_DECIDES",
                f"zlecenie {order.order_id} jest {order.state.value} albo pozycja jest {position.state.value}{deal}: "
                "uruchom silnik, niech uzgadnianie je rozstrzygnie (runbook 8.5)",
            )
        if _position_fields(position):
            return self.refuse(
                "BLOCKED_WITH_FIELDS",
                f"pozycja {epic} w dzienniku jest BLOCKED z polami pozycji (kierunek, wielkość albo poziomy), ale bez "
                "deal id: tego przypadku polecenie nie rozstrzyga; zapytaj Claude",
            )

        # 2. broker: związany rachunek, bez pozycji i bez zlecenia oczekującego na instrumencie
        bound = engine.runtime.account_id
        if bound is None:
            return self.refuse("NO_BOUND_ACCOUNT", "brak związanego rachunku (uruchom bind-account)")
        if not await engine.login():
            return self.refuse("LOGIN_FAILED", "logowanie do brokera nie powiodło się")
        try:
            active = await engine.probe(engine.raw_broker.account)
            positions = await engine.probe(engine.raw_broker.list_positions)
            working = await engine.probe(engine.raw_broker.list_working_orders)
        except Exception as exc:
            return self.refuse("BROKER_UNREADABLE", f"brokera nie da się odczytać ({type(exc).__name__}: {exc})")
        if active.account_id != bound:
            return self.refuse("WRONG_ACCOUNT", "sesja nie jest na związanym rachunku")
        mine = [p for p in positions if p.epic == epic]
        if mine:
            deals = ", ".join(f"{p.deal_id} {p.side.value} {p.size}" for p in mine)
            return self.refuse(
                "POSITION_AT_BROKER",
                f"broker nadal pokazuje pozycję na {epic} ({deals}): zdecyduj o niej w aplikacji Capital.com",
            )
        pending = [w.order_id for w in working if w.epic == epic]
        if pending:
            return self.refuse(
                "WORKING_ORDER",
                f"na {epic} jest zlecenie oczekujące u brokera ({', '.join(pending)}): anuluj je w aplikacji",
            )

        # 3. pełna historia zamknięć od INTENT − HISTORY_MARGIN (adapter: całość albo wyjątek)
        since = order.created_at - HISTORY_MARGIN
        if self.clock.now_utc() - since > HISTORY_MAX_RANGE:
            return self.refuse(
                "HISTORY_OUT_OF_RANGE",
                f"zlecenie {order.order_id} jest starsze niż {HISTORY_MAX_RANGE.days} dni, historia brokera go już "
                "nie obejmuje; zapytaj Claude",
            )
        try:
            closures = await engine.probe(engine.raw_broker.closed_since, since)
        except Exception as exc:
            return self.refuse(
                "HISTORY_UNREADABLE",
                f"historii brokera nie da się odczytać ({type(exc).__name__}: {exc}); spróbuj ponownie za kilka minut",
            )
        listed = [c for c in closures if c.epic == epic and c.ts >= since]
        others = {c.deal_id for c in listed if journal.has_trade(c.deal_id) and not _owner_booked(journal, c.deal_id)}
        candidates = [c for c in listed if c.deal_id not in others]
        self._print_state(order, since, listed, others)
        if not candidates:
            return self._not_placed(order, since, others)

        # 4. które zamknięcie: identyfikator z alarmu, wpisany (nigdy z opcji polecenia)
        typed = self.ask(DEAL_PROMPT).strip()
        chosen = [c for c in candidates if c.deal_id == typed]
        if not chosen:
            return self.abort(f"{typed!r} nie jest żadnym z wypisanych zamknięć")
        if len(chosen) > 1:
            return self.refuse(
                "AMBIGUOUS_CLOSURE",
                f"historia pokazuje {len(chosen)} zamknięcia deala {typed}: jest niejednoznaczna; zapytaj Claude",
            )
        closed = chosen[0]
        acknowledged = [typed]
        if journal.has_trade(closed.deal_id):  # przerwane wcześniejsze uruchomienie: zaksięgowane, dokańczamy zapisy
            flags = (journal.trade_flags(closed.deal_id) or "").split()
            booked_as = "ESTIMATED" if "ESTIMATED" in flags else "EXACT"
            self.out(f"transakcja {closed.deal_id} jest już zaksięgowana przez wcześniejsze, przerwane uruchomienie: "
                     "dokańczam zapisy bez ponownego księgowania")  # fmt: skip
            self.audit("RESUMED", order_id=order.order_id, chosen=closed.deal_id, booked=booked_as,
                       acknowledged=acknowledged)  # fmt: skip
            return self._finish(order, closed, booked_as)

        # 5. dane wejścia: potwierdzenie brokera (dokładnie) albo szacunek najgorszej straty ze stopu
        snapshot = None
        confirm = "NO_REFERENCE"
        if order.deal_reference is not None:
            try:
                snapshot = await engine.probe(engine.raw_broker.confirm, order.deal_reference)
                confirm = "SNAPSHOT" if snapshot is not None else "NONE"
            except BrokerRejected as exc:
                # trwała odpowiedź brokera (odrzucenie albo przyjęcie bez danych na tę referencję): dokładnych danych
                # wejścia nie będzie nigdy, więc szacunek; ponowienie niczego by nie zmieniło
                confirm = f"REJECTED: {exc.reason}"
                self.out(f"potwierdzenie brokera na referencję {order.deal_reference} to odrzucenie ({exc.reason}): "
                         "broker nie poda danych wejścia")  # fmt: skip
            except Exception as exc:
                return self.refuse(
                    "CONFIRM_UNREADABLE",
                    f"potwierdzenia brokera nie da się teraz odczytać ({type(exc).__name__}); szacunku nie księgujemy, "
                    "gdy dokładne dane mogą istnieć; spróbuj ponownie",
                )
            if snapshot is not None and (
                snapshot.epic != epic or snapshot.side is not order.side or snapshot.deal_id != closed.deal_id
            ):
                return self.refuse(
                    "CONFIRM_CONTRADICTS",
                    f"potwierdzenie brokera wskazuje {snapshot.deal_id} {snapshot.side.value} {snapshot.epic}, a nie "
                    f"wpisane zamknięcie {closed.deal_id}: dowody są sprzeczne; zapytaj Claude",
                )
        estimate: dict[str, Any] | None = None
        if snapshot is not None:
            self.out(f"dane wejścia z potwierdzenia brokera: kierunek {snapshot.side.value}, wielkość {snapshot.size}, "
                     f"poziom otwarcia {snapshot.open_level}, czas {snapshot.opened_at.isoformat()}")  # fmt: skip
            booked = PositionRecord(
                epic, PositionState.BLOCKED, closed.deal_id, snapshot.side, snapshot.size, snapshot.open_level,
                snapshot.stop_level, snapshot.opened_at,
            )  # fmt: skip
        else:
            if len(candidates) > 1:
                return self.refuse(
                    "AMBIGUOUS_CLOSURE",
                    f"historia pokazuje {len(candidates)} zamknięć na {epic}, a broker nie zna danych wejścia, które "
                    "by je rozróżniły: nie da się udowodnić, które jest tą transakcją; zapytaj Claude",
                )
            estimate = await self._estimate(order, closed)
            if not isinstance(estimate, dict):
                return estimate
            acknowledged.append(ESTIMATE_WORD)
            booked = PositionRecord(
                epic, PositionState.BLOCKED, closed.deal_id, estimate["side"], estimate["size"],
                estimate["assumed_entry"], None, order.created_at,
            )  # fmt: skip

        # 6. księgowanie jedyną ścieżką, potem zlecenie, pozycja i znacznik historii
        self.audit(
            "BOOKING", order=order, position=position, closures=listed, chosen=closed.deal_id,
            source="CONFIRM" if snapshot is not None else "ESTIMATE", confirm=confirm, snapshot=snapshot,
            estimate=estimate, acknowledged=acknowledged,
        )  # fmt: skip
        if snapshot is not None:  # księga bierze prawdziwy kontekst wejścia (notowanie decyzji, czasy) tego zlecenia
            journal.set_meta(META_ENTRY_BY_DEAL.format(closed.deal_id), order.order_id)
        try:
            await engine.executor.book(booked, closed, estimated=estimate is not None, by_owner=True)
        except BookingError as exc:
            self.out(f"odmowa: księgowanie nie powiodło się ({exc}); nic nie zaksięgowano, zlecenie i pozycja bez "
                     "zmian; spróbuj ponownie albo zapytaj Claude")  # fmt: skip
            return 1
        return self._finish(order, closed, "EXACT" if estimate is None else "ESTIMATED")

    def _print_state(self, order: Any, since: datetime, listed: list, others: set[str]) -> None:
        epic = self.epic
        created = order.created_at.isoformat()
        self.out(f"zlecenie {order.order_id}: {order.state.value} ({order.note}), utworzone {created}")
        self.out(f"pozycja {epic} w dzienniku: BLOCKED (bez deal id)")
        self.out(f"broker: brak pozycji i zleceń oczekujących na {epic} na związanym rachunku")
        self.out(f"zamknięcia na {epic} w historii brokera od {since.isoformat()}:")
        for c in listed:
            local = self.engine.calendar.to_local(c.ts)
            tail = "  (już w księdze jako inna transakcja)" if c.deal_id in others else ""
            self.out(f"  {c.deal_id}  {c.reason}  poziom {c.exit_level}  {c.ts.isoformat()} ({local:%H:%M} "
                     f"{local.tzname()}){tail}")  # fmt: skip
        if not listed:
            self.out("  (brak)")

    async def _estimate(self, order: Any, closed: Any) -> dict[str, Any] | int:
        """Szacunek najgorszej straty ze stopu (wejście = wyjście ± odległość stopu) po wpisaniu SZACUNEK; inaczej kod
        wyjścia (odmowa albo przerwanie). Wartość punktu z reguł brokera, tak jak policzy ją księgowanie szacunku."""
        side, size, stop, ctx_vpp, inputs = _estimate_inputs(self.journal, order)
        try:
            rules = await self.engine.probe(self.engine.raw_broker.instrument_rules, self.epic)
        except Exception as exc:
            return self.refuse(
                "RULES_UNREADABLE",
                f"wartości punktu od brokera nie da się odczytać ({type(exc).__name__}); spróbuj ponownie za kilka "
                "minut",
            )
        vpp = rules.value_per_point
        if ctx_vpp is not None and ctx_vpp != vpp:
            return self.refuse(
                "VALUE_PER_POINT_MISMATCH",
                f"wartość punktu w kontekście wejścia ({ctx_vpp}) różni się od reguł brokera ({vpp}): dowody są "
                "sprzeczne; zapytaj Claude",
            )
        assumed = closed.exit_level + stop if side is Side.BUY else closed.exit_level - stop
        self.out(f"brak danych wejścia u brokera: zostanie zaksięgowana NAJGORSZA możliwa strata ze stopu (SZACUNEK): "
                 f"{size} x {stop} pkt x {vpp} EUR/pkt = -{size * stop * vpp} EUR przed kosztami; wiersz księgi "
                 "dostanie znacznik ESTIMATED; zysku z szacunku nigdy nie księgujemy")  # fmt: skip
        if self.ask(ESTIMATE_PROMPT).strip() != ESTIMATE_WORD:
            return self.abort()
        return {"side": side, "size": size, "stop_distance": stop, "value_per_point": vpp, "assumed_entry": assumed,
                "inputs": inputs}  # fmt: skip

    def _not_placed(self, order: Any, since: datetime, others: set[str]) -> int:
        """Przypadek (ii): czysta historia i pusty broker. NOT_PLACED tylko wtedy, gdy dziennik nie zapisał dla
        zlecenia żadnej pozycji ani zamknięcia u brokera (inaczej historia mogła się jeszcze nie pojawić: odmowa)."""
        journal, epic = self.journal, self.epic
        seen = sorted(_recorded_deals(journal, order.order_id) - others)
        if seen:
            return self.refuse(
                "NO_CLOSURE_IN_HISTORY",
                f"dziennik zapisał dla zlecenia {order.order_id} pozycję albo zamknięcie u brokera "
                f"({', '.join(seen)}), a historia od {since.isoformat()} nie pokazuje żadnego zamknięcia na {epic}: "
                "historia może się spóźniać; spróbuj ponownie za kilka minut, a jeśli to się powtarza, zapytaj Claude "
                "(runbook 8.5)",
            )
        self.out(f"historia brokera nie pokazuje zamknięcia na {epic}, a broker nie ma pozycji ani zlecenia: zlecenie "
                 "najpewniej nie zostało wykonane i nic nie zostanie zaksięgowane. Jeśli w ostatnich minutach "
                 f"zamknąłeś pozycję {epic} w aplikacji, przerwij (historia brokera może się spóźniać)")  # fmt: skip
        if self.ask(NOT_PLACED_PROMPT.format(order=order.order_id, epic=epic)).strip() != NOT_PLACED_WORD:
            return self.abort()
        self.audit("NOT_PLACED", order=order, since=since, acknowledged=[NOT_PLACED_WORD])
        now = self.clock.now_utc()
        check_order_transition(OrderState.UNKNOWN, OrderState.NOT_PLACED)
        note = _note_tokens("RESOLVED_BY_OWNER", "NOT_PLACED_NO_CLOSURE", f"BY={_token(self.who)}", _causes(order.note))
        journal.update_order(order.order_id, state=OrderState.NOT_PLACED, note=note, updated_at=now)
        check_position_transition(PositionState.BLOCKED, PositionState.FLAT)
        journal.upsert_position(PositionRecord(epic, PositionState.FLAT))
        self.audit("DONE", order_id=order.order_id, outcome="NOT_PLACED")
        self.out(f"zlecenie {order.order_id} → NOT_PLACED (RESOLVED_BY_OWNER), pozycja {epic} → FLAT; nic nie "
                 f"zaksięgowano; {self._kill_hint()}")  # fmt: skip
        return 0

    def _finish(self, order: Any, closed: Any, booked_as: str) -> int:
        """Zapisy po księdze (każdy idempotentny): zlecenie CONFIRMED, pozycja FLAT, znacznik historii, audyt."""
        from gwstock.reconcile import META_CLOSED_MARKER

        journal, epic = self.journal, self.epic
        now = self.clock.now_utc()
        check_order_transition(OrderState.UNKNOWN, OrderState.CONFIRMED)
        note = _note_tokens(
            "RESOLVED_BY_OWNER", f"BOOKED={booked_as}", f"CLOSURE={closed.deal_id}", f"BY={_token(self.who)}",
            _causes(order.note),
        )  # fmt: skip
        journal.update_order(order.order_id, state=OrderState.CONFIRMED, deal_id=closed.deal_id, note=note,
                             updated_at=now)  # fmt: skip
        check_position_transition(PositionState.BLOCKED, PositionState.FLAT)
        journal.upsert_position(PositionRecord(epic, PositionState.FLAT))
        marker = journal.get_meta(META_CLOSED_MARKER)
        if not marker or datetime.fromisoformat(marker) < closed.ts:
            journal.set_meta(META_CLOSED_MARKER, closed.ts.isoformat())
        net = _booked_net(journal, closed.deal_id)
        self.audit("DONE", deal_id=closed.deal_id, order_id=order.order_id, net_pnl=net, booked=booked_as)
        how = "DOKŁADNIE z potwierdzenia" if booked_as == "EXACT" else "SZACUNEK"
        day = self.engine.calendar.to_local(closed.ts).date()
        shown = net if net is not None else "?"
        self.out(f"zaksięgowano transakcję {closed.deal_id} ({how}): wynik netto {shown} EUR, "
                 f"dzień {day}; zlecenie {order.order_id} → CONFIRMED (RESOLVED_BY_OWNER), pozycja {epic} → FLAT; "
                 f"{self._kill_hint()}")  # fmt: skip
        return 0

    def _kill_hint(self) -> str:
        kill = self.engine.kill
        if kill.level() is KillLevel.NONE:
            return "kill switch: NONE; uruchom silnik"
        return (f"kill switch nadal {kill.level().name} ({kill.reason()}): przeczytaj alarmy, zdejmij go poleceniem "
                "clear-kill i uruchom silnik")  # fmt: skip


def _position_fields(p: PositionRecord) -> bool:
    return any(v is not None for v in (p.side, p.size, p.open_level, p.stop_level, p.opened_at))


def _owner_booked(journal: Any, deal_id: str) -> bool:
    """Transakcja zaksięgowana przez `resolve-lost-trade` (znacznik RESOLVED_BY_OWNER), np. przed przerwaniem."""
    return FLAGS_BY_OWNER in (journal.trade_flags(deal_id) or "").split()


def _recorded_deals(journal: Any, order_id: str) -> set[str]:
    """Deale, które menedżer albo uzgadnianie zapisali dla zlecenia (pozycja na liście albo zamknięcie w historii:
    UNMATCHED_CLOSED_DEAL, AMBIGUOUS_MATCH, CONFIRM_MISMATCH, REJECTED_BUT_LISTED, FOREIGN_POSITION ...)."""
    seen: set[str] = set()
    for source in (SOURCE_ORDER_EVENT, SOURCE_RECONCILE_EVENT):
        for e in journal.events(source):
            try:
                data = json.loads(e.payload)
            except ValueError:
                continue
            deals = data.get("deal_ids") if isinstance(data, dict) and data.get("order_id") == order_id else None
            if isinstance(deals, list):
                seen.update(d for d in deals if isinstance(d, str) and d)
    return seen


def _estimate_inputs(journal: Any, order: Any) -> tuple[Side, Decimal, Decimal, Decimal | None, str]:
    """Kierunek, wielkość, odległość stopu i wartość punktu z kontekstu wejścia zlecenia; nieczytelny kontekst: pola
    zlecenia (bez wartości punktu)."""
    text = journal.get_meta(META_ENTRY_CTX.format(order.order_id))
    try:
        ctx = json.loads(text) if text else None
        if isinstance(ctx, dict) and ctx.get("client_id") == order.order_id:
            side = Side(ctx["side"])
            size, stop, vpp = (Decimal(ctx[k]) for k in ("size", "stop_distance", "value_per_point"))
            if side is order.side and all(v.is_finite() and v > 0 for v in (size, stop, vpp)):
                return side, size, stop, vpp, "entry_ctx"
    except (ValueError, TypeError, KeyError, ArithmeticError):
        pass
    return order.side, order.size, order.stop_distance, None, "order"


def _booked_net(journal: Any, deal_id: str) -> Decimal | None:
    """Wynik netto zaksięgowanej transakcji z jej zdarzenia TRADE_BOOKED (rekord księgi nie ma deal_id)."""
    net = None
    for e in journal.events(SOURCE_EVENT):
        try:
            data = json.loads(e.payload)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "TRADE_BOOKED" and data.get("deal_id") == deal_id:
            net = data.get("net_pnl")
    return Decimal(net) if isinstance(net, str) else None


def _causes(note: str) -> str:
    return " ".join(t for t in note.split() if t.startswith("CAUSE="))


def _token(text: str) -> str:
    return "_".join(text.split())


def _note_tokens(*parts: str) -> str:
    return " ".join(p for p in parts if p)


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


@contextlib.contextmanager
def _engine_stopped(paths: Paths):
    """Blokada instancji dla poleceń właściciela, które wolno uruchomić tylko przy zatrzymanym silniku."""
    lock = InstanceLock(paths.data_dir / LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise AlreadyRunning(f"{exc}; stop the engine first (a running engine keeps its own state)") from None
    try:
        yield lock
    finally:
        lock.release()


async def _smoke_engine(paths: Paths, hold_s: int = 0) -> int:
    async def run(engine: Engine) -> int:
        with _stop_on_signals(engine):  # Ctrl-C/SIGTERM w trakcie wstrzymania: zamknięcie zamiast porzucenia pozycji
            return await smoke(engine, hold_s=hold_s)

    with _engine_stopped(paths) as lock:  # nigdy obok działającego silnika: blokada najpierw
        return await _with_engine(paths, run, lock=lock)


async def _reset_engine(paths: Paths, by: str) -> int:
    with _engine_stopped(paths) as lock:
        return await _with_engine(paths, lambda engine: reset_position(engine, by, input), lock=lock)


async def _resolve_engine(paths: Paths, by: str) -> int:
    with _engine_stopped(paths) as lock:  # blokada instancji przed konfiguracją, dziennikiem i siecią
        return await _with_engine(paths, lambda engine: resolve_lost_trade(engine, by, input), lock=lock)


async def _bind(paths: Paths) -> int:
    import aiohttp

    from gwstock.broker.capitalcom import LIVE_BASE_URL, CapitalComBroker, load_credentials
    from gwstock.clock import SystemClock

    creds = dataclasses.replace(load_credentials(paths.key_file), account_id=None)
    with _engine_stopped(paths):
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
    return asyncio.run(_smoke_engine(paths, getattr(args, "hold", 0)))


def _cmd_clear_kill(paths: Paths, args: argparse.Namespace) -> int:
    with _engine_stopped(paths):  # działający silnik trzymałby swój poziom w pamięci
        return clear_kill(paths.data_dir, args.by, input)


def _cmd_reset_position(paths: Paths, args: argparse.Namespace) -> int:
    return asyncio.run(_reset_engine(paths, args.by))


def _cmd_resolve_lost_trade(paths: Paths, args: argparse.Namespace) -> int:
    return asyncio.run(_resolve_engine(paths, args.by))


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
    "clear-kill": _cmd_clear_kill,
    "reset-position": _cmd_reset_position,
    "resolve-lost-trade": _cmd_resolve_lost_trade,
}

_HELP = {
    "run": "run the engine (default without a command)",
    "bind-account": "choose the trading account (owner, interactive)",
    "smoke": "L0 acceptance probe: one minimal entry with a stop, closed at once (live only)",
    "status": "read-only state: mode, kill switch, position, orders, heartbeat, last decision, today's trades",
    "health": "exit 0 if the heartbeat is fresher than 120 s (Docker healthcheck)",
    "clear-kill": "owner: clear the kill switch (engine stopped; asks for CLEAR)",
    "reset-position": "owner: set a BLOCKED position FLAT when the broker shows nothing (engine stopped; RESET)",
    "resolve-lost-trade": "owner: book a lost trade (UNKNOWN order, BLOCKED position, broker flat) from the broker "
    "history (engine stopped; asks for the closing deal id)",
}


def _hold_seconds(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected whole seconds, got {text!r}") from None
    if not 0 <= value <= SMOKE_HOLD_MAX_S:
        raise argparse.ArgumentTypeError(f"expected 0 to {SMOKE_HOLD_MAX_S} seconds, got {value}")
    return value


def _owner_name(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("a non-empty name of who does it")
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gwstock", description="gw-stock-engine (DE40 CFD, Capital.com)")
    sub = parser.add_subparsers(dest="command", metavar="command")
    parsers = {name: sub.add_parser(name, help=_HELP[name]) for name in COMMANDS}
    parsers["smoke"].add_argument(
        "--hold", type=_hold_seconds, default=0, metavar="SECONDS",
        help=f"keep the probe position open for 0-{SMOKE_HOLD_MAX_S} s under normal supervision before closing it",
    )  # fmt: skip
    for name in ("clear-kill", "reset-position", "resolve-lost-trade"):
        parsers[name].add_argument("--by", type=_owner_name, required=True, help="who does it (audit trail)")
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
