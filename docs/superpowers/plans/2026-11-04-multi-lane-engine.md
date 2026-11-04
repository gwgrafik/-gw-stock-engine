# Plan: silnik z wieloma pozycjami naraz i wieloma wejściami dziennie (wariant "lanes")

Stan: 4.11.2026. Decyzja właściciela: budujemy równolegle z testem forward (`docs/preregistration/forward-us500-b1-m5-sesje.md`),
kilka wejść dziennie i więcej niż jedna pozycja naraz. To jest NOWY WARIANT (K+1) z osobną pre-rejestracją. Rentowność nie jest
wykazana: t ≈ 0,6 dla kandydata B1/M5 na US500, 42 sprawdzone warianty. Cel tego planu to narzędzie pomiarowe, nie obietnica zysku.

## Kontekst techniczny (ustalony z kodu i notatek API)
- Dziennik: tabela `positions` ma `epic TEXT PRIMARY KEY` (jedna pozycja na instrument). Rachunek ma `hedgingMode: false`
  (`docs/ops/capitalcom-api-notes.md` §2: drugie wejście na tym samym instrumencie agreguje pozycję).
- Silnik (`app.py`, `execution.py`, `risk.py`, `reconcile.py`) jest zbudowany pod JEDEN instrument (`self._epic`, `strat.instrument`).
- v1 nie ma celu zysku (take profit); reguła B1 z badań ma cel 1,5 R, limit czasu pozycji (M5: 50 min) i stop na poziomie świecy.

## Rozstrzygnięcia (rulings)
- **R1. Pasmo ("lane") = jeden instrument + jedna reguła + własna konfiguracja. Najwyżej JEDNA otwarta pozycja na instrument.**
  "Kilka pozycji naraz" znaczy pasma na RÓŻNYCH instrumentach jednocześnie (np. DE40 i US500). Kilka pozycji na tym samym
  instrumencie jest poza zakresem: wymaga `hedgingMode` (decyzja właściciela w panelu Capital.com) i zmiany klucza w dzienniku.
- **R2. Pasmo DE40 `orb_index_cfd` v1 działa BEZ ZMIANY zachowania, a pliki `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`
  zostają bajt w bajt (odcisk pre-rejestracji v1 się nie zmienia).** Wszystkie dotychczasowe testy muszą pozostać zielone.
- **R3. Bramka portfelowa ponad bramkami pasm:** maks. liczba otwartych pozycji, maks. suma ryzyka otwartych pozycji (strata
  przy stopach), wspólny dzienny limit straty i łączne obsunięcie (dziennik `realized_pnl` jest już globalny po dniu), wspólny
  kill switch i wspólny depozyt. Limity tylko z konfiguracji.
- **R4. Wyjścia reguł świecowych:** stop i cel zdejmowane przez brokera (ustawione atomowo przy otwarciu), limit czasu pozycji i
  zaplanowane spłaszczenie przez silnik. Stop i cel w jednej świecy: STOP. Wyjść nigdy nie blokuje bramka ryzyka.
- **R5. Reguła jest wtykiem pasma:** `orb_index_cfd` (istniejąca) i `bar_b1_m5` (nowa). Nowa reguła = nowy wariant + pre-rejestracja.
- **R6. Wielkość:** jak w v1 (minimalna przez pierwsze sesje pasma, wielkość wg reguł dopiero po `data/L1_PASSED`).
- **R7. Uzbrojenie bez zmian:** tylko właściciel (`data/ARM_LIVE`, `live_allowed`); kod niczego nie uzbraja. Pasmo bez zamrożonego
  odcisku pre-rejestracji działa najwyżej jako dry_run (szczegóły w projekcie, zadanie 3).
- **R8. Waluta:** US500/US100 są w USD, rachunek w EUR. Projekt (zadanie 3) ma ustalić, w jakiej walucie broker podaje wynik
  zrealizowany i jak go księgować, bez zgadywania kursu.

## Zasady wspólne dla wszystkich zadań
- Czytaj `CLAUDE.md`: TDD (test najpierw, obserwuj błąd z właściwego powodu), `Decimal` zamiast `float`, wartości tylko z `config/*.toml`,
  kod NIGDY nie tworzy `data/ARM_LIVE` ani `data/L1_PASSED` i nie ustawia `live_allowed = true`, żadnych kluczy ani haseł w plikach.
- Dokumentacja i komentarze po polsku, identyfikatory po angielsku. Commity po polsku z prefiksem (`feat:`, `test:`, `docs:`), jedna
  zmiana logiczna na commit. NIGDY `git commit --amend`, `push --force` ani `reset --hard` na cudzych commitach.
