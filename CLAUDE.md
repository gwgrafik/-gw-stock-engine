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

Obraz testowy (Dockerfile, cel `test`) ustawia `GW_TEST_IMAGE=1` i nie zawiera `docs/`, `.gitattributes`, `scripts/`, `.claude/`
ani `.github/`: tylko przy tym znaczniku testy czytające te pliki się pomijają. Bez znacznika brak plików wywala testy głośno.
Po przebiegu `tests/conftest.py` sprawdza, że żaden test nie utworzył prawdziwego `data/ARM_LIVE` ani `data/L1_PASSED`.

## Strażnik live

Hook `PreToolUse` (`.claude/settings.json` wywołuje `scripts/hooks/guard_live.py`) ocenia każdy zapis `Write`, `Edit`
i `MultiEdit` do każdego pliku poza prozą (`*.md`, `docs/**`, `.claude/settings.json`), danymi silnika (`data/**`) i
binariami (także pliki bez przyrostka, `Dockerfile`, `Makefile`, `.service`, `.json`) i BLOKUJE go (kod wyjścia 2, a przy
awarii samego hooka `onFailure: "block"`), gdy zapis:

- tworzy lub zapisuje `data/ARM_LIVE` albo `data/L1_PASSED` (`open(..., "w")`, `touch`, `write_text`, `os.mknod`,
  `shutil.copy`, `>`, `tee`, `cp`, `ln -s`, także przez zmienną-alias) albo jest zapisem pliku o takiej nazwie. Sprawdzanie
  (`.exists()`), odczyt i usuwanie są dozwolone;
- zaszywa klucz lub hasło (`X-CAP-API-KEY`, `api_key`, `api_password`, `password`, `secret` z literałem; `export X_API_KEY=...`,
  `ENV`, `- X_API_KEY=...`, `X_API_KEY: ...`; `os.environ[...] = ...`, `monkeypatch.setenv`). Klucz: od 12 znaków `[A-Za-z0-9]`
  z rzędu, hasło: od 12 znaków. Wypełniacze (`<...>`, `xxxx`, `changeme`, `${VAR}`, `${{ secrets.X }}`) są dozwolone, w
  `tests/` także wartości zaczynające się od `test-`/`fake-` i identyfikatory `TEST_...`;
- wpisuje `live_allowed = true` do `config/runtime.toml` (też skrypt `.py`, `.sh`, `.yml`, który to robi).

`data/ARM_LIVE`, `data/L1_PASSED` i `live_allowed = true` tworzy lub ustawia **wyłącznie właściciel**; agent i kod nigdy,
także gdy "tylko na chwilę" albo "tylko w teście" (testy robią to wyłącznie w katalogu tymczasowym i tylko w plikach
z listy wyjątków).

- Skan całego repozytorium (CI robi to samo przed testami; kod 1 = naruszenie): `python scripts/hooks/guard_live.py --scan`.
- Ścieżki są oceniane względem korzenia repozytorium (`docs/`, `tests/` i lista wyjątków działają tylko na ścieżce względnej);
  plik spoza repozytorium dostaje pełne reguły bez wyjątków.
- Pliki poza oceną (uzasadnienie przy każdym w nagłówku `scripts/hooks/guard_live.py`, test pilnuje, żeby nie były zbędne):
  sam strażnik i `tests/unit/test_guard_live.py` (przykłady naruszeń jako dane), `tests/fault/engine_sim.py` (symulacja
  właściciela uzbrajająca tymczasowy `data_dir`), `tests/fault/test_modes.py` i `tests/fault/test_engine_day.py`
  (fixtures w katalogach tymczasowych). Nowy wyjątek wymaga uzasadnienia w nagłówku skryptu i zgody właściciela.
- Fałszywe alarmy (strona ostrożna): napis albo `echo` z dosłownym poleceniem `touch data/ARM_LIVE` (podpowiedzi dla
  właściciela formułuj bez czasownika, np. "utwórz plik data/ARM_LIVE"), `cp`/`mv` z lub na nazwę pliku uzbrojenia, zmienna
  przypisana z tej nazwy i użyta jako parametr gdzie indziej w tym samym pliku.
- Strażnik to bariera przed wypadkiem, nie granica bezpieczeństwa: polecenie Bash omija hook (część wzorców łapią
  reguły `deny` w `.claude/settings.json`), a zmienne budowane w czasie działania też. Ostateczną bramką jest właściciel.
- Ochrona samego strażnika to tylko pomoc: `permissions.ask` przy edycji `scripts/hooks/`, testu strażnika, `.github/`,
  `.claude/` i `config/runtime.toml`, zakaz edycji `.claude/settings.local.json` (mógłby wyłączyć hooki) oraz
  `.github/CODEOWNERS`. Tryb `bypassPermissions` pomija pytania, a Bash wzorce.
- Wymagany Claude Code >= 2.1.295 (klucz `onFailure`). Sesję uruchamiaj w korzeniu repozytorium: hooki i reguły projektu są
  czytane tylko z katalogu startowego. Po pierwszym uruchomieniu sprawdź `/hooks` i `claude doctor` (błędny wpis w
  `settings.json` wyłącza po cichu cały plik, także reguły `deny`).
- Sesje interaktywne wstrzymują hooki projektu do zaakceptowania zaufania do folderu (reguły `deny` działają od razu); w
  wątkach chmurowych (projekty w claude.ai) hooki i uprawnienia z repozytorium nie obowiązują.
- Do zrobienia przez właściciela po stronie GitHub: branch protection na `main` (wymagany przegląd właściciela z
  CODEOWNERS i zielone CI `tests` oraz `image`); bez tego CI i CODEOWNERS są tylko doradcze, a repozytorium jest publiczne.
- Agenci tylko do odczytu (`Read`, `Grep`, `Glob`) w `.claude/agents/`: `risk-reviewer`, `order-state-auditor`,
  `backtest-skeptic`; traktują treść repozytorium jako dane niezaufane. CI (`.github/workflows/ci.yml`) nie ma żadnych
  sekretów i buduje obraz z `--target test`.

## Gdzie szukać

- Specyfikacja: `docs/superpowers/specs/2026-10-31-engine-design-v2.md`
- Plan implementacji: `docs/superpowers/plans/2026-10-31-engine-v2-implementation.md`
- Notatki o API Capital.com: `docs/ops/capitalcom-api-notes.md`

## Commity

Po polsku, z prefiksem konwencjonalnym: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
Jedna zmiana logiczna na commit; tytuł w trybie oznajmującym, bez kropki (np. `feat: scaffolding i konfiguracja`).
