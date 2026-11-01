"""Bramka L1 (spec §11) liczona wyłącznie z danych dziennika, bez zmiany czegokolwiek.

L1 (po `fixed_min_size_sessions` = 5 sesjach na wielkości minimalnej): zero niewyjaśnionych rozbieżności
pozycja/broker starszych niż 60 s, zero nieobsłużonych odrzuceń, koszty zmierzone w granicach 2× modelu. To POMOC
w decyzji właściciela: kod niczego nie dopuszcza ani nie tworzy `data/L1_PASSED`.

Definicje czterech liczb (`gate_l1_status`):

- `sessions_at_min_size`: liczba RÓŻNYCH dni (lokalnych, Europe/Berlin), w których zaksięgowano transakcję wejścia
  strategii o wielkości minimalnej. Wielkość minimalna: parametr `min_size`, a bez niego najmniejsza wielkość wśród
  wejść strategii. Rola transakcji (`trade_roles`): `TradeRecord` nie ma `deal_id`, więc rolę ustalamy z kontekstu
  wejścia, który silnik zapisuje przed wysyłką (`entry_ctx:<client_id>` w `meta`). Zlecenia dnia
  `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}` przeglądamy przez `count_orders` i `get_meta`, a transakcję dopasowujemy
  po DOKŁADNYCH czasach `signal_ts` i `submit_ts`. Rola `smoke` (próba akceptacyjna L0) nie jest sesją (zgodnie
  z `Journal.count_smoke_trades` i licznikiem sesji silnika). Transakcja bez kontekstu (np. zdegradowany wiersz
  COST_FALLBACK) ma rolę `unknown` i też się nie liczy: nie da się wykazać, że to wejście strategii (stronniczość
  w stronę ostrożności; licznik sesji silnika policzyłby ją jako sesję).
- `unreconciled_over_60s`: liczba RÓŻNYCH incydentów MISMATCH_TIMEOUT. Wiersze `reconcile.event` z
  `event == MISMATCH_TIMEOUT` zliczamy po wartości `since` (to samo zdarzenie powtarza się co przebieg uzgadniania),
  a wiersze `kill.audit` z `reason == MISMATCH_TIMEOUT` i `event` ENGAGE/ESCALATE/REASON po jednym. Wynik to większa
  z tych dwóch liczb. Wiersz nieczytelny (nie JSON), który zawiera nazwę przyczyny, liczy się jako osobny incydent.
- `unhandled_rejections`: liczba RÓŻNYCH zleceń z wierszem `event` ENTRY_UNRESOLVED albo wciąż nierozstrzygniętych
  w bazie (`unresolved_orders`: INTENT/SUBMITTING/UNKNOWN), plus liczba włączeń kill switcha z przyczyną
  ENTRY_EXCEPTION (`kill.audit` ENGAGE/ESCALATE/REASON; ta przyczyna nie ma osobnego wiersza zdarzenia). Zlecenie
  ZAKOŃCZONE (CONFIRMED, REJECTED, NOT_PLACED) nie jest nieobsłużone.
- `cost_deviation_ratio`: Σ(`spread_cost` + `slippage_penalty`) księgi podzielona przez Σ kosztu z modelu po
  wszystkich transakcjach (także smoke: to prawdziwe wypełnienia). Koszt z modelu jednej transakcji =
  (`decision_ask` − `decision_bid`) × wielkość × wartość punktu + (poślizg wejścia + dodatkowy poślizg przy wyjściu
  STOP, oba z `costs_v1.toml`) × wielkość × wartość punktu. Wartość punktu `value_per_point` = 1 (DE40: 1 € za punkt
  przy wielkości 1; założenie). Brak transakcji: 0; model 0 i koszt 0: 0; koszt bez modelu: nieskończoność
  (bramka niespełniona).

OGRANICZENIA kosztów, do przeglądu przez właściciela: (1) `slippage_penalty` w księdze jest liczony z konfiguracji
(nie z pomiaru), więc w liczniku i mianowniku jest ten sam składnik, a stosunek jest zaniżony względem rzeczywistej
rozbieżności; (2) księga zaokrągla każdy składnik do 0,01 €, a przy wielkości 0,001 koszt modelu to ok. 0,0025 € na
transakcję, więc pojedynczy wiersz to 0,00 albo 0,01 i stosunek jest zgrubny; (3) `spread_cost` księgi to maksimum
z kosztu zrealizowanego i modelu połówek spreadu z chwili wejścia i wyjścia. Dlatego `cost_points` podaje też pomiar
w PUNKTACH (bez zaokrąglania): średni spread i średni poślizg wejścia względem notowania decyzji.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

from gwstock.config import CostConfig, load_config
from gwstock.journal import Journal, RawEvent, TradeRecord
from gwstock.orders.ids import make_client_id
from gwstock.report.ledger import exact_sum, trade_day
from gwstock.types import Side

__all__ = [
    "GateL1", "gate_l1_status", "trade_roles", "cost_points", "format_ratio", "read_events", "COST_DEVIATION_LIMIT",
]  # fmt: skip

# Nazwy zgodne ze stałymi silnika (pilnuje ich test `test_literals_match_the_engine_constants`); nie importujemy
# `gwstock.execution`, żeby raport nie ładował całego silnika (pamięć kontenera).
SOURCE_EVENT = "event"
SOURCE_GATE_BLOCK = "gate_block"
SOURCE_DECISION = "decision"
SOURCE_RECONCILE = "reconcile.event"
SOURCE_KILL_AUDIT = "kill.audit"
SOURCE_WATCHDOG = "watchdog"
SOURCE_ENGINE = "engine"
META_ENTRY_CTX = "entry_ctx:{}"
ROLE_ENTRY = "entry"
ROLE_SMOKE = "smoke"
ROLE_UNKNOWN = "unknown"
ENTRY_UNRESOLVED = "ENTRY_UNRESOLVED"
ENTRY_EXCEPTION = "ENTRY_EXCEPTION"
MISMATCH_TIMEOUT = "MISMATCH_TIMEOUT"
ENGAGE_EVENTS = ("ENGAGE", "ESCALATE", "REASON")  # wiersze `kill.audit` oznaczające włączenie przyczyny

COST_DEVIATION_LIMIT = Decimal("2")  # spec §11: rozbieżność większa niż 2× model uruchamia przegląd
DEFAULT_VALUE_PER_POINT = Decimal("1")  # DE40: 1 € za punkt przy wielkości 1 (założenie, patrz docstring modułu)
ENV_CONFIG_DIR = "GW_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "config"

_EXACT = Context(prec=100, traps=[InvalidOperation, Overflow, Inexact])
_RATIO = Context(prec=28)
_TWO_PLACES = Decimal("0.01")


# --- reguła -------------------------------------------------------------------------------------------------------


def _check_count(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"GateL1.{name}: expected int, got {type(value).__name__} {value!r}")
    if value < 0:
        raise ValueError(f"GateL1.{name}: must not be negative, got {value}")


def _check_required(required_sessions: object) -> None:
    if not isinstance(required_sessions, int) or isinstance(required_sessions, bool):
        got = f"{type(required_sessions).__name__} {required_sessions!r}"
        raise TypeError(f"required_sessions: expected int, got {got}")
    if required_sessions < 1:
        raise ValueError(f"required_sessions: must be at least 1, got {required_sessions}")


def format_ratio(ratio: Decimal) -> str:
    """Stosunek kosztów do wyświetlenia: dwa miejsca po przecinku (ROUND_HALF_UP), nieskończoność jako `inf`."""
    if ratio.is_infinite():
        return "inf"
    return format(ratio.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True)
class GateL1:
    """Cztery liczby bramki L1 (patrz docstring modułu); `passed` sprawdza je wobec wymaganej liczby sesji."""

    sessions_at_min_size: int
    unreconciled_over_60s: int
    unhandled_rejections: int
    cost_deviation_ratio: Decimal

    def __post_init__(self) -> None:
        for name in ("sessions_at_min_size", "unreconciled_over_60s", "unhandled_rejections"):
            _check_count(name, getattr(self, name))
        ratio = self.cost_deviation_ratio
        if not isinstance(ratio, Decimal):
            raise TypeError(f"GateL1.cost_deviation_ratio: expected Decimal, got {type(ratio).__name__} {ratio!r}")
        if ratio.is_nan():
            raise ValueError("GateL1.cost_deviation_ratio: must not be NaN")
        if ratio < 0:
            raise ValueError(f"GateL1.cost_deviation_ratio: must not be negative, got {ratio}")

    def passed(self, required_sessions: int) -> bool:
        """Cztery warunki (spec §11): sesje ≥ wymagane, zero rozbieżności, zero odrzuceń, koszty ≤ 2× model."""
        _check_required(required_sessions)
        return (
            self.sessions_at_min_size >= required_sessions
            and self.unreconciled_over_60s == 0
            and self.unhandled_rejections == 0
            and self.cost_deviation_ratio <= COST_DEVIATION_LIMIT
        )

    def reasons(self, required_sessions: int) -> list[str]:
        """Opisy niespełnionych warunków (pusta lista: bramka spełniona)."""
        _check_required(required_sessions)
        reasons = []
        if self.sessions_at_min_size < required_sessions:
            missing = required_sessions - self.sessions_at_min_size
            reasons.append(
                f"za mało sesji na wielkości minimalnej: {self.sessions_at_min_size} z {required_sessions} "
                f"(brakuje {missing})"
            )
        if self.unreconciled_over_60s:
            reasons.append(f"rozbieżności pozycja/broker starsze niż 60 s: {self.unreconciled_over_60s} (wymagane 0)")
        if self.unhandled_rejections:
            reasons.append(f"nieobsłużone odrzucenia: {self.unhandled_rejections} (wymagane 0)")
        if self.cost_deviation_ratio > COST_DEVIATION_LIMIT:
            reasons.append(
                f"koszty zmierzone / model: {format_ratio(self.cost_deviation_ratio)} > {COST_DEVIATION_LIMIT} "
                "(przegląd kosztów)"
            )
        return reasons


# --- odczyt zdarzeń -------------------------------------------------------------------------------------------------


def read_events(journal: Journal, source: str) -> list[tuple[RawEvent, dict[str, Any] | None]]:
    """Wiersze źródła z rozkodowanym ładunkiem JSON; None, gdy ładunek nie jest obiektem JSON (nieczytelny)."""
    rows = []
    for event in journal.events(source):
        try:
            data = json.loads(event.payload)
        except ValueError:
            data = None
        rows.append((event, data if isinstance(data, dict) else None))
    return rows


def _engagements(journal: Journal, reason: str) -> int:
    """Liczba włączeń przyczyny kill switcha w `kill.audit` (ENGAGE/ESCALATE/REASON); nieczytelny wiersz z nazwą też."""
    count = 0
    for event, data in read_events(journal, SOURCE_KILL_AUDIT):
        if data is None:
            count += reason in event.payload
        elif data.get("reason") == reason and data.get("event") in ENGAGE_EVENTS:
            count += 1
    return count


def _mismatch_incidents(journal: Journal) -> int:
    incidents: set[tuple[str, Any]] = set()
    for event, data in read_events(journal, SOURCE_RECONCILE):
        if data is None:
            if MISMATCH_TIMEOUT in event.payload:
                incidents.add(("row", event.id))
        elif data.get("event") == MISMATCH_TIMEOUT:
            since = data.get("since")
            incidents.add(("since", since) if since is not None else ("row", event.id))
    return max(len(incidents), _engagements(journal, MISMATCH_TIMEOUT))


def _unhandled_rejections(journal: Journal) -> int:
    incidents: set[tuple[str, Any]] = set()
    for event, data in read_events(journal, SOURCE_EVENT):
        if data is None:
            if ENTRY_UNRESOLVED in event.payload:
                incidents.add(("row", event.id))
        elif data.get("event") == ENTRY_UNRESOLVED:
            order_id = data.get("order_id")
            incidents.add(("order", order_id) if isinstance(order_id, str) and order_id else ("row", event.id))
    incidents.update(("order", order.order_id) for order in journal.unresolved_orders())
    return len(incidents) + _engagements(journal, ENTRY_EXCEPTION)


# --- role transakcji ------------------------------------------------------------------------------------------------


def _context_times(text: str) -> tuple[datetime, datetime] | None:
    try:
        ctx = json.loads(text)
        return datetime.fromisoformat(ctx["signal_ts"]), datetime.fromisoformat(ctx["submit_ts"])
    except (ValueError, TypeError, KeyError):
        return None


def trade_roles(journal: Journal, trades: list[TradeRecord]) -> list[str]:
    """Rola każdej transakcji: `entry`, `smoke` albo `unknown` (brak kontekstu wejścia); definicja w docstringu modułu.

    Dla dnia sygnału i dni sąsiednich (nazwa zlecenia nosi dzień handlowy, a czas UTC może wypaść w innym dniu)
    przegląda zlecenia `gws-{dzień}-{instrument}-{seq}-{entry|smoke}`, czyta ich `entry_ctx` i dopasowuje transakcję
    po `signal_ts` i `submit_ts`. Tylko publiczne metody dziennika.
    """
    index: dict[tuple[datetime, datetime], str] = {}
    scanned: set[tuple[str, date]] = set()
    for trade in trades:
        for offset in (-1, 0, 1):
            day = trade.signal_ts.date() + timedelta(days=offset)
            if (trade.instrument, day) in scanned:
                continue
            scanned.add((trade.instrument, day))
            for seq in range(1, journal.count_orders(day) + 1):
                for role in (ROLE_ENTRY, ROLE_SMOKE):
                    try:
                        client_id = make_client_id(day, trade.instrument, seq, role)
                    except (TypeError, ValueError):
                        continue
                    text = journal.get_meta(META_ENTRY_CTX.format(client_id))
                    times = None if text is None else _context_times(text)
                    if times is not None:
                        index.setdefault(times, role)
    return [index.get((trade.signal_ts, trade.submit_ts), ROLE_UNKNOWN) for trade in trades]


# --- koszty ---------------------------------------------------------------------------------------------------------


def _modelled_cost(trade: TradeRecord, costs: CostConfig, value_per_point: Decimal) -> Decimal:
    per_point = _EXACT.multiply(trade.size, value_per_point)
    slippage_points = costs.entry_slippage_points
    if trade.exit_reason == "STOP":
        slippage_points = _EXACT.add(slippage_points, costs.stop_exit_extra_slippage_points)
    points = _EXACT.add(_EXACT.subtract(trade.decision_ask, trade.decision_bid), slippage_points)
    return _EXACT.multiply(points, per_point)


def _cost_ratio(trades: list[TradeRecord], costs: CostConfig, value_per_point: Decimal) -> Decimal:
    measured = exact_sum(_EXACT.add(t.spread_cost, t.slippage_penalty) for t in trades)
    modelled = exact_sum(_modelled_cost(t, costs, value_per_point) for t in trades)
    if modelled <= 0:
        return Decimal(0) if measured <= 0 else Decimal("Infinity")
    return _RATIO.divide(measured, modelled)


def cost_points(trades: list[TradeRecord]) -> tuple[int, Decimal | None, Decimal | None]:
    """Pomiar w punktach bez zaokrągleń księgi: (liczba transakcji, średni spread decyzji, średni poślizg wejścia).

    Poślizg wejścia = o ile wypełnienie jest GORSZE od notowania decyzji (BUY: wypełnienie − ask, SELL: bid −
    wypełnienie; ujemny przy poprawie ceny). Transakcje o spreadzie 0 (zdegradowane wiersze, gdzie notowanie = poziom
    otwarcia) są pomijane. Brak takich transakcji: (0, None, None). Średnie to iloraz o precyzji 28 cyfr.
    """
    usable = [t for t in trades if t.decision_ask > t.decision_bid]
    if not usable:
        return 0, None, None
    spreads = exact_sum(_EXACT.subtract(t.decision_ask, t.decision_bid) for t in usable)
    slips = exact_sum(
        _EXACT.subtract(t.entry_fill, t.decision_ask)
        if t.side is Side.BUY
        else _EXACT.subtract(t.decision_bid, t.entry_fill)
        for t in usable
    )
    count = Decimal(len(usable))
    return len(usable), _RATIO.divide(spreads, count), _RATIO.divide(slips, count)


# --- status ---------------------------------------------------------------------------------------------------------


def _default_costs() -> CostConfig:
    return load_config(Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR))[2]


def gate_l1_status(
    journal: Journal,
    *,
    min_size: Decimal | None = None,
    costs: CostConfig | None = None,
    value_per_point: Decimal = DEFAULT_VALUE_PER_POINT,
) -> GateL1:
    """Cztery liczby bramki L1 z dziennika (definicje w docstringu modułu).

    `min_size`: wielkość minimalna sesji (domyślnie najmniejsza wśród wejść strategii). `costs`: stawki modelu
    (domyślnie `costs_v1.toml` z `GW_CONFIG_DIR` albo `config/`). `value_per_point`: wartość punktu (DE40: 1).
    """
    if min_size is not None and (not isinstance(min_size, Decimal) or min_size <= 0):
        raise ValueError(f"min_size: expected a positive Decimal, got {min_size!r}")
    if not isinstance(value_per_point, Decimal) or value_per_point <= 0:
        raise ValueError(f"value_per_point: expected a positive Decimal, got {value_per_point!r}")
    trades = journal.trades()
    roles = trade_roles(journal, trades)
    entries = [t for t, role in zip(trades, roles) if role == ROLE_ENTRY]
    floor = min_size if min_size is not None else min((t.size for t in entries), default=None)
    sessions = len({trade_day(t) for t in entries if t.size == floor})
    return GateL1(
        sessions_at_min_size=sessions,
        unreconciled_over_60s=_mismatch_incidents(journal),
        unhandled_rejections=_unhandled_rejections(journal),
        cost_deviation_ratio=_cost_ratio(trades, costs if costs is not None else _default_costs(), value_per_point),
    )
