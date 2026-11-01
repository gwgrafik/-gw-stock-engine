# Runbook właściciela: gw-stock-engine na VPS

Dokument dla właściciela, który nie musi umieć programować. Każdy blok `bash` to **jedno polecenie** (albo jeden krótki skrypt). Kopiujesz go w całości i wklejasz do terminala. Nie wpisuj znaku `$` na początku. Wynik polecenia jest opisany zawsze tekstem pod blokiem, nigdy w bloku.

Silnik to **narzędzie pomiarowe**: sprawdza, czy strategia i infrastruktura działają zgodnie ze specyfikacją. Nie obiecuje zysku, a wynik pojedynczej sesji to grosze i szum.

Wszystkie polecenia silnika z tego dokumentu istnieją w kodzie i zostały sprawdzone z jego pomocą (`--help`), także `clear-kill`, `reset-position`, `gwstock.prereg` i `gwstock.report`. Odbiór całości (kryteria akceptacji, bramki L0 i L1, miejsce na Twój podpis) jest w `docs/ops/acceptance-v1.md`.

Spis: 0 Zasady, 1 Pierwsze uruchomienie, 2 Tryb `dry_run`, 3 Weryfikacja pierwszej świecy, 4 Ćwiczenia awaryjne, 5 Próba `smoke`, 6 Uzbrajanie, 7 Pierwsze 5 sesji i bramka L1, 8 Operacje, 9 Rozwiązywanie problemów, 10 Lista kontrolna, Dodatek A (plik z kluczem).

---

## 0. Zasady

1. **Nic nie wchodzi na VPS bez pomiaru pamięci i Twojej zgody.** Na serwerze działa bot z prawdziwymi pieniędzmi (Solana), n8n i boty OKX, a wolnej pamięci jest około 700 MB. W tym dokumencie są dwie „bramy zgody" (punkty 1.2 i 2.1): przed każdą z nich wykonujesz pomiar, porównujesz go z progami i dopiero wtedy decydujesz. Wątpliwość oznacza: nie idź dalej i zapytaj.
2. **Klucz nigdy w czacie, w repozytorium, w poleceniach ani w logach.** Repozytorium jest publiczne. Klucz leży wyłącznie w pliku `/home/claude/.config/gw/capitalcom.json` (tryb 600) i trafia do kontenera tylko do odczytu. Zawartości tego pliku nigdy nie wypisujesz w terminalu. Wyniki poleceń z tego dokumentu są bezpieczne do wklejenia w czacie, z jednym wyjątkiem: `bind-account` wypisuje numery rachunków i salda, ich nie wklejaj.
3. **Kod nigdy sam nie uzbraja handlu.** Uzbrajasz wyłącznie Ty (punkt 6): plikiem `data/ARM_LIVE` i wpisem `live_allowed = true`. Bez tego silnik pracuje w trybie `dry_run`: robi wszystko (logowanie, notowania, decyzje), ale **nie wysyła zleceń**.
4. **Restart zawsze oznacza „zatrzymaj, potem uruchom"**: dwa osobne polecenia, jedno po drugim. Nigdy nakładanie dwóch instancji. Nie używaj polecenia `restart` Composa ani `down`; `down` usuwa kontener razem z jego logami.
5. **SSH jako `root`, ale wszystkie polecenia projektu jako użytkownik `claude`** (to on jest właścicielem pozostałych botów i kluczy). Każde polecenie z tego dokumentu wykonujesz w katalogu `/home/claude/gw-stock-engine`, chyba że napisano inaczej.
6. **Wątpliwość oznacza STOP.** Jeśli coś wygląda dziwnie, użyj jednego z „szybkich hamulców" poniżej, a przy prawdziwej pozycji sprawdź aplikację Capital.com. Każdy z nich jest bezpieczny.
7. **Nie edytuj kodu ani plików `config/strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`.** Ich zmiana tworzy nowy wariant strategii i unieważnia pre-rejestrację. Jedyny plik konfiguracji, który zmieniasz Ty, to `config/runtime.toml` (i tylko podanymi poleceniami).
8. **Czas.** Godziny w tym dokumencie to czas Europe/Berlin (taki sam jak w Holandii). Logi Dockera i pliki w `data/` są w UTC: zimą Berlin = UTC + 1 godzina (do 28.03.2027), latem UTC + 2.

### Szybkie hamulce (gdy coś wygląda dziwnie)

Hamulec 1: brak nowych wejść. Silnik działa dalej i zarządza istniejącą pozycją (miękki kill switch, zdejmuje go tylko `clear-kill`, punkt 8.4):

```bash
touch data/STOP
```

Hamulec 2: zdjęcie uzbrojenia. Działający silnik przestaje wchodzić w nowe pozycje natychmiast i **do restartu** (ponowne uzbrojenie wymaga restartu: zatrzymaj, utwórz `data/ARM_LIVE`, uruchom):

```bash
rm -f data/ARM_LIVE
```

Hamulec 3: zatrzymanie silnika. Otwarta pozycja zostaje u brokera ze stopem:

```bash
docker compose -f docker/compose.yml stop
```

### Ograniczenia / do potwierdzenia

Te pozycje nie zostały jeszcze sprawdzone na prawdziwym koncie i **rozstrzygają je dopiero `dry_run` i próba `smoke`** (numery C odnoszą się do `docs/ops/capitalcom-api-notes.md`, sekcja 6):

| Co | Dlaczego ważne | Gdzie się rozstrzyga |
|---|---|---|
| Pola stopu w liście pozycji (C5, C24, C6): czy broker w ogóle pokazuje poziom stopu i skąd liczy odległość | Jeśli broker nie pokazuje stopu, silnik zapisuje stop „wyliczony z naszego żądania" i `smoke` wypisze o tym ostrzeżenie; bez zobaczenia stopu w aplikacji nie uzbrajaj | `smoke` (punkt 5), Twoje oczy w aplikacji |
| Drugie zamknięcie tej samej pozycji (C29, C22): czy broker odpowiada czystym „nie znaleziono" | Awaryjne spłaszczenie może wysłać drugie zamknięcie; jeśli broker odpowiedziałby inaczej, mogłaby powstać nowa pozycja | `smoke`, krok [6] |
| Znacznik czasu świecy (C9, stała `CANDLE_STAMP_IS_PERIOD_START`) | Pomyłka wybrałaby świecę sprzed otwarcia zamiast pierwszej | Bramka L0, punkt 3 |
| Opóźnienie i limity historii (C7, C7d, C27, C28): kiedy wpis o zamknięciu stopem pojawia się w historii i czy odpowiedź ma limit wierszy | Silnik księguje zamknięcie stopem z historii; zbyt duże opóźnienie opóźnia księgowanie | `smoke`, krok [8], i pierwsze sesje |
| Kod błędu przy zamkniętym rynku (C14b), tryb hedgingu (C15), potwierdzenia po zmianie i zamknięciu (C3) | Wpływają tylko na klasę odmowy lub rzadkie ścieżki; adapter zachowuje się ostrożnie | pierwsze realne odmowy w dzienniku |

---

## 1. Pierwsze uruchomienie na VPS

Cel: zbudować obraz, uruchomić testy w obrazie, zmierzyć pamięć, powiązać rachunek. **Żadne zlecenie nie jest wysyłane.** Silnik jako usługa nie jest jeszcze uruchamiany.

### 1.1 Wejście na serwer jako `claude`

Zaloguj się przez SSH jako `root`, a potem przełącz się na użytkownika `claude`:

```bash
su - claude
```

Sprawdź, kim jesteś:

```bash
whoami
```

Ma wyświetlić `claude`. Za każdym razem, gdy połączenie SSH się rozłączy, powtórz te dwa polecenia oraz przejście do katalogu projektu (punkt 1.4).

### 1.2 Pomiar stanu serwera (tylko odczyt) i brama zgody nr 1

Poniższe polecenie niczego nie uruchamia. Wypisuje stan serwera i zapisuje go w pliku `~/gw-pomiar-przed.txt`, żebyś mógł go później porównać:

```bash
(
date -u
echo "--- pamięć (MB)"; free -m
echo "--- dysk"; df -h /var/lib /home
echo "--- wersje"; docker version --format 'Docker {{.Server.Version}}'; docker compose version
echo "--- cgroup (cgroup2fs oznacza v2)"; stat -fc %T /sys/fs/cgroup
echo "--- swap w systemie"; cat /proc/swaps
echo "--- docker info (cgroup, swap, ostrzeżenia)"; docker info 2>&1 | grep -iE 'cgroup|swap|warning'
echo "--- kontenery na serwerze (pamięć)"; docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'
) 2>&1 | tee ~/gw-pomiar-przed.txt
```

Jak czytać wynik i **brama zgody nr 1** (przed jakąkolwiek budową obrazu):

| Co | Zielone: idź dalej | Żółte: zatrzymaj się i zapytaj Claude | Czerwone: STOP |
|---|---|---|---|
| Pamięć serwera: sekcja „pamięć (MB)", kolumna `available` | co najmniej 600 MB | 450 do 599 MB | poniżej 450 MB |
| Wolne miejsce na dysku: sekcja „dysk", kolumna `Avail` dla `/var/lib` | co najmniej 3 GB | 2 do 3 GB | poniżej 2 GB |
| Wersja cgroup: sekcja „cgroup" | `cgroup2fs` | – | `tmpfs` (cgroup v1): nie idź dalej, procedura pomiaru jest wtedy inna |
| Ostrzeżenia w sekcji „docker info" | brak linii z „No swap limit support" | – | jest taka linia: nie idź dalej |

Dlaczego te progi: budowa obrazu i uruchomienie testów zużywają chwilowo do ok. 400 MB (kontener testowy ma twardy limit 384 MB i jest pierwszą ofiarą jądra przy braku pamięci), a po nich silnik ma stale mieścić się w 256 MB. Wynik wklej Claude (jest bezpieczny) i przejdź dalej dopiero po jego „ok" albo po spełnieniu zielonych progów. **Zgodę na budowę i testy wyrażasz właśnie tym krokiem.**

### 1.3 Kontrola pliku z kluczem (bez wypisywania wartości)

Sprawdź właściciela i uprawnienia katalogu oraz pliku:

```bash
stat -c '%U:%G %a %n' /home/claude/.config/gw /home/claude/.config/gw/capitalcom.json
```

Oczekiwane dwie linie: `claude:claude 700 /home/claude/.config/gw` i `claude:claude 600 /home/claude/.config/gw/capitalcom.json`. Jeśli pliku nie ma, użyj Dodatku A (zakładanie pliku) i wróć tutaj. Jeśli plik jest, ale ma inne uprawnienia, popraw je:

```bash
chmod 700 /home/claude/.config/gw && chmod 600 /home/claude/.config/gw/capitalcom.json
```

Jeśli właścicielem nie jest `claude`, nie poprawiaj tego sam: zapytaj.

Sprawdź nazwy pól. To polecenie wypisuje **same nazwy**, nigdy wartości:

```bash
grep -o '"[A-Za-z0-9_]*" *:' /home/claude/.config/gw/capitalcom.json
```

Ma wypisać dokładnie trzy nazwy: `"api_key":`, `"identifier":` i `"api_password":` (w dowolnej kolejności), nic więcej i nic mniej; dodatkowe pole z wielką literą lub cyfrą też byłoby tu widoczne. Numeru rachunku w tym pliku **nie ma**: rachunek wskaże polecenie `bind-account` (punkt 1.11). Dodatkowe pole spowoduje odmowę startu.

Sprawdź, czy plik jest poprawnym JSON-em (bez wypisywania zawartości):

```bash
python3 -c "import json; json.load(open('/home/claude/.config/gw/capitalcom.json')); print('JSON poprawny')"
```

### 1.4 Pobranie kodu

Nazwa repozytorium zaczyna się od myślnika, dlatego polecenie podaje pełny adres i jawny katalog docelowy. Pobiera gałąź `feat/engine-v2`:

```bash
git clone --branch feat/engine-v2 https://github.com/gwgrafik/-gw-stock-engine.git /home/claude/gw-stock-engine
```

Przejdź do katalogu projektu (od teraz wszystkie polecenia wykonuj tutaj):

