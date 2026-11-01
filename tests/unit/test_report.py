"""Testy księgi CSV, raportu dziennego i bramki L1 (spec §10, §11) na ręcznie policzonych transakcjach."""

import csv
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.config import CostConfig, load_config
from gwstock.journal import Journal, OrderRecord, TradeRecord
from gwstock.orders.ids import make_client_id
from gwstock.report import cli as report_cli
from gwstock.report import gates, ledger
from gwstock.report.gates import GateL1, gate_l1_status
from gwstock.report.ledger import LEDGER_COLUMNS, export_csv, trades_summary
from gwstock.serialization import dump
from gwstock.types import OrderState, Side

REPO = Path(__file__).resolve().parents[2]
D1, D2, D3, D4, D5, D6 = (date(2026, 11, n) for n in (2, 3, 4, 5, 6, 9))  # dni sesji XETRA (pn-pt)
COSTS = CostConfig(Decimal("0.5"), Decimal("1.0"), Decimal("0.004"), False)
SPEC_HEADER = [
    "instrument", "side", "size", "decision_bid", "decision_ask", "entry_fill", "exit_fill", "gross_pnl",
    "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl", "signal_ts", "submit_ts", "confirm_ts",
    "exit_ts", "latency_ms", "exit_reason",
]  # fmt: skip


def at(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=timezone.utc)


def make_trade(day: date = D1, **over) -> TradeRecord:
    fields = dict(
        instrument="DE40", side=Side.BUY, size=Decimal("0.001"),
        decision_bid=Decimal("25780.30"), decision_ask=Decimal("25782.30"),
        entry_fill=Decimal("25782.30"), exit_fill=Decimal("25761.80"),
        gross_pnl=Decimal("-0.02"), spread_cost=Decimal("0.00"), slippage_penalty=Decimal("0.00"),
        financing=Decimal("0.00"), fees=Decimal("0.00"), net_pnl=Decimal("-0.02"),
        signal_ts=at(day, 8, 5), submit_ts=at(day, 8, 5, 1), confirm_ts=at(day, 8, 5, 2),
        exit_ts=at(day, 16, 25), latency_ms=2000, exit_reason="FORCED",
    )  # fmt: skip
    fields.update(over)
    return TradeRecord(**fields)


def book_session(journal: Journal, day: date, *, role="entry", seq=1, ctx=True, vpp="1", **over) -> TradeRecord:
    """Zlecenie (rola `role`), kontekst wejścia i zaksięgowana transakcja, jak po prawdziwym dniu silnika.

    `vpp`: wartość punktu zapisana w kontekście wejścia (None: kontekst bez tego pola).
    """
    trade = make_trade(day, **over)
    client_id = make_client_id(day, "DE40", seq, role)
    deal_id = f"DEAL-{day:%Y%m%d}-{seq}-{role}"
    journal.insert_order(
        OrderRecord(
            client_id, "DE40", trade.side, trade.size, Decimal("30"), OrderState.CONFIRMED, f"ref-{seq}", deal_id,
            trade.signal_ts, trade.confirm_ts,
        )
    )  # fmt: skip
    if ctx:
        context = {"client_id": client_id, "signal_ts": trade.signal_ts, "submit_ts": trade.submit_ts}
        if vpp is not None:
            context["value_per_point"] = Decimal(vpp)
        journal.set_meta(f"entry_ctx:{client_id}", dump(context))
    assert journal.record_trade(trade, deal_id, day)
    return trade


def emit(journal: Journal, source: str, payload: dict, ts: datetime) -> None:
    journal.append_raw(source, dump(payload), ts)


def smoke_times(day: date) -> dict:
    """Próba smoke jest wcześniej niż wejście strategii (inne czasy sygnału i wysyłki)."""
    return dict(signal_ts=at(day, 7, 0), submit_ts=at(day, 7, 0, 1), confirm_ts=at(day, 7, 0, 2), exit_ts=at(day, 7, 1))


def later_times(day: date, hour: int) -> dict:
    """Inne czasy niż domyślne (drugie wejście tego samego dnia), żeby kontekst pasował do właściwej transakcji."""
    return dict(signal_ts=at(day, hour, 5), submit_ts=at(day, hour, 5, 1), confirm_ts=at(day, hour, 5, 2),
                exit_ts=at(day, hour, 40))  # fmt: skip


def five_clean_sessions(journal: Journal) -> None:
    for seq, day in enumerate((D1, D2, D3, D4, D5), start=1):
        book_session(journal, day, seq=1)


# --- księga CSV --------------------------------------------------------------------------------------------------


def test_ledger_columns_are_the_spec_header_and_the_trade_record_fields():
    assert list(LEDGER_COLUMNS) == SPEC_HEADER
    assert [f.name for f in dataclasses.fields(TradeRecord)] == SPEC_HEADER


def test_csv_header_is_exact_and_one_row_per_trade_in_insertion_order(journal, tmp_path):
    first = book_session(journal, D1)
    second = book_session(journal, D2, side=Side.SELL, size=Decimal("0.002"), exit_reason="STOP")
    path = tmp_path / "ledger.csv"
    assert export_csv(journal, path) == 2
    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == SPEC_HEADER
    assert len(rows) == 3
    assert rows[1][:3] == ["DE40", "BUY", "0.001"] and rows[2][:3] == ["DE40", "SELL", "0.002"]
    assert rows[1][-1] == "FORCED" and rows[2][-1] == "STOP"
    assert rows[1][13] == "2026-11-02T08:05:00Z" and rows[2][13] == "2026-11-03T08:05:00Z"
    assert rows[1][14] == "2026-11-02T08:05:01Z" and rows[1][16] == "2026-11-02T16:25:00Z"
    assert rows[1][17] == "2000"
    # każda wartość wraca do tej z dziennika (nic nie zaokrąglono)
    for row, trade in zip(rows[1:], (first, second)):
        for name, cell in zip(SPEC_HEADER, row):
            value = getattr(trade, name)
            if isinstance(value, Decimal):
                assert Decimal(cell) == value
                assert cell == format(value, "f")


def test_csv_decimals_are_plain_strings_without_scientific_notation(journal, tmp_path):
    book_session(
        journal, D1, size=Decimal("1E-3"), gross_pnl=Decimal("-0.0000001"), spread_cost=Decimal("1E+2"),
        slippage_penalty=Decimal("0.5000"), net_pnl=Decimal("-100.5000001"), financing=Decimal("0E-10"),
    )  # fmt: skip
    path = tmp_path / "ledger.csv"
    export_csv(journal, path)
    text = path.read_text(encoding="utf-8")
    row = text.splitlines()[1].split(",")
    assert row[2] == "0.001" and row[7] == "-0.0000001" and row[8] == "100" and row[9] == "0.5000"
    assert row[10] == "0.0000000000" and row[12] == "-100.5000001"
    assert "E" not in "".join(row[2:13]) and "e" not in "".join(row[2:13])


def test_csv_file_is_utf8_with_unix_newlines_and_mode_600(journal, tmp_path):
    book_session(journal, D1)
    path = tmp_path / "ledger.csv"
    export_csv(journal, path)
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and raw.count(b"\n") == 2
    raw.decode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("ledger.csv")] == ["ledger.csv"]


def test_csv_of_an_empty_journal_is_just_the_header(journal, tmp_path):
    path = tmp_path / "ledger.csv"
    assert export_csv(journal, path) == 0
    assert path.read_text(encoding="utf-8") == ",".join(SPEC_HEADER) + "\n"


