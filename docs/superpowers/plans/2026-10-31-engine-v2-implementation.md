# Silnik v2 (CFD na indeksy, Capital.com demo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zbudować autonomiczny silnik, który raz dziennie handluje US100 (ORB) na koncie demo Capital.com, za każdym razem potwierdza stan u brokera, egzekwuje twarde limity ryzyka i mierzy zysk netto po kosztach.

**Architecture:** Jeden proces asyncio, jeden plik SQLite (WAL). Rdzeń (dziennik, automat stanów, uzgadnianie, ryzyko, kill switch, koszty, księga) jest niezależny od brokera i testowany na `FakeBroker` z wstrzykiwaniem awarii. Capital.com to jeden z wymiennych adapterów interfejsu `Broker`.

**Tech Stack:** Python 3.12 (CI także 3.13), `aiohttp` (jedyna zależność wykonania), standardowy `sqlite3`, `tomllib`, `zoneinfo`; `pytest`, `pytest-asyncio`, `pyyaml` tylko w środowisku deweloperskim.

**Spec:** `docs/superpowers/specs/2026-10-31-engine-design-v2.md` (czytaj razem z planem; numery sekcji `§` odnoszą się do niej).

## Global Constraints

- Kod i identyfikatory po angielsku, dokumentacja po polsku. Kwoty i ceny jako `Decimal`, nigdy `float`.
- Python 3.12; jedyna zależność wykonania `aiohttp` (przypięta wersja w `requirements.lock`); każda dodatkowa wymaga uzasadnienia i pomiaru pamięci (§3).
- Czas w bazie w UTC (ISO 8601); logika sesji w `America/New_York` przez `zoneinfo`, **bez stałych przesunięć** (§4).
- Zlecenie nie wychodzi bez wiersza INTENT zapisanego z `PRAGMA synchronous=FULL` (§6, niezmiennik 1).
- Host brokera to stała `https://demo-api-capital.backend-capital.com`; **w kodzie nie istnieje ścieżka live** (§9, §13, kryterium 7).
- Wartości startowe (§5): `virtual_capital` 1000; `max_loss_per_trade` 5; `max_daily_loss` 10; `max_effective_leverage` 5; `max_entries_per_day` 1; stop `0,10 × ATR14`; stop ≥ `3 × spread`; straż poślizgu 25% odległości stopu; spłaszczenie 15:55 ET (12:55 ET w skrócone sesje); cisza notowań 60 s; tolerancja rozbieżności 60 s; uzgadnianie co 10 s; ping co 5 min; timeout `SUBMITTING` 10 s; instrument `US100`.
- Koszty (§10): poślizg 0,5 pkt na wejściu i dodatkowo 1,0 pkt przy wyjściu stopem; brak prowizji; finansowanie 0 (brak pozycji przez noc); stawki w `config/costs_v1.toml`.
- Pamięć kontenera (§13): `mem_limit` 256m, `memswap_limit` 256m, `mem_reservation` 128m, `oom_score_adj` 1000, `pids_limit`, system plików tylko do odczytu.
- Żadnych sekretów w repo ani w CI. Klucz leży w `/secrets/capitalcom_demo.json` na VPS.
- Wyjść (zamknięć pozycji) nigdy nie blokuje bramka ryzyka (§9).
- Repo jest publiczne do czasu decyzji właściciela: nie commituj niczego, czego nie chcesz publikować.

## Review Focus

Wejścia i awarie, które spec implikuje, a które łatwo pominąć (test każdej linii jest dopisany w zadaniu, które posiada kod):

1. **Start po godzinie spłaszczenia z otwartą pozycją** (restart o 16:30 ET): spodziewane natychmiastowe spłaszczenie i brak nowego wejścia (Zadanie 14).
2. **Brak lub spóźniona pierwsza świeca** o 09:35 (luka strumienia): spodziewany wpis `NO_SIGNAL_DATA` i brak wejścia, nie wejście na starych danych (Zadanie 11 i 14).
3. **Awaria zapisu do dziennika** (pełny dysk, zablokowana baza): spodziewane, że zlecenie **nie** wychodzi i włącza się kill switch miękki (Zadanie 7).
4. **Druga instancja silnika** (nakładający się restart): spodziewana odmowa startu zamiast drugiej sesji brokera (Zadanie 14).
5. **Broker potwierdza inną wielkość niż zażądana** (zaokrąglenie do kroku, częściowe wykonanie): spodziewane zapisanie faktycznej wielkości, przeliczenie ryzyka i stop dla faktycznej wielkości (Zadanie 7).

---

## Struktura plików

```
pyproject.toml, requirements.lock, CLAUDE.md, .gitignore
config/            strategy_v1.toml, risk_v1.toml, costs_v1.toml
src/gwstock/
  config.py        wczytywanie TOML do niemutowalnych dataclass
  types.py         enumy, dataclass domenowe, wyjątki brokera
  clock.py         Clock, SystemClock, FakeClock
  calendar.py      kalendarz NYSE (tabela statyczna), konwersje ET/UTC
  journal.py       SQLite WAL: zdarzenia surowe, zlecenia, pozycje, ryzyko, kill, transakcje
  prereg.py        odcisk SHA-256 konfiguracji, adaptera i klasy instrumentu
  broker/          base.py (Broker, Capabilities), fake.py, capitalcom.py
  orders/          fsm.py, ids.py, manager.py
  reconcile.py     Reconciler
  risk.py          RiskGate, KillSwitch
  costs.py         CostModel
  strategy.py      atr14, decide, Skip, EntrySignal
  execution.py     Executor (wejście, wyjście, spłaszczenie)
  watchdog.py      Watchdog, InstanceLock
  app.py           Engine (pętla dnia), main
  report/          ledger.py, gates.py, cli.py
tests/             unit/, fault/, integration_demo/
docker/            Dockerfile, compose.yml
docs/ops/          capitalcom-api-notes.md, compose-notes.md, runbook.md
.github/workflows/ci.yml, .claude/ (hooki, agenci), scripts/hooks/guard_live.py
```

Zależności zadań: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18. Zadania 9–11 mogą biec równolegle po 8; 13 po 4 i 12.

---

### Task 1: Rozpoznanie dokumentacji (Capital.com API i Docker Compose)

Spec (§13, §16 pyt. 7, 8, 12) wymaga tego **przed** kodem adaptera i wdrożenia.

**Files:**
- Create: `docs/ops/capitalcom-api-notes.md`, `docs/ops/compose-notes.md`

**Interfaces:**
- Produces: dokładne ścieżki, nagłówki i pola (np. endpoint potwierdzenia `dealReference`, historia aktywności i transakcji, historia cen, pozycje, konto), na które powołują się Zadania 4, 8, 13; potwierdzenie pól Compose.

