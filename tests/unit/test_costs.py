"""Testy modelu kosztów (spec §10): liczby policzone ręcznie, zaokrąglenia, walidacja i spójność sum księgi."""

from __future__ import annotations

import dataclasses
import itertools
import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from gwstock.config import CostConfig, load_config
from gwstock.costs import FINANCING_NOT_MODELLED, CostBreakdown, CostModel, TradeFacts
from gwstock.types import Side

D = Decimal

CFG = CostConfig(
    entry_slippage_points=D("0.5"),
    stop_exit_extra_slippage_points=D("1.0"),
    fx_markup=D("0.004"),
    apply_fx_markup=False,
)
CFG_FX = dataclasses.replace(CFG, apply_fx_markup=True)
model = CostModel(CFG)
model_fx = CostModel(CFG_FX)


def facts(**kw):
    base = dict(side=Side.BUY, size=D("0.2"), value_per_point=D("1"),
                mid_entry=D("25000.0"), mid_exit=D("25030.0"),
                entry_fill=D("25000.9"), exit_fill=D("25029.1"),
                spread_entry=D("1.8"), spread_exit=D("1.8"),
                exit_reason="FORCED", nights_held=0)  # fmt: skip
    return TradeFacts(**{**base, **kw})


def sell_facts(**kw):
    """SELL: size 0,5 × vpp 2 = 1 € na punkt; wejście po bid (mid − 0,8), wyjście po ask (mid + 1,0)."""
    base = dict(side=Side.SELL, size=D("0.5"), value_per_point=D("2"),
                mid_entry=D("25000.0"), mid_exit=D("24980.0"),
                entry_fill=D("24999.2"), exit_fill=D("24981.0"),
                spread_entry=D("1.6"), spread_exit=D("2.0"),
                exit_reason="FORCED", nights_held=0)  # fmt: skip
    return TradeFacts(**{**base, **kw})


def parts(b: CostBreakdown):
    return b.gross_pnl, b.spread_cost, b.slippage_penalty, b.financing, b.fees, b.net_pnl


# --- liczby z planu (policzone ręcznie) ---------------------------------------------------------


def test_time_exit_net():
    b = model.compute(facts())
    assert b.gross_pnl == D("6.00") and b.spread_cost == D("0.36")
    assert b.slippage_penalty == D("0.10") and b.net_pnl == D("5.54")
    assert b.financing == D("0.00") and b.fees == D("0.00")
    assert b.notes == ()


def test_stop_exit_adds_extra_slippage():
    b = model.compute(facts(exit_reason="STOP"))
    assert b.slippage_penalty == D("0.30")  # (0.5 + 1.0) × 0.2
    assert b.net_pnl == D("5.34")  # 6.00 − 0.36 − 0.30


@pytest.mark.parametrize("reason", ["MANUAL", "FORCED"])
def test_only_stop_gets_extra_slippage(reason):
    assert model.compute(facts(exit_reason=reason)).slippage_penalty == D("0.10")


def test_spread_cost_never_below_quoted_spread_when_fills_are_at_mid():
    b = model.compute(facts(entry_fill=D("25000.0"), exit_fill=D("25030.0")))
    assert b.spread_cost == D("0.36")  # realizowany 0, modelowy (0.9 + 0.9) × 0.2
    assert b.net_pnl == D("5.54")


def test_shipped_config_gives_the_plan_numbers():
    _, _, cost_cfg = load_config(Path("config"))
    b = CostModel(cost_cfg).compute(facts())
    assert parts(b) == (D("6.00"), D("0.36"), D("0.10"), D("0.00"), D("0.00"), D("5.54"))
    assert CostModel(cost_cfg).compute(facts(exit_reason="STOP")).slippage_penalty == D("0.30")


# --- strona SELL ---------------------------------------------------------------------------------


def test_short_trade_sign():
    # gross = (25000.0 − 24980.0) × 1 = 20.00; spread: realizowany (0.8 + 1.0) = modelowy (0.8 + 1.0) = 1.80
    # poślizg 0.5 × 1 = 0.50; net = 20.00 − 1.80 − 0.50 = 17.70
    b = model.compute(sell_facts())
    assert parts(b) == (D("20.00"), D("1.80"), D("0.50"), D("0.00"), D("0.00"), D("17.70"))


