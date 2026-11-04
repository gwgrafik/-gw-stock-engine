"""Reguła świecowa B1 na świecach M5 (`bar_b1_m5`): czyste funkcje decyzji, bez wejścia/wyjścia, zegara i brokera.

Przeniesiona z `research/candle_strategies.py` (funkcja `signals`, gałąź B1, interwał M5). Źródłem prawdy reguły jest ta
funkcja badawcza, a `tests/unit/test_strategy_bars.py` porównuje obie na danych syntetycznych. Zmiana reguły (progu,
sposobu liczenia bufora albo stopu) to NOWY wariant z osobną pre-rejestracją.

`bar_signal(candles, spread, rules, cfg)` zwraca `BarSignal` albo `None` (brak sygnału). Świeca sygnału to OSTATNIA
świeca listy i musi być ZAKOŃCZONA (pilnuje wywołujący); zakres tworzy `n = cfg.lookback_bars` świec tuż przed nią
(bez niej), starsze świece listy są ignorowane, a lista krótsza niż `n + 1` świec daje `None`. Świece mają ceny MID
(tak je buduje adapter: (bid + ask) / 2 osobno dla O, H, L, C, czyli `mid()` z badań). Kroki:

1. bufor `b = max(rules.stop_step, cfg.spread_buffer_fraction × spread)`;
2. `upper` / `lower` = najwyższy high / najniższy low z `n` poprzednich świec; zakres świecy sygnału `rng = h − l`,
   gdy `rng <= 0` → brak sygnału;
3. long (BUY): `c > upper + b` ORAZ korpus `|c − o| >= cfg.min_body_fraction × rng` ORAZ
   `c − l >= cfg.long_min_close_fraction × rng`; stop = `min(l, upper − b)`;
4. short (SELL): `c < lower − b` ORAZ ten sam korpus ORAZ `c − l <= cfg.short_max_close_fraction × rng`;
   stop = `max(h, lower + b)`; long sprawdzany jest pierwszy, jak w badaniach.

Progi korpusu i położenia zamknięcia są porównywane bez dzielenia (`|c − o| >= 0.60 × rng` zamiast `|c − o| / rng`),
więc granice (dokładnie 0.60, 0.75, 0.25) są rozstrzygane w dokładnej arytmetyce `Decimal`. Wszystkie progi, cel i limit
czasu pochodzą z `BarRuleConfig` (`config/lane_us500_b1_m5.toml`); w kodzie nie ma żadnej z tych liczb.

`entry_plan(signal, quote, rules, risk, cfg, ...)` zamienia sygnał na plan wejścia z bieżącego notowania: wejście
long po ask, short po bid; `stop_distance` = odległość od ceny wejścia do `signal.stop_level` zaokrąglona W GÓRĘ do
`rules.stop_step` (gdy > 0), jak w v1, więc ryzyko nigdy nie rośnie przez zaokrąglenie; `target_distance` =
`signal.target_r × stop_distance` (bez zaokrąglenia: krok celu dobierze adapter); wielkość z tej samej polityki co v1
(`strategy.select_size`: `rules.min_size` przez pierwsze `cfg.fixed_min_size_sessions` sesji pasma albo bez zgody
właściciela, potem wielkość wg reguł). Zwraca `Skip` z przyczyną `STOP_TOO_TIGHT` (cena jest już na poziomie stopu
albo za nim, albo odległość po zaokrągleniu jest mniejsza niż `rules.min_stop_distance`; stopu NIE poszerzamy, bo to
zmieniłoby geometrię reguły) albo `SKIPPED_MIN_SIZE`. `rules.value_per_point` musi być w walucie rachunku (przeliczenie
USD → EUR to sprawa wywołującego, zob. rozstrzygnięcie R8 w planie).

Czego ta funkcja NIE robi (robi wywołujący, bo w badaniach leży to poza `signals`): okna czasowe sygnałów,
zaplanowane spłaszczenie, kontrola ciągłości świec (w badaniach `GAPPED_DATA`), filtry z `run()` (stop bliżej niż
0,01% ceny, odchylenie od zamknięcia sygnału > 0,5 ATR14, koszt rundy > 0,20 R), limity dzienne (10 wejść, koniec dnia
po −3 R) oraz stop, cel i limit czasu po wejściu.

Arytmetyka: wyłącznie `Decimal` w jawnym kontekście 60 cyfr znaczących. Wyjątek (zły typ, spread ujemny albo
nieskończony, świece i reguły z różnych instrumentów, niemalejące znaczniki świec, ujemny `session_index`) oznacza błąd
wywołującego lub adaptera, a nie brak sygnału.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow

from gwstock.config import BarRuleConfig, RiskConfig
from gwstock.strategy import SKIPPED_MIN_SIZE, STOP_TOO_TIGHT, Skip, round_up_to_step, select_size
from gwstock.types import Candle, InstrumentRules, Quote, Side

# Obliczenia reguły: 60 cyfr znaczących, bez zależności od kontekstu otoczenia.
_CALC = Context(
    prec=60,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


def _require_positive_decimal(owner: str, name: str, value: object) -> None:
    where = f"{owner}.{name}"
    if not isinstance(value, Decimal):
        raise TypeError(f"{where}: expected Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{where}: expected a finite Decimal, got {value!r}")
    if value <= 0:
        raise ValueError(f"{where}: expected > 0, got {value}")


@dataclass(frozen=True)
class BarSignal:
    """Sygnał reguły: kierunek, POZIOM stopu (cena), cel w wielokrotnościach R, limit czasu i koniec świecy sygnału."""

    side: Side
    stop_level: Decimal
    target_r: Decimal
    time_limit_min: int
    signal_candle_end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError(f"BarSignal.side: expected Side, got {self.side!r}")
        _require_positive_decimal("BarSignal", "stop_level", self.stop_level)
        _require_positive_decimal("BarSignal", "target_r", self.target_r)
        limit = self.time_limit_min
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"BarSignal.time_limit_min: expected a positive integer, got {limit!r}")
        end = self.signal_candle_end
        if not isinstance(end, datetime) or end.utcoffset() != timedelta(0):
            raise ValueError(f"BarSignal.signal_candle_end: expected a timezone-aware UTC datetime, got {end!r}")


@dataclass(frozen=True)
class BarEntryPlan:
    """Plan wejścia: kierunek, wielkość, odległość stopu i celu (w punktach od ceny wejścia), notowanie z decyzji."""

    side: Side
    size: Decimal
    stop_distance: Decimal
    target_distance: Decimal
    decision_quote: Quote

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError(f"BarEntryPlan.side: expected Side, got {self.side!r}")
        _require_positive_decimal("BarEntryPlan", "size", self.size)
        _require_positive_decimal("BarEntryPlan", "stop_distance", self.stop_distance)
        _require_positive_decimal("BarEntryPlan", "target_distance", self.target_distance)
        if not isinstance(self.decision_quote, Quote):
            raise TypeError(f"BarEntryPlan.decision_quote: expected Quote, got {self.decision_quote!r}")


def _require_spread(spread: object) -> None:
    if not isinstance(spread, Decimal):
        raise TypeError(f"bar_signal: spread: expected Decimal, got {type(spread).__name__} {spread!r}")
    if not spread.is_finite() or spread < 0:
        raise ValueError(f"bar_signal: spread: expected a finite Decimal >= 0, got {spread!r}")


def bar_signal(
    candles: Sequence[Candle], spread: Decimal, rules: InstrumentRules, cfg: BarRuleConfig
) -> BarSignal | None:
    """Sygnał reguły B1 dla ostatniej (zakończonej) świecy listy albo `None`; reguły i kolejność w docstringu modułu.

    `spread` to bieżący spread instrumentu (ask − bid) w punktach, `rules.stop_step` to najmniejszy bufor wybicia.
    """
    _require_spread(spread)
    for index, candle in enumerate(candles):
        if not isinstance(candle, Candle):
            raise TypeError(f"bar_signal: candles[{index}]: expected Candle, got {type(candle).__name__} {candle!r}")
    candle_epics = {candle.epic for candle in candles}
    if len(candle_epics | {rules.epic, cfg.instrument}) != 1:
        raise ValueError(
            "bar_signal: candles, rules and config must be for the same epic, "
            f"got candles {sorted(candle_epics)}, rules {rules.epic!r}, config {cfg.instrument!r}"
        )

    lookback = cfg.lookback_bars
    if len(candles) < lookback + 1:
        return None
    window = tuple(candles[-(lookback + 1) :])
    for earlier, later in zip(window, window[1:]):
        if not earlier.start < later.start:
            raise ValueError(
                f"bar_signal: candle starts must be strictly ascending, got {earlier.start.isoformat()} "
                f"followed by {later.start.isoformat()}"
            )
    prior, signal = window[:-1], window[-1]

    rng = _CALC.subtract(signal.high, signal.low)
    if rng <= 0:
        return None
    buffer = max(rules.stop_step, _CALC.multiply(cfg.spread_buffer_fraction, spread))
    upper = max(candle.high for candle in prior)
    lower = min(candle.low for candle in prior)
    body_ok = _CALC.abs(_CALC.subtract(signal.close, signal.open)) >= _CALC.multiply(cfg.min_body_fraction, rng)
    close_from_low = _CALC.subtract(signal.close, signal.low)
    end = signal.start + timedelta(minutes=cfg.bar_minutes)

    if (
        signal.close > _CALC.add(upper, buffer)
        and body_ok
        and close_from_low >= _CALC.multiply(cfg.long_min_close_fraction, rng)
    ):
        stop = min(signal.low, _CALC.subtract(upper, buffer))
        return BarSignal(Side.BUY, stop, cfg.target_r, cfg.time_limit_min, end)
    if (
        signal.close < _CALC.subtract(lower, buffer)
        and body_ok
        and close_from_low <= _CALC.multiply(cfg.short_max_close_fraction, rng)
    ):
        stop = max(signal.high, _CALC.add(lower, buffer))
        return BarSignal(Side.SELL, stop, cfg.target_r, cfg.time_limit_min, end)
    return None


def entry_plan(
    signal: BarSignal,
    quote: Quote,
    rules: InstrumentRules,
    risk: RiskConfig,
    cfg: BarRuleConfig,
    session_index: int = 0,
    rule_size_allowed: bool = False,
) -> BarEntryPlan | Skip:
    """Plan wejścia z sygnału i bieżącego notowania (long po ask, short po bid); zasady w docstringu modułu.

    `session_index` to liczba zakończonych sesji live TEGO pasma (0 = brak), `rule_size_allowed` ustawia silnik na
    `True` tylko, gdy właściciel utworzył `data/L1_PASSED`. Domyślnie wielkość to `rules.min_size`.
    """
    if not isinstance(signal, BarSignal):
        raise TypeError(f"entry_plan: signal: expected BarSignal, got {type(signal).__name__} {signal!r}")
    if isinstance(session_index, bool) or not isinstance(session_index, int) or session_index < 0:
        raise ValueError(f"entry_plan: session_index: expected a non-negative integer, got {session_index!r}")
    epics = {"quote": quote.epic, "rules": rules.epic, "config": cfg.instrument}
    if len(set(epics.values())) != 1:
        raise ValueError(f"entry_plan: quote, rules and config must be for the same epic, got {epics}")

    if signal.side is Side.BUY:
        distance = _CALC.subtract(quote.ask, signal.stop_level)
    else:
        distance = _CALC.subtract(signal.stop_level, quote.bid)
    if distance <= 0:
        return Skip(STOP_TOO_TIGHT)
    stop = round_up_to_step(distance, rules.stop_step) if rules.stop_step > 0 else distance
    if stop < rules.min_stop_distance:
        return Skip(STOP_TOO_TIGHT)

    size = select_size(
        stop,
        quote,
        rules,
        risk,
        session_index=session_index,
        fixed_min_size_sessions=cfg.fixed_min_size_sessions,
        rule_size_allowed=rule_size_allowed,
    )
    if size is None:
        return Skip(SKIPPED_MIN_SIZE)
    return BarEntryPlan(signal.side, size, stop, _CALC.multiply(signal.target_r, stop), quote)
