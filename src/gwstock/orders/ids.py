"""Własne identyfikatory zleceń `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}` i ich odczyt.

Capital.com nie przyjmuje identyfikatora od klienta (spec §6), więc to jest NASZ klucz w dzienniku, łączony z
odpowiedzią brokera. Format musi pozostać jednoznacznie parsowalny: `instrument` i `rola` to wyłącznie [A-Za-z0-9]
(bez "-"), a `seq` to liczba całkowita >= 1 bez zer wiodących. Dziennik liczy zlecenia dnia po prefiksie
`gws-{YYYYMMDD}-` (`Journal.count_orders`), więc część daty nie może się zmienić.
"""

from __future__ import annotations

import re
from datetime import date, datetime

PREFIX = "gws"
MAX_CLIENT_ID_LENGTH = 128

# `re.fullmatch` zamiast `^...$` (znak `$` dopuszczałby końcowy "\n"); jawne klasy ASCII zamiast `\d` i `\w`
# (te dopuszczają cyfry i litery spoza ASCII).
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_DAY = re.compile(r"[0-9]{8}")
_SEQ = re.compile(r"[1-9][0-9]*")


def _check_token(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__} {value!r}")
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be non-empty and match [A-Za-z0-9]+ (no '-'), got {value!r}")


def make_client_id(day: date, instrument: str, seq: int, role: str) -> str:
    """Buduje `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}` (<= 128 znaków); błędny argument to `TypeError`/`ValueError`."""
    # `datetime` jest podklasą `date`, ale dzień z godziną to nie dzień handlowy
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError(f"day must be a date without time, got {type(day).__name__} {day!r}")
    _check_token("instrument", instrument)
    _check_token("role", role)
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise TypeError(f"seq must be int, got {type(seq).__name__} {seq!r}")
    if seq < 1:
        raise ValueError(f"seq must be >= 1, got {seq!r}")
    client_id = f"{PREFIX}-{day.year:04d}{day.month:02d}{day.day:02d}-{instrument}-{seq:d}-{role}"
    if len(client_id) > MAX_CLIENT_ID_LENGTH:
        raise ValueError(f"client id longer than {MAX_CLIENT_ID_LENGTH} characters ({len(client_id)}): {client_id!r}")
    return client_id


def parse_client_id(client_id: str) -> tuple[date, str, int, str]:
    """Odwrotność `make_client_id`: `(dzień, instrument, seq, rola)`; tekst niezgodny z formatem to `ValueError`.

    Przyjmuje tylko ciągi, które `make_client_id` mogłoby wyprodukować (`make_client_id(*parse_client_id(x)) == x`).
    """
    if not isinstance(client_id, str):
        raise TypeError(f"client_id must be str, got {type(client_id).__name__} {client_id!r}")
    if len(client_id) > MAX_CLIENT_ID_LENGTH:
        raise ValueError(f"client id longer than {MAX_CLIENT_ID_LENGTH} characters: {client_id!r}")
    parts = client_id.split("-")
    if len(parts) != 5:
        raise ValueError(f"malformed client id {client_id!r}: expected {PREFIX}-YYYYMMDD-instrument-seq-role")
    prefix, day_text, instrument, seq_text, role = parts
    if prefix != PREFIX:
        raise ValueError(f"malformed client id {client_id!r}: prefix must be {PREFIX!r}")
    if not _DAY.fullmatch(day_text):
        raise ValueError(f"malformed client id {client_id!r}: day must be 8 digits YYYYMMDD")
    try:
        day = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:]))
    except ValueError:
        raise ValueError(f"malformed client id {client_id!r}: {day_text} is not a calendar day") from None
    if not _TOKEN.fullmatch(instrument):
        raise ValueError(f"malformed client id {client_id!r}: instrument must match [A-Za-z0-9]+")
    if not _SEQ.fullmatch(seq_text):
        raise ValueError(f"malformed client id {client_id!r}: seq must be an integer >= 1 without leading zeros")
    if not _TOKEN.fullmatch(role):
        raise ValueError(f"malformed client id {client_id!r}: role must match [A-Za-z0-9]+")
    return day, instrument, int(seq_text), role
