"""Bramka portfelowa w wykonawcy (zadanie 12, projekt wielu pasm §4.1): `Executor._prepare` woła `PortfolioGate` PO
bramce pasma; werdykt to suma powodów obu bramek, a pole `portfolio_reasons` w `gate_block` pojawia się tylko, gdy
bramka portfelowa coś dodała (bajty dziennika DE40 bez bramki portfelowej i z nią, gdy nic nie dodaje, bez zmian).

Kandydat bramki portfelowej ma wartość punktu w WALUCIE INSTRUMENTU (bramka sama przelicza ją przez `FxView`), a nie
przeliczoną przez wykonawcę: inaczej kwota USD byłaby przeliczona dwa razy.
"""

import json
from decimal import Decimal

import pytest

from gwstock.config import load_portfolio
from gwstock.costs import CostModel
from gwstock.execution import Executor
from gwstock.portfolio import (
    PORTFOLIO_MAX_POSITIONS,
    PORTFOLIO_UNMANAGED_EXPOSURE,
    PortfolioGate,
    PortfolioLane,
)
from gwstock.risk import Verdict
from gwstock.types import Side
from tests.fault.test_execution_lanes import (  # noqa: F401  (fixtures)
    DAY,
    US_RULES,
    StubFx,
    clock,
    de,
    de_lane,
    fake,
    fx,
    hold,
    lane_build,
    memory,
    plan,
    us,
    us_gate,
    us_lane,
    us_quote,
)

D = Decimal


class Exposure:
    def __init__(self, epics=()) -> None:
        self.epics = epics

    def unmanaged_epics(self):
        return self.epics


class SpyGate:
    """Bramka portfelowa-szpieg: zapisuje kandydatów i zwraca zadany werdykt."""

    def __init__(self, reasons=()) -> None:
        self.reasons = list(reasons)
        self.seen = []

    def check_entry(self, c, account):
        self.seen.append(c)
        return Verdict(not self.reasons, list(self.reasons))


def blocks(journal):
    return [json.loads(e.payload) for e in journal.events("gate_block")]


@pytest.fixture
def build(mgr, fake, journal, kill, cost_cfg, clock, risk_cfg, notifier, memory):
    def _build(lane, gate, portfolio, *, fx=None, live=True):
        return Executor.for_lane(
            mgr, fake, journal, gate, kill, CostModel(cost_cfg), clock, lane, risk_cfg, notifier, live=live,
            memory=memory, fx=fx, portfolio=portfolio,
        )  # fmt: skip

    return _build


@pytest.fixture
def portfolio(journal, calendar, clock):
    cfg = load_portfolio("config")
    lanes = [PortfolioLane("DE40", "EUR", D("1.00")), PortfolioLane("US500", "USD", D("1.00"))]
    return lambda **kw: PortfolioGate(cfg, lanes, journal, calendar, clock, fx=StubFx(), **kw)


async def test_portfolio_reasons_join_the_lane_reasons_in_one_gate_block(build, de_lane, gate, journal, fake,
                                                                         signal, rules, portfolio):
    ex = build(de_lane, gate, portfolio(exposure=Exposure({"US100"})))
    assert await ex.enter(signal, rules, DAY) is None
    assert "open_position" not in fake.calls
    (block,) = blocks(journal)
    assert block["reasons"] == [PORTFOLIO_UNMANAGED_EXPOSURE]
    assert block["portfolio_reasons"] == [PORTFOLIO_UNMANAGED_EXPOSURE]


async def test_no_portfolio_reasons_field_when_the_portfolio_gate_adds_nothing(build, de_lane, gate, journal,
                                                                               signal, rules, portfolio, clock):
    ex = build(de_lane, gate, portfolio())
    clock.advance(3600)  # poza oknem DE40: blokuje tylko bramka pasma
    assert await ex.enter(signal, rules, DAY) is None
    (block,) = blocks(journal)
    assert "portfolio_reasons" not in block and block["reasons"] == ["OUTSIDE_WINDOW", "STALE_DATA"]


async def test_both_gates_reasons_are_listed_lane_first(build, de_lane, gate, journal, signal, rules, clock):
    ex = build(de_lane, gate, SpyGate([PORTFOLIO_MAX_POSITIONS]))
    clock.advance(3600)
    assert await ex.enter(signal, rules, DAY) is None
    (block,) = blocks(journal)
    assert block["reasons"] == ["OUTSIDE_WINDOW", "STALE_DATA", PORTFOLIO_MAX_POSITIONS]
    assert block["portfolio_reasons"] == [PORTFOLIO_MAX_POSITIONS]


async def test_a_passing_portfolio_gate_lets_the_entry_through(build, de_lane, gate, fake, signal, rules,
                                                               portfolio):
    ex = build(de_lane, gate, portfolio())
    rec = await ex.enter(signal, rules, DAY)
    assert rec is not None and "open_position" in fake.calls


async def test_the_portfolio_candidate_carries_the_instrument_currency_value_per_point(build, us_lane, us_gate,
                                                                                       fx, plan, fake):
    spy = SpyGate([PORTFOLIO_MAX_POSITIONS])
    ex = build(us_lane, us_gate, spy, fx=fx)
    await ex.enter(plan, US_RULES, DAY)
    (c,) = spy.seen
    assert c.epic == "US500" and c.value_per_point == US_RULES.value_per_point  # USD, nie przeliczone
    assert "open_position" not in fake.calls


async def test_smoke_entries_also_pass_the_portfolio_gate(build, de_lane, gate, journal, fake, signal, rules):
    ex = build(de_lane, gate, SpyGate([PORTFOLIO_MAX_POSITIONS]))
    assert await ex.enter(signal, rules, DAY, smoke=True) is None
    (block,) = blocks(journal)
    assert block["smoke"] is True and block["portfolio_reasons"] == [PORTFOLIO_MAX_POSITIONS]


async def test_the_portfolio_gate_never_blocks_an_exit(build, de_lane, gate, fake, journal, rules):
    ex = build(de_lane, gate, SpyGate([PORTFOLIO_MAX_POSITIONS]))
    pos = hold(fake, journal, "DE40", "DEAL-9", Side.BUY, D("0.001"), D("25782.3"), D("25752.3"))
    closed = await ex.flatten(pos, "FORCED")
    assert closed is not None and "close_position" in fake.calls


async def test_a_raising_portfolio_gate_blocks_the_entry(build, de_lane, gate, journal, fake, signal, rules):
    class Boom:
        def check_entry(self, c, account):
            raise RuntimeError("bug")

    ex = build(de_lane, gate, Boom())
    assert await ex.enter(signal, rules, DAY) is None
    assert "open_position" not in fake.calls
    (block,) = blocks(journal)
    assert block["portfolio_reasons"] == ["PORTFOLIO_GATE_ERROR"]


async def test_dry_run_records_the_decision_only_after_both_gates_pass(build, de_lane, gate, journal, signal,
                                                                       rules):
    ex = build(de_lane, gate, SpyGate([PORTFOLIO_MAX_POSITIONS]), live=False)
    await ex.enter(signal, rules, DAY)
    assert blocks(journal) and not journal.events("decision")
    ex = build(de_lane, gate, SpyGate(), live=False)
    await ex.enter(signal, rules, DAY)
    assert journal.events("decision")


def test_the_gate_error_code_matches_the_portfolio_module():
    from gwstock import execution, portfolio as portfolio_mod

    assert execution.PORTFOLIO_GATE_ERROR == portfolio_mod.PORTFOLIO_GATE_ERROR
