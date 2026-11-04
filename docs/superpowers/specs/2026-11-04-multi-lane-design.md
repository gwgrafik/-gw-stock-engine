# Projekt: rdzeń pasm (lanes) i bramka portfelowa

Stan: 4.11.2026. Zadanie 3 planu `docs/superpowers/plans/2026-11-04-multi-lane-engine.md`. To jest WYŁĄCZNIE projekt: żaden
plik kodu, testu ani konfiguracji nie został zmieniony. Projekt opiera się na odczycie kodu gałęzi `feat/engine-v2`
(commit `01f8b5f`). Przy każdej tezie o istniejącym zachowaniu podany jest plik i funkcja; gdzie czegoś nie da się ustalić
z kodu ani z notatek, jest napis **nie ustalono** i sposób sprawdzenia.

Kontekst bezpieczeństwa: prawdziwe pieniądze, rachunek 40 EUR, depozyt indeksów 5% (`docs/ops/capitalcom-live-readout-2026-11-01.md`).
Rentowność nie jest wykazana; wariant z pasmami to narzędzie pomiarowe (K+1), nie obietnica zysku.

Spis treści:
1. Architektura pasm
2. Zmiany per plik i funkcja (z oceną ryzyka dla DE40 v1)
3. Cel zysku (take profit)
4. Bramka portfelowa
5. Uzgadnianie wielu pozycji
6. Pre-rejestracja i uzbrojenie wielu pasm
7. Waluta (R8)
8. Plan zadań implementacyjnych
9. Plan testów odporności
10. Otwarte pytania do właściciela
Załącznik A: ustalenia z kodu, które dotyczą bezpieczeństwa (lista kontrolna dla przeglądu)

---

## 0. Najważniejsze ustalenia (streszczenie)

1. **Dziś silnik nie obsłużyłby US500 w ogóle.** `capitalcom_parse.parse_instrument_rules` rzuca `ParseError`, gdy waluta
   instrumentu różni się od waluty rachunku sesji ("value_per_point would need an FX rate"); adapter
   (`CapitalComBroker.instrument_rules` w `broker/capitalcom.py`) zamienia to na `BrokerError`. US500/US100 są w USD
   (odczyt live 2026-11-01), rachunek w EUR. Pasmo w USD wymaga jawnego modelu walutowego (sekcja 7) zanim cokolwiek pójdzie live.
2. **Trzy znaczniki uzgadniania są globalne, a muszą być per instrument.** `reconcile.META_CLOSED_MARKER` (`closed_marker`)
   i `reconcile.META_MISMATCH_SINCE` (`mismatch_since`) to jeden klucz `meta` na cały dziennik. Przy dwóch obiektach
   `Reconciler` (po jednym na instrument): (a) `history_start` (`reconcile.history_start`) bierze `max(closed_marker, opened_at)`,
   więc zaksięgowanie zamknięcia na US500 o 21:00 przesunęłoby początek historii pozycji DE40 otwartej wcześniej i jej
   zamknięcie z 17:00 zniknęłoby z odczytu: pozycja DE40 utknęłaby w `BLOCKED` (`LOCAL_POSITION_MISSING`); (b) jeden
   przebieg czyści `mismatch_since` (`_plan_mismatch`, `plan.mismatch_since = ""`), który drugi właśnie ustawił, więc
   `MISMATCH_TIMEOUT` mógłby nigdy nie wypalić. To jest najgroźniejsza pułapka refaktoru.
3. **Liczniki "sesji" i "wejść dnia" są globalne.** `Journal.count_entry_orders(day)` liczy wejścia wszystkich instrumentów,
   a `Engine.session_index` to `len(trades()) − count_smoke_trades()` po całej księdze. Bez zmiany US500 zużywałby
   `max_entries_per_day = 1` pasma DE40, a transakcje US500 przyspieszałyby przejście DE40 na wielkość według reguł
   (`strategy.decide`, warunek `session_index >= fixed_min_size_sessions`). Oba muszą stać się per instrument.
4. **Bramka ryzyka liczy ryzyko otwarte tylko własnego instrumentu i wartością punktu kandydata.** `RiskGate._open_risk(vpp)`
   czyta `current_position(self._strat.instrument)` i mnoży przez `vpp` KANDYDATA. Przy dwóch instrumentach DAILY_LOSS nie
   widziałby pozycji drugiego pasma. To przenosi się do bramki portfelowej (sekcja 4).
5. **Stan "per proces" w silniku jest pojedynczy, a musi być per instrument:** `_SessionGuard.market_closed_at` (zamknięty
   rynek US500 wstrzymałby o minutę spłaszczenie DE40 w `Engine._market_closed_backoff`), `Watchdog.note_quote` (notowanie
   US500 karmi straż ciszy DE40 przez `GuardedBroker.quote` → `on_quote`), `Watchdog.set_session_window` (jedno okno),
   `ExitMemory.last_own_deal` (awaryjne spłaszczenie przy nieczytelnym dzienniku zamknęłoby tylko jeden z dwóch deali),
   `Engine.last_quote`.
6. **Take profit z historii jest dziś księgowany jako STOP.** `capitalcom_history.REASONS` mapuje `TP` na `"STOP"`, a
   `costs.CostModel.compute` dolicza wtedy `stop_exit_extra_slippage_points`. Do tego `Executor._exit_reason` daje `FORCED`,
   gdy deal ma zapisany zamiar wyjścia silnika, nawet jeśli faktycznie zamknął go cel (wyścig: limit czasu pozycji kontra TP).
7. **`PUT /positions/{dealId}` ze samym `stopLevel` może kasować take profit** (C5d w `docs/ops/capitalcom-api-notes.md` §2.13:
   nie ustalono). `TradingMixin.set_stop` wysyła wyłącznie `{"stopLevel": ...}`. Dla pasm z celem zabezpieczenie musi wysyłać
   stop i cel razem albo najpierw rozstrzygnąć C5d próbą.
8. **Pętla dnia jest przywiązana do jednego kalendarza.** `Engine.run_forever` → `run_day(day)` → `_session_times` z jednego
   `ExchangeCalendar`; po zamknięciu XETRA (17:30) silnik przechodzi w spoczynek (`_idle_until`) do północy, a sesja NYSE
   trwa do 22:00 czasu berlińskiego. Bez nowej pętli pasmo US500 nigdy by nie handlowało.
9. **Zachowanie DE40 v1 da się zachować dowodnie tylko testem charakteryzującym.** Proponuję jako PIERWSZE zadanie
   implementacyjne zapis "złotych śladów" (kolejność wywołań `FakeBroker.calls` i pełna treść `raw_events`, `orders`,
   `positions`, `trades`, `meta`) dla scenariuszy z `tests/fault/test_engine_day.py` i `test_trade_lifecycle_e2e.py` PRZED
   jakimkolwiek refaktorem i porównanie bajt w bajt po każdym kolejnym zadaniu, przy konfiguracji z jednym pasmem DE40.
10. **"Identyczne zachowanie DE40" ma dwa znaczenia i trzeba je rozdzielić.** (a) Konfiguracja z JEDYNYM pasmem DE40:
    identyczna co do bajtu (cel twardy, testowalny). (b) Praca razem z innymi pasmami: wspólny kill switch, wspólne limity
    straty i depozyt z definicji zmieniają wynik DE40 (np. HARD wywołany na US500 zamyka też DE40). To jest decyzja
    właściciela (pytanie 1 w sekcji 10), nie błąd projektu.

---

## 1. Architektura pasm

### 1.1 Pojęcia

- **Pasmo (lane)** = jeden instrument + jedna reguła (wtyk) + własna konfiguracja (R1, R5). Identyfikatorem pasma jest
  `epic` (R1: najwyżej jedno pasmo i jedna otwarta pozycja na instrument). Dzięki temu cały istniejący kod kluczowany
  `epic` (tabela `positions`, `OrderRecord.epic`, `make_client_id(day, instrument, ...)`, `META_ENTRY_LAST` w
  `execution.py`) pozostaje poprawny bez nowego klucza.
- **Wtyk reguły**: `orb_index_cfd` (istniejący `strategy.decide`) i `bar_b1_m5` (`strategy_bars.py` z zadania 2). Wtyk jest
  czystą funkcją; okna czasowe, spłaszczenie, limit czasu pozycji i filtry dnia należą do pasma (plan, zadanie 2 pkt 1).
- **Portfel** = wszystko, co wspólne: proces, dziennik, sesja brokera, limit zapytań, kill switch, depozyt, limity straty.

### 1.2 Co jest wspólne, a co per pasmo

| Element | Wspólny | Per pasmo | Źródło w kodzie dziś |
|---|---|---|---|
| Proces, pętla (jedna korutyna, O13) | tak | | `app.Engine._loop`, decyzja O13 w `decisions-log.md` |
| Blokada instancji | tak | | `watchdog.InstanceLock` (`engine.lock`) |
| Dziennik SQLite (jedno połączenie, WAL) | tak | | `journal.Journal` |
| Sesja brokera, logowanie, ponowne logowanie | tak | | `app._SessionGuard`, `GuardedBroker` |
| Limit zapytań (0,2 s ogólnie, 0,1 s między zleceniami) | tak | | `broker/capitalcom.py`: `GENERAL_MIN_INTERVAL_S`, `_Spacer` |
| Kill switch (poziom, pliki `STOP`/`HARD_STOP`) | tak | | `risk.KillSwitch` |
| Menedżer zleceń i flaga `in_flight` | tak (szereguje wejścia wszystkich pasm) | | `orders/manager.OrderManager` |
| Tryb procesu i rozbrojenie (`ARM_LIVE`, `live_allowed`, rachunek) | tak | | `app.Engine._check_armed`, `_downgrade` |
| Tryb pasma (live tylko z zamrożonym odciskiem pasma) | | tak (R7) | nowe |
| Limity straty dnia i łączne, liczniki `realized_pnl` | tak (R3) | | `journal.realized_pnl*`, `Executor._enforce_loss_limits` |
| Bramka portfelowa | tak (R3) | | nowe |
| Bramka pasma (okno, wejścia dnia, FORMAT, LOSS_PER_TRADE, STALE) | | tak | `risk.RiskGate` |
| Kalendarz giełdy i godziny | | tak | `calendar.ExchangeCalendar` (XETRA; NYSE z zadania 1) |
| Konfiguracja reguły i wielkości | | tak | `config.StrategyConfig`; nowy plik pasma z zadania 2 |
| Wykonawca (straże, spłaszczenie, księgowanie) | pamięć wyjść wspólna | instancja per pasmo | `execution.Executor`, `ExitMemory` |
| Uzgadnianie | | instancja per pasmo (z kluczami per instrument) | `reconcile.Reconciler(epic=...)` |
| Straż ciszy notowań, okno sesji | | tak | `watchdog.Watchdog._check_silence` (dziś jedno okno) |
| Tętno | tak (z polami per pasmo) | | `Watchdog._write_heartbeat` |
| Bramka L1 i plik `L1_PASSED` | | tak (R6) | `report/gates.gate_l1_status`, `app.L1_PASSED_FILE` |

