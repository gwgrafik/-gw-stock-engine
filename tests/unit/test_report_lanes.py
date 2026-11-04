"""Raport per pasmo (zadanie 15): osobne sekcje pasm w ich walutach, sekcja portfela w EUR (wiersze w EUR i rozliczenia,
oczekujące osobno), bramka L1 per pasmo, eksport CSV z ostrzeżeniem o walutach. Raport jednego pasma w EUR pilnuje
`test_report_single_lane_golden.py`."""

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.orders.ids import make_client_id
from gwstock.report import cli as report_cli
from tests.unit.report_lane_support import (  # noqa: F401  (jpath, lane_cfg, plain_cfg to fixtures)
    DAY1, PORTFOLIO, REPO, US, block, book_lane, book_usd, five_sessions, gates_line_prefix, jpath, lane_cfg, later,
    plain_cfg, run, two_lanes,
)  # fmt: skip
from tests.unit.test_report import D1, D2, D3, D4, D5, at, emit, smoke_times

RATIO_LABEL = "  koszty zmierzone / model (poślizg wejścia w punktach, bez zaokrągleń): "


def test_two_lanes_get_separate_sections_with_the_currency_on_every_amount(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    code, out, err = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert "Pasma w dzienniku: DE40 (księga w EUR), US500 (księga w USD)" in lines
    assert block(lines, DAY1, 12) == [
        DAY1,
        "Pasmo DE40 (księga w EUR)",
        "  Tryb: brak zapisu decyzji",
        "  Transakcje: 1",
        "  Brutto: -0.02 EUR",
        "  Koszt spreadu: 0.01 EUR",
        "  Poślizg (model z konfiguracji): 0.00 EUR",
        "  Finansowanie: 0.00 EUR",
        "  Opłaty: 0.00 EUR",
        "  Netto: -0.03 EUR",
        "  Netto narastająco: -0.03 EUR",
        "  blokady bramki ryzyka: 0",
    ]  # fmt: skip
    assert block(lines, "Pasmo US500 (księga w USD)", 13) == [
        "Pasmo US500 (księga w USD)",
        "  Tryb: brak zapisu decyzji",
        "  Transakcje: 1",
        "  Brutto: 7.20 USD",
        "  Koszt spreadu: 0.40 USD",
        "  Poślizg (model z konfiguracji): 0.20 USD",
        "  Finansowanie: 0.00 USD",
        "  Opłaty: 0.00 USD",
        "  Netto: 6.60 USD",
        "  Netto narastająco: 6.60 USD",
        "  Kwota w EUR z rozliczeń: 5.90 EUR (transakcje: 1, w tym szacunek z kursu: 0); czeka na rozliczenie: 0",
        "  blokady bramki ryzyka: 0",
        PORTFOLIO,
    ]  # fmt: skip


def test_the_portfolio_section_sums_eur_only_and_never_the_usd_amounts(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    lines = out.splitlines()
    day1 = lines[lines.index(DAY1) :]
    assert block(day1, PORTFOLIO, 5) == [
        PORTFOLIO,
        "  Wynik netto w EUR: dzień 5.87 EUR (pasma w EUR: -0.03 EUR, rozliczone z walut obcych: 5.90 EUR)",
        "  Wynik netto w EUR narastająco: 5.87 EUR",
        "  Czeka na rozliczenie w EUR od początku dziennika: 1 (kwoty w EUR nieznane: poza sumami w EUR i poza licznikiem dziennika)",
        "  Wynik zrealizowany wg dziennika: dzień 5.87 EUR, łącznie od początku 5.87 EUR",
    ]  # fmt: skip
    assert "NIEZGODNE" not in out  # licznik (EUR) zgadza się z księgą w EUR; 6,60 USD nie wchodzi do żadnej sumy EUR
    assert "6.57" not in out and "4.07" not in out  # -0,03 + 6,60 (+ -2,50) byłoby zmieszaniem walut


def test_a_trade_awaiting_settlement_is_marked_pending_not_added(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-03"], capsys)
    lines = out.splitlines()
    assert block(lines, "Pasmo US500 (księga w USD)", 13)[8:13] == [
        "  Netto: -2.50 USD",
        "  Netto narastająco: 4.10 USD",
        "  Kwota w EUR z rozliczeń: 0.00 EUR (transakcje: 0, w tym szacunek z kursu: 0); czeka na rozliczenie: 1",
        "  blokady bramki ryzyka: 0",
        PORTFOLIO,
    ]  # fmt: skip
    portfolio = block(lines, PORTFOLIO, 5)
    assert portfolio[1] == "  Wynik netto w EUR: dzień 0.00 EUR (pasma w EUR: 0.00 EUR, rozliczone z walut obcych: 0.00 EUR)"
    assert portfolio[2] == "  Wynik netto w EUR narastająco: 5.87 EUR"
    assert "NIEZGODNE" not in out  # oczekująca transakcja nie jest jeszcze w liczniku dziennika, więc nie ma różnicy


def test_a_counter_that_disagrees_with_the_eur_ledger_is_still_flagged(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    journal.add_realized_pnl(D1, Decimal("0.50"))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert ("  Wynik zrealizowany wg dziennika: dzień 6.37 EUR, łącznie od początku 6.37 EUR NIEZGODNE z księgą "
            "(księga: dzień 5.87 EUR, łącznie 5.87 EUR)") in out.splitlines()  # fmt: skip


def test_a_usd_amount_added_to_the_eur_counter_is_caught(journal, capsys, lane_cfg, jpath):
    # błąd, przed którym broni raport: do licznika w EUR trafiła kwota w USD (6,60) obok rozliczenia 5,90 EUR
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    book_usd(journal, D1, seq=1, settle="5.90")
    journal.add_realized_pnl(D1, Decimal("6.60"))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert "NIEZGODNE" in out


def test_the_summary_is_per_lane_in_its_own_currency_and_the_portfolio_in_eur(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    start = lines.index("Podsumowanie od początku dziennika:")
    assert lines[start : start + 19] == [
        "Podsumowanie od początku dziennika:",
        "Pasmo DE40 (księga w EUR):",
        "  transakcje: 1 (netto dodatnie 0, ujemne 1, zerowe 0)",
        "  brutto: -0.02 EUR",
        "  koszty: spread 0.01 EUR, poślizg (model) 0.00 EUR, finansowanie 0.00 EUR, opłaty 0.00 EUR",
        "  netto: -0.03 EUR",
        "  najgorszy dzień: 2026-11-02 (-0.03 EUR)",
        "Pasmo US500 (księga w USD):",
        "  transakcje: 2 (netto dodatnie 1, ujemne 1, zerowe 0)",
        "  brutto: 5.00 USD",
        "  koszty: spread 0.60 USD, poślizg (model) 0.30 USD, finansowanie 0.00 USD, opłaty 0.00 USD",
        "  netto: 4.10 USD",
        "  najgorszy dzień: 2026-11-03 (-2.50 USD)",
        "  w EUR z rozliczeń: 5.90 EUR (transakcje: 1, w tym szacunek z kursu: 0); czeka na rozliczenie: 1",
        "Portfel (EUR):",
        "  netto w EUR: 5.87 EUR (pasma w EUR: -0.03 EUR, rozliczone z walut obcych: 5.90 EUR)",
        "  czeka na rozliczenie w EUR: 1 (kwota w EUR nieznana)",
        "  najgorszy dzień w EUR: 2026-11-02 (5.87 EUR)",
        "",
    ]  # fmt: skip


def test_a_single_foreign_lane_is_never_printed_in_the_old_layout(journal, capsys, lane_cfg, jpath):
    book_usd(journal, D1, settle="5.90")
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "Pasmo US500 (księga w USD)" in lines and PORTFOLIO in lines
    assert "  Brutto: 7.20 USD" in lines  # kwota USD zawsze z walutą
    assert not [line for line in lines if line.startswith(("Brutto:", "Netto:"))]  # żadnych wierszy starego układu
    assert "  Wynik netto w EUR: dzień 5.90 EUR (pasma w EUR: 0.00 EUR, rozliczone z walut obcych: 5.90 EUR)" in lines


def test_two_lanes_in_the_account_currency_also_get_sections(journal, capsys, plain_cfg, jpath):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    book_lane(journal, D1, "DE30", net_pnl=Decimal("0.04"), **later(D1))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "Pasma w dzienniku: DE40 (księga w EUR), DE30 (księga w EUR)" in lines
    assert "  Wynik netto w EUR: dzień 0.01 EUR (pasma w EUR: 0.01 EUR, rozliczone z walut obcych: 0.00 EUR)" in lines


def test_lane_modes_and_gate_blocks_are_attributed_to_the_lane_by_instrument(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    emit(journal, "decision", {"event": "DECISION", "day": "2026-11-02", "mode": "live"}, at(D1, 8, 5))
    emit(journal, "decision", {"event": "LANE_DECISION", "epic": US, "day": "2026-11-02", "mode": "dry_run"},
         at(D1, 14, 5))  # fmt: skip
    emit(journal, "gate_block", {"reasons": ["MARGIN"], "candidate": {"epic": "DE40"}, "day": "2026-11-02"}, at(D1, 8, 6))
    emit(journal, "gate_block", {"reasons": ["PORTFOLIO_OPEN_RISK", "MARGIN"], "candidate": {"epic": US},
                                 "day": "2026-11-02"}, at(D1, 14, 6))  # fmt: skip
    emit(journal, "gate_block", {"reasons": ["PNL_PENDING"], "epic": US, "currency": "USD", "day": "2026-11-02"},
         at(D1, 14, 7))  # fmt: skip
    emit(journal, "gate_block", {"reasons": ["KILL"], "epic": US, "day": "2026-11-02", "smoke": True}, at(D1, 14, 8))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    de40 = lines[lines.index("Pasmo DE40 (księga w EUR)") : lines.index("Pasmo US500 (księga w USD)")]
    us500 = lines[lines.index("Pasmo US500 (księga w USD)") : lines.index(PORTFOLIO)]
    assert "  Tryb: live" in de40 and "  blokady bramki ryzyka: 1 (MARGIN: 1)" in de40
    assert "  Tryb: dry_run" in us500
    assert "  blokady bramki ryzyka: 2 (MARGIN: 1, PNL_PENDING: 1, PORTFOLIO_OPEN_RISK: 1) + próby smoke: 1" in us500


def test_lane_decisions_alone_make_a_section_for_a_lane_without_trades(journal, capsys, lane_cfg, jpath):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    emit(journal, "decision", {"event": "LANE_DECISION", "epic": US, "day": "2026-11-02", "mode": "dry_run"},
         at(D1, 14, 5))  # fmt: skip
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert "Pasma w dzienniku: DE40 (księga w EUR), US500 (księga w USD)" in lines  # waluta z config/lanes.toml
    assert block(lines, "Pasmo US500 (księga w USD)", 4) == [
        "Pasmo US500 (księga w USD)", "  Tryb: dry_run", "  Transakcje: 0", "  blokady bramki ryzyka: 0",
    ]  # fmt: skip


def test_a_lane_without_trades_and_without_a_config_entry_says_so(journal, capsys, plain_cfg, jpath):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    emit(journal, "decision", {"event": "LANE_DECISION", "epic": US, "day": "2026-11-02", "mode": "dry_run"},
         at(D1, 14, 5))  # fmt: skip
    _, out, _ = run(["--journal", jpath], capsys)
    assert "Pasma w dzienniku: DE40 (księga w EUR), US500 (brak transakcji)" in out.splitlines()


def test_a_lanes_file_alone_does_not_change_a_single_lane_report(journal, capsys, lane_cfg, jpath, monkeypatch):
    for day in (D1, D2):
        book_lane(journal, day, "DE40")
    _, with_lanes, _ = run(["--journal", jpath, "--all"], capsys)
    monkeypatch.setenv("GW_CONFIG_DIR", str(REPO / "config"))
    _, without_lanes, _ = run(["--journal", jpath, "--all"], capsys)
    assert with_lanes == without_lanes and "Pasmo" not in with_lanes
    (lane_cfg / "lanes.toml").write_text("enabled = [", encoding="utf-8")
    monkeypatch.setenv("GW_CONFIG_DIR", str(lane_cfg))
    _, broken, _ = run(["--journal", jpath, "--all"], capsys)
    assert broken == without_lanes  # plik pasm czytamy dopiero, gdy dziennik ma więcej niż jedno pasmo


def test_portfolio_events_stay_in_one_shared_block(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": make_client_id(D1, US, 9, "entry")}, at(D1, 14, 8))
    emit(journal, "event", {"event": "TRADE_SETTLED", "deal_id": "DEAL-X", "net_eur": "5.90"}, at(D1, 16, 30))
    emit(journal, "event", {"event": "COST_FALLBACK"}, at(D1, 16, 31))
    emit(journal, "kill.audit", {"event": "ENGAGE", "level": "SOFT", "reason": "PNL_PENDING"}, at(D1, 16, 32))
    emit(journal, "watchdog", {"event": "LOGIN_LOCKOUT"}, at(D1, 16, 33))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    shared = lines[lines.index("Błędy i zdarzenia (wspólne dla pasm):") :][:6]
    assert shared == [
        "Błędy i zdarzenia (wspólne dla pasm):",
        "  ENTRY_UNRESOLVED: 1",
        "  kill switch: ENGAGE SOFT PNL_PENDING x1",
        "  blokady logowania (LOGIN_LOCKOUT): 1",
        "  inne zdarzenia: COST_FALLBACK x1",
        "",
    ]  # fmt: skip
    assert "TRADE_SETTLED" not in out  # rozliczenie to zwykły przebieg, nie błąd


def test_each_lane_gets_its_own_l1_gate_from_its_own_trades(journal, capsys, lane_cfg, jpath):
    five_sessions(journal, "DE40", days=(D1, D2, D3, D4, D5))
    five_sessions(journal, US, days=(D1, D2))
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    lines = out.splitlines()
    tail = "(transakcje tego pasma; rozbieżności i odrzucenia z całego dziennika; ocena wyjaśnień należy do właściciela)"
    assert f"Bramka L1 (spec §11), pasmo DE40: PASS {tail}" in lines
    assert f"Bramka L1 (spec §11), pasmo US500: NOT YET {tail}" in lines
    de40 = lines[lines.index(f"Bramka L1 (spec §11), pasmo DE40: PASS {tail}") :][:3]
    assert de40[1] == "  sesje na wielkości minimalnej: 5 z 5 (brakuje 0)"
    us = lines[lines.index(f"Bramka L1 (spec §11), pasmo US500: NOT YET {tail}") :][:3]
    assert us[1] == "  sesje na wielkości minimalnej: 2 z 5 (brakuje 3)"


@pytest.mark.parametrize(
    ("fill", "slip", "verdict", "ratio"),
    [("7583.9", "0.30", "PASS", "2.00"), ("7584.0", "0.40", "NOT YET", "2.67")],  # model pasma 0,15 pkt; v1 ma 0,5 pkt
)
def test_a_lane_gate_uses_the_cost_model_of_its_lane_config(journal, capsys, lane_cfg, jpath, fill, slip, verdict, ratio):
    # poślizg 0,3 pkt to dokładnie 2 x model pasma (równo przechodzi), 0,4 pkt to 2,67 x (przegląd); wg modelu v1 (0,5)
    # oba przeszłyby z zapasem, więc to rozróżnia model pasma od modelu v1
    five_sessions(journal, US, entry_fill=Decimal(fill))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert any(line.startswith(gates_line_prefix("US500", verdict)) for line in lines)
    assert any(f"średni niekorzystny poślizg wejścia: {slip} pkt (model: 0.15 pkt;" in line for line in lines)
    ratio_lines = [line for line in lines if line.startswith(RATIO_LABEL)]
    assert ratio_lines and ratio_lines[-1].startswith(f"{RATIO_LABEL}{ratio} (najwyżej 2;")  # ostatnia: pasmo US500


def test_incidents_of_the_portfolio_count_in_every_lane_gate(journal, capsys, lane_cfg, jpath):
    five_sessions(journal, "DE40")
    five_sessions(journal, US)
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 14, 10)}, at(D1, 14, 20))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    assert any(line.startswith(gates_line_prefix("DE40", "NOT YET")) for line in lines)
    assert any(line.startswith(gates_line_prefix("US500", "NOT YET")) for line in lines)
    mismatch = "  rozbieżności pozycja/broker starsze niż 60 s: 1 (wymagane 0; dni: 2026-11-02)"
    assert lines.count(mismatch) == 2  # raz w bramce każdego pasma


def test_the_money_in_a_foreign_lane_gate_is_named_in_its_currency(journal, capsys, lane_cfg, jpath):
    five_sessions(journal, US)
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    us = lines[lines.index(next(l for l in lines if l.startswith(gates_line_prefix("US500", "PASS")))) :]
    assert any(line.startswith("  koszty w USD (informacyjnie): ratio ") for line in us)
    assert any(line.startswith("  uwaga: koszty w USD są zaokrąglane") for line in us)
    assert not any(line.startswith("  koszty w EUR") for line in us)


def test_a_lane_without_a_config_entry_is_not_assessed(journal, capsys, plain_cfg, jpath):
    five_sessions(journal, "DE40")
    book_lane(journal, D1, "DE30", **later(D1))
    _, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert any(line.startswith(gates_line_prefix("DE40", "PASS")) for line in lines)
    assert ("Bramka L1 (spec §11), pasmo DE30: NIE OCENIONO (brak wpisu tego pasma w config/lanes.toml: "
            "nie znam wymaganej liczby sesji ani modelu kosztów)") in lines  # fmt: skip


def test_an_unreadable_lanes_file_is_reported_and_the_rest_still_prints(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    (lane_cfg / "lanes.toml").write_text("enabled = [", encoding="utf-8")
    code, out, err = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert any(
        line.startswith("UWAGA: nie wczytano pasm z config/lanes.toml (lanes.toml: cannot read the TOML file")
        for line in lines
    )  # fmt: skip
    assert "Pasmo US500 (księga w [waluta instrumentu])" in lines  # nazwa waluty nieznana, ale NIE EUR
    assert "  Netto: 6.60 [waluta instrumentu]" in lines
    assert "Bramka L1 (spec §11), pasmo US500: NIE OCENIONO" in "\n".join(lines)


def test_the_gate_instruction_for_an_extra_lane_does_not_name_the_v1_file(journal, capsys, lane_cfg, jpath):
    five_sessions(journal, "DE40")
    five_sessions(journal, US)
    _, out, _ = run(["--journal", jpath], capsys)
    assert out.count("Aby dopuścić wielkość według reguł utwórz plik data/L1_PASSED") == 1  # tylko pasmo v1
    assert "Aby dopuścić wielkość według reguł pasma US500" in out
    assert "kod go nigdy nie tworzy" in out


def test_a_foreign_owner_estimate_names_its_currency(journal, capsys, lane_cfg, jpath):
    book_lane(journal, D1, "DE40")
    book_usd(journal, D2, flags="ESTIMATED RESOLVED_BY_OWNER", net_pnl=Decimal("-2.50"), gross_pnl=Decimal("-2.50"),
             exit_reason="STOP")  # fmt: skip
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    assert any(
        line.startswith("UWAGA: transakcja z 2026-11-03 (wyjście 17:25 czasu Europe/Berlin, STOP, netto -2.50 USD)")
        for line in out.splitlines()
    )  # fmt: skip


def test_the_multi_lane_report_never_prints_ids_and_makes_no_claims(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    for secret in ("DEAL-", "gws-2026", "ref-"):
        assert secret not in out
    statement = "To pomiar, nie prognoza. Wynik netto po kosztach; brak przewagi nie jest wykazany ani wykluczony."
    assert out.splitlines()[-1] == statement
    for line in out.splitlines()[:-1]:
        for forbidden in ("zysk", "przewag", "wygr", "sukces"):
            assert forbidden not in line.lower(), line


def test_csv_with_foreign_rows_says_its_net_column_is_not_eur_and_points_at_the_eur_export(
    journal, capsys, lane_cfg, jpath, tmp_path
):
    two_lanes(journal)
    csv_path, eur_path = tmp_path / "ledger.csv", tmp_path / "eur.csv"
    code, out, _ = run(["--journal", jpath, "--csv", str(csv_path), "--csv-eur", str(eur_path)], capsys)
    assert code == 0
    lines = out.splitlines()
    assert f"CSV: {csv_path} (wiersze danych: 3)" in lines
    assert ("UWAGA: w CSV kolumny kwot wierszy pasm w walucie obcej (2 z 3 wierszy) są w walucie instrumentu, "
            "nie w EUR; kwoty w EUR: --csv-eur") in lines  # fmt: skip
    assert f"CSV (kwoty w EUR): {eur_path} (wiersze danych: 3)" in lines
    with open(eur_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert [row[6] for row in rows[1:]] == ["ACCOUNT_CURRENCY", "SETTLED", "PENDING"]
    assert rows[2][1] == "USD"  # waluta pasma z config/lanes.toml


def test_csv_of_the_account_currency_only_has_no_currency_warning(journal, capsys, plain_cfg, jpath, tmp_path):
    book_lane(journal, D1, "DE40")
    _, out, _ = run(["--journal", jpath, "--csv", str(tmp_path / "ledger.csv")], capsys)
    assert "UWAGA: w CSV" not in out


def test_the_eur_csv_option_is_independent_and_failure_is_exit_1(journal, capsys, lane_cfg, jpath, tmp_path):
    two_lanes(journal)
    code, out, err = run(["--journal", jpath, "--csv-eur", str(tmp_path / "missing" / "eur.csv")], capsys)
    assert code == 1 and "CSV export failed" in err
    assert "Pasmo US500 (księga w USD)" in out  # reszta raportu i tak wydrukowana


def test_a_settlement_from_the_rate_estimate_is_named_as_an_estimate(journal, capsys, lane_cfg, jpath):
    # dziś silnik rozlicza fazę 2 szacunkiem z kursu brokera (ESTIMATED_FX), nie kwotą z historii: raport ma to mówić
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    deal = book_usd(journal, D1)
    assert journal.settle_trade_pnl(deal, Decimal("5.80"), source="ESTIMATED_FX", rate=Decimal("1.1379"))
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    assert "  Kwota w EUR z rozliczeń: 5.80 EUR (transakcje: 1, w tym szacunek z kursu: 1); czeka na rozliczenie: 0" in lines
    assert "  w tym szacunek z kursu brokera (ESTIMATED_FX): 1 transakcji, 5.80 EUR (nie kwota z historii brokera)" in lines
    assert "  w EUR z rozliczeń: 5.80 EUR (transakcje: 1, w tym szacunek z kursu: 1); czeka na rozliczenie: 0" in lines
    assert "NIEZGODNE" not in out  # licznik dziennika zawiera tę samą kwotę


def test_a_settlement_from_the_broker_history_is_not_called_an_estimate(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    assert "ESTIMATED_FX" not in out and "w tym szacunek z kursu: 0" in out


def test_a_journal_with_only_lane_decisions_still_prints_the_lane(journal, capsys, lane_cfg, jpath):
    emit(journal, "decision", {"event": "LANE_DECISION", "epic": US, "day": "2026-11-02", "mode": "dry_run"},
         at(D1, 14, 5))  # fmt: skip
    code, out, _ = run(["--journal", jpath], capsys)
    lines = out.splitlines()
    assert code == 0 and "Dziennik jest pusty: brak transakcji i zapisów dnia." not in lines
    assert "Pasma w dzienniku: US500 (księga w USD)" in lines and DAY1 in lines
    assert block(lines, "Pasmo US500 (księga w USD)", 3) == [
        "Pasmo US500 (księga w USD)", "  Tryb: dry_run", "  Transakcje: 0",
    ]  # fmt: skip
    assert "  Wynik netto w EUR: dzień 0.00 EUR (pasma w EUR: 0.00 EUR, rozliczone z walut obcych: 0.00 EUR)" in lines


def test_a_lane_booked_in_two_currencies_prints_no_sum_in_either_but_the_eur_total_stays_right(
    journal, capsys, lane_cfg, jpath
):
    book_lane(journal, D1, US, net_pnl=Decimal("1.00"))  # księgowany w EUR (nie powinno się zdarzyć dla pasma USD)
    book_usd(journal, D2, settle="5.90")
    _, out, _ = run(["--journal", jpath, "--all"], capsys)
    lines = out.splitlines()
    assert lines.count(report_cli.MIXED_CURRENCY) >= 2  # w dniu pasma i w podsumowaniu
    assert not [line for line in lines if line.startswith(("  Netto:", "  netto:")) and "EUR" not in line]
    assert "  Wynik netto w EUR narastająco: 6.90 EUR" in lines  # wiersz w EUR + rozliczenie, wiersz po wierszu


def test_smoke_trades_and_gate_blocks_without_an_instrument_stay_in_their_own_lane(journal, capsys, lane_cfg, jpath):
    book_lane(journal, D1, "DE40")
    book_lane(journal, D1, "DE40", seq=2, role="smoke", **smoke_times(D1))
    book_usd(journal, D1, seq=1, settle="5.90")
    emit(journal, "gate_block", {"reasons": ["STALE_QUOTE"], "day": "2026-11-02"}, at(D1, 8, 6))  # bez instrumentu
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    de40 = lines[lines.index("Pasmo DE40 (księga w EUR)") : lines.index("Pasmo US500 (księga w USD)")]
    us500 = lines[lines.index("Pasmo US500 (księga w USD)") : lines.index(PORTFOLIO)]
    assert "  Transakcje: 2 (w tym próby smoke: 1)" in de40
    assert "  blokady bramki ryzyka: 1 (STALE_QUOTE: 1)" in de40  # zapis sprzed pasm to pasmo v1
    assert "  Transakcje: 1" in us500 and "  blokady bramki ryzyka: 0" in us500


def test_an_unreadable_settlement_is_pending_in_the_report_and_the_counter_mismatch_shows(
    journal, capsys, lane_cfg, jpath
):
    book_lane(journal, D1, "DE40", net_pnl=Decimal("-0.03"))
    deal = book_usd(journal, D1, settle="5.90")
    journal.set_meta(f"trade_fx:{deal}", "{not json")
    _, out, _ = run(["--journal", jpath, "--day", "2026-11-02"], capsys)
    lines = out.splitlines()
    assert "UWAGA: 1 nieczytelnych rozliczeń w EUR (trade_fx) potraktowano jak oczekujące na rozliczenie." in lines
    assert "  Wynik netto w EUR: dzień -0.03 EUR (pasma w EUR: -0.03 EUR, rozliczone z walut obcych: 0.00 EUR)" in lines
    assert "NIEZGODNE" in out  # licznik ma już 5,90, księga nie umie tego pokazać: różnica ma być widoczna


def test_eur_csv_on_a_single_lane_journal_marks_every_row_as_account_currency(
    journal, capsys, plain_cfg, jpath, tmp_path
):
    book_lane(journal, D1, "DE40")
    path = tmp_path / "eur.csv"
    _, out, _ = run(["--journal", jpath, "--csv-eur", str(path)], capsys)
    assert f"CSV (kwoty w EUR): {path} (wiersze danych: 1)" in out.splitlines()
    assert "Pasmo" not in out  # jedno pasmo w EUR: stary układ
    assert path.read_text(encoding="utf-8").splitlines()[1].split(",")[6] == "ACCOUNT_CURRENCY"


def test_the_multi_lane_report_does_not_change_the_journal(journal, capsys, lane_cfg, jpath):
    two_lanes(journal)
    journal.close()
    before = Path(jpath).read_bytes()
    code, _, _ = run(["--journal", jpath, "--all"], capsys)
    assert code == 0 and Path(jpath).read_bytes() == before
    assert not Path(jpath + "-wal").exists() and not Path(jpath + "-shm").exists()


def test_the_settled_event_literal_matches_the_engine():
    import inspect

    from gwstock import execution

    assert '"TRADE_SETTLED"' in inspect.getsource(execution.Executor.settle_foreign_pnl)
    assert "TRADE_SETTLED" in report_cli.BENIGN_EVENTS


def test_report_event_literals_match_the_engine_constants():
    from gwstock import engine_lanes

    assert report_cli.EVENT_LANE_DECISION == engine_lanes.LANE_DECISION
    assert report_cli.EVENT_DECISION == "DECISION"
