# Silnik tradingowy na akcje USA: projekt v1

Data: 2026-10-31
Status: do przeglądu właściciela (projekt zatwierdzony w rozmowie, specyfikacja jeszcze nie)
Zakres: wariant A (paper trading z twardą bramką dowodową przed jakimikolwiek prawdziwymi pieniędzmi)
Podstawa: `docs/research/2026-10-31-silnik-tradingowy-akcje-usa.md` (znaczniki [H]/[M]/[L] to pewność z tamtego raportu)

## 1. Cel i kryterium sukcesu

Autonomiczny silnik (bez zatwierdzania każdej transakcji) działający 24/7 w jednym kontenerze Docker na małym VPS (3,6 GB RAM), który:

1. skanuje akcje USA, wykrywa wybicia przy podwyższonym wolumenie, wchodzi i wychodzi według zamrożonej specyfikacji,
2. **za każdym razem potwierdza stan u brokera**, zanim zmieni własny,
3. egzekwuje twarde limity ryzyka i kill switch,
4. mierzy **zysk netto po realistycznych kosztach** i raportuje go uczciwie.

**Sukces v1 to nie zysk.** Sukces v1 to: silnik przechodzi bramkę P (hydraulika, sekcja 11) i dostarcza zalogowane dane potrzebne do podjęcia decyzji o bramkach A i B. Raport researchu nie znalazł niezależnego dowodu przewagi netto tej klasy strategii [M], więc rozsądnym założeniem roboczym jest oczekiwanie netto zerowe lub ujemne.

Zasady przejęte z `gw-solana-sniper` (`docs/GW_MASTER_HANDOFF_2026-10-23.md`), obowiązują także tutaj:

- liczy się zrealizowany zysk netto po kosztach, a nie „testy przeszły";
- pozycja jest `CLOSED` dopiero po potwierdzeniu u brokera;
- nieudane wyjście **nigdy** nie znika z nadzoru;
- dzienny budżet ryzyka liczony według czasu zamknięcia i z realnymi kosztami;
- jedna baza kodu, jedna bramka ryzyka, jeden dziennik, jedna księga.

### Poza zakresem v1

Prawdziwe pieniądze i jakakolwiek ścieżka kodu live; krótka sprzedaż, dźwignia, opcje, ułamki akcji; LLM w jakiejkolwiek roli; ML; panel WWW; wiele strategii naraz; rozliczenia podatkowe (poza eksportem fills+fees). Eksperymenty z odroczonymi pomysłami opisuje sekcja 15.

## 2. Fakty, na których stoi projekt

| Fakt | Konsekwencja w projekcie | Pewność |
|---|---|---|
| Brak niezależnego dowodu dodatniej przewagi netto ORB / „stocks in play" po kosztach | Silnik jest instrumentem pomiarowym; bramki dowodowe; brak obietnic | M |
| Zlecenia ułamkowe tylko `DAY`; bracket/OCO/OTO dla ułamków niepotwierdzone | v1 handluje wyłącznie pełnymi akcjami, żeby stop leżał u brokera | H / niepotwierdzone |
| Konta Alpaca to konta margin; poniżej 2 000 USD dźwignia 1x; reguły GFV poniżej 2 000 USD nieznane | Sizing z `buying_power` i wirtualnego limitu; pytanie do supportu przed bramką A | H / niepotwierdzone |
| Paper: wypełnienia po NBBO, bez poślizgu, wpływu na rynek i opłat regulacyjnych; w ok. 10% losowe wypełnienie częściowe | Własny model kosztów; wynik paper to górna granica; losowe częściowe wypełnienia służą jako fixture | H / M |
| Basic: strumień IEX tylko w czasie rzeczywistym, do 30 symboli; SIP dopiero po 15 minutach | Ranking uniwersum z opóźnionego SIP, wejścia na ≤30 symbolach z IEX | H |
| Jedno połączenie strumienia na endpoint (drugie dostaje 406) | Blokada instancji, brak nakładających się restartów | H |
| `trade_updates` bez gwarancji powtórek i kolejności; Alpaca ponownie wysyła `new` po reconnect | Odzyskiwanie przez REST, deduplikacja po identyfikatorze wykonania | M |
| Zlecenia na wstrzymanej spółce są przyjmowane i czekają do wznowienia | Brak nowych wejść w halcie, pozycje pod nadzorem | M |
| Platforma miała awarie, w tym wyłączenie handlu ułamkowego i notional (15 i 21.10.2026) | Pełne akcje i stop u brokera są odporniejsze na awarie niż ułamki | H |

## 3. Architektura

Jeden proces asyncio, jeden plik SQLite (WAL). Moduły mają jedno zadanie i komunikują się przez wąskie interfejsy.