```bash
cd /home/claude/gw-stock-engine
```

Sprawdź, jaka wersja kodu jest pobrana:

```bash
git log --oneline -3
```

Najwyższa linia ma mieć skrót i opis, które poda Ci Claude jako „aktualna wersja do wdrożenia". Jeśli katalog już istnieje, klonowanie zakończy się błędem: wtedy nic nie nadpisuj, tylko zapytaj.

### 1.5 Użytkownik kontenera i katalog danych

Kontener ma działać jako ten sam użytkownik, który jest właścicielem pliku z kluczem (inaczej plik z uprawnieniami 600 byłby w kontenerze nieczytelny). Jedno polecenie zapisuje to na stałe w pliku `docker/.env` (plik jest ignorowany przez git i nie zawiera sekretów):

```bash
printf 'GW_UID=%s\nGW_GID=%s\n' "$(id -u claude)" "$(id -g claude)" > docker/.env
```

Sprawdź zawartość:

```bash
cat docker/.env
```

Mają być dwie linie: `GW_UID=` i `GW_GID=` z liczbami użytkownika `claude` (zwykle `1000`). Utwórz katalog danych dla `claude` z dostępem tylko dla niego. **Zrób to przed pierwszym uruchomieniem Dockera**, bo inaczej Docker utworzyłby ten katalog jako `root` i silnik nie mógłby w nim pisać:

```bash
mkdir -p data && chmod 700 data
```

```bash
ls -ld data
```

Ma pokazać `drwx------` i właściciela `claude claude`.

### 1.6 Kontrola pliku Compose (nic nie uruchamia)

Walidacja składni i zmiennych. Brak jakiegokolwiek wyniku oznacza sukces; komunikat o brakującej zmiennej `GW_UID` oznacza błąd w punkcie 1.5:

```bash
docker compose -f docker/compose.yml config -q
```

Zobacz, co Docker faktycznie dostanie (tylko wybrane linie):

```bash
docker compose -f docker/compose.yml config | grep -E 'mem_limit|memswap_limit|mem_reservation|oom_score_adj|pids_limit|cpus|read_only|user:|restart|ports'
```

Oczekiwane wartości: `cpus: 0.5`, `mem_limit: "268435456"`, `mem_reservation: "134217728"`, `memswap_limit: "268435456"`, `oom_score_adj: 1000`, `pids_limit: 64`, `read_only: true`, `restart: unless-stopped`, `user: 1000:1000` (lub inne liczby użytkownika `claude`). Kolejność linii bywa inna, a dwie dodatkowe linie `read_only: true` to montowania konfiguracji i klucza tylko do odczytu: to normalne. Linii `ports` **nie ma** i tak ma być: silnik niczego nie wystawia na zewnątrz.

### 1.7 Obraz testowy i testy w obrazie

Budowa obrazu testowego (kilka minut, potrzebuje internetu do pobrania obrazu bazowego i pakietów). Ten sam obraz służy dwóm usługom: testom logiki (punkt 1.7) i odczytom live (punkt 1.8):

```bash
docker compose -f docker/compose.yml --profile test build gw-stock-test
```

Uruchomienie testów logiki w obrazie. Kontener testowy **nie ma klucza ani sieci** (najmniejsze uprawnienia: testy używają tylko lokalnych atrap), działa z systemem plików tylko do odczytu i ma limit pamięci 384 MB:

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
```

Ostatnia linia ma mieć postać `... passed, ... skipped, ... deselected in ...` (liczby będą się zmieniać z wersją kodu) i **nie może** zawierać słów `failed` ani `error`. Pominięte (`skipped`) testy to te, które wymagają Dockera albo katalogu `docs/`, nieobecnych w obrazie. Jeśli kontener zostanie zabity z kodem 137 (brak pamięci), nie ponawiaj: zgłoś to.

### 1.8 Odczyty live tylko do odczytu (test klucza)

Osobna usługa `gw-stock-live-readonly` dostaje klucz tylko do odczytu i sieć, a dane silnika tylko do odczytu. Uruchamia wyłącznie testy `integration_live_readonly`: **tylko czytają** konto, reguły DE40, notowania, świece i historię; żadnych zleceń (strażnik w teście przerywa każde żądanie inne niż odczyt i logowanie). **Każdy z 8 testów loguje się osobno, więc pełny zestaw to 8 logowań** w odstępach ponad 1,2 sekundy. Uruchamiaj go rzadko (raz teraz) i wyłącznie wtedy, gdy silnik nie działa:

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-live-readonly
```

Wszystkie 8 testów ma mieć wynik `PASSED`. Jeśli widzisz `SKIPPED`, kontener nie widzi pliku z kluczem (wróć do punktów 1.3 i 1.5). Jeśli widzisz `FAILED`, **nie ponawiaj pełnego zestawu** (każdy przebieg to 8 kolejnych logowań, patrz punkt 9): zapisz wynik i zapytaj.

**Pojedyncze logowanie.** Gdy trzeba tylko sprawdzić, czy klucz działa (po `bind-account`, po wymianie klucza, po błędzie logowania), użyj jednego testu: loguje się dokładnie **raz** i sprawdza konto (a po `bind-account` także powiązany rachunek):

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-live-readonly python -m pytest -m integration_live_readonly -k test_accounts_listed -v -p no:cacheprovider
```

Wynik: `1 passed`.

### 1.9 Obraz silnika

```bash
docker compose -f docker/compose.yml build gw-stock-engine
```

```bash
docker images gw-stock-engine --format '{{.Repository}}:{{.Tag}} {{.Size}}'
```

Oczekiwane: `gw-stock-engine:local` o rozmiarze ok. 130 MB i `gw-stock-engine:test` ok. 150 MB.

### 1.10 Próba izolacji i limitów (krótki kontener, kilka sekund)

Ten skrypt uruchamia na chwilę kontener silnika z jego ustawieniami i sprawdza: kto jest użytkownikiem, czy klucz jest czytelny, gdzie wolno pisać (tylko `/data` i `/tmp`, a `/tmp` ma 32 MiB), czy limity pamięci, swapu, procesów i `oom_score_adj` zostały naprawdę nałożone przez jądro. Niczego nie wypisuje z klucza:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python - <<'EOF'
import os
import stat

problems = []


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def check(label, got, expected):
    ok = got == expected
    print(("OK      " if ok else "BŁĄD    ") + f"{label}: {got} (oczekiwano {expected})")
    if not ok:
        problems.append(label)


def can_write(path):
    probe = os.path.join(path, ".probe")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


print("== użytkownik, klucz, zapis")
print("uid:gid w kontenerze:", f"{os.getuid()}:{os.getgid()}")
check("kontener nie działa jako root", os.getuid() != 0, True)
key = "/secrets/capitalcom.json"
check("klucz jest plikiem (nie katalogiem)", os.path.isfile(key), True)
check("klucz czytelny w kontenerze", os.access(key, os.R_OK), True)
check("uprawnienia klucza", stat.filemode(os.stat(key).st_mode), "-rw-------")
for path, expected in (("/data", True), ("/tmp", True), ("/app", False), ("/app/config", False)):
    check("zapis do " + path, can_write(path), expected)
check("konfiguracja czytelna", os.access("/app/config/strategy_v1.toml", os.R_OK), True)
tmp = os.statvfs("/tmp")
check("rozmiar /tmp w MiB", round(tmp.f_blocks * tmp.f_frsize / 1048576), 32)

print("== limity nałożone przez jądro")
if os.path.exists("/sys/fs/cgroup/memory.max"):
    cg = "/sys/fs/cgroup/"
    check("memory.max (limit pamięci)", read(cg + "memory.max"), "268435456")
    check("memory.swap.max (swap wyłączony)", read(cg + "memory.swap.max"), "0")
    check("pids.max (limit procesów)", read(cg + "pids.max"), "64")
    print("memory.low (rezerwacja):", read(cg + "memory.low"), "(oczekiwano 134217728)")
    print("cpu.max:", read(cg + "cpu.max"), "(oczekiwano 50000 100000)")
else:
    print("UWAGA: cgroup v1 (inne pliki niż w v2). Sprawdzam odpowiedniki.")
    cg = "/sys/fs/cgroup/"
    check("memory.limit_in_bytes", read(cg + "memory/memory.limit_in_bytes"), "268435456")
    check("memory.memsw.limit_in_bytes (swap wyłączony)", read(cg + "memory/memory.memsw.limit_in_bytes"), "268435456")
    check("pids.max", read(cg + "pids/pids.max"), "64")
check("oom_score_adj procesu 1", read("/proc/1/oom_score_adj"), "1000")
nnp = [line.split()[1] for line in open("/proc/self/status") if line.startswith("NoNewPrivs")]
check("no-new-privileges", nnp[0] if nnp else None, "1")

print()
print("WYNIK: WSZYSTKO OK" if not problems else "WYNIK: BŁĄD, NIE KONTYNUUJ: " + ", ".join(problems))
raise SystemExit(1 if problems else 0)
EOF
```

Compose wypisze przy okazji kilka linii o tworzeniu sieci i kontenera: to normalne. Ostatnia linia ma brzmieć **`WYNIK: WSZYSTKO OK`**. Najważniejsza jest linia `memory.swap.max` z wartością `0`: oznacza, że kontener naprawdę nie może używać swapu. Jeśli jest inna wartość albo pliku brakuje, jądro VPS nie egzekwuje `memswap_limit`: **STOP, nie uruchamiaj silnika** i zgłoś wynik (to jest pytanie otwarte 9 ze specyfikacji, decyzja należy do Ciebie i Claude).

### 1.11 Powiązanie rachunku (`bind-account`)

Silnik handluje tylko na rachunku, który sam wskażesz. Polecenie jest interaktywne i **wymaga, żeby silnik nie działał** (dziś jeszcze nie działa). Loguje się raz do Capital.com i **niczego nie kupuje**:

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock bind-account
```

Co zobaczysz i co wpisujesz (komunikaty programu są po angielsku):

1. Lista rachunków tego loginu, każdy w osobnej linii w postaci `1) <numer rachunku>  currency EUR  status ENABLED  balance ...  available ...`. Lista jest tylko na Twoim terminalu i nic nie jest jeszcze zapisane. Numerów **nie wklejaj** w czacie.
2. Pytanie `Number of the account to bind (1-N):`. Wpisz numer pozycji z listy (cyfrę, nie numer rachunku) dla rachunku **w EUR, `ENABLED` i z saldem większym od zera**, czyli tego z Twoimi 40 EUR. Rachunku z saldem 0, w innej walucie albo nieaktywnego program odmówi.
3. Pytanie `Type the last 4 characters of the chosen account id to confirm:`. Wpisz ostatnie 4 znaki numeru wybranego rachunku z listy z kroku 1. To jawne potwierdzenie.
4. Wynik: `Bound account ... written to ... (mode 600).` Plik `data/account.json` powstał. Przy pomyłce program pisze `bind-account aborted: ... nothing written` i nic nie zapisuje: uruchom polecenie jeszcze raz. Jeśli silnik działa, program odmówi (`refused: another engine instance holds ...`) i do niczego się nie zaloguje: najpierw `stop`.

Compose sam przydziela terminal, więc nie trzeba żadnych dodatkowych opcji. Przy okazji mogą pojawić się linie logu z godziną i słowem `INFO`: to normalne, zignoruj je.

Jeśli wybierasz inny rachunek niż poprzednio powiązany, program każe wpisać słowo `OVERWRITE`. Sprawdź w aplikacji Capital.com, że wybrałeś rachunek z saldem 40 €. Potem wykonaj **pojedyncze logowanie** z punktu 1.8 (polecenie z `-k test_accounts_listed`): tym razem sprawdzi ono także powiązany rachunek. Pełnego zestawu 8 logowań nie powtarzaj.

### 1.12 Stan silnika (`status`)

Polecenie tylko czyta pliki i dziennik, nie loguje się do brokera i niczego nie zmienia:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

Oczekiwane, bo silnik jeszcze nie pracował: linia `mode (files): dry_run (...)` z listą brakujących warunków uzbrojenia (to dobrze), `heartbeat: none` i `journal: none yet`.

---

## 2. Uruchomienie w trybie `dry_run`

### 2.0 Zamrożenie pre-rejestracji

Specyfikacja (sekcja 11) wymaga zamrożenia odcisków konfiguracji **przed pierwszą sesją `dry_run`**. Odcisk to skrót SHA-256 plików `config/strategy_v1.toml`, `risk_v1.toml` i `costs_v1.toml` (bajt po bajcie) razem z nazwą adaptera i klasą instrumentu; pliku `runtime.toml` w nim nie ma. Silnik porównuje go przy każdym starcie z zamrożonym i przy różnicy zostaje w `dry_run`.

**Zasady, od których zależy, czy to zadziała:**

- Zamrażasz **na serwerze, w katalogu projektu, poleceniami z Compose** (poniżej). Plik `data/preregistration.json` powstaje wtedy z numerem właściciela `GW_UID` i uprawnieniami 600, więc silnik w kontenerze może go przeczytać. Plik utworzony z innego konta (np. po uruchomieniu programu bezpośrednio jako `root`, poza Composem) ma uprawnienia 600 i innego właściciela: kontener go nie przeczyta, a silnik zostanie w `dry_run` z powodem `no frozen pre-registration fingerprint`.
- **Nigdy nie otwieraj i nie zapisuj ponownie** plików `strategy_v1.toml`, `risk_v1.toml` i `costs_v1.toml` (edytor, Notatnik, kopiowanie przez Windows zmieniają końce linii albo dodają znacznik BOM, a zmiana jednego bajtu zmienia odcisk). Silnik nadal by taki plik wczytał, ale odcisk byłby inny. Na serwerze pliki pochodzą z repozytorium (końce linii LF pilnuje plik `.gitattributes`) i tak mają zostać.
- **Po każdym pobraniu kodu i po każdym wdrożeniu** wykonaj kontrolę `--check` z tego punktu (w punkcie 8.2 jest to osobny krok). Pobranie samych dokumentów nie zmienia odcisku.

Pokaż, co zostałoby zamrożone (niczego nie zapisuje):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --print
```

