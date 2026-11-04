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
  `BrokerRejected(reason)`; ACCEPTED → migawka `OpenPosition` (id pozycji z jedynego wpisu `affectedDeals`, który
  musi mieć status `OPENED`, cena i wielkość z potwierdzenia, `opened_at` z `createdDateUTC` pozycji, a gdy pozycji
  już nie ma: z wiersza otwarcia w historii); dodatkowy wpis `affectedDeals` (netting z inną pozycją, C15) albo
  odczyt pozycji niezgodny z potwierdzeniem (dealId, epic, kierunek, wielkość, `workingOrderId`) → `BrokerError`
  (wynik nieznany, nie czyste wykonanie); każdy inny status lub kształt → `BrokerError` (nigdy odrzucenie); migawka
  jest zapamiętywana i nie zmienia się po zamknięciu pozycji;
- stop pozycji: klucze `stopLevel`/`stopDistance` w odpowiedzi → wartość brokera (jawny `null` → brak stopu);
  kluczy brak (jak w oficjalnych przykładach) → zapis adaptera: poziom wykonania ∓ wysłane `stopDistance` (BUY
  minus, SELL plus; sprawdzony ze `stopLevel` potwierdzenia, jeśli je ma) albo poziom z potwierdzonego `set_stop`,
  `stop_derived=True`; brak zapisu (np. po restarcie) → None (C5, C24); gdy JAKAKOLWIEK odpowiedź z pozycją
  pokazała już klucze stopu, brak klucza znaczy brak stopu (None), a zapis adaptera nie jest już używany;
- cel zysku (zadanie 7, R4, projekt §3.1; pola celu NIEPOTWIERDZONE na rachunku, C5/C5d/C7, do sprawdzenia skryptem
  research/diag_fx.py): `TargetedEntryRequest` dodaje do `POST /positions` `profitDistance` (po `stopDistance`; zwykły
  `EntryRequest` ma ciało bez zmian co do bajta) i NIGDY nie idzie bez celu; cel pozycji tą samą regułą co stop:
  klucz `profitLevel`/`profitDistance` w odpowiedzi → wartość brokera (jawny `null` → brak celu), kluczy brak →
  zapis adaptera (wykonanie ± wysłana odległość, `target_derived=True`, sprawdzony z `profitLevel` potwierdzenia),
  a gdy JAKAKOLWIEK odpowiedź pokazała klucze celu, brak klucza znaczy brak celu; pozycja z celem to
  `TargetedOpenPosition`, bez celu zwykły `OpenPosition`;
- `close_position`: ZAWSZE `DELETE /positions/{dealId}` (nigdy przeciwne zlecenie), potem `GET /confirms` z
  referencją z odpowiedzi i `GET /positions/{dealId}`: sukces tylko przy ACCEPTED, które przypisuje zamknięcie
  naszemu dealId (`PUT` i `DELETE` dzielą referencję `p_<dealId>`, C3), i 404 pozycji; REJECTED →
  `BrokerRejected(reason)`; cokolwiek innego → `BrokerTimeout("close outcome unknown")`; 404
  `error.not-found.dealId` na `DELETE` → `BrokerRejected("POSITION_NOT_FOUND")`;
- `set_protection` (pasma z celem): jeden `PUT` ze `stopLevel` I `profitLevel` (NIEPOTWIERDZONE, C5d, czy `PUT` bez
  `profitLevel` kasuje cel, więc oba poziomy idą zawsze razem); potwierdzenie `AMENDED` i odczyt kontrolny sprawdzają
  OBA poziomy (różnica albo `null` → `BrokerError`; wynik nieznany → `BrokerTimeout`); `target_level=None` → `PUT` tylko
  ze stopem i pozycja nadal mająca cel kończy się `BrokerError`; `set_stop` zostaje bez zmian (DE40 v1);
- `set_stop`: `PUT` ze `stopLevel`, potwierdzenie z naszym dealId jako `AMENDED` (inaczej `BrokerTimeout`), jego
  `stopLevel` i `stopLevel` pozycji (jeśli broker je podaje; sam `stopDistance` nie jest porównywany, C6) równe
  żądanemu (inaczej `BrokerError`); broker pokazywał już klucze stopu, a pozycja po `PUT` nie ma żadnego →
  `BrokerTimeout` bez zapisu;
- powód odrzucenia w potwierdzeniu nazywający zamknięty rynek → `MarketClosedRejection` (jednocześnie
  `BrokerRejected` i `MarketClosed`);
- `closed_since`: kompletność albo wyjątek (szczegóły w `capitalcom_history`); zakres > 14 dni →
  `HISTORY_RANGE_TOO_LARGE` przed zapytaniem; okna ≤ 1 doba; obcięte okno dzielone; nieudane okno → wyjątek; deal,
  którego otwarcia nie ma w zakresie, jest szukany wstecz z filtrem `dealId` (do 14 dni, wspólny limit zapytań;
  wyniki okien w całości przed `since` i skończonych co najmniej `LOOKBACK_CACHE_MIN_AGE` temu są zapamiętywane,
  najwyżej `LOOKBACK_CACHE_LIMIT` okien);
