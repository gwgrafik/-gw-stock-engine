"""Księga z walutą (zadanie 15): wiersz instrumentu w USD ma `net_pnl` w USD, kwota EUR jest w `Journal.trade_fx`;
sumy w EUR nigdy nie mieszają walut, a transakcja bez rozliczenia jest oczekująca."""

import csv
from decimal import Decimal

import pytest

from gwstock.journal import JournalCorruptError
from gwstock.report import ledger
from tests.unit.report_lane_support import US, book_lane, book_usd
from tests.unit.test_report import D1, D2


def test_the_ledger_tells_account_currency_pending_and_settled_rows_apart(journal):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    book_usd(journal, D1, seq=1, settle="5.90")
    book_usd(journal, D2, seq=2, net_pnl=Decimal("-2.50"), exit_reason="STOP")
    rows = ledger.read_ledger(journal)
    assert [(r.trade.instrument, r.foreign, r.pending) for r in rows] == [
        ("DE40", False, False), (US, True, False), (US, True, True),
    ]  # fmt: skip
    assert [r.net_eur for r in rows] == [Decimal("-0.03"), Decimal("5.90"), None]
    assert [r.trade.net_pnl for r in rows] == [Decimal("-0.03"), Decimal("6.60"), Decimal("-2.50")]
    assert rows[1].fx is not None and rows[1].fx.source == "HISTORY_TRANSACTIONS" and rows[0].fx is None


def test_the_eur_summary_never_adds_instrument_currency_amounts(journal):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    book_usd(journal, D1, seq=1, settle="5.90")  # 6,60 USD = 5,90 EUR
    book_usd(journal, D2, seq=2, net_pnl=Decimal("-2.50"), exit_reason="STOP")  # czeka: kwota EUR nieznana
    summary = ledger.eur_summary(ledger.read_ledger(journal))
    assert summary.net == Decimal("5.87") and summary.account_net == Decimal("-0.03")
    assert summary.settled_net == Decimal("5.90") and summary.settled == 1 and summary.pending == 1
    assert summary.per_day == {D1: Decimal("5.87")}  # dzień z samą transakcją oczekującą nie ma kwoty w EUR
    assert summary.worst_day == (D1, Decimal("5.87"))
    assert ledger.eur_summary([]).net == Decimal(0) and ledger.eur_summary([]).worst_day is None


def test_an_unreadable_settlement_is_pending_and_flagged_not_a_crash(journal):
    book_usd(journal, D1, seq=1, settle="5.90")
    journal.set_meta("trade_fx:" + "DEAL-US500-20261102-1-entry", "{not json")
    with pytest.raises(JournalCorruptError):
        journal.trade_fx("DEAL-US500-20261102-1-entry")  # zakładamy, że dziennik naprawdę ma uszkodzony zapis
    (row,) = ledger.read_ledger(journal)
    assert row.foreign and row.pending and row.net_eur is None and row.fx_unreadable


def test_export_eur_csv_has_one_row_per_trade_with_the_status_of_its_eur_amount(journal, tmp_path):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    book_usd(journal, D1, seq=1, settle="5.90")
    book_usd(journal, D2, seq=2, net_pnl=Decimal("-2.50"), exit_reason="STOP")
    path = tmp_path / "eur.csv"
    assert ledger.export_eur_csv(journal, path, {US: "USD"}) == 3
    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(ledger.EUR_COLUMNS)
    assert rows[1:] == [
        ["DE40", "EUR", "2026-11-02T16:25:00Z", "FORCED", "-0.03", "-0.03", "ACCOUNT_CURRENCY", "", ""],
        [US, "USD", "2026-11-02T16:25:00Z", "TARGET", "6.60", "5.90", "SETTLED", "HISTORY_TRANSACTIONS", ""],
        [US, "USD", "2026-11-03T16:25:00Z", "STOP", "-2.50", "", "PENDING", "", ""],
    ]  # fmt: skip
    assert "DEAL-" not in path.read_text(encoding="utf-8")  # żadnych identyfikatorów deali


def test_the_plain_ledger_export_keeps_its_nineteen_columns(journal, tmp_path):
    book_usd(journal, D1, settle="5.90")
    path = tmp_path / "ledger.csv"
    assert ledger.export_csv(journal, path) == 1
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(ledger.LEDGER_COLUMNS) and len(ledger.LEDGER_COLUMNS) == 19


def test_ledger_literals_match_the_engine_constants():
    from gwstock import app, fx

    assert ledger.ESTIMATED_FX == fx.ESTIMATED_FX
    assert ledger.ACCOUNT_CURRENCY == app.ACCOUNT_CURRENCY
