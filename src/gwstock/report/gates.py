"""Bramka L1 (spec §11) liczona wyłącznie z danych dziennika, bez zmiany czegokolwiek.

L1 (po `fixed_min_size_sessions` = 5 sesjach na wielkości minimalnej): zero niewyjaśnionych rozbieżności
pozycja/broker starszych niż 60 s, zero nieobsłużonych odrzuceń, koszty zmierzone w granicach 2× modelu. To POMOC
w decyzji właściciela: kod niczego nie dopuszcza ani nie tworzy `data/L1_PASSED`. Liczby obejmują CAŁY dziennik, a ocena
wyjaśnień (czy dana rozbieżność została wyjaśniona) należy do właściciela.

Definicje czterech liczb bramki (`gate_l1_status`, szczegóły i daty incydentów w `l1_details`) i liczby próbek kosztu
(razem pięć wielkości w `GateL1`):

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
  nierozstrzygnięte w bazie (`unresolved_orders`: każde zlecenie w stanie nieostatecznym), plus liczba włączeń
  kill switcha z przyczyną ENTRY_EXCEPTION (`kill.audit` ENGAGE/ESCALATE/REASON; ta przyczyna nie ma osobnego
  wiersza zdarzenia).
  Uwaga: wiersz ENTRY_UNRESOLVED liczy się NAWET wtedy, gdy zlecenie rozstrzygnięto później (ślad incydentu zostaje,
  a kod nie ocenia wyjaśnień), a zlecenie w toku (w v1 INTENT, SUBMITTING albo UNKNOWN) liczy się, gdy raport powstaje
  w trakcie wejścia. Zlecenie w stanie ostatecznym (CONFIRMED, REJECTED, NOT_PLACED, CANCELED, EXPIRED) bez wiersza
  ENTRY_UNRESOLVED nie jest nieobsłużone.
- `cost_deviation_ratio` (WERDYKT bramki, w PUNKTACH, bez zaokrągleń): Σ(niekorzystny poślizg wejścia) /
  (n × `entry_slippage_points` z `costs_v1.toml`), gdzie n to liczba PRÓBEK KOSZTU, a poślizg wejścia z wypełnienia
  to BUY: wypełnienie − ask, SELL: bid − wypełnienie, z poprawą ceny liczoną jako 0 (nie odejmuje się od innych
  transakcji). Spread NIE wchodzi do werdyktu (jest zarazem pomiarem i wejściem modelu, więc rozcieńczałby
  rozbieżność). Przy modelu 0,5 pkt i progu 2× przegląd wypada, gdy średni niekorzystny poślizg przekracza 1,0 pkt.
  Model 0 przy poślizgu > 0: nieskończoność. Próbka kosztu: transakcja o roli `entry` lub `smoke` z użytecznym
  notowaniem decyzji (ask > bid). Poślizg i spread przy wyjściu STOP NIE są mierzone (księga nie zna poziomu stopu
  brokera, a jego pochodzenie jest niepotwierdzone); `CostPoints.stop_exits` tylko je liczy.
- `cost_samples` (piąte pole `GateL1`): liczba próbek kosztu. Bramka NIE przechodzi, gdy próbek jest mniej niż
  wymaganych sesji (`brak pomiaru kosztów`): zero dowodów to nie dowód.

Transakcje WYKLUCZONE z sesji, z próbek kosztu i z kosztu w EUR (`L1Details.excluded_trades`, raport drukuje ich
liczbę): rola `unknown` (brak kontekstu wejścia, np. wiersz COST_FALLBACK), rola `invalid` (kontekst jest, ale wartość
punktu jest uszkodzona: nie napis, nie liczba, nie skończona albo poza zakresem
[`MIN_VALUE_PER_POINT`, `MAX_VALUE_PER_POINT`] = [0,001, 1000], czyli domyślna wartość punktu 1 dla DE40 podzielona
i pomnożona przez 1000; w konfiguracji nie ma limitu wartości punktu) i rola `no_quote` (kontekst jest, ale notowanie
decyzji jest nieużyteczne: ask ≤ bid). Uszkodzony zapis nie daje więc żadnej liczby. Znana, ostrożna konsekwencja: gdy
wszystkie wejścia o wielkości minimalnej są wykluczone, próg wielkości minimalnej przesuwa się na kolejną wielkość.

Informacyjnie (nie wpływa na werdykt): średni i największy spread decyzji, największy poślizg wejścia, liczba wyjść
STOP, łączny stosunek (spread + poślizg wejścia) / (spread + model) (`CostPoints.combined_ratio`) oraz koszt w EUR
(`l1_details().eur_ratio`): Σ(`spread_cost` + `slippage_penalty`) księgi / Σ modelu w EUR z wartością punktu
z `entry_ctx` danej transakcji. Księga zaokrągla każdy składnik do 0,01 €, a przy wielkości 0,001 koszt modelu to
ok. 0,0025 € na transakcję, więc ten stosunek jest zgrubny (wiersz to 0,00 albo 0,01); `slippage_penalty` jest liczony
z konfiguracji, nie z pomiaru, a `spread_cost` to maksimum z kosztu zrealizowanego i modelu połówek spreadu.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

from gwstock.config import CostConfig, load_config
from gwstock.journal import Journal, RawEvent, TradeRecord
from gwstock.orders.ids import make_client_id
from gwstock.report.ledger import exact_sum, local_day, trade_day
from gwstock.types import Side

__all__ = [
    "GateL1", "L1Details", "CostPoints", "TradeContext", "gate_l1_status", "l1_details", "trade_contexts",
    "trade_roles", "cost_points", "format_ratio", "read_events", "COST_DEVIATION_LIMIT", "MAX_VALUE_PER_POINT",
    "MIN_VALUE_PER_POINT",
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
ROLE_UNKNOWN = "unknown"  # brak kontekstu wejścia
ROLE_INVALID = "invalid"  # kontekst jest, ale wartość punktu jest uszkodzona
ROLE_NO_QUOTE = "no_quote"  # kontekst jest i poprawny, ale notowanie decyzji jest nieużyteczne (ask <= bid)
COST_ROLES = (ROLE_ENTRY, ROLE_SMOKE)  # role, których transakcje są próbkami kosztu
ENTRY_UNRESOLVED = "ENTRY_UNRESOLVED"
ENTRY_EXCEPTION = "ENTRY_EXCEPTION"
MISMATCH_TIMEOUT = "MISMATCH_TIMEOUT"
ENGAGE_EVENTS = ("ENGAGE", "ESCALATE", "REASON")  # wiersze `kill.audit` oznaczające włączenie przyczyny

COST_DEVIATION_LIMIT = Decimal("2")  # spec §11: rozbieżność większa niż 2× model uruchamia przegląd
DEFAULT_VALUE_PER_POINT = Decimal("1")  # domyślna wartość punktu instrumentu (DE40: 1 € za punkt przy wielkości 1)
MAX_VALUE_PER_POINT = 1000 * DEFAULT_VALUE_PER_POINT  # powyżej tego wartość punktu z kontekstu jest uszkodzona
MIN_VALUE_PER_POINT = DEFAULT_VALUE_PER_POINT / 1000  # poniżej tego też (dzielenie kosztu przez ~0 dałoby absurd)
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


def format_ratio(ratio: Decimal, limit: Decimal | None = None) -> str:
    """Stosunek do wyświetlenia: dwa miejsca po przecinku (ROUND_HALF_UP), nieskończoność jako `inf`, a wartość
    niemożliwa do zapisania (NaN, poza zakresem precyzji) jako `n/a`; nigdy nie rzuca i nigdy nie drukuje `-0.00`.

    Z `limit`: stosunek PONAD limitem, który po zaokrągleniu do 2 miejsc wypadłby jako limit (`2.00` przy limicie 2),
    jest drukowany z kolejnymi miejscami (3 do 12, to samo zaokrąglenie), aż druk przekroczy limit; gdyby i to nie
    wystarczyło, zaokrąglany w górę na 12. miejscu. Druk nigdy nie przeczy porównaniu na dokładnej wartości.
    """
    if ratio.is_infinite():
        return "inf"
    if ratio.is_nan():
        return "n/a"
    try:
        text = ratio.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        if limit is not None and ratio > limit and text <= limit:
            for places in range(3, 13):
                finer = ratio.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                if finer > limit:
                    text = finer
                    break
            else:
                text = ratio.quantize(Decimal(1).scaleb(-12), rounding=ROUND_CEILING)
        return format(abs(text) if text.is_zero() else text, "f")  # nigdy "-0.00"
    except ArithmeticError:  # poza zakresem precyzji kontekstu
        return "n/a"


@dataclass(frozen=True)
class GateL1:
    """Cztery liczby bramki L1 (patrz docstring modułu) i liczba próbek kosztu; `passed` sprawdza je wobec wymaganej
    liczby sesji. `cost_samples` bez podania to 0, czyli brak pomiaru kosztów (bramka nie przechodzi)."""

    sessions_at_min_size: int
    unreconciled_over_60s: int
    unhandled_rejections: int
    cost_deviation_ratio: Decimal
    cost_samples: int = 0

    def __post_init__(self) -> None:
        for name in ("sessions_at_min_size", "unreconciled_over_60s", "unhandled_rejections", "cost_samples"):
            _check_count(name, getattr(self, name))
        ratio = self.cost_deviation_ratio
        if not isinstance(ratio, Decimal):
            raise TypeError(f"GateL1.cost_deviation_ratio: expected Decimal, got {type(ratio).__name__} {ratio!r}")
        if ratio.is_nan():
            raise ValueError("GateL1.cost_deviation_ratio: must not be NaN")
        if ratio < 0:
            raise ValueError(f"GateL1.cost_deviation_ratio: must not be negative, got {ratio}")

    def passed(self, required_sessions: int) -> bool:
        """Warunki (spec §11): sesje ≥ wymagane, zero rozbieżności, zero odrzuceń, pomiar kosztów z co najmniej tylu
        próbek co wymagane sesje i koszty ≤ 2× model."""
        _check_required(required_sessions)
        return (
            self.sessions_at_min_size >= required_sessions
            and self.unreconciled_over_60s == 0
            and self.unhandled_rejections == 0
            and self.cost_samples >= required_sessions
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
        if self.cost_samples < required_sessions:
            reasons.append(f"brak pomiaru kosztów: użytecznych próbek {self.cost_samples} z {required_sessions}")
        if self.cost_deviation_ratio > COST_DEVIATION_LIMIT:
            ratio_text = format_ratio(self.cost_deviation_ratio, COST_DEVIATION_LIMIT)
            reasons.append(f"koszty zmierzone / model: {ratio_text} > {COST_DEVIATION_LIMIT} (przegląd kosztów)")
        return reasons


# --- odczyt zdarzeń -------------------------------------------------------------------------------------------------


def read_events(journal: Journal, source: str) -> list[tuple[RawEvent, dict[str, Any] | None]]:
    """Wiersze źródła z rozkodowanym ładunkiem JSON; None, gdy ładunek nie jest obiektem JSON (nieczytelny)."""
    rows = []
    for event in journal.events(source):
        try:
            data = json.loads(event.payload)
        except (ValueError, RecursionError):  # nie JSON albo zagnieżdżony tak głęboko, że parser się poddaje
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
    """Rola transakcji (`entry`, `smoke`, `unknown`, `invalid`) i wartość punktu z kontekstu wejścia (None: brak)."""

    role: str
    value_per_point: Decimal | None


def _usable_value_per_point(value: object) -> Decimal | None:
    """Wartość punktu z kontekstu albo None, gdy jest uszkodzona.

    Silnik zapisuje ją jako napis (`Decimal` przez `dump`), więc tylko napis jest przyjmowany (bez float, bool, null,
    list). Skończona i w zakresie [`MIN_VALUE_PER_POINT`, `MAX_VALUE_PER_POINT`]; nic nie rzuca.
    """
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except (ArithmeticError, ValueError):
        return None
    return number if number.is_finite() and MIN_VALUE_PER_POINT <= number <= MAX_VALUE_PER_POINT else None


def _parse_context(text: str) -> tuple[datetime, datetime, Decimal | None] | None:
    try:
        ctx = json.loads(text)
        signal_ts, submit_ts = datetime.fromisoformat(ctx["signal_ts"]), datetime.fromisoformat(ctx["submit_ts"])
        vpp = _usable_value_per_point(ctx.get("value_per_point"))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):  # także zbyt głęboko zagnieżdżony JSON
        return None
    return signal_ts, submit_ts, vpp


def trade_contexts(journal: Journal, trades: list[TradeRecord]) -> list[TradeContext]:
    """Rola i wartość punktu każdej transakcji z kontekstu wejścia; definicja w docstringu modułu.

    Dla dnia sygnału i dni sąsiednich (nazwa zlecenia nosi dzień handlowy, a czas UTC może wypaść w innym dniu)
    przegląda zlecenia `gws-{dzień}-{instrument}-{seq}-{entry|smoke}`, czyta ich `entry_ctx` i dopasowuje transakcję
    po `signal_ts` i `submit_ts`. Bez dopasowania: rola `unknown`; z dopasowaniem, ale z uszkodzoną wartością punktu:
    rola `invalid`. W obu przypadkach wartość punktu to None. Tylko publiczne metody dziennika.
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
                        signal_ts, submit_ts, vpp = parsed
                        usable_role = role if vpp is not None else ROLE_INVALID
                        index.setdefault((signal_ts, submit_ts), TradeContext(usable_role, vpp))
    unknown = TradeContext(ROLE_UNKNOWN, None)
    return [index.get((trade.signal_ts, trade.submit_ts), unknown) for trade in trades]