- pamięć adaptera (wysłane odległości stopu i celu, stopy i cele deali, strony deali, migawki potwierdzeń) jest
  ograniczona do `RECORD_LIMIT` wpisów na tablicę (najstarsze wypadają).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from gwstock.broker import capitalcom_history as history
from gwstock.broker.capitalcom_history import DealRow
from gwstock.broker.capitalcom_parse import (
    Opening,
    ParseError,
    build,
    amended_target,
    check_amendment,
    check_closure,
    check_ident,
    is_market_closed_code,
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
    MarketClosed,
    OpenPosition,
    Side,
    TargetedEntryRequest,
    TargetedOpenPosition,
    WorkingOrder,
)

RECORD_LIMIT = 256
"""Najwięcej wpisów w każdej tablicy pamięci adaptera (referencje, deale, migawki)."""
LOOKBACK_CACHE_LIMIT = 64
"""Najwięcej zapamiętanych okien wyszukiwania wstecz (deal, okno w całości przed `since`); najstarsze wypadają."""
LOOKBACK_CACHE_MIN_AGE = timedelta(hours=23)
"""Okno trafia do pamięci tylko, gdy skończyło się co najmniej tyle przed teraz (opóźnienie historii to sekundy, C7)."""
NOT_FOUND_DEAL = "error.not-found.dealId"
NOT_FOUND_REFERENCE = "error.not-found.dealReference"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
_MAX_REASON_LEN = 200
_MAX_WAIT_FOR_WINDOW_END_S = 1.0  # koniec ostatniego okna historii to „teraz” w górę do pełnej sekundy


class MarketClosedRejection(BrokerRejected, MarketClosed):
    """dealStatus REJECTED z powodem nazywającym zamknięty rynek (C14b): dla menedżera zleceń to nadal odrzucenie
    (`BrokerRejected`: wejście kończy się REJECTED, nie UNKNOWN), dla wykonawcy zamknięty rynek (`MarketClosed`:
    stop zostaje, ponowienie po otwarciu)."""