- [ ] **Step 1: Odczytaj publiczną dokumentację API Capital.com** (`https://open-api.capital.com/`) i zapisz w `capitalcom-api-notes.md` dla każdej operacji: metodę i pełną ścieżkę, nagłówki, pola żądania i odpowiedzi, kody błędów. Operacje: `session/encryptionKey`, `session`, `ping`, `positions` (POST, GET, DELETE, PUT stop), potwierdzenie po `dealReference`, `markets/{epic}` (reguły: min wielkość, krok, wartość punktu, depozyt, minimalna odległość stopu, status rynku), historia cen (5-minutowe i dzienne), historia aktywności i transakcji (pola: cena i przyczyna zamknięcia przez stop), `accounts`, strumień OHLC (subskrypcja, ping).
- [ ] **Step 2: Zweryfikuj w dokumentacji Docker Compose** pola `mem_limit`, `memswap_limit`, `mem_reservation`, `oom_score_adj`, `pids_limit`, `read_only`, `tmpfs` oraz ich zachowanie przy cgroup v2; zapisz w `compose-notes.md` cytaty z linkami i polecenie `docker compose config` do walidacji na VPS.
- [ ] **Step 3: Oznacz każdą pozycję** jako `udokumentowane` albo `do potwierdzenia na demo` (Zadanie 18).
- [ ] **Step 4: Weryfikacja** — `grep -c "udokumentowane\|do potwierdzenia" docs/ops/capitalcom-api-notes.md` zwraca liczbę ≥ liczby operacji z Kroku 1; żadnego klucza ani hasła w plikach: `grep -rniE "api[_-]?key *[:=] *[A-Za-z0-9]{16,}" docs/ops` nic nie zwraca.
- [ ] **Step 5: Commit** — `git add docs/ops && git commit -m "docs: notatki o API Capital.com i Compose"`

---

### Task 2: Scaffolding i konfiguracja

**Files:**
- Create: `pyproject.toml`, `requirements.lock`, `.gitignore`, `CLAUDE.md`, `src/gwstock/__init__.py`, `src/gwstock/config.py`, `config/strategy_v1.toml`, `config/risk_v1.toml`, `config/costs_v1.toml`, `tests/unit/test_config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `StrategyConfig(instrument: str, stop_atr_fraction: Decimal, min_stop_spread_multiple: Decimal, slippage_guard_fraction: Decimal, flatten_before_close_min: int, entry_deadline_after_range_min: int)` (ostatnie pole dodaje plan, wartość 5: wejście możliwe od 09:35 do 09:40 ET; **dopisz je do tabeli wartości startowych w §5 specyfikacji**)
  - `RiskConfig(virtual_capital: Decimal, max_loss_per_trade: Decimal, max_daily_loss: Decimal, max_effective_leverage: Decimal, max_entries_per_day: int, stale_quote_s: int, mismatch_tolerance_s: int, reconcile_interval_s: int, ping_interval_s: int, submit_timeout_s: int)`
  - `CostConfig(entry_slippage_points: Decimal, stop_exit_extra_slippage_points: Decimal, fx_markup: Decimal, apply_fx_markup: bool)`
  - `load_config(config_dir: Path) -> tuple[StrategyConfig, RiskConfig, CostConfig]`

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal
from pathlib import Path
from gwstock.config import load_config

def test_config_holds_spec_starting_values():
    s, r, c = load_config(Path("config"))
    assert s.instrument == "US100"
    assert s.stop_atr_fraction == Decimal("0.10")
    assert s.min_stop_spread_multiple == Decimal("3")
    assert s.slippage_guard_fraction == Decimal("0.25")
    assert s.flatten_before_close_min == 5 and s.entry_deadline_after_range_min == 5
    assert r.virtual_capital == Decimal("1000")
    assert (r.max_loss_per_trade, r.max_daily_loss) == (Decimal("5"), Decimal("10"))
    assert r.max_effective_leverage == Decimal("5") and r.max_entries_per_day == 1
    assert (r.stale_quote_s, r.mismatch_tolerance_s, r.reconcile_interval_s,
            r.ping_interval_s, r.submit_timeout_s) == (60, 60, 10, 300, 10)
    assert c.entry_slippage_points == Decimal("0.5")
    assert c.stop_exit_extra_slippage_points == Decimal("1.0")

def test_load_config_rejects_float_money(tmp_path):
    ...  # kopia config/ z wartością `max_loss_per_trade = 5.0` jako liczba TOML-float → ValueError("use string decimals")
```

- [ ] **Step 2: Run** `pytest tests/unit/test_config.py -v` — Expected: FAIL (moduł nie istnieje).
- [ ] **Step 3: Implement `load_config`** w `config.py` (wartości w TOML jako napisy, np. `max_loss_per_trade = "5"`, parsowane do `Decimal`; liczby TOML-float odrzucane). Utwórz trzy pliki TOML z wartościami z Global Constraints. `pyproject.toml`: pakiet `gwstock` w `src/`, znacznik pytest `integration_demo`, `asyncio_mode = "auto"`. `CLAUDE.md`: zasady pracy (TDD, brak ścieżki live, wartości tylko z konfiguracji, nie wklejać kluczy). `requirements.lock` z `pip freeze` po instalacji.
- [ ] **Step 4: Run** `pytest tests/unit/test_config.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** — `git add pyproject.toml requirements.lock .gitignore CLAUDE.md src config tests && git commit -m "feat: scaffolding i konfiguracja"`

---

### Task 3: Zegar i kalendarz NYSE

**Files:**
- Create: `src/gwstock/clock.py`, `src/gwstock/calendar.py`, `tests/unit/test_clock_calendar.py`

**Interfaces:**
- Consumes: —
- Produces:
  - `class Clock(Protocol): def now_utc(self) -> datetime; async def sleep(self, seconds: float) -> None`
  - `SystemClock()`, `FakeClock(start: datetime)` (`sleep` przesuwa czas bez czekania; `advance(seconds: float)`; `elapsed_since_start() -> float`)
  - `NyseCalendar`: `holidays(year: int) -> frozenset[date]`, `is_session_day(d: date) -> bool`, `close_time_et(d: date) -> time` (16:00 lub 13:00), `flatten_time_et(d: date, before_close_min: int) -> time`, `et_to_utc(d: date, t: time) -> datetime`, `to_et(dt_utc: datetime) -> datetime`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date, time, datetime, timezone
from gwstock.calendar import NyseCalendar

cal = NyseCalendar()

def test_dst_boundary_us_2026():
    assert cal.et_to_utc(date(2026, 10, 30), time(9, 30)) == datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)
    assert cal.et_to_utc(date(2026, 11, 2), time(9, 30)) == datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)

def test_holidays_and_weekends_are_not_session_days():
    for d in (date(2026, 11, 26), date(2026, 12, 25), date(2026, 7, 3), date(2026, 4, 3), date(2026, 11, 28)):
        assert not cal.is_session_day(d)

def test_early_close_flattens_at_1255():
    assert cal.close_time_et(date(2026, 11, 27)) == time(13, 0)
    assert cal.flatten_time_et(date(2026, 11, 27), 5) == time(12, 55)
    assert cal.flatten_time_et(date(2026, 11, 30), 5) == time(15, 55)

def test_table_covers_2026_and_2027_and_is_consistent():
    for year in (2026, 2027):
        hol = cal.holidays(year)
        assert len(hol) >= 9 and all(d.weekday() < 5 for d in hol)
```

- [ ] **Step 2: Run** `pytest tests/unit/test_clock_calendar.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `calendar.py` z **tabelą statyczną 2026–2027** (święta i skrócone sesje przepisz z oficjalnego kalendarza NYSE, adres w komentarzu nad tabelą; test spójności wyżej chroni przed literówką) i konwersjami przez `zoneinfo.ZoneInfo("America/New_York")`. `FakeClock.sleep` ma tylko zwiększać czas wewnętrzny.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/clock.py src/gwstock/calendar.py tests/unit/test_clock_calendar.py && git commit -m "feat: zegar i kalendarz NYSE"`

---

### Task 4: Typy domenowe, interfejs Broker i FakeBroker

**Files:**
- Create: `src/gwstock/types.py`, `src/gwstock/broker/__init__.py`, `src/gwstock/broker/base.py`, `src/gwstock/broker/fake.py`, `tests/conftest.py`, `tests/unit/test_fake_broker.py`, `tests/unit/broker_contract.py`