def trade_roles(journal: Journal, trades: list[TradeRecord]) -> list[str]:
    """Rola każdej transakcji wg kontekstu wejścia: `entry`, `smoke`, `unknown` (brak kontekstu) albo `invalid`
    (kontekst z uszkodzoną wartością punktu). Rolę `no_quote` (nieużyteczne notowanie) nadaje dopiero `l1_details`."""
    return [context.role for context in trade_contexts(journal, trades)]


# --- koszty ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostPoints:
    """Pomiar kosztu w punktach, bez zaokrągleń księgi.

    `trades`: liczba próbek (z użytecznym notowaniem: ask > bid); `spread`, `spread_max`: średni i największy spread
    decyzji;
    `entry_slippage`, `entry_slippage_max`: średni i największy NIEKORZYSTNY poślizg wejścia (poprawa ceny jako 0);
    `ratio`: WERDYKT, Σ niekorzystnego poślizgu / (n × `entry_slippage_points`); `combined_ratio`: informacyjnie,
    Σ(spread + poślizg ze znakiem) / Σ(spread + model), nie mniej niż 0; `stop_exits`: liczba wyjść STOP wśród
    WSZYSTKICH podanych transakcji (ich spread i poślizg nie są mierzone).
    """

    trades: int
    spread: Decimal | None
    spread_max: Decimal | None
    entry_slippage: Decimal | None
    entry_slippage_max: Decimal | None
    ratio: Decimal
    combined_ratio: Decimal
    stop_exits: int


