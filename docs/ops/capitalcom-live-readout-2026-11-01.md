# Odczyt z API Capital.com (live, tylko odczyt), 2026-11-01

Odczyt wykonał właściciel skryptem tylko do odczytu (logowanie, `GET /accounts`, `GET /markets/{epic}`, wylogowanie), bez żadnych zleceń. Dokument nie zawiera kluczy, haseł, identyfikatorów rachunków ani sald. Rynki były zamknięte (weekend), więc notowania to ostatnia migawka.

## Rachunki

Dwa rachunki typu CFD w EUR, oba `ENABLED`. Rachunek docelowy wiąże polecenie `bind-account` (spec, sekcja 9). Host demo odpowiada na ten klucz `error.null.accountId` (brak konta demo).

## Reguły instrumentów

| Instrument | Waluta | Typ | `minDealSize` | `minSizeIncrement` | `maxDealSize` | `minStopOrProfitDistance` | `minGuaranteedStopDistance` | Depozyt | Migawka (bid / offer) | Spread |
|---|---|---|---|---|---|---|---|---|---|---|
| **DE40** | EUR | INDICES | 0,001 | 0,001 | 1000 | 0,01% ceny | 0,1% ceny | 5% | 25780,3 / 25782,3 | 2,0 pkt |
| US100 | USD | INDICES | 0,001 | 0,001 | 1250 | 0,01% ceny | 0,1% ceny | 5% | 28401,8 / 28403,6 | 1,8 pkt |
| US500 | USD | INDICES | 0,01 | 0,01 | 2250 | 0,01% ceny | 0,1% ceny | 5% | 7583,2 / 7583,8 | 0,6 pkt |

Wspólne dla wszystkich trzech: `minStepDistance` 0,1 pkt, `marketOrderPreference` `AVAILABLE_DEFAULT_ON`, `trailingStopsPreference` `NOT_AVAILABLE`, `leverage` nie podane (depozyt 5%), `marketStatus` `CLOSED` w chwili odczytu.

## Wnioski dla projektu

- Minimalna pozycja DE40 (0,001) to wartość ok. 26 € i depozyt ok. 1,3 €, więc konto 40 € w pełni wystarcza.
- Minimalna odległość stopu to 0,01% ceny (ok. 2,6 pkt dla DE40), więc stop `0,10 × ATR14` rzędu kilkudziesięciu punktów mieści się w regułach.
- Stop trailing jest niedostępny; strategia go nie używa.
- Godziny handlu DE40 do odczytania z pola godzin otwarcia w `GET /markets/{epic}` (zadanie 1 planu).
