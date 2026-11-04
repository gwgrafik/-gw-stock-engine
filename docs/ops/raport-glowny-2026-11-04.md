# Raport główny: gw-stock-engine (DE40, Capital.com)

Stan na środę 4 listopada 2026, ok. 11:30 czasu Berlina. Bez kluczy, numerów kont i sald. Autor: Claude, na podstawie wyników z VPS wklejonych przez właściciela.

## 1. W jednym akapicie

Silnik handlowy na CFD DE40 jest zbudowany, przetestowany i **wdrożony na VPS**. Pierwsza prawdziwa transakcja próbna (smoke) przeszła 7 z 7. **Rentowność strategii nie jest wykazana**: testy historyczne na 87 dniach pokazują, że strategia silnika po kosztach lekko traci i wypada gorzej niż "zawsze kupuj". Dlatego handel na żywo na wielkości 0,001 traktujemy jako **eksperyment wykonania i pomiaru kosztów**, a nie źródło dochodu.

## 2. Co działa (potwierdzone)

| Obszar | Stan | Dowód |
|---|---|---|
| Kod silnika (18 zadań, poprawki z przeglądów) | gotowy | gałąź `feat/engine-v2`, 3833 testy lokalnie, CI na GitHub Actions zielone (`tests` 3.12 i 3.13, `image`) |
| Wdrożenie na VPS | działa | obraz `gw-stock-engine:local`, limity 256 MB / swap 0 / 64 procesy, pamięć ok. 35–40 MB, kontener `healthy` |
| Tryb dry_run | działa od 1.11 | codzienne decyzje o 08:05 UTC, brak błędów; jedno ostrzeżenie o błędnej świecy brokera (bid > ask), silnik ponowił zapytanie |
| Ćwiczenia awaryjne 4.1–4.4 | przeszły | miękki i twardy kill switch, zabicie kontenera i procesu, odcięcie sieci (kill switch po 23 s) |
| Pre-rejestracja | zamrożona 1.11 14:38 UTC | odcisk `23046a64…0850`, `--check` OK |
| Pierwsza świeca vs wykres | zgodna | świeca 09:00 (Berlin) = początek, porównana z aplikacją |
| **Smoke (4.11, ok. 11:19 Berlin)** | **PASS 7/7** | wejście DE40 0,001 po 26 260,9, stop 26 225,7 (35 pkt) potwierdzony przez API, pozycja widoczna ze stopem, zamknięcie przez silnik, księgowanie, drugie zamknięcie odrzucone (`POSITION_NOT_FOUND`) |
| Opóźnienie historii brokera | 2,8 s | zamknięcie widoczne w `closed_since` 2,8 s po wysłaniu |

## 3. Co było blokadą i jak ją usunęliśmy

Trzy próby smoke (10:10, 10:25, 11:02) były odrzucane kodem `error.validation.cfd-product.not-active`. Konto było typu CFD, aktywne, z saldem, a DE40 handlowalny. Przyczyną było **niedokończone uruchomienie produktu CFD po stronie Capital.com** (kwestionariusz oceny wiedzy i celu handlu w aplikacji). Po jego wypełnieniu przez właściciela ręczny zakup 0,001 w aplikacji przeszedł, a smoke przeszedł 7/7. Silnik zachował się poprawnie w każdej próbie: odrzucenie zapisane jako `REJECTED`, bez pozycji i bez zleceń niewyjaśnionych.

## 4. Wyniki badań strategii (uczciwie)

Dane: 120 934 świece minutowe DE40 (5.07 – 2.11.2026), pobrane jednym logowaniem; do testów 87 dni handlowych.

- **Reguły świecowe B1–B4** (z opracowania świecowego), 14 wariantów (M1/M5, cały dzień/sesje): **0 kandydatów po etapie wyboru**, czyli żadna nie dała przewagi po kosztach. Kilka jest dodatnich brutto, ale spread i poślizg (ok. 3–4 pkt na transakcję) zjadają wynik.
- **Strategia silnika (ORB, pierwsza świeca 09:00–09:05, stop 0,10 ATR)**, 72 transakcje: **−210 pkt netto** (−0,21 € przy 0,001), trafność 14%, 62 z 72 transakcji kończy się stopem. Długie pozycje +158 pkt, krótkie −368 pkt. **"Zawsze kupuj" zarobiło +314 pkt** (DE40 w tym okresie rósł), "zawsze sprzedawaj" −48. Wniosek: **kolor pierwszej świecy nie daje przewagi**, a wąski stop jest głębiej niż szum pierwszych minut.
- **Test szerokości stopu** (0,10 / 0,25 / 0,50 / 1,00 ATR): skrypt gotowy, **wynik z VPS jeszcze nie otrzymany**. Wszystko poza 0,10 ATR to nowe warianty (K+1), a próba jest mała (ok. 70 dni).
- Wszystkie wyniki: próba za mała, żeby odróżnić przewagę od przypadku. Nic z tego nie obiecuje zysku.

## 5. Jakie inwestycje testowe robimy

