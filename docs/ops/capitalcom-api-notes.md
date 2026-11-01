# Notatki o API Capital.com (rozpoznanie dokumentacji)

Odczyt dokumentacji: 2026-10-31. Dokument jest jedynym źródłem dla Zadania 4 (typy, `Broker`, `FakeBroker`), Zadania 8 (uzgadnianie) i Zadania 13 (adapter Capital.com). Reguły instrumentu DE40 zmierzone na koncie live są w `docs/ops/capitalcom-live-readout-2026-11-01.md` i tu nie są powielane.

Dokument nie zawiera kluczy, haseł, tokenów, numerów rachunków ani sald naszego konta (poza liczbami z oficjalnego przykładu [S1], użytymi w sekcjach 2.4 i 2.8 do wyprowadzenia równania sald). Wszystkie identyfikatory w przykładach to symbole zastępcze w nawiasach ostrych, a liczby w polach salda w przykładach JSON to `<number>`. Ceny, czasy i wielkości w przykładach JSON są poglądowe (kształt z oficjalnej specyfikacji, ceny DE40 z odczytu [S4]); wartości reguł DE40 pochodzą wyłącznie z [S4].

## 0. Źródła i znaczenie statusów

| Skrót | Źródło | Wiarygodność |
|---|---|---|
| [S1] | Oficjalna dokumentacja <https://open-api.capital.com/> (strona Redoc; odczytałem renderowany tekst oraz wbudowaną w stronę specyfikację OpenAPI 3.0.0, wersja 1.0.0) | Oficjalne. Przykłady w specyfikacji są poglądowe: nie zawierają każdego pola, które zwraca żywe API |
| [S2] | Centrum pomocy i strony Capital.com: <https://help.capital.com/hc/en-us/articles/27299104990482-Trading-hours-extended-on-key-indices>, <https://capital.com/en-eu/learn/market-guides/what-is-indices-trading> oraz strona rynku <https://capital.com/en-int/markets/indices/germany-40-index-1> | Oficjalne, ale marketingowo-pomocnicze, nie część kontraktu API |
| [S3] | Niezależny konektor StockSharp, plik `CapitalCom/Native/Model/Rest.cs`: <https://github.com/StockSharp/Connectors/blob/main/CapitalCom/Native/Model/Rest.cs> | **Nieoficjalne.** Używam wyłącznie jako wskazówki, że pole istnieje w żywych odpowiedziach, choć nie ma go w oficjalnym przykładzie. Każde takie pole jest oznaczone `do potwierdzenia` |
| [S4] | `docs/ops/capitalcom-live-readout-2026-11-01.md` | Pomiar własny na koncie live (tylko odczyt) |

Status każdej pozycji, dokładnie jeden z dwóch napisów:

- `udokumentowane` oznacza, że oficjalna dokumentacja [S1] wprost podaje to, o co pytamy (ścieżkę, nagłówek, pole, wartość).
- `do potwierdzenia w dry_run lub w odczytach live` oznacza, że dokumentacja milczy, podaje tylko przykład, z którego da się coś wywnioskować, albo informacja pochodzi z [S3]. Adapter ma wtedy zachować ostrożność, a wartość rozstrzyga `dry_run` (odczyty), próba `smoke` (pierwsza transakcja na wielkości minimalnej) lub Zadanie 18. Lista takich pozycji z numerami C1 do C29 jest w sekcji 6.

Czego nie udało się przeczytać: repozytorium z kolekcją Postman (<https://github.com/capital-com-sv/capital-api-postman>) zwróciło w moim środowisku 403, więc nie było źródłem. Wszystkie ścieżki, nagłówki i przykłady poniżej pochodzą ze specyfikacji [S1].

## 1. Zasady ogólne

### 1.1 Adresy i nagłówki

- Baza live: `https://api-capital.backend-capital.com`, wszystkie ścieżki REST mają prefiks `/api/v1`. Baza demo: `https://demo-api-capital.backend-capital.com` (klucz właściciela jest odrzucany na hoście demo, bo nie ma konta demo [S4]; adapter używa wyłącznie hosta live). `udokumentowane` [S1, "General information"].
- Nagłówki, `udokumentowane` [S1]:

| Nagłówek | Gdzie | Znaczenie |
|---|---|---|
| `X-CAP-API-KEY` | tylko `GET /session/encryptionKey` i `POST /session` | Klucz API z Settings > API integrations |
| `CST` | wszystkie żądania po zalogowaniu | Token autoryzacji klienta, zwracany w nagłówkach odpowiedzi `POST /session` |
| `X-SECURITY-TOKEN` | wszystkie żądania po zalogowaniu | Token rachunku ("shows which financial account is used for the trades"), zwracany jak wyżej |
| `Content-Type: application/json` | żądania z ciałem | Ciało to JSON |

Cytat [S1]: "On starting the session you will receive the `CST` and `X-SECURITY-TOKEN` parameters in the response headers. `CST` is an authorization token, `X-SECURITY-TOKEN` shows which financial account is used for the trades. These headers should be passed on subsequent requests to the API. Both tokens are valid for 10 minutes after the last use."

- Tokeny żyją tylko w pamięci procesu (spec §13). Klucz idzie wyłącznie w `X-CAP-API-KEY` i tylko do `/session*`.
- Dokumentacja deklaruje nagłówki `CST` i `X-SECURITY-TOKEN` także w odpowiedziach innych operacji (np. `ping`, `PUT /session`). Adapter powinien aktualizować tokeny z nagłówków odpowiedzi, jeśli są obecne. Czy tokeny faktycznie się zmieniają po `PUT /session`: C18, `do potwierdzenia w dry_run lub w odczytach live`.

### 1.2 Sesja i limity

Cytaty [S1, "Do you have any limitations on your API?"], wszystkie `udokumentowane`:

- "Session is active for 10 minutes. In case your inactivity is longer than this period then an error will occur upon next request." Każde udane żądanie przedłuża sesję ("valid for 10 minutes after the last use"). Spec §13: ping co 5 minut.
- "The maximum request rate is 10 per second per user." Silnik trzyma się połowy: 5 zapytań/s ogółem (spec §13).
- "The maximum request rate is 1 per 0.1 seconds per user when opening positions or creating orders. Otherwise the position/order requests are going to be rejected." Odstęp co najmniej 0,1 s między `POST /positions`, `POST /workingorders`.
- "`POST /session` endpoint limit is 1 request per second per API key." Logowania są serializowane; odpowiedź przy przekroczeniu: HTTP 429, `{"errorCode": "error.too-many.requests"}`.
- "`POST /positions` and `POST /workingorders` endpoint limit is 1000 requests per hour in the Demo account." Dotyczy demo, nie live.
- "You have max 100 attempts per 24hrs to successfully generate API keys." (generowanie klucza w panelu, nie logowanie).
- WebSocket: sesja 10 minut, do 40 instrumentów (sekcja 2.21).
- Czy limity nie-sesyjne zwracają 429, jaki kod błędu i czy jest nagłówek `Retry-After`: dokumentacja milczy, C20, `do potwierdzenia w dry_run lub w odczytach live`. Adapter mapuje HTTP 429 na wyjątek limitu i wstrzymuje zapytania niekrytyczne (spec §13).

Budżet silnika v1 (notowanie przez polling REST; strumień WebSocket odroczony, co jest odchyleniem od spec §4, który traktuje strumień jako źródło podstawowe): `GET /markets/{epic}` co 5 s (0,2/s) + `GET /positions` co 10 s (0,1/s) + `ping` co 5 min + okazjonalnie świece i historia. Razem ok. 0,3 do 0,5 zapytania/s, daleko poniżej 5/s.

### 1.3 Format czasu, liczb i odpowiedzi

- Parametry zapytań `from` i `to` mają format `YYYY-MM-DDTHH:MM:SS` bez milisekund i bez sufiksu strefy; filtracja jest po polach UTC (`snapshotTimeUTC` dla cen, `dateUTC` dla historii), więc `from`/`to` to czas UTC. `udokumentowane` [S1: "Date format: YYYY-MM-DDTHH:MM:SS (e.g. 2022-04-01T01:01:00). Filtration by date based on snapshotTimeUTC parameter"].
- Znaczniki czasu w odpowiedziach to napisy `2022-04-05T09:46:01.872` (milisekundy, **bez** sufiksu strefy). Pola o nazwie z końcówką UTC (`createdDateUTC`, `snapshotTimeUTC`, `updateTimeUTC`, `dateUTC`, `dateUtc`) są w UTC. Pola bez tej końcówki (`createdDate`, `snapshotTime`, `updateTime`, `date`) to czas lokalny o nieznanym przesunięciu: w przykładach różnica wynosi +1 h (ceny, luty), +2 h (ceny, kwiecień) i +3 h (pozycje, kwiecień; `timezoneOffset: 3`), czyli zależy od przykładu i jest zależna od czasu letniego. **Reguła dla adaptera: czytać wyłącznie pola UTC i traktować napis jako UTC (`tzinfo=UTC`); nigdy nie używać pól lokalnych.** Znaczenie pól lokalnych: C19, `do potwierdzenia w dry_run lub w odczytach live`.
- Wielkość liter w nazwach jest niespójna: `dateUTC` (aktywność), `dateUtc` (transakcje), `createdDateUTC`, `updateTimeUTC`. Parser ma używać dokładnych nazw z tabel poniżej.
- Znaczniki czasu WebSocket i `GET /time` to milisekundy epoki Unix (liczby).
- Liczby w odpowiedziach to liczby JSON (ceny, wielkości, `upl`), w historii transakcji `size` jest **napisem** (`"1.05"`). Adapter parsuje JSON z `parse_float=Decimal` i nie używa `float` w obliczeniach. Przy wysyłce liczb (`size`, `stopDistance`) zaokrąglamy do kroku instrumentu (sekcja 2.16) i serializujemy tak, by nie wprowadzać zapisu naukowego.
- `Content-Type` odpowiedzi `POST /session` i `DELETE /session` jest w specyfikacji opisany jako `text/plain`, mimo że treść to JSON; pozostałe odpowiedzi to `application/json`. Adapter czyta treść jako JSON niezależnie od `Content-Type` (w `aiohttp`: `await resp.json(content_type=None)`). Czy to samo dotyczy żywych odpowiedzi: C19b, `do potwierdzenia w dry_run lub w odczytach live`.

### 1.4 Błędy

Format błędu: `{"errorCode": "<kod>"}` [S1]. Dokumentacja opisuje statusy 200, 400, 401, 404, 429. Kody występujące w specyfikacji, wszystkie `udokumentowane` jako przykłady:

| HTTP | `errorCode` | Operacja |
|---|---|---|
| 400 | `error.null.api.key` | `POST /session` (brak `X-CAP-API-KEY`) |
| 401 | `error.invalid.details` | `POST /session` (zły identyfikator, hasło lub klucz) |
| 429 | `error.too-many.requests` | `POST /session` |
| 400 | `error.invalid.accountId` | `PUT /session` |
| 400 | `error.invalid.leverage.value` | `PUT /accounts/preferences` |
| 400 | `error.invalid.stoploss.minvalue: <granica>`, `error.invalid.takeprofit.maxvalue: <granica>` | `POST /positions` |
| 400 | `error.invalid.stoploss.maxvalue: <granica>`, `error.invalid.takeprofit.minvalue: <granica>` | `PUT /positions/{dealId}` |
| 404 | `error.not-found.dealId` | `GET`, `PUT`, `DELETE /positions/{dealId}` |
| 404 | `error.not-found.dealReference` | `GET /confirms/{dealReference}` |
| 404 | `error.not-found.epic` | `GET /markets/{epic}` |
| 400 | `error.invalid.resolution` | `GET /prices/{epic}` |
| 400 | `error.invalid.from` | `GET /history/activity` |
| 400 | `error.null.searchTerm`, `error.null.epic` | `GET /markets` |
| 400 | `error.not-in-future.goodTillDate` | `POST /workingorders` (nieużywane w v1) |

Uwagi:

- Kody `stoploss`/`takeprofit` niosą po dwukropku liczbową granicę (np. `error.invalid.stoploss.minvalue: 17.146`). Z przykładów wynika, że to poziom ceny, nie odległość (przykłady dotyczą instrumentu o cenie ok. 24). Adapter wydziela część po `": "` i zapisuje ją w dzienniku; semantyka: C14, `do potwierdzenia w dry_run lub w odczytach live`.
- Nie ma w dokumentacji kodu błędu dla: wygasłej sesji (powszechnie raportowany `error.invalid.session.token` nie występuje w [S1]), zbyt małej wielkości, niezgodnej z krokiem wielkości, niewystarczających środków, zamkniętego rynku przy `POST /positions`. Zasada dla adaptera: **każde HTTP 401 na żądaniu po zalogowaniu = ponowne logowanie raz (serializowane) i jedna powtórka żądania**; każdy nierozpoznany kod 4xx przy otwarciu to `BrokerRejected(<errorCode>)` z surową treścią w dzienniku. C13 i C14, `do potwierdzenia w dry_run lub w odczytach live`.
- Na hoście demo odczyt właściciela dał `error.null.accountId` [S4]; kodu nie ma w [S1].
- **Które odpowiedzi `POST /session` liczą się do blokady logowań w silniku (ochrona klucza przed zablokowaniem).** Tylko odrzucenie danych logowania: każde 401, 400 `error.null.api.key` i 403 z `errorCode` Capital.com, który nie jest przejściowy (adapter zgłasza `BrokerAuthError`, a po trzech takich odrzuceniach silnik przestaje się logować). **403 z kodem przejściowym nie liczy się** (zwykły `BrokerError` z odstępem 30 s do 5 min, bez blokady): `error.too-many.requests`, `error.service.unavailable`, `error.maintenance` i każdy kod zawierający `too-many` albo `unavailable` (wielkość liter bez znaczenia). 403 bez `errorCode` (strona brzegowa lub WAF) też jest przejściowe. 401 z kodem przejściowym nadal się liczy. Tabela „Błędy” wyżej zawiera tylko przykłady z [S1]; żywe kody 403 są nieudokumentowane (C13, C20), więc granica „przejściowy czy odrzucenie” to heurystyka adaptera. Jej pomyłka kosztuje: albo logowanie ponawiane z odstępem do 5 min, gdyby prawdziwa blokada konta miała kod z `too-many` lub `unavailable` (alarm do właściciela i tak idzie), albo zbędna blokada logowań, gdyby kod przejściowy miał inną nazwę.

## 2. Operacje

### Przegląd i statusy operacji

Trzy kolumny opisowe i jedna kolumna statusu. "Kształt" dotyczy ścieżki, metody, nagłówków, ciała żądania i struktury odpowiedzi; "Pola krytyczne" dotyczy semantyki pól, na których opiera się silnik (numery C odsyłają do sekcji 6). Napis statusu występuje wyłącznie w kolumnie "Status operacji", dokładnie jeden na wiersz. Status operacji = najsłabsza z dwóch wartości dla pól, które silnik faktycznie czyta.

| Operacja | Kształt | Pola krytyczne | Status operacji | Sekcja |
|---|---|---|---|---|
| `GET /time` | opisany w [S1] | opisane w [S1] | udokumentowane | 2.1 |
| `GET /ping` | opisany w [S1] | opisane w [S1] | udokumentowane | 2.2 |
| `GET /session/encryptionKey` | opisany w [S1] | opisane w [S1] | udokumentowane | 2.3 |
| `POST /session` | opisany w [S1] | otwarte: C13, C17, C19b | do potwierdzenia w dry_run lub w odczytach live | 2.4 |
| `GET /session` | opisany w [S1] | opisane w [S1] | udokumentowane | 2.5 |
| `PUT /session` | opisany w [S1] | otwarte: C18 | do potwierdzenia w dry_run lub w odczytach live | 2.6 |
| `DELETE /session` | opisany w [S1] | opisane w [S1] | udokumentowane | 2.7 |
| `GET /accounts` | opisany w [S1] | otwarte: C17 | do potwierdzenia w dry_run lub w odczytach live | 2.8 |
| `GET /accounts/preferences` | opisany w [S1] | otwarte: C15, C16b | do potwierdzenia w dry_run lub w odczytach live | 2.9 |
| `POST /positions` | opisany w [S1] | otwarte: C6, C14, C16c | do potwierdzenia w dry_run lub w odczytach live | 2.10 |
| `GET /positions` | opisany w [S1] | otwarte: C5, C24 | do potwierdzenia w dry_run lub w odczytach live | 2.11 |
| `GET /positions/{dealId}` | opisany w [S1] | otwarte: C5, C24 | do potwierdzenia w dry_run lub w odczytach live | 2.12 |
| `PUT /positions/{dealId}` (stop) | opisany w [S1] | otwarte: C5d | do potwierdzenia w dry_run lub w odczytach live | 2.13 |
| `DELETE /positions/{dealId}` | opisany w [S1] | otwarte: C3, C11b, C22 | do potwierdzenia w dry_run lub w odczytach live | 2.14 |
| `GET /confirms/{dealReference}` | opisany w [S1] | otwarte: C1, C2, C3, C4 | do potwierdzenia w dry_run lub w odczytach live | 2.15 |
| `GET /markets/{epic}` (reguły, status rynku, notowanie, godziny) | opisany w [S1] | otwarte: C10, C11, C12, C16, C25, C26 | do potwierdzenia w dry_run lub w odczytach live | 2.16 |
| `GET /markets?epics=` | opisany w [S1] | otwarte: C23 | do potwierdzenia w dry_run lub w odczytach live | 2.17 |
| `GET /prices/{epic}` (świece 5-minutowe) | opisany w [S1] | otwarte: C9 | do potwierdzenia w dry_run lub w odczytach live | 2.18 |
| `GET /prices/{epic}` (świece dzienne) | opisany w [S1] | otwarte: C9 | do potwierdzenia w dry_run lub w odczytach live | 2.18 |
| `GET /history/activity` (cena i przyczyna zamknięcia) | opisany w [S1] | otwarte: C7, C7b | do potwierdzenia w dry_run lub w odczytach live | 2.19 |
| `GET /history/transactions` | opisany w [S1] | otwarte: C8 | do potwierdzenia w dry_run lub w odczytach live | 2.20 |
| `GET /workingorders` (tylko odczyt) | opisany w [S1] | opisane w [S1] | udokumentowane | 2.22 |
| WebSocket: połączenie, `marketData.*`, `OHLCMarketData.*`, `ping` | opisany w [S1] | otwarte: C21 | do potwierdzenia w dry_run lub w odczytach live | 2.21 |
| Godziny otwarcia DE40 (`instrument.openingHours`) | struktura opisana w [S1] | otwarte: C12 | do potwierdzenia w dry_run lub w odczytach live | 2.16.3 |

Operacje `marketnavigation`, `clientsentiment`, `watchlists`, `accounts/topUp` oraz zapis zleceń oczekujących (`POST /workingorders`, `PUT`/`DELETE /workingorders/{dealId}`) istnieją w API, ale v1 ich nie używa (spec §6: zlecenia oczekujące poza zakresem; `topUp` działa tylko na koncie demo). Nie opisuję ich dalej. Wyjątek: `GET /workingorders` (sekcja 2.22) jest czytane, bo spec §7 wymaga wykrycia obcego zlecenia oczekującego na naszym instrumencie.

### 2.1 `GET /api/v1/time`

Test łączności i czas serwera. Bez autoryzacji ("Authentication is not required for this endpoint").

```jsonc
// 200
{ "serverTime": 1649259764171 }   // milisekundy epoki
```

Błędy: brak. Do czego: opcjonalna kontrola dryfu zegara VPS względem serwera (nieużywana w v1). Status operacji: `udokumentowane`.

### 2.2 `GET /api/v1/ping`

"Ping the service to keep a trading session alive." Nagłówki: `CST`, `X-SECURITY-TOKEN`.

```jsonc
// 200
{ "status": "OK" }
```

Wygasła sesja daje błąd HTTP (kod nieudokumentowany, sekcja 1.4). Wywołanie co 5 minut (spec §13), co daje zapas względem 10 minut. Status operacji: `udokumentowane`.

### 2.3 `GET /api/v1/session/encryptionKey`

Nagłówek: `X-CAP-API-KEY`. Zwraca klucz publiczny do szyfrowania hasła klucza API.

```jsonc
// 200
{ "encryptionKey": "<klucz publiczny RSA, base64 DER>", "timeStamp": 1649058606014 }
```

Procedura szyfrowania według przykładu Java w [S1]: bajty `password + "|" + timeStamp` → Base64 → szyfrowanie RSA (`X509EncodedKeySpec`, padding PKCS1) kluczem `encryptionKey` → Base64; wynik idzie w polu `password` z `encryptedPassword: true`. Uwaga: tekst dokumentacji nazywa to "AES encryption", a kod używa RSA; obowiązuje kod.

**Zalecenie dla Zadania 13:** v1 loguje się jawnym hasłem (`"encryptedPassword": false`, opcja w pełni udokumentowana, ruch idzie po TLS). Ścieżka szyfrowana wymaga biblioteki RSA (np. `cryptography`), a spec §3 dopuszcza tylko `aiohttp`, `tzdata` i `pytest`. Plan Zadania 13 wspomina "encryptionKey + POST /session": proszę to traktować jako wariant opcjonalny; podstawą jest hasło jawne. Status operacji: `udokumentowane`.

### 2.4 `POST /api/v1/session`

Nagłówek: `X-CAP-API-KEY`. Limit: 1 żądanie na sekundę na klucz. Ciało:

```jsonc
{ "identifier": "<login>", "password": "<hasło klucza API>", "encryptedPassword": false }
```

| Pole | Typ | Wymagane | Opis [S1] |
|---|---|---|---|
| `identifier` | string | tak | "Client login identifier" |
| `password` | string | tak | "API key custom password" (hasło ustawione przy generowaniu klucza, nie hasło do konta) |
| `encryptedPassword` | boolean | nie | domyślnie `false` |

Odpowiedź 200: nagłówki `CST` i `X-SECURITY-TOKEN` oraz ciało (schemat, wartości salda to symbole zastępcze):

```jsonc
{
  "accountType": "CFD",
  "accountInfo": { "balance": <number>, "deposit": <number>, "profitLoss": <number>, "available": <number> },
  "currencyIsoCode": "EUR",
  "currencySymbol": "€",
  "currentAccountId": "<ACCOUNT_ID>",          // rachunek aktywny w sesji
  "streamingHost": "wss://api-streaming-capital.backend-capital.com/",
  "accounts": [
    { "accountId": "<ACCOUNT_ID>", "accountName": "<nazwa>", "preferred": true,
      "accountType": "CFD", "currency": "EUR", "symbol": "€",
      "balance": { "balance": <number>, "deposit": <number>, "profitLoss": <number>, "available": <number> } }
  ],
  "clientId": "<CLIENT_ID>",
  "timezoneOffset": 3,
  "hasActiveDemoAccounts": false,
  "hasActiveLiveAccounts": true,
  "trailingStopsEnabled": false
}
```

Błędy: 400 `error.null.api.key`, 401 `error.invalid.details`, 429 `error.too-many.requests`.

Uwagi dla adaptera:

- Aktywny rachunek sesji to `currentAccountId` (w przykładzie rachunek z `preferred: true`). Spec §9 wiąże rachunek docelowy poleceniem `bind-account`; jeśli `currentAccountId` różni się od związanego, adapter wykonuje jednorazowo `PUT /session` (sekcja 2.6) tuż po logowaniu, przed jakąkolwiek subskrypcją strumienia.
- Struktura odpowiedzi i nagłówki są opisane w [S1]. Pola salda nie mają opisu znaczenia, ale oficjalny przykład daje równanie: `balance` (92,89) = `deposit` (90,38) + `profitLoss` (2,51) co do grosza, a `available` = 64,66 (liczby z przykładu [S1], nie z naszego rachunku). Hipoteza robocza: `balance` = kapitał własny (wpłaty plus niezrealizowany wynik), `deposit` = wpłacone środki bez wyniku otwartych pozycji, `profitLoss` = niezrealizowany wynik, `available` = środki dostępne do otwarcia pozycji, a zatem `margin_used = balance − available` (28,23 w przykładzie). C17, `do potwierdzenia w dry_run lub w odczytach live` (równanie sald w odczycie bez pozycji; `margin_used` dopiero w porównaniu z panelem przy pozycji otwartej na wielkości minimalnej, czyli w próbie `smoke`, bez zapisywania liczb w repo).
- Kod błędu przy wygasłej sesji i sposób odnowienia: sekcja 1.4 (C13).
- Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C13, C17, C19b).

