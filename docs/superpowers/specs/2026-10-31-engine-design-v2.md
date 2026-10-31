# Silnik tradingowy v2: CFD na indeksy, Capital.com demo

Data: 2026-10-31
Status: do przeglądu właściciela (zastępuje v1: akcje USA i Alpaca, które jest w `archiwum/`)
Zakres: wariant A (demo z twardą bramką dowodową przed jakimikolwiek prawdziwymi pieniędzmi)
Podstawa: `docs/research/2026-10-31-silnik-tradingowy-akcje-usa.md`, `docs/research/2026-10-31-brokerzy-w-ue-do-automatu.md` i notatki w `docs/research/notes/`. Znaczniki [H]/[M]/[L] oznaczają pewność z tych notatek.

## Decyzje właściciela, które ukształtowały v2

1. Alpaca wykluczona (praktycznie tylko USA).
2. Trading 212 wykluczony: warunki API (pkt 4.2(a)) zabraniają handlu algorytmicznego, czyli dokładnie tego, co budujemy.
3. Pierwszy adapter to **Capital.com demo**. Konto live nie dostaje żadnych środków, dopóki nie przejdziemy bramki A. **Uwaga:** warunki Capital.com dla klientów z NL są ostrzejsze w sprawie robotów niż wersja czytana wcześniej (sekcja 2), więc wybór pozostaje warunkowy do pisemnej odpowiedzi supportu.
4. Wariant **A**: CFD na indeksy, jeden instrument, bez filtra relative volume (notowania CFD nie niosą wolumenu giełdy).
5. Właściciel **mieszka w Holandii** i tymczasowo jest w Polsce. Wcześniejsze założenie o polskiej rezydencji podatkowej było błędne. Podatki, ochrona inwestora i dostępność brokerów są oceniane dla rezydenta NL (research w toku, sekcja 16).

## 1. Cel i kryterium sukcesu

Autonomiczny silnik (bez zatwierdzania każdej transakcji), działający w jednym kontenerze Docker, który:

1. raz dziennie handluje jednym indeksem CFD według zamrożonej reguły ORB,
2. **za każdym razem potwierdza stan u brokera**, zanim zmieni własny,
3. egzekwuje twarde limity ryzyka i kill switch,
4. mierzy **zysk netto po realistycznych kosztach** i raportuje go uczciwie.

**Sukces v1 to nie zysk.** Sukces v1 to: silnik przechodzi bramkę P (hydraulika, sekcja 11) i dostarcza zalogowane dane o kosztach i zachowaniu brokera. Niezależne repliki ORB na indeksach CFD z 2026 roku dają po kosztach wynik bliski zera (NQ +0,002 R, DAX −0,038 R, SPX −0,081 R na transakcję [M, jedna publikacja blogowa]), a według obliczeń agentów badawczych wykrycie przewagi +0,002 R wymagałoby ok. 4,8 mln transakcji. **Rozsądnym założeniem roboczym jest oczekiwanie netto zerowe lub ujemne.**

Zasady przejęte z `gw-solana-sniper` (`docs/GW_MASTER_HANDOFF_2026-10-23.md`), obowiązują także tutaj:

- liczy się zrealizowany zysk netto po kosztach, a nie „testy przeszły";
- pozycja jest `CLOSED` dopiero po potwierdzeniu u brokera;
- nieudane wyjście **nigdy** nie znika z nadzoru;
- dzienny budżet ryzyka liczony według czasu zamknięcia i z realnymi kosztami;
- jedna baza kodu, jedna bramka ryzyka, jeden dziennik, jedna księga.

**Ograniczenie zasobów.** Silnik ma działać na istniejącym, współdzielonym VPS z ok. 700 MB wolnej pamięci, używanym swapem i botem z prawdziwymi pieniędzmi. Stąd cel pamięciowy ok. 100 MB, twardy limit kontenera (sekcja 13) i brak ciężkich runtime'ów.

### Poza zakresem v1

Prawdziwe pieniądze i jakakolwiek ścieżka kodu live; akcje (prawdziwe i CFD) oraz skaner spółek; wolumen, RVOL, EDGAR, halty; opcje, dźwignia powyżej limitu z sekcji 9; LLM; ML; panel WWW; wiele strategii i instrumentów naraz; rozliczenia podatkowe (poza eksportem pełnej księgi transakcji). Odroczone eksperymenty opisuje sekcja 15.

## 2. Fakty, na których stoi projekt