### 1.3 Pętla

Jedna korutyna jak dziś (O13). Każdy takt:

1. wspólne: `kill.scan_files`, `_ensure_login`, `_ensure_account`;
2. dla każdego pasma w STAŁEJ kolejności z konfiguracji: krok notowania;
3. wspólne: ping;
4. dla każdego pasma: zarządzanie (uzgadnianie tego pasma, stop dla pozycji bez stopu, straże wypełnienia, przy HARD
   awaryjne spłaszczenie, EXITING → ponowienie, limit czasu pozycji, godzina spłaszczenia);
5. dla każdego pasma aktywnego w swoim oknie: dane sygnału, decyzja, kontrola świec;
6. `watchdog.tick`.

Z jednym pasmem DE40 kolejność kroków jest dokładnie ta z `Engine._tick` (login, rachunek, notowanie, ping, zarządzanie,
dane sygnału, decyzja, kontrola świec). To jest warunek konieczny R2 i pilnuje go test złotego śladu (zadanie 4).

**Dzień portfela.** Dziś `run_forever` iteruje dni lokalne XETRA (`Engine._today`). W wersji z pasmami dniem portfela jest
data lokalna strefy kalendarza pasma DE40 (Europe/Berlin), bo w tej strefie liczony jest `realized_pnl` DE40, a limit
dzienny jest wspólny. Każde pasmo liczy swoje godziny (`_Times`) z własnego kalendarza dla tej samej daty. Pętla dnia trwa
od najwcześniejszego `preopen` do najpóźniejszego `close` aktywnych pasm; pasmo bez sesji danego dnia (np. święto NYSE)
zarządza tylko pozostałą pozycją (dzisiejsze `_off_day`). Sesja NYSE (15:30–22:00 czasu berlińskiego) mieści się w tej
samej dacie berlińskiej, więc klucz dnia jest jednoznaczny; zmiana czasu w USA i UE w różne tygodnie nie przesuwa daty.
Do rozstrzygnięcia przez właściciela: czy klucz `realized_pnl` dla pasma US500 ma być datą berlińską (rekomendacja), czy
nowojorską (pytanie 4).

### 1.4 Odrzucone alternatywy

- **Osobny kontener (proces) na pasmo.** Odrzucone: (a) dwa procesy to dwie sesje tym samym kluczem API; czy drugie
  logowanie unieważnia tokeny pierwszego: **nie ustalono** (`docs/ops/capitalcom-api-notes.md` §1.2 milczy; sprawdzić
  można tylko dwoma równoległymi logowaniami tylko do odczytu, co samo w sobie grozi zablokowaniem pracy silnika);
  (b) limity Capital.com (10 zapytań/s i 1 zlecenie na 0,1 s NA UŻYTKOWNIKA, §1.2) nie byłyby koordynowane; (c) dwa dzienniki
  uniemożliwiają atomową kontrolę wspólnego limitu straty i depozytu (O8: licznik i księga w jednej transakcji);
  (d) dwa kill switche zamiast wspólnego (R3); (e) limit pamięci 256 MB na współdzielonym VPS (spec §13).
- **Zadania asyncio na pasmo (współbieżnie).** Odrzucone: łamie O13 (wejście, uzgadnianie i spłaszczenie nigdy
  równolegle); flaga `in_flight` broni tylko w jedną stronę (`OrderManager`, O12).
- **Jeden `Reconciler` dla wszystkich instrumentów naraz.** Możliwe później (jedno `list_positions` na takt), ale wymaga
  przepisania macierzy §7; dziś jest ona napisana dla jednego `epic` i pokryta 1349 liniami testów
  (`tests/fault/test_reconcile.py`). Rekomendacja: instancja na instrument z kluczami per instrument; koszt to drugie
  `list_positions` + `list_working_orders` co 10 s (0,2 zapytania/s więcej, budżet 5/s).
- **Kilka pozycji na tym samym instrumencie.** Poza zakresem (R1): wymaga `hedgingMode` i zmiany klucza tabeli `positions`.
- **Osobna klasa silnika dla wielu pasm obok nietkniętego `Engine`.** Kusząca dla R2, ale daje dwie ścieżki kodu
  bezpieczeństwa (poprawka w jednej nie trafia do drugiej). Odrzucone na rzecz refaktoru `Engine` pilnowanego złotym śladem.

---

## 2. Zmiany per plik i funkcja

Ocena ryzyka dla DE40 v1: **N** (brak wpływu przy jednym paśmie, zmiana addytywna), **Ś** (refaktor ścieżki, którą
przechodzi DE40; wymaga złotego śladu), **W** (zmiana ścieżki ochronnej albo księgowania DE40; wymaga złotego śladu i
przeglądu `risk-reviewer` / `order-state-auditor`).

### 2.1 `src/gwstock/app.py`

| Miejsce | Dziś | Zmiana | Ryzyko |
|---|---|---|---|
| `Engine.__init__`: `self._epic = strat.instrument` | jedno pasmo | lista pasm (`LaneSpec` + stan); `self.strat` zostaje dla pasma DE40 (CLI go czyta) | Ś |
| `_build_executor`, `_build_reconciler` | jeden wykonawca, jedno uzgadnianie | po jednym na pasmo; wspólne `ExitMemory`; `check_wiring` sprawdza każde pasmo live (`reconciler.booker is executor` per pasmo) | Ś |
| `_quote_step`: `self.broker.quote(self._epic)` | jedno | per pasmo; `last_quote` per pasmo | Ś |
| `_load_signal_data`: `instrument_rules`, `daily_candles(self._epic, ...)` | ORB | per wtyk (ORB: jak dziś; B1: świece M5) | Ś |
| `_decide`: `candles_5m(self._epic, ...)`, `quote`, `instrument_rules` | ORB | bez zmian dla ORB, nowa ścieżka dla B1 | N dla DE40 |
| `_neighbour_candles`, `_candles_followup_step` | ORB | tylko pasmo ORB | N |
| `_position`, `_needs_management`: `current_position(self._epic)`, `unresolved_orders()` z filtrem `epic` | jedno | per pasmo; `_needs_management` portfela = którekolwiek pasmo | Ś |
| `_entries_today`: `count_entry_orders(day)` | globalne | `count_entry_orders(day, epic)` | Ś (z jednym pasmem wynik równy) |
| `session_index` | cała księga | per instrument (`trades` z `instrument == epic` minus smoke tego instrumentu) | Ś (z jednym pasmem równe) |
| `_decision_step`, `_decide`, `_enter` | jedna decyzja dziennie | bez zmian dla ORB; B1: decyzja po każdej zakończonej świecy M5 w oknach | N dla DE40 |
| `_manage` | HARD → `executor.emergency_flatten()` jednego pasma | HARD → awaryjne spłaszczenie WSZYSTKICH pasm w stałej kolejności; dodatkowo limit czasu pozycji pasma B1 | W |
| `_market_closed_backoff`: `_guard.market_closed_at` | globalne | słownik per `epic` (ustawiany w `GuardedBroker.close_position` po `deal_id` → epic z dziennika albo z argumentu) | W (z jednym pasmem równe) |
| `GuardedBroker.open_position`: `_entry_block()` | globalne dry_run | dodatkowo tryb pasma po `req.epic` (druga linia obrony R7) | Ś |
| `GuardedBroker.quote` → `on_quote` | jedna straż ciszy | `note_quote(epic, ts)` | Ś |
| `_session_times`, `_must_flatten`, `_phase` | jeden kalendarz | per pasmo | Ś |
| `_enforce_limits(day)` | `executor.enforce_loss_limits` | raz na dzień portfela + limity portfelowe | Ś |
| `run_forever`, `_run_day`, `_idle_until`, `_off_day`, `_next_wake` | dzień XETRA | dzień portfela (sekcja 1.3); `_next_wake` zbiera terminy wszystkich pasm | W (pętla to serce DE40) |
| `DayReport`, `_report`, `_day_net`, `_log_stop` | jedna pozycja | raport per pasmo + suma portfela | N |
| `ACCOUNT_CURRENCY = "EUR"` | rachunek EUR | bez zmian (rachunek zostaje w EUR) | N |

### 2.2 `src/gwstock/execution.py`

| Miejsce | Zmiana | Ryzyko |
|---|---|---|
| `Executor.__init__`: `self._epic = strat.instrument`, `self._strat` (tylko `slippage_guard_fraction`), `self._risk.max_loss_per_trade` | konstruktor przyjmuje `LaneSpec` (epic, `slippage_guard_fraction`, `max_loss_per_trade`, kalendarz, waluta instrumentu); dla DE40 wartości z `strategy_v1.toml`/`risk_v1.toml` bez zmian | Ś |
| `_prepare`: `EntryCandidate(epic=self._epic, ...)`, `make_client_id(day, self._epic, ...)`, `META_ENTRY_LAST` | bez zmiany logiki; po bramce pasma wywołanie bramki portfelowej (sekcja 4); w kontekście wejścia dodatkowe pola `target_distance`, `time_limit_min`, waluta (tylko gdy niepuste: kontekst DE40 bajt w bajt) | W |
| `_after_submit`, `guard_adopted_fill`, `_guard_values` | ryzyko wielkości liczone w walucie instrumentu → przeliczenie na EUR (sekcja 7); dla EUR bez zmian | W |
| `emergency_flatten`, `_flatten_without_journal` | `self._epic` per pasmo; `ExitMemory.last_own_deal` → słownik `epic → deal_id` (pole dotychczasowe zostaje jako widok dla DE40 albo znika po migracji testów) | W |
| `_protect`, `_attach_stop`, `_protection_distance`, `_max_loss_distance` | dla pasm z celem: stop i cel razem (sekcja 3); odległość z limitu straty w walucie instrumentu | W |
| `_exit_reason` | kolejność: `STOP` → `TARGET` (nowe) → zamiar silnika (`FORCED`) → przyczyna brokera | W (dla DE40: zamknięcie TP ustawionym ręcznie przez właściciela; patrz 3.5) |
| `_full_trade`, `_fallback_trade`, `book` | waluta instrumentu i dwufazowe księgowanie EUR (sekcja 7) | W |
| `_enforce_loss_limits` | bez zmian (`realized_pnl` jest już globalny po dniu); dodatkowo progi portfela | Ś |

