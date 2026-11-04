# Silnik z pasmami (wiele pozycji naraz, wiele wejść dziennie): stan na 4.11.2026, wieczór

Dokument dla właściciela. Bez kluczy, numerów kont i sald. Pełny projekt: `docs/superpowers/specs/2026-11-04-multi-lane-design.md`,
plan i rozstrzygnięcia: `docs/superpowers/plans/2026-11-04-multi-lane-engine.md`.

## 1. Najważniejsze

- Kod pasm jest zbudowany i przetestowany **na FakeBrokerze**, ale **na VPS nic z niego nie działa**: tam stoi stary obraz Dockera
  (kod `src/` jest wpisany do obrazu). `git pull` nie zmienia działającego silnika. **Nie przebudowuj obrazu (`build`, `up --build`)
  przed świadomą decyzją**, bo wciągnąłbyś kod, który nie przeszedł jeszcze prób na rachunku (sekcja 5).
- Silnik DE40 `orb_index_cfd` v1 zachowuje się tak samo jak przed zmianami: 18 złotych śladów (porównanie bajt w bajt) i pełny zestaw testów
  (ponad 5200) przechodzą, pliki `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml` oraz odcisk pre-rejestracji v1 są nietknięte i przypięte w teście.
- **Rentowność nie jest wykazana.** Kandydat (reguła B1 na świecach M5, US500) ma t ≈ 0,6 na 42 transakcjach z oceny końcowej, wybrany spośród
  42 sprawdzonych wariantów na trzech skorelowanych indeksach. To hipoteza do testu forward (`docs/preregistration/forward-us500-b1-m5-sesje.md`),
  nie strategia gotowa do zarabiania. Przy koncie 40 EUR i wielkości 0,01 nawet dodatnia przewaga to grosze.

## 2. Co zbudowano (zadania 1-15 + poprawki po przeglądach)

| Element | Gdzie | Uwagi |
|---|---|---|
| Kalendarz NYSE | `calendar.py` | tabela świąt 2026-2027 z reguł, **do porównania z oficjalnym kalendarzem NYSE** (nie było dostępu do źródła) |
| Reguła B1/M5 | `strategy_bars.py`, `lanes.py`, `config/lane_us500_b1_m5.toml` | cała zamrożona hipoteza (filtry dnia, 10 wejść, dzienny stop -3 R); test krzyżowy z `research/` |
| Dziennik per instrument | `journal.py` | liczniki i księga per instrument, księgowanie dwufazowe dla walut obcych, bez zmiany schematu |
| Typy celu, FakeBroker | `types.py`, `broker/fake.py` | cel niosą podklasy `Targeted*` (klasy bazowe bez zmian) |
| Adapter Capital.com | `broker/capitalcom_*.py` | cel zysku (`profitDistance`), `set_protection` (stop i cel jednym PUT), TARGET z historii, waluta instrumentu; **założenia niepotwierdzone na koncie** (sekcja 5) |
| Uzgadnianie per instrument | `reconcile.py` | znaczniki per instrument (DE40 zachowuje stare klucze), pozycja bez pasma tylko zgłaszana |
| Wykonawca pasma | `execution.py`, `lanes.py` | `Executor.for_lane`, TARGET/FORCED, straż w walucie obcej, awaryjne spłaszczenie wielu pasm |
| Bramka portfelowa | `portfolio.py`, `config/portfolio.toml` | wspólne limity (propozycja równa v1), maks. 2 pozycje, fail-closed |
| Silnik z pasmami | `app.py`, `engine_lanes.py`, `fx.py`, `watchdog.py` | jedna korutyna, pętla dnia z NYSE, kurs EURUSD po stronie niekorzystnej, izolacja wyjątków pasm |
| Pre-rejestracja i uzbrojenie pasm | `prereg.py`, `cli.py` | odcisk per pasmo, zamraża tylko właściciel; pasmo live tylko przy WSZYSTKICH warunkach, inaczej dry_run z powodem |
| Raport per pasmo | `report/*` | bez mieszania walut, raport DE40 przy jednym paśmie bajt w bajt jak dotąd |
| Diagnostyka walut | `research/diag_fx.py` | tylko odczyt, uruchamia właściciel |
| Symulacje odporności | `tests/fault/test_multi_lane.py` | 26 scenariuszy (restart z dwiema pozycjami, kill twardy, święta, zmiany czasu...) |

Niezależne przeglądy (zadanie 9 i 12) znalazły po 2 ustalenia ważne, wszystkie naprawione; symulacje (zadanie 14) znalazły 2 błędy, naprawione.

## 3. Jak pasmo trafia na rachunek (dopiero w przyszłości, świadoma decyzja właściciela)

