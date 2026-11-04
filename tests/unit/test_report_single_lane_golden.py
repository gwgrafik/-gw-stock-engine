"""Raport jednego pasma (DE40) jest IDENTYCZNY bajt w bajt jak przed raportem per pasmo (zadanie 15, kryterium R2).

Wzorce w `tests/unit/report_golden/*.txt` nagrano z kodu raportu PRZED zmianą na pasma, na dziennikach zbudowanych
poniżej (tylko instrument DE40, w walucie rachunku). Test nigdy nie nagrywa wzorców sam: zmiana wzorca to świadoma,
ręczna edycja pliku, a nie efekt uboczny uruchomienia testów.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.journal import Journal, OrderRecord
from gwstock.orders.ids import make_client_id
from gwstock.report import cli as report_cli
from gwstock.types import OrderState, Side
from tests.unit.test_report import (
    D1, D2, D3, D4, D5, D6, at, book_estimate_with_a_matching_context, book_session, emit, five_clean_sessions,
    later_times, make_trade, smoke_times,
)  # fmt: skip

GOLDEN = Path(__file__).parent / "report_golden"
JOURNAL_MARK = "<JOURNAL>"
CSV_MARK = "<CSV>"


def build_history(journal: Journal) -> None:
    """Sześć dni DE40: sesje, próba smoke, wyjście stopem, dwa wejścia w dniu, szacunek właściciela, transakcje bez
    kontekstu, z uszkodzoną wartością punktu i bez użytecznego notowania, plus zdarzenia wszystkich rodzajów."""
    book_session(journal, D1, gross_pnl=Decimal("-0.02"), spread_cost=Decimal("0.01"), net_pnl=Decimal("-0.03"))
    book_session(journal, D1, role="smoke", seq=2, **smoke_times(D1))
    book_session(journal, D2, gross_pnl=Decimal("0.05"), spread_cost=Decimal("0.01"), slippage_penalty=Decimal("0.01"),
                 net_pnl=Decimal("0.03"))  # fmt: skip
    book_session(journal, D3, exit_reason="STOP", gross_pnl=Decimal("-0.31"), spread_cost=Decimal("0.02"),
                 slippage_penalty=Decimal("0.02"), net_pnl=Decimal("-0.35"), entry_fill=Decimal("25783.30"))  # fmt: skip
    book_session(journal, D4, seq=1)
    book_session(journal, D4, seq=2, size=Decimal("0.002"), side=Side.SELL, net_pnl=Decimal("0.07"),
                 gross_pnl=Decimal("0.09"), spread_cost=Decimal("0.02"), entry_fill=Decimal("25780.30"),
                 **later_times(D4, 10))  # fmt: skip
    book_estimate_with_a_matching_context(journal, D5)
    book_session(journal, D6, ctx=False, net_pnl=Decimal("-0.04"))
    book_session(journal, D6, seq=2, vpp=None, net_pnl=Decimal("-0.05"), **later_times(D6, 9))
    book_session(journal, D6, seq=3, decision_bid=Decimal("25781.30"), decision_ask=Decimal("25781.30"),
                 net_pnl=Decimal("0.01"), **later_times(D6, 11))  # fmt: skip
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-03", "mode": "dry_run", "outcome": "SIGNAL"},
         at(D2, 8, 5))  # fmt: skip
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-04", "mode": "live", "outcome": "SKIP"},
         at(D3, 8, 5))  # fmt: skip
    emit(journal, "decision", {"event": "DECISION_CANDLES", "day": "2026-11-04"}, at(D3, 8, 11))
    emit(journal, "gate_block", {"reasons": ["ENTRIES_PER_DAY", "MARGIN"], "day": "2026-11-03"}, at(D2, 8, 6))
    emit(journal, "gate_block", {"reasons": ["MARGIN"], "day": "2026-11-03"}, at(D2, 8, 7))
    emit(journal, "gate_block", {"reasons": ["KILL"], "day": "2026-11-03", "smoke": True}, at(D2, 8, 7))
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": "gws-20261103-DE40-1-entry"}, at(D2, 8, 8))
    emit(journal, "event", {"event": "COST_FALLBACK", "deal_id": "DEAL-SECRET"}, at(D3, 16, 26))
    emit(journal, "event", {"event": "TRADE_BOOKED", "deal_id": "DEAL-SECRET"}, at(D3, 16, 26))
    emit(journal, "reconcile.event", {"event": "CLOSURE_FOUND"}, at(D3, 16, 27))
    emit(journal, "reconcile.event", {"event": "CLOSURE_BOOKED", "deal_id": "DEAL-X"}, at(D3, 16, 27))
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D3, 8, 10)}, at(D3, 8, 20))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "ENTRY_UNRESOLVED"}, at(D2, 8, 8))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "MISMATCH_TIMEOUT"}, at(D3, 8, 20))
    emit(journal, "kill.audit", {"event": "CLEAR", "level": "NONE", "reason": ""}, at(D3, 10, 0))
    emit(journal, "watchdog", {"event": "LOGIN_LOCKOUT", "failures": 3}, at(D2, 9, 0))
    journal.append_raw("event", "{broken", at(D3, 16, 28))
    journal.insert_order(OrderRecord(make_client_id(D6, "DE40", 9, "entry"), "DE40", Side.BUY, Decimal("0.001"),
                                     Decimal("30"), OrderState.UNKNOWN, "r", None, at(D6, 8, 5), at(D6, 8, 6)))  # fmt: skip
    journal.add_realized_pnl(D2, Decimal("0.10"))  # licznik rozjechany z księgą: wiersz NIEZGODNE


def build_two_clean_days(journal: Journal) -> None:
    for day in (D1, D2):
        book_session(journal, day)


def build_four_sessions(journal: Journal) -> None:
    for day in (D1, D2, D3, D4):
        book_session(journal, day)


def build_five_sessions(journal: Journal) -> None:
    five_clean_sessions(journal)


def build_empty(journal: Journal) -> None:
    return None


def build_only_problems(journal: Journal) -> None:
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "STALE_QUOTE"}, at(D2, 8, 20))
    emit(journal, "event", {"event": "COST_FALLBACK"}, at(D2, 8, 21))


SCENARIOS = {
    "history_all": (build_history, ["--all"]),
    "history_default": (build_history, []),
    "history_day_with_trades": (build_history, ["--day", "2026-11-03"]),
    "history_day_without_records": (build_history, ["--day", "2026-11-20"]),
    "history_csv": (build_history, ["--day", "2026-11-04", "--csv", CSV_MARK]),
    "two_clean_days_all": (build_two_clean_days, ["--all"]),
    "four_sessions": (build_four_sessions, []),
    "five_sessions": (build_five_sessions, ["--all"]),
    "empty": (build_empty, []),
    "only_problems": (build_only_problems, []),
}


@pytest.fixture
def config_env(monkeypatch):
    monkeypatch.setenv("GW_CONFIG_DIR", str(Path(__file__).resolve().parents[2] / "config"))


def render(name: str, journal: Journal, tmp_path: Path, capsys) -> str:
    build, argv = SCENARIOS[name]
    build(journal)
    jpath = tmp_path / "journal.sqlite"
    csv_path = tmp_path / "ledger.csv"
    argv = [str(csv_path) if arg == CSV_MARK else arg for arg in argv]
    code = report_cli.main(["--journal", str(jpath), *argv])
    captured = capsys.readouterr()
    assert code == 0 and captured.err == ""
    return captured.out.replace(str(jpath), JOURNAL_MARK).replace(str(csv_path), CSV_MARK)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_single_lane_report_is_byte_identical_to_the_recorded_pattern(name, journal, tmp_path, capsys, config_env):
    produced = render(name, journal, tmp_path, capsys)
    expected = (GOLDEN / f"{name}.txt").read_bytes().decode("utf-8")
    assert produced == expected


def test_every_pattern_has_a_scenario():
    assert sorted(path.stem for path in GOLDEN.glob("*.txt")) == sorted(SCENARIOS)
    assert make_trade(D1).instrument == "DE40"  # wzorce dotyczą jednego pasma w walucie rachunku