### 2.3 `src/gwstock/risk.py`

| Miejsce | Zmiana | Ryzyko |
|---|---|---|
| `RiskGate._check_entry`: `epic == self._strat.instrument`, `instrument = self._strat.instrument` | pasmo zna swój `epic` i okna z konfiguracji pasma | Ś |
| `entries_per_day` → `count_entry_orders(day)` | per instrument | Ś |
| `daily_loss` + `_open_risk(vpp_d)` | w bramce pasma bez zmian liczenia dla DE40 przy jednym paśmie; w bramce portfelowej suma ryzyka WSZYSTKICH pozycji, każda swoją wartością punktu z kontekstu wejścia (sekcja 4.3) | W |
| `_window` | ORB: bez zmian; B1: wiele okien z konfiguracji pasma, plus `teraz < godzina spłaszczenia pasma` | N dla DE40 |
| `KillSwitch` | bez zmian (wspólny) | N |

### 2.4 `src/gwstock/reconcile.py`

| Miejsce | Zmiana | Ryzyko |
|---|---|---|
| `META_CLOSED_MARKER`, `META_MISMATCH_SINCE` | klucze per instrument: DE40 zachowuje dotychczasowe nazwy (`closed_marker`, `mismatch_since`), inne pasma `closed_marker:<epic>`, `mismatch_since:<epic>`; jedna funkcja `meta_key(name, epic, legacy_epic)` | W (to naprawa groźnej kolizji, ustalenie 2) |
| `history_start(journal, position)` | czyta znacznik instrumentu pozycji | W (także `cli.reset_position`) |
| `run_once`: `mine`, `working_ours` | bez zmiany logiki; zwraca dodatkowo pozycje na instrumentach bez pasma (sekcja 5) | N |
| `_plan_position`: `at_broker.stop_level is None` → `unprotected` | dla pasm z celem dodatkowo `untargeted` (pozycja bez celu, sekcja 3.4) | N dla DE40 |
| `_plan_closure_not_in_history` | bez zmian | N |

### 2.5 `src/gwstock/journal.py`

| Miejsce | Zmiana | Ryzyko |
|---|---|---|
| `count_entry_orders(day)` | opcjonalny `epic`: zakres prefiksu `gws-{YYYYMMDD}-{epic}-` + sufiks `-entry` (bez parametru: dzisiejsze zachowanie) | N |
| `count_smoke_trades()` | opcjonalny `epic` (`trades.instrument`) | N |
| nowe | `trades_for(epic)` albo filtr w wywołującym | N |
| schemat `SCHEMA_VERSION = 1` | **bez zmiany schematu.** Cel pozycji i dane walutowe w `meta` (`target:<deal_id>`, `trade_fx:<deal_id>`, `pnl_pending:<deal_id>`). Uzasadnienie: `Journal._setup` odmawia wersji innej niż 1, a dziennik DE40 żyje już na VPS; migracja schematu to ryzyko bez potrzeby | N |
| nowe | `record_trade_pending(...)` i `settle_trade_pnl(...)` dla dwufazowego księgowania EUR (sekcja 7.4), każde w jednej transakcji `_write` | N dla DE40 (DE40 dalej przez `record_trade`) |

### 2.6 `src/gwstock/orders/` i `src/gwstock/types.py`

- `orders/manager.OrderManager`: bez zmian logiki (już kluczowany `req.epic`: `_guard`, `match_entry_positions`, `_move_position`). Ryzyko N.
- `orders/fsm.py`: **bez nowych krawędzi** (cel zysku to inne źródło zamknięcia, nie nowy stan). Ryzyko N.
- `orders/ids.make_client_id`: bez zmian (`US500` spełnia `[A-Za-z0-9]+`). Ryzyko N.
- `types.EntryRequest`: nowe pole `target_distance: Decimal | None = None` (domyślne None: każde istniejące wywołanie bez zmian). N.
- `types.OpenPosition`: nowe pola `target_level: Decimal | None = None`, `target_derived: bool = False`. N.
- `types.CLOSE_REASONS`: dodać `"TARGET"`. Ryzyko Ś: `costs.TradeFacts` i `ClosedDeal.__post_init__` walidują po tej liście.
- `types.InstrumentRules`: nowe pole `currency: str | None = None` (sekcja 7). N.

### 2.7 `src/gwstock/broker/`

| Plik, funkcja | Zmiana | Ryzyko |
|---|---|---|
| `capitalcom_trading.TradingMixin.open_position` | klucz `profitDistance` w ciele TYLKO gdy `req.target_distance is not None` (ciało DE40 identyczne co do kluczy i kolejności) | Ś (test ciała żądania DE40 bajt w bajt) |
| `capitalcom_parse.parse_position`, `PositionRow` | odczyt `profitLevel`/`profitDistance` (analogicznie do stopu, C5) | N |
| `capitalcom_parse.parse_opening`, `Opening` | odczyt `profitLevel` z potwierdzenia (pole z [S3]) | N |
| `TradingMixin._snapshot`, `_snapshot_stop`, `list_positions` | cel wyliczony z wysłanego `profitDistance` (jak `stop_derived`) | N |
| `TradingMixin.set_stop` | nowa metoda `set_protection(deal_id, stop_level, target_level)` z ciałem `{"stopLevel", "profitLevel"}`; `set_stop` bez zmian dla DE40 | N dla DE40 |
| `capitalcom_history.REASONS` | `TP` → `"TARGET"` zamiast `"STOP"` | Ś (dla DE40 tylko TP ustawiony ręcznie; patrz 3.5) |
| `capitalcom_parse.parse_instrument_rules` | parametr `allow_foreign_currency: bool = False`; z `True` zwraca `InstrumentRules(currency=...)` zamiast `ParseError` | N dla DE40 (domyślne zachowanie bez zmian) |
| `broker/base.Broker` | nowa metoda `set_protection`; opcjonalnie `transactions_since` (sekcja 7) | N |
| `broker/fake.FakeBroker` | wiele instrumentów (`rules` per epic), zamknięty rynek per epic, `trigger_target`, cel atomowy, `ClosedDeal("TARGET")`; zachowanie dla jednego instrumentu bez zmian (testy kontraktu `tests/unit/broker_contract.py`) | Ś (FakeBroker to fundament 12 tys. linii testów awarii) |

### 2.8 `src/gwstock/watchdog.py`

- `Watchdog.note_quote(ts)` → `note_quote(ts, epic=None)`; `set_session_window(start, end)` → okna per epic; `_check_silence`
  per epic, przyczyna `QUOTE_SILENCE` wspólna (kill miękki portfela, R3), alarm z nazwą instrumentu. Ryzyko Ś.
- `_write_heartbeat`: pole `last_quote_ts` zostaje (dla DE40, `cli.health` czyta tylko `ts`), dochodzi `lanes` z polami per pasmo. N.
- Liczniki odrzuceń, logowań, uzgadniania: wspólne. Ryzyko N. Uwaga: `note_reconcile_error` liczy przebiegi z wyjątkiem z
  rzędu (`RECONCILE_FAILURE_LIMIT = 3`); przy dwóch uzgadnianiach na takt licznik musi być per pasmo albo próg liczony na
  pasmo, inaczej udany przebieg jednego pasma zerowałby awarię drugiego (`note_reconcile_ok`). Ryzyko W dla wykrywania awarii.

### 2.9 `src/gwstock/cli.py`, `prereg.py`, `config.py`, `report/`

- `cli.build_engine`: kalendarz XETRA na sztywno (`ExchangeCalendar(XETRA)`) → pasma z konfiguracji. Bez pliku pasm:
  dokładnie dzisiejsze złożenie (pasmo DE40). Ś.
- `cli.status`, `_status_*`, `cli.reset_position`, `cli.resolve_lost_trade` (`_LostTrade`), `cli.smoke`: dziś
  `engine.strat.instrument`. Dodać `--epic` (domyślnie DE40) i sekcje per pasmo w `status`. `smoke` zostaje dla DE40;
  smoke US500 to osobna decyzja właściciela (pytanie 6). Ś.
- `cli.mode_decision`, `runtime_mode`: tryb procesu bez zmian; tryb pasma osobno (sekcja 6). Ś.
- `prereg.CONFIG_FILES`, `_FORMAT_TAG`: bez zmian dla v1 (odcisk DE40 nie może się zmienić); odciski pasm w nowej funkcji. N.
- `config.load_config`: bez zmian; nowe `load_portfolio`, `load_lanes`, ładowanie pliku pasma (zadanie 2). N.
- `report/gates.gate_l1_status`, `trade_contexts`, `_engagement_days`, `_mismatch_days`, `_rejection_days`: dziś po całym
  dzienniku. L1 per instrument (filtr `trade.instrument`); incydenty kill switcha i rozbieżności zostają portfelowe
  (ostrożnie: liczą się dla każdego pasma). Ś.

### 2.10 `scripts/hooks/guard_live.py` (wymaga zgody właściciela)

Jeśli pasmo dostaje własny plik bramki wielkości (np. `data/L1_PASSED_US500`), strażnik live musi go chronić tak jak
`data/L1_PASSED`. Edycja `scripts/hooks/` wymaga zgody właściciela (`permissions.ask`, `CLAUDE.md`). Alternatywa bez zmiany
strażnika: jeden plik `data/L1_PASSED` z treścią będącą listą pasm (odczyt tylko). Pytanie 7.

---

## 3. Cel zysku (take profit)

### 3.1 Adapter (`capitalcom_trading.py`, `capitalcom_parse.py`, `capitalcom_history.py`)

