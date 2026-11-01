"""Historia aktywności Capital.com (`GET /history/activity`): okna zapytań, odczyt wierszy i rozpoznanie zamknięć.

Czysty moduł bez sieci i bez adresów hostów; adapter (`capitalcom_trading`) robi zapytania i woła te funkcje.
Źródło: `docs/ops/capitalcom-api-notes.md` §2.19 i oficjalna specyfikacja [S1] (przykład "Filter list by dealId").

Zasady (kompletność albo wyjątek, nigdy lista częściowa):
- okna zapytań mają najwyżej dobę (maksimum API), sąsiednie okna zachodzą na siebie o 1 sekundę (włączność granic
  `from`/`to` jest nieopisana), początek = `since` w dół do pełnej sekundy, koniec = teraz W GÓRĘ do pełnej sekundy;
- odpowiedź z co najmniej `ACTIVITY_SUSPECT_ROWS` wierszami może być obcięta (limit nieudokumentowany, C27), więc
  okno dzieli się na połowy; okna nie da się podzielić albo zapytań jest więcej niż `MAX_HISTORY_REQUESTS` →
  wyjątek (adapter: `HISTORY_TRUNCATED`);
- liczą się wyłącznie wiersze typu `POSITION` ze statusem `ACCEPTED`; odrzucone operacje (`REJECTED`,
  `MODIFY_REJECT`, `CANCEL_REJECT`) są pomijane, inne udokumentowane typy (`WORKING_ORDER`, `EDIT_STOP_AND_LIMIT`,
  `SWAP`, `SYSTEM`) też; każdy inny typ albo status wiersza `POSITION` → `ParseError` (nie zgadujemy, czy to
  zamknięcie: C7c);
- wiersz otwarcia i wiersz zamknięcia różni tylko kierunek `details.direction` (zamknięcie ma kierunek przeciwny do
  pozycji), więc `classify` musi znać stronę pozycji: z pamięci adaptera (nasze wejścia, ostatnia lista pozycji), z
  wiersza zamknięcia systemowego (źródło inne niż `USER`: kierunek przeciwny do pozycji) albo z pary wierszy
  `USER` (najwcześniejszy jest otwarciem, jeśli jako jedyny ma swój kierunek); bez tego deal jest „nierozstrzygnięty”:
  adapter czyta świeżą listę pozycji, potem historię tego deala wstecz od `since` (`lookback_windows`, filtr
  `dealId`), a gdy i to nie pomoże, zgłasza wyjątek (`HISTORY_AMBIGUOUS`) zamiast zgadywać (C7d);
- wiersz w kierunku pozycji PO rzekomym zamknięciu (np. otwarcie przez API ze źródłem innym niż `USER`, z którego
  wywnioskowano by odwrotną stronę) → deal sprzeczny (`HISTORY_INCONSISTENT`), nigdy otwarcie zaksięgowane jako
  wyjście;
- przyczyna: `SL`/`TP` → "STOP", `USER` → "MANUAL", `CLOSE_OUT`/`DEALER`/`SYSTEM` → "FORCED", źródło nieznane →
  "FORCED" (nigdy "STOP") i licznik alarmowy adaptera (C7);
- identyczne wiersze (np. z nakładki okien) liczą się raz; różne zamknięcia jednego dealId zostają (częściowe
  zamknięcia: uzgadnianie zgłasza AMBIGUOUS_CLOSURE).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from gwstock.broker.capitalcom_parse import (
    ParseError,
    dec,
    get,
    ident,
    items,
    obj,
    path,
    shown,
    side_of,
    text,
    utc_stamp,
)
from gwstock.types import Side

HISTORY_MAX_RANGE = timedelta(days=14)
"""Najdłuższy obsługiwany zakres `closed_since` (dłuższy → `HISTORY_RANGE_TOO_LARGE` przed jakimkolwiek zapytaniem)."""
WINDOW_MAX = timedelta(days=1)
"""Maksymalny zakres `from`–`to` jednego zapytania (notatki §2.19, changelog [S1] 2023-07-04)."""
WINDOW_OVERLAP = timedelta(seconds=1)
"""Nakładka sąsiednich okien: wiersz na granicy nie zginie przy żadnej włączności `from`/`to`."""
ACTIVITY_SUSPECT_ROWS = 20
"""Tyle wierszy w jednej odpowiedzi to możliwe obcięcie (limit nieudokumentowany, C27): okno jest dzielone."""
MAX_HISTORY_REQUESTS = 64
"""Górny limit zapytań jednego odczytu historii (okna, podziały i wyszukiwanie wstecz razem); więcej → wyjątek."""
_MIN_SPLIT = timedelta(seconds=4)

POSITION = "POSITION"
_IGNORED_TYPES = frozenset({"WORKING_ORDER", "EDIT_STOP_AND_LIMIT", "SWAP", "SYSTEM"})
_DEAL_DONE = "ACCEPTED"
_NOTHING_HAPPENED = frozenset({"REJECTED", "MODIFY_REJECT", "CANCEL_REJECT"})
USER = "USER"
REASONS = {"SL": "STOP", "TP": "STOP", "USER": "MANUAL", "CLOSE_OUT": "FORCED", "DEALER": "FORCED", "SYSTEM": "FORCED"}
UNKNOWN_SOURCE_REASON = "FORCED"


def api_time(moment: datetime) -> str:
    """Parametr `from`/`to`: UTC bez strefy i bez ułamków (`YYYY-MM-DDTHH:MM:SS`), filtracja po `dateUTC`."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def floor_second(moment: datetime) -> datetime:
    return moment.replace(microsecond=0)


