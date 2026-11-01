"""Księga transakcji (spec §10): eksport do CSV i dokładne podsumowanie.

CSV: nagłówek dokładnie jak w specyfikacji (19 pól `TradeRecord` w tej kolejności), jeden wiersz na transakcję
w kolejności księgowania; `Decimal` jako zwykły napis (`format(d, "f")`, bez notacji naukowej i bez float), czasy
ISO-8601 w UTC z `Z`, kierunek jako wartość (`BUY`/`SELL`); plik UTF-8, końce linii `\\n`, uprawnienia 600, zapis
atomowy.

Sumy są dokładne: kontekst o precyzji 100 cyfr z pułapką na zaokrąglenie zamiast cichej utraty cyfr.
Dzień transakcji to dzień LOKALNY (Europe/Berlin) czasu zamknięcia, jak `exit_day` w księgowaniu silnika.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.journal import Journal, TradeRecord

__all__ = ["LEDGER_COLUMNS", "export_csv", "trades_summary", "exact_sum", "trade_day", "local_day", "decimal_text"]

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


# --- podsumowanie ------------------------------------------------------------------------------------------------


def trades_summary(trades: Iterable[TradeRecord]) -> dict[str, Any]:
    """Sumy księgi (dokładne) i liczniki: `count`, `gross`, `spread_cost`, `slippage_penalty`, `financing`, `fees`,
    `net`, `wins` (netto > 0), `losses` (netto < 0), `breakeven` (netto = 0), `worst_day` = (dzień, netto dnia) albo
    None.
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