```
            ┌────────────┐   ┌────────────┐
 dane  ───► │  data      │──►│ strategy   │ czyste funkcje: dane → sygnał
            └────────────┘   └─────┬──────┘
                                   ▼
                            ┌────────────┐   PASS/BLOCK
                            │   risk     │◄── kill switch
                            └─────┬──────┘
                                  ▼
 ┌─────────┐   ┌────────────┐  ┌────────────┐   ┌──────────────┐
 │ journal │◄─►│  orders    │─►│ execution  │──►│   broker     │──► Alpaca paper
 │ SQLite  │   │ (FSM)      │  │ entry/exit │   │ (interfejs)  │    REST + trade_updates
 └────▲────┘   └─────▲──────┘  └────────────┘   └──────┬───────┘
      │              │                                  │ ten sam interfejs:
      │        ┌─────┴──────┐   ┌────────────┐          ▼
      └────────│ reconciler │   │ watchdog   │      FakeBroker (testy, awarie)
               └────────────┘   └────────────┘
                 report / costs czytają dziennik
```

| Moduł | Odpowiada za | Nigdy nie robi |
|---|---|---|
| `broker` | Jedyna rozmowa z Alpaca (REST, `trade_updates`) za interfejsem `Broker`; `FakeBroker` ma ten sam interfejs | Nie podejmuje decyzji handlowych |
| `journal` | Surowy dziennik zdarzeń, zlecenia, wypełnienia, pozycje, stan ryzyka i kill switcha | Nie zawiera logiki strategii |
| `orders` | Automat stanów zlecenia i pozycji, generowanie `client_order_id` | Nie wysyła nic bez wiersza INTENT w dzienniku |
| `reconcile` | Uzgadnianie dziennika z brokerem (źródło prawdy: broker) | Nie otwiera nowych pozycji |
| `risk` | Bramka PASS/BLOCK, kill switch | Nie blokuje wyjść |
| `strategy` | Dane wejściowe → sygnał | Nie ma dostępu do brokera ani dziennika |
| `data` | Dane z Alpaca, halty, EDGAR, kalendarz | Nie interpretuje sygnałów |
| `execution` | Procedury wejścia, wyjścia i awaryjnego spłaszczenia | Nie zmienia stanu pozycji bez zdarzenia od brokera |
| `costs` | Konserwatywny model kosztów | Nie zmienia wypełnień w dzienniku, dopisuje kolumny kosztowe |
| `watchdog` | Cisza strumieni, tętno, wyzwalacze kill switcha | Nie handluje |
| `report` | Zysk brutto i netto, wskaźniki bramek, eksport fills+fees CSV | Nie modyfikuje danych |

Zależności zewnętrzne ograniczone do `alpaca-py` (przypięta wersja), `tzdata` (jeśli obraz nie zawiera bazy stref czasowych) i narzędzi deweloperskich (`pytest`). Dane o haltach i zgłoszeniach EDGAR pobieramy biblioteką standardową (`urllib` w wątku roboczym), a bazę obsługuje standardowy `sqlite3` z jednym wątkiem zapisu. Każda dodatkowa zależność wymaga uzasadnienia w opisie zmiany.

## 4. Dzień pracy silnika (czas `America/New_York`)

Wszystkie decyzje czasowe opierają się na kalendarzu i zegarze Alpaca oraz `zoneinfo`, **bez stałych przesunięć**. W bazie czas zapisywany jest w UTC. Test regresyjny obejmuje zmianę czasu w USA 1.11.2026 (sesja w Warszawie przesuwa się wtedy z 14:30 na 15:30).

| Czas ET | Faza | Działanie |
|---|---|---|
| przed 09:25 | Przygotowanie | Rekoncyliacja startowa; pobranie dziennych świec i wolumenów; filtr uniwersum; wykluczenia (halty, EDGAR) |
| 09:30–09:35 | Zakres otwarcia | Zbieranie pierwszej 5-minutowej świecy (z IEX do podglądu) |
| ok. 09:50 | Ranking | Opóźniony SIP udostępnia świecę 09:30–09:35; liczenie RVOL; lista ≤10 symboli; subskrypcja IEX |
| 09:50–11:30 | Okno wejść | Wyzwalacze, bramka ryzyka, wejścia |
| do 15:55 | Zarządzanie | Stopy u brokera, nadzór, wyjścia silnika |
| 15:55 | Spłaszczenie | Procedura wyjścia dla wszystkich pozycji |
| po 16:00 | Zamknięcie | Rekoncyliacja końcowa, raport dzienny |

Poza sesją silnik utrzymuje tętno i pętlę uzgadniania, ale nie handluje.

## 5. Strategia v1 (jedna zamrożona specyfikacja)

Nazwa: `orb_rvol_long`. To **wariant** opublikowanej strategii, a nie jej odtworzenie: filtr RVOL liczymy z opóźnionego SIP, a wejścia z IEX dla ≤30 symboli. Dodatkowo handlujemy wyłącznie długo i bez dźwigni, a pozycje sizujemy ryzykiem (sekcja 9). Wyniku z artykułu (1637%, Sharpe 2,81, [M] backtest bez poślizgu) nie traktujemy jako oczekiwania.

Reguły:

