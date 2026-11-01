"""Symulowany dzień silnika (Zadanie 14): rynek DE40 na `FakeBroker`, zegar `FakeClock`, budowa `Engine`.

Moduł pomocniczy, NIE conftest: pliki testów silnika importują stąd fixtures jawnie (`from tests.fault.engine_sim
import ...`), bo conftest w `tests/fault/` nadpisałby `clock` wszystkim testom awarii.

Dzień wzorcowy: poniedziałek 2026-11-02 (CET, UTC+1). Otwarcie 09:00 = 08:00 UTC, pierwsza świeca 09:00-09:05,
decyzja 09:05 = 08:05 UTC, termin wejścia 09:10, spłaszczenie 17:25 = 16:25 UTC, zamknięcie 17:30 = 16:30 UTC.
ATR14 = 300 pkt (świece dzienne z zakresem 300), więc stop = 0,10 × 300 = 30 pkt. Notowanie: spread 2.0, mid 25781.3
do 12:00 UTC, potem `late_mid` (domyślnie +30 pkt: zysk pozycji długiej).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gwstock.app import ARM_FILE, Engine, Mode
from gwstock.broker.fake import FakeBroker
from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import RuntimeConfig
from gwstock.journal import Journal
from gwstock.types import Candle, InstrumentRules, Quote

D = Decimal
CAL = ExchangeCalendar(XETRA)
DAY = date(2026, 11, 2)
EPIC = "DE40"


def berlin(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return CAL.local_to_utc(day, time(hh, mm, ss))


OPEN_UTC = berlin(DAY, 9, 0)
DECISION_UTC = berlin(DAY, 9, 5)
DEADLINE_UTC = berlin(DAY, 9, 10)
FLATTEN_UTC = berlin(DAY, 17, 25)
CLOSE_UTC = berlin(DAY, 17, 30)
START_UTC = berlin(DAY, 8, 45)  # przed przygotowaniem (08:50)
LATE_UTC = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MID = D("25781.3")
HALF_SPREAD = D("1.0")


def daily_candles(day: date, atr: Decimal = D("300"), count: int = 20, base: Decimal = D("25700")) -> list[Candle]:
    """`count` świec dziennych przed `day`; każda z zakresem `atr` i zamknięciem = otwarciem, więc ATR14 = `atr`."""
    half = atr / 2
    out = []
    for i in range(count, 0, -1):
        start = datetime.combine(day - timedelta(days=i), time(0, 0), tzinfo=timezone.utc)
        out.append(Candle(EPIC, start, base, base + half, base - half, base))
    return out


def five_minute_candles(day: date, first_open: Decimal, first_close: Decimal, *, include_first: bool = True):
    """Świece 5-minutowe wokół otwarcia; pierwsza (start = otwarcie sesji w UTC) ma podane otwarcie i zamknięcie."""
    open_utc = berlin(day, 9, 0)
    out = [Candle(EPIC, open_utc - timedelta(minutes=5), D("25779"), D("25783"), D("25777"), D("25780"))]
    if include_first:
        hi, lo = max(first_open, first_close) + 5, min(first_open, first_close) - 5
        out.append(Candle(EPIC, open_utc, first_open, hi, lo, first_close))
    for k in range(1, 12):
        out.append(Candle(EPIC, open_utc + timedelta(minutes=5 * k), D("25790"), D("25795"), D("25785"), D("25791")))
    return out


@dataclass
class Market:
    """Rynek symulowany: mid zależny od czasu (`mid_at`), spread 2.0, notowanie świeże (ts = teraz)."""

    late_mid: Decimal = MID + 30
    change_at: datetime = LATE_UTC
    frozen_from: datetime | None = None  # od tej chwili para (bid, ask) się nie zmienia (cisza notowań)
    overrides: list[tuple[datetime, Decimal]] = field(default_factory=list)

    def mid_at(self, now: datetime) -> Decimal:
        mid = MID if now < self.change_at else self.late_mid
        for when, value in self.overrides:
            if now >= when:
                mid = value
        return mid

    def quote(self, now: datetime) -> Quote:
        if self.frozen_from is not None and now >= self.frozen_from:
            mid = self.mid_at(self.frozen_from)
            return Quote(EPIC, mid - HALF_SPREAD, mid + HALF_SPREAD, self.frozen_from)
        mid = self.mid_at(now)
        return Quote(EPIC, mid - HALF_SPREAD, mid + HALF_SPREAD, now)


class RunawaySimulation(BaseException):
    """Zegar symulacji minął limit testu: pętla silnika nie zatrzymała się sama (test ma paść, nie wisieć).

    `BaseException`, żeby żadna siatka `except Exception` w silniku jej nie połknęła.
    """


class SimClock(FakeClock):
    """`FakeClock` z hakami wołanymi po każdym `sleep` i z limitem czasu, który silnik może przespać od ostatniego
    jawnego `advance` testu (przeskok testu o tygodnie nie jest pętlą bez końca, a silnik, który się nie zatrzymał,
    jest)."""

    def __init__(self, start: datetime, limit: timedelta = timedelta(days=8)) -> None:
        super().__init__(start)
        self.hooks: list[Callable[[], None]] = []
        self._limit = limit
        self._anchor = start
        self._sleeping = False

    def advance(self, seconds: float) -> None:
        super().advance(seconds)
        if not self._sleeping:
            self._anchor = self.now_utc()

    async def sleep(self, seconds: float) -> None:
        self._sleeping = True
        try:
            await super().sleep(seconds)
        finally:
            self._sleeping = False
        if self.now_utc() - self._anchor > self._limit:
            raise RunawaySimulation(f"engine slept more than {self._limit} since {self._anchor.isoformat()}")
        for hook in list(self.hooks):
            hook()


@pytest.fixture
def clock(request) -> SimClock:
    """Nadpisuje fixture z conftest w module, który go importuje: 08:45 w Berlinie w dniu wzorcowym; parametr
    pośredni (`indirect=["clock"]`) podaje inny start (np. dni zmiany czasu)."""
    return SimClock(getattr(request, "param", START_UTC))


@pytest.fixture
def market() -> Market:
    return Market()


@pytest.fixture
def day_rules() -> InstrumentRules:
    return InstrumentRules(EPIC, D("0.001"), D("0.001"), D("1"), D("0.05"), D("2.6"), D("0.1"))


@pytest.fixture
def broker(clock, day_rules, market) -> FakeBroker:
    """FakeBroker związany z rachunkiem ACC-1 (EUR, ENABLED, saldo 40); świeca 09:00 rosnąca (sygnał BUY)."""
    fake = FakeBroker(clock, day_rules, market.quote(clock.now_utc()), account_id="ACC-1")
    fake.set_quote_fn(market.quote)
    fake.set_daily_candles(daily_candles(DAY))
    fake.set_candles_5m(five_minute_candles(DAY, D("25780"), D("25790")))
    return fake


@pytest.fixture
def runtime() -> RuntimeConfig:
    return RuntimeConfig(live_allowed=True, account_id="ACC-1")


@pytest.fixture
def sim_root(tmp_path) -> Iterator[Path]:
    """Katalog symulacji: tmpfs (`/dev/shm`), jeśli jest, bo dzień to ~6000 zapisów z fsync; inaczej `tmp_path`.

    Dotyczy tylko testów: dziennik nadal pracuje w WAL + synchronous=FULL, tmpfs jedynie nie kosztuje fsync.
    """
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        root = Path(tempfile.mkdtemp(prefix="gwstock-sim-", dir=shm))
        yield root
        shutil.rmtree(root, ignore_errors=True)
    else:
        yield tmp_path


@pytest.fixture
def data_dir(sim_root) -> Path:
    """Nadpisuje fixture z conftest: `data/` symulacji (STOP, HARD_STOP, L1_PASSED, tętno, alarmy, blokada)."""
    path = sim_root / "data"
    path.mkdir()
    return path


@pytest.fixture
def journal_path(sim_root) -> Path:
    return sim_root / "journal.sqlite"


def arm(data_dir: Path, config_dir: Path) -> None:
    """Uzbrojenie symulacji tak, jak robi to WŁAŚCICIEL: plik uzbrojenia w `data/` i `live_allowed = true`.

    (Kod silnika nigdy nie tworzy tego pliku; tu tworzy go fixture testu w roli właściciela.)
    """
    (data_dir / ARM_FILE).write_text("")
    set_live_allowed(config_dir, True)


def bind(data_dir: Path, account_id: str | None) -> None:
    """Powiązanie rachunku tak, jak zapisuje je `bind-account` (None: brak powiązania)."""
    path = data_dir / "account.json"
    if account_id is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps({"account_id": account_id}))


def disarm(data_dir: Path) -> None:
    """Właściciel usuwa plik uzbrojenia."""
    (data_dir / ARM_FILE).unlink()


def set_live_allowed(config_dir: Path, flag: bool) -> None:
    (config_dir / "runtime.toml").write_text(f"live_allowed = {'true' if flag else 'false'}\n")


@pytest.fixture
def config_dir(sim_root) -> Path:
    """Katalog `config/` symulacji: kopie plików strategii, ryzyka i kosztów oraz `runtime.toml` właściciela."""
    path = sim_root / "config"
    path.mkdir()
    for name in ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml", "runtime.toml"):
        (path / name).write_text(Path("config", name).read_text())
    return path


class EngineFactory:
    """Buduje silnik na tym samym brokerze i (domyślnie) tym samym pliku dziennika; `restart()` zamyka dziennik
    i otwiera go na nowo z dysku (nowe obiekty: nic z pamięci poprzedniej instancji). Symulacja jest uzbrojona
    (plik uzbrojenia i `live_allowed = true`), więc tryb live działa, dopóki test go nie rozbroi."""

    def __init__(self, broker, clock, cfgs, data_dir, runtime, journal_path, notifier, config_dir) -> None:
        self.broker = broker
        self.clock = clock
        self.cfgs = cfgs
        self.data_dir = data_dir
        self.runtime = runtime
        self.journal_path = journal_path
        self.notifier = notifier
        self.config_dir = config_dir
        self.journals: list[Journal] = []
        arm(data_dir, config_dir)

    def __call__(self, mode: Mode = Mode.LIVE, **kw) -> Engine:
        journal = kw.pop("journal", None) or self.open_journal()
        cfgs = kw.pop("cfgs", self.cfgs)
        runtime = kw.pop("runtime", self.runtime)
        bind(self.data_dir, runtime.account_id)  # jak w produkcji: runtime.account_id pochodzi z data/account.json
        return Engine(
            kw.pop("broker", self.broker), journal, self.clock, kw.pop("calendar", CAL), cfgs, self.data_dir,
            runtime, mode, notifier=kw.pop("notifier", self.notifier),
            config_dir=kw.pop("config_dir", self.config_dir), **kw,
        )  # fmt: skip

    def open_journal(self) -> Journal:
        journal = Journal.open(str(self.journal_path))
        self.journals.append(journal)
        return journal

    def restart(self, old: Engine, mode: Mode = Mode.LIVE, **kw) -> Engine:
        old.journal.close()
        return self(mode, **kw)

    def close(self) -> None:
        for j in self.journals:
            try:
                j.close()
            except Exception:
                pass


@pytest.fixture
def cfgs(strat_cfg, risk_cfg, cost_cfg):
    return (strat_cfg, risk_cfg, cost_cfg)


@pytest.fixture
def make_engine(broker, clock, cfgs, data_dir, runtime, journal_path, notifier, config_dir):
    factory = EngineFactory(broker, clock, cfgs, data_dir, runtime, journal_path, notifier, config_dir)
    yield factory
    factory.close()


@pytest.fixture
def engine(make_engine) -> Engine:
    return make_engine(Mode.LIVE)


# --- pomocnicze --------------------------------------------------------------------------------------------


def at_time(broker: FakeBroker, clock: FakeClock, when: datetime, action: Callable[[], None], method: str = "quote"):
    """Wykonuje `action` raz, przy pierwszym wywołaniu `method` brokera o czasie `when` lub później."""
    done = {"v": False}

    def cb() -> None:
        if not done["v"] and clock.now_utc() >= when:
            done["v"] = True
            action()

    broker.on_call(method, cb)
    return done


def stop_engine_at(engine: Engine, clock: SimClock, when: datetime) -> None:
    """Prośba o zatrzymanie silnika po pierwszym `sleep`, który dociera do `when` (skraca test; działa także wtedy,
    gdy silnik w spoczynku nie woła brokera)."""

    def hook() -> None:
        if clock.now_utc() >= when:
            engine.request_shutdown()

    clock.hooks.append(hook)


def payloads(journal: Journal, source: str) -> list[dict]:
    return [json.loads(e.payload) for e in journal.events(source)]


def engine_decisions(journal: Journal) -> list[dict]:
    """Zdarzenia decyzji silnika (źródło `decision`, kod DECISION), bez zapisu `dry_run` wykonawcy."""
    return [p for p in payloads(journal, "decision") if p.get("event") == "DECISION"]


def calls_at(broker: FakeBroker, clock: FakeClock, method: str) -> list[datetime]:
    seen: list[datetime] = []
    broker.on_call(method, lambda: seen.append(clock.now_utc()))
    return seen


def with_risk(cfgs, **over):
    strat, risk, costs = cfgs
    return (strat, replace(risk, **over), costs)