**Interfaces:**
- Produces (`types.py`): `Side(str, Enum)` BUY/SELL; `OrderState(str, Enum)` INTENT, SUBMITTING, UNKNOWN, CONFIRMED, REJECTED, NOT_PLACED, WORKING, PARTIAL, CANCELING, CANCELED, EXPIRED; `PositionState(str, Enum)` FLAT, ENTERING, OPEN, EXITING, BLOCKED; `KillLevel(IntEnum)` NONE=0, SOFT=1, HARD=2; frozen dataclasses `Quote(epic, bid, ask, ts)` (właściwości `mid`, `spread`), `Candle(epic, start, open, high, low, close)` (ceny mid, `start` w UTC), `InstrumentRules(epic, min_size, size_step, value_per_point, margin_rate, min_stop_distance)`, `OpenPosition(deal_id, epic, side, size, open_level, stop_level, opened_at)`, `ClosedDeal(deal_id, epic, exit_level, reason, ts)` (reason: `"STOP"`, `"MANUAL"`, `"FORCED"`), `AccountInfo(equity, available, margin_used)`, `EntryRequest(client_id, epic, side, size, stop_distance)`, `EntryAck(deal_reference)`; wyjątki `BrokerError`, `BrokerTimeout`, `BrokerRejected(reason)`, `SessionExpired`, `MarketClosed`.
- Produces (`broker/base.py`): `Capabilities(stop_attach: str, idempotency_key: str | None, keepalive_s: int, reconcile: str, is_cfd: bool)` oraz `Broker(Protocol)` z metodami: `capabilities`, `async login() -> None`, `async keepalive() -> None`, `async instrument_rules(epic) -> InstrumentRules`, `async quote(epic) -> Quote`, `async daily_candles(epic, count: int) -> list[Candle]`, `async candles_5m(epic, start: datetime, end: datetime) -> list[Candle]`, `async open_position(req: EntryRequest) -> EntryAck`, `async confirm(deal_reference: str) -> OpenPosition | None`, `async list_positions() -> list[OpenPosition]`, `async close_position(deal_id: str) -> None`, `async set_stop(deal_id: str, stop_level: Decimal) -> None`, `async closed_since(since: datetime) -> list[ClosedDeal]`, `async account() -> AccountInfo`.
- Produces (`broker/fake.py`): `FakeBroker(clock: Clock, rules: InstrumentRules, quote: Quote)` ze sterowaniem: `set_quote(q)`, `fail_next(method: str, exc: Exception, applied: bool = False)` (`applied=True`: operacja skutkuje u brokera, ale klient widzi wyjątek, czyli timeout po przyjęciu), `on_call(method: str, callback: Callable[[], None])` (wywoływane tuż przed wykonaniem metody), `trigger_stop(deal_id, level)`, `expire_session()`, `set_market_closed(flag: bool)`, `fill_size_factor: Decimal = 1` (broker potwierdza `size × factor`), atrybuty `positions: dict[str, OpenPosition]`, `calls: list[str]`.
- Produces (`tests/unit/broker_contract.py`): klasa `BrokerContract` z testami wspólnymi dla każdego adaptera (używana w Zadaniu 13).
- Produces (`tests/conftest.py`): fixtures `clock` (`FakeClock` na 2026-11-02 14:00 UTC), `quote` (bid 24999.1, ask 25000.9), `rules` (US100: min_size 0.1, step 0.1, vpp 1, margin 0.05, min_stop 10), `fake`, `risk_cfg`, `strat_cfg`, `cost_cfg`. Kolejne zadania dopisują potrzebne fixtures (`journal`, `mgr`, `req`, `gate`, `kill`, `reconciler`).

- [ ] **Step 1: Write the failing tests** (`test_fake_broker.py`)

```python
async def test_open_position_attaches_stop_atomically(fake, quote):
    ack = await fake.open_position(EntryRequest("gws-20261102-US100-1-entry", "US100", Side.BUY, Decimal("0.1"), Decimal("30")))
    pos = await fake.confirm(ack.deal_reference)
    assert pos.stop_level == quote.ask - Decimal("30") and pos.size == Decimal("0.1")

async def test_timeout_after_acceptance_leaves_position_open(fake):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    with pytest.raises(BrokerTimeout):
        await fake.open_position(req)
    assert len(await fake.list_positions()) == 1

async def test_stop_trigger_removes_position_and_reports_closed_deal(fake):
    ...  # trigger_stop → list_positions() == [] i closed_since(t0) zawiera ClosedDeal(reason="STOP", exit_level=level)
```

- [ ] **Step 2: Run** `pytest tests/unit/test_fake_broker.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** typy, `Broker`/`Capabilities` i `FakeBroker` (stan w pamięci, wszystkie metody zapisują nazwę w `calls`; `capabilities = Capabilities("atomic", None, 600, "positions", True)`). `BrokerContract`: testy login/keepalive, otwarcie ze stopem, `confirm`, `list_positions`, `close_position`, odrzucenie przy `set_market_closed(True)` (`MarketClosed`).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/types.py src/gwstock/broker tests/unit && git commit -m "feat: typy, interfejs Broker i FakeBroker"`

---

### Task 5: Dziennik SQLite

**Files:**
- Create: `src/gwstock/journal.py`, `tests/unit/test_journal.py`

**Interfaces:**
- Consumes: `OrderState`, `PositionState`, `KillLevel`, `Side` z `types.py`.
- Produces:
  - `OrderRecord(order_id, epic, side, size, stop_distance, state, deal_reference, deal_id, created_at, updated_at, note)`; `PositionRecord(epic, state, deal_id, side, size, open_level, stop_level, opened_at)`; `KillState(level, reason, since)`; `TradeRecord(instrument, side, size, decision_bid, decision_ask, entry_fill, exit_fill, gross_pnl, spread_cost, slippage_penalty, financing, fees, net_pnl, signal_ts, submit_ts, confirm_ts, exit_ts, latency_ms, exit_reason)`.
  - `Journal.open(path: str) -> Journal` (ustawia `journal_mode=WAL`, `synchronous=FULL`); `pragma(name: str)`; `close() -> None`; `append_raw(source: str, payload: str, ts: datetime) -> int`; `insert_order(o: OrderRecord) -> None`; `update_order(order_id: str, **fields) -> OrderRecord`; `get_order(order_id: str) -> OrderRecord | None`; `unresolved_orders() -> list[OrderRecord]` (SUBMITTING, UNKNOWN); `upsert_position(p: PositionRecord) -> None`; `current_position(epic: str) -> PositionRecord | None`; `add_realized_pnl(exit_day: date, net: Decimal) -> None`; `realized_pnl(day: date) -> Decimal`; `set_kill(level: KillLevel, reason: str, ts: datetime) -> None`; `kill_state() -> KillState`; `record_trade(t: TradeRecord) -> None`; `trades() -> list[TradeRecord]`; wyjątek `JournalWriteError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pragmas(tmp_path):
    j = Journal.open(str(tmp_path / "j.sqlite"))
    assert j.pragma("journal_mode") == "wal" and j.pragma("synchronous") == 2  # FULL

def test_daily_loss_survives_reopen_and_is_keyed_by_exit_day(tmp_path):
    p = str(tmp_path / "j.sqlite")
    j = Journal.open(p); j.add_realized_pnl(date(2026, 11, 2), Decimal("-3.5")); j.close()
    assert Journal.open(p).realized_pnl(date(2026, 11, 2)) == Decimal("-3.5")

def test_kill_state_survives_reopen(tmp_path): ...   # SOFT zapisany, po ponownym otwarciu nadal SOFT z przyczyną

def test_write_failure_raises_journal_write_error(tmp_path, monkeypatch):
    ...  # zablokowana/pełna baza (monkeypatch kursora na sqlite3.OperationalError) → JournalWriteError

def test_raw_event_is_stored_before_anything_else(tmp_path): ...  # append_raw zwraca rosnące id
```