def test_csv_overwrites_an_existing_file_atomically_and_keeps_it_on_failure(journal, tmp_path, monkeypatch):
    book_session(journal, D1)
    path = tmp_path / "ledger.csv"
    path.write_text("old\n")
    export_csv(journal, path)
    assert path.read_text(encoding="utf-8").startswith("instrument,")
    good = path.read_bytes()
    book_session(journal, D2)

    def failing_replace(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(ledger.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk says no"):
        export_csv(journal, path)
    monkeypatch.undo()
    assert path.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("ledger.csv")] == ["ledger.csv"]


def test_csv_refuses_a_directory_and_a_missing_parent(journal, tmp_path):
    book_session(journal, D1)
    with pytest.raises(IsADirectoryError):
        export_csv(journal, tmp_path)
    with pytest.raises(FileNotFoundError):
        export_csv(journal, tmp_path / "missing" / "ledger.csv")


# --- podsumowanie księgi -------------------------------------------------------------------------------------------


def test_trades_summary_sums_exactly_and_finds_the_worst_day(journal):
    book_session(journal, D1, gross_pnl=Decimal("0.10"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.02"),
                 financing=Decimal("0.00"), fees=Decimal("0.00"), net_pnl=Decimal("0.07"))  # fmt: skip
    book_session(journal, D2, gross_pnl=Decimal("0.20"), spread_cost=Decimal("0.03"), slippage_penalty=Decimal("0.04"),
                 financing=Decimal("0.01"), fees=Decimal("0.02"), net_pnl=Decimal("0.10"))  # fmt: skip
    book_session(journal, D3, gross_pnl=Decimal("-0.30"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.00"),
                 financing=Decimal("0.00"), fees=Decimal("0.00"), net_pnl=Decimal("-0.31"))  # fmt: skip
    book_session(journal, D4, gross_pnl=Decimal("0.01"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.00"),
                 financing=Decimal("0.00"), fees=Decimal("0.00"), net_pnl=Decimal("0.00"))  # fmt: skip
    summary = trades_summary(journal.trades())
    assert summary == {
        "count": 4, "gross": Decimal("0.01"), "spread_cost": Decimal("0.06"), "slippage_penalty": Decimal("0.06"),
        "financing": Decimal("0.01"), "fees": Decimal("0.02"), "net": Decimal("-0.14"), "wins": 2, "losses": 1,
        "breakeven": 1, "worst_day": (D3, Decimal("-0.31")),
    }  # fmt: skip


def test_trades_summary_is_exact_where_binary_floats_are_not():
    trades = [make_trade(D1 + timedelta(days=i), net_pnl=Decimal("0.1")) for i in range(3)]
    assert trades_summary(trades)["net"] == Decimal("0.3")  # 0.1 * 3 w float to 0.30000000000000004


def test_trades_summary_groups_by_local_exit_day_and_of_nothing_is_zero():
    late = make_trade(D1, exit_ts=datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), net_pnl=Decimal("-0.50"))
    # 23:30 UTC = 00:30 czasu Berlina następnego dnia: dzień zamknięcia to 2026-11-03
    assert trades_summary([late])["worst_day"] == (date(2026, 11, 3), Decimal("-0.50"))
    empty = trades_summary([])
    assert empty["count"] == 0 and empty["net"] == Decimal("0") and empty["worst_day"] is None
    assert empty["wins"] == empty["losses"] == empty["breakeven"] == 0


# --- GateL1: sama reguła ---------------------------------------------------------------------------------------------


def test_gate_l1_is_a_frozen_validated_dataclass():
    gate = GateL1(5, 0, 0, Decimal("1"), cost_samples=5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.unhandled_rejections = 1  # type: ignore[misc]
    zero = Decimal(0)
    bad = [(-1, 0, 0, zero), (5, -1, 0, zero), (5, 0, -1, zero), (5, 0, 0, Decimal(-1)), (True, 0, 0, zero),
           (5.0, 0, 0, zero), (5, 0, 0, 1.0), (5, 0, 0, Decimal("NaN"))]  # fmt: skip
    for args in bad:
        with pytest.raises((TypeError, ValueError)):
            GateL1(*args)
    for samples in (-1, True, 2.0, "3", None):
        with pytest.raises((TypeError, ValueError)):
            GateL1(5, 0, 0, zero, cost_samples=samples)  # type: ignore[arg-type]
    assert GateL1(5, 0, 0, zero).cost_samples == 0  # bez podanych próbek: brak pomiaru


def test_gate_l1_needs_five_sessions_three_clean_conditions_and_cost_samples():
    assert GateL1(5, 0, 0, Decimal("1.4"), cost_samples=5).passed(5) is True
    assert GateL1(6, 0, 0, Decimal("0"), cost_samples=6).passed(5) is True
    assert GateL1(4, 0, 0, Decimal("1"), cost_samples=5).passed(5) is False  # 4 sesje
    assert GateL1(5, 1, 0, Decimal("1"), cost_samples=5).passed(5) is False  # rozbieżność > 60 s
    assert GateL1(5, 0, 1, Decimal("1"), cost_samples=5).passed(5) is False  # nieobsłużone odrzucenie
    assert GateL1(5, 0, 0, Decimal("2.0001"), cost_samples=5).passed(5) is False  # koszty > 2x model
    assert GateL1(5, 0, 0, Decimal("2"), cost_samples=5).passed(5) is True  # dokładnie 2x: jeszcze bez przeglądu
    assert GateL1(5, 0, 0, Decimal("Infinity"), cost_samples=5).passed(5) is False


def test_gate_l1_never_passes_without_enough_usable_cost_samples():
    # zero dowodów to nie dowód: 0 próbek nie przechodzi, nawet gdy stosunek wynosi 0
    assert GateL1(5, 0, 0, Decimal("0")).passed(5) is False
    assert GateL1(5, 0, 0, Decimal("0"), cost_samples=0).passed(5) is False
    assert GateL1(1, 0, 0, Decimal("0"), cost_samples=0).passed(1) is False
    # mniej próbek niż wymaganych sesji też nie przechodzi
    assert GateL1(5, 0, 0, Decimal("0.5"), cost_samples=4).passed(5) is False
    assert GateL1(5, 0, 0, Decimal("0.5"), cost_samples=5).passed(5) is True
    assert GateL1(5, 0, 0, Decimal("0.5"), cost_samples=9).passed(5) is True


def test_gate_l1_required_sessions_must_be_a_positive_integer():
    gate = GateL1(0, 0, 0, Decimal("0"))
    for bad in (0, -1, True, 5.0, "5"):
        with pytest.raises((TypeError, ValueError)):
            gate.passed(bad)  # type: ignore[arg-type]
    assert GateL1(1, 0, 0, Decimal("0"), cost_samples=1).passed(1) is True


def test_gate_l1_reasons_name_every_failed_condition():
    assert GateL1(5, 0, 0, Decimal("1"), cost_samples=5).reasons(5) == []
    reasons = GateL1(4, 2, 1, Decimal("2.5"), cost_samples=3).reasons(5)
    assert len(reasons) == 5
    assert "4 z 5" in reasons[0] and "brakuje 1" in reasons[0]
    assert "2" in reasons[1] and "60 s" in reasons[1]
    assert "nieobsłużone" in reasons[2]
    assert reasons[3] == "brak pomiaru kosztów: użytecznych próbek 3 z 5"
    assert "2.5" in reasons[4]
    assert GateL1(5, 0, 0, Decimal("0")).reasons(5) == ["brak pomiaru kosztów: użytecznych próbek 0 z 5"]


# --- gate_l1_status na dzienniku --------------------------------------------------------------------------


def test_status_of_an_empty_journal(journal):
    assert gate_l1_status(journal, costs=COSTS) == GateL1(0, 0, 0, Decimal("0"))


def test_four_sessions_do_not_pass_five_clean_sessions_do(journal):
    for day in (D1, D2, D3, D4):
        book_session(journal, day)
    four = gate_l1_status(journal, costs=COSTS)
    assert four.sessions_at_min_size == 4 and four.passed(5) is False
    book_session(journal, D5)
    five = gate_l1_status(journal, costs=COSTS)
    assert five.sessions_at_min_size == 5 and five.passed(5) is True


def test_smoke_trades_are_not_sessions(journal):
    for day in (D1, D2, D3, D4):
        book_session(journal, day)
    book_session(journal, D5, role="smoke", seq=1, **smoke_times(D5))  # próba smoke tego dnia, ale nie sesja
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 4
    book_session(journal, D5, seq=2)  # prawdziwe wejście strategii w tym dniu
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 5


def test_roles_are_found_by_the_entry_context_timestamps(journal):
    entry = book_session(journal, D1)
    smoke = book_session(journal, D2, role="smoke")
    lost = book_session(journal, D3, ctx=False)  # zdegradowany wiersz bez kontekstu
    assert gates.trade_roles(journal, [entry, smoke, lost]) == ["entry", "smoke", "unknown"]


def test_smoke_classification_agrees_with_the_engine_session_index(journal):
    # silnik liczy sesje jako len(trades) - count_smoke_trades(); smoke wykryty tu musi być tym samym zbiorem
    book_session(journal, D1)
    book_session(journal, D2, role="smoke", **smoke_times(D2))
    book_session(journal, D3)
    book_session(journal, D4, role="smoke", **smoke_times(D4))
    roles = gates.trade_roles(journal, journal.trades())
    assert roles.count("smoke") == journal.count_smoke_trades() == 2
    assert roles.count("entry") == len(journal.trades()) - journal.count_smoke_trades()


def test_a_trade_without_context_is_not_counted_as_a_session(journal):
    for day in (D1, D2, D3, D4):
        book_session(journal, day)
    book_session(journal, D5, ctx=False)
    status = gate_l1_status(journal, costs=COSTS)
    assert status.sessions_at_min_size == 4  # nie da się wykazać, że to wejście strategii


def test_smoke_context_on_the_neighbouring_day_is_still_found(journal):
    # polecenie smoke może nazwać zlecenie datą dnia lokalnego, a sygnał mieć czas UTC z innej daty kalendarzowej
    trade = make_trade(D2, signal_ts=at(D2, 23, 50), submit_ts=at(D2, 23, 51), confirm_ts=at(D2, 23, 52))
    client_id = make_client_id(D3, "DE40", 1, "smoke")
    journal.insert_order(OrderRecord(client_id, "DE40", Side.BUY, trade.size, Decimal("30"), OrderState.CONFIRMED,
                                     "r", "D", trade.signal_ts, trade.confirm_ts))  # fmt: skip
    context = {"signal_ts": trade.signal_ts, "submit_ts": trade.submit_ts, "value_per_point": Decimal("1")}
    journal.set_meta(f"entry_ctx:{client_id}", dump(context))
    journal.record_trade(trade, "D", D3)
    assert gates.trade_roles(journal, [trade]) == ["smoke"]


def test_min_size_defaults_to_the_smallest_entry_size_and_can_be_given(journal):
    book_session(journal, D1, size=Decimal("0.001"))
    book_session(journal, D2, size=Decimal("0.001"))
    book_session(journal, D3, size=Decimal("0.002"))
    book_session(journal, D4, size=Decimal("0.003"))
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 2
    assert gate_l1_status(journal, min_size=Decimal("0.002"), costs=COSTS).sessions_at_min_size == 1
    assert gate_l1_status(journal, min_size=Decimal("0.0005"), costs=COSTS).sessions_at_min_size == 0


def test_a_smoke_trade_does_not_set_the_minimum(journal):
    book_session(journal, D1, role="smoke", size=Decimal("0.0005"))
    book_session(journal, D2, size=Decimal("0.001"))
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 1


def test_a_session_is_a_day(journal):
    book_session(journal, D1, seq=1)
    book_session(journal, D1, seq=2)  # dwa wejścia jednego dnia to jedna sesja
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 1


def test_mismatch_timeouts_are_counted_as_distinct_incidents(journal):
    ts = at(D1, 8, 20)
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, ts)
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, ts + timedelta(seconds=10))
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, ts + timedelta(seconds=20))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 1  # jedna rozbieżność, trzy przebiegi
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D3, 8, 10)}, at(D3, 8, 20))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 2


