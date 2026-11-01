# Odbiór v1: kryteria akceptacji i bramki L0, L1, L2

Dokument odbioru silnika `gw-stock-engine` v1 (zadanie 18 planu). Kod: gałąź `feat/engine-v2`; stan dowodów offline spisano dla commitu `ca79717` (2026-11-01). Część „offline” wykonał agent w sesji deweloperskiej (bez VPS, bez klucza, bez sieci do brokera). Resztę wykonuje **właściciel na VPS** według `docs/ops/runbook.md`. Polecenia poniżej są powtórzone dokładnie tak, jak w runbooku (katalog `/home/claude/gw-stock-engine`, użytkownik `claude`); gdyby kiedykolwiek różniły się od runbooka, obowiązuje runbook, a różnicę trzeba zgłosić.

Dokument nie jest dowodem, że silnik jest bezpieczny albo opłacalny. Jest uczciwym wykazem tego, co sprawdzono, czego nie sprawdzono i kto ma to sprawdzić.

## 1. Zastrzeżenia (przeczytaj najpierw)

1. **Rentowność NIE jest wykazana i nie jest tu sprawdzana.** Sukces v1 to silnik pracujący bez niewyjaśnionych rozbieżności z brokerem, który dostarcza zalogowane dane o kosztach i zachowaniu brokera przy minimalnym ryzyku (spec. sekcja 1). Rozsądne założenie robocze to wynik netto zerowy lub ujemny.
2. **Gotowość techniczna jest udowodniona dopiero po integracji, `dry_run` i `smoke`.** Testy offline sprawdzają logikę silnika na atrapach (`FakeBroker` i lokalny serwer udający Capital.com). Nie dowodzą, jak zachowuje się prawdziwy broker: niepotwierdzone założenia o jego API są wypisane w runbooku (tabela „Ograniczenia / do potwierdzenia”) i w `docs/ops/capitalcom-api-notes.md`, sekcja 6.
3. **Nic, co wymaga VPS, żywego brokera albo upływu czasu, nie jest tu oznaczone jako udowodnione** (pamięć pod limitem cgroup, sesje `dry_run`, ćwiczenia, `smoke`, pierwsze 5 sesji live).
4. **Statystyka (to rzędy wielkości, nie wyniki):**
   - „±8 € rocznie” to **nie** przedział ufności, tylko ok. jedno odchylenie standardowe rocznej sumy wyników przy wielkości z reguł, przy założeniach: ok. 250 transakcji, niezależne transakcje, odchylenie jednej transakcji 2–2,8 R, R ≈ 0,18–0,25 € (`docs/preregistration/v1.md`, sekcja 6).
   - „4,8 mln transakcji” (wykrycie przewagi +0,002 R, specyfikacja) i „ok. 9,5 mln” (te same dane przy standardowych założeniach: test dwustronny 5%, moc 80%, σ = 2,2 R) różnią się dwukrotnie. To **rząd wielkości**; wniosek jest ten sam: wykrycie przewagi tej skali jest nieosiągalne.
   - Jedna transakcja dziennie to ok. 250 rocznie, więc **1000 transakcji (bramka L2) to ok. 4 lata**. Dlatego v1 nie planuje skalowania kapitału.
5. **Wynik sesji na wielkości minimalnej to grosze i szum.** Strata na stopie przy wielkości 0,001 wynosi ok. 0,03 € (spec. sekcja 5), a księga zaokrągla każdy składnik kosztu do 0,01 €. Raport w tabeli dnia pokazuje poślizg „z modelu konfiguracji”, a nie pomiar; mierzony poślizg wejścia jest w bloku bramki L1.
6. **To zdjęcie stanu, nie świadectwo na zawsze.** Po każdej zmianie kodu powtórz testy i sprawdź CI; statusy poniżej dotyczą commitu z nagłówka.

## 2. Znaczenie kolumny „Status”

Każdy wiersz ma dokładnie jeden z trzech statusów:

- `PROVEN OFFLINE (test / zestaw)`: udowodnione testem uruchomionym bez VPS, klucza i brokera (w sesji deweloperskiej albo w CI). W nawiasie nazwa testu, zestawu albo uruchomienia. Dotyczy wyłącznie tego, co ten test sprawdza, na atrapach.
- `OWNER STEP — NOT YET DONE`: wymaga VPS, żywego brokera, klucza, czasu albo decyzji właściciela, i nie zostało jeszcze wykonane. Po wykonaniu właściciel wpisuje datę i wynik w kolumnie „Wynik właściciela” (sekcje 4 i 5).
- `NOT PROVEN`: nie jest udowodnione ani offline, ani żadnym krokiem właściciela opisanym dziś w runbooku. Wymaga pracy (kod, test albo decyzja) albo jest znanym otwartym punktem. W komórce jest powód.

## 3. Co faktycznie uruchomiono offline

| Uruchomienie | Wynik |
|---|---|
| Pełny zestaw: `python -m pytest -m "not integration_live_readonly"` z korzenia czystej kopii commitu `ca79717` (Python 3.13.18) | `3025 passed, 8 deselected, 11 xfailed` w ok. 69 s. 8 odznaczonych to odczyty live (`integration_live_readonly`, tylko VPS). 11 `xfailed` to znane, jawnie oznaczone punkty (`xfail(strict=True)`): 6 luk strażnika live w `tests/unit/test_guard_live.py` oraz 5 przypadków dwóch defektów kodu znalezionych przez e2e (BUG-E2E-1 i BUG-E2E-2, sekcja 7) |
| CI GitHub Actions, run `41270735303` na `ca79717` | `tests (Python 3.12)` i `tests (Python 3.13)` zielone. **`image` (testy w obrazie, bez sieci) CZERWONE:** `4 failed, 2565 passed, 462 skipped, 8 deselected, 5 xfailed`; cztery testy z `tests/unit/test_prereg.py` czytają `docs/` i `.gitattributes`, których nie ma w obrazie testowym |
| Polecenia z runbooka sprawdzone z kodem | `python -m gwstock --help` i `--help` każdego polecenia, `python -m gwstock.prereg --help`, `python -m gwstock.report --help`, `docker compose -f docker/compose.yml config` (usługi `gw-stock-engine`, `gw-stock-test`, `gw-stock-live-readonly`; wartości limitów z punktu 1.6 runbooka zgodne); `prereg --print`, `--write`, `--check`, `--write --force`, `report --day`, `--all`, `--csv` uruchomione na czystej kopii z pustym katalogiem danych |
| Pomiar pamięci pod limitem cgroup | **Nie wykonano.** W sesji deweloperskiej jest klient Dockera bez demona, a pomiar ma sens tylko na jądrze VPS |