def _entry_slippage(trade: TradeRecord) -> Decimal:
    """O ile wypełnienie wejścia jest GORSZE od notowania decyzji (BUY: wypełnienie − ask, SELL: bid − wypełnienie)."""
    if trade.side is Side.BUY:
        return _EXACT.subtract(trade.entry_fill, trade.decision_ask)
    return _EXACT.subtract(trade.decision_bid, trade.entry_fill)


def cost_points(trades: list[TradeRecord], costs: CostConfig, include: Sequence[bool] | None = None) -> CostPoints:
    """Pomiar w punktach (werdykt kosztów bramki L1; definicja w docstringu modułu).

    `include`: maska transakcji branych pod uwagę, jedna flaga na transakcję (domyślnie wszystkie); maska innej długości
    to `ValueError`, nie ciche obcięcie. Transakcje o spreadzie 0 (zdegradowane wiersze, gdzie notowanie = poziom
    otwarcia) nie są próbkami. Sumy są dokładne; średnie to iloraz o precyzji 28 cyfr.
    """
    if include is not None and len(include) != len(trades):
        raise ValueError(f"include: expected one flag per trade ({len(trades)}), got {len(include)}")
    stop_exits = sum(1 for t in trades if t.exit_reason == "STOP")
    chosen = trades if include is None else [t for t, keep in zip(trades, include) if keep]
    usable = [t for t in chosen if t.decision_ask > t.decision_bid]
    if not usable:
        return CostPoints(0, None, None, None, None, Decimal(0), Decimal(0), stop_exits)
    spreads = [_EXACT.subtract(t.decision_ask, t.decision_bid) for t in usable]
    slips = [_entry_slippage(t) for t in usable]
    adverse = [max(slip, Decimal(0)) for slip in slips]
    count = Decimal(len(usable))
    model = costs.entry_slippage_points
    modelled = _EXACT.multiply(count, model)
    if modelled > 0:
        ratio = _RATIO.divide(exact_sum(adverse), modelled)
    else:
        ratio = Decimal(0) if exact_sum(adverse) == 0 else Decimal("Infinity")
    combined_modelled = exact_sum([*spreads, *([model] * len(usable))])  # spread > 0, więc model > 0
    combined = max(_RATIO.divide(exact_sum([*spreads, *slips]), combined_modelled), Decimal(0))
    return CostPoints(
        len(usable), _RATIO.divide(exact_sum(spreads), count), max(spreads),
        _RATIO.divide(exact_sum(adverse), count), max(adverse), ratio, combined, stop_exits,
    )  # fmt: skip


