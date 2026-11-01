# Odbiór v1: kryteria akceptacji i bramki L0, L1, L2

Dokument odbioru silnika `gw-stock-engine` v1 (zadanie 18 planu). Kod: gałąź `feat/engine-v2`; stan dowodów offline spisano dla kodu z commitu `d8be683` (2026-11-01, po rundzie hardeningu; wcześniejsza wersja tego dokumentu opisywała `ca79717`, a od `075ae3d` zmieniły się tylko komentarze). Część „offline” wykonał agent w sesji deweloperskiej (bez VPS, bez klucza, bez sieci do brokera). Resztę wykonuje **właściciel na VPS** według `docs/ops/runbook.md`. Polecenia poniżej są powtórzone dokładnie tak, jak w runbooku (katalog `/home/claude/gw-stock-engine`, użytkownik `claude`); gdyby kiedykolwiek różniły się od runbooka, obowiązuje runbook, a różnicę trzeba zgłosić.

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
7. **Hardening zamknął znane defekty kodu, ale nie dowodzi niczego o prawdziwym brokerze.** BUG-E2E-1 i BUG-E2E-2 są naprawione i udowodnione na atrapie. Zachowanie Capital.com (kształt potwierdzeń i historii, opóźnienie wpisu zamknięcia, tryb konta, stop widoczny w aplikacji) rozstrzyga dopiero próba `smoke`. Decyzje, zaakceptowane luki i lista kroków właściciela: `docs/superpowers/decisions-log.md`.

## 2. Znaczenie kolumny „Status”

Każdy wiersz ma dokładnie jeden z trzech statusów:

- `PROVEN OFFLINE (test / zestaw)`: udowodnione testem uruchomionym bez VPS, klucza i brokera (w sesji deweloperskiej albo w CI). W nawiasie nazwa testu, zestawu albo uruchomienia. Dotyczy wyłącznie tego, co ten test sprawdza, na atrapach.
- `OWNER STEP — NOT YET DONE`: wymaga VPS, żywego brokera, klucza, czasu albo decyzji właściciela, i nie zostało jeszcze wykonane. Po wykonaniu właściciel wpisuje datę i wynik w kolumnie „Wynik właściciela” (sekcje 4 i 5).
- `NOT PROVEN`: nie jest udowodnione ani offline, ani żadnym krokiem właściciela opisanym dziś w runbooku. Wymaga pracy (kod, test albo decyzja) albo jest znanym otwartym punktem. W komórce jest powód.

## 3. Co faktycznie uruchomiono offline

| Uruchomienie | Wynik |
|---|---|
| Pełny zestaw: `python -m pytest -m "not integration_live_readonly" -W error` z korzenia czystej kopii kodu z commitu `d8be683` (Python 3.12) | `3713 passed, 8 deselected, 6 xfailed` w ok. 93 s. 8 odznaczonych to odczyty live (`integration_live_readonly`, tylko VPS). 6 `xfailed` to jawnie oznaczone luki strażnika live w `tests/unit/test_guard_live.py` (`xfail(strict=True)`, sekcja 7); defekty BUG-E2E-1 i BUG-E2E-2 nie mają już `xfail` |
| CI GitHub Actions: run `41279529142` na `505a243` i run `41283304148` na `075ae3d` (gałąź `feat/engine-v2`) | w obu **wszystkie trzy zadania zielone**: `tests (Python 3.12)`, `tests (Python 3.13)` (na `075ae3d`: `3713 passed, 8 deselected, 6 xfailed`) oraz `image` (budowa celu `test`, testy w obrazie bez sieci i z systemem plików tylko do odczytu, budowa celu `runtime` i import silnika bez sieci; na `075ae3d`: `2998 passed, 721 skipped, 8 deselected`). Wcześniejszy run `41270735303` na `ca79717` miał `image` CZERWONE (cztery testy z `tests/unit/test_prereg.py` czytały `docs/` i `.gitattributes`, których nie ma w obrazie testowym); poprawka to znacznik `GW_TEST_IMAGE=1` w obrazie testowym. CI nie sprawdza limitów Compose (pamięć, `cap_drop`, `read_only`, healthcheck) ani pomiaru cgroup: to kroki właściciela (K6, L0.4, L0.5) |
| Polecenia z runbooka sprawdzone z kodem | Dla `ca79717`: `python -m gwstock --help` i `--help` każdego polecenia, `python -m gwstock.prereg --help`, `python -m gwstock.report --help`, `docker compose -f docker/compose.yml config` (usługi `gw-stock-engine`, `gw-stock-test`, `gw-stock-live-readonly`; wartości limitów z punktu 1.6 runbooka zgodne); `prereg --print`, `--write`, `--check`, `--write --force`, `report --day`, `--all`, `--csv` uruchomione na czystej kopii z pustym katalogiem danych. Od tego czasu doszło `resolve-lost-trade --by` (sprawdzone `--help`; cytaty komunikatów w runbooku pilnuje `tests/unit/test_compose.py`). Zachowanie w kontenerze Docker (np. przekazanie słowa `FORCE` ze standardowego wejścia do `compose run -T`), cgroup v2 i SELinux nie były sprawdzone poza CI |
| Pomiar pamięci pod limitem cgroup | **Nie wykonano.** W sesji deweloperskiej jest klient Dockera bez demona, a pomiar ma sens tylko na jądrze VPS |