1. Uniwersum: akcje zwykłe USA spełniające filtry z tabeli, bez symboli wstrzymanych w handlu i bez świeżych zgłoszeń rozwadniających (EDGAR).
2. RVOL = wolumen 09:30–09:35 podzielony przez średnią wolumenu tego samego okna z 14 poprzednich sesji. Na listę trafia 10 symboli o najwyższym RVOL spośród tych z RVOL ≥ próg.
3. Kierunek: tylko long, jeśli pierwsza 5-minutowa świeca jest wzrostowa (zamknięcie > otwarcie).
4. Wyzwalacz: ostatnia cena z IEX powyżej maksimum zakresu otwarcia plus 1 tick, w oknie wejść. Maksimum zakresu bierzemy z tej samej opóźnionej świecy SIP 09:30–09:35, z której liczymy RVOL (jedno źródło dla zakresu i wolumenu); IEX służy wyłącznie do ceny bieżącej.
5. Stop: odległość `0,10 × ATR14` od ceny wejścia (zgodnie z opublikowanym wariantem); sygnał odrzucany, gdy odległość stopu < 3 × bieżący spread (stop byłby szumem).
6. Brak celu zysku; wyjście: stop u brokera albo spłaszczenie o 15:55 ET.
7. Wielkość: `shares = floor(min(max_loss_per_trade / stop_distance, max_notional / entry_price))`; wymagane ≥ 1.

### Wartości startowe

Są to **założenia projektowe, nie wyniki badań**. Przed pierwszym dniem na papierze zamrażamy je skrótem w pre-rejestracji (sekcja 11). Późniejsza zmiana tworzy nowy wariant i zwiększa liczbę przetestowanych wariantów K.

| Parametr | Wartość startowa |
|---|---|
| `virtual_capital_usd` (księga główna) | 1000 |
| `shadow_capital_usd` (księga cień, liczona nie wykonywana) | 20 |
| Cena akcji | 10 – 300 USD |
| Średni dzienny wolumen (20 sesji) | ≥ 1 000 000 akcji |
| ATR14 dzienny | ≥ 2,0 USD |
| Próg RVOL | ≥ 2,0 |
| Rozmiar listy | 10 symboli |
| Okno wejść | 09:50 – 11:30 ET |
| Spłaszczenie | 15:55 ET |
| Wykluczenie po zgłoszeniu rozwadniającym | 3 sesje |
| `max_loss_per_trade_usd` | 5 |
| `max_notional_per_position_pct` | 33 |
| `max_open_positions` | 3 |
| `max_entries_per_day` | 6 |
| `max_daily_loss_usd` | 15 |
| Limit wejścia | ask + 0,10 × odległość stopu; anulowanie po 5 s |
| Czas oczekiwania na wypełnienie wyjścia przed zleceniem rynkowym | 2 s |
| Cisza strumienia danych (kill switch miękki) | 60 s |
| Tolerancja rozbieżności z brokerem | 60 s |
| Cykl rekoncyliacji w sesji | 10 s |

Uzasadnienie progu ATR: według obliczeń agentów badawczych średnia przewaga rzędu 0,08 R to ok. 0,8 centa na akcję przy ATR 1 USD, a 1 cent półspreadu na wejściu plus stop zjada ją dla ATR poniżej ok. 2,5 USD [M, obliczenie]. Wartość 2,0 USD jest kompromisem między próbą a kosztem i pozostaje do zamrożenia lub zmiany przed pre-rejestracją.

## 6. Zlecenia: automat stanów

### Zlecenie

| Stan | Znaczenie | Wyjścia |
|---|---|---|
| `INTENT` | Zapisane w dzienniku z `synchronous=FULL`, nic nie wysłano | → `SUBMITTING` |
| `SUBMITTING` | Wysyłka w toku | → `WORKING`, `REJECTED`, `UNKNOWN` (timeout) |
| `UNKNOWN` | Nie wiadomo, czy broker przyjął | → przez zapytanie po `client_order_id` do `WORKING`, `FILLED`, `CANCELED`, `REJECTED`, `NOT_PLACED` |
| `WORKING` | Przyjęte (`new`, `accepted`, `pending_new`, `held`) | → `PARTIAL`, `FILLED`, `CANCELING`, `EXPIRED`, `REJECTED` |
| `PARTIAL` | Część wypełniona, reszta czeka | → `FILLED`, `CANCELING`, `EXPIRED` |
| `CANCELING` | Anulowanie wysłane; wypełnienia nadal możliwe | → `CANCELED` (z ewentualnym skumulowanym wypełnieniem), `FILLED` |
| `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`, `NOT_PLACED` | Stany końcowe | – |

Reguły:

- `client_order_id` deterministyczny i unikalny: `gws-{YYYYMMDD}-{symbol}-{seq}-{rola}` (≤ 128 znaków, `seq` rośnie monotonicznie w dzienniku). Identyfikator nigdy nie jest używany ponownie, także po `NOT_PLACED`.
- **Timeout to `UNKNOWN`, nigdy „nieudane".** Wyjście z `UNKNOWN` tylko przez zapytanie po `client_order_id`. Trzy nieudane zapytania w ciągu 30 s po upływie okna wysyłki daje `NOT_PLACED`. Jeśli zlecenie pojawi się później, jest zdarzeniem nieoczekiwanym: anulujemy je i zgłaszamy.
- Ilość wypełniona pochodzi od brokera (skumulowana). Wiersz wypełnienia powstaje po identyfikatorze wykonania, a gdy go brak, po kluczu (zlecenie, skumulowana ilość). Duplikaty i spóźnione zdarzenia są idempotentne.
- Nieznany typ zdarzenia (np. `done_for_day`, `restated`) trafia do dziennika, nie zmienia stanu i zwiększa licznik alarmowy. Typy zdarzeń obsługujemy jako napisy, bo enum SDK jest niepełny.
- Każde surowe zdarzenie jest zapisywane **przed** przetworzeniem, bo SDK połyka wyjątki handlerów.

