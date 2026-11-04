"""Odczyty dziennika dla silnika z pasmami (zadanie 12): wszystkie zapisy pozycji i transakcje z `deal_id`.

Tylko odczyt, bez zmiany schematu (`SCHEMA_VERSION = 1`).
"""

from decimal import Decimal

import pytest

from gwstock.journal import PositionRecord
from gwstock.types import PositionState, Side
from tests.unit.test_journal import DAY, make_position, make_trade


def test_positions_lists_every_instrument_in_epic_order(journal):
    assert journal.positions() == []
    journal.upsert_position(make_position(epic="US500", deal_id="D-2"))
    journal.upsert_position(make_position())
    journal.upsert_position(PositionRecord("US100", PositionState.FLAT))
    assert [p.epic for p in journal.positions()] == ["DE40", "US100", "US500"]
    assert journal.positions()[2] == make_position(epic="US500", deal_id="D-2")


def test_trades_by_deal_returns_the_deal_ids_in_booking_order_and_filters_by_instrument(journal):
    journal.record_trade(make_trade(), "D-1", DAY)
    journal.record_trade_pending(make_trade(instrument="US500", side=Side.SELL), "D-2", DAY)
    journal.record_trade(make_trade(net_pnl=Decimal("0.01")), "D-3", DAY)
    assert [d for d, _ in journal.trades_by_deal()] == ["D-1", "D-2", "D-3"]
    assert [(d, t.instrument) for d, t in journal.trades_by_deal("US500")] == [("D-2", "US500")]
    assert [t for _, t in journal.trades_by_deal()] == journal.trades()


def test_trades_by_deal_never_silently_returns_the_whole_ledger_for_a_bad_epic(journal):
    with pytest.raises(ValueError):
        journal.trades_by_deal("")