### 2.5 `GET /api/v1/session`

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Zwraca szczegóły bieżącej sesji; służy do weryfikacji, który rachunek jest aktywny (po `PUT /session`).

```jsonc
// 200
{
  "clientId": "<CLIENT_ID>",
  "accountId": "<ACCOUNT_ID>",                 // aktywny rachunek (uwaga: nazwa inna niż currentAccountId w POST)
  "timezoneOffset": 3,
  "locale": "en",
  "currency": "EUR",
  "streamEndpoint": "wss://api-streaming-capital.backend-capital.com/"
}
```

Status operacji: `udokumentowane`.

### 2.6 `PUT /api/v1/session` (przełączenie rachunku)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Ciało: `{ "accountId": "<ACCOUNT_ID>" }` (wymagane).

```jsonc
// 200
{ "trailingStopsEnabled": false, "dealingEnabled": true,
  "hasActiveDemoAccounts": false, "hasActiveLiveAccounts": true }
// 400
{ "errorCode": "error.invalid.accountId" }
```

Cytat [S1]: "WebSocket streaming falls off when the financial account is changed with the help of the `PUT​ /session` endpoint." Dlatego jedyne dozwolone przełączenie to jednorazowe, tuż po logowaniu (spec §13). Pole `dealingEnabled` w odpowiedzi warto zapisać w dzienniku. Czy po przełączeniu zmienia się `X-SECURITY-TOKEN` (nagłówki odpowiedzi są zadeklarowane): C18; adapter po `PUT` czyta tokeny z odpowiedzi, jeśli są, i weryfikuje `GET /session` (`accountId` równe związanemu), inaczej `BrokerError` i zamknięcie.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C18).

### 2.7 `DELETE /api/v1/session`

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Wylogowanie.

```jsonc
// 200
{ "status": "SUCCESS" }
```

Status operacji: `udokumentowane`.

### 2.8 `GET /api/v1/accounts`

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Lista rachunków klienta (podstawa `list_accounts()` i `bind-account`).

```jsonc
// 200
{ "accounts": [
  { "accountId": "<ACCOUNT_ID>", "accountName": "<nazwa>", "status": "ENABLED",
    "accountType": "CFD", "preferred": true,
    "balance": { "balance": <number>, "deposit": <number>, "profitLoss": <number>, "available": <number> },
    "currency": "EUR", "symbol": "€" }
] }
```

Odczyt właściciela [S4]: dwa rachunki CFD w EUR, oba `ENABLED`. Mapowanie na `AccountInfo(account_id, currency, status, equity, available, margin_used)`: `account_id` ← `accountId`, `currency` ← `currency`, `status` ← `status`, `available` ← `balance.available`; `equity` ← `balance.balance` oraz `margin_used` ← `balance.balance − balance.available` wynikają z równania z sekcji 2.4 (`balance` = `deposit` + `profitLoss`, w przykładzie [S1] `margin_used` wychodzi 28,23) i są hipotezą, C17, `do potwierdzenia w dry_run lub w odczytach live`.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C17).

### 2.9 `GET /api/v1/accounts/preferences`

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Tryb konta i dźwignie.

```jsonc
// 200 (liczby z przykładu dokumentacji, poglądowe)
{ "hedgingMode": false,
  "leverages": { "SHARES": {"current": 2, "available": [1,2,3,5]},
                 "CURRENCIES": {"current": 1, "available": [1,10,20,30]},
                 "INDICES": {"current": 10, "available": [1,10,20]},
                 "CRYPTOCURRENCIES": {"current": 1, "available": [1,2]},
                 "COMMODITIES": {"current": 5, "available": [1,5,10,20]} } }
```

`PUT /accounts/preferences` (ciało `leverages` i/lub `hedgingMode`) istnieje, ale **silnik go nie wywołuje**: zmiana dźwigni lub trybu to decyzja właściciela w panelu.

Cytat [S1]: "The `hedgingMode` parameter value shows whether the hedging mode is engaged." Spec §6 i pytanie 4 wymagają sprawdzenia trybu w próbie `smoke`: `hedgingMode: false` to tryb bez hedgingu; zakładamy, że oznacza agregację pozycji (drugie wejście na tym samym instrumencie zmienia istniejącą pozycję, jak zakłada spec §6). Dokumentacja nie opisuje zachowania szczegółowo (C15). Dźwignia dla indeksów (`leverages.INDICES.current`) a depozyt 5% z [S4]: zależność nie jest opisana (C16b).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C15, C16b).

### 2.10 `POST /api/v1/positions` (otwarcie pozycji ze stopem)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Limit: 1 żądanie na 0,1 s. Cytat [S1]: "Please note that when creating the position an order is created first with the 'o_' prefix in the `dealReference` parameter".

Ciało (pola z [S1]):

| Pole | Typ | Wymagane | Opis |
|---|---|---|---|
| `direction` | string | tak | `BUY` lub `SELL` |
| `epic` | string | tak | np. `DE40` |
| `size` | number | tak | wielkość transakcji (jednostki instrumentu, DE40: wielokrotność `minSizeIncrement`) |
| `guaranteedStop` | boolean | nie | domyślnie `false`; v1 nie używa |
| `trailingStop` | boolean | nie | domyślnie `false`; na DE40 niedostępny [S4] |
| `stopLevel` | number | nie | poziom ceny uruchomienia stopu |
| `stopDistance` | number | nie | "Distance between current and stop loss triggering price" |
| `stopAmount` | number | nie | kwota straty uruchamiająca stop |
| `profitLevel`, `profitDistance`, `profitAmount` | number | nie | analogicznie dla take profit (v1 nie używa) |

Ciało wejścia v1 (stop atomowo, spec §8):

```jsonc
{ "epic": "DE40", "direction": "BUY", "size": 0.001, "guaranteedStop": false, "trailingStop": false, "stopDistance": 30.0 }
```

Odpowiedź 200 niesie **wyłącznie** referencję:

```jsonc
{ "dealReference": "o_<uuid>" }
```

Cytat [S1]: "a successful response does not always mean that the position has been successfully opened. The status of the position can be confirmed using `GET /confirms/{dealReference}`". Więc HTTP 200 tu nie jest potwierdzeniem; stan zlecenia pozostaje `SUBMITTING` do potwierdzenia (spec §6). Brak klucza idempotencji od klienta: nasz `gws-...` identyfikator łączymy z `dealReference` wyłącznie w dzienniku.

Błędy 400: `error.invalid.stoploss.minvalue: <granica>`, `error.invalid.takeprofit.maxvalue: <granica>` (sekcja 1.4).

Uwagi do potwierdzenia:

- `stopDistance` liczony względem "current" ceny: nie wiadomo, czy względem notowania z chwili wysyłki, czy ceny wykonania; wynikowy poziom stopu odczytujemy po potwierdzeniu z pozycji (sekcja 2.11/2.12) i porównujemy z oczekiwanym: dla BUY `level − stopDistance`, dla SELL `level + stopDistance` (stop pozycji kupna leży poniżej ceny wykonania, stop pozycji sprzedaży powyżej). C6, `do potwierdzenia w dry_run lub w odczytach live`.
- Odległość stopu musi spełniać `dealingRules.minStopOrProfitDistance` i `maxStopOrProfitDistance` (sekcja 2.16) oraz najpewniej krok `minStepDistance`; sama reguła krok-stopu: C16c, `do potwierdzenia w dry_run lub w odczytach live`.
- Kody błędów dla niedoboru środków, wielkości poniżej minimum, niezgodnej z krokiem, rynku zamkniętego: C14, `do potwierdzenia w dry_run lub w odczytach live`.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C6, C14, C16c).

### 2.11 `GET /api/v1/positions` (lista pozycji, prymityw uzgadniania)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. "Returns all open positions for the active account".

```jsonc
// 200
{ "positions": [
  { "position": {
      "contractSize": 1,
      "createdDate": "2026-11-02T09:05:03.100",          // czas lokalny, NIE używać
      "createdDateUTC": "2026-11-02T08:05:03.100",       // czas otwarcia (UTC)
      "dealId": "<DEAL_ID>",                             // identyfikator pozycji
      "dealReference": "p_<DEAL_ID>",
      "workingOrderId": "<ORDER_ID>",                    // id zlecenia otwierającego: przypuszczalnie równe `dealId` z potwierdzenia (C2), NIE referencja o_...
      "size": 0.001,
      "leverage": 20,
      "upl": <number>,                                   // niezrealizowany wynik
      "direction": "BUY",
      "level": 25782.3,                                  // poziom otwarcia
      "currency": "EUR",
      "guaranteedStop": false
      // [S3, nie w przykładzie S1]: "trailingStop", "stopLevel", "stopDistance", "stopAmount",
      //                              "profitLevel", "profitDistance", "profitAmount"
    },
    "market": {
      "instrumentName": "<nazwa>", "expiry": "-", "marketStatus": "TRADEABLE", "epic": "DE40",
      "symbol": "<symbol>", "instrumentType": "INDICES", "lotSize": 1,
      "high": <number>, "low": <number>, "percentageChange": <number>, "netChange": <number>,
      "bid": 25780.3, "offer": 25782.3,
      "updateTime": "...", "updateTimeUTC": "2026-11-02T08:05:03.700",
      "delayTime": 0, "streamingPricesAvailable": true, "scalingFactor": 1, "marketModes": ["REGULAR"]
    } }
] }
```

Pola potrzebne silnikowi i ich status:

| Potrzebne | Pole | Status |
|---|---|---|
| identyfikator pozycji | `position.dealId` | udokumentowane |
| referencja | `position.dealReference` (przykłady: `"p_" + dealId`) | udokumentowane |
| kierunek | `position.direction` (`BUY` lub `SELL`) | udokumentowane |
| wielkość | `position.size` | udokumentowane |
| poziom otwarcia | `position.level` (w przykładach dla BUY równe `market.offer`, czyli cena kupna) | udokumentowane |
| czas otwarcia | `position.createdDateUTC` | udokumentowane |
| instrument | `market.epic` | udokumentowane |
| poziom stopu | `position.stopLevel` | do potwierdzenia w dry_run lub w odczytach live (C5; występuje w [S3], nie w oficjalnym przykładzie) |
| odległość stopu | `position.stopDistance` | do potwierdzenia w dry_run lub w odczytach live (C5) |
| brak stopu | `stopLevel` obecny i jawnie `null` (albo ponowny odczyt po `PUT /positions/{dealId}` nadal go nie pokazuje) daje `stop_level = None` i procedurę zabezpieczenia (spec §7). **Brak samego klucza w odpowiedzi nie oznacza braku stopu** | do potwierdzenia w dry_run lub w odczytach live (C5) |
| stop wyliczony (`derived`) | gdy kluczy stopu w ogóle nie ma w odpowiedzi `GET /positions` (tak jest w oficjalnych przykładach [S1]): `stop_level` = poziom wykonania (`confirms.level`) − `stopDistance` dla BUY lub + `stopDistance` dla SELL, gdzie `stopDistance` to wartość wysłana w `POST /positions`; adapter oznacza wartość jako `derived` i zapisuje ją w dzienniku. Weryfikacja: właściciel ogląda pozycję w aplikacji Capital.com (próba `smoke`) | do potwierdzenia w dry_run lub w odczytach live (C24) |

Brak pozycji: `{"positions": []}` (przypuszczalnie; kształt pustej listy w [S1] nieopisany, C5b). Reguła spec §7: zniknięcie dealId z listy = pozycja zamknięta; cenę i przyczynę ustala historia (sekcja 2.19). Koszt: 1 zapytanie na 10 s.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C5, C24).

### 2.12 `GET /api/v1/positions/{dealId}`

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametr ścieżki `dealId` ("Permanent deal reference for a confirmed trade"). Odpowiedź ma ten sam kształt co jeden element listy z 2.11 (`position` + `market`). Błąd 404: `error.not-found.dealId` (także dla pozycji zamkniętej, przypuszczalnie; C5c). Służy do weryfikacji pozycji zaraz po potwierdzeniu wejścia: kontrola krzyżowa `position.workingOrderId == confirm.dealId` (C2) oraz kontrola stopu z regułą `derived` z sekcji 2.11 (C5, C24).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C2, C5, C24).

### 2.13 `PUT /api/v1/positions/{dealId}` (ustawienie lub zmiana stopu)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Ciało (pola jak przy `POST /positions`, wszystkie opcjonalne): `guaranteedStop`, `trailingStop`, `stopLevel`, `stopDistance`, `stopAmount`, `profitLevel`, `profitDistance`, `profitAmount`.

Do ustawienia stopu (procedura zabezpieczenia pozycji bez stopu, spec §7; `set_stop(deal_id, stop_level)`):

```jsonc
{ "stopLevel": 25750.0 }
```

```jsonc
// 200
{ "dealReference": "p_<DEAL_ID>" }
// 400 przykłady
{ "errorCode": "error.invalid.stoploss.maxvalue: <granica>" }
{ "errorCode": "error.invalid.takeprofit.minvalue: <granica>" }
// 404
{ "errorCode": "error.not-found.dealId" }
```

Jak przy otwarciu, 200 niesie tylko referencję (`dealReference` z treści odpowiedzi; nie budować jej ręcznie z `dealId`); wynik potwierdza `GET /confirms/{dealReference}` z tą referencją lub ponowny odczyt pozycji. Czy pominięcie `profit*` kasuje istniejący take profit i czy `PUT` ze `stopLevel` zachowuje pozostałe ustawienia: C5d (v1 nie ustawia take profit, więc ryzyko jest małe). Modyfikacje pojawiają się w historii aktywności jako typ `EDIT_STOP_AND_LIMIT`.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C5d).

### 2.14 `DELETE /api/v1/positions/{dealId}` (zamknięcie pozycji)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Bez ciała i bez parametrów w [S1]; zamyka pozycję w całości (brak opisanego parametru wielkości, więc zamknięcia częściowe są nieobsługiwane).

```jsonc
// 200
{ "dealReference": "p_<DEAL_ID>" }
// 404
{ "errorCode": "error.not-found.dealId" }
```

Zamknięcie następuje przypuszczalnie po cenie rynkowej; 200 niesie tylko referencję (`dealReference` z treści odpowiedzi `DELETE`; nie budować jej ręcznie z `dealId`), więc po wywołaniu: `GET /confirms/{dealReference}` z tą referencją oraz kontrola, że `dealId` zniknął z listy pozycji (spec §6: stan pozycji zmienia się tylko na podstawie odpowiedzi brokera). Przy przekroczeniu czasu oczekiwania nie wysyłamy ponownie na ślepo (spec §8): najpierw lista pozycji. Odpowiedź potwierdzenia dla zamknięcia (pola `status`, `level`, ewentualny wynik) nie jest opisana (C3). Zachowanie przy rynku zamkniętym (kod błędu, kolejkowanie): C11b. Czy `DELETE` zamyka pozycję w całości: C22.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C3, C11b, C22).

### 2.15 `GET /api/v1/confirms/{dealReference}` (potwierdzenie po `dealReference`)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametr ścieżki: `dealReference` z `POST`/`PUT`/`DELETE` (prefiks `o_` dla zlecenia otwarcia, `p_` dla operacji na pozycji). Cytat [S1]: "In case of mentioning the order prefix formed because of the position creation the opened positions IDs will be shown in the `affectedDeals` array".

```jsonc
// 200 (kształt z przykładu dokumentacji dla otwarcia)
{
  "date": "2026-11-02T08:05:03.100",           // czas; strefa nieopisana, nie używać do logiki
  "status": "OPEN",
  "dealStatus": "ACCEPTED",
  "epic": "DE40",
  "dealReference": "o_<uuid>",
  "dealId": "<ORDER_ID>",                       // w przykładzie to id zlecenia, nie pozycji
  "affectedDeals": [ { "dealId": "<DEAL_ID>", "status": "OPENED" } ],   // id pozycji
  "level": 25782.3,                             // cena wykonania
  "size": 0.001,
  "direction": "BUY",
  "guaranteedStop": false,
  "trailingStop": false
  // [S3, nie w przykładzie S1]: "reason", "stopLevel", "profitLevel"
}
// 404
{ "errorCode": "error.not-found.dealReference" }
```

Wnioski do potwierdzenia (wszystkie z przykładów i z [S3], nie ze zdania dokumentacji):

- **Identyfikator pozycji.** W przykładzie `dealId` potwierdzenia (kończy się na `...43`) różni się od `affectedDeals[0].dealId` (`...45`), a przykład pozycji ma `workingOrderId` `...5c` i `dealId` `...5e`, a więc ta sama relacja "zlecenie, pozycja = zlecenie + 2". Wniosek: `dealId` pozycji (do `GET`/`PUT`/`DELETE /positions/{dealId}`) to `affectedDeals[i].dealId` ze statusem `OPENED`, nie główne `dealId` potwierdzenia, a `position.workingOrderId` jest równe `dealId` potwierdzenia (to identyfikator zlecenia, nie jego referencja `o_...`). C2, `do potwierdzenia w dry_run lub w odczytach live`. Adapter zapisuje w dzienniku oba identyfikatory i do `OpenPosition.deal_id` bierze `affectedDeals`. **Główna kontrola krzyżowa:** odczyt `GET /positions/{kandydat}` i sprawdzenie, że `position.workingOrderId == confirm.dealId`. Heurystyka zapasowa (gdy tego pola brak albo kandydatów jest kilku): dopasowanie po `list_positions()` według kierunku, wielkości i czasu otwarcia.
- **Wartości `status` i `dealStatus`.** [S1] pokazuje tylko `status: "OPEN"` i `dealStatus: "ACCEPTED"`. Odrzucenie to według [S3] `dealStatus: "REJECTED"` z polem `reason` (tekst). Pełna lista wartości: C4, `do potwierdzenia w dry_run lub w odczytach live`.
- **Wyścig.** Potwierdzenie tuż po `POST` może jeszcze zwrócić 404 `error.not-found.dealReference` (konektor [S3] ponawia do 6 razy z odstępem 150 ms razy numer próby). Adapter (Zadanie 13) nie ponawia: dokładnie ten kształt 404 daje `confirm(ref) = None`, a menedżer zleceń przechodzi do `UNKNOWN` i rozstrzyga ponownym `confirm` i listą pozycji (spec §6). Każde inne 404, 4xx lub 5xx to `BrokerError`, nigdy `None` ani odrzucenie. C1, `do potwierdzenia w dry_run lub w odczytach live`.
- **Zamknięcie.** Potwierdzenie zamknięcia pobieramy dla `dealReference` zwróconego w treści odpowiedzi `DELETE`: pola nieopisane (C3).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C1, C2, C3, C4).

### 2.16 `GET /api/v1/markets/{epic}` (reguły instrumentu, status rynku, notowanie, godziny otwarcia)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametr ścieżki `epic` (np. `DE40`). Błąd 404: `error.not-found.epic`.

```jsonc
// 200 (kształt z przykładu dokumentacji; wartości DE40 z [S4] gdzie znane)
{
  "instrument": {
    "epic": "DE40", "symbol": "<symbol>", "expiry": "-", "name": "<nazwa>",
    "lotSize": <number>,                 // DE40: nieodczytane w [S4]; oczekiwane 1 (C16)
    "type": "INDICES",
    "guaranteedStopAllowed": <bool>, "streamingPricesAvailable": true,
    "currency": "EUR",
    "marginFactor": 5, "marginFactorUnit": "PERCENTAGE",   // depozyt 5% wg [S4]; nazwa pola z przykładu [S1]
    "openingHours": { "mon": ["00:00 - 21:59", "23:05 - 00:00"], "tue": [...], "wed": [...], "thu": [...],
                      "fri": ["00:00 - 21:59"], "sat": [], "sun": ["23:05 - 00:00"], "zone": "UTC" },
    "overnightFee": { "longRate": <number>, "shortRate": <number>,
                      "swapChargeTimestamp": 1707948000000, "swapChargeInterval": 1440 }
  },
  "dealingRules": {             // `unit` z przykładu [S1]; `value` dla DE40 z [S4]
    "minStepDistance":           { "unit": "POINTS",     "value": 0.1 },
    "minDealSize":               { "unit": "POINTS",     "value": 0.001 },
    "maxDealSize":               { "unit": "POINTS",     "value": 1000 },
    "minSizeIncrement":          { "unit": "POINTS",     "value": 0.001 },
    "minGuaranteedStopDistance": { "unit": "PERCENTAGE", "value": 0.1 },
    "minStopOrProfitDistance":   { "unit": "PERCENTAGE", "value": 0.01 },
    "maxStopOrProfitDistance":   { "unit": "PERCENTAGE", "value": <number> },
    "marketOrderPreference": "AVAILABLE_DEFAULT_ON",
    "trailingStopsPreference": "NOT_AVAILABLE"
  },
  "snapshot": {
    "marketStatus": "TRADEABLE",
    "netChange": <number>, "percentageChange": <number>,
    "updateTime": "...",                 // czas lokalny
    "delayTime": 0,
    "bid": 25780.3, "offer": 25782.3, "high": <number>, "low": <number>,
    "decimalPlacesFactor": <number>, "scalingFactor": 1, "marketModes": ["REGULAR"]
    // [S3, nie w przykładzie S1]: "updateTimeUTC"
  }
}
```