def test_mismatch_timeout_is_found_in_the_kill_audit_when_the_reconcile_event_is_missing(journal):
    audit = {"event": "ENGAGE", "level": "SOFT", "reason": "MISMATCH_TIMEOUT", "previous_level": "NONE",
             "previous_reason": ""}  # fmt: skip
    emit(journal, "kill.audit", audit, at(D1, 8, 20))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 1
    # to samo zdarzenie widoczne w obu źródłach to nadal jedna rozbieżność
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, at(D1, 8, 20))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 1
    # kolejne zdarzenie w audycie (drugi dzień) to druga rozbieżność
    emit(journal, "kill.audit", {**audit, "event": "REASON"}, at(D3, 8, 20))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 2


def test_other_kill_reasons_and_unrelated_events_are_not_mismatches(journal):
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "STALE_QUOTE"}, at(D1, 8, 20))
    emit(journal, "kill.audit", {"event": "CLEAR", "level": "NONE", "reason": ""}, at(D1, 9, 0))
    emit(journal, "kill.audit", {"event": "RECOVERED", "level": "SOFT", "reason": "MISMATCH_TIMEOUT"}, at(D1, 9, 5))
    emit(journal, "reconcile.event", {"event": "CLOSURE_FOUND"}, at(D1, 9, 6))
    emit(journal, "decision", {"event": "DECISION", "why": "MISMATCH_TIMEOUT"}, at(D1, 9, 7))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 0


def test_an_unparseable_row_that_names_the_reason_still_counts(journal):
    journal.append_raw("reconcile.event", "MISMATCH_TIMEOUT since=?", at(D1, 8, 20))
    journal.append_raw("kill.audit", "{not json", at(D1, 8, 21))
    assert gate_l1_status(journal, costs=COSTS).unreconciled_over_60s == 1


def test_unhandled_rejections_count_unresolved_entries_exceptions_and_open_orders(journal):
    unresolved = {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261102-DE40-1-entry", "state": "UNKNOWN"}
    emit(journal, "event", unresolved, at(D1, 8, 6))
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 1
    emit(journal, "event", unresolved, at(D1, 8, 7))  # to samo zlecenie, drugi wpis
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 1
    # to samo zlecenie nadal nierozstrzygnięte w bazie: nadal jeden incydent
    journal.insert_order(OrderRecord("gws-20261102-DE40-1-entry", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                                     OrderState.UNKNOWN, "r", None, at(D1, 8, 5), at(D1, 8, 6)))  # fmt: skip
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 1
    # inne zlecenie nierozstrzygnięte, bez zdarzenia (np. awaria po INTENT)
    journal.insert_order(OrderRecord("gws-20261103-DE40-1-entry", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                                     OrderState.INTENT, None, None, at(D2, 8, 5), at(D2, 8, 5)))  # fmt: skip
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 2
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "ENTRY_EXCEPTION"}, at(D3, 8, 6))
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 3


def test_resolved_and_rejected_orders_are_not_unhandled(journal):
    for seq, state in enumerate((OrderState.CONFIRMED, OrderState.REJECTED, OrderState.NOT_PLACED), start=1):
        journal.insert_order(OrderRecord(f"gws-20261102-DE40-{seq}-entry", "DE40", Side.BUY, Decimal("0.001"),
                                         Decimal("30"), state, "r", None, at(D1, 8, 5), at(D1, 8, 6)))  # fmt: skip
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 0


def test_cost_verdict_is_the_adverse_entry_slippage_over_the_model(journal):
    # A: BUY, bid 100 / ask 102, wypełnienie 102.5 -> poślizg 0.5; B: BUY, bid 200 / ask 201, wypełnienie 202 -> 1.0
    # werdykt = (0.5 + 1.0) / (2 * 0.5) = 1.5; wielkość, wartość punktu, zaokrąglenia księgi i STOP nie mają wpływu
    book_session(journal, D1, size=Decimal("1"), decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 entry_fill=Decimal("102.5"), exit_reason="FORCED")  # fmt: skip
    book_session(journal, D2, size=Decimal("2"), decision_bid=Decimal("200"), decision_ask=Decimal("201"),
                 entry_fill=Decimal("202"), exit_reason="STOP")  # fmt: skip
    details = gates.l1_details(journal, costs=COSTS)
    assert details.gate.cost_deviation_ratio == Decimal("1.5")
    assert details.gate.cost_samples == details.points.trades == 2
    # informacyjnie: spread + poślizg wejścia kontra spread + model: (2.5 + 2.0) / (3 + 1.0)
    assert details.points.combined_ratio == Decimal("4.5") / Decimal("4")


def test_the_spread_is_not_netted_into_the_verdict(journal):
    # spread 20 pkt i poślizg 3.0: łączny stosunek (20 + 3) / (20 + 0.5) = 1.12 by przeszedł, werdykt 3.0 / 0.5 = 6
    book_session(journal, D1, decision_bid=Decimal("100"), decision_ask=Decimal("120"), entry_fill=Decimal("123"))
    details = gates.l1_details(journal, costs=COSTS)
    assert details.gate.cost_deviation_ratio == Decimal("6")
    assert details.points.combined_ratio == Decimal("23") / Decimal("20.5")


def test_price_improvement_does_not_offset_adverse_slippage(journal):
    # SELL: poślizg = bid - wypełnienie; 100 / 102, wypełnienie 99.5 -> 0.5 niekorzystny
    book_session(journal, D1, side=Side.SELL, decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 entry_fill=Decimal("99.5"))  # fmt: skip
    assert gate_l1_status(journal, costs=COSTS).cost_deviation_ratio == Decimal("1")
    # SELL po 101.5 przy bid 100 -> poprawa 1.5: liczona jako 0, nie odejmuje się od pierwszego
    book_session(journal, D2, side=Side.SELL, decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 entry_fill=Decimal("101.5"))  # fmt: skip
    details = gates.l1_details(journal, costs=COSTS)
    assert details.gate.cost_deviation_ratio == Decimal("0.5")  # 0.5 / (2 * 0.5)
    assert details.points.combined_ratio == Decimal("0.6")  # informacyjnie, ze znakiem: (2.5 + 0.5) / (4 + 1.0)


def test_cost_ratio_boundary_two_passes_above_two_fails(journal):
    # 5 sesji z poślizgiem 1.0 (wypełnienie 103 przy ask 102): 5.0 / (5 * 0.5) = 2 -> przechodzi
    for day in (D1, D2, D3, D4, D5):
        book_session(journal, day, decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                     entry_fill=Decimal("103"))  # fmt: skip
    at_limit = gate_l1_status(journal, costs=COSTS)
    assert at_limit.cost_deviation_ratio == Decimal("2") and at_limit.passed(5) is True
    # szósta z poślizgiem 1.1: 6.1 / 3.0 > 2
    book_session(journal, D6, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("103.1"))
    above = gate_l1_status(journal, costs=COSTS)
    assert above.cost_deviation_ratio == Decimal("6.1") / Decimal("3") and above.passed(5) is False


def test_cost_ratio_edge_cases(journal):
    details = gates.l1_details(journal, costs=COSTS)
    assert details.gate.cost_deviation_ratio == Decimal("0") and details.points.trades == 0
    # wiersz zdegradowany (notowanie = poziom otwarcia, spread 0) nie jest próbką kosztu
    book_session(journal, D1, decision_bid=Decimal("100"), decision_ask=Decimal("100"), entry_fill=Decimal("100"))
    details = gates.l1_details(journal, costs=COSTS)
    assert details.gate.cost_deviation_ratio == Decimal("0") and details.gate.cost_samples == 0
    # zerowy model poślizgu: niekorzystny poślizg wobec modelu 0 to nieskończoność, brak poślizgu to 0
    zero = CostConfig(Decimal("0"), Decimal("0"), Decimal("0"), False)
    book_session(journal, D2, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("102"))
    assert gate_l1_status(journal, costs=zero).cost_deviation_ratio == Decimal("0")
    book_session(journal, D3, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("103"))
    assert gate_l1_status(journal, costs=zero).cost_deviation_ratio == Decimal("Infinity")


def test_a_gate_with_no_usable_cost_sample_does_not_pass(journal):
    for day in (D1, D2, D3, D4, D5):  # pięć sesji, ale każda z notowaniem zdegradowanym (bid = ask)
        book_session(journal, day, decision_bid=Decimal("100"), decision_ask=Decimal("100"), entry_fill=Decimal("100"))
    status = gate_l1_status(journal, costs=COSTS)
    assert status.sessions_at_min_size == 0 and status.cost_samples == 0 and status.cost_deviation_ratio == 0
    assert status.passed(5) is False
    assert status.reasons(5) == [
        "za mało sesji na wielkości minimalnej: 0 z 5 (brakuje 5)",
        "brak pomiaru kosztów: użytecznych próbek 0 z 5",
    ]
    assert GateL1(5, 0, 0, Decimal("0"), cost_samples=status.cost_samples).passed(5) is False


def test_fewer_usable_cost_samples_than_required_sessions_do_not_pass(journal):
    for day in (D1, D2, D3, D4):
        book_session(journal, day)
    book_session(journal, D5, decision_bid=Decimal("100"), decision_ask=Decimal("100"), entry_fill=Decimal("100"))
    status = gate_l1_status(journal, costs=COSTS)
    # sesja bez użytecznego notowania nie jest ani próbką, ani sesją (ta sama rola `no_quote`)
    assert status.sessions_at_min_size == 4 and status.cost_samples == 4
    assert status.passed(5) is False
    assert status.reasons(5) == [
        "za mało sesji na wielkości minimalnej: 4 z 5 (brakuje 1)",
        "brak pomiaru kosztów: użytecznych próbek 4 z 5",
    ]
    book_session(journal, D6)  # szósta sesja z pomiarem: pięć sesji i pięć próbek
    assert gate_l1_status(journal, costs=COSTS).passed(5) is True


