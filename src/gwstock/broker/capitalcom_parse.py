"""Czyste parsowanie odpowiedzi REST Capital.com (bez sieci i bez adresów hostów).

Zasady:
- liczby JSON wyłącznie jako `Decimal` (`loads`: także liczby całkowite; NaN/Infinity i powtórzone klucze to błąd),
  więc `float` nigdy nie powstaje; napis z liczbą NIE jest liczbą (pole musi być liczbą JSON);
- znaczniki czasu wyłącznie z pól UTC (`snapshotTimeUTC`); pola lokalne (`snapshotTime`, `updateTime`) nigdy nie są
  parsowane do logiki;
- brak pola, zły typ albo nieoczekiwana jednostka to `ParseError` z nazwą pola, nigdy z treścią odpowiedzi
  (poza krótkimi kodami wyliczeniowymi jak jednostka `PERCENTAGE`); adapter zamienia go na `BrokerError`;
- błędy konstruktorów typów domenowych (`ValueError`/`TypeError` z `gwstock.types`, także `ArithmeticError`) też
  stają się `ParseError` z nazwą pola; arytmetyka parserów poza zakresem (`decimal.Overflow`) zamienia adapter.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from gwstock.types import AccountInfo, InstrumentRules, Quote, Side, WorkingOrder

_ENUM = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
_UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?")  # bez strefy (notatki §1.3)
_FIELD = re.compile(r"[A-Za-z]+\.[A-Za-z_]+")
# identyfikator brokera, który trafia do ścieżki URL (dealId, dealReference): bez `/`, `?`, `#`, spacji, `.`/`..`
_IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_MAX_RAW_REASON_LEN = 4096
_MAX_EXPONENT = 30
"""Liczba z odpowiedzi brokera (cena, wielkość, saldo) ma rząd wielkości co najwyżej 10^30 w obie strony (zero zawsze
przechodzi): absurdalny wykładnik mieści się jeszcze w `Decimal` (`1e999999999`), ale każde działanie na nim kończy się
`decimal.Overflow`, więc odpowiedź z takim polem odrzucamy od razu, razem z nazwą pola."""


class ParseError(ValueError):
    """Odpowiedź brokera ma nieoczekiwany kształt; komunikat wskazuje pole, nie zawiera treści odpowiedzi."""


class _Duplicate(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite number")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate("duplicate key")
        result[key] = value
    return result


def loads(raw: bytes | str) -> Any:
    """JSON z liczbami jako `Decimal`; NaN, Infinity, powtórzony klucz, zły tekst i liczba z wykładnikiem poza zakresem
    `Decimal` (np. `1e99999999999999999999`) → `ParseError`."""
    problem = "not valid JSON"
    try:
        return json.loads(
            raw,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique,
        )
    except _Duplicate:
        problem = "duplicate key in a JSON object"
    except (ValueError, RecursionError, ArithmeticError):
        pass  # ArithmeticError: wykładnik liczby poza zakresem `Decimal` (`decimal.InvalidOperation` przy parsowaniu)
    raise ParseError(problem)


def dumps(value: Any) -> str:
    """JSON ciała żądania: `Decimal` zapisany dokładnie i bez notacji naukowej; `float` jest odrzucany."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("request body: non-finite Decimal")
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("request body: object keys must be strings")
        return "{" + ",".join(f"{json.dumps(k)}:{dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in value) + "]"
    raise TypeError(f"request body: cannot encode {type(value).__name__} (floats are never sent)")


# --- pola --------------------------------------------------------------------------------------