#### 2.16.1 Gdzie leżą pola reguł (Zadanie 4: `InstrumentRules`)

| Potrzebne | Ścieżka w odpowiedzi | Jednostka / uwagi | Status |
|---|---|---|---|
| minimalna wielkość | `dealingRules.minDealSize.value` | DE40: 0,001 [S4]. Pole `unit` ma w przykładach wartość `"POINTS"` także dla wielkości; traktować jako jednostki wielkości, nie punkty ceny | udokumentowane |
| maksymalna wielkość | `dealingRules.maxDealSize.value` | DE40: 1000 [S4] | udokumentowane |
| krok wielkości | `dealingRules.minSizeIncrement.value` | DE40: 0,001 [S4]. W oficjalnej specyfikacji nie ma pola `dealSize.increment`; właściwa nazwa to `minSizeIncrement` | udokumentowane |
| krok wielkości (reguła) | wielkość = `minDealSize` + k × krok; odrzucenie wielkości niezgodnej z krokiem | reguła wnioskowana z nazwy pola, kod błędu nieznany (C14) | do potwierdzenia w dry_run lub w odczytach live |
| minimalna odległość stopu | `dealingRules.minStopOrProfitDistance` (`unit`, `value`) | DE40: `PERCENTAGE` 0,01 [S4], czyli 0,01% ceny (ok. 2,6 pkt przy cenie 25 780). Przy `unit == "PERCENTAGE"`: odległość w punktach = cena × `value` / 100; przy `"POINTS"`: `value` | udokumentowane |
| maksymalna odległość stopu | `dealingRules.maxStopOrProfitDistance` | jak wyżej (w przykładzie 60%) | udokumentowane |
| minimalna odległość stopu gwarantowanego | `dealingRules.minGuaranteedStopDistance` | DE40: 0,1% [S4]; v1 nie używa GSL | udokumentowane |
| krok odległości | `dealingRules.minStepDistance` | DE40: 0,1 pkt [S4]; odległość stopu zaokrąglać do wielokrotności tego kroku | do potwierdzenia w dry_run lub w odczytach live (C16c: czy broker odrzuca niewielokrotności) |
| depozyt (margin) | `instrument.marginFactor` + `instrument.marginFactorUnit` | nazwa pola i jednostki z przykładu [S1]; `margin_rate = marginFactor / 100`, gdy jednostka to `PERCENTAGE` | udokumentowane |
| depozyt DE40 (wartość) | `instrument.marginFactor` dla `DE40` | odczyt [S4] podaje "depozyt 5%" bez nazwy pola; oczekiwane `marginFactor` = 5 i `PERCENTAGE` (C25) | do potwierdzenia w dry_run lub w odczytach live |
| wartość punktu | **brak pola** | Wartość 1 punktu = `size` × `lotSize` w walucie instrumentu. Dla DE40 oczekujemy `lotSize` = 1 i waluty EUR, czyli 1 EUR na punkt na jednostkę wielkości. Przykład [S2]: pozycja po "€10 per point" ma ekspozycję 16 000 × €10 = €160 000. Wielkość 0,001 to ok. 26 EUR wartości nominalnej [S4], co jest zgodne z 1 EUR na punkt | do potwierdzenia w dry_run lub w odczytach live (C16: wartość pochodna, sprawdzić z `upl` otwartej pozycji) |
| status rynku | `snapshot.marketStatus` | `TRADEABLE` (przykład [S1]) i `CLOSED` (przykład listy `GET /markets` oraz odczyt weekendowy [S4]) | udokumentowane |
| status rynku (pełna lista) | `snapshot.marketStatus` | trzecia wartość `EDITS_ONLY` występuje wyłącznie w typach niezależnej biblioteki; pełna lista wartości nieznana (C11) | do potwierdzenia w dry_run lub w odczytach live |
| tryby rynku | `snapshot.marketModes` | `["REGULAR"]`, w przykładzie zamkniętego rynku `["VIEW_ONLY"]` | do potwierdzenia w dry_run lub w odczytach live (C11) |
| zlecenie rynkowe dostępne | `dealingRules.marketOrderPreference` | DE40: `AVAILABLE_DEFAULT_ON` [S4] | udokumentowane |
| stop trailing | `dealingRules.trailingStopsPreference` | DE40: `NOT_AVAILABLE` [S4] | udokumentowane |
| waluta instrumentu | `instrument.currency` | DE40: EUR [S4] | udokumentowane |
| finansowanie nocne | `instrument.overnightFee.{longRate,shortRate,swapChargeTimestamp,swapChargeInterval}` | `swapChargeInterval` w przykładzie 1440 (minuty); znacznik w ms epoki | udokumentowane |
| finansowanie nocne DE40 (godzina) | `swapChargeTimestamp` dla `DE40` | strona rynku w [S2] podaje godzinę naliczenia 21:00 UTC; zgodność z polem API nieznana (C26) | do potwierdzenia w dry_run lub w odczytach live |

Mapowanie na `InstrumentRules(epic, min_size, size_step, value_per_point, margin_rate, min_stop_distance)` (Zadanie 4): `min_size` ← `minDealSize.value`; `size_step` ← `minSizeIncrement.value`; `value_per_point` ← `lotSize` (oczekiwane 1 dla DE40, do odczytu: C16); `margin_rate` ← `marginFactor / 100`; `min_stop_distance` ← punkty z `minStopOrProfitDistance` przeliczone na aktualnej cenie (`cena × 0,0001` dla DE40, w fixture 2,6). Ponieważ minimum zależy od ceny, adapter przelicza je przy każdym odczycie reguł (raz na sesję wystarczy; cena zmienia się o ułamki procenta w ciągu dnia, więc dodajemy margines bezpieczeństwa, np. wybraną stopę w konfiguracji strategii).

#### 2.16.2 Notowanie (bid, ask, znacznik czasu) przez REST

Silnik v1 odpytuje REST co ok. 5 s (strumień WebSocket odroczony, sekcja 2.21). Źródło notowania: `GET /markets/{epic}` → `snapshot.bid` (cena sprzedaży), `snapshot.offer` (cena kupna; w strumieniu WebSocket to samo pole nazywa się `ofr`), `snapshot.updateTime`. Status pól `bid`, `offer`, `marketStatus` (przykład w [S1]): `udokumentowane`.

Znacznik czasu notowania:

- Przykład odpowiedzi `GET /markets/{epic}` ma w `snapshot` tylko `updateTime` (czas lokalny). Wariant `GET /markets?searchTerm=` oraz obiekt `market` w `GET /positions` mają `updateTime` i `updateTimeUTC`; konektor [S3] ma `updateTimeUTC` także w `snapshot`. Wniosek: **spodziewane pole `snapshot.updateTimeUTC`**, ale nie ma go w oficjalnym przykładzie. C10, `do potwierdzenia w dry_run lub w odczytach live`.
- Reguła dla `Quote.ts` i straży ciszy (do czasu rozstrzygnięcia C10): `Quote.ts` = chwila zegara silnika, w której obserwowana krotka (`bid`, `offer`, `snapshot.updateTime` oraz `updateTimeUTC`, jeśli jest) **ostatnio się zmieniła** (przy pierwszym odczycie: teraz). Dzięki temu zamrożona migawka z `marketStatus == "TRADEABLE"`, która wciąż zwraca HTTP 200, uruchamia straż "brak świeżego notowania w 60 s". Przy `CLOSED` notowanie jest ostatnią migawką [S4], więc nie jest "świeże". Gdy potrzebny jest udokumentowany czas UTC notowania, awaryjnym źródłem jest `GET /markets?searchTerm=DE40`, którego elementy mają `updateTimeUTC` w przykładzie [S1] (C23).
- `snapshot.delayTime` (w przykładach 0): jednostka i znaczenie nieopisane (C10).

Alternatywne, tańsze lub dokładniejsze źródła notowań: `GET /markets?epics=DE40` zwraca `marketDetails[]` o tym samym kształcie co ten endpoint (sekcja 2.17); `GET /positions` niesie `market.bid/offer/updateTimeUTC` dla otwartych pozycji (przydatne do straży poślizgu bez dodatkowego zapytania).

#### 2.16.3 Godziny otwarcia DE40 (`instrument.openingHours`)

Pole: `instrument.openingHours`, obiekt z kluczami `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun` (każdy to lista napisów `"HH:MM - HH:MM"`, pusta lista = zamknięte) oraz `zone` (w przykładzie `"UTC"`). Format z przykładu [S1] (instrument SILVER, nie DE40):

```jsonc
"openingHours": { "mon": ["00:00 - 21:59", "23:05 - 00:00"], "tue": [...], "wed": [...], "thu": [...],
                  "fri": ["00:00 - 21:59"], "sat": [], "sun": ["23:05 - 00:00"], "zone": "UTC" }
```

Struktura pola jest w przykładzie [S1]. **Rzeczywiste wartości dla DE40 nie były odczytane**: odczyt z 2026-11-01 [S4] ich nie zapisał, a dokumentacja nie podaje ich dla DE40 (C12). Status wartości: `do potwierdzenia w dry_run lub w odczytach live`; przy pierwszym `dry_run` zapisać do dziennika surowe `instrument.openingHours` dla `DE40` i sprawdzić, czy `zone` to `UTC`.

Informacje pomocnicze [S2], nie kontrakt API: od 1 czerwca 2025 dzienna przerwa na Germany 40 skrócona do 5 minut (17:00 do 17:05 czasu Nowego Jorku), czyli 21:00 do 21:05 UTC przy czasie letnim USA i 22:00 do 22:05 UTC przy zimowym; handel od niedzieli wieczór do piątku wieczór. Cytat: "The usual one-hour daily break (5:00pm-6:00pm ET) has now been reduced to just five minutes (5:00-5:05pm ET)". Okno strategii v1 (09:00 do 17:25 Berlin) mieści się daleko od przerwy, więc godziny otwarcia z API służą diagnostyce, a **bramką wejścia jest `snapshot.marketStatus == "TRADEABLE"`** (plus kalendarz XETRA ze spec §4).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C10, C11, C12, C16, C25, C26).

### 2.17 `GET /api/v1/markets` (lista i wyszukiwanie)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametry: `searchTerm` (tekst) albo `epics` (lista rozdzielona przecinkami, maksymalnie 50). Gdy podano oba, liczy się tylko `searchTerm`. Bez parametrów zwraca wszystkie rynki.

- Z `searchTerm`: `{"markets": [ { "epic", "symbol", "instrumentName", "instrumentType", "lotSize", "expiry", "marketStatus", "bid", "offer", "high", "low", "netChange", "percentageChange", "updateTime", "updateTimeUTC", "delayTime", "streamingPricesAvailable", "scalingFactor", "marketModes", "pipPosition", "tickSize" } ]}`.
- Z `epics`: `{"marketDetails": [ { "instrument": {...}, "dealingRules": {...}, "snapshot": {...} } ]}` (ten sam kształt co sekcja 2.16, tablica).
- Błędy 400: `error.null.searchTerm`, `error.null.epic`.

Użycie w v1: jednorazowe potwierdzenie, że `epic` `DE40` istnieje (`epics=DE40`); poza tym `GET /markets/{epic}`. Pola `pipPosition` i `tickSize` (liczba miejsc i krok ceny) są tylko w wariancie z `searchTerm`; krok ceny DE40 jest też pośrednio w `minStepDistance`. Nie wiadomo, czy `marketDetails[]` z `epics=` ma ten sam `snapshot` (w tym `updateTimeUTC`) co `GET /markets/{epic}` i czy `searchTerm` zwraca `updateTimeUTC` także dla DE40: C23.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C23).

### 2.18 `GET /api/v1/prices/{epic}` (historia cen: świece 5-minutowe i dzienne)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametry zapytania, wszystkie opcjonalne [S1]:

| Parametr | Typ | Opis i domyślne |
|---|---|---|
| `resolution` | string | `MINUTE`, `MINUTE_5`, `MINUTE_15`, `MINUTE_30`, `HOUR`, `HOUR_4`, `DAY`, `WEEK`; domyślnie `MINUTE` |
| `max` | integer | maksymalna liczba wartości w odpowiedzi; domyślnie 10, maksimum 1000 |
| `from` | string | początek, `YYYY-MM-DDTHH:MM:SS` (UTC, filtracja po `snapshotTimeUTC`) |
| `to` | string | koniec, ten sam format |

Cytat [S1]: "By default returns the minute prices within the last 10 minutes". Odpowiedź (kształt z przykładu; ceny poglądowe):

```jsonc
// 200
{ "prices": [
  { "snapshotTime": "...",                         // czas lokalny, NIE używać
    "snapshotTimeUTC": "2026-11-02T08:00:00",      // czas świecy (UTC)
    "openPrice":  { "bid": 25780.3, "ask": 25782.3 },
    "closePrice": { "bid": ..., "ask": ... },
    "highPrice":  { "bid": ..., "ask": ... },
    "lowPrice":   { "bid": ..., "ask": ... },
    "lastTradedVolume": 187 }
  ],
  "instrumentType": "INDICES" }
// 400
{ "errorCode": "error.invalid.resolution" }
```

Format znaczników: **`snapshotTimeUTC` jest czasem UTC** (pole jest podstawą filtracji `from`/`to`), napis bez sufiksu strefy i bez milisekund (`YYYY-MM-DDTHH:MM:SS`). Ceny świecy są podane osobno dla strony sprzedaży (`bid`) i kupna (`ask`); pola `openPrice`, `closePrice`, `highPrice`, `lowPrice` są obiektami `{bid, ask}`. Mid dla `Candle` (Zadanie 4) liczymy jako `(bid + ask) / 2` osobno dla każdego z pól O, H, L, C; to przybliżenie (maksimum mid nie jest średnią maksimów bid i ask), dopuszczalne w v1. Niezależna biblioteka `capital-api-client@1.0.7` dodaje do obiektów cen pole `lastTraded` (nullable); w [S1] go nie ma.

#### 2.18.1 Świece 5-minutowe (sygnał i zapasowy odczyt)

Zapytanie dla pierwszej świecy sesji (zimą Berlin = UTC+1, więc 09:00 Berlin = 08:00 UTC; konwersja przez `zoneinfo`, nie stałą):

```text
GET /api/v1/prices/DE40?resolution=MINUTE_5&from=2026-11-02T08:00:00&to=2026-11-02T08:04:58&max=2
```

Przykład to odczyt zakresu otwarcia o 08:05:03 UTC (09:05:03 w Berlinie): `to` jest **obcięte do teraz − 5 s** (`CANDLE_TO_SKEW_MARGIN_S`, stała procedury; bez obcięcia byłoby 08:05:00), a `max` wynika z obciętego okna (liczba okresów + 2). Stempel 08:00:00 pierwszej świecy mieści się między `from` a `to`, więc świeca zakończona o 08:05:00 jest dostępna w odczycie od 09:05:00 Berlin (silnik ponawia odczyt do terminu wejścia). Wcześniejszy przykład (`to=08:10:00`, czyli w przyszłości względem odczytu) odpadł: zegar silnika może wyprzedzać zegar brokera, a czy broker odrzuca `to` z przyszłości, nie wiadomo (C9).