def test_smoke_trades_take_part_in_the_cost_measurement(journal):
    book_session(journal, D1, role="smoke", decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 entry_fill=Decimal("103.1"))  # fmt: skip
    status = gate_l1_status(journal, costs=COSTS)
    assert status.cost_deviation_ratio == Decimal("1.1") / Decimal("0.5") and status.cost_samples == 1


def test_costs_default_to_the_configuration_directory(journal, monkeypatch):
    monkeypatch.setenv("GW_CONFIG_DIR", str(REPO / "config"))
    book_session(journal, D1, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("102.5"))
    assert gate_l1_status(journal).cost_deviation_ratio == Decimal("1")  # config/costs_v1.toml: 0.5 pkt wejścia


def test_measured_points_use_decision_quotes_and_fills():
    buy = make_trade(D1, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("102.5"),
                     exit_reason="STOP")  # fmt: skip
    sell = make_trade(D2, side=Side.SELL, decision_bid=Decimal("200"), decision_ask=Decimal("201"),
                      entry_fill=Decimal("199"))  # fmt: skip
    degraded = make_trade(D3, decision_bid=Decimal("300"), decision_ask=Decimal("300"), entry_fill=Decimal("300"))
    points = gates.cost_points([buy, sell, degraded], COSTS)
    assert points.trades == 2  # wiersz zdegradowany (spread 0) nie jest próbką
    assert (points.spread, points.spread_max) == (Decimal("1.5"), Decimal("2"))  # (2 + 1) / 2
    assert (points.entry_slippage, points.entry_slippage_max) == (Decimal("0.75"), Decimal("1.0"))  # (0.5 + 1.0) / 2
    assert points.ratio == Decimal("1.5")  # 1.5 / (2 * 0.5)
    assert points.combined_ratio == Decimal("4.5") / Decimal("4")  # (2.5 + 2.0) / (3 + 1.0)
    assert points.stop_exits == 1  # liczone po wszystkich transakcjach: nie są mierzone
    empty = gates.cost_points([], COSTS)
    assert (empty.trades, empty.spread, empty.entry_slippage, empty.ratio) == (0, None, None, Decimal("0"))
    assert (empty.spread_max, empty.entry_slippage_max, empty.combined_ratio, empty.stop_exits) == (
        None, None, Decimal("0"), 0)  # fmt: skip


def test_cost_points_can_exclude_trades_and_report_a_price_improvement_as_zero_slippage():
    better = make_trade(D1, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("101"))
    worse = make_trade(D2, decision_bid=Decimal("100"), decision_ask=Decimal("102"), entry_fill=Decimal("104"))
    points = gates.cost_points([better, worse], COSTS, include=[True, False])
    assert points.trades == 1 and points.entry_slippage == 0 and points.entry_slippage_max == 0
    assert points.ratio == Decimal("0")
    assert gates.cost_points([better, worse], COSTS).ratio == Decimal("2") / Decimal("1")  # (0 + 2) / (2 * 0.5)


def test_eur_ratio_uses_the_value_per_point_stored_in_the_entry_context(journal):
    # A: size 1, spread 2, FORCED: model EUR = (2 + 0.5) * 1 * vpp; księga 4.0 + 0.5 = 4.5
    # B: size 2, spread 1, STOP: model EUR = (1 + 0.5 + 1.0) * 2 * vpp = 5.0 * vpp; księga 3.0 + 3.0 = 6.0
    book_session(journal, D1, size=Decimal("1"), decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 spread_cost=Decimal("4.00"), slippage_penalty=Decimal("0.50"), exit_reason="FORCED")  # fmt: skip
    book_session(journal, D2, size=Decimal("2"), decision_bid=Decimal("200"), decision_ask=Decimal("201"),
                 spread_cost=Decimal("3.00"), slippage_penalty=Decimal("3.00"), exit_reason="STOP")  # fmt: skip
    details = gates.l1_details(journal, costs=COSTS)
    assert details.eur_ratio == Decimal("1.4") and details.excluded_trades == 0  # 10.5 / 7.5


def test_eur_ratio_with_a_different_value_per_point_in_the_context(journal):
    book_session(journal, D1, vpp="2", size=Decimal("1"), decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 spread_cost=Decimal("4.00"), slippage_penalty=Decimal("0.50"), exit_reason="FORCED")  # fmt: skip
    book_session(journal, D2, vpp="2", size=Decimal("2"), decision_bid=Decimal("200"), decision_ask=Decimal("201"),
                 spread_cost=Decimal("3.00"), slippage_penalty=Decimal("3.00"), exit_reason="STOP")  # fmt: skip
    details = gates.l1_details(journal, costs=COSTS)
    assert details.eur_ratio == Decimal("0.7") and details.excluded_trades == 0  # 10.5 / 15


def test_trades_without_a_usable_context_are_excluded_everywhere_and_counted(journal):
    book_session(journal, D1, vpp="2", size=Decimal("1"), decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 spread_cost=Decimal("4.00"), slippage_penalty=Decimal("0.50"), exit_reason="FORCED")  # fmt: skip
    book_session(journal, D2, ctx=False, size=Decimal("2"), decision_bid=Decimal("200"), decision_ask=Decimal("201"),
                 entry_fill=Decimal("260"), spread_cost=Decimal("3.00"), slippage_penalty=Decimal("3.00"))  # fmt: skip
    book_session(journal, D3, vpp=None, size=Decimal("1"), decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                 entry_fill=Decimal("190"), spread_cost=Decimal("9.00"))  # fmt: skip
    details = gates.l1_details(journal, costs=COSTS)
    assert details.roles == ("entry", "unknown", "invalid")
    assert details.excluded_trades == 2
    assert details.eur_ratio == Decimal("4.5") / Decimal("5")  # tylko A: (2 + 0.5) * 1 * 2 = 5; ani B, ani C
    assert details.points.trades == 1 and details.gate.cost_samples == 1  # B i C (ogromny poślizg) poza próbką
    assert details.gate.sessions_at_min_size == 1


@pytest.mark.parametrize(
    "bad",
    ["0", "-2", "abc", "NaN", "Infinity", "-Infinity", "", "1001", "1E+999999", "1e999999999999999999999",
     "0.0009", "1E-6", "1E-30", "1E-999999",
     1, 1.0, True, None, [1], {"a": 1}, float("inf")],
)  # fmt: skip
def test_a_corrupted_or_absurd_value_per_point_makes_the_trade_unusable(journal, bad):
    trade = book_session(journal, D1, vpp=None)
    client_id = make_client_id(D1, "DE40", 1, "entry")
    context = {"client_id": client_id, "signal_ts": trade.signal_ts.isoformat(), "value_per_point": bad,
               "submit_ts": trade.submit_ts.isoformat()}  # fmt: skip
    journal.set_meta(f"entry_ctx:{client_id}", json.dumps(context))
    details = gates.l1_details(journal, costs=COSTS)
    assert details.roles == ("invalid",)  # nie wejście, nie smoke
    assert details.excluded_trades == 1
    assert details.gate.sessions_at_min_size == 0 and details.gate.cost_samples == 0
    assert details.eur_ratio == Decimal("0")  # żadnej liczby z uszkodzonego rekordu


@pytest.mark.parametrize("good", ["1", "2.5", "1000", "0.001"])
def test_a_sane_value_per_point_is_accepted(journal, good):
    book_session(journal, D1, vpp=good)
    details = gates.l1_details(journal, costs=COSTS)
    assert details.roles == ("entry",) and details.excluded_trades == 0
    assert gates.MAX_VALUE_PER_POINT == Decimal("1000")  # 1000 x domyślna wartość punktu
    assert gates.MIN_VALUE_PER_POINT == Decimal("0.001")  # domyślna wartość punktu / 1000


def test_incident_dates_are_local_days_of_the_counted_incidents(journal):
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, at(D1, 8, 20))
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, at(D1, 8, 30))
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D3, 8, 10)}, at(D3, 8, 20))
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261103-DE40-1-entry"}, at(D2, 8, 8))
    journal.insert_order(OrderRecord("gws-20261105-DE40-1-entry", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                                     OrderState.UNKNOWN, "r", None, at(D4, 8, 5), at(D4, 8, 6)))  # fmt: skip
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "ENTRY_EXCEPTION"}, at(D5, 8, 6))
    details = gates.l1_details(journal, costs=COSTS)
    assert details.mismatch_days == (D1, D3)
    assert details.rejection_days == (D2, D4, D5)
    assert details.gate.unreconciled_over_60s == 2 and details.gate.unhandled_rejections == 3


def test_an_entry_unresolved_row_counts_even_when_the_order_was_resolved_later(journal):
    journal.insert_order(OrderRecord("gws-20261102-DE40-1-entry", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                                     OrderState.CONFIRMED, "r", "D", at(D1, 8, 5), at(D1, 8, 9)))  # fmt: skip
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261102-DE40-1-entry"}, at(D1, 8, 6))
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 1  # rozstrzygnięte później, ślad został
    journal.insert_order(OrderRecord("gws-20261103-DE40-1-entry", "DE40", Side.BUY, Decimal("0.001"), Decimal("30"),
                                     OrderState.SUBMITTING, "r", None, at(D2, 8, 5), at(D2, 8, 5)))  # fmt: skip
    assert gate_l1_status(journal, costs=COSTS).unhandled_rejections == 2  # wejście w toku w chwili raportu


# --- raport: wyjście -------------------------------------------------------------------------------------------------


@pytest.fixture
def config_env(monkeypatch):
    monkeypatch.setenv("GW_CONFIG_DIR", str(REPO / "config"))


