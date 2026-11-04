"""Testy bramki portfelowej (`gwstock.portfolio`, spec wielu pasm §4) i ładowania `config/portfolio.toml`.

Granice z tabeli §4.2: „równo przechodzi, o grosz więcej blokuje”. Kontrola, której nie da się ocenić, blokuje wejście
swoim powodem; wyjść bramka nie blokuje nigdy. Przy jednym paśmie DE40 i limitach z `config/portfolio.toml` bramka nie
dodaje powodu, którego nie dałaby bramka pasma (`RiskGate`): test równoważności na siatce wartości.

Uruchamiać z korzenia repozytorium (ścieżki względne `config/`). Zegar: 09:06 w Berlinie (okno wejścia DE40).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import shutil
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock import execution, prereg
from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import PortfolioConfig, RiskConfig, load_config, load_portfolio
from gwstock.journal import Journal, JournalReadError, OrderRecord, PositionRecord, TradeRecord
from gwstock.portfolio import (
    PORTFOLIO_DAILY_LOSS,
    PORTFOLIO_FX_UNAVAILABLE,
    PORTFOLIO_GATE_ERROR,
    PORTFOLIO_LEVERAGE,
    PORTFOLIO_MARGIN,
    PORTFOLIO_MAX_POSITIONS,
    PORTFOLIO_OPEN_RISK,
    PORTFOLIO_PNL_PENDING,
    PORTFOLIO_TOTAL_DRAWDOWN,
    PORTFOLIO_UNMANAGED_EXPOSURE,
    REASON_ORDER,
    PortfolioGate,
    PortfolioLane,
)
from gwstock.risk import DAILY_LOSS, DUPLICATE, LEVERAGE, MARGIN, TOTAL_DRAWDOWN, EntryCandidate, RiskGate, Verdict
from gwstock.serialization import dump
from gwstock.types import AccountInfo, KillLevel, OrderState, PositionState, Side

D = Decimal
CAL = ExchangeCalendar(XETRA)
DAY = date(2026, 11, 2)  # poniedziałek, dzień sesji XETRA
EARLIER = date(2026, 10, 30)
T = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
CONFIG = Path("config")
PORTFOLIO_FILE = "portfolio.toml"

DE40 = PortfolioLane("DE40", "EUR", D("1.00"))
FR40 = PortfolioLane("FR40", "EUR", D("1.00"))  # drugie pasmo w EUR (instrument testowy)
US500 = PortfolioLane("US500", "USD", D("1.00"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(CAL.local_to_utc(DAY, time(9, 6)))


@pytest.fixture(scope="module")
def pcfg() -> PortfolioConfig:
    return load_portfolio(CONFIG)


@pytest.fixture(scope="module")
def rcfg() -> RiskConfig:
    return load_config(CONFIG)[1]


class Fx:
    """Kurs z brokera (atrapa wspólnego `gwstock.fx.FxView`): `to_account(kwota, waluta)` mnoży przez stały kurs albo
    zwraca/rzuca, co mu zadano."""

    account_currency = "EUR"

    def __init__(self, rate: object = D("0.9"), *, raises: bool = False) -> None:
        self.rate = rate
        self.raises = raises
        self.calls: list[tuple[Decimal, str]] = []

    def par_is_upper_bound(self, currency: str) -> bool:
        return False

    def to_account(self, amount: Decimal, currency: str) -> Decimal | None:
        self.calls.append((amount, currency))
        if self.raises:
            raise RuntimeError("fx feed down")
        if not isinstance(self.rate, Decimal):
            return self.rate  # type: ignore[return-value]
        return amount * self.rate


class Exposure:
    def __init__(self, epics=(), *, raises: bool = False) -> None:
        self.epics = epics
        self.raises = raises

    def unmanaged_epics(self):
        if self.raises:
            raise RuntimeError("no successful broker read yet")
        return self.epics


def build(journal, clock, cfg, *, lanes=(DE40, FR40), fx=None, exposure=None) -> PortfolioGate:
    return PortfolioGate(cfg, list(lanes), journal, CAL, clock, fx=fx, exposure=exposure)


def acct(*, equity="40", available="40", currency="EUR") -> AccountInfo:
    return AccountInfo("ACC-1", currency, "ENABLED", D(equity), D(available), D("0"))


ACCT = acct()


def cand(**over) -> EntryCandidate:
    """Kandydat FR40: 0.001 × stop 30 × 1 = ryzyko 0,03 EUR, nominał 8 EUR, depozyt 0,40 EUR."""
    fields = dict(
        epic="FR40",
        side=Side.BUY,
        size=D("0.001"),
        stop_distance=D("30"),
        entry_price=D("8000"),
        value_per_point=D("1"),
        margin_rate=D("0.05"),
        quote_age_s=5,
    )
    fields.update(over)
    return EntryCandidate(**fields)


def put_position(
    journal: Journal,
    epic: str = "DE40",
    *,
    state: PositionState = PositionState.OPEN,
    size: str | None = "0.005",
    open_level: str | None = "20000",
    stop: str | None = "19900",
    vpp: str | None = "1",
    deal: str | None = "DEAL-1",
    ctx: bool = True,
    via: str = "by_deal",
    currency: str | None = None,
    seq: int = 1,
) -> str:
    """Pozycja w dzienniku z kontekstem wejścia jak w `Executor._prepare` (domyślnie ryzyko 0,50 EUR, nominał 100)."""
    client_id = f"gws-20261102-{epic}-{seq}-entry"
    if state is PositionState.ENTERING:
        journal.upsert_position(PositionRecord(epic, state))
    else:
        journal.upsert_position(
            PositionRecord(
                epic, state, deal, Side.BUY, None if size is None else D(size),
                None if open_level is None else D(open_level), None if stop is None else D(stop), T,
            )
        )  # fmt: skip
    if ctx:
        context = {
            "client_id": client_id, "epic": epic, "side": Side.BUY, "size": D(size or "0.005"),
            "stop_distance": D("100"), "decision_bid": D("19999"), "decision_ask": D("20001"),
            "decision_ts": T, "signal_ts": T, "submit_ts": T,
        }  # fmt: skip
        if vpp is not None:
            context["value_per_point"] = D(vpp)
        if currency is not None:
            context["currency"] = currency
        journal.set_meta(f"entry_ctx:{client_id}", dump(context))
        if via == "by_deal" and deal is not None:
            journal.set_meta(f"entry_by_deal:{deal}", client_id)
        elif via == "last":
            journal.set_meta(f"entry_last:{epic}", client_id)
            journal.insert_order(
                OrderRecord(
                    client_id, epic, Side.BUY, D(size or "0.005"), D("100"), OrderState.CONFIRMED, "R", deal, T, T
                )
            )
    return client_id


def put_order(journal: Journal, epic: str = "DE40", *, seq: int = 1, state=OrderState.SUBMITTING, ctx=True) -> str:
    client_id = f"gws-20261102-{epic}-{seq}-entry"
    journal.insert_order(OrderRecord(client_id, epic, Side.BUY, D("0.005"), D("100"), state, None, None, T, T))
    if ctx:
        context = {
            "client_id": client_id, "epic": epic, "side": Side.BUY, "size": D("0.005"),
            "stop_distance": D("100"), "value_per_point": D("1"), "decision_bid": D("19999"),
            "decision_ask": D("20001"), "decision_ts": T, "signal_ts": T, "submit_ts": T,
        }  # fmt: skip
        journal.set_meta(f"entry_ctx:{client_id}", dump(context))
    return client_id


def reasons(gate: PortfolioGate, c=None, account=ACCT) -> list[str]:
    return gate.check_entry(cand() if c is None else c, account).reasons


def trade(net: str) -> TradeRecord:
    return TradeRecord(
        "US500", Side.BUY, D("0.01"), D("1"), D("1"), D("1"), D("2"), D(net), D("0"), D("0"), D("0"), D("0"), D(net),
        T, T, T, T, 0, "STOP",
    )  # fmt: skip


# --- stałe ------------------------------------------------------------------------------------------


def test_reason_constants_are_their_own_names_in_table_order():
    assert list(REASON_ORDER) == [
        "PORTFOLIO_MAX_POSITIONS", "PORTFOLIO_OPEN_RISK", "PORTFOLIO_DAILY_LOSS", "PORTFOLIO_TOTAL_DRAWDOWN",
        "PORTFOLIO_LEVERAGE", "PORTFOLIO_MARGIN", "PORTFOLIO_PNL_PENDING", "PORTFOLIO_FX_UNAVAILABLE",
        "PORTFOLIO_UNMANAGED_EXPOSURE",
    ]  # fmt: skip
    assert PORTFOLIO_GATE_ERROR == "PORTFOLIO_GATE_ERROR"


def test_entry_context_keys_match_the_executor():
    # bramka czyta kontekst wejścia zapisany przez wykonawcę; klucze nie mogą się rozjechać
    from gwstock import portfolio

    assert portfolio.META_ENTRY_CTX == execution.META_ENTRY_CTX
    assert portfolio.META_ENTRY_BY_DEAL == execution.META_ENTRY_BY_DEAL
    assert portfolio.META_ENTRY_LAST == execution.META_ENTRY_LAST


def test_lanes_must_be_unique_and_well_formed(journal, clock, pcfg):
    with pytest.raises(ValueError):
        build(journal, clock, pcfg, lanes=(DE40, DE40))
    with pytest.raises(ValueError):
        PortfolioLane("", "EUR", D("1"))
    with pytest.raises(ValueError):
        PortfolioLane("DE40", "", D("1"))
    with pytest.raises((TypeError, ValueError)):
        PortfolioLane("DE40", "EUR", 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PortfolioLane("DE40", "EUR", D("0"))


# --- czysty start, wyjścia --------------------------------------------------------------------------


def test_clean_candidate_with_empty_book_passes(journal, clock, pcfg):
    v = build(journal, clock, pcfg).check_entry(cand(), ACCT)
    assert v == Verdict(True, [])


def test_exit_is_never_blocked_even_with_a_broken_journal(journal, clock, pcfg):
    gate = build(journal, clock, pcfg)
    journal.add_realized_pnl(DAY, D("-100"))
    put_position(journal, "DE40")
    put_position(journal, "FR40", deal="DEAL-2")
    journal.close()
    assert gate.check_exit() == Verdict(True, [])


# --- PORTFOLIO_MAX_POSITIONS -----------------------------------------------------------------------


def test_one_open_position_below_the_limit_of_two_passes(journal, clock, pcfg):
    assert pcfg.max_open_positions == 2
    put_position(journal, "DE40")
    assert reasons(build(journal, clock, pcfg)) == []


def test_two_occupied_instruments_block_a_third_entry(journal, clock, pcfg):
    gate = build(journal, clock, pcfg, lanes=(DE40, FR40, PortfolioLane("UK100", "EUR", D("1.00"))))
    put_position(journal, "DE40", size="0.001")
    put_position(journal, "FR40", size="0.001", deal="DEAL-2")
    assert PORTFOLIO_MAX_POSITIONS in reasons(gate, cand(epic="UK100"))


@pytest.mark.parametrize("state", [PositionState.BLOCKED, PositionState.ENTERING, PositionState.EXITING])
def test_every_state_but_flat_occupies_a_slot(journal, clock, pcfg, state):
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1))
    put_position(journal, "DE40", state=state)
    assert PORTFOLIO_MAX_POSITIONS in reasons(gate)


def test_flat_position_does_not_occupy_a_slot(journal, clock, pcfg):
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1))
    journal.upsert_position(PositionRecord("DE40", PositionState.FLAT))
    assert reasons(gate) == []


def test_unresolved_order_occupies_a_slot_and_counts_once_with_its_position(journal, clock, pcfg):
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1))
    put_order(journal, "DE40", state=OrderState.INTENT)
    assert PORTFOLIO_MAX_POSITIONS in reasons(gate)
    gate2 = build(journal, clock, pcfg)  # limit 2: pozycja ENTERING i jej zlecenie to JEDEN instrument
    journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
    assert PORTFOLIO_MAX_POSITIONS not in reasons(gate2)


def test_resolved_order_does_not_occupy_a_slot(journal, clock, pcfg):
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1))
    put_order(journal, "DE40", state=OrderState.REJECTED)
    assert reasons(gate) == []


def test_position_on_instrument_without_lane_found_through_its_order_counts(journal, clock, pcfg):
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1), lanes=(FR40,))
    put_order(journal, "DE40", state=OrderState.UNKNOWN)
    assert PORTFOLIO_MAX_POSITIONS in reasons(gate)


# --- PORTFOLIO_OPEN_RISK ---------------------------------------------------------------------------


def _risk_gate(journal, clock, pcfg, limit="1.00"):
    # limit dzienny wyżej, żeby granicę wyznaczała wyłącznie suma ryzyka otwartego
    return build(journal, clock, dataclasses.replace(pcfg, max_open_risk=D(limit)))


def test_open_risk_equal_to_the_limit_passes(journal, clock, pcfg):
    put_position(journal, "DE40")  # 0.005 × 100 × 1 = 0,50
    gate = _risk_gate(journal, clock, pcfg)
    assert reasons(gate, cand(size=D("0.01"), stop_distance=D("50"))) == []  # 0,50 + 0,50 = 1,00


def test_open_risk_a_cent_over_the_limit_blocks(journal, clock, pcfg):
    put_position(journal, "DE40")
    gate = _risk_gate(journal, clock, pcfg)
    assert reasons(gate, cand(size=D("0.01"), stop_distance=D("51"))) == [PORTFOLIO_OPEN_RISK]  # 1,01


def test_open_risk_uses_value_per_point_of_the_position_context_not_of_the_candidate(journal, clock, pcfg):
    put_position(journal, "DE40", vpp="2", size="0.0025", stop="19800")  # 0.0025 × 200 × 2 = 1,00 (kandydat: 1)
    gate = _risk_gate(journal, clock, pcfg, "1.03")
    assert reasons(gate) == []  # 1,00 + 0,03
    gate = _risk_gate(journal, clock, pcfg, "1.02")
    assert reasons(gate) == [PORTFOLIO_OPEN_RISK]


def test_open_risk_context_found_through_the_last_entry_pointer(journal, clock, pcfg):
    put_position(journal, "DE40", vpp="2", size="0.0025", stop="19800", via="last")
    assert reasons(_risk_gate(journal, clock, pcfg, "1.02")) == [PORTFOLIO_OPEN_RISK]
    assert reasons(_risk_gate(journal, clock, pcfg, "1.03")) == []


def test_short_position_risk_is_the_absolute_distance(journal, clock, pcfg):
    put_position(journal, "DE40", stop="20100")
    assert reasons(_risk_gate(journal, clock, pcfg, "0.53")) == []
    assert reasons(_risk_gate(journal, clock, pcfg, "0.52")) == [PORTFOLIO_OPEN_RISK]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": PositionState.ENTERING},
        {"stop": None},
        {"size": None},
        {"open_level": None},
        {"vpp": None},
        {"ctx": False},
        {"state": PositionState.BLOCKED, "size": None, "open_level": None, "stop": None, "deal": None, "ctx": False},
    ],
    ids=["entering", "no-stop", "no-size", "no-open-level", "no-vpp", "no-context", "blocked-lost-trade"],
)
def test_unknown_position_risk_counts_the_full_loss_per_trade_of_its_lane(journal, clock, pcfg, kwargs):
    put_position(journal, "DE40", **kwargs)  # pełne 1,00 pasma DE40
    assert PORTFOLIO_OPEN_RISK not in reasons(_risk_gate(journal, clock, pcfg, "1.03"))
    assert PORTFOLIO_OPEN_RISK in reasons(_risk_gate(journal, clock, pcfg, "1.02"))


def test_position_on_instrument_without_lane_counts_unknown_position_risk(journal, clock, pcfg):
    assert pcfg.unknown_position_risk >= D("1.00")
    put_order(journal, "UK100", state=OrderState.UNKNOWN)  # instrument bez pasma, ryzyko nieznane
    cfg = dataclasses.replace(pcfg, unknown_position_risk=D("0.70"), max_open_risk=D("0.73"))
    got = reasons(build(journal, clock, cfg))
    assert PORTFOLIO_OPEN_RISK not in got
    assert PORTFOLIO_LEVERAGE in got  # waluta instrumentu bez pasma nieznana: nominału nie da się ocenić
    cfg = dataclasses.replace(cfg, max_open_risk=D("0.72"))
    assert PORTFOLIO_OPEN_RISK in reasons(build(journal, clock, cfg))


def test_risks_of_all_positions_add_up(journal, clock, pcfg):
    gate = build(
        journal, clock, dataclasses.replace(pcfg, max_open_positions=3, max_open_risk=D("1.03")),
        lanes=(DE40, FR40, PortfolioLane("UK100", "EUR", D("1.00"))),
    )  # fmt: skip
    put_position(journal, "DE40", size="0.0025", stop="19800")  # 0,50 (nominał 50)
    put_position(journal, "FR40", size="0.0025", stop="19800", deal="DEAL-2")  # 0,50
    assert reasons(gate, cand(epic="UK100")) == []  # 1,03
    assert reasons(gate, cand(epic="UK100", stop_distance=D("31"))) == [PORTFOLIO_OPEN_RISK]


def test_malformed_position_context_blocks_rather_than_guessing(journal, clock, pcfg):
    client_id = put_position(journal, "DE40")
    journal.set_meta(f"entry_ctx:{client_id}", "{not json")
    got = reasons(build(journal, clock, pcfg))
    assert PORTFOLIO_OPEN_RISK in got and PORTFOLIO_DAILY_LOSS in got


@pytest.mark.parametrize("vpp", ["0", "-1", "NaN", "abc"])
def test_bad_value_per_point_in_context_blocks(journal, clock, pcfg, vpp):
    client_id = put_position(journal, "DE40")
    text = journal.get_meta(f"entry_ctx:{client_id}")
    journal.set_meta(f"entry_ctx:{client_id}", text.replace('"value_per_point":"1"', f'"value_per_point":"{vpp}"'))
    assert PORTFOLIO_OPEN_RISK in reasons(build(journal, clock, pcfg))


def test_context_of_another_client_is_not_trusted(journal, clock, pcfg):
    client_id = put_position(journal, "DE40")
    text = journal.get_meta(f"entry_ctx:{client_id}")
    journal.set_meta(f"entry_ctx:{client_id}", text.replace(client_id, "gws-20261102-DE40-9-entry"))
    assert PORTFOLIO_OPEN_RISK in reasons(build(journal, clock, pcfg))


# --- PORTFOLIO_DAILY_LOSS --------------------------------------------------------------------------


def test_daily_loss_limit_of_the_portfolio_equals_risk_v1(pcfg, rcfg):
    assert pcfg.max_daily_loss == rcfg.max_daily_loss == D("1.50")


def test_daily_loss_equal_to_the_limit_with_the_candidate_passes(journal, clock, pcfg):
    journal.add_realized_pnl(DAY, D("-1.47"))
    assert reasons(build(journal, clock, pcfg)) == []  # 1,47 + 0,03 = 1,50


def test_daily_loss_a_cent_over_the_limit_with_the_candidate_blocks(journal, clock, pcfg):
    journal.add_realized_pnl(DAY, D("-1.48"))
    assert reasons(build(journal, clock, pcfg)) == [PORTFOLIO_DAILY_LOSS]  # 1,51


def test_daily_loss_reached_blocks_any_candidate(journal, clock, pcfg):
    journal.add_realized_pnl(DAY, D("-1.50"))
    assert PORTFOLIO_DAILY_LOSS in reasons(build(journal, clock, pcfg), cand(size=D("0.001"), stop_distance=D("1")))


def test_daily_loss_counts_open_risk_of_every_lane(journal, clock, pcfg):
    # RiskGate DE40 nie widziałby pozycji FR40 (ustalenie A5): portfel ją widzi
    put_position(journal, "FR40", stop="19800", deal="DEAL-2")  # 0.005 × 200 = 1,00
    journal.add_realized_pnl(DAY, D("-0.47"))
    gate = build(journal, clock, pcfg)
    assert reasons(gate, cand(epic="DE40")) == []  # 0,47 + 1,00 + 0,03 = 1,50
    assert reasons(gate, cand(epic="DE40", stop_distance=D("31"))) == [PORTFOLIO_DAILY_LOSS]


def test_daily_loss_open_risk_alone_at_the_limit_blocks(journal, clock, pcfg):
    put_position(journal, "FR40", stop="19800", deal="DEAL-2")  # 1,00
    journal.add_realized_pnl(DAY, D("-0.50"))
    assert PORTFOLIO_DAILY_LOSS in reasons(build(journal, clock, pcfg), cand(epic="DE40"))


def test_gain_of_the_day_does_not_offset_risk(journal, clock, pcfg):
    journal.add_realized_pnl(DAY, D("5"))
    assert PORTFOLIO_DAILY_LOSS in reasons(build(journal, clock, pcfg), cand(size=D("0.051")))  # 1,53 > 1,50
    assert reasons(build(journal, clock, pcfg), cand(size=D("0.05"), entry_price=D("2000"))) == []  # 1,50


def test_loss_of_another_day_is_not_the_daily_loss(journal, clock, pcfg):
    journal.add_realized_pnl(EARLIER, D("-1.49"))
    assert reasons(build(journal, clock, pcfg)) == []


def test_daily_loss_cannot_be_evaluated_without_the_trading_day(journal, pcfg):
    class BrokenClock:
        def now_utc(self):
            raise RuntimeError("clock")

    assert PORTFOLIO_DAILY_LOSS in reasons(build(journal, BrokenClock(), pcfg))


# --- PORTFOLIO_TOTAL_DRAWDOWN ----------------------------------------------------------------------


def test_total_drawdown_at_the_limit_blocks_and_a_cent_above_passes(journal, clock, pcfg, rcfg):
    assert pcfg.max_total_drawdown == rcfg.max_total_drawdown == D("12")
    journal.add_realized_pnl(EARLIER, D("-11.99"))
    gate = build(journal, clock, pcfg)
    assert reasons(gate) == []
    journal.add_realized_pnl(date(2026, 10, 29), D("-0.01"))
    assert reasons(gate) == [PORTFOLIO_TOTAL_DRAWDOWN]


# --- PORTFOLIO_LEVERAGE ----------------------------------------------------------------------------


def test_leverage_equal_to_the_limit_passes_and_a_cent_over_blocks(journal, clock, pcfg):
    put_position(journal, "DE40")  # nominał 0.005 × 20000 × 1 = 100
    gate = build(journal, clock, pcfg)  # 5 × min(40, 40) = 200
    big = dict(size=D("0.0125"), stop_distance=D("1"), margin_rate=D("0.01"))
    assert reasons(gate, cand(**big)) == []  # 100 + 0.0125 × 8000 = 200
    assert reasons(gate, cand(**big, entry_price=D("8000.8"))) == [PORTFOLIO_LEVERAGE]  # 200,01


def test_leverage_capital_is_the_smaller_of_virtual_capital_and_equity(journal, clock, pcfg):
    gate = build(journal, clock, pcfg)
    c = cand(size=D("0.005"), stop_distance=D("1"))  # nominał 40
    assert reasons(gate, c, acct(equity="8")) == []  # 5 × 8 = 40
    assert reasons(gate, c, acct(equity="7.99")) == [PORTFOLIO_LEVERAGE]


@pytest.mark.parametrize("equity", ["0", "-1"])
def test_no_capital_blocks_leverage(journal, clock, pcfg, equity):
    assert PORTFOLIO_LEVERAGE in reasons(build(journal, clock, pcfg), account=acct(equity=equity))


def test_unknown_notional_of_an_open_position_blocks_leverage(journal, clock, pcfg):
    put_position(journal, "DE40", ctx=False)
    assert PORTFOLIO_LEVERAGE in reasons(build(journal, clock, pcfg))


def test_entering_position_notional_comes_from_its_order_context(journal, clock, pcfg):
    journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
    put_order(journal, "DE40")  # 0.005 × 20000 × 1 = 100
    gate = build(journal, clock, pcfg)
    big = dict(size=D("0.0125"), stop_distance=D("1"), margin_rate=D("0.01"))
    assert PORTFOLIO_LEVERAGE not in reasons(gate, cand(**big))
    assert PORTFOLIO_LEVERAGE in reasons(gate, cand(**big, entry_price=D("8000.8")))


def test_entering_position_without_order_context_blocks_leverage(journal, clock, pcfg):
    journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
    put_order(journal, "DE40", ctx=False)
    assert PORTFOLIO_LEVERAGE in reasons(build(journal, clock, pcfg))


# --- PORTFOLIO_MARGIN ------------------------------------------------------------------------------


def test_margin_equal_to_the_usable_funds_passes_and_a_cent_over_blocks(journal, clock, pcfg):
    gate = build(journal, clock, pcfg)
    c = cand(size=D("0.004"), margin_rate=D("1"), stop_distance=D("1"))  # depozyt 0.004 × 8000 = 32
    assert reasons(gate, c, acct(available="40")) == []  # 40 × (1 − 0,20) = 32
    assert reasons(gate, c, acct(available="39.98")) == [PORTFOLIO_MARGIN]  # 31,984


@pytest.mark.parametrize("bad", [None, "account", object()])
def test_missing_account_blocks_leverage_and_margin(journal, clock, pcfg, bad):
    got = reasons(build(journal, clock, pcfg), account=bad)
    assert PORTFOLIO_LEVERAGE in got and PORTFOLIO_MARGIN in got


def test_account_in_another_currency_blocks_leverage_and_margin(journal, clock, pcfg):
    got = reasons(build(journal, clock, pcfg), account=acct(currency="USD"))
    assert got == [PORTFOLIO_LEVERAGE, PORTFOLIO_MARGIN]


# --- PORTFOLIO_PNL_PENDING -------------------------------------------------------------------------


def test_trade_waiting_for_its_eur_amount_blocks_entries_until_settled(journal, clock, pcfg):
    journal.record_trade_pending(trade("-0.40"), "DEAL-9", DAY)
    gate = build(journal, clock, pcfg)
    assert reasons(gate) == [PORTFOLIO_PNL_PENDING]
    assert gate.check_exit().passed
    journal.settle_trade_pnl("DEAL-9", D("-0.37"), source="HISTORY_TRANSACTIONS")
    assert reasons(gate) == []


def test_unreadable_pending_marker_blocks(journal, clock, pcfg):
    journal.set_meta("pnl_pending:DEAL-9", "not-a-day")
    assert PORTFOLIO_PNL_PENDING in reasons(build(journal, clock, pcfg))


# --- PORTFOLIO_FX_UNAVAILABLE i przeliczenie na EUR -------------------------------------------------


def test_usd_candidate_without_fx_blocks_every_amount_check(journal, clock, pcfg):
    got = reasons(build(journal, clock, pcfg, lanes=(DE40, US500)), cand(epic="US500"))
    assert got == [
        PORTFOLIO_OPEN_RISK, PORTFOLIO_DAILY_LOSS, PORTFOLIO_LEVERAGE, PORTFOLIO_MARGIN, PORTFOLIO_FX_UNAVAILABLE,
    ]  # fmt: skip


def test_usd_candidate_with_fx_passes_and_is_converted(journal, clock, pcfg):
    fx = Fx(D("0.9"))
    gate = build(journal, clock, pcfg, lanes=(DE40, US500), fx=fx)
    assert reasons(gate, cand(epic="US500")) == []
    assert {currency for _, currency in fx.calls} == {"USD"}


def test_usd_candidate_risk_is_checked_in_eur(journal, clock, pcfg):
    journal.add_realized_pnl(DAY, D("-1.23"))
    gate = build(journal, clock, pcfg, lanes=(DE40, US500), fx=Fx(D("0.9")))
    assert reasons(gate, cand(epic="US500", size=D("0.01"))) == []  # 0,30 USD × 0,9 = 0,27; 1,23 + 0,27 = 1,50
    gate = build(journal, clock, pcfg, lanes=(DE40, US500), fx=Fx(D("0.91")))
    assert reasons(gate, cand(epic="US500", size=D("0.01"))) == [PORTFOLIO_DAILY_LOSS]  # 0,273


def test_open_usd_position_without_fx_blocks_a_eur_candidate(journal, clock, pcfg):
    put_position(journal, "US500")
    got = reasons(build(journal, clock, pcfg, lanes=(DE40, US500)), cand(epic="DE40"))
    assert PORTFOLIO_FX_UNAVAILABLE in got and PORTFOLIO_OPEN_RISK in got and PORTFOLIO_LEVERAGE in got


def test_currency_from_the_entry_context_wins_over_the_lane(journal, clock, pcfg):
    put_position(journal, "FR40", currency="USD")
    got = reasons(build(journal, clock, pcfg), cand(epic="DE40"))
    assert PORTFOLIO_FX_UNAVAILABLE in got


@pytest.mark.parametrize("rate", [None, D("NaN"), D("-0.9"), D("0"), 0.9, "0.9", D("Infinity")])
def test_unusable_fx_answer_blocks(journal, clock, pcfg, rate):
    got = reasons(build(journal, clock, pcfg, lanes=(DE40, US500), fx=Fx(rate)), cand(epic="US500"))
    assert PORTFOLIO_FX_UNAVAILABLE in got and PORTFOLIO_DAILY_LOSS in got


def test_fx_exception_blocks_and_does_not_raise(journal, clock, pcfg):
    got = reasons(build(journal, clock, pcfg, lanes=(DE40, US500), fx=Fx(raises=True)), cand(epic="US500"))
    assert PORTFOLIO_FX_UNAVAILABLE in got


def test_eur_only_book_never_asks_for_fx(journal, clock, pcfg):
    fx = Fx(raises=True)
    put_position(journal, "DE40")
    assert reasons(build(journal, clock, pcfg, fx=fx)) == []
    assert fx.calls == []


# --- PORTFOLIO_UNMANAGED_EXPOSURE (opcjonalne, pytanie 5) -----------------------------------------


def test_unmanaged_exposure_is_not_checked_without_a_view(journal, clock, pcfg):
    assert reasons(build(journal, clock, pcfg, exposure=None)) == []


def test_unmanaged_exposure_blocks_when_the_view_reports_it(journal, clock, pcfg):
    assert reasons(build(journal, clock, pcfg, exposure=Exposure(["UK100"]))) == [PORTFOLIO_UNMANAGED_EXPOSURE]
    assert reasons(build(journal, clock, pcfg, exposure=Exposure([]))) == []


@pytest.mark.parametrize("view", [Exposure(raises=True), Exposure(None), Exposure("UK100")])
def test_unmanaged_exposure_that_cannot_be_read_blocks(journal, clock, pcfg, view):
    assert PORTFOLIO_UNMANAGED_EXPOSURE in reasons(build(journal, clock, pcfg, exposure=view))


# --- nieoceniona kontrola blokuje ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("unresolved_orders", {PORTFOLIO_MAX_POSITIONS, PORTFOLIO_OPEN_RISK, PORTFOLIO_DAILY_LOSS, PORTFOLIO_LEVERAGE}),
        ("current_position", {PORTFOLIO_MAX_POSITIONS, PORTFOLIO_OPEN_RISK, PORTFOLIO_DAILY_LOSS, PORTFOLIO_LEVERAGE}),
        ("realized_pnl", {PORTFOLIO_DAILY_LOSS}),
        ("realized_pnl_total", {PORTFOLIO_TOTAL_DRAWDOWN}),
        ("pnl_pending_deals", {PORTFOLIO_PNL_PENDING}),
        ("get_meta", {PORTFOLIO_OPEN_RISK, PORTFOLIO_DAILY_LOSS, PORTFOLIO_LEVERAGE}),
    ],
)
def test_journal_read_failure_blocks_with_the_reason_of_the_check(journal, clock, pcfg, monkeypatch, method, expected):
    put_position(journal, "DE40")  # get_meta czyta się tylko dla otwartej pozycji

    def boom(*args, **kwargs):
        raise JournalReadError("database is locked")

    monkeypatch.setattr(journal, method, boom)
    v = build(journal, clock, pcfg).check_entry(cand(), ACCT)
    assert not v.passed
    assert set(v.reasons) == expected


BAD_CANDIDATE = [
    ("size", None), ("size", D(0)), ("size", 0.001), ("stop_distance", None), ("stop_distance", D(0)),
    ("stop_distance", D(-5)), ("value_per_point", None), ("value_per_point", D("NaN")), ("entry_price", None),
    ("margin_rate", None), ("epic", None), ("epic", "UK100"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "value"), BAD_CANDIDATE, ids=[f"{n}={v!r}" for n, v in BAD_CANDIDATE])
def test_candidate_that_cannot_be_evaluated_blocks_and_never_raises(journal, clock, pcfg, name, value):
    v = build(journal, clock, pcfg).check_entry(cand(**{name: value}), ACCT)
    assert not v.passed
    assert set(v.reasons) <= set(REASON_ORDER)


@pytest.mark.parametrize("junk", [None, object(), "candidate", 42])
def test_object_that_is_not_a_candidate_blocks(journal, clock, pcfg, junk):
    v = build(journal, clock, pcfg).check_entry(junk, ACCT)
    assert not v.passed


def test_unexpected_failure_of_the_gate_is_gate_error(journal, clock, pcfg, monkeypatch, caplog):
    gate = build(journal, clock, pcfg)

    def bug(*args, **kwargs):
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(gate, "_check_entry", bug)
    assert gate.check_entry(cand(), ACCT) == Verdict(False, [PORTFOLIO_GATE_ERROR])


def test_reasons_follow_the_table_order_without_duplicates(journal, clock, pcfg):
    journal.add_realized_pnl(EARLIER, D("-12"))
    journal.add_realized_pnl(DAY, D("-2"))
    journal.record_trade_pending(trade("-0.40"), "DEAL-9", DAY)
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1), exposure=Exposure(["UK100"]))
    put_position(journal, "DE40", ctx=False)
    got = reasons(gate, cand(size=D("1")), acct(available="0"))
    assert got == [r for r in REASON_ORDER if r in got] and len(got) == len(set(got))
    assert set(got) == set(REASON_ORDER) - {PORTFOLIO_FX_UNAVAILABLE}


def test_gate_does_not_write_to_the_journal(journal, clock, pcfg):
    put_position(journal, "DE40")
    before = (journal.events(), journal.unresolved_orders(), journal.get_meta("entry_by_deal:DEAL-1"))
    build(journal, clock, pcfg).check_entry(cand(), ACCT)
    assert (journal.events(), journal.unresolved_orders(), journal.get_meta("entry_by_deal:DEAL-1")) == before


# --- równoważność z RiskGate przy jednym paśmie DE40 ------------------------------------------------


def _de40(**over) -> EntryCandidate:
    return cand(epic="DE40", entry_price=D("20000"), **over)


GRID_DAY_PNL = ["0", "-0.50", "-1.20", "-1.47", "-1.48", "-1.50", "-1.51", "2"]
GRID_TOTAL = ["0", "-10.49", "-10.50"]  # plus wynik dnia; −10,50 − 1,50 = −12
GRID_RISK = [("0.001", "30"), ("0.01", "99"), ("0.01", "100"), ("0.01", "101"), ("0.002", "15")]
GRID_ACCOUNT = [("40", "40"), ("10", "40"), ("8", "40"), ("40", "1"), ("0", "40")]
GRID_BOOK = ["empty", "open", "blocked", "entering", "order"]


def _book(journal: Journal, kind: str) -> None:
    if kind == "open":
        put_position(journal, "DE40")
    elif kind == "blocked":
        put_position(journal, "DE40", state=PositionState.BLOCKED, size=None, open_level=None, stop=None, deal=None)
    elif kind == "entering":
        journal.upsert_position(PositionRecord("DE40", PositionState.ENTERING))
        put_order(journal, "DE40")
    elif kind == "order":
        put_order(journal, "DE40", state=OrderState.INTENT)


def test_single_de40_lane_portfolio_adds_no_reason_beyond_the_lane_gate(tmp_path, clock, pcfg, rcfg, strat_cfg):
    """Jedno pasmo DE40, limity z `config/portfolio.toml`: każda blokada portfela jest też blokadą `RiskGate`, a bez
    pozycji kontrole wspólne (strata dnia, obsunięcie, dźwignia, depozyt) dają ten sam werdykt w obu bramkach."""
    pairs = [
        (PORTFOLIO_DAILY_LOSS, DAILY_LOSS), (PORTFOLIO_TOTAL_DRAWDOWN, TOTAL_DRAWDOWN),
        (PORTFOLIO_LEVERAGE, LEVERAGE), (PORTFOLIO_MARGIN, MARGIN),
    ]  # fmt: skip
    checked, seen = 0, set()
    for n, (day_pnl, total, book) in enumerate(itertools.product(GRID_DAY_PNL, GRID_TOTAL, GRID_BOOK)):
        journal = Journal.open(str(tmp_path / f"j{n}.sqlite"))
        try:
            journal.add_realized_pnl(DAY, D(day_pnl))
            journal.add_realized_pnl(EARLIER, D(total))
            _book(journal, book)
            lane = build(journal, clock, pcfg, lanes=(DE40,))
            risk = RiskGate(rcfg, strat_cfg, journal, CAL, clock, None)
            for (size, stop), (equity, available) in itertools.product(GRID_RISK, GRID_ACCOUNT):
                c = _de40(size=D(size), stop_distance=D(stop), margin_rate=D("0.05"))
                a = acct(equity=equity, available=available)
                p = lane.check_entry(c, a)
                r = risk.check_entry(c, a)
                where = f"pnl={day_pnl} total={total} risk={size}x{stop} acct={equity}/{available} book={book}"
                assert p.passed or not r.passed, f"{where}: portfolio {p.reasons}, lane gate passed"
                seen |= set(p.reasons)
                if book != "empty":
                    assert DUPLICATE in r.reasons, where  # każda zajętość DE40 to DUPLICATE bramki pasma
                else:
                    for mine, theirs in pairs:
                        assert (mine in p.reasons) == (theirs in r.reasons), f"{where}: {p.reasons} vs {r.reasons}"
                    assert set(p.reasons) <= {mine for mine, _ in pairs} | {PORTFOLIO_OPEN_RISK}, where
                checked += 1
        finally:
            journal.close()
    # siatka naprawdę dochodzi do granic: każda wspólna kontrola portfela blokuje w którymś punkcie
    assert seen >= {mine for mine, _ in pairs} | {PORTFOLIO_OPEN_RISK, PORTFOLIO_LEVERAGE}
    assert checked == len(GRID_DAY_PNL) * len(GRID_TOTAL) * len(GRID_RISK) * len(GRID_ACCOUNT) * len(GRID_BOOK)


def test_kill_switch_is_left_to_the_lane_gate(journal, clock, pcfg):
    journal.set_kill(KillLevel.HARD, "TEST", T)
    assert reasons(build(journal, clock, pcfg)) == []  # KILL zostaje w RiskGate (spec §4.2)


# --- konfiguracja `config/portfolio.toml` ------------------------------------------------------------


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """Kopia katalogu konfiguracji (pliki v1 i portfela) do modyfikacji w teście."""
    dst = tmp_path / "config"
    dst.mkdir()
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml", PORTFOLIO_FILE):
        shutil.copyfile(CONFIG / name, dst / name)
    return dst


def set_key(path: Path, key: str, toml_literal: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines) if re.match(rf"\s*{key}\s*=", line)]
    assert len(hits) == 1, f"{key} not found exactly once in {path.name}"
    lines[hits[0]] = f"{key} = {toml_literal}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def drop_key(path: Path, key: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(rf"\s*{key}\s*=", ln)]
    assert len(kept) == len(lines) - 1
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def test_repository_portfolio_values_are_the_conservative_proposal(pcfg, rcfg):
    assert pcfg == PortfolioConfig(
        max_open_positions=2,
        max_open_risk=D("1.50"),
        max_daily_loss=D("1.50"),
        max_total_drawdown=D("12"),
        max_effective_leverage=D("5"),
        virtual_capital=D("40"),
        margin_reserve_fraction=D("0.20"),
        unknown_position_risk=D("1.50"),
        fx_quote_epic="EURUSD",
        fx_quote_max_age_s=60,
    )
    # budżet ryzyka portfela nie rośnie z liczbą pasm: wspólne limity równe v1
    assert pcfg.max_daily_loss == rcfg.max_daily_loss
    assert pcfg.max_total_drawdown == rcfg.max_total_drawdown
    assert pcfg.max_open_risk <= rcfg.max_daily_loss


def test_portfolio_file_is_marked_as_a_proposal_for_the_owner():
    text = (CONFIG / PORTFOLIO_FILE).read_text(encoding="utf-8")
    assert "PROPOZYCJA, do zatwierdzenia przez właściciela" in text


def test_portfolio_config_is_immutable(pcfg):
    with pytest.raises(dataclasses.FrozenInstanceError):
        pcfg.max_daily_loss = D("100")  # type: ignore[misc]


def test_portfolio_file_is_outside_the_v1_fingerprint(cfg_dir):
    assert PORTFOLIO_FILE not in prereg.CONFIG_FILES
    before = prereg.config_fingerprint(cfg_dir)
    set_key(cfg_dir / PORTFOLIO_FILE, "max_open_positions", "1")
    assert prereg.config_fingerprint(cfg_dir) == before
    (cfg_dir / PORTFOLIO_FILE).unlink()
    assert prereg.config_fingerprint(cfg_dir) == before
    assert prereg.config_fingerprint(cfg_dir) == prereg.config_fingerprint(CONFIG)


LENIENT = [
    ("max_daily_loss", '"1.51"', "max_daily_loss"),
    ("max_total_drawdown", '"12.01"', "max_total_drawdown"),
    ("max_open_risk", '"1.51"', "max_daily_loss"),
    ("max_effective_leverage", '"5.01"', "max_effective_leverage"),
    ("virtual_capital", '"40.01"', "virtual_capital"),
    ("margin_reserve_fraction", '"0.19"', "margin_reserve_fraction"),
    ("unknown_position_risk", '"0.99"', "max_loss_per_trade"),
]


@pytest.mark.parametrize(("key", "literal", "risk_key"), LENIENT, ids=[k for k, _, _ in LENIENT])
def test_value_more_lenient_than_risk_v1_is_rejected(cfg_dir, key, literal, risk_key):
    set_key(cfg_dir / PORTFOLIO_FILE, key, literal)
    with pytest.raises(ValueError) as err:
        load_portfolio(cfg_dir)
    text = str(err.value)
    assert PORTFOLIO_FILE in text and key in text and "risk_v1.toml" in text and risk_key in text


STRICTER_OR_EQUAL = [
    ("max_daily_loss", '"1.49"'), ("max_daily_loss", '"1.50"'),
    ("max_total_drawdown", '"11.99"'), ("max_open_risk", '"1.00"'),
    ("max_effective_leverage", '"4.99"'), ("virtual_capital", '"39.99"'),
    ("margin_reserve_fraction", '"0.21"'), ("unknown_position_risk", '"1.00"'), ("unknown_position_risk", '"2"'),
    ("max_open_positions", "1"),
]  # fmt: skip


@pytest.mark.parametrize(("key", "literal"), STRICTER_OR_EQUAL, ids=[f"{k}={v}" for k, v in STRICTER_OR_EQUAL])
def test_value_equal_or_stricter_than_risk_v1_is_accepted(cfg_dir, key, literal):
    set_key(cfg_dir / PORTFOLIO_FILE, key, literal)
    cfg = load_portfolio(cfg_dir)
    assert getattr(cfg, key) == (int(literal) if '"' not in literal else D(literal.strip('"')))


def test_consistency_can_be_checked_against_a_given_risk_config(cfg_dir, rcfg):
    strict = dataclasses.replace(rcfg, max_daily_loss=D("1.00"))
    with pytest.raises(ValueError, match="max_daily_loss"):
        load_portfolio(cfg_dir, strict)
    assert load_portfolio(cfg_dir, rcfg).max_daily_loss == D("1.50")


def test_missing_risk_v1_is_an_error(cfg_dir):
    (cfg_dir / "risk_v1.toml").unlink()
    with pytest.raises(FileNotFoundError):
        load_portfolio(cfg_dir)


def test_missing_portfolio_file_is_an_error(cfg_dir):
    (cfg_dir / PORTFOLIO_FILE).unlink()
    with pytest.raises(FileNotFoundError):
        load_portfolio(cfg_dir)


ALL_KEYS = [f.name for f in dataclasses.fields(PortfolioConfig)]


@pytest.mark.parametrize("key", ALL_KEYS)
def test_missing_key_is_rejected_with_file_and_key(cfg_dir, key):
    drop_key(cfg_dir / PORTFOLIO_FILE, key)
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: missing key\(s\): {key}"):
        load_portfolio(cfg_dir)


def test_unknown_key_is_rejected(cfg_dir):
    with (cfg_dir / PORTFOLIO_FILE).open("a", encoding="utf-8") as fh:
        fh.write('max_dialy_loss = "1.00"\n')
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: unknown key\(s\): max_dialy_loss"):
        load_portfolio(cfg_dir)


DECIMAL_KEYS = [
    "max_open_risk", "max_daily_loss", "max_total_drawdown", "max_effective_leverage", "virtual_capital",
    "margin_reserve_fraction", "unknown_position_risk",
]  # fmt: skip


@pytest.mark.parametrize("key", DECIMAL_KEYS)
@pytest.mark.parametrize("literal", ["1.0", "1", '"1e0"', '" 1"', '"-1"', "true"])
def test_decimal_keys_accept_only_plain_decimal_strings(cfg_dir, key, literal):
    set_key(cfg_dir / PORTFOLIO_FILE, key, literal)
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: {key}"):
        load_portfolio(cfg_dir)


@pytest.mark.parametrize("key", ["max_open_risk", "max_daily_loss", "max_total_drawdown", "unknown_position_risk"])
def test_amounts_must_be_positive(cfg_dir, key):
    set_key(cfg_dir / PORTFOLIO_FILE, key, '"0"')
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: {key}"):
        load_portfolio(cfg_dir)


@pytest.mark.parametrize("key", ["max_open_positions", "fx_quote_max_age_s"])
@pytest.mark.parametrize("literal", ["0", "-1", "1.0", '"2"', "true"])
def test_integer_keys_must_be_positive_integers(cfg_dir, key, literal):
    set_key(cfg_dir / PORTFOLIO_FILE, key, literal)
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: {key}"):
        load_portfolio(cfg_dir)


@pytest.mark.parametrize("literal", ['""', '"  "', "1"])
def test_fx_quote_epic_must_be_a_non_empty_string(cfg_dir, literal):
    set_key(cfg_dir / PORTFOLIO_FILE, "fx_quote_epic", literal)
    with pytest.raises(ValueError, match=rf"{PORTFOLIO_FILE}: fx_quote_epic"):
        load_portfolio(cfg_dir)


def test_malformed_toml_names_the_file(cfg_dir):
    (cfg_dir / PORTFOLIO_FILE).write_text("max_open_positions = = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match=PORTFOLIO_FILE):
        load_portfolio(cfg_dir)


def test_load_config_ignores_the_portfolio_file(cfg_dir):
    (cfg_dir / PORTFOLIO_FILE).write_text("garbage = = \n", encoding="utf-8")
    assert load_config(cfg_dir) == load_config(CONFIG)


# --- zadanie 12: zajętość z wszystkich zapisów pozycji dziennika (Journal.positions) ------------------------------


def test_a_journal_position_on_an_instrument_without_a_lane_occupies_and_carries_the_unknown_risk(journal, clock, pcfg):
    journal.upsert_position(PositionRecord("US100", PositionState.BLOCKED))  # bez pasma, bez zlecenia
    gate = build(journal, clock, dataclasses.replace(pcfg, max_open_positions=1))
    got = reasons(gate)
    assert PORTFOLIO_MAX_POSITIONS in got and PORTFOLIO_OPEN_RISK in got  # ryzyko nieznane = unknown_position_risk
