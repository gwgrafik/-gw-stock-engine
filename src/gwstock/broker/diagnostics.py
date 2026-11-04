"""Odczyty diagnostyczne próby pasma (`smoke --epic`, zadanie 17): surowe pola celu zysku i wiersze historii transakcji.

Służą WYŁĄCZNIE rozstrzygnięciu niepotwierdzonych założeń o brokerze na jednej próbie właściciela (projekt wielu pasm
§3.1, §7.5): C5 (nazwy i obecność kluczy celu w `GET /positions/{dealId}` i `GET /confirms/{dealReference}`), C5d (czy
`PUT` z samym stopem kasuje cel: odczyt po `set_stop`) i C8 (waluta i kwota rozliczenia wyniku w `GET
/history/transactions`). Silnik ich nie używa i nie należą do protokołu `Broker`: adapter Capital.com i FakeBroker mają
metody `diagnose_target_fields(deal_id, deal_reference)` i `diagnose_transactions(start, end)`, a próba woła je po
nazwie (brak metody = „nie ustalono”). Tylko odczyt, niczego nie zmieniają u brokera.

Do wydruku trafiają wyłącznie nazwy kluczy i wartości z list poniżej, przepuszczone przez `show` (liczby jawnie, napisy z
maskowaniem identyfikatorów i długich liczb); `dealId`, `dealReference`, `reference` i numery rachunków nigdy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "FieldsRead", "TargetFieldsReport", "SettlementRow", "split_fields", "parse_transactions", "show",
    "TARGET_KEY", "OTHER_KEYS",
]  # fmt: skip

# Klucze celu zysku (także nazwy spoza założeń adaptera, np. `limitLevel`): profit*/limit* Level/Distance/Amount.
TARGET_KEY = re.compile(r"^(profit|limit)(level|distance|amount)$", re.IGNORECASE)
# Pozostałe klucze rekordu pozycji/potwierdzenia potrzebne ustaleniom (C5: poziom wykonania i stop; C8: waluta, upl).
OTHER_KEYS = ("currency", "upl", "level", "stopLevel", "stopDistance")
_MAX_TEXT = 60

_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4,12}"
                   r"(?![0-9A-Za-z])")  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_LONG_NUM = re.compile(r"(?<![\d.])\d{6,}(?!\d)(?!\.\d)")  # całkowite od 6 cyfr (numery); ceny z ułamkiem zostają


@dataclass(frozen=True)
class FieldsRead:
    """Odczyt jednego rekordu: `found` (pozycja na liście / potwierdzenie znane), klucze celu i inne klucze z
    `OTHER_KEYS` z SUROWEJ odpowiedzi (wartości jak w odpowiedzi: `Decimal`, napis albo None dla jawnego `null`);
    `error`: nazwa wyjątku, gdy odczyt się nie udał (wtedy `found` False)."""

    found: bool
    target: dict[str, Any] = field(default_factory=dict)
    other: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class TargetFieldsReport:
    """Pola celu pozycji (`GET /positions/{dealId}`) i potwierdzenia wejścia (`GET /confirms/{dealReference}`)."""

    position: FieldsRead
    confirm: FieldsRead


@dataclass(frozen=True)
class SettlementRow:
    """Wiersz `GET /history/transactions` bez `reference` (powiązanie z dealId jest niepotwierdzone, C8, i nie jest
    drukowane): typ, waluta, kwota (`size` jako napis liczby: wynik, C8), czas UTC, nazwa instrumentu, notatka."""

    kind: str | None
    currency: str | None
    amount: Decimal | None
    ts: datetime | None
    instrument: str | None
    note: str | None


def split_fields(record: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(klucze celu, klucze z `OTHER_KEYS`) rekordu; nie-słownik to dwa puste słowniki."""
    if not isinstance(record, dict):
        return {}, {}
    target = {k: v for k, v in record.items() if isinstance(k, str) and TARGET_KEY.match(k)}
    other = {k: record[k] for k in OTHER_KEYS if k in record}
    return target, other


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return number if number.is_finite() else None
    return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def parse_transactions(payload: Any) -> list[SettlementRow]:
    """Wiersze `transactions` odpowiedzi (inne elementy pomijane); nieczytelna kwota albo czas to None."""
    rows = payload.get("transactions") if isinstance(payload, dict) else None
    out: list[SettlementRow] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append(SettlementRow(
            _text(row.get("transactionType")), _text(row.get("currency")), _amount(row.get("size")),
            _time(row.get("dateUtc", row.get("dateUTC"))), _text(row.get("instrumentName")), _text(row.get("note")),
        ))  # fmt: skip
    return out


def _token(match: re.Match[str]) -> str:
    text = match.group(0)
    return "<TOKEN>" if re.search(r"\d", text) and re.search(r"[A-Za-z]", text) else text


def show(value: Any) -> str:
    """Wartość do wydruku: liczby jawnie (bez zapisu naukowego), None jako `null`, napisy bez identyfikatorów UUID,
    długich tokenów i liczb całkowitych od 6 cyfr, przycięte do 60 znaków; inne typy: sama nazwa typu."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, int):
        return "<NUM>" if len(str(abs(value))) >= 6 else str(value)
    if not isinstance(value, str):
        return type(value).__name__
    text = _LONG_NUM.sub("<NUM>", _TOKEN.sub(_token, _UUID.sub("<ID>", value)))
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"