### Pozycja

`FLAT` → `ENTERING` (zlecenie wejścia aktywne) → `OPEN` (ilość dodatnia potwierdzona wypełnieniem) → `EXITING` → `FLAT`, plus `BLOCKED` (rozbieżność z brokerem: tylko decyzja rekoncyliacji lub właściciela). Ilość pozycji jest wyprowadzana z wypełnień i porównywana z pozycją brokera. Pozycja nigdy nie przechodzi do `FLAT` na podstawie samego wysłania zlecenia.

## 7. Uzgadnianie z brokerem

Broker jest źródłem prawdy. Uzgadnianie uruchamia się: po starcie (przed jakimkolwiek wejściem), po każdym ponownym połączeniu strumienia, co 10 s w sesji oraz po zamknięciu sesji.

Procedura: pobierz otwarte zlecenia, pozycje i wypełnienia od ostatniego znacznika (REST, w tym `/v2/account/activities`); porównaj z dziennikiem; dla każdej rozbieżności zastosuj regułę:

| Rozbieżność | Reakcja |
|---|---|
| Zlecenie `WORKING` lokalnie, a u brokera końcowe | Dopisz brakujące zdarzenia z obiektu zlecenia, zastosuj stan końcowy |
| Zlecenie `UNKNOWN` | Zapytanie po `client_order_id`, mapowanie stanu albo `NOT_PLACED` (sekcja 6) |
| U brokera zlecenie z naszym prefiksem, którego nie ma w dzienniku | Zapisz jako `UNKNOWN`, anuluj, zgłoś |
| Pozycja u brokera, której nie mamy, z naszym prefiksem w historii zleceń | Przyjmij, dołącz zarządzanie wyjściem i stopem |
| Pozycja u brokera bez śladu w naszych zleceniach | `BLOCKED`, kill switch miękki, alarm; **nie dotykamy** (może być ręczna) |
| Pozycja lokalna, której nie ma u brokera | Szukaj wypełnienia wyjścia; jeśli jest, zaksięguj; jeśli brak, `BLOCKED` i alarm |
| Różnica ilości | `BLOCKED`, alarm |

Nierozstrzygnięta rozbieżność trwająca dłużej niż 60 s włącza kill switch miękki. Po ponownym połączeniu zdarzenie `new` dla otwartych zleceń traktujemy jako powtórkę.

## 8. Wejście, wyjście, halty

**Wejście.** Zlecenie typu OTO: limit z ograniczonym poślizgiem plus noga stop-loss po stronie brokera, tak aby ochrona przetrwała awarię VPS. Jeśli broker odrzuci OTO, wariant zapasowy to osobne zlecenie stop wysyłane natychmiast po pierwszym wypełnieniu; pozycja bez stopu dłużej niż 10 s uruchamia procedurę zabezpieczenia, a gdy ta zawiedzie, kill switch twardy. Zlecenie wejścia nieprzyjęte w 5 s jest anulowane, bez pogoni za ceną.

**Wyjście silnika** (spłaszczenie 15:55, wyjątki, ręczne polecenie):

1. Pozycja → `EXITING`.
2. Anuluj nogę stop, czekaj na potwierdzenie anulowania do 3 s.
3. Po potwierdzeniu: limit sprzedaży po bid minus poślizg, czekaj 2 s.
4. Bez wypełnienia: anuluj i wyślij zlecenie rynkowe zamykające pozycję.
5. W każdym kroku, gdzie wynik jest niejasny (timeout, odmowa), **uzgadniaj, nie wysyłaj ponownie na ślepo**.
6. Jeśli anulowanie stopu zostało odrzucone, bo stop już się wypełnił: zaksięguj zamknięcie przez stop.

Przeciwne zlecenia przy aktywnej nodze stop mogą dać odmowę (według notatek 403), a bracket bywa wypełniany podwójnie w szybkim rynku; stąd kolejność anuluj → potwierdź → sprzedaj oraz uzgadnianie po każdym zdarzeniu stopu.

**Awaryjne spłaszczenie** to ta sama procedura ze skróconymi limitami czasu, uruchamiana przez kill switch twardy.

**Halty.** Symbol z aktywnym haltem: brak nowych wejść. Otwarta pozycja: nie zalewamy zleceniami (zlecenia w halcie są przyjmowane i czekają), noga stop zostaje; po wznowieniu uzgadniamy i wykonujemy procedurę. Pozycja w halcie o 15:55 jest wyjątkiem raportowanym (ekspozycja przez noc), a uzgadnianie startowe następnego dnia ją przejmuje. Źródła halt: RSS Nasdaq Trader (co 60 s), CSV NYSE; na Basic nie zakładamy strumienia `statuses`/`lulds` (produkcyjny dostęp wymaga kontaktu ze sprzedażą [M]).