def test_short_trade_loss_and_stop():
    # gross = (25000.0 − 25010.0) × 1 = −10.00; exit po ask (mid + 1.0): realizowany 0.8 + 1.0 = 1.80
    b = model.compute(sell_facts(mid_exit=D("25010.0"), exit_fill=D("25011.0"), exit_reason="STOP"))
    assert parts(b) == (D("-10.00"), D("1.80"), D("1.50"), D("0.00"), D("0.00"), D("-13.30"))


def test_long_trade_loss():
    # BUY: gross = (24970.0 − 25000.0) × 0.2 = −6.00; wyjście po bid (mid − 0.9)
    b = model.compute(facts(mid_exit=D("24970.0"), exit_fill=D("24969.1")))
    assert parts(b) == (D("-6.00"), D("0.36"), D("0.10"), D("0.00"), D("0.00"), D("-6.46"))


# --- spread_cost = max(zrealizowany, modelowy) ---------------------------------------------------


def test_realized_spread_above_model_when_fills_are_bad():
    # wejście 2.0 ponad mid, wyjście 3.0 pod mid: (2.0 + 3.0) × 0.2 = 1.00 > model 0.36
    b = model.compute(facts(entry_fill=D("25002.0"), exit_fill=D("25027.0")))
    assert b.spread_cost == D("1.00")
    assert b.net_pnl == D("4.90")  # 6.00 − 1.00 − 0.10


def test_realized_spread_above_model_for_sell():
    # SELL: wejście 2.0 pod mid, wyjście 3.0 ponad mid: (2.0 + 3.0) × 1 = 5.00
    b = model.compute(sell_facts(entry_fill=D("24998.0"), exit_fill=D("24983.0")))
    assert b.spread_cost == D("5.00")
    assert b.net_pnl == D("14.50")  # 20.00 − 5.00 − 0.50


def test_negative_realized_spread_falls_back_to_model():
    # poprawa ceny po obu stronach: (−1.0) + (−1.0) = −2.0 × 0.2 = −0.40 → max(−0.40, 0.36) = 0.36
    b = model.compute(facts(entry_fill=D("24999.0"), exit_fill=D("25031.0")))
    assert b.spread_cost == D("0.36")
    assert b.net_pnl == D("5.54")


def test_negative_realized_spread_falls_back_to_model_for_sell():
    b = model.compute(sell_facts(entry_fill=D("25001.0"), exit_fill=D("24979.0")))
    assert b.spread_cost == D("1.80")


def test_realized_slightly_below_model_uses_model():
    # (0.2 + 0.1) × 0.2 = 0.06 < 0.36
    b = model.compute(facts(entry_fill=D("25000.2"), exit_fill=D("25029.9")))
    assert b.spread_cost == D("0.36")


def test_model_spread_uses_both_quoted_spreads():
    # (3.0/2 + 1.0/2) × 0.2 = 0.40 przy wypełnieniach po mid
    b = model.compute(facts(entry_fill=D("25000.0"), exit_fill=D("25030.0"),
                            spread_entry=D("3.0"), spread_exit=D("1.0")))  # fmt: skip
    assert b.spread_cost == D("0.40")


def test_zero_spreads_and_mid_fills_give_zero_spread_cost():
    b = model.compute(facts(entry_fill=D("25000.0"), exit_fill=D("25030.0"),
                            spread_entry=D("0"), spread_exit=D("0")))  # fmt: skip
    assert b.spread_cost == D("0.00")


def test_value_per_point_scales_everything():
    # vpp 2.5: gross 30 × 0.2 × 2.5 = 15.00; spread 1.8 × 0.5 = 0.90; poślizg 0.5 × 0.5 = 0.25
    b = model.compute(facts(value_per_point=D("2.5")))
    assert parts(b) == (D("15.00"), D("0.90"), D("0.25"), D("0.00"), D("0.00"), D("13.85"))


# --- zaokrąglanie do 0,01 (ROUND_HALF_UP, od zera) -----------------------------------------------


def test_spread_cost_rounds_half_up():
    # model: (5.0/2 + 5.0/2) × 0.001 = 0.005 → 0.01 (zaokrąglenie do parzystej dałoby 0.00)
    b = model.compute(facts(size=D("0.001"), entry_fill=D("25000.0"), exit_fill=D("25030.0"),
                            spread_entry=D("5.0"), spread_exit=D("5.0")))  # fmt: skip
    assert b.spread_cost == D("0.01")