| Fakt | Konsekwencja w projekcie | Pewność |
|---|---|---|
| Capital.com: REST i WebSocket, osobne hosty demo i live; sesja ważna 10 minut (`CST` i `X-SECURITY-TOKEN`), utrzymywana przez ping | Pętla utrzymania sesji i serializowane logowanie (sekcja 13) | H |
| Limity: 10 zapytań/s ogólnie, 1 zlecenie na 0,1 s, `POST /session` 1/s na klucz, demo 1000 zleceń na godzinę, WebSocket 40 instrumentów na subskrypcję | Budżet zapytań (sekcja 13); dla jednego instrumentu limity nie są wiążące | H |
| Klucz API wymaga 2FA, **ten sam klucz działa na demo i live**, nie ma kluczy tylko do odczytu | Jedyną barierą jest host: stała hosta demo w kodzie, brak ścieżki live, konto live bez środków (sekcja 9) | M |
| Stop i take-profit można dołączyć atomowo do `POST /positions` (`stopLevel`, `stopDistance`, `profitLevel`); **nie** dla prawdziwych akcji | Wejście ze stopem u brokera w jednym wywołaniu, bez okna bez ochrony; tylko CFD (sekcja 8) | H |
| Zakres historii aktywności ograniczony do 1 dnia | Uzgadnianie opieramy na liście pozycji, a historię pobieramy częściej niż co dobę (sekcja 7) | H |
| W dokumentacji nie ma klucza idempotencji podawanego przez klienta | Wyjaśnianie niepewnych wysyłek przez listę pozycji; jedna pozycja na instrument (sekcja 6) | M |
| Brak strumienia zdarzeń o pozycjach i zleceniach w opisanym WebSocket (strumień cen i świec OHLC) | Stan pozycji pochodzi z odpytywania REST, nie ze strumienia (sekcja 7) | M |
| Spread w migawce poza sesją: US100 1,8 pkt, US500 0,6 pkt, DE40 2,0 pkt, ok. 0,007–0,010% wartości (poziomy indeksów to założenie agenta) | Koszt wejścia jest ułamkiem R; mierzymy go na własnym strumieniu bid/ask | M (obliczenie agenta) |
| Finansowanie long US100: −0,02237% dziennie od pełnej wartości, naliczane ok. 21:00 UTC (źródła rozbieżne: 21:00 lub 22:00) | Spłaszczenie przed naliczeniem; brak pozycji przez noc | L–M |
| Demo prawdopodobnie nie modeluje poślizgu; brak źródeł o wierności demo | Demo sprawdza hydraulikę, nie koszty ani przewagę | L (brak źródeł) |
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
 │ journal │◄─►│  orders    │──►│ execution  │──►│   broker     │──► Capital.com (demo)
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
| `broker` | Interfejs `Broker` z deklaracją zdolności oraz adaptery: `capitalcom` (demo) i `FakeBroker` (wzorzec testów kontraktowych każdego adaptera) | Nie podejmuje decyzji handlowych |
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

## 4. Dzień pracy silnika (czas `America/New_York`)

Instrument v1: **US100** (nazwa instrumentu do potwierdzenia na demo). Wybór: jedyny z trzech zbadanych indeksów, dla którego replikacja daje wynik netto w pobliżu zera, a nie wyraźnie ujemny (NQ +0,002 R wobec −0,038 R dla DAX i −0,081 R dla SPX); to nie jest dowód przewagi.

Wszystkie decyzje czasowe opierają się na `zoneinfo`, **bez stałych przesunięć**. W bazie czas jest w UTC. Fixtures DST: tygodnie niezgodności zmian czasu między USA a UE (25–31.10.2026, koniec DST w USA 1.11.2026, a w marcu 2027 ok. 14–27.03, do weryfikacji testem). Kalendarz świąt i skróconych sesji NYSE jest **tabelą statyczną 2026–2027** w repo (kalendarz NYSE jest publikowany z wyprzedzeniem), z testem jednostkowym i przeglądem co kwartał.

| Czas ET | Faza | Działanie |
|---|---|---|
| przed 09:25 | Przygotowanie | Logowanie, rekoncyliacja startowa, ATR14 z dziennych świec, kalendarz (święto lub skrócona sesja oznacza brak wejścia), stan rynku instrumentu |
| 09:30–09:35 | Zakres otwarcia | Zbieranie pierwszej świecy 5-minutowej |
| 09:35 | Decyzja | Kierunek wg koloru świecy, bramka ryzyka, wejście rynkowe ze stopem atomowo (jedno wejście dziennie) |
| do 15:55 | Zarządzanie | Stop u brokera, nadzór, uzgadnianie co 10 s |
| 15:55 | Spłaszczenie | Procedura wyjścia (przed naliczeniem finansowania ok. 21:00 UTC, czyli 15:55 ET to 20:55 UTC zimą i 19:55 UTC latem) |
| po 16:00 | Zamknięcie | Rekoncyliacja końcowa, raport dzienny |