- [ ] **Step 2: Run** `pytest tests/unit/test_journal.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `Journal` z jednym połączeniem i jednym wątkiem zapisu (`threading.Lock`); schemat tworzony przy `open`; każda operacja zapisu w transakcji; `sqlite3.Error` opakowany w `JournalWriteError`; kwoty jako napisy `Decimal`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/journal.py tests/unit/test_journal.py && git commit -m "feat: dziennik SQLite (WAL, FULL)"`

---

### Task 6: Automat stanów i identyfikatory

**Files:**
- Create: `src/gwstock/orders/__init__.py`, `src/gwstock/orders/fsm.py`, `src/gwstock/orders/ids.py`, `tests/unit/test_fsm_ids.py`

**Interfaces:**
- Consumes: `OrderState`, `PositionState`.
- Produces: `ALLOWED_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]]`; `check_order_transition(old: OrderState, new: OrderState) -> None` (rzuca `InvalidTransition(Exception)`, zdefiniowany w `fsm.py`); `ALLOWED_POSITION_TRANSITIONS`, `check_position_transition(...)`; `make_client_id(day: date, instrument: str, seq: int, role: str) -> str` w formacie `gws-{YYYYMMDD}-{instrument}-{seq}-{role}` (≤ 128 znaków).

- [ ] **Step 1: Write the failing tests**

```python
def test_spec_transitions():
    ok = [(INTENT, SUBMITTING), (SUBMITTING, CONFIRMED), (SUBMITTING, REJECTED), (SUBMITTING, UNKNOWN),
          (UNKNOWN, CONFIRMED), (UNKNOWN, NOT_PLACED)]
    for a, b in ok: check_order_transition(a, b)
    for a, b in [(INTENT, CONFIRMED), (CONFIRMED, SUBMITTING), (NOT_PLACED, CONFIRMED), (REJECTED, UNKNOWN)]:
        with pytest.raises(InvalidTransition): check_order_transition(a, b)

def test_terminal_states_have_no_exits():
    for s in (CONFIRMED, REJECTED, NOT_PLACED): assert not ALLOWED_ORDER_TRANSITIONS[s]

def test_position_flow(): ...   # FLAT→ENTERING→OPEN→EXITING→FLAT; OPEN→BLOCKED; FLAT→OPEN niedozwolone

def test_client_id_format_and_uniqueness():
    a = make_client_id(date(2026, 11, 2), "US100", 1, "entry")
    assert a == "gws-20261102-US100-1-entry" and len(a) <= 128
    assert a != make_client_id(date(2026, 11, 2), "US100", 2, "entry")
```

- [ ] **Step 2: Run** `pytest tests/unit/test_fsm_ids.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** tabele przejść zgodnie z §6 (stany końcowe CONFIRMED, REJECTED, NOT_PLACED bez wyjść; pełny zestaw WORKING/PARTIAL/CANCELING/CANCELED/EXPIRED dopuszczony tylko w przejściach między sobą, nieużywany w v1).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/orders tests/unit/test_fsm_ids.py && git commit -m "feat: automat stanów zleceń i identyfikatory"`

---

### Task 7: Menedżer zleceń (wejście, UNKNOWN, błędy zapisu)

**Files:**
- Create: `src/gwstock/orders/manager.py`, `tests/fault/test_order_manager.py`

**Interfaces:**
- Consumes: `Journal`, `Broker`/`FakeBroker`, `Clock`, `RiskConfig`, `make_client_id`, `check_order_transition`, `EntryRequest`.
- Produces: `OrderManager(journal: Journal, broker: Broker, clock: Clock, cfg: RiskConfig)` z `async submit_entry(req: EntryRequest) -> OrderRecord` (zwraca rekord w stanie CONFIRMED, REJECTED lub NOT_PLACED; `PositionRecord` ustawiony na OPEN po CONFIRMED) i `async resolve_unknown(order: OrderRecord) -> OrderRecord`. Wyjątek `EntryBlocked(reason: str)`.

- [ ] **Step 1: Write the failing tests** (`tests/fault/test_order_manager.py`)

```python
async def test_intent_row_exists_before_broker_call(journal, fake, mgr, req):
    seen = {}
    fake.on_call("open_position", lambda: seen.setdefault("state", journal.get_order(req.client_id).state))
    await mgr.submit_entry(req)
    assert seen["state"] == OrderState.SUBMITTING   # INTENT zapisany i przeszedł w SUBMITTING przed wywołaniem

async def test_timeout_then_position_found_is_adopted_not_duplicated(journal, fake, mgr, req):
    fake.fail_next("open_position", BrokerTimeout(), applied=True)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.CONFIRMED and fake.calls.count("open_position") == 1

async def test_timeout_without_position_becomes_not_placed_after_three_checks(journal, fake, mgr, req, clock):
    fake.fail_next("open_position", BrokerTimeout(), applied=False)
    rec = await mgr.submit_entry(req)
    assert rec.state == OrderState.NOT_PLACED and fake.calls.count("list_positions") == 3
    assert clock.elapsed_since_start() >= 30   # trzy zapytania w 30 s

async def test_rejected_entry_is_terminal_and_logged(...): ...   # BrokerRejected("MIN_STOP_DISTANCE") → REJECTED, note z powodem

async def test_no_second_entry_while_unknown(...):  # drugi submit_entry dla tego samego epic w stanie SUBMITTING/UNKNOWN → EntryBlocked

async def test_journal_write_failure_means_no_order_is_sent(journal, fake, mgr, req, monkeypatch):   # Review Focus 3
    monkeypatch.setattr(journal, "insert_order", lambda *_: (_ for _ in ()).throw(JournalWriteError()))
    with pytest.raises(JournalWriteError):
        await mgr.submit_entry(req)
    assert "open_position" not in fake.calls

async def test_broker_confirms_different_size_records_actual_size(...):   # Review Focus 5
    fake.fill_size_factor = Decimal("0.5")
    rec = await mgr.submit_entry(req)
    assert journal.current_position("US100").size == req.size * Decimal("0.5")
```

- [ ] **Step 2: Run** `pytest tests/fault/test_order_manager.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement `OrderManager.submit_entry`**: INTENT (zapis) → `SUBMITTING` → `broker.open_position`; po `EntryAck` `confirm(deal_reference)`; `BrokerTimeout` → `UNKNOWN` → `resolve_unknown`: do 3 odczytów `list_positions` co 10 s (`clock.sleep`), dopasowanie po epic, kierunku i `opened_at ≥ created_at`; brak → `NOT_PLACED`. Każda surowa odpowiedź trafia do `append_raw` przed przetworzeniem. Faktyczną wielkość z potwierdzenia zapisz w `PositionRecord`. Błąd `JournalWriteError` propaguje się bez wywołania brokera.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/orders/manager.py tests/fault && git commit -m "feat: menedżer zleceń z rozstrzyganiem UNKNOWN"`

---

### Task 8: Uzgadnianie z brokerem

**Files:**
- Create: `src/gwstock/reconcile.py`, `tests/fault/test_reconcile.py`

**Interfaces:**
- Consumes: `Journal`, `Broker`, `Clock`, `RiskConfig`, `KillSwitch` (Zadanie 9 dostarcza klasę; w tym zadaniu użyj `Protocol` `KillSink` z metodą `soft(reason: str) -> None`, którą `KillSwitch` spełni).
- Produces: `Reconciler(journal, broker, clock, kill: KillSink, cfg: RiskConfig)`; `async run_once() -> ReconcileResult`; `ReconcileResult(blocked: bool, notes: list[str], closed: list[ClosedDeal], unprotected: list[OpenPosition])`.