def path(where: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{where}[{key}]"
    return f"{where}.{key}" if where else key


def shown(value: Any) -> str:
    """Krótki kod wyliczeniowy do komunikatu (np. jednostka); wszystko inne jako znacznik bez treści."""
    if isinstance(value, str) and _ENUM.fullmatch(value):
        return value
    return "<unexpected value>" if isinstance(value, str) else f"<{type(value).__name__}>"


def obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParseError(f"{where or 'response'}: expected a JSON object")
    return value


def items(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ParseError(f"{where}: expected a JSON list")
    return value


def get(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ParseError(f"{path(where, key)}: missing")
    return d[key]


def dec(d: dict[str, Any], key: str, where: str) -> Decimal:
    value = get(d, key, where)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ParseError(f"{path(where, key)}: expected a JSON number, got {type(value).__name__}")
    if value and abs(value.adjusted()) > _MAX_EXPONENT:  # np. 1e999999999: mieści się w Decimal, ale nie w niczym innym
        raise ParseError(f"{path(where, key)}: number out of range")
    return value


def text(d: dict[str, Any], key: str, where: str) -> str:
    value = get(d, key, where)
    if not isinstance(value, str) or not value:
        raise ParseError(f"{path(where, key)}: expected a non-empty string, got {type(value).__name__}")
    return value


def optional_text(d: dict[str, Any], key: str, where: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParseError(f"{path(where, key)}: expected a string, got {type(value).__name__}")
    return value


def positive(d: dict[str, Any], key: str, where: str) -> Decimal:
    value = dec(d, key, where)
    if value <= 0:
        raise ParseError(f"{path(where, key)}: expected a positive number")
    return value


def ident(d: dict[str, Any], key: str, where: str) -> str:
    """Identyfikator brokera (dealId, dealReference) bezpieczny jako segment ścieżki URL."""
    value = text(d, key, where)
    if not _IDENT.fullmatch(value):
        raise ParseError(f"{path(where, key)}: expected an identifier of letters, digits, '.', '_' or '-'")
    return value


def side_of(d: dict[str, Any], key: str, where: str) -> Side:
    value = get(d, key, where)
    if value == "BUY":
        return Side.BUY
    if value == "SELL":
        return Side.SELL
    raise ParseError(f"{path(where, key)}: expected BUY or SELL, got {shown(value)}")


def check_ident(name: str, value: object) -> str:
    """Argument wywołania (deal_id, deal_reference) przed wstawieniem do ścieżki URL; zły → TypeError/ValueError."""
    if not isinstance(value, str):
        raise TypeError(f"{name}: expected a string, got {type(value).__name__}")
    if not _IDENT.fullmatch(value):
        raise ValueError(f"{name}: expected letters, digits, '.', '_' or '-' (no '/', '?', '#', spaces), got {value!r:.40}")
    return value


def clean_reason(value: Any) -> str | None:
    """Powód brokera do komunikatu: tylko znaki drukowalne; nie-napis albo pusty → None.

    Bez przycinania do długości komunikatu: adapter najpierw usuwa sekrety, dopiero potem przycina (inaczej cięcie
    w połowie sekretu zostawiłoby jego początek). Górna granica `_MAX_RAW_REASON_LEN` tylko ogranicza pracę.
    """
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value[:_MAX_RAW_REASON_LEN] if ch.isprintable()).strip()
    return value or None


def is_market_closed_code(code: str) -> bool:
    """Czy `errorCode` odmowy mówi o zamkniętym rynku. Kod jest NIEUDOKUMENTOWANY (notatki §1.4, C14b): rozpoznajemy
    każdy kod, który zawiera słowa "market" i "closed" (wielkość liter bez znaczenia). Pomyłka tylko zmienia klasę
    odmowy (`MarketClosed` zamiast `BrokerRejected`): w obu przypadkach nic nie zostało wykonane."""
    lowered = code.lower()
    return "market" in lowered and "closed" in lowered


def build(cls: type, where: str, *args: Any) -> Any:
    """Konstruktor typu domenowego; jego `ValueError`/`TypeError` → `ParseError` z nazwą pola (bez wartości)."""
    try:
        return cls(*args)
    except (TypeError, ValueError) as exc:
        head = str(exc).split(":", 1)[0]
        problem = head if _FIELD.fullmatch(head) else type(exc).__name__
    except ArithmeticError as exc:
        problem = f"value (out of range: {type(exc).__name__})"
    raise ParseError(f"{where}: invalid {problem}")


# --- sesja i rachunki -------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginInfo:
    """Z treści `POST /session`: rachunek aktywny sesji i rachunki klienta (id → waluta albo None)."""

    current_account_id: str
    account_currencies: dict[str, str | None]


def parse_login(payload: Any) -> LoginInfo:
    root = obj(payload, "")
    current = text(root, "currentAccountId", "")
    currencies: dict[str, str | None] = {}
    for i, item in enumerate(items(get(root, "accounts", ""), "accounts")):
        where = path("accounts", i)
        account = obj(item, where)
        account_id = text(account, "accountId", where)
        if account_id in currencies:
            raise ParseError(f"{where}.accountId: duplicate account id")
        currency = account.get("currency")
        currencies[account_id] = currency if isinstance(currency, str) and currency else None
    if current not in currencies:
        raise ParseError("currentAccountId: not among accounts")
    return LoginInfo(current, currencies)


def parse_session_account(payload: Any) -> str:
    """`GET /session` → `accountId` aktywnego rachunku (uwaga: inna nazwa niż `currentAccountId`)."""
    return text(obj(payload, ""), "accountId", "")


def parse_accounts(payload: Any) -> list[AccountInfo]:
    """`GET /accounts` → `AccountInfo` (equity = balance.balance, margin_used = balance − available; C17)."""
    result: list[AccountInfo] = []
    seen: set[str] = set()
    for i, item in enumerate(items(get(obj(payload, ""), "accounts", ""), "accounts")):
        where = path("accounts", i)
        account = obj(item, where)
        account_id = text(account, "accountId", where)
        if account_id in seen:
            raise ParseError(f"{where}.accountId: duplicate account id")
        seen.add(account_id)
        balance = obj(get(account, "balance", where), path(where, "balance"))
        equity = dec(balance, "balance", path(where, "balance"))
        available = dec(balance, "available", path(where, "balance"))
        info = build(
            AccountInfo,
            where,
            account_id,
            text(account, "currency", where),
            text(account, "status", where),
            equity,
            available,
            equity - available,
        )
        result.append(info)
    return result


# --- reguły instrumentu i notowanie (`GET /markets/{epic}`) -------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """`snapshot` z `GET /markets/{epic}`. `marker` to obserwowana krotka świeżości (bez parsowania czasu)."""

    bid: Decimal
    offer: Decimal
    marker: tuple[Decimal, Decimal, str | None, str | None]


def parse_snapshot(payload: Any) -> Snapshot:
    """bid/offer (liczby dodatnie) oraz napisy `updateTime` i `updateTimeUTC` (jeśli są) jako znacznik zmian.

    `updateTime` jest czasem lokalnym bez strefy, a `updateTimeUTC` w `GET /markets/DE40` nie ma (odczyt live);
    żaden z nich nie jest parsowany, służą tylko do wykrycia zmiany (rozstrzygnięcie o `Quote.ts`, C10).
    """
    snapshot = obj(get(obj(payload, ""), "snapshot", ""), "snapshot")
    bid = dec(snapshot, "bid", "snapshot")
    offer = dec(snapshot, "offer", "snapshot")
    if bid <= 0 or offer <= 0:
        raise ParseError("snapshot.bid/offer: expected positive prices")
    marker = (
        bid,
        offer,
        optional_text(snapshot, "updateTime", "snapshot"),
        optional_text(snapshot, "updateTimeUTC", "snapshot"),
    )
    return Snapshot(bid, offer, marker)


_EXPECTED_SNAPSHOT_FIELDS = (("delayTime", Decimal(0)), ("scalingFactor", Decimal(1)))
_MAX_SHOWN_NUMBER_LEN = 24


def snapshot_anomalies(payload: Any, scrub: Callable[[str], str] = lambda raw: raw) -> list[str]:
    """Opisy pól `snapshot.delayTime` ≠ 0 i `snapshot.scalingFactor` ≠ 1 z `GET /markets/{epic}` (np.
    `snapshot.delayTime=15`); brak pola albo migawki to nie anomalia, a zły kształt nie jest tu błędem.

    Znaczenie obu pól jest nieopisane (C10), w odczycie live DE40 to 0 i 1. Wynik służy tylko ostrzeżeniu: liczba
    jest pokazana (przycięta), każda inna wartość jako znacznik bez treści (`shown`). `scrub` (adapter podaje
    swoje usuwanie sekretów) działa na PEŁNYM zapisie liczby, dopiero potem następuje przycięcie: przycięta liczba
    zostawiłaby początek sekretu złożonego z cyfr.
    """
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict):
        return []
    found = []
    for key, expected in _EXPECTED_SNAPSHOT_FIELDS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if isinstance(value, Decimal) and value == expected:
            continue
        text_value = scrub(str(value))[:_MAX_SHOWN_NUMBER_LEN] if isinstance(value, Decimal) else shown(value)
        found.append(f"snapshot.{key}={text_value}")
    return found


def rule(dealing: dict[str, Any], name: str) -> tuple[str, Decimal]:
    """Reguła `dealingRules.<name>` jako (jednostka, wartość) z obiektu `{unit, value}`."""
    where = path("dealingRules", name)
    item = obj(get(dealing, name, "dealingRules"), where)
    unit = get(item, "unit", where)
    if not isinstance(unit, str):
        raise ParseError(f"{where}.unit: expected a string, got {type(unit).__name__}")
    return unit, dec(item, "value", where)


def _rule_in(dealing: dict[str, Any], name: str, unit: str) -> Decimal:
    found, value = rule(dealing, name)
    if found != unit:
        raise ParseError(f"dealingRules.{name}.unit: expected {unit}, got {shown(found)}")
    return value


def _require(condition: bool, where: str, expectation: str) -> None:
    if not condition:
        raise ParseError(f"{where}: expected {expectation}")


def parse_instrument_rules(
    epic: str, payload: Any, account_currency: str | None, *, allow_foreign_currency: bool = False
) -> InstrumentRules:
    """`GET /markets/{epic}` → `InstrumentRules` (notatki §2.16.1; jednostki inne niż oczekiwane → `ParseError`).

    - `min_size` ← `minDealSize.value`, `size_step` ← `minSizeIncrement.value` (jednostka `POINTS`, czyli jednostki
      wielkości, notatki §5 pkt 5);
    - `stop_step` ← `minStepDistance.value` w `POINTS` (DE40: 0,1; czy krok dotyczy stopu: C16c);
    - `value_per_point` ← `instrument.lotSize` w walucie instrumentu (C16), więc domyślnie waluta instrumentu musi być
      walutą rachunku sesji (inaczej potrzebny byłby kurs, którego adapter nie zgaduje) i `currency` zostaje None
      (zachowanie DE40 v1 bez zmian);
    - `allow_foreign_currency=True` (tylko słowem kluczowym; pasma w USD, projekt §7, R8): zamiast `ParseError` przy
      walucie instrumentu innej niż rachunku zwraca `InstrumentRules(currency=<waluta instrumentu>)`. Zawsze niesie
      walutę instrumentu (także gdy równa się walucie rachunku): wywołujący sam porównuje ją z walutą rachunku.
      `value_per_point` nadal jest w walucie INSTRUMENTU: adapter niczego nie przelicza i nie zgaduje kursu, a
      rozliczenie wyniku w EUR (C8, kurs i narzut brokera) jest NIEPOTWIERDZONE, do sprawdzenia skryptem
      research/diag_fx.py na rachunku. Kod waluty spoza formatu ISO 4217 (trzy wielkie litery ASCII) → `ParseError`;
    - `margin_rate` ← `marginFactor / 100` przy `marginFactorUnit == "PERCENTAGE"` (C25);
    - `min_stop_distance` w punktach ← `minStopOrProfitDistance`: `PERCENTAGE` → value / 100 × mid migawki
      (DE40: 0,01% ≈ 2,6 pkt), `POINTS` → value; minimum zależy od ceny, więc liczone przy każdym odczycie.
    """
    root = obj(payload, "")
    instrument = obj(get(root, "instrument", ""), "instrument")
    dealing = obj(get(root, "dealingRules", ""), "dealingRules")

    min_size = _rule_in(dealing, "minDealSize", "POINTS")
    _require(min_size > 0, "dealingRules.minDealSize.value", "a positive size")
    size_step = _rule_in(dealing, "minSizeIncrement", "POINTS")
    _require(size_step > 0, "dealingRules.minSizeIncrement.value", "a positive size step")
    stop_step = _rule_in(dealing, "minStepDistance", "POINTS")
    _require(stop_step >= 0, "dealingRules.minStepDistance.value", "a non-negative distance in points")

    lot_size = dec(instrument, "lotSize", "instrument")
    _require(lot_size > 0, "instrument.lotSize", "a positive lot size")
    currency = text(instrument, "currency", "instrument")
    if not allow_foreign_currency and (account_currency is None or currency != account_currency):
        raise ParseError(
            f"instrument.currency: {shown(currency)} differs from the session account currency "
            f"{shown(account_currency)}; value_per_point would need an FX rate"
        )

    factor = dec(instrument, "marginFactor", "instrument")
    unit = text(instrument, "marginFactorUnit", "instrument")
    if unit != "PERCENTAGE":
        raise ParseError(f"instrument.marginFactorUnit: expected PERCENTAGE, got {shown(unit)}")
    _require(0 < factor <= 100, "instrument.marginFactor", "a percentage in (0, 100]")

    stop_unit, stop_value = rule(dealing, "minStopOrProfitDistance")
    _require(stop_value >= 0, "dealingRules.minStopOrProfitDistance.value", "a non-negative distance")
    if stop_unit == "PERCENTAGE":
        snapshot = parse_snapshot(root)
        quote = build(Quote, "snapshot", epic, snapshot.bid, snapshot.offer, datetime(1970, 1, 1, tzinfo=timezone.utc))
        min_stop = stop_value / 100 * quote.mid
    elif stop_unit == "POINTS":
        min_stop = stop_value
    else:
        raise ParseError(f"dealingRules.minStopOrProfitDistance.unit: expected PERCENTAGE or POINTS, got {shown(stop_unit)}")

    return build(
        InstrumentRules, "instrument rules", epic, min_size, size_step, lot_size, factor / 100, min_stop, stop_step,
        currency if allow_foreign_currency else None,
    )  # fmt: skip


# --- świece (`GET /prices/{epic}`) ------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """Świeca z odpowiedzi: znacznik `snapshotTimeUTC` (UTC) i ceny mid (bid + ask) / 2 dla O, H, L, C."""

    stamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def utc_stamp(d: dict[str, Any], key: str, where: str) -> datetime:
    """Napis `YYYY-MM-DDTHH:MM:SS[.ffffff]` bez strefy z pola UTC → datetime z `tzinfo=UTC`."""
    value = get(d, key, where)
    moment = None
    if isinstance(value, str) and _UTC_STAMP.fullmatch(value):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            moment = None
    if moment is None:
        raise ParseError(f"{path(where, key)}: expected YYYY-MM-DDTHH:MM:SS without a zone suffix")
    return moment.replace(tzinfo=timezone.utc)


def _mid(item: dict[str, Any], name: str, where: str) -> Decimal:
    field = path(where, name)
    pair = obj(get(item, name, where), field)
    bid, ask = dec(pair, "bid", field), dec(pair, "ask", field)
    _require(0 < bid <= ask, field, "positive bid <= ask")
    return (bid + ask) / 2


def parse_bars(payload: Any) -> list[Bar]:
    """`prices[]` w kolejności odpowiedzi. Czas WYŁĄCZNIE z `snapshotTimeUTC`; `snapshotTime` (lokalny) pomijany."""
    bars: list[Bar] = []
    for i, item in enumerate(items(get(obj(payload, ""), "prices", ""), "prices")):
        where = path("prices", i)
        bar_item = obj(item, where)
        moment = utc_stamp(bar_item, "snapshotTimeUTC", where)
        o, h, low, c = (_mid(bar_item, name, where) for name in ("openPrice", "highPrice", "lowPrice", "closePrice"))
        _require(low <= h, path(where, "highPrice"), "highPrice >= lowPrice")
        bars.append(Bar(moment, o, h, low, c))
    return bars


# --- część handlowa: referencje, potwierdzenia, pozycje, zlecenia oczekujące -------------------------------


def parse_reference(payload: Any) -> str:
    """`{"dealReference": "..."}` z `POST /positions`, `PUT`/`DELETE /positions/{dealId}` (notatki §2.10, §2.13, §2.14)."""
    return ident(obj(payload, ""), "dealReference", "")


def parse_deal_status(payload: Any, reference: str) -> tuple[str, str | None]:
    """(`dealStatus`, `reason`) z `GET /confirms/{ref}` (§2.15); potwierdzenie innej referencji → `ParseError`.

    Referencja jest sprawdzana PRZED statusem, więc cudze potwierdzenie nigdy nie staje się odrzuceniem naszego.
    `dealStatus` musi być napisem; jego wartość ocenia wywołujący (tylko ACCEPTED i REJECTED mają znaczenie).
    """
    root = obj(payload, "")
    echoed = root.get("dealReference")
    if echoed is not None and echoed != reference:
        raise ParseError("dealReference: the confirmation is for another reference")
    status = text(root, "dealStatus", "")
    return status, clean_reason(root.get("reason"))


@dataclass(frozen=True)
class Opening:
    """Potwierdzenie otwarcia (ACCEPTED): `order_id` = główne `dealId` (zlecenie), `deal_id` = pozycja (C2).

    `stop_reported`: potwierdzenie ma klucz `stopLevel` (pole znane z [S3]); `stop_level` to jego wartość (None dla
    jawnego `null`). `target_reported` / `target_level`: to samo dla klucza `profitLevel` (cel zysku, R4;
    NIEPOTWIERDZONE, że żywe API go zwraca: C5, do sprawdzenia skryptem research/diag_fx.py).
    """

    order_id: str | None
    deal_id: str
    epic: str
    side: Side
    size: Decimal
    level: Decimal
    stop_reported: bool = False
    stop_level: Decimal | None = None
    target_reported: bool = False
    target_level: Decimal | None = None


def _level_key(root: dict[str, Any], key: str, where: str) -> tuple[bool, Decimal | None]:
    """(czy klucz poziomu jest, jego wartość albo None dla `null`); wartość inna niż liczba dodatnia → błąd."""
    if key not in root:
        return False, None
    if root[key] is None:
        return True, None
    return True, positive(root, key, where)


def _stop_key(root: dict[str, Any], where: str) -> tuple[bool, Decimal | None]:
    """Klucz `stopLevel` (patrz `_level_key`)."""
    return _level_key(root, "stopLevel", where)


def _target_key(root: dict[str, Any], where: str) -> tuple[bool, Decimal | None]:
    """Klucz `profitLevel` (patrz `_level_key`)."""
    return _level_key(root, "profitLevel", where)


def parse_opening(payload: Any) -> Opening:
    """Id pozycji = JEDYNY wpis `affectedDeals`, ze statusem `OPENED` (notatki §2.15, C2); inaczej `ParseError`.

    Brak wpisu `OPENED` (np. `AMENDED` przy agregacji z inną pozycją, C15) albo kilka takich wpisów: nie wiadomo,
    którą pozycję utworzyło zlecenie. Jakikolwiek inny wpis obok naszego `OPENED` (np. zlecenie zmniejszyło albo
    zamknęło inną pozycję przy wyłączonym hedgingu, C15): to nie jest czyste wykonanie. W obu przypadkach adapter nie
    zgaduje; wynik zlecenia rozstrzygają menedżer zleceń i uzgadnianie z listy pozycji.
    """
    root = obj(payload, "")
    affected = items(get(root, "affectedDeals", ""), "affectedDeals")
    opened = []
    for i, item in enumerate(affected):
        where = path("affectedDeals", i)
        deal = obj(item, where)
        if deal.get("status") == "OPENED":
            opened.append(ident(deal, "dealId", where))
    if len(opened) != 1:
        raise ParseError(f"affectedDeals: expected exactly one OPENED deal, found {len(opened)}")
    if len(affected) != 1:
        raise ParseError(
            f"affectedDeals: {len(affected) - 1} other affected deal(s) besides the OPENED one (not a clean fill)"
        )
    order_id = root.get("dealId")
    stop_reported, stop_level = _stop_key(root, "")
    target_reported, target_level = _target_key(root, "")
    return Opening(
        order_id=order_id if isinstance(order_id, str) and order_id else None,
        deal_id=opened[0],
        epic=text(root, "epic", ""),
        side=side_of(root, "direction", ""),
        size=positive(root, "size", ""),
        level=positive(root, "level", ""),
        stop_reported=stop_reported,
        stop_level=stop_level,
        target_reported=target_reported,
        target_level=target_level,
    )


def _our_statuses(root: dict[str, Any], deal_id: str) -> list[Any]:
    affected = items(get(root, "affectedDeals", ""), "affectedDeals")
    return [d.get("status") for d in affected if isinstance(d, dict) and d.get("dealId") == deal_id]


def check_closure(payload: Any, deal_id: str) -> None:
    """Potwierdzenie `DELETE` musi przypisać ZAMKNIĘCIE naszemu dealId (C3): `status` OPEN, brak naszego dealId w
    `affectedDeals` albo wyłącznie `AMENDED`/`OPENED` przy nim → `ParseError` (np. nieaktualne potwierdzenie `PUT`
    pod wspólną referencją `p_<dealId>`)."""
    root = obj(payload, "")
    if root.get("status") == "OPEN":
        raise ParseError("status: OPEN, the confirmation does not report a closure")
    statuses = _our_statuses(root, deal_id)
    if not any(isinstance(s, str) and s not in ("AMENDED", "OPENED") for s in statuses):
        raise ParseError("affectedDeals: no closure of this deal in the confirmation")


def check_amendment(payload: Any, deal_id: str) -> tuple[bool, Decimal | None]:
    """Potwierdzenie `PUT` musi mieć nasz dealId jako `AMENDED` w `affectedDeals`; zwraca klucz `stopLevel` (jak
    `Opening`), który wywołujący porównuje z żądanym poziomem."""
    root = obj(payload, "")
    if "AMENDED" not in _our_statuses(root, deal_id):
        raise ParseError("affectedDeals: this deal is not reported as AMENDED")
    return _stop_key(root, "")


def amended_target(payload: Any) -> tuple[bool, Decimal | None]:
    """Klucz `profitLevel` potwierdzenia `PUT` (jak w `Opening`): (czy jest, wartość albo None dla `null`); wywołujący
    porównuje go z żądanym celem (`set_protection`). Że potwierdzenie `PUT` w ogóle niesie `profitLevel`, jest
    NIEPOTWIERDZONE (pole znane z [S3]); jego brak nie jest błędem."""
    return _target_key(obj(payload, ""), "")


@dataclass(frozen=True)
class PositionRow:
    """Element `GET /positions` / `GET /positions/{dealId}` (§2.11). `stop_reported`: jakikolwiek klucz stopu był w
    odpowiedzi; `stop_level_reported`: był klucz `stopLevel` (poziom podany wprost, nie wyliczony z `stopDistance`,
    którego punkt odniesienia, wykonanie czy notowanie, jest niepotwierdzony: C6). Cel zysku (R4): `target_reported`:
    jakikolwiek klucz celu (`profitLevel`, `profitDistance`) był w odpowiedzi; `target_level_reported`: był klucz
    `profitLevel` (poziom wprost). Pola celu są NIEPOTWIERDZONE na rachunku (C5; research/diag_fx.py). Wszystkie pola
    są wymagane: wartość domyślna flagi mogłaby po cichu wyłączyć porównanie poziomu w `set_stop`/`set_protection`."""

    deal_id: str
    working_order_id: str | None
    epic: str
    side: Side
    size: Decimal
    level: Decimal
    opened_at: datetime
    stop_reported: bool
    stop_level: Decimal | None
    stop_level_reported: bool
    target_reported: bool
    target_level: Decimal | None
    target_level_reported: bool


def parse_position(item: Any, where: str = "") -> PositionRow:
    """`{position, market}` → `PositionRow`; czas WYŁĄCZNIE z `createdDateUTC` (`createdDate` jest lokalny).

    Stop (C5, C24): klucz `stopLevel` obecny → jego wartość (jawny `null` → brak stopu); inaczej klucz
    `stopDistance` obecny → `level ∓ stopDistance` (BUY minus, SELL plus; `null` → brak stopu); żadnego klucza →
    `stop_reported=False` i adapter sięga do własnego zapisu.

    Cel zysku (R4, projekt §3.1) tą samą regułą: klucz `profitLevel` obecny → jego wartość (jawny `null` → brak celu);
    inaczej klucz `profitDistance` obecny → `level ± profitDistance` (BUY plus, SELL minus, bo cel leży po stronie
    zysku; `null` → brak celu); żadnego klucza → `target_reported=False` i adapter sięga do własnego zapisu. Nazwy
    kluczy i znak `profitDistance` są NIEPOTWIERDZONE (występują w [S3] i w przykładzie zleceń oczekujących, nie w
    oficjalnym przykładzie pozycji; do sprawdzenia skryptem research/diag_fx.py). Wartość niedodatnia albo nie-liczba
    → `ParseError`, jak dla stopu.
    """
    entry = obj(item, where)
    pos_where, market_where = path(where, "position"), path(where, "market")
    position = obj(get(entry, "position", where), pos_where)
    market = obj(get(entry, "market", where), market_where)
    side = side_of(position, "direction", pos_where)
    level = positive(position, "level", pos_where)
    reported, stop = True, None
    if "stopLevel" in position:
        if position["stopLevel"] is not None:
            stop = positive(position, "stopLevel", pos_where)
    elif "stopDistance" in position:
        if position["stopDistance"] is not None:
            distance = positive(position, "stopDistance", pos_where)
            stop = level - distance if side is Side.BUY else level + distance
    else:
        reported = False
    target_reported, target = True, None
    if "profitLevel" in position:
        if position["profitLevel"] is not None:
            target = positive(position, "profitLevel", pos_where)
    elif "profitDistance" in position:
        if position["profitDistance"] is not None:
            profit = positive(position, "profitDistance", pos_where)
            target = level + profit if side is Side.BUY else level - profit
    else:
        target_reported = False
    return PositionRow(
        deal_id=ident(position, "dealId", pos_where),
        working_order_id=optional_text(position, "workingOrderId", pos_where),
        epic=text(market, "epic", market_where),
        side=side,
        size=positive(position, "size", pos_where),
        level=level,
        opened_at=utc_stamp(position, "createdDateUTC", pos_where),
        stop_reported=reported,
        stop_level=stop,
        stop_level_reported="stopLevel" in position,
        target_reported=target_reported,
        target_level=target,
        target_level_reported="profitLevel" in position,
    )


def parse_positions(payload: Any) -> list[PositionRow]:
    """`GET /positions` → wiersze w kolejności brokera; powtórzony `dealId` → `ParseError`."""
    rows: list[PositionRow] = []
    for i, item in enumerate(items(get(obj(payload, ""), "positions", ""), "positions")):
        row = parse_position(item, path("positions", i))
        if any(r.deal_id == row.deal_id for r in rows):
            raise ParseError(f"positions[{i}].position.dealId: duplicate deal id")
        rows.append(row)
    return rows


def parse_working_orders(payload: Any) -> list[WorkingOrder]:
    """`GET /workingorders` → `WorkingOrder(dealId, epic, direction, orderSize, orderLevel)` z `workingOrderData`."""
    orders: list[WorkingOrder] = []
    for i, item in enumerate(items(get(obj(payload, ""), "workingOrders", ""), "workingOrders")):
        where = path(path("workingOrders", i), "workingOrderData")
        data = obj(get(obj(item, path("workingOrders", i)), "workingOrderData", path("workingOrders", i)), where)
        order_id = text(data, "dealId", where)
        if any(o.order_id == order_id for o in orders):
            raise ParseError(f"{where}.dealId: duplicate order id")
        order = build(
            WorkingOrder,
            where,
            order_id,
            text(data, "epic", where),
            side_of(data, "direction", where),
            positive(data, "orderSize", where),
            positive(data, "orderLevel", where),
        )
        orders.append(order)
    return orders