## 4. Kryteria akceptacji v1 (spec. sekcja 17)

Polecenia oznaczone `L0.n` i `L1.n` są w sekcji 5 (wraz z oczekiwanym wynikiem i miejscem na wynik właściciela).

### K1. Testy przechodzą (spec. 17.1)

Co dowodzi: logika silnika, kontrakt adaptera i niezmienniki działają na atrapach; testy są powtarzalne lokalnie, w CI (Python 3.12 i 3.13) i w obrazie Docker.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K1a. Pełny zestaw lokalnie | `python -m pytest -m "not integration_live_readonly"` | ostatnia linia `... passed ...` bez `failed` i `error` | agent, offline | PROVEN OFFLINE (pełny zestaw, czysta kopia `ca79717`, Python 3.13) |
| K1b. CI, Python 3.12 i 3.13 | zakładka Actions na GitHubie, zadania `tests (Python 3.12)` i `tests (Python 3.13)` | oba zielone | agent / GitHub | PROVEN OFFLINE (CI run `41270735303` na `ca79717`: oba zadania zielone) |
| K1c. Testy w obrazie, w CI | zadanie `image` w CI (cel `test`, bez sieci) | zielone | agent / GitHub | NOT PROVEN (na `ca79717` czerwone: 4 testy z `tests/unit/test_prereg.py` nie znajdują w obrazie `docs/` ani `.gitattributes`; poprawka w sekcji 7) |
| K1d. Testy w obrazie, na VPS | L0.1 | brak `failed` i `error` | właściciel | OWNER STEP — NOT YET DONE (do poprawki z K1c krok pokaże te same 4 `failed`) |
| K1e. Każdy niezmiennik (1) do (6) ma co najmniej jeden test | pytest, jak K1a | (1) `test_intent_row_exists_before_broker_call`; (2) `test_timeout_then_position_found_is_adopted_not_duplicated`, `test_flatten_timeout_reconciles_instead_of_resending`; (3) `test_leverage_at_the_limit_passes_and_a_tick_over_blocks`; (4) `test_daily_loss_survives_reopen_and_is_keyed_by_exit_day`; (5) `test_exits_are_never_blocked_even_when_kill_is_hard`, `test_an_active_kill_never_blocks_flatten`; (6) `test_concurrent_logins_share_one_post_session` | agent, offline | PROVEN OFFLINE (`tests/fault/test_order_manager.py`, `test_execution.py`, `test_reconcile.py`; `tests/unit/test_risk.py`, `test_journal.py`, `test_capitalcom_adapter.py`) |
| K1f. Niezmienniki sprawdzane po KAŻDYM teście awarii wspólnym haczykiem | – | – | – | NOT PROVEN (w HEAD nie ma wspólnego haczyka; niezmienniki są asercjami w poszczególnych testach; testy e2e czytają stan z dysku i sprawdzają rozliczenie księgi (`assert_books_balance`), ale nie wszystkie sześć niezmienników po każdym teście) |
| K1g. Kontrakt adaptera na `FakeBroker` i na adapterze Capital.com z lokalnym serwerem atrapą | pytest, jak K1a | te same testy kontraktu przechodzą dla obu | agent, offline | PROVEN OFFLINE (`tests/unit/broker_contract.py` użyty w `test_fake_broker.py` i `test_capitalcom_trading.py`) |
| K1h. Kontrakt w odczytach live | L0.2 | `8 passed` | właściciel | OWNER STEP — NOT YET DONE |

### K2. `dry_run`, potem pierwsza sesja live (spec. 17.2)

Co dowodzi: cały dzień pracy (logowanie, potwierdzenie rachunku, zakres otwarcia, sygnał, wejście ze stopem, nadzór, spłaszczenie, raport) przebiega bez ingerencji, najpierw bez zleceń, potem na wielkości minimalnej.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K2a. Logika pełnego dnia na atrapie | pytest `tests/fault/test_engine_day.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_full_day_long_entry_flat_at_1725_and_report`, `test_startup_reconcile_runs_before_any_entry_and_the_stop_is_attached`, `test_short_entry_is_booked_with_its_result` w `tests/fault/test_engine_day.py`) |
| K2b. `dry_run` nie wysyła zleceń | pytest `tests/fault/test_modes.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_dry_run_never_calls_open_position`, `test_dry_run_broker_refuses_an_entry_even_if_asked`) |
| K2c. Co najmniej jedna pełna sesja `dry_run` na notowaniach live, bez rozbieżności i błędów | L0.3 | w logu `decision`/`DayReport`, w `data/alerts.log` brak `ERROR`, `status`: `unresolved orders: 0` | właściciel | OWNER STEP — NOT YET DONE |
| K2d. Pierwsza świeca to świeca 09:00 (semantyka stempla, odstępstwo D2) | L0.6 i krok [7] w L0.8 | O, H, L, C z silnika zgodne z wykresem 09:00 do 09:05 | właściciel | OWNER STEP — NOT YET DONE |
| K2e. Pierwsza sesja live na wielkości minimalnej, bez ingerencji, aż po raport | L1.1 i L1.2 | `status`: pozycja `FLAT` po 17:30, raport z jedną transakcją | właściciel | OWNER STEP — NOT YET DONE |

### K3. Zabicie procesu w środku wejścia i wyjścia oraz OOM (spec. 17.3)