- [ ] **Step 1: Write the failing tests** — jeden test na każdy wiersz tabeli z §7:

```python
async def test_position_adopted_when_local_unknown(...):      # broker ma pozycję, zlecenie UNKNOWN → pozycja OPEN, order CONFIRMED
async def test_foreign_position_blocks_and_triggers_soft_kill(...):   # brak śladu → blocked=True, kill.soft wywołane, pozycji nie dotykamy (brak close_position w fake.calls)
async def test_local_position_missing_at_broker_books_stop(...):      # trigger_stop → closed zawiera reason="STOP", PositionState FLAT
async def test_local_position_missing_without_activity_is_blocked(...)
async def test_size_mismatch_blocks(...)
async def test_working_order_on_our_epic_is_flagged(...)
async def test_unprotected_position_is_reported(...):                 # stop_level=None → result.unprotected == [pos]
async def test_mismatch_older_than_60s_triggers_soft_kill(...)        # FakeClock +61 s bez rozwiązania → kill.soft
```

- [ ] **Step 2: Run** `pytest tests/fault/test_reconcile.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement `Reconciler.run_once`**: `list_positions` + `closed_since(ostatni znacznik)`; macierz reakcji z §7; zamknięcie przez stop księgowane z `ClosedDeal` (cena i przyczyna); znacznik czasu rozbieżności w dzienniku do liczenia 60 s; każda surowa odpowiedź przez `append_raw`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/reconcile.py tests/fault/test_reconcile.py && git commit -m "feat: uzgadnianie z brokerem"`

---

### Task 9: Bramka ryzyka i kill switch

**Files:**
- Create: `src/gwstock/risk.py`, `src/gwstock/notify.py`, `tests/unit/test_risk.py`, `tests/fault/test_kill_switch.py`

**Interfaces:**
- Consumes: `RiskConfig`, `Journal`, `NyseCalendar`, `Clock`, `Quote`, `AccountInfo`, `KillLevel`.
- Produces: `EntryCandidate(epic, side, size, stop_distance, entry_price, value_per_point, margin_rate, quote_age_s)`; `Verdict(passed: bool, reasons: list[str])`; `RiskGate(cfg, journal, calendar, clock)` z `check_entry(c: EntryCandidate, account: AccountInfo) -> Verdict` i `check_exit() -> Verdict` (zawsze `passed=True`); `KillSwitch(journal, clock, data_dir: Path, notifier: Notifier)` z `level() -> KillLevel`, `soft(reason: str)`, `hard(reason: str)`, `clear(requested_by: str)`, `scan_files() -> None` (`data/STOP` → soft, `data/HARD_STOP` → hard). Powody w `Verdict.reasons` to stałe napisy: `FORMAT`, `NO_STOP`, `LOSS_PER_TRADE`, `LEVERAGE`, `MARGIN`, `ENTRIES_PER_DAY`, `DAILY_LOSS`, `NOT_SESSION`, `OUTSIDE_WINDOW`, `DUPLICATE`, `STALE_DATA`, `KILL`. Okno wejścia to od 09:35 ET do 09:35 + `entry_deadline_after_range_min` (i nigdy po spłaszczeniu).
- Produces (`notify.py`): `Notifier(Protocol)` z `alert(level: str, message: str) -> None`; `FileNotifier(path: Path)` dopisuje linie `{ts} {level} {message}` do pliku alarmów (§13).

- [ ] **Step 1: Write the failing tests**

```python
def test_loss_check_blocks_when_over_limit():
    v = gate.check_entry(cand(size=Decimal("0.2"), stop_distance=Decimal("30"), value_per_point=Decimal("1")), acct)
    assert not v.passed and "LOSS_PER_TRADE" in v.reasons       # 0.2 × 30 = 6 > 5

def test_leverage_check():  # notional 25000 × 0.3 = 7500 > 5 × 1000
    ...  "LEVERAGE" in reasons

def test_one_entry_per_day_and_daily_loss(): ...   # po wejściu "ENTRIES_PER_DAY"; realized -10 → "DAILY_LOSS"

def test_window_and_holiday():   # 2026-11-26 → "NOT_SESSION"; 09:34 ET i 09:41 ET → "OUTSIDE_WINDOW"; 09:36 ET → przechodzi; 2026-11-27 (skrócona sesja) 09:36 ET → przechodzi

def test_kill_switch_writes_alert_line(tmp_path): ...   # kill.soft("x") → FileNotifier dopisał linię z "x" i poziomem SOFT

def test_stale_quote_blocks(): ...   # quote_age_s=61 → "STALE_DATA"

def test_exits_are_never_blocked_even_when_kill_is_hard():
    kill.hard("test"); assert gate.check_exit().passed

def test_kill_level_persists_and_clear_requires_explicit_action(journal, clock, tmp_path): ...

def test_stop_file_triggers_soft_and_hard_file_triggers_hard(tmp_path): ...
```

- [ ] **Step 2: Run** `pytest tests/unit/test_risk.py tests/fault/test_kill_switch.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** kontrole z tabeli §9 (powody jako stałe napisy użyte w testach), kolejność jak w tabeli; kill switch zapisuje poziom w dzienniku; `clear` wymaga niepustego `requested_by` i zapisuje wpis audytowy. Brak automatycznego zdejmowania.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/risk.py tests && git commit -m "feat: bramka ryzyka i kill switch"`

---

### Task 10: Model kosztów

**Files:**
- Create: `src/gwstock/costs.py`, `tests/unit/test_costs.py`

**Interfaces:**
- Consumes: `CostConfig`, `Side`.
- Produces: `TradeFacts(side, size, value_per_point, mid_entry, mid_exit, entry_fill, exit_fill, spread_entry, spread_exit, exit_reason: str, nights_held: int)`; `CostBreakdown(gross_pnl, spread_cost, slippage_penalty, financing, fees, net_pnl)`; `CostModel(cfg: CostConfig).compute(t: TradeFacts) -> CostBreakdown`.
- Zasady (§10): `gross_pnl` liczone na cenach **mid** (decyzji i wyjścia); `spread_cost = max(koszt zrealizowany w wypełnieniach, koszt modelowy z notowań)`; `net_pnl = gross_pnl − spread_cost − slippage_penalty − financing − fees`.

- [ ] **Step 1: Write the failing tests**

```python
def facts(**kw):
    base = dict(side=Side.BUY, size=Decimal("0.2"), value_per_point=Decimal("1"),
                mid_entry=Decimal("25000.0"), mid_exit=Decimal("25030.0"),
                entry_fill=Decimal("25000.9"), exit_fill=Decimal("25029.1"),
                spread_entry=Decimal("1.8"), spread_exit=Decimal("1.8"),
                exit_reason="FORCED", nights_held=0)
    return TradeFacts(**{**base, **kw})

def test_time_exit_net():
    b = model.compute(facts())
    assert b.gross_pnl == Decimal("6.00") and b.spread_cost == Decimal("0.36")
    assert b.slippage_penalty == Decimal("0.10") and b.net_pnl == Decimal("5.54")

def test_stop_exit_adds_extra_slippage():
    b = model.compute(facts(exit_reason="STOP"))
    assert b.slippage_penalty == Decimal("0.30")         # (0.5 + 1.0) × 0.2

def test_spread_cost_never_below_quoted_spread_when_fills_are_at_mid():
    b = model.compute(facts(entry_fill=Decimal("25000.0"), exit_fill=Decimal("25030.0")))
    assert b.spread_cost == Decimal("0.36")

def test_short_trade_sign(): ...   # SELL: gross = (mid_entry − mid_exit) × size
def test_financing_is_zero_without_nights_and_fx_markup_off_by_default(): ...
```

