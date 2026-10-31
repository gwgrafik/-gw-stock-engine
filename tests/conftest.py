"""Wspólne fixtures testów. Uruchamiać z korzenia repozytorium (ścieżka względna `config/`).

Kolejne zadania dopisują tu swoje fixtures (`journal`, `mgr`, `req`, `gate`, `kill`, `reconciler`).
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.broker.fake import FakeBroker
from gwstock.clock import FakeClock
from gwstock.config import CostConfig, RiskConfig, StrategyConfig, load_config
from gwstock.types import InstrumentRules, Quote


@pytest.fixture
def clock() -> FakeClock:
    # 2026-11-02 07:30 UTC = 08:30 w Berlinie (czas zimowy), poniedziałek, dzień sesji XETRA
    return FakeClock(datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc))


@pytest.fixture
def quote(clock: FakeClock) -> Quote:
    return Quote("DE40", Decimal("25780.3"), Decimal("25782.3"), clock.now_utc())


@pytest.fixture
def rules() -> InstrumentRules:
    return InstrumentRules(
        epic="DE40",
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
        value_per_point=Decimal("1"),
        margin_rate=Decimal("0.05"),
        min_stop_distance=Decimal("2.6"),
    )


@pytest.fixture
def fake(clock: FakeClock, rules: InstrumentRules, quote: Quote) -> FakeBroker:
    """Świeży FakeBroker: działa jak nowa sesja (login niewymagany, dopóki test nie wywoła `expire_session`)."""
    return FakeBroker(clock, rules, quote)


@pytest.fixture(scope="session")
def _configs() -> tuple[StrategyConfig, RiskConfig, CostConfig]:
    return load_config(Path("config"))


@pytest.fixture
def strat_cfg(_configs) -> StrategyConfig:
    return _configs[0]


@pytest.fixture
def risk_cfg(_configs) -> RiskConfig:
    return _configs[1]


@pytest.fixture
def cost_cfg(_configs) -> CostConfig:
    return _configs[2]
