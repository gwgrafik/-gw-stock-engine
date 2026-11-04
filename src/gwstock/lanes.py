"""Pasma (lanes) silnika: jeden instrument + jedna reguła + własna konfiguracja (projekt wielu pasm, R1).

Na razie tylko `LaneSpec`: to, czego wykonawca (`gwstock.execution.Executor`) potrzebuje o paśmie. Wtyki reguł, okna
i filtry dnia dojdą w zadaniu 11 projektu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from gwstock.calendar import ExchangeCalendar
from gwstock.config import RiskConfig, StrategyConfig

_EPIC = re.compile(r"[A-Za-z0-9]+")  # jak człon instrumentu identyfikatora zlecenia (`orders.ids.make_client_id`)
_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class LaneSpec:
    """Opis pasma dla wykonawcy (projekt §2.2). Wszystkie wartości pochodzą z konfiguracji pasma.

    - `epic`: instrument pasma (najwyżej jedna otwarta pozycja na instrument, R1);
    - `calendar`: kalendarz dnia księgowania (data lokalna to klucz `realized_pnl` i podstawa liczenia nocy
      trzymania); wspólny dla pasm portfela (dzień portfela, projekt §1.3, pytanie 4 właściciela);
    - `slippage_guard_fraction`: straż poślizgu wejścia (ułamek odległości stopu);
    - `max_loss_per_trade`: limit straty jednej transakcji w WALUCIE RACHUNKU (straż wielkości, odległość stopu z limitu);
    - `currency`: waluta instrumentu (ISO 4217) albo None, gdy instrument jest w walucie rachunku (DE40 v1); inna niż
      waluta rachunku wymaga przeliczeń przez `FxView` i dwufazowego księgowania (projekt §7);
    - `uses_target`: pasmo wchodzi z celem zysku dołączonym atomowo (`TargetedEntryRequest`, R4) i zabezpiecza
      pozycję stopem RAZEM z celem (`Broker.set_protection`); pasmo bez celu (DE40 v1) księguje zamknięcie celem
      jak STOP (projekt §3.5: zachowanie v1 bajt w bajt).
    """

    epic: str
    calendar: ExchangeCalendar
    slippage_guard_fraction: Decimal
    max_loss_per_trade: Decimal
    currency: str | None = None
    uses_target: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.epic, str) or not _EPIC.fullmatch(self.epic):
            raise ValueError(f"LaneSpec.epic: expected letters and digits only, got {self.epic!r}")
        if not isinstance(self.calendar, ExchangeCalendar):
            raise TypeError(f"LaneSpec.calendar: expected ExchangeCalendar, got {self.calendar!r}")
        for name in ("slippage_guard_fraction", "max_loss_per_trade"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"LaneSpec.{name}: expected a finite Decimal > 0, got {value!r}")
        if self.currency is not None and (not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency)):
            raise ValueError(f"LaneSpec.currency: expected a 3-letter ISO 4217 code or None, got {self.currency!r}")
        if not isinstance(self.uses_target, bool):
            raise TypeError(f"LaneSpec.uses_target: expected bool, got {self.uses_target!r}")

    @classmethod
    def from_v1(cls, strat: StrategyConfig, risk: RiskConfig, calendar: ExchangeCalendar) -> LaneSpec:
        """Pasmo DE40 `orb_index_cfd` v1 z `strategy_v1.toml` i `risk_v1.toml` (bez celu, w walucie rachunku)."""
        return cls(
            epic=strat.instrument,
            calendar=calendar,
            slippage_guard_fraction=strat.slippage_guard_fraction,
            max_loss_per_trade=risk.max_loss_per_trade,
        )