- [ ] **Step 2: Run** `pytest tests/unit/test_costs.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement `CostModel.compute`** wg zasad powyżej; zaokrąglenie do 0,01 (`ROUND_HALF_UP`).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/costs.py tests/unit/test_costs.py && git commit -m "feat: model kosztów"`

---

### Task 11: Strategia ORB

**Files:**
- Create: `src/gwstock/strategy.py`, `tests/unit/test_strategy.py`

**Interfaces:**
- Consumes: `Candle`, `Quote`, `InstrumentRules`, `StrategyConfig`, `RiskConfig`, `Side`.
- Produces: `atr14(daily: list[Candle]) -> Decimal` (średnia prostych z ostatnich 14 prawdziwych zakresów TR = max(H−L, |H−Cprev|, |L−Cprev|); wymaga 15 świec); `EntrySignal(side, stop_distance, size, decision_quote: Quote)`; `Skip(reason: str)`; `decide(first: Candle | None, atr: Decimal, quote: Quote, rules: InstrumentRules, strat: StrategyConfig, risk: RiskConfig) -> EntrySignal | Skip`; `round_down_to_step(value: Decimal, step: Decimal) -> Decimal`.
- Powody `Skip`: `NO_SIGNAL_DATA`, `DOJI`, `STOP_TOO_TIGHT`, `SKIPPED_MIN_SIZE`.

- [ ] **Step 1: Write the failing tests**

```python
def test_long_when_first_candle_bullish_and_size_rounds_down():
    s = decide(candle(o="25000", c="25010"), Decimal("300"), quote("24999.1", "25000.9"),
               rules(min_size="0.1", step="0.1", vpp="1", min_stop="10"), strat, risk)
    assert s.side == Side.BUY and s.stop_distance == Decimal("30.0") and s.size == Decimal("0.1")
    # min(5/(30×1)=0.1666, 5×1000/25000=0.2) → 0.1666 → krok 0.1 → 0.1

def test_short_when_bearish(): ...
def test_doji_is_skipped(): assert decide(candle(o="25000", c="25000"), ...) == Skip("DOJI")
def test_missing_first_candle_is_no_signal_data():     # Review Focus 2
    assert decide(None, Decimal("300"), q, r, strat, risk) == Skip("NO_SIGNAL_DATA")
def test_stop_not_smaller_than_broker_minimum():
    ... min_stop="40" → stop_distance == Decimal("40")
def test_stop_tighter_than_three_spreads_is_skipped():   # ATR 30 → stop 3.0 < 3 × 1.8
    ... == Skip("STOP_TOO_TIGHT")
def test_below_minimum_size_is_skipped(): ... min_size="0.5" → Skip("SKIPPED_MIN_SIZE")
def test_atr14_matches_hand_computed_value(): ...   # 15 świec o stałym zakresie 100 → Decimal("100")
```

- [ ] **Step 2: Run** `pytest tests/unit/test_strategy.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** reguły 2–7 z §5: kierunek z koloru świecy (zamknięcie > otwarcie → BUY, < → SELL, równe → DOJI); `stop = max(stop_atr_fraction × ATR, rules.min_stop_distance)`; odrzucenie, gdy `stop < min_stop_spread_multiple × quote.spread`; `size = round_down_to_step(min(max_loss / (stop × vpp), max_effective_leverage × virtual_capital / quote.mid), step)`; poniżej `min_size` → `SKIPPED_MIN_SIZE`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/strategy.py tests/unit/test_strategy.py && git commit -m "feat: strategia ORB"`

---

### Task 12: Wykonanie wejścia, wyjścia i spłaszczenia

**Files:**
- Create: `src/gwstock/execution.py`, `tests/fault/test_execution.py`

**Interfaces:**
- Consumes: `OrderManager`, `Broker`, `Journal`, `RiskGate`, `KillSwitch`, `CostModel`, `Clock`, `StrategyConfig`, `EntrySignal`, `InstrumentRules`, `make_client_id`.
- Produces: `Executor(manager, broker, journal, gate, kill, cost_model, clock, strat, risk)`; `async enter(signal: EntrySignal, rules: InstrumentRules, day: date) -> OrderRecord | None` (None gdy bramka zablokuje; powód zapisany); `async flatten(position: OpenPosition, reason: str) -> ClosedDeal | None`; `async emergency_flatten() -> None`; `async protect_unprotected(position: OpenPosition) -> bool` (ustawia stop; po porażce kill twardy).

- [ ] **Step 1: Write the failing tests**

```python
async def test_entry_goes_through_risk_gate_and_attaches_stop(...): ...    # pozycja ze stopem == ask − stop_distance
async def test_slippage_guard_closes_position_when_fill_deviates_over_25pct_of_stop(...):
    # fill 10 pkt gorszy przy stop_distance 30 (> 7.5) → close_position wywołane, zdarzenie SLIPPAGE_GUARD w dzienniku
async def test_flatten_closes_and_books_trade_with_costs(...):             # TradeRecord z net_pnl z CostModel
async def test_flatten_when_stop_already_hit_books_stop_not_second_close(...):   # trigger_stop w trakcie → brak drugiego close_position
async def test_flatten_during_market_closed_keeps_stop_and_retries(...):   # MarketClosed → pozycja EXITING, alarm, brak porzucenia
async def test_flatten_timeout_reconciles_instead_of_resending(...):       # BrokerTimeout(applied=True) → pozycja zniknęła → księgowanie, 1 close_position
async def test_unprotected_position_gets_stop_else_hard_kill(...)
async def test_entry_is_blocked_when_kill_active(...)
```

- [ ] **Step 2: Run** `pytest tests/fault/test_execution.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** procedury z §8: `enter` → `gate.check_entry` → `manager.submit_entry` → porównanie ceny wykonania z notowaniem decyzji (straż poślizgu `slippage_guard_fraction × stop_distance`); `flatten` → `EXITING` → `close_position`; przy timeoucie lub odmowie najpierw `Reconciler`-owy odczyt `list_positions`, nigdy ponowna wysyłka na ślepo; po zamknięciu `TradeRecord` z `CostModel.compute` i `journal.add_realized_pnl(exit_day, net)`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/execution.py tests/fault/test_execution.py && git commit -m "feat: wykonanie wejścia, wyjścia i spłaszczenia"`

---

### Task 13: Adapter Capital.com (demo)

**Files:**
- Create: `src/gwstock/broker/capitalcom.py`, `tests/unit/test_capitalcom_adapter.py`, `tests/integration_demo/test_capitalcom_demo.py`

**Interfaces:**
- Consumes: `Broker`, `BrokerContract` (Zadanie 4), `docs/ops/capitalcom-api-notes.md` (Zadanie 1: ścieżki i pola), `Clock`.
- Produces: `DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"`; `Credentials(api_key: str, identifier: str, api_password: str)` z `load_credentials(path: Path) -> Credentials`; `CapitalComBroker(creds: Credentials, session: aiohttp.ClientSession, clock: Clock, base_url: str = DEMO_BASE_URL)` implementujący `Broker`; `ValueError` przy `base_url != DEMO_BASE_URL`.

- [ ] **Step 1: Write the failing tests** — adapter wobec lokalnego serwera `aiohttp.web` udającego API (fixture `fake_capital_server`, bez nowych zależności):

