# Odczyt live Capital.com z VPS (31.10.2026): DE40, rachunki, świece

Źródło: test tylko do odczytu wykonany przez właściciela na VPS nowym kluczem API (host live `https://api-capital.backend-capital.com/api/v1`): logowanie, `GET /accounts`, `GET /markets/DE40`, `GET /prices/DE40?resolution=MINUTE_5&max=3`, `GET /positions`, zamknięcie sesji. Bez zleceń. Dokument nie zawiera kluczy, tokenów ani kwot sald. Uzupełnia `capitalcom-api-notes.md` (statusy pozycji C…) i `capitalcom-live-readout-2026-11-01.md`.

## Rachunki

- Logowanie: poprawne.
- Dwa rachunki typu CFD, waluta EUR, status `ENABLED`.
- Rachunek z saldem > 0 jest rachunkiem **preferowanym**; drugi (niepreferowany) ma saldo 0. Sesja otwarta bez `PUT /session` ląduje więc na rachunku z saldem. `bind-account` nadal wybiera rachunek po identyfikatorze i saldzie, nigdy po kolejności.

## `GET /markets/DE40`

Klucze główne odpowiedzi: `instrument`, `dealingRules`, `snapshot`.

`instrument`: `epic, symbol, expiry, name, lotSize, type, guaranteedStopAllowed, streamingPricesAvailable, currency, marginFactor, marginFactorUnit, openingHours, country, overnightFee`.

`dealingRules`:

| Reguła | Wartość |
|---|---|
| `minDealSize` | 0.001 |
| `maxDealSize` | 1000 |
| `minSizeIncrement` | 0.001 |
| `minStepDistance` | 0.1 pkt |
| `minStopOrProfitDistance` | 0.01% |
| `maxStopOrProfitDistance` | 100% |
| `minGuaranteedStopDistance` | 0.1% |
| `marketOrderPreference` | `AVAILABLE_DEFAULT_ON` |
| `trailingStopsPreference` | `NOT_AVAILABLE` |

`openingHours` (strefa **UTC**): pon–czw 00:00–21:00 i 21:05–24:00 (przerwa 5 minut dziennie), pt 00:00–21:00, sob zamknięte, nd 22:00–24:00. Instrument handluje się więc niemal całą dobę; **stan rynku nie jest wskaźnikiem sesji Xetra**. Sesję wyznacza profil giełdy (`XETRA`), nie `marketStatus`.

`snapshot`: `marketStatus, percentageChange, updateTime, delayTime, bid, offer, decimalPlacesFactor, scalingFactor, marketModes`.
- W sobotę `marketStatus` = `CLOSED`.
- `updateTime` ma postać `"2026-10-30T21:59:55.526"` **bez strefy czasowej** i wygląda na czas lokalny (CET, UTC+1), nie UTC: piątkowe zamknięcie jest o 21:00 UTC, a ostatnia aktualizacja to 21:59 według tego pola.
- Pola `updateTimeUTC` w `snapshot` **nie ma**.

## `GET /prices/DE40?resolution=MINUTE_5&max=3`

- Zwrócono 3 świece. Klucze główne: `prices, instrumentType, tickSize, pipPosition`.
- Pola świecy: `snapshotTime`, `snapshotTimeUTC`, `openPrice`, `closePrice`, `highPrice`, `lowPrice`, `lastTradedVolume`.
- Każda cena (open, close, high, low) ma osobno `bid` i `ask`; świeca mid wymaga policzenia (bid + ask) / 2.
- `snapshotTime` jest w czasie lokalnym (przykład: `"2026-10-30T21:45:00"` przy `snapshotTimeUTC` = `"2026-10-30T20:45:00"`, różnica +1 h = CET). **Używać wyłącznie `snapshotTimeUTC`.**

## `GET /positions`

Pusta lista (brak otwartych pozycji).

## Wnioski dla silnika (rozstrzygnięcia sterujące)

1. Czas wyłącznie z pól UTC (`snapshotTimeUTC`); `snapshotTime` i `snapshot.updateTime` (lokalne, bez strefy) nigdy nie są parsowane do logiki. Świeżość notowania liczymy zmianą krotki (bid, offer, updateTime), nie parsowaniem czasu (rozstrzygnięcie o `Quote.ts` pozostaje w mocy).
2. `minStepDistance` = 0,1 pkt: odległość stopu zaokrąglamy **w górę** do wielokrotności 0,1 pkt (hipoteza do potwierdzenia na próbie `smoke`), a wielkość liczymy z zaokrąglonej odległości, więc ryzyko nie rośnie. `InstrumentRules` dostaje opcjonalne pole `stop_step` (domyślnie 0 = bez zaokrąglania).
3. Trailing stop niedostępny: v1 go nie używa, więc brak wpływu.
4. **Znacznik świecy: początek czy koniec okresu?** Dokumentacja nie rozstrzyga, a pomyłka wybrałaby świecę sprzed otwarcia zamiast pierwszej. Przed uzbrojeniem trzeba to potwierdzić w `dry_run`: silnik zapisuje w zdarzeniu decyzji początek UTC wybranej świecy oraz jej OHLC, a właściciel porównuje to z wykresem 5-minutowym DE40 w aplikacji Capital.com dla 09:00 czasu Berlina. Bramka L0.
5. Wartości `marginFactor`, `guaranteedStopAllowed` i `overnightFee` nie zostały przekazane w odczycie; pozostają w `capitalcom-live-readout-2026-11-01.md` (5%) lub jako „do potwierdzenia".