Opisane w [S1]: parametry, `resolution=MINUTE_5`, `snapshotTimeUTC` jako podstawa filtracji, limit 1000. Przykład odpowiedzi "Use not default resolution, start date and max parameters" (dokładne żądanie nie jest podane; z odstępów wynika `MINUTE_15`, a parametry przykładowe to `from=2022-02-24T00:00:00` i `max`) zwraca cztery świece o `snapshotTimeUTC` `00:00:00`, `00:15:00`, `00:30:00`, `00:45:00`. Z tego **wnioskuję**, że znacznik to **początek** świecy (pierwsza świeca ma czas równy `from`). Czy `to` jest włączne, nie wiadomo. To wniosek z jednego przykładu, więc C9, `do potwierdzenia w dry_run lub w odczytach live`.

Zasady bezpieczne do czasu potwierdzenia (Zadanie 13/14):

- Świecę pierwszą uznajemy za gotową dopiero, gdy jest w odpowiedzi element o `snapshotTimeUTC` równym oczekiwanemu początkowi, a zegar silnika jest ≥ początek + 5 minut. Zapytanie o szerszy zakres i wybór po dokładnym `snapshotTimeUTC` zamiast polegania na `max`.
- Nie wiadomo, czy odpowiedź zawiera świecę trwającą (niedomkniętą); filtrujemy: tylko świece z początkiem + 5 min ≤ teraz.
- Odpowiedź bez elementu dla oczekiwanej świecy do 09:10 Berlin = `NO_SIGNAL_DATA` (plan, Zadania 11 i 14).
- Zakres żądany przez silnik jest półotwarty [start, end), a wynik filtrowany lokalnie po `snapshotTimeUTC`. **Limit 1000 punktów (`max`) sprawdza się na ŻĄDANYM oknie**, nie na obciętym: `ValueError` nie zależy od zegara (jest też dla okna w całości w przyszłości). Okno puste po obcięciu `to` daje pustą listę bez zapytania.
- Przy odwróconej semantyce stempla (`CANDLE_STAMP_IS_PERIOD_START = False`, C9) świeca kończąca się dokładnie w chwili odczytu przychodzi w odczycie o ok. 5 s późniejszym (margines `to`); silnik ponawia do terminu wejścia, więc to kosztuje kilka sekund, a nie sygnał.
- Opóźnienie dostępności świecy po jej zamknięciu: C9, `do potwierdzenia w dry_run lub w odczytach live`.

#### 2.18.2 Świece dzienne (ATR14)

```text
GET /api/v1/prices/DE40?resolution=DAY&max=30
GET /api/v1/prices/DE40?resolution=DAY&from=2026-10-01T00:00:00&to=2026-11-02T00:00:00&max=30
```

`resolution=DAY` jest w liście udokumentowanych. **Nieudokumentowane i do ustalenia w `dry_run` (C9):** (a) granica doby dziennej świecy DE40 (00:00 UTC? zamknięcie sesji CFD?) widoczna w `snapshotTimeUTC` pierwszej świecy dnia; (b) czy ostatnia świeca w odpowiedzi to dzień trwający (wtedy odrzucamy ją przy ATR); (c) co zwraca zapytanie z samym `max` bez `from`/`to` (domyślny zakres "ostatnie 10 minut" z [S1] dotyczy minut, nie dni); (d) czy dni bez handlu (weekend) są pomijane. ATR14 wymaga 15 zamkniętych świec dziennych (14 zakresów + poprzedni close); `count` w `daily_candles(epic, count)` żąda zapasu (np. 30) i filtruje.

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C9).

### 2.19 `GET /api/v1/history/activity` (historia aktywności: cena i przyczyna zamknięcia)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametry, wszystkie opcjonalne [S1]:

| Parametr | Typ | Opis |
|---|---|---|
| `from`, `to` | string | `YYYY-MM-DDTHH:MM:SS`, filtracja po `dateUTC`. **Maksymalny zakres między `from` i `to` to 1 dzień**; jeśli podano tylko jedno, wybierany jest zakres 1-dniowy |
| `lastPeriod` | integer | okno w sekundach wstecz od teraz (nie działa z `from`/`to`); domyślnie 600, maksimum 86400 |
| `detailed` | boolean | `true` dodaje obiekt `details`; domyślnie `false` |
| `dealId` | string | tylko aktywność dla danego `dealId` |
| `filter` | string | filtr FIQL po `epic`, `source`, `status`, `type`, np. `source!=DEALER;type!=POSITION;status==REJECTED;epic==OIL_CRUDE,GOLD` (`;` = i, `,` = lub) |

Cytat [S1] (changelog 2023-07-04): "Set maximum date range for parameters `from`, `to`, `lastPeriod` to 1 day for the `GET /history/activity`". Wartości wyliczeniowe [S1]:

| Pole | Wartości |
|---|---|
| `source` | `CLOSE_OUT`, `DEALER`, `SL`, `SYSTEM`, `TP`, `USER` |
| `status` | `ACCEPTED`, `CREATED`, `EXECUTED`, `EXPIRED`, `REJECTED`, `MODIFIED`, `MODIFY_REJECT`, `CANCELLED`, `CANCEL_REJECT`, `UNKNOWN` |
| `type` | `POSITION`, `WORKING_ORDER`, `EDIT_STOP_AND_LIMIT`, `SWAP`, `SYSTEM` |

Odpowiedź (z `detailed=true`, `dealId` filtrowane; kształt z przykładu [S1], wartości poglądowe):

```jsonc
// 200
{ "activities": [
  { "date": "...", "dateUTC": "2026-11-02T10:12:00.420",
    "epic": "DE40", "dealId": "<DEAL_ID>",
    "source": "SL", "type": "POSITION", "status": "ACCEPTED",
    "details": { "dealReference": "p_<DEAL_ID>", "marketName": "<nazwa>", "currency": "EUR",
                 "size": 0.001, "direction": "SELL", "level": 25750.0, "guaranteedStop": false }
    // [S3, nie w przykładzie S1] dodatkowe pola w "details": "goodTillDate", "trailingStop", "stopLevel",
    //                            "stopDistance", "stopAmount", "profitLevel", "profitDistance", "profitAmount"
  },
  { "date": "...", "dateUTC": "2026-11-02T08:05:03.100", "epic": "DE40", "dealId": "<DEAL_ID>",
    "source": "USER", "type": "POSITION", "status": "ACCEPTED",
    "details": { "dealReference": "p_<DEAL_ID>", "size": 0.001, "direction": "BUY", "level": 25782.3, ... } }
] }
// 400
{ "errorCode": "error.invalid.from" }
```

**Gdzie są cena i przyczyna zamknięcia:**

| Potrzebne | Pole | Status |
|---|---|---|
| cena zamknięcia | `activities[].details.level` we wpisie typu `POSITION`, status `ACCEPTED`, o kierunku `details.direction` przeciwnym do kierunku pozycji, z `dateUTC` późniejszym niż otwarcie; dostępne tylko przy `detailed=true` | do potwierdzenia w dry_run lub w odczytach live (C7) |
| przyczyna zamknięcia | `activities[].source`: `SL` = stop loss (przypuszczalnie), `TP` = take profit, `USER` = polecenie użytkownika/API, `CLOSE_OUT` = zamknięcie przez system (np. brak depozytu), `DEALER`, `SYSTEM`; wartości wyliczenia są wymienione w [S1] | **przypisanie `SL` do zadziałania stopu i `USER` do ręcznego/API zamknięcia: do potwierdzenia w dry_run lub w odczytach live** (C7) |
| czas zamknięcia | `activities[].dateUTC` | udokumentowane (nazwa pola) |
| identyfikator pozycji | `activities[].dealId` (ten sam dla otwarcia i zamknięcia w przykładzie) | udokumentowane |
| referencja | `details.dealReference` (`p_<dealId>`) | udokumentowane |
| rozmiar zamknięcia | `details.size` | udokumentowane |

Dlaczego tylko "przypuszczalnie": dokumentacja wymienia wartości `SL` i `TP` w wyliczeniu `source`, ale **nie mówi wprost**, że zadziałanie stopu daje `source: "SL"`. Jedyny przykład pełnego cyklu (otwarcie `USER`, zamknięcie `CLOSE_OUT`, odrzucona edycja `SYSTEM`) nie pokazuje stopu. Reguła mapowania dla Zadań 4 i 8 (`ClosedDeal.reason`):

- `source == "SL"` lub `"TP"` → `"STOP"` (v1 nie ustawia take profit).
- `source == "USER"` → `"MANUAL"` (polecenie użytkownika w aplikacji albo nasze `DELETE` przez API; czy zamknięcie było nasze, rozstrzyga silnik po stanie `EXITING`, nie adapter).
- `source` `CLOSE_OUT`, `DEALER`, `SYSTEM` → `"FORCED"`.
- `source` spoza wyliczenia → `"FORCED"` (nigdy `"STOP"`) i licznik `unknown_close_sources` adaptera, na którym alarmuje Watchdog (Zadanie 14).
- Dodatkowa weryfikacja stopu (dla silnika, nie adaptera): jeśli `details.level` jest w pobliżu lokalnie zapisanego poziomu stopu (±spread), to zamknięcie przez stop jest wiarygodne nawet gdy `source` nie jest `SL`.

**Jak adapter odróżnia otwarcie od zamknięcia (C7d).** Wiersz otwarcia i wiersz zamknięcia mają ten sam typ `POSITION`, status `ACCEPTED` i ten sam `dealId`; różni je tylko `details.direction` (zamknięcie ma kierunek przeciwny do pozycji, a źródło otwarcia przez API to `USER`). Adapter zna stronę pozycji z własnej pamięci (nasze potwierdzenia, ostatnia lista pozycji), z wiersza zamknięcia systemowego (źródło inne niż `USER`: jego kierunek jest przeciwny do pozycji) albo z pary wierszy `USER`, w której najwcześniejszy jako jedyny ma swój kierunek (to otwarcie). Gdy to nie wystarcza, czyta świeżą listę pozycji, a potem historię tego jednego deala wstecz od `since` (`dealId=<id>`, okna ≤ 1 doba, najnowsze najpierw, do granicy 14 dni od teraz, wspólny limit zapytań), aż znajdzie wcześniejszy wiersz, który rozstrzyga stronę (zwykle otwarcie w kierunku przeciwnym; tak obca pozycja otwarta przed `since` i zamknięta przez właściciela, gdy silnik nie działał, nie blokuje na stałe naszych zamknięć). Odpowiedź z filtrem `dealId`, w której jest wiersz innego deala (albo bez `dealId`), znaczy, że filtr nie zadziałał → `BrokerError` (jak wiersz spoza okna). Założenie dotyczy KAŻDEGO wiersza odpowiedzi, także typów `SWAP` i `SYSTEM`, które normalnie pomijamy: sprawdzenie `dealId` jest przed filtrem typów (C27). Koszt pełnego wyszukiwania wstecz: do 14 okien, czyli z oknem głównym około 15 zapytań i około 3 s (odstęp 0,2 s) na każdy nierozstrzygnięty deal przy pierwszym wywołaniu. Potem wynik okna **w całości przed `since`** (koniec okna < `since`, czyli co najmniej ok. doby przed teraz: późne wiersze już tam nie dochodzą) jest zapamiętywany w ograniczonej pamięci adaptera (najwyżej 64 okna, klucz: rachunek sesji, `dealId`, granice okna), więc kolejne odczyty z tym samym `since` nie powtarzają tych zapytań i nie zużywają ich limitu. Doba bezpośrednio przed `since` jest zawsze czytana od nowa, błędy i niepełne wyniki nie są zapamiętywane, a późny wiersz w zapamiętanym pustym oknie skończyłby się `HISTORY_AMBIGUOUS`, nie błędnym księgowaniem. Jeśli deal nadal jest nierozstrzygnięty, `closed_since` rzuca `BrokerError("HISTORY_AMBIGUOUS…")` zamiast zgadywać (np. samotny wiersz `USER`, którego otwarcia nie ma w 14 dniach, albo otwarcie, którego zamknięcie jeszcze nie trafiło do historii); wyczerpany limit zapytań → `HISTORY_TRUNCATED`. Sprzeczne wiersze → `HISTORY_INCONSISTENT`, także wtedy, gdy po rzekomym zamknięciu pojawia się wiersz w kierunku pozycji (np. otwarcie przez API ze źródłem innym niż `USER`: inaczej po restarcie otwarcie zostałoby zaksięgowane jako wyjście).

**Uwaga operacyjna.** Pozycja właściciela trzymana dłużej niż 14 dni i zamknięta przez niego, gdy silnik nie działał, ma otwarcie poza zasięgiem wyszukiwania wstecz: dla naszego stałego `since` (otwarcie naszej pozycji − 1 min) każde `closed_since` rzuca wtedy `HISTORY_AMBIGUOUS`, aż `since` przesunie się za jej wiersz zamknięcia. Uzgadnianie i spłaszczenie pozostają zablokowane (alarm Watchdoga), a rozstrzyga właściciel: sprawdza w aplikacji, czy nasza pozycja jest zamknięta, i księguje ją ręcznie.

**Kompletność (C27).** `closed_since(since)` zwraca wszystkie zamknięcia od `since` albo rzuca: zakres dłuższy niż 14 dni → `HISTORY_RANGE_TOO_LARGE` przed zapytaniem; okna `from`/`to` po najwyżej 1 dobie, sąsiednie zachodzą o 1 s (włączność granic nieopisana), `from` = `since` w dół do pełnej sekundy, `to` ostatniego okna = teraz w górę do pełnej sekundy (adapter czeka do tej chwili, więc `to` nigdy nie jest w przyszłości); zawsze `detailed=true`, bez `filter` (C7b) i bez `dealId`; odpowiedź z co najmniej 20 wierszami może być obcięta (limit nieudokumentowany), więc okno jest dzielone na połowy, a gdy się nie da (okno < 4 s) albo potrzeba ponad 64 zapytań (razem z wyszukiwaniem wstecz) → `HISTORY_TRUNCATED`; wiersz z `dateUTC` spoza zapytanego okna znaczy, że filtr nie zadziałał tak, jak zakładamy → `BrokerError`; nieudane okno → wyjątek (nigdy pominięte); identyczne wiersze (np. z nakładki okien) liczą się raz, a różne zamknięcia tego samego `dealId` (zamknięcia częściowe) zostają osobno. Liczą się tylko wiersze `POSITION`/`ACCEPTED`; `REJECTED`, `MODIFY_REJECT`, `CANCEL_REJECT` są pomijane, typy `WORKING_ORDER`, `EDIT_STOP_AND_LIMIT`, `SWAP`, `SYSTEM` też, a każdy inny status wiersza `POSITION` albo typ spoza wyliczenia → `BrokerError` (C7c). `ClosedDeal.epic` ← `activities[].epic` (ten sam identyfikator co `EntryRequest.epic`, np. `DE40`; nigdy `details.marketName`).

Uwagi praktyczne:

- Zakres 1 dnia: zapytanie `from = początek dnia sesji UTC`, `to = teraz`, ale nie więcej niż 24 h; po każdej wykrytej zmianie i po zamknięciu sesji (spec §7). Dla zamknięcia po stopie preferuj `dealId=<DEAL_ID>&detailed=true` (zawęża wynik).
- Opóźnienie pojawienia się wpisu po zamknięciu i kolejność wpisów: C7, `do potwierdzenia w dry_run lub w odczytach live`.
- Parametr `filter` zawiera znaki `;`, `=`, `!`, `,`: przekazać przez parametry `aiohttp` (kodowanie URL), nie sklejać ręcznie. Dokładna składnia: wyłącznie przykład z [S1], C7b.
- Koszt: zapytania o historię liczą się do limitu 10 zapytań/s (budżet silnika 5/s).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C7, C7b).

### 2.20 `GET /api/v1/history/transactions` (historia transakcji)

Nagłówki: `CST`, `X-SECURITY-TOKEN`. Parametry, opcjonalne [S1]: `from`, `to` (`YYYY-MM-DDTHH:MM:SS`, filtracja po `dateUTC`), `lastPeriod` (sekundy; domyślnie 600; limit górny nie podany), `type` (jedna z: `INACTIVITY_FEE`, `RESERVE`, `VOID`, `UNRESERVE`, `WRITE_OFF_OR_CREDIT`, `CREDIT_FACILITY`, `FX_COMMISSION`, `COMPLAINT_SETTLEMENT`, `DEPOSIT`, `WITHDRAWAL`, `REFUND`, `WITHDRAWAL_MONEY_BACK`, `TRADE`, `SWAP`, `TRADE_COMMISSION`, `TRADE_COMMISSION_GSL`, `NEGATIVE_BALANCE_PROTECTION`, `TRADE_CORRECTION`, `CHARGEBACK`, `ADJUSTMENT`, `BONUS`, `TRANSFER`, `CORPORATE_ACTION`, `CONVERSION`, `REBATE`, `TRADE_SLIPPAGE_PROTECTION`). Domyślnie transakcje z ostatnich 10 minut. Ograniczenie 1 dnia z changelogu dotyczy tylko `history/activity`; dla transakcji [S1] limitu zakresu nie podaje (C8).

```jsonc
// 200 (kształt z przykładu)
{ "transactions": [
  { "date": "...", "dateUtc": "2026-11-02T10:12:00.420",
    "instrumentName": "<nazwa>", "transactionType": "TRADE", "note": "Trade closed",
    "reference": "<REFERENCE>", "size": "<string z liczbą>", "currency": "EUR", "status": "PROCESSED" }
] }
```

Co z tego wynika dla silnika:

| Potrzebne | Pole | Status |
|---|---|---|
| zdarzenie zamknięcia | `transactionType == "TRADE"` i `note == "Trade closed"` | do potwierdzenia w dry_run lub w odczytach live (C8) |
| czas | `dateUtc` (uwaga na wielkość liter; w przykładzie równe `date`) | udokumentowane (nazwa); strefa `date`: C19 |
| wynik (kwota) | `size` jako **napis**; w przykładach to wartości rzędu 1,05 i 416,0 przy zleceniach 1 lot, więc wygląda na kwotę wyniku, nie wielkość pozycji; [S1] nie opisuje | do potwierdzenia w dry_run lub w odczytach live (C8) |
| powiązanie z pozycją | `reference` (przykład: liczba, bez `dealId`) | do potwierdzenia w dry_run lub w odczytach live (C8) |
| cena zamknięcia | **brak w oficjalnym przykładzie** | do potwierdzenia w dry_run lub w odczytach live (C8) |
| przyczyna zamknięcia | **brak** | do potwierdzenia w dry_run lub w odczytach live (C8) |