Zamroź (zapisuje `data/preregistration.json` i wypisuje gotowy blok z odciskami; jeśli ta sama konfiguracja jest już zamrożona, niczego nie zmienia):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --write
```

Polecenie **tylko wypisuje** blok z odciskami. Kontener nie ma dostępu do dokumentów (jedyne miejsca zapisu to `/data` i `/tmp`), więc nie zapisuje `docs/preregistration/v1.md` i nie robi żadnego commita. Wklej cały wydruk w czacie (zawiera same skróty, jest bezpieczny) razem z datą pierwszego dnia `dry_run`. Claude wpisze blok i datę do `docs/preregistration/v1.md` i zatwierdzi dokument w repozytorium; potem pobierzesz go przy najbliższej aktualizacji (punkt 8.2). Dopóki odciski nie są wpisane w dokumencie, pre-rejestracja nie jest kompletna.

Kontrola, czy bieżąca konfiguracja zgadza się z zamrożoną (zaraz po zamrożeniu, po każdej aktualizacji i przed uzbrojeniem):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --check
```

Wynik: linia `OK: the configuration matches the frozen pre-registration` i kod wyjścia 0 to zgodność. Kod 1 (`MISMATCH: ...`, z nazwami plików, które się różnią) znaczy, że konfiguracja jest inna niż zamrożona. Kod 2 (`NOT FROZEN: ...`) znaczy, że nic nie zamrożono albo plik jest nieczytelny. W obu przypadkach silnik zostaje w `dry_run`.

**Przy niezgodności (kod 1) nie używaj `--force`.** Wklej wynik Claude i nic nie zmieniaj, dopóki nie odpowie. Jeśli niczego świadomie nie zmieniałeś, a różnią się same końce linii (typowo po przeniesieniu pliku przez Windows), właściwe jest przywrócenie plików z repozytorium, a nie nowy wariant. Wykonaj to polecenie **dopiero po potwierdzeniu przez Claude**, a potem powtórz `--check`:

```bash
git checkout -- config/strategy_v1.toml config/risk_v1.toml config/costs_v1.toml
```

Opcja `--force` służy wyłącznie do **świadomego rozpoczęcia nowego wariantu strategii** (K+1), opisanego w `docs/preregistration/v1.md`, sekcja 7: Claude najpierw dopisuje wiersz do tabeli wariantów, dopiero potem zamrażasz od nowa. Program przy niezgodnej konfiguracji prosi o wpisanie słowa `FORCE` i czyta je ze standardowego wejścia, dlatego polecenie podaje je z rurki. Użyj go wyłącznie na wyraźne polecenie Claude i nie uzbrajaj po takiej zmianie bez ustalenia z nim dalszych kroków:

```bash
echo FORCE | docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --write --force
```

Od zamrożenia **nie zmieniaj** `config/strategy_v1.toml`, `risk_v1.toml` ani `costs_v1.toml`. Zmiana unieważnia pre-rejestrację i tworzy nowy wariant (zwiększa liczbę prób K).

### 2.1 Brama zgody nr 2: pierwszy start silnika

Przed pierwszym uruchomieniem usługi sprawdź ponownie wolną pamięć:

```bash
free -m
```

Warunki startu (wszystkie naraz):

- kolumna `available` co najmniej **500 MB** (w najgorszym razie silnik zajmie 256 MB, więc reszta serwera zachowa co najmniej 244 MB); 400 do 499 MB to żółte światło: zapytaj; poniżej 400 MB: STOP,
- w punkcie 1.10 jest `WYNIK: WSZYSTKO OK` z `memory.swap.max` równym `0`,
- testy z punktu 1.7 przeszły bez `failed`,
- pre-rejestracja jest zamrożona (punkt 2.0: `--check` kończy się `OK`, a Claude wpisał odciski do dokumentu): specyfikacja wymaga tego przed pierwszą sesją `dry_run`,
- po stronie Capital.com nic nie jest w trakcie (silnik w `dry_run` i tak nie wysyła zleceń).

**Zgoda: uruchamiasz silnik tylko jako świadomą decyzję po spełnieniu tych warunków.** Nie masz pewności: wklej wynik Claude i poczekaj na „ok". Jeśli po starcie pamięć zacznie rosnąć ponad progi z punktu 2.4, zatrzymaj usługę poleceniem z tego punktu.

### 2.2 Start

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Tryb jest `dry_run`, dopóki nie spełnisz wszystkich czterech warunków uzbrojenia z punktu 6. Kontener startuje z polityką `unless-stopped`: po restarcie serwera lub Dockera uruchomi się sam (w trybie wynikającym z plików). Aby tego uniknąć, zatrzymaj go (hamulec 3 z początku dokumentu); zatrzymany ręcznie sam nie wstanie.

### 2.3 Pierwsze minuty

```bash
docker compose -f docker/compose.yml ps
```

Status ma przejść z `Up ... (health: starting)` do `Up ... (healthy)` w ciągu około dwóch minut.

```bash
docker logs --tail 30 gw-stock-engine
```

W pierwszych liniach powinieneś zobaczyć: `mode: dry_run, no orders will be sent: ...` (lista warunków, których brakuje do trybu live), `engine started: mode dry_run, instrument DE40`, a w dzień bez sesji (weekend, święto) linię `day ...: DayReport(... skip_reason='NOT_SESSION' ...)`. Silnik wtedy po prostu czeka; jego tętno widać w `status`.

### 2.4 Pomiar pamięci po 10 minutach pracy

Odczekaj co najmniej 10 minut od startu, a potem uruchom pomiar. Skrypt zapisuje wynik w `~/gw-pomiar-po.txt`; porównaj go z `~/gw-pomiar-przed.txt`:

```bash
(
date -u
echo "--- zużycie pamięci kontenera (docker stats)"; docker stats --no-stream gw-stock-engine
echo "--- pamięć serwera (MB)"; free -m
echo "--- zamówiona konfiguracja (oczekiwane: 268435456 268435456 134217728 1000 64 true)"
docker inspect gw-stock-engine --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.MemoryReservation}} {{.HostConfig.OomScoreAdj}} {{.HostConfig.PidsLimit}} {{.HostConfig.ReadonlyRootfs}}'
echo "--- jądro: pliki cgroup w kontenerze (brak = brak pliku na tym jądrze)"
docker exec gw-stock-engine sh -c 'for f in memory.max memory.swap.max memory.current memory.peak memory.low memory.events memory.swap.current pids.current pids.max cpu.max; do if [ -r /sys/fs/cgroup/$f ]; then echo "$f: $(tr "\n" " " < /sys/fs/cgroup/$f)"; else echo "$f: brak"; fi; done'
echo "--- OOM, restarty, zdrowie"
docker inspect gw-stock-engine --format 'OOMKilled={{.State.OOMKilled}} restarty={{.RestartCount}} zdrowie={{.State.Health.Status}}'
echo "--- wszystkie kontenery na serwerze (pamięć)"; docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'
) 2>&1 | tee ~/gw-pomiar-po.txt
```

Progi decyzyjne (**silnik musi mieścić się w 256 MB z zapasem, a reszta serwera ma pozostać nietknięta**):

| Pomiar | Zielone: dalej | Żółte: zapytaj Claude, nie idź dalej | Czerwone: zatrzymaj silnik (hamulec 3) |
|---|---|---|---|
| `MEM USAGE` silnika po 10 minutach | do 100 MiB | 101 do 150 MiB | powyżej 150 MiB |
| `available` serwera po starcie | co najmniej 400 MB | 300 do 399 MB | poniżej 300 MB |
| Pamięć pozostałych kontenerów względem pomiaru „przed" | zmiana do 10% | 10 do 25% | powyżej 25% |
| `memory.swap.max` / `memory.swap.current` | `0` / `0` | – | cokolwiek innego |
| `OOMKilled`, `oom_kill` w `memory.events`, restarty | `false`, `0`, `0` | – | `true` albo więcej niż 0 |

Przy czerwonym wyniku zatrzymaj silnik od razu i wklej pomiar Claude:

```bash
docker compose -f docker/compose.yml stop
```

Dla orientacji: sam interpreter z biblioteką `aiohttp` zajmuje ok. 45 MB, a nieruchomy silnik ok. 30 MB w odczycie zużycia pamięci kontenera. Kontrola zdrowia co minutę uruchamia na chwilę drugi proces Pythona (kolejne ok. 45 MB, w tym samym limicie 256 MB).

### 2.5 Jak wygląda zwykły dzień sesji

Wszystkie godziny w czasie Berlin. W kolumnach UTC są godziny, które zobaczysz w logach.

| Berlin | UTC zimą (do 28.03.2027) | UTC latem | Co robi silnik |
|---|---|---|---|
| ok. 08:50 | 07:50 | 06:50 | Przygotowanie: logowanie, uzgodnienie stanu z brokerem, ATR14 |
| 09:00 do 09:05 | 08:00 do 08:05 | 07:00 do 07:05 | Zakres otwarcia: zbieranie pierwszej świecy 5-minutowej, bez decyzji |
| 09:05 do 09:10 | 08:05 do 08:10 | 07:05 do 07:10 | **Decyzja** po zamknięciu pierwszej świecy (termin 09:10, potem `NO_SIGNAL_DATA`). W `dry_run` sygnał kończy się wpisem, nie zleceniem |
| do 17:25 | do 16:25 | do 15:25 | Nadzór: uzgadnianie z brokerem co 10 s, ping sesji co 5 min |
| 17:25 | 16:25 | 15:25 | Spłaszczenie (5 minut przed zamknięciem 17:30). W `dry_run` nie ma czego zamykać |
| po 17:30 | po 16:30 | po 15:30 | Uzgodnienie końcowe i raport dnia w logu |
| noc, weekend, święto | | | Spoczynek: silnik tylko zapisuje tętno i pilnuje plików `STOP` |

Kalendarz silnika zna na razie wyłącznie pełne sesje (09:00 do 17:30) i święta; lista skróconych sesji (`early_closes`) jest **pusta**, więc spłaszczenie jest zawsze o 17:25. Skrócona sesja 30.12.2026 jest do sprawdzenia w grudniu (punkt 8.6).

