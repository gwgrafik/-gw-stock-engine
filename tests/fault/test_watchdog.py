"""Testy nadzoru (spec §9, §13): blokada instancji, tętno, cisza notowań, logowanie z ochroną przed blokadą konta,
odrzucenia z rzędu, uparcie zawodzące uzgadnianie, liczba nieznanych odpowiedzi brokera i ograniczanie alarmów.

Prawdziwe `Journal` i `KillSwitch` na `FakeClock`; Watchdog nie handluje, tylko włącza kill switch miękki i alarmuje.
"""

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import gwstock.watchdog as watchdog_mod
from gwstock.clock import FakeClock
from gwstock.risk import KillSwitch
from gwstock.types import KillLevel
from gwstock.watchdog import (
    LOGIN_FAILED,
    QUOTE_SILENCE,
    RECONCILE_FAILING,
    REJECTIONS,
    UNKNOWN_BROKER_RESPONSES,
    AlreadyRunning,
    InstanceLock,
    RateLimitedNotifier,
    Watchdog,
    heartbeat_age_s,
    read_heartbeat,
)

T0 = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)  # 09:00 w Berlinie: otwarcie sesji
CLOSE = datetime(2026, 11, 2, 16, 30, tzinfo=timezone.utc)  # 17:30 w Berlinie


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


class CountingBroker:
    """Atrapa brokera z publicznym licznikiem adaptera `unknown_close_sources`."""

    def __init__(self) -> None:
        self.unknown_close_sources = 0


@pytest.fixture
def broker() -> CountingBroker:
    return CountingBroker()


@pytest.fixture
def heartbeat(data_dir: Path) -> Path:
    return data_dir / "heartbeat.json"


@pytest.fixture
def wd(clock, journal, kill, broker, risk_cfg, heartbeat, notifier, data_dir) -> Watchdog:
    reset = data_dir / "LOGIN_RESET"
    w = Watchdog(clock, journal, kill, broker, risk_cfg, heartbeat, notifier=notifier, reset_path=reset)
    w.set_session_window(T0, CLOSE)
    return w


def alerts_with(notifier, text):
    return [(level, message) for level, message in notifier.alerts if text in message]


# --- blokada instancji (spec §13: plik blokady w /data, bez dublowania sesji) ---------------------


def test_second_instance_is_refused(tmp_path):
    first = InstanceLock(tmp_path / "engine.lock")
    first.acquire()
    with pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "engine.lock").acquire()
    first.release()


def test_released_lock_can_be_taken_again_and_holds_the_pid(tmp_path):
    path = tmp_path / "engine.lock"
    with InstanceLock(path) as lock:
        assert lock.held
        assert path.read_text().strip() == str(os.getpid())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not lock.held
    second = InstanceLock(path)
    second.acquire()
    assert second.held
    second.release()
    second.release()  # idempotentne


def test_refused_lock_does_not_overwrite_the_holders_pid(tmp_path):
    path = tmp_path / "engine.lock"
    with InstanceLock(path):
        path.write_text("4242\n")  # pid właściciela blokady (np. inny proces)
        with pytest.raises(AlreadyRunning, match="4242"):
            InstanceLock(path).acquire()
        assert path.read_text() == "4242\n"