Co dowodzi: po nagłym zatrzymaniu nie powstaje zdublowane zlecenie ani pozycja osierocona, a zamknięcie jest księgowane raz.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K3a. Restart w środku wejścia: pozycja zaadoptowana, bez duplikatu | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_restart_mid_entry_adopts_position_without_duplicate` w `test_engine_day.py`; `test_timeout_then_position_found_is_adopted_not_duplicated` w `test_order_manager.py`; `test_crash_between_writes_submitting_order_with_open_position_is_confirmed` w `test_reconcile.py`). To symulacja restartu (dziennik zamknięty i otwarty od nowa), nie prawdziwy `kill -9` |
| K3b. Restart w środku wyjścia i po wybiciu stopu: jedno zaksięgowanie, żadnej pozycji osieroconej | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_crash_between_writes_open_position_already_stopped_out_is_confirmed_and_booked` w `test_reconcile.py`; `test_resolve_after_crash_between_writes_never_unbooks_a_filled_trade` w `test_order_manager.py`; `test_stop_hit_by_broker_is_booked_once` w `test_engine_day.py`; `test_blocked_lost_trade_survives_restarts_and_keeps_raising_the_alarm` w `test_lost_trade_regression.py`) |
| K3c. Prawdziwe zabicie procesu w kontenerze, w sesji | L0.5 (ćwiczenia 4.3 i 4.3b) | `Exited (137)` i ręczny start (4.3); samoczynny restart (4.3b); `unresolved orders: 0`, bez `ERROR` | właściciel | OWNER STEP — NOT YET DONE |
| K3d. Zabicie przez OOM | – | – | – | NOT PROVEN (OOM nie jest wywoływany celowo na współdzielonym VPS; skutek dla silnika jest taki jak przy `kill -9`, który ćwiczy K3c, ale samo zachowanie po zabiciu przez jądro nie zostało sprawdzone; limity i `OOMKilled=false` sprawdza K6) |
| K3e. Pierwsze sesje live: brak zdublowań i osieroconych pozycji | L1.2 | `status`/raport bez `ENTRY_UNRESOLVED`, jedna transakcja na sesję | właściciel | OWNER STEP — NOT YET DONE |
| K3f. Scenariusze e2e cyklu życia: restart z dysku w każdym ważnym punkcie (zgubione potwierdzenie i stop, jedno zaksięgowanie, timeout zamknięcia, twardy kill przy pozycji, awaria zapisu dziennika, atomowość księgowania, zmiany u brokera przy wyłączonym silniku, pełny dzień z restartem) | pytest `tests/fault/test_trade_lifecycle_e2e.py` | przechodzi (57 przeszło, 5 `xfail` opisanych niżej) | agent, offline | PROVEN OFFLINE (`tests/fault/test_trade_lifecycle_e2e.py`, scenariusze a do h i kryteria A do C, nazwy w sekcji 6). „Zabicie procesu” to restart na nowych obiektach z dziennika otwartego z dysku oraz obraz katastrofy pliku dziennika, nie prawdziwy `kill -9` |
| K3g. Wejście przyjęte przez uzgadnianie (zgubione potwierdzenie, restart po wysłaniu, błąd zapisu po wysłaniu) przechodzi straże po wypełnieniu: wielkość i poślizg | pytest, to samo | pozycja o ryzyku większym niż limit zostaje zamknięta od razu | agent, offline | NOT PROVEN (BUG-E2E-1: test `xfail`; taka pozycja zostaje otwarta do 17:25, ze stopem u brokera; sekcja 7) |
| K3h. Po zgubionym potwierdzeniu i wybiciu stopu (pozycja `BLOCKED`) właściciel ma drogę wyjścia dostarczonymi narzędziami (`reset-position`) | pytest, to samo | `reset-position` kończy się kodem 0 | agent, offline | NOT PROVEN (BUG-E2E-2: test `xfail`; `reset-position` odmawia, dopóki zlecenie jest niewyjaśnione, a uzgadnianie czeka na właściciela; sekcja 7) |

### K4. Ćwiczenia kill switcha (spec. 17.4)

Co dowodzi: oba poziomy kill switcha włączają się z właściwych przyczyn, a ich zdjęcie wymaga `clear-kill`.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K4a. Pliki `data/STOP` (SOFT) i `data/HARD_STOP` (HARD) | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_stop_file_triggers_soft_and_hard_file_triggers_hard`, `test_hard_stop_file_alone_is_hard`, `test_clear_is_refused_while_a_stop_file_exists` w `tests/fault/test_kill_switch.py`) |
| K4b. Cisza notowań dłuższa niż 60 s | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_quote_silence_over_60s_in_session_soft_kills` w `test_watchdog.py`; `test_quote_silence_61s_triggers_soft_kill_and_blocks_entry` w `test_engine_day.py`) |
| K4c. Wygaśnięcie sesji | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_session_expiry_triggers_relogin_then_retry`, `test_session_expiry_mid_flatten_relogs_and_retries_the_close_on_the_same_tick` w `test_engine_day.py`; `test_401_triggers_one_serialized_relogin_then_retry` w `test_capitalcom_adapter.py`) |
| K4d. Rozbieżność z brokerem dłuższa niż 60 s | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_mismatch_older_than_60s_triggers_soft_kill` w `tests/fault/test_reconcile.py`) |
| K4e. Ćwiczenia na VPS: `STOP`, `HARD_STOP`, `docker kill`, awaria procesu, odcięcie sieci | L0.5 | alarmy i `status` jak w punktach 4.1 do 4.4 runbooka | właściciel | OWNER STEP — NOT YET DONE |

### K5. Raport dzienny (spec. 17.5)

Co dowodzi: raport podaje wynik brutto i netto, koszty, liczniki błędów wykonania i rozbieżności oraz status bramki L1.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K5a. Treść raportu i eksport CSV na syntetycznym dzienniku | pytest `tests/unit/test_report.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_report_prints_one_day_with_exact_lines`, `test_report_summary_totals_and_the_statement`, `test_csv_header_is_exact_and_one_row_per_trade_in_insertion_order` w `tests/unit/test_report.py`) |
| K5b. Raport z prawdziwego dziennika po sesji | L1.2 | blok „Dzień”, „Podsumowanie”, „Bramka L1”; kod wyjścia 0 | właściciel | OWNER STEP — NOT YET DONE |

### K6. Pamięć pod limitem cgroup (spec. 17.6)

Co dowodzi: silnik mieści się w 256 MB z zapasem, a reszta serwera pozostaje nietknięta. Wynik trzeba **opisać** w sekcji 8 tego dokumentu; jeśli nie mieści się w limicie z zapasem, jest to decyzja o hostingu (spec. pytanie 16).

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K6. Pomiar `docker stats --no-stream` i plików cgroup po 10 minutach oraz po pełnej sesji | L0.4 | `MEM USAGE` do 100 MiB po 10 minutach, szczyt do 170 MiB po pełnej sesji, `memory.swap.max` = `0`, `OOMKilled=false` | właściciel | OWNER STEP — NOT YET DONE |

### K7. Brak zleceń bez uzbrojenia (spec. 17.7)