## 9. Silnik ryzyka i kill switch

Bramka ryzyka jest wywoływana synchronicznie na ścieżce każdego zlecenia wejścia. Specyfikacja kontroli pochodzi z `trader-risk` (MIT, `Jakeschincariol/day-trader-skill`, commit `3d0f4ef`), zaadaptowana do autonomicznego silnika.

| Kontrola | Blokuje wejście, gdy |
|---|---|
| Format | Brak symbolu, ilości ≤ 0, brak limitu albo nogi stop |
| Stop obowiązkowy | Brak stopu, stop po złej stronie |
| Strata na transakcję | `shares × stop_distance` > `max_loss_per_trade_usd` |
| Wirtualny kapitał | Ekspozycja po transakcji > `virtual_capital_usd` lub notional > `max_notional_per_position_pct` |
| Pozycje | Otwartych + oczekujących wejść ≥ `max_open_positions` |
| Dzienny limit strat | Strata zrealizowana (według czasu zamknięcia, netto) + ryzyko otwartych ≥ `max_daily_loss_usd` |
| Liczba wejść | `max_entries_per_day` wykorzystane |
| Okno godzinowe | Poza oknem wejść, poza sesją, święto, skrócona sesja |
| Duplikaty | Ten sam symbol już w pozycji lub z aktywnym zleceniem |
| Siła nabywcza | `buying_power` brokera mniejsza niż notional |
| Kill switch | Aktywny którykolwiek poziom |

**Wyjść nie blokujemy nigdy.** Licznik strat dziennych jest zapisywany na dysku, więc restart go nie zeruje.

### Kill switch

| Poziom | Skutek | Wyzwalacze |
|---|---|---|
| Miękki | Brak nowych wejść; istniejące pozycje zarządzane normalnie | Plik `data/STOP`; dzienny limit strat; rozbieżność z brokerem > 60 s; cisza strumienia danych > 60 s; rozłączenie `trade_updates` > 30 s; 3 odrzucone/nieudane zlecenia z rzędu; licznik nieznanych zdarzeń |
| Twardy | Anuluj zlecenia wejścia, spłaszcz wszystkie pozycje procedurą z sekcji 8, potem poziom miękki | Plik `data/HARD_STOP`; strata dzienna ≥ 2 × limit; pozycja bez stopu dłużej niż 10 s po nieudanym zabezpieczeniu |

Stan kill switcha jest zapisywany w bazie i przeżywa restart. Zdjęcie poziomu wymaga jawnej akcji właściciela (usunięcie pliku i polecenie CLI), nigdy automatycznie.

## 10. Kapitał, koszty i księga

**Wirtualny kapitał.** Konto paper ma 100 tys. USD i, według jednych źródeł, nie da się zmienić jego kapitału (dokumentacja jest sprzeczna), więc silnik sam egzekwuje `virtual_capital_usd` jako twardy limit. Księga główna (1000 USD, pełne akcje, stop u brokera) daje realizm kosztów. Księga cień 20 USD jest tylko liczona z tych samych sygnałów i pokazuje, co zostałoby z konta 20 USD; nie wysyła zleceń. Definicja: dla każdej transakcji księgi głównej cień otwiera pozycję ułamkową o wartości `shadow_capital_usd × max_notional_per_position_pct / 100` (6,60 USD przy wartościach startowych), po tych samych cenach wejścia i wyjścia i w tych samych chwilach, a koszty liczy tym samym modelem z zaokrągleniem do centa na wypełnienie. Cień nie uwzględnia ograniczeń ułamków w Alpaca (brak stopu u brokera), więc jest górną granicą wyniku konta 20 USD. Uzasadnienie: na 20 USD nie da się mieć stopu u brokera (ułamki) ani realizmu kosztów, ten test sprawdza tylko hydraulikę.

**Model kosztów (konserwatywny, zastępowany pomiarem po bramce A).** Wypełnienie z paper to punkt wyjścia, do którego silnik dolicza: poślizg 0,01 USD na akcję na każdej stronie oraz dodatkowo 0,02 USD na akcję przy wyjściu stopem; opłaty regulacyjne (opłata SEC od wartości sprzedaży, opłata CAT na akcję, TAF z konfiguracji), każda zaokrąglana w górę do centa **na każde wypełnienie** (najgorsza z dwóch interpretacji w dokumentacji). Stawki w konfiguracji, nie w kodzie, bo się zmieniają (np. przerwa TAF do 2026-12-31).

**Księga.** Dla każdej transakcji: `gross_pnl`, `fees`, `slippage_penalty`, `net_pnl`, czasy sygnału, wysyłki, wypełnienia (UTC), opóźnienie sygnał → wypełnienie, powód wyjścia. Eksport `fills+fees` do CSV od pierwszego dnia (podstawa do późniejszych rozliczeń; przeliczenie na PLN poza zakresem v1).

