"""Testy `serialization.dump`: deterministyczny JSON surowych zdarzeń (Decimal jako napis, czas jako ISO UTC)."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gwstock.serialization import dump
from gwstock.types import EntryAck, KillLevel, OpenPosition, Side

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)


@dataclass(frozen=True)
class _Inner:
    when: datetime
    amount: Decimal


@dataclass(frozen=True)
class _Outer:
    zeta: int
    alpha: list
    inner: _Inner


def test_decimal_is_written_as_exact_string():
    assert json.loads(dump(Decimal("0.0035"))) == "0.0035"
    assert json.loads(dump(Decimal("25782.30"))) == "25782.30"  # bez utraty zer i bez float


def test_datetime_is_iso_utc():
    assert json.loads(dump(T0)) == "2026-11-02T07:30:00+00:00"


def test_aware_non_utc_datetime_is_converted_to_utc():
    berlin_winter = timezone(timedelta(hours=1))
    assert json.loads(dump(datetime(2026, 11, 2, 8, 30, tzinfo=berlin_winter))) == "2026-11-02T07:30:00+00:00"


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        dump(datetime(2026, 11, 2, 7, 30))


def test_enum_is_written_by_value():
    assert json.loads(dump(Side.BUY)) == "BUY"
    assert json.loads(dump(KillLevel.HARD)) == 2


def test_primitives_pass_through():
    assert json.loads(dump({"n": None, "t": True, "f": False, "i": 7, "s": "x", "x": 1.5})) == {
        "n": None, "t": True, "f": False, "i": 7, "s": "x", "x": 1.5,
    }


def test_dataclass_becomes_object_with_all_fields():
    pos = OpenPosition("DEAL-1", "DE40", Side.SELL, Decimal("0.007"), Decimal("25780.3"), None, T0)
    assert json.loads(dump(pos)) == {
        "deal_id": "DEAL-1",
        "epic": "DE40",
        "side": "SELL",
        "size": "0.007",
        "open_level": "25780.3",
        "stop_level": None,
        "opened_at": "2026-11-02T07:30:00+00:00",
        "stop_derived": False,
    }
    assert json.loads(dump(EntryAck("REF-1"))) == {"deal_reference": "REF-1"}


def test_nested_containers_and_dataclasses():
    obj = {"order": _Outer(1, [Decimal("1.5"), (Side.BUY, None)], _Inner(T0, Decimal("2")))}
    assert json.loads(dump(obj)) == {
        "order": {
            "alpha": ["1.5", ["BUY", None]],
            "inner": {"amount": "2", "when": "2026-11-02T07:30:00+00:00"},
            "zeta": 1,
        }
    }


def test_key_order_is_deterministic():
    a = dump({"b": 1, "a": {"y": 2, "x": 3}})
    b = dump({"a": {"x": 3, "y": 2}, "b": 1})
    assert a == b
    assert a.index('"a"') < a.index('"b"')
    d = dump(_Outer(1, [], _Inner(T0, Decimal(0))))  # pola dataclass też sortowane, nie w kolejności deklaracji
    assert d.index('"alpha"') < d.index('"zeta"')


def test_enum_dict_key_is_written_by_value():
    assert json.loads(dump({Side.BUY: 1})) == {"BUY": 1}


def test_non_string_dict_key_is_rejected():
    with pytest.raises(TypeError, match="key"):
        dump({1: "x"})


@pytest.mark.parametrize("value", [object(), {1, 2}, b"raw", _Inner])
def test_unsupported_type_is_rejected(value):
    with pytest.raises(TypeError):
        dump(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_float_is_rejected(value):
    with pytest.raises(ValueError):
        dump(value)


def test_output_is_ascii_so_any_text_survives_storage():
    text = dump({"note": "zażółć \udc80"})  # także samotny surogat (np. zepsuta odpowiedź) nie psuje zapisu
    assert text.isascii()
    assert json.loads(text) == {"note": "zażółć \udc80"}