def run(argv, capsys):
    code = report_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def jpath(tmp_path, journal) -> str:
    """Ścieżka pliku dziennika z fixture `journal` (tmp_path/journal.sqlite)."""
    return str(tmp_path / "journal.sqlite")


STATEMENT = "To pomiar, nie prognoza. Wynik netto po kosztach; brak przewagi nie jest wykazany ani wykluczony."


def test_report_prints_one_day_with_exact_lines(journal, capsys, config_env, jpath):
    book_session(journal, D1, gross_pnl=Decimal("-0.02"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.00"),
                 net_pnl=Decimal("-0.03"))  # fmt: skip
    book_session(journal, D2, gross_pnl=Decimal("0.05"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.01"),
                 net_pnl=Decimal("0.03"))  # fmt: skip
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-03", "mode": "dry_run", "outcome": "SIGNAL"},
         at(D2, 8, 5))  # fmt: skip
    emit(journal, "gate_block", {"reasons": ["ENTRIES_PER_DAY", "MARGIN"], "day": "2026-11-03"}, at(D2, 8, 6))
    emit(journal, "gate_block", {"reasons": ["MARGIN"], "day": "2026-11-03"}, at(D2, 8, 7))
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261103-DE40-1-entry"}, at(D2, 8, 8))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "ENTRY_UNRESOLVED"}, at(D2, 8, 8))
    emit(journal, "watchdog", {"event": "LOGIN_LOCKOUT", "failures": 3}, at(D2, 9, 0))
    code, out, err = run(["--journal", jpath], capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    start = lines.index("Dzień: 2026-11-03 (czas Europe/Berlin, dzień zamknięcia transakcji)")
    assert lines[start : start + 17] == [
        "Dzień: 2026-11-03 (czas Europe/Berlin, dzień zamknięcia transakcji)",
        "Tryb: dry_run",
        "Transakcje: 1",
        "Brutto: 0.05",
        "Koszt spreadu: 0.01",
        "Poślizg (model z konfiguracji): 0.01",
        "Finansowanie: 0.00",
        "Opłaty: 0.00",
        "Netto: 0.03",
        "Netto narastająco: 0.00",
        "Wynik zrealizowany wg dziennika: dzień 0.03, łącznie od początku 0.00",
        "Błędy i zdarzenia:",
        "  blokady bramki ryzyka: 2 (ENTRIES_PER_DAY: 1, MARGIN: 2)",
        "  ENTRY_UNRESOLVED: 1",
        "  kill switch: ENGAGE SOFT ENTRY_UNRESOLVED x1",
        "  blokady logowania (LOGIN_LOCKOUT): 1",
        "",
    ]
    assert "Dzień: 2026-11-02" not in out  # domyślnie tylko ostatni dzień
    assert STATEMENT in lines


def test_report_marks_smoke_trades_in_the_day_and_explains_the_rounding(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    book_session(journal, D1, role="smoke", seq=2, **smoke_times(D1))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "Transakcje: 2 (w tym próby smoke: 1)" in lines
    assert "  próby smoke (poza liczbą sesji): 1" in lines
    assert CAVEAT in lines


def test_report_default_is_the_last_day_and_all_lists_every_day_with_cumulative_net(journal, capsys, config_env, jpath):
    book_session(journal, D1, net_pnl=Decimal("-0.03"))
    book_session(journal, D2, net_pnl=Decimal("0.10"), gross_pnl=Decimal("0.12"), spread_cost=Decimal("0.02"))
    book_session(journal, D3, net_pnl=Decimal("-0.30"), gross_pnl=Decimal("-0.28"), spread_cost=Decimal("0.02"))
    path = jpath
    _, last, _ = run(["--journal", path], capsys)
    assert last.count("Dzień: ") == 1 and "Dzień: 2026-11-04" in last
    assert "Netto narastająco: -0.23" in last
    _, everything, _ = run(["--journal", path, "--all"], capsys)
    days = [line for line in everything.splitlines() if line.startswith("Dzień: ")]
    assert [d[7:17] for d in days] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    cumulative = [line for line in everything.splitlines() if line.startswith("Netto narastająco: ")]
    assert cumulative == ["Netto narastająco: -0.03", "Netto narastająco: 0.07", "Netto narastająco: -0.23"]
    _, one, _ = run(["--journal", path, "--day", "2026-11-03"], capsys)
    assert one.count("Dzień: ") == 1 and "Dzień: 2026-11-03" in one and "Netto narastająco: 0.07" in one


def test_report_summary_totals_and_the_statement(journal, capsys, config_env, jpath):
    book_session(journal, D1, gross_pnl=Decimal("-0.02"), spread_cost=Decimal("0.01"), net_pnl=Decimal("-0.03"))
    book_session(journal, D2, gross_pnl=Decimal("-0.28"), spread_cost=Decimal("0.02"), net_pnl=Decimal("-0.30"))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    start = lines.index("Podsumowanie od początku dziennika:")
    assert lines[start : start + 5] == [
        "Podsumowanie od początku dziennika:",
        "  transakcje: 2 (netto dodatnie 0, ujemne 2, zerowe 0)",
        "  brutto: -0.30",
        "  koszty: spread 0.03, poślizg (model) 0.00, finansowanie 0.00, opłaty 0.00",
        "  netto: -0.33",
    ]
    assert "  najgorszy dzień: 2026-11-03 (-0.30)" in lines
    assert lines[-1] == STATEMENT
    assert out.count(STATEMENT) == 1
    for forbidden in ("zysk", "przewag", "wygr", "sukces"):
        for line in lines:
            if line != STATEMENT:
                assert forbidden not in line.lower(), line  # żadnych twierdzeń o opłacalności poza zdaniem pomiarowym


def test_report_exposes_the_ledger_vs_counter_mismatch(journal, capsys, config_env, jpath):
    book_session(journal, D1, net_pnl=Decimal("-0.03"))
    journal.add_realized_pnl(D1, Decimal("0.10"))  # licznik rozjechany z księgą (np. ręczna ingerencja)
    _, out, _ = run(["--journal", jpath], capsys)
    assert "NIEZGODNE" in out


CAVEAT = ("  uwaga: koszty w EUR są zaokrąglane do 0,01 € przy wielkości 0,001, więc ratio w EUR jest zgrubne; "
          "poślizg stopu przy wyjściu NIE jest mierzony")  # fmt: skip
INSTRUCTION = "Aby dopuścić wielkość według reguł utwórz plik data/L1_PASSED (kod go nigdy nie tworzy)."
VERDICT_TAIL = "(cały dziennik; ocena wyjaśnień należy do właściciela)"


RATIO_LABEL = "  koszty zmierzone / model (poślizg wejścia w punktach, bez zaokrągleń): "
COMBINED_LABEL = "    informacyjnie, nie wpływa na werdykt: spread + poślizg wejścia / model (spread + poślizg): "
THIN_AT_REQUIRED = "  uwaga: dowody nadal skąpe przy n = wymaganej liczbie sesji (5); to wciąż mało, by oceniać koszty"
THIN_BELOW = "  uwaga: dowody są skąpe: pomiar kosztów z {} transakcji przy wymaganych 5; to za mało, by oceniać koszty"
STOPS_LINE = "    zamknięcia stopem: 0 (spread i poślizg przy wyjściu stopem nie są mierzone)"
SPREAD_LINE = "    spread w chwili decyzji: średni 2.00 pkt, największy 2.00 pkt"


def test_report_l1_block_not_yet_with_four_sessions(journal, capsys, config_env, jpath):
    for day in (D1, D2, D3, D4):
        book_session(journal, day)
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    start = lines.index(f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}")
    assert lines[start : start + 15] == [
        f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}",
        "  sesje na wielkości minimalnej: 4 z 5 (brakuje 1)",
        "  rozbieżności pozycja/broker starsze niż 60 s: 0 (wymagane 0)",
        "  nieobsłużone odrzucenia: 0 (wymagane 0)",
        RATIO_LABEL + "0.00 (najwyżej 2; próbki pomiaru: 4 z 5)",
        "    średni niekorzystny poślizg wejścia: 0.00 pkt (model: 0.5 pkt; poprawa ceny liczona jako 0)",
        "    największy niekorzystny poślizg wejścia: 0.00 pkt",
        SPREAD_LINE,
        STOPS_LINE,
        "    informacyjnie, nie wpływa na werdykt: spread + poślizg wejścia / model (spread + poślizg): 0.80",
        "  koszty w EUR (informacyjnie): ratio 0.00",
        CAVEAT,
        "  uwaga: dowody są skąpe: pomiar kosztów z 4 transakcji przy wymaganych 5; to za mało, by oceniać koszty",
        "  powód: za mało sesji na wielkości minimalnej: 4 z 5 (brakuje 1)",
        "  powód: brak pomiaru kosztów: użytecznych próbek 4 z 5",
    ]
    assert lines[start + 15] == ""
    assert INSTRUCTION not in out


def test_report_l1_block_passes_with_five_clean_sessions_and_prints_the_instruction(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    start = lines.index(f"Bramka L1 (spec §11): PASS {VERDICT_TAIL}")
    assert lines[start : start + 14] == [
        f"Bramka L1 (spec §11): PASS {VERDICT_TAIL}",
        "  sesje na wielkości minimalnej: 5 z 5 (brakuje 0)",
        "  rozbieżności pozycja/broker starsze niż 60 s: 0 (wymagane 0)",
        "  nieobsłużone odrzucenia: 0 (wymagane 0)",
        RATIO_LABEL + "0.00 (najwyżej 2; próbki pomiaru: 5 z 5)",
        "    średni niekorzystny poślizg wejścia: 0.00 pkt (model: 0.5 pkt; poprawa ceny liczona jako 0)",
        "    największy niekorzystny poślizg wejścia: 0.00 pkt",
        SPREAD_LINE,
        STOPS_LINE,
        "    informacyjnie, nie wpływa na werdykt: spread + poślizg wejścia / model (spread + poślizg): 0.80",
        "  koszty w EUR (informacyjnie): ratio 0.00",
        CAVEAT,
        THIN_AT_REQUIRED,
        INSTRUCTION,
    ]
    # zastrzeżenie o zaokrągleniu stoi w bloku bramki NAD instrukcją o pliku data/L1_PASSED
    assert lines.index(CAVEAT) < lines.index(INSTRUCTION)
    assert not any(line.startswith("  powód:") for line in lines)
    assert not any("dowody są skąpe" in line for line in lines)  # to jest wariant dla n < wymagane


def test_report_gate_verdict_is_entry_slippage_not_the_spread_and_not_the_rounded_euro_ratio(
    journal, capsys, config_env, jpath
):
    # wypełnienia 1.1 pkt gorsze od ask (model 0.5): 1.1 / 0.5 = 2.2; księga w EUR pokazuje 0.00
    for day in (D1, D2, D3, D4, D5):
        book_session(journal, day, decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                     entry_fill=Decimal("103.1"))  # fmt: skip
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}" in lines
    assert RATIO_LABEL + "2.20 (najwyżej 2; próbki pomiaru: 5 z 5)" in lines
    assert "    średni niekorzystny poślizg wejścia: 1.10 pkt (model: 0.5 pkt; poprawa ceny liczona jako 0)" in lines
    assert "    największy niekorzystny poślizg wejścia: 1.10 pkt" in lines
    assert f"{COMBINED_LABEL}1.24" in lines
    assert "  koszty w EUR (informacyjnie): ratio 0.00" in lines
    assert "  powód: koszty zmierzone / model: 2.20 > 2 (przegląd kosztów)" in lines
    assert INSTRUCTION not in lines


def test_report_shows_the_number_of_stop_exits_as_unmeasured(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    book_session(journal, D6, exit_reason="STOP")
    _, out, _ = run(["--journal", jpath], capsys)
    assert "    zamknięcia stopem: 1 (spread i poślizg przy wyjściu stopem nie są mierzone)" in out.splitlines()


@pytest.mark.parametrize(
    "spoil, expected",
    [
        ("mismatch", "  rozbieżności pozycja/broker starsze niż 60 s: 1 (wymagane 0; dni: 2026-11-06)"),
        ("rejection", "  nieobsłużone odrzucenia: 1 (wymagane 0; dni: 2026-11-06)"),
    ],
)
def test_report_l1_each_other_condition_blocks_the_gate(journal, capsys, config_env, spoil, expected, jpath):
    five_clean_sessions(journal)
    if spoil == "mismatch":
        emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D5, 9, 0)}, at(D5, 9, 2))
    else:
        emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261106-DE40-2-entry"}, at(D5, 8, 8))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}" in lines
    assert expected in lines
    assert INSTRUCTION not in lines


