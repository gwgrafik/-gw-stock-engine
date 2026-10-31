"""Strategia ORB `orb_index_cfd` (spec §5): czysta funkcja decyzji, bez wejścia/wyjścia, zegara i brokera.

Z pierwszej świecy 5-minutowej, dziennego ATR14, bieżącego notowania i reguł instrumentu `decide` zwraca
`EntrySignal` (kierunek, odległość stopu, wielkość) albo `Skip` z jedną z czterech przyczyn.

Kolejność kontroli (pierwsza zgłoszona przyczyna wygrywa):

1. brak pierwszej świecy albo ATR niepoprawny (nie-skończony lub <= 0)  → `NO_SIGNAL_DATA`;
2. zamknięcie == otwarcie                                              → `DOJI`;
3. stop = max(`stop_atr_fraction` × ATR, min. stop brokera), zaokrąglony W GÓRĘ do `stop_step`, gdy ten jest > 0;
   stop <= 0 albo stop < `min_stop_spread_multiple` × spread          → `STOP_TOO_TIGHT`;
4. wielkość (tabela niżej); wielkość wg reguł poniżej `rules.min_size` → `SKIPPED_MIN_SIZE`.

Kierunek wynika wyłącznie z koloru świecy (zamknięcie > otwarcie → BUY, < → SELL); zakres świecy (high/low) nie jest
używany. Odległość stopu zaokrąglona w górę jest używana we WSZYSTKICH dalszych krokach (kontrola spreadu, wielkość),
więc ryzyko nigdy nie rośnie przez zaokrąglenie.

Polityka wielkości (spec §5 pkt 6 i §11 bramka L1):

- `rules.min_size`, gdy `session_index < strat.fixed_min_size_sessions` ALBO `rule_size_allowed` nie jest dokładnie
  `True` (bezpieczna wartość domyślna; `True` ustawia wyłącznie silnik, gdy właściciel utworzył `data/L1_PASSED`);
- wielkość wg reguł tylko, gdy zachodzą OBA warunki:
  `floor_do_kroku(min(max_loss_per_trade / (stop × vpp), max_effective_leverage × virtual_capital / (mid × vpp)))`.

W trybie minimalnym funkcja NIE sprawdza limitu straty: przy bardzo dużym ATR (stop 1500 pkt) wielkość 0.001 daje
stratę 1.50 EUR > `max_loss_per_trade`, a mimo to zwracany jest sygnał. Zablokuje go bramka ryzyka (LOSS_PER_TRADE).

Arytmetyka: wyłącznie `Decimal`, w jawnym kontekście 60 cyfr znaczących (niezależnym od kontekstu otoczenia);
zaokrąglenia do kroku są dokładne (ułamki), bez utraty precyzji. Wyjątek (zły typ ATR, ujemny `session_index`, wartość
punktu lub mid <= 0 przy liczeniu wielkości) oznacza błąd wywołującego lub adaptera, a nie brak sygnału.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow
from fractions import Fraction

from gwstock.config import RiskConfig, StrategyConfig
from gwstock.types import Candle, InstrumentRules, Quote, Side

NO_SIGNAL_DATA = "NO_SIGNAL_DATA"
DOJI = "DOJI"
STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
SKIPPED_MIN_SIZE = "SKIPPED_MIN_SIZE"
SKIP_REASONS = frozenset({NO_SIGNAL_DATA, DOJI, STOP_TOO_TIGHT, SKIPPED_MIN_SIZE})

ATR_PERIOD = 14

# Obliczenia strategii: 60 cyfr znaczących, bez zależności od kontekstu otoczenia.
_CALC = Context(
    prec=60,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
# Mnożenie krok × liczba kroków: szeroka precyzja i pułapka na Inexact, więc wynik jest dokładny albo wyjątek.
_EXACT = Context(
    prec=400,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
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
class EntrySignal:
    """Decyzja o wejściu: kierunek, odległość stopu w punktach, wielkość i notowanie z chwili decyzji."""

    side: Side
    stop_distance: Decimal
    size: Decimal
    decision_quote: Quote

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError(f"EntrySignal.side: expected Side, got {self.side!r}")
        _require_positive_decimal("EntrySignal", "stop_distance", self.stop_distance)
        _require_positive_decimal("EntrySignal", "size", self.size)
        if not isinstance(self.decision_quote, Quote):
            raise TypeError(f"EntrySignal.decision_quote: expected Quote, got {self.decision_quote!r}")


@dataclass(frozen=True)
class Skip:
    """Brak wejścia; `reason` to jedna z `SKIP_REASONS`."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in SKIP_REASONS:
            raise ValueError(f"Skip.reason: expected one of {sorted(SKIP_REASONS)}, got {self.reason!r}")


def _check_step_args(fn: str, value: object, step: object) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{fn}: value: expected Decimal, got {type(value).__name__} {value!r}")
    if not isinstance(step, Decimal):
        raise TypeError(f"{fn}: step: expected Decimal, got {type(step).__name__} {step!r}")
    if not value.is_finite():
        raise ValueError(f"{fn}: value must be finite, got {value!r}")
    if not step.is_finite() or step <= 0:
        raise ValueError(f"{fn}: step must be a finite Decimal > 0, got {step!r}")


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Największa wielokrotność `step` nie większa niż `value` (ku −∞). Dokładne; `step` <= 0 to ValueError."""
    _check_step_args("round_down_to_step", value, step)
    return _EXACT.multiply(Decimal(math.floor(Fraction(value) / Fraction(step))), step)


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Najmniejsza wielokrotność `step` nie mniejsza niż `value` (ku +∞). Dokładne; `step` <= 0 to ValueError."""
    _check_step_args("round_up_to_step", value, step)
    return _EXACT.multiply(Decimal(math.ceil(Fraction(value) / Fraction(step))), step)