Co dowodzi: tryb live wymaga jednocześnie `live_allowed = true`, pliku `data/ARM_LIVE`, powiązanego rachunku i zgodnego odcisku konfiguracji; bez nich silnik pracuje w `dry_run`.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K7a. Cztery warunki uzbrojenia, każdy osobno | pytest `tests/fault/test_modes.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_live_requires_arm_file_live_allowed_bound_account_and_matching_fingerprint`, `test_any_missing_arming_condition_means_dry_run_with_the_reason`, `test_truthy_values_that_are_not_true_never_arm`) |
| K7b. Zdjęcie uzbrojenia wyłącza wejścia działającego silnika, pozycja nadal jest spłaszczana | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_arm_file_removed_between_days_stops_entries_and_the_leftover_is_still_flattened`, `test_arm_file_removed_between_0900_and_0905_means_no_entry`, `test_live_allowed_flipped_to_false_stops_the_next_entry_but_the_open_position_is_flattened` w `test_modes.py`) |
| K7c. Kod nigdy nie tworzy `data/ARM_LIVE` ani `data/L1_PASSED` | pytest oraz skan strażnika (krok CI) | brak naruszeń | agent, offline | PROVEN OFFLINE (`test_engine_code_never_creates_arm_file` w `test_modes.py`; skan `scripts/hooks/guard_live.py --scan` w CI, opis w `CLAUDE.md`, sekcja „Strażnik live”) |
| K7d. Odcisk konfiguracji zgodny z zamrożonym, inaczej `dry_run` | pytest `tests/unit/test_prereg.py` i `test_modes.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_prereg_fingerprint_matching_the_frozen_file_arms` w `test_modes.py` oraz zestaw `tests/unit/test_prereg.py`) |
| K7e. Na VPS bez uzbrojenia silnik naprawdę pracuje w `dry_run` | L0.3 | `mode (files): dry_run (...)`, w logu `mode: dry_run, no orders will be sent` | właściciel | OWNER STEP — NOT YET DONE |

## 5. Bramki dowodowe (spec. sekcja 11)

Bramki nie mierzą przewagi, tylko jakość pomiaru i bezpieczeństwo. Wszystkie kroki poniżej to **kroki właściciela na VPS** (agent przygotował polecenia i sprawdził je z kodem). Kroki przed `smoke` wykonuj w `dry_run`. Nie zaczynaj kolejnego kroku, gdy poprzedni dał wynik inny niż oczekiwany: zatrzymaj się i zapytaj Claude.

### Bramka L0 (przed pierwszym zleceniem live)

**Kolejność wykonania** (numery L0.n to numery pozycji, nie kolejność): L0.1, L0.2, `bind-account` z L0.8, **L0.7 (zamrożenie przed pierwszą sesją `dry_run`)**, L0.3 razem z L0.4, L0.6, L0.5, `smoke` z L0.8, na końcu L0.9. Jest to kolejność punktów 1 do 6 runbooka.

#### L0.1 Testy w obrazie (runbook 1.7)

- Dowodzi: logika przechodzi w obrazie Docker, w systemie plików tylko do odczytu, bez sieci i bez klucza.
- Polecenie (po zbudowaniu obrazu testowego z punktu 1.7 runbooka):

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
```

- Oczekiwany wynik: ostatnia linia `... passed, ... skipped, ... deselected in ...`, bez `failed` i `error`. Kod 137 to brak pamięci: nie ponawiaj, zgłoś.
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE (patrz K1c: do czasu poprawki testów krok pokaże 4 `failed`).
- Wynik właściciela (data, wynik): ______________________

#### L0.2 Odczyty live tylko do odczytu (runbook 1.8)

