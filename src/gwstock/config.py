"""Ładowanie konfiguracji z plików TOML (`config/`) i stanu powiązania rachunku (`data/account.json`).

Zasady:
- kwoty i ułamki w TOML są napisami ("1.00") parsowanymi do Decimal; liczba TOML-float jest odrzucana;
  napis to wyłącznie zwykły zapis dziesiętny `cyfry` albo `cyfry.cyfry` (ASCII, bez spacji, `_`, wykładnika i plusa);
- liczniki i sekundy są liczbami całkowitymi, flagi są wartościami bool TOML;
- granice (sprawdzane przy wczytaniu, nie w konstruktorach): pola `*_s`, `max_entries_per_day`, `range_minutes`,
  `bar_minutes`, `lookback_bars`, `time_limit_min`, `target_r`
  oraz kwoty i dźwignia ryzyka (`virtual_capital`, `max_*`) są > 0 (to dzielniki, okresy i limity), pola `*_fraction`
  i `fx_markup` leżą w [0, 1], pozostałe liczby są >= 0 (zero ma tam sens według specyfikacji);
- brakujący klucz, nieznany klucz, zły typ lub poza granicą to ValueError z nazwą pliku i klucza (literówka nie
  przechodzi po cichu); plik TOML, którego nie da się odczytać, to ValueError z nazwą pliku;
- wszystkie dataclasses są niemutowalne.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar, get_type_hints


@dataclass(frozen=True)
class StrategyConfig:
    instrument: str
    exchange_profile: str
    range_minutes: int
    stop_atr_fraction: Decimal
    min_stop_spread_multiple: Decimal
    slippage_guard_fraction: Decimal
    flatten_before_close_min: int
    entry_deadline_after_range_min: int
    fixed_min_size_sessions: int


@dataclass(frozen=True)
class RiskConfig:
    virtual_capital: Decimal
    max_loss_per_trade: Decimal
    max_daily_loss: Decimal
    max_total_drawdown: Decimal
    max_effective_leverage: Decimal
    max_entries_per_day: int
    stale_quote_s: int
    mismatch_tolerance_s: int
    reconcile_interval_s: int
    ping_interval_s: int
    submit_timeout_s: int
    margin_reserve_fraction: Decimal


@dataclass(frozen=True)
class CostConfig:
    entry_slippage_points: Decimal
    stop_exit_extra_slippage_points: Decimal
    fx_markup: Decimal
    apply_fx_markup: bool


@dataclass(frozen=True)
class RuntimeConfig:
    live_allowed: bool
    account_id: str | None


@dataclass(frozen=True)
class BarRuleConfig:
    """Parametry reguły świecowej B1 (`strategy_bars`): jeden plik na pasmo, np. `lane_us500_b1_m5.toml`."""

    instrument: str
    bar_minutes: int
    lookback_bars: int
    min_body_fraction: Decimal
    long_min_close_fraction: Decimal
    short_max_close_fraction: Decimal
    spread_buffer_fraction: Decimal
    target_r: Decimal
    time_limit_min: int
    fixed_min_size_sessions: int


_T = TypeVar("_T")

# Zwykły zapis dziesiętny: tylko cyfry ASCII, opcjonalna część ułamkowa i opcjonalny minus (odrzucany niżej jako
# wartość ujemna). `Decimal(napis)` przyjmuje też spacje, `_`, wykładnik, plus i cyfry spoza ASCII, a konfiguracja
# jest wprowadzana ręcznie, więc zapis ma być jednoznaczny.
_PLAIN_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _is_positive_field(key: str) -> bool:
    """Pola, które muszą być > 0: okresy `*_s`, dzielniki i limity ryzyka, liczba wejść i długość zakresu."""
    return key.endswith("_s") or key in {
        "virtual_capital",
        "max_loss_per_trade",
        "max_daily_loss",
        "max_total_drawdown",
        "max_effective_leverage",
        "max_entries_per_day",
        "range_minutes",
        "bar_minutes",
        "lookback_bars",
        "time_limit_min",
        "target_r",
    }


def _is_unit_fraction_field(key: str) -> bool:
    """Ułamki, które muszą leżeć w [0, 1]: pola `*_fraction` i narzut walutowy."""
    return key.endswith("_fraction") or key == "fx_markup"


def _convert(source: str, key: str, value: Any, expected: type) -> Any:
    where = f"{source}: {key}"
    if expected is Decimal:
        if isinstance(value, float):
            raise ValueError(f"{where}: TOML float is not allowed, use string decimals (e.g. \"1.00\")")
        if not isinstance(value, str):
            raise ValueError(f"{where}: use string decimals, got {type(value).__name__}")
        if _PLAIN_DECIMAL.fullmatch(value) is None:
            raise ValueError(f"{where}: {value!r} is not a plain decimal number (digits with an optional fraction)")
        number = Decimal(value)
        if number.is_signed():  # także „-0”
            raise ValueError(f"{where}: {value!r} must be a finite, non-negative decimal")
        if _is_positive_field(key) and number <= 0:
            raise ValueError(f"{where}: {value!r} must be a decimal > 0")
        if _is_unit_fraction_field(key) and number > 1:
            raise ValueError(f"{where}: {value!r} must be a fraction in [0, 1]")
        return number
    if expected is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{where}: expected TOML bool (true/false), got {type(value).__name__}")
        return value
    if expected is int:
        if isinstance(value, float):
            raise ValueError(f"{where}: expected integer, got TOML float {value!r}")
        # bool jest podklasą int, więc musi być odrzucone jawnie
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{where}: expected non-negative integer, got {value!r}")
        if _is_positive_field(key) and value == 0:
            raise ValueError(f"{where}: expected positive integer (> 0), got {value!r}")
        return value
    if expected is str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}: expected non-empty string, got {value!r}")
        return value
    raise TypeError(f"unsupported config field type {expected!r} for {where}")


def _read_toml(path: Path) -> dict[str, Any]:
    """Wczytuje plik TOML; błąd składni albo kodowania to ValueError z nazwą pliku (brak pliku: FileNotFoundError)."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path.name}: cannot read the TOML file: {exc}") from None