def test_report_explains_what_the_unhandled_rejections_count_includes(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261106-DE40-2-entry"}, at(D5, 8, 8))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    note = ("    liczy ENTRY_UNRESOLVED także po późniejszym rozstrzygnięciu zlecenia oraz zlecenia INTENT/SUBMITTING, "
            "gdy raport powstaje w trakcie wejścia")  # fmt: skip
    assert lines[lines.index("  nieobsłużone odrzucenia: 1 (wymagane 0; dni: 2026-11-06)") + 1] == note


EXCLUDED = (
    "  wykluczone z pomiaru kosztów i z liczby sesji "
    "(brak kontekstu wejścia, uszkodzona wartość punktu albo nieużyteczne notowanie decyzji): "
)


def test_report_counts_exactly_the_excluded_trades(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    book_session(journal, D1, seq=2, ctx=False, **later_times(D1, 10))  # bez kontekstu
    book_session(journal, D2, seq=2, ctx=False, **later_times(D2, 10))  # bez kontekstu
    book_session(journal, D3, seq=2, vpp="0", **later_times(D3, 10))  # kontekst z uszkodzoną wartością punktu
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert EXCLUDED + "3" in lines
    assert sum(1 for line in lines if line.startswith("  wykluczone z pomiaru")) == 1
    assert "  sesje na wielkości minimalnej: 5 z 5 (brakuje 0)" in lines  # wykluczone nie są sesjami
    assert RATIO_LABEL + "0.00 (najwyżej 2; próbki pomiaru: 5 z 5)" in lines  # ani próbkami kosztów
    assert not any("wartość punktu = 1" in line for line in lines)  # żadnego cichego zapasowego 1


def test_report_omits_the_excluded_line_when_every_trade_has_a_context(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    _, out, _ = run(["--journal", jpath], capsys)
    assert "wykluczone z pomiaru" not in out


def test_report_survives_corrupted_value_per_point_in_the_entry_context(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    trade = book_session(journal, D6, vpp=None)
    client_id = make_client_id(D6, "DE40", 1, "entry")
    for index, bad in enumerate(["1E+999999", "-1", "NaN", "9" * 400, 1e400, True]):
        context = {"client_id": client_id, "signal_ts": trade.signal_ts.isoformat(), "value_per_point": bad,
                   "submit_ts": trade.submit_ts.isoformat()}  # fmt: skip
        journal.set_meta(f"entry_ctx:{client_id}", json.dumps(context))
        code, out, err = run(["--journal", jpath], capsys)
        assert code == 0 and err == "", (index, bad)
        assert EXCLUDED + "1" in out.splitlines()
        assert "Bramka L1 (spec §11): PASS" in out  # pięć czystych sesji; uszkodzony wiersz tylko wykluczony


def test_report_says_plainly_that_the_evidence_is_thin_below_and_at_the_required_n(journal, capsys, config_env, jpath):
    for day in (D1, D2):
        book_session(journal, day)
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert THIN_BELOW.format(2) in lines and THIN_AT_REQUIRED not in lines
    for day in (D3, D4):
        book_session(journal, day)
    _, out, _ = run(["--journal", jpath], capsys)
    assert THIN_BELOW.format(4) in out.splitlines()
    book_session(journal, D5)  # dokładnie tyle próbek, ile wymaganych sesji: nadal skąpo
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert THIN_AT_REQUIRED in lines and not any(line.startswith("  uwaga: dowody są skąpe") for line in lines)
    book_session(journal, D6)  # więcej próbek niż wymaganych: bez ostrzeżenia
    _, out, _ = run(["--journal", jpath], capsys)
    assert "dowody" not in out


def test_report_on_an_empty_journal(journal, capsys, config_env, jpath):
    code, out, err = run(["--journal", jpath], capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert "Dziennik jest pusty: brak transakcji i zapisów dnia." in lines
    assert f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}" in lines
    assert "  sesje na wielkości minimalnej: 0 z 5 (brakuje 5)" in lines
    assert RATIO_LABEL + "brak danych (najwyżej 2; próbki pomiaru: 0 z 5)" in lines
    assert "  powód: brak pomiaru kosztów: użytecznych próbek 0 z 5" in lines
    assert "  koszty w EUR (informacyjnie): brak danych" in lines  # nie "ratio 0.00": zero próbek to nie dowód
    assert lines[-1] == STATEMENT
    assert "Dzień: " not in out


def test_report_for_a_day_without_records(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    code, out, _ = run(["--journal", jpath, "--day", "2026-12-01"], capsys)
    assert code == 0
    assert "Dzień: 2026-12-01 (czas Europe/Berlin, dzień zamknięcia transakcji)" in out
    assert "Tryb: brak zapisu decyzji" in out
    lines = out.splitlines()
    assert "Transakcje: 0" in lines and "Netto: 0.00" in lines and "Brutto: 0.00" in lines
    assert "Wynik zrealizowany wg dziennika: dzień 0.00, łącznie od początku -0.02" in lines


def test_report_mode_of_the_day_comes_from_the_decision_events(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-02", "mode": "live"}, at(D1, 8, 5))
    emit(journal, "decision", {"event": "DECISION_CANDLES", "day": "2026-11-02"}, at(D1, 8, 11))  # bez trybu
    emit(journal, "decision", {"request": {}, "candidate": {}, "day": "2026-11-02"}, at(D1, 8, 5))  # wpis dry_run
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert "Tryb: live" in out.splitlines()
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-02", "mode": "dry_run"}, at(D1, 8, 6))
    _, mixed, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert "Tryb: live, dry_run" in mixed.splitlines()


def test_report_counts_kills_by_event_level_and_reason_per_day(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    for ts in (at(D1, 8, 20), at(D1, 8, 30)):
        emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "STALE_QUOTE"}, ts)
    emit(journal, "kill.audit", {"event": "ESCALATE", "level": "HARD", "reason": "UNPROTECTED_POSITION"}, at(D1, 9, 0))
    emit(journal, "kill.audit", {"event": "CLEAR", "level": "NONE", "reason": ""}, at(D1, 10, 0))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "OTHER_DAY"}, at(D2, 8, 20))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert ("  kill switch: CLEAR NONE x1; ENGAGE SOFT STALE_QUOTE x2; ESCALATE HARD UNPROTECTED_POSITION x1"
            in out.splitlines())  # fmt: skip
    assert "OTHER_DAY" not in out


def test_report_lists_other_events_and_warns_about_unreadable_rows(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    emit(journal, "event", {"event": "COST_FALLBACK", "deal_id": "DEAL-SECRET"}, at(D1, 16, 26))
    emit(journal, "event", {"event": "TRADE_BOOKED", "deal_id": "DEAL-SECRET"}, at(D1, 16, 26))
    emit(journal, "reconcile.event", {"event": "CLOSURE_FOUND"}, at(D1, 16, 27))
    journal.append_raw("event", "{broken", at(D1, 16, 28))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "  inne zdarzenia: CLOSURE_FOUND x1, COST_FALLBACK x1" in lines  # TRADE_BOOKED to nie błąd
    assert "UWAGA: 1 nieczytelnych zapisów zdarzeń pominięto w liczniku." in lines


def test_report_never_prints_ids_or_secrets(journal, capsys, config_env, jpath):
    book_session(journal, D1)
    secret_payload = {"event": "COST_FALLBACK", "deal_id": "DEAL-SECRET-1", "accountId": "ACC-SECRET-2",
                      "order_id": "gws-20261102-DE40-1-entry", "message": "token CST-SECRET-3"}  # fmt: skip
    emit(journal, "event", secret_payload, at(D1, 16, 26))
    emit(journal, "event", {**secret_payload, "event": "ENTRY_UNRESOLVED"}, at(D1, 16, 27))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "STALE_QUOTE", "extra": "ACC-SECRET-2"},
         at(D1, 16, 28))  # fmt: skip
    emit(journal, "watchdog", {"event": "LOGIN_LOCKOUT", "detail": "CST-SECRET-3"}, at(D1, 16, 29))
    emit(journal, "broker.account", {"response": {"accountId": "ACC-SECRET-2", "balance": "40.00"}}, at(D1, 7, 0))
    _, out, err = run(["--journal", jpath, "--all"], capsys)
    for text in (out, err):
        for secret in ("DEAL-", "ACC-SECRET", "CST-SECRET", "gws-2026", "40.00"):
            assert secret not in text


def test_report_csv_option_exports_the_ledger(journal, capsys, config_env, tmp_path, jpath):
    book_session(journal, D1)
    book_session(journal, D2)
    target = tmp_path / "out.csv"
    code, out, _ = run(["--journal", jpath, "--csv", str(target)], capsys)
    assert code == 0
    assert f"CSV: {target} (wiersze danych: 2)" in out.splitlines()
    assert target.read_text(encoding="utf-8").splitlines()[0] == ",".join(SPEC_HEADER)
    assert len(target.read_text(encoding="utf-8").splitlines()) == 3


def test_report_csv_failure_is_exit_1_and_the_rest_is_still_printed(journal, capsys, config_env, tmp_path, jpath):
    book_session(journal, D1)
    code, out, err = run(["--journal", jpath, "--csv", str(tmp_path)], capsys)  # katalog
    assert code == 1 and "CSV" in err
    assert "Bramka L1" in out


def test_report_exit_codes_and_usage_errors(journal, capsys, config_env, tmp_path, jpath):
    path = jpath
    assert run(["--journal", str(tmp_path / "nope.sqlite")], capsys)[0] == 1
    code, out, err = run(["--journal", str(tmp_path / "nope.sqlite")], capsys)
    assert out == "" and "nope.sqlite" in err
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"this is not a database" * 100)
    assert run(["--journal", str(garbage)], capsys)[0] == 1
    for argv in (["--journal", path, "--day", "2026/11/02"], ["--journal", path, "--day", "x"],
                 ["--journal", path, "--day", "2026-11-02", "--all"], ["--bogus"]):  # fmt: skip
        with pytest.raises(SystemExit) as excinfo:
            report_cli.main(argv)
        assert excinfo.value.code == 2
        capsys.readouterr()