@pytest.mark.parametrize(
    ("spread", "expected"),
    [("4.99", "0.00"), ("5.0", "0.01"), ("5.01", "0.01"), ("15.0", "0.02"), ("25.0", "0.03")],
)
def test_spread_cost_rounding_boundaries(spread, expected):
    # model: (s/2 + s/2) × 0.001 = s/1000: 0.00499, 0.005, 0.00501, 0.015, 0.025
    b = model.compute(facts(size=D("0.001"), entry_fill=D("25000.0"), exit_fill=D("25030.0"),
                            spread_entry=D(spread), spread_exit=D(spread)))  # fmt: skip
    assert b.spread_cost == D(expected)


def test_gross_rounds_half_up_and_away_from_zero():
    # 25.0 pkt × 0.001 = 0.025 → 0.03; strata −0.025 → −0.03
    win = model.compute(facts(size=D("0.001"), mid_exit=D("25025.0")))
    loss = model.compute(facts(size=D("0.001"), mid_exit=D("24975.0")))
    assert win.gross_pnl == D("0.03") and loss.gross_pnl == D("-0.03")


def test_slippage_rounds_half_up():
    # 0.5 × 0.01 = 0.005 → 0.01; (0.5 + 1.0) × 0.01 = 0.015 → 0.02
    assert model.compute(facts(size=D("0.01"))).slippage_penalty == D("0.01")
    assert model.compute(facts(size=D("0.01"), exit_reason="STOP")).slippage_penalty == D("0.02")


def test_tiny_negative_gross_has_no_negative_zero():
    # −0.1 pkt × 0.001 = −0.0001 → "0.00", nie "-0.00" (wiersz księgi/CSV)
    zero_cfg = dataclasses.replace(CFG, entry_slippage_points=D("0"), stop_exit_extra_slippage_points=D("0"))
    t = facts(size=D("0.001"), mid_exit=D("24999.9"), entry_fill=D("25000.0"), exit_fill=D("24999.9"),
              spread_entry=D("0"), spread_exit=D("0"))  # fmt: skip
    for cfg in (zero_cfg, dataclasses.replace(zero_cfg, apply_fx_markup=True)):
        b = CostModel(cfg).compute(t)
        assert [str(v) for v in parts(b)] == ["0.00"] * 6


def test_components_have_two_decimal_places():
    b = model.compute(facts(size=D("0.037"), mid_exit=D("25012.3456")))
    for value in parts(b):
        assert value.as_tuple().exponent == -2


# --- markup walutowy -----------------------------------------------------------------------------


def test_fx_markup_off_by_default_in_shipped_config():
    _, _, cost_cfg = load_config(Path("config"))
    assert cost_cfg.apply_fx_markup is False
    assert CostModel(cost_cfg).compute(facts()).fees == D("0.00")


def test_fx_markup_off_gives_zero_fees_even_with_rate_set():
    assert model.compute(facts()).fees == D("0.00")


def test_fx_markup_on_is_rate_times_absolute_gross():
    # 0.004 × 6.00 = 0.024 → 0.02; net = 6.00 − 0.36 − 0.10 − 0.02 = 5.52
    b = model_fx.compute(facts())
    assert b.fees == D("0.02") and b.net_pnl == D("5.52")


def test_fx_markup_on_a_loss_is_still_a_cost():
    # gross −6.00 → fees 0.02 (|gross|), net = −6.00 − 0.36 − 0.10 − 0.02 = −6.48
    b = model_fx.compute(facts(mid_exit=D("24970.0"), exit_fill=D("24969.1")))
    assert b.gross_pnl == D("-6.00") and b.fees == D("0.02") and b.net_pnl == D("-6.48")


def test_fx_markup_rounds_half_up():
    # gross 25.0 × 0.05 = 1.25; fees 0.004 × 1.25 = 0.005 → 0.01; poślizg 0.025 → 0.03; spread 0.09
    b = model_fx.compute(facts(size=D("0.05"), mid_exit=D("25025.0"), entry_fill=D("25000.0"),
                               exit_fill=D("25025.0")))  # fmt: skip
    assert parts(b) == (D("1.25"), D("0.09"), D("0.03"), D("0.00"), D("0.01"), D("1.12"))