| Test | Wielkość | Koszt / ryzyko | Status |
|---|---|---|---|
| Smoke (próba ścieżki zleceń) | DE40 0,001 na 90 s | ułamek centa | **zrobiony, PASS** |
| Ręczny zakup w aplikacji (test aktywacji CFD) | DE40 0,001 | spread | zrobiony, zamknięty ręcznie |
| Pierwsze 5 sesji live | DE40 0,001, jedna decyzja dziennie o 09:05 | ok. 3 centy ryzyka na transakcję (stop ok. 35 pkt), koszt spreadu ok. 0,3 centa | **decyzja właściciela: A) rozbroić i dry_run, B) uzbrojone, start silnika** |
| Wielkość według reguł | ok. 0,007 | ryzyko ok. 0,2 € na transakcję | dopiero po `L1_PASSED` tworzonym przez właściciela |

Limity ryzyka (konfiguracja): strata na transakcję 1,00 €, dzienna 1,50 € (miękki kill switch), łączna 12 € (twardy), dźwignia do 5. **Limity blokują dalsze wejścia po zaksięgowaniu straty, ale nie gwarantują maksymalnej straty przy luce cenowej.**

Założenia kosztów (`costs_v1.toml`): brak prowizji, spread z danych (ok. 1,8 pkt na DE40), poślizg 0,5 pkt, dodatkowo 1,0 pkt przy wyjściu stopem, brak finansowania nocnego (zamknięcie 17:25), brak przewalutowania (DE40 w EUR). Prawdziwy koszt zmierzy saldo przed i po smoke.

## 6. Na czym się skupiamy (priorytety)

1. **Decyzja A/B** po smoke: rozbroić czy uruchomić silnik na realne sesje 0,001.
2. **Zmierzyć realny koszt** (saldo konta po smoke vs 40,00 €) i poślizg z pierwszych sesji.
3. **Wynik testu szerokości stopu** (komenda w sekcji 8): czy jakikolwiek wariant strategii jest wart dalszej obserwacji.
4. **Badanie innych instrumentów bez ryzyka** (tylko odczyt i test historyczny): złoto (`GOLD`: min. 0,01, spread ok. 0,5 USD, depozyt 5%, waluta USD), US100, wybrane akcje. Integracja z silnikiem dopiero dla tych, które wyjdą na plus po kosztach poza próbą. Silnik jest dziś zbudowany tylko pod DE40.
5. **Nie rozszerzać handlu bez dowodu** na przewagę po kosztach.

## 7. Po stronie właściciela (otwarte)

- Prywatne repozytorium (jest publiczne); weryfikacja loginu w `CODEOWNERS`; branch protection na `main`.
- Data rozpoczęcia i tabela K w `docs/preregistration/v1.md`.
- Doradca podatkowy (rezydent NL, CFD); ryzyko regulaminowe Capital.com dla botów (T&C 8.12.3(b), świadomie zaakceptowane).
- 30.12.2026: możliwe inne godziny giełdy, dopisać dzień do `early_closes`.
- Jednorazowe sprawdzenie hooka strażnika w świeżej lokalnej sesji Claude Code (`/hooks`, `claude doctor`).
- `data/ARM_LIVE`, `live_allowed = true` i `data/L1_PASSED` tworzy wyłącznie właściciel.

## 8. Komendy (na VPS, w katalogu `/home/claude/-gw-stock-engine`, jako `claude`)

```bash
# stan
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
# logi i kondycja
docker logs --tail 40 gw-stock-engine ; docker compose -f docker/compose.yml ps
# start / stop silnika
docker compose -f docker/compose.yml up -d gw-stock-engine
docker compose -f docker/compose.yml stop
# uzbrojenie (tylko właściciel) i rozbrojenie
sed -i 's/^live_allowed = false/live_allowed = true/' config/runtime.toml && touch data/ARM_LIVE
rm -f data/ARM_LIVE && sed -i 's/^live_allowed = true/live_allowed = false/' config/runtime.toml
# raport dnia
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.report --day 2026-11-04
# test szerokości stopu (czeka na uruchomienie, bez logowania)
docker compose -f docker/compose.yml run --rm -T -e GW_STOP_ATR=0.10,0.25,0.50,1.00 gw-stock-engine python - < research/orb_daily_backtest.py
```

Skrypty badawcze (tylko odczyt, `research/`): `fetch_m1.py` (pobranie świec, jedno logowanie), `candle_strategies.py` (B1–B4), `orb_daily_backtest.py` (strategia silnika), `scalp_backtest.py` (wybicie 15 min), `diag_account.py` (diagnostyka kont i instrumentu).

## 9. Ograniczenia i uczciwa ocena

- To narzędzie pomiarowe. **Rentowność nie została wykazana**, a dostępne testy wskazują raczej lekką stratę po kosztach.
- Próby są małe (kilkadziesiąt dni, kilkadziesiąt transakcji): wynik pojedynczej sesji na 0,001 to grosze i szum.
- Nie sprawdzono jeszcze: realnego poślizgu w sesji (tylko smoke), zachowania przy luce cenowej i wielosesyjnej pracy live.
- Rozpisane i zamrożone: wariant K=1 (`orb_index_cfd` v1). Każda zmiana strategii to nowy wariant z nową pre-rejestracją.
- Silnik jest tylko pod DE40. Inne instrumenty wymagają osobnej konfiguracji kosztów, wielkości i godzin.

## 10. Repozytorium

https://github.com/gwgrafik/-gw-stock-engine, gałąź `feat/engine-v2` (`main` nietknięty). Specyfikacja: `docs/superpowers/specs/2026-10-31-engine-design-v2.md`. Dziennik decyzji: `docs/superpowers/decisions-log.md`. Runbook: `docs/ops/runbook.md`. Kryteria odbioru: `docs/ops/acceptance-v1.md`.