- Dowodzi: klucz działa, reguły DE40, notowania, świece 5-minutowe i dzienne oraz historia są czytelne i zgodne z oczekiwaniami; żadnych zleceń.
- Polecenie (silnik nie może działać; to 8 logowań, uruchom raz):

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-live-readonly
```

- Oczekiwany wynik: `8 passed`, zero `SKIPPED` i `FAILED`. Przy `FAILED` nie ponawiaj pełnego zestawu: pojedyncze logowanie (runbook 1.8, polecenie z `-k test_accounts_listed`, wynik `1 passed`).
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data, wynik): ______________________
- Uwaga: zapis odpowiedzi do `tests/integration_live_readonly/fixtures/` i uzupełnienie statusów w `docs/ops/capitalcom-api-notes.md` (plan, zadanie 18, krok 2) **nie jest wykonane**: testy niczego nie zapisują, więc wymaga to osobnej pracy po odczytach (sekcja 7).

#### L0.3 Pełna sesja `dry_run` na notowaniach live (runbook 2.0 do 2.6)

- Dowodzi: silnik przez cały dzień (08:50 do 17:35 czasu Berlina) loguje się, uzgadnia stan, zbiera pierwszą świecę, podejmuje decyzję i nie wysyła zleceń, bez rozbieżności i błędów.
- Polecenia (po zamrożeniu pre-rejestracji, krok L0.7, i po bramce zgody nr 2 z punktu 2.1 runbooka): start `docker compose -f docker/compose.yml up -d gw-stock-engine`, a po 17:35 kontrola dzienna z punktu 2.6:

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

```bash
docker logs --since 24h gw-stock-engine 2>&1 | grep -E "decision|DayReport|ERROR|WARNING"
```

- Oczekiwany wynik: `heartbeat` świeże, `kill switch: NONE`, `position DE40: FLAT`, `unresolved orders: 0`, linia `last decision <data>: ...` (sygnał albo `SKIP` z powodem), w logu brak `ERROR`, w `data/alerts.log` brak alarmów `ERROR`.
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data, wynik): ______________________

#### L0.4 Pomiar pamięci (runbook 1.10, 2.4, 2.6; spec. pytanie 9, kryterium K6)

- Dowodzi: silnik mieści się w limicie 256 MB z zapasem, kontener naprawdę nie używa swapu, a reszta serwera nie ucierpiała. Wynik wpisz w sekcji 8.
- Polecenia: skrypt próby izolacji z punktu 1.10 (`WYNIK: WSZYSTKO OK`), blok pomiaru z punktu 2.4 po 10 minutach pracy (zapisuje `~/gw-pomiar-po.txt`), a po pełnej sesji ponownie ten blok. Rdzeń pomiaru:

```bash
docker stats --no-stream gw-stock-engine
```

```bash
free -m
```

- Oczekiwany wynik (progi z punktu 2.4 runbooka): `MEM USAGE` do 100 MiB po 10 minutach (101 do 150 MiB żółte, powyżej czerwone); szczyt po pełnej sesji do 170 MiB; `available` serwera co najmniej 400 MB; `memory.swap.max` = `0`; `OOMKilled=false`, brak `oom_kill`, restarty 0; pozostałe kontenery bez zmian (do 10%).
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela: sekcja 8.

#### L0.5 Ćwiczenia awaryjne (runbook, punkt 4)

- Dowodzi: że mechanizmy bezpieczeństwa działają na prawdziwym kontenerze: `STOP`, `HARD_STOP`, zabicie kontenera, zabicie procesu w kontenerze (samoczynny restart), odcięcie sieci; kill switch zdejmuje wyłącznie `clear-kill`.
- Polecenia: punkty 4.1 do 4.4 runbooka (w tym `clear-kill --by`, wpisanie `CLEAR`); wszystkie w `dry_run`.
- Oczekiwany wynik: alarmy w `data/alerts.log` i linie `kill switch: SOFT (STOP_FILE, ...)`, `kill switch: HARD (HARD_STOP_FILE, ...)` w `status`; po `docker kill` kod `Exited (137)` i ręczny start; po zabiciu procesu w kontenerze `restarty` o jeden większe i `zdrowie=healthy`; po odcięciu sieci alarm `QUOTE_SILENCE` i `SOFT`; po każdym ćwiczeniu `kill switch: NONE` po `clear-kill`, `unresolved orders: 0`.
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data, wynik): ______________________

#### L0.6 Pierwsza świeca porównana z wykresem (runbook 3)

- Dowodzi: znacznik czasu świecy oznacza jej początek (stała `CANDLE_STAMP_IS_PERIOD_START`); bez tego strategia mogłaby handlować na świecy sprzed otwarcia.
- Polecenie (po decyzji dnia, najlepiej na dwóch różnych dniach):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

- Oczekiwany wynik: w linii `last decision` początek świecy `...T08:00:00+00:00` zimą (`...T07:00:00+00:00` latem), O/H/L/C zgodne z świecą 09:00 do 09:05 na wykresie 5-minutowym Capital.com (różnica do ok. 1 do 2 punktów jest normalna: silnik zapisuje ceny środkowe).
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data, wynik): ______________________

#### L0.7 Pre-rejestracja: zamrożenie i kontrola (runbook 2.0)

- Dowodzi: odciski konfiguracji strategii, ryzyka i kosztów (plus adapter i klasa instrumentu) są zamrożone **przed** pierwszą sesją `dry_run`; zmiana któregokolwiek bajtu to nowy wariant i silnik zostaje w `dry_run`.
- Polecenia:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --write
```

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --check
```

- Oczekiwany wynik: `--write` wypisuje `pre-registration written: ...` oraz blok do wklejenia (wklejasz go Claude, który wpisuje go do `docs/preregistration/v1.md` i zatwierdza dokument); `--check` kończy się linią `OK: the configuration matches the frozen pre-registration` i kodem wyjścia 0. Po każdej aktualizacji kodu `--check` powtarzasz.
- Kto: właściciel (zamrożenie i kontrola), Claude (wpis do dokumentu). Status: OWNER STEP — NOT YET DONE (logika narzędzia: PROVEN OFFLINE, zestaw `tests/unit/test_prereg.py`)
- Wynik właściciela (data, odcisk skrócony do 12 znaków): ______________________

#### L0.8 Powiązanie rachunku i próba `smoke` (runbook 1.11 i 5)

- Dowodzi: silnik handluje tylko na rachunku wskazanym przez właściciela; cała ścieżka zleceń (otwarcie ze stopem, widoczność pozycji i stopu, zamknięcie, księgowanie, **drugie zamknięcie odrzucone jako „nie znaleziono”**, ostatnie świece, opóźnienie historii) działa na prawdziwym brokerze przy wielkości 0,001.
- Polecenia: najpierw `bind-account`:

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock bind-account
```

  potem, po zatrzymaniu silnika, **tymczasowym** uzbrojeniu przez właściciela (punkt 5 runbooka: `live_allowed = true` i plik `data/ARM_LIVE`) oraz kontroli `status` (`mode (files): live (all arming conditions met)`):

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock smoke --hold 90
```

- Oczekiwany wynik: `bind-account` kończy się linią `Bound account ... written to ... (mode 600).`; `smoke` wypisuje `PASS [1]` do `PASS [7]` (krok [6] to drugie zamknięcie, musi być `PASS`), linie `INFO [7]` ze świecami i `INFO [8]` z opóźnieniem historii, a na końcu `PASS smoke: 7/7 checks passed` i kod wyjścia 0. W aplikacji Capital.com w czasie `--hold` widać pozycję DE40 0,001 **ze stopem**; po próbie w historii jest dokładnie jedna transakcja i żadnej otwartej pozycji. Po próbie: zdjęcie `data/ARM_LIVE` i `live_allowed = false`.
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE (logika `bind-account` i `smoke` na atrapie: PROVEN OFFLINE, `tests/fault/test_modes.py`, np. `test_smoke_opens_minimum_size_with_stop_and_closes_immediately`, `test_smoke_second_close_that_is_not_position_not_found_is_a_loud_fail`)
- Wynik właściciela (data, wynik, czy stop widoczny w aplikacji): ______________________

#### L0.9 Uzbrojenie przez właściciela (runbook 6)

- Dowodzi: uzbrojenie jest świadomą decyzją właściciela. **Agent i kod nigdy nie tworzą `data/ARM_LIVE` ani nie ustawiają `live_allowed = true`.**
- Warunek: wszystkie kroki L0.1 do L0.8 mają wynik zgodny z oczekiwanym, a lista kontrolna z punktu 10 runbooka jest w całości zaznaczona.
- Polecenia: punkt 6 runbooka; sprawdzenie:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

- Oczekiwany wynik: `mode (files): live (all arming conditions met)`, a w logu po starcie `mode: live (all four arming conditions met; ...)`.
- Kto: wyłącznie właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data i godzina uzbrojenia): ______________________

### Bramka L1 (po 5 sesjach na wielkości minimalnej)

Co dowodzi: pomiar kosztów i zachowania brokera przy minimalnym ryzyku jest kompletny i bezpieczny; dopiero potem wielkość może być liczona regułami strategii. Logika bramki w raporcie: PROVEN OFFLINE (`test_report_l1_block_not_yet_with_four_sessions`, `test_report_l1_block_passes_with_five_clean_sessions_and_prints_the_instruction`, `test_report_l1_each_other_condition_blocks_the_gate` w `tests/unit/test_report.py`; `test_gate_l1_needs_five_sessions_three_clean_conditions_and_cost_samples`). Skutek pliku `data/L1_PASSED` dla wielkości: PROVEN OFFLINE (`test_first_five_sessions_use_minimum_size_then_rule_size` w `tests/fault/test_engine_day.py`).

#### L1.1 Pięć sesji live na wielkości 0,001

- Polecenia: każdy dzień sesji, po 17:35, kontrola dzienna z punktu 2.6 runbooka i raport (L1.2).
- Oczekiwany wynik: 5 sesji z jedną transakcją wejścia każda (próby `smoke` nie liczą się jako sesje); `status`: `live sessions (trades without smoke): 5; L1_PASSED: no`.
- Kto: właściciel, przez co najmniej 5 dni sesji (czas, nie da się skrócić). Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (daty sesji): ______________________

#### L1.2 Raport po każdej sesji i na koniec piątej

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock.report
```

