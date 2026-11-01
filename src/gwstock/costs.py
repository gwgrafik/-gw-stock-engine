"""Model kosztów (spec §10): z faktów jednej zamkniętej transakcji robi wiersz księgi.

Zasady:
- `gross_pnl` liczony na cenach **mid** (decyzji i wyjścia), a nie na wypełnieniach;
- `spread_cost = max(koszt zrealizowany w wypełnieniach, koszt modelowy z notowanych spreadów)`,
  więc nigdy nie jest niższy niż model z notowań (poprawa ceny w wypełnieniu nie zmniejsza kosztu);
- `slippage_penalty` = poślizg wejścia (+ dodatkowy poślizg, gdy wyjście to STOP) z konfiguracji;
- finansowanie w v1 to zawsze 0,00; pozycja trzymana przez noc nie przewraca liczenia, tylko dostaje notatkę
  `FINANCING_NOT_MODELLED` (księgowanie zamkniętej transakcji nie może się nie udać);
- `fees` to markup walutowy `fx_markup × |gross_pnl|` (stosowany do przeliczanego wyniku), tylko gdy
  `apply_fx_markup`; w przeciwnym razie 0,00;
- każdy składnik jest liczony dokładnie (`Decimal`, bez `float`) i zaokrąglany dopiero na końcu, osobno,
  do 0,01 metodą `ROUND_HALF_UP` (połówka od zera, także dla strat);
- `net_pnl = gross − spread_cost − slippage_penalty − financing − fees` z już ZAOKRĄGLONYCH składników,
  więc wiersz księgi sumuje się co do grosza.

Nieprawidłowe wejście to `ValueError` z nazwą pola; poprawne wejście nigdy nie rzuca. Wielkości `TradeFacts` mają
granicę |wartość| < 10^30 (dość dla każdej realnej ceny i wielkości), więc iloczyny mieszczą się w kontekście
obliczeń (120 cyfr) i nie trzeba polegać na łapaniu wyjątków `decimal` ze środka liczenia.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from gwstock.config import CostConfig
from gwstock.types import CLOSE_REASONS, Side

FINANCING_NOT_MODELLED = "FINANCING_NOT_MODELLED"

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
# Granica wielkości faktów transakcji: |wartość| < 10^30. Iloczyn dwóch takich wartości ma < 61 cyfr, a wynik brutto
# (różnica cen razy wielkość razy wartość punktu) < 10^91, więc razem z dwoma miejscami po przecinku mieści się w
# kontekście `_CTX` (120 cyfr).
_MAGNITUDE_LIMIT = Decimal(10) ** 30
# Szeroka precyzja, żeby iloczyny i sumy były dokładne dla każdej realnej ceny i wielkości.
_CTX = Context(
    prec=120,
    rounding=ROUND_HALF_UP,
    Emin=-999999,
    Emax=999999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


def _check_decimal(
    owner: str, name: str, value: object, *, minimum: str | None = None, positive: bool = False, bounded: bool = False
) -> None:
    where = f"{owner}.{name}"
    if not isinstance(value, Decimal):
        raise ValueError(f"{where}: expected Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{where}: expected a finite Decimal, got {value!r}")
    # porównanie jest dokładne; `abs()` zaokrągliłoby wartość do precyzji kontekstu otoczenia
    if bounded and not -_MAGNITUDE_LIMIT < value < _MAGNITUDE_LIMIT:
        raise ValueError(f"{where}: magnitude must be below {_MAGNITUDE_LIMIT:.0E}, got {value}")
    if positive and value <= 0:
        raise ValueError(f"{where}: must be > 0, got {value}")
    if minimum is not None and value < Decimal(minimum):
        raise ValueError(f"{where}: must be >= {minimum}, got {value}")


def _money(value: Decimal) -> Decimal:
    """Do 0,01 (ROUND_HALF_UP); zero zawsze jako `0.00`, nigdy `-0.00`.

    Uwaga: `quantize` bierze precyzję z kontekstu OTOCZENIA (tylko tryb zaokrąglenia jest podany jawnie) i rzuca
    `InvalidOperation`, gdy wynik ma więcej cyfr, niż kontekst pozwala. Dlatego wolno wywoływać ją wyłącznie wewnątrz
    `localcontext(_CTX)` (120 cyfr), tak jak robi to `CostModel.compute`; w domyślnym kontekście (28 cyfr) duży wynik
    by się nie zmieścił.
    """
    cents = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    return _ZERO if cents.is_zero() else cents


@dataclass(frozen=True)
class TradeFacts:
    """Fakty jednej zamkniętej transakcji.

    `mid_*` to środek notowania (decyzji i wyjścia), `*_fill` to ceny wypełnień od brokera, `spread_*` to
    notowany spread (ask − bid) w chwili wejścia i wyjścia. `exit_reason` ∈ {"STOP", "MANUAL", "FORCED"}
    (jak `ClosedDeal.reason`), `nights_held` to liczba nocy z otwartą pozycją.
    """

    side: Side
    size: Decimal
    value_per_point: Decimal
    mid_entry: Decimal
    mid_exit: Decimal
    entry_fill: Decimal
    exit_fill: Decimal
    spread_entry: Decimal
    spread_exit: Decimal
    exit_reason: str
    nights_held: int

    def __post_init__(self) -> None:
        _validate_facts(self)


def _validate_facts(t: object) -> None:
    if not isinstance(t, TradeFacts):
        raise ValueError(f"expected TradeFacts, got {type(t).__name__}")
    if not isinstance(t.side, Side):
        raise ValueError(f"TradeFacts.side: expected Side, got {t.side!r}")
    _check_decimal("TradeFacts", "size", t.size, positive=True, bounded=True)
    _check_decimal("TradeFacts", "value_per_point", t.value_per_point, positive=True, bounded=True)
    for name in ("mid_entry", "mid_exit", "entry_fill", "exit_fill"):
        _check_decimal("TradeFacts", name, getattr(t, name), bounded=True)
    for name in ("spread_entry", "spread_exit"):
        _check_decimal("TradeFacts", name, getattr(t, name), minimum="0", bounded=True)
    if not isinstance(t.exit_reason, str) or t.exit_reason not in CLOSE_REASONS:
        raise ValueError(f"TradeFacts.exit_reason: expected one of {sorted(CLOSE_REASONS)}, got {t.exit_reason!r}")
    # bool jest podklasą int, ale `True` jako liczba nocy to błąd wywołującego
    if not isinstance(t.nights_held, int) or isinstance(t.nights_held, bool) or t.nights_held < 0:
        raise ValueError(f"TradeFacts.nights_held: expected an integer >= 0, got {t.nights_held!r}")


@dataclass(frozen=True)
class CostBreakdown:
    """Wiersz księgi kosztów; `net_pnl` musi być równe `gross − spread − slippage − financing − fees`.

    `notes` niesie znaczniki ostrzeżeń (np. `FINANCING_NOT_MODELLED`) do notatki wiersza księgi.
    """

    gross_pnl: Decimal
    spread_cost: Decimal
    slippage_penalty: Decimal
    financing: Decimal
    fees: Decimal
    net_pnl: Decimal
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("gross_pnl", "spread_cost", "slippage_penalty", "financing", "fees", "net_pnl"):
            _check_decimal("CostBreakdown", name, getattr(self, name))
        if not isinstance(self.notes, tuple) or not all(isinstance(n, str) for n in self.notes):
            raise ValueError(f"CostBreakdown.notes: expected a tuple of str, got {self.notes!r}")
        with localcontext(_CTX):
            expected = self.gross_pnl - self.spread_cost - self.slippage_penalty - self.financing - self.fees
            if self.net_pnl != expected:
                raise ValueError(f"CostBreakdown.net_pnl: {self.net_pnl} does not add up, expected {expected}")


class CostModel:
    """Czysta funkcja kosztów z konfiguracji (`costs_v1.toml`); nie ma stanu ani wejścia/wyjścia."""

    def __init__(self, cfg: CostConfig) -> None:
        if not isinstance(cfg, CostConfig):
            raise ValueError(f"CostModel: expected CostConfig, got {type(cfg).__name__}")
        for name in ("entry_slippage_points", "stop_exit_extra_slippage_points", "fx_markup"):
            _check_decimal("CostConfig", name, getattr(cfg, name), minimum="0")
        if not isinstance(cfg.apply_fx_markup, bool):
            raise ValueError(f"CostConfig.apply_fx_markup: expected bool, got {cfg.apply_fx_markup!r}")
        self._cfg = cfg

    def compute(self, t: TradeFacts) -> CostBreakdown:
        # fakty mogły ominąć konstruktor (np. `object.__setattr__`), a księgowanie ma liczyć tylko poprawne dane
        _validate_facts(t)
        cfg = self._cfg
        notes = (FINANCING_NOT_MODELLED,) if t.nights_held > 0 else ()
        try:
            with localcontext(_CTX):
                per_point = t.size * t.value_per_point  # € na punkt całej pozycji
                direction = 1 if t.side is Side.BUY else -1

                gross = direction * (t.mid_exit - t.mid_entry) * per_point
                # koszt zrealizowany: o ile wypełnienia są gorsze od mid (ujemny przy poprawie ceny)
                realized = direction * ((t.entry_fill - t.mid_entry) + (t.mid_exit - t.exit_fill)) * per_point
                modeled = (t.spread_entry / 2 + t.spread_exit / 2) * per_point
                spread = max(realized, modeled)

                slippage_points = cfg.entry_slippage_points
                if t.exit_reason == "STOP":
                    slippage_points += cfg.stop_exit_extra_slippage_points
                slippage = slippage_points * per_point

                fees = cfg.fx_markup * abs(gross) if cfg.apply_fx_markup else _ZERO

                gross_r, spread_r, slippage_r = _money(gross), _money(spread), _money(slippage)
                financing_r, fees_r = _ZERO, _money(fees)
                net_r = gross_r - spread_r - slippage_r - financing_r - fees_r
        except ArithmeticError as exc:  # np. wartości poza sensownym zakresem; nie wyciekają wyjątki decimal
            raise ValueError(f"TradeFacts: values out of the supported range ({type(exc).__name__})") from exc
        return CostBreakdown(gross_r, spread_r, slippage_r, financing_r, fees_r, net_r, notes)
