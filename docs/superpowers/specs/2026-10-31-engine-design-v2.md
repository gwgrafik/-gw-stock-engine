# Silnik tradingowy v2.1: CFD na DE40, Capital.com live (40 €)

Data: 2026-10-31
Status: do przeglądu właściciela (zastępuje v1: akcje USA i Alpaca, które jest w `archiwum/`)
Zakres: wariant A, **live od pierwszego dnia na 40 €** (decyzja właściciela), z uzbrajaniem ręcznym i pierwszym tygodniem na wielkości minimalnej
Podstawa: `docs/research/2026-10-31-silnik-tradingowy-akcje-usa.md`, `docs/research/2026-10-31-brokerzy-w-ue-do-automatu.md` i notatki w `docs/research/notes/`. Znaczniki [H]/[M]/[L] oznaczają pewność z tych notatek.

## Decyzje właściciela, które ukształtowały v2

1. Alpaca wykluczona (praktycznie tylko USA).
2. Trading 212 wykluczony: warunki API (pkt 4.2(a)) zabraniają handlu algorytmicznego, czyli dokładnie tego, co budujemy.
3. Pierwszy adapter to **Capital.com, od razu live** (decyzja właściciela z 2026-11-01: bez fazy demo; konto demo dla tego klucza zresztą nie istnieje, host demo odpowiada `error.null.accountId`). Rachunek docelowy (z 40 €) wiąże jawnie polecenie `bind-account` (sekcja 9).
4. Wariant **A**: CFD na indeksie **DE40** (waluta EUR, bez przewalutowania na koncie w EUR), jeden instrument, bez filtra relative volume. Testujemy **tylko na DE40**. Inne instrumenty w EUR (np. CFD na akcje z Euronext) to odroczone eksperymenty (sekcja 15).
5. Właściciel **mieszka w Holandii** i tymczasowo jest w Polsce. Wcześniejsze założenie o polskiej rezydencji podatkowej było błędne. Podatki, ochrona inwestora i dostępność brokerów są oceniane dla rezydenta NL (research w toku, sekcja 16).
6. Pisemnej zgody Capital.com na handel przez API **nie będzie** (właściciel zrezygnował z maila). Ryzyko regulaminowe (T&C v2.1: roboty „tylko gdy wyraźnie dozwolone") jest **świadomie zaakceptowane**. Łagodzenie: jedna transakcja dziennie, kill switch na odmowę dostępu, na koncie tylko kwota akceptowalna do utraty.
7. Kapitał 40 €; limity ryzyka z sekcji 5; pierwszy tydzień wyłącznie na wielkości minimalnej (0,001).

## 1. Cel i kryterium sukcesu

Autonomiczny silnik (bez zatwierdzania każdej transakcji), działający w jednym kontenerze Docker, który:

1. raz dziennie handluje jednym indeksem CFD (**DE40**) według zamrożonej reguły ORB, na prawdziwym rachunku Capital.com z 40 €,
2. **za każdym razem potwierdza stan u brokera**, zanim zmieni własny,
3. egzekwuje twarde limity ryzyka i kill switch,
4. mierzy **zysk netto po realistycznych kosztach** i raportuje go uczciwie.

**Sukces v1 to nie zysk.** Sukces v1 to: silnik pracuje bez niewyjaśnionych rozbieżności z brokerem i dostarcza zalogowane dane o kosztach i zachowaniu brokera przy minimalnym ryzyku. Niezależne repliki ORB na indeksach CFD z 2026 roku dają po kosztach wynik bliski zera (NQ +0,002 R, DAX −0,038 R, SPX −0,081 R na transakcję [M, jedna publikacja blogowa]; różnica między NQ a DAX mieści się w szumie), a według obliczeń agentów badawczych wykrycie przewagi +0,002 R wymagałoby ok. 4,8 mln transakcji. **Rozsądnym założeniem roboczym jest oczekiwanie netto zerowe lub ujemne.**

Zasady przejęte z `gw-solana-sniper` (`docs/GW_MASTER_HANDOFF_2026-10-23.md`), obowiązują także tutaj:

- liczy się zrealizowany zysk netto po kosztach, a nie „testy przeszły";
- pozycja jest `CLOSED` dopiero po potwierdzeniu u brokera;
- nieudane wyjście **nigdy** nie znika z nadzoru;
- dzienny budżet ryzyka liczony według czasu zamknięcia i z realnymi kosztami;
- jedna baza kodu, jedna bramka ryzyka, jeden dziennik, jedna księga.

**Ograniczenie zasobów.** Silnik ma działać na istniejącym, współdzielonym VPS z ok. 700 MB wolnej pamięci, używanym swapem i botem z prawdziwymi pieniędzmi. Stąd cel pamięciowy ok. 100 MB, twardy limit kontenera (sekcja 13) i brak ciężkich runtime'ów.

### Poza zakresem v1

Skalowanie kapitału ponad kwotę z sekcji 5; akcje (prawdziwe i CFD) oraz skaner spółek; wolumen, RVOL, EDGAR, halty; opcje; dźwignia powyżej limitu z sekcji 9; LLM; ML; panel WWW; wiele strategii i instrumentów naraz; rozliczenia podatkowe (poza eksportem pełnej księgi transakcji). Odroczone eksperymenty opisuje sekcja 15.

## 2. Fakty, na których stoi projekt

| Fakt | Konsekwencja w projekcie | Pewność |
|---|---|---|
| Capital.com: REST i WebSocket, osobne hosty demo i live; sesja ważna 10 minut (`CST` i `X-SECURITY-TOKEN`), utrzymywana przez ping | Pętla utrzymania sesji i serializowane logowanie (sekcja 13) | H |
| Limity: 10 zapytań/s ogólnie, 1 zlecenie na 0,1 s, `POST /session` 1/s na klucz, demo 1000 zleceń na godzinę, WebSocket 40 instrumentów na subskrypcję | Budżet zapytań (sekcja 13); dla jednego instrumentu limity nie są wiążące | H |
| Klucz API wymaga 2FA i nie ma kluczy tylko do odczytu; **klucz live działa** (logowanie i odczyty potwierdzone 2026-11-01), a host demo odpowiada `error.null.accountId` (brak konta demo) | Silnik używa wyłącznie hosta live; klucz oznacza prawo handlu, więc plik z uprawnieniami 600 poza repo i środowiskiem deweloperskim (sekcja 13) | H |
| Stop i take-profit można dołączyć atomowo do `POST /positions` (`stopLevel`, `stopDistance`, `profitLevel`); **nie** dla prawdziwych akcji | Wejście ze stopem u brokera w jednym wywołaniu, bez okna bez ochrony; tylko CFD (sekcja 8) | H |
| Zakres historii aktywności ograniczony do 1 dnia | Uzgadnianie opieramy na liście pozycji, a historię pobieramy częściej niż co dobę (sekcja 7) | H |
| W dokumentacji nie ma klucza idempotencji podawanego przez klienta | Wyjaśnianie niepewnych wysyłek przez listę pozycji; jedna pozycja na instrument (sekcja 6) | M |
| Brak strumienia zdarzeń o pozycjach i zleceniach w opisanym WebSocket (strumień cen i świec OHLC) | Stan pozycji pochodzi z odpytywania REST, nie ze strumienia (sekcja 7) | M |
| Odczyt z API 2026-11-01 (rynek zamknięty, ostatnia migawka): **DE40** bid 25780,3 / offer 25782,3 (spread 2,0 pkt, ok. 0,008%), waluta EUR; reguły: `minDealSize` 0,001, `minSizeIncrement` 0,001, `maxDealSize` 1000, `minStopOrProfitDistance` 0,01% ceny, depozyt 5%, trailing niedostępny, zlecenie rynkowe dostępne. Dla porównania US100 (USD) 1,8 pkt, US500 (USD) 0,6 pkt | Wielkość w krokach 0,001; minimalna pozycja DE40 ≈ 26 € wartości i ≈ 1,3 € depozytu; stop w zakresie 0,01–100% ceny | H (odczyt z API) |
| Konto zawiera dwa rachunki CFD w EUR (saldo 0 € i 40 €, oba `ENABLED`) | Rachunek docelowy wiąże jawnie polecenie `bind-account`, a silnik przy starcie sprawdza identyfikator i saldo (sekcja 9) | H |
| Finansowanie overnight CFD na indeksy (przykład US100: −0,02237% dziennie od pełnej wartości), naliczane ok. 21:00 UTC (źródła rozbieżne: 21:00 lub 22:00) | Spłaszczenie przed naliczeniem; brak pozycji przez noc | L–M |
| Brak źródeł o wierności demo; **demo nie jest używane** | Koszty (spread, poślizg stopu) mierzymy od pierwszego dnia live na wielkości minimalnej | L (brak źródeł) |
| Dla klienta z NL obowiązują warunki **Capital Com Group Ltd (CySEC 463/25), T&C v2.1 z września 2026** (wersja niderlandzka v2.0 z czerwca 2026), a nie v19 z lutego 2024. Pkt 8.12.3(b): roboty i algorytmy tylko „gdy wyraźnie dozwolone"; pkt 8.24.8 dopuszcza systemy dostępne w platformie, np. MetaTrader; w wyciągniętym tekście nie ma słowa „API". Jednocześnie Capital.com publikuje i promuje API oraz własny serwer MCP | Zewnętrzny bot w Pythonie nie jest oczywiście objęty. **Pisemne potwierdzenie jest warunkiem bramki A**, a pytanie obejmuje też demo (sekcja 16) | M |
| Który podmiot dostaje nowego klienta z NL, nie jest potwierdzone: warunki dla NL wystawia Capital Com Group Ltd (CySEC 463/25), a wcześniej wskazywany Capital Com SV Investments (319/17) dotyczył starszej umowy; trwa też przejęcie klientów FxPro | Pytanie do supportu; dane w profilu (adres w NL, BSN) muszą być prawdziwe | M |
| Ochrona: fundusz CySEC do 20 000 EUR; polisa do 1 mln EUR obejmuje tylko klientów Capital Com Group Ltd. Kifid i holenderski system rekompensat **nie** obejmują firm z paszportem; droga odwoławcza to firma, Ombudsman Cypru, potem sąd w Cyprze (wyjątek tylko dla rezydentów PL i DE) | Brak wpływu na silnik; informacja dla właściciela | M |
| Podatek w NL (informacja ogólna): wynik z CFD osoby prywatnej domyślnie w box 3, czyli zryczałtowany zwrot od wartości majątku na 1 stycznia (6,00% dla „innych aktywów" w 2026, stawka 36%, kwota wolna 59 357 EUR); pojedyncze transakcje nie są opodatkowane ani odliczane; ryzyko box 1 wynika z orzecznictwa o „przewidywalnej korzyści", a znane wyroki o botach dotyczyły arbitrażu kryptowalut z trafnością 75–81%; reforma (rzeczywisty zwrot od 2028) nie jest uchwalona | Księga zachowuje pełne dane transakcji; konsultacja z doradcą w NL przed bramką A | M |
| AFM stosuje te same limity ESMA dla CFD (indeksy 20:1, zamknięcie przy 50% depozytu, ochrona przed saldem ujemnym); nie znaleziono zakazu handlu algorytmicznego dla klientów detalicznych | Limity z sekcji 9 są zgodne z regulacją | M |
| Czasowy pobyt w Polsce nie zmienia rezydencji podatkowej w NL; warunki Capital.com (pkt 3.6.2, 13.1.1(b), 4.4.2) wymagają prawdziwych danych o zamieszkaniu i ostrzegają, że dostęp z innego kraju „może nie być dozwolony" | Prawdziwe dane w profilu; pytanie do supportu o logowania z Polski; silnik uruchamiany z serwera w UE | M |
| Wolne ok. 700 MB, swap w użyciu; limit cgroup z `oom_score_adj` jądro respektuje przy braku pamięci | Limit kontenera i zabicie naszego procesu w pierwszej kolejności (sekcja 13) | H (dokumentacja) |

## 3. Architektura

Jeden proces asyncio, jeden plik SQLite (WAL). Moduły mają jedno zadanie i komunikują się przez wąskie interfejsy.

```
 notowania/świece ──► ┌──────────┐   sygnał   ┌────────────┐  PASS/BLOCK
 (z brokera, 5 min)   │ strategy │───────────►│    risk    │◄── kill switch
                      └──────────┘            └─────┬──────┘
                                                    ▼
 ┌─────────┐   ┌────────────┐   ┌────────────┐   ┌──────────────┐
 │ journal │◄─►│  orders    │──►│ execution  │──►│   broker     │──► Capital.com (live)
 │ SQLite  │   │ (FSM)      │   │ wejście/   │   │ (interfejs)  │    REST + WebSocket
 └────▲────┘   └─────▲──────┘   │ wyjście    │   └──────┬───────┘
      │              │          └────────────┘          │ ten sam interfejs:
      │        ┌─────┴──────┐   ┌────────────┐          ▼
      └────────│ reconciler │   │  watchdog  │      FakeBroker (testy, awarie)
               └────────────┘   └────────────┘
                 report / costs czytają dziennik
```

| Moduł | Odpowiada za | Nigdy nie robi |
|---|---|---|
| `broker` | Interfejs `Broker` z deklaracją zdolności oraz adaptery: `capitalcom` (live) i `FakeBroker` (wzorzec testów kontraktowych każdego adaptera) | Nie podejmuje decyzji handlowych |
| `journal` | Surowy dziennik zdarzeń, zlecenia, pozycje, stan ryzyka i kill switcha, księga | Nie zawiera logiki strategii |
| `orders` | Automat stanów zlecenia i pozycji, generowanie własnych identyfikatorów | Nie wysyła nic bez wiersza INTENT w dzienniku |
| `reconcile` | Uzgadnianie dziennika z brokerem (źródło prawdy: broker) | Nie otwiera nowych pozycji |
| `risk` | Bramka PASS/BLOCK, kill switch | Nie blokuje wyjść |
| `strategy` | Dane wejściowe (świece, ATR) → sygnał | Nie ma dostępu do brokera ani dziennika |
| `data` | Świece i notowania z brokera, kalendarz sesji | Nie interpretuje sygnałów |
| `execution` | Procedury wejścia, wyjścia i awaryjnego spłaszczenia | Nie zmienia stanu pozycji bez potwierdzenia brokera |
| `costs` | Konserwatywny model kosztów | Nie zmienia wypełnień w dzienniku, dopisuje kolumny kosztowe |
| `watchdog` | Cisza strumienia, tętno, ważność sesji, wyzwalacze kill switcha | Nie handluje |
| `report` | Zysk brutto i netto, wskaźniki bramek, eksport księgi do CSV | Nie modyfikuje danych |

**Deklaracja zdolności adaptera** (interfejs `Broker` ją wymusza, żeby kolejne adaptery nie wymagały zmian w rdzeniu): sposób dołączania stopu (`atomic`, `after_fill`, `separate`), obsługa bracket, obsługa ułamków, rodzaj klucza idempotencji i jego maksymalna długość, okres keepalive, prymityw uzgadniania, czy instrument jest CFD, reguły instrumentu (minimalna wielkość, krok, minimalna odległość stopu, depozyt). Adapter Capital.com deklaruje: stop `atomic`, brak klucza idempotencji, keepalive < 10 min, uzgadnianie przez listę pozycji, instrument CFD.

**Zależności zewnętrzne:** `aiohttp` (REST i WebSocket w jednej bibliotece), `tzdata` (jeśli obraz nie ma bazy stref czasowych) oraz `pytest` w środowisku deweloperskim. Bazę obsługuje standardowy `sqlite3` z jednym wątkiem zapisu. Każda dodatkowa zależność wymaga uzasadnienia i zmierzonego zużycia pamięci.

## 4. Dzień pracy silnika (czas giełdy instrumentu)

Instrument v1: **DE40** (indeks niemiecki, waluta EUR, bez przewalutowania na koncie w EUR). Wybór wynika z prostoty: wynik w EUR upraszcza księgę i ryzyko, sesja zaczyna się o 9:00 czasu środkowoeuropejskiego, a różnica między replikacjami DAX (−0,038 R) i NQ (+0,002 R) mieści się w szumie [M]; to nie jest dowód przewagi żadnego z nich.

Wszystkie decyzje czasowe opierają się na `zoneinfo` i **profilu giełdy** (strefa, godziny otwarcia i zamknięcia, święta, skrócone sesje), **bez stałych przesunięć**. W bazie czas jest w UTC. W v1 istnieje jeden profil: `XETRA` (Europe/Berlin, sesja 09:00–17:30). Kalendarz świąt to tabela statyczna 2026–2027 w repo, przepisana z oficjalnego kalendarza Deutsche Börse (m.in. 1 stycznia, Wielki Piątek, Poniedziałek Wielkanocny, 1 maja, 24, 25 i 31 grudnia), z testem spójności i kwartalnym przeglądem. Fixtures zmiany czasu: koniec czasu letniego w UE 25.10.2026 i początek 28.03.2027.

| Czas lokalny (Berlin) | Faza | Działanie |
|---|---|---|
| przed 08:55 | Przygotowanie | Logowanie, potwierdzenie rachunku (identyfikator i saldo), rekoncyliacja startowa, ATR14 z dziennych świec, kalendarz, stan rynku |
| 09:00–09:05 | Zakres otwarcia | Zbieranie pierwszej świecy 5-minutowej |
| 09:05 | Decyzja | Kierunek wg koloru świecy, bramka ryzyka, wejście rynkowe ze stopem atomowo (jedno wejście dziennie); termin do 09:10 |
| do 17:25 | Zarządzanie | Stop u brokera, nadzór, uzgadnianie co 10 s |
| 17:25 | Spłaszczenie | Procedura wyjścia, 5 minut przed zamknięciem sesji i długo przed naliczeniem finansowania (ok. 21:00 UTC) |
| po 17:30 | Zamknięcie | Rekoncyliacja końcowa, raport dzienny |

W skrócone sesje (jeśli kalendarz je wskaże) spłaszczamy 5 minut przed skróconym zamknięciem.

**Źródło danych to sam broker:** świece 5-minutowe ze strumienia OHLC (udokumentowane rozdzielczości od MINUTE do WEEK, do 40 instrumentów na subskrypcję) z zapasowym odczytem historii cen przez REST (endpoint i format do potwierdzenia w dry-run), dzienne świece do ATR z REST. Nie używamy żadnych zewnętrznych danych w v1.

## 5. Strategia v1 (jedna zamrożona specyfikacja)

Nazwa: `orb_index_cfd`. To **inny wariant** niż pierwotny pomysł z akcjami: bez filtra relative volume, na indeksie CFD, w obu kierunkach (CFD pozwala handlować krótko bez pożyczki). Wynik opublikowanej strategii na akcjach „in play" (1637%, Sharpe 2,81, backtest bez poślizgu i z dźwignią) nie jest oczekiwaniem.

Reguły:

1. Raz dziennie, tylko DE40, tylko w dni sesji giełdy (nie w święta).
2. Pierwsza świeca 5-minutowa od otwarcia sesji (09:00–09:05 czasu Berlina). Jeśli zamknięcie > otwarcie, kierunek long. Jeśli zamknięcie < otwarcie, kierunek short. Gdy równe, brak wejścia.
3. Wejście rynkowe tuż po zamknięciu pierwszej świecy, najpóźniej do terminu wejścia.
4. Stop: odległość `0,10 × ATR14` (dzienny, w punktach) od ceny wejścia, ustawiona atomowo przy otwarciu pozycji jako `stopDistance`. Nie mniej niż minimalna odległość stopu brokera (`minStopOrProfitDistance`: 0,01% ceny, ok. 2,6 pkt). Sygnał odrzucany, gdy odległość stopu < 3 × bieżący spread.
5. Brak celu zysku. Wyjście: stop u brokera albo spłaszczenie 5 minut przed zamknięciem sesji (17:25).
6. Wielkość: `size = floor_do_kroku(min(max_loss_per_trade / (stop_distance × wartość_punktu), limit_wielkości_wg_dźwigni))`, krok 0,001. W pierwszych `fixed_min_size_sessions` sesjach wielkość jest **stała i minimalna** (0,001). Gdy wielkość jest mniejsza niż minimalna, wpis `SKIPPED_MIN_SIZE`, bez wejścia.
7. Straż poślizgu: jeśli cena wykonania odbiega od notowania w chwili decyzji o więcej niż 25% odległości stopu, pozycja jest natychmiast zamykana, a zdarzenie zapisane.

### Wartości startowe

Są to **założenia projektowe, nie wyniki badań**. Przed pierwszą sesją `dry_run` zamrażamy je skrótem w pre-rejestracji (sekcja 11). Późniejsza zmiana tworzy nowy wariant i zwiększa liczbę przetestowanych wariantów K.

| Parametr | Wartość startowa |
|---|---|
| `virtual_capital` (EUR) | 40 |
| Instrument | DE40 (waluta EUR) |
| `max_loss_per_trade` | 1,00 |
| `max_daily_loss` | 1,50 |
| `max_total_drawdown` (od startu) | 12 |
| `max_effective_leverage` (notional / `virtual_capital`) | 5 |
| `max_entries_per_day` | 1 |
| `fixed_min_size_sessions` | 5 |
| Odległość stopu | 0,10 × ATR14 |
| Minimalna odległość stopu względem spreadu | 3 × spread |
| Straż poślizgu | 25% odległości stopu |
| Spłaszczenie | 5 minut przed zamknięciem sesji (17:25) |
| Cisza strumienia notowań (kill switch miękki) | 60 s |
| Tolerancja rozbieżności z brokerem | 60 s |
| Cykl uzgadniania w sesji | 10 s |
| Okres ping sesji | 5 min |
| Termin wejścia po zakresie otwarcia | 5 min (wejście możliwe 09:05–09:10; po terminie `NO_SIGNAL_DATA`) |

**Wykonalność na 40 €** (reguły odczytane z API 2026-11-01). Minimalna pozycja 0,001 DE40 ma wartość ok. 26 € i wymaga depozytu ok. 1,3 € (5%). Przy limicie dźwigni 5 największa dozwolona wielkość to ok. 0,007 (wartość ok. 180 €, depozyt ok. 9 €). Przy stopie 25–35 pkt (założenie: ATR14 rzędu 250–350 pkt) strata na stopie wynosi ok. 0,18–0,25 € (0,4–0,6% kapitału), a w pierwszych pięciu sesjach na wielkości 0,001 ok. 0,03 €. Wynik pojedynczej sesji to grosze, a wynik roczny to szum rzędu ±8 € (odchylenie jednej transakcji 2–2,8 R), z którego niczego nie wywnioskujemy.

## 6. Zlecenia: automat stanów

### Zlecenie

| Stan | Znaczenie | Wyjścia |
|---|---|---|
| `INTENT` | Zapisane w dzienniku z `synchronous=FULL`, nic nie wysłano | → `SUBMITTING` |
| `SUBMITTING` | Wysyłka w toku | → `CONFIRMED`, `REJECTED`, `UNKNOWN` (timeout 10 s) |
| `UNKNOWN` | Nie wiadomo, czy broker przyjął | → przez listę pozycji do `CONFIRMED`, `NOT_PLACED` |
| `CONFIRMED` | Broker potwierdził wykonanie (pozycja otwarta) | – |
| `REJECTED`, `NOT_PLACED` | Stany końcowe | – |

Zlecenia oczekujące (`LIMIT` i `STOP` istnieją w API) nie są używane w v1. Pełny zestaw stanów (`WORKING`, `PARTIAL`, `CANCELING`, `CANCELED`, `EXPIRED`) zostaje w automacie dla kolejnych adapterów i jest ćwiczony na `FakeBroker`.

Reguły:

- Własny identyfikator `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}` jest zapisany w INTENT. **Capital.com nie przyjmuje identyfikatora od klienta**, więc nasz identyfikator łączymy z odpowiedzią brokera (`dealReference`, a po potwierdzeniu identyfikator pozycji). Nazwy pól i endpointu potwierdzenia ustalamy odczytem dokumentacji, w `dry_run` i na próbie `smoke`.
- **Timeout to `UNKNOWN`, nigdy „nieudane".** Wyjaśnienie: v1 ma co najwyżej jedną pozycję na instrument i jedno wejście dziennie, więc po timeoucie sprawdzamy listę pozycji. Jeśli jest pozycja na naszym instrumencie otwarta po chwili INTENT (zgodny kierunek i wielkość), przyjmujemy ją jako własną. Jeśli po trzech zapytaniach w 30 s jej nie ma, stan to `NOT_PLACED`. Identyfikator nie jest używany ponownie.
- **Ochrona przed podwójnym wejściem:** dopóki stan wejścia to `SUBMITTING` lub `UNKNOWN`, nie wysyłamy kolejnego wejścia. Przy kontach z agregacją pozycji drugie wejście na tym samym instrumencie zwiększyłoby pozycję, więc ta zasada jest krytyczna; tryb konta (agregacja czy hedging) sprawdzamy w próbie `smoke`.
- Nieznany typ zdarzenia lub nieznana odpowiedź brokera trafia do dziennika, nie zmienia stanu i zwiększa licznik alarmowy.
- Każda surowa odpowiedź brokera jest zapisywana **przed** przetworzeniem.
- Zamiana zlecenia nie jest używana: anulujemy i składamy nowe z nowym `seq`.

### Pozycja

`FLAT` → `ENTERING` → `OPEN` (pozycja potwierdzona przez brokera) → `EXITING` → `FLAT`, plus `BLOCKED` (rozbieżność z brokerem: tylko decyzja rekoncyliacji lub właściciela). Stan pozycji zmienia się wyłącznie na podstawie odpowiedzi i listy pozycji brokera, nigdy na podstawie samego wysłania zlecenia.

## 7. Uzgadnianie z brokerem

Broker jest źródłem prawdy. Uzgadnianie uruchamia się po starcie (przed jakimkolwiek wejściem), po każdym odnowieniu sesji, co 10 s w sesji i po jej zamknięciu. Przy jednym instrumencie koszt to ok. 6 zapytań na minutę, daleko poniżej limitu.

| Rozbieżność | Reakcja |
|---|---|
| Pozycja u brokera na naszym instrumencie, a my mamy `UNKNOWN` lub `SUBMITTING` | Przyjmij jako własną, dołącz zarządzanie wyjściem i weryfikację stopu |
| Pozycja u brokera bez śladu w naszych zleceniach | `BLOCKED`, kill switch miękki, alarm; **nie dotykamy** (może być ręczna) |
| Pozycja lokalna, której nie ma u brokera | Szukaj zamknięcia w historii aktywności (zakres do 1 dnia); jeśli jest (stop lub wyjście silnika), zaksięguj; jeśli brak, `BLOCKED` i alarm |
| Różnica wielkości lub kierunku | `BLOCKED`, alarm |
| Zlecenie oczekujące u brokera na naszym instrumencie | Obce w v1: alarm i anulowanie po decyzji właściciela |
| Pozycja otwarta bez ustawionego stopu | Procedura zabezpieczenia: ustaw stop; jeśli zawiedzie, kill switch twardy |

Nierozstrzygnięta rozbieżność trwająca dłużej niż 60 s włącza kill switch miękki. Zamknięcie pozycji przez stop u brokera wykrywamy jako zniknięcie pozycji z listy, a cenę i przyczynę zamknięcia odczytujemy z historii aktywności lub transakcji (pola do potwierdzenia w `dry_run` i na próbie `smoke`). Historia ma limit jednego dnia, więc pobieramy ją po każdej wykrytej zmianie i po zamknięciu sesji.

## 8. Wejście, wyjście, brak handlu

**Wejście.** Zlecenie rynkowe z atomowo dołączonym stopem (`stopDistance`), więc nie ma okna bez ochrony. Po potwierdzeniu silnik porównuje cenę wykonania z notowaniem z chwili decyzji (straż poślizgu, sekcja 5) i zapisuje obie. Rynek nie pozwala ograniczyć poślizgu zleceniem limitowym w tym wariancie, więc poślizg jest zjawiskiem mierzonym, nie ograniczanym.

**Wyjście silnika** (spłaszczenie 5 minut przed zamknięciem sesji, wyjątki, ręczne polecenie):

1. Pozycja → `EXITING`.
2. Zamknij pozycję wywołaniem zamykającym u brokera.
3. W razie timeoutu lub odmowy: uzgadniaj, nie wysyłaj ponownie na ślepo. Gdy rynek instrumentu jest niedostępny, stop u brokera zostaje, alarm, ponawianie po wznowieniu.
4. Jeśli pozycja zniknęła, bo zadziałał stop, zaksięguj zamknięcie przez stop.

**Awaryjne spłaszczenie** to ta sama procedura ze skróconymi limitami czasu, uruchamiana przez kill switch twardy i powtarzana do uzyskania `FLAT` lub wyczerpania prób (wtedy alarm i pozycja zostaje pod nadzorem).

**Luka i stop.** Stop u brokera wykonuje się po następnej dostępnej cenie, więc przy luce strata może przekroczyć odległość stopu. Gwarantowany stop (GSL) jest w regulaminie, ale jego dostępność dla naszego konta nie jest potwierdzona (pytanie 1) i kosztuje; v1 z niego nie korzysta.

**Brak handlu.** Święta, skrócone sesje, instrument niedostępny, wygasły kalendarz, kill switch i dane starsze niż 60 s oznaczają brak wejścia, zapisany z powodem.

## 9. Silnik ryzyka i kill switch

Bramka ryzyka jest wywoływana synchronicznie na ścieżce każdego wejścia. Specyfikacja kontroli pochodzi z `trader-risk` (MIT, `Jakeschincariol/day-trader-skill`, commit `3d0f4ef`), zaadaptowana do autonomicznego silnika.

| Kontrola | Blokuje wejście, gdy |
|---|---|
| Format | Brak instrumentu, kierunku, wielkości lub stopu |
| Stop obowiązkowy | Brak stopu albo stop po złej stronie |
| Strata na transakcję | `size × stop_distance × wartość_punktu` > `max_loss_per_trade` |
| Dźwignia | Notional / `virtual_capital` > `max_effective_leverage` |
| Depozyt | Wymagany depozyt przekracza dostępny depozyt konta po potrąceniu rezerwy lub poziom depozytu spadłby poniżej progu bezpieczeństwa |
| Liczba wejść | `max_entries_per_day` wykorzystane |
| Dzienny limit strat | Strata zrealizowana (według czasu zamknięcia, netto) + ryzyko otwartych ≥ `max_daily_loss` |
| Strata łączna | Strata łączna od startu ≥ `max_total_drawdown` |
| Okno godzinowe | Poza oknem, święto, skrócona sesja, stan rynku niedostępny |
| Duplikat | Pozycja lub zlecenie na tym instrumencie już istnieje |
| Dane | Notowanie starsze niż 60 s lub brak ATR |
| Kill switch | Aktywny którykolwiek poziom |

**Wyjść nie blokujemy nigdy.** Licznik strat dziennych jest zapisany na dysku, więc restart go nie zeruje.

**Uzbrajanie live i ochrona przed pomyłką.** Silnik ma dwa tryby: `dry_run` (domyślny: pełna logika na notowaniach live, ale **bez wysyłania zleceń**, decyzje trafiają do dziennika) i `live`. Przejście do `live` wymaga jednocześnie: (1) pliku `data/ARM_LIVE` utworzonego przez właściciela (kod nigdy go nie tworzy), (2) `live_allowed = true` w `config/runtime.toml`, (3) powiązania rachunku poleceniem `bind-account`, które wypisuje rachunki (typ, waluta, saldo) i zapisuje wybrany identyfikator po jawnym potwierdzeniu właściciela, (4) zgodności odcisku konfiguracji z pre-rejestracją. Przy każdym starcie silnik sprawdza, że rachunek o zapisanym identyfikatorze istnieje, jest `ENABLED`, ma saldo > 0 i walutę EUR; w przeciwnym razie zostaje w `dry_run` i zgłasza alarm. Jeśli aktywny rachunek sesji różni się od powiązanego, silnik **jednorazowo** wywołuje `PUT /session` tuż po logowaniu, przed subskrypcją strumienia, i nigdy w trakcie pracy. Klucz leży w pliku z uprawnieniami 600 poza repo i poza środowiskiem deweloperskim.

### Kill switch

| Poziom | Skutek | Wyzwalacze |
|---|---|---|
| Miękki | Brak nowych wejść; istniejące pozycje zarządzane normalnie | Plik `data/STOP`; dzienny limit strat; rozbieżność z brokerem > 60 s; cisza strumienia notowań > 60 s; nieudane logowanie lub wygaśnięcie sesji bez odnowienia; trzy odrzucenia z rzędu; nieznane odpowiedzi brokera ponad próg; stan rynku niedostępny w oknie handlu |
| Twardy | Zamknij pozycje procedurą z sekcji 8, potem poziom miękki | Plik `data/HARD_STOP`; strata dzienna ≥ 2 × limit; strata łączna ≥ `max_total_drawdown`; pozycja bez stopu dłużej niż 10 s po nieudanym zabezpieczeniu |

Stan kill switcha jest zapisywany w bazie i przeżywa restart. Zdjęcie poziomu wymaga jawnej akcji właściciela, nigdy automatycznie.

## 10. Kapitał, koszty i księga

**Kapitał.** Rachunek live ma 40 €, a silnik dodatkowo egzekwuje `virtual_capital` jako twardy limit ryzyka i dźwigni (nigdy większy niż saldo rachunku). Instrument i rachunek są w EUR, więc nie ma przewalutowania ani potrzeby kursu EURUSD. Księga „cień 20 USD" z wcześniejszej wersji odpada.

**Model kosztów (konserwatywny, zastępowany pomiarem po bramce L1).** Wypełnienie z brokera jest punktem wyjścia, do którego silnik dolicza: spread liczony z **własnego strumienia bid/ask w chwili decyzji**, a nie z założeń; poślizg 0,5 punktu na wejściu i dodatkowo 1,0 punktu przy zamknięciu stopem; finansowanie overnight (w v1 zero, bo pozycji nie trzymamy przez noc, ale pole w księdze zostaje); markup walutowy 0,4%, **jeśli** dotyczy wyniku z CFD przy różnicy waluty instrumentu i konta (pytanie 5); brak prowizji od transakcji na indeksach CFD. Wszystkie stawki są w konfiguracji, nie w kodzie.

**Księga.** Dla każdej transakcji: instrument, kierunek, wielkość, notowanie w chwili decyzji, cena wykonania wejścia i wyjścia, `gross_pnl`, `spread_cost`, `slippage_penalty`, `financing`, `fees`, `net_pnl`, czasy sygnału, wysyłki i potwierdzenia (UTC), opóźnienie sygnał → wykonanie, przyczyna wyjścia, waluta konta i instrumentu oraz kurs przeliczenia, jeśli broker go poda. Eksport pełnej księgi do CSV od pierwszego dnia, jako podstawa późniejszych rozliczeń dla rezydenta NL (przeliczenia i klasyfikacja podatkowa poza zakresem v1).

## 11. Bramki dowodowe i pre-rejestracja

**Ważne ograniczenie.** Strategia handluje raz dziennie na jednym instrumencie, czyli ok. 250 transakcji rocznie. Przy przewadze netto bliskiej zera próba rzędu tysięcy transakcji jest nieosiągalna w rozsądnym czasie (1000 transakcji to ok. 4 lata). Dlatego bramki nie mierzą przewagi, tylko jakość pomiaru i bezpieczeństwo.

Decyzją właściciela nie ma fazy demo. Bezpieczeństwo zapewniają: tryb `dry_run`, wielkość minimalna przez pierwsze sesje, twarde limity i kill switch. Progi poniżej to **propozycje** [M] do zamrożenia przed pierwszą sesją.

| Bramka | Warunek |
|---|---|
| **L0 (przed pierwszym zleceniem live)** | Wszystkie testy przechodzą (lokalnie, w CI i w obrazie); odczyty live przechodzą (`integration_live_readonly`); co najmniej jedna pełna sesja `dry_run` na notowaniach live bez rozbieżności i błędów; ćwiczenia kill switcha (oba poziomy) i restartu; pamięć zmierzona pod limitem cgroup; odciski konfiguracji zamrożone w pre-rejestracji; rachunek powiązany; próba `smoke` (jedno wejście i natychmiastowe zamknięcie o wielkości 0,001 ze stopem) zakończona bez rozbieżności; uzbrojenie `ARM_LIVE` przez właściciela |
| **L1 (po `fixed_min_size_sessions` = 5 sesjach na wielkości minimalnej)** | Zero niewyjaśnionych rozbieżności pozycja/broker starszych niż 60 s; zero nieobsłużonych odrzuceń; spread i poślizg stopów zmierzone i porównane z modelem (rozbieżność kosztów większa niż 2× model uruchamia przegląd); dopiero wtedy wielkość wg reguł z sekcji 5 |
| **L2 (zmiana kapitału lub zakresu)** | Co najmniej 1000 transakcji i jednostronna dolna granica 95% (klastrowana po dniach) średniej netto > 0. Przy jednej transakcji dziennie to ok. 4 lata, więc w v1 **nie planujemy skalowania**; bramka istnieje, żeby nikt nie skalował kapitału bez dowodu |

Dodatkowe zasady statystyczne: t ≥ 2 tylko dla jednej wstępnie zarejestrowanej specyfikacji; przy ponad ok. 10 wariantach t ≥ 3 albo deflated Sharpe ≥ 0,95.

**Pre-rejestracja** (`docs/preregistration/v1.md`, powstaje w planie implementacji, **przed** pierwszą sesją `dry_run`): skróty SHA-256 plików `config/strategy_v1.toml`, `config/risk_v1.toml` i `config/costs_v1.toml`, **adapter, klasa instrumentu i model kosztów** w skrócie, liczba wariantów K, definicje metryk i bramek, data rozpoczęcia. Zmiana któregokolwiek elementu po tej dacie tworzy nowy wariant.

## 12. Testy

Pisane przed kodem (TDD) dla automatu stanów, uzgadniania i ryzyka.

- **Jednostkowe:** przejścia stanów, generowanie identyfikatorów, każda kontrola ryzyka, model kosztów, wielkość pozycji i zaokrąglanie do kroku, okna czasowe (święta, skrócone sesje, zmiana czasu).
- **Awarie na `FakeBroker`:** timeout wysyłki → `UNKNOWN` → wyjaśnienie przez listę pozycji; podwójne wejście zablokowane; odpowiedź brokera po stronie błędu; wygaśnięcie sesji (10 minut) i odnowienie; wyścig logowań (limit 1/s); pozycja zamknięta przez stop u brokera; pozycja obca; pozycja bez stopu; instrument niedostępny; cisza strumienia; odrzucenie wejścia z powodu minimalnej odległości stopu; restart procesu w środku wejścia i wyjścia (`kill -9`); luka większa niż stop.
- **Niezmienniki (sprawdzane po każdym teście awarii):** (1) żadne wejście nie wyszło bez wiersza INTENT; (2) żadna zmiana stanu pozycji bez odpowiedzi lub listy pozycji brokera; (3) dźwignia i ekspozycja ≤ limity; (4) licznik strat dziennych przeżywa restart; (5) wyjścia nigdy nie zablokowane; (6) co najwyżej jedna sesja brokera na raz.
- **Integracyjne tylko do odczytu na koncie live (`integration_live_readonly`):** logowanie, rachunki, reguły instrumentu, notowania i świece, **bez zleceń**; uruchamiane na VPS z kluczem z pliku (nigdy z czatu ani repo). Ścieżkę zleceń sprawdza kontrolowana próba `smoke` na wielkości minimalnej (sekcja 11).
- **Odtwarzanie:** zapis surowych odpowiedzi z dnia `dry_run` lub z sesji na wielkości minimalnej służy jako fixture regresyjny; jest uzupełnieniem, nie dowodem.

Przy każdej deklaracji ukończenia obowiązuje weryfikacja uruchomieniem testów, a nie opis.

### Gdzie biegną testy i CI

| Rodzaj testu | Wymaga | Gdzie biegnie |
|---|---|---|
| Jednostkowe, awarie na `FakeBroker`, niezmienniki | Tylko Python, bez sieci i bez kluczy | Sesja deweloperska, VPS, CI |
| Budowa obrazu Dockera i testy logiki uruchamiane w obrazie | Demon Dockera | CI i VPS |
| Integracyjne tylko do odczytu na koncie live (`integration_live_readonly`) | Klucz live i dostęp do Capital.com | **Tylko VPS** |
| `dry_run` i sesje live (bramki L0 i L1) | Klucz, sieć, ciągła praca | **Tylko VPS** |

CI to GitHub Actions (`.github/workflows/ci.yml`):

- Wyzwalacze: `push`, `pull_request` i ręczne uruchomienie (`workflow_dispatch`).
- Zadanie `tests`: macierz Python 3.12 i 3.13, instalacja z przypiętych wersji, `pytest -m "not integration_live_readonly"`. Testy `integration_live_readonly` mają znacznik i **nigdy nie biegną w CI**.
- Zadanie `image`: budowa obrazu `python:3.12-slim` i uruchomienie w nim testów logiki.
- **CI nie dostaje żadnych sekretów brokera ani dostępu do VPS.** Wdrożenie na VPS jest ręczne; automatyczny deploy z CI jest poza zakresem v1.
- Higiena łańcucha dostaw: `permissions: contents: read`, akcje zewnętrzne przypięte do pełnego SHA commita.
- Zielony CI na `main` jest warunkiem uznania zmiany za ukończoną i niezależnym potwierdzeniem testów uruchomionych lokalnie; nie zastępuje ich.
- Wypchnięcie pliku workflow może wymagać dodatkowego uprawnienia integracji GitHub (pytanie 17). W razie odmowy właściciel dodaje plik przez interfejs GitHub.

## 13. Wdrożenie i operacje

- Jeden kontener `python:3.12-slim`, użytkownik bez uprawnień, system plików tylko do odczytu z tmpfs i wolumenem `/data` (SQLite).
- **Limit pamięci:** `mem_limit` 256 MB, `memswap_limit` równy `mem_limit` (kontener bez swapu), niższe `mem_reservation`, `pids_limit`, **`oom_score_adj` +1000**, żeby przy ogólnym braku pamięci jądro zabijało nasz proces w pierwszej kolejności, a nie bota z prawdziwymi pieniędzmi. Zabicie procesu nie spłaszcza pozycji, bo stop jest u brokera; po restarcie obowiązuje uzgadnianie. Obsługę pól w Compose v5.6.0 i rachunkowość swapu w jądrze VPS sprawdzamy w pierwszym zadaniu planu, zanim cokolwiek trafi na serwer. Cel zużycia ok. 100 MB jest założeniem, mierzonym w kryteriach akceptacji.
- **Nic nie wchodzi na współdzielony VPS bez pomiaru pamięci i zgody właściciela.** Rozwój i testy logiki odbywają się poza nim (sesja deweloperska, CI).
- Klucz, identyfikator, hasło API i identyfikator rachunku w pliku `~/.config/gw/capitalcom.json` na VPS (uprawnienia 600, jak pozostałe klucze), montowanym do kontenera tylko do odczytu jako `/secrets/capitalcom.json`. Tokeny sesji (`CST`, `X-SECURITY-TOKEN`) żyją w pamięci i nie wymagają zapisu na dysk.
- **Uzbrajanie live** (sekcja 9): bez pliku `data/ARM_LIVE` silnik pracuje w trybie `dry_run` i nie wysyła zleceń.
- **Utrzymanie sesji:** ping co 5 minut, ponowne logowanie przy odpowiedzi 401, logowania serializowane (limit 1/s). Nie przełączamy rachunków w trakcie pracy (`PUT /session` zatrzymuje strumień); jedyne dozwolone przełączenie to jednorazowe, tuż po logowaniu, przed subskrypcją strumienia. Plik blokady w `/data` i procedura restartu „zatrzymaj, potem uruchom", żeby nie dublować sesji.
- Tętno do pliku co kilka sekund; healthcheck Dockera sprawdza świeżość.
- Powiadomienia przez interfejs `Notifier`: w v1 log i plik alarmów; adapter Telegram jako późniejsze rozszerzenie.
- Budżet zapytań: silnik ogranicza się do połowy limitów brokera (ok. 5 zapytań/s ogólnie), czyta kody odpowiedzi i przy limitach wstrzymuje zapytania niekrytyczne; uzgadnianie ma priorytet.
- Wdrożenie na VPS jest ręczne i niezależne od CI (sekcja 12).
- W razie awarii brokera silnik nie ponawia w pętli; przechodzi w kill switch miękki.

## 14. Struktura repozytorium i praca z Claude Code

```
pyproject.toml
CLAUDE.md                     zasady pracy, komendy, zakazy
.claude/settings.json         hooki fail-closed, reguły deny
.claude/agents/               risk-reviewer, order-state-auditor, backtest-skeptic (tylko Read, Grep, Glob)
.github/workflows/ci.yml      CI: testy logiki (Python 3.12 i 3.13) oraz w obrazie Dockera
src/gwstock/                  config, clock, journal, broker/ (base, fake, capitalcom), orders,
                              reconcile, risk, data, strategy, execution, costs, watchdog, report, app
tests/                        unit, fault, integration_live_readonly
config/                       strategy_v1.toml, risk_v1.toml
docs/                         research, superpowers/specs, superpowers/plans, preregistration, ops
docker/                       Dockerfile, compose.yml
```

Zabezpieczenia pracy z Claude Code (od najmocniejszej): klucze i dostęp do środowiska produkcyjnego poza zasięgiem agenta; reguły `deny`; hooki `PreToolUse` z `onFailure: "block"` (hooki domyślnie przepuszczają przy błędzie) blokujące zmiany hosta brokera; piaskownica; subagenci recenzujący tylko do odczytu. Wzorce Bash w regułach nie są granicą bezpieczeństwa. CLAUDE.md jest tylko wskazówką.

Narzędzia zewnętrzne: oficjalny MCP Capital.com (repozytorium organizacji Capital.com) jako referencja do przeczytania, nie zależność wykonania (README zastrzega, że API może się zmienić lub zostać wyłączone bez uprzedzenia); Day Trader Skill i `tradermonty` jako referencje.

## 15. Odroczone eksperymenty (każdy zwiększa K)

US100 i US500 (osobny profil giełdy NYSE, sesja od 15:30 czasu środkowoeuropejskiego, wynik w USD wymagający przeliczenia na EUR); CFD na akcje w EUR (np. z Euronext) z wolumenem z zewnętrznego źródła i filtrem RVOL (inny wariant: depozyt 20%, dywidendy, luki); time-stop i wyjście po utracie momentum (parametry 180 s / +0,4% nie mają poparcia w badaniach); prawdziwe akcje przez inną trasę (IBKR przy 500–1000 USD, wymaga potwierdzenia OAuth 1.0a); adapter cTrader po zatwierdzeniu aplikacji; drugi, osobny host; adapter Telegram.

## 16. Pytania otwarte i sposób rozstrzygnięcia

Żadne nie blokuje pisania silnika na `FakeBroker`. Pytania 2 i 3 rozstrzygnięto odczytem z API (2026-11-01). Pytania 4, 7, 8, 10, 12 i 13 rozstrzyga `dry_run` i pierwsze sesje na wielkości minimalnej. Pytanie 1 zostało pominięte decyzją właściciela (ryzyko zaakceptowane, sekcja 18). Pytanie 19 rozstrzyga polecenie `bind-account`.

| # | Pytanie | Jak rozstrzygnąć |
|---|---|---|
| 1 | Capital.com: zgoda na handel przez API (pkt 8.12.3(b), 8.24.8 T&C v2.1), podmiot, GSL | **Pominięte decyzją właściciela**; szkic maila zostaje w `docs/ops/capital-com-support-email.md` |
| 2 | Czy klucz API powstaje bez zakończonej weryfikacji | **Rozstrzygnięte:** klucz live działa, logowanie i odczyty przeszły (2026-11-01) |
| 3 | DE40: minimalna wielkość, krok, depozyt, minimalna odległość stopu, nazwa instrumentu | **Rozstrzygnięte odczytem API (2026-11-01):** 0,001 / 0,001, depozyt 5%, 0,01% ceny, nazwa `DE40`; godziny handlu odczytujemy z pola godzin otwarcia (zadanie 1 planu) |
| 4 | Tryb konta (agregacja czy hedging) i zachowanie drugiego wejścia na tym samym instrumencie | Próba `smoke` i dziennik pierwszych sesji |
| 5 | Czy markup walutowy 0,4% dotyczy wyniku CFD | Nie dotyczy DE40 (instrument i konto w EUR); do sprawdzenia przy dodaniu instrumentu w innej walucie |
| 6 | Konsultacja z holenderskim doradcą podatkowym: klasyfikacja wyników z CFD i częstego handlu automatycznego, obowiązki sprawozdawcze dla zagranicznego brokera | Doradca w NL (research ogólny w toku) |
| 7 | Endpoint i pola potwierdzenia wykonania, historii aktywności i transakcji (cena wyjścia, przyczyna zamknięcia przez stop) | Odczyt dokumentacji i `dry_run`/próba `smoke` |
| 8 | Format i dostępność historii świec 5-minutowych i dziennych przez REST; opóźnienie świec OHLC ze strumienia | `dry_run` |
| 9 | Pomiar pamięci procesu pod limitem cgroup oraz rachunkowość swapu (`--memory-swap`) w jądrze VPS | Pomiar na VPS po zgodzie właściciela |
| 10 | Czy zaokrąglenie czasu wejścia o 09:05 i zachowanie stopów przy luce są zgodne z modelem | Dziennik pierwszych sesji live |
| 11 | Dostęp z Polski podczas czasowego pobytu (pkt 3.6.2) | Logowanie z Windowsa w Polsce zadziałało 2026-11-01; właściciel wraca do NL w czwartek; obserwacja, bez maila |
| 12 | Czy `GET /prices` zwraca dane potrzebne do ATR i świec zapasowych bez limitów utrudniających | `dry_run` |
| 13 | Faktyczny spread i poślizg stopów na wielkości minimalnej | Pierwsze sesje live (bramka L1) |
| 14 | Źródło danych do rozszerzeń o akcje (jeśli kiedyś) | Decyzja właściciela po zmierzeniu kosztów |
| 15 | Rachunek na koncie live: waluta bazowa i przewalutowania | E-mail, decyzja właściciela |
| 16 | Czy hosting na współdzielonym VPS wystarczy, czy potrzebny osobny host | Pomiar pamięci (pytanie 9) i decyzja właściciela |
| 17 | Czy integracja GitHub może wypychać pliki `.github/workflows` | Próba przy implementacji; w razie odmowy właściciel dodaje plik |
| 18 | Plan B, jeśli Capital.com odmówi lub odpowie niejednoznacznie: trasy, które regulamin wprost dopuszcza lub które mają jawne API dla klientów detalicznych (MetaTrader u Capital.com, cTrader u brokerów UE, Saxo OpenAPI, LYNX/IBKR) i ich ograniczenia (pamięć VPS, minimalny kapitał) | Decyzja właściciela po odpowiedzi supportu; adaptery są wymienne dzięki interfejsowi `Broker` |
| 19 | Który z dwóch rachunków (0 € i 40 €) jest docelowy | Polecenie `bind-account`: wypisuje rachunki z walutą i saldem, zapisuje identyfikator po potwierdzeniu właściciela |

## 17. Kryteria akceptacji v1

1. Wszystkie testy jednostkowe i awaryjne z sekcji 12 przechodzą (lokalnie i w CI, na Pythonie 3.12 i 3.13 oraz w obrazie Dockera); niezmienniki spełnione po każdym teście awarii; kontrakt adaptera przechodzi na `FakeBroker` i w odczytach live.
2. `dry_run` na notowaniach live bez zleceń przez co najmniej jedną pełną sesję, potem pierwsza sesja live na wielkości minimalnej bez ingerencji: logowanie → potwierdzenie rachunku → zakres otwarcia → sygnał → wejście ze stopem → nadzór → spłaszczenie → raport.
3. Zabicie procesu w środku wejścia i wyjścia oraz zabicie przez OOM: po restarcie brak zdublowań i pozycji osieroconych, potwierdzone testem i na pierwszych sesjach live.
4. Ćwiczenia kill switcha: oba poziomy przez pliki, cisza strumienia, wygaśnięcie sesji i rozbieżność z brokerem zasymulowane.
5. Raport dzienny z wynikiem brutto i netto, kosztami, licznikami błędów wykonania i rozbieżności.
6. Zużycie pamięci zmierzone pod limitem cgroup i opisane.
7. Silnik nie wysyła zleceń bez uzbrojenia (`ARM_LIVE`, powiązany rachunek, `live_allowed`, zgodny odcisk konfiguracji); bez uzbrojenia pracuje w trybie `dry_run`.

## 18. Główne ryzyka projektu

| Ryzyko | Skutek | Łagodzenie |
|---|---|---|
| Strategia nie ma przewagi netto | Wynik bliski zera lub ujemny | To spodziewany wynik; bramki mierzą jakość pomiaru; silnik jako instrument pomiarowy |
| Regulamin Capital.com dla NL (T&C v2.1): algorytmy tylko „gdy wyraźnie dozwolone", w tekście brak słowa „API" | Zawieszenie dostępu API lub konta, trudna droga odwoławcza (Cypr) | **Ryzyko zaakceptowane przez właściciela** (bez maila do supportu); jedna transakcja dziennie, kill switch na odmowę dostępu, na koncie tylko kwota do utraty |
| Odwołanie lub zmiana API bez uprzedzenia, zmiana podmiotu (FxPro) | Silnik przestaje działać | Interfejs `Broker` z deklaracją zdolności; adapter wymienny; kill switch miękki |
| Klucz z prawami handlowymi i 40 € na koncie | Przejęcie klucza oznacza możliwość handlu na koncie | Plik z uprawnieniami 600 poza repo i środowiskiem deweloperskim, klucza nie wklejać do czatu, rotacja po każdym wycieku; limity i kill switch |
| Błąd w automacie stanów lub uzgadnianiu | Duplikaty, pozycje osierocone | TDD, `FakeBroker` z awariami, niezmienniki, recenzja `order-state-auditor` |
| OOM na współdzielonym VPS | Zabity proces, ryzyko dla bota z prawdziwymi pieniędzmi | Limit cgroup i `oom_score_adj` +1000, stop u brokera, pomiar przed wdrożeniem |
| Pierwszy kontakt API zleceń z prawdziwymi pieniędzmi | Błąd parsowania odpowiedzi, podwójne wejście | `dry_run`, próba `smoke` na 0,001, wielkość minimalna przez 5 sesji, kill switch |
| Dwa rachunki CFD | Zlecenie na zły rachunek | `bind-account`, sprawdzenie identyfikatora i salda przy każdym starcie |
| Próba zbyt mała do wnioskowania | Fałszywe poczucie przewagi | Bramki bez kryterium zysku; skalowanie poza zakresem v1 |
| Błędne założenia o rezydencji i podatkach | Złe rozliczenie | Doradca podatkowy w NL przed bramką A; eksport pełnej księgi |
| Publiczne repo | Ujawnienie treści | Brak sekretów w repo; zmiana na prywatne zaplanowana przez właściciela |