Wniosek: **cenę i przyczynę zamknięcia bierzemy z historii aktywności (sekcja 2.19), a historia transakcji służy do kontroli księgowej (kwota wyniku, opłaty `TRADE_COMMISSION`, finansowanie `SWAP`)**. Niezależna biblioteka `capital-api-client@1.0.7` (<https://cdn.jsdelivr.net/npm/capital-api-client@1.0.7/dist/index.d.ts>, nieoficjalna) opisuje transakcję także polami `transactionId`, `dateUTC`, `openLevel`, `closeLevel`, `cash`, `pnl`; jeśli żywe API je zwraca, byłyby użyteczne (cena zamknięcia w `closeLevel`), ale to nie wynika z [S1], więc nie wolno na tym polegać bez odczytu (C8).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C8).

### 2.21 Strumień WebSocket (OHLC, notowania, ping): odroczony w v1

Decyzja projektowa v1: polling REST; strumień zostaje na później (odchylenie od spec §4, który traktuje strumień jako źródło podstawowe). Dokumentacja strumienia jest opisana tu, by adapter mógł go dodać bez ponownego czytania.

- Adres: `wss://api-streaming-capital.backend-capital.com/connect`. Zwracany też jako `streamingHost` (POST /session) i `streamEndpoint` (GET /session).
- Każda wiadomość od klienta ma pola: `destination`, `correlationId`, `cst`, `securityToken` (wartości `CST` i `X-SECURITY-TOKEN`; **w ciele wiadomości, nie w nagłówkach**), `payload`.
- Sesja strumienia trwa 10 minut; podtrzymanie: wiadomość `ping` co najmniej raz na 10 minut ("In order to keep the connection alive, ping service at least once every 10 minutes"). Limit 40 instrumentów na subskrypcję; strumień zrywa się po `PUT /session`.

Subskrypcja notowań:

```jsonc
// żądanie
{ "destination": "marketData.subscribe", "correlationId": "1", "cst": "<CST>", "securityToken": "<X-SECURITY-TOKEN>",
  "payload": { "epics": ["DE40"] } }
// odpowiedź
{ "status": "OK", "destination": "marketData.subscribe", "correlationId": "1", "payload": { "subscriptions": { "DE40": "PROCESSED" } } }
// zdarzenie
{ "status": "OK", "destination": "quote",
  "payload": { "epic": "DE40", "product": "CFD", "bid": <number>, "bidQty": <number>, "ofr": <number>, "ofrQty": <number>, "timestamp": 1660297190627 } }
```

Subskrypcja świec OHLC:

```jsonc
{ "destination": "OHLCMarketData.subscribe", "correlationId": "3", "cst": "<CST>", "securityToken": "<X-SECURITY-TOKEN>",
  "payload": { "epics": ["DE40"], "resolutions": ["MINUTE_5"], "type": "classic" } }
// odpowiedź: payload.subscriptions = { "DE40:MINUTE_5:classic": "PROCESSED" }
// zdarzenie
{ "status": "OK", "destination": "ohlc.event",
  "payload": { "resolution": "MINUTE_5", "epic": "DE40", "type": "classic", "priceType": "bid",
               "t": 1671714000000, "h": <number>, "l": <number>, "o": <number>, "c": <number> } }
```

`resolutions`: `MINUTE` (domyślnie), `MINUTE_5`, `MINUTE_15`, `MINUTE_30`, `HOUR`, `HOUR_4`, `DAY`, `WEEK`; `type`: `classic` (domyślnie) lub `heikin-ashi`. Wypisanie: `OHLCMarketData.unsubscribe` i `marketData.unsubscribe` (te same pola; `OHLCMarketData.unsubscribe` przyjmuje `epics`, opcjonalnie `resolutions` i `types`). Ping:

```jsonc
{ "destination": "ping", "correlationId": "5", "cst": "<CST>", "securityToken": "<X-SECURITY-TOKEN>" }
// odpowiedź: { "status": "OK", "destination": "ping", "correlationId": "5", "payload": {} }
```

Otwarte przed użyciem strumienia (spec pytanie 8): `t` jako początek świecy w ms epoki, czy zdarzenie niesie także `priceType: "ask"`, czy zdarzenie przychodzi przy każdej zmianie świecy trwającej, opóźnienie względem REST (C21).

Status operacji: `do potwierdzenia w dry_run lub w odczytach live` (C21).

### 2.22 `GET /api/v1/workingorders` (zlecenia oczekujące, tylko odczyt)

Źródło: oficjalna specyfikacja [S1] <https://open-api.capital.com/>, grupa "Trading > Orders", operacja "All working orders" (odczyt specyfikacji OpenAPI wbudowanej w stronę, 2026-10-31). Nagłówki: `CST`, `X-SECURITY-TOKEN`; bez parametrów. Cytat [S1]: "Returns all open working orders for the active account".

```jsonc
// 200 (kształt z przykładu [S1]; wartości poglądowe)
{ "workingOrders": [
  { "workingOrderData": {
      "dealId": "<ORDER_ID>",                    // identyfikator zlecenia oczekującego
      "direction": "BUY",
      "epic": "<EPIC>",
      "orderSize": 1,
      "leverage": 10,
      "orderLevel": 20,                          // poziom zlecenia
      "timeInForce": "GOOD_TILL_DATE",           // albo "GOOD_TILL_CANCELLED"
      "goodTillDate": "...", "goodTillDateUTC": "2022-06-09T01:01:00.000",  // tylko przy GOOD_TILL_DATE
      "createdDate": "...", "createdDateUTC": "2022-04-06T09:48:28.114",
      "guaranteedStop": true,
      "orderType": "LIMIT",
      "stopDistance": -3, "profitDistance": 3,   // tak w przykładzie (odległość stopu ujemna)
      "trailingStop": false,
      "currencyCode": "USD" },
    "marketData": { "instrumentName": "<nazwa>", "epic": "<EPIC>", "marketStatus": "TRADEABLE",
                    "bid": <number>, "offer": <number>, "updateTimeUTC": "...", ... } }   // jak `market` w 2.11
] }
```

Mapowanie na `WorkingOrder(order_id, epic, side, size, level)` (spec §7: obce zlecenie oczekujące na naszym instrumencie): `order_id` ← `workingOrderData.dealId`, `epic` ← `workingOrderData.epic`, `side` ← `workingOrderData.direction`, `size` ← `workingOrderData.orderSize`, `level` ← `workingOrderData.orderLevel`; kolejność jak w odpowiedzi. Brak pola, zły typ, liczba jako napis, wartość niedodatnia albo powtórzony `dealId` → `BrokerError` (nigdy lista niepełna). Kształt pustej odpowiedzi nie jest pokazany; spodziewane `{"workingOrders": []}` (jak C5b). v1 nie składa ani nie anuluje zleceń oczekujących (anulowanie obcego zlecenia to decyzja właściciela).

Status operacji: `udokumentowane`.

## 3. Skąd wziąć które pole (ściąga dla Zadań 4, 8, 13)

| Potrzebne | Operacja i pole | Status |
|---|---|---|
| `dealReference` zlecenia | `POST /positions` → `dealReference` (`o_...`) | udokumentowane |
| `dealId` pozycji | `GET /confirms/{dealReference}` → `affectedDeals[].dealId` (status `OPENED`); kontrola krzyżowa: `GET /positions/{dealId}` → `position.workingOrderId == confirm.dealId` (zapasowo dopasowanie po `GET /positions`: kierunek, wielkość, czas) | do potwierdzenia w dry_run lub w odczytach live (C2) |
| potwierdzenie wykonania (przyjęcie) | `GET /confirms/{dealReference}` → `dealStatus` (`ACCEPTED`), `status` (`OPEN`), `level`, `size`, `direction` | udokumentowane |
| potwierdzenie wykonania (odrzucenie) | `GET /confirms/{dealReference}` → `dealStatus` (`REJECTED`), `reason` | do potwierdzenia w dry_run lub w odczytach live (C4) |
| kierunek pozycji | `GET /positions` → `position.direction` | udokumentowane |
| wielkość pozycji | `GET /positions` → `position.size` | udokumentowane |
| poziom otwarcia | `GET /positions` → `position.level`; cena wykonania także `confirms.level` | udokumentowane |
| czas otwarcia | `GET /positions` → `position.createdDateUTC` | udokumentowane |
| poziom stopu | `GET /positions` → `position.stopLevel` | do potwierdzenia w dry_run lub w odczytach live (C5) |
| odległość stopu (wysyłka) | `POST /positions` → `stopDistance` w ciele żądania | udokumentowane |
| odległość stopu (odczyt) | `GET /positions` → `position.stopDistance` | do potwierdzenia w dry_run lub w odczytach live (C5, C6) |
| stop wyliczony (`derived`) | brak kluczy stopu w `GET /positions`: poziom wykonania − `stopDistance` (BUY) lub + `stopDistance` (SELL), z własnego żądania | do potwierdzenia w dry_run lub w odczytach live (C24) |
| cena zamknięcia | `GET /history/activity?detailed=true` → `details.level` wpisu zamykającego | do potwierdzenia w dry_run lub w odczytach live (C7) |
| przyczyna zamknięcia (stop czy ręczne) | `GET /history/activity` → `source` (`SL` vs `USER`) | do potwierdzenia w dry_run lub w odczytach live (C7) |
| status rynku | `GET /markets/{epic}` → `snapshot.marketStatus` | udokumentowane (`TRADEABLE`, `CLOSED`) |
| minimalna wielkość | `dealingRules.minDealSize.value` | udokumentowane |
| maksymalna wielkość | `dealingRules.maxDealSize.value` | udokumentowane |
| krok wielkości | `dealingRules.minSizeIncrement.value` (nie `dealSize.increment`; takiego pola nie ma) | udokumentowane |
| krok wielkości (reguła) | wielkość = `minDealSize` + k × krok; odrzucenie wielkości niezgodnej z krokiem | do potwierdzenia w dry_run lub w odczytach live (C14) |
| wartość punktu | brak pola; `lotSize` × `size` w walucie instrumentu | do potwierdzenia w dry_run lub w odczytach live (C16) |
| depozyt (pole) | `instrument.marginFactor` (+ `marginFactorUnit`) | udokumentowane |
| depozyt DE40 (wartość 5%) | `instrument.marginFactor` dla `DE40` | do potwierdzenia w dry_run lub w odczytach live (C25) |
| minimalna odległość stopu | `dealingRules.minStopOrProfitDistance` (`PERCENTAGE` 0,01 dla DE40) | udokumentowane |
| godziny otwarcia (struktura) | `instrument.openingHours` | udokumentowane |
| godziny otwarcia (wartości DE40) | `instrument.openingHours` dla `DE40` (nieodczytane) | do potwierdzenia w dry_run lub w odczytach live (C12) |
| notowanie (bid/ask) | `GET /markets/{epic}` → `snapshot.bid`, `snapshot.offer` | udokumentowane |
| czas notowania (UTC) | `snapshot.updateTimeUTC` | do potwierdzenia w dry_run lub w odczytach live (C10) |
| świece 5-minutowe (pola) | `GET /prices/DE40?resolution=MINUTE_5` → `prices[].snapshotTimeUTC`, `openPrice`..`lowPrice` `{bid, ask}` | udokumentowane |
| świece 5-minutowe (znacznik i granice) | znacznik jako początek świecy, włączność `to`, świeca trwająca | do potwierdzenia w dry_run lub w odczytach live (C9) |
| świece dzienne (parametr) | `GET /prices/DE40?resolution=DAY` | udokumentowane |
| świece dzienne (granica doby) | granica doby DE40, świeca trwająca, `max` bez `from`/`to` | do potwierdzenia w dry_run lub w odczytach live (C9) |
| rachunek aktywny w sesji | `POST /session` → `currentAccountId`; `GET /session` → `accountId` | udokumentowane |
| lista rachunków (pola) | `GET /accounts` → `accounts[].{accountId, currency, status, balance.*}` | udokumentowane |
| `equity`, `margin_used` | `equity` = `balance.balance`; `margin_used` = `balance.balance − balance.available` (równanie `balance` = `deposit` + `profitLoss` z przykładu [S1]) | do potwierdzenia w dry_run lub w odczytach live (C17) |
| tryb konta | `GET /accounts/preferences` → `hedgingMode` | udokumentowane |
| zlecenia oczekujące | `GET /workingorders` → `workingOrders[].workingOrderData.{dealId, epic, direction, orderSize, orderLevel}` | udokumentowane |
| zamknięcie a otwarcie w historii | kierunek `details.direction` względem strony pozycji (sekcja 2.19) | do potwierdzenia w dry_run lub w odczytach live (C7d) |

## 4. Sekwencje dla adaptera (Zadanie 13) i uzgadniania (Zadanie 8)

**Logowanie.** `POST /session` (jawne hasło) → odczyt nagłówków `CST`, `X-SECURITY-TOKEN` oraz ciała JSON (niezależnie od `Content-Type`) → jeśli `currentAccountId` ≠ rachunek związany: jedno `PUT /session` z `accountId` → `GET /session` i kontrola `accountId`; rachunek spoza `accounts[]` = `BrokerError` bez dalszych wywołań. Jedno logowanie naraz (`asyncio.Lock`), odstęp ≥ 1 s między `POST /session`. `udokumentowane` (kroki), kolejność tokenów po `PUT`: C18.

**Utrzymanie.** `GET /ping` co 5 minut; 401 na dowolnym żądaniu → jedno serializowane ponowne logowanie i jedna powtórka żądania; po ponownym logowaniu uruchomić uzgadnianie (spec §7).

**Notowanie i świece.** `GET /markets/{epic}` co ok. 5 s (notowanie, status rynku); `GET /prices/DE40?resolution=MINUTE_5&from&to` dla zakresu otwarcia i zapasowych odczytów; `GET /prices/DE40?resolution=DAY&max=30` raz przed otwarciem (ATR14).

**Wejście.** `INTENT` w dzienniku → `POST /positions` (ze `stopDistance`, odstęp ≥ 0,1 s od poprzedniego zlecenia) → `dealReference` w dzienniku → `GET /confirms/{dealReference}` (jedna próba na wywołanie `confirm`; ponawia menedżer zleceń w `UNKNOWN`) → przy `ACCEPTED`: `dealId` pozycji z `affectedDeals` → `GET /positions/{dealId}`: kontrola krzyżowa `position.workingOrderId == confirm.dealId` (C2) i kontrola stopu. Kontrola stopu (adapter: jeśli potwierdzenie ma `stopLevel`, musi równać się wyliczonemu, inaczej `BrokerError`; C6): jeśli `stopLevel` jest w odpowiedzi pozycji, ma być zgodny z oczekiwanym (BUY `level − stopDistance`, SELL `level + stopDistance`); jeśli klucz jest obecny i `null`, `PUT /positions/{dealId}` z `stopLevel`, a gdy zawiedzie, kill switch twardy; jeśli klucza w ogóle nie ma, stop jest wyliczony z własnego żądania i oznaczony `derived` (C24). Timeout `POST` = `UNKNOWN` → `GET /positions` do 3 razy w 30 s (spec §6).

**Wyjście.** Wyłącznie `DELETE /positions/{dealId}` (nigdy przeciwne zlecenie rynkowe) → `GET /confirms/{dealReference}` z referencją z treści odpowiedzi (nie budować `p_<dealId>` ręcznie) → `GET /positions/{dealId}` (musi być 404); `dealStatus` `ACCEPTED` przypisane naszemu `dealId` (C3) i 404 pozycji → zamknięte, `REJECTED` → `BrokerRejected(reason)`, każdy inny wynik potwierdzenia (404, błąd, timeout, brak referencji) → `BrokerTimeout("close outcome unknown")`; `DELETE` na nieistniejący deal (404 `error.not-found.dealId`) → `BrokerRejected("POSITION_NOT_FOUND")` (C29); timeout lub odmowa → uzgadnianie, nie ponawianie na ślepo.

**Uzgadnianie co 10 s.** `GET /positions` → porównanie z dziennikiem (tabela spec §7). Pozycja lokalna zniknęła: `GET /history/activity?from=<początek dnia sesji UTC>&to=<teraz>&detailed=true&dealId=<dealId>` → wpis zamykający daje `details.level` i `source`; brak wpisu = `BLOCKED` i alarm.

**Mapowanie na typy (Zadanie 4).**

| Typ | Pola ← źródło |
|---|---|
| `Quote(epic, bid, ask, ts)` | `bid` ← `snapshot.bid`; `ask` ← `snapshot.offer`; `ts` ← chwila zegara silnika, w której krotka (`bid`, `offer`, `updateTime`) ostatnio się zmieniła (sekcja 2.16.2; C10) |
| `Candle(epic, start, open, high, low, close)` | `start` ← `snapshotTimeUTC` (UTC); OHLC ← `(bid + ask) / 2` z `openPrice` itd. |
| `InstrumentRules(...)` | sekcja 2.16.1 |
| `OpenPosition(deal_id, epic, side, size, open_level, stop_level, opened_at, stop_derived)` | `deal_id` ← `position.dealId`; `epic` ← `market.epic`; `side` ← `position.direction`; `size` ← `position.size`; `open_level` ← `position.level`; `stop_level` ← `position.stopLevel` gdy klucz jest w odpowiedzi (C5), inaczej `level ∓ position.stopDistance` gdy ten klucz jest, a gdy kluczy stopu nie ma wcale, wartość z zapisu adaptera (poziom wykonania ∓ wysłane `stopDistance` albo poziom z potwierdzonego `set_stop`) i `stop_derived=True` (C24); `None` dla jawnego `null` albo gdy adapter nie ma zapisu (np. po restarcie); `opened_at` ← `position.createdDateUTC`. Migawka `confirm`: `deal_id` ← `affectedDeals[]` `OPENED`, `open_level`/`size`/`side`/`epic` ← potwierdzenie, `opened_at` i stop ← `GET /positions/{dealId}` (pozycja już zamknięta: `opened_at` ← `dateUTC` wiersza otwarcia w historii, C28) |
| `ClosedDeal(deal_id, epic, exit_level, reason, ts)` | `deal_id` ← `activities[].dealId`; `epic` ← `activities[].epic`; `exit_level` ← `details.level`; `reason` wg reguły z 2.19; `ts` ← `dateUTC` (C7, C7c, C7d, C27) |
| `WorkingOrder(order_id, epic, side, size, level)` | `workingOrderData.{dealId, epic, direction, orderSize, orderLevel}` (sekcja 2.22) |
| `AccountInfo(...)` | `equity` ← `balance.balance`; `available` ← `balance.available`; `margin_used` ← `balance.balance − balance.available` (sekcja 2.8; C17) |
| `EntryAck(deal_reference)` | `POST /positions` → `dealReference` |
| `Capabilities` | `stop_attach="atomic"` (stop w treści `POST /positions`), `idempotency_key=None`, `keepalive_s=600` (sesja 10 minut; ping wcześniej), `reconcile="positions"`, `is_cfd=True` |

## 5. Niespójności i pułapki dokumentacji

1. Szyfrowanie hasła: tekst mówi "AES", kod przykładowy używa RSA PKCS1 (sekcja 2.3).
2. `POST /session` ma ciało opisane jako `text/plain` (sekcja 1.3).
3. Przykład `GET /accounts` powtarza ten sam `accountId` dla obu rachunków (poglądowy przykład); w żywej odpowiedzi identyfikatory są różne [S4].
4. Nazwy: `dateUTC` (aktywność) kontra `dateUtc` (transakcje); `currentAccountId` (POST /session) kontra `accountId` (GET /session).
5. W przykładach `unit` dla wielkości to `"POINTS"`, mimo że chodzi o jednostki wielkości.
6. Przykłady pozycji nie zawierają pól stopu, choć tworzymy pozycje ze stopem; pola stopu w odpowiedziach są znane tylko z [S3].
7. Przykłady historii aktywności nie pokazują zamknięcia przez stop, tylko przez `CLOSE_OUT`.
8. Postman i przykładowy bot Java wskazane w dokumentacji nie były czytane.

## 6. Lista pozycji do potwierdzenia (dla `dry_run`, próby `smoke`, Zadania 18)

Kolumna "Gdzie": **odczyt** = da się rozstrzygnąć odczytem bez zleceń (`dry_run`, odczyty live); **smoke** = wymaga pierwszej transakcji na wielkości minimalnej (Zadanie 18) po zgodzie właściciela. Wszystkie pozycje mają status `do potwierdzenia w dry_run lub w odczytach live`. Warianty z literą (C2b, C5b, C5c, C5d, C7b, C7c, C7d, C11b, C14b, C16b, C16c, C19b) należą do pozycji nadrzędnej o tym samym numerze. Pozycje rozstrzygnięte odczytem live z 2026-10-31 (`docs/ops/capitalcom-live-probe-2026-10-31.md`) są oznaczone **rozstrzygnięte** w kolumnie „Co sprawdzić”; ich część nadal otwarta jest wymieniona osobno.

| ID | Co sprawdzić | Gdzie | Spec |
|---|---|---|---|
| C1 | Czy `GET /confirms/{ref}` tuż po `POST` zwraca 404 i ile prób trzeba. Adapter: dokładnie 404 `error.not-found.dealReference` → `confirm` = None (bez ponawiania w adapterze; ponawia menedżer zleceń w `UNKNOWN`), każde inne 404 → `BrokerError`. Weryfikacja: smoke, surowa odpowiedź pierwszego `confirm` w dzienniku | smoke | pyt. 7 |
| C2 | Który identyfikator z potwierdzenia jest `dealId` pozycji (`affectedDeals[].dealId` kontra `dealId`) oraz czy `position.workingOrderId == confirm.dealId`. Adapter: id pozycji = JEDYNY wpis `affectedDeals`, i to ze statusem `OPENED` (żaden, np. `AMENDED` przy agregacji, kilka albo jakikolwiek wpis dodatkowy → `BrokerError`, C15); `GET /positions/{id}` musi zgadzać się epikiem, kierunkiem i (gdy oba są) `workingOrderId == confirm.dealId`, inaczej `BrokerError`. C2b: czy po zamknięciu pozycji potwierdzenie otwarcia nadal ma ten wpis `OPENED` (jeśli nie, `confirm` po restarcie dla zamkniętej pozycji da `BrokerError`: bezpiecznie, ale z blokadą). Weryfikacja: smoke, `GET /confirms/{o_ref}` przed i po zamknięciu pozycji | smoke | pyt. 7 |
| C3 | Struktura potwierdzenia zamknięcia (`DELETE`) i zmiany stopu (`PUT`): `status`, `affectedDeals`, `level`, `stopLevel`, wynik. `PUT` i `DELETE` zwracają tę samą referencję `p_<dealId>` [S1], więc potwierdzenie odczytane tuż po `DELETE` może jeszcze pokazywać wynik wcześniejszego `PUT` (sukces nie jest chroniony kontrolą listy w silniku: po `close_position` bez wyjątku wykonawca od razu szuka zamknięcia w historii). Dlatego adapter sam sprawdza wynik. **Zamknięcie:** sukces tylko przy `dealStatus` `ACCEPTED`, `status` innym niż `OPEN` i wpisie naszego `dealId` w `affectedDeals` ze statusem innym niż `AMENDED`/`OPENED` (np. `FULLY_CLOSED`), a potem 404 na `GET /positions/{dealId}`; pozycja nadal jest, odczyt nieudany albo potwierdzenie bez takiego wpisu → `BrokerTimeout("close outcome unknown")`. **Stop:** sukces tylko przy `ACCEPTED` z naszym `dealId` jako `AMENDED`; `stopLevel` potwierdzenia (jeśli jest) i `stopLevel` z `GET /positions/{dealId}` (jeśli broker go podaje) muszą równać się żądanemu, inaczej `BrokerError`; sam `stopDistance` nie jest porównywany (punkt odniesienia niepotwierdzony, C6), ale `stopDistance: null` znaczy brak stopu (`BrokerError`); broker pokazywał już klucze stopu, a pozycja po `PUT` nie ma żadnego → `BrokerTimeout` (bez zapisu stopu); brak wpisu `AMENDED` albo nieudany odczyt → `BrokerTimeout`. Pozostałe ryzyko: gdy ani potwierdzenie, ani pozycja nie pokazują stopu, nieaktualnego potwierdzenia `PUT` nie da się odróżnić (adapter przyjmuje i zapisuje żądany poziom). Weryfikacja: smoke, surowe potwierdzenia `PUT` i `DELETE` w dzienniku: wartości `status` i `affectedDeals[].status` oraz obecność `stopLevel` | smoke | pyt. 7 |
| C4 | Pełna lista `status`/`dealStatus` i wartości `reason` przy odrzuceniu | smoke | pyt. 7 |
| C5 | Pola stopu w `GET /positions` (`stopLevel`, `stopDistance`, `profitLevel`, `trailingStop`): czy klucze są w ogóle zwracane i czy brak stopu to jawny `null`; kształt pustej listy; 404 po zamknięciu; skutek `PUT` (C5b, C5c, C5d) | smoke | pyt. 7 |
| C6 | Czy `stopDistance` liczy się od notowania, czy od ceny wykonania; zgodność `stopLevel` z `level − stopDistance` (BUY) i `level + stopDistance` (SELL) | smoke | pyt. 7 |
| C7 | Aktywność po zamknięciu przez stop: `source` (`SL`?), `details.level`, opóźnienie wpisu, składnia `filter`. Adapter: `SL`/`TP` → STOP, `USER` → MANUAL, `CLOSE_OUT`/`DEALER`/`SYSTEM` → FORCED, inne → FORCED i licznik `unknown_close_sources`; `filter` nieużywany (C7b). C7c: tylko `POSITION`/`ACCEPTED` to transakcje; `REJECTED`/`MODIFY_REJECT`/`CANCEL_REJECT` pomijane; inny status wiersza `POSITION` albo typ spoza wyliczenia → `BrokerError`. C7d: otwarcie i zamknięcie różni tylko kierunek; deal bez rozpoznanej strony → wyszukanie wstecz z `dealId`, potem `HISTORY_AMBIGUOUS`; wiersz w kierunku pozycji po zamknięciu → `HISTORY_INCONSISTENT` (sekcja 2.19). Weryfikacja: smoke, surowa historia (`detailed=true`) po wejściu, po zamknięciu przez API i po wybiciu stopu: wiersz otwarcia `USER` w kierunku pozycji, zamknięcie w kierunku przeciwnym, źródło stopu `SL`; zmierzyć opóźnienie wpisu (menedżer czyta historię w T0 + 30 s) | smoke (stop), odczyt (kształt) | pyt. 7 |
| C8 | Historia transakcji: znaczenie `size`, `reference`, brak ceny i `dealId`, zakres czasu, pola `openLevel`/`closeLevel`/`pnl` | smoke | pyt. 7 |
| C9 | **Rozstrzygnięte** (odczyt 2026-10-31): pola świecy `snapshotTime` (lokalny, CET), `snapshotTimeUTC`, `openPrice`/`closePrice`/`highPrice`/`lowPrice` jako `{bid, ask}`, `lastTradedVolume`. Otwarte: znacznik = początek świecy (stała adaptera `CANDLE_STAMP_IS_PERIOD_START = True`, czytana wyłącznie w `_stamp_shift`; bramka L0: w `dry_run` właściciel porównuje zapisany początek UTC i OHLC świecy 09:00 Berlin z wykresem 5-minutowym w aplikacji), wyłączność `to`, świeca trwająca w odpowiedzi, czy przyjmowane jest `to` równe teraz, czy `to` z przyszłości (zegar silnika przed zegarem brokera) jest odrzucane (adapter nie sprawdza: zawsze obcina `to` do teraz − 5 s, sekcja 2.18.1), granica doby świec dziennych DE40, `max` bez `from`/`to`, opóźnienie świecy | odczyt | pyt. 8, 12 |
| C10 | **Rozstrzygnięte** (odczyt 2026-10-31): `snapshot` w `GET /markets/DE40` NIE ma `updateTimeUTC`, a `updateTime` jest lokalny (CET, bez strefy); adapter liczy świeżość zmianą krotki. Otwarte: jednostka `delayTime`, częstotliwość odświeżania notowania. Adapter: `snapshot.delayTime` ≠ 0 albo `snapshot.scalingFactor` ≠ 1 (odczyt live DE40: 0 i 1) niczego nie przerywa (ceny są używane tak, jak podano), ale daje jedno ostrzeżenie (WARNING loggera `gwstock.broker.capitalcom`, bez treści wartości innych niż liczby) na epik w procesie, także gdy wartość nie jest liczbą; brak pola nie jest anomalią. Jeśli ostrzeżenie się pojawi, ceny mogą być opóźnione lub przeskalowane: zgłoś, nie ignoruj | odczyt | pyt. 8 |
| C11 | Wartości `marketStatus` i `marketModes`; zachowanie `POST` przy `CLOSED` (kod błędu); C11b zamknięcie przy `CLOSED` | odczyt (status), smoke (błąd) | pyt. 7 |
| C12 | **Rozstrzygnięte** (odczyt 2026-10-31): `zone` = `UTC`; DE40 pon–czw 00:00–21:00 i 21:05–24:00, pt 00:00–21:00, sob zamknięte, nd 22:00–24:00 (rynek prawie całą dobę, więc `marketStatus` nie wyznacza sesji XETRA) | odczyt | Zadanie 1 |
| C13 | Kod i status HTTP przy wygasłej sesji | odczyt (odczekać ponad 10 min) | §13 |
| C14 | Kody błędów `POST /positions`: za mała wielkość, zły krok, brak środków, zbyt mały stop, rynek zamknięty; semantyka liczby po dwukropku. Adapter: każde 4xx z `errorCode` na `POST`/`PUT`/`DELETE` → `BrokerRejected(errorCode)` (nigdy ponowione). C14b: kod przy zamkniętym rynku jest nieudokumentowany; adapter zamienia na `MarketClosed` każdy kod zawierający słowa `market` i `closed` (pomyłka zmienia tylko klasę odmowy: w obu przypadkach nic nie wykonano); ten sam test stosuje do `reason` odrzucenia w potwierdzeniu, które wtedy jest `MarketClosedRejection` (jednocześnie `BrokerRejected` dla menedżera zleceń i `MarketClosed` dla wykonawcy). Weryfikacja: pierwszy realny kod odmowy w dzienniku (smoke tylko za zgodą właściciela, np. w przerwie 21:00–21:05 UTC) | smoke | pyt. 7 |
| C15 | Tryb `hedgingMode` i skutek drugiego wejścia na tym samym instrumencie. Adapter: potwierdzenie otwarcia, w którym `affectedDeals` nie ma DOKŁADNIE jednego wpisu, naszego `OPENED`, to nie jest czyste wypełnienie → `BrokerError` (nie zgadywana pozycja): brak `OPENED` (np. `AMENDED`, gdy zlecenie dołączyło do istniejącej pozycji), a także jakikolwiek wpis dodatkowy (inny deal w dowolnym statusie, w tym `AMENDED`, `PARTIALLY_CLOSED`, `FULLY_CLOSED`, `DELETED`; drugi wpis naszego deala). Zlecenie zostaje wtedy `UNKNOWN`, a menedżer zleceń i uzgadnianie rozstrzygają; nic nie jest zapamiętywane, więc kolejny `confirm` pyta brokera od nowa. Do tego kontrola krzyżowa migawki: wielkość pozycji różna od wielkości z potwierdzenia (większa: zlecenie dołączyło do innej pozycji; mniejsza: część już zamknięta) → `BrokerError`. **Sprawdzenie wielkości jest blokujące:** gdyby na żywym brokerze wielkość z potwierdzenia i z pozycji różniły się przy czystym wypełnieniu, KAŻDE wejście przeszłoby przez `UNKNOWN` (pierwsza runda menedżera adoptuje pozycję: zlecenie `CONFIRMED` z notatką `ADOPTED_AFTER_UNKNOWN VIA=positions`, ok. 10 s zwłoki, bez `ENTRY_UNRESOLVED`); `smoke` wypisuje wtedy `WARN [2]`, a właściciel to zgłasza, zamiast pomijać (usunięcie sprawdzenia to jedna linia w `_snapshot`). Weryfikacja: smoke (nie może być adopcji po `UNKNOWN` przy czystym wejściu), ręczny handel właściciela na DE40 przy otwartej pozycji silnika | smoke | pyt. 4 |
| C16 | Wartość punktu (`size` × `lotSize` w EUR) z porównania `upl`; C16b zależność dźwigni od depozytu 5%; C16c krok `minStepDistance` | smoke | pyt. 3 |
| C17 | Znaczenie `balance`, `deposit`, `profitLoss`, `available`. **Część odczytowa:** czy `balance` = `deposit` + `profitLoss` (równanie z oficjalnego przykładu [S1]; da się sprawdzić w odczycie bez pozycji). **Część wymagająca pozycji:** czy `margin_used = balance − available` zgadza się z depozytem pokazanym w aplikacji przy otwartej pozycji (bez pozycji nie ma depozytu, z którym dałoby się porównać, więc sam odczyt tego nie rozstrzyga) | odczyt (równanie sald), smoke (`margin_used`) | Zadanie 4 |
| C18 | Czy `CST`/`X-SECURITY-TOKEN` zmieniają się po `PUT /session` lub w odpowiedziach `ping` | odczyt | §13 |
| C19 | Strefa pól lokalnych (`date`, `createdDate`, `snapshotTime`); **rozstrzygnięte** (odczyt 2026-10-31): `snapshotTime` i `snapshot.updateTime` to CET (UTC + 1 h zimą). Adapter nie czyta żadnego pola lokalnego (także `date` potwierdzenia i historii). C19b `Content-Type` odpowiedzi `POST /session` | odczyt | pyt. 8 |
| C20 | Nagłówek `Retry-After` i kod błędu przy 429 poza logowaniem | odczyt (ostrożnie) | §13 |
| C21 | Strumień WebSocket: znacznik `t`, `priceType`, opóźnienie względem REST | odczyt (po wdrożeniu strumienia) | pyt. 8 |
| C22 | Czy `DELETE /positions/{dealId}` zamyka pozycję w całości i nie przyjmuje wielkości | smoke | pyt. 7 |
| C23 | Czy `GET /markets?epics=DE40` zwraca `marketDetails[]` z `snapshot` tym samym co `GET /markets/{epic}` (w tym `updateTimeUTC`) i czy `GET /markets?searchTerm=` zwraca `updateTimeUTC` dla DE40 | odczyt | pyt. 8 |
| C24 | Stop `derived`: gdy `GET /positions` nie zwraca kluczy stopu, czy stop wyliczony z własnego żądania zgadza się ze stopem widocznym w aplikacji Capital.com (weryfikacja wzrokowa przez właściciela). Adapter: klucz `stopLevel` obecny → wartość brokera (`null` → brak stopu); sam `stopDistance` → `level ∓ stopDistance`; kluczy brak → zapis adaptera (poziom wykonania z potwierdzenia ∓ wysłane `stopDistance`, po udanym `set_stop` jego poziom) z `stop_derived=True`; brak zapisu (restart, utracona odpowiedź `POST`) → `None` (silnik ustawi stop). Gdy JAKAKOLWIEK odpowiedź z pozycją pokazała już klucze stopu, brak klucza w odczycie pozycji znaczy brak stopu (`None`, nie wartość wyliczona ani `stopLevel` z potwierdzenia): wtedy zapis adaptera nie może udawać ochrony, np. gdy właściciel usunął stop w aplikacji. Reguła dotyczy tylko odczytu pozycji, który się odbył: migawka `confirm` pozycji zamkniętej przed odczytem niesie wyliczony stop (`stop_derived=True`), bo nie ma czego zabezpieczać. `stopLevel` w potwierdzeniu otwarcia (jeśli jest) musi równać się wartości wyliczonej, inaczej `BrokerError` (C6). Pamięć ograniczona do 256 wpisów na tablicę | smoke | pyt. 7 |
| C25 | Wartość `instrument.marginFactor` i `marginFactorUnit` dla `DE40` (odczyt [S4] podaje "depozyt 5%") | odczyt | pyt. 3 |
| C26 | Godzina naliczenia finansowania nocnego DE40 w polu `overnightFee.swapChargeTimestamp` (strona rynku podaje 21:00 UTC) | odczyt | §4 |
| C27 | Granice `GET /history/activity` (także z filtrem `dealId` przy wyszukiwaniu wstecz): czy `to` równe teraz jest przyjmowane, czy granice `from`/`to` są włączne, czy istnieje nieudokumentowany limit liczby wierszy (adapter: ≥ 20 wierszy w odpowiedzi → podział okna, stała `ACTIVITY_SUSPECT_ROWS`), czy filtr działa po `dateUTC` (wiersz spoza okna → `BrokerError`), czy KAŻDY wiersz odpowiedzi z filtrem `dealId` niesie ten `dealId`, także typu `SWAP` lub `SYSTEM` (adapter: inny albo brak → `BrokerError`, bo filtr „nie zadziałał”; jeśli żywe wiersze `SWAP`/`SYSTEM` przychodzą w odpowiedzi filtrowanej bez `dealId`, wyszukiwanie wstecz będzie je odrzucać i założenie trzeba poluzować). Wyniki wyszukiwania wstecz dla okien w całości przed `since` są zapamiętywane (najwyżej 64 okna, sekcja 2.19), więc powtórzone odczyty z tym samym `since` nie zużywają budżetu zapytań. Weryfikacja: `dry_run` (odczyt bez zleceń: okno dobowe kończące się teraz zwraca `{"activities": []}` bez błędu; test `integration_live_readonly` `test_closed_since_two_days_does_not_raise`), po smoke: liczba wierszy z okna dobowego równa sumie z jego połówek | odczyt, smoke | pyt. 7 |
| C28 | Migawka `confirm` pozycji już zamkniętej: `opened_at` z `dateUTC` jedynego wiersza otwarcia tego `dealId` w ostatniej dobie historii (pozycja otwarta: z `createdDateUTC`); czy oba czasy są równe co do milisekundy. Weryfikacja: smoke, porównać `createdDateUTC` pozycji z `dateUTC` jej wiersza otwarcia | smoke | pyt. 7 |
| C29 | Drugie zamknięcie: `DELETE /positions/{dealId}` na deal już zamknięty to czyste 404 `error.not-found.dealId` bez skutku (adapter: `BrokerRejected("POSITION_NOT_FOUND")`), a zamknięcie zawsze idzie przez `DELETE` po `dealId` (nigdy przeciwne zlecenie). Założenie wykonawcy (Zadanie 12: awaryjne spłaszczenie może wysłać drugie zamknięcie). Weryfikacja: smoke za zgodą właściciela: po zamknięciu jeszcze raz `DELETE` tego `dealId`, potem lista pozycji pusta i brak nowej pozycji w aplikacji | smoke | Zadanie 12 |

Lista kontrolna właściciela dla próby `smoke` (Zadanie 18), z pozycji powyżej, które rozstrzyga tylko prawdziwa transakcja na wielkości minimalnej:

1. Potwierdzenie otwarcia (C1, C2, C2b): surowe `GET /confirms/{o_ref}` zaraz po wejściu i po zamknięciu; `affectedDeals[]` z `OPENED`, `dealId` zlecenia równe `workingOrderId` pozycji.
2. Stop (C5, C6, C24): czy `GET /positions` pokazuje klucze `stopLevel`/`stopDistance`; czy stop w aplikacji Capital.com równa się poziomowi wykonania ∓ wysłane `stopDistance`.
3. Zmiana stopu i zamknięcie (C3, C29): surowe potwierdzenia `PUT` i `DELETE` (`status`, `affectedDeals[].status`, `stopLevel`); drugi `DELETE` tego samego `dealId` daje czyste 404 bez nowej pozycji.
4. Historia (C7, C7c, C7d, C27, C28): wiersz otwarcia `USER` w kierunku pozycji, zamknięcie w kierunku przeciwnym (`USER` przy zamknięciu przez API, `SL` po stopie), `dateUTC` otwarcia równe `createdDateUTC`; **typy pomijane przez adapter** (`WORKING_ORDER`, `EDIT_STOP_AND_LIMIT`, `SWAP`, `SYSTEM`) i statusy `REJECTED`/`MODIFY_REJECT`/`CANCEL_REJECT` nigdy nie niosą zamknięcia pozycji (np. przy finansowaniu nocnym `SWAP` albo wymuszonym zamknięciu sprawdzić, że zamknięcie ma osobny wiersz `POSITION`/`ACCEPTED`); opóźnienie wpisu po zamknięciu; w odpowiedzi z filtrem `dealId` każdy wiersz (także `SWAP`/`SYSTEM`, jeśli jakiś się trafi) ma ten `dealId`.
5. Kody odmów (C14, C14b): pierwszy realny kod odmowy w dzienniku.
6. Tryb konta (C15): `hedgingMode` i to, że ręczna transakcja właściciela na DE40 nie łączy się z naszym `dealId`; czyste wejście ze `smoke` nie może się kończyć adopcją po `UNKNOWN` (`WARN [2]`): to znaczyłoby, że potwierdzenie ma dodatkowy wpis `affectedDeals` albo inną wielkość niż pozycja.
7. Saldo (C17): przy otwartej pozycji (`smoke --hold`) porównać `margin_used = balance − available` z depozytem pokazanym w aplikacji (równanie `balance` = `deposit` + `profitLoss` sprawdza już sam odczyt).

Sugestia dla Zadania 18: pierwszy `dry_run` zapisuje do dziennika surowe odpowiedzi `GET /markets/DE40`, `GET /prices/DE40` (5 minut i dzień) i `GET /accounts/preferences` (bez zapisywania sald w repo), co zamyka C9 do C12, C17 (tylko równanie `balance` = `deposit` + `profitLoss`; `margin_used` rozstrzyga punkt 7 listy `smoke` wyżej), C19, C23, C25 i C26 bez żadnego zlecenia.