## 11. Bramki dowodowe i pre-rejestracja

Progi poniżej to **propozycje agentów badawczych [M]**, zakotwiczone w ich obliczeniach, a nie wartości ze źródeł. Zamrażamy je przed pierwszym dniem.

| Bramka | Warunek |
|---|---|
| **P (hydraulika)** | Co najmniej 40 czystych dni sesyjnych; zero trwałych rozbieżności zlecenie/pozycja starszych niż 60 s; najwyżej 0,5% zleceń nieudanych lub odrzuconych; kill switch przetestowany ćwiczeniem; model kosztów konserwatywny |
| **A (mikro-live, dopiero po odpowiedzi Alpaca)** | Co najmniej 150–200 transakcji papierowych z wynikiem netto ≥ 0 przy modelu konserwatywnym; reguła beznadziejności (odrzucenie, jeśli górna granica 90% < +0,05 R); potem co najmniej 100 transakcji na żywo do zmierzenia kosztów. Ta bramka może tylko odrzucić, nie udowodnić przewagi |
| **B (skalowanie)** | Co najmniej 1000 transakcji w co najmniej 6 miesięcy; jednostronna dolna granica 95% (klastrowana po dniach) średniej netto na transakcję > 0; wynik netto nadal ≥ 0 po usunięciu najlepszego 1% transakcji; obsunięcie w granicach 1,5 × 95. percentyla rozkładu bootstrapowego plus twardy limit bezwzględny |

Dodatkowe zasady statystyczne: t ≥ 2 tylko dla jednej wstępnie zarejestrowanej specyfikacji; przy ponad ok. 10 wariantach t ≥ 3 albo deflated Sharpe ≥ 0,95.

**Pre-rejestracja** (`docs/preregistration/v1.md`, powstaje w planie implementacji, **przed** pierwszym dniem na papierze): skróty SHA-256 plików `config/strategy_v1.toml` i `config/risk_v1.toml`, liczba wariantów K, definicje metryk i bramek, data rozpoczęcia. Zmiana któregokolwiek parametru po tej dacie tworzy nowy wariant.

## 12. Testy

Pisane przed kodem (TDD) dla automatu stanów, uzgadniania i ryzyka.

- **Jednostkowe:** przejścia stanów, deduplikacja wypełnień, generowanie `client_order_id`, każda kontrola ryzyka, model kosztów, okna czasowe (święta, skrócone sesje, zmiana czasu 1.11.2026).
- **Awarie na `FakeBroker`:** zdublowane i spóźnione zdarzenia, zdarzenie po anulowaniu, wypełnienia częściowe (w tym losowe), timeout → `UNKNOWN` → wyjaśnienie, duplikat `client_order_id` (422), rozłączenie w środku operacji, cisza strumienia, halt, restart procesu w środku wejścia i wyjścia (`kill -9`), podwójne wypełnienie nogi stop, zlecenie obce, pozycja obca.
- **Niezmienniki (sprawdzane po każdym teście awarii):** (1) żadne zlecenie nie wyszło bez wiersza INTENT; (2) żadna zmiana stanu pozycji bez zdarzenia brokera; (3) ekspozycja ≤ limity wirtualne; (4) licznik strat dziennych przeżywa restart; (5) wyjścia nigdy nie zablokowane; (6) co najwyżej jedno połączenie na endpoint.
- **Integracyjne na papierze:** jedno zlecenie na typ, uruchamiane na VPS z kluczami paper z pliku (nigdy z czatu ani repo). Rozstrzygają pytania otwarte z sekcji 16.
- **Odtwarzanie:** zapis surowych zdarzeń z dnia papierowego służy jako fixture regresyjny. Odtworzenie logu produkcyjnego rzadko odtwarza zachowanie, więc traktujemy je jako uzupełnienie, nie dowód.

Przy każdej deklaracji ukończenia obowiązuje weryfikacja uruchomieniem testów, a nie opis.

### Gdzie biegną testy i CI

| Rodzaj testu | Wymaga | Gdzie biegnie |
|---|---|---|
| Jednostkowe, awarie na `FakeBroker`, niezmienniki | Tylko Python, bez sieci i bez kluczy | Sesja deweloperska, VPS, CI |
| Budowa obrazu Dockera i testy logiki uruchamiane w obrazie | Demon Dockera | CI i VPS |
| Integracyjne na papierze (`integration_paper`) | Klucze paper i dostęp do Alpaca | **Tylko VPS** |
| Pełne sesje paper (kryteria 2–3, bramka P) | Klucze, sieć, ciągła praca | **Tylko VPS** |

CI to GitHub Actions (`.github/workflows/ci.yml`):