**Gdzie czytać decyzję dnia.** Stan i ostatnia decyzja (z początkiem świecy w UTC i jej ceną otwarcia, maksimum, minimum, zamknięcia):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Szukaj linii `last decision <data>: SIGNAL ...` albo `SKIP <powód>` z końcówką `first candle <początek UTC> O ... H ... L ... C ...`. `SKIP` z powodem (`DOJI`, `STOP_TOO_TIGHT`, `NO_SIGNAL_DATA` i inne) nie jest błędem. Ta sama decyzja jest w logu jako linia ze słowem `decision`:

```bash
docker logs --since 24h gw-stock-engine 2>&1 | grep -E "decision|DayReport|ERROR|WARNING"
```

### 2.6 Codzienna kontrola (po 17:35)

```bash
docker compose -f docker/compose.yml ps
```

Ma być `Up ... (healthy)`.

```bash
tail -n 30 data/alerts.log
```

Plik alarmów: linie w formacie `czas-UTC POZIOM treść`. Alarmy `ERROR` i poziomy kill switcha (`SOFT`, `HARD`) wymagają Twojej uwagi; rutynowe `INFO` nie. Pusty albo nieistniejący plik oznacza brak alarmów.

```bash
docker stats --no-stream gw-stock-engine
```

Tętno (silnik zapisuje je co kilka sekund; kontrola zdrowia sprawdza, że plik jest świeższy niż 120 s). Pole `ts` ma być sprzed kilku sekund, a `kill_level` powinno być `NONE`:

```bash
cat data/heartbeat.json
```

Pamięć ma zostać w zielonych progach z punktu 2.4. **Po pierwszej pełnej sesji powtórz cały pomiar z punktu 2.4:** pole `memory.peak` (jeśli jądro je ma) pokazuje szczyt od startu kontenera; bez niego patrz na najwyższy odczyt zużycia pamięci kontenera z całego dnia. Szczyt do 170 MiB jest zielony, 171 do 220 MiB żółty (zapytaj), powyżej 220 MiB albo `OOMKilled=true` czerwony.

---

## 3. Weryfikacja pierwszej świecy (bramka L0)

Dokumentacja Capital.com nie mówi wprost, czy znacznik czasu świecy oznacza jej **początek** czy **koniec**. Gdyby silnik się mylił, wybrałby świecę sprzed otwarcia zamiast pierwszej świecy sesji i cała strategia opierałaby się na złych danych. Dlatego przed uzbrojeniem porównujesz to ręcznie.

Zrób to po pierwszej sesji `dry_run`, najlepiej na dwóch różnych dniach.