def test_report_without_usable_configuration_is_exit_1(journal, capsys, monkeypatch, tmp_path, jpath):
    monkeypatch.setenv("GW_CONFIG_DIR", str(tmp_path / "no-config"))
    code, out, err = run(["--journal", jpath], capsys)
    assert code == 1 and "configuration" in err and out == ""


def test_report_reads_the_journal_without_changing_it(journal, capsys, config_env, tmp_path, jpath):
    five_clean_sessions(journal)
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-06", "mode": "dry_run"}, at(D5, 8, 5))
    db = Path(jpath)

    def snapshot():
        # indeks pamięci współdzielonej (-shm) zmienia się przy każdym odczycie; trwały stan to baza i -wal
        return {
            p.name: (None if p.name.endswith("-shm") else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in db.parent.iterdir()
        }

    before = snapshot()
    code, _, _ = run(["--journal", str(db), "--all"], capsys)
    assert code == 0
    assert snapshot() == before  # ani bajt pliku bazy, ani -wal, ani nowy plik


def test_report_works_on_a_closed_journal_without_leaving_files(tmp_path, capsys, config_env):
    path = tmp_path / "closed" / "journal.sqlite"
    path.parent.mkdir()
    j = Journal.open(str(path))
    book_session(j, D1)
    j.close()
    before = sorted(p.name for p in path.parent.iterdir())
    code, out, _ = run(["--journal", str(path)], capsys)
    assert code == 0 and "Transakcje: 1" in out
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_report_refuses_a_database_that_is_not_a_journal_of_this_schema(capsys, config_env, tmp_path):
    import sqlite3

    other = tmp_path / "other.sqlite"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE x (a)")
    conn.commit()
    conn.close()  # user_version 0: obca baza, nie dziennik
    newer = tmp_path / "newer.sqlite"
    conn = sqlite3.connect(newer)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    for path in (other, newer):
        code, out, err = run(["--journal", str(path)], capsys)
        assert code == 1 and out == "" and "schema version" in err
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(path.name)] == [path.name]  # bez śladów


def test_format_ratio_rounds_half_up_to_two_places_and_names_infinity():
    assert gates.format_ratio(Decimal("1.4")) == "1.40"
    assert gates.format_ratio(Decimal("2.005")) == "2.01"
    assert gates.format_ratio(Decimal("0")) == "0.00"
    assert gates.format_ratio(Decimal("Infinity")) == "inf"
    assert gates.format_ratio(Decimal("-0.001")) == "0.00"  # nigdy "-0.00"


@pytest.mark.parametrize("value", ["-0.001", "-0", "-0.0", "-0.004", "0", "0.001"])
def test_points_are_never_printed_as_negative_zero(value):
    assert report_cli._points(Decimal(value)) == "0.00"
    assert report_cli._points(Decimal("-0.006")) == "-0.01"  # prawdziwa wartość ujemna zostaje
    assert report_cli._points(Decimal("1.234")) == "1.23" and report_cli._points(None) == "-"


def test_report_has_no_data_lines_when_no_trade_is_a_cost_sample(journal, capsys, config_env, jpath, monkeypatch):
    zero = CostConfig(Decimal("0"), Decimal("0"), Decimal("0"), False)
    monkeypatch.setattr(report_cli, "load_config", lambda _dir: (load_config(REPO / "config")[0], None, zero))
    book_session(journal, D1, spread_cost=Decimal("0.01"), decision_bid=Decimal("100"), decision_ask=Decimal("100"))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "  koszty w EUR (informacyjnie): brak danych" in lines  # zablokowane notowanie: żadnej liczby
    assert RATIO_LABEL + "brak danych (najwyżej 2; próbki pomiaru: 0 z 5)" in lines
    assert EXCLUDED + "1" in lines


def test_report_cannot_write_to_the_journal(journal, jpath):
    reader = report_cli.open_journal_readonly(Path(jpath))
    try:
        with pytest.raises(Exception):
            reader.set_meta("x", "y")
        assert reader.get_meta("x") is None
    finally:
        reader.close()


def test_default_journal_is_the_engine_journal_in_the_data_dir(capsys, config_env, monkeypatch, tmp_path):
    from gwstock import cli  # ta sama nazwa pliku co w silniku

    assert report_cli.JOURNAL_FILE == cli.JOURNAL_FILE
    data = tmp_path / "enginedata"
    data.mkdir()
    engine_journal = Journal.open(str(data / cli.JOURNAL_FILE))
    book_session(engine_journal, D1)
    engine_journal.close()
    monkeypatch.setenv("GW_DATA_DIR", str(data))
    code, out, _ = run([], capsys)
    assert code == 0 and "Dzień: 2026-11-02" in out
    assert f"Dziennik: {data / cli.JOURNAL_FILE}" in out.splitlines()


def test_literals_match_the_engine_constants():
    from gwstock import execution, reconcile, watchdog

    assert gates.META_ENTRY_CTX == execution.META_ENTRY_CTX
    assert gates.ROLE_ENTRY == execution.ROLE_ENTRY and gates.ROLE_SMOKE == execution.ROLE_SMOKE
    assert gates.ENTRY_UNRESOLVED == execution.ENTRY_UNRESOLVED
    assert gates.ENTRY_EXCEPTION == execution.ENTRY_EXCEPTION
    assert gates.SOURCE_EVENT == execution.SOURCE_EVENT and gates.SOURCE_GATE_BLOCK == execution.SOURCE_GATE_BLOCK
    assert gates.SOURCE_RECONCILE == reconcile.SOURCE_EVENT
    assert gates.SOURCE_WATCHDOG == watchdog.SOURCE_WATCHDOG
    assert gates.MISMATCH_TIMEOUT == "MISMATCH_TIMEOUT"
    assert gates.SOURCE_DECISION == execution.SOURCE_DECISION


def test_test_costs_equal_the_shipped_cost_configuration():
    assert load_config(REPO / "config")[2] == COSTS