- Wyzwalacze: `push`, `pull_request` i ręczne uruchomienie (`workflow_dispatch`).
- Zadanie `tests`: macierz Python 3.12 i 3.13, instalacja z przypiętych wersji, `pytest -m "not integration_paper"`. Testy `integration_paper` mają znacznik i **nigdy nie biegną w CI**.
- Zadanie `image`: budowa obrazu `python:3.12-slim` i uruchomienie w nim testów logiki, żeby wykryć braki w obrazie (np. bazę stref czasowych).
- **CI nie dostaje żadnych sekretów Alpaca ani dostępu do VPS.** Klucze paper nigdy nie trafiają do sekretów GitHub. Wdrożenie na VPS jest ręczne (pobranie zmian i `docker compose`); automatyczny deploy z CI jest poza zakresem v1.
- Higiena łańcucha dostaw: `permissions: contents: read`, akcje zewnętrzne przypięte do pełnego SHA commita.
- Zielony CI na `main` jest warunkiem uznania zmiany za ukończoną i niezależnym potwierdzeniem testów uruchomionych lokalnie; nie zastępuje ich. Jeśli plan konta GitHub udostępnia ochronę gałęzi, ustawiamy wymóg zielonego CI przed scaleniem.
- Wypchnięcie pliku workflow może wymagać dodatkowego uprawnienia integracji GitHub (pytanie 19). W razie odmowy właściciel dodaje plik przez interfejs GitHub, a jego treść podaje plan implementacji.
- Limit minut CI zależy od planu konta; testy logiki są małe.

## 13. Wdrożenie i operacje

- Jeden kontener `python:3.12-slim`, użytkownik bez uprawnień, system plików tylko do odczytu z tmpfs i wolumenem `/data` (SQLite). Limit pamięci 512 MB. Osobno od bota Solana, bez współdzielenia czegokolwiek.
- Klucze paper z pliku zamontowanego tylko do odczytu (`/secrets/alpaca_paper.json`). Środowisko deweloperskie Claude Code nie ma dostępu do tego pliku.
- **Brak ścieżki live w kodzie.** Adres API jest stałą (paper); adapter live powstanie dopiero po bramce A jako osobna, przeglądana zmiana.
- **Blokada instancji:** plik blokady w `/data` oraz procedura restartu „zatrzymaj, potem uruchom" (nigdy nakładanie), bo drugie połączenie strumienia dostaje 406. `stop_grace_period` pozwala zamknąć połączenia; zamknięcie procesu nie spłaszcza pozycji, bo mają stopy u brokera.
- Tętno do pliku co kilka sekund; healthcheck Dockera sprawdza świeżość.
- Powiadomienia przez interfejs `Notifier`: w v1 log i plik alarmów; adapter Telegram jako późniejsze rozszerzenie.
- Zużycie pamięci (założenie < 150 MB) mierzymy w kryteriach akceptacji, nie zakładamy.
- W czasie awarii Alpaca silnik nie ponawia zleceń w pętli; przechodzi w kill switch miękki.
- Wdrożenie na VPS jest ręczne i niezależne od CI (sekcja 12): CI nie ma ani kluczy, ani dostępu do serwera.

## 14. Struktura repozytorium i praca z Claude Code

```
pyproject.toml
CLAUDE.md                     zasady pracy, komendy, zakazy
.claude/settings.json         hooki fail-closed, reguły deny
.claude/agents/               risk-reviewer, order-state-auditor, backtest-skeptic (tylko Read, Grep, Glob)
.github/workflows/ci.yml      CI: testy logiki (Python 3.12 i 3.13) oraz w obrazie Dockera
src/gwstock/                  config, clock, journal, broker, orders, reconcile, risk,
                              data, strategy, execution, costs, watchdog, report, app
tests/                        unit, fault, integration_paper
config/                       strategy_v1.toml, risk_v1.toml
docs/                         research, superpowers/specs, superpowers/plans, preregistration, ops
docker/                       Dockerfile, compose.yml
```

Zabezpieczenia pracy z Claude Code (warstwy, od najmocniejszej): klucze i dostęp do środowiska produkcyjnego poza zasięgiem agenta; reguły `deny`; hooki `PreToolUse` z `onFailure: "block"` (hooki domyślnie przepuszczają przy błędzie) blokujące zmiany adresu API; piaskownica; subagenci recenzujący tylko do odczytu. Wzorce Bash w regułach nie są granicą bezpieczeństwa. CLAUDE.md jest tylko wskazówką.

Skille i narzędzia zewnętrzne: Alpaca MCP wyłącznie do rozwoju (przypięta wersja, paper, bez zestawu `trading`), nigdy na ścieżce zleceń; Day Trader Skill i `tradermonty` jako referencje do przeczytania, nie zależności wykonania.

## 15. Odroczone eksperymenty (każdy zwiększa K)

Time-stop i wyjście po utracie momentum (parametry 180 s / +0,4% / stop −1,2% z jednej z odpowiedzi nie mają poparcia w badaniach); ułamki akcji ze stopem lokalnym; klasyfikator newsów (LLM klasy Haiku) dopiero po bazowym teście bez LLM; drugie okno wejść; filtr reżimu rynkowego; adapter Telegram; panel WWW.

## 16. Pytania otwarte i sposób rozstrzygnięcia

Żadne nie blokuje pisania silnika na `FakeBroker`. Pytania 1–7 blokują bramkę A, pytania 8–13 i 17–18 rozstrzygamy testem na papierze przed pierwszym dniem pomiarowym, pytania 14–16 to doradca w Polsce, a pytanie 19 rozstrzygamy przy pierwszej próbie wypchnięcia pliku workflow.