Pasmo jest live tylko, gdy spełnione są **wszystkie** warunki: proces uzbrojony (cztery warunki jak dla DE40), jawny `config/lanes.toml` z `enabled = true`
(w repozytorium go nie ma), zamrożony i zgodny odcisk pasma, waluta obca obsłużona w adapterze, pliki pasma równe wczytanym. W przeciwnym razie
pasmo działa jako dry_run, a `status` podaje powód. Wielkość pasm jest zawsze minimalna (US500: 0,01). Nie ma mechanizmu `L1_PASSED` dla pasm.
Komendy (`prereg --lane US500 --print/--write/--check`, `status --epic`, `reset-position --epic` itd.): `docs/ops/runbook.md`, sekcja 6.1.
Kod nigdy nie tworzy `data/ARM_LIVE` ani `data/L1_PASSED` i nie ustawia `live_allowed`.

## 4. Kolejność, którą zalecam, zanim cokolwiek pójdzie live

1. **Uruchom `research/diag_fx.py` na VPS** (komenda w nagłówku skryptu): ustali walutę rozliczenia US500, kurs, nazwę epiku EURUSD i reguły handlu.
2. **Wynik testu forward** B1/M5 na US500 po 50 nowych transakcjach (ok. 4 tygodnie sesji). Bez dodatniego werdyktu pasmo nie zasługuje na uzbrojenie.
3. **Próba smoke na US500** (jedno prawdziwe zlecenie 0,01, koszt rzędu centów) z otwartą pozycją, żeby ustalić pola celu zysku, zachowanie `set_stop`
   wobec celu i rozliczenie wyniku USD w EUR (niepotwierdzone C5, C5d, C7, C8). Wymaga Twojej zgody i uzbrojenia.
4. Decyzje właściciela (sekcja 6), zamrożenie odcisku pasma (`--lane US500 --write`), `config/lanes.toml`, dopiero potem przebudowa obrazu.

## 5. Znane ograniczenia (uczciwie)

- Wszystko sprawdzone tylko na FakeBrokerze i fałszywym serwerze HTTP. Pola celu, przyczyna `TP`, zachowanie `set_stop` wobec celu i rozliczenie USD nie są potwierdzone u Capital.com.
- Wynik USD księgowany jest w fazie 2 wyłącznie **szacunkiem** (`ESTIMATED_FX`): kurs EURUSD z chwili rozliczenia, strona niekorzystna, zaokrąglenie w dół do centa,
  bez narzutu przewalutowania (według notatek ok. 0,4%, C8 otwarte) i bez korekty z historii transakcji brokera.
- **Pułapka operacyjna:** nierozliczona transakcja USD (`pnl_pending`) przy wyłączonych pasmach włącza kill SOFT `PNL_PENDING_WITHOUT_LANES`. Żadne polecenie CLI nie
  rozlicza takiego znacznika; odblokowuje dopiero ponowne włączenie `config/lanes.toml`, aby silnik go rozliczył. Do rozważenia: polecenie właściciela do ręcznego rozliczenia kwotą z aplikacji.
- Kill switch jest wspólny dla całego procesu (R3). Pasmo w dry_run bez pozycji nie włącza go ciszą notowań ani błędami uzgadniania (tylko alarm), ale pasmo live tak.
- Odcisk pasma obejmuje kod reguły (`strategy_bars.py`, `lanes.py`): każda zmiana tych plików wymaga ponownego zamrożenia jako nowy wariant (K+1).
- `status` pokazuje tryb pasma wyliczony z plików, nie z działającego silnika.
- Nie zrobiono scenariusza „wejście dwóch pasm w tym samym takcie” (okna sesji DE40 i US500 się nie nakładają).
- Tabela świąt NYSE (zwłaszcza Juneteenth 2027, Boże Narodzenie 2027, brak zamknięcia 31.12.2027) nie jest potwierdzona u źródła.

## 6. Pytania do właściciela (zalecane odpowiedzi w nawiasach)

1. Limity portfela wspólne dla wszystkich pasm: dzienna strata 1,50 EUR, łączna 12 EUR (tak; `config/portfolio.toml` to propozycja równa v1).
2. Pasmo US500 live przed werdyktem forward (nie; najwyżej dry_run).
3. Próba smoke na US500 po `diag_fx` (tak).
4. Ręczny handel na instrumentach, które mają pasmo (nie handlować ręcznie na DE40 i US500).
5. Filtry dnia z badań w paśmie B1 (tak, już są).
6. Portfel z pasmami jako nowy wariant K+1 z osobną pre-rejestracją (tak; reguła DE40 i odcisk v1 bez zmian).
7. Pozycja właściciela na instrumencie bez pasma: silnik jej nie zamyka, ostrzega i wstrzymuje wszystkie wejścia (taki jest obecny wybór).
8. Jak księgować zamknięcie DE40 celem ustawionym ręcznie: dziś TARGET jest mapowany na STOP w paśmie bez celu (zgodnie z v1).
9. Czy notowanie EURUSD z Capital.com może służyć do przeliczeń (tak, po `diag_fx`).
