"""Ładowanie konfiguracji z plików TOML (`config/`) i stanu powiązania rachunku (`data/account.json`).

Zasady:
- kwoty i ułamki w TOML są napisami ("1.00") parsowanymi do Decimal; liczba TOML-float jest odrzucana;
  napis to wyłącznie zwykły zapis dziesiętny `cyfry` albo `cyfry.cyfry` (ASCII, bez spacji, `_`, wykładnika i plusa);
- liczniki i sekundy są liczbami całkowitymi, flagi są wartościami bool TOML;
- granice (sprawdzane przy wczytaniu, nie w konstruktorach): pola `*_s`, `max_entries_per_day`, `range_minutes`,
  `bar_minutes`, `lookback_bars`, `time_limit_min`, `target_r`, `max_open_positions`, `unknown_position_risk`
  oraz kwoty i dźwignia ryzyka (`virtual_capital`, `max_*`) są > 0 (to dzielniki, okresy i limity), pola `*_fraction`
  i `fx_markup` leżą w [0, 1], pozostałe liczby są >= 0 (zero ma tam sens według specyfikacji); pola filtrów dnia pasma
  świecowego (`atr_period`, `day_stop_r`, `min_stop_fraction`, `slippage_filter_atr_fraction`, `max_round_trip_cost_r`)
  są > 0, bo zero wyłączałoby filtr albo odrzucałoby każde wejście;
- brakujący klucz, nieznany klucz, zły typ lub poza granicą to ValueError z nazwą pliku i klucza (literówka nie
  przechodzi po cichu); plik TOML, którego nie da się odczytać, to ValueError z nazwą pliku;
- wszystkie dataclasses są niemutowalne.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, fields
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    """Parametry pasma świecowego B1: jeden plik na pasmo, np. `lane_us500_b1_m5.toml`.

    Pola do `fixed_min_size_sessions` to czysta reguła sygnału i wielkości (`strategy_bars`). Pozostałe to zakres reguł
    `run()` z `research/candle_strategies.py`, który pasmo dokłada do sygnału (`lanes.decide_entry`, `lanes.exit_due`):

    - `session_timezone`, `signal_windows` ("GG:MM-GG:MM,..." czasu lokalnego, początek świecy sygnału w [od, do)),
      `flatten_before_close_min` (zaplanowane wyjście: zamknięcie kalendarza pasma minus tyle minut);
    - `atr_period` (ATR ze świec M5 do filtra poślizgu), `gap_tolerance_bars` (ile świec może brakować w rozpiętości
      zakresu, zanim dane to `GAPPED_DATA`);
    - `max_entries_per_day`, `day_stop_r` (WARTOŚĆ BEZWZGLĘDNA straty dnia w R, wynik dnia <= -`day_stop_r` kończy dzień);
    - `min_stop_fraction` (stop bliżej niż ten ułamek ceny wejścia), `slippage_filter_atr_fraction` (cena wejścia dalej
      od zamknięcia świecy sygnału niż ten ułamek ATR), `max_round_trip_cost_r` (koszt rundy w R),
      `assumed_slippage_points` (ZAŁOŻONY poślizg wejścia i wyjścia w punktach, jak `GW_SLIP` w badaniach).
    """

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
    session_timezone: str
    signal_windows: str
    flatten_before_close_min: int
    atr_period: int
    gap_tolerance_bars: int
    max_entries_per_day: int
    day_stop_r: Decimal
    min_stop_fraction: Decimal
    slippage_filter_atr_fraction: Decimal
    max_round_trip_cost_r: Decimal
    assumed_slippage_points: Decimal


@dataclass(frozen=True)
class PortfolioConfig:
    """Limity portfela (R3, spec wielu pasm §4.4) z `config/portfolio.toml`: wspólne dla wszystkich pasm.

    Kwoty w EUR. Plik leży POZA odciskiem pre-rejestracji v1 (`prereg.CONFIG_FILES`). `load_portfolio` odmawia
    wartości łagodniejszych od `risk_v1.toml` (zob. `PORTFOLIO_VS_RISK`).
    """

    max_open_positions: int
    max_open_risk: Decimal
    max_daily_loss: Decimal
    max_total_drawdown: Decimal
    max_effective_leverage: Decimal
    virtual_capital: Decimal
    margin_reserve_fraction: Decimal
    unknown_position_risk: Decimal
    fx_quote_epic: str
    fx_quote_max_age_s: int


PORTFOLIO_FILE = "portfolio.toml"
RISK_FILE = "risk_v1.toml"

# Spójność portfela z v1 (spec §4.4): (pole portfela, pole `risk_v1.toml`, kierunek). "max": wartość portfela nie
# większa od v1 (limit, kapitał, dźwignia; suma ryzyka otwartego nie większa od dziennego limitu straty, rozstrzygnięcie
# właściciela-kontrolera R9); "min": nie mniejsza (rezerwa depozytu, ryzyko pozycji o nieznanym stopie).
PORTFOLIO_VS_RISK: tuple[tuple[str, str, str], ...] = (
    ("max_open_risk", "max_daily_loss", "max"),
    ("max_daily_loss", "max_daily_loss", "max"),
    ("max_total_drawdown", "max_total_drawdown", "max"),
    ("max_effective_leverage", "max_effective_leverage", "max"),
    ("virtual_capital", "virtual_capital", "max"),
    ("margin_reserve_fraction", "margin_reserve_fraction", "min"),
    ("unknown_position_risk", "max_loss_per_trade", "min"),
)


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
        "max_open_positions",
        "max_open_risk",
        "unknown_position_risk",
        "range_minutes",
        "bar_minutes",
        "lookback_bars",
        "time_limit_min",
        "target_r",
        "atr_period",
        "day_stop_r",
        "min_stop_fraction",
        "slippage_filter_atr_fraction",
        "max_round_trip_cost_r",
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


# Godzina lokalna "GG:MM" (dokładnie dwie cyfry ASCII, bez spacji): okna sygnałów pasma.
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def _parse_clock(text: str) -> time:
    match = _CLOCK.fullmatch(text)
    if match is None:
        raise ValueError(f"signal_windows: {text!r} is not a clock time HH:MM")
    return time(int(match.group(1)), int(match.group(2)))


def parse_signal_windows(text: str) -> tuple[tuple[time, time], ...]:
    """Okna sygnałów "GG:MM-GG:MM,GG:MM-GG:MM" jako pary czasu lokalnego [od, do), rosnąco i bez nakładania.

    Zapis ścisły: bez spacji, dwie cyfry ASCII na godzinę i minutę, `od` < `do`, kolejne okno zaczyna się nie wcześniej
    niż kończy się poprzednie (styk dozwolony), co najmniej jedno okno. Błąd to ValueError z nazwą klucza
    `signal_windows`; wywołujący dopisuje nazwę pliku.
    """
    if not isinstance(text, str) or not text:
        raise ValueError(f"signal_windows: expected a non-empty string, got {text!r}")
    windows: list[tuple[time, time]] = []
    for part in text.split(","):
        bounds = part.split("-")
        if len(bounds) != 2:
            raise ValueError(f"signal_windows: {part!r} is not a window HH:MM-HH:MM")
        start, end = _parse_clock(bounds[0]), _parse_clock(bounds[1])
        if not start < end:
            raise ValueError(f"signal_windows: window {part!r} must start before it ends")
        if windows and start < windows[-1][1]:
            raise ValueError(f"signal_windows: window {part!r} overlaps or precedes the previous window")
        windows.append((start, end))
    return tuple(windows)


def load_bar_rule_config(path: Path) -> BarRuleConfig:
    """Wczytuje plik pasma świecowego (np. `config/lane_us500_b1_m5.toml`), ściśle jak pliki v1.

    Brak pliku to FileNotFoundError; brakujący lub nieznany klucz, zły typ i wartość poza granicą to ValueError z nazwą
    pliku i klucza. Dodatkowo `session_timezone` musi być znaną strefą IANA, a `signal_windows` poprawnym zapisem okien
    (`parse_signal_windows`). Osobne od `load_config`: pliki v1 i ich odcisk pre-rejestracji się nie zmieniają.
    """
    path = Path(path)
    cfg = _load_table(path, BarRuleConfig)
    try:
        ZoneInfo(cfg.session_timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"{path.name}: session_timezone: unknown time zone {cfg.session_timezone!r}") from None
    try:
        parse_signal_windows(cfg.signal_windows)
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from None
    return cfg


def load_portfolio(config_dir: Path, risk: RiskConfig | None = None) -> PortfolioConfig:
    """Wczytuje `config_dir/portfolio.toml` ściśle jak pliki v1 i sprawdza spójność z limitami v1.

    `risk`: limity v1 do porównania; bez niego czytane z `config_dir/risk_v1.toml` (ściśle). Brak pliku to
    FileNotFoundError. Brakujący lub nieznany klucz, zły typ, wartość poza granicą i wartość ŁAGODNIEJSZA od
    `risk_v1.toml` (zob. `PORTFOLIO_VS_RISK`) to ValueError z nazwą pliku i klucza: łagodniejszy próg portfela byłby
    martwy (wykonawca DE40 eskaluje kill switch na globalnym wyniku z progami v1) i mylący. Równy przechodzi.
    """
    config_dir = Path(config_dir)
    cfg = _load_table(config_dir / PORTFOLIO_FILE, PortfolioConfig)
    if risk is None:
        risk = _load_table(config_dir / RISK_FILE, RiskConfig)
    for key, risk_key, bound in PORTFOLIO_VS_RISK:
        mine, theirs = getattr(cfg, key), getattr(risk, risk_key)
        if (bound == "max" and mine > theirs) or (bound == "min" and mine < theirs):
            relation = "greater than" if bound == "max" else "less than"
            raise ValueError(
                f"{PORTFOLIO_FILE}: {key}: {mine} is more lenient than {RISK_FILE} {risk_key} = {theirs} "
                f"(portfolio limits must not be {relation} the v1 limits)"
            )
    return cfg


LANES_FILE = "lanes.toml"
LANE_RULES = frozenset({"bar_b1_m5"})  # wtyki kolejnych pasm; pasmo DE40 orb_index_cfd v1 jest zawsze i nie ma wpisu
LANE_EXCHANGES = frozenset({"NYSE", "XETRA"})  # nazwy profili `gwstock.calendar`
_FILE_NAME = re.compile(r"[A-Za-z0-9_.-]+\.toml")
_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class LaneConfig:
    """Wpis `[[lane]]` w `config/lanes.toml`: kolejne pasmo obok DE40 v1 (projekt wielu pasm §1, R1, R5).

    - `rule`: wtyk reguły (`bar_b1_m5`); `rule_file`: plik reguły w `config/` (instrument pasma pochodzi z niego);
    - `exchange_profile`: kalendarz pasma (`NYSE`, `XETRA`); `currency`: waluta instrumentu (ISO 4217);
    - `slippage_guard_fraction`: straż poślizgu wejścia; `max_loss_per_trade`: limit straty transakcji w EUR (nie
      łagodniejszy od `risk_v1.toml`); `costs_file`: model kosztów pasma w `config/` (format `costs_v1.toml`).
    """

    rule: str
    rule_file: str
    exchange_profile: str
    currency: str
    slippage_guard_fraction: Decimal
    max_loss_per_trade: Decimal
    costs_file: str


@dataclass(frozen=True)
class LoadedLane:
    """Pasmo z `lanes.toml` z wczytanymi plikami reguły i kosztów."""

    cfg: LaneConfig
    bar: BarRuleConfig
    costs: CostConfig

    @property
    def epic(self) -> str:
        return self.bar.instrument


def load_lanes(config_dir: Path) -> tuple[LoadedLane, ...] | None:
    """Kolejne pasma z `config_dir/lanes.toml` albo None (brak pliku albo `enabled = false`): wtedy silnik pracuje
    wyłącznie z pasmem DE40 v1, dokładnie jak dotąd. Plik leży POZA odciskiem pre-rejestracji v1.

    Ściśle jak pliki v1: klucze najwyższego poziomu `enabled` (bool) i `lane` (tablica tabel), każdy wpis z kluczami
    `LaneConfig`; nazwy plików tylko w `config_dir` (bez ścieżek); `max_loss_per_trade` nie większy od
    `risk_v1.toml`; instrument różny od DE40 v1 i niepowtarzalny (R1). Błąd to ValueError z nazwą pliku i klucza.
    Włączenie pasma NIE uzbraja go: pasmo bez zamrożonej pre-rejestracji pracuje najwyżej jako dry_run (silnik).
    """
    config_dir = Path(config_dir)
    path = config_dir / LANES_FILE
    if not path.exists():
        return None
    raw = _read_toml(path)
    unknown = sorted(set(raw) - {"enabled", "lane"})
    if unknown:
        raise ValueError(f"{LANES_FILE}: unknown key(s): {', '.join(unknown)}")
    if "enabled" not in raw:
        raise ValueError(f"{LANES_FILE}: missing key(s): enabled")
    if not _convert(LANES_FILE, "enabled", raw["enabled"], bool):
        return None
    entries = raw.get("lane")
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        raise ValueError(f"{LANES_FILE}: lane: expected at least one [[lane]] table when enabled = true")
    strat, risk, _ = load_config(config_dir)
    hints = get_type_hints(LaneConfig)
    names = [f.name for f in fields(LaneConfig)]
    lanes: list[LoadedLane] = []
    for n, entry in enumerate(entries, start=1):
        where = f"{LANES_FILE} lane {n}"
        extra = sorted(set(entry) - set(names))
        if extra:
            raise ValueError(f"{where}: unknown key(s): {', '.join(extra)}")
        missing = [k for k in names if k not in entry]
        if missing:
            raise ValueError(f"{where}: missing key(s): {', '.join(missing)}")
        cfg = LaneConfig(**{k: _convert(where, k, entry[k], hints[k]) for k in names})
        if cfg.rule not in LANE_RULES:
            raise ValueError(f"{where}: rule: {cfg.rule!r} is not one of {sorted(LANE_RULES)}")
        if cfg.exchange_profile not in LANE_EXCHANGES:
            raise ValueError(f"{where}: exchange_profile: {cfg.exchange_profile!r} is not one of {sorted(LANE_EXCHANGES)}")
        if not _CURRENCY.fullmatch(cfg.currency):
            raise ValueError(f"{where}: currency: expected a 3-letter ISO 4217 code, got {cfg.currency!r}")
        if cfg.slippage_guard_fraction <= 0:
            raise ValueError(f"{where}: slippage_guard_fraction: must be > 0")
        if cfg.max_loss_per_trade > risk.max_loss_per_trade:
            raise ValueError(
                f"{where}: max_loss_per_trade: {cfg.max_loss_per_trade} is more lenient than {RISK_FILE} "
                f"max_loss_per_trade = {risk.max_loss_per_trade}"
            )
        for key in ("rule_file", "costs_file"):
            if not _FILE_NAME.fullmatch(getattr(cfg, key)) or getattr(cfg, key).startswith("."):
                raise ValueError(f"{where}: {key}: expected a .toml file name in the config directory")
        bar = load_bar_rule_config(config_dir / cfg.rule_file)
        costs = _load_table(config_dir / cfg.costs_file, CostConfig)
        if bar.instrument == strat.instrument:
            raise ValueError(f"{where}: instrument {bar.instrument} is the v1 lane ({strat.instrument}); one lane each")
        if any(other.epic == bar.instrument for other in lanes):
            raise ValueError(f"{where}: duplicate lane for {bar.instrument} (R1: at most one lane per instrument)")
        lanes.append(LoadedLane(cfg, bar, costs))
    return tuple(lanes)


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
