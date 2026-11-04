# Wdrożenie nowego obrazu (silnik z pasmami) na VPS: procedura etapowa

Dla właściciela. Zakłada skład v1: **sam DE40, bez `config/lanes.toml`**. W tym składzie nowy kod ma zachowywać się jak stary
(18 złotych śladów, odcisk pre-rejestracji v1 `23046a64…0850` przypięty w teście, schemat dziennika bez zmian, ciała żądań DE40 bajt w bajt).
To wynik końcowego przeglądu (werdykt: wdrażać po poprawkach; poprawki są w gałęzi). Zasady ogólne aktualizacji: `docs/ops/runbook.md` 8.2.

**Nie wdrażaj przed 17:35 w dniu sesji i nigdy przy otwartej pozycji.** Silnik z jutra (5.11) ma wykonać swoją decyzję o 09:05 na STARYM obrazie.
Wszystkie ścieżki poniżej: katalog `/home/claude/-gw-stock-engine` (zaczyna się od myślnika: używaj ścieżek bezwzględnych, jak dotąd).

## Etap 1: przebudowa obrazu, nadal sam DE40 (najwcześniej 5.11 po 17:35)

### 1.A Przed zatrzymaniem (tylko odczyt)
1. Pozycja ma być FLAT: `docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status` (linia `position DE40: FLAT`, `kill switch: NONE`).
2. Raport dnia i saldo z aplikacji zapisz osobno (to pomiar kosztu dnia).
3. Brak pliku pasm (jego obecność zmienia skład silnika, a błędny plik zatrzymuje start): `test ! -e config/lanes.toml && echo BRAK_LANES_OK`.
4. Konfiguracja v1 bez zmian: `git status --short` (może pokazać tylko lokalną zmianę `config/runtime.toml` z uzbrojenia) i `git diff --stat HEAD origin/feat/engine-v2 -- config/strategy_v1.toml config/risk_v1.toml config/costs_v1.toml config/runtime.toml` (wynik pusty).

### 1.B Zabezpieczenie drogi wycofania (silnik zatrzymany: `docker compose -f docker/compose.yml stop`)
1. Zachowaj stary obraz: `docker tag gw-stock-engine:local gw-stock-engine:v1-przed-v2` (bez tego `build` nadpisze jedyny tag).
2. Skopiuj dane przy zatrzymanym silniku: `cp -a data data-kopia-przed-v2` (dziennik, `account.json`, `preregistration.json`).
3. Zapisz logi: `docker logs gw-stock-engine > data/logi-przed-v2.txt 2>&1`.

### 1.C Sprawdzenia (tylko odczyt)
1. Z którego commitu jest stary obraz (porównanie z `0b9daab`, ostatnią zmianą `src/` przed pracami nad pasmami):
   `docker run --rm --network none --entrypoint sh gw-stock-engine:v1-przed-v2 -c 'cd /app/src && find . -name "*.py" | sort | xargs sha256sum' > ~/img.sha`
   `mkdir -p ~/v0 && git archive 0b9daab src | tar -x -C ~/v0 && (cd ~/v0/src && find . -name "*.py" | sort | xargs sha256sum) | diff - ~/img.sha && echo IMG_OK`
   Jeśli diff coś pokaże, wklej wynik: obraz pochodzi ze starszego kodu i trzeba go ocenić osobno.
2. Dziennik: `user_version`, transakcje według instrumentu i brak nowych kluczy (silnik zatrzymany):
   ```bash
   docker compose -f docker/compose.yml run --rm -T gw-stock-engine python - <<'PY'
   import sqlite3
   c = sqlite3.connect("file:/data/journal.sqlite?mode=ro&immutable=1", uri=True)
   print("user_version", c.execute("PRAGMA user_version").fetchall())
   print("trades", c.execute("SELECT instrument, count(*) FROM trades GROUP BY instrument").fetchall())
   print("nowe klucze", c.execute("SELECT key FROM meta WHERE key LIKE 'pnl_pending:%' OR key LIKE 'trade_fx:%' OR key LIKE 'closed_marker:%' OR key LIKE 'mismatch_since:%'").fetchall())
   PY
   ```
   Oczekiwane: w `trades` wyłącznie `DE40`, `nowe klucze` puste.
3. Kształt pól celu w odpowiedziach Capital.com dla DE40 (nowy adapter ignoruje je dla instrumentów bez pasma z celem, więc to tylko informacja):
   `docker compose -f docker/compose.yml run --rm -T -e GW_EPIC=DE40 -e GW_CONFIRMS=3 gw-stock-engine python - < research/diag_fx.py`.