W skrócone sesje (zamknięcie 13:00 ET) spłaszczamy o 12:55 ET.

**Źródło danych to sam broker:** świece 5-minutowe ze strumienia OHLC (udokumentowane rozdzielczości od MINUTE do WEEK, do 40 instrumentów na subskrypcję) z zapasowym odczytem historii cen przez REST (endpoint i format do potwierdzenia na demo), dzienne świece do ATR z REST. Nie używamy żadnych zewnętrznych danych w v1.

## 5. Strategia v1 (jedna zamrożona specyfikacja)

Nazwa: `orb_index_cfd`. To **inny wariant** niż v1 z akcjami: bez filtra relative volume, na indeksie CFD, w obu kierunkach (indeks CFD pozwala handlować krótko bez pożyczki). Wynik opublikowanej strategii na akcjach „in play" (1637%, Sharpe 2,81, backtest bez poślizgu i z dźwignią) nie jest oczekiwaniem.

Reguły:

1. Raz dziennie, tylko US100, tylko w dni sesji (nie w święta i nie w skrócone sesje).
2. Pierwsza świeca 5-minutowa 09:30–09:35 ET. Jeśli zamknięcie > otwarcie, kierunek long. Jeśli zamknięcie < otwarcie, kierunek short. Gdy równe, brak wejścia.
3. Wejście rynkowe tuż po 09:35 ET.
4. Stop: odległość `0,10 × ATR14` (dzienny, w punktach) od ceny wejścia, ustawiona atomowo przy otwarciu pozycji jako `stopDistance`. Nie mniej niż minimalna odległość stopu brokera (z danych instrumentu na demo). Sygnał odrzucany, gdy odległość stopu < 3 × bieżący spread.
5. Brak celu zysku. Wyjście: stop u brokera albo spłaszczenie o 15:55 ET.
6. Wielkość: `size = floor_do_kroku(min(max_loss_per_trade / (stop_distance × wartość_punktu), limit_dźwigni))`. Gdy wielkość jest mniejsza niż minimalna wielkość brokera, wpis `SKIPPED_MIN_SIZE`, bez wejścia.
7. Straż poślizgu: jeśli cena wykonania odbiega od notowania w chwili decyzji o więcej niż 25% odległości stopu, pozycja jest natychmiast zamykana, a zdarzenie zapisane.

### Wartości startowe

Są to **założenia projektowe, nie wyniki badań**. Przed pierwszym dniem na demo zamrażamy je skrótem w pre-rejestracji (sekcja 11). Późniejsza zmiana tworzy nowy wariant i zwiększa liczbę przetestowanych wariantów K.

| Parametr | Wartość startowa |
|---|---|
| `virtual_capital` (waluta konta) | 1000 |
| Instrument | US100 |
| `max_loss_per_trade` | 5 |
| `max_daily_loss` | 10 |
| `max_effective_leverage` (notional / `virtual_capital`) | 5 |
| `max_entries_per_day` | 1 |
| Odległość stopu | 0,10 × ATR14 |
| Minimalna odległość stopu względem spreadu | 3 × spread |
| Straż poślizgu | 25% odległości stopu |
| Spłaszczenie | 15:55 ET |
| Cisza strumienia notowań (kill switch miękki) | 60 s |
| Tolerancja rozbieżności z brokerem | 60 s |
| Cykl uzgadniania w sesji | 10 s |
| Okres ping sesji | 5 min |
| Termin wejścia po zakresie otwarcia | 5 min (wejście możliwe 09:35–09:40 ET; po terminie `NO_SIGNAL_DATA`) |

**Wykonalność na koncie 20 USD.** Przy jednym kontrakcie wartość pozycji na US100 jest o rząd wielkości większa niż 20 USD, a przy dźwigni 20:1 (limit ESMA dla indeksów) i depozycie 5% minimalna pozycja wymaga zapewne kilkuset USD depozytu. Faktyczną minimalną wielkość zmierzymy na demo (`GET /markets/{instrument}`). To oznacza, że na koncie rzędu 20 USD ten wariant może być w ogóle niewykonalny na live, a demo sprawdza wyłącznie hydraulikę.

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