- **Otwarcie.** `POST /positions` przyjmuje `profitLevel`, `profitDistance`, `profitAmount` (notatki §2.10, `udokumentowane`
  jako pola). Wysyłamy `profitDistance` = odległość celu w punktach od ceny wykonania, zaokrągloną W DÓŁ do kroku
  `minStepDistance` (cel bliżej, nigdy dalej; odwrotnie niż stop w S4). Czy `profitDistance` liczony jest od ceny wykonania,
  czy od notowania: **nie ustalono** (ta sama niepewność co C6 dla stopu); sprawdzić próbą na wielkości minimalnej:
  poziom celu w `GET /positions` kontra cena wykonania. Odległość musi spełniać `minStopOrProfitDistance` (wspólna reguła
  stopu i celu, §2.16.1); odmowa `error.invalid.takeprofit.*` (§1.4) daje REJECTED, wejście dnia zużyte (S3).
- **Odczyt.** Klucze `profitLevel`/`profitDistance` w `GET /positions` są tylko w [S3] (C5): **nie ustalono**, czy żywe API je
  zwraca. Reguła jak dla stopu (`parse_position`): klucz `profitLevel` → wartość; sam `profitDistance` → `level ± distance`
  (BUY plus, SELL minus); brak kluczy → cel wyliczony z wysłanego żądania (`target_derived=True`); gdy jakakolwiek odpowiedź
  pokazała klucze celu, brak klucza = brak celu.
- **Zmiana ochrony.** `PUT /positions/{dealId}` (§2.13): czy pominięcie `profit*` kasuje istniejący cel (C5d): **nie
  ustalono**. Dlatego dla pasm z celem `set_protection` wysyła zawsze oba poziomy; potwierdzenie `check_amendment` musi
  wtedy porównać oba (`stopLevel` i `profitLevel`, gdy obecne). Dla DE40 `set_stop` zostaje bez zmian.
- **Zamknięcie przez cel.** Historia aktywności: wiersz `POSITION`/`ACCEPTED` ze źródłem `TP` (wyliczenie w §2.19);
  przypisanie `TP` do zadziałania celu: **nie ustalono** (C7 dotyczy też `SL`), sprawdzić surową historią po pierwszym
  zamknięciu celem. Mapowanie: `TP` → `"TARGET"`. Kierunek wiersza zamknięcia jest przeciwny do pozycji, więc
  `capitalcom_history.classify` działa bez zmian.

### 3.2 Maszyna stanów pozycji (`orders/fsm.py`)

Bez nowych stanów i krawędzi. Zamknięcie celem przebiega jak zamknięcie stopem: pozycja OPEN znika u brokera, uzgadnianie
widzi zamknięcie w historii, księguje i przechodzi `OPEN → FLAT` (istniejąca krawędź „pozycja zniknęła u brokera”).
Uzasadnienie: tabela przejść jest testowana dokładnie (T2 w `decisions-log.md`), a cel nie wprowadza nowej niejasności stanu.

### 3.3 Uzgadnianie: wykrycie zamknięcia przez cel i rozliczenie

- `Reconciler._plan_position`, gałąź „D poza B”: bez zmian. Jedno zamknięcie D w historii → `plan.book`, FLAT; przyczyna z
  `ClosedDeal.reason` = `TARGET`.
- `Executor._exit_reason`: nowa kolejność `STOP`, `TARGET` (przyczyny brokera mają pierwszeństwo przed zamiarem silnika),
  potem `exit_intent` (`FORCED`), potem przyczyna brokera. Bez tego wyścig „limit czasu pozycji wysłał `DELETE`, a w tej
  samej chwili zadziałał cel” księgowałby zamknięcie celem jako `FORCED` (zamiar jest w `ExitMemory.exit_intents`).
- `costs.CostModel.compute`: `TARGET` bez `stop_exit_extra_slippage_points` (jak w badaniach: wyjście celem kosztuje
  połowę spreadu, `research/candle_strategies.run`). Wymaga zmiany `costs.py`, który jest POZA odciskiem v1 (A14), więc dla
  DE40 obowiązuje reguła 3.5.
- Pozycja bez celu u brokera dla pasma z celem: wpis `UNTARGETED` w wyniku uzgadniania; silnik próbuje `set_protection`
  raz na takt zarządzania; porażka to alarm, NIE kill switch HARD (stop chroni pozycję, a limit czasu i godzina spłaszczenia
  ją zamkną). Cel nie jest ochroną, więc jego brak nie blokuje niczego poza zdarzeniem.

### 3.4 Stan niepewny (UNKNOWN) dla wyjścia przez cel

| Sytuacja | Co widzi silnik | Stan | Rozstrzygnięcie |
|---|---|---|---|
| Cel zamknął pozycję, historia jeszcze bez wiersza (opóźnienie C7) | deal zniknął z listy, brak zamknięcia | `BLOCKED` `LOCAL_POSITION_MISSING` → po `mismatch_tolerance_s` SOFT `MISMATCH_TIMEOUT` (`_plan_mismatch`) | kolejny przebieg księguje, gdy historia dogoni; kill switch zdejmuje właściciel (H1, opcja A). `CLOSURE_NOT_IN_HISTORY` się NIE pojawia (silnik nie wysłał zamknięcia) |
| Limit czasu pozycji: `DELETE` w trakcie zadziałania celu | `close_position` → `BrokerRejected("POSITION_NOT_FOUND")` albo `BrokerTimeout` | `_flatten` → lista bez deala → `_settle` | zamknięcie z historii ze źródłem `TP` → `TARGET` (dzięki nowej kolejności w `_exit_reason`) |
| Wejście z celem, potwierdzenie bez klucza celu | `confirm` → `OpenPosition(target_level=None albo wyliczony)` | OPEN | cel wyliczony (`target_derived`); weryfikacja wzrokowa właściciela na próbie |
| Odrzucenie odległości celu | `open_position` → `BrokerRejected(error.invalid.takeprofit...)` | REJECTED (natychmiastowe) | wejście zużyte; alarm; nic nie wysłano |
| Cel i stop w tej samej świecy (gwałtowny ruch) | broker zamyka jednym z nich | jak wyżej | księga pokazuje faktyczną przyczynę z historii; R4 „STOP przy kolizji” dotyczy wyłącznie symulacji (`FakeBroker`, badania), nie handlu |
| Cel u brokera, a w dzienniku decyzja limitu czasu (EXITING) i restart | po restarcie EXITING, `recheck` listą (`_flatten`) | EXITING → FLAT | `_settle` z historii; przyczyna `TARGET` |

### 3.5 Zachowanie DE40 a cel

DE40 v1 nie ustawia celu, więc żadna z nowych ścieżek nie jest dla niego aktywna, z jednym wyjątkiem: właściciel może
ręcznie dodać cel w aplikacji do pozycji silnika. Dziś takie zamknięcie jest księgowane jako `STOP` z dodatkowym poślizgiem
stopu. Żeby R2 obowiązywało bajt w bajt, pasmo DE40 (konfiguracja bez celu) księguje `TARGET` jako `STOP` (mapowanie w
wykonawcy pasma, nie w adapterze). Alternatywa (prostsza i dokładniejsza): globalne `TARGET`; to zmienia wiersz księgi DE40
wyłącznie w przypadku ręcznego celu właściciela. Pytanie 8.

### 3.6 Limit czasu pozycji i zaplanowane spłaszczenie (R4)

- Limit czasu: `opened_at` brokera + `time_limit_min` (B1/M5: 50 min z pliku pasma). Silnik spłaszcza w pierwszym takcie
  zarządzania po terminie (`Executor.flatten(position, "TIME_LIMIT")`). W księdze przyczyna `FORCED` (zamiar silnika),
  w zdarzeniu `FLATTENED` pole `flatten_reason = TIME_LIMIT`. Badania mierzą czas od otwarcia świecy wejścia
  (`research/candle_strategies.run`: `held = b.t − nb.t`); różnica kilku sekund jest pomijalna, ale trzeba ją wpisać do
  pre-rejestracji pasma.
- Godzina spłaszczenia pasma: kalendarz pasma i `flatten_before_close_min` (NYSE 16:00 − 5 min = 15:55, zgodnie z hipotezą
  forward `GW_FLAT=15:55`).
- Wyjścia nigdy nie są blokowane bramką (niezmiennik; `RiskGate.check_exit`).

---

## 4. Bramka portfelowa (R3)

### 4.1 Miejsce

Nowy moduł `src/gwstock/portfolio.py` z klasą `PortfolioGate` (tylko odczyt, nigdy nie rzuca, jak `RiskGate`). Wołana w
`Executor._prepare` PO bramce pasma i przed identyfikatorem zlecenia; werdykt to suma powodów obu bramek (zdarzenie
`gate_block` z polem `portfolio_reasons`). Z jednym pasmem DE40 i limitami portfela nie ostrzejszymi niż `risk_v1.toml`
bramka nie dodaje żadnego powodu, którego nie dałaby bramka pasma; test równoważności to sprawdza na siatce wartości.
Uwaga: dodanie pola do `gate_block` zmienia bajty dziennika, więc pole pojawia się tylko, gdy niepuste.

### 4.2 Kontrole i powody odmowy

| Powód | Warunek blokady | Dane |
|---|---|---|
| `PORTFOLIO_MAX_POSITIONS` | liczba instrumentów z pozycją ≠ FLAT (dowolny stan, w tym BLOCKED i ENTERING) plus instrumentów z nierozstrzygniętym zleceniem ≥ `max_open_positions` | `journal.current_position` dla każdego epic z tabeli `positions`, `unresolved_orders()` |
| `PORTFOLIO_OPEN_RISK` | Σ ryzyka otwartych pozycji (4.3) + ryzyko kandydata > `max_open_risk` | jak niżej |
| `PORTFOLIO_DAILY_LOSS` | `max(0, −realized_pnl(dzień portfela))` + Σ ryzyka otwartych ≥ `max_daily_loss`, albo z kandydatem > `max_daily_loss` (ta sama formuła co `RiskGate.daily_loss`, ale po wszystkich pozycjach) | `journal.realized_pnl` |
| `PORTFOLIO_TOTAL_DRAWDOWN` | `realized_pnl_total() ≤ −max_total_drawdown` | `journal.realized_pnl_total` |
| `PORTFOLIO_LEVERAGE` | Σ nominału otwartych pozycji + nominał kandydata > `max_effective_leverage × min(virtual_capital, equity)` (EUR) | kontekst wejścia (`size`, cena), kurs (sekcja 7) |
| `PORTFOLIO_MARGIN` | wymagany depozyt kandydata (EUR) > `available × (1 − margin_reserve_fraction)`; `available` z `GET /accounts` obejmuje już depozyt wszystkich pozycji rachunku | `AccountInfo.available` |
| `PORTFOLIO_PNL_PENDING` | istnieje transakcja zamknięta bez rozliczonego wyniku w EUR (`pnl_pending:*` w `meta`, sekcja 7.4) | `meta` |
| `PORTFOLIO_FX_UNAVAILABLE` | kandydat albo otwarta pozycja w walucie innej niż EUR i brak ważnego kursu z brokera | sekcja 7.3 |
| `PORTFOLIO_UNMANAGED_EXPOSURE` (opcjonalnie, pytanie 5) | ostatni udany odczyt listy brokera pokazał pozycję na instrumencie bez pasma | wynik uzgadniania (sekcja 5.1) |
| `PORTFOLIO_GATE_ERROR` | nieoczekiwany wyjątek samej bramki | jak `GATE_ERROR` (K5) |
| `KILL` | wspólny kill switch | zostaje w bramce pasma (`RiskGate.kill_active`) |

