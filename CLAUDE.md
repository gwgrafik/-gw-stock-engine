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

## Strażnik live

Hook `PreToolUse` (`.claude/settings.json` wywołuje `scripts/hooks/guard_live.py`) ocenia każdy zapis `Write`, `Edit`
i `MultiEdit` do plików `.py .sh .bash .toml .yml .yaml .cfg .ini .env` (oraz `Dockerfile` i `Makefile`) i BLOKUJE go
(kod wyjścia 2, a przy awarii samego hooka `onFailure: "block"`), gdy zapis:

- tworzy lub zapisuje `data/ARM_LIVE` albo `data/L1_PASSED` (`open(..., "w")`, `touch`, `write_text`, `os.mknod`,
  `shutil.copy`, `>`, `tee`, `cp`, `ln -s`, także przez zmienną-alias). Sprawdzanie (`.exists()`), odczyt i usuwanie są dozwolone;
- zaszywa klucz lub hasło API (`X-CAP-API-KEY`, `api_key`, `api_password`, `password` z literałem od 12 znaków
  `[A-Za-z0-9]`, który nie jest oczywistym wypełniaczem typu `<...>`, `xxxx`, `changeme`);
- wpisuje `live_allowed = true` do `config/runtime.toml`.

`data/ARM_LIVE`, `data/L1_PASSED` i `live_allowed = true` tworzy lub ustawia **wyłącznie właściciel**; agent i kod nigdy,
także gdy "tylko na chwilę" albo "tylko w teście" (testy robią to wyłącznie w katalogu tymczasowym i tylko w plikach
z listy wyjątków).

- Skan całego repozytorium (CI robi to samo przed testami; kod 1 = naruszenie): `python scripts/hooks/guard_live.py --scan`.
- Pliki poza oceną (uzasadnienie przy każdym w nagłówku `scripts/hooks/guard_live.py`, test pilnuje, żeby nie były zbędne):
  sam strażnik i `tests/unit/test_guard_live.py` (przykłady naruszeń jako dane), `tests/fault/engine_sim.py` (symulacja
  właściciela uzbrajająca tymczasowy `data_dir`), `tests/fault/test_modes.py` i `tests/fault/test_engine_day.py`
  (fixtures w katalogach tymczasowych), a dla reguły kluczy `tests/unit/test_capitalcom_adapter.py` (sztuczne dane
  logowania). Nowy wyjątek wymaga uzasadnienia w nagłówku skryptu i zgody właściciela. `docs/**` i `*.md` to proza.
- Strażnik to bariera przed wypadkiem, nie granica bezpieczeństwa: polecenie Bash omija hook (część wzorców łapią
  reguły `deny` w `.claude/settings.json`), a zmienne budowane w czasie działania też. Ostateczną bramką jest właściciel.
- Agenci tylko do odczytu (`Read`, `Grep`, `Glob`) w `.claude/agents/`: `risk-reviewer`, `order-state-auditor`,
  `backtest-skeptic`. CI (`.github/workflows/ci.yml`) nie ma żadnych sekretów i buduje obraz z `--target test`.

## Gdzie szukać

- Specyfikacja: `docs/superpowers/specs/2026-10-31-engine-design-v2.md`
- Plan implementacji: `docs/superpowers/plans/2026-10-31-engine-v2-implementation.md`
- Notatki o API Capital.com: `docs/ops/capitalcom-api-notes.md`

## Commity

Po polsku, z prefiksem konwencjonalnym: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
Jedna zmiana logiczna na commit; tytuł w trybie oznajmującym, bez kropki (np. `feat: scaffolding i konfiguracja`).
