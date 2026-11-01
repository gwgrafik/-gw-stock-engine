"""Wspólne fixtures testów. Uruchamiać z korzenia repozytorium (ścieżka względna `config/`).

Kolejne zadania dopisują tu swoje fixtures (`journal`, `mgr`, `req`, `gate`, `kill`, `reconciler`, `signal`,
`executor`).
"""

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from gwstock.broker.capitalcom import CapitalComBroker, Credentials
from gwstock.broker.fake import FakeBroker
from gwstock.calendar import XETRA, ExchangeCalendar
from gwstock.clock import FakeClock
from gwstock.config import CostConfig, RiskConfig, StrategyConfig, load_config
from gwstock.costs import CostModel
from gwstock.execution import Executor
from gwstock.journal import Journal, PositionRecord
from gwstock.orders.manager import OrderManager
from gwstock.reconcile import Reconciler
from gwstock.risk import KillSwitch, RiskGate
from gwstock.strategy import EntrySignal
from gwstock.types import ClosedDeal, EntryRequest, InstrumentRules, Quote, Side
from tests.unit.fake_capital_server import TEST_API_KEY, TEST_IDENTIFIER, TEST_PASSWORD, FakeCapitalServer


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


@pytest.fixture
def journal(tmp_path) -> Iterator[Journal]:
    """Świeży dziennik SQLite w katalogu testu; zamykany po teście."""
    j = Journal.open(str(tmp_path / "journal.sqlite"))
    yield j
    j.close()


@pytest.fixture
def req() -> EntryRequest:
    """Wejście BUY DE40, wielkość 0.007, stop 30 pkt; identyfikator z dnia fixture `clock`."""
    return EntryRequest("gws-20261102-DE40-1-entry", "DE40", Side.BUY, Decimal("0.007"), Decimal("30"))


@pytest.fixture
def mgr(journal: Journal, fake: FakeBroker, clock: FakeClock, risk_cfg: RiskConfig) -> OrderManager:
    return OrderManager(journal, fake, clock, risk_cfg)