- Testy: `python -m pytest -m "not integration_live_readonly" -q -W error` musi przechodzić (obecnie ok. 3840 testów). Skan:
  `python scripts/hooks/guard_live.py --scan`.
- Nie dotykaj plików poza zakresem zadania. Zmiana `config/strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml` jest zabroniona.
- Nie wykonuj połączeń z brokerem ani z siecią (zadania są offline).

### Task 1: Profil giełdy NYSE w `calendar.py`

Dodaj profil `NYSE` obok istniejącego `XETRA` (zobacz `ExchangeProfile`, `ExchangeCalendar` w `src/gwstock/calendar.py` i jak
`XETRA` definiuje święta, skrócone sesje i wygaśnięcie kalendarza). Wymagania:

1. Strefa `America/New_York`, otwarcie 09:30, zamknięcie 16:00 czasu lokalnego; zmiany czasu obsługuje strefa (nie stałe przesunięcie UTC).
2. Święta NYSE na lata 2026 i 2027 (Nowy Rok, Martin Luther King Jr. Day, Presidents' Day, Wielki Piątek, Memorial Day, Juneteenth,
   Dzień Niepodległości z przeniesieniem na piątek/poniedziałek, Labor Day, Thanksgiving, Boże Narodzenie z przeniesieniem). Wylicz
   daty z reguł i wpisz je jawnie z komentarzem skąd (nazwa święta i reguła). Gdy nie możesz czegoś potwierdzić, napisz to w komentarzu
   słowem "DO POTWIERDZENIA przez właściciela" i NIE zgaduj.
3. Skrócone sesje (zamknięcie 13:00 ET): dzień po Thanksgiving, 24 grudnia (gdy wypada w dzień sesji), 3 lipca (gdy to dzień sesji, a
   4 lipca wypada w dzień roboczy po nim); tak samo oznacz jako "DO POTWIERDZENIA", jeśli reguła jest niepewna.
4. Kalendarz wygasa po ostatnim wpisanym roku tak samo jak XETRA (`CalendarExpiredError`).
5. Testy (nowy plik `tests/unit/test_calendar_nyse.py`): dzień zwykły, weekend, każde święto 2026, skrócona sesja (`close_time` = 13:00),
   przejście na czas zimowy w USA 1.11.2026 (otwarcie 09:30 ET to 13:30 UTC przed zmianą i 14:30 UTC po niej),
   `flatten_time` 5 minut przed zamknięciem, wygaśnięcie.
6. Nie zmieniaj zachowania `XETRA`; wszystkie istniejące testy kalendarza zostają zielone.

### Task 2: Reguła świecowa B1/M5 jako czysta funkcja (`src/gwstock/strategy_bars.py`)

Przenieś regułę B1 z `research/candle_strategies.py` (funkcja `signals`, gałąź `B1`, interwał M5: `n=12`, limit czasu 50 min) do
czystej funkcji bez wejścia/wyjścia, zegara i brokera, w stylu `src/gwstock/strategy.py` (`Decimal` zamiast `float`, bez `import` brokera).

1. Wejście: lista ZAKOŃCZONYCH świec M5 (typ `Candle` z `gwstock.types`, z bid i ask; cena środkowa = (bid+ask)/2 dla OHLC jak w
   `mid()` w badaniach), bieżący spread, krok stopu instrumentu (`STEP`), reguły instrumentu. Okna czasowe i spłaszczenie NIE należą do
   tej funkcji (robi je wywołujący).
2. Reguła (dokładnie jak w badaniach): `b = max(step, 0.25 * spread)`; zakres poprzednich `n` świec (bez bieżącej): `upper`/`lower`;
   świeca sygnału: `rng = h - l > 0`, `body = |c - o| / rng`, `clv = (c - l) / rng`. Long: `c > upper + b` i `body >= 0,60` i `clv >= 0,75`,
   stop = `min(l, upper - b)`. Short: `c < lower - b` i `body >= 0,60` i `clv <= 0,25`, stop = `max(h, lower + b)`. Cel = 1,5 R. Limit czasu 50 min.
   Wartości progowe (`n`, 0,60, 0,75, 0,25, 1,5, 50, 0,25) MUSZĄ pochodzić z nowego pliku konfiguracji `config/lane_us500_b1_m5.toml`
   (napisy dla ułamków), nie z kodu; dodaj ładowanie i walidację w `config.py` w stylu istniejących plików (ściśle, błąd z nazwą pliku).