def _eur_ratio(
    trades: list[TradeRecord], roles: Sequence[str], contexts: list[TradeContext], costs: CostConfig
) -> tuple[Decimal, int]:
    """Informacyjny stosunek kosztów w EUR i liczba transakcji, z których go policzono (role `entry` i `smoke`).

    Każda taka transakcja ma użyteczne notowanie i wartość punktu z zakresu, więc model w EUR jest dodatni.
    """
    chosen = [(t, c) for t, role, c in zip(trades, roles, contexts) if role in COST_ROLES]
    if not chosen:
        return Decimal(0), 0
    measured = exact_sum(_EXACT.add(t.spread_cost, t.slippage_penalty) for t, _ in chosen)
    modelled_parts = []
    for trade, context in chosen:
        slippage_points = costs.entry_slippage_points
        if trade.exit_reason == "STOP":
            slippage_points = _EXACT.add(slippage_points, costs.stop_exit_extra_slippage_points)
        points = _EXACT.add(_EXACT.subtract(trade.decision_ask, trade.decision_bid), slippage_points)
        modelled_parts.append(_EXACT.multiply(points, _EXACT.multiply(trade.size, context.value_per_point)))
    return _RATIO.divide(measured, exact_sum(modelled_parts)), len(chosen)


# --- status ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class L1Details:
    """Status L1 z danymi pomocniczymi dla raportu.

    `gate`: liczby werdyktu (z liczbą próbek kosztu); `roles`: rola każdej transakcji księgi (`entry`, `smoke`,
    `unknown`, `invalid`, `no_quote`); `points`: pomiar w punktach; `eur_ratio`, `eur_samples`: koszty w EUR
    (informacja) i liczba transakcji, z których je policzono (0: brak danych); `mismatch_days`, `rejection_days`: dni
    lokalne policzonych incydentów (po jednym na incydent).
    """

    gate: GateL1
    roles: tuple[str, ...]
    points: CostPoints
    eur_ratio: Decimal
    eur_samples: int
    mismatch_days: tuple[date, ...]
    rejection_days: tuple[date, ...]

    @property
    def excluded_trades(self) -> int:
        """Liczba transakcji wykluczonych z sesji i z pomiaru kosztów: bez kontekstu (`unknown`), z uszkodzoną wartością
        punktu (`invalid`) albo bez użytecznego notowania decyzji (`no_quote`)."""
        return sum(1 for role in self.roles if role not in COST_ROLES)