Kontrola, której nie da się ocenić (błąd dziennika, brak kursu, nieczytelny kontekst), BLOKUJE swoim powodem (zasada
nadrzędna z `risk.py`).

### 4.3 Co liczy się jako ryzyko otwartej pozycji

Dla każdej pozycji w dzienniku w stanie ≠ FLAT (wszystkie instrumenty, także bez pasma, jeśli są w tabeli):

- znane `size`, `open_level`, `stop_level` i wartość punktu z kontekstu wejścia (`entry_ctx` przez `Executor._entry_client_id`):
  `size × |open_level − stop_level| × value_per_point`, przeliczone na EUR (sekcja 7.3);
- ENTERING albo nieznany stop, wielkość lub wartość punktu: pełne `max_loss_per_trade` pasma tego instrumentu (jak dziś
  `RiskGate._open_risk`), a dla instrumentu bez pasma `portfolio.unknown_position_risk` (nowe pole; konserwatywnie);
- BLOCKED bez pól (zgubiona transakcja): jak wyżej (zasada przejściowa H6 w `decisions-log.md`);
- stop po złej stronie albo stop wyliczony (`stop_derived`) liczy się tak samo jak znany: to jest NASZ stop z żądania.

Ryzyko pozycji to strata przy stopie; luka cenowa może ją przekroczyć (granice zabezpieczeń w `decisions-log.md`).

### 4.4 Pola konfiguracji (`config/portfolio.toml`, nowy, poza odciskiem v1)

```toml
# Limity portfela (R3). Kwoty w EUR jako napisy. Zmiana któregokolwiek pola zmienia odcisk pasm innych niż DE40 v1.
max_open_positions = 2
max_open_risk = "..."          # EUR, suma strat przy stopach otwartych pozycji
max_daily_loss = "..."         # EUR, wspólny limit dnia portfela (kill: jak S9, progi z tego pliku)
max_total_drawdown = "..."     # EUR od startu, wspólny
max_effective_leverage = "..." # nominał portfela / min(virtual_capital, equity)
virtual_capital = "..."        # EUR
margin_reserve_fraction = "..."
unknown_position_risk = "..."  # EUR, ryzyko pozycji bez znanego stopu na instrumencie bez pasma
fx_quote_epic = "EURUSD"       # instrument brokera do przeliczeń (sekcja 7.3), jeśli właściciel to zatwierdzi
fx_quote_max_age_s = 60
```

Wartości liczbowe ustala właściciel (pytanie 2). Zasada spójności: progi portfela nie mogą być łagodniejsze od
`risk_v1.toml` (ładowanie odmawia z nazwą pliku i klucza), bo `Executor._enforce_loss_limits` DE40 i tak eskaluje kill
switch na globalnym `realized_pnl` z progami `risk_v1.toml`; łagodniejszy próg portfela byłby martwy i mylący.

---

## 5. Uzgadnianie wielu pozycji

### 5.1 Pozycja na instrumencie bez pasma

Dziś: `Reconciler.run_once` filtruje `mine = [p for p in positions if p.epic == self._epic]`, więc pozycja właściciela na
innym instrumencie jest dla silnika niewidoczna (nie blokuje, nie jest liczona). Projekt:

- uzgadnianie NIGDY jej nie dotyka (niezmiennik: uzgadnianie tylko czyta) i nie zapisuje jej do tabeli `positions`;
- wynik uzgadniania pierwszego pasma w takcie niesie `unmanaged: list[OpenPosition]` (pozycje na instrumentach bez pasma,
  z tej samej odpowiedzi `list_positions`, bez dodatkowego zapytania);
- zdarzenie `UNMANAGED_POSITION` raz na deal w procesie i alarm WARN;
- depozyt takiej pozycji już obniża `available`, więc `MARGIN` widzi ją bez zmian kodu;
- czy blokuje nowe wejścia (`PORTFOLIO_UNMANAGED_EXPOSURE`): pytanie 5. Rekomendacja: TAK dla pasm innych niż DE40 v1
  (ryzyko tej pozycji jest nieznane), NIE dla DE40 v1, dopóki właściciel nie zdecyduje inaczej (dziś DE40 to dopuszcza).

### 5.2 Kolizja z pozycją ręczną właściciela na instrumencie pasma

Zachowanie istniejące i niezmienione, ale skutek staje się portfelowy:

- pozycja właściciela obok naszej (`hedgingMode` włączony albo inny kierunek przy agregacji): `FOREIGN_POSITION`, pozycja
  `BLOCKED`, kill switch SOFT (`_plan_position`, gałąź `others`). Kill switch jest wspólny, więc ręczna transakcja na US500
  blokuje też wejścia DE40 do `clear-kill`;
- przy `hedgingMode: false` (notatki §2.9; C15) ręczna transakcja w tym samym kierunku AGREGUJE się z naszym dealem:
  `SIZE_MISMATCH`, `BLOCKED`, SOFT; przy HARD awaryjne spłaszczenie zamknie całą (zagregowaną) pozycję (koszt pomyłki O9);
- pozycja właściciela na instrumencie pasma bez naszej pozycji: `FOREIGN_POSITION`, `BLOCKED` bez pól, SOFT; po jej
  zamknięciu BLOCKED zdejmuje się sam, kill switch zostaje (K7).

Zasada operacyjna do runbooka: właściciel nie handluje ręcznie instrumentami, które mają pasmo. Na instrumentach bez pasma
może (z zastrzeżeniem 5.1).

### 5.3 Rozbieżność brokera dla jednej z dwóch pozycji

Każde pasmo ma własne `BLOCKED`, a kill switch SOFT jest wspólny: rozbieżność US500 blokuje wejścia DE40, ale zarządzanie
pozycją DE40 (stop, spłaszczenie 17:25) trwa normalnie, bo SOFT nie zamyka (`Watchdog`/`KillSwitch`, spec §9). Pozycja
`BLOCKED` jednego pasma nie wpływa na macierz drugiego, pod warunkiem kluczy per instrument (ustalenie 2).

### 5.4 Kolejność przy kill switchu HARD

`Engine._manage` woła dziś `executor.emergency_flatten()` przy każdym takcie z HARD. Przy dwóch pasmach: awaryjne
spłaszczenie pasm w stałej kolejności, każde do `EMERGENCY_ATTEMPTS = 5` prób z limitem `max(1 s, submit_timeout_s / 2)` =
5 s i przerwą 1 s (`execution.py`). Najgorszy przypadek pierwszego pasma to ok. 30 s, zanim drugie dostanie pierwszą próbę.
Obie pozycje mają stop u brokera. Rekomendacja: przeplatać próby (próba 1 pasma A, próba 1 pasma B, próba 2 A...), co
wymaga podziału `emergency_flatten` na krok jednej próby; przy jednym paśmie przebieg identyczny. Ryzyko W; zadanie 12.

---

## 6. Pre-rejestracja wielu pasm i uzbrojenie

### 6.1 Odcisk per pasmo (rekomendacja), nie łączny

- **Pasmo DE40 v1**: dokładnie dzisiejszy odcisk (`prereg.config_fingerprint`: `strategy_v1.toml`, `risk_v1.toml`,
  `costs_v1.toml`, adapter `capitalcom-live`, klasa `index-cfd`, znacznik `gwstock-prereg/1`) i dzisiejszy plik
  `data/preregistration.json`. Nic się nie zmienia (R2).
- **Pasmo nowe** (np. `US500/bar_b1_m5`): odcisk z nowym znacznikiem `gwstock-prereg-lane/1`, polami: nazwa pasma, plik
  pasma (`config/lane_us500_b1_m5.toml`), `config/portfolio.toml`, plik kosztów pasma, adapter, klasa instrumentu, oraz
  SUROWE BAJTY pliku wtyku (`src/gwstock/strategy_bars.py`). Uzasadnienie bajtów kodu: reguła B1 jest w kodzie, a jej zmiana
  to nowy wariant (R5); w v1 kod `strategy.py` jest poza odciskiem (A14), co wymaga dyscypliny, a tu można to wymusić.
  Zapis w `data/preregistration-<pasmo>.json` (to samo zapisywanie atomowe 600 co `prereg._atomic_write_600`).
- **Odcisk łączny portfela** odrzucony: każda zmiana nowego pasma zmieniałaby wariant DE40 i odwrotnie.

### 6.2 Warunek uzbrojenia

- Proces live (bez zmian, `cli.mode_decision`): `live_allowed = true`, `data/ARM_LIVE`, związany rachunek, a odcisk DE40 v1
  zgodny z zamrożonym. Pytanie 3: czy proces ma wolno uzbroić, gdy DE40 nie ma odcisku, a inne pasmo ma (rekomendacja: TAK,
  warunek „co najmniej jedno pasmo z zgodnym odciskiem”; DE40 wtedy dry_run).
- Pasmo live: proces live ORAZ odcisk pasma zgodny z jego zamrożonym zapisem. Inaczej pasmo działa jako dry_run (R7):
  pełna decyzja i zdarzenie `decision`, bez zlecenia (`Executor.live = False` dla tego pasma, `GuardedBroker.open_position`
  odmawia po `req.epic`).
