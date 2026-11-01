"""Bramka L1 (spec §11) liczona wyłącznie z danych dziennika, bez zmiany czegokolwiek.

L1 (po `fixed_min_size_sessions` = 5 sesjach na wielkości minimalnej): zero niewyjaśnionych rozbieżności
pozycja/broker starszych niż 60 s, zero nieobsłużonych odrzuceń, koszty zmierzone w granicach 2× modelu. To POMOC
w decyzji właściciela: kod niczego nie dopuszcza ani nie tworzy `data/L1_PASSED`. Liczby obejmują CAŁY dziennik, a ocena
wyjaśnień (czy dana rozbieżność została wyjaśniona) należy do właściciela.

Definicje czterech liczb (`gate_l1_status`, szczegóły i daty incydentów w `l1_details`):

- `sessions_at_min_size`: liczba RÓŻNYCH dni (lokalnych, Europe/Berlin), w których zaksięgowano transakcję wejścia
  strategii o wielkości minimalnej. Wielkość minimalna: parametr `min_size`, a bez niego najmniejsza wielkość wśród
  wejść strategii. Rola transakcji (`trade_contexts`): `TradeRecord` nie ma `deal_id`, więc rolę ustalamy z kontekstu
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
- `unhandled_rejections`: liczba RÓŻNYCH zleceń, które mają wiersz `event` ENTRY_UNRESOLVED ALBO są wciąż
  nierozstrzygnięte w bazie (`unresolved_orders`: INTENT/SUBMITTING/UNKNOWN), plus liczba włączeń kill switcha
  z przyczyną ENTRY_EXCEPTION (`kill.audit` ENGAGE/ESCALATE/REASON; ta przyczyna nie ma osobnego wiersza zdarzenia).
  Uwaga: wiersz ENTRY_UNRESOLVED liczy się NAWET wtedy, gdy zlecenie rozstrzygnięto później (ślad incydentu zostaje,
  a kod nie ocenia wyjaśnień), a zlecenie INTENT/SUBMITTING liczy się, gdy raport powstaje w trakcie wejścia.
  Zlecenie ZAKOŃCZONE (CONFIRMED, REJECTED, NOT_PLACED) bez wiersza ENTRY_UNRESOLVED nie jest nieobsłużone.
- `cost_deviation_ratio` (WERDYKT bramki, w PUNKTACH, bez zaokrągleń): Σ(zmierzony koszt) / Σ(koszt z modelu) po
  wszystkich transakcjach z użytecznym notowaniem decyzji (także smoke: to prawdziwe wypełnienia), gdzie dla
  transakcji zmierzony koszt = spread w chwili decyzji (ask − bid) + poślizg wejścia z wypełnienia (BUY: wypełnienie −
  ask, SELL: bid − wypełnienie; ujemny przy poprawie ceny), a koszt z modelu = ten sam spread + `entry_slippage_points`
  z `costs_v1.toml`. Wielkość i wartość punktu się skracają. Wynik jest ograniczony od dołu zerem. Brak użytecznych
  transakcji: 0 (to brak danych, nie dowód). Poślizg stopu przy wyjściu NIE jest mierzony (księga nie zna poziomu
  stopu brokera, a jego pochodzenie jest niepotwierdzone), więc dodatkowy poślizg STOP nie wchodzi ani do licznika,
  ani do mianownika.

Koszt w EUR (`l1_details().eur_ratio`) to tylko INFORMACJA: Σ(`spread_cost` + `slippage_penalty`) księgi / Σ modelu
w EUR, z wartością punktu z `entry_ctx` danej transakcji (zapasowo 1 dla transakcji bez kontekstu, o czym raport
informuje). Księga zaokrągla każdy składnik do 0,01 €, a przy wielkości 0,001 koszt modelu to ok. 0,0025 € na
transakcję, więc ten stosunek jest zgrubny (wiersz to 0,00 albo 0,01); `slippage_penalty` jest liczony z konfiguracji,
nie z pomiaru, a `spread_cost` to maksimum z kosztu zrealizowanego i modelu połówek spreadu.
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
from gwstock.report.ledger import exact_sum, local_day, trade_day
from gwstock.types import Side

__all__ = [
    "GateL1", "L1Details", "CostPoints", "TradeContext", "gate_l1_status", "l1_details", "trade_contexts",
    "trade_roles", "cost_points", "format_ratio", "read_events", "COST_DEVIATION_LIMIT",
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
DEFAULT_VALUE_PER_POINT = Decimal("1")  # zapasowa wartość punktu (DE40: 1 € za punkt przy wielkości 1)
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


def _engagement_days(journal: Journal, reason: str) -> list[date]:
    """Dni (lokalne) włączeń przyczyny kill switcha w `kill.audit` (ENGAGE/ESCALATE/REASON), po jednym na wiersz.

    Wiersz nieczytelny, który zawiera nazwę przyczyny, też się liczy.
    """
    days = []
    for event, data in read_events(journal, SOURCE_KILL_AUDIT):
        if data is None:
            if reason in event.payload:
                days.append(local_day(event.ts))
        elif data.get("reason") == reason and data.get("event") in ENGAGE_EVENTS:
            days.append(local_day(event.ts))
    return days


def _mismatch_days(journal: Journal) -> list[date]:
    incidents: dict[tuple[str, Any], date] = {}
    for event, data in read_events(journal, SOURCE_RECONCILE):
        if data is None:
            if MISMATCH_TIMEOUT in event.payload:
                incidents.setdefault(("row", event.id), local_day(event.ts))
        elif data.get("event") == MISMATCH_TIMEOUT:
            since = data.get("since")
            key = ("since", since) if since is not None else ("row", event.id)
            incidents.setdefault(key, local_day(event.ts))
    audits = _engagement_days(journal, MISMATCH_TIMEOUT)
    return sorted(incidents.values() if len(incidents) >= len(audits) else audits)


def _rejection_days(journal: Journal) -> list[date]:
    incidents: dict[tuple[str, Any], date] = {}
    for event, data in read_events(journal, SOURCE_EVENT):
        if data is None:
            if ENTRY_UNRESOLVED in event.payload:
                incidents.setdefault(("row", event.id), local_day(event.ts))
        elif data.get("event") == ENTRY_UNRESOLVED:
            order_id = data.get("order_id")
            key = ("order", order_id) if isinstance(order_id, str) and order_id else ("row", event.id)
            incidents.setdefault(key, local_day(event.ts))
    for order in journal.unresolved_orders():
        incidents.setdefault(("order", order.order_id), local_day(order.created_at))
    return sorted([*incidents.values(), *_engagement_days(journal, ENTRY_EXCEPTION)])


# --- role i kontekst transakcji ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeContext:
    """Rola transakcji (`entry`, `smoke`, `unknown`) i wartość punktu z kontekstu wejścia (None: brak)."""

    role: str
    value_per_point: Decimal | None


def _parse_context(text: str) -> tuple[datetime, datetime, Decimal | None] | None:
    try:
        ctx = json.loads(text)
        signal_ts, submit_ts = datetime.fromisoformat(ctx["signal_ts"]), datetime.fromisoformat(ctx["submit_ts"])
    except (ValueError, TypeError, KeyError):
        return None
    vpp = None
    try:
        value = Decimal(ctx["value_per_point"])
        vpp = value if value.is_finite() and value > 0 else None
    except (ArithmeticError, ValueError, TypeError, KeyError):
        pass
    return signal_ts, submit_ts, vpp


def trade_contexts(journal: Journal, trades: list[TradeRecord]) -> list[TradeContext]:
    """Rola i wartość punktu każdej transakcji z kontekstu wejścia; definicja w docstringu modułu.

    Dla dnia sygnału i dni sąsiednich (nazwa zlecenia nosi dzień handlowy, a czas UTC może wypaść w innym dniu)
    przegląda zlecenia `gws-{dzień}-{instrument}-{seq}-{entry|smoke}`, czyta ich `entry_ctx` i dopasowuje transakcję
    po `signal_ts` i `submit_ts`. Bez dopasowania: rola `unknown`, wartość punktu None. Tylko publiczne metody
    dziennika.
    """
    index: dict[tuple[datetime, datetime], TradeContext] = {}
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
                    parsed = None if text is None else _parse_context(text)
                    if parsed is not None:
                        index.setdefault(parsed[:2], TradeContext(role, parsed[2]))
    unknown = TradeContext(ROLE_UNKNOWN, None)
    return [index.get((trade.signal_ts, trade.submit_ts), unknown) for trade in trades]


def trade_roles(journal: Journal, trades: list[TradeRecord]) -> list[str]:
    """Rola każdej transakcji: `entry`, `smoke` albo `unknown` (brak kontekstu wejścia)."""
    return [context.role for context in trade_contexts(journal, trades)]


# --- koszty ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostPoints:
    """Pomiar kosztu w punktach, bez zaokrągleń księgi.

    `trades`: liczba transakcji z użytecznym notowaniem (ask > bid); `spread`: średni spread decyzji;
    `entry_slippage`: średni poślizg wejścia względem notowania decyzji; `ratio`: Σ zmierzonego / Σ modelu (> 0 lub 0).
    """

    trades: int
    spread: Decimal | None
    entry_slippage: Decimal | None
    ratio: Decimal


def _entry_slippage(trade: TradeRecord) -> Decimal:
    """O ile wypełnienie wejścia jest GORSZE od notowania decyzji (BUY: wypełnienie − ask, SELL: bid − wypełnienie)."""
    if trade.side is Side.BUY:
        return _EXACT.subtract(trade.entry_fill, trade.decision_ask)
    return _EXACT.subtract(trade.decision_bid, trade.entry_fill)


def cost_points(trades: list[TradeRecord], costs: CostConfig) -> CostPoints:
    """Pomiar w punktach (werdykt kosztów bramki L1; definicja w docstringu modułu).

    Transakcje o spreadzie 0 (zdegradowane wiersze, gdzie notowanie = poziom otwarcia) są pomijane. Średnie to iloraz
    o precyzji 28 cyfr; sumy są dokładne.
    """
    usable = [t for t in trades if t.decision_ask > t.decision_bid]
    if not usable:
        return CostPoints(0, None, None, Decimal(0))
    spreads = [_EXACT.subtract(t.decision_ask, t.decision_bid) for t in usable]
    slips = [_entry_slippage(t) for t in usable]
    measured = exact_sum([*spreads, *slips])
    modelled = exact_sum([*spreads, *([costs.entry_slippage_points] * len(usable))])  # spread > 0, więc model > 0
    count = Decimal(len(usable))
    ratio = max(_RATIO.divide(measured, modelled), Decimal(0))
    return CostPoints(
        len(usable), _RATIO.divide(exact_sum(spreads), count), _RATIO.divide(exact_sum(slips), count), ratio
    )


def _eur_ratio(
    trades: list[TradeRecord], contexts: list[TradeContext], costs: CostConfig, fallback_vpp: Decimal
) -> tuple[Decimal, int]:
    """Informacyjny stosunek kosztów w EUR i liczba transakcji, dla których przyjęto zapasową wartość punktu."""
    measured = exact_sum(_EXACT.add(t.spread_cost, t.slippage_penalty) for t in trades)
    defaulted = 0
    modelled_parts = []
    for trade, context in zip(trades, contexts):
        vpp = context.value_per_point
        if vpp is None:
            vpp, defaulted = fallback_vpp, defaulted + 1
        slippage_points = costs.entry_slippage_points
        if trade.exit_reason == "STOP":
            slippage_points = _EXACT.add(slippage_points, costs.stop_exit_extra_slippage_points)
        points = _EXACT.add(_EXACT.subtract(trade.decision_ask, trade.decision_bid), slippage_points)
        modelled_parts.append(_EXACT.multiply(points, _EXACT.multiply(trade.size, vpp)))
    modelled = exact_sum(modelled_parts)
    if modelled <= 0:
        return (Decimal(0) if measured <= 0 else Decimal("Infinity")), defaulted
    return _RATIO.divide(measured, modelled), defaulted


# --- status ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class L1Details:
    """Status L1 z danymi pomocniczymi dla raportu.

    `gate`: cztery liczby werdyktu; `roles`: rola każdej transakcji księgi; `points`: pomiar w punktach (werdykt
    kosztów); `eur_ratio`: koszty w EUR (informacja); `default_vpp_trades`: ile transakcji w `eur_ratio` ma zapasową
    wartość punktu; `mismatch_days`, `rejection_days`: dni lokalne policzonych incydentów (po jednym na incydent).
    """

    gate: GateL1
    roles: tuple[str, ...]
    points: CostPoints
    eur_ratio: Decimal
    default_vpp_trades: int
    fallback_value_per_point: Decimal
    mismatch_days: tuple[date, ...]
    rejection_days: tuple[date, ...]


def _default_costs() -> CostConfig:
    return load_config(Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR))[2]


def l1_details(
    journal: Journal,
    *,
    min_size: Decimal | None = None,
    costs: CostConfig | None = None,
    value_per_point: Decimal = DEFAULT_VALUE_PER_POINT,
    trades: list[TradeRecord] | None = None,
) -> L1Details:
    """Status bramki L1 z danymi pomocniczymi (definicje w docstringu modułu).

    `min_size`: wielkość minimalna sesji (domyślnie najmniejsza wśród wejść strategii). `costs`: stawki modelu
    (domyślnie `costs_v1.toml` z `GW_CONFIG_DIR` albo `config/`). `value_per_point`: wartość punktu zapasowa, tylko dla
    transakcji bez kontekstu wejścia i tylko w informacyjnym koszcie w EUR (DE40: 1). `trades`: księga już odczytana
    przez wywołującego (domyślnie `journal.trades()`).
    """
    if min_size is not None and (not isinstance(min_size, Decimal) or min_size <= 0):
        raise ValueError(f"min_size: expected a positive Decimal, got {min_size!r}")
    if not isinstance(value_per_point, Decimal) or value_per_point <= 0:
        raise ValueError(f"value_per_point: expected a positive Decimal, got {value_per_point!r}")
    costs = costs if costs is not None else _default_costs()
    trades = journal.trades() if trades is None else trades
    contexts = trade_contexts(journal, trades)
    entries = [t for t, context in zip(trades, contexts) if context.role == ROLE_ENTRY]
    floor = min_size if min_size is not None else min((t.size for t in entries), default=None)
    sessions = len({trade_day(t) for t in entries if t.size == floor})
    mismatches, rejections = _mismatch_days(journal), _rejection_days(journal)
    points = cost_points(trades, costs)
    eur_ratio, defaulted = _eur_ratio(trades, contexts, costs, value_per_point)
    return L1Details(
        gate=GateL1(sessions, len(mismatches), len(rejections), points.ratio),
        roles=tuple(context.role for context in contexts),
        points=points,
        eur_ratio=eur_ratio,
        default_vpp_trades=defaulted,
        fallback_value_per_point=value_per_point,
        mismatch_days=tuple(mismatches),
        rejection_days=tuple(rejections),
    )


def gate_l1_status(journal: Journal, *, min_size: Decimal | None = None, costs: CostConfig | None = None) -> GateL1:
    """Cztery liczby bramki L1 z dziennika (definicje w docstringu modułu); szczegóły: `l1_details`."""
    return l1_details(journal, min_size=min_size, costs=costs).gate