| # | Pytanie | Kto / jak |
|---|---|---|
| 1 | Czy rezydent podatkowy Polski może otworzyć indywidualne konto live w Alpaca Securities LLC; jakie dokumenty | Właściciel: e-mail do Alpaca (`docs/ops/alpaca-support-email.md`) |
| 2 | Czy w pełni autonomiczny handel jest dozwolony; czym jest przepływ „non-retail in nature" | Alpaca support |
| 3 | GFV i intraday margin poniżej 2 000 USD; `buying_power` z niezaksięgowanymi wpływami | Alpaca support, ewentualnie mikro-test live |
| 4 | Opłaty regulacyjne: agregacja dzienna czy na wypełnienie; przerwa TAF | Alpaca support |
| 5 | Wpłaty i wypłaty małej kwoty z Polski, opłaty | Alpaca support |
| 6 | Zachowanie zleceń w haltach, limit Trading API, liczba połączeń `trade_updates` | Alpaca support + nagłówki `X-RateLimit-*` na papierze |
| 7 | Dokumenty podatkowe z Alpaca | Alpaca support, potem doradca |
| 8 | Czy da się ustawić konto paper na inną kwotę | Test w panelu paper (nie blokuje: i tak mamy limit wirtualny) |
| 9 | Czy `most-actives` i `movers` działają na Basic | Test jednym wywołaniem (nie blokuje: strategia ich nie wymaga) |
| 10 | Czy `delayed_sip` przyjmuje `bars:["*"]` na Basic; liczenie symboli do limitu 30 | Test na papierze (rozstrzyga wybór źródła rankingu; wariant zapasowy to REST z `end ≤ teraz − 15 min`) |
| 11 | Czy OTO/bracket z pełnymi akcjami zachowuje się zgodnie z założeniem | Test na papierze (jedno zlecenie na typ) |
| 12 | Zakres unikalności `client_order_id` po anulowaniu/wypełnieniu; odpowiedź 422 | Test na papierze (nie blokuje: schemat identyfikatora jest unikalny z konstrukcji) |
| 13 | Różnica IEX–SIP w cenie i wolumenie per symbol | Tydzień logowania na papierze |
| 14–16 | Status podatkowy częstego handlu (art. 30b), kursy NBP, stanowisko KNF o reverse solicitation | Doradca podatkowy lub prawny w Polsce |
| 17 | Czy noga stop z OTO dostaje właściwą ilość przy częściowym wypełnieniu wejścia | Test na papierze (losowe 10% częściowych wypełnień) |
| 18 | Czy nogi OTO dziedziczą `client_order_id` albo jak je odnaleźć przez zlecenie nadrzędne | Test na papierze |
| 19 | Czy integracja GitHub może wypychać pliki `.github/workflows` | Próba przy implementacji; w razie odmowy właściciel dodaje plik przez interfejs GitHub |

## 17. Kryteria akceptacji v1

1. Wszystkie testy jednostkowe i awaryjne z sekcji 12 przechodzą (lokalnie i w CI, na Pythonie 3.12 i 3.13 oraz w obrazie Dockera); niezmienniki spełnione po każdym teście awarii.
2. Pełna sesja na papierze na VPS bez ingerencji: skan → sygnał → wejście → stop → wyjście → spłaszczenie → raport.
3. Zabicie procesu w środku wejścia i wyjścia: po restarcie brak zdublowanych zleceń i pozycji osieroconych, potwierdzone testem i na papierze.
4. Ćwiczenia kill switcha: oba poziomy przez pliki, cisza strumienia i rozbieżność z brokerem zasymulowane.
5. Raport dzienny z wynikiem brutto i netto, kosztami, licznikami błędów wykonania i rozbieżności.
6. Zużycie pamięci zmierzone i opisane.
7. W kodzie nie istnieje ścieżka live.

## 18. Główne ryzyka projektu

| Ryzyko | Skutek | Łagodzenie |
|---|---|---|
| Strategia nie ma przewagi netto | Wynik bliski zera lub ujemny | To spodziewany wynik; bramki dowodowe; silnik jako instrument pomiarowy |
| Dane Basic (IEX) zniekształcają RVOL | Wariant ≠ artykuł; rozjazd między treningiem a produkcją | Jawnie nazwany wariant; pomiar IEX/SIP; decyzja o płatnym SIP dopiero na dowodach |
| Błąd w automacie stanów lub uzgadnianiu | Duplikaty, pozycje osierocone | TDD, `FakeBroker` z awariami, niezmienniki, niezależna recenzja `order-state-auditor` |
| Awaria Alpaca | Brak wyjścia, zlecenia w zawieszeniu | Stop u brokera, kill switch miękki, brak pętli ponowień |
| Dryf zakresu | Opóźnienie i błędy | Sekcja „poza zakresem" i odroczone eksperymenty |
| Publiczne repo | Ujawnienie treści | Brak sekretów w repo; wypychanie dopiero po zmianie na prywatne |