def test_fx_markup_is_computed_from_unrounded_gross():
    # surowy gross 1.2451 (→ 1.25); 0.004 × 1.2451 = 0.0049804 → 0.00 (z zaokrąglonego 1.25 wyszłoby 0.01)
    b = model_fx.compute(facts(size=D("1"), mid_exit=D("25001.2451"), entry_fill=D("25000.0"),
                               exit_fill=D("25001.2451"), spread_entry=D("0"), spread_exit=D("0")))  # fmt: skip
    assert b.gross_pnl == D("1.25") and b.fees == D("0.00")
    assert b.net_pnl == D("0.75")  # 1.25 − 0 − 0.50 − 0 − 0


def test_fx_markup_for_sell_uses_absolute_gross():
    b = model_fx.compute(sell_facts())  # gross 20.00 → fees 0.08
    assert b.fees == D("0.08") and b.net_pnl == D("17.62")


# --- finansowanie i notatki ----------------------------------------------------------------------


def test_financing_is_zero_without_nights_and_fx_markup_off_by_default():
    b = model.compute(facts(nights_held=0))
    assert b.financing == D("0.00") and b.fees == D("0.00") and b.notes == ()


@pytest.mark.parametrize("nights", [1, 2, 30])
def test_nights_held_is_not_modelled_but_flagged_and_never_raises(nights):
    b = model.compute(facts(nights_held=nights))
    assert b.financing == D("0.00")
    assert b.notes == (FINANCING_NOT_MODELLED,)
    assert "FINANCING_NOT_MODELLED" in b.notes
    assert b.net_pnl == D("5.54")  # finansowanie nie wpływa na wynik


def test_nights_held_and_fx_markup_together():
    b = model_fx.compute(facts(nights_held=1))
    assert b.notes == ("FINANCING_NOT_MODELLED",) and b.fees == D("0.02") and b.net_pnl == D("5.52")


# --- walidacja wejścia: ValueError z nazwą pola --------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", D("0")), ("size", D("-0.2")), ("size", 0.2), ("size", 1), ("size", "0.2"), ("size", None),
        ("size", D("NaN")), ("size", D("Infinity")),
        ("value_per_point", D("0")), ("value_per_point", D("-1")), ("value_per_point", 1.0),
        ("value_per_point", 1), ("value_per_point", D("NaN")),
        ("mid_entry", 25000.0), ("mid_entry", 25000), ("mid_entry", D("NaN")), ("mid_entry", D("Infinity")),
        ("mid_exit", 25030.0), ("mid_exit", D("-Infinity")), ("mid_exit", None),
        ("entry_fill", 25000.9), ("entry_fill", D("sNaN")),
        ("exit_fill", 25029.1), ("exit_fill", "25029.1"),
        ("spread_entry", D("-0.1")), ("spread_entry", 1.8), ("spread_entry", D("NaN")),
        ("spread_exit", D("-0.1")), ("spread_exit", 1.8), ("spread_exit", D("Infinity")),
        ("exit_reason", "TAKE_PROFIT"), ("exit_reason", "stop"), ("exit_reason", ""), ("exit_reason", None),
        ("exit_reason", 1), ("exit_reason", ["STOP"]),
        ("nights_held", -1), ("nights_held", 1.0), ("nights_held", "0"), ("nights_held", True),
        ("nights_held", D("1")), ("nights_held", None),
        ("side", "BUY"), ("side", None),
    ],
)  # fmt: skip
def test_invalid_facts_raise_value_error_naming_the_field(field, value):
    with pytest.raises(ValueError, match=field):
        facts(**{field: value})


def test_compute_revalidates_facts_that_bypassed_construction():
    f = facts()
    object.__setattr__(f, "size", D("0"))
    with pytest.raises(ValueError, match="size"):
        model.compute(f)
    g = facts()
    object.__setattr__(g, "exit_reason", "TAKE_PROFIT")
    with pytest.raises(ValueError, match="exit_reason"):
        model.compute(g)


def test_compute_rejects_something_that_is_not_trade_facts():
    with pytest.raises(ValueError, match="TradeFacts"):
        model.compute({"size": D("0.2")})  # type: ignore[arg-type]


def test_absurd_magnitude_is_a_value_error_not_a_decimal_exception():
    with pytest.raises(ValueError, match="TradeFacts"):
        model.compute(facts(size=D("1E+400")))