- Rozbrojenie (`_check_armed`, `_downgrade`) jest globalne: brak `ARM_LIVE` albo `live_allowed` obniża WSZYSTKIE pasma do
  dry_run do końca procesu (jak K8). Niezgodność odcisku jednego pasma w trakcie pracy (plik zmieniony na dysku) obniża
  tylko to pasmo; sprawdzane na początku dnia i przed każdym wejściem pasma.
- Kod nigdy nie tworzy plików uzbrojenia ani nie ustawia `live_allowed` (R7, `CLAUDE.md`).

### 6.3 Gdy część pasm nie ma odcisku

Pasma bez odcisku są dry_run, ale ich pozycje (gdyby jakieś istniały, np. z wcześniejszego uzbrojenia) są zarządzane:
wyjścia nigdy nie zależą od trybu (dzisiejszy `Mode.DRY_RUN` też zarządza pozycją). Pasmo dry_run nadal liczy się do
`PORTFOLIO_MAX_POSITIONS` tylko przez rzeczywiste pozycje w dzienniku (decyzje dry_run nie tworzą pozycji).

### 6.4 Wielkość (R6)

Per pasmo: `session_index` pasma (ustalenie 3) i plik bramki L1 pasma (pytanie 7). Pierwsze `fixed_min_size_sessions`
sesji pasma na wielkości minimalnej (US500: `minDealSize` 0,01; odczyt live 2026-11-01).

---

## 7. Waluta (R8)

### 7.1 Co wiadomo z kodu i notatek

- `capitalcom_parse.parse_instrument_rules`: `value_per_point ← instrument.lotSize` „w walucie instrumentu (C16)” i ODMOWA
  przy walucie instrumentu ≠ walucie rachunku sesji. Dla US500 (USD) na rachunku EUR silnik dziś nie dostaje reguł.
- Księga (`execution._full_trade`, `costs.CostModel.compute`): wynik brutto = poziomy × wielkość × `value_per_point`, więc w
  walucie instrumentu. `ClosedDeal` (`types.py`) nie niesie kwoty wyniku ani waluty; adapter nie czyta wyniku od brokera.
- `costs.CostModel`: `fees = fx_markup × |gross|` tylko przy `apply_fx_markup` (dla DE40 `false`). To jest narzut, nie kurs.
- Historia aktywności (§2.19): `details.currency` w przykładzie `EUR` dla DE40; czy dla US500 to `USD` (waluta instrumentu)
  czy `EUR`: **nie ustalono**.
- Historia transakcji (§2.20, C8): wiersz `TRADE` „Trade closed” z polem `size` (napis), które wygląda na kwotę wyniku, i
  polem `currency`; czy to waluta rachunku i czy kwota jest już przeliczona (z narzutem): **nie ustalono**. Typy transakcji
  obejmują `CONVERSION` i `FX_COMMISSION`, co sugeruje osobne wiersze przewalutowania: **nie ustalono**.
- Lista pozycji (§2.11): `position.currency` i `upl`; waluta `upl` dla US500: **nie ustalono**.
- Saldo (`AccountInfo`, `parse_accounts`): w EUR (rachunek EUR).

### 7.2 Co jest niepewne

1. W jakiej walucie broker rozlicza wynik zrealizowany US500 na rachunku EUR (C8).
2. Jaki kurs i narzut stosuje (C8, `fx_markup = 0.004` w `costs_v1.toml` to założenie ze spec §10, nie pomiar).
3. Czy kwota z `GET /history/transactions` da się jednoznacznie powiązać z `dealId` (pole `reference`, C8).
4. Kiedy (opóźnienie) wiersz transakcji pojawia się po zamknięciu.

### 7.3 Ryzyko przed wejściem (bez zgadywania kursu)

Kontrole w EUR (`LOSS_PER_TRADE`, `LEVERAGE`, `MARGIN`, `DAILY_LOSS`, straże wypełnienia, odległość stopu z limitu straty)
dostają kwotę w walucie instrumentu. Rekomendacja: kurs wyłącznie z brokera, z notowania instrumentu `EURUSD`
(`GuardedBroker.quote`), nie starszego niż `fx_quote_max_age_s`, po stronie niekorzystnej dla nas (strata USD → EUR przez
`bid` EURUSD, czyli większa kwota EUR). Brak kursu → `PORTFOLIO_FX_UNAVAILABLE` (wejście zablokowane), a w strażach po
wypełnieniu (wyjście nie może czekać): ostrożnie, tzn. jeśli kursu nie ma, straż ryzyka wielkości traktuje kwotę USD jak EUR
tylko wtedy, gdy ostatni znany kurs EURUSD ≥ 1 (USD słabszy), inaczej zamyka. Czy właściciel akceptuje notowanie EURUSD
brokera jako źródło przeliczeń: pytanie 9. To NIE jest zgadywanie: kurs pochodzi od brokera, ale nie jest kursem rozliczenia.

### 7.4 Księgowanie (dwufazowe dla walut obcych)

- DE40 (EUR = waluta rachunku): `Journal.record_trade` bez zmian (R2).
- Instrument w USD: faza 1 przy zamknięciu: wiersz księgi w walucie instrumentu + `pnl_pending:<deal_id>` w JEDNEJ transakcji,
  bez dopisania do `realized_pnl`; bramka portfelowa blokuje wszystkie wejścia (`PORTFOLIO_PNL_PENDING`); wyjścia działają.
  Faza 2: kwota EUR z `GET /history/transactions` (dopasowanie po czasie i referencji; zasady dopasowania dopiero po pomiarze
  C8) → `settle_trade_pnl`: dopisanie do `realized_pnl` dnia wyjścia, zapis `trade_fx:<deal_id>` (kwota EUR, źródło, kurs
  efektywny), usunięcie `pnl_pending` w JEDNEJ transakcji; potem `enforce_loss_limits`. Brak wiersza po N próbach →
  alarm i narzędzie właściciela (jak `resolve-lost-trade`), nigdy wymyślona kwota.
- Wariant uproszczony (pytanie 9): faza 2 z kursem EURUSD brokera z chwili zamknięcia, oznaczona `ESTIMATED_FX`, z późniejszą
  korektą o różnicę do kwoty z historii transakcji (korekta też w jednej transakcji z licznikiem).
- Eksport CSV (19 kolumn, T8) zostaje; waluta instrumentu i kwota EUR w osobnym eksporcie.

### 7.5 Jak to zbadać w trybie tylko do odczytu

Skrypt diagnostyczny w stylu `research/diag_account.py` (tylko odczyt, bez numerów i sekretów w wyjściu), uruchamiany przez
właściciela na VPS:

1. `GET /markets/US500`: `instrument.currency`, `lotSize`, `marginFactor`, wszystkie pola związane z walutą (zapis surowy do
   pliku lokalnego, nie do repozytorium).
2. `GET /history/transactions` z `from`/`to` za okres, w którym na rachunku były zamknięte transakcje na instrumencie w USD
   (jeśli właściciel kiedykolwiek ręcznie handlował US500/US100): `currency`, `size`, `reference`, `note`, obecność wierszy
   `CONVERSION`/`FX_COMMISSION`. Jeśli takich transakcji nigdy nie było, odczyt tylko do odczytu tego NIE rozstrzygnie:
   potrzebna próba na wielkości minimalnej US500 (0,01; ryzyko przy stopie kilku punktów to kilka centów USD), co jest
   decyzją właściciela (pytanie 6).
3. `GET /history/activity` dla tego samego okresu: `details.currency` wiersza zamknięcia.
4. `GET /markets/EURUSD` (albo epic właściwy dla pary; nazwa epiku: **nie ustalono**, sprawdzić `GET /markets?searchTerm=EURUSD`):
   dostępność notowania jako źródła przeliczeń.
5. Przy próbie (nie odczyt): `GET /positions` z otwartą pozycją US500: `position.currency` i `upl`; saldo `GET /accounts`
   przed i po zamknięciu porównane z kwotą z historii transakcji.

---

## 8. Plan zadań implementacyjnych

Zadania 1–3 są w planie. Numeracja dalsza. „Równolegle” znaczy: osobne pliki, bez wspólnych edycji; „po kolei” znaczy:
zależność interfejsu albo ten sam plik. Każde zadanie: TDD, `Decimal`, wartości z konfiguracji, testy
`python -m pytest -m "not integration_live_readonly" -q -W error` zielone, `python scripts/hooks/guard_live.py --scan` czyste,
pliki `*_v1.toml` bajt w bajt.

### Zadanie 4 (pierwsze, po kolei przed wszystkimi zmianami w `src/`): złote ślady DE40 — MAŁE/ŚREDNIE

- Pliki: `tests/fault/test_golden_de40.py`, `tests/fault/golden/*.json` (dane), bez zmian w `src/`.
- Co: dla scenariuszy z `test_engine_day.py` i `test_trade_lifecycle_e2e.py` (dzień z wejściem i spłaszczeniem 17:25, wybicie
  stopem, restart w trakcie pozycji, dzień bez sygnału, dry_run, HARD z awaryjnym spłaszczeniem, zamknięty rynek przy
  spłaszczeniu) zapis: sekwencja `FakeBroker.calls` z czasem zegara symulacji, pełna zawartość tabel `orders`, `positions`,
  `trades`, `realized_pnl`, `kill_state`, `meta` oraz `raw_events` (źródło, ładunek, czas), plus `DayReport`.
- Kryterium: test porównuje bajt w bajt; zapis wzorca tylko jawnym przełącznikiem środowiska, nigdy automatycznie.
- Uwaga: identyfikatory `REF-n`/`DEAL-n` FakeBrokera są deterministyczne, czas z `SimClock`, więc ślad jest stabilny.

### Zadanie 5 (równolegle z 6): dziennik i liczniki per instrument — MAŁE

- Pliki: `src/gwstock/journal.py`, `tests/unit/test_journal.py`.
- Interfejsy: `count_entry_orders(day, epic: str | None = None)`, `count_smoke_trades(epic: str | None = None)`,
  `trades(epic: str | None = None)`; `record_trade_pending`, `settle_trade_pnl` (sekcja 7.4), bez zmiany schematu.
- Kryteria: bez argumentu zachowanie identyczne (istniejące testy); z `epic` tylko ten instrument; zakres po kluczu głównym.

### Zadanie 6 (równolegle z 5): typy i FakeBroker wielu instrumentów z celem — ŚREDNIE