- Oczekiwany wynik: blok „Dzień” z transakcją, blok „Podsumowanie” i blok „Bramka L1 (spec §11)”. Czytaj go tak:
  - `NOT YET` z powodem to „jeszcze nie”, nie błąd; powód `brak pomiaru kosztów: użytecznych próbek N z 5` także znaczy **jeszcze nie** (za mało próbek kosztu), a nie „koszty są dobre”.
  - Werdykt kosztów to **niekorzystny poślizg wejścia** względem modelu (najwyżej 2×, czyli średnio do 1,0 pkt przy modelu 0,5 pkt). Spread w chwili decyzji jest podany informacyjnie. **Koszty wyjść stopem są niezmierzone** (raport podaje tylko liczbę takich wyjść); właściciel ogląda je ręcznie w eksporcie CSV (kolumny `exit_reason`, `exit_fill`) i w aplikacji.
  - `PASS` oznacza wyłącznie, że liczby z dziennika mieszczą się w progach. Raport nie ocenia, czy rozbieżności zostały wyjaśnione: to ocena właściciela po przeglądzie (runbook, punkt 7).
- Eksport księgi do przeglądu (plik pojawia się jako `data/ledger.csv`):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock.report --csv /data/ledger.csv
```

- Kto: właściciel. Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data, status bramki, uwagi): ______________________

#### L1.3 Przegląd i decyzja o `data/L1_PASSED`

- Przegląd (runbook, punkt 7): zero niewyjaśnionych rozbieżności pozycja/broker starszych niż 60 s; zero nieobsłużonych odrzuceń; rozbieżność kosztów nie większa niż 2× model (inaczej przegląd z Claude); transakcje w raporcie zgadzają się z historią w aplikacji Capital.com; pamięć w zielonych progach.
- Polecenie, **tylko właściciel, tylko po przeglądzie, który wypadł dobrze, i po 5 sesjach**:

```bash
touch data/L1_PASSED
```

- Oczekiwany wynik: `status` pokazuje `L1_PASSED: yes`; od 6. sesji wielkość liczą reguły strategii (w granicach limitów ryzyka). Bez pliku wielkość pozostaje 0,001. Wycofanie: `rm -f data/L1_PASSED`.
- Kto: wyłącznie właściciel (agent i kod nigdy). Status: OWNER STEP — NOT YET DONE
- Wynik właściciela (data utworzenia pliku albo decyzja „jeszcze nie”): ______________________

### Bramka L2 (zmiana kapitału lub zakresu)

- Warunek: co najmniej 1000 transakcji i jednostronna dolna granica 95% (klastrowana po dniach) średniej netto większa od zera.
- Przy jednej transakcji dziennie to **ok. 4 lata**. Przy przewadze bliskiej zera próba rzędu tysięcy transakcji jest nieosiągalna w rozsądnym czasie, więc **v1 nie planuje skalowania kapitału**. Bramka istnieje, żeby nikt nie skalował kapitału bez dowodu.
- Status: NOT PROVEN (warunek niespełniony i niezaplanowany w v1; dziś transakcji jest zero)

## 6. Zestawy awaryjne i scenariusze e2e a kryteria

### Zestawy w HEAD (pytest, atrapy, bez sieci)

| Zestaw | Funkcji testowych | Kryteria |
|---|---|---|
| `tests/fault/test_order_manager.py` | 67 | K1 (niezmienniki 1, 2), K3 |
| `tests/fault/test_execution.py` | 88 | K1 (niezmienniki 3, 5), K3, K4 |
| `tests/fault/test_reconcile.py` | 62 | K1 (niezmiennik 2), K3, K4 (rozbieżność 60 s) |
| `tests/fault/test_kill_switch.py` | 61 | K4 (pliki `STOP`, `HARD_STOP`, zdjęcie) |
| `tests/fault/test_watchdog.py` | 34 | K4 (cisza notowań, logowania, blokada instancji, tętno) |
| `tests/fault/test_engine_day.py` | 48 | K2, K3, K4 (wygaśnięcie sesji) |
| `tests/fault/test_modes.py` | 79 | K2 (`dry_run`), K7, `bind-account`, `smoke`, `clear-kill`, `reset-position` |
| `tests/fault/test_lost_trade_regression.py` | 6 | K3 (zgubiona transakcja jest księgowana albo głośno zablokowana) |
| `tests/fault/test_trade_lifecycle_e2e.py` | 26 | K1, K2 (scenariusz h), K3, K4 (limity strat, twardy kill) |
| `tests/unit/test_capitalcom_adapter.py`, `test_capitalcom_trading.py`, `test_fake_broker.py` | 100, 137, 62 | K1 (kontrakt adaptera), K4 (sesja), niezmiennik 6 |
| `tests/unit/test_risk.py`, `test_journal.py`, `test_costs.py`, `test_strategy.py` | 82, 61, 48, 70 | K1 (niezmienniki 3, 4), K5 (koszty) |
| `tests/unit/test_report.py`, `test_prereg.py`, `test_compose.py` | 100, 53, 45 | K5, K7 (odcisk), spójność runbooka z kodem i Compose |
| `tests/integration_live_readonly/` | 8 | K1h (tylko VPS, nie biegną w CI) |

### Scenariusze e2e cyklu życia transakcji (`tests/fault/test_trade_lifecycle_e2e.py`, `e2e_support.py`)

Pliki są w HEAD od commitu `ca79717` (26 funkcji testowych, w uruchomieniu 57 przypadków przeszło i 5 `xfail`). Prawdziwe są dziennik SQLite na dysku testu, menedżer zleceń, uzgadnianie, wykonawca, bramka ryzyka, kill switch i model kosztów, a w scenariuszu h cały `Engine`; atrapą jest tylko broker (`FakeBroker` ze wstrzykiwaniem awarii) i zegar. Każdy restart zamyka dziennik, otwiera go z dysku, buduje nowe obiekty i sprawdza słabymi referencjami, że nic ze starej instancji nie przeżyło (kryterium B); broker przeżywa restarty (kryterium C); księgowanie jest „wszystko albo nic” (kryterium A). To nadal atrapa brokera i symulowany restart, nie prawdziwy `kill -9` ani prawdziwy Capital.com.

| Scenariusz | Temat | Kryteria | Status |
|---|---|---|---|
| B, C | restart zamyka dziennik, przebudowuje każdy obiekt i zostawia brokera | K1, K3 | PROVEN OFFLINE (`test_restart_closes_the_journal_rebuilds_every_object_and_keeps_the_broker`) |
| a | zgubione potwierdzenie i wybicie stopu przed pierwszą kontrolą: `BLOCKED`, kill switch miękki, zdejmuje tylko właściciel; awaria w każdym punkcie rozstrzygania | K3, K4 | PROVEN OFFLINE (`test_a_lost_ack_and_stop_out_before_the_first_check_blocks_soft_kills_and_only_the_owner_clears`, `test_a_crash_at_every_point_of_the_unknown_resolution_still_ends_blocked_and_soft_killed`) |
| b | wybicie stopu u brokera księgowane dokładnie raz; limity strat na dokładnych granicach; awaria między zaksięgowaniem a eskalacją kill switcha | K3, K4 | PROVEN OFFLINE (`test_b_stop_out_at_the_broker_is_booked_exactly_once`, `test_b_loss_limit_kills_fire_at_the_exact_boundaries_after_the_reconciler_books`, `test_b_crash_between_booking_and_kill_escalation_keeps_entries_blocked_and_the_startup_check_reapplies`) |
| c | timeout zamknięcia: lista pozycji zamiast ponownej wysyłki, opóźnione zamknięcie księgowane raz | K3 | PROVEN OFFLINE (`test_c_flatten_close_timeout_lists_positions_never_resends_and_the_lagging_closure_is_booked_once`, `test_c_close_timeout_not_applied_keeps_exiting_and_the_retry_closes_only_after_the_list_shows_it`) |
| d | twardy kill przy otwartej pozycji: awaryjne spłaszczenie i księgowanie przez restarty; wyjścia nigdy nie blokowane przez bramkę | K3, K4, niezmiennik 5 | PROVEN OFFLINE (`test_d_hard_kill_mid_position_emergency_flattens_and_books_across_restarts`, `test_d_emergency_flatten_with_every_journal_write_failing_still_closes_and_is_booked_after_restart`, `test_d_exits_are_never_blocked_by_the_gate_even_with_every_entry_check_failing`) |
| e | błąd zapisu dziennika w każdym punkcie wejścia: nigdy wysyłka „na ślepo” | K1 (niezmiennik 1), K3 | PROVEN OFFLINE (`test_e_a_journal_write_failure_at_each_write_point_of_enter_never_sends_blind`, `test_e_a_crash_between_the_entry_context_and_the_intent_row_sends_nothing_and_blocks_the_retry`) |
| f | zapis transakcji wszystko albo nic; obraz pliku dziennika zrobiony między dwoma instrukcjami | K3 | PROVEN OFFLINE (`test_f_record_trade_is_all_or_nothing_when_a_statement_fails_and_a_retry_books_both_once`, `test_f_a_kill_9_image_taken_between_the_trade_insert_and_the_pnl_update_holds_neither`) |
| g | to, co broker zrobił, gdy silnika nie było, jest wykrywane po restarcie | K3 | PROVEN OFFLINE (`test_g_what_the_broker_did_while_the_engine_was_down_is_discovered_after_the_restart`, `test_g_a_fill_accepted_while_down_without_a_broker_stop_is_adopted_and_protected_with_the_entry_stop`) |
| h | pełny dzień `Engine` z restartem między wejściem a wyjściem: jedna transakcja, spójna księga; awaria między zaksięgowaniem a eskalacją; obraz po `kill -9` w trakcie księgowania | K2, K3 | PROVEN OFFLINE (`test_h_full_engine_day_with_a_restart_between_entry_and_exit_books_one_trade_and_a_consistent_ledger`, `test_engine_crash_between_booking_and_kill_escalation_reapplies_the_hard_kill_before_any_broker_call`, `test_engine_kill_9_inside_record_trade_restarts_from_the_crash_image_and_books_exactly_once`, `test_engine_journal_failure_between_the_trade_and_pnl_statements_books_once_on_the_next_pass`) |
| BUG-E2E-1 | wejście przyjęte przez uzgadnianie omija straże po wypełnieniu (wielkość, poślizg) | K3g | NOT PROVEN (`test_bug_e2e_1_*`: 4 przypadki `xfail`) |
| BUG-E2E-2 | zgubiona transakcja w stanie `BLOCKED` nie ma wyjścia przez `reset-position` | K3h | NOT PROVEN (`test_bug_e2e_2_the_owner_can_resolve_a_blocked_lost_trade_with_the_provided_tools`: `xfail`) |

## 7. Otwarte punkty w chwili spisywania

Te punkty nie zmieniają statusów w tabelach powyżej; są tu, żeby podpis właściciela był świadomy. Zamyka je runda poprawek (hardening) przed uzbrojeniem albo decyzja właściciela.

| Punkt | Wpływ | Co dalej |
|---|---|---|
| Zadanie `image` w CI czerwone: 4 testy z `tests/unit/test_prereg.py` czytają `docs/` i `.gitattributes`, których nie ma w obrazie testowym (powinny się pomijać jak testy z `test_compose.py`) | krok 1.7 runbooka na VPS pokaże 4 `failed`; kryterium K1c | poprawka testów, potem ponowny CI |
| **BUG-E2E-1** (e2e, `xfail`): wejście przyjęte przez uzgadnianie (zgubiona odpowiedź, restart po wysłaniu, błąd zapisu po wysłaniu) nigdy nie przechodzi straży po wypełnieniu: pozycja o ryzyku większym niż limit albo z poślizgiem ponad próg straży zostaje otwarta do spłaszczenia o 17:25 (stop u brokera jest) | rzadka ścieżka (awaria w chwili wejścia); przy wielkości 0,001 w pierwszych 5 sesjach skutek finansowy jest groszowy, ale zachowanie jest niezgodne ze specyfikacją (sekcja 5 pkt 7, sekcja 9) | poprawka kodu w rundzie hardening |
| **BUG-E2E-2** (e2e, `xfail`): po zgubionym potwierdzeniu i wybiciu stopu pozycja jest `BLOCKED`, uzgadnianie czeka na właściciela, a `reset-position` odmawia, dopóki zlecenie jest niewyjaśnione: właściciel nie ma drogi wyjścia dostarczonymi narzędziami | wejścia pozostają zablokowane miękkim kill switchem (po bezpiecznej stronie), ale wyjście ze stanu wymaga pomocy Claude (punkt 8.5 runbooka nie obiecuje tu rozwiązania) | poprawka kodu w rundzie hardening |
| Lista poprawek z przeglądu zadania 14: przypadki brzegowe `reset-position` (okno historii, brak czasu otwarcia), `smoke --hold` (drugi sygnał, odczyt dziennika po wstrzymaniu), 403 bez kodu błędu liczone jako odrzucenie danych logowania | rzadkie ścieżki operatorskie punktów 5 i 8.5 runbooka; nie dotyczą zwykłego dnia | runda hardening |
| Lista poprawek z przeglądu zadania 12: szczegóły awaryjnego spłaszczenia (drugie zamknięcie bez odczytu listy, kolejność zapisu intencji wyjścia, treść jednego alarmu), limit czasu ochrony stopu 2 s może przy wolnym łączu włączyć twardy kill niepotrzebnie (błąd po bezpiecznej stronie) | rzadkie ścieżki awaryjne | runda hardening |
| Alarm `LOGIN_LOCKOUT` w kodzie wspomina plik `data/LOGIN_RESET` zdejmujący blokadę logowań; runbook świadomie go nie opisuje (omija zasadę jednej próby po problemie z kluczem) | właściciel może zobaczyć w alarmie plik, którego nie ma tworzyć (runbook, punkt 9) | decyzja, czy zostawić w kodzie |
| Specyfikacja (wiersz 57 i pytanie 13) nadal mówi o pomiarze „poślizgu stopów”; v1 mierzy poślizg wejścia i spread, a koszty wyjść STOP są niezmierzone | opis w specyfikacji jest szerszy niż pomiar | poprawić tekst specyfikacji |
| Brak narzędzia zapisującego odczyty live do `tests/integration_live_readonly/fixtures/` (plan, zadanie 18, krok 2) | fixtures nie powstaną z samego uruchomienia L0.2 | osobna praca po odczytach |
| Komunikat raportu przy `PASS` (`Aby dopuścić wielkość według reguł utwórz plik data/L1_PASSED ...`) nie wspomina o przeglądzie właściciela | możliwe wrażenie, że `PASS` to zgoda; runbook (punkt 7) wyjaśnia, że nie jest | opcjonalnie doprecyzować komunikat w kodzie |

## 8. Pomiar pamięci (wypełnia właściciel po L0.4)

Wynik jest warunkiem kryterium K6. Gdy któryś pomiar jest w kolumnie czerwonej progu z punktu 2.4 runbooka, kryterium nie jest spełnione i potrzebna jest decyzja o hostingu (spec. pytanie 16).

| Pomiar | Próg zielony | Wynik | Data |
|---|---|---|---|
| `MEM USAGE` silnika po 10 minutach | do 100 MiB | | |
| Szczyt po pełnej sesji (`memory.peak` albo najwyższy odczyt) | do 170 MiB | | |
| `available` serwera po starcie silnika | co najmniej 400 MB | | |
| `memory.swap.max` / `memory.swap.current` | `0` / `0` | | |
| `OOMKilled`, `oom_kill`, restarty | `false`, `0`, `0` | | |
| Pozostałe kontenery względem pomiaru „przed” | zmiana do 10% | | |
| Wersja cgroup (`cgroup2fs` czy `tmpfs`) | `cgroup2fs` | | |

## 9. Podpis właściciela

Podpisując, potwierdzasz, że przeczytałeś zastrzeżenia z sekcji 1 i że wiesz, co jest udowodnione, a co nie. Podpis nie uzbraja silnika: uzbrajasz go osobno, punktem 6 runbooka.

- [ ] Rozumiem, że rentowność nie jest wykazana, a wynik pojedynczej sesji na wielkości minimalnej to grosze i szum.
- [ ] Rozumiem, że testy offline dowodzą logiki na atrapach, a gotowość techniczną potwierdzają dopiero odczyty live, `dry_run` i `smoke` na VPS.
- [ ] Wszystkie kroki bramki L0 (L0.1 do L0.8) mają w sekcji 5 wpisaną datę i wynik zgodny z oczekiwanym.
- [ ] Pomiar pamięci (sekcja 8) jest wpisany i mieści się w zielonych progach.
- [ ] Lista kontrolna z punktu 10 runbooka jest w całości zaznaczona.
- [ ] Przeczytałem otwarte punkty z sekcji 7 i świadomie zdecydowałem, jak je traktuję.
- [ ] Wiem, że `data/ARM_LIVE` i `live_allowed = true` ustawiam wyłącznie ja, i wiem, jak rozbroić silnik (hamulce 1 do 3 i punkt 6 runbooka).
- [ ] Wiem, że `data/L1_PASSED` tworzę wyłącznie ja, dopiero po 5 sesjach i po przeglądzie; status `PASS` w raporcie nie jest zgodą.
- [ ] Wiem, że bramka L2 nie jest planowana w v1 i że nie skaluję kapitału bez dowodu.

Imię i nazwisko: ______________________________

Data: ____-__-__

Podpis: ______________________________