def _load_table(path: Path, cls: type[_T]) -> _T:
    raw = _read_toml(path)
    hints = get_type_hints(cls)
    names = [f.name for f in fields(cls)]  # type: ignore[arg-type]
    unknown = sorted(set(raw) - set(names))
    if unknown:
        raise ValueError(f"{path.name}: unknown key(s): {', '.join(unknown)}")
    missing = [n for n in names if n not in raw]
    if missing:
        raise ValueError(f"{path.name}: missing key(s): {', '.join(missing)}")
    return cls(**{n: _convert(path.name, n, raw[n], hints[n]) for n in names})


def load_config(config_dir: Path) -> tuple[StrategyConfig, RiskConfig, CostConfig]:
    config_dir = Path(config_dir)
    return (
        _load_table(config_dir / "strategy_v1.toml", StrategyConfig),
        _load_table(config_dir / "risk_v1.toml", RiskConfig),
        _load_table(config_dir / "costs_v1.toml", CostConfig),
    )


def load_bar_rule_config(path: Path) -> BarRuleConfig:
    """Wczytuje plik reguły świecowej pasma (np. `config/lane_us500_b1_m5.toml`), ściśle jak pliki v1.

    Brak pliku to FileNotFoundError; brakujący lub nieznany klucz, zły typ i wartość poza granicą to ValueError z nazwą
    pliku i klucza. Osobne od `load_config`: pliki v1 i ich odcisk pre-rejestracji się nie zmieniają.
    """
    return _load_table(Path(path), BarRuleConfig)


def _load_live_allowed(path: Path) -> bool:
    if not path.exists():
        return False
    raw = _read_toml(path)
    unknown = sorted(set(raw) - {"live_allowed"})
    if unknown:
        raise ValueError(f"{path.name}: unknown key(s): {', '.join(unknown)}")
    if "live_allowed" not in raw:
        return False
    return _convert(path.name, "live_allowed", raw["live_allowed"], bool)


def _load_account_id(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: malformed account file (not valid JSON): {exc}") from None
    if not isinstance(data, dict) or "account_id" not in data:
        raise ValueError(f"{path.name}: malformed account file, expected object with key account_id")
    extra = sorted(str(key) for key in data if key != "account_id")
    if extra:
        # plik wiążący rachunek ma jeden klucz; nieznany klucz to literówka albo ręczna edycja (bez wartości w komunikacie)
        raise ValueError(
            f"{path.name}: nieznane klucze: {', '.join(extra)}; plik wiążący rachunek zawiera wyłącznie account_id"
        )
    account_id = data["account_id"]
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError(f"{path.name}: account_id must be a non-empty string")
    return account_id


def load_runtime(config_dir: Path, data_dir: Path) -> RuntimeConfig:
    """`live_allowed` z `config/runtime.toml` (brak pliku = false); `account_id` wyłącznie z `data/account.json`.

    Błąd w którymkolwiek pliku to ValueError (fail closed, bez zgadywania). Brak `account.json` = None.
    """
    return RuntimeConfig(
        live_allowed=_load_live_allowed(Path(config_dir) / "runtime.toml"),
        account_id=_load_account_id(Path(data_dir) / "account.json"),
    )