### 1.D Aktualizacja (runbook 8.2: bramy pamięci 600 MB przed budową i 500 MB przed startem)
1. `git pull --ff-only` (jeśli jesteś uzbrojony, najpierw rozbrój wg runbooka 6; po aktualizacji uzbrajasz sam).
2. Test w obrazie testowym pod Pythonem 3.12, bez sieci: `docker compose -f docker/compose.yml --profile test build gw-stock-test` i `docker compose -f docker/compose.yml --profile test run --rm gw-stock-test`.
3. `docker compose -f docker/compose.yml build gw-stock-engine`.
4. Odcisk: `docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --check` (kod 0, `OK`) oraz `--print` (odcisk `23046a64…0850`). Cokolwiek innego: **nie uruchamiaj**, wklej wynik.
5. `docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status` (bez linii `lane …`).
6. Uzbrojenie (jeśli rozbrajałeś) i start: polecenia z runbooka 6 i 8.1. Uzbrajasz Ty; ja nie.

### 1.E Po starcie (po 10 minutach)
- `docker logs gw-stock-engine 2>&1 | grep -E "EngineMisconfigured|lanes|LANE_|UNMANAGED|PNL_PENDING|Traceback"` (wynik pusty).
- `cat data/heartbeat.json` (bez klucza `lanes`), pomiar pamięci z runbooka 2.4 (progi: silnik do 100 MiB, serwer `available` ≥ 400 MB).
- Następnego dnia o 09:05 pierwsza decyzja nowego obrazu: raport dnia (`gwstock.report --day`) i porównanie z oczekiwaniem (jedna pozycja albo brak).

### 1.F Wycofanie (gdy cokolwiek wygląda dziwnie)
1. `docker compose -f docker/compose.yml stop`
2. `docker tag gw-stock-engine:v1-przed-v2 gw-stock-engine:local` (kod `src/` siedzi w obrazie, więc przywrócenie tagu przywraca stary silnik; pliki w `config/` z hosta są dla starego kodu bez znaczenia)
3. `docker compose -f docker/compose.yml up -d gw-stock-engine`

Dziennik: nowy kod nie zmienia schematu, więc starego dziennika nie trzeba przywracać; kopia `data-kopia-przed-v2` jest na wypadek uszkodzenia.

## Etap 2: pasmo US500 w dry_run (decyzje na prawdziwych notowaniach, bez zleceń)
Warunek: etap 1 działa co najmniej jedną pełną sesję bez uwag. Utwórz `config/lanes.toml` z `enabled = true` dla pasma US500 (format i pola: `docs/superpowers/specs/2026-11-04-multi-lane-design.md` sekcja 6 i testy `tests/unit/test_config_lanes.py`;
przed utworzeniem pliku poproś mnie o gotową treść). Bez zamrożonego odcisku pasma pozostaje w dry_run, a `status --epic US500` podaje powód. Pasmo w dry_run tylko mierzy:
ciszą notowań ani błędami uzgadniania nie zatrzymuje wejść DE40.

## Etap 3: ustalenia o brokerze (jedna minimalna próba)
1. `research/diag_fx.py` z `GW_EPIC=US500` (waluta rozliczenia, kurs, nazwa epiku EURUSD, reguły handlu). Komenda w nagłówku skryptu.
2. `smoke --epic US500` wg `docs/ops/runbook.md` 5.1: jedno zlecenie 0,01 ze stopem i celem, spłaszczane przez silnik. Warunki: proces uzbrojony, pasmo w `lanes.toml`. Wklej tylko linie PASS/FAIL/INFO/LOUD i blok USTALENIA.
   Gdy cokolwiek się nie zamknie: usuń `data/ARM_LIVE`, zamknij pozycję przyciskiem w aplikacji, sprawdź historię (runbook 5.1).

## Etap 4: pasmo live na minimalnej wielkości (dopiero po wyniku forward i Twojej decyzji)
`python -m gwstock.prereg --lane US500 --print`, potem `--write` (zamraża Ty), `--check`; pasmo staje się live tylko przy wszystkich warunkach z runbooka 6.1. Wielkość pasm jest zawsze minimalna.
Zalecenie: nie przed werdyktem testu forward B1/M5 (50 nowych transakcji).

## Czego ten dokument nie obejmuje
Rentowność nie jest wykazana. Zachowanie broker-specyficzne (pola celu, rozliczenie USD) jest niepotwierdzone do czasu etapu 3.
