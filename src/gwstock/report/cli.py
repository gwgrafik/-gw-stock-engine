"""Raport dzienny `python -m gwstock.report` (spec §10, §11): wynik netto po kosztach, liczniki błędów, bramka L1, CSV.

Opcje: `--day YYYY-MM-DD` (domyślnie ostatni dzień z transakcją lub decyzją, a gdy takich nie ma, ostatni dzień
z zapisem błędu), `--all` (wszystkie dni), `--csv PATH` (eksport księgi, 19 kolumn), `--csv-eur PATH` (osobny eksport
walut i kwot w EUR każdej transakcji), `--journal PATH` (domyślnie `$GW_DATA_DIR/journal.sqlite`, `GW_DATA_DIR`
domyślnie `data`; konfiguracja z `$GW_CONFIG_DIR`, domyślnie `config`).
Kody wyjścia: 0 raport wypisany, 1 błąd (brak/uszkodzony dziennik, brak konfiguracji, nieudany eksport CSV,
wartości poza zakresem arytmetyki; to ostatnia deska ratunku, a pojedynczy dziwny zapis nie przerywa raportu:
liczby, których nie da się wydrukować, wychodzą jako `n/a`), 2 błąd użycia.

Pasma (projekt wielu pasm, zadanie 15). Dziennik jednego pasma w walucie rachunku (dotychczasowy DE40) daje raport
IDENTYCZNY bajt w bajt jak przed pasmami (pilnuje tego `tests/unit/test_report_single_lane_golden.py`). Gdy dziennik
ma więcej niż jedno pasmo (instrument), transakcję w walucie obcej albo decyzję pasma dodatkowego (`LANE_DECISION`),
raport ma osobne sekcje pasm: w każdej kwoty z walutą pasma (wiersze księgi instrumentu w USD mają `net_pnl` w USD, nie
w EUR), potem sekcję portfela, w której wynik w EUR to wyłącznie wiersze w EUR i kwoty EUR z rozliczeń brokera
(`Journal.trade_fx`); transakcja bez rozliczenia jest OCZEKUJĄCA i nie wchodzi do żadnej sumy w EUR ani do porównania
z licznikiem dziennika. Bramka L1 jest per pasmo (jego transakcje, jego wymagana liczba sesji i model kosztów z
`config/lanes.toml`), a incydenty portfela liczą się w każdej. Zdarzenia wspólne (kill switch, blokady logowania,
nierozstrzygnięte wejścia) są w jednym bloku portfela.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.config import CostConfig, StrategyConfig, load_config, load_lanes
from gwstock.journal import Journal, JournalError, TradeRecord
from gwstock.report import gates
from gwstock.report.gates import L1Details, format_ratio, read_events
from gwstock.report.ledger import (
    ACCOUNT_CURRENCY, BookedTrade, EurSummary, decimal_text, eur_summary, exact_sum, export_csv, export_eur_csv,
    local_day, read_ledger, trade_day, trades_summary,
)  # fmt: skip

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
L1_HEAD = "Bramka L1 (spec §11)"
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
BENIGN_EVENTS = frozenset({"TRADE_BOOKED", "CLOSURE_BOOKED", "TRADE_SETTLED"})  # SETTLED: rozliczenie fazy 2 w EUR
ESTIMATED_FLAG = "ESTIMATED"  # znacznik księgi szacunku właściciela (`Journal.trade_flags`)
ROLE_ESTIMATED = "estimated"  # rola szacunku w raporcie: poza bramką L1 (ani sesja, ani próbka kosztu)
L1_ESTIMATED_LABEL = "  wykluczone z bramki szacunki właściciela (ESTIMATED): "
EVENT_DECISION = "DECISION"  # decyzja pasma v1 (bez instrumentu: pasmo v1)
EVENT_LANE_DECISION = "LANE_DECISION"  # decyzja pasma dodatkowego (pole `epic`); jak `engine_lanes.LANE_DECISION`

# Układ per pasmo (dziennik z więcej niż jednym pasmem, walutą obcą albo decyzją pasma dodatkowego).
LANES_LINE = "Pasma w dzienniku: {}"
LANE_HEADER = "Pasmo {epic} (księga w {currency})"
LANE_NO_TRADES = "brak transakcji"
UNKNOWN_CURRENCY = "[waluta instrumentu]"  # waluta obca o nieznanej nazwie (brak wpisu w config/lanes.toml): NIGDY EUR
PORTFOLIO_HEADER = "Portfel (wspólny, wyniki w EUR)"
SHARED_HEADER = "Błędy i zdarzenia (wspólne dla pasm):"
LANES_UNREADABLE = "UWAGA: nie wczytano pasm z config/lanes.toml ({}); waluty i bramki L1 pasm dodatkowych niedostępne"
MIXED_CURRENCY = (
    "  UWAGA: transakcje tego pasma są księgowane w różnych walutach (EUR i waluta instrumentu): "
    "sumy w walucie księgi pominięte, kwoty w EUR w sekcji portfela"
)
PENDING_NOTE = "kwoty w EUR nieznane: poza sumami w EUR i poza licznikiem dziennika"
CSV_FOREIGN_NOTE = (
    "UWAGA: w CSV kolumny kwot wierszy pasm w walucie obcej ({foreign} z {total} wierszy) są w walucie instrumentu, "
    "nie w EUR; kwoty w EUR: --csv-eur"
)
FX_UNREADABLE_NOTE = "UWAGA: {} nieczytelnych rozliczeń w EUR (trade_fx) potraktowano jak oczekujące na rozliczenie."
L1_LANE_TAIL = (
    "(transakcje tego pasma; rozbieżności i odrzucenia z całego dziennika; "
    "ocena wyjaśnień należy do właściciela)"
)
L1_LANE_INSTRUCTION = (
    "Aby dopuścić wielkość według reguł pasma {epic} utwórz plik bramki wielkości tego pasma (nazwę pliku wskaże "
    "właściciel, projekt wielu pasm, pytanie 7; kod go nigdy nie tworzy). To informacja dla właściciela, nie zgoda: "
    "plik tworzysz dopiero po własnym przeglądzie raportu (runbook, sekcja 7)."
)
L1_NOT_ASSESSED = (
    "Bramka L1 (spec §11), pasmo {epic}: NIE OCENIONO (brak wpisu tego pasma w config/lanes.toml: "
    "nie znam wymaganej liczby sesji ani modelu kosztów)"
)
L1_LANE_CAVEAT = (
    "  uwaga: koszty w {currency} są zaokrąglane do 0,01 {currency} w księdze, więc ratio jest zgrubne dla małych "
    "kwot; poślizg stopu przy wyjściu NIE jest mierzony"
)
ESTIMATED_WARNING = (
    "UWAGA: transakcja z {day} (wyjście {time} czasu Europe/Berlin, {reason}, netto {net}) zaksięgowana przez "
    "właściciela jako SZACUNEK (ostrożny szacunek straty, nie górna granica); sprawdź wynik w Capital.com"
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
class _LaneDay:
    """Dzień jednego pasma (instrumentu): jego wiersze księgi, tryb z zapisów decyzji i blokady bramki pasma."""

    rows: list[BookedTrade] = field(default_factory=list)
    smoke_trades: int = 0
    modes: list[str] = field(default_factory=list)
    gate_blocks: int = 0
    gate_reasons: Counter = field(default_factory=Counter)
    smoke_blocks: int = 0


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
    rows: list[BookedTrade] = field(default_factory=list)
    lanes: dict[str, _LaneDay] = field(default_factory=dict)  # tylko do układu per pasmo; stary układ go nie czyta

    @property
    def has_problems(self) -> bool:
        return bool(
            self.gate_blocks or self.smoke_blocks or self.unresolved or self.kills or self.lockouts or self.other
        )

    def lane(self, epic: str) -> _LaneDay:
        return self.lanes.setdefault(epic, _LaneDay())


@dataclass
class _Data:
    days: dict[date, _Day]
    session_days: set[date]
    unreadable: int
    epics: tuple[str, ...] = ()  # pasma widoczne w dzienniku: pasmo v1 pierwsze, reszta alfabetycznie
    multi: bool = False  # układ per pasmo (patrz docstring modułu)


def _local_day(event) -> date:
    return local_day(event.ts)


def _block_epic(data: dict) -> str | None:
    """Instrument zapisu `gate_block`: z kandydata (`candidate.epic`) albo z pola `epic` (blokady walutowe) albo None."""
    candidate = data.get("candidate")
    epic = candidate.get("epic") if isinstance(candidate, dict) else None
    if not (isinstance(epic, str) and epic):
        epic = data.get("epic")
    return epic if isinstance(epic, str) and epic else None


def _collect(journal: Journal, rows: list[BookedTrade], v1_epic: str) -> _Data:
    days: dict[date, _Day] = {}
    session_days: set[date] = set()
    epics: set[str] = set()
    lane_decisions = False

    def day_of(day: date) -> _Day:
        return days.setdefault(day, _Day())

    for row in rows:
        day = trade_day(row.trade)
        day_of(day).trades.append(row.trade)
        day_of(day).rows.append(row)
        day_of(day).lane(row.trade.instrument).rows.append(row)
        epics.add(row.trade.instrument)
        session_days.add(day)
    unreadable = 0

    for event, data in read_events(journal, gates.SOURCE_DECISION):
        if data is None:
            unreadable += 1
            continue
        code = data.get("event")
        if code not in (EVENT_DECISION, EVENT_LANE_DECISION):
            continue
        if not (isinstance(data.get("day"), str) and isinstance(data.get("mode"), str)):
            continue
        epic = v1_epic if code == EVENT_DECISION else data.get("epic")
        if not (isinstance(epic, str) and epic):
            continue
        try:
            day = date.fromisoformat(data["day"])
        except ValueError:
            continue
        session_days.add(day)
        epics.add(epic)
        bucket = day_of(day)
        if code == EVENT_DECISION:  # tryb dnia pasma v1 (stary układ czyta tylko te zapisy)
            if data["mode"] not in bucket.modes:
                bucket.modes.append(data["mode"])
        else:
            lane_decisions = True
        if data["mode"] not in bucket.lane(epic).modes:
            bucket.lane(epic).modes.append(data["mode"])

    for event, data in read_events(journal, gates.SOURCE_GATE_BLOCK):
        if data is None:
            unreadable += 1
            continue
        bucket = day_of(_local_day(event))
        epic = _block_epic(data) or v1_epic  # zapis bez instrumentu pochodzi z pasma v1
        epics.add(epic)
        lane = bucket.lane(epic)
        if data.get("smoke") is True:
            bucket.smoke_blocks += 1
            lane.smoke_blocks += 1
            continue
        bucket.gate_blocks += 1
        lane.gate_blocks += 1
        reasons = data.get("reasons")
        for reason in reasons if isinstance(reasons, list) else []:
            if isinstance(reason, str):
                bucket.gate_reasons[reason] += 1
                lane.gate_reasons[reason] += 1

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
    multi = len(epics) > 1 or lane_decisions or any(row.foreign for row in rows)
    ordered = tuple(sorted(epics, key=lambda epic: (epic != v1_epic, epic)))
    return _Data(days, session_days, unreadable, ordered, multi)


def _count_smoke(data: _Data, rows: list[BookedTrade], roles: list[str]) -> None:
    """Próby smoke (rola z bramki L1) w liczniku dnia i dnia pasma."""
    for row, role in zip(rows, roles):
        if role == gates.ROLE_SMOKE:
            bucket = data.days[trade_day(row.trade)]
            bucket.smoke_trades += 1
            bucket.lane(row.trade.instrument).smoke_trades += 1


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


def _estimated_lines(booked: list[BookedTrade], currency_of: Callable[[BookedTrade], str] | None = None) -> list[str]:
    """Uwaga dla każdej transakcji zaksięgowanej przez właściciela jako SZACUNEK (`resolve-lost-trade`): jej wynik to
    ostrożny szacunek straty, nie pomiar. Bez identyfikatora deala (raport ich nie drukuje): dzień, godzina, wynik.

    `currency_of` (układ per pasmo): nazwa waluty księgi wiersza, dopisywana do kwoty (waluta obca to nie EUR)."""
    lines = []
    for row in booked:
        trade = row.trade
        if ESTIMATED_FLAG not in (row.flags or "").split():
            continue
        local = _CALENDAR.to_local(trade.exit_ts)
        net = _d(trade.net_pnl) if currency_of is None else f"{_d(trade.net_pnl)} {currency_of(row)}"
        lines.append(ESTIMATED_WARNING.format(
            day=local.date().isoformat(), time=f"{local:%H:%M}", reason=trade.exit_reason, net=net,
        ))  # fmt: skip
    return lines


def _days(days: tuple[date, ...]) -> str:
    return "; dni: " + ", ".join(day.isoformat() for day in sorted(set(days))) if days else ""


def _gate_lines(
    details: L1Details, required: int, costs: CostConfig, *, estimates: int = 0, head: str = L1_HEAD,
    tail: str = L1_VERDICT_TAIL, currency: str | None = None, instruction: str = L1_INSTRUCTION,
) -> list[str]:  # fmt: skip
    """Blok bramki L1. Domyślne argumenty dają dotychczasowy tekst (jedno pasmo w EUR); układ per pasmo podaje `head`,
    `tail`, nazwę waluty księgi pasma (`currency`, tylko do opisu kosztów w walucie księgi) i `instruction`."""
    gate, points = details.gate, details.points
    reasons = gate.reasons(required)
    missing = max(0, required - gate.sessions_at_min_size)
    lines = [
        f"{head}: {'PASS' if gate.passed(required) else 'NOT YET'} {tail}",
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
    lines.append(f"  koszty w {currency or ACCOUNT_CURRENCY} (informacyjnie): {eur}")
    lines.append(L1_EUR_CAVEAT if currency in (None, ACCOUNT_CURRENCY) else L1_LANE_CAVEAT.format(currency=currency))
    if details.excluded_trades:
        lines.append(f"{L1_EXCLUDED_LABEL}{details.excluded_trades}")
    if estimates:
        lines.append(f"{L1_ESTIMATED_LABEL}{estimates}")
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
        lines.append(instruction)
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


# --- układ per pasmo ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _LaneConf:
    """Co raport wie o pasmie z konfiguracji: waluta księgi, wymagana liczba sesji L1 i model kosztów."""

    currency: str
    required: int
    costs: CostConfig


@dataclass(frozen=True)
class _Lane:
    """Pasmo (instrument) w układzie per pasmo.

    - `currency`: kod waluty księgi (EUR dla pasma w walucie rachunku); None: waluta obca bez wpisu w `lanes.toml` albo
      pasmo bez transakcji i bez wpisu; `foreign`: księga pasma w walucie obcej (transakcje dwufazowe albo wpis);
    - `mixed`: wiersze pasma w różnych walutach (nie powinno się zdarzyć): jego sumy w walucie księgi nie są drukowane;
    - `required`, `costs`: bramka L1 pasma (None: pasma nie ma w konfiguracji, bramki nie oceniamy).
    """

    epic: str
    currency: str | None
    foreign: bool
    mixed: bool
    has_rows: bool
    v1: bool
    required: int | None
    costs: CostConfig | None

    @property
    def label(self) -> str:
        """Nazwa waluty do opisu kwot; waluta obca o nieznanej nazwie NIGDY nie udaje EUR."""
        return self.currency or UNKNOWN_CURRENCY

    @property
    def known(self) -> bool:
        return self.foreign or self.has_rows or self.currency is not None

    @property
    def header(self) -> str:
        if self.known:
            return LANE_HEADER.format(epic=self.epic, currency=self.label)
        return f"Pasmo {self.epic} ({LANE_NO_TRADES})"

    @property
    def tag(self) -> str:
        return f"{self.epic} (księga w {self.label})" if self.known else f"{self.epic} ({LANE_NO_TRADES})"


def _load_lane_confs(
    config_dir: Path, strat: StrategyConfig, costs: CostConfig
) -> tuple[dict[str, _LaneConf], str | None]:  # fmt: skip
    """Pasma z `config/lanes.toml` (bez pliku: tylko pasmo v1) i komunikat błędu, gdy plik jest nieczytelny.

    Nieczytelny plik nie przerywa raportu: pasma dodatkowe zostają bez nazwy waluty i bez oceny L1 (nic nie zgadujemy).
    """
    confs = {strat.instrument: _LaneConf(ACCOUNT_CURRENCY, strat.fixed_min_size_sessions, costs)}
    try:
        loaded = load_lanes(config_dir)
    except (OSError, ValueError) as exc:
        return confs, str(exc)
    for lane in loaded or ():
        confs[lane.epic] = _LaneConf(lane.cfg.currency, lane.bar.fixed_min_size_sessions, lane.costs)
    return confs, None


def _build_lanes(
    data: _Data, rows: list[BookedTrade], v1_epic: str, confs: dict[str, _LaneConf]
) -> list[_Lane]:  # fmt: skip
    lanes = []
    for epic in data.epics:
        lane_rows = [row for row in rows if row.trade.instrument == epic]
        conf = confs.get(epic)
        foreign = any(row.foreign for row in lane_rows) or (conf is not None and conf.currency != ACCOUNT_CURRENCY)
        mixed = foreign and any(not row.foreign for row in lane_rows)
        if foreign:
            currency = None if conf is None else conf.currency
        else:
            currency = ACCOUNT_CURRENCY if lane_rows or conf is not None or epic == v1_epic else None
        lanes.append(_Lane(
            epic, currency, foreign, mixed, bool(lane_rows), epic == v1_epic,
            None if conf is None else conf.required, None if conf is None else conf.costs,
        ))  # fmt: skip
    return lanes


def _kills_text(bucket: _Day) -> str:
    return "; ".join(
        f"{' '.join(part for part in key if part)} x{count}" for key, count in sorted(bucket.kills.items())
    ) or "brak"  # fmt: skip


def _gate_blocks_text(gate_blocks: int, gate_reasons: Counter, smoke_blocks: int) -> str:
    text = f"{gate_blocks}" + (f" ({_counts(gate_reasons)})" if gate_reasons else "")
    if smoke_blocks:
        text += f" + próby smoke: {smoke_blocks}"
    return text


def _settlement_text(rows: list[BookedTrade], *, prefix: str) -> str:
    """Kwoty w EUR wierszy pasma w walucie obcej: rozliczone (suma, liczba, w tym szacunek z kursu) i oczekujące.

    Szacunek z kursu (`ESTIMATED_FX`) jest nazwany: to nie kwota z historii brokera."""
    settled = [row for row in rows if row.fx is not None]
    pending = sum(1 for row in rows if row.foreign and row.fx is None)
    total = f"{_d(exact_sum(row.fx.net_eur for row in settled))} {ACCOUNT_CURRENCY}"
    estimated = sum(1 for row in settled if row.estimated_fx)
    return (
        f"{prefix}: {total} (transakcje: {len(settled)}, w tym szacunek z kursu: {estimated}); "
        f"czeka na rozliczenie: {pending}"
    )


def _estimate_text(summary: EurSummary, *, indent: str) -> str:
    return (
        f"{indent}w tym szacunek z kursu brokera (ESTIMATED_FX): {summary.estimated} transakcji, "
        f"{_d(summary.estimated_net)} EUR (nie kwota z historii brokera)"
    )


def _lane_day_lines(lane: _Lane, bucket: _LaneDay, lane_rows: list[BookedTrade], day: date) -> list[str]:
    summary = trades_summary(row.trade for row in bucket.rows)
    smoke_note = f" (w tym próby smoke: {bucket.smoke_trades})" if bucket.smoke_trades else ""
    lines = [
        lane.header,
        f"  Tryb: {', '.join(bucket.modes) if bucket.modes else NO_DECISION}",
        f"  Transakcje: {summary['count']}{smoke_note}",
    ]
    if lane.has_rows:
        if lane.mixed:
            lines.append(MIXED_CURRENCY)
        else:
            money = lane.label
            cumulative = exact_sum(row.trade.net_pnl for row in lane_rows if trade_day(row.trade) <= day)
            lines += [
                f"  Brutto: {_d(summary['gross'])} {money}",
                f"  Koszt spreadu: {_d(summary['spread_cost'])} {money}",
                f"  Poślizg (model z konfiguracji): {_d(summary['slippage_penalty'])} {money}",
                f"  Finansowanie: {_d(summary['financing'])} {money}",
                f"  Opłaty: {_d(summary['fees'])} {money}",
                f"  Netto: {_d(summary['net'])} {money}",
                f"  Netto narastająco: {_d(cumulative)} {money}",
            ]
        if lane.foreign:
            lines.append(_settlement_text(bucket.rows, prefix="  Kwota w EUR z rozliczeń"))
    blocks = _gate_blocks_text(bucket.gate_blocks, bucket.gate_reasons, bucket.smoke_blocks)
    lines.append(f"  blokady bramki ryzyka: {blocks}")
    return lines


def _portfolio_day_lines(journal: Journal, day: date, bucket: _Day, total: EurSummary) -> list[str]:
    """Wynik portfela w EUR (wiersze w EUR i rozliczenia; oczekujące nie wchodzą) i zgodność z licznikiem dziennika."""
    today = eur_summary(bucket.rows)
    cumulative = exact_sum(net for net_day, net in total.per_day.items() if net_day <= day)
    realized_day, realized_total = journal.realized_pnl(day), journal.realized_pnl_total()
    lines = [
        PORTFOLIO_HEADER,
        f"  Wynik netto w EUR: dzień {_d(today.net)} EUR (pasma w EUR: {_d(today.account_net)} EUR, "
        f"rozliczone z walut obcych: {_d(today.settled_net)} EUR)",
        f"  Wynik netto w EUR narastająco: {_d(cumulative)} EUR",
    ]
    if today.estimated:
        lines.append(_estimate_text(today, indent="  "))
    if total.pending:
        lines.append(f"  Czeka na rozliczenie w EUR od początku dziennika: {total.pending} ({PENDING_NOTE})")
    realized = (
        f"  Wynik zrealizowany wg dziennika: dzień {_d(realized_day)} EUR, łącznie od początku {_d(realized_total)} EUR"
    )
    if realized_day != today.net or realized_total != total.net:
        realized += f" NIEZGODNE z księgą (księga: dzień {_d(today.net)} EUR, łącznie {_d(total.net)} EUR)"
    lines.append(realized)
    return lines


def _multi_day_lines(
    journal: Journal, day: date, bucket: _Day, lanes: list[_Lane], rows_of: dict[str, list[BookedTrade]],
    total: EurSummary,
) -> list[str]:  # fmt: skip
    lines = [DAY_HEADER.format(day.isoformat())]
    for lane in lanes:
        lines += _lane_day_lines(lane, bucket.lanes.get(lane.epic, _LaneDay()), rows_of[lane.epic], day)
    lines += _portfolio_day_lines(journal, day, bucket, total)
    lines += [
        SHARED_HEADER,
        f"  ENTRY_UNRESOLVED: {bucket.unresolved}",
        f"  kill switch: {_kills_text(bucket)}",
        f"  blokady logowania ({LOGIN_LOCKOUT}): {bucket.lockouts}",
    ]
    if bucket.other:
        lines.append("  inne zdarzenia: " + ", ".join(f"{code} x{n}" for code, n in sorted(bucket.other.items())))
    lines.append("")
    return lines


def _multi_summary_lines(lanes: list[_Lane], rows_of: dict[str, list[BookedTrade]], total: EurSummary) -> list[str]:
    lines = ["Podsumowanie od początku dziennika:"]
    for lane in lanes:
        lane_rows = rows_of[lane.epic]
        lines.append(f"{lane.header}:")
        if not lane_rows:
            lines.append("  brak transakcji")
            continue
        if lane.mixed:
            lines.append(MIXED_CURRENCY)
        else:
            s, money = trades_summary(row.trade for row in lane_rows), lane.label
            counts = f"netto dodatnie {s['wins']}, ujemne {s['losses']}, zerowe {s['breakeven']}"
            lines += [
                f"  transakcje: {s['count']} ({counts})",
                f"  brutto: {_d(s['gross'])} {money}",
                f"  koszty: spread {_d(s['spread_cost'])} {money}, "
                f"poślizg (model) {_d(s['slippage_penalty'])} {money}, "
                f"finansowanie {_d(s['financing'])} {money}, opłaty {_d(s['fees'])} {money}",
                f"  netto: {_d(s['net'])} {money}",
            ]
            if s["worst_day"] is not None:
                worst_day, worst_net = s["worst_day"]
                lines.append(f"  najgorszy dzień: {worst_day.isoformat()} ({_d(worst_net)} {money})")
        if lane.foreign:
            lines.append(_settlement_text(lane_rows, prefix="  w EUR z rozliczeń"))
    lines += [
        "Portfel (EUR):",
        f"  netto w EUR: {_d(total.net)} EUR (pasma w EUR: {_d(total.account_net)} EUR, "
        f"rozliczone z walut obcych: {_d(total.settled_net)} EUR)",
    ]
    if total.estimated:
        lines.append(_estimate_text(total, indent="  "))
    if total.pending:
        lines.append(f"  czeka na rozliczenie w EUR: {total.pending} (kwota w EUR nieznana)")
    if total.worst_day is not None:
        worst_day, worst_net = total.worst_day
        lines.append(f"  najgorszy dzień w EUR: {worst_day.isoformat()} ({_d(worst_net)} EUR)")
    lines.append("")
    return lines


def _lane_gate_lines(lane: _Lane, details: L1Details, estimates: int) -> list[str]:
    """Bramka L1 jednego pasma: jego transakcje, jego wymagana liczba sesji i model kosztów; bez wpisu w konfiguracji
    pasma nie oceniamy (wymagań ani kosztów nie zgadujemy z pasma v1)."""
    if lane.required is None or lane.costs is None:
        return [L1_NOT_ASSESSED.format(epic=lane.epic), ""]
    instruction = L1_INSTRUCTION if lane.v1 else L1_LANE_INSTRUCTION.format(epic=lane.epic)
    return _gate_lines(
        details, lane.required, lane.costs, estimates=estimates, head=f"{L1_HEAD}, pasmo {lane.epic}",
        tail=L1_LANE_TAIL, currency=lane.label, instruction=instruction,
    )  # fmt: skip


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
        "--csv-eur", type=Path, metavar="PATH", help="zapisz waluty i kwoty w EUR każdej transakcji do osobnego CSV"
    )
    parser.add_argument(
        "--journal", type=Path, metavar="PATH", help="plik dziennika (domyślnie $GW_DATA_DIR/journal.sqlite)"
    )
    return parser


def _report(
    args: argparse.Namespace, journal_path: Path, journal: Journal, strat: StrategyConfig, costs: CostConfig,
    config_dir: Path,
):  # fmt: skip
    """Treść raportu (lista linii) i komunikaty błędów eksportu CSV (pusta lista: brak błędów albo CSV niezamówione)."""
    rows = read_ledger(journal)  # księga z walutą: zbiór transakcji jedną instrukcją (zgodny przy piszącym silniku)
    trades = [row.trade for row in rows]
    estimated = [ESTIMATED_FLAG in (row.flags or "").split() for row in rows]
    data = _collect(journal, rows, strat.instrument)
    lanes: list[_Lane] = []
    confs: dict[str, _LaneConf] = {}
    lanes_error: str | None = None
    details_by_epic: dict[str, L1Details] = {}
    # szacunek właściciela to nie pomiar: bramka L1 (sesje, próbki kosztu) liczy wyłącznie pozostałe transakcje
    if not data.multi:  # jedno pasmo w walucie rachunku: dotychczasowy raport, bajt w bajt
        details = gates.l1_details(journal, costs=costs, trades=[t for t, e in zip(trades, estimated) if not e])
        measured_roles = iter(details.roles)
        roles = [ROLE_ESTIMATED if e else next(measured_roles) for e in estimated]
    else:  # bramka L1 osobno dla każdego pasma: jego transakcje i model kosztów (incydenty portfela: cały dziennik)
        confs, lanes_error = _load_lane_confs(config_dir, strat, costs)
        lanes = _build_lanes(data, rows, strat.instrument, confs)
        role_iters = {}
        for lane in lanes:
            lane_trades = [t for t, e in zip(trades, estimated) if not e and t.instrument == lane.epic]
            lane_costs = costs if lane.costs is None else lane.costs  # bez wpisu pasma: tylko role (wyniku nie drukujemy)
            details_by_epic[lane.epic] = gates.l1_details(journal, costs=lane_costs, trades=lane_trades)
            role_iters[lane.epic] = iter(details_by_epic[lane.epic].roles)
        roles = [ROLE_ESTIMATED if e else next(role_iters[t.instrument]) for t, e in zip(trades, estimated)]
    _count_smoke(data, rows, roles)
    lines = ["Raport gw-stock-engine (pomiar)", f"Dziennik: {journal_path}"]
    csv_errors = []
    if args.csv is not None:
        try:
            exported = export_csv(journal, args.csv)
            lines.append(f"CSV: {args.csv} (wiersze danych: {exported})")
            foreign = sum(1 for row in rows if row.foreign)
            if foreign:
                lines.append(CSV_FOREIGN_NOTE.format(foreign=foreign, total=exported))
        except OSError as exc:
            csv_errors.append(f"CSV export failed: {exc}")
    if args.csv_eur is not None:
        try:
            exported = export_eur_csv(journal, args.csv_eur, {epic: conf.currency for epic, conf in confs.items()})
            lines.append(f"CSV (kwoty w EUR): {args.csv_eur} (wiersze danych: {exported})")
        except OSError as exc:
            csv_errors.append(f"CSV export failed: {exc}")
    if data.multi:
        if lanes_error is not None:
            lines.append(LANES_UNREADABLE.format(lanes_error))
        lines.append(LANES_LINE.format(", ".join(lane.tag for lane in lanes)))
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
    total = eur_summary(rows)
    rows_of = {lane.epic: [row for row in rows if row.trade.instrument == lane.epic] for lane in lanes}
    for day in shown:
        bucket = data.days.get(day, _Day())
        if data.multi:
            lines += _multi_day_lines(journal, day, bucket, lanes, rows_of, total)
        else:
            lines += _day_lines(journal, day, bucket, trades)
    if data.unreadable:
        lines += [f"UWAGA: {data.unreadable} nieczytelnych zapisów zdarzeń pominięto w liczniku.", ""]
    unreadable_fx = sum(1 for row in rows if row.fx_unreadable)
    if unreadable_fx:
        lines += [FX_UNREADABLE_NOTE.format(unreadable_fx), ""]
    if data.multi:
        label = {lane.epic: lane.label for lane in lanes}
        warnings = _estimated_lines(
            rows, lambda row: label[row.trade.instrument] if row.foreign else ACCOUNT_CURRENCY
        )  # fmt: skip
    else:
        warnings = _estimated_lines(rows)
    if warnings:
        lines += [*warnings, ""]
    if data.multi:
        lines += _multi_summary_lines(lanes, rows_of, total)
        for lane in lanes:
            lane_estimates = sum(1 for row, e in zip(rows, estimated) if e and row.trade.instrument == lane.epic)
            lines += _lane_gate_lines(lane, details_by_epic[lane.epic], lane_estimates)
    else:
        lines += _summary_lines(trades)
        lines += _gate_lines(details, strat.fixed_min_size_sessions, costs, estimates=sum(estimated))
    lines.append(STATEMENT)
    return lines, csv_errors


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
            lines, csv_errors = _report(args, journal_path, journal, strat, costs, config_dir)
        finally:
            journal.close()
    except (JournalError, sqlite3.Error, ArithmeticError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    for csv_error in csv_errors:
        print(f"error: {csv_error}", file=sys.stderr)
    return 1 if csv_errors else 0