- Własny identyfikator `gws-{YYYYMMDD}-{instrument}-{seq}-{rola}` jest zapisany w INTENT. **Capital.com nie przyjmuje identyfikatora od klienta**, więc nasz identyfikator łączymy z odpowiedzią brokera (`dealReference`, a po potwierdzeniu identyfikator pozycji). Nazwy pól i endpointu potwierdzenia ustalamy odczytem dokumentacji i testem na demo.
- **Timeout to `UNKNOWN`, nigdy „nieudane".** Wyjaśnienie: v1 ma co najwyżej jedną pozycję na instrument i jedno wejście dziennie, więc po timeoucie sprawdzamy listę pozycji. Jeśli jest pozycja na naszym instrumencie otwarta po chwili INTENT (zgodny kierunek i wielkość), przyjmujemy ją jako własną. Jeśli po trzech zapytaniach w 30 s jej nie ma, stan to `NOT_PLACED`. Identyfikator nie jest używany ponownie.
- **Ochrona przed podwójnym wejściem:** dopóki stan wejścia to `SUBMITTING` lub `UNKNOWN`, nie wysyłamy kolejnego wejścia. Przy kontach z agregacją pozycji drugie wejście na tym samym instrumencie zwiększyłoby pozycję, więc ta zasada jest krytyczna; tryb konta (agregacja czy hedging) sprawdzamy na demo.
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

Nierozstrzygnięta rozbieżność trwająca dłużej niż 60 s włącza kill switch miękki. Zamknięcie pozycji przez stop u brokera wykrywamy jako zniknięcie pozycji z listy, a cenę i przyczynę zamknięcia odczytujemy z historii aktywności lub transakcji (pola do potwierdzenia na demo). Historia ma limit jednego dnia, więc pobieramy ją po każdej wykrytej zmianie i po zamknięciu sesji.

## 8. Wejście, wyjście, brak handlu

**Wejście.** Zlecenie rynkowe z atomowo dołączonym stopem (`stopDistance`), więc nie ma okna bez ochrony. Po potwierdzeniu silnik porównuje cenę wykonania z notowaniem z chwili decyzji (straż poślizgu, sekcja 5) i zapisuje obie. Rynek nie pozwala ograniczyć poślizgu zleceniem limitowym w tym wariancie, więc poślizg jest zjawiskiem mierzonym, nie ograniczanym.

**Wyjście silnika** (spłaszczenie 15:55, wyjątki, ręczne polecenie):

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
| Okno godzinowe | Poza oknem, święto, skrócona sesja, stan rynku niedostępny |
| Duplikat | Pozycja lub zlecenie na tym instrumencie już istnieje |
| Dane | Notowanie starsze niż 60 s lub brak ATR |
| Kill switch | Aktywny którykolwiek poziom |

**Wyjść nie blokujemy nigdy.** Licznik strat dziennych jest zapisany na dysku, więc restart go nie zeruje.

**Zabezpieczenie przed handlem na live.** Klucz Capital.com działa na demo i na live, więc nie polegamy na samej konfiguracji: adres hosta demo jest stałą w kodzie, adapter live nie istnieje, przy starcie silnik sprawdza, że odpowiedź konta pochodzi z hosta demo, a konto live nie ma środków do czasu bramki A. Klucz leży w pliku na serwerze, nigdy w środowisku deweloperskim ani w repo.

### Kill switch

| Poziom | Skutek | Wyzwalacze |
|---|---|---|
| Miękki | Brak nowych wejść; istniejące pozycje zarządzane normalnie | Plik `data/STOP`; dzienny limit strat; rozbieżność z brokerem > 60 s; cisza strumienia notowań > 60 s; nieudane logowanie lub wygaśnięcie sesji bez odnowienia; trzy odrzucenia z rzędu; nieznane odpowiedzi brokera ponad próg; stan rynku niedostępny w oknie handlu |
| Twardy | Zamknij pozycje procedurą z sekcji 8, potem poziom miękki | Plik `data/HARD_STOP`; strata dzienna ≥ 2 × limit; pozycja bez stopu dłużej niż 10 s po nieudanym zabezpieczeniu |

Stan kill switcha jest zapisywany w bazie i przeżywa restart. Zdjęcie poziomu wymaga jawnej akcji właściciela, nigdy automatycznie.

## 10. Kapitał, koszty i księga

**Wirtualny kapitał.** Konto demo Capital.com może mieć do 100 000 (doładowanie przez API), więc silnik sam egzekwuje `virtual_capital` jako twardy limit ryzyka i dźwigni. Księga „cień 20 USD" z v1 odpada: przy minimalnej wielkości pozycji na indeksie 20 USD jest zbyt małe, a pomiar tego, co zostałoby z takiego konta, wynika wprost z minimalnej wielkości zmierzonej na demo.