def ceil_second(moment: datetime) -> datetime:
    down = moment.replace(microsecond=0)
    return down if down == moment else down + timedelta(seconds=1)


def history_windows(since: datetime, now: datetime) -> list[tuple[datetime, datetime]]:
    """Okna [od, do] w pełnych sekundach pokrywające [since, now]: każde ≤ 1 doba, sąsiednie zachodzą o 1 s.

    Ostatnie okno ma co najmniej 1 s (`from` == `to` mogłoby zostać odrzucone albo nie objąć ułamka sekundy).
    """
    start = floor_second(since)
    end = max(ceil_second(now), start + timedelta(seconds=1))
    windows: list[tuple[datetime, datetime]] = []
    while True:
        stop = min(start + WINDOW_MAX, end)
        windows.append((start, stop))
        if stop >= end:
            return windows
        start = stop - WINDOW_OVERLAP


def lookback_windows(since: datetime, cap: datetime) -> list[tuple[datetime, datetime]]:
    """Okna WSTECZ od `since` do `cap` (najnowsze najpierw), każde ≤ 1 doba, sąsiednie zachodzą o 1 s."""
    floor_cap, end = floor_second(cap), ceil_second(since)
    windows: list[tuple[datetime, datetime]] = []
    while end > floor_cap:
        start = max(end - WINDOW_MAX, floor_cap)
        windows.append((start, end))
        if start <= floor_cap:
            break
        end = start + WINDOW_OVERLAP
    return windows


