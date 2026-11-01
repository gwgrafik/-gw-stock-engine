"""Adapter Capital.com, część handlowa: wejście, potwierdzenie, pozycje, stop, zamknięcie, historia zamknięć i
zlecenia oczekujące (`TradingMixin`, klasa bazowa `CapitalComBroker`).

Moduł nie zna adresu hosta i nie rozmawia z siecią sam: korzysta z transportu `CapitalComBroker` (`_request`:
tokeny, limity, 401 → jedno ponowne logowanie i jedna powtórka, mapowanie błędów; `_error`: jedyna droga tworzenia
wyjątków, czyści sekrety; `_parse`: `ParseError` → `BrokerError` z nazwą pola). Źródła: notatki
`docs/ops/capitalcom-api-notes.md` §2.10–§2.15, §2.19, §2.22 (`GET /workingorders`) i rozstrzygnięcia T13 w
`progress.md`; założenia mają numery C w sekcji 6 notatek.

Zasady:
- `open_position`: `POST /positions` z `stopDistance` (stop atomowo), `guaranteedStop=false`, `trailingStop=false`,
  liczby dokładnie jako tekst `Decimal` (nigdy `float`); odstęp zleceń ≥ 0,1 s; NIGDY ponawiane przy timeoucie, 5xx,
  429 ani błędzie połączenia (wynik nieznany rozstrzyga menedżer zleceń); jedyna powtórka to ta po 401 (żądanie nie
  zostało przetworzone); odpowiedź = tylko `dealReference` → `EntryAck`;
- `confirm`: 404 `error.not-found.dealReference` → None (WYŁĄCZNIE ten kształt); dealStatus REJECTED →
  `BrokerRejected(reason)`; ACCEPTED → migawka `OpenPosition` (id pozycji z `affectedDeals` `OPENED`, cena i
  wielkość z potwierdzenia, `opened_at` z `createdDateUTC` pozycji, a gdy pozycji już nie ma: z wiersza otwarcia w
  historii); każdy inny status lub kształt → `BrokerError` (nigdy odrzucenie); migawka jest zapamiętywana i nie
  zmienia się po zamknięciu pozycji;
- stop pozycji: klucze `stopLevel`/`stopDistance` w odpowiedzi → wartość brokera (jawny `null` → brak stopu);
  kluczy brak (jak w oficjalnych przykładach) → zapis adaptera: poziom wykonania ∓ wysłane `stopDistance` (BUY
  minus, SELL plus) albo poziom z potwierdzonego `set_stop`, `stop_derived=True`; brak zapisu (np. po restarcie) →
  None (C5, C24);
- `close_position`: ZAWSZE `DELETE /positions/{dealId}` (nigdy przeciwne zlecenie), potem `GET /confirms` z
  referencją z odpowiedzi: ACCEPTED → None, REJECTED → `BrokerRejected(reason)`, cokolwiek innego (404, błąd,
  timeout, dziwny status, brak referencji) → `BrokerTimeout("close outcome unknown")`; 404 `error.not-found.dealId`
  na `DELETE` → `BrokerRejected("POSITION_NOT_FOUND")`; `set_stop` tak samo (`PUT` ze `stopLevel`);
- `closed_since`: kompletność albo wyjątek (szczegóły w `capitalcom_history`); zakres > 14 dni →
  `HISTORY_RANGE_TOO_LARGE` przed zapytaniem; okna ≤ 1 doba; obcięte okno dzielone; nieudane okno → wyjątek;
- pamięć adaptera (wysłane odległości stopu, stopy deali, strony deali, migawki potwierdzeń) jest ograniczona do
  `RECORD_LIMIT` wpisów na tablicę (najstarsze wypadają).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from gwstock.broker import capitalcom_history as history
from gwstock.broker.capitalcom_history import DealRow
from gwstock.broker.capitalcom_parse import (
    Opening,
    ParseError,
    build,
    check_ident,
    parse_deal_status,
    parse_opening,
    parse_position,
    parse_positions,
    parse_reference,
    parse_working_orders,
    shown,
)
from gwstock.types import (
    BrokerError,
    BrokerRejected,
    BrokerTimeout,
    ClosedDeal,
    EntryAck,
    EntryRequest,
    OpenPosition,
    Side,
    WorkingOrder,
)

RECORD_LIMIT = 256
"""Najwięcej wpisów w każdej tablicy pamięci adaptera (referencje, deale, migawki)."""
NOT_FOUND_DEAL = "error.not-found.dealId"
NOT_FOUND_REFERENCE = "error.not-found.dealReference"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
_MAX_REASON_LEN = 200
_MAX_WAIT_FOR_WINDOW_END_S = 1.0  # koniec ostatniego okna historii to „teraz” w górę do pełnej sekundy


def _remember(table: OrderedDict, key: str, value: Any) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > RECORD_LIMIT:
        table.popitem(last=False)


def _stop_from(level: Decimal, side: Side, distance: Decimal) -> Decimal:
    """Stop pozycji kupna leży poniżej ceny wykonania, sprzedaży powyżej (notatki §2.10, C6)."""
    return level - distance if side is Side.BUY else level + distance


def _since_arg(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("closed_since: since must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _attempt(parser: Callable[..., Any], *args: Any) -> tuple[Any, str | None]:
    """(wynik, None) albo (None, opis problemu) zamiast `ParseError`, bez łańcucha wyjątków."""
    try:
        return parser(*args), None
    except ParseError as exc:
        return None, f"unexpected response: {exc}"
    except ArithmeticError as exc:
        return None, f"a number is out of range ({type(exc).__name__})"


class TradingMixin:
    """Część handlowa adaptera. Wymaga od klasy docelowej: `_clock`, `_request`, `_error`, `_scrub`, `_parse`."""

    def _init_trading(self) -> None:
        self.unknown_close_sources = 0
        self._sent_stops: OrderedDict[str, tuple[Side, Decimal]] = OrderedDict()  # dealReference → (strona, odległość)
        self._deal_stops: OrderedDict[str, Decimal] = OrderedDict()  # dealId → nasz stop (wyliczony albo z set_stop)
        self._deal_sides: OrderedDict[str, Side] = OrderedDict()  # dealId → strona (potwierdzenie, lista pozycji)
        self._snapshots: OrderedDict[str, OpenPosition] = OrderedDict()  # dealReference → migawka potwierdzenia

    # --- wejście i potwierdzenie ---------------------------------------------------------------------

    async def open_position(self, req: EntryRequest) -> EntryAck:
        """`POST /positions` ze stopem atomowo; zwraca wyłącznie referencję (`o_...`), wynik zna dopiero `confirm`."""
        if not isinstance(req, EntryRequest):
            raise TypeError(f"open_position: expected EntryRequest, got {type(req).__name__}")
        epic = check_ident("epic", req.epic)
        if req.size <= 0 or req.stop_distance <= 0:
            raise ValueError("open_position: size and stop_distance must be positive")
        body = {
            "epic": epic,
            "direction": req.side.value,
            "size": req.size,
            "guaranteedStop": False,
            "trailingStop": False,
            "stopDistance": req.stop_distance,
        }
        payload = await self._request("POST", "/positions", body=body, order=True)
        reference = self._parse("POST /positions", parse_reference, payload)
        _remember(self._sent_stops, reference, (req.side, req.stop_distance))
        return EntryAck(reference)

    async def confirm(self, deal_reference: str) -> OpenPosition | None:
        """Migawka wykonania zlecenia o tej referencji (patrz docstring modułu); None tylko dla nieznanej referencji."""
        reference = check_ident("deal_reference", deal_reference)
        cached = self._snapshots.get(reference)
        if cached is not None:
            return cached
        where = f"GET /confirms/{reference}"
        payload = await self._request("GET", f"/confirms/{reference}", not_found=NOT_FOUND_REFERENCE)
        if payload is None:
            return None
        status, reason = self._parse(where, parse_deal_status, payload, reference)
        if status == "REJECTED":
            raise self._rejection(reason)
        if status != "ACCEPTED":
            raise self._error(BrokerError, f"{where}: unexpected dealStatus {shown(status)}")
        opening = self._parse(where, parse_opening, payload)
        snapshot = await self._snapshot(reference, opening, where)
        _remember(self._snapshots, reference, snapshot)
        return snapshot

    async def _snapshot(self, reference: str, opening: Opening, where: str) -> OpenPosition:
        """Migawka z potwierdzenia i pozycji (`GET /positions/{dealId}`: czas otwarcia, stop, kontrola krzyżowa)."""
        path = f"/positions/{opening.deal_id}"
        reply = await self._request("GET", path, not_found=NOT_FOUND_DEAL)
        stop_reported, stop = False, None
        if reply is not None:
            row = self._parse(f"GET {path}", parse_position, reply)
            checks = (
                ("dealId", row.deal_id == opening.deal_id),
                ("epic", row.epic == opening.epic),
                ("direction", row.side is opening.side),
                # pozycja.workingOrderId == potwierdzenie.dealId (notatki §2.15, C2), gdy oba są podane
                ("workingOrderId", None in (row.working_order_id, opening.order_id)
                 or row.working_order_id == opening.order_id),
            )  # fmt: skip
            mismatch = [name for name, ok in checks if not ok]
            if mismatch:
                raise self._error(
                    BrokerError, f"GET {path}: the position does not match the confirmation ({', '.join(mismatch)})"
                )
            opened_at, stop_reported, stop = row.opened_at, row.stop_reported, row.stop_level
        else:  # pozycja już zamknięta (stop, ręcznie): czas otwarcia z wiersza otwarcia w historii
            opened_at = await self._opened_at_from_history(opening, where)
        derived = False
        if not stop_reported:
            sent = self._sent_stops.get(reference)
            if sent is not None and sent[0] is opening.side:
                stop, derived = _stop_from(opening.level, opening.side, sent[1]), True
        snapshot = self._parse(
            where, build, OpenPosition, "confirm", opening.deal_id, opening.epic, opening.side, opening.size,
            opening.level, stop, opened_at, derived,
        )  # fmt: skip
        _remember(self._deal_sides, opening.deal_id, opening.side)
        if derived:
            _remember(self._deal_stops, opening.deal_id, stop)
        return snapshot

    async def _opened_at_from_history(self, opening: Opening, where: str) -> datetime:
        """`dateUTC` jedynego wiersza otwarcia (kierunek pozycji) tego dealId w ostatniej dobie; inaczej wyjątek."""
        end = history.ceil_second(self._clock.now_utc())
        rows = await self._activity_rows([(end - history.WINDOW_MAX, end)], deal_id=opening.deal_id)
        opens = [r for r in rows if r.deal_id == opening.deal_id and r.direction is opening.side]
        if len(opens) != 1:
            raise self._error(
                BrokerError,
                f"{where}: the position is no longer open and its opening appears {len(opens)} times in the last day "
                "of history (expected exactly once)",
            )
        return opens[0].ts

    # --- pozycje, stop, zamknięcie --------------------------------------------------------------------

    async def list_positions(self) -> list[OpenPosition]:
        """Wszystkie pozycje aktywnego rachunku (`GET /positions`), w kolejności brokera; stop wg docstringu modułu."""
        where = "GET /positions"
        rows = self._parse(where, parse_positions, await self._request("GET", "/positions"))
        result: list[OpenPosition] = []
        for row in rows:
            stop, derived = row.stop_level, False
            if not row.stop_reported:
                stop = self._deal_stops.get(row.deal_id)
                derived = stop is not None
            position = self._parse(
                where, build, OpenPosition, "positions", row.deal_id, row.epic, row.side, row.size, row.level, stop,
                row.opened_at, derived,
            )  # fmt: skip
            result.append(position)
        for row in rows:
            _remember(self._deal_sides, row.deal_id, row.side)
        return result

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        """`PUT /positions/{dealId}` ze `stopLevel`, potem potwierdzenie; zapis stopu tylko po ACCEPTED."""
        deal_id = check_ident("deal_id", deal_id)
        if not isinstance(stop_level, Decimal):
            raise TypeError(f"set_stop: stop_level must be a Decimal, got {type(stop_level).__name__}")
        if not stop_level.is_finite() or stop_level <= 0:
            raise ValueError("set_stop: stop_level must be a positive finite Decimal")
        path = f"/positions/{deal_id}"
        payload = await self._request("PUT", path, body={"stopLevel": stop_level}, not_found=NOT_FOUND_DEAL)
        if payload is None:
            raise self._error(BrokerRejected, POSITION_NOT_FOUND)
        await self._await_outcome(f"PUT {path}", "stop", payload)
        _remember(self._deal_stops, deal_id, stop_level)

    async def close_position(self, deal_id: str) -> None:
        """Zamknięcie w całości: wyłącznie `DELETE /positions/{dealId}`, wynik z `GET /confirms` (docstring modułu)."""
        deal_id = check_ident("deal_id", deal_id)
        path = f"/positions/{deal_id}"
        payload = await self._request("DELETE", path, not_found=NOT_FOUND_DEAL)
        if payload is None:  # deal już nie istnieje: czyste 404 bez skutku (do potwierdzenia na próbie smoke)
            raise self._error(BrokerRejected, POSITION_NOT_FOUND)
        await self._await_outcome(f"DELETE {path}", "close", payload)
        self._deal_stops.pop(deal_id, None)

    async def _await_outcome(self, where: str, what: str, payload: Any) -> None:
        """Wynik `PUT`/`DELETE` z potwierdzenia: ACCEPTED → None; REJECTED → `BrokerRejected`; reszta → `BrokerTimeout`.

        Wyjątek jest tworzony poza blokiem `except`, więc nie ma łańcucha (`__context__`) z obcymi obiektami.
        """
        status, reason = None, None
        reference, problem = _attempt(parse_reference, payload)
        if problem is None:
            reply = None
            try:
                reply = await self._request("GET", f"/confirms/{reference}", not_found=NOT_FOUND_REFERENCE)
            except BrokerError as exc:  # timeout, błąd, wygasła sesja: wynik operacji nieznany
                problem = f"{type(exc).__name__}: {exc}"
            if problem is None and reply is None:
                problem = f"GET /confirms/{reference}: unknown reference (HTTP 404)"
            if problem is None:
                parsed, problem = _attempt(parse_deal_status, reply, reference)
                if parsed is not None:
                    status, reason = parsed
        if status == "REJECTED":
            raise self._rejection(reason)
        if status == "ACCEPTED":
            return
        if problem is None:
            problem = f"unexpected dealStatus {shown(status)}"
        raise self._error(BrokerTimeout, f"{where}: {what} outcome unknown ({problem})")

    def _rejection(self, reason: str | None) -> BrokerRejected:
        """`BrokerRejected` z powodem brokera: najpierw bez sekretów, potem przycięty (nigdy pół sekretu)."""
        return self._error(BrokerRejected, self._scrub(reason or "REJECTED")[:_MAX_REASON_LEN])

    # --- historia zamknięć ---------------------------------------------------------------------------

    async def closed_since(self, since: datetime) -> list[ClosedDeal]:
        """WSZYSTKIE zamknięcia z `ts >= since` (każdy instrument), rosnąco po czasie, albo wyjątek."""
        since = _since_arg(since)
        now = self._clock.now_utc()
        if now - since > history.HISTORY_MAX_RANGE:
            raise self._error(
                BrokerError,
                f"HISTORY_RANGE_TOO_LARGE: closed_since reads at most {history.HISTORY_MAX_RANGE.days} days of "
                f"history, asked for {now - since}; nothing was requested",
            )
        if since > now:
            return []
        rows = await self._activity_rows(history.history_windows(since, now))
        found = history.classify(rows, self._deal_sides)
        if found.undecided and not found.conflicting:
            await self.list_positions()  # strona otwartych deali z listy brokera (zapisywana w pamięci)
            found = history.classify(rows, self._deal_sides)
        if found.conflicting:
            raise self._error(BrokerError, self._history_problem("HISTORY_INCONSISTENT", found.conflicting,
                                                                 "rows contradict each other or the known side"))  # fmt: skip
        if found.undecided:
            raise self._error(BrokerError, self._history_problem("HISTORY_AMBIGUOUS", found.undecided,
                                                                 "cannot tell the opening from a closing"))  # fmt: skip
        closed: list[ClosedDeal] = []
        unknown = 0
        for row in found.closures:
            if row.ts < since:
                continue
            reason, known = history.reason_for(row.source)
            unknown += not known
            where = "GET /history/activity"
            closed.append(self._parse(where, build, ClosedDeal, "activity", row.deal_id, row.epic, row.level, reason,
                                      row.ts))  # fmt: skip
        self.unknown_close_sources += unknown
        return closed

    @staticmethod
    def _history_problem(code: str, deals: set[str], what: str) -> str:
        shown_ids = ", ".join(sorted(deals)[:3]) + (", ..." if len(deals) > 3 else "")
        return f"{code}: {what} for {len(deals)} deal(s) in the history ({shown_ids}); nothing returned"

    async def _activity_rows(
        self, windows: list[tuple[datetime, datetime]], *, deal_id: str | None = None
    ) -> list[DealRow]:
        """Wiersze `POSITION`/`ACCEPTED` ze wszystkich okien (bez duplikatów); obcięte okno dzielone, błąd → wyjątek."""
        where = "GET /history/activity"
        queue = deque(windows)
        found: dict[tuple, DealRow] = {}
        sent = 0
        while queue:
            start, end = queue.popleft()
            if sent >= history.MAX_HISTORY_REQUESTS:
                raise self._error(
                    BrokerError,
                    f"HISTORY_TRUNCATED: more than {history.MAX_HISTORY_REQUESTS} history requests would be needed; "
                    "nothing returned",
                )
            wait = (end - self._clock.now_utc()).total_seconds()
            if wait > 0:  # `to` nigdy w przyszłości (wariant ostrożny): koniec okna to teraz w górę do sekundy
                await self._clock.sleep(min(wait, _MAX_WAIT_FOR_WINDOW_END_S))
            params = {"from": history.api_time(start), "to": history.api_time(end), "detailed": "true"}
            if deal_id is not None:
                params["dealId"] = deal_id
            sent += 1
            payload = await self._request("GET", "/history/activity", params=params)
            count, rows = self._parse(where, history.parse_activity, payload, start, end)
            if count >= history.ACTIVITY_SUSPECT_ROWS:
                halves = history.split_window(start, end)
                if halves is None:
                    raise self._error(
                        BrokerError,
                        f"HISTORY_TRUNCATED: {count} rows in a {int((end - start).total_seconds())} s window may be a "
                        "truncated answer and the window cannot be split further; nothing returned",
                    )
                queue.extendleft(reversed(halves))
                continue
            for row in rows:
                found.setdefault(row.key, row)
        return list(found.values())

    # --- zlecenia oczekujące -------------------------------------------------------------------------

    async def list_working_orders(self) -> list[WorkingOrder]:
        """Wszystkie zlecenia oczekujące aktywnego rachunku (`GET /workingorders`, notatki §2.22); tylko odczyt."""
        where = "GET /workingorders"
        return self._parse(where, parse_working_orders, await self._request("GET", "/workingorders"))
