"""Wspólne pomoce testów raportu per pasmo (zadanie 15): dziennik z dwoma pasmami (DE40 w EUR, US500 w USD z księgowaniem
dwufazowym), katalog konfiguracji z `lanes.toml` i uruchamianie raportu."""

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.journal import Journal, OrderRecord
from gwstock.orders.ids import make_client_id
from gwstock.report import cli as report_cli
from gwstock.serialization import dump
from gwstock.types import OrderState
from tests.unit.test_report import D1, D2, D3, D4, D5, at, make_trade


REPO = Path(__file__).resolve().parents[2]


US = "US500"


USD_TRADE = dict(  # wiersz księgi pasma US500: wszystkie kwoty w USD (walucie instrumentu)
    size=Decimal("0.01"), decision_bid=Decimal("7583.0"), decision_ask=Decimal("7583.6"),
    entry_fill=Decimal("7583.8"), exit_fill=Decimal("7591.0"), gross_pnl=Decimal("7.20"),
    spread_cost=Decimal("0.40"), slippage_penalty=Decimal("0.20"), net_pnl=Decimal("6.60"), exit_reason="TARGET",
)  # fmt: skip


LANES_TOML = """enabled = true

[[lane]]
rule = "bar_b1_m5"
rule_file = "lane_us500_b1_m5.toml"
exchange_profile = "NYSE"
currency = "USD"
slippage_guard_fraction = "0.25"
max_loss_per_trade = "1.00"
costs_file = "costs_us500.toml"
"""


COSTS_US500 = """entry_slippage_points = "0.15"
stop_exit_extra_slippage_points = "0.5"
fx_markup = "0.004"
apply_fx_markup = true
"""


def book_lane(
    journal: Journal, day: date, epic: str, *, seq: int = 1, role: str = "entry", vpp: str = "1",
    foreign: bool = False, settle: str | None = None, flags: str = "", **over,
) -> str:  # fmt: skip
    """Zlecenie, kontekst wejścia i zaksięgowana transakcja pasma `epic`; zwraca `deal_id`.

    `foreign`: księgowanie fazy 1 (`record_trade_pending`, wiersz w walucie instrumentu); `settle`: kwota EUR fazy 2."""
    trade = make_trade(day, instrument=epic, **over)
    client_id = make_client_id(day, epic, seq, role)
    deal_id = f"DEAL-{epic}-{day:%Y%m%d}-{seq}-{role}"
    journal.insert_order(OrderRecord(
        client_id, epic, trade.side, trade.size, Decimal("30"), OrderState.CONFIRMED, f"ref-{epic}-{seq}", deal_id,
        trade.signal_ts, trade.confirm_ts,
    ))  # fmt: skip
    context = {"client_id": client_id, "signal_ts": trade.signal_ts, "submit_ts": trade.submit_ts,
               "value_per_point": Decimal(vpp)}  # fmt: skip
    journal.set_meta(f"entry_ctx:{client_id}", dump(context))
    if foreign:
        assert journal.record_trade_pending(trade, deal_id, day, flags=flags)
        if settle is not None:
            assert journal.settle_trade_pnl(deal_id, Decimal(settle), source="HISTORY_TRANSACTIONS")
    else:
        assert journal.record_trade(trade, deal_id, day, flags=flags)
    return deal_id


def book_usd(journal: Journal, day: date, *, seq: int = 1, settle: str | None = None, **over) -> str:
    return book_lane(journal, day, US, seq=seq, foreign=True, settle=settle, **{**USD_TRADE, **over})


@pytest.fixture
def jpath(tmp_path, journal) -> str:
    return str(tmp_path / "journal.sqlite")


@pytest.fixture
def lane_cfg(tmp_path, monkeypatch) -> Path:
    """Katalog konfiguracji z włączonym pasmem US500 (USD) i własnym modelem kosztów."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml", "lane_us500_b1_m5.toml"):
        shutil.copy(REPO / "config" / name, cfg / name)
    (cfg / "costs_us500.toml").write_text(COSTS_US500, encoding="utf-8")
    (cfg / "lanes.toml").write_text(LANES_TOML, encoding="utf-8")
    monkeypatch.setenv("GW_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture
def plain_cfg(monkeypatch) -> None:
    monkeypatch.setenv("GW_CONFIG_DIR", str(REPO / "config"))  # bez lanes.toml: tylko pasmo v1


def run(argv, capsys):
    code = report_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def block(lines: list[str], header: str, count: int) -> list[str]:
    start = lines.index(header)
    return lines[start : start + count]


DAY1 = "Dzień: 2026-11-02 (czas Europe/Berlin, dzień zamknięcia transakcji)"


DAY2 = "Dzień: 2026-11-03 (czas Europe/Berlin, dzień zamknięcia transakcji)"


PORTFOLIO = "Portfel (wspólny, wyniki w EUR)"


def two_lanes(journal: Journal, *, settle: str | None = "5.90") -> None:
    """D1: DE40 -0,03 EUR i US500 +6,60 USD (rozliczone na `settle` EUR); D2: US500 -2,50 USD (czeka na rozliczenie)."""
    book_lane(journal, D1, "DE40", gross_pnl=Decimal("-0.02"), spread_cost=Decimal("0.01"), net_pnl=Decimal("-0.03"))
    book_usd(journal, D1, seq=1, settle=settle)
    book_usd(journal, D2, gross_pnl=Decimal("-2.20"), spread_cost=Decimal("0.20"), slippage_penalty=Decimal("0.10"),
             net_pnl=Decimal("-2.50"), exit_reason="STOP", **later(D2))  # fmt: skip


def later(day: date) -> dict:
    return dict(signal_ts=at(day, 14, 5), submit_ts=at(day, 14, 5, 1), confirm_ts=at(day, 14, 5, 2),
                exit_ts=at(day, 15, 40))  # fmt: skip


def five_sessions(journal: Journal, epic: str, days=(D1, D2, D3, D4, D5), **over) -> None:
    for day in days:
        if epic == US:
            book_usd(journal, day, settle="5.90", **over)
        else:
            book_lane(journal, day, epic, **over)


def gates_line_prefix(epic: str, verdict: str) -> str:
    return f"Bramka L1 (spec §11), pasmo {epic}: {verdict} "