def split_window(start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
    """Dwie połowy okna (druga zaczyna się 1 s przed końcem pierwszej); okno krótsze niż 4 s → None."""
    if end - start < _MIN_SPLIT:
        return None
    middle = start + timedelta(seconds=int((end - start).total_seconds()) // 2)
    return [(start, middle), (middle - WINDOW_OVERLAP, end)]


@dataclass(frozen=True)
class DealRow:
    """Wiersz `POSITION`/`ACCEPTED`: otwarcie, dołożenie albo zamknięcie (rozstrzyga `classify`)."""

    deal_id: str
    epic: str
    ts: datetime
    source: str
    direction: Side
    level: Decimal
    key: tuple = field(repr=False)  # tożsamość zdarzenia (identyczne wiersze to jedno zdarzenie)


def parse_activity(payload: Any, start: datetime, end: datetime) -> tuple[int, list[DealRow]]:
    """(liczba wszystkich wierszy odpowiedzi, wiersze `POSITION`/`ACCEPTED`); zły kształt → `ParseError`.

    Każdy wiersz (każdego typu) musi mieć `dateUTC` w zapytanym oknie [start, end + 1 s): wiersz spoza niego znaczy,
    że broker nie zastosował filtra tak, jak zakładamy (np. zignorował zakres albo filtruje po czasie lokalnym), a
    wtedy lista mogłaby być niepełna bez śladu.
    """
    rows = items(get(obj(payload, ""), "activities", ""), "activities")
    deals: list[DealRow] = []
    for i, item in enumerate(rows):
        where = path("activities", i)
        row = obj(item, where)
        ts = utc_stamp(row, "dateUTC", where)
        if not start <= ts < end + WINDOW_OVERLAP:
            raise ParseError(f"{where}.dateUTC: outside the requested window")
        kind, status = text(row, "type", where), text(row, "status", where)
        if kind != POSITION:
            if kind not in _IGNORED_TYPES:
                raise ParseError(f"{where}.type: unexpected activity type {shown(kind)}")
            continue
        if status in _NOTHING_HAPPENED:
            continue
        if status != _DEAL_DONE:
            raise ParseError(f"{where}.status: unexpected status {shown(status)} for a POSITION activity")
        details = obj(get(row, "details", where), path(where, "details"))
        level = dec(details, "level", path(where, "details"))
        if level <= 0:
            raise ParseError(f"{where}.details.level: expected a positive price")
        deal_id, epic, source = ident(row, "dealId", where), text(row, "epic", where), text(row, "source", where)
        direction = side_of(details, "direction", path(where, "details"))
        key = (deal_id, ts, epic, source, direction, level, repr(details.get("size")))
        deals.append(DealRow(deal_id, epic, ts, source, direction, level, key))
    return len(rows), deals


def opposite(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY


def reason_for(source: str) -> tuple[str, bool]:
    """(przyczyna `ClosedDeal.reason`, czy źródło jest znane); nieznane → "FORCED", nigdy "STOP"."""
    reason = REASONS.get(source)
    return (reason, True) if reason is not None else (UNKNOWN_SOURCE_REASON, False)


@dataclass
class Classification:
    closures: list[DealRow] = field(default_factory=list)
    undecided: set[str] = field(default_factory=set)  # nie wiadomo, który wiersz jest otwarciem
    conflicting: set[str] = field(default_factory=set)  # wiersze przeczą sobie albo znanej stronie pozycji


def classify(rows: list[DealRow], sides: Mapping[str, Side]) -> Classification:
    """Zamknięcia spośród wierszy `POSITION`/`ACCEPTED` (patrz docstring modułu); kolejność: czas, dealId."""
    by_deal: dict[str, list[DealRow]] = {}
    for row in sorted(rows, key=lambda r: (r.ts, r.deal_id)):
        by_deal.setdefault(row.deal_id, []).append(row)
    result = Classification()
    for deal_id, deal_rows in by_deal.items():
        side = sides.get(deal_id)
        system = {r.direction for r in deal_rows if r.source != USER}  # zamknięcia systemowe (stop, wymuszenie)
        if side is None:
            if len(system) > 1:
                result.conflicting.add(deal_id)
                continue
            if system:
                side = opposite(next(iter(system)))
            else:
                first = deal_rows[0].direction
                if sum(r.direction is first for r in deal_rows) != 1 or len(deal_rows) < 2:
                    result.undecided.add(deal_id)
                    continue
                side = first  # najwcześniejszy wiersz jest jedynym w swoim kierunku: to otwarcie
        elif side in system:
            result.conflicting.add(deal_id)
            continue
        closures = [r for r in deal_rows if r.direction is not side]
        if closures and any(r.direction is side and r.ts > closures[0].ts for r in deal_rows):
            # wiersz strony pozycji PO rzekomym zamknięciu: wnioskowana strona (np. z wiersza systemowego, gdy
            # otwarcie przez API nie ma źródła USER) albo wiersz jest błędny; nie księgujemy otwarcia jako wyjścia
            result.conflicting.add(deal_id)
            continue
        result.closures.extend(closures)
    result.closures.sort(key=lambda r: (r.ts, r.deal_id))
    return result
