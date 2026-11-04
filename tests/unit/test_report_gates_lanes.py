"""Bramka L1 per pasmo (zadanie 15): liczona z transakcji jednego instrumentu, incydenty portfela w każdym pasmie."""

from decimal import Decimal

import pytest

from gwstock.journal import OrderRecord
from gwstock.orders.ids import make_client_id
from gwstock.report import gates
from gwstock.report.gates import gate_l1_status
from gwstock.types import OrderState, Side
from tests.unit.report_lane_support import US, USD_TRADE, book_lane, book_usd
from tests.unit.test_report import COSTS, D1, D2, D3, D4, D5, D6, at, emit


def test_l1_counts_only_the_trades_of_the_given_instrument(journal):
    for day in (D1, D2, D3):
        book_lane(journal, day, "DE40")
    for day in (D3, D4, D5, D6):
        book_lane(journal, day, US, vpp="1")
    assert gate_l1_status(journal, costs=COSTS, instrument="DE40").sessions_at_min_size == 3
    assert gate_l1_status(journal, costs=COSTS, instrument=US).sessions_at_min_size == 4
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 6  # bez filtra: cały dziennik, jak dotąd
    assert gates.l1_details(journal, costs=COSTS, instrument="DE40").roles == ("entry",) * 3


def test_the_minimum_size_is_the_minimum_of_the_lane_not_of_the_ledger(journal):
    book_lane(journal, D1, "DE40", size=Decimal("0.001"))
    for day in (D2, D3):
        book_lane(journal, day, US, size=Decimal("0.01"))
    assert gate_l1_status(journal, costs=COSTS, instrument=US).sessions_at_min_size == 2  # minimum pasma: 0,01
    assert gate_l1_status(journal, costs=COSTS).sessions_at_min_size == 1  # cały dziennik: minimum 0,001


def test_cost_samples_and_verdict_of_a_lane_ignore_the_other_lane(journal):
    book_lane(journal, D1, "DE40")  # bez poślizgu
    book_lane(journal, D2, US, entry_fill=Decimal("7586.6"), decision_bid=Decimal("7583.0"),
              decision_ask=Decimal("7583.6"))  # poślizg 3 pkt  # fmt: skip
    de40 = gates.l1_details(journal, costs=COSTS, instrument="DE40")
    us = gates.l1_details(journal, costs=COSTS, instrument=US)
    assert de40.gate.cost_samples == 1 and de40.gate.cost_deviation_ratio == Decimal(0)
    assert us.gate.cost_samples == 1 and us.gate.cost_deviation_ratio == Decimal("6")  # 3 / 0,5
    assert gates.l1_details(journal, costs=COSTS).gate.cost_samples == 2


def test_portfolio_incidents_count_for_every_lane(journal):
    for day in (D1, D2):
        book_lane(journal, day, "DE40")
        book_lane(journal, day, US, seq=1)
    emit(journal, "reconcile.event", {"event": "MISMATCH_TIMEOUT", "since": at(D1, 8, 10)}, at(D1, 8, 20))
    emit(journal, "event", {"event": "ENTRY_UNRESOLVED", "order_id": make_client_id(D2, US, 9, "entry")}, at(D2, 8, 8))
    journal.insert_order(OrderRecord(make_client_id(D2, "DE40", 9, "entry"), "DE40", Side.BUY, Decimal("0.001"),
                                     Decimal("30"), OrderState.UNKNOWN, "r", None, at(D2, 9, 5), at(D2, 9, 6)))  # fmt: skip
    for instrument in ("DE40", US):
        gate = gate_l1_status(journal, costs=COSTS, instrument=instrument)
        assert gate.unreconciled_over_60s == 1  # incydent portfela liczy się w KAŻDYM pasmie
        assert gate.unhandled_rejections == 2  # także odrzucenie wejścia drugiego pasma
    assert gates.l1_details(journal, costs=COSTS, instrument=US).mismatch_days == (D1,)


def test_a_trade_list_given_with_an_instrument_is_filtered_to_it(journal):
    book_lane(journal, D1, "DE40")
    book_lane(journal, D2, US)
    details = gates.l1_details(journal, costs=COSTS, trades=journal.trades(), instrument=US)
    assert len(details.roles) == 1 and details.gate.sessions_at_min_size == 1


@pytest.mark.parametrize("bad", ["", 5, b"DE40", ["DE40"]])
def test_the_instrument_argument_is_validated_never_silently_everything(journal, bad):
    book_lane(journal, D1, "DE40")
    assert gate_l1_status(journal, costs=COSTS, instrument="DE40").sessions_at_min_size == 1  # kontrola dodatnia
    with pytest.raises((TypeError, ValueError), match="instrument"):
        gate_l1_status(journal, costs=COSTS, instrument=bad)


def test_equal_timestamps_in_two_lanes_do_not_swap_their_contexts(journal):
    # dwa pasma zdecydowały w tej samej chwili: kontekst (wartość punktu) każdej transakcji ma być jej własny
    book_lane(journal, D1, "DE40", vpp="1")
    book_lane(journal, D1, US, vpp="5", **USD_TRADE)
    trades = journal.trades()
    assert trades[0].signal_ts == trades[1].signal_ts and trades[0].submit_ts == trades[1].submit_ts
    contexts = gates.trade_contexts(journal, trades)
    assert [c.value_per_point for c in contexts] == [Decimal("1"), Decimal("5")]


def test_the_cost_ratio_in_the_book_currency_is_never_computed_across_lanes(journal):
    book_lane(journal, D1, "DE40", spread_cost=Decimal("4.00"), slippage_penalty=Decimal("0.50"))
    book_usd(journal, D2, settle="5.90")
    assert gates.l1_details(journal, costs=COSTS).eur_samples == 0  # kwoty EUR i USD: brak wspólnej waluty
    assert gates.l1_details(journal, costs=COSTS, instrument="DE40").eur_samples == 1
    assert gates.l1_details(journal, costs=COSTS, instrument=US).eur_samples == 1
