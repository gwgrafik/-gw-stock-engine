# gw-stock-engine

## Cel projektu

Autonomiczny silnik handlowy na CFD indeksu DE40 (Capital.com, konto 40 EUR, później prawdziwe pieniądze).
To **narzędzie pomiarowe**: sprawdza, czy strategia i infrastruktura działają zgodnie ze specyfikacją.
Nie obiecuje zysku, a wynik pojedynczej sesji to grosze i szum (spec §5).

## Zasady pracy

- **TDD**: najpierw test i obserwacja, że pada z właściwego powodu, potem kod, potem zielony test.
- **Decimal wszędzie**: kwoty, ceny, wielkości i ułamki jako `Decimal`, nigdy `float`. W TOML jako napisy (`"1.00"`).
- **Wartości tylko z konfiguracji** (`config/*.toml`): żadnych liczb strategii ani ryzyka zaszytych w kodzie.
- **Brak ścieżki live w kodzie**: kod NIGDY nie tworzy `data/ARM_LIVE` i nie przestawia `live_allowed`.
  Uzbraja wyłącznie właściciel (spec §9). Bez uzbrojenia silnik pracuje w trybie `dry_run`.
- **Wyjść (zamknięć pozycji) nigdy nie blokuje bramka ryzyka.** Blokować wolno tylko wejścia.
- **Klucze i hasła**: nigdy do czatu, plików, commitów ani logów. Repo jest publiczne.
  Klucz leży w `~/.config/gw/capitalcom.json` (chmod 600) i jest montowany do kontenera tylko do odczytu.
- Jedyna zależność wykonania to `aiohttp`; każda następna wymaga uzasadnienia i pomiaru pamięci.
- Kod i identyfikatory po angielsku, dokumentacja po polsku.

## Testy

Z korzenia repozytorium (ścieżki względne `config/`):

```
python -m pytest -m "not integration_live_readonly"
```

Testy `integration_live_readonly` łączą się z żywym kontem (tylko odczyt) i uruchamia je ręcznie właściciel.
Instalacja zależności: `pip install -r requirements-dev.lock` (testy) lub `requirements.lock` (sam silnik).

## Gdzie szukać

- Specyfikacja: `docs/superpowers/specs/2026-10-31-engine-design-v2.md`
- Plan implementacji: `docs/superpowers/plans/2026-10-31-engine-v2-implementation.md`
- Notatki o API Capital.com: `docs/ops/capitalcom-api-notes.md`

## Commity

Po polsku, z prefiksem konwencjonalnym: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
Jedna zmiana logiczna na commit; tytuł w trybie oznajmującym, bez kropki (np. `feat: scaffolding i konfiguracja`).
