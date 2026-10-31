"""Serializacja ładunków surowych zdarzeń dziennika (`Journal.append_raw`) do tekstu JSON.

Zasady:
- `Decimal` jako napis (dokładnie, bez float), `datetime` jako ISO-8601 w UTC (naiwny czas to `ValueError`);
- enum po wartości, dataclass jako obiekt ze wszystkimi polami, krotka jak lista;
- klucze słowników to napisy (albo enumy o wartości napisowej), sortowane: ten sam obiekt daje zawsze ten sam tekst;
- wynik jest czystym ASCII (znaki spoza ASCII jako `\\uXXXX`), więc żaden tekst, nawet zepsuty, nie psuje zapisu;
- nieznany typ to `TypeError`, a float lub Decimal nieskończony albo NaN to `ValueError` (nie zgadujemy reprezentacji).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def dump(obj: Any) -> str:
    """Tekst JSON obiektu (deterministyczny: posortowane klucze, zwarte separatory)."""
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _plain(value: Any) -> Any:
    """Zamienia wartość na drzewo typów JSON (dict, list, str, int, float, bool, None)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):  # przed str/int: enumy projektu dziedziczą po str albo int
        return _plain(value.value)
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"cannot serialize non-finite Decimal {value!r}: expected a finite value")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"cannot serialize naive datetime {value!r}: expected timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {_key(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    raise TypeError(f"cannot serialize {type(value).__name__} {value!r}")


def _key(key: Any) -> str:
    plain = key.value if isinstance(key, Enum) else key
    if not isinstance(plain, str):
        raise TypeError(f"cannot serialize dict key {key!r}: expected str")
    return plain