def test_large_but_plausible_values_do_not_raise():
    b = model.compute(facts(size=D("1000000"), mid_exit=D("30000.123456789")))
    assert b.gross_pnl == D("5000123456.79")
    assert b.net_pnl == b.gross_pnl - b.spread_cost - b.slippage_penalty - b.financing - b.fees


def test_zero_price_move_is_valid():
    b = model.compute(facts(mid_exit=D("25000.0"), entry_fill=D("25000.0"), exit_fill=D("25000.0"),
                            spread_entry=D("0"), spread_exit=D("0")))  # fmt: skip
    assert parts(b) == (D("0.00"), D("0.00"), D("0.10"), D("0.00"), D("0.00"), D("-0.10"))


@pytest.mark.parametrize(
    "bad",
    [
        dict(entry_slippage_points=D("-0.1")),
        dict(entry_slippage_points=0.5),
        dict(stop_exit_extra_slippage_points=D("-1")),
        dict(stop_exit_extra_slippage_points=D("NaN")),
        dict(fx_markup=D("-0.004")),
        dict(fx_markup=0.004),
        dict(apply_fx_markup=1),
        dict(apply_fx_markup="false"),
    ],
)
def test_invalid_cost_config_is_rejected_at_construction(bad):
    name = next(iter(bad))
    with pytest.raises(ValueError, match=name):
        CostModel(dataclasses.replace(CFG, **bad))


def test_cost_model_requires_a_cost_config():
    with pytest.raises(ValueError, match="CostConfig"):
        CostModel({"entry_slippage_points": D("0.5")})  # type: ignore[arg-type]


# --- niezmienność i typy wyniku ------------------------------------------------------------------


def test_trade_facts_and_breakdown_are_frozen():
    f = facts()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.size = D("1")  # type: ignore[misc]
    b = model.compute(f)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.net_pnl = D("0")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.notes = ()  # type: ignore[misc]


def test_result_fields_are_decimal_and_notes_is_a_tuple_of_str():
    b = model.compute(facts(nights_held=1))
    for value in parts(b):
        assert type(value) is Decimal
    assert isinstance(b.notes, tuple) and all(isinstance(n, str) for n in b.notes)


def test_breakdown_field_order_is_the_ledger_order():
    assert [f.name for f in dataclasses.fields(CostBreakdown)] == [
        "gross_pnl", "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl", "notes",
    ]  # fmt: skip
    assert [f.name for f in dataclasses.fields(TradeFacts)] == [
        "side", "size", "value_per_point", "mid_entry", "mid_exit", "entry_fill", "exit_fill",
        "spread_entry", "spread_exit", "exit_reason", "nights_held",
    ]  # fmt: skip


def test_breakdown_notes_default_to_empty_tuple():
    b = CostBreakdown(D("1.00"), D("0.10"), D("0.10"), D("0.00"), D("0.00"), D("0.80"))
    assert b.notes == ()


def test_breakdown_that_does_not_add_up_is_rejected():
    with pytest.raises(ValueError, match="net_pnl"):
        CostBreakdown(D("1.00"), D("0.10"), D("0.10"), D("0.00"), D("0.00"), D("0.81"))


