"""Czyste parsowanie odpowiedzi REST Capital.com (bez sieci i bez adresów hostów).

Zasady:
- liczby JSON wyłącznie jako `Decimal` (`loads`: także liczby całkowite; NaN/Infinity i powtórzone klucze to błąd),
  więc `float` nigdy nie powstaje; napis z liczbą NIE jest liczbą (pole musi być liczbą JSON);
- znaczniki czasu wyłącznie z pól UTC (`snapshotTimeUTC`); pola lokalne (`snapshotTime`, `updateTime`) nigdy nie są
  parsowane do logiki;
- brak pola, zły typ albo nieoczekiwana jednostka to `ParseError` z nazwą pola, nigdy z treścią odpowiedzi
  (poza krótkimi kodami wyliczeniowymi jak jednostka `PERCENTAGE`); adapter zamienia go na `BrokerError`;
- błędy konstruktorów typów domenowych (`ValueError`/`TypeError` z `gwstock.types`) też stają się `ParseError`
  z nazwą pola.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from gwstock.types import AccountInfo, InstrumentRules, Quote

_ENUM = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
_UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?")  # bez strefy (notatki §1.3)
_FIELD = re.compile(r"[A-Za-z]+\.[A-Za-z_]+")


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
    """JSON z liczbami jako `Decimal`; NaN, Infinity, powtórzony klucz i zły tekst → `ParseError`."""
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
    except (ValueError, RecursionError):
        pass
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


def build(cls: type, where: str, *args: Any) -> Any:
    """Konstruktor typu domenowego; jego `ValueError`/`TypeError` → `ParseError` z nazwą pola (bez wartości)."""
    try:
        return cls(*args)
    except (TypeError, ValueError) as exc:
        head = str(exc).split(":", 1)[0]
        problem = head if _FIELD.fullmatch(head) else type(exc).__name__
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


def parse_instrument_rules(epic: str, payload: Any, account_currency: str | None) -> InstrumentRules:
    """`GET /markets/{epic}` → `InstrumentRules` (notatki §2.16.1; jednostki inne niż oczekiwane → `ParseError`).

    - `min_size` ← `minDealSize.value`, `size_step` ← `minSizeIncrement.value` (jednostka `POINTS`, czyli jednostki
      wielkości, notatki §5 pkt 5);
    - `stop_step` ← `minStepDistance.value` w `POINTS` (DE40: 0,1; czy krok dotyczy stopu: C16c);
    - `value_per_point` ← `instrument.lotSize` w walucie instrumentu (C16), więc waluta instrumentu musi być walutą
      rachunku sesji (inaczej potrzebny byłby kurs, którego adapter nie zgaduje);
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
    if account_currency is None or currency != account_currency:
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

    return build(InstrumentRules, "instrument rules", epic, min_size, size_step, lot_size, factor / 100, min_stop, stop_step)


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