def _default_costs() -> CostConfig:
    return load_config(Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR))[2]


def l1_details(
    journal: Journal,
    *,
    min_size: Decimal | None = None,
    costs: CostConfig | None = None,
    trades: list[TradeRecord] | None = None,
) -> L1Details:
    """Status bramki L1 z danymi pomocniczymi (definicje w docstringu modułu).

    `min_size`: wielkość minimalna sesji (domyślnie najmniejsza wśród wejść strategii). `costs`: stawki modelu
    (domyślnie `costs_v1.toml` z `GW_CONFIG_DIR` albo `config/`). `trades`: księga już odczytana przez wywołującego
    (domyślnie `journal.trades()`). Rola `no_quote` trafia do transakcji z poprawnym kontekstem (`entry`, `smoke`), ale
    z notowaniem decyzji ask <= bid: nie jest sesją ani próbką, tak samo jak `unknown` i `invalid`.
    """
    if min_size is not None and (not isinstance(min_size, Decimal) or min_size <= 0):
        raise ValueError(f"min_size: expected a positive Decimal, got {min_size!r}")
    costs = costs if costs is not None else _default_costs()
    trades = journal.trades() if trades is None else trades
    contexts = trade_contexts(journal, trades)
    roles = tuple(
        ROLE_NO_QUOTE if context.role in COST_ROLES and not trade.decision_ask > trade.decision_bid else context.role
        for trade, context in zip(trades, contexts)
    )
    entries = [t for t, role in zip(trades, roles) if role == ROLE_ENTRY]
    floor = min_size if min_size is not None else min((t.size for t in entries), default=None)
    sessions = len({trade_day(t) for t in entries if t.size == floor})
    mismatches, rejections = _mismatch_days(journal), _rejection_days(journal)
    points = cost_points(trades, costs, [role in COST_ROLES for role in roles])
    eur_ratio, eur_samples = _eur_ratio(trades, roles, contexts, costs)
    return L1Details(
        gate=GateL1(sessions, len(mismatches), len(rejections), points.ratio, cost_samples=points.trades),
        roles=roles,
        points=points,
        eur_ratio=eur_ratio,
        eur_samples=eur_samples,
        mismatch_days=tuple(mismatches),
        rejection_days=tuple(rejections),
    )


def gate_l1_status(journal: Journal, *, min_size: Decimal | None = None, costs: CostConfig | None = None) -> GateL1:
    """Liczby bramki L1 z dziennika (definicje w docstringu modułu); szczegóły: `l1_details`."""
    return l1_details(journal, min_size=min_size, costs=costs).gate
