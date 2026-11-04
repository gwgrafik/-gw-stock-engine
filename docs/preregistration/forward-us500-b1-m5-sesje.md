# Hipoteza badawcza H-US500-B1-M5-SESJE (zamrożona 4.11.2026, test forward bez zleceń)

To NIE jest rejestracja strategii silnika. Silnik handluje tylko DE40 według `orb_index_cfd v1`. To zapis hipotezy badawczej,
zamrożony PRZED zebraniem nowych danych, żeby odróżnić przypadek od przewagi.

## Skąd hipoteza
Test historyczny `research/candle_strategies.py` (reguły B1-B4, 14 wariantów na instrument, podział chronologiczny 60/20/20) na
świecach minutowych 7.07-4.11.2026: DE40 0 kandydatów, US100 0 kandydatów, US500 1 kandydat (B1 / świece M5 / okno SESJE),
nieprzyjęty przez próg PF > 1,2 przy kosztach x1,5 (PF 1,11 w ocenie końcowej, 42 transakcje, netto +22 pkt przy x1,5).
Łącznie sprawdzono 42 warianty reguł świecowych na trzech silnie skorelowanych indeksach, więc jeden kandydat jest w granicach
tego, co daje przypadek. Dane do 4.11.2026 są ZUŻYTE (opracowanie, wybór i ocena końcowa) i nie są dowodem.

## Reguła (zamrożona w stanie kodu `6c15bc8`; zmiana sygnału B1 = NOWY wariant)
- Instrument US500, reguła B1, świece M5 (N=12), okno sygnałów 09:45-11:45 i 14:00-15:30 czasu nowojorskiego, wyjście zaplanowane 15:55.
- Środowisko: `GW_EPIC=US500 GW_SLIP=0.15 GW_STOP_EXTRA=0.3 GW_STEP=0.1 GW_POINT_EUR=0 GW_TZ=America/New_York`
  `GW_WIN_ALL=09:45-15:30 GW_WIN_SESJE=09:45-11:45,14:00-15:30 GW_FLAT=15:55`.
- Poślizg i dodatkowy koszt przy stopie to ZAŁOŻENIA (proporcjonalne do ceny względem DE40), nie pomiar z konta. Ocena przy kosztach x1,5.

## Test forward
- Pierwszy dzień nowych danych: 2026-11-05 (czas NY). Uruchomienie: `GW_ONLY="B1/M5/SESJE" GW_FORWARD_FROM=2026-11-05` na odświeżonych świecach.
- Bez zleceń, bez zmian reguły i parametrów w trakcie testu.
- Werdykt dopiero od 50 NOWYCH transakcji (przy ok. 2,5 transakcji dziennie to ok. 4 tygodnie sesji).
- SPEŁNIA: netto > 0 przy kosztach x1,5 i PF > 1,2 na nowych transakcjach. Oznacza to wyłącznie "kandydat do zbudowania wariantu silnika
  i osobnej pre-rejestracji", nie zysk.
- NIE SPEŁNIA: hipotezę odrzucamy i nie wracamy do niej na tych samych danych.
- Wynik pojedynczego tygodnia nie ma znaczenia; liczy się tylko werdykt po 50 nowych transakcjach.

## Ograniczenia
Próba mała (średni wynik na transakcję poniżej 1 pkt przy odchyleniu kilkakrotnie większym), koszty US500 niezmierzone na koncie,
brak modelu walutowego (USD vs konto EUR), indeksy USA skorelowane z DE40 i między sobą.