3. Wyjście: `BarSignal` (zamrożony dataclass): `side`, `stop_level`, `target_r`, `time_limit_min`, `signal_candle_end` (czas UTC).
   Osobna funkcja `entry_plan(signal, quote, rules, risk, ...)` zwraca wielkość, `stop_distance` i `target_distance` z ceny wejścia
   (long po ask, short po bid), z polityką wielkości jak w `strategy.py` (nie kopiuj kodu: wydziel wspólną część albo wywołaj istniejącą
   funkcję bez zmiany jej zachowania). Odległość stopu zaokrąglona W GÓRĘ do kroku brokera, tak jak w v1.
4. Test krzyżowy: `tests/unit/test_strategy_bars.py` z danymi syntetycznymi (ziarno losowe, co najmniej 2000 serii) porównuje decyzje
   (kierunek, poziom stopu z tolerancją 1e-9) z `research/candle_strategies.signals` dla B1/M5; test pomijany, gdy brak katalogu `research/`.
   Do tego testy jednostkowe: brak sygnału przy `rng == 0`, granice progów (0,60, 0,75), long i short, krok stopu, wielkość minimalna.
5. Nie dotykaj `strategy.py` poza ewentualnym wydzieleniem wspólnej funkcji wielkości; testy `strategy` zostają zielone.

### Task 3: Projekt rdzenia pasm i bramki portfelowej (TYLKO projekt, bez zmian w kodzie)

Przeczytaj `src/gwstock/{app,execution,risk,reconcile,journal,cli,watchdog}.py`, `src/gwstock/orders/`, `src/gwstock/broker/`,
`tests/fault/`, `docs/superpowers/specs/2026-10-31-engine-design-v2.md` i `docs/superpowers/decisions-log.md`. Napisz dokument
`docs/superpowers/specs/2026-11-04-multi-lane-design.md` (po polsku), który pozwoli rozdzielić implementację na zadania dla kolejnych
agentów. Dokument ma zawierać:

1. Architekturę pasm zgodną z rozstrzygnięciami R1-R8 (jeden proces, wspólny dziennik, jedna sesja brokera i jeden limit
   zapytań, wspólny kill switch; co jest wspólne, a co per pasmo), z uzasadnieniem odrzuconych alternatyw (np. osobne kontenery).
2. Listę KONKRETNYCH zmian per plik i funkcja (miejsca, gdzie dziś jest `self._epic`, `strat.instrument`, `current_position(epic)`,
   `count_entry_orders`), z oceną ryzyka każdej zmiany dla zachowania pasma DE40 v1 (R2).
3. Cel zysku (take profit): zmiany w adapterze (`capitalcom_trading.py`), maszynie stanów pozycji, uzgadnianiu (wykrycie zamknięcia
   przez cel, rozliczenie z historii brokera) i dzienniku. Jak wygląda stan niepewny (UNKNOWN) dla wyjścia przez cel.
4. Bramkę portfelową (R3): nowe kontrole, nazwy przyczyn odmowy, pola konfiguracji, co liczy się jako ryzyko otwartej pozycji.
5. Uzgadnianie wielu pozycji: co robi z pozycją na instrumencie bez pasma, kolizje z pozycją ręczną właściciela.
6. Pre-rejestrację wielu pasm (odcisk per pasmo albo łączny), warunek uzbrojenia, zachowanie gdy część pasm nie ma odcisku.
7. Walutę (R8): jak broker raportuje wynik zrealizowany dla instrumentu w USD (na podstawie `docs/ops/capitalcom-api-notes.md` i kodu
   `capitalcom_parse.py`), co jest niepewne i jak to zbadać w trybie tylko do odczytu na rachunku.
8. Plan zadań implementacyjnych w kolejności zależności (każde zadanie: pliki, interfejsy, kryteria odbioru, główne testy), z
   zaznaczeniem, które można wykonywać równolegle w osobnych plikach, a które muszą iść po kolei. Oszacuj rozmiar (małe/średnie/duże).
9. Plan testów odporności: rozszerzenie `tests/fault/engine_sim.py` o wiele pasm, scenariusze awarii (restart w trakcie dwóch pozycji,
   rozbieżność brokera dla jednej z nich, zamknięcie przez cel, kill switch twardy przy dwóch pozycjach).
10. Otwarte pytania do właściciela (wyłącznie rzeczy, których nie da się ustalić z kodu).
Nie zmieniaj żadnego pliku poza nowym dokumentem. Jeśli czegoś nie da się ustalić z kodu, napisz "nie ustalono" i wskaż, jak to sprawdzić.