```python
def test_refuses_any_host_other_than_demo():
    with pytest.raises(ValueError):
        CapitalComBroker(creds, session, clock, base_url="https://api-capital.backend-capital.com")

async def test_login_stores_cst_and_security_token_and_sends_them(...): ...
async def test_401_triggers_one_serialized_relogin_then_retry(...):          # dwa równoległe żądania → jedno POST /session
async def test_open_position_sends_stop_distance_atomically(...):            # treść żądania zawiera stopDistance, size, direction, epic
async def test_timeout_maps_to_broker_timeout_and_rejection_to_broker_rejected(...)
async def test_orders_respect_100ms_spacing(...):                            # FakeClock: dwa open_position → odstęp ≥ 0.1 s
async def test_keepalive_calls_ping_with_session_tokens(...)
class TestCapitalComContract(BrokerContract): ...                            # ten sam zestaw co FakeBroker
```

`integration_demo` (znacznik `integration_demo`, pomijane bez pliku klucza): `test_instrument_rules_us100_loadable`, `test_open_and_close_position_with_stop_on_demo`.

- [ ] **Step 2: Run** `pytest tests/unit/test_capitalcom_adapter.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement `CapitalComBroker`** wg `capitalcom-api-notes.md`: logowanie (`encryptionKey` + `POST /session`; nagłówki `CST` i `X-SECURITY-TOKEN`), jedno logowanie naraz (`asyncio.Lock`), ponowienie raz po 401, limiter 5 zapytań/s ogólnie (połowa limitu brokera, §13) i 1 zlecenie na 0,1 s, `ping` zgodnie z `keepalive_s`, mapowanie błędów HTTP i timeoutów na wyjątki z `types.py`, ceny i wielkości jako `Decimal`. `capabilities = Capabilities("atomic", None, 600, "positions", True)`. Nie przełączaj kont (`PUT /session`). Odpowiedź otwarcia pozycji zwraca `EntryAck(deal_reference)`.
- [ ] **Step 4: Run** `pytest tests/unit/test_capitalcom_adapter.py -v` — Expected: PASS. Zweryfikuj `grep -rn "api-capital.backend-capital.com" src | grep -v demo-api` — Expected: brak wyników.
- [ ] **Step 5: Commit** — `git add src/gwstock/broker/capitalcom.py tests && git commit -m "feat: adapter Capital.com demo"`

---

### Task 14: Watchdog, blokada instancji i pętla dnia

**Files:**
- Create: `src/gwstock/watchdog.py`, `src/gwstock/app.py`, `tests/fault/test_watchdog.py`, `tests/fault/test_engine_day.py`

**Interfaces:**
- Consumes: wszystkie moduły z Zadań 2–13.
- Produces: `InstanceLock(path: Path)` (`acquire()` rzuca `AlreadyRunning`); `Watchdog(clock, journal, kill, broker, risk, heartbeat_path: Path)` z `async tick() -> None` (cisza notowań > 60 s, wygasła sesja bez odnowienia, trzy odrzucenia z rzędu → `kill.soft`; zapis tętna); `Engine(broker, journal, clock, calendar, cfgs, data_dir)` z `async run_day(day: date) -> DayReport` i `async run_forever() -> None`; `DayReport(entries: int, final_position_state: PositionState, net_pnl: Decimal | None, skip_reason: str | None)`; `main()` w `python -m gwstock` (składa `FileNotifier(data_dir / "alerts.log")`, `KillSwitch`, `Reconciler`, `Executor` i `Engine`).

- [ ] **Step 1: Write the failing tests** (symulowany dzień na `FakeBroker` i `FakeClock`)

```python
async def test_full_day_long_entry_flat_at_1555_and_report(...):
    rep = await engine.run_day(date(2026, 11, 2))
    assert rep.entries == 1 and rep.final_position_state == PositionState.FLAT and rep.net_pnl is not None

async def test_no_entry_on_holiday_and_on_early_close_after_flatten(...)
async def test_restart_mid_entry_adopts_position_without_duplicate(...):      # nowa instancja Engine na tym samym dzienniku i brokerze
async def test_stop_hit_by_broker_is_booked_once(...)
async def test_start_after_flatten_time_with_open_position_flattens_and_does_not_enter(...):   # Review Focus 1 (16:30 ET)
async def test_missing_first_candle_at_0935_means_no_entry_and_logged(...):                    # Review Focus 2
async def test_second_instance_is_refused(tmp_path):                                           # Review Focus 4
    InstanceLock(tmp_path / "engine.lock").acquire()
    with pytest.raises(AlreadyRunning): InstanceLock(tmp_path / "engine.lock").acquire()
async def test_quote_silence_61s_triggers_soft_kill_and_blocks_entry(...)
async def test_session_expiry_triggers_relogin_then_soft_kill_when_login_fails(...)
async def test_engine_pings_every_300s_during_session(...)
```

- [ ] **Step 2: Run** `pytest tests/fault/test_watchdog.py tests/fault/test_engine_day.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** oś czasu z §4 (przygotowanie → zakres otwarcia → decyzja po 09:35 ET (czekanie na pierwszą świecę najdłużej `entry_deadline_after_range_min` minut, potem `NO_SIGNAL_DATA`) → nadzór co `reconcile_interval_s` → spłaszczenie o `flatten_time_et` → rekoncyliacja końcowa); uzgadnianie przy starcie **przed** jakimkolwiek wejściem; po starcie po godzinie spłaszczenia z otwartą pozycją natychmiastowe `flatten`; blokada instancji przez `fcntl.flock` na `/data/engine.lock`; tętno do pliku; zakończenie czystym zamknięciem połączeń.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/watchdog.py src/gwstock/app.py tests/fault && git commit -m "feat: watchdog, blokada instancji i pętla dnia"`

---

### Task 15: Księga, raport, bramka P i pre-rejestracja

**Files:**
- Create: `src/gwstock/prereg.py`, `src/gwstock/report/__init__.py`, `src/gwstock/report/ledger.py`, `src/gwstock/report/gates.py`, `src/gwstock/report/cli.py`, `tests/unit/test_report.py`, `docs/preregistration/v1.md`

**Interfaces:**
- Consumes: `Journal`, `TradeRecord`, `load_config`.
- Produces: `fingerprint(paths: list[Path], adapter: str, instrument_class: str) -> str` (SHA-256 hex); `export_csv(journal: Journal, path: Path) -> int` (liczba wierszy); `GateP(sessions: int, unreconciled_over_60s: int, unhandled_rejections: int, kill_drills_done: bool, memory_measured: bool)`; `gate_p_status(journal, drills: set[str], memory_mb: float | None) -> GateP`; `python -m gwstock.report` drukuje dzienny raport (brutto, netto, koszty, liczniki błędów).

- [ ] **Step 1: Write the failing tests**

```python
def test_fingerprint_changes_with_any_input(tmp_path): ...    # zmiana pliku, adaptera lub klasy instrumentu zmienia odcisk; ten sam wejściowy → ten sam
def test_csv_contains_all_spec_columns_and_one_row_per_trade(...):
    assert header == ["instrument","side","size","decision_bid","decision_ask","entry_fill","exit_fill",
                      "gross_pnl","spread_cost","slippage_penalty","financing","fees","net_pnl",
                      "signal_ts","submit_ts","confirm_ts","exit_ts","latency_ms","exit_reason"]