## 4. Kryteria akceptacji v1 (spec. sekcja 17)

Polecenia oznaczone `L0.n` i `L1.n` są w sekcji 5 (wraz z oczekiwanym wynikiem i miejscem na wynik właściciela).

### K1. Testy przechodzą (spec. 17.1)

Co dowodzi: logika silnika, kontrakt adaptera i niezmienniki działają na atrapach; testy są powtarzalne lokalnie, w CI (Python 3.12 i 3.13) i w obrazie Docker.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K1a. Pełny zestaw lokalnie | `python -m pytest -m "not integration_live_readonly"` | ostatnia linia `... passed ...` bez `failed` i `error` | agent, offline | PROVEN OFFLINE (pełny zestaw, czysta kopia `d8be683`, Python 3.12: `3713 passed, 8 deselected, 6 xfailed`) |
| K1b. CI, Python 3.12 i 3.13 | zakładka Actions na GitHubie, zadania `tests (Python 3.12)` i `tests (Python 3.13)` | oba zielone | agent / GitHub | PROVEN OFFLINE (CI run `41279529142` na `505a243` i run `41283304148` na `075ae3d`: oba zadania zielone) |
| K1c. Testy w obrazie, w CI | zadanie `image` w CI (cel `test`, bez sieci) | zielone | agent / GitHub | PROVEN OFFLINE (CI run `41279529142` na `505a243`: `image` zielone, a potem run `41283304148` na `075ae3d`: `2998 passed, 721 skipped, 8 deselected`; budowa celów `test` i `runtime` na prawdziwym demonie Dockera GitHub Actions). Dotyczy wyłącznie budowy i testów logiki w obrazie: limity Compose, cgroup i healthcheck to OWNER STEP (K6, K3c, K4e) |
| K1d. Testy w obrazie, na VPS | L0.1 | brak `failed` i `error` | właściciel | OWNER STEP — NOT YET DONE (zielone `image` w CI zmniejsza ryzyko, ale nie zastępuje uruchomienia na VPS) |
| K1e. Każdy niezmiennik (1) do (6) ma co najmniej jeden test | pytest, jak K1a | (1) `test_intent_row_exists_before_broker_call`; (2) `test_timeout_then_position_found_is_adopted_not_duplicated`, `test_flatten_timeout_reconciles_instead_of_resending`; (3) `test_leverage_at_the_limit_passes_and_a_tick_over_blocks`; (4) `test_daily_loss_survives_reopen_and_is_keyed_by_exit_day`; (5) `test_exits_are_never_blocked_even_when_kill_is_hard`, `test_an_active_kill_never_blocks_flatten`; (6) `test_concurrent_logins_share_one_post_session` | agent, offline | PROVEN OFFLINE (`tests/fault/test_order_manager.py`, `test_execution.py`, `test_reconcile.py`; `tests/unit/test_risk.py`, `test_journal.py`, `test_capitalcom_adapter.py`) |
| K1f. Niezmienniki sprawdzane asercjami w każdym teście awarii przez wspólne helpery (brzmienie spec. 17.1 po zmianie z hardeningu) | – | – | – | NOT PROVEN (wspólne helpery są, m.in. `assert_books_balance` w testach E2E, ale w HEAD nie ma generycznego haczyka, który sprawdzałby wszystkie sześć niezmienników po KAŻDYM teście awarii; pokrycie wynika z przeglądu testów, nie z mechanizmu. Specyfikację poprawiono tak, żeby tego nie obiecywała, decyzja P1 w `docs/superpowers/decisions-log.md`) |
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
| K3f. Scenariusze e2e cyklu życia: restart z dysku w każdym ważnym punkcie (zgubione potwierdzenie i stop, jedno zaksięgowanie, timeout zamknięcia, twardy kill przy pozycji, awaria zapisu dziennika, atomowość księgowania, zmiany u brokera przy wyłączonym silniku, pełny dzień z restartem) | pytest `tests/fault/test_trade_lifecycle_e2e.py` | przechodzi (63 przypadki, żadnego `xfail`) | agent, offline | PROVEN OFFLINE (`tests/fault/test_trade_lifecycle_e2e.py`, scenariusze a do h i kryteria A do C, nazwy w sekcji 6). „Zabicie procesu” to restart na nowych obiektach z dziennika otwartego z dysku oraz obraz katastrofy pliku dziennika, nie prawdziwy `kill -9` |
| K3g. Wejście przyjęte przez uzgadnianie (zgubione potwierdzenie, restart po wysłaniu, błąd zapisu po wysłaniu) przechodzi straże po wypełnieniu: wielkość i poślizg (BUG-E2E-1) | pytest `test_execution.py`, `test_engine_day.py`, `test_trade_lifecycle_e2e.py` | pozycja o ryzyku większym niż limit albo z poślizgiem ponad próg straży zostaje zamknięta od razu, a straż nie ginie po restarcie | agent, offline | PROVEN OFFLINE (naprawa `6275009`, poprawki `90a6d15`, `524650f`, `af37edc`, `1bc3ce1`; `test_guard_adopted_fill_closes_an_oversize_fill_adopted_by_reconciliation`, `test_guard_adopted_fill_closes_a_slipped_fill_using_the_decision_quote_of_the_entry_context` i `test_guard_adopted_fill_is_idempotent` w `test_execution.py`; `test_reconcile_once_attaches_the_stop_before_the_fill_guard_closes_an_adopted_oversize_fill` w `test_engine_day.py`; `test_bug_e2e_1_an_adopted_oversize_fill_is_closed_by_the_risk_guard` (3 ścieżki), `test_bug_e2e_1_engine_restart_mid_entry_still_closes_a_slipped_fill` i `test_crash_after_adoption_before_the_fill_guard_still_closes_after_the_restart` w `test_trade_lifecycle_e2e.py`). Na atrapie: zachowanie prawdziwego brokera przy zgubionym potwierdzeniu nie było sprawdzane |
| K3h. Po zgubionym potwierdzeniu i wybiciu stopu (pozycja `BLOCKED`) właściciel ma drogę wyjścia dostarczonym narzędziem `resolve-lost-trade`, a strata trafia do liczników (BUG-E2E-2) | pytest `test_modes.py`, `test_trade_lifecycle_e2e.py` | polecenie kończy się kodem 0, transakcja jest zaksięgowana raz, zlecenie `CONFIRMED`, pozycja `FLAT`, kill switch nietknięty | agent, offline | PROVEN OFFLINE (naprawa `58708be`, `8a048ed`, `3c7e710`, runbook `5f288cd`; `test_bug_e2e_2_the_owner_books_a_blocked_lost_trade_with_resolve_lost_trade` w `test_trade_lifecycle_e2e.py`; `test_resolve_lost_trade_books_the_worst_case_loss_when_the_broker_knows_no_entry`, `test_resolve_lost_trade_books_exactly_from_the_confirm_when_the_reference_is_known`, `test_resolve_lost_trade_never_books_a_profit_from_an_estimate`, `test_resolve_lost_trade_refuses_and_changes_nothing`, `test_resolve_lost_trade_rerun_after_a_crash_after_booking_finishes_without_booking_twice` i `test_resolve_lost_trade_applies_the_loss_limit_of_the_closure_day` w `test_modes.py`). Na atrapie: czy prawdziwy `GET /confirms` zna wejście po zamknięciu pozycji (C2b, C28) i jak wygląda historia po stopie (C7), rozstrzyga dopiero `smoke`; bez danych wejścia narzędzie księguje szacunek najgorszej straty (znacznik `ESTIMATED`) |
| K3i. Odrzucenie wejścia w potwierdzeniu nie gubi transakcji: `REJECTED` dopiero po czytelnej liście i historii bez naszego deala | pytest `test_order_manager.py`, `test_lost_trade_regression.py` | pozycja na liście → `BLOCKED`, zamknięcie w historii → `UNKNOWN`/`BLOCKED`, odczyt nieudany → `UNKNOWN`, nigdy `REJECTED`/`FLAT` z niezaksięgowaną stratą | agent, offline | PROVEN OFFLINE (`002141d`, `9b43d6d`, `4abc51d`; `test_a_rejected_confirm_with_our_position_listed_blocks_instead_of_rejecting`, `test_a_rejected_confirm_with_an_empty_list_reads_the_history_before_rejecting`, `test_rejected_on_confirm_during_resolution_checks_the_list_and_history_before_not_placed` w `test_order_manager.py`; `test_deferred_rejection_and_stop_out_before_the_list_read_is_blocked_not_rejected` w `test_lost_trade_regression.py`). Zaakceptowane ryzyko resztkowe: lista opóźniona względem wypełnienia i stop w ciągu następnych 10 s (decyzja O10) |
| K3j. Awaryjne spłaszczenie nie wysyła drugiego zamknięcia bez odczytu listy, także gdy zapis `EXITING` zawiódł albo zamknięcie wysłano w poprzednim takcie | pytest `test_execution.py`, `test_engine_day.py` | jedno `close_position` do czasu, aż lista pokaże deal | agent, offline | PROVEN OFFLINE (`ce3a93d`, `e97d763`; `test_emergency_flatten_never_resends_a_close_without_a_list_read_when_the_exiting_write_failed`, `test_a_close_sent_on_an_earlier_tick_is_never_resent_blind`, `test_emergency_flatten_closes_our_last_known_deal_when_the_journal_cannot_be_read` w `test_execution.py`; `test_downgrade_keeps_the_executor_memory_of_sent_closes` w `test_engine_day.py`). Że prawdziwy broker odrzuca drugie `DELETE` tego samego `dealId` jako czyste 404 bez skutku (C29), sprawdza `smoke` |
| K3k. Opóźniona historia po spłaszczeniu 17:25 daje specyficzny alarm `CLOSURE_NOT_IN_HISTORY`, a zamknięcie jest księgowane po „dogonieniu” historii | pytest `test_reconcile.py`, `test_engine_day.py` | alarm raz na incydent, kill switch SOFT do `clear-kill`, jedno zaksięgowanie | agent, offline | PROVEN OFFLINE (`47ee113`, `782a400`; `test_closure_not_in_history_after_an_engine_close_raises_a_specific_alert_once` w `test_reconcile.py`, `test_a_lagging_history_after_the_1725_close_reaches_the_owner_as_closure_not_in_history` w `test_engine_day.py`). Faktyczne opóźnienie wpisu w historii Capital.com jest niezmierzone (C7): OWNER STEP — NOT YET DONE (krok [8] próby `smoke`); od wyniku zależy, czy wystarczy opcja A (decyzja H1) |