1. Uruchom `status` (polecenie z punktu 2.5) tego samego dnia po decyzji (pokazuje tylko ostatnią decyzję) i zapisz z linii `last decision`: **początek świecy** (`first candle ...`) oraz **O, H, L, C**.
2. Zimą początek świecy ma być `...T08:00:00+00:00`, latem `...T07:00:00+00:00`. To jest 09:00 czasu Berlina. **Pułapka zmiany czasu:** 09:00 w Berlinie to 08:00 UTC zimą, ale 07:00 UTC latem. Do 28.03.2027 obowiązuje czas zimowy (zmiana na zimowy była 25.10.2026), od 28.03.2027 letni. Silnik liczy to przez strefę czasową, a nie stałym przesunięciem, ale to Ty sprawdzasz wynik.
3. Otwórz aplikację Capital.com, instrument DE40 („Germany 40"), wykres 5-minutowy, i znajdź świecę zaczynającą się o **09:00** czasu Berlina w tym samym dniu (sprawdź, że aplikacja pokazuje czas lokalny, a nie UTC).
4. Porównaj O, H, L, C z silnika z tą świecą. Silnik zapisuje cenę **środkową** (średnia kupna i sprzedaży), a wykres w aplikacji zwykle pokazuje jedną ze stron, więc różnica do około 1 do 2 punktów jest normalna.
5. Wynik:
   - **Pasuje do świecy 09:00 (09:00 do 09:05):** bramka L0 w tym punkcie zaliczona.
   - **Pasuje do świecy 08:55 (08:55 do 09:00) albo do 09:05:** znacznik jest przesunięty. **Nie edytuj żadnego kodu.** Stała `CANDLE_STAMP_IS_PERIOD_START` (adapter Capital.com, plik `src/gwstock/broker/capitalcom.py`) rozstrzyga, czy znacznik oznacza początek (dziś `True`, niepotwierdzone założenie). Zgłoś Claude: wiersz `last decision` oraz co widzisz w aplikacji dla świec 08:55, 09:00 i 09:05 (zrzut ekranu). Kontroler przestawi stałą, przebuduje obraz, a Ty powtórzysz dzień `dry_run`. **Nie uzbrajaj, dopóki nie zostanie to potwierdzone.**
   - **Początek świecy w ogóle inny niż 08:00 / 07:00 UTC** albo `NO_SIGNAL_DATA` pod rząd: zgłoś, to samo.

---

## 4. Ćwiczenia awaryjne (w `dry_run`, po co najmniej jednej pełnej sesji)

Cel: zobaczyć na własne oczy, że mechanizmy bezpieczeństwa działają, zanim cokolwiek zależy od nich naprawdę. Wszystkie ćwiczenia robisz w `dry_run`, czyli bez żadnej pozycji. Do zdjęcia kill switcha służy polecenie `clear-kill` (punkt 8.4): ćwiczenia 4.1, 4.2 i 4.4 kończą się nim, a program prosi wtedy o wpisanie słowa `CLEAR`.

Zasada ogólna: kill switch zapisuje swój stan w dzienniku i **przeżywa restart**. Usunięcie pliku `STOP` albo `HARD_STOP` **nie zdejmuje** poziomu: robi to wyłącznie `clear-kill`.

### 4.1 Miękki kill switch: `data/STOP`

```bash
touch data/STOP
```

Odczekaj ok. 15 sekund i sprawdź alarm:

```bash
tail -n 5 data/alerts.log
```

Oczekiwane: linia w rodzaju `... SOFT kill switch SOFT: STOP_FILE` (z poziomu `NONE` alarm brzmi `kill switch <POZIOM>: <powód>`).

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Oczekiwane: `kill switch: SOFT (STOP_FILE, since ...)`. Poziom miękki oznacza: brak nowych wejść, istniejąca pozycja jest zarządzana normalnie.

Powrót do normy (kolejność ma znaczenie). Najpierw usuń plik:

```bash
rm -f data/STOP
```

Odczekaj ok. 15 sekund i sprawdź stan jeszcze raz. `status` **nadal** ma pokazywać `kill switch: SOFT` (tak ma być: usunięcie pliku nie zdejmuje poziomu):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Zatrzymaj silnik (`clear-kill` działa tylko przy zatrzymanym silniku, bo bierze blokadę instancji i inaczej odmawia):

```bash
docker compose -f docker/compose.yml stop
```

Zdejmij kill switch. Program pokaże poprzedni poziom, powód i ostatnie alarmy, poprosi o wpisanie słowa `CLEAR` (`Type CLEAR to clear the kill switch:`) i zapisze, kto zdjął blokadę; na końcu wypisze `kill switch cleared by wlasciciel (was SOFT: STOP_FILE); start the engine`. Po `--by` jest tylko nazwa do zapisu w dzienniku, możesz wpisać swoje imię bez spacji:

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

Uruchom silnik:

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Po kilkunastu sekundach sprawdź stan (ma być `kill switch: NONE`):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

### 4.2 Twardy kill switch: `data/HARD_STOP`

```bash
touch data/HARD_STOP
```

Odczekaj ok. 15 sekund i sprawdź alarm:

```bash
tail -n 5 data/alerts.log
```

Oczekiwane: alarm o twardym poziomie z powodem `HARD_STOP_FILE`. Z poziomu `NONE` brzmi `kill switch HARD: HARD_STOP_FILE`, a jeśli wcześniej był miękki (np. po 4.1 bez zdjęcia), brzmi `kill switch escalated SOFT -> HARD: HARD_STOP_FILE (was: ...)`.

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Oczekiwane: `kill switch: HARD (HARD_STOP_FILE, since ...)`. Poziom twardy w trybie live **zamyka otwarte pozycje** procedurą awaryjną, potem zostaje jak miękki. W `dry_run` nie ma czego zamykać, więc widzisz tylko zmianę stanu. Twardy poziom **zostaje, dopóki go nie zdejmiesz** poleceniem `clear-kill`, także po restarcie.

Powrót (kolejność jak w 4.1). Usuń oba pliki, na wszelki wypadek:

```bash
rm -f data/STOP data/HARD_STOP
```

```bash
docker compose -f docker/compose.yml stop
```

Zdejmij kill switch (wpisz `CLEAR`, jak w 4.1):

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

### 4.3 Nagłe zabicie kontenera (docker kill), najlepiej w trakcie sesji

Symuluje nagłe zatrzymanie bez czystego zamknięcia. **Zabicie poleceniem docker kill jest dla Dockera „ręcznym zatrzymaniem", więc polityka restartu go nie podnosi**; to ćwiczenie sprawdza, że po ręcznym starcie silnik wraca do właściwego stanu. Samoczynny restart po prawdziwej awarii sprawdza ćwiczenie 4.3b.

```bash
docker kill -s KILL gw-stock-engine
```

Sprawdź:

```bash
docker compose -f docker/compose.yml ps -a
```

Oczekiwane: `Exited (137)`. Kontener zabity ręcznie **nie wstaje sam** (tak działa Docker 29 na maszynie testowej, na VPS to potwierdza to ćwiczenie). Uruchom go ponownie zwykłym startem:

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Sprawdź logi i stan:

```bash
docker logs --tail 20 gw-stock-engine
```

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Oczekiwane: w logach nowy start (`engine started`), w `status` świeże tętno, ten sam poziom kill switcha co przed zabiciem i `unresolved orders: 0`, bez zdublowanych zleceń. Blokada instancji (`data/engine.lock`) zwalnia się sama, nie usuwaj jej ręcznie.

### 4.3b Awaria procesu wewnątrz kontenera: samoczynny restart

To jest prawdziwa awaria: ginie sam proces Pythona, a kontener i Docker „nie wiedzą", że to Ty. Polityka `unless-stopped` ma go podnieść **sama**. Najlepiej w trakcie sesji (po 09:15), żeby zobaczyć także uzgadnianie startowe. Polecenie szuka procesu silnika (uruchomionego jako python -m gwstock) i zabija go sygnałem KILL (w obrazie nie ma `pkill`, dlatego przeszukuje `/proc`):

```bash
docker exec gw-stock-engine sh -c 'for p in /proc/[0-9]*; do if [ "$(tr "\0" " " < $p/cmdline)" = "python -m gwstock " ]; then kill -9 "${p#/proc/}"; echo "zabito proces ${p#/proc/}"; fi; done'
```

Oczekiwane: jedna linia `zabito proces N` (zwykle `7`). Po sekundzie, bez żadnego Twojego polecenia, Docker uruchamia kontener ponownie. Sprawdź licznik restartów i stan:

```bash
docker inspect gw-stock-engine --format 'status={{.State.Status}} restarty={{.RestartCount}} OOMKilled={{.State.OOMKilled}} zdrowie={{.State.Health.Status}}'
```

Oczekiwane: `status=running`, `restarty` o jeden większe niż przed ćwiczeniem, `OOMKilled=false`, a `zdrowie` przechodzi z `starting` do `healthy` w ciągu kilkunastu sekund. Logi nie są tracone przy restarcie:

```bash
docker logs --tail 30 gw-stock-engine
```

Oczekiwane: druga linia `engine started: mode ...` (a w sesji uzgadnianie startowe z brokerem) i **żadnych** linii `ERROR` ani `Traceback`.

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Oczekiwane: świeże tętno, ten sam poziom kill switcha co przed ćwiczeniem, `unresolved orders: 0`, pozycja bez zmian.

```bash
tail -n 5 data/alerts.log
```

Oczekiwane: brak nowych alarmów `ERROR`. Jeśli kontener nie wstał sam albo w logach są błędy, zatrzymaj go (`stop`) i zgłoś.

### 4.4 Odcięcie sieci (tylko w godzinach sesji, 09:00 do 17:25)

Poza sesją silnik nie odpytuje notowań i nic by się nie wydarzyło. W sesji odłącz kontener od sieci:

```bash
docker network disconnect gw-stock-engine_default gw-stock-engine
```

Odczekaj około 90 sekund i sprawdź alarmy:

```bash
tail -n 10 data/alerts.log
```

Oczekiwane: alarm o ciszy notowań (`QUOTE_SILENCE`, po 60 s ciszy) i kill switch `SOFT`; tętno nadal jest świeże (kontener jest zdrowy, tylko ślepy). Przywróć sieć:

```bash
docker network connect gw-stock-engine_default gw-stock-engine
```

Silnik zaczyna znów dostawać notowania, ale `SOFT` **zostaje**, dopóki go nie zdejmiesz: zatrzymaj, `clear-kill`, uruchom, dokładnie jak w 4.1.

Jeśli w którymkolwiek ćwiczeniu zobaczysz coś, czego ta instrukcja nie opisuje: zatrzymaj silnik (hamulec 3 z początku dokumentu) i zgłoś.

---

## 5. Próba `smoke`

**Uwaga: to jest pierwsza prawdziwa transakcja.** `smoke` wysyła na Twoje konto jedno zlecenie rynkowe DE40 o wielkości **0,001** (najmniejsza możliwa) ze stopem, **trzyma je otwarte tyle sekund, ile zażądasz opcją `--hold`** (żebyś zdążył obejrzeć pozycję i jej stop w aplikacji), a potem **zamyka**. Sprawdza całą ścieżkę zleceń: otwarcie, widoczność pozycji i stopu, zamknięcie, księgowanie, drugie zamknięcie (musi zostać odrzucone), ostatnie świece i opóźnienie historii. Koszt to spread i poślizg przy wielkości 0,001, czyli rząd ułamka grosza do około 1 centa (jeden punkt przy 0,001 to ok. 0,001 €; dokładną wartość punktu potwierdza ta próba). W czasie `--hold` cena może się ruszyć o kilka punktów, co przy 0,001 to ułamki centa; stop ogranicza stratę.

### Warunki wstępne (wszystkie)

- Bramka L0 zaliczona do tego miejsca: pełna sesja `dry_run` bez rozbieżności i błędów, świeca zweryfikowana (punkt 3), ćwiczenia (punkt 4), pamięć zmierzona (punkty 2.4 i 2.6), testy w obrazie i odczyty live przeszły.
- `bind-account` wykonane (jest `data/account.json`).
- Pre-rejestracja zamrożona (punkt 2.0; `--check` kończy się `OK`), bo `smoke` działa tylko w trybie `live`, a ten wymaga zgodnego odcisku konfiguracji.
- Na rachunku jest saldo (40 €), a w aplikacji Capital.com nie ma żadnej otwartej pozycji DE40.
- Dzień roboczy, 10:00 do 16:00 czasu Berlina (płynny rynek, wąski spread), **nie** w okolicy 09:00 do 09:10 ani 17:25, i **nie** w przerwie 21:00 do 21:05 UTC.
- Aplikacja Capital.com otwarta na ekranie pozycji DE40 i odświeżana.

### Procedura

Zatrzymaj silnik (`smoke` nie uruchomi się obok działającego silnika, blokuje to plik blokady):

```bash
docker compose -f docker/compose.yml stop
```

Uzbrój **na czas próby**: wpisz `live_allowed = true` i utwórz plik `data/ARM_LIVE` (to Twoja decyzja, kod tego nigdy nie zrobi):

```bash
sed -i 's/^live_allowed = false/live_allowed = true/' config/runtime.toml
```

```bash
touch data/ARM_LIVE
```

Sprawdź, że wszystkie warunki są spełnione (ma być `mode (files): live (all arming conditions met)`):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

Uruchom próbę z trzymaniem pozycji przez 90 sekund (dopuszczalne wartości `--hold` to 0 do 600 sekund; 0 zamyka od razu):

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock smoke --hold 90
```

**Nie przerywaj polecenia w trakcie** (bez Ctrl+C): przerwanie mogłoby zostawić pozycję otwartą. Gdyby tak się stało, patrz „Reguła przerwania" niżej.

### Co zobaczysz

Linie `PASS [n]`, `FAIL [n]` i `INFO [n]`, a na końcu wynik `PASS smoke: 7/7 checks passed` albo `FAIL smoke: ...` (kod wyjścia 0 tylko, gdy każdy z siedmiu kroków jest `PASS`). Znaczenie kroków: [1] rachunek poprawny, [2] wejście przyjęte (z poziomem otwarcia i stopu), [3] pozycja widoczna ze stopem, [4] spłaszczenie przez wykonawcę (po upływie `--hold`), [5] pozycja zniknęła i transakcja zaksięgowana (z kosztami), [6] drugie zamknięcie odrzucone jako „nie znaleziono" (to jest wymagane), [7] trzy ostatnie świece 5-minutowe do porównania z wykresem, [8] opóźnienie historii. Dokładne brzmienie linii może się nieco różnić od opisu.

Jeśli w kroku [2] jest `INFO ... the API did not show the stop ...`, broker nie pokazał stopu w liście pozycji i silnik zapisał stop **wyliczony z naszego żądania**. Wtedy stop musisz zobaczyć w aplikacji sam (poniżej): bez tego nie uzbrajaj.

### Co sprawdzasz w aplikacji Capital.com

Dzięki `--hold` pozycja jest otwarta około 90 sekund:

1. Zaraz po starcie polecenia odśwież ekran pozycji: pozycja DE40 o wielkości 0,001 powinna się pojawić **z ustawionym stop lossem**. Zapisz poziom stopu.
2. Porównaj ten poziom z `stop level` z kroku [2] wydruku.
3. Po próbie otwórz historię (aktywność lub zamknięte pozycje): ma być **dokładnie jedna** transakcja DE40 0,001, otwarta i zamknięta, i **żadnej** pozostałej otwartej pozycji.
4. Porównaj świece z kroku [7] z wykresem 5-minutowym (zasada ta sama co w punkcie 3: początek świecy w UTC i ceny środkowe).

Jeśli nie widzisz stopu przy pozycji, **zapisz to i zgłoś Claude** (to pozycja C24 z listy ograniczeń) i nie uzbrajaj.

### Po próbie (zawsze, także gdy wszystko jest PASS)

Zdejmij uzbrojenie. Uzbrajanie na stałe to osobna decyzja (punkt 6):

```bash
rm -f data/ARM_LIVE
```

```bash
sed -i 's/^live_allowed = true/live_allowed = false/' config/runtime.toml
```

```bash
grep '^live_allowed' config/runtime.toml
```

Ma być `live_allowed = false`. Zapisz wynik próby (cały wydruk) i przekaż Claude. Sprawdź stan przed powrotem do `dry_run`:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

Ma być `mode (files): dry_run (...)`. **Jeśli widzisz `kill switch: SOFT`**, a zwłaszcza po nieudanym kroku [6] (wtedy silnik sam włącza miękki kill switch z powodem `SMOKE_SECOND_CLOSE_UNEXPECTED`), silnik nie wejdzie już w żadną pozycję, dopóki go nie zdejmiesz: wykonaj `clear-kill` (silnik jest zatrzymany, jak trzeba; wpisz `CLEAR`), ale **dopiero po wyjaśnieniu przyczyny z Claude**:

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

Silnik po próbie zostaje zatrzymany; jeśli chcesz wrócić do pracy w `dry_run`, uruchom go (restart zawsze jako osobny krok po zatrzymaniu, więc to już jest ten drugi krok):

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

**Reguła przerwania:** przy jakiejkolwiek wątpliwości (FAIL, pozycja nadal otwarta w aplikacji, dziwny wydruk, przerwane polecenie) natychmiast usuń `data/ARM_LIVE` (hamulec 2 z początku dokumentu), sprawdź aplikację, a gdy pozycja jest otwarta, **zamknij ją ręcznie w aplikacji**. Potem opisz sytuację Claude i nie ponawiaj próby.

**Co się dzieje po przerwaniu `--hold` i co wtedy zostaje w dzienniku.** Jeśli polecenie zostanie przerwane w trakcie wstrzymania (Ctrl+C, sygnał, zerwane połączenie SSH, błąd), program sam próbuje zamknąć pozycję próby: najpierw procedurą awaryjną wykonawcy, a gdy broker nadal ją pokazuje, jednym bezpośrednim zamknięciem po numerze transakcji (to zamknięcie **nie idzie przez wykonawcę silnika**). Gdy się uda, wypisze `INFO [4] deal ... closed after the interrupt; the engine's reconciliation books it if needed`. Dziennik silnika **zostaje wtedy z pozycją `OPEN`** (`status` pokazuje `position DE40: OPEN ...`), dopóki uzgadnianie silnika nie zaksięguje zamknięcia z historii brokera, czyli dopóki nie uruchomisz silnika. Gdy się nie uda, wypisze głośną linię `FAIL [4] LOUD: deal ... left open with its stop; ...`: pozycja ma wtedy stop u brokera, ale zamknij ją ręcznie w aplikacji (albo uruchom silnik, który ją uzgodni i spłaszczy o 17:25). W obu przypadkach najpierw zdejmij uzbrojenie (reguła przerwania powyżej), potem uruchom silnik w `dry_run` (punkt 2.2), odczekaj kilka minut na uzgodnienie i sprawdź `status` (ma być `position DE40: FLAT`). Wynik opisz Claude. Jeśli pozycja pozostaje `OPEN` albo `BLOCKED`, nie obchodź tego: punkt 8.5.

---

## 6. Uzbrajanie (live)

**Wykonuj dopiero po zaliczeniu całej listy z punktu 10 i tylko z własnej, świadomej decyzji.** Nikt i nic nie zrobi tego za Ciebie.

### Cztery warunki (wszystkie naraz)

1. `live_allowed = true` w `config/runtime.toml`.
2. Plik `data/ARM_LIVE` utworzony przez Ciebie.
3. Powiązany rachunek: `bind-account` wykonane (jest `data/account.json`).
4. Zamrożona pre-rejestracja zgodna z bieżącą konfiguracją (kontrola `--check` z punktu 2.0 kończy się `OK`), od tego czasu bez zmian w `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`. Gdy odcisk jest inny, `status` pokazuje `dry_run` z powodem `the configuration fingerprint differs from the frozen pre-registration`.

Dodatkowo silnik przy **każdym** starcie sprawdza u brokera, że powiązany rachunek istnieje, jest `ENABLED`, ma saldo większe od zera, walutę EUR i jest aktywny w sesji. Jeśli nie, zostaje w `dry_run` do końca procesu i zgłasza alarm.

### Procedura (poza sesją albo po 17:30; silnik zatrzymany)

```bash
docker compose -f docker/compose.yml stop
```

```bash
sed -i 's/^live_allowed = false/live_allowed = true/' config/runtime.toml
```

```bash
touch data/ARM_LIVE
```

Sprawdź (ma być `mode (files): live (all arming conditions met)`):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

W logu ma być linia `mode: live (all four arming conditions met; ...)`:

```bash
docker logs --tail 20 gw-stock-engine
```

### Co się zmienia po uzbrojeniu

- Silnik **wysyła prawdziwe zlecenia** raz dziennie (jedno wejście, okno 09:05 do 09:10), ze stopem założonym przez brokera razem z wejściem, i o 17:25 zamyka pozycję.
- Pierwsze 5 sesji handluje wielkością minimalną 0,001. Większa wielkość wymaga bramki L1 (punkt 7).
- Limity: strata na transakcję do 1,00 €, dzienna 1,50 €, łączna od startu 12 €, dźwignia do 5. Ich przekroczenie włącza kill switch.
- Wynik pojedynczej sesji to grosze i szum. To jest pomiar działania systemu, nie źródło zysku.

### Rozbrajanie

Najlepiej poza sesją albo po spłaszczeniu. **Samo usunięcie pliku `data/ARM_LIVE` działa od razu także na działający silnik: przestaje on wchodzić w nowe pozycje i tak zostaje do restartu** (istniejąca pozycja jest nadal zarządzana). To dlatego jest to hamulec 2 z początku dokumentu. Ponowne uzbrojenie zawsze wymaga restartu: zatrzymaj, utwórz `data/ARM_LIVE`, uruchom. Pełne rozbrojenie to: usunięcie pliku, zatrzymanie, zmiana `live_allowed`, kontrola i uruchomienie (restart zawsze w dwóch krokach):

```bash
rm -f data/ARM_LIVE
```

```bash
docker compose -f docker/compose.yml stop
```

```bash
sed -i 's/^live_allowed = true/live_allowed = false/' config/runtime.toml
```

Sprawdź stan przed uruchomieniem (ma być `mode (files): dry_run (...)`):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

W logu ma być `mode: dry_run, no orders will be sent: ...`:

```bash
docker logs --tail 20 gw-stock-engine
```

Pozycja otwarta w chwili rozbrojenia nadal jest zarządzana (stop u brokera, spłaszczenie o 17:25), bo wyjść nie blokujemy nigdy.

---

## 7. Pierwsze 5 sesji i bramka L1

Pierwsze 5 sesji live silnik handluje wielkością minimalną 0,001. Po każdej sesji, po 17:35, uruchom raport. Raport tylko czyta (dziennik otwiera wyłącznie do odczytu), więc można go puścić przy pracującym silniku:

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock.report
```

Domyślnie raport pokazuje ostatni dzień z zapisem; opcja `--day` z datą w postaci RRRR-MM-DD pokazuje wskazany dzień, `--all` wszystkie dni, a `--journal` wskazuje inny plik dziennika (zwykle niepotrzebne). Gdy silnik jest zatrzymany, użyj tego samego polecenia z `run --rm -T` zamiast `exec -T`. Dodatkowo wykonaj codzienną kontrolę z punktu 2.6 (stan, alarmy, tętno, pamięć). Linia `live sessions (trades without smoke): N; L1_PASSED: yes` albo `no` w `status` mówi, ile sesji liczy bramka i czy plik bramki istnieje.

Eksport księgi transakcji do CSV (plik pojawia się na serwerze jako `data/ledger.csv`, uprawnienia 600). Ścieżka wyjściowa musi leżeć pod `/data` albo `/tmp`, bo reszta systemu plików kontenera jest tylko do odczytu (a `/tmp` znika po zakończeniu polecenia):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock.report --csv /data/ledger.csv
```

**Jak czytać raport.** Blok „Dzień” to wynik, koszty i liczniki błędów (poślizg w tabeli dnia pochodzi z modelu w konfiguracji, nie jest pomiarem). Pod nimi jest blok „Bramka L1” i to w nim są punkty, które decydują o bramce:

- Status `PASS` albo `NOT YET` z listą powodów. `NOT YET` znaczy „jeszcze nie”, nie awarię. Powód `brak pomiaru kosztów: użytecznych próbek N z 5` także znaczy **jeszcze nie** (za mało próbek kosztu), a nie „koszty są w normie”.
- Werdykt kosztów to stosunek niekorzystnego poślizgu wejścia do modelu, liczony w punktach (najwyżej 2, czyli przy modelu 0,5 pkt średnio do 1,0 pkt). Pod nim raport podaje średni i największy poślizg wejścia, spread w chwili decyzji (informacyjnie: nie wchodzi do werdyktu), liczbę zamknięć stopem (spread i poślizg przy wyjściu stopem nie są mierzone) oraz koszt w EUR (informacyjnie i zgrubnie, bo księga zaokrągla do 0,01 €).
- Ostrzeżenie o skąpych dowodach: pięć próbek to wciąż mało, by oceniać koszty.

Co przeglądasz po każdej sesji i na koniec piątej (warunki bramki L1 ze specyfikacji, sekcja 11):

- **zero** niewyjaśnionych rozbieżności pozycja/broker starszych niż 60 sekund (w `alerts.log` nie ma `ERROR` bez wyjaśnienia),
- **zero** nieobsłużonych odrzuceń zleceń,
- v1 mierzy poślizg wejścia i spread; koszty wyjść STOP są niezmierzone (raport tylko liczy takie wyjścia); rozbieżność kosztów większa niż 2 razy model oznacza przegląd z Claude; próbki kosztu to transakcje wejścia ORAZ próby smoke dowolnej wielkości, a nie jedna próbka na sesję; listę próbek czyta właściciel (eksport CSV z opcją `--csv`),
- transakcje w raporcie zgadzają się z historią w aplikacji Capital.com (cena wejścia, wyjścia, wynik),
- pamięć nadal w zielonych progach (punkt 2.4).

**Bramka L1.** Dopiero po przeglądzie (lista wyżej), który wypadł dobrze, i po 5 sesjach, utwórz plik. Gdy bramka w raporcie ma status `PASS`, na końcu bloku bramki pojawia się zdanie „Aby dopuścić wielkość według reguł utwórz plik data/L1_PASSED (kod go nigdy nie tworzy).” To tylko informacja o skutku pliku, **a nie zgoda ani polecenie**: `PASS` liczy wyłącznie liczby z całego dziennika i nie ocenia, czy rozbieżności zostały wyjaśnione. To Twoja ocena po przeglądzie (a przy jakiejkolwiek wątpliwości, także rozmowa z Claude) rozstrzyga o pliku:

```bash
touch data/L1_PASSED
```

Plik tworzy wyłącznie właściciel, kod nigdy. Silnik sprawdza go przy każdej decyzji, więc restart nie jest potrzebny. **Bez tego pliku wielkość pozostaje 0,001**, nawet po 5 sesjach. Z plikiem, od 6. sesji, wielkość liczona jest regułami strategii (zawsze w granicach limitów ryzyka). Wycofanie bramki:

```bash
rm -f data/L1_PASSED
```

---

## 8. Operacje

### 8.1 Zatrzymanie i uruchomienie (zawsze dwa osobne kroki)

```bash
docker compose -f docker/compose.yml stop
```

Poczekaj na koniec polecenia (do 30 sekund, silnik zamyka się czysto) i sprawdź, że kontener stoi:

```bash
docker compose -f docker/compose.yml ps -a
```

Dopiero wtedy start:

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Po zatrzymaniu otwarta pozycja zostaje u brokera ze swoim stopem; nowy start uzgadnia ją z brokerem, zanim zrobi cokolwiek innego.

### 8.2 Aktualizacja kodu (nigdy przy otwartej pozycji)

Wykonuj poza sesją, np. po 17:35 albo w weekend. Przebudowa obrazów i kontener testowy zużywają chwilowo do ok. 400 MB, więc obowiązuje ta sama zasada co na początku: **bez pomiaru pamięci nic nie budujesz** (zasada 0.1). Jeśli jesteś uzbrojony, najpierw rozbrój (punkt 6), żeby system kontroli wersji nie widział lokalnej zmiany pliku `runtime.toml`.

Sprawdź, że nie ma otwartej pozycji (ma być `position DE40: FLAT`):

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Zapisz logi kontenera (znikają razem z kontenerem przy jego odtworzeniu):

```bash
docker logs gw-stock-engine > data/logi-przed-aktualizacja.txt 2>&1
```

Zatrzymaj silnik (to zwalnia jego pamięć):

```bash
docker compose -f docker/compose.yml stop
```

**Brama pamięci przed budową.** Zmierz wolną pamięć serwera:

```bash
free -m
```

Kolumna `available`: co najmniej **600 MB** to zielone światło; 450 do 599 MB to żółte (zapytaj Claude, nie buduj); poniżej 450 MB to czerwone. Przy żółtym albo czerwonym **nie buduj niczego**: nic jeszcze nie zmieniłeś (kod i obraz są stare), więc po prostu uruchom z powrotem dotychczasowy silnik poleceniem z punktu 8.1 i zapytaj Claude, co dalej.

Przy zielonym świetle pobierz nowy kod:

```bash
git pull --ff-only
```

```bash
git log --oneline -3
```

Sprawdź skrót z Claude (pobranie samych dokumentów, np. wpisu pre-rejestracji, nie zmienia odcisku konfiguracji), a potem zbuduj obraz testowy i powtórz testy logiki (punkty 1.7 i 1.9):

```bash
docker compose -f docker/compose.yml --profile test build gw-stock-test
```

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
```

Zbuduj obraz silnika:

```bash
docker compose -f docker/compose.yml build gw-stock-engine
```

Sprawdź, że konfiguracja nadal zgadza się z zamrożoną pre-rejestracją (punkt 2.0). Ten krok wykonujesz **po każdej aktualizacji**, zanim uruchomisz silnik; `OK` i kod wyjścia 0 to zgodność:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --check
```

**Brama pamięci przed startem.** Zmierz jeszcze raz (kontenery testowe już nie istnieją):

```bash
free -m
```

Wymagane **500 MB** w kolumnie `available` (400 do 499 MB to żółte: zapytaj; poniżej 400 MB: nie uruchamiaj). Dopiero wtedy start:

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

**Pomiar po aktualizacji.** Po 10 minutach pracy wykonaj pomiar z punktu 2.4 (blok zapisujący `~/gw-pomiar-po.txt`) albo przynajmniej te dwa polecenia:

```bash
docker stats --no-stream gw-stock-engine
```

```bash
free -m
```

Progi są te same co w punkcie 2.4: `MEM USAGE` silnika do 100 MiB (zielone), 101 do 150 MiB żółte, powyżej 150 MiB czerwone; `available` serwera co najmniej 400 MB. Przy żółtym lub czerwonym wyniku zatrzymaj silnik (hamulec 3) i zapytaj Claude.

Jeśli aktualizacja zmienia `strategy_v1.toml`, `risk_v1.toml` albo `costs_v1.toml`, to nowy wariant strategii: odcisk przestanie pasować do pre-rejestracji (`--check` pokaże `MISMATCH` i nazwy plików, które się różnią), a silnik zostanie w `dry_run`. **Nie używaj `--force` i nie uzbrajaj ponownie bez ustalenia z Claude** (punkt 2.0).

### 8.3 Czytanie alarmów

```bash
tail -n 50 data/alerts.log
```

```bash
grep -E "ERROR|HARD" data/alerts.log | tail -n 20
```

Format linii: `czas-UTC POZIOM treść`. Alarmy są też w logu kontenera (polecenie docker logs), ale `data/alerts.log` przeżywa odtworzenie kontenera.

### 8.4 Poziomy kill switcha

| Poziom | Skutek | Przykładowe powody |
|---|---|---|
| **Miękki (SOFT)** | Brak nowych wejść; istniejąca pozycja jest zarządzana normalnie | plik `data/STOP`; dzienny limit strat; rozbieżność z brokerem dłuższa niż 60 s; cisza notowań dłuższa niż 60 s; nieudane logowanie; trzy odrzucenia z rzędu |
| **Twardy (HARD)** | Zamyka pozycje procedurą awaryjną, potem działa jak miękki | plik `data/HARD_STOP`; strata dzienna co najmniej 2 razy limit; strata łączna od startu 12 €; pozycja bez stopu dłużej niż 10 s |

Oba poziomy są zapisane w dzienniku, przeżywają restart i **zdejmuje je wyłącznie Twoje polecenie `clear-kill`**; twardy zostaje do tego czasu także po restarcie. Dotyczy to również miękkiego poziomu włączonego przez nieudane logowanie (punkt 9): sam restart go nie zdejmuje. **`clear-kill` działa tylko przy zatrzymanym silniku** (bierze blokadę instancji i inaczej odmawia). Procedura zdjęcia, po kolei. Najpierw ustal przyczynę (polecenie z punktu 8.3). Potem usuń pliki, zatrzymaj silnik i uruchom `clear-kill` (program pokaże poziom, powód i ostatnie alarmy, a zdejmie blokadę dopiero po wpisaniu słowa `CLEAR`):

```bash
rm -f data/STOP data/HARD_STOP
```

```bash
docker compose -f docker/compose.yml stop
```

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

Polecenie odmówi, jeśli pliki `STOP` albo `HARD_STOP` nadal istnieją (`refused: data/STOP exists; remove it first ...`), jeśli silnik działa albo jeśli kill switch jest już zdjęty (`kill switch: NONE; nothing to clear`). Inne słowo niż `CLEAR` kończy polecenie bez zmian (`aborted: the kill switch stays as it is`). Na końcu uruchom silnik (`up -d`, jak w punkcie 8.1).

### 8.5 Stan `BLOCKED` pozycji

`BLOCKED` oznacza, że silnik nie potrafi **udowodnić**, co jest na koncie (np. inny rozmiar lub kierunek niż zapisany, pozycja, której nie otworzył, albo niejednoznaczna historia zamknięcia). Silnik nie zgaduje i nie dotyka takiej pozycji (poza awaryjnym spłaszczeniem pozycji, o której dowodnie wie, że jest jego). Bezpieczne rozwiązanie, zawsze w tej kolejności:

1. Otwórz aplikację Capital.com i sprawdź, **czy istnieje otwarta pozycja DE40**.
2. Jeśli **jest** (prawdziwa ekspozycja): zdecyduj, czy ją zamknąć. Zamknij ją ręcznie w aplikacji, jeśli nie ma być trzymana.
3. Zatrzymaj silnik (hamulec 3 z początku dokumentu). `reset-position` działa tylko przy zatrzymanym silniku.
4. Gdy w aplikacji **nie ma** pozycji DE40, wyczyść stan lokalny poleceniem poniżej. Działa tylko na pozycję `BLOCKED` (albo `ENTERING`); logowanie do brokera robi jedno. Polecenie samo sprawdza u brokera, że nie ma pozycji ani zlecenia oczekującego na DE40, i **odmawia** w kilku przypadkach (punkt 5). Prosi o potwierdzenie i zapisuje zdarzenie audytowe:

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock reset-position --by wlasciciel
```

   Program najpierw pokazuje stan z dziennika (linia `journal position DE40: ...`, z numerem transakcji) i pyta o jedno słowo, zależnie od przypadku: gdy zamknięcia tej transakcji nie da się zaksięgować z historii brokera, prosi o **wpisanie jej numeru** (`Type the deal id ... to accept that it stays unbooked and set FLAT:`), a przed pytaniem wypisuje ostrzeżenie `WARNING: deal ... is not booked in the ledger ...`, które znaczy, że jej wynik **nie trafi do limitów strat** (sprawdź tę transakcję w aktywności w aplikacji i opisz ją Claude); dla pozycji z polami, ale bez numeru transakcji, prosi o `NO-DEAL-ID`; w pozostałych przypadkach o `RESET`. Inne słowo kończy polecenie bez zmian (`aborted: nothing changed`).

5. Odmowa nie jest awarią; przeczytaj jej treść i nie obchodź jej:
   - `refused: unresolved orders in the journal ...` albo `refused: position DE40 is OPEN ...` (`EXITING`) z dopiskiem, że `reset-position is for BLOCKED`: silnik sam uzgodni i zaksięguje taki stan. Uruchom silnik (punkt 8.1), odczekaj na uzgodnienie i sprawdź `status`. **Jeśli po tym `status` nadal pokazuje `BLOCKED` i niewyjaśnione zlecenie (`unresolved orders: 1`), a ponowny `reset-position` odmawia z tego samego powodu, nie obchodź tego:** to znany otwarty punkt (silnik czeka na właściciela, a polecenie czeka na silnik). Zostaw miękki kill switch (wejścia są zablokowane), upewnij się w aplikacji, że nie ma pozycji DE40, i zgłoś Claude.
   - `refused: the closure of deal ... is in the broker history ...`: zamknięcie tej transakcji jest w historii brokera i **silnik je zaksięguje sam**. Uruchom silnik, odczekaj na uzgodnienie i wracaj do `reset-position` tylko wtedy, gdy `status` nadal pokazuje `BLOCKED`.
   - `refused: the broker still shows a position on DE40 ...` albo `refused: a working order on DE40 exists at the broker ...`: wróć do punktu 1 (decyzja o pozycji albo anulowanie zlecenia w aplikacji).
   - `refused: the broker cannot be read ...`, `refused: login failed`, `refused: the session is not on the bound account ...` albo `refused: the broker history cannot be read ...`: nic nie zmieniono. Zapytaj Claude; nie ponawiaj w pętli (każde uruchomienie loguje się do brokera).

   Po sukcesie (`position DE40 set FLAT by wlasciciel ...; start the engine`) uruchom silnik (punkt 8.1).

### 8.6 Uwagi operatorskie

- **Nieczytelny stan kill switcha przy starcie.** Jeśli przy starcie dziennik był chwilowo nieczytelny, kill switch ustawia się na `SOFT` z powodem `KILL_STATE_UNREADABLE` i **zostaje miękki przez kolejne restarty**, aż go zdejmiesz `clear-kill`. To zamierzona ochrona: nie jest awarią sam w sobie, ale sprawdź przyczynę (miejsce na dysku, uprawnienia do `data/`).
- **Pozycja trzymana dłużej niż 14 dni.** Silnik pobiera historię w oknach nie dłuższych niż 14 dni. Jeśli Ty (właściciel) trzymasz własną pozycję DE40 dłużej niż 14 dni i zamkniesz ją, gdy silnik jest wyłączony, historia może stać się niejednoznaczna i silnik zgłosi głośny błąd historii (`HISTORY_AMBIGUOUS`). Wtedy potrzebna jest Twoja interwencja z Claude; nie próbuj tego obchodzić. Zasada praktyczna: nie handluj ręcznie DE40 na rachunku silnika.
- **Grudzień 2026.** Lista skróconych sesji (`early_closes`) w kalendarzu XETRA jest dziś **pusta**: silnik zna tylko pełne sesje. Deutsche Börse zapowiedziała jednak możliwe odstępstwo od godzin handlu w dniu **30.12.2026** (możliwe wcześniejsze zamknięcie). Przed końcem grudnia poproś Claude o sprawdzenie komunikatu Deutsche Börse i dopisanie `2026-12-30` z godziną z komunikatu do `early_closes`. Jeśli aktualizacji jeszcze nie ma, rano 30.12.2026 użyj hamulca 1 (plik `data/STOP`) i niczego nie uzbrajaj na ten dzień.
- **Kalendarz wygasa po 31.12.2027.** Od 2028-01-01 silnik traktuje każdy dzień jako „wygasły kalendarz" i nie wchodzi w pozycje. Zadbaj o aktualizację tabeli świąt przed końcem 2027 roku.
- **Kopia zapasowa.** Katalog `data/` (dziennik SQLite, liczniki strat, stan kill switcha) kopiuj tylko po zatrzymaniu silnika (hamulec 3) i **poza katalogiem projektu**: kopia wewnątrz repozytorium trafiłaby do kontekstu budowy obrazu i mogłaby trafić do publicznego repozytorium. Polecenie tworzy kopię w `~/gw-kopie` (dostęp tylko dla `claude`):

```bash
(umask 077; mkdir -p ~/gw-kopie && cp -a data ~/gw-kopie/data-$(date +%F-%H%M))
```

Po kopii uruchom silnik zwykłym startem (punkt 8.1).
- **Restart serwera.** Po restarcie VPS kontener startuje sam (polityka `unless-stopped`), chyba że był zatrzymany ręcznie. Po każdym restarcie serwera sprawdź `ps` i `status`.
- **Strażnik i CI (tylko dla porządku).** Repozytorium ma zabezpieczenia pracy z Claude Code (blokada zapisów, które tworzyłyby `data/ARM_LIVE` albo `data/L1_PASSED`, zaszywałyby klucze albo ustawiały `live_allowed = true`) oraz CI w GitHub Actions bez żadnych sekretów. Opis jest w pliku `CLAUDE.md`, sekcja „Strażnik live”. To bariera przed wypadkiem, nie granica bezpieczeństwa: ostateczną bramką jesteś Ty, a wdrożenie na serwer jest zawsze ręczne i nie zależy od CI.

---

## 9. Rozwiązywanie problemów

Tabela mówi, co oznacza objaw i które polecenia z listy pod tabelą wykonać (numery w nawiasach). **Wszystkie polecenia są w blokach pod tabelą**, nie w samej tabeli.

| Objaw | Co to znaczy i co sprawdzić | Co zrobić |
|---|---|---|
| Kontener ciągle się restartuje (`Restarting`) | Proces kończy się błędem zaraz po starcie. Kod wyjścia i `OOMKilled` pokaże polecenie 2, przyczynę polecenie 3 | **Najpierw zatrzymaj** (polecenie 1), żeby nie zapętlać logowań do brokera: **każdy restart daje 3 nowe próby logowania**. Kod 3: inna instancja (wiersz `AlreadyRunning`). Kod 2: błąd konfiguracji lub pliku klucza (komunikat w logu, bez treści sekretów; zob. też wiersz o katalogu zamiast pliku klucza). `OOMKilled=true`: następny wiersz |
| `OOMKilled=true` (brak pamięci) | Jądro zabiło proces po przekroczeniu 256 MB (polecenia 2 i 4, plik `~/gw-pomiar-po.txt`) | Zatrzymaj (1), zgłoś Claude z wynikiem pomiaru. **Nie podnoś limitu** `mem_limit` na własną rękę: serwer dzieli pamięć z botem z prawdziwymi pieniędzmi |
| Status `unhealthy` | Tętno starsze niż 120 s: silnik wisi albo nie zapisuje do `data/` (miejsce na dysku, uprawnienia). Polecenia 5, 6 i 3 | Zatrzymaj (1), odczytaj logi (3), uruchom (polecenie z punktu 8.1); jeśli się powtarza, zgłoś |
| `refused: another engine instance holds ...` (kod 3, `AlreadyRunning`) | Inny silnik, `bind-account`, `smoke`, `clear-kill` albo `reset-position` trzyma blokadę (polecenie 7) | Zatrzymaj drugą instancję. Plik `engine.lock` zwalnia się sam po zakończeniu procesu: **nie usuwaj go ręcznie**. Polecenia `bind-account`, `smoke`, `clear-kill` i `reset-position` działają tylko przy zatrzymanym silniku |
| **Błąd logowania, alarm `LOGIN_FAILED`** (kill switch miękki) | Capital.com odrzuca klucz lub hasło albo logowanie nie przechodzi. Trzy nieudane logowania z rzędu, także z powodu sieci albo awarii brokera, włączają miękki kill switch `LOGIN_FAILED`. Blokada dalszych prób (`LOGIN_LOCKOUT`) zapada tylko po 3 odrzuceniach samych danych logowania; błędy sieci nie wliczają się do niej (silnik czeka wtedy 30 s, potem do 5 minut). Licznik jest tylko w pamięci, więc **każdy restart kontenera daje 3 nowe próby**. Alarm `LOGIN_LOCKOUT` wspomina w treści plik `data/LOGIN_RESET`: **nie twórz go** (omija ochronę klucza przed zablokowaniem po serii błędnych logowań); jedyną dozwoloną drogą jest procedura obok. Alarmy: polecenie 8 | **Zasada: nie więcej niż jedna próba logowania po alarmie albo problemie z kluczem.** Procedura: (a) zatrzymaj silnik (1), (b) odczekaj co najmniej 10 minut, (c) sprawdź w Capital.com, czy klucz API nie został wymieniony lub wyłączony i czy hasło się zgadza; zweryfikuj plik klucza (punkt 1.3), (d) wykonaj **jedną** próbę: pojedyncze logowanie (polecenie 9), **nie** pełny zestaw 8 logowań i **nie** wielokrotne uruchamianie silnika, (e) gdy próba przeszła: zdejmij kill switch (polecenie 10, silnik jest zatrzymany), potem uruchom silnik. Miękki kill switch od nieudanego logowania **nie znika sam po restarcie**: bez `clear-kill` silnik nie wejdzie w pozycję. (f) gdy próba nie przeszła: STOP, nie ponawiaj, zapytaj |
| Silnik w `dry_run`, mimo że uzbrojony | Silnik sam podaje powód w logu przy starcie i w `status` (polecenia 11 i 6) | Możliwe powody: `live_allowed is not true` (sprawdź polecenie 12); `data/ARM_LIVE does not exist` (pamiętaj: usunięcie pliku wyłącza wejścia do restartu); `no bound account` (zrób `bind-account`); `no frozen pre-registration fingerprint` (nie zamrożono pre-rejestracji albo plik `data/preregistration.json` ma innego właściciela niż użytkownik kontenera i jest dla niego nieczytelny: polecenie 15 i punkt 2.0) albo `fingerprint differs` (zmieniono pliki strategii, ryzyka lub kosztów, także samymi końcami linii: polecenie 24, czyli `prereg --check`, wypisze nazwy plików, które się różnią; zob. następny wiersz); po starcie także kontrola rachunku u brokera (rachunek nieaktywny, saldo 0, waluta inna niż EUR, rachunek sesji inny niż powiązany). Po każdej zmianie plików: zatrzymaj, potem uruchom (tryb wybiera się przy starcie) |
| Kontrola `prereg --check` kończy się kodem 1 (`MISMATCH`) albo 2 (`NOT FROZEN`) | Kod 1: konfiguracja jest inna niż zamrożona, a polecenie 24 wypisuje nazwy plików, które się różnią. Kod 2: nic nie zamrożono albo plik jest nieczytelny | Silnik zostaje w `dry_run`. **Nie używaj `--force`.** Wklej wynik Claude i nic nie zmieniaj, dopóki nie odpowie. Jeśli niczego świadomie nie zmieniałeś, a różnią się końce linii, przywrócenie plików z repozytorium (punkt 2.0, po potwierdzeniu Claude) jest właściwe. `--force` to wyłącznie świadome rozpoczęcie nowego wariantu (K+1), ustalone z Claude |
| Zegar serwera się rozjechał | Decyzje i kontrola ciszy notowań liczą czas z zegara serwera (polecenia 13 i 14) | Różnica powyżej 2 s jest podejrzana, powyżej 10 s wymaga naprawy synchronizacji czasu (NTP) przez administratora serwera. Do czasu naprawy hamulec 1 (plik `data/STOP`) |
| `Permission denied` w `data/` albo przy kluczu | Numer właściciela plików nie zgadza się z użytkownikiem kontenera (polecenia 15 i 16, wynik próby izolacji z punktu 1.10) | Numery w wyniku polecenia 15 muszą równać się `GW_UID` i `GW_GID`. Jeśli katalog `data` powstał jako `root`, napraw go jako `root` (polecenia 17 i 18). Jeśli polecenie 19 pokazuje `Enforcing`, a uprawnienia są poprawne, zgłoś to: **nie dodawaj sam** oznaczeń `:z` do montowań (zmieniają etykiety plików w katalogu domowym) |
| **Katalog zamiast pliku klucza**: próba izolacji pokazuje, że klucz nie jest plikiem, albo Docker zgłasza błąd montowania | Gdy plik klucza nie istniał w chwili uruchomienia polecenia docker compose, **Docker (jako root) założył w jego miejscu pusty KATALOG** o tej nazwie, a klucz nie jest czytelny (polecenie 20 pokaże `d` na początku uprawnień) | Zatrzymaj wszystko (1). Wróć na konto `root` (polecenie 17) i usuń **pusty** katalog poleceniem `rmdir` (polecenie 21; `rmdir` odmówi usunięcia czegokolwiek niepustego). Wróć na `claude` (polecenie 22) i załóż plik od nowa według Dodatku A. Zanim ponownie uruchomisz cokolwiek, powtórz punkt 1.3 |
| `error while interpolating ... GW_UID` | Brak pliku `docker/.env` | Wykonaj polecenie z punktu 1.5. Jednorazowo, tylko w tej sesji, można też użyć polecenia 23 |
| `bind-account aborted: ... nothing written` | Pomyłka w numerze pozycji albo w ostatnich 4 znakach numeru rachunku | Uruchom polecenie jeszcze raz (punkt 1.11) |
| Brak alarmów i tętna po zmianie czasu | Silnik liczy czas przez strefę czasową, zmiana czasu nie wymaga żadnych działań | Sprawdź logi (3). Jeśli wątpisz, sprawdź punkt 3 w następnej sesji |

### Polecenia do tabeli

1\. Zatrzymanie silnika:

```bash
docker compose -f docker/compose.yml stop
```

2\. Kod wyjścia, brak pamięci i licznik restartów:

```bash
docker inspect gw-stock-engine --format 'OOMKilled={{.State.OOMKilled}} kod={{.State.ExitCode}} restarty={{.RestartCount}}'
```

3\. Ostatnie logi silnika:

```bash
docker logs --tail 50 gw-stock-engine
```

4\. Zużycie pamięci kontenera i serwera:

```bash
docker stats --no-stream gw-stock-engine
```

```bash
free -m
```

5\. Dziennik kontroli zdrowia:

```bash
docker inspect gw-stock-engine --format '{{json .State.Health.Log}}'
```

6\. Stan silnika (kill switch, tętno, tryb):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

7\. Działające kontenery (czy jest inny silnik):

```bash
docker ps
```

8\. Ostatnie alarmy:

```bash
tail -n 30 data/alerts.log
```

9\. Pojedyncze logowanie (jedna próba klucza, silnik musi być zatrzymany):

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-live-readonly python -m pytest -m integration_live_readonly -k test_accounts_listed -v -p no:cacheprovider
```

10\. Zdjęcie kill switcha po nieudanym logowaniu (silnik zatrzymany; wpisz `CLEAR`):

```bash
docker compose -f docker/compose.yml run --rm gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

11\. Powód trybu `dry_run` z logu startowego:

```bash
docker logs gw-stock-engine 2>&1 | grep "mode:"
```

12\. Wartość `live_allowed` (druga linia komentarza nie jest wypisywana):

```bash
grep '^live_allowed' config/runtime.toml
```

13\. Różnica zegara serwera względem serwera Capital.com (zapytanie bez logowania, bez ryzyka dla klucza; wynik w milisekundach i zawiera opóźnienie sieci, więc kilkaset ms jest normalne):

```bash
S=$(curl -sS -m 10 https://api-capital.backend-capital.com/api/v1/time | grep -o '[0-9]\{13\}'); echo "różnica zegara serwera względem Capital.com [ms]: $(( $(date +%s%3N) - S ))"
```

14\. Stan synchronizacji czasu:

```bash
timedatectl
```

15\. Numery właściciela plików w `data/`:

```bash
ls -ln data
```

16\. Zawartość pliku z numerami użytkownika (nie zawiera sekretów):

```bash
cat docker/.env
```

17\. Przełączenie na konto `root` (wyjście z konta `claude`):

```bash
exit
```

18\. Jako `root`: naprawa właściciela katalogu danych:

```bash
chown -R claude:claude /home/claude/gw-stock-engine/data
```

19\. Tryb SELinux:

```bash
getenforce
```

20\. Rodzaj ścieżki klucza (pierwszy znak `-` to plik, `d` to katalog):

```bash
ls -ld /home/claude/.config/gw/capitalcom.json
```

21\. Jako `root`: usunięcie pustego katalogu założonego przez Dockera w miejscu pliku klucza:

```bash
rmdir /home/claude/.config/gw/capitalcom.json
```

22\. Powrót na konto `claude`:

```bash
su - claude
```

23\. Jednorazowe ustawienie zmiennych w bieżącej sesji (po nim wróć do katalogu projektu):

```bash
export GW_UID=$(id -u claude) GW_GID=$(id -g claude)
```

24\. Kontrola zgodności konfiguracji z zamrożoną pre-rejestracją (kod 0 i `OK`: zgodna; kod 1: inna, z nazwami plików; kod 2: nic nie zamrożono):

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --check
```

---

## 10. Lista kontrolna przed uzbrojeniem

Uzbrajasz (punkt 6) dopiero, gdy **każda** pozycja jest zaznaczona. Nie ma „prawie".

- [ ] Testy w obrazie przeszły (`passed`, zero `failed` i `error`).
- [ ] Odczyty live tylko do odczytu przeszły: jeden pełny przebieg (8 testów) z samymi `PASSED` i zero `SKIPPED`, a po `bind-account` pojedyncze logowanie z wynikiem `1 passed`.
- [ ] Pamięć zmierzona i w zielonych progach: po 10 minutach do 100 MiB, szczyt po pełnej sesji do 170 MiB, `available` serwera co najmniej 400 MB, `OOMKilled=false`, pozostałe kontenery bez zmian.
- [ ] `memory.swap.max` ma wartość `0` (kontener naprawdę nie używa swapu).
- [ ] Próba izolacji (punkt 1.10): `WYNIK: WSZYSTKO OK`.
- [ ] Co najmniej jedna pełna sesja `dry_run` bez rozbieżności i bez alarmów `ERROR` w `data/alerts.log`.
- [ ] Pierwsza świeca zweryfikowana z wykresem w aplikacji (punkt 3), na co najmniej dwóch dniach; znacznik czasu świecy potwierdzony.
- [ ] Ćwiczenia wykonane i kill switch zdjęty po każdym: `STOP`, `HARD_STOP`, docker kill (ćwiczenie 4.3), odcięcie sieci.
- [ ] Awaria procesu wewnątrz kontenera (ćwiczenie 4.3b): kontener wstał sam, bez błędów w logach i bez nowych alarmów `ERROR`.
- [ ] `bind-account` wykonane, `data/account.json` istnieje, rachunek to ten z 40 € w EUR.
- [ ] Pre-rejestracja zamrożona (punkt 2.0), odciski wpisane do dokumentu przez Claude, `--check` kończy się `OK`, a od zamrożenia żadnych zmian w `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`.
- [ ] `smoke` zakończony `PASS smoke: N/N`, krok [6] (drugie zamknięcie) na `PASS`, stop obejrzany w aplikacji albo zgłoszony Claude.
- [ ] Po `smoke` usunięto `data/ARM_LIVE` i `live_allowed` wróciło do `false`; uzbrojenie na stałe jest osobną, świadomą decyzją.
- [ ] W aplikacji Capital.com nie ma żadnej otwartej pozycji DE40, a saldo zgadza się z oczekiwanym.
- [ ] Wiesz, jak rozbroić (punkt 6) i jak zatrzymać natychmiast (szybkie hamulce 1 do 3 z początku dokumentu).
- [ ] Sprawdzono kalendarz: gdy zbliża się 30.12.2026, aktualizacja `early_closes` jest zrobiona (punkt 8.6).
- [ ] Dokument odbioru `docs/ops/acceptance-v1.md`: kroki bramki L0 mają wpisane daty i wyniki zgodne z oczekiwanymi, a zastrzeżenia (rentowność nie jest wykazana) są przeczytane.

---

## Dodatek A. Zakładanie albo wymiana pliku z kluczem

Wykonuj tylko wtedy, gdy pliku nie ma, ma złe pola albo wymieniasz klucz API w Capital.com. **Klucza nigdy nie wklejasz w czacie ani nie wpisujesz w treść polecenia:** poniższe polecenie samo zapyta o trzy wartości i nie pokaże ich na ekranie. Wartości bierzesz z Capital.com (ustawienia API: klucz API i hasło klucza, oraz login do konta).

Najpierw katalog (tylko dla `claude`):

```bash
install -d -m 700 /home/claude/.config/gw
```

Teraz utworzenie pliku. To **jedno** polecenie; wklej je w całości, a potem wpisz po kolei: klucz API, login (identyfikator) i hasło klucza API, każde zatwierdź Enterem (nic się nie wyświetla):

```bash
read -r -s -p "api_key: " GW_K; echo; read -r -s -p "identifier (login): " GW_I; echo; read -r -s -p "api_password: " GW_P; echo; (umask 077; GW_K="$GW_K" GW_I="$GW_I" GW_P="$GW_P" python3 -c 'import json, os; print(json.dumps({"api_key": os.environ["GW_K"], "identifier": os.environ["GW_I"], "api_password": os.environ["GW_P"]}))' > /home/claude/.config/gw/capitalcom.json); unset GW_K GW_I GW_P; chmod 600 /home/claude/.config/gw/capitalcom.json; echo "zapisano"
```

Potem wróć do punktu 1.3 i sprawdź uprawnienia oraz nazwy pól. Stary, zużyty lub kiedykolwiek wklejony do czatu klucz traktuj jako spalony: wygeneruj nowy w Capital.com i wpisz go tym poleceniem.