- Pliki: `src/gwstock/types.py`, `src/gwstock/broker/fake.py`, `src/gwstock/broker/base.py`, `tests/unit/test_fake_broker.py`,
  `tests/unit/broker_contract.py`, `tests/unit/test_types.py`.
- Interfejsy: sekcja 2.6; `Broker.set_protection`; `FakeBroker(rules=[...])`, `set_market_closed(flag, epic=None)`,
  `trigger_target(deal_id)`; konwencja „stop i cel w jednej świecy: STOP” w symulacji świec (R4).
- Kryteria: wszystkie istniejące testy bez zmian (konstruktor z jednym `rules` zostaje); nowe testy kontraktu dla celu.

### Zadanie 7 (po 6): adapter Capital.com: cel, przyczyna TARGET, waluta instrumentu — ŚREDNIE

- Pliki: `capitalcom_trading.py`, `capitalcom_parse.py`, `capitalcom_history.py`, `capitalcom.py` (`instrument_rules`),
  testy `tests/unit/test_capitalcom_trading.py`, `test_capitalcom_adapter.py`, `fake_capital_server.py`.
- Kryteria: ciało `POST /positions` bez celu identyczne (test bajtów); `profitDistance` tylko z celem; `set_protection` wysyła
  oba poziomy i sprawdza oba w potwierdzeniu; `TP` → `TARGET`; `parse_instrument_rules(..., allow_foreign_currency=True)`
  zwraca `currency`, domyślnie odmowa jak dziś.

### Zadanie 8 (po 5 i 6): uzgadnianie per instrument — ŚREDNIE

- Pliki: `src/gwstock/reconcile.py`, `src/gwstock/cli.py` (tylko `history_start` w `reset_position`), `tests/fault/test_reconcile.py`.
- Interfejsy: `Reconciler(..., epic, legacy_keys: bool)`; `history_start(journal, position)` czyta znacznik instrumentu;
  `ReconcileResult.unmanaged`, `ReconcileResult.untargeted`.
- Kryteria: test „dwa uzgadniania na jednym dzienniku”: zamknięcie US500 zaksięgowane później nie zmienia początku historii
  DE40; `mismatch_since` jednego nie czyści drugiego; DE40 używa dotychczasowych kluczy (złoty ślad bez zmian).

### Zadanie 9 (po 5 i 6): wykonawca świadomy pasma — DUŻE

- Pliki: `src/gwstock/execution.py`, `src/gwstock/costs.py` (TARGET bez dodatkowego poślizgu), `tests/fault/test_execution.py`.
- Interfejsy: `LaneSpec`; `ExitMemory.last_own_deal_by_epic`; `_exit_reason` z `TARGET`; `flatten(position, "TIME_LIMIT")`;
  przeliczenie walut przez wstrzyknięty `FxView` (sekcja 7.3); dwufazowe księgowanie dla walut obcych.
- Kryteria: przy `LaneSpec` DE40 złoty ślad bez zmian; nowe testy: wyścig limit czasu / cel, awaryjne spłaszczenie dwóch
  pasm przy nieczytelnym dzienniku zamyka oba własne deale, straż wielkości w USD.

### Zadanie 10 (po 5; równolegle z 8 i 9): bramka portfelowa i konfiguracja portfela — ŚREDNIE

- Pliki: `src/gwstock/portfolio.py` (nowy), `src/gwstock/config.py` (`PortfolioConfig`, `load_portfolio`),
  `config/portfolio.toml` (wartości od właściciela), `tests/unit/test_portfolio.py`.
- Kryteria: tabela 4.2 z granicami „równo przechodzi, o grosz więcej blokuje”; nieoceniona kontrola blokuje; test
  równoważności z `RiskGate` przy jednym paśmie DE40; konfiguracja łagodniejsza od `risk_v1.toml` odrzucona przy ładowaniu.

### Zadanie 11 (po zadaniach 1 i 2): pasmo B1 (decyzja, okna, filtry dnia) — ŚREDNIE

- Pliki: `src/gwstock/lanes.py` (nowy: `LaneSpec`, wtyki, okna), rozszerzenie pliku pasma z zadania 2, `tests/unit/test_lanes.py`.
- Zakres, którego zadanie 2 NIE obejmuje, a który jest w badaniach (`research/candle_strategies.run`) i musi być w paśmie
  albo jawnie odrzucony w pre-rejestracji: `MAX_DAY = 10` wejść dziennie, dzienny stop `DAY_STOP_R = −3,0 R`,
  `MIN_STOP_FRAC = 0,0001` (`STOP_TOO_CLOSE`), filtr poślizgu `0,5 × ATR` (`SLIP_FILTER`, ATR ze świec M5), koszt > 0,20 R
  (`COST_OVER_0.20R`), luka w danych (`GAPPED_DATA`), brak nakładania transakcji (kolejna dopiero po wyjściu poprzedniej),
  wejście na otwarciu następnej świecy. Wszystkie progi z pliku pasma.
- Kryteria: test krzyżowy decyzji pasma z `research/candle_strategies.run` na danych syntetycznych (pomijany bez `research/`).

### Zadanie 12 (po 4, 5, 8, 9, 10, 11): silnik z pasmami — DUŻE (po kolei, jeden agent)

- Pliki: `src/gwstock/app.py`, `src/gwstock/watchdog.py`, `tests/fault/test_engine_day.py` (tylko nowe testy), `tests/fault/test_watchdog.py`.
- Zakres: sekcja 1.3, 2.1, 2.8, 5.4; per instrument: `market_closed_at`, straż ciszy, okna, `last_quote`, liczniki uzgadniania.
- Kryteria: złote ślady DE40 bez zmian; z jednym pasmem kolejność wywołań brokera identyczna; dwa pasma działają w jednej
  korutynie (żadnych zadań w tle); pętla dnia obejmuje sesję NYSE.

### Zadanie 13 (po 12; równolegle z 14 i 15): pre-rejestracja i uzbrojenie pasm, CLI — ŚREDNIE

- Pliki: `src/gwstock/prereg.py` (nowe funkcje, stare bez zmian), `src/gwstock/cli.py`, `tests/unit/test_prereg.py`,
  `tests/fault/test_modes.py` (nowe testy), runbook. Ewentualna zmiana `scripts/hooks/guard_live.py` TYLKO po zgodzie właściciela.
- Kryteria: odcisk v1 identyczny (nowy test porównuje `prereg.config_fingerprint(config/)` z wartością policzoną PRZED
  zmianą i wpisaną do testu; odciski v1 zamrożono na VPS 2026-11-01, commit `b3d6893`); pasmo bez odcisku = dry_run; rozbrojenie
  globalne; `status`, `reset-position`, `resolve-lost-trade` z `--epic`.

### Zadanie 14 (po 12): symulacja wielu pasm i testy odporności — DUŻE

- Pliki: `tests/fault/engine_sim.py` (rozszerzenie, plik jest na liście wyjątków strażnika: nowe fikstury uzbrajające tylko w
  katalogu tymczasowym), `tests/fault/test_multi_lane.py` (nowy; nowy wyjątek strażnika wymaga zgody właściciela, więc
  preferowane: uzbrajanie wyłącznie przez istniejące funkcje `engine_sim.arm`).
- Zakres: sekcja 9.

### Zadanie 15 (po 5; równolegle): raport per pasmo — MAŁE/ŚREDNIE

- Pliki: `src/gwstock/report/*.py`, `tests/unit/test_report.py`.
- Kryteria: L1 DE40 liczy tylko transakcje DE40; incydenty portfela liczą się dla każdego pasma; raport DE40 przy jednym
  paśmie identyczny.

### Zadanie 16 (w każdej chwili; równolegle): diagnostyka walut tylko do odczytu — MAŁE

- Pliki: `research/diag_fx.py` (nowy), test jednostkowy parsera na danych syntetycznych.
- Zakres: sekcja 7.5 kroki 1–4. Uruchamia właściciel; agent nie łączy się z brokerem.

Kolejność krytyczna: 4 → (5 ∥ 6) → (7 ∥ 8 ∥ 9 ∥ 10, z 7/8/9 po 6) → 11 (po 1, 2) → 12 → (13 ∥ 14 ∥ 15). Zadanie 16 i
decyzje właściciela (sekcja 10) blokują dopiero uzbrojenie pasma US500, nie kod.

---

## 9. Plan testów odporności

### 9.1 Rozszerzenie `tests/fault/engine_sim.py`

- `Market` per instrument (DE40: dzisiejszy; US500: mid ok. 7583, spread 0,6, krok 0,1, `min_size` 0,01, depozyt 5%,
  waluta USD) i notowanie `EURUSD` dla przeliczeń.
- `FakeBroker` z regułami obu instrumentów, zamkniętym rynkiem per instrument i `trigger_target`.
- Dzień wzorcowy wielu pasm: poniedziałek 2026-11-02 (po zmianie czasu w USA 1.11 i w UE 25.10: Berlin UTC+1, NY UTC−5);
  DE40 09:00–17:30 Berlin, NYSE 09:30–16:00 NY = 15:30–22:00 Berlin; świece M5 US500 z sygnałem B1 w oknie 09:45–11:45 NY.
- `EngineFactory` przyjmuje listę pasm; bez listy = dzisiejsza fabryka (złote ślady).

### 9.2 Scenariusze awarii (każdy z asercjami niezmienników: `assert_books_balance` z `e2e_support.py`, brak wejścia przy
kill switchu, brak FLAT bez księgowania, żadnej drugiej wysyłki zamknięcia bez odczytu listy)

1. **Restart w trakcie dwóch pozycji.** DE40 OPEN ze stopem, US500 OPEN ze stopem i celem; silnik zatrzymany i uruchomiony
   ponownie; w przerwie US500 zamknięty celem, DE40 wybity stopem. Oczekiwane: oba zaksięgowane z właściwą przyczyną
   (`TARGET`, `STOP`), oba FLAT, liczniki dnia portfela równe sumie, `closed_marker` per instrument, brak `BLOCKED`.
2. **Ten sam restart, ale zamknięcia w odwrotnej kolejności czasu** (US500 zamknięty wcześniej niż DE40, księgowanie DE40
   później): regresja ustalenia 2 (globalny `closed_marker`).
