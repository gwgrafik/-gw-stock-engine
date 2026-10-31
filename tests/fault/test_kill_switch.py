"""Testy awaryjne kill switcha i notyfikatora (spec §9).

Niezmienniki pod lupą (prawdziwe pieniądze):
- kill switch NIGDY nie zawodzi przy włączaniu: awaria dziennika lub notyfikatora nie cofa poziomu w pamięci;
- poziom przeżywa restart i nigdy nie jest zdejmowany automatycznie ani po cichu;
- powtarzane wywołania (uzgadnianie co 10 s) są tanie: jeden zapis i jeden alarm na zmianę, nie na wywołanie;
- poziom nie spada (soft przy HARD zostaje HARD); zdjęcie poziomu to jawna akcja z wpisem audytowym.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.journal import Journal, JournalReadError, JournalWriteError, KillState
from gwstock.notify import FileNotifier, Notifier
from gwstock.reconcile import Reconciler
from gwstock.risk import KillSwitch, KillView
from gwstock.types import KillLevel, OpenPosition, Side

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)  # = clock.now_utc() na starcie fixture `clock`


def audit(journal: Journal) -> list[dict]:
    return [json.loads(e.payload) for e in journal.events("kill.audit")]


@pytest.fixture
def set_kill_calls(journal, monkeypatch) -> list[tuple[KillLevel, str]]:
    """Podsłuch `journal.set_kill`: lista (poziom, przyczyna) kolejnych zapisów."""
    calls: list[tuple[KillLevel, str]] = []
    real = journal.set_kill

    def spy(level, reason, ts):
        calls.append((level, reason))
        return real(level, reason, ts)

    monkeypatch.setattr(journal, "set_kill", spy)
    return calls


def break_method(monkeypatch, obj, name: str, exc: Exception) -> None:
    def fail(*a, **k):
        raise exc

    monkeypatch.setattr(obj, name, fail)


# --- testy z briefu ---------------------------------------------------------------------------------


def test_kill_switch_writes_alert_line(tmp_path, journal, clock, data_dir):
    alerts = tmp_path / "alerts.log"
    ks = KillSwitch(journal, clock, data_dir, FileNotifier(alerts, clock))
    ks.soft("x")
    lines = alerts.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    ts, level, message = lines[0].split(" ", 2)
    assert ts == "2026-11-02T07:30:00+00:00" and level == "SOFT" and "x" in message


def test_kill_level_persists_and_clear_requires_explicit_action(tmp_path, clock, data_dir, notifier):
    path = str(tmp_path / "restart.sqlite")
    j1 = Journal.open(path)
    KillSwitch(j1, clock, data_dir, notifier).hard("test")
    j1.close()

    j2 = Journal.open(path)  # restart procesu
    try:
        ks = KillSwitch(j2, clock, data_dir, notifier)
        assert ks.level() is KillLevel.HARD and ks.reason() == "test"  # poziom i przyczyna wróciły z dziennika
        clock.advance(3 * 86400)
        ks.scan_files()
        ks.soft("again")
        assert ks.level() is KillLevel.HARD  # nic go nie zdejmuje samo
        with pytest.raises(ValueError):
            ks.clear("")
        assert ks.level() is KillLevel.HARD
        ks.clear("owner")  # dopiero jawna akcja
        assert ks.level() is KillLevel.NONE
    finally:
        j2.close()


def test_stop_file_triggers_soft_and_hard_file_triggers_hard(journal, clock, data_dir, notifier):
    ks = KillSwitch(journal, clock, data_dir, notifier)
    ks.scan_files()
    assert ks.level() is KillLevel.NONE and notifier.alerts == []  # brak plików: brak skutku

    (data_dir / "STOP").write_text("")
    ks.scan_files()
    assert ks.level() is KillLevel.SOFT
    assert journal.kill_state().reason == "STOP_FILE"

    (data_dir / "HARD_STOP").write_text("")
    ks.scan_files()
    assert ks.level() is KillLevel.HARD
    assert journal.kill_state() == KillState(KillLevel.HARD, "HARD_STOP_FILE", clock.now_utc())


def test_hard_stop_file_alone_is_hard(journal, clock, data_dir, notifier):
    ks = KillSwitch(journal, clock, data_dir, notifier)
    (data_dir / "HARD_STOP").write_text("")
    ks.scan_files()
    assert ks.level() is KillLevel.HARD and journal.kill_state().reason == "HARD_STOP_FILE"


def test_both_files_at_once_end_hard_with_a_single_escalation_alert(journal, clock, data_dir, notifier):
    ks = KillSwitch(journal, clock, data_dir, notifier)
    (data_dir / "STOP").write_text("")
    (data_dir / "HARD_STOP").write_text("")
    ks.scan_files()
    assert ks.level() is KillLevel.HARD
    assert [level for level, _ in notifier.alerts] == ["HARD"]  # soft przy HARD jest tylko śladem audytowym


def test_repeated_scans_with_the_files_present_do_not_spam(journal, clock, data_dir, notifier, set_kill_calls):
    ks = KillSwitch(journal, clock, data_dir, notifier)
    (data_dir / "STOP").write_text("")
    for _ in range(20):
        ks.scan_files()
    assert len(notifier.alerts) == 1 and set_kill_calls == [(KillLevel.SOFT, "STOP_FILE")]


def test_a_stop_file_that_is_a_dangling_symlink_still_counts(journal, clock, data_dir, notifier):
    (data_dir / "STOP").symlink_to(data_dir / "nowhere")
    ks = KillSwitch(journal, clock, data_dir, notifier)
    ks.scan_files()
    assert ks.level() is KillLevel.SOFT


def test_unreadable_stop_file_check_fails_closed_to_soft(journal, clock, data_dir, notifier, monkeypatch):
    real = os.lstat

    def lstat(path, *a, **k):
        if os.path.basename(str(path)) in ("STOP", "HARD_STOP"):
            raise PermissionError(13, "denied")
        return real(path, *a, **k)

    ks = KillSwitch(journal, clock, data_dir, notifier)
    monkeypatch.setattr(os, "lstat", lstat)
    ks.scan_files()
    assert ks.level() is KillLevel.SOFT
    assert journal.kill_state().reason == "STOP_FILE_UNREADABLE"
    with pytest.raises(ValueError):  # a clear() nie może sprawdzić plików, więc odmawia
        ks.clear("owner")


def test_missing_data_dir_is_simply_no_stop_file(journal, clock, tmp_path, notifier):
    ks = KillSwitch(journal, clock, tmp_path / "does-not-exist", notifier)
    ks.scan_files()
    assert ks.level() is KillLevel.NONE


# --- poziomy, przejścia, idempotencja ---------------------------------------------------------------


def test_new_switch_starts_with_no_kill(kill, journal):
    assert kill.level() is KillLevel.NONE and kill.reason() == ""
    assert journal.kill_state().level is KillLevel.NONE


def test_soft_sets_level_and_persists_with_the_clock_time(kill, journal, notifier, clock):
    kill.soft("daily loss")
    assert kill.level() is KillLevel.SOFT and kill.reason() == "daily loss"
    assert journal.kill_state() == KillState(KillLevel.SOFT, "daily loss", clock.now_utc())
    assert len(notifier.alerts) == 1
    level, message = notifier.alerts[0]
    assert level == "SOFT" and "daily loss" in message


def test_hard_sets_level_and_persists(kill, journal, notifier, clock):
    kill.hard("no stop")
    assert kill.level() is KillLevel.HARD
    assert journal.kill_state() == KillState(KillLevel.HARD, "no stop", clock.now_utc())
    assert [level for level, _ in notifier.alerts] == ["HARD"] and "no stop" in notifier.alerts[0][1]


def test_hard_over_soft_upgrades_and_alerts_again(kill, journal, notifier):
    kill.soft("a")
    kill.hard("b")
    assert kill.level() is KillLevel.HARD
    assert (journal.kill_state().level, journal.kill_state().reason) == (KillLevel.HARD, "b")
    assert [level for level, _ in notifier.alerts] == ["SOFT", "HARD"]
    assert "a" in notifier.alerts[1][1] and "b" in notifier.alerts[1][1]  # eskalacja mówi, z czego i dlaczego


def test_soft_while_hard_keeps_hard_and_only_leaves_an_audit_trace_once(kill, journal, notifier, set_kill_calls):
    kill.hard("h")
    for _ in range(5):
        kill.soft("s")
    assert kill.level() is KillLevel.HARD
    assert journal.kill_state().level is KillLevel.HARD and journal.kill_state().reason == "h"
    assert set_kill_calls == [(KillLevel.HARD, "h")]  # żadnego zapisu soft
    assert len(notifier.alerts) == 1  # i żadnego nowego alarmu
    suppressed = [a for a in audit(journal) if a["event"] == "SUPPRESSED"]
    assert len(suppressed) == 1 and suppressed[0]["reason"] == "s" and suppressed[0]["level"] == "SOFT"


def test_ten_repeated_soft_calls_write_one_row_and_one_alert(kill, journal, notifier, set_kill_calls):
    for _ in range(10):  # tak woła Reconciler co 10 s, dopóki trwa rozbieżność
        kill.soft("X")
    assert set_kill_calls == [(KillLevel.SOFT, "X")]
    assert len(notifier.alerts) == 1
    assert len(journal.events("kill.audit")) == 1


def test_repeated_hard_calls_are_no_ops_too(kill, journal, notifier, set_kill_calls):
    for _ in range(10):
        kill.hard("X")
    assert set_kill_calls == [(KillLevel.HARD, "X")] and len(notifier.alerts) == 1
    assert len(journal.events("kill.audit")) == 1


def test_a_new_reason_at_the_same_level_is_written_and_alerted(kill, journal, notifier, set_kill_calls):
    kill.soft("a")
    kill.soft("b")
    kill.soft("a")  # znana przyczyna: bez zmian
    kill.soft("b")
    assert set_kill_calls == [(KillLevel.SOFT, "a"), (KillLevel.SOFT, "b")]
    assert len(notifier.alerts) == 2 and all(level == "SOFT" for level, _ in notifier.alerts)
    assert journal.kill_state().reason == "b"
    assert [a["reason"] for a in audit(journal)] == ["a", "b"]  # pełna historia przyczyn zostaje w audycie


def test_restart_does_not_alert_again_for_the_persisted_reason(tmp_path, clock, data_dir, notifier):
    path = str(tmp_path / "restart.sqlite")
    j1 = Journal.open(path)
    KillSwitch(j1, clock, data_dir, notifier).soft("X")
    j1.close()
    notifier.alerts.clear()

    j2 = Journal.open(path)
    try:
        ks = KillSwitch(j2, clock, data_dir, notifier)
        assert notifier.alerts == [] and ks.level() is KillLevel.SOFT  # samo odtworzenie nie krzyczy
        ks.soft("X")  # Reconciler po restarcie znów zgłasza to samo
        assert notifier.alerts == [] and len(j2.events("kill.audit")) == 1
        ks.soft("Y")  # a nowa przyczyna już tak
        assert len(notifier.alerts) == 1
    finally:
        j2.close()


def test_empty_reason_is_recorded_as_unspecified(kill, journal):
    kill.soft("")
    kill.hard("   ")
    assert journal.kill_state().reason == "UNSPECIFIED" and kill.level() is KillLevel.HARD


async def test_kill_switch_works_as_the_reconciler_kill_sink(journal, fake, clock, risk_cfg, kill, notifier):
    # Reconciler woła soft() przy każdym przebiegu, dopóki obca pozycja trwa: bez lawiny zapisów i alarmów
    fake.positions["DEAL-9"] = OpenPosition(
        "DEAL-9", "DE40", Side.BUY, Decimal("0.007"), Decimal("25782.3"), Decimal("25752.3"), T0 - timedelta(hours=1)
    )
    reconciler = Reconciler(journal, fake, clock, kill, risk_cfg, "DE40")
    for _ in range(4):  # 40 s: poniżej tolerancji 60 s, więc tylko FOREIGN_POSITION
        await reconciler.run_once()
        clock.advance(10)
    assert kill.level() is KillLevel.SOFT and journal.kill_state().reason == "FOREIGN_POSITION"
    assert [a["reason"] for a in audit(journal)] == ["FOREIGN_POSITION"]
    assert len(notifier.alerts) == 1


# --- clear ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["", "   ", "\n", None, 5])
def test_clear_requires_a_non_empty_requester(kill, journal, notifier, who):
    kill.soft("x")
    alerts_before = list(notifier.alerts)
    with pytest.raises(ValueError):
        kill.clear(who)
    assert kill.level() is KillLevel.SOFT and journal.kill_state().level is KillLevel.SOFT
    assert notifier.alerts == alerts_before


@pytest.mark.parametrize("name", ["STOP", "HARD_STOP"])
def test_clear_is_refused_while_a_stop_file_exists(kill, data_dir, journal, name):
    kill.hard("x")
    (data_dir / name).write_text("")
    with pytest.raises(ValueError, match=name):
        kill.clear("owner")
    assert kill.level() is KillLevel.HARD and journal.kill_state().level is KillLevel.HARD
    (data_dir / name).unlink()
    kill.clear("owner")
    assert kill.level() is KillLevel.NONE


def test_clear_writes_journal_audit_and_alert(kill, journal, notifier, clock):
    kill.soft("daily loss")
    clock.advance(60)
    kill.clear("owner@example")
    assert kill.level() is KillLevel.NONE and kill.reason() == ""
    state = journal.kill_state()
    assert state.level is KillLevel.NONE and state.since == clock.now_utc()
    entry = audit(journal)[-1]
    assert entry["event"] == "CLEAR" and entry["requested_by"] == "owner@example"
    assert entry["previous_level"] == "SOFT" and entry["previous_reason"] == "daily loss"
    assert entry["ts"] == clock.now_utc().isoformat()
    level, message = notifier.alerts[-1]
    assert level == "CLEAR" and "owner@example" in message and "daily loss" in message


def test_after_clear_the_same_reason_engages_again(kill, journal, notifier):
    kill.soft("X")
    kill.clear("owner")
    kill.soft("X")  # problem trwa: Reconciler zgłasza to samo
    assert kill.level() is KillLevel.SOFT and journal.kill_state().level is KillLevel.SOFT
    assert [level for level, _ in notifier.alerts] == ["SOFT", "CLEAR", "SOFT"]


def test_after_clear_a_suppressed_soft_is_remembered_afresh(kill, journal):
    kill.hard("h")
    kill.soft("s")
    kill.clear("owner")
    kill.hard("h2")
    kill.soft("s")
    assert len([a for a in audit(journal) if a["event"] == "SUPPRESSED"]) == 2


def test_clear_with_nothing_engaged_is_still_audited(kill, journal, notifier):
    kill.clear("owner")
    assert kill.level() is KillLevel.NONE
    assert audit(journal)[-1]["previous_level"] == "NONE" and notifier.alerts[-1][0] == "CLEAR"


def test_clear_fails_loudly_and_stays_engaged_when_the_journal_write_fails(kill, journal, monkeypatch):
    kill.soft("x")
    break_method(monkeypatch, journal, "set_kill", JournalWriteError("disk full"))
    with pytest.raises(JournalWriteError):
        kill.clear("owner")
    assert kill.level() is KillLevel.SOFT  # nieudane zdjęcie poziomu nie zdejmuje go w pamięci


def test_clear_survives_a_failing_notifier_and_audit(kill, journal, notifier, monkeypatch):
    kill.soft("x")
    notifier.fail = RuntimeError("smtp down")
    break_method(monkeypatch, journal, "append_raw", JournalWriteError("disk full"))
    kill.clear("owner")  # stan zdjęty w dzienniku, więc zdjęty także w pamięci
    assert kill.level() is KillLevel.NONE and journal.kill_state().level is KillLevel.NONE


# --- awarie: kill switch nie może zawieść -------------------------------------------------------------


@pytest.mark.parametrize("engage", ["soft", "hard"])
@pytest.mark.parametrize("exc", [JournalWriteError("disk full"), RuntimeError("weird")], ids=["write-error", "other"])
def test_journal_write_failure_still_engages_in_memory_and_alerts(kill, journal, notifier, monkeypatch, engage, exc):
    break_method(monkeypatch, journal, "set_kill", exc)
    getattr(kill, engage)("x")  # nie rzuca
    assert kill.level() is (KillLevel.SOFT if engage == "soft" else KillLevel.HARD)
    assert journal.kill_state().level is KillLevel.NONE  # dziennik nic nie wie
    assert len(notifier.alerts) == 1
    level, message = notifier.alerts[0]
    assert level == engage.upper() and "JOURNAL_WRITE_FAILED" in message and "x" in message


def test_journal_write_failure_retries_on_the_next_call_and_then_goes_quiet(
    kill, journal, notifier, monkeypatch, clock
):
    state = {"down": True, "calls": 0}
    real = journal.set_kill

    def flaky(level, reason, ts):
        state["calls"] += 1
        if state["down"]:
            raise JournalWriteError("locked")
        return real(level, reason, ts)

    monkeypatch.setattr(journal, "set_kill", flaky)
    kill.soft("x")
    kill.soft("x")  # dziennik nadal leży: ponowna próba, ale bez nowego alarmu
    assert len(notifier.alerts) == 1 and journal.kill_state().level is KillLevel.NONE

    state["down"] = False
    kill.soft("x")  # dziennik wrócił: stan zostaje utrwalony
    assert journal.kill_state() == KillState(KillLevel.SOFT, "x", clock.now_utc())
    assert [level for level, _ in notifier.alerts] == ["SOFT", "INFO"] and "x" in notifier.alerts[1][1]
    calls = state["calls"]
    kill.soft("x")
    kill.scan_files()
    assert state["calls"] == calls and len(notifier.alerts) == 2  # i znów cicho


def test_scan_files_also_retries_a_pending_journal_write(kill, journal, notifier, monkeypatch):
    real = journal.set_kill
    down = {"v": True}

    def flaky(level, reason, ts):
        if down["v"]:
            raise JournalWriteError("locked")
        return real(level, reason, ts)

    monkeypatch.setattr(journal, "set_kill", flaky)
    kill.hard("x")
    down["v"] = False
    kill.scan_files()
    assert journal.kill_state().level is KillLevel.HARD


def test_audit_write_failure_does_not_stop_the_kill(kill, journal, notifier, monkeypatch):
    break_method(monkeypatch, journal, "append_raw", JournalWriteError("disk full"))
    kill.soft("x")
    assert kill.level() is KillLevel.SOFT and journal.kill_state().level is KillLevel.SOFT  # stan zapisany
    assert "JOURNAL_WRITE_FAILED" in notifier.alerts[0][1]


@pytest.mark.parametrize("engage", ["soft", "hard"])
def test_notifier_failure_does_not_stop_the_kill(kill, journal, notifier, engage):
    notifier.fail = RuntimeError("alert channel down")
    getattr(kill, engage)("x")  # nie rzuca
    assert kill.level() is (KillLevel.SOFT if engage == "soft" else KillLevel.HARD)
    assert journal.kill_state().reason == "x"
    assert len(notifier.alerts) == 1  # próba była, jedna


def test_journal_and_notifier_both_down_still_engage(kill, journal, notifier, monkeypatch):
    notifier.fail = OSError("no alert channel")
    break_method(monkeypatch, journal, "set_kill", JournalWriteError("disk full"))
    break_method(monkeypatch, journal, "append_raw", JournalWriteError("disk full"))
    kill.hard("x")
    assert kill.level() is KillLevel.HARD


def test_unexpected_internal_error_does_not_stop_the_kill(kill, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr("gwstock.risk._reason_text", boom)
    kill.hard("x")  # nie rzuca, a poziom i tak obowiązuje
    assert kill.level() is KillLevel.HARD


def test_unreadable_clock_does_not_stop_the_kill(journal, data_dir, notifier):
    class BrokenClock:
        def now_utc(self):
            raise RuntimeError("clock exploded")

    ks = KillSwitch(journal, BrokenClock(), data_dir, notifier)
    ks.soft("x")
    assert ks.level() is KillLevel.SOFT and journal.kill_state().reason == "x"


def test_unreadable_kill_state_at_startup_starts_soft_and_never_downgrades_what_it_cannot_see(
    journal, clock, data_dir, notifier, monkeypatch, set_kill_calls
):
    break_method(monkeypatch, journal, "kill_state", JournalReadError("locked"))
    ks = KillSwitch(journal, clock, data_dir, notifier)  # nie rzuca
    assert ks.level() is KillLevel.SOFT
    assert len(notifier.alerts) == 1 and "KILL_STATE_UNREADABLE" in notifier.alerts[0][1]

    ks.soft("x")  # zapisanego stanu nie znamy (mógł być HARD): soft go nie nadpisuje
    assert set_kill_calls == [] and ks.level() is KillLevel.SOFT
    ks.hard("y")  # HARD nie obniży niczego, więc jest zapisywany
    assert set_kill_calls == [(KillLevel.HARD, "y")]
    ks.soft("z")  # stan znany: zwykłe zasady (HARD zostaje)
    assert ks.level() is KillLevel.HARD


def test_clear_after_an_unreadable_start_is_the_explicit_way_out(
    journal, clock, data_dir, notifier, monkeypatch, set_kill_calls
):
    break_method(monkeypatch, journal, "kill_state", JournalReadError("locked"))
    ks = KillSwitch(journal, clock, data_dir, notifier)
    ks.clear("owner")
    assert ks.level() is KillLevel.NONE
    assert set_kill_calls[-1][0] is KillLevel.NONE
    ks.soft("w")  # po jawnym zdjęciu stan jest znany, więc soft znów jest zapisywany
    assert set_kill_calls[-1] == (KillLevel.SOFT, "w")


def test_concurrent_callers_see_one_transition(kill, notifier, set_kill_calls):
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(50):
            kill.soft("X")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set_kill_calls == [(KillLevel.SOFT, "X")] and len(notifier.alerts) == 1


# --- FileNotifier ----------------------------------------------------------------------------------

ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00"


def test_implementations_satisfy_the_protocols(tmp_path, kill, notifier):
    assert isinstance(FileNotifier(tmp_path / "a.log"), Notifier)
    assert isinstance(notifier, Notifier)
    assert isinstance(kill, KillView)


def test_file_notifier_line_format_with_injected_clock(tmp_path, clock):
    path = tmp_path / "alerts.log"
    n = FileNotifier(path, clock)
    n.alert("SOFT", "kill switch engaged: x")
    clock.advance(90)
    n.alert("CLEAR", "cleared")
    assert path.read_text(encoding="utf-8") == (
        "2026-11-02T07:30:00+00:00 SOFT kill switch engaged: x\n"
        "2026-11-02T07:31:30+00:00 CLEAR cleared\n"
    )


def test_file_notifier_default_clock_is_the_system_clock(tmp_path):
    path = tmp_path / "alerts.log"
    before = datetime.now(timezone.utc)
    FileNotifier(path).alert("HARD", "boom")
    after = datetime.now(timezone.utc)
    ts, level, message = path.read_text(encoding="utf-8").rstrip("\n").split(" ", 2)
    assert re.fullmatch(ISO, ts) and level == "HARD" and message == "boom"
    assert before - timedelta(seconds=1) <= datetime.fromisoformat(ts) <= after + timedelta(seconds=1)


def test_file_notifier_keeps_one_line_per_alert(tmp_path, clock):
    path = tmp_path / "alerts.log"
    n = FileNotifier(path, clock)
    n.alert("SOFT", "line one\nline two\r\nline three\rend sep")
    n.alert("SOFT", "next")
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2
    assert "line one line two" in text and "line three end" in text


def test_file_notifier_level_cannot_break_the_line(tmp_path, clock):
    path = tmp_path / "alerts.log"
    FileNotifier(path, clock).alert("SO FT\nX", "m")
    line = path.read_text(encoding="utf-8").splitlines()
    assert len(line) == 1 and line[0].split(" ", 2)[1] == "SO_FT_X"


def test_file_notifier_creates_the_parent_directory_and_appends(tmp_path, clock):
    path = tmp_path / "deep" / "er" / "alerts.log"
    n = FileNotifier(path, clock)
    n.alert("SOFT", "a")
    n2 = FileNotifier(path, clock)  # nowy obiekt dopisuje do istniejącego pliku
    n2.alert("SOFT", "b")
    assert [line.split(" ", 2)[2] for line in path.read_text(encoding="utf-8").splitlines()] == ["a", "b"]


def test_file_notifier_survives_a_non_ascii_or_broken_message(tmp_path, clock):
    path = tmp_path / "alerts.log"
    FileNotifier(path, clock).alert("SOFT", "zażółć gęślą jaźń \ud800 koniec")  # samotny surogat nie zakodowałby się
    text = path.read_text(encoding="utf-8")
    assert "zażółć gęślą jaźń" in text and "koniec" in text


def test_file_notifier_never_raises_and_falls_back_to_stderr_when_the_path_is_a_directory(tmp_path, clock, capsys):
    target = tmp_path / "alerts.log"
    target.mkdir()
    FileNotifier(target, clock).alert("HARD", "must be seen")  # nie rzuca
    err = capsys.readouterr().err
    assert "HARD" in err and "must be seen" in err and "2026-11-02T07:30:00+00:00" in err


def test_file_notifier_never_raises_when_the_parent_cannot_be_created(tmp_path, clock, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    FileNotifier(blocker / "sub" / "alerts.log", clock).alert("SOFT", "still visible")
    err = capsys.readouterr().err
    assert "SOFT" in err and "still visible" in err


def test_file_notifier_survives_even_a_closed_stderr_and_a_broken_clock(tmp_path, monkeypatch):
    class BrokenClock:
        def now_utc(self):
            raise RuntimeError("clock exploded")

        async def sleep(self, seconds):
            pass

    class DeadStream:
        def write(self, text):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    target = tmp_path / "alerts.log"
    target.mkdir()
    monkeypatch.setattr("sys.stderr", DeadStream())
    FileNotifier(target, BrokenClock()).alert("SOFT", "nobody listens")  # nie rzuca


def test_kill_switch_with_a_file_notifier_that_cannot_write_still_engages(tmp_path, journal, clock, data_dir, capsys):
    target = tmp_path / "alerts.log"
    target.mkdir()  # zapis niemożliwy
    ks = KillSwitch(journal, clock, data_dir, FileNotifier(target, clock))
    ks.hard("x")
    assert ks.level() is KillLevel.HARD and journal.kill_state().level is KillLevel.HARD
    assert "x" in capsys.readouterr().err