def atr14(daily: list[Candle]) -> Decimal:
    """Średnia prosta z 14 ostatnich prawdziwych zakresów TR dziennych świec (ATR14, w punktach).

    TR_i = max(H − L, |H − C_prev|, |L − C_prev|). Wymaga co najmniej 15 świec (14 zakresów potrzebuje 14 poprzednich
    zamknięć); używa 15 OSTATNICH, a z najstarszej z nich tylko zamknięcia. Świece to ceny MID (buduje je adapter).

    Wywołujący podaje świece rosnąco wg `start`; funkcja niczego nie sortuje, tylko sprawdza, że całe wejście ma
    ściśle rosnące `start` (inaczej ValueError). Świeca z high < low to błąd danych (ValueError). Wynik to dzielenie
    `Decimal` w kontekście 60 cyfr znaczących, bez dalszego zaokrąglania.
    """
    needed = ATR_PERIOD + 1
    if len(daily) < needed:
        raise ValueError(f"atr14: need at least {needed} daily candles, got {len(daily)}")
    for earlier, later in zip(daily, daily[1:]):
        if not earlier.start < later.start:
            raise ValueError(
                f"atr14: candle starts must be strictly ascending, got {earlier.start.isoformat()} "
                f"followed by {later.start.isoformat()}"
            )
    window = daily[-needed:]
    total = Decimal(0)
    for prev, cur in zip(window, window[1:]):
        if cur.high < cur.low:
            raise ValueError(f"atr14: candle {cur.start.isoformat()} has high {cur.high} below low {cur.low}")
        true_range = max(
            _CALC.subtract(cur.high, cur.low),
            _CALC.abs(_CALC.subtract(cur.high, prev.close)),
            _CALC.abs(_CALC.subtract(cur.low, prev.close)),
        )
        total = _CALC.add(total, true_range)
    return _CALC.divide(total, Decimal(ATR_PERIOD))


def _rule_size(stop: Decimal, quote: Quote, rules: InstrumentRules, risk: RiskConfig) -> Decimal:
    """Wielkość wg reguł: mniejszy z limitów straty i dźwigni, w dół do kroku wielkości."""
    vpp = rules.value_per_point
    mid = _CALC.divide(_CALC.add(quote.bid, quote.ask), Decimal(2))
    if vpp <= 0:
        raise ValueError(f"decide: rules.value_per_point must be > 0 to size a position, got {vpp}")
    if mid <= 0:
        raise ValueError(f"decide: quote.mid must be > 0 to size a position, got {mid}")
    loss_limit = _CALC.divide(risk.max_loss_per_trade, _CALC.multiply(stop, vpp))
    leverage_limit = _CALC.divide(
        _CALC.multiply(risk.max_effective_leverage, risk.virtual_capital),
        _CALC.multiply(mid, vpp),
    )
    return round_down_to_step(min(loss_limit, leverage_limit), rules.size_step)


def decide(
    first: Candle | None,
    atr: Decimal,
    quote: Quote,
    rules: InstrumentRules,
    strat: StrategyConfig,
    risk: RiskConfig,
    session_index: int = 0,
    rule_size_allowed: bool = False,
) -> EntrySignal | Skip:
    """Decyzja strategii ORB; reguły, kolejność kontroli i polityka wielkości w docstringu modułu.

    `session_index` to liczba zakończonych sesji live (0 = brak), `rule_size_allowed` ustawia silnik na `True` tylko,
    gdy właściciel utworzył `data/L1_PASSED`. Domyślnie wielkość to `rules.min_size`.
    """
    if not isinstance(atr, Decimal):
        raise TypeError(f"decide: atr: expected Decimal, got {type(atr).__name__} {atr!r}")
    if isinstance(session_index, bool) or not isinstance(session_index, int) or session_index < 0:
        raise ValueError(f"decide: session_index: expected a non-negative integer, got {session_index!r}")

    if first is None or not atr.is_finite() or atr <= 0:
        return Skip(NO_SIGNAL_DATA)
    if first.close == first.open:
        return Skip(DOJI)
    side = Side.BUY if first.close > first.open else Side.SELL

    stop = max(_CALC.multiply(strat.stop_atr_fraction, atr), rules.min_stop_distance)
    if rules.stop_step > 0:
        stop = round_up_to_step(stop, rules.stop_step)
    spread = _CALC.subtract(quote.ask, quote.bid)
    if stop <= 0 or stop < _CALC.multiply(strat.min_stop_spread_multiple, spread):
        return Skip(STOP_TOO_TIGHT)

    # `is not True`: tylko jawne True włącza wielkość wg reguł (prawdziwa, lecz inna wartość nie wystarcza).
    if session_index < strat.fixed_min_size_sessions or rule_size_allowed is not True:
        size = rules.min_size
    else:
        size = _rule_size(stop, quote, rules, risk)
        if size < rules.min_size:
            return Skip(SKIPPED_MIN_SIZE)
    return EntrySignal(side, stop, size, quote)