**Model kosztów (konserwatywny, zastępowany pomiarem po bramce A).** Wypełnienie z demo jest punktem wyjścia, do którego silnik dolicza: spread liczony z **własnego strumienia bid/ask w chwili decyzji**, a nie z założeń; poślizg 0,5 punktu na wejściu i dodatkowo 1,0 punktu przy zamknięciu stopem; finansowanie overnight (w v1 zero, bo pozycji nie trzymamy przez noc, ale pole w księdze zostaje); markup walutowy 0,4%, **jeśli** dotyczy wyniku z CFD przy różnicy waluty instrumentu i konta (pytanie 5); brak prowizji od transakcji na indeksach CFD. Wszystkie stawki są w konfiguracji, nie w kodzie.

**Księga.** Dla każdej transakcji: instrument, kierunek, wielkość, notowanie w chwili decyzji, cena wykonania wejścia i wyjścia, `gross_pnl`, `spread_cost`, `slippage_penalty`, `financing`, `fees`, `net_pnl`, czasy sygnału, wysyłki i potwierdzenia (UTC), opóźnienie sygnał → wykonanie, przyczyna wyjścia, waluta konta i instrumentu oraz kurs przeliczenia, jeśli broker go poda. Eksport pełnej księgi do CSV od pierwszego dnia, jako podstawa późniejszych rozliczeń dla rezydenta NL (przeliczenia i klasyfikacja podatkowa poza zakresem v1).

## 11. Bramki dowodowe i pre-rejestracja

**Ważne ograniczenie.** Strategia handluje raz dziennie na jednym instrumencie, czyli ok. 250 transakcji rocznie. Przy przewadze netto bliskiej zera próba rzędu tysięcy transakcji jest nieosiągalna w rozsądnym czasie (1000 transakcji to ok. 4 lata). Dlatego bramki nie mierzą przewagi, tylko jakość pomiaru i bezpieczeństwo.

Progi poniżej to **propozycje**, zakotwiczone w obliczeniach agentów badawczych [M] i do zamrożenia przed pierwszym dniem.

| Bramka | Warunek |
|---|---|
| **P (hydraulika, demo)** | Co najmniej 40 czystych sesji; zero trwałych rozbieżności pozycja/broker starszych niż 60 s; zero nieobsłużonych odrzuceń (odrzucenia obsłużone poprawnie, np. rynek niedostępny, nie liczą się jako błąd); kill switch przetestowany ćwiczeniem obu poziomów; restart w środku wejścia i wyjścia bez zdublowań; pamięć zmierzona pod limitem cgroup |
| **A (mikro-live, koszt)** | Bramka P zaliczona; pisemne odpowiedzi Capital.com na pytania z sekcji 16 (dozwolone API, podmiot, reguły instrumentu); decyzja właściciela o kwocie, której utrata jest akceptowalna; konsultacja z holenderskim doradcą podatkowym. Cel: co najmniej 20 transakcji na żywo do zmierzenia spreadu, poślizgu i zachowania stopów, **bez kryterium zysku**. Ta bramka może tylko odrzucić (np. koszty rzeczywiste znacznie wyższe od modelu), nie udowodnić przewagi |
| **B (skalowanie)** | Co najmniej 1000 transakcji i jednostronna dolna granica 95% (klastrowana po dniach) średniej netto > 0. Przy jednej transakcji dziennie to ok. 4 lata, więc w v1 **nie planujemy skalowania**; bramka istnieje, żeby nikt nie skalował kapitału bez dowodu |

Dodatkowe zasady statystyczne: t ≥ 2 tylko dla jednej wstępnie zarejestrowanej specyfikacji; przy ponad ok. 10 wariantach t ≥ 3 albo deflated Sharpe ≥ 0,95.

**Pre-rejestracja** (`docs/preregistration/v1.md`, powstaje w planie implementacji, **przed** pierwszym dniem na demo): skróty SHA-256 plików `config/strategy_v1.toml` i `config/risk_v1.toml`, **adapter, klasa instrumentu i model kosztów** w skrócie, liczba wariantów K, definicje metryk i bramek, data rozpoczęcia. Zmiana któregokolwiek elementu po tej dacie tworzy nowy wariant.

## 12. Testy

Pisane przed kodem (TDD) dla automatu stanów, uzgadniania i ryzyka.

