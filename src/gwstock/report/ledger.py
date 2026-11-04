"""Księga transakcji (spec §10): eksport do CSV i dokładne podsumowanie.

CSV: nagłówek dokładnie jak w specyfikacji (19 pól `TradeRecord` w tej kolejności), jeden wiersz na transakcję
w kolejności księgowania; `Decimal` jako zwykły napis (`format(d, "f")`, bez notacji naukowej i bez float), czasy
ISO-8601 w UTC z `Z`, kierunek jako wartość (`BUY`/`SELL`); plik UTF-8, końce linii `\\n`, uprawnienia 600, zapis
atomowy.

Sumy są dokładne: kontekst o precyzji 100 cyfr z pułapką na zaokrąglenie zamiast cichej utraty cyfr.
Dzień transakcji to dzień LOKALNY (Europe/Berlin) czasu zamknięcia, jak `exit_day` w księgowaniu silnika.

Waluty (projekt wielu pasm §7.4). Wiersz księgi instrumentu w walucie rachunku (DE40, EUR) ma `net_pnl` w EUR. Wiersz
instrumentu w walucie obcej (US500, USD) ma wszystkie kwoty w WALUCIE INSTRUMENTU, a kwota w EUR (ta, która trafia do
licznika `realized_pnl`) leży osobno w `Journal.trade_fx(deal_id)` po rozliczeniu (faza 2); do tego czasu transakcja
jest OCZEKUJĄCA (`pnl_pending`) i jej kwota w EUR jest nieznana. Dlatego sumowanie `journal.trades()` wprost mieszałoby
waluty. Zasada: `trades_summary` sumuje wiersze JEDNEGO pasma (jedna waluta księgi); suma w EUR przez wiersze wielu pasm
to wyłącznie `eur_summary` (EUR z wiersza w EUR albo z rozliczenia, oczekujące osobno, nigdy kwota w USD). Eksport CSV
(19 kolumn spec §10) zostaje bez zmian; waluta i kwota w EUR każdej transakcji są w osobnym `export_eur_csv`.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.journal import Journal, JournalCorruptError, TradeFx, TradeRecord

__all__ = [
    "LEDGER_COLUMNS", "EUR_COLUMNS", "ACCOUNT_CURRENCY", "ESTIMATED_FX", "BookedTrade", "EurSummary", "export_csv",
    "export_eur_csv", "read_ledger", "eur_summary", "trades_summary", "exact_sum", "trade_day", "local_day",
    "decimal_text",
]  # fmt: skip

ACCOUNT_CURRENCY = "EUR"  # waluta rachunku (jak `app.ACCOUNT_CURRENCY`; raport nie ładuje silnika, pilnuje test)
EUR_COLUMNS = (
    "instrument", "book_currency", "exit_ts", "exit_reason", "net_pnl_book", "net_pnl_eur", "eur_status", "eur_source",
    "eur_rate",
)  # fmt: skip
ESTIMATED_FX = "ESTIMATED_FX"  # rozliczenie z szacunku kursu brokera (jak `fx.ESTIMATED_FX`), nie kwota z historii
STATUS_ACCOUNT = "ACCOUNT_CURRENCY"  # wiersz księgi jest już w EUR
STATUS_SETTLED = "SETTLED"  # waluta obca, kwota EUR rozliczona (`Journal.trade_fx`)
STATUS_PENDING = "PENDING"  # waluta obca, kwota EUR jeszcze nieznana (`pnl_pending`) albo nieczytelna

LEDGER_COLUMNS = (
    "instrument", "side", "size", "decision_bid", "decision_ask", "entry_fill", "exit_fill", "gross_pnl",
    "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl", "signal_ts", "submit_ts", "confirm_ts",
    "exit_ts", "latency_ms", "exit_reason",
)  # fmt: skip

_EXACT = Context(prec=100, traps=[InvalidOperation, Overflow, Inexact])
_CALENDAR = ExchangeCalendar(XETRA)


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Dokładna suma `Decimal` (pusta: `Decimal(0)`); zaokrąglenie byłoby wyjątkiem, nie cichą utratą cyfr."""
    total = Decimal(0)
    for value in values:
        total = _EXACT.add(total, value)
    return total


def local_day(moment: datetime) -> date:
    """Dzień lokalny giełdy (Europe/Berlin) chwili UTC."""
    return _CALENDAR.to_local(moment).date()


def trade_day(trade: TradeRecord) -> date:
    """Dzień sesji (czas lokalny giełdy) czasu zamknięcia transakcji."""
    return local_day(trade.exit_ts)


def decimal_text(value: Decimal) -> str:
    """Zwykły napis `Decimal` (bez notacji naukowej, cyfry bez zmian); zero bez znaku: `-0.00` to `0.00`."""
    return format(abs(value) if value.is_zero() else value, "f")


# --- CSV ---------------------------------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if hasattr(value, "value"):  # enum (Side)
        return str(value.value)
    return str(value)