def test_python_dash_m_gwstock_report_runs(journal, tmp_path, jpath):
    book_session(journal, D1)
    env = {**os.environ, "GW_CONFIG_DIR": str(REPO / "config"), "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "gwstock.report", "--journal", jpath],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "Dzień: 2026-11-02" in result.stdout and STATEMENT in result.stdout


# --- zero ze znakiem -----------------------------------------------------------------------------------------------


def test_csv_negative_zero_is_written_as_plain_zero(journal, tmp_path):
    book_session(journal, D1, gross_pnl=Decimal("-0.00"), fees=Decimal("-0E-4"), net_pnl=Decimal("-0.02"))
    path = tmp_path / "ledger.csv"
    export_csv(journal, path)
    row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert row[7] == "0.00" and row[11] == "0.0000" and row[12] == "-0.02"
    assert "-0.00" not in path.read_text(encoding="utf-8")


def test_report_negative_zero_is_printed_as_zero(journal, capsys, config_env, jpath):
    book_session(journal, D1, fees=Decimal("-0.00"), financing=Decimal("-0.00"))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "Opłaty: 0.00" in lines and "Finansowanie: 0.00" in lines
    assert "-0.00" not in out


def test_an_arithmetic_error_while_building_the_report_is_exit_1_not_a_traceback(
    journal, capsys, config_env, jpath, monkeypatch
):
    def exploding(*args, **kwargs):
        raise ArithmeticError("value out of range")

    monkeypatch.setattr(gates, "l1_details", exploding)
    code, out, err = run(["--journal", jpath], capsys)
    assert code == 1 and out == "" and "value out of range" in err


# --- fix round 3: odd records never take the report down ------------------------------------------------------


@pytest.mark.parametrize("bad", ["1E-30", "1E-6", "0.0009", "1E-999999"])
def test_a_tiny_value_per_point_is_excluded_and_the_report_still_prints_everything(
    journal, capsys, config_env, jpath, bad
):
    # powtórka z przeglądu: wartość punktu 1E-30 przepuszczona dzieliła koszt EUR przez ~0 i kładła cały raport
    trade = book_session(journal, D1, vpp=None, spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.01"))
    client_id = make_client_id(D1, "DE40", 1, "entry")
    context = {"client_id": client_id, "signal_ts": trade.signal_ts.isoformat(), "value_per_point": bad,
               "submit_ts": trade.submit_ts.isoformat()}  # fmt: skip
    journal.set_meta(f"entry_ctx:{client_id}", json.dumps(context))
    code, out, err = run(["--journal", jpath, "--all"], capsys)
    lines = out.splitlines()
    assert code == 0 and err == ""
    assert any(line.startswith("Dzień: 2026-11-02") for line in lines)  # wynik dnia jest
    assert any(line.startswith("Bramka L1") for line in lines)  # blok L1 jest
    assert EXCLUDED + "1" in lines
    assert "  koszty w EUR (informacyjnie): brak danych" in lines  # żadnej liczby z uszkodzonego rekordu
    assert "ratio 8000000" not in out


@pytest.mark.parametrize("fill", ["25782.30", "1E+3", "1E+27", "1E+60"])
def test_an_absurd_fill_still_prints_the_daily_result_and_the_gate_block(journal, capsys, config_env, jpath, fill):
    # wypełnienie absurdalnie dalekie od notowania: stosunek jest ogromny, a raport nadal powstaje
    book_session(journal, D1, entry_fill=Decimal(fill))
    code, out, err = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert code == 0 and err == ""
    assert any(line.startswith("Dzień: 2026-11-02") for line in lines)
    assert f"Bramka L1 (spec §11): NOT YET {VERDICT_TAIL}" in lines
    assert STATEMENT == lines[-1]
    if fill in ("1E+27", "1E+60"):  # poza zakresem wydruku: "n/a", nie absurdalna liczba
        verdict = next(line for line in lines if line.startswith(RATIO_LABEL))
        assert verdict.startswith(RATIO_LABEL + "n/a ")
        assert "  powód: koszty zmierzone / model: n/a > 2 (przegląd kosztów)" in lines


def test_format_ratio_and_points_never_raise_and_print_n_a_when_out_of_range():
    for huge in (Decimal("1E+30"), Decimal("1E+999999"), Decimal("NaN")):
        assert gates.format_ratio(huge) == "n/a"
        assert report_cli._points(huge) == "n/a"
    assert gates.format_ratio(Decimal("Infinity")) == "inf"  # nieskończoność ma własny napis
    assert report_cli._points(Decimal("Infinity")) == "n/a"
    assert gates.format_ratio(Decimal("1E+20")) == "100000000000000000000.00"  # w zakresie: liczba, nie "n/a"


def test_points_use_the_same_rounding_as_ratios():
    assert report_cli._points(Decimal("1.005")) == "1.01"  # w górę (nie parzysta): jak format_ratio
    assert gates.format_ratio(Decimal("1.005")) == "1.01"
    assert report_cli._points(Decimal("2.015")) == "2.02" and gates.format_ratio(Decimal("2.015")) == "2.02"


def test_a_printed_ratio_never_contradicts_the_limit_it_is_compared_with():
    limit = Decimal("2")
    assert gates.format_ratio(Decimal("2"), limit=limit) == "2.00"
    assert gates.format_ratio(Decimal("1.996"), limit=limit) == "2.00"  # nie przekracza limitu: "2.00" jest spójne
    assert gates.format_ratio(Decimal("2.002"), limit=limit) == "2.002"  # przekracza: widać to w druku
    assert gates.format_ratio(Decimal("2.004"), limit=limit) == "2.004"
    assert gates.format_ratio(Decimal("2.0004"), limit=limit) == "2.0004"  # kolejne miejsca, aż widać przekroczenie
    assert gates.format_ratio(Decimal("2.00004"), limit=limit) == "2.00004"
    assert gates.format_ratio(Decimal("2.0000000000004"), limit=limit) == "2.000000000001"  # w górę na 12. miejscu
    assert gates.format_ratio(Decimal("2.01"), limit=limit) == "2.01"
    assert gates.format_ratio(Decimal("1.4"), limit=limit) == "1.40"
    reasons = GateL1(5, 0, 0, Decimal("2.002"), cost_samples=5).reasons(5)
    assert reasons == ["koszty zmierzone / model: 2.002 > 2 (przegląd kosztów)"]


def test_report_near_the_limit_prints_a_ratio_that_visibly_exceeds_it(journal, capsys, config_env, jpath):
    # poślizg 1.001 pkt przy modelu 0.5: 2.002 > 2; wcześniej druk mówił "2.00 > 2"
    for day in (D1, D2, D3, D4, D5):
        book_session(journal, day, decision_bid=Decimal("100"), decision_ask=Decimal("102"),
                     entry_fill=Decimal("103.001"))  # fmt: skip
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert RATIO_LABEL + "2.002 (najwyżej 2; próbki pomiaru: 5 z 5)" in lines
    assert "  powód: koszty zmierzone / model: 2.002 > 2 (przegląd kosztów)" in lines


# --- fix round 3: roles, mask, JSON depth ---------------------------------------------------------------------------


def test_a_trade_with_a_context_but_no_usable_quote_is_excluded_with_its_own_role(journal):
    book_session(journal, D1)
    book_session(journal, D2, decision_bid=Decimal("100"), decision_ask=Decimal("100"), entry_fill=Decimal("100"))
    book_session(journal, D3, role="smoke", decision_bid=Decimal("100"), decision_ask=Decimal("99"),
                 entry_fill=Decimal("100"))  # fmt: skip
    assert gates.trade_roles(journal, journal.trades()) == ["entry", "entry", "smoke"]  # rola z kontekstu
    details = gates.l1_details(journal, costs=COSTS)
    assert details.roles == ("entry", "no_quote", "no_quote")  # zablokowane i odwrócone notowanie
    assert details.excluded_trades == 2
    assert details.gate.sessions_at_min_size == 1 and details.gate.cost_samples == 1


def test_report_counts_trades_without_a_usable_quote_in_the_excluded_line(journal, capsys, config_env, jpath):
    five_clean_sessions(journal)
    book_session(journal, D1, seq=2, decision_bid=Decimal("100"), decision_ask=Decimal("100"),
                 entry_fill=Decimal("100"), **later_times(D1, 10))  # fmt: skip
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert EXCLUDED + "1" in lines
    assert RATIO_LABEL + "0.00 (najwyżej 2; próbki pomiaru: 5 z 5)" in lines
    assert "  sesje na wielkości minimalnej: 5 z 5 (brakuje 0)" in lines


def test_the_include_mask_must_match_the_trades():
    trades = [make_trade(D1), make_trade(D2)]
    for mask in ([True], [True, True, True], []):
        with pytest.raises(ValueError, match="include"):
            gates.cost_points(trades, COSTS, include=mask)
    assert gates.cost_points(trades, COSTS, include=[True, False]).trades == 1
    assert gates.cost_points([], COSTS, include=[]).trades == 0


def test_deeply_nested_json_is_an_unusable_context_not_a_crash(journal):
    assert gates._parse_context("[" * 200000) is None
    assert gates._parse_context('{"a":' * 100000) is None
    trade = book_session(journal, D1, ctx=False)
    client_id = make_client_id(D1, "DE40", 1, "entry")
    journal.set_meta(f"entry_ctx:{client_id}", "[" * 200000)
    assert gates.trade_roles(journal, [trade]) == ["unknown"]


def test_deeply_nested_event_payloads_are_unreadable_rows_not_a_crash(journal, capsys, config_env, jpath):
    journal.append_raw("event", "[" * 200000, at(D1, 9, 0))
    assert gates.read_events(journal, "event")[0][1] is None
    book_session(journal, D1)
    code, out, err = run(["--journal", jpath], capsys)
    assert code == 0 and err == ""
    assert "UWAGA: 1 nieczytelnych zapisów zdarzeń pominięto w liczniku." in out.splitlines()


def test_stale_docstrings_describe_the_current_behaviour():
    assert "invalid" in gates.trade_roles.__doc__ and "no_quote" in gates.l1_details.__doc__
    assert "pięć wielkości" in gates.__doc__ and "Definicje czterech liczb (" not in gates.__doc__
    assert "wartości poza zakresem" in report_cli.__doc__  # kod wyjścia 1 także dla arytmetyki