- **Jednostkowe:** przejścia stanów, generowanie identyfikatorów, każda kontrola ryzyka, model kosztów, wielkość pozycji i zaokrąglanie do kroku, okna czasowe (święta, skrócone sesje, zmiana czasu).
- **Awarie na `FakeBroker`:** timeout wysyłki → `UNKNOWN` → wyjaśnienie przez listę pozycji; podwójne wejście zablokowane; odpowiedź brokera po stronie błędu; wygaśnięcie sesji (10 minut) i odnowienie; wyścig logowań (limit 1/s); pozycja zamknięta przez stop u brokera; pozycja obca; pozycja bez stopu; instrument niedostępny; cisza strumienia; odrzucenie wejścia z powodu minimalnej odległości stopu; restart procesu w środku wejścia i wyjścia (`kill -9`); luka większa niż stop.
- **Niezmienniki (sprawdzane po każdym teście awarii):** (1) żadne wejście nie wyszło bez wiersza INTENT; (2) żadna zmiana stanu pozycji bez odpowiedzi lub listy pozycji brokera; (3) dźwignia i ekspozycja ≤ limity; (4) licznik strat dziennych przeżywa restart; (5) wyjścia nigdy nie zablokowane; (6) co najwyżej jedna sesja brokera na raz.
- **Integracyjne na demo (`integration_demo`):** jedno wywołanie na typ operacji na koncie demo, uruchamiane na VPS z kluczem z pliku (nigdy z czatu ani repo). Rozstrzygają pytania z sekcji 16.
- **Odtwarzanie:** zapis surowych odpowiedzi z dnia na demo służy jako fixture regresyjny; jest uzupełnieniem, nie dowodem.

Przy każdej deklaracji ukończenia obowiązuje weryfikacja uruchomieniem testów, a nie opis.

### Gdzie biegną testy i CI

| Rodzaj testu | Wymaga | Gdzie biegnie |
|---|---|---|
| Jednostkowe, awarie na `FakeBroker`, niezmienniki | Tylko Python, bez sieci i bez kluczy | Sesja deweloperska, VPS, CI |
| Budowa obrazu Dockera i testy logiki uruchamiane w obrazie | Demon Dockera | CI i VPS |
| Integracyjne na demo (`integration_demo`) | Klucz demo i dostęp do Capital.com | **Tylko VPS** |
| Pełne sesje demo (kryteria 2–3, bramka P) | Klucz, sieć, ciągła praca | **Tylko VPS** |

CI to GitHub Actions (`.github/workflows/ci.yml`):

- Wyzwalacze: `push`, `pull_request` i ręczne uruchomienie (`workflow_dispatch`).
- Zadanie `tests`: macierz Python 3.12 i 3.13, instalacja z przypiętych wersji, `pytest -m "not integration_demo"`. Testy `integration_demo` mają znacznik i **nigdy nie biegną w CI**.
- Zadanie `image`: budowa obrazu `python:3.12-slim` i uruchomienie w nim testów logiki.
- **CI nie dostaje żadnych sekretów brokera ani dostępu do VPS.** Wdrożenie na VPS jest ręczne; automatyczny deploy z CI jest poza zakresem v1.
- Higiena łańcucha dostaw: `permissions: contents: read`, akcje zewnętrzne przypięte do pełnego SHA commita.
- Zielony CI na `main` jest warunkiem uznania zmiany za ukończoną i niezależnym potwierdzeniem testów uruchomionych lokalnie; nie zastępuje ich.
- Wypchnięcie pliku workflow może wymagać dodatkowego uprawnienia integracji GitHub (pytanie 17). W razie odmowy właściciel dodaje plik przez interfejs GitHub.

## 13. Wdrożenie i operacje