def test_breakdown_rejects_non_decimal_and_bad_notes():
    with pytest.raises(ValueError, match="gross_pnl"):
        CostBreakdown(1.0, D("0"), D("0"), D("0"), D("0"), 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="notes"):
        CostBreakdown(D("1"), D("0"), D("0"), D("0"), D("0"), D("1"), notes=["X"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fees"):
        CostBreakdown(D("1"), D("0"), D("0"), D("0"), D("NaN"), D("1"))


# --- przekrojowo: sumy księgi i niezależny wzorzec ----------------------------------------------


def _round_half_up_cents(x: Fraction) -> Decimal:
    scaled = x * 100
    sign = -1 if scaled < 0 else 1
    cents = math.floor(abs(scaled) + Fraction(1, 2))
    return Decimal(sign * cents).scaleb(-2)


def _oracle(cfg: CostConfig, t: TradeFacts) -> tuple[Decimal, ...]:
    """Ten sam model w dokładnej arytmetyce ułamków; zaokrągla dopiero na końcu, osobno każdy składnik."""
    f = {k: Fraction(v) for k, v in dataclasses.asdict(t).items()
         if isinstance(v, Decimal)}  # fmt: skip
    k = f["size"] * f["value_per_point"]
    direction = 1 if t.side is Side.BUY else -1
    gross = direction * (f["mid_exit"] - f["mid_entry"]) * k
    realized = direction * ((f["entry_fill"] - f["mid_entry"]) + (f["mid_exit"] - f["exit_fill"])) * k
    modeled = (f["spread_entry"] / 2 + f["spread_exit"] / 2) * k
    spread = max(realized, modeled)
    extra = Fraction(cfg.stop_exit_extra_slippage_points) if t.exit_reason == "STOP" else Fraction(0)
    slippage = (Fraction(cfg.entry_slippage_points) + extra) * k
    fees = Fraction(cfg.fx_markup) * abs(gross) if cfg.apply_fx_markup else Fraction(0)
    g, s, sl, fi, fe = (_round_half_up_cents(x) for x in (gross, spread, slippage, Fraction(0), fees))
    return g, s, sl, fi, fe, g - s - sl - fi - fe


def _sweep():
    sizes = [D("0.001"), D("0.01"), D("0.2"), D("1.37")]
    vpps = [D("1"), D("2.5")]
    moves = [D("-37.3"), D("-0.4"), D("0"), D("0.35"), D("30.0")]
    fill_devs = [(D("0.9"), D("0.9")), (D("-0.4"), D("0.2")), (D("2.0"), D("3.0")), (D("0.37"), D("0.11"))]
    spreads = [(D("1.8"), D("1.8")), (D("0.5"), D("4.25"))]
    return itertools.product(
        (Side.BUY, Side.SELL), ("STOP", "MANUAL", "FORCED"), (False, True), sizes, vpps, moves, fill_devs,
        spreads, (0, 2),
    )  # fmt: skip


def test_ledger_row_adds_up_exactly_and_matches_exact_oracle_on_a_sweep():
    mid_entry = D("25000.0")
    count = 0
    for side, reason, fx, size, vpp, move, (dev_in, dev_out), (sp_in, sp_out), nights in _sweep():
        cfg = dataclasses.replace(CFG, apply_fx_markup=fx)
        mid_exit = mid_entry + move
        # BUY: wejście ponad mid, wyjście pod mid; SELL odwrotnie (odchylenie dodatnie = niekorzystne)
        sign = 1 if side is Side.BUY else -1
        t = facts(side=side, size=size, value_per_point=vpp, mid_exit=mid_exit,
                  entry_fill=mid_entry + sign * dev_in, exit_fill=mid_exit - sign * dev_out,
                  spread_entry=sp_in, spread_exit=sp_out, exit_reason=reason, nights_held=nights)  # fmt: skip
        b = CostModel(cfg).compute(t)
        gross, spread, slippage, financing, fees, net = parts(b)
        # sumy: dokładnie, bez żadnej tolerancji
        assert gross - spread - slippage - financing - fees == net
        # każdy składnik jest kwotą z dokładnością do 0,01 (i bez „-0.00")
        for value in (gross, spread, slippage, financing, fees, net):
            assert type(value) is Decimal and value.as_tuple().exponent == -2
            assert not str(value).startswith("-0.00")
        assert spread >= 0 and slippage >= 0 and fees >= 0 and financing == 0
        assert (b.notes == (FINANCING_NOT_MODELLED,)) is (nights > 0)
        # niezależny wzorzec w ułamkach
        assert parts(b) == _oracle(cfg, t)
        count += 1
    assert count == 2 * 3 * 2 * 4 * 2 * 5 * 4 * 2 * 2


def test_spread_cost_is_never_below_the_quoted_spread_model_on_a_sweep():
    for side, size, (dev_in, dev_out) in itertools.product(
        (Side.BUY, Side.SELL), (D("0.2"), D("1.37")), [(D("-5"), D("-5")), (D("0"), D("0")), (D("4"), D("4"))]
    ):
        sign = 1 if side is Side.BUY else -1
        t = facts(side=side, size=size, mid_exit=D("25010.0"),
                  entry_fill=D("25000.0") + sign * dev_in, exit_fill=D("25010.0") - sign * dev_out)  # fmt: skip
        modeled = ((t.spread_entry + t.spread_exit) / 2 * size).quantize(D("0.01"))
        assert model.compute(t).spread_cost >= modeled