def test_lock_held_by_another_process_is_refused(tmp_path):
    path = tmp_path / "engine.lock"
    code = (
        "import sys, time; sys.path.insert(0, 'src'); from pathlib import Path; "
        "from gwstock.watchdog import InstanceLock; l = InstanceLock(Path(sys.argv[1])); l.acquire(); "
        "print('held', flush=True); sys.stdin.read()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as child:  # kontekst zamyka potoki i czeka na proces
        try:
            assert child.stdout.readline().strip() == "held"
            with pytest.raises(AlreadyRunning):
                InstanceLock(path).acquire()
        finally:
            child.stdin.close()
            child.wait(timeout=10)
    lock = InstanceLock(path)  # proces zakończony: jądro zwolniło blokadę
    lock.acquire()
    lock.release()


def test_acquiring_twice_on_one_object_is_an_error(tmp_path):
    lock = InstanceLock(tmp_path / "engine.lock")
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()


# --- tętno: zapis atomowy i świeżość ------------------------------------------------------------------


async def test_heartbeat_is_written_atomically_with_status_fields(wd, heartbeat, clock, kill):
    wd.set_status(mode="dry_run", state="MANAGING")
    wd.note_quote(T0)
    kill.soft("STOP_FILE")
    await wd.tick()
    data = json.loads(heartbeat.read_text())
    assert data == {
        "ts": T0.isoformat(), "mode": "dry_run", "state": "MANAGING", "last_quote_ts": T0.isoformat(),
        "kill_level": "SOFT",
    }  # fmt: skip
    assert [p.name for p in heartbeat.parent.iterdir() if p.name.startswith("heartbeat")] == ["heartbeat.json"]
    assert read_heartbeat(heartbeat) == data


async def test_failed_heartbeat_write_keeps_the_previous_file_intact(wd, heartbeat, clock, monkeypatch):
    await wd.tick()
    before = heartbeat.read_text()
    clock.advance(5)

    def broken_replace(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(watchdog_mod.os, "replace", broken_replace)
    await wd.tick()  # nie rzuca: tętno to sygnał zdrowia, nie warunek pracy
    assert heartbeat.read_text() == before
    assert not any(p.name.endswith(".tmp") for p in heartbeat.parent.iterdir())


async def test_heartbeat_age_drives_freshness(wd, heartbeat, clock):
    assert heartbeat_age_s(heartbeat, clock.now_utc()) is None  # brak pliku
    await wd.tick()
    assert heartbeat_age_s(heartbeat, T0 + timedelta(seconds=119)) == 119
    heartbeat.write_text("{not json")
    assert heartbeat_age_s(heartbeat, T0) is None


# --- cisza notowań > 60 s w sesji → kill switch miękki ----------------------------------------------


async def test_quote_silence_over_60s_in_session_soft_kills(wd, clock, kill, notifier):
    wd.note_quote(T0)
    clock.advance(60)
    await wd.tick()
    assert kill.level() is KillLevel.NONE  # równo 60 s przechodzi
    clock.advance(1)
    await wd.tick()
    assert kill.level() is KillLevel.SOFT and kill.reason() == QUOTE_SILENCE
    assert alerts_with(notifier, QUOTE_SILENCE)


async def test_a_frozen_quote_is_silence_even_when_polled(wd, clock, kill):
    for _ in range(13):  # odczyt co 5 s, ale para (bid, ask) się nie zmienia: ts zostaje 09:00:00
        wd.note_quote(T0)
        clock.advance(5)
    await wd.tick()
    assert kill.reason() == QUOTE_SILENCE


async def test_fresh_quotes_keep_the_kill_switch_off(wd, clock, kill):
    for _ in range(100):
        clock.advance(5)
        wd.note_quote(clock.now_utc())
        await wd.tick()
    assert kill.level() is KillLevel.NONE


async def test_quote_silence_is_not_checked_outside_the_session_window(wd, clock, kill):
    wd.set_session_window(None, None)
    clock.advance(3600)
    await wd.tick()
    assert kill.level() is KillLevel.NONE
    wd.set_session_window(T0 - timedelta(hours=2), T0 - timedelta(hours=1))  # okno już minęło
    await wd.tick()
    assert kill.level() is KillLevel.NONE


async def test_silence_is_measured_from_the_window_start_not_from_last_nights_quote(wd, clock, kill):
    wd.note_quote(T0 - timedelta(hours=15))  # ostatnie notowanie wczoraj wieczorem
    wd.set_session_window(T0, CLOSE)
    await wd.tick()
    assert kill.level() is KillLevel.NONE
    clock.advance(61)
    await wd.tick()
    assert kill.reason() == QUOTE_SILENCE


async def test_silence_is_measured_from_a_late_start_not_from_the_window_start(wd, clock, kill):
    clock.advance(3600)  # silnik wystartował o 10:00, w środku sesji, bez żadnego notowania
    wd.set_session_window(T0, CLOSE)
    await wd.tick()
    assert kill.level() is KillLevel.NONE
    clock.advance(61)
    await wd.tick()
    assert kill.reason() == QUOTE_SILENCE


# --- logowanie: trzy porażki z rzędu → kill miękki i koniec prób (ochrona konta przed blokadą) -------


def test_three_consecutive_login_failures_soft_kill_and_stop_further_logins(wd, clock, kill, notifier):
    for n in range(1, 4):
        assert wd.may_login()
        wd.note_login_failure("HTTP 401 error.invalid.details", credential=True)
        assert (kill.level() is KillLevel.SOFT) == (n == 3)
        clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    assert kill.reason() == LOGIN_FAILED
    assert not wd.may_login()
    clock.advance(24 * 3600)
    assert not wd.may_login()  # czas nie zdejmuje blokady: tylko właściciel albo udane logowanie
    assert alerts_with(notifier, "LOGIN_LOCKOUT")


def test_login_lockout_alert_points_to_the_runbook_and_not_to_the_reset_file(wd, clock, notifier):
    # AD-a: alarm mówi to, co runbook (napraw klucz, jedna próba logowania), a nie reklamuje obejścia data/LOGIN_RESET
    for _ in range(3):
        wd.note_login_failure("HTTP 401 error.invalid.details", credential=True)
        clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    [(level, message)] = alerts_with(notifier, "LOGIN_LOCKOUT")
    assert level == "ERROR" and message.startswith("LOGIN_LOCKOUT: 3 credential rejections; no further login attempts")
    assert "follow the runbook (section 9, LOGIN_FAILED)" in message and "one single-login probe" in message
    assert "positions keep their broker stops" in message
    assert "LOGIN_RESET" not in message and "restarts the engine" not in message


def test_a_failed_login_waits_before_the_next_attempt(wd, clock):
    wd.note_login_failure("timeout")
    assert not wd.may_login()
    clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S - 1)
    assert not wd.may_login()
    clock.advance(1)
    assert wd.may_login()


def test_a_successful_login_resets_the_failure_count(wd, clock, kill):
    wd.note_login_failure("x")
    clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    wd.note_login_failure("x")
    clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    wd.note_login_success()
    wd.note_login_failure("x")
    clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    wd.note_login_failure("x")
    assert kill.level() is KillLevel.NONE  # nigdy trzy z rzędu


def test_owner_reset_file_lifts_the_lockout_once_and_is_consumed(wd, clock, kill, data_dir, notifier):
    for _ in range(3):
        wd.note_login_failure("x", credential=True)
        clock.advance(watchdog_mod.LOGIN_RETRY_PAUSE_S)
    assert not wd.may_login()
    (data_dir / "LOGIN_RESET").write_text("")
    assert wd.may_login()
    assert not (data_dir / "LOGIN_RESET").exists()
    assert kill.level() is KillLevel.SOFT  # kill switch zdejmuje wyłącznie właściciel (clear)
    assert alerts_with(notifier, "LOGIN_LOCKOUT_RESET")


def test_transient_login_failures_back_off_exponentially_and_never_lock_out(wd, clock, kill, notifier):
    pauses = []
    for _ in range(8):  # awaria sieci, timeout, 5xx, 429: to nie jest odrzucenie danych logowania
        assert wd.may_login()
        wd.note_login_failure("BrokerTimeout: POST /session: no response")
        waited = 0
        while not wd.may_login() and waited <= 3600:  # ograniczenie: zła implementacja nie zawiesza testu
            clock.advance(1)
            waited += 1
        pauses.append(waited)
    assert pauses == [30, 60, 120, 240, 300, 300, 300, 300]  # 30 s, podwajane, najwyżej 5 minut
    assert not alerts_with(notifier, "LOGIN_LOCKOUT")
    assert kill.reason() == LOGIN_FAILED  # trzy porażki z rzędu: brak nowych wejść, ale logowania trwają
    wd.note_login_success()
    wd.note_login_failure("timeout")
    clock.advance(30)
    assert wd.may_login()  # po udanym logowaniu odstęp zaczyna się od nowa


def test_only_credential_rejections_count_toward_the_lockout(wd, clock, kill):
    for credential in (True, False, True, False, False):
        wd.note_login_failure("x", credential=credential)
        clock.advance(watchdog_mod.LOGIN_BACKOFF_MAX_S)
    assert wd.may_login()  # dwa odrzucenia danych logowania: jeszcze bez blokady
    wd.note_login_failure("HTTP 401 error.invalid.details", credential=True)
    clock.advance(24 * 3600)
    assert not wd.may_login()


def test_two_failures_then_success_neither_kills_nor_locks(wd, clock, kill):
    wd.note_login_failure("timeout")
    clock.advance(30)
    wd.note_login_failure("timeout")
    clock.advance(60)
    assert wd.may_login()
    wd.note_login_success()
    assert kill.level() is KillLevel.NONE


async def test_silence_watch_can_be_rearmed_after_a_long_entry(wd, clock, kill):
    wd.note_quote(T0)
    clock.advance(70)  # wejście trwało 70 s (rozstrzyganie UNKNOWN), bez odczytu notowań w pętli
    wd.rearm_silence()
    await wd.tick()
    assert kill.level() is KillLevel.NONE
    clock.advance(61)
    await wd.tick()
    assert kill.reason() == QUOTE_SILENCE  # po ponownym uzbrojeniu straż działa normalnie


# --- trzy odrzucenia z rzędu → kill miękki --------------------------------------------------------


def test_three_consecutive_rejections_soft_kill(wd, kill):
    wd.note_rejection("MIN_STOP_DISTANCE")
    wd.note_rejection("MIN_STOP_DISTANCE")
    assert kill.level() is KillLevel.NONE
    wd.note_rejection("MIN_STOP_DISTANCE")
    assert kill.reason() == REJECTIONS


def test_a_success_between_rejections_resets_the_count(wd, kill):
    for _ in range(5):
        wd.note_rejection("x")
        wd.note_rejection("x")
        wd.note_success()
    assert kill.level() is KillLevel.NONE


# --- uzgadnianie, które ciągle rzuca: alarm, potem kill miękki (bez cichego ponawiania) --------------


def test_consecutive_reconcile_errors_alarm_and_soft_kill(wd, kill, notifier):
    wd.note_reconcile_error("BrokerError", "HTTP 503")
    wd.note_reconcile_error("BrokerError", "HTTP 503")
    assert kill.level() is KillLevel.NONE
    assert alerts_with(notifier, "RECONCILE_ERROR")  # już pierwszy błąd jest widoczny
    wd.note_reconcile_error("BrokerError", "HTTP 503")
    assert kill.reason() == RECONCILE_FAILING
    assert any(level == "ERROR" for level, _ in alerts_with(notifier, RECONCILE_FAILING))


def test_a_good_reconcile_resets_the_error_count(wd, kill):
    for _ in range(4):
        wd.note_reconcile_error("BrokerError", "x")
        wd.note_reconcile_error("BrokerError", "x")
        wd.note_reconcile_ok()
    assert kill.level() is KillLevel.NONE


@pytest.mark.parametrize(
    "code", ["HISTORY_AMBIGUOUS", "HISTORY_INCONSISTENT", "HISTORY_TRUNCATED", "HISTORY_RANGE_TOO_LARGE"]
)
def test_history_completeness_failures_are_loud_at_once(wd, notifier, code):
    wd.note_reconcile_error("BrokerError", f"GET /history/activity: {code} for deal X")
    [message] = [m for level, m in alerts_with(notifier, code) if level == "ERROR"]  # od razu, przed progiem
    assert message.startswith(code) and "owner" in message


def test_starved_reconcile_alarms_after_repeated_skips(wd, notifier, kill):
    for _ in range(watchdog_mod.RECONCILE_SKIP_LIMIT - 1):
        wd.note_reconcile_skipped()
    assert not alerts_with(notifier, "RECONCILE_STARVED")
    wd.note_reconcile_skipped()
    assert alerts_with(notifier, "RECONCILE_STARVED") and kill.level() is KillLevel.NONE


# --- nieznane źródła zamknięć w historii (licznik adaptera) ---------------------------------------


async def test_growing_unknown_close_sources_alarm_and_soft_kill_over_the_limit(wd, broker, kill, notifier, clock):
    await wd.tick()
    assert notifier.alerts == []
    broker.unknown_close_sources = 1
    await wd.tick()
    assert alerts_with(notifier, "UNKNOWN_CLOSE_SOURCE") and kill.level() is KillLevel.NONE
    broker.unknown_close_sources = watchdog_mod.UNKNOWN_RESPONSES_LIMIT
    clock.advance(1)
    await wd.tick()
    assert kill.reason() == UNKNOWN_BROKER_RESPONSES


async def test_a_broker_without_the_counter_is_fine(clock, journal, kill, risk_cfg, heartbeat, notifier):
    w = Watchdog(clock, journal, kill, object(), risk_cfg, heartbeat, notifier=notifier)
    await w.tick()
    assert kill.level() is KillLevel.NONE


# --- ograniczanie alarmów: ten sam alarm najwyżej raz na 5 minut ---------------------------------------


def test_same_alert_at_most_once_per_window_then_reports_suppressed(clock, notifier):
    rl = RateLimitedNotifier(notifier, clock)
    for _ in range(30):  # co 10 s przez 5 minut (takt uzgadniania)
        rl.alert("ERROR", "FLATTEN_UNVERIFIED DEAL-1: list unreadable")
        clock.advance(10)
    assert notifier.alerts == [("ERROR", "FLATTEN_UNVERIFIED DEAL-1: list unreadable")]
    rl.alert("ERROR", "FLATTEN_UNVERIFIED DEAL-1: list unreadable")
    assert notifier.alerts[-1] == ("ERROR", "FLATTEN_UNVERIFIED DEAL-1: list unreadable (+29 suppressed in 300 s)")


def test_different_alerts_are_never_suppressed(clock, notifier):
    rl = RateLimitedNotifier(notifier, clock)
    rl.alert("ERROR", "A")
    rl.alert("WARN", "A")
    rl.alert("ERROR", "B")
    assert notifier.alerts == [("ERROR", "A"), ("WARN", "A"), ("ERROR", "B")]


def test_keyed_alerts_share_one_budget_even_with_changing_text(clock, notifier):
    rl = RateLimitedNotifier(notifier, clock)
    rl.alert_keyed("RECONCILE_ERROR", "WARN", "RECONCILE_ERROR 1")
    rl.alert_keyed("RECONCILE_ERROR", "WARN", "RECONCILE_ERROR 2")
    clock.advance(300)
    rl.alert_keyed("RECONCILE_ERROR", "WARN", "RECONCILE_ERROR 3")
    assert [m for _, m in notifier.alerts] == ["RECONCILE_ERROR 1", "RECONCILE_ERROR 3 (+1 suppressed in 300 s)"]


def test_rate_limiter_never_raises_when_the_notifier_fails(clock, notifier):
    notifier.fail = RuntimeError("alert sink down")
    RateLimitedNotifier(notifier, clock).alert("ERROR", "x")  # bez wyjątku