- Jeden kontener `python:3.12-slim`, użytkownik bez uprawnień, system plików tylko do odczytu z tmpfs i wolumenem `/data` (SQLite).
- **Limit pamięci:** `mem_limit` 256 MB, `memswap_limit` równy `mem_limit` (kontener bez swapu), niższe `mem_reservation`, `pids_limit`, **`oom_score_adj` +1000**, żeby przy ogólnym braku pamięci jądro zabijało nasz proces w pierwszej kolejności, a nie bota z prawdziwymi pieniędzmi. Zabicie procesu nie spłaszcza pozycji, bo stop jest u brokera; po restarcie obowiązuje uzgadnianie. Obsługę pól w Compose v5.6.0 i rachunkowość swapu w jądrze VPS sprawdzamy w pierwszym zadaniu planu, zanim cokolwiek trafi na serwer. Cel zużycia ok. 100 MB jest założeniem, mierzonym w kryteriach akceptacji.
- **Nic nie wchodzi na współdzielony VPS bez pomiaru pamięci i zgody właściciela.** Rozwój i testy logiki odbywają się poza nim (sesja deweloperska, CI).
- Klucz, identyfikator i hasło API w pliku zamontowanym tylko do odczytu (`/secrets/capitalcom_demo.json`). Tokeny sesji (`CST`, `X-SECURITY-TOKEN`) żyją w pamięci i nie wymagają zapisu na dysk.
- **Brak ścieżki live w kodzie** (sekcja 9).
- **Utrzymanie sesji:** ping co 5 minut, ponowne logowanie przy odpowiedzi 401, logowania serializowane (limit 1/s). Nie przełączamy kont przez `PUT /session`, bo zatrzymuje to strumień. Plik blokady w `/data` i procedura restartu „zatrzymaj, potem uruchom", żeby nie dublować sesji.
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
tests/                        unit, fault, integration_demo
config/                       strategy_v1.toml, risk_v1.toml
docs/                         research, superpowers/specs, superpowers/plans, preregistration, ops
docker/                       Dockerfile, compose.yml
```

Zabezpieczenia pracy z Claude Code (od najmocniejszej): klucze i dostęp do środowiska produkcyjnego poza zasięgiem agenta; reguły `deny`; hooki `PreToolUse` z `onFailure: "block"` (hooki domyślnie przepuszczają przy błędzie) blokujące zmiany hosta brokera; piaskownica; subagenci recenzujący tylko do odczytu. Wzorce Bash w regułach nie są granicą bezpieczeństwa. CLAUDE.md jest tylko wskazówką.

Narzędzia zewnętrzne: oficjalny MCP Capital.com (repozytorium organizacji Capital.com) jako referencja do przeczytania, nie zależność wykonania (README zastrzega, że API może się zmienić lub zostać wyłączone bez uprzedzenia); Day Trader Skill i `tradermonty` jako referencje.

## 15. Odroczone eksperymenty (każdy zwiększa K)

DE40 (godziny dzienne czasu środkowoeuropejskiego, spread ok. 2,0 pkt) jako drugi instrument; US500; time-stop i wyjście po utracie momentum (parametry 180 s / +0,4% nie mają poparcia w badaniach); akcje USA przez CFD z filtrem RVOL i wolumenem z zewnętrznego źródła (w tym konto paper-only dostawcy danych, niezbadane); prawdziwe akcje przez inną trasę (IBKR przy 500–1000 USD, wymaga potwierdzenia OAuth 1.0a); adapter cTrader po zatwierdzeniu aplikacji; drugi, osobny host; adapter Telegram.

## 16. Pytania otwarte i sposób rozstrzygnięcia

Żadne nie blokuje pisania silnika na `FakeBroker`. Pytania 1, 6 i 11 blokują bramkę A. Pytania 2–5, 7–10 i 12 rozstrzygamy na demo lub odczytem dokumentacji przed pierwszym dniem pomiarowym. Pytania 13–16 są decyzjami lub pomiarami po bramce P.

| # | Pytanie | Jak rozstrzygnąć |
|---|---|---|
| 1 | Capital.com: czy handel przez publiczne API przez nienadzorowany program jest dozwolony wobec pkt 8.12.3(b) i 8.24.8 T&C v2.1 (także na demo) i który punkt to dopuszcza; który podmiot obsługuje rezydenta NL; dostępność GSL; opłata za nieaktywność | E-mail do supportu (`docs/ops/capital-com-support-email.md`) |
| 2 | Czy klucz API dla demo powstaje bez zakończonej weryfikacji KYC | Test na koncie (właściciel) |
| 3 | US100: minimalna wielkość, krok, wartość punktu, depozyt, minimalna odległość stopu, godziny i przerwy handlu, nazwa instrumentu w API | `GET /markets/{instrument}` na demo |
| 4 | Tryb konta (agregacja czy hedging) i zachowanie drugiego wejścia na tym samym instrumencie | Test na demo |
| 5 | Czy markup walutowy 0,4% dotyczy wyniku CFD; waluta konta; czas naliczania finansowania a DST | E-mail do supportu, pomiar na demo |
| 6 | Konsultacja z holenderskim doradcą podatkowym: klasyfikacja wyników z CFD i częstego handlu automatycznego, obowiązki sprawozdawcze dla zagranicznego brokera | Doradca w NL (research ogólny w toku) |
| 7 | Endpoint i pola potwierdzenia wykonania, historii aktywności i transakcji (cena wyjścia, przyczyna zamknięcia przez stop) | Odczyt dokumentacji i test na demo |
| 8 | Format i dostępność historii świec 5-minutowych i dziennych przez REST; opóźnienie świec OHLC ze strumienia | Test na demo |
| 9 | Pomiar pamięci procesu pod limitem cgroup oraz rachunkowość swapu (`--memory-swap`) w jądrze VPS | Pomiar na VPS po zgodzie właściciela |
| 10 | Czy zaokrąglenie czasu wejścia o 09:35 ET i zachowanie stopów przy luce są zgodne z modelem | Dziennik z kilku sesji demo |
| 11 | Dostęp z Polski podczas czasowego pobytu (pkt 3.6.2) i czy demo wygasa | E-mail do supportu oraz próbne logowanie do demo z Polski |
| 12 | Czy `GET /prices` zwraca dane potrzebne do ATR i świec zapasowych bez limitów utrudniających | Test na demo |
| 13 | Faktyczna różnica spreadu i poślizgu demo względem live | Mikro-live po bramce A; demo sprawdza hydraulikę |
| 14 | Źródło danych do rozszerzeń o akcje (jeśli kiedyś) | Decyzja właściciela po zmierzeniu kosztów |
| 15 | Rachunek na koncie live: waluta bazowa i przewalutowania | E-mail, decyzja właściciela |
| 16 | Czy hosting na współdzielonym VPS wystarczy, czy potrzebny osobny host | Pomiar pamięci (pytanie 9) i decyzja właściciela |
| 17 | Czy integracja GitHub może wypychać pliki `.github/workflows` | Próba przy implementacji; w razie odmowy właściciel dodaje plik |
| 18 | Plan B, jeśli Capital.com odmówi lub odpowie niejednoznacznie: trasy, które regulamin wprost dopuszcza lub które mają jawne API dla klientów detalicznych (MetaTrader u Capital.com, cTrader u brokerów UE, Saxo OpenAPI, LYNX/IBKR) i ich ograniczenia (pamięć VPS, minimalny kapitał) | Decyzja właściciela po odpowiedzi supportu; adaptery są wymienne dzięki interfejsowi `Broker` |

## 17. Kryteria akceptacji v1

1. Wszystkie testy jednostkowe i awaryjne z sekcji 12 przechodzą (lokalnie i w CI, na Pythonie 3.12 i 3.13 oraz w obrazie Dockera); niezmienniki spełnione po każdym teście awarii; kontrakt adaptera przechodzi na `FakeBroker` i na demo.
2. Pełna sesja na demo na VPS bez ingerencji: logowanie → zakres otwarcia → sygnał → wejście ze stopem → nadzór → spłaszczenie → raport.
3. Zabicie procesu w środku wejścia i wyjścia oraz zabicie przez OOM: po restarcie brak zdublowań i pozycji osieroconych, potwierdzone testem i na demo.
4. Ćwiczenia kill switcha: oba poziomy przez pliki, cisza strumienia, wygaśnięcie sesji i rozbieżność z brokerem zasymulowane.
5. Raport dzienny z wynikiem brutto i netto, kosztami, licznikami błędów wykonania i rozbieżności.
6. Zużycie pamięci zmierzone pod limitem cgroup i opisane.
7. W kodzie nie istnieje ścieżka live, a host demo jest stałą.

## 18. Główne ryzyka projektu

| Ryzyko | Skutek | Łagodzenie |
|---|---|---|
| Strategia nie ma przewagi netto | Wynik bliski zera lub ujemny | To spodziewany wynik; bramki mierzą jakość pomiaru; silnik jako instrument pomiarowy |
| Regulamin Capital.com dla NL (T&C v2.1): algorytmy tylko „gdy wyraźnie dozwolone", w tekście brak słowa „API" | Zawieszenie dostępu API lub konta, trudna droga odwoławcza (Cypr) | Pisemne potwierdzenie przed jakimikolwiek środkami na live i przed dłuższym używaniem demo; plan B (pytanie 18); demo bez środków |
| Odwołanie lub zmiana API bez uprzedzenia, zmiana podmiotu (FxPro) | Silnik przestaje działać | Interfejs `Broker` z deklaracją zdolności; adapter wymienny; kill switch miękki |
| Ten sam klucz na demo i live | Przypadkowy handel na live | Stała hosta demo, brak adaptera live, konto live bez środków |
| Błąd w automacie stanów lub uzgadnianiu | Duplikaty, pozycje osierocone | TDD, `FakeBroker` z awariami, niezmienniki, recenzja `order-state-auditor` |
| OOM na współdzielonym VPS | Zabity proces, ryzyko dla bota z prawdziwymi pieniędzmi | Limit cgroup i `oom_score_adj` +1000, stop u brokera, pomiar przed wdrożeniem |
| Demo bez poślizgu | Zawyżony wynik | Własny model kosztów; mikro-live do pomiaru |
| Próba zbyt mała do wnioskowania | Fałszywe poczucie przewagi | Bramki bez kryterium zysku; skalowanie poza zakresem v1 |
| Błędne założenia o rezydencji i podatkach | Złe rozliczenie | Doradca podatkowy w NL przed bramką A; eksport pełnej księgi |
| Publiczne repo | Ujawnienie treści | Brak sekretów w repo; zmiana na prywatne zaplanowana przez właściciela |
