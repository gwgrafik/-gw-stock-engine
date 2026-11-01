# Obraz silnika gw-stock-engine (spec §13): minimalny, bez powłoki w CMD, bez sekretów, bez narzędzi sieciowych.
#
# Cele budowy (wspólna baza, jeden kontekst = korzeń repozytorium):
#   runtime  (DOMYŚLNY, ostatni etap): tylko requirements.lock + src/. Konfigurację (/app/config), dane (/data) i klucz
#            (/secrets/capitalcom.json) dostarcza Compose przez montowania; w obrazie ich nie ma.
#   test     : requirements-dev.lock + src/ + tests/ + config/ + docker/, uruchamia pytest bez testów live.
#
# Budowa:  docker compose -f docker/compose.yml build gw-stock-engine
# Testy:   docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
#
# Baza przypięta tagiem I skrótem manifestu (indeks wieloarchitekturowy, odczytany z rejestru 2026-11-01). Bez ruchomych tagów.
# Zmiana bazy = świadoma decyzja: nowy tag + nowy skrót, potem pełny przebieg testów i pomiaru pamięci z runbooka.
FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS base

# PYTHONDONTWRITEBYTECODE: system plików kontenera jest tylko do odczytu (bytecode kompilujemy przy budowie).
# HOME=/tmp: kontener działa jako obcy uid bez katalogu domowego; /tmp to tmpfs w Compose.
# PYTHONPATH: pakiet gwstock nie jest instalowany pipem (jedyna zależność wykonania to aiohttp), tylko leży w /app/src.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Domyślny użytkownik bez uprawnień (uid 10001). Compose nadpisuje go uid/gid właściciela plików na hoście
# (`user: ${GW_UID}:${GW_GID}`), żeby plik klucza z uprawnieniami 600 był czytelny w kontenerze.
RUN groupadd --gid 10001 gw \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin gw


# ---------------------------------------------------------------------------------------------------------------------
FROM base AS test

# Znacznik obrazu testowego: testy czytające docs/, .gitattributes, scripts/ lub .github/ pomijają się TYLKO przy nim
# (bez znacznika brak tych plików ma głośno wywalać testy, a nie po cichu je wyłączać).
ENV GW_TEST_IMAGE=1

COPY requirements.lock requirements-dev.lock /app/
RUN pip install -r requirements-dev.lock

COPY pyproject.toml /app/pyproject.toml
COPY src/ /app/src/
COPY tests/ /app/tests/
COPY config/ /app/config/
COPY docker/ /app/docker/
COPY .dockerignore /app/.dockerignore

RUN chmod -R a+rX /app \
 && python -c "import zoneinfo, gwstock.calendar; zoneinfo.ZoneInfo('Europe/Berlin')"

USER 10001:10001
CMD ["python", "-m", "pytest", "-m", "not integration_live_readonly", "-q", "-p", "no:cacheprovider"]


# ---------------------------------------------------------------------------------------------------------------------
# Ostatni etap = domyślny cel `docker build` bez `--target`.
FROM base AS runtime

COPY requirements.lock /app/requirements.lock
RUN pip install -r requirements.lock

COPY src/ /app/src/

# Uprawnienia do odczytu dla każdego uid (plik w kontekście mógł mieć umask 077), bytecode skompilowany przy budowie,
# a baza stref czasowych sprawdzona teraz: kalendarz XETRA buduje ZoneInfo("Europe/Berlin") przy imporcie, więc brak
# bazy tz w obrazie ma zatrzymać budowę, a nie pierwszy start na VPS.
RUN chmod -R a+rX /app \
 && python -m compileall -q /app/src \
 && python -c "import zoneinfo, gwstock.calendar; zoneinfo.ZoneInfo('Europe/Berlin')"

USER 10001:10001
CMD ["python", "-m", "gwstock"]