def test_gate_p_requires_40_clean_sessions(...): ...          # 39 sesji → niespełniona; 40 i brak rozbieżności → spełniona
def test_report_prints_net_and_gross_and_error_counters(capsys, ...): ...
```

- [ ] **Step 2: Run** `pytest tests/unit/test_report.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** moduły; `docs/preregistration/v1.md` ze szkieletem: skróty plików `config/*.toml`, adapter (`capitalcom-demo`), klasa instrumentu (`index-cfd`), liczba wariantów K = 1, definicje metryk i bramek P/A/B z §11, data rozpoczęcia (do wpisania przez właściciela tuż przed pierwszym dniem; skrypt `python -m gwstock.prereg --write` uzupełnia odciski).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add src/gwstock/prereg.py src/gwstock/report docs/preregistration tests/unit/test_report.py && git commit -m "feat: księga, raport, bramka P i pre-rejestracja"`

---

### Task 16: Docker, Compose i runbook

**Files:**
- Create: `docker/Dockerfile`, `docker/compose.yml`, `docs/ops/runbook.md`, `tests/unit/test_compose.py`

**Interfaces:**
- Consumes: `docs/ops/compose-notes.md` (Zadanie 1).
- Produces: obraz `python:3.12-slim` (użytkownik bez uprawnień, `WORKDIR /app`, instalacja z `requirements.lock`, `CMD ["python","-m","gwstock"]`); usługa `gw-stock-engine`.

- [ ] **Step 1: Write the failing test**

```python
def test_compose_enforces_memory_and_isolation():
    svc = yaml.safe_load(Path("docker/compose.yml").read_text())["services"]["gw-stock-engine"]
    assert svc["mem_limit"] == "256m" and svc["memswap_limit"] == "256m"
    assert svc["mem_reservation"] == "128m" and svc["oom_score_adj"] == 1000
    assert svc["read_only"] is True and "pids_limit" in svc
    assert any("/secrets/capitalcom_demo.json" in v and v.endswith(":ro") for v in svc["volumes"])
    assert "ports" not in svc and svc.get("restart") == "unless-stopped"
    assert svc["healthcheck"]["test"]      # świeżość pliku tętna
```

- [ ] **Step 2: Run** `pytest tests/unit/test_compose.py -v` — Expected: FAIL.
- [ ] **Step 3: Write** `Dockerfile` i `compose.yml` (wolumen `./data:/data`, `tmpfs: /tmp`, klucz tylko do odczytu, zmienne bez sekretów). `runbook.md`: procedura restartu „zatrzymaj, potem uruchom" (nigdy nakładanie), polecenie pomiaru `docker stats --no-stream`, kroki walidacji na VPS (`docker compose config`, sprawdzenie rachunkowości swapu w jądrze), zasada „nic nie wchodzi na VPS bez pomiaru i zgody właściciela", `data/STOP` i `data/HARD_STOP`.
- [ ] **Step 4: Run** `pytest tests/unit/test_compose.py -v` — Expected: PASS. Na maszynie z Dockerem: `docker build -f docker/Dockerfile -t gw-stock-engine:test . && docker run --rm gw-stock-engine:test python -m pytest -m "not integration_demo" -q` — Expected: testy przechodzą w obrazie.
- [ ] **Step 5: Commit** — `git add docker docs/ops/runbook.md tests/unit/test_compose.py && git commit -m "feat: Docker, Compose z limitami pamięci i runbook"`

---

### Task 17: CI i zabezpieczenia pracy z Claude Code

**Files:**
- Create: `.github/workflows/ci.yml`, `.claude/settings.json`, `.claude/agents/risk-reviewer.md`, `.claude/agents/order-state-auditor.md`, `.claude/agents/backtest-skeptic.md`, `scripts/hooks/guard_live.py`, `tests/unit/test_guard_live.py`

**Interfaces:**
- Produces: `guard_live.violates(text: str) -> bool` (prawda, gdy tekst zawiera host live `api-capital.backend-capital.com` bez prefiksu `demo-`); hook `PreToolUse` z `"onFailure": "block"` wołający skrypt przy `Write`/`Edit`.

- [ ] **Step 1: Write the failing tests**

```python
def test_guard_flags_live_host_only():
    assert violates("BASE = 'https://api-capital.backend-capital.com/api/v1'")
    assert not violates("BASE = 'https://demo-api-capital.backend-capital.com'")
    assert not violates("def f(): return 1")
```

- [ ] **Step 2: Run** `pytest tests/unit/test_guard_live.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** skrypt i konfigurację. `ci.yml`: wyzwalacze `push`, `pull_request`, `workflow_dispatch`; `permissions: contents: read`; zadanie `tests` (macierz Python 3.12 i 3.13, instalacja z `requirements.lock`, `pytest -m "not integration_demo"`); zadanie `image` (budowa obrazu i testy w obrazie); akcje zewnętrzne przypięte do pełnego SHA; **bez żadnych sekretów**. Agenci tylko do odczytu (`tools: Read, Grep, Glob`): `risk-reviewer` (przegląd `risk.py`, `execution.py`), `order-state-auditor` (przegląd `orders/`, `reconcile.py` pod kątem niezmienników z §12), `backtest-skeptic` (przegląd wyników i bramek).
- [ ] **Step 4: Run** `pytest tests/unit/test_guard_live.py -v` — Expected: PASS. Spróbuj wypchnąć workflow: jeśli integracja GitHub odmówi (spec pyt. 17), poproś właściciela o dodanie pliku przez interfejs i odnotuj w `docs/ops/runbook.md`.
- [ ] **Step 5: Commit** — `git add .github .claude scripts tests/unit/test_guard_live.py && git commit -m "feat: CI i zabezpieczenia pracy z Claude Code"`

---

### Task 18: Odbiór v1 (demo, VPS, kryteria akceptacji)

**Files:**
- Modify: `docs/ops/capitalcom-api-notes.md` (statusy z demo), `docs/ops/runbook.md`
- Create: `tests/integration_demo/fixtures/` (zapisane odpowiedzi demo), `docs/ops/acceptance-v1.md`

**Interfaces:**
- Consumes: gotowy silnik (Zadania 2–17), klucz demo w `/secrets/capitalcom_demo.json` na VPS, pisemne odpowiedzi Capital.com (spec pyt. 1), zgoda właściciela na uruchomienie na VPS.

- [ ] **Step 1: Pomiar pamięci przed wdrożeniem** — uruchom kontener pod limitem cgroup lokalnie lub na VPS po zgodzie właściciela; `docker stats --no-stream` w sesji symulowanej; zapisz RSS w `acceptance-v1.md`. Warunek: RSS poniżej limitu z zapasem; w przeciwnym razie decyzja o hostingu (spec pyt. 16).
- [ ] **Step 2: Testy `integration_demo` na VPS** — `pytest -m integration_demo -v`; zapisz odpowiedzi do `tests/integration_demo/fixtures/` (bez tokenów i danych konta); uzupełnij statusy w `capitalcom-api-notes.md` (pyt. 3, 4, 7, 8, 12 ze specyfikacji).
- [ ] **Step 3: Ćwiczenia kill switcha i restartu** — `data/STOP`, `data/HARD_STOP`, `kill -9` w środku wejścia i wyjścia, zabicie przez OOM (`docker kill --signal=KILL`), brak sieci; po każdym restart z uzgodnieniem bez zdublowań i pozycji osieroconych.
- [ ] **Step 4: Pełna sesja demo bez ingerencji** i raport dzienny (`python -m gwstock.report`).
- [ ] **Step 5: Przejdź kryteria akceptacji 1–7 z §17**, odhaczając je w `acceptance-v1.md` z poleceniem i wynikiem dla każdego. Bramka P (40 sesji) biegnie dalej na demo.
- [ ] **Step 6: Commit** — `git add docs tests/integration_demo && git commit -m "docs: odbiór v1 na demo"`