class RecordingKillSink:
    """Atrapa kill switcha (`KillSink`): zapisuje przyczyny kolejnych wywołań `soft`."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def soft(self, reason: str) -> None:
        self.calls.append(reason)


class RecordingBooker:
    """Atrapa księgującego (`TradeBooker`): zapisuje argumenty; z ustawionym `fail` rzuca go po zapisie."""

    def __init__(self) -> None:
        self.calls: list[tuple[PositionRecord, ClosedDeal]] = []
        self.fail: Exception | None = None

    async def book(self, position: PositionRecord, closed: ClosedDeal) -> None:
        self.calls.append((position, closed))
        if self.fail is not None:
            raise self.fail


@pytest.fixture
def kill_sink() -> RecordingKillSink:
    return RecordingKillSink()


@pytest.fixture
def booker() -> RecordingBooker:
    return RecordingBooker()


@pytest.fixture
def reconciler(
    journal: Journal,
    fake: FakeBroker,
    clock: FakeClock,
    kill_sink: RecordingKillSink,
    risk_cfg: RiskConfig,
    booker: RecordingBooker,
) -> Reconciler:
    """Uzgadnianie dla DE40 z atrapami kill switcha i księgującego, bez bramki wejścia."""
    return Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40", booker=booker)


class RecordingNotifier:
    """Atrapa `Notifier`: zapisuje alarmy jako (poziom, treść); z ustawionym `fail` rzuca go po zapisie."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.fail: Exception | None = None

    def alert(self, level: str, message: str) -> None:
        self.alerts.append((level, message))
        if self.fail is not None:
            raise self.fail


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def calendar() -> ExchangeCalendar:
    return ExchangeCalendar(XETRA)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    """Katalog `data/` testu (tu leżą pliki `STOP` i `HARD_STOP`)."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def kill(journal: Journal, clock: FakeClock, data_dir: Path, notifier: RecordingNotifier) -> KillSwitch:
    return KillSwitch(journal, clock, data_dir, notifier)


@pytest.fixture
def gate(
    risk_cfg: RiskConfig,
    strat_cfg: StrategyConfig,
    journal: Journal,
    calendar: ExchangeCalendar,
    clock: FakeClock,
    kill: KillSwitch,
) -> RiskGate:
    """Bramka ryzyka z żywym kill switchem; domyślny `clock` to 08:30 w Berlinie, czyli przed oknem wejścia."""
    return RiskGate(risk_cfg, strat_cfg, journal, calendar, clock, kill)


@pytest.fixture
def signal(quote: Quote) -> EntrySignal:
    """Sygnał BUY DE40: stop 30 pkt, wielkość 0.007 (ryzyko 0,21 EUR), notowanie decyzji = fixture `quote`."""
    return EntrySignal(Side.BUY, Decimal("30"), Decimal("0.007"), quote)


@pytest.fixture
def executor(
    mgr: OrderManager,
    fake: FakeBroker,
    journal: Journal,
    gate: RiskGate,
    kill: KillSwitch,
    cost_cfg: CostConfig,
    clock: FakeClock,
    strat_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    calendar: ExchangeCalendar,
    notifier: RecordingNotifier,
) -> Executor:
    """Wykonawca w trybie LIVE (wysyła zlecenia do FakeBroker): prawdziwy menedżer, bramka, kill switch i koszty.

    Domyślny `clock` (08:30 w Berlinie) jest przed oknem wejścia: testy wejścia nadpisują `clock`.
    """
    return Executor(
        mgr, fake, journal, gate, kill, CostModel(cost_cfg), clock, strat_cfg, risk_cfg, calendar, notifier, live=True
    )


@pytest.fixture
async def fake_capital_server(clock: FakeClock) -> AsyncIterator[FakeCapitalServer]:
    """Lokalny serwer udający REST API Capital.com na 127.0.0.1 (zegar testu = fixture `clock`)."""
    server = FakeCapitalServer(clock)
    await server.start()
    yield server
    await server.close()


@pytest.fixture
def capital_credentials() -> Credentials:
    """Dane logowania, które zna `fake_capital_server`; bez związanego rachunku (jak `bind-account`)."""
    return Credentials(api_key=TEST_API_KEY, identifier=TEST_IDENTIFIER, api_password=TEST_PASSWORD)


@pytest.fixture
def capital_sent() -> list[tuple[str, str, datetime]]:
    """Wysłane żądania adaptera jako (metoda, ścieżka względem /api/v1, zegar testu w chwili wysyłki)."""
    return []


@pytest.fixture
async def capital_http(clock: FakeClock, capital_sent: list) -> AsyncIterator[aiohttp.ClientSession]:
    """Sesja HTTP adaptera; każde wysłane żądanie trafia do `capital_sent` z czasem zegara testu."""

    async def on_request_start(session, context, params) -> None:
        capital_sent.append((params.method, params.url.path.removeprefix("/api/v1"), clock.now_utc()))

    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(on_request_start)
    async with aiohttp.ClientSession(trace_configs=[trace]) as session:
        yield session


@pytest.fixture
def capital_broker(
    fake_capital_server: FakeCapitalServer,
    capital_credentials: Credentials,
    capital_http: aiohttp.ClientSession,
    clock: FakeClock,
) -> CapitalComBroker:
    """Adapter Capital.com na lokalnym serwerze testowym, z zegarem testu; niezalogowany."""
    return CapitalComBroker(capital_credentials, capital_http, clock, base_url=fake_capital_server.base_url)


# --- strażnik live: żaden test nie tworzy prawdziwych plików uzbrojenia w `<repo>/data` -------------------------------------

ARMING_FILE_NAMES = ("ARM_LIVE", "L1_PASSED")


def arming_files_created(data_dir: Path, existed_before: frozenset[str]) -> list[str]:
    """Pliki uzbrojenia, które pojawiły się w `data_dir` w trakcie przebiegu (te sprzed przebiegu, np. właściciela, nie)."""
    return [name for name in ARMING_FILE_NAMES if name not in existed_before and (data_dir / name).exists()]


@pytest.fixture(scope="session", autouse=True)
def _no_test_creates_the_real_arming_files():
    """Test, który zapomniał o tmp_path i uzbroił prawdziwy `data/`, ma wywalić przebieg (spec §9: uzbraja tylko właściciel).

    Sprawdzamy tylko wtedy, gdy `<repo>/data` istnieje (VPS, dev po uruchomieniu silnika); plik, który istniał przed
    przebiegiem (utworzył go właściciel), nie jest błędem.
    """
    data_dir = Path(__file__).resolve().parents[1] / "data"
    if not data_dir.is_dir():
        yield
        return
    before = frozenset(name for name in ARMING_FILE_NAMES if (data_dir / name).exists())
    yield
    created = arming_files_created(data_dir, before)
    assert not created, f"testy utworzyły prawdziwe pliki uzbrojenia w {data_dir}: {created} (uzbraja tylko właściciel)"