3. **Rozbieżność brokera dla jednej z dwóch pozycji.** US500 z inną wielkością u brokera (`SIZE_MISMATCH`). Oczekiwane:
   US500 BLOCKED, SOFT; DE40 OPEN zarządzany, spłaszczony 17:25 i zaksięgowany; nowe wejście DE40 następnego dnia
   zablokowane do `clear-kill` (wspólny kill).
4. **Zamknięcie przez cel z opóźnioną historią.** US500 znika z listy, historia pokazuje zamknięcie dopiero po 90 s.
   Oczekiwane: BLOCKED `LOCAL_POSITION_MISSING`, SOFT `MISMATCH_TIMEOUT` po 60 s, księgowanie po pojawieniu się historii,
   przyczyna `TARGET`, bez `CLOSURE_NOT_IN_HISTORY`.
5. **Wyścig limitu czasu i celu.** W chwili `DELETE` (limit 50 min) cel zamyka pozycję (`on_call` FakeBrokera). Oczekiwane:
   brak drugiej wysyłki, przyczyna `TARGET`, nie `FORCED`.
6. **Kill switch twardy przy dwóch pozycjach.** `data/HARD_STOP` przy otwartych DE40 i US500. Oczekiwane: obie pozycje
   zamknięte w ciągu limitu (przeplatane próby), obie zaksięgowane `FORCED`, HARD zostaje do `clear-kill`.
7. **HARD przy dwóch pozycjach i nieczytelnym dzienniku.** Oczekiwane: zamknięte oba NASZE deale tego procesu (po jednym na
   instrument), tylko te, które broker listuje; regresja `ExitMemory.last_own_deal`.
8. **HARD, gdy zamknięcie pierwszego pasma ciągle zawodzi.** Oczekiwane: drugie pasmo dostaje próbę w pierwszych sekundach
   (przeplatanie), nie po 30 s.
9. **Zamknięty rynek US500 przy spłaszczeniu 15:55 NY** i spłaszczenie DE40 tego samego dnia: brak wstrzymania DE40
   (regresja `market_closed_at` per instrument). Wariant: zamknięty rynek DE40 o 17:25 nie wstrzymuje zarządzania US500.
10. **Cisza notowań tylko na US500.** Oczekiwane: SOFT `QUOTE_SILENCE` z nazwą US500; notowania DE40 nie zerują zegara US500.
11. **Pozycja właściciela na instrumencie bez pasma (US100)**: zdarzenie `UNMANAGED_POSITION`, nic nie dotknięte;
    wejście zgodnie z decyzją z pytania 5.
12. **Ręczna transakcja właściciela na US500 przy otwartej pozycji silnika (agregacja)**: `SIZE_MISMATCH`, SOFT; DE40 zarządzany.
13. **Wynik w USD bez kwoty EUR** (historia transakcji nieczytelna): `PORTFOLIO_PNL_PENDING` blokuje wejścia obu pasm,
    wyjścia działają; po pojawieniu się kwoty rozliczenie atomowe i eskalacja limitów.
14. **Limit dzienny portfela przekroczony przez sumę dwóch pasm** (każde z osobna pod limitem): kill SOFT/HARD wg progów.
15. **Wejście dwóch pasm w tym samym takcie** (DE40 decyzja 09:05 i świeca US500, symulacja czasu nakładających się okien):
    szeregowanie przez `OrderManager.in_flight`, uzgadnianie obu pomija przebieg w trakcie wejścia, brak `FOREIGN_POSITION`.
16. **Pasmo bez odcisku przy procesie live**: zero `open_position` dla tego epiku (także `GuardedBroker` po `req.epic`), DE40 live.
17. **Rozbrojenie w trakcie dnia przy dwóch pozycjach**: obie dalej zarządzane i spłaszczone, zero nowych wejść.
18. **Święto NYSE w dniu sesji XETRA** (i odwrotnie): pasmo bez sesji tylko zarządza pozostałością.

---

## 10. Otwarte pytania do właściciela

Wyłącznie decyzje, których nie da się ustalić z kodu.

1. **Czy praca DE40 razem z innymi pasmami (wspólny kill switch, wspólne limity, wspólny depozyt) jest nadal wariantem
   `orb_index_cfd v1`, czy nowym wariantem DE40 (K+1)?** Skutek: HARD z US500 zamyka DE40, SOFT z US500 blokuje wejście DE40,
   limit portfela może zablokować DE40. Konfiguracja z samym DE40 jest identyczna w każdym przypadku.
2. **Wartości `config/portfolio.toml`**: `max_open_positions` (2?), `max_open_risk`, `max_daily_loss`, `max_total_drawdown`,
   `max_effective_leverage`, `virtual_capital`, `margin_reserve_fraction`, `unknown_position_risk`. Czy limity dzienny (1,50 €)
   i łączny (12 €) z `risk_v1.toml` mają być wspólne dla portfela bez zmiany, czy ostrzejsze?
3. **Czy proces wolno uzbroić, gdy DE40 v1 nie ma zgodnego odcisku, a inne pasmo ma** (DE40 wtedy dry_run)?
4. **Klucz dnia limitu straty dla US500**: data berlińska (rekomendacja, jeden dzień portfela) czy nowojorska?
5. **Pozycja właściciela na instrumencie bez pasma**: czy blokuje nowe wejścia pasm (wszystkich, tylko nowych, żadnych)?
6. **Próba `smoke` na US500 (wielkość 0,01) na prawdziwym rachunku**, potrzebna do rozstrzygnięcia waluty wyniku (C8), pól celu
   (C5, C5d) i przyczyny `TP` (C7), jeśli odczyt tylko do odczytu (sekcja 7.5) nie wystarczy. Czy i kiedy?
7. **Bramka wielkości per pasmo**: osobny plik (np. `data/L1_PASSED_US500`, wymaga zmiany strażnika `scripts/hooks/guard_live.py`
   za Twoją zgodą) czy jeden plik z listą pasm?
8. **Zamknięcie DE40 przez cel ustawiony ręcznie przez Ciebie**: księgować jak dziś (`STOP` z dodatkowym poślizgiem) czy
   dokładnie (`TARGET`)? Pierwsze zachowuje v1 bajt w bajt.
9. **Przeliczenie USD → EUR**: czy notowanie EURUSD Capital.com (strona niekorzystna, nie starsze niż 60 s) jest akceptowalnym
   źródłem dla kontroli ryzyka i szacunku wyniku, dopóki kwota EUR z historii transakcji nie zostanie rozliczona? Czy do tego
   czasu wszystkie wejścia mają być blokowane (rekomendacja: tak)?
10. **Filtry dnia z badań dla B1/M5** (`MAX_DAY = 10`, dzienny stop −3 R, filtr poślizgu 0,5 ATR, koszt > 0,20 R, luka danych):
    wchodzą do pasma i pre-rejestracji w tej postaci? Zadanie 2 ich nie obejmuje, a hipoteza forward
    (`docs/preregistration/forward-us500-b1-m5-sesje.md`) mierzy regułę razem z nimi.
11. **Czy pasmo US500 może handlować na żywo przed werdyktem testu forward** (50 nowych transakcji), czy do tego czasu tylko
    dry_run? Plan zakłada budowę równolegle; uzbrojenie przed werdyktem to osobna decyzja.
12. **Ręczny handel instrumentami pasm**: potwierdzenie zasady „nie handluję ręcznie DE40 ani US500, gdy mają pasmo”
    (sekcja 5.2), która trafi do runbooka.

---

## Załącznik A: lista kontrolna ustaleń bezpieczeństwa dla przeglądu (risk-reviewer, order-state-auditor)

| # | Ustalenie | Plik, funkcja | Zadanie |
|---|---|---|---|
| A1 | `closed_marker` globalny przesuwa początek historii innego instrumentu | `reconcile.history_start`, `Reconciler._apply` | 8 |
| A2 | `mismatch_since` globalny: jedno uzgadnianie czyści drugie | `Reconciler._plan_mismatch` | 8 |
| A3 | `count_entry_orders` globalny: pasma zużywają sobie wejście dnia | `journal.count_entry_orders`, `RiskGate`, `Engine._entries_today` | 5, 12 |
| A4 | `session_index` globalny: przyspiesza wielkość reguł DE40 | `Engine.session_index`, `strategy.decide` | 5, 12 |
| A5 | ryzyko otwarte tylko własnego instrumentu i wartością punktu kandydata | `RiskGate._open_risk` | 10 |
| A6 | `market_closed_at` globalny wstrzymuje spłaszczenie innego pasma | `_SessionGuard`, `GuardedBroker.close_position`, `Engine._market_closed_backoff` | 12 |
| A7 | notowanie dowolnego instrumentu zeruje straż ciszy | `GuardedBroker.quote` (`on_quote`), `Watchdog.note_quote` | 12 |
| A8 | udany przebieg uzgadniania jednego pasma zeruje licznik awarii drugiego | `Watchdog.note_reconcile_ok` | 12 |
| A9 | `last_own_deal` jeden: awaryjne spłaszczenie bez dziennika zamyka jedną pozycję | `ExitMemory`, `Executor._flatten_without_journal` | 9 |
| A10 | `TP` księgowany jako `STOP` (z dodatkowym poślizgiem) | `capitalcom_history.REASONS`, `costs.CostModel.compute` | 7, 9 |
| A11 | zamiar silnika wygrywa z celem w przyczynie wyjścia | `Executor._exit_reason` | 9 |
| A12 | `PUT` ze samym stopem może skasować cel (C5d) | `TradingMixin.set_stop` | 7 |
| A13 | waluta instrumentu ≠ rachunku: odmowa reguł, a po jej zdjęciu kwoty USD w licznikach EUR | `parse_instrument_rules`, `_full_trade`, `record_trade` | 7, 9 |
| A14 | pętla dnia zna jeden kalendarz; po 17:30 spoczynek do północy | `Engine.run_forever`, `_run_day`, `_idle_until` | 12 |
| A15 | awaryjne spłaszczenie po kolei: drugie pasmo czeka do ok. 30 s | `Executor.emergency_flatten`, `Engine._manage` | 9, 12 |
| A16 | FakeBroker: jeden instrument, jeden znacznik zamkniętego rynku | `broker/fake.FakeBroker._require_epic`, `set_market_closed` | 6 |
| A17 | bramka L1 i incydenty liczone po całym dzienniku | `report/gates.gate_l1_status`, `_mismatch_days` | 15 |