def _atomic_write_600(path: Path, data: bytes) -> None:
    """Zapis atomowy z uprawnieniami 600: plik tymczasowy w tym samym katalogu, fsync, `os.replace`."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def export_csv(journal: Journal, path: Path) -> int:
    """Zapisuje całą księgę do `path` (CSV, patrz docstring modułu); zwraca liczbę wierszy danych.

    Istniejący plik jest podmieniany atomowo; katalog w miejscu pliku to `IsADirectoryError`; brak katalogu
    nadrzędnego to `FileNotFoundError` (nie tworzymy go po cichu).
    """
    path = Path(path)
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory; give a file name")
    trades = journal.trades()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(LEDGER_COLUMNS)
    for trade in trades:
        writer.writerow([_cell(getattr(trade, name)) for name in LEDGER_COLUMNS])
    _atomic_write_600(path, buffer.getvalue().encode("utf-8"))
    return len(trades)


# --- księga z walutą ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BookedTrade:
    """Wiersz księgi z tym, co potrzebne do sum w walucie: znaczniki (`Journal.trade_flags`) i stan kwoty w EUR.

    - `foreign`: instrument w walucie obcej (księgowanie dwufazowe: `pnl_pending` albo `trade_fx` w dzienniku); jego
      `trade.*` są w walucie INSTRUMENTU, nie w EUR;
    - `fx`: rozliczenie w EUR (`Journal.trade_fx`), tylko dla `foreign` po fazie 2;
    - `pending`: `foreign` bez kwoty w EUR (czeka na rozliczenie albo rozliczenie jest nieczytelne: `fx_unreadable`).
    Identyfikator deala nie jest przechowywany: raport go nie drukuje.
    """

    trade: TradeRecord
    flags: str | None = None
    fx: TradeFx | None = None
    foreign: bool = False
    pending: bool = False
    fx_unreadable: bool = False

    @property
    def net_eur(self) -> Decimal | None:
        """Wynik netto w EUR: `net_pnl` wiersza w EUR, kwota z rozliczenia dla waluty obcej, a przed rozliczeniem None
        (kwota nieznana; NIGDY `net_pnl` wiersza w walucie instrumentu)."""
        if not self.foreign:
            return self.trade.net_pnl
        return None if self.fx is None else self.fx.net_eur

    @property
    def estimated_fx(self) -> bool:
        """Kwota w EUR pochodzi z SZACUNKU kursu brokera (`ESTIMATED_FX`), nie z rozliczenia w historii brokera."""
        return self.fx is not None and self.fx.source == ESTIMATED_FX

    @property
    def eur_status(self) -> str:
        if not self.foreign:
            return STATUS_ACCOUNT
        return STATUS_PENDING if self.fx is None else STATUS_SETTLED


def read_ledger(journal: Journal) -> list[BookedTrade]:
    """Cała księga w kolejności księgowania, każdy wiersz z walutą i stanem kwoty w EUR (tylko odczyt).

    Zbiór transakcji czytany jest JEDNĄ instrukcją (`trades_by_deal`), potem lista oczekujących, potem znaczniki i
    rozliczenia każdej z nich: zapis (faza 1 razem z `pnl_pending` w jednej transakcji, faza 2 przenosi kwotę i zdejmuje
    `pnl_pending` w jednej transakcji) nie może więc dać wiersza w EUR dla transakcji w walucie obcej, także gdy silnik
    pisze w trakcie raportu. Nieczytelne rozliczenie to transakcja oczekująca z `fx_unreadable`, nie wyjątek.
    """
    booked = journal.trades_by_deal()
    pending = {deal_id for deal_id, _ in journal.pnl_pending_deals()}
    rows = []
    for deal_id, trade in booked:
        flags = journal.trade_flags(deal_id)
        if deal_id in pending:
            rows.append(BookedTrade(trade, flags, None, True, True))
            continue
        try:
            fx = journal.trade_fx(deal_id)
        except JournalCorruptError:
            rows.append(BookedTrade(trade, flags, None, True, True, True))
            continue
        rows.append(BookedTrade(trade, flags, fx, fx is not None, False))
    return rows


@dataclass(frozen=True)
class EurSummary:
    """Wynik netto w EUR z wierszy wielu pasm, bez mieszania walut.

    `net` = `account_net` (wiersze w EUR) + `settled_net` (kwoty EUR z rozliczeń transakcji w walucie obcej);
    `settled`, `pending`: liczba rozliczonych i oczekujących transakcji w walucie obcej (oczekujące NIE mają kwoty w EUR
    i nie wchodzą do żadnej sumy); `per_day`: wynik w EUR po dniu lokalnym zamknięcia (tylko dni z co najmniej jedną
    kwotą w EUR); `worst_day`: (dzień, wynik) albo None; `estimated`, `estimated_net`: ile z rozliczonych transakcji (i
    ile EUR) to szacunek z kursu brokera (`ESTIMATED_FX`), a nie kwota z historii brokera.
    """

    net: Decimal
    account_net: Decimal
    settled_net: Decimal
    settled: int
    pending: int
    per_day: dict[date, Decimal]
    worst_day: tuple[date, Decimal] | None
    estimated: int = 0
    estimated_net: Decimal = Decimal(0)


def eur_summary(rows: Iterable[BookedTrade]) -> EurSummary:
    """Suma w EUR (dokładna): wiersz w EUR wnosi `net_pnl`, waluta obca kwotę z rozliczenia, oczekująca nic."""
    account: list[Decimal] = []
    settled: list[Decimal] = []
    estimated: list[Decimal] = []
    pending = 0
    per_day: dict[date, list[Decimal]] = {}
    for row in rows:
        amount = row.net_eur
        if amount is None:
            pending += 1
            continue
        (settled if row.foreign else account).append(amount)
        if row.estimated_fx:
            estimated.append(amount)
        per_day.setdefault(trade_day(row.trade), []).append(amount)
    daily = {day: exact_sum(values) for day, values in per_day.items()}
    account_net, settled_net = exact_sum(account), exact_sum(settled)
    worst = min(daily.items(), key=lambda item: (item[1], item[0]), default=None)
    return EurSummary(
        exact_sum([account_net, settled_net]), account_net, settled_net, len(settled), pending, daily, worst,
        len(estimated), exact_sum(estimated),
    )  # fmt: skip


def export_eur_csv(journal: Journal, path: Path, currencies: Mapping[str, str] | None = None) -> int:
    """Zapisuje do `path` osobny eksport walut i kwot w EUR (kolumny `EUR_COLUMNS`), jeden wiersz na transakcję w
    kolejności księgowania; zwraca liczbę wierszy danych. Ten sam format pliku i zapis atomowy co `export_csv`.

    `book_currency`: waluta kolumn kwot wiersza księgi (EUR dla wierszy w walucie rachunku; dla waluty obcej kod z
    `currencies[instrument]`, a bez niego puste: nazwa waluty nie jest zapisana w dzienniku); `net_pnl_book`: `net_pnl`
    wiersza w tej walucie; `net_pnl_eur` puste przed rozliczeniem; `eur_status`: ACCOUNT_CURRENCY, SETTLED albo PENDING;
    `eur_source`, `eur_rate`: źródło i kurs efektywny rozliczenia (puste, gdy brak). Bez identyfikatorów deali.
    """
    path = Path(path)
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory; give a file name")
    names = currencies or {}
    rows = read_ledger(journal)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EUR_COLUMNS)
    for row in rows:
        trade, fx = row.trade, row.fx
        currency = names.get(trade.instrument, "") if row.foreign else ACCOUNT_CURRENCY
        amount, rate = row.net_eur, None if fx is None else fx.rate
        writer.writerow([
            trade.instrument, currency, _cell(trade.exit_ts), trade.exit_reason, _cell(trade.net_pnl),
            "" if amount is None else _cell(amount), row.eur_status, "" if fx is None else fx.source,
            "" if rate is None else _cell(rate),
        ])  # fmt: skip
    _atomic_write_600(path, buffer.getvalue().encode("utf-8"))
    return len(rows)


# --- podsumowanie ------------------------------------------------------------------------------------------------


def trades_summary(trades: Iterable[TradeRecord]) -> dict[str, Any]:
    """Sumy księgi (dokładne) i liczniki: `count`, `gross`, `spread_cost`, `slippage_penalty`, `financing`, `fees`,
    `net`, `wins` (netto > 0), `losses` (netto < 0), `breakeven` (netto = 0), `worst_day` = (dzień, netto dnia) albo
    None.

    Wiersze muszą być w JEDNEJ walucie księgi (jedno pasmo): suma wierszy wielu pasm z walutą obcą byłaby zmieszaniem
    walut. Wynik w EUR z wielu pasm to `eur_summary`.
    """
    rows = list(trades)
    per_day: dict[date, list[Decimal]] = {}
    for trade in rows:
        per_day.setdefault(trade_day(trade), []).append(trade.net_pnl)
    daily = {day: exact_sum(values) for day, values in per_day.items()}
    worst = min(daily.items(), key=lambda item: (item[1], item[0]), default=None)
    return {
        "count": len(rows),
        "gross": exact_sum(t.gross_pnl for t in rows),
        "spread_cost": exact_sum(t.spread_cost for t in rows),
        "slippage_penalty": exact_sum(t.slippage_penalty for t in rows),
        "financing": exact_sum(t.financing for t in rows),
        "fees": exact_sum(t.fees for t in rows),
        "net": exact_sum(t.net_pnl for t in rows),
        "wins": sum(1 for t in rows if t.net_pnl > 0),
        "losses": sum(1 for t in rows if t.net_pnl < 0),
        "breakeven": sum(1 for t in rows if t.net_pnl == 0),
        "worst_day": worst,
    }
