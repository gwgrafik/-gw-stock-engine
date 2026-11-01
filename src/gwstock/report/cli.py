"""Raport dzienny `python -m gwstock.report` (spec §10, §11): wynik netto po kosztach, liczniki błędów, bramka L1, CSV.

Opcje: `--day YYYY-MM-DD` (domyślnie ostatni dzień z transakcją lub decyzją, a gdy takich nie ma, ostatni dzień
z zapisem błędu), `--all` (wszystkie dni), `--csv PATH` (eksport księgi), `--journal PATH` (domyślnie
`$GW_DATA_DIR/journal.sqlite`, `GW_DATA_DIR` domyślnie `data`; konfiguracja z `$GW_CONFIG_DIR`, domyślnie `config`).
Kody wyjścia: 0 raport wypisany, 1 błąd (brak/uszkodzony dziennik, brak konfiguracji, nieudany eksport CSV,
wartości poza zakresem arytmetyki; to ostatnia deska ratunku, a pojedynczy dziwny zapis nie przerywa raportu:
liczby, których nie da się wydrukować, wychodzą jako `n/a`), 2 błąd użycia.

Dziennik otwieramy TYLKO DO ODCZYTU (`Journal.open_readonly`, ten sam otwieracz co `status`): raport można puścić, gdy
silnik pracuje, a plik nie zmienia się ani o bajt (na zamkniętym dzienniku nie powstaje też `-wal` ani `-shm`). Raport
nie drukuje identyfikatorów rachunku, deali ani zleceń, tylko liczby i kody zdarzeń. Dzień zdarzenia i transakcji to
dzień LOKALNY giełdy (Europe/Berlin). Raport nie przesądza o opłacalności: wynik netto po kosztach jest pomiarem,
a ocena bramki L1 należy do właściciela.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.config import CostConfig, load_config
from gwstock.journal import Journal, JournalError, TradeRecord
from gwstock.report import gates
from gwstock.report.gates import L1Details, format_ratio, read_events
from gwstock.report.ledger import decimal_text, exact_sum, export_csv, local_day, trade_day, trades_summary

__all__ = ["main", "open_journal_readonly", "JOURNAL_FILE"]

ENV_CONFIG_DIR = "GW_CONFIG_DIR"
ENV_DATA_DIR = "GW_DATA_DIR"
DEFAULT_CONFIG_DIR = "config"
DEFAULT_DATA_DIR = "data"
JOURNAL_FILE = "journal.sqlite"  # ta sama nazwa co w `gwstock.cli` (pilnuje tego test)

STATEMENT = "To pomiar, nie prognoza. Wynik netto po kosztach; brak przewagi nie jest wykazany ani wykluczony."
L1_INSTRUCTION = (
    "Aby dopuścić wielkość według reguł utwórz plik data/L1_PASSED (kod go nigdy nie tworzy). To informacja dla "
    "właściciela, nie zgoda: plik tworzysz dopiero po własnym przeglądzie raportu (runbook, sekcja 7)."
)
L1_VERDICT_TAIL = "(cały dziennik; ocena wyjaśnień należy do właściciela)"
L1_EUR_CAVEAT = (
    "  uwaga: koszty w EUR są zaokrąglane do 0,01 € przy wielkości 0,001, więc ratio w EUR jest zgrubne; "
    "poślizg stopu przy wyjściu NIE jest mierzony"
)
L1_RATIO_LABEL = "  koszty zmierzone / model (poślizg wejścia w punktach, bez zaokrągleń): "
L1_EXCLUDED_LABEL = (
    "  wykluczone z pomiaru kosztów i z liczby sesji "
    "(brak kontekstu wejścia, uszkodzona wartość punktu albo nieużyteczne notowanie decyzji): "
)
L1_REJECTION_NOTE = (
    "    liczy ENTRY_UNRESOLVED także po późniejszym rozstrzygnięciu zlecenia oraz zlecenia INTENT/SUBMITTING, "
    "gdy raport powstaje w trakcie wejścia"
)
DAY_HEADER = "Dzień: {} (czas Europe/Berlin, dzień zamknięcia transakcji)"
NO_DECISION = "brak zapisu decyzji"
# Kody zdarzeń pokazywane osobnym licznikiem albo nie będące błędem; reszta idzie do „inne zdarzenia”. CLOSURE_BOOKED
# (uzgadnianie zaksięgowało zamknięcie) powtarza się przy idempotentnym ponownym księgowaniu: transakcje liczy księga.
BENIGN_EVENTS = frozenset({"TRADE_BOOKED", "CLOSURE_BOOKED"})
ESTIMATED_FLAG = "ESTIMATED"  # znacznik księgi szacunku właściciela (`Journal.trade_flags`)
ESTIMATED_WARNING = (
    "UWAGA: transakcja z {day} (wyjście {time} czasu Europe/Berlin, {reason}, netto {net}) zaksięgowana przez "
    "właściciela jako SZACUNEK (najgorsza strata ze stopu); sprawdź wynik w Capital.com"
)
LOGIN_LOCKOUT = "LOGIN_LOCKOUT"
EVENT_SOURCES = (gates.SOURCE_EVENT, gates.SOURCE_RECONCILE, gates.SOURCE_WATCHDOG, gates.SOURCE_ENGINE)

_DAY_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CALENDAR = ExchangeCalendar(XETRA)


# --- dziennik tylko do odczytu --------------------------------------------------------------------------------------


def open_journal_readonly(path: Path) -> Journal:
    """Ten sam otwieracz co `status`: `Journal.open_readonly` (bez pliku `-wal` `immutable=1`, więc na zamkniętym
    dzienniku nie zostaje żaden nowy plik; przy działającym silniku `mode=ro`; `query_only`; sprawdzenie wersji schematu
    i `application_id`, bez oznaczania). Każdy zapis to błąd; brak pliku albo baza spoza gwstock: `JournalError`.
    """
    return Journal.open_readonly(str(path))


# --- zebrane dane dnia ------------------------------------------------------------------------------------------------


@dataclass
class _Day:
    trades: list[TradeRecord] = field(default_factory=list)
    smoke_trades: int = 0
    modes: list[str] = field(default_factory=list)
    gate_blocks: int = 0
    gate_reasons: Counter = field(default_factory=Counter)
    smoke_blocks: int = 0
    unresolved: int = 0
    kills: Counter = field(default_factory=Counter)
    lockouts: int = 0
    other: Counter = field(default_factory=Counter)

    @property
    def has_problems(self) -> bool:
        return bool(
            self.gate_blocks or self.smoke_blocks or self.unresolved or self.kills or self.lockouts or self.other
        )


@dataclass
class _Data:
    days: dict[date, _Day]
    session_days: set[date]
    unreadable: int


def _local_day(event) -> date:
    return local_day(event.ts)


def _collect(journal: Journal, trades: list[TradeRecord], roles: list[str]) -> _Data:
    days: dict[date, _Day] = {}
    session_days: set[date] = set()

    def day_of(day: date) -> _Day:
        return days.setdefault(day, _Day())

    for trade, role in zip(trades, roles):
        day = trade_day(trade)
        day_of(day).trades.append(trade)
        day_of(day).smoke_trades += role == gates.ROLE_SMOKE
        session_days.add(day)
    unreadable = 0

    for event, data in read_events(journal, gates.SOURCE_DECISION):
        if data is None:
            unreadable += 1
        elif data.get("event") == "DECISION" and isinstance(data.get("day"), str) and isinstance(data.get("mode"), str):
            try:
                day = date.fromisoformat(data["day"])
            except ValueError:
                continue
            session_days.add(day)
            if data["mode"] not in day_of(day).modes:
                day_of(day).modes.append(data["mode"])

    for event, data in read_events(journal, gates.SOURCE_GATE_BLOCK):
        if data is None:
            unreadable += 1
            continue
        bucket = day_of(_local_day(event))
        if data.get("smoke") is True:
            bucket.smoke_blocks += 1
            continue
        bucket.gate_blocks += 1
        reasons = data.get("reasons")
        for reason in reasons if isinstance(reasons, list) else []:
            if isinstance(reason, str):
                bucket.gate_reasons[reason] += 1

    for event, data in read_events(journal, gates.SOURCE_KILL_AUDIT):
        if data is None:
            unreadable += 1
            continue
        key = tuple(str(data.get(name) or "") for name in ("event", "level", "reason"))
        day_of(_local_day(event)).kills[key] += 1

    for source in EVENT_SOURCES:
        for event, data in read_events(journal, source):
            if data is None:
                unreadable += 1
                continue
            code = data.get("event")
            if not isinstance(code, str) or not code:
                continue
            bucket = day_of(_local_day(event))
            if code == gates.ENTRY_UNRESOLVED:
                bucket.unresolved += 1
            elif code == LOGIN_LOCKOUT:
                bucket.lockouts += 1
            elif code not in BENIGN_EVENTS:
                bucket.other[code] += 1
    return _Data(days, session_days, unreadable)


# --- wypisywanie ------------------------------------------------------------------------------------------------------


def _d(value: Decimal) -> str:
    """Kwota do wydruku: cyfry bez zmian; zero bez znaku i z dwoma miejscami (`0.00`, nie `0` ani `-0.00`)."""
    if value.is_zero() and value.as_tuple().exponent > -2:
        return "0.00"
    return decimal_text(value)


def _counts(counter: Counter) -> str:
    return ", ".join(f"{name}: {count}" for name, count in sorted(counter.items()))


def _day_lines(journal: Journal, day: date, bucket: _Day, all_trades: list[TradeRecord]) -> list[str]:
    summary = trades_summary(bucket.trades)
    cumulative = exact_sum(t.net_pnl for t in all_trades if trade_day(t) <= day)
    realized_day, realized_total = journal.realized_pnl(day), journal.realized_pnl_total()
    ledger_total = exact_sum(t.net_pnl for t in all_trades)
    realized = f"Wynik zrealizowany wg dziennika: dzień {_d(realized_day)}, łącznie od początku {_d(realized_total)}"
    if realized_day != summary["net"] or realized_total != ledger_total:
        realized += f" NIEZGODNE z księgą (księga: dzień {_d(summary['net'])}, łącznie {_d(ledger_total)})"
    blocks = f"{bucket.gate_blocks}" + (f" ({_counts(bucket.gate_reasons)})" if bucket.gate_reasons else "")
    if bucket.smoke_blocks:
        blocks += f" + próby smoke: {bucket.smoke_blocks}"
    smoke_note = f" (w tym próby smoke: {bucket.smoke_trades})" if bucket.smoke_trades else ""
    kills = "; ".join(
        f"{' '.join(part for part in key if part)} x{count}" for key, count in sorted(bucket.kills.items())
    ) or "brak"  # fmt: skip
    lines = [
        DAY_HEADER.format(day.isoformat()),
        f"Tryb: {', '.join(bucket.modes) if bucket.modes else NO_DECISION}",
        f"Transakcje: {summary['count']}{smoke_note}",
        f"Brutto: {_d(summary['gross'])}",
        f"Koszt spreadu: {_d(summary['spread_cost'])}",
        f"Poślizg (model z konfiguracji): {_d(summary['slippage_penalty'])}",
        f"Finansowanie: {_d(summary['financing'])}",
        f"Opłaty: {_d(summary['fees'])}",
        f"Netto: {_d(summary['net'])}",
        f"Netto narastająco: {_d(cumulative)}",
        realized,
        "Błędy i zdarzenia:",
        f"  blokady bramki ryzyka: {blocks}",
        f"  ENTRY_UNRESOLVED: {bucket.unresolved}",
        f"  kill switch: {kills}",
        f"  blokady logowania ({LOGIN_LOCKOUT}): {bucket.lockouts}",
    ]
    if bucket.other:
        lines.append("  inne zdarzenia: " + ", ".join(f"{code} x{n}" for code, n in sorted(bucket.other.items())))
    lines.append("")
    return lines


def _summary_lines(trades: list[TradeRecord]) -> list[str]:
    s = trades_summary(trades)
    lines = [
        "Podsumowanie od początku dziennika:",
        f"  transakcje: {s['count']} (netto dodatnie {s['wins']}, ujemne {s['losses']}, zerowe {s['breakeven']})",
        f"  brutto: {_d(s['gross'])}",
        f"  koszty: spread {_d(s['spread_cost'])}, poślizg (model) {_d(s['slippage_penalty'])}, "
        f"finansowanie {_d(s['financing'])}, opłaty {_d(s['fees'])}",
        f"  netto: {_d(s['net'])}",
    ]
    if s["worst_day"] is not None:
        day, net = s["worst_day"]
        lines.append(f"  najgorszy dzień: {day.isoformat()} ({_d(net)})")
    lines.append("")
    return lines


def _estimated_lines(trades: list[TradeRecord], flags: list[str | None]) -> list[str]:
    """Uwaga dla każdej transakcji zaksięgowanej przez właściciela jako SZACUNEK (`resolve-lost-trade`): jej wynik to
    najgorsza strata ze stopu, nie pomiar. Bez identyfikatora deala (raport ich nie drukuje): dzień, godzina, wynik."""
    lines = []
    for trade, marks in zip(trades, flags, strict=True):
        if ESTIMATED_FLAG not in (marks or "").split():
            continue
        local = _CALENDAR.to_local(trade.exit_ts)
        lines.append(ESTIMATED_WARNING.format(
            day=local.date().isoformat(), time=f"{local:%H:%M}", reason=trade.exit_reason, net=_d(trade.net_pnl),
        ))  # fmt: skip
    return lines


def _days(days: tuple[date, ...]) -> str:
    return "; dni: " + ", ".join(day.isoformat() for day in sorted(set(days))) if days else ""


def _gate_lines(details: L1Details, required: int, costs: CostConfig) -> list[str]:
    gate, points = details.gate, details.points
    reasons = gate.reasons(required)
    missing = max(0, required - gate.sessions_at_min_size)
    lines = [
        f"Bramka L1 (spec §11): {'PASS' if gate.passed(required) else 'NOT YET'} {L1_VERDICT_TAIL}",
        f"  sesje na wielkości minimalnej: {gate.sessions_at_min_size} z {required} (brakuje {missing})",
        "  rozbieżności pozycja/broker starsze niż 60 s: "
        f"{gate.unreconciled_over_60s} (wymagane 0{_days(details.mismatch_days)})",
        f"  nieobsłużone odrzucenia: {gate.unhandled_rejections} (wymagane 0{_days(details.rejection_days)})",
    ]
    if gate.unhandled_rejections:
        lines.append(L1_REJECTION_NOTE)
    limit = gates.COST_DEVIATION_LIMIT
    verdict = format_ratio(gate.cost_deviation_ratio, limit) if points.trades else "brak danych"
    lines.append(f"{L1_RATIO_LABEL}{verdict} (najwyżej {limit}; próbki pomiaru: {points.trades} z {required})")
    if points.trades:
        lines += [
            f"    średni niekorzystny poślizg wejścia: {_points(points.entry_slippage)} pkt "
            f"(model: {costs.entry_slippage_points} pkt; poprawa ceny liczona jako 0)",
            f"    największy niekorzystny poślizg wejścia: {_points(points.entry_slippage_max)} pkt",
            f"    spread w chwili decyzji: średni {_points(points.spread)} pkt, "
            f"największy {_points(points.spread_max)} pkt",
        ]
    lines.append(
        f"    zamknięcia stopem: {points.stop_exits} (spread i poślizg przy wyjściu stopem nie są mierzone)"
    )
    if points.trades:
        lines.append(
            "    informacyjnie, nie wpływa na werdykt: spread + poślizg wejścia / model (spread + poślizg): "
            f"{format_ratio(points.combined_ratio)}"
        )
    eur = f"ratio {format_ratio(details.eur_ratio)}" if details.eur_samples else "brak danych"
    lines.append(f"  koszty w EUR (informacyjnie): {eur}")
    lines.append(L1_EUR_CAVEAT)
    if details.excluded_trades:
        lines.append(f"{L1_EXCLUDED_LABEL}{details.excluded_trades}")
    smoke = details.roles.count(gates.ROLE_SMOKE)
    if smoke:
        lines.append(f"  próby smoke (poza liczbą sesji): {smoke}")
    if points.trades < required:
        lines.append(
            f"  uwaga: dowody są skąpe: pomiar kosztów z {points.trades} transakcji przy wymaganych {required}; "
            "to za mało, by oceniać koszty"
        )
    elif points.trades == required:
        lines.append(
            f"  uwaga: dowody nadal skąpe przy n = wymaganej liczbie sesji ({required}); "
            "to wciąż mało, by oceniać koszty"
        )
    lines += [f"  powód: {reason}" for reason in reasons]
    if not reasons:
        lines.append(L1_INSTRUCTION)
    lines.append("")
    return lines


def _points(value: Decimal | None) -> str:
    """Punkty do wydruku z dwoma miejscami, tym samym zaokrągleniem co `format_ratio` (ROUND_HALF_UP); nigdy `-0.00`,
    a wartość niemożliwa do zapisania (poza zakresem precyzji, nieskończona, NaN) to `n/a`."""
    if value is None:
        return "-"
    try:
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return "n/a"
    if rounded.is_nan():
        return "n/a"
    return format(abs(rounded) if rounded.is_zero() else rounded, "f")


# --- polecenie --------------------------------------------------------------------------------------------------


def _parse_day(text: str) -> date:
    if not _DAY_FORMAT.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a calendar date: {text!r}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gwstock.report",
        description="Raport dzienny: wynik netto po kosztach, liczniki błędów, bramka L1, eksport księgi.",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--day", type=_parse_day, metavar="YYYY-MM-DD", help="dzień do pokazania")
    which.add_argument("--all", action="store_true", help="wszystkie dni z zapisami")
    parser.add_argument("--csv", type=Path, metavar="PATH", help="zapisz księgę do pliku CSV")
    parser.add_argument(
        "--journal", type=Path, metavar="PATH", help="plik dziennika (domyślnie $GW_DATA_DIR/journal.sqlite)"
    )
    return parser


def _report(args: argparse.Namespace, journal_path: Path, journal: Journal, strat_sessions: int, costs: CostConfig):
    """Treść raportu (lista linii) i komunikat błędu eksportu CSV (None: brak błędu albo CSV niezamówione)."""
    trades = journal.trades()
    details = gates.l1_details(journal, costs=costs, trades=trades)
    data = _collect(journal, trades, list(details.roles))
    lines = ["Raport gw-stock-engine (pomiar)", f"Dziennik: {journal_path}"]
    csv_error = None
    if args.csv is not None:
        try:
            rows = export_csv(journal, args.csv)
            lines.append(f"CSV: {args.csv} (wiersze danych: {rows})")
        except OSError as exc:
            csv_error = f"CSV export failed: {exc}"
    lines.append("")
    if args.all:
        shown = sorted(day for day, bucket in data.days.items() if day in data.session_days or bucket.has_problems)
    elif args.day is not None:
        shown = [args.day]
    elif data.session_days:
        shown = [max(data.session_days)]
    else:
        problems = [day for day, bucket in data.days.items() if bucket.has_problems]
        shown = [max(problems)] if problems else []
    if not shown:
        lines += ["Dziennik jest pusty: brak transakcji i zapisów dnia.", ""]
    for day in shown:
        lines += _day_lines(journal, day, data.days.get(day, _Day()), trades)
    if data.unreadable:
        lines += [f"UWAGA: {data.unreadable} nieczytelnych zapisów zdarzeń pominięto w liczniku.", ""]
    estimated = _estimated_lines(trades, journal.trade_flags())
    if estimated:
        lines += [*estimated, ""]
    lines += _summary_lines(trades)
    lines += _gate_lines(details, strat_sessions, costs)
    lines.append(STATEMENT)
    return lines, csv_error


def main(argv: Sequence[str] | None = None) -> int:
    """Polecenie `python -m gwstock.report`; zwraca kod wyjścia (patrz docstring modułu)."""
    args = _build_parser().parse_args(argv)
    config_dir = Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR)
    data_dir = Path(os.environ.get(ENV_DATA_DIR) or DEFAULT_DATA_DIR)
    journal_path = args.journal if args.journal is not None else data_dir / JOURNAL_FILE
    try:
        strat, _, costs = load_config(config_dir)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load the configuration from {config_dir}: {exc}", file=sys.stderr)
        return 1
    if strat.fixed_min_size_sessions < 1:
        print("error: fixed_min_size_sessions must be at least 1 for the L1 status", file=sys.stderr)
        return 1
    try:
        journal = open_journal_readonly(journal_path)
        try:
            lines, csv_error = _report(args, journal_path, journal, strat.fixed_min_size_sessions, costs)
        finally:
            journal.close()
    except (JournalError, sqlite3.Error, ArithmeticError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    if csv_error is not None:
        print(f"error: {csv_error}", file=sys.stderr)
        return 1
    return 0