### K4. Ćwiczenia kill switcha (spec. 17.4)

Co dowodzi: oba poziomy kill switcha włączają się z właściwych przyczyn, a ich zdjęcie wymaga `clear-kill`.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K4a. Pliki `data/STOP` (SOFT) i `data/HARD_STOP` (HARD) | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_stop_file_triggers_soft_and_hard_file_triggers_hard`, `test_hard_stop_file_alone_is_hard`, `test_clear_is_refused_while_a_stop_file_exists` w `tests/fault/test_kill_switch.py`) |
| K4b. Cisza notowań dłuższa niż 60 s | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_quote_silence_over_60s_in_session_soft_kills` w `test_watchdog.py`; `test_quote_silence_61s_triggers_soft_kill_and_blocks_entry` w `test_engine_day.py`) |
| K4c. Wygaśnięcie sesji | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_session_expiry_triggers_relogin_then_retry`, `test_session_expiry_mid_flatten_relogs_and_retries_the_close_on_the_same_tick` w `test_engine_day.py`; `test_401_triggers_one_serialized_relogin_then_retry` w `test_capitalcom_adapter.py`) |
| K4d. Rozbieżność z brokerem dłuższa niż 60 s | pytest | przechodzi | agent, offline | PROVEN OFFLINE (`test_mismatch_older_than_60s_triggers_soft_kill` w `tests/fault/test_reconcile.py`) |
| K4e. Ćwiczenia na VPS: `STOP`, `HARD_STOP`, `docker kill`, awaria procesu, odcięcie sieci | L0.5 | alarmy i `status` jak w punktach 4.1 do 4.4 runbooka | właściciel | OWNER STEP — NOT YET DONE |
| K4f. Kill switch po awarii dziennika: HARD w pamięci przed odczytem dziennika; nieczytelny zapisany stan to SOFT, a `clear` i `clear-kill` odmawiają, dopóki stanu nie da się odczytać (mógłby być HARD) | pytest `test_kill_switch.py`, `test_modes.py` | przechodzi | agent, offline | PROVEN OFFLINE (`cf91cf1`, `5427f80`, `07187d7`; `test_hard_is_in_memory_before_a_slow_read_of_an_unknown_stored_state`, `test_clear_refuses_while_the_stored_state_is_unreadable` w `test_kill_switch.py`; `test_clear_kill_shows_the_stored_state_when_the_first_read_failed_and_reports_the_merged_level`, `test_clear_kill_refuses_a_permanently_unreadable_kill_state_and_says_only_a_repair_helps` w `test_modes.py`). Znane ograniczenie: miękki kill switch niezapisany przy pełnym dysku ginie po restarcie (decyzja K6) |

### K5. Raport dzienny (spec. 17.5)

Co dowodzi: raport podaje wynik brutto i netto, koszty, liczniki błędów wykonania i rozbieżności oraz status bramki L1.

| Część | Polecenie | Oczekiwany wynik | Kto | Status |
|---|---|---|---|---|
| K5a. Treść raportu i eksport CSV na syntetycznym dzienniku | pytest `tests/unit/test_report.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_report_prints_one_day_with_exact_lines`, `test_report_summary_totals_and_the_statement`, `test_csv_header_is_exact_and_one_row_per_trade_in_insertion_order` w `tests/unit/test_report.py`) |
| K5c. Raport i `status` oznaczają transakcję zaksięgowaną z szacunku, a zdanie przy `PASS` mówi, że to informacja, nie zgoda | pytest `test_report.py`, `test_modes.py` | przechodzi | agent, offline | PROVEN OFFLINE (`test_report_warns_about_estimated_trades`, `test_l1_pass_line_says_it_is_information_not_consent` w `test_report.py`; `test_status_marks_a_trade_booked_from_an_estimate` w `test_modes.py`) |
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
- Kto: właściciel. Status: OWNER STEP — NOT YET DONE (zadanie `image` w CI jest zielone, K1c, ale to nie zastępuje uruchomienia na VPS).
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

- Oczekiwany wynik: `bind-account` kończy się linią `Bound account ... written to ... (mode 600).`; `smoke` wypisuje `PASS [1]` do `PASS [7]` (krok [6] to drugie zamknięcie, musi być `PASS`), linie `INFO [7]` ze świecami i `INFO [8]` z opóźnieniem historii, a na końcu `PASS smoke: 7/7 checks passed` i kod wyjścia 0. Czyste wejście NIE może kończyć się adopcją po `UNKNOWN`: linia `WARN [2]` znaczy, że potwierdzenie ma dodatkowy wpis `affectedDeals` albo inną wielkość niż pozycja (C15), i trzeba ją zgłosić, a nie pominąć. W aplikacji Capital.com w czasie `--hold` widać pozycję DE40 0,001 **ze stopem**, a depozyt zgodny z `margin_used = balance − available` (C17); po próbie w historii jest dokładnie jedna transakcja i żadnej otwartej pozycji. Po próbie: zdjęcie `data/ARM_LIVE` i `live_allowed = false`.
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
| `tests/fault/test_order_manager.py` | 75 | K1 (niezmienniki 1, 2), K3 |
| `tests/fault/test_execution.py` | 122 | K1 (niezmienniki 3, 5), K3, K4 |
| `tests/fault/test_reconcile.py` | 71 | K1 (niezmiennik 2), K3, K4 (rozbieżność 60 s) |
| `tests/fault/test_kill_switch.py` | 67 | K4 (pliki `STOP`, `HARD_STOP`, zdjęcie, stan nieczytelny) |
| `tests/fault/test_watchdog.py` | 35 | K4 (cisza notowań, logowania, blokada instancji, tętno) |
| `tests/fault/test_engine_day.py` | 54 | K2, K3, K4 (wygaśnięcie sesji) |
| `tests/fault/test_modes.py` | 107 | K2 (`dry_run`), K7, `bind-account`, `smoke`, `clear-kill`, `reset-position`, `resolve-lost-trade` |
| `tests/fault/test_lost_trade_regression.py` | 8 | K3 (zgubiona transakcja jest księgowana albo głośno zablokowana) |
| `tests/fault/test_trade_lifecycle_e2e.py` | 27 | K1, K2 (scenariusz h), K3, K4 (limity strat, twardy kill) |
| `tests/unit/test_capitalcom_adapter.py`, `test_capitalcom_trading.py`, `test_fake_broker.py` | 107, 140, 65 | K1 (kontrakt adaptera), K4 (sesja), niezmiennik 6 |
| `tests/unit/test_risk.py`, `test_journal.py`, `test_costs.py`, `test_strategy.py` | 82, 84, 52, 78 | K1 (niezmienniki 3, 4), K5 (koszty) |
| `tests/unit/test_report.py`, `test_prereg.py`, `test_compose.py` | 104, 53, 46 | K5, K7 (odcisk), spójność runbooka z kodem i Compose |
| `tests/integration_live_readonly/` | 8 | K1h (tylko VPS, nie biegną w CI) |

### Scenariusze e2e cyklu życia transakcji (`tests/fault/test_trade_lifecycle_e2e.py`, `e2e_support.py`)

Pliki są w HEAD od commitu `ca79717` (27 funkcji testowych, w uruchomieniu 63 przypadki, wszystkie przechodzą, żadnego `xfail`; dwa defekty znalezione przez te testy, BUG-E2E-1 i BUG-E2E-2, naprawiono w rundzie hardening, a ich testy zamieniono na zwykłe). Prawdziwe są dziennik SQLite na dysku testu, menedżer zleceń, uzgadnianie, wykonawca, bramka ryzyka, kill switch i model kosztów, a w scenariuszu h cały `Engine`; atrapą jest tylko broker (`FakeBroker` ze wstrzykiwaniem awarii) i zegar. Każdy restart zamyka dziennik, otwiera go z dysku, buduje nowe obiekty i sprawdza słabymi referencjami, że nic ze starej instancji nie przeżyło (kryterium B); broker przeżywa restarty (kryterium C); księgowanie jest „wszystko albo nic” (kryterium A). To nadal atrapa brokera i symulowany restart, nie prawdziwy `kill -9` ani prawdziwy Capital.com.

| Scenariusz | Temat | Kryteria | Status |
|---|---|---|---|
| B, C | restart zamyka dziennik, przebudowuje każdy obiekt i zostawia brokera | K1, K3 | PROVEN OFFLINE (`test_restart_closes_the_journal_rebuilds_every_object_and_keeps_the_broker`) |
| a | zgubione potwierdzenie i wybicie stopu przed pierwszą kontrolą: `BLOCKED`, kill switch miękki, zdejmuje tylko właściciel; awaria w każdym punkcie rozstrzygania | K3, K4 | PROVEN OFFLINE (`test_a_lost_ack_and_stop_out_before_the_first_check_blocks_soft_kills_and_only_the_owner_clears`, `test_a_crash_at_every_point_of_the_unknown_resolution_still_ends_blocked_and_soft_killed`) |
| b | wybicie stopu u brokera księgowane dokładnie raz; limity strat na dokładnych granicach; awaria między zaksięgowaniem a eskalacją kill switcha | K3, K4 | PROVEN OFFLINE (`test_b_stop_out_at_the_broker_is_booked_exactly_once`, `test_b_loss_limit_kills_fire_at_the_exact_boundaries_after_the_reconciler_books`, `test_b_crash_between_booking_and_kill_escalation_keeps_entries_blocked_and_the_startup_check_reapplies`) |
| c | timeout zamknięcia: lista pozycji zamiast ponownej wysyłki, opóźnione zamknięcie księgowane raz | K3 | PROVEN OFFLINE (`test_c_flatten_close_timeout_lists_positions_never_resends_and_the_lagging_closure_is_booked_once`, `test_c_close_timeout_not_applied_keeps_exiting_and_the_retry_closes_only_after_the_list_shows_it`) |
| d | twardy kill przy otwartej pozycji: awaryjne spłaszczenie i księgowanie przez restarty; wyjścia nigdy nie blokowane przez bramkę | K3, K4, niezmiennik 5 | PROVEN OFFLINE (`test_d_hard_kill_mid_position_emergency_flattens_and_books_across_restarts`, `test_d_emergency_flatten_with_every_journal_write_failing_still_closes_and_is_booked_after_restart`, `test_d_exits_are_never_blocked_by_the_gate_even_with_every_entry_check_failing`) |
| e | błąd zapisu dziennika w każdym punkcie wejścia: nigdy wysyłka „na ślepo”; śmierć między kontekstem wejścia a wierszem INTENT niczego nie wysyła, a ponowna próba po restarcie nadpisuje sierotę kontekstu i idzie dalej | K1 (niezmiennik 1), K3 | PROVEN OFFLINE (`test_e_a_journal_write_failure_at_each_write_point_of_enter_never_sends_blind`, `test_e_a_crash_between_the_entry_context_and_the_intent_row_sends_nothing_and_the_retry_proceeds`) |
| f | zapis transakcji wszystko albo nic; obraz pliku dziennika zrobiony między dwoma instrukcjami | K3 | PROVEN OFFLINE (`test_f_record_trade_is_all_or_nothing_when_a_statement_fails_and_a_retry_books_both_once`, `test_f_a_kill_9_image_taken_between_the_trade_insert_and_the_pnl_update_holds_neither`) |
| g | to, co broker zrobił, gdy silnika nie było, jest wykrywane po restarcie | K3 | PROVEN OFFLINE (`test_g_what_the_broker_did_while_the_engine_was_down_is_discovered_after_the_restart`, `test_g_a_fill_accepted_while_down_without_a_broker_stop_is_adopted_and_protected_with_the_entry_stop`) |
| h | pełny dzień `Engine` z restartem między wejściem a wyjściem: jedna transakcja, spójna księga; awaria między zaksięgowaniem a eskalacją; obraz po `kill -9` w trakcie księgowania | K2, K3 | PROVEN OFFLINE (`test_h_full_engine_day_with_a_restart_between_entry_and_exit_books_one_trade_and_a_consistent_ledger`, `test_engine_crash_between_booking_and_kill_escalation_reapplies_the_hard_kill_before_any_broker_call`, `test_engine_kill_9_inside_record_trade_restarts_from_the_crash_image_and_books_exactly_once`, `test_engine_journal_failure_between_the_trade_and_pnl_statements_books_once_on_the_next_pass`) |
| BUG-E2E-1 | wejście przyjęte przez uzgadnianie przechodzi straże po wypełnieniu (wielkość, poślizg) | K3g | PROVEN OFFLINE (`test_bug_e2e_1_an_adopted_oversize_fill_is_closed_by_the_risk_guard` (3 ścieżki), `test_bug_e2e_1_engine_restart_mid_entry_still_closes_a_slipped_fill`, `test_crash_after_adoption_before_the_fill_guard_still_closes_after_the_restart`; naprawa `6275009`) |
| BUG-E2E-2 | zgubiona transakcja w stanie `BLOCKED` ma wyjście przez `resolve-lost-trade` i trafia do liczników | K3h | PROVEN OFFLINE (`test_bug_e2e_2_the_owner_books_a_blocked_lost_trade_with_resolve_lost_trade`: kod 0, jedna transakcja ze znacznikiem `ESTIMATED`, zlecenie `CONFIRMED`, pozycja `FLAT`, wynik w liczniku dnia zamknięcia, po restarcie i dwóch przebiegach uzgadniania nadal `FLAT` bez nowego `MISMATCH_TIMEOUT`; naprawa `58708be`) |

## 7. Otwarte i zamknięte punkty

Stan na kod z commitu `d8be683`. Punkty zamknięte są tu zostawione, żeby podpis właściciela był świadomy, co się zmieniło od pierwszej wersji tego dokumentu (`ca79717`); zamyka je kod i testy z podanych commitów, a nie wzmianka w dokumencie. Punkty otwarte nie zmieniają statusów w tabelach powyżej.

| Punkt | Stan | Dowód albo co dalej |
|---|---|---|
| Zadanie `image` w CI czerwone (cztery testy z `tests/unit/test_prereg.py`) | ZAMKNIĘTE | `GW_TEST_IMAGE=1` w obrazie testowym; CI run `41279529142` na `505a243` i `41283304148` na `075ae3d`, wszystkie trzy zadania zielone (K1c) |
| **BUG-E2E-1**: wejście przyjęte przez uzgadnianie omijało straże po wypełnieniu | ZAMKNIĘTE | `6275009`, poprawki `90a6d15`, `be6d841`, `5e55510`, `524650f`, `af37edc`, `1bc3ce1` (K3g) |
| **BUG-E2E-2**: zgubiona transakcja (`UNKNOWN` + `BLOCKED`) bez wyjścia dla właściciela, strata poza licznikami | ZAMKNIĘTE | `resolve-lost-trade`: `3c7e710`, `8a048ed`, `58708be`, runbook 8.5 krok 6 `5f288cd`, `23d5dc6` (K3h). Do czasu rozstrzygnięcia obowiązuje zasada przejściowa: pozycja `BLOCKED` bez pól liczy się do ryzyka otwartego jako pełne `max_loss_per_trade` (decyzja H6) |
| Lista poprawek z przeglądu zadania 14: przypadki brzegowe `reset-position` (h1, h6, h7), `smoke --hold` (h2, h4, h5), 403 bez kodu błędu liczone jako odrzucenie danych logowania, 403 z kodem przejściowym | ZAMKNIĘTE | `daf5be1` (h1), `23d5dc6` (h6, h7), `0d6aced` (h2, h4, h5), `1581514` (h3); 403 bez `errorCode` rozstrzygnięte wcześniej (`9746350`) |
| Lista poprawek z przeglądu zadania 12: drugie zamknięcie bez odczytu listy (N1), kolejność zapisu intencji wyjścia (N2), treść alarmu (N3), wyjście `BLOCKED` tylko przy HARD (N4), limit czasu ochrony stopu 2 s | ZAMKNIĘTE | `ce3a93d` (N1), `e97d763` (N2 do N4, awaria dziennika), `f42ec7b` (N5, L23-a, O-3 do O-5), `90a6d15` (L23-a przy realnych limitach czasu) (K3j) |
| Alarm `LOGIN_LOCKOUT` wspominał plik `data/LOGIN_RESET` | ZAMKNIĘTE | `c460e52`: alarm odsyła do procedury z runbooka (zatrzymaj silnik, sprawdź klucz, jedna próba logowania), a plik zostaje jako mechanizm wsparcia, którego właściciel nie tworzy (decyzja T14) |
| Specyfikacja (wiersz 57 i pytanie 13) mówiła o pomiarze „poślizgu stopów” | ZAMKNIĘTE | poprawiony tekst specyfikacji; v1 mierzy poślizg wejścia i spread, a koszty wyjść STOP są niezmierzone (decyzja L2) |
| Komunikat raportu przy `PASS` nie wspominał o przeglądzie właściciela | ZAMKNIĘTE | `c52c62f`: „To informacja dla właściciela, nie zgoda...” (K5c) |
| Brak narzędzia zapisującego odczyty live do `tests/integration_live_readonly/fixtures/` (plan, zadanie 18, krok 2) | OTWARTE | fixtures nie powstaną z samego uruchomienia L0.2; osobna praca po odczytach z VPS |
| Opóźnienie wpisu zamknięcia w historii Capital.com (C7) niezmierzone | OTWARTE, OWNER STEP | pomiar na próbie `smoke` (krok [8]); jeśli historia opóźnia się regularnie, każdy dzień kończy się `BLOCKED` + SOFT i potrzebna jest opcja B (decyzja H1) |
| `REJECTED_BUT_LISTED`: pozycja u brokera nie jest prowadzona przez silnik | OTWARTE z założenia | właściciel sprawdza pozycję w aplikacji i zamyka ją ręcznie, jeśli jest niechciana (runbook, decyzja O11) |
| Kontrola zgodności wielkości potwierdzenia i pozycji jest blokująca | OTWARTE, OWNER STEP | gdyby żywy broker raportował różne wielkości przy czystym wypełnieniu, każde wejście przeszłoby przez `UNKNOWN` (`WARN [2]` w `smoke`); sprawdza `smoke` (decyzja A8) |
| Niezmienniki po KAŻDYM teście awarii wspólnym haczykiem (K1f) | OTWARTE | spec. 17.1 poprawiona; generycznego haczyka nie ma (decyzja P1) |
| Stany `WORKING`, `PARTIAL`, `CANCELING` | OTWARTE z założenia | w v1 nieosiągalne; gdyby wystąpiły, uzgadnianie kończy się `InvalidTransition`, wejścia zostają zablokowane i po 60 s włącza się SOFT (decyzja P3) |
| Miękki kill switch niezapisany przy pełnym dysku ginie po restarcie; po restarcie na pełnym dysku możliwe jest jedno pierwsze zlecenie dnia | OTWARTE, znane ograniczenie | decyzje K6 i H4 |
| `fixed_min_size_sessions = 0` ładuje się, ale raport L1 odmawia oceny | OTWARTE, drobne | nie dotyczy wartości 5 z pre-rejestracji (decyzja L6) |
| Zabezpieczenie gałęzi `main`, login w CODEOWNERS, jednorazowa kontrola hooka w świeżej sesji lokalnej Claude Code | OWNER STEP | decyzja C1; lista w `docs/superpowers/decisions-log.md`, sekcja „Otwarte po stronie właściciela” |
| Zaparkowane drobiazgi (komentarze, długość wierszy, drobne luki testowe adaptera, tail dziennika) | OTWARTE, nieblokujące | decyzja P6 |

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
- [ ] Przeczytałem `docs/superpowers/decisions-log.md`, w tym zaakceptowane luki (strażnik live nie obejmuje Basha, ochrona gałęzi i CODEOWNERS to moje kroki) i sekcję „Otwarte po stronie właściciela”.
- [ ] Wiem, że `data/ARM_LIVE` i `live_allowed = true` ustawiam wyłącznie ja, i wiem, jak rozbroić silnik (hamulce 1 do 3 i punkt 6 runbooka).
- [ ] Wiem, że `data/L1_PASSED` tworzę wyłącznie ja, dopiero po 5 sesjach i po przeglądzie; status `PASS` w raporcie nie jest zgodą.
- [ ] Wiem, że bramka L2 nie jest planowana w v1 i że nie skaluję kapitału bez dowodu.

Imię i nazwisko: ______________________________

Data: ____-__-__

Podpis: ______________________________