class _Budget:
    """Wspólny limit zapytań o historię w jednym wywołaniu (okna, podziały i wyszukiwanie wstecz)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.left = limit


def _remember(table: OrderedDict, key: Any, value: Any, limit: int | None = None) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > (RECORD_LIMIT if limit is None else limit):
        table.popitem(last=False)


def _stop_from(level: Decimal, side: Side, distance: Decimal) -> Decimal:
    """Stop pozycji kupna leży poniżej ceny wykonania, sprzedaży powyżej (notatki §2.10, C6)."""
    return level - distance if side is Side.BUY else level + distance


def _target_from(level: Decimal, side: Side, distance: Decimal) -> Decimal:
    """Cel pozycji kupna leży powyżej ceny wykonania, sprzedaży poniżej (po stronie zysku; odwrotnie niż stop).
    NIEPOTWIERDZONE, że `profitDistance` liczy się od ceny wykonania, a nie od notowania (jak C6 dla stopu)."""
    return level + distance if side is Side.BUY else level - distance


def _check_level(method: str, name: str, value: object) -> None:
    """Poziom stopu albo celu z wywołania: dodatni skończony `Decimal` (inaczej TypeError/ValueError przed wysyłką)."""
    if not isinstance(value, Decimal):
        raise TypeError(f"{method}: {name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{method}: {name} must be a positive finite Decimal")


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
    """Część handlowa adaptera. Wymaga od klasy docelowej: `_clock`, `_request`, `_error`, `_scrub`, `_parse`,
    `_session_account_id`."""

    def _init_trading(self) -> None:
        self.unknown_close_sources = 0
        self._sent_stops: OrderedDict[str, tuple[Side, Decimal]] = OrderedDict()  # dealReference → (strona, odległość)
        self._deal_stops: OrderedDict[str, Decimal] = OrderedDict()  # dealId → nasz stop (wyliczony albo z set_stop)
        self._deal_sides: OrderedDict[str, Side] = OrderedDict()  # dealId → strona (potwierdzenie, lista pozycji)
        # dealReference → (strona, odległość celu) wysłane w `POST /positions` (tylko wejścia z celem)
        self._sent_targets: OrderedDict[str, tuple[Side, Decimal]] = OrderedDict()
        self._deal_targets: OrderedDict[str, Decimal] = OrderedDict()  # dealId → nasz cel (wyliczony albo z set_protection)
        self._snapshots: OrderedDict[str, OpenPosition] = OrderedDict()  # dealReference → migawka potwierdzenia
        self._unknown_seen: OrderedDict[tuple, None] = OrderedDict()  # zamknięcia o nieznanym źródle już policzone
        # (rachunek, dealId, początek, koniec) → wiersze okna wyszukiwania wstecz w całości przed `since`
        self._lookback_cache: OrderedDict[tuple, tuple[DealRow, ...]] = OrderedDict()
        # czy jakakolwiek odpowiedź z pozycją pokazała klucze stopu: od tej chwili brak klucza = brak stopu, a zapis
        # adaptera nie może już udawać ochrony (stop_derived=True tylko, dopóki klucze nigdy nie wystąpiły)
        self._stop_keys_seen = False
        # to samo dla kluczy celu (`profitLevel`/`profitDistance`): po pierwszym ich pojawieniu brak klucza = brak celu
        self._target_keys_seen = False

    # --- wejście i potwierdzenie ---------------------------------------------------------------------

    async def open_position(self, req: EntryRequest) -> EntryAck:
        """`POST /positions` ze stopem atomowo; zwraca wyłącznie referencję (`o_...`), wynik zna dopiero `confirm`.

        Zwykły `EntryRequest` (DE40 v1): ciało bez zmian co do bajta. `TargetedEntryRequest` (pasma z celem, R4):
        do ciała dochodzi `profitDistance` (odległość celu w punktach, dokładnie taka, jaką podał wywołujący; kroku
        brokera pilnuje wywołujący: w dół, cel bliżej, nigdy dalej), po `stopDistance`. Wejście z celem NIGDY nie
        idzie bez celu: odległość niedodatnia albo bez wartości to błąd PRZED wysyłką, a odmowa celu przez brokera
        (`error.invalid.takeprofit.*`) kończy się `BrokerRejected` bez ponowienia bez celu. NIEPOTWIERDZONE: nazwa
        pola `profitDistance` w `POST` (notatki §2.10) i punkt odniesienia odległości (wypełnienie czy notowanie).
        """
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
        target_distance = None
        if isinstance(req, TargetedEntryRequest):
            target_distance = req.target_distance
            if not isinstance(target_distance, Decimal) or not target_distance.is_finite() or target_distance <= 0:
                raise ValueError("open_position: target_distance must be a positive finite Decimal for a targeted entry")
            body["profitDistance"] = target_distance
        payload = await self._request("POST", "/positions", body=body, order=True)
        reference = self._parse("POST /positions", parse_reference, payload)
        _remember(self._sent_stops, reference, (req.side, req.stop_distance))
        if target_distance is not None:
            _remember(self._sent_targets, reference, (req.side, target_distance))
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
        target_reported, target = False, None
        if reply is not None:
            row = self._parse(f"GET {path}", parse_position, reply)
            checks = (
                ("dealId", row.deal_id == opening.deal_id),
                ("epic", row.epic == opening.epic),
                ("direction", row.side is opening.side),
                # większa pozycja: zlecenie dołączyło do innej (C15); obie wielkości są w komunikacie, bo surowa
                # odpowiedź pozycji nie trafia do dziennika (to liczby, nigdy sekrety)
                (f"size: confirm {opening.size}, position {row.size}", row.size == opening.size),
                # pozycja.workingOrderId == potwierdzenie.dealId (notatki §2.15, C2), gdy oba są podane
                ("workingOrderId", None in (row.working_order_id, opening.order_id)
                 or row.working_order_id == opening.order_id),
            )  # fmt: skip
            mismatch = [name for name, ok in checks if not ok]
            if mismatch:
                raise self._error(
                    BrokerError, f"GET {path}: the position does not match the confirmation ({'; '.join(mismatch)})"
                )
            self._note_stop_keys([row])
            self._note_target_keys([row])
            opened_at, stop_reported, stop = row.opened_at, row.stop_reported, row.stop_level
            target_reported, target = row.target_reported, row.target_level
        else:  # pozycja już zamknięta (stop, cel, ręcznie): czas otwarcia z wiersza otwarcia w historii
            opened_at = await self._opened_at_from_history(opening, where)
        stop, derived = self._snapshot_stop(reference, opening, where, reply is not None, stop_reported, stop)
        target, target_derived = self._snapshot_target(
            reference, opening, where, reply is not None, target_reported, target
        )
        if target is None:  # bez celu zawsze zwykły `OpenPosition` (DE40 v1: bez zmian)
            snapshot = self._parse(
                where, build, OpenPosition, "confirm", opening.deal_id, opening.epic, opening.side, opening.size,
                opening.level, stop, opened_at, derived,
            )  # fmt: skip
        else:
            snapshot = self._parse(
                where, build, TargetedOpenPosition, "confirm", opening.deal_id, opening.epic, opening.side,
                opening.size, opening.level, stop, opened_at, derived, target, target_derived,
            )  # fmt: skip
        _remember(self._deal_sides, opening.deal_id, opening.side)
        if not stop_reported and stop is not None:
            _remember(self._deal_stops, opening.deal_id, stop)
        if not target_reported and target is not None:
            _remember(self._deal_targets, opening.deal_id, target)
        return snapshot

    def _snapshot_stop(
        self,
        reference: str,
        opening: Opening,
        where: str,
        position_read: bool,
        position_reported: bool,
        position_stop: Decimal | None,
    ) -> tuple[Decimal | None, bool]:
        """(stop migawki, stop_derived). Kolejność: stop pokazany przez brokera w odczycie pozycji; odczyt pozycji BEZ
        klucza, choć broker już kiedyś klucze pokazywał → brak stopu (także gdy potwierdzenie ma `stopLevel`: liczy się
        stan pozycji, wykonawca ją zabezpieczy); inaczej nasz zapis (wykonanie ∓ wysłana odległość, `stop_derived`),
        sprawdzony ze `stopLevel` potwierdzenia, gdy je ma (różnica → `BrokerError`, C6); bez zapisu `stopLevel`
        potwierdzenia albo None. Pozycji już nie ma (zamknięta przed odczytem): reguła kluczy nie ma zastosowania,
        bo nie ma czego chronić, więc migawka niesie wyliczony stop (oznaczony)."""
        if position_reported:
            return position_stop, False
        candidate = None
        sent = self._sent_stops.get(reference)
        if sent is not None and sent[0] is opening.side:
            candidate = self._parse(where, _stop_from, opening.level, opening.side, sent[1])
        if opening.stop_reported and candidate is not None and opening.stop_level != candidate:
            raise self._error(
                BrokerError,
                f"{where}: the confirmation's stopLevel {opening.stop_level} differs from the stop {candidate} derived "
                "from our request (fill level ∓ stopDistance, C6)",
            )
        if position_read and self._stop_keys_seen:
            return None, False
        if candidate is not None:
            return candidate, True
        return (opening.stop_level if opening.stop_reported else None), False

    def _snapshot_target(
        self,
        reference: str,
        opening: Opening,
        where: str,
        position_read: bool,
        position_reported: bool,
        position_target: Decimal | None,
    ) -> tuple[Decimal | None, bool]:
        """(cel migawki, target_derived); ta sama kolejność co `_snapshot_stop`: cel pokazany przez brokera w odczycie
        pozycji (jawny `null` → brak celu); odczyt pozycji BEZ kluczy celu, choć broker je już kiedyś pokazywał → brak
        celu (nie udajemy go zapisem adaptera); inaczej nasz zapis (wykonanie ± wysłana odległość, `target_derived`),
        sprawdzony z `profitLevel` potwierdzenia, gdy je ma (różnica albo `null` → `BrokerError`, jak C6 dla stopu);
        bez zapisu `profitLevel` potwierdzenia albo None. Wejście bez celu nie ma zapisu, więc wynik to None, chyba że
        broker sam pokazał cel (np. ręczny cel właściciela, projekt §3.5). Pozycji już nie ma (zamknięta celem przed
        odczytem): migawka niesie wyliczony cel (oznaczony). Wejście z celem, po którym broker pokazuje pozycję bez
        celu, daje uczciwą migawkę bez celu (silnik widzi brak i go uzupełnia), nigdy wyliczony cel dla pozycji, której
        broker go nie ma. NIEPOTWIERDZONE: nazwy kluczy i ich obecność w żywych odpowiedziach (C5)."""
        if position_reported:
            return position_target, False
        candidate = None
        sent = self._sent_targets.get(reference)
        if sent is not None and sent[0] is opening.side:
            candidate = self._parse(where, _target_from, opening.level, opening.side, sent[1])
        if opening.target_reported and candidate is not None and opening.target_level != candidate:
            raise self._error(
                BrokerError,
                f"{where}: the confirmation's profitLevel {opening.target_level} differs from the target {candidate} "
                "derived from our request (fill level ± profitDistance)",
            )
        if position_read and self._target_keys_seen:
            return None, False
        if candidate is not None:
            return candidate, True
        return (opening.target_level if opening.target_reported else None), False

    async def _opened_at_from_history(self, opening: Opening, where: str) -> datetime:
        """`dateUTC` jedynego wiersza otwarcia (kierunek pozycji) tego dealId w ostatniej dobie; inaczej wyjątek."""
        end = history.ceil_second(self._clock.now_utc())
        budget = _Budget(history.MAX_HISTORY_REQUESTS)
        rows = await self._activity_rows([(end - history.WINDOW_MAX, end)], budget, deal_id=opening.deal_id)
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
        self._note_stop_keys(rows)
        self._note_target_keys(rows)
        result: list[OpenPosition] = []
        for row in rows:
            stop, derived = row.stop_level, False
            if not row.stop_reported and not self._stop_keys_seen:
                stop = self._deal_stops.get(row.deal_id)
                derived = stop is not None
            target, target_derived = row.target_level, False
            if not row.target_reported and not self._target_keys_seen:
                target = self._deal_targets.get(row.deal_id)
                target_derived = target is not None
            if target is None:
                position = self._parse(
                    where, build, OpenPosition, "positions", row.deal_id, row.epic, row.side, row.size, row.level,
                    stop, row.opened_at, derived,
                )  # fmt: skip
            else:
                position = self._parse(
                    where, build, TargetedOpenPosition, "positions", row.deal_id, row.epic, row.side, row.size,
                    row.level, stop, row.opened_at, derived, target, target_derived,
                )  # fmt: skip
            result.append(position)
        for row in rows:
            _remember(self._deal_sides, row.deal_id, row.side)
        return result

    def _note_stop_keys(self, rows: list) -> None:
        if any(row.stop_reported for row in rows):
            self._stop_keys_seen = True

    def _note_target_keys(self, rows: list) -> None:
        if any(row.target_reported for row in rows):
            self._target_keys_seen = True

    async def set_protection(self, deal_id: str, stop_level: Decimal, target_level: Decimal | None) -> None:
        """`PUT /positions/{dealId}` ze `stopLevel` I `profitLevel` w JEDNYM żądaniu, potwierdzenie i odczyt kontrolny.

        Pasma z celem (R4) używają tej metody zamiast `set_stop`: NIEPOTWIERDZONE (C5d), czy `PUT` bez `profitLevel`
        kasuje istniejący cel, więc oba poziomy idą zawsze razem, a broker sprawdza je atomowo (odmowa jednego,
        np. cel po stronie straty albo zbyt bliski stop, nie zmienia żadnego; to założenie o brokerze jest
        NIEPOTWIERDZONE, a odczyt kontrolny wychwyciłby częściową zmianę). `target_level=None`: pozycja bez celu, `PUT`
        niesie wtedy tylko `stopLevel` (adapter nie umie skasować istniejącego celu, więc pozycja, która nadal go
        ma, kończy się `BrokerError`, nie udawanym sukcesem).

        Sukces wymaga (jak w `set_stop`, C3) potwierdzenia ACCEPTED z naszym dealId jako `AMENDED` (inaczej wynik
        nieznany: `BrokerTimeout`), a potem OBU poziomów zgodnych z żądanymi tam, gdzie broker je podaje: `stopLevel` i
        `profitLevel` potwierdzenia oraz `stopLevel` i `profitLevel` pozycji (różnica albo `null` → `BrokerError`; sam
        `stopDistance`/`profitDistance` nie jest porównywany, bo jego punkt odniesienia jest niepotwierdzony, C6, ale
        `null` znaczy brak). Gdy broker pokazywał już klucze stopu (celu), a pozycja po `PUT` nie ma żadnego:
        wynik nieznany (`BrokerTimeout`), nic nie jest zapisywane. Pozycja zniknęła → `BrokerRejected("POSITION_NOT_FOUND")`.
        Nazwy kluczy celu w potwierdzeniu i w pozycji są NIEPOTWIERDZONE (C5); do sprawdzenia skryptem
        research/diag_fx.py na rachunku.
        """
        deal_id = check_ident("deal_id", deal_id)
        _check_level("set_protection", "stop_level", stop_level)
        if target_level is not None:
            _check_level("set_protection", "target_level", target_level)
        path = f"/positions/{deal_id}"
        where = f"PUT {path}"
        body = {"stopLevel": stop_level}
        if target_level is not None:
            body["profitLevel"] = target_level
        payload = await self._request("PUT", path, body=body, not_found=NOT_FOUND_DEAL)
        if payload is None:
            raise self._error(BrokerRejected, POSITION_NOT_FOUND)
        confirmation, problem = await self._outcome(payload)
        stop_shown = target_shown = None
        if problem is None:
            stop_shown, problem = _attempt(check_amendment, confirmation, deal_id)
        if problem is None:
            target_shown, problem = _attempt(amended_target, confirmation)
        if problem is None:
            self._check_amendment_levels(where, stop_level, target_level, stop_shown, target_shown)
            reply = None
            try:
                reply = await self._request("GET", path, not_found=NOT_FOUND_DEAL)
            except BrokerError as exc:  # odczyt kontrolny nieudany: wynik nieznany (ponowienie tego samego jest bezpieczne)
                problem = f"position check failed ({type(exc).__name__}: {exc})"
            if problem is None:
                if reply is None:
                    raise self._error(BrokerRejected, POSITION_NOT_FOUND)
                row = self._parse(f"GET {path}", parse_position, reply)
                self._note_stop_keys([row])
                self._note_target_keys([row])
                problem = self._check_position_levels(path, deal_id, stop_level, target_level, row)
        if problem is not None:
            raise self._error(BrokerTimeout, f"{where}: protection outcome unknown ({problem})")
        _remember(self._deal_stops, deal_id, stop_level)
        if target_level is None:
            self._deal_targets.pop(deal_id, None)
        else:
            _remember(self._deal_targets, deal_id, target_level)

    def _check_amendment_levels(
        self,
        where: str,
        stop_level: Decimal,
        target_level: Decimal | None,
        stop_shown: tuple[bool, Decimal | None],
        target_shown: tuple[bool, Decimal | None],
    ) -> None:
        """Poziomy z potwierdzenia `PUT` (gdy broker je podaje) muszą równać się żądanym; różnica → `BrokerError`."""
        present, level = stop_shown
        if present and level != stop_level:
            raise self._error(
                BrokerError, f"{where}: the confirmation's stopLevel {level} differs from the requested {stop_level}"
            )
        present, level = target_shown
        if target_level is None:
            if present and level is not None:
                raise self._error(
                    BrokerError, f"{where}: the confirmation's profitLevel {level} shows a target, none was requested"
                )
        elif present and level != target_level:
            raise self._error(
                BrokerError, f"{where}: the confirmation's profitLevel {level} differs from the requested {target_level}"
            )

    def _check_position_levels(
        self, path: str, deal_id: str, stop_level: Decimal, target_level: Decimal | None, row: Any
    ) -> str | None:
        """Odczyt kontrolny po `PUT`: poziomy pozycji równe żądanym (różnica albo `null` → `BrokerError`); zwraca opis
        problemu dla wyniku nieznanego (broker pokazuje klucze, a ta pozycja ich nie ma) albo None."""
        where = f"GET {path}"
        no_stop = row.stop_reported and row.stop_level is None
        if row.deal_id != deal_id or no_stop or (row.stop_level_reported and row.stop_level != stop_level):
            raise self._error(
                BrokerError, f"{where}: the position's stop {row.stop_level} differs from the requested {stop_level}"
            )
        if target_level is None:
            if row.target_reported and row.target_level is not None:
                raise self._error(
                    BrokerError, f"{where}: the position still has a target {row.target_level}, none was requested"
                )
        else:
            no_target = row.target_reported and row.target_level is None
            if no_target or (row.target_level_reported and row.target_level != target_level):
                raise self._error(
                    BrokerError,
                    f"{where}: the position's target {row.target_level} differs from the requested {target_level}",
                )
        if self._stop_keys_seen and not row.stop_reported:  # broker pokazuje klucze, a tu ich nie ma: brak stopu
            return f"{where}: the broker shows stop keys, but this position has none after the PUT"
        if target_level is not None and self._target_keys_seen and not row.target_reported:
            return f"{where}: the broker shows target keys, but this position has none after the PUT"
        return None

    async def set_stop(self, deal_id: str, stop_level: Decimal) -> None:
        """`PUT /positions/{dealId}` ze `stopLevel`, potwierdzenie i odczyt kontrolny pozycji; zapis stopu po sukcesie.

        Sukces wymaga (C3): potwierdzenia ACCEPTED z naszym dealId jako `AMENDED` (inaczej wynik nieznany:
        `BrokerTimeout`), równego `stopLevel` w potwierdzeniu, jeśli je ma, oraz równego `stopLevel` pozycji, jeśli
        broker go podaje (różnica albo `null` → `BrokerError`, nie sukces; sam `stopDistance` nie jest porównywany,
        bo jego punkt odniesienia jest niepotwierdzony, C6). Gdy broker pokazywał już klucze stopu, a pozycja po `PUT`
        nie ma żadnego: wynik nieznany (`BrokerTimeout`), nic nie jest zapisywane. Pozycja zniknęła →
        `BrokerRejected("POSITION_NOT_FOUND")`.
        """
        deal_id = check_ident("deal_id", deal_id)
        if not isinstance(stop_level, Decimal):
            raise TypeError(f"set_stop: stop_level must be a Decimal, got {type(stop_level).__name__}")
        if not stop_level.is_finite() or stop_level <= 0:
            raise ValueError("set_stop: stop_level must be a positive finite Decimal")
        path = f"/positions/{deal_id}"
        where = f"PUT {path}"
        payload = await self._request("PUT", path, body={"stopLevel": stop_level}, not_found=NOT_FOUND_DEAL)
        if payload is None:
            raise self._error(BrokerRejected, POSITION_NOT_FOUND)
        confirmation, problem = await self._outcome(payload)
        reported = None
        if problem is None:
            reported, problem = _attempt(check_amendment, confirmation, deal_id)
        if problem is None:
            present, level = reported
            if present and level != stop_level:
                raise self._error(
                    BrokerError, f"{where}: the confirmation's stopLevel {level} differs from the requested {stop_level}"
                )
            reply = None
            try:
                reply = await self._request("GET", path, not_found=NOT_FOUND_DEAL)
            except BrokerError as exc:  # odczyt kontrolny nieudany: wynik nieznany (ponowienie tego samego jest bezpieczne)
                problem = f"position check failed ({type(exc).__name__}: {exc})"
            if problem is None:
                if reply is None:
                    raise self._error(BrokerRejected, POSITION_NOT_FOUND)
                row = self._parse(f"GET {path}", parse_position, reply)
                self._note_stop_keys([row])
                # porównujemy tylko `stopLevel` podany wprost; sam `stopDistance` nie mówi, od czego liczono (C6),
                # ale `null` znaczy brak stopu
                no_stop = row.stop_reported and row.stop_level is None
                if row.deal_id != deal_id or no_stop or (row.stop_level_reported and row.stop_level != stop_level):
                    raise self._error(
                        BrokerError,
                        f"GET {path}: the position's stop {row.stop_level} differs from the requested {stop_level}",
                    )
                if self._stop_keys_seen and not row.stop_reported:  # broker pokazuje klucze, a tu ich nie ma: brak stopu
                    problem = f"GET {path}: the broker shows stop keys, but this position has none after the PUT"
        if problem is not None:
            raise self._error(BrokerTimeout, f"{where}: stop outcome unknown ({problem})")
        _remember(self._deal_stops, deal_id, stop_level)

    async def close_position(self, deal_id: str) -> None:
        """Zamknięcie w całości: wyłącznie `DELETE /positions/{dealId}`, potem `GET /confirms` i `GET /positions/{id}`.

        Sukces wymaga potwierdzenia ACCEPTED, które przypisuje zamknięcie NASZEMU dealId (C3: `PUT` i `DELETE` dzielą
        referencję `p_<dealId>`, więc samo ACCEPTED może być nieaktualnym potwierdzeniem `PUT`), i 404 pozycji po nim.
        Cokolwiek innego → `BrokerTimeout("close outcome unknown")`; REJECTED → `BrokerRejected(reason)`.
        """
        deal_id = check_ident("deal_id", deal_id)
        path = f"/positions/{deal_id}"
        payload = await self._request("DELETE", path, not_found=NOT_FOUND_DEAL)
        if payload is None:  # deal już nie istnieje: czyste 404 bez skutku (do potwierdzenia na próbie smoke, C29)
            raise self._error(BrokerRejected, POSITION_NOT_FOUND)
        confirmation, problem = await self._outcome(payload)
        if problem is None:
            _, problem = _attempt(check_closure, confirmation, deal_id)
        if problem is None:
            try:
                still = await self._request("GET", path, not_found=NOT_FOUND_DEAL)
            except BrokerError as exc:
                problem = f"position check failed ({type(exc).__name__}: {exc})"
            else:
                if still is not None:
                    problem = f"GET {path}: the position is still open after an accepted close"
        if problem is not None:
            raise self._error(BrokerTimeout, f"DELETE {path}: close outcome unknown ({problem})")
        self._deal_stops.pop(deal_id, None)
        self._deal_targets.pop(deal_id, None)

    async def _outcome(self, payload: Any) -> tuple[Any, str | None]:
        """Potwierdzenie `PUT`/`DELETE` po referencji z odpowiedzi: (treść, None) przy ACCEPTED, (None, problem) gdy
        wynik nieznany (404, błąd, timeout, wygasła sesja, inny status, brak referencji); REJECTED → wyjątek.

        Wyjątki są tworzone poza blokami `except`, więc nie mają łańcucha (`__context__`) z obcymi obiektami.
        """
        reference, problem = _attempt(parse_reference, payload)
        if problem is not None:
            return None, f"response without a usable dealReference ({problem})"
        try:
            reply = await self._request("GET", f"/confirms/{reference}", not_found=NOT_FOUND_REFERENCE)
        except BrokerError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if reply is None:
            return None, f"GET /confirms/{reference}: unknown reference (HTTP 404)"
        parsed, problem = _attempt(parse_deal_status, reply, reference)
        if problem is not None:
            return None, problem
        status, reason = parsed
        if status == "REJECTED":
            raise self._rejection(reason)
        if status != "ACCEPTED":
            return None, f"unexpected dealStatus {shown(status)}"
        return reply, None

    def _rejection(self, reason: str | None) -> BrokerRejected:
        """`BrokerRejected` z powodem brokera: najpierw bez sekretów, potem przycięty (nigdy pół sekretu); powód
        nazywający zamknięty rynek → `MarketClosedRejection` (C14b)."""
        cls = MarketClosedRejection if reason and is_market_closed_code(reason) else BrokerRejected
        return self._error(cls, self._scrub(reason or "REJECTED")[:_MAX_REASON_LEN])

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
        budget = _Budget(history.MAX_HISTORY_REQUESTS)
        rows = await self._activity_rows(history.history_windows(since, now), budget)
        found = history.classify(rows, self._deal_sides)
        if found.undecided and not found.conflicting:
            await self.list_positions()  # strona otwartych deali z listy brokera (zapisywana w pamięci)
            found = history.classify(rows, self._deal_sides)
        if found.undecided and not found.conflicting:  # np. obca pozycja sprzed `since`, zamknięta w zakresie
            rows = await self._look_back(rows, found.undecided, since, now, budget)
            found = history.classify(rows, self._deal_sides)
        if found.conflicting:
            raise self._error(BrokerError, self._history_problem("HISTORY_INCONSISTENT", found.conflicting,
                                                                 "rows contradict each other or the known side"))  # fmt: skip
        if found.undecided:
            raise self._error(BrokerError, self._history_problem("HISTORY_AMBIGUOUS", found.undecided,
                                                                 "cannot tell the opening from a closing"))  # fmt: skip
        closed: list[ClosedDeal] = []
        unknown: list[tuple] = []
        for row in found.closures:
            if row.ts < since:
                continue
            reason, known = history.reason_for(row.source)
            if not known and row.key not in self._unknown_seen:
                unknown.append(row.key)
            where = "GET /history/activity"
            closed.append(self._parse(where, build, ClosedDeal, "activity", row.deal_id, row.epic, row.level, reason,
                                      row.ts))  # fmt: skip
        for key in unknown:  # każde zamknięcie liczone raz, nie przy każdym odczycie
            _remember(self._unknown_seen, key, None)
        self.unknown_close_sources += len(unknown)
        return closed

    async def _look_back(
        self, rows: list[DealRow], undecided: set[str], since: datetime, now: datetime, budget: _Budget
    ) -> list[DealRow]:
        """Dla każdego nierozstrzygniętego deala: historia z filtrem `dealId` WSTECZ od `since`, okna ≤ 1 doba, do
        granicy 14 dni od teraz, aż jego wiersze dadzą się rozstrzygnąć (zwykle: wcześniejsze otwarcie o kierunku
        przeciwnym). Limit zapytań wspólny z oknami głównymi; jego wyczerpanie → `HISTORY_TRUNCATED`. Okna z pamięci
        (`_lookback_rows`) nie zużywają limitu."""
        merged = {row.key: row for row in rows}
        windows = history.lookback_windows(since, now - history.HISTORY_MAX_RANGE)
        for deal_id in sorted(undecided):
            for window in windows:
                for row in await self._lookback_rows(deal_id, window, since, now, budget):
                    merged.setdefault(row.key, row)
                deal_rows = [r for r in merged.values() if r.deal_id == deal_id]
                verdict = history.classify(deal_rows, self._deal_sides)
                if deal_id not in verdict.undecided:
                    break
        return list(merged.values())

    async def _lookback_rows(
        self, deal_id: str, window: tuple[datetime, datetime], since: datetime, now: datetime, budget: _Budget
    ) -> tuple[DealRow, ...]:
        """Wiersze deala w jednym oknie wyszukiwania wstecz (filtr `dealId`), kompletne albo wyjątek.

        Okno, które się już nie zmieni, jest zapamiętywane w ograniczonej pamięci (`LOOKBACK_CACHE_LIMIT`, klucz z
        rachunkiem sesji), więc kolejne odczyty z tym samym `since` nie powtarzają zapytań, a trafienie nie zużywa
        limitu zapytań. Warunek zapisu jest tutaj, nie w budowie okien: okno kończy się przed `since` ORAZ co najmniej
        `LOOKBACK_CACHE_MIN_AGE` przed teraz (opóźniony wiersz już nie dojdzie); okno sięgające `since` jest zawsze
        czytane od nowa. Błąd odczytu nic nie zapisuje (wyjątek idzie przed zapisem, nigdy pusty wynik). Rachunek sesji
        zmieniony w trakcie żądania (ponowne logowanie) oznacza, że nie wiadomo, czyje to wiersze: bez zapisu.
        """
        account = self._session_account_id
        key = (account, deal_id, *window)
        cached = self._lookback_cache.get(key)
        if cached is not None:
            return cached
        rows = tuple(await self._activity_rows([window], budget, deal_id=deal_id))
        settled = window[1] < since and window[1] <= now - LOOKBACK_CACHE_MIN_AGE
        if settled and account is not None and self._session_account_id == account:
            _remember(self._lookback_cache, key, rows, LOOKBACK_CACHE_LIMIT)
        return rows

    @staticmethod
    def _history_problem(code: str, deals: set[str], what: str) -> str:
        shown_ids = ", ".join(sorted(deals)[:3]) + (", ..." if len(deals) > 3 else "")
        return f"{code}: {what} for {len(deals)} deal(s) in the history ({shown_ids}); nothing returned"

    async def _activity_rows(
        self, windows: list[tuple[datetime, datetime]], budget: _Budget, *, deal_id: str | None = None
    ) -> list[DealRow]:
        """Wiersze `POSITION`/`ACCEPTED` ze wszystkich okien (bez duplikatów); obcięte okno dzielone, błąd → wyjątek."""
        where = "GET /history/activity"
        queue = deque(windows)
        found: dict[tuple, DealRow] = {}
        while queue:
            start, end = queue.popleft()
            if budget.left <= 0:
                raise self._error(
                    BrokerError,
                    f"HISTORY_TRUNCATED: more than {budget.limit} history requests would be needed; nothing returned",
                )
            wait = (end - self._clock.now_utc()).total_seconds()
            if wait > 0:  # `to` nigdy w przyszłości (wariant ostrożny): koniec okna to teraz w górę do sekundy
                await self._clock.sleep(min(wait, _MAX_WAIT_FOR_WINDOW_END_S))
            params = {"from": history.api_time(start), "to": history.api_time(end), "detailed": "true"}
            if deal_id is not None:
                params["dealId"] = deal_id
            budget.left -= 1
            payload = await self._request("GET", "/history/activity", params=params)
            count, rows = self._parse(where, history.parse_activity, payload, start, end, deal_id)
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
