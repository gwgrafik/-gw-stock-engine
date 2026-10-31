"""Ładowanie konfiguracji z plików TOML (`config/`) i stanu powiązania rachunku (`data/account.json`).

Zasady:
- kwoty i ułamki w TOML są napisami ("1.00") parsowanymi do Decimal; liczba TOML-float jest odrzucana;
- liczniki i sekundy są liczbami całkowitymi, flagi są wartościami bool TOML;
- brakujący klucz, nieznany klucz lub zły typ to ValueError z nazwą klucza (literówka nie przechodzi po cichu);
- wszystkie dataclasses są niemutowalne.
"""

from __future__ import annotations

import json
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


_T = TypeVar("_T")


def _convert(source: str, key: str, value: Any, expected: type) -> Any:
    where = f"{source}: {key}"
    if isinstance(value, float):
        raise ValueError(f"{where}: TOML float is not allowed, use string decimals (e.g. \"1.00\")")
    if expected is Decimal:
        if not isinstance(value, str):
            raise ValueError(f"{where}: use string decimals, got {type(value).__name__}")
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"{where}: {value!r} is not a decimal number") from None
        if not number.is_finite() or number < 0:
            raise ValueError(f"{where}: {value!r} must be a finite, non-negative decimal")
        return number
    if expected is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{where}: expected TOML bool (true/false), got {type(value).__name__}")
        return value
    if expected is int:
        # bool jest podklasą int, więc musi być odrzucone jawnie
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{where}: expected non-negative integer, got {value!r}")
        return value
    if expected is str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}: expected non-empty string, got {value!r}")
        return value
    raise TypeError(f"unsupported config field type {expected!r} for {where}")


def _load_table(path: Path, cls: type[_T]) -> _T:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
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


def _load_live_allowed(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
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
