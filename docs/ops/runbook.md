# Runbook właściciela: gw-stock-engine na VPS

Dokument dla właściciela, który nie musi umieć programować. Każdy blok `bash` to **jedno polecenie** (albo jeden krótki skrypt). Kopiujesz go w całości i wklejasz do terminala. Nie wpisuj znaku `$` na początku. Wynik polecenia jest opisany zawsze tekstem pod blokiem, nigdy w bloku.

Silnik to **narzędzie pomiarowe**: sprawdza, czy strategia i infrastruktura działają zgodnie ze specyfikacją. Nie obiecuje zysku, a wynik pojedynczej sesji to grosze i szum.

Oznaczenia: **[po zadaniu 14b]** i **[po zadaniu 15]** przy poleceniu znaczą, że polecenie powstaje w tych zadaniach planu i dopóki ich nie ma, nie wykonuj tego kroku (Claude powie, kiedy są gotowe).

Spis: 0 Zasady, 1 Pierwsze uruchomienie, 2 Tryb `dry_run`, 3 Weryfikacja pierwszej świecy, 4 Ćwiczenia awaryjne, 5 Próba `smoke`, 6 Uzbrajanie, 7 Pierwsze 5 sesji i bramka L1, 8 Operacje, 9 Rozwiązywanie problemów, 10 Lista kontrolna, Dodatek A (plik z kluczem).

---

## 0. Zasady

1. **Nic nie wchodzi na VPS bez pomiaru pamięci i Twojej zgody.** Na serwerze działa bot z prawdziwymi pieniędzmi (Solana), n8n i boty OKX, a wolnej pamięci jest około 700 MB. W tym dokumencie są dwie „bramy zgody" (punkty 1.2 i 2.1): przed każdą z nich wykonujesz pomiar, porównujesz go z progami i dopiero wtedy decydujesz. Wątpliwość oznacza: nie idź dalej i zapytaj.
2. **Klucz nigdy w czacie, w repozytorium, w poleceniach ani w logach.** Repozytorium jest publiczne. Klucz leży wyłącznie w pliku `/home/claude/.config/gw/capitalcom.json` (tryb 600) i trafia do kontenera tylko do odczytu. Żadnego `cat` tego pliku. Wyniki poleceń z tego dokumentu są bezpieczne do wklejenia w czacie, z jednym wyjątkiem: `bind-account` wypisuje numery rachunków i salda, ich nie wklejaj.
3. **Kod nigdy sam nie uzbraja handlu.** Uzbrajasz wyłącznie Ty (punkt 6): plikiem `data/ARM_LIVE` i wpisem `live_allowed = true`. Bez tego silnik pracuje w trybie `dry_run`: robi wszystko (logowanie, notowania, decyzje), ale **nie wysyła zleceń**.
4. **Restart zawsze oznacza „zatrzymaj, potem uruchom"**: dwa osobne polecenia, jedno po drugim. Nigdy nakładanie dwóch instancji. Nie używaj polecenia `restart` Composa ani `down`; `down` usuwa kontener razem z jego logami.
5. **SSH jako `root`, ale wszystkie polecenia projektu jako użytkownik `claude`** (to on jest właścicielem pozostałych botów i kluczy). Każde polecenie z tego dokumentu wykonujesz w katalogu `/home/claude/gw-stock-engine`, chyba że napisano inaczej.
6. **Wątpliwość oznacza STOP.** Jeśli coś wygląda dziwnie: `touch data/STOP` (brak nowych wejść), a przy prawdziwej pozycji także sprawdź aplikację Capital.com. Usunięcie `data/ARM_LIVE` i zatrzymanie kontenera to zawsze bezpieczny ruch.
7. **Nie edytuj kodu ani plików `config/strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`.** Ich zmiana tworzy nowy wariant strategii i unieważnia pre-rejestrację. Jedyny plik konfiguracji, który zmieniasz Ty, to `config/runtime.toml` (i tylko podanymi poleceniami).
8. **Czas.** Godziny w tym dokumencie to czas Europe/Berlin (taki sam jak w Holandii). Logi Dockera i pliki w `data/` są w UTC: zimą Berlin = UTC + 1 godzina (do 28.03.2027), latem UTC + 2.

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
| `free -m`, kolumna `available` | co najmniej 600 MB | 450 do 599 MB | poniżej 450 MB |
| Wolne miejsce na dysku (`df`, kolumna `Avail` dla `/var/lib`) | co najmniej 3 GB | 2 do 3 GB | poniżej 2 GB |
| Wersja cgroup (`stat`) | `cgroup2fs` | – | `tmpfs` (cgroup v1): nie idź dalej, procedura pomiaru jest wtedy inna |
| Ostrzeżenia w `docker info` | brak linii z „No swap limit support" | – | jest taka linia: nie idź dalej |

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
grep -o '"[a-z_]*" *:' /home/claude/.config/gw/capitalcom.json
```

Ma wypisać dokładnie trzy nazwy: `"api_key":`, `"identifier":` i `"api_password":` (w dowolnej kolejności), nic więcej i nic mniej. Numeru rachunku w tym pliku **nie ma**: rachunek wskaże polecenie `bind-account` (punkt 1.11). Dodatkowe pole spowoduje odmowę startu.

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

Budowa obrazu testowego (kilka minut, potrzebuje internetu do pobrania obrazu bazowego i pakietów):

```bash
docker compose -f docker/compose.yml --profile test build gw-stock-test
```

Uruchomienie testów w obrazie (kontener testowy ma limit pamięci 384 MB i działa tak samo odizolowany jak silnik):

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
```

Ostatnia linia ma mieć postać `... passed, ... skipped, ... deselected in ...` (liczby będą się zmieniać z wersją kodu) i **nie może** zawierać słów `failed` ani `error`. Pominięte (`skipped`) testy to te, które wymagają Dockera albo katalogu `docs/`, nieobecnych w obrazie. Jeśli kontener zostanie zabity z kodem 137 (brak pamięci), nie ponawiaj: zgłoś to.

### 1.8 Odczyty live tylko do odczytu (test klucza)

Ten test loguje się do Capital.com (kilkanaście logowań w odstępach ponad sekundy) i **tylko czyta**: konto, reguły DE40, notowania, świece, historię. Żadnych zleceń. Wykonuj go wyłącznie wtedy, gdy silnik nie działa:

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test python -m pytest -m integration_live_readonly -v -p no:cacheprovider
```

Wszystkie testy mają mieć wynik `PASSED`. Jeśli widzisz `SKIPPED`, kontener nie widzi pliku z kluczem (wróć do punktów 1.3 i 1.5). Jeśli widzisz `FAILED`, **nie ponawiaj w pętli** (limity logowania, patrz punkt 9): zapisz wynik i zapytaj. Po `bind-account` (punkt 1.11) powtórz ten krok jeszcze raz: wtedy testy sprawdzą także powiązany rachunek.

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
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock bind-account
```

Co zobaczysz i co wpisujesz (komunikaty programu są po angielsku):

1. Lista rachunków tego loginu, każdy w osobnej linii w postaci `1) <numer rachunku>  currency EUR  status ENABLED  balance ...  available ...`. Lista jest tylko na Twoim terminalu i nic nie jest jeszcze zapisane. Numerów **nie wklejaj** w czacie.
2. Pytanie `Number of the account to bind (1-N):`. Wpisz numer pozycji z listy (cyfrę, nie numer rachunku) dla rachunku **w EUR, `ENABLED` i z saldem większym od zera**, czyli tego z Twoimi 40 EUR. Rachunku z saldem 0, w innej walucie albo nieaktywnego program odmówi.
3. Pytanie `Type the last 4 characters of the chosen account id to confirm:`. Wpisz ostatnie 4 znaki numeru wybranego rachunku z listy z kroku 1. To jawne potwierdzenie.
4. Wynik: `Bound account ... written to ... (mode 600).` Plik `data/account.json` powstał. Przy pomyłce program pisze `bind-account aborted: ... nothing written` i nic nie zapisuje: uruchom polecenie jeszcze raz. Jeśli silnik działa, program odmówi (`refused: another engine instance holds ...`) i do niczego się nie zaloguje: najpierw `stop`.

Przy okazji mogą pojawić się linie logu z godziną i słowem `INFO`: to normalne, zignoruj je.

Jeśli wybierasz inny rachunek niż poprzednio powiązany, program każe wpisać słowo `OVERWRITE`. Sprawdź w aplikacji Capital.com, że wybrałeś rachunek z saldem 40 €, a potem wróć do punktu 1.8 i powtórz test odczytów (tym razem sprawdzi powiązany rachunek).

### 1.12 Stan silnika (`status`)

Polecenie tylko czyta pliki i dziennik, nie loguje się do brokera i niczego nie zmienia:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock status
```

Oczekiwane, bo silnik jeszcze nie pracował: linia `mode (files): dry_run (...)` z listą brakujących warunków uzbrojenia (to dobrze), `heartbeat: none` i `journal: none yet`.

---

## 2. Uruchomienie w trybie `dry_run`

### 2.0 Zamrożenie pre-rejestracji [po zadaniu 15]

Specyfikacja (sekcja 11) wymaga zamrożenia odcisków konfiguracji **przed pierwszą sesją `dry_run`**. Po zadaniu 15 wykonaj raz:

```bash
docker compose -f docker/compose.yml run --rm -T gw-stock-engine python -m gwstock.prereg --write
```

Od tego momentu **nie zmieniaj** `config/strategy_v1.toml`, `risk_v1.toml` ani `costs_v1.toml`. Zmiana unieważnia pre-rejestrację i tworzy nowy wariant (zwiększa liczbę prób K). Kontener nie ma zapisu nigdzie poza `/data` i `/tmp`; gdyby polecenie zgłosiło brak możliwości zapisu dokumentu pre-rejestracji w repozytorium, nie obchodź tego, tylko zgłoś (dokładny przebieg opisze zadanie 15).

### 2.1 Brama zgody nr 2: pierwszy start silnika

Przed pierwszym uruchomieniem usługi sprawdź ponownie wolną pamięć:

```bash
free -m
```

Warunki startu (wszystkie naraz):

- kolumna `available` co najmniej **500 MB** (w najgorszym razie silnik zajmie 256 MB, więc reszta serwera zachowa co najmniej 244 MB); 400 do 499 MB to żółte światło: zapytaj; poniżej 400 MB: STOP,
- w punkcie 1.10 jest `WYNIK: WSZYSTKO OK` z `memory.swap.max` równym `0`,
- testy z punktu 1.7 przeszły bez `failed`,
- po stronie Capital.com nic nie jest w trakcie (silnik w `dry_run` i tak nie wysyła zleceń).

**Zgoda: uruchamiasz silnik tylko jako świadomą decyzję po spełnieniu tych warunków.** Nie masz pewności: wklej wynik Claude i poczekaj na „ok". Jeśli po starcie pamięć zacznie rosnąć ponad progi z punktu 2.4, zatrzymaj usługę poleceniem z tego punktu.

### 2.2 Start

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Tryb jest `dry_run`, dopóki nie spełnisz wszystkich czterech warunków uzbrojenia z punktu 6. Kontener startuje z polityką `unless-stopped`: po restarcie serwera lub Dockera uruchomi się sam (w trybie wynikającym z plików). Aby tego uniknąć, zatrzymaj go poleceniem `docker compose -f docker/compose.yml stop`; zatrzymany ręcznie sam nie wstanie.

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

| Pomiar | Zielone: dalej | Żółte: zapytaj Claude, nie idź dalej | Czerwone: `docker compose -f docker/compose.yml stop` |
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

Dla orientacji: sam interpreter z biblioteką `aiohttp` zajmuje ok. 45 MB, a nieruchomy silnik ok. 30 MB w `docker stats`. Kontrola zdrowia co minutę uruchamia na chwilę drugi proces Pythona (kolejne ok. 45 MB, w tym samym limicie 256 MB).

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

W skrócone sesje (kalendarz je zna) spłaszczenie jest 5 minut przed skróconym zamknięciem.

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

Pamięć ma zostać w zielonych progach z punktu 2.4. **Po pierwszej pełnej sesji powtórz cały pomiar z punktu 2.4:** pole `memory.peak` (jeśli jądro je ma) pokazuje szczyt od startu kontenera; bez niego patrz na najwyższy odczyt `docker stats` z całego dnia. Szczyt do 170 MiB jest zielony, 171 do 220 MiB żółty (zapytaj), powyżej 220 MiB albo `OOMKilled=true` czerwony.

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

Cel: zobaczyć na własne oczy, że mechanizmy bezpieczeństwa działają, zanim cokolwiek zależy od nich naprawdę. Wszystkie ćwiczenia robisz w `dry_run`, czyli bez żadnej pozycji. Do zdjęcia kill switcha używasz polecenia `clear-kill` **[po zadaniu 14b]**; dopóki go nie ma, **nie rób ćwiczeń 4.1, 4.2 i 4.4** (po nich kill switch nie dałby się zdjąć). Ćwiczenie 4.3 możesz zrobić wcześniej.

Zasada ogólna: kill switch zapisuje swój stan w dzienniku i **przeżywa restart**. Usunięcie pliku `STOP` albo `HARD_STOP` **nie zdejmuje** poziomu: robi to wyłącznie `clear-kill`.

### 4.1 Miękki kill switch: `data/STOP`

```bash
touch data/STOP
```

Odczekaj ok. 15 sekund i sprawdź alarm:

```bash
tail -n 5 data/alerts.log
```

Oczekiwane: linia w rodzaju `... SOFT kill switch SOFT: STOP_FILE`.

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock status
```

Oczekiwane: `kill switch: SOFT (STOP_FILE, since ...)`. Poziom miękki oznacza: brak nowych wejść, istniejąca pozycja jest zarządzana normalnie.

Powrót do normy (kolejność ma znaczenie). Najpierw usuń plik:

```bash
rm -f data/STOP
```

Sprawdź, że `status` **nadal** pokazuje `SOFT` (tak ma być). Zatrzymaj silnik:

```bash
docker compose -f docker/compose.yml stop
```

Zdejmij kill switch **[po zadaniu 14b]**. Program pokaże poprzedni poziom i powód, poprosi o wpisanie potwierdzenia (wpisz dokładnie to, o co prosi) i zapisze, kto zdjął blokadę. Po `--by` jest tylko nazwa do zapisu w dzienniku, możesz wpisać swoje imię bez spacji:

```bash
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock clear-kill --by wlasciciel
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

Oczekiwane: alarm o eskalacji do `HARD` z powodem `HARD_STOP_FILE`.

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

Zdejmij kill switch **[po zadaniu 14b]**:

```bash
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

### 4.3 Nagłe zabicie procesu (`kill -9`), najlepiej w trakcie sesji

Symuluje awarię bez żadnego czystego zamknięcia:

```bash
docker kill -s KILL gw-stock-engine
```

Sprawdź:

```bash
docker compose -f docker/compose.yml ps -a
```

Oczekiwane: `Exited (137)`. Kontener zabity ręcznie **nie wstaje sam** (polityka restartu nie dotyczy ręcznego zabicia; tak działa Docker 29 na maszynie testowej, na VPS to potwierdza to ćwiczenie). Uruchom go ponownie zwykłym startem:

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

Silnik zaczyna znów dostawać notowania, ale `SOFT` **zostaje**, dopóki go nie zdejmiesz: `stop`, `clear-kill` **[po zadaniu 14b]**, `up -d`, jak w 4.1.

Jeśli w którymkolwiek ćwiczeniu zobaczysz coś, czego ta instrukcja nie opisuje: `docker compose -f docker/compose.yml stop` i zgłoś.

---

## 5. Próba `smoke`

**Uwaga: to jest pierwsza prawdziwa transakcja.** `smoke` wysyła na Twoje konto jedno zlecenie rynkowe DE40 o wielkości **0,001** (najmniejsza możliwa) ze stopem i **zamyka je od razu**. Sprawdza całą ścieżkę zleceń: otwarcie, widoczność pozycji i stopu, zamknięcie, księgowanie, drugie zamknięcie (musi zostać odrzucone), ostatnie świece i opóźnienie historii. Koszt to spread i poślizg przy wielkości 0,001, czyli rząd ułamka grosza do około 1 centa (jeden punkt przy 0,001 to ok. 0,001 €; dokładną wartość punktu potwierdza ta próba).

### Warunki wstępne (wszystkie)

- Bramka L0 zaliczona do tego miejsca: pełna sesja `dry_run` bez rozbieżności i błędów, świeca zweryfikowana (punkt 3), ćwiczenia (punkt 4), pamięć zmierzona (punkty 2.4 i 2.6), testy w obrazie i odczyty live przeszły.
- `bind-account` wykonane (jest `data/account.json`).
- Pre-rejestracja zamrożona [po zadaniu 15], bo `smoke` działa tylko w trybie `live`, a ten wymaga zgodnego odcisku konfiguracji.
- Na rachunku jest saldo (40 €), a w aplikacji Capital.com nie ma żadnej otwartej pozycji DE40.
- Dzień roboczy, 10:00 do 16:00 czasu Berlina (płynny rynek, wąski spread), **nie** w okolicy 09:00 do 09:10 ani 17:25, i **nie** w przerwie 21:00 do 21:05 UTC.
- Aplikacja Capital.com otwarta na ekranie pozycji DE40.

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

Uruchom próbę:

```bash
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock smoke
```

### Co zobaczysz

Linie `PASS [n]`, `FAIL [n]` i `INFO [n]`, a na końcu `PASS smoke: N/N checks passed` (kod wyjścia 0) albo `FAIL`. Znaczenie kroków: [1] rachunek poprawny, [2] wejście przyjęte (z poziomem otwarcia i stopu), [3] pozycja widoczna ze stopem, [4] spłaszczenie przez wykonawcę, [5] pozycja zniknęła i transakcja zaksięgowana (z kosztami), [6] drugie zamknięcie odrzucone jako „nie znaleziono" (to jest wymagane), [7] trzy ostatnie świece 5-minutowe do porównania z wykresem, [8] opóźnienie historii.

Jeśli w kroku [2] jest `INFO ... the API did not show the stop: verify the stop in the Capital.com app before arming`, broker nie pokazał stopu w liście pozycji i silnik zapisał stop **wyliczony z naszego żądania**. Wtedy musisz sam zobaczyć stop w aplikacji (poniżej).

### Co sprawdzasz w aplikacji Capital.com

Pozycja żyje tylko kilka sekund, więc:

1. W momencie startu polecenia odświeżaj ekran pozycji: pozycja DE40 o wielkości 0,001 powinna się pojawić **z ustawionym stop lossem** (zapisz poziom).
2. Po próbie otwórz historię (aktywność lub zamknięte pozycje): ma być **dokładnie jedna** transakcja DE40 0,001, otwarta i zamknięta, i **żadnej** pozostałej otwartej pozycji.
3. W szczegółach transakcji poszukaj poziomu stopu; porównaj z `stop level` z kroku [2].
4. Porównaj świece z kroku [7] z wykresem 5-minutowym (zasada ta sama co w punkcie 3: początek świecy w UTC i ceny środkowe).

Jeśli nie zdążysz zobaczyć stopu na żywo ani znaleźć go w historii, **zapisz to i zgłoś Claude**: poziom stopu to pozycja C24, a próba nie ma dziś opcji dłuższego trzymania pozycji.

### Po próbie (zawsze, także gdy wszystko jest PASS)

Zdejmij uzbrojenie. Uzbrajanie na stałe to osobna decyzja (punkt 6):

```bash
rm -f data/ARM_LIVE
```

```bash
sed -i 's/^live_allowed = true/live_allowed = false/' config/runtime.toml
```

```bash
grep live_allowed config/runtime.toml
```

Ma być `live_allowed = false`. Zapisz wynik próby (cały wydruk) i przekaż Claude. Silnik po próbie zostaje zatrzymany; jeśli chcesz wrócić do pracy w `dry_run`, uruchom go (restart zawsze jako osobny krok po `stop`, więc to już jest ten drugi krok):

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

**Reguła przerwania:** przy jakiejkolwiek wątpliwości (FAIL, pozycja nadal otwarta w aplikacji, dziwny wydruk) natychmiast usuń `data/ARM_LIVE` (polecenie wyżej), sprawdź aplikację, a gdy pozycja jest otwarta, **zamknij ją ręcznie w aplikacji**. Potem opisz sytuację Claude i nie ponawiaj próby.

---

## 6. Uzbrajanie (live)

**Wykonuj dopiero po zaliczeniu całej listy z punktu 10 i tylko z własnej, świadomej decyzji.** Nikt i nic nie zrobi tego za Ciebie.

### Cztery warunki (wszystkie naraz)

1. `live_allowed = true` w `config/runtime.toml`.
2. Plik `data/ARM_LIVE` utworzony przez Ciebie.
3. Powiązany rachunek: `bind-account` wykonane (jest `data/account.json`).
4. Zamrożona pre-rejestracja [po zadaniu 15], zgodna z bieżącą konfiguracją: `python -m gwstock.prereg --write` (punkt 2.0), od tego czasu bez zmian w `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`.

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

Najlepiej poza sesją albo po spłaszczeniu. W razie nagłej potrzeby podczas sesji najpierw `touch data/STOP`. Rozbrojenie to: usunięcie pliku, zatrzymanie, uruchomienie (restart zawsze w dwóch krokach):

```bash
rm -f data/ARM_LIVE
```

```bash
docker compose -f docker/compose.yml stop
```

```bash
sed -i 's/^live_allowed = true/live_allowed = false/' config/runtime.toml
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

W logu ma być `mode: dry_run, no orders will be sent: ...`. Pozycja otwarta w chwili rozbrojenia nadal jest zarządzana (stop u brokera, spłaszczenie o 17:25), bo wyjść nie blokujemy nigdy.

---

## 7. Pierwsze 5 sesji i bramka L1

Pierwsze 5 sesji live silnik handluje wielkością minimalną 0,001. Po każdej sesji, po 17:35:

```bash
docker compose -f docker/compose.yml exec -T gw-stock-engine python -m gwstock.report
```

**[po zadaniu 15]**: raport dzienny (wynik, koszty, status bramki L1 i instrukcja). Dodatkowo `status` i `tail -n 30 data/alerts.log` (punkt 2.6).

Co przeglądasz po każdej sesji i na koniec piątej (warunki bramki L1 ze specyfikacji, sekcja 11):

- **zero** niewyjaśnionych rozbieżności pozycja/broker starszych niż 60 sekund (w `alerts.log` nie ma `ERROR` bez wyjaśnienia),
- **zero** nieobsłużonych odrzuceń zleceń,
- spread i poślizg stopów zmierzone i porównane z modelem; rozbieżność kosztów większa niż 2 razy model oznacza przegląd z Claude,
- transakcje w raporcie zgadzają się z historią w aplikacji Capital.com (cena wejścia, wyjścia, wynik),
- pamięć nadal w zielonych progach (punkt 2.4).

**Bramka L1.** Dopiero po przeglądzie, który wypadł dobrze, i po 5 sesjach, utwórz plik:

```bash
touch data/L1_PASSED
```

Plik tworzy wyłącznie właściciel, kod nigdy. Silnik sprawdza go przy każdej decyzji, więc restart nie jest potrzebny. **Bez tego pliku wielkość pozostaje 0,001**, nawet po 5 sesjach. Z plikiem, od 6. sesji, wielkość liczona jest regułami strategii (zawsze w granicach limitów ryzyka). Wycofanie: `rm -f data/L1_PASSED`.

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

Wykonuj poza sesją, np. po 17:35 albo w weekend, gdy `status` pokazuje `position DE40: FLAT`. Jeśli jesteś uzbrojony, najpierw rozbrój (punkt 6), żeby `git` nie widział lokalnej zmiany `runtime.toml`.

Zapisz logi kontenera (znikają razem z kontenerem przy jego odtworzeniu):

```bash
docker logs gw-stock-engine > data/logi-przed-aktualizacja.txt 2>&1
```

```bash
docker compose -f docker/compose.yml stop
```

```bash
git pull --ff-only
```

```bash
git log --oneline -3
```

Sprawdź skrót z Claude, a potem zbuduj oba obrazy i powtórz testy (punkty 1.7 i 1.9):

```bash
docker compose -f docker/compose.yml --profile test build gw-stock-test
```

```bash
docker compose -f docker/compose.yml --profile test run --rm gw-stock-test
```

```bash
docker compose -f docker/compose.yml build gw-stock-engine
```

```bash
docker compose -f docker/compose.yml up -d gw-stock-engine
```

Jeśli aktualizacja zmienia `strategy_v1.toml`, `risk_v1.toml` albo `costs_v1.toml`, to nowy wariant strategii: odcisk przestanie pasować do pre-rejestracji i silnik zostanie w `dry_run`. **Nie uzbrajaj ponownie bez ustalenia z Claude.**

### 8.3 Czytanie alarmów

```bash
tail -n 50 data/alerts.log
```

```bash
grep -E "ERROR|HARD" data/alerts.log | tail -n 20
```

Format linii: `czas-UTC POZIOM treść`. Alarmy są też w logu kontenera (`docker logs`), ale `data/alerts.log` przeżywa odtworzenie kontenera.

### 8.4 Poziomy kill switcha

| Poziom | Skutek | Przykładowe powody |
|---|---|---|
| **Miękki (SOFT)** | Brak nowych wejść; istniejąca pozycja jest zarządzana normalnie | plik `data/STOP`; dzienny limit strat; rozbieżność z brokerem dłuższa niż 60 s; cisza notowań dłuższa niż 60 s; nieudane logowanie; trzy odrzucenia z rzędu |
| **Twardy (HARD)** | Zamyka pozycje procedurą awaryjną, potem działa jak miękki | plik `data/HARD_STOP`; strata dzienna co najmniej 2 razy limit; strata łączna od startu 12 €; pozycja bez stopu dłużej niż 10 s |

Oba poziomy są zapisane w dzienniku, przeżywają restart i **zdejmuje je wyłącznie Twoje polecenie `clear-kill`**; twardy zostaje do tego czasu także po restarcie. Procedura zdjęcia, po kolei. Najpierw ustal przyczynę (polecenie z punktu 8.3). Potem usuń pliki, zatrzymaj silnik i uruchom `clear-kill` **[po zadaniu 14b]**:

```bash
rm -f data/STOP data/HARD_STOP
```

```bash
docker compose -f docker/compose.yml stop
```

```bash
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock clear-kill --by wlasciciel
```

Polecenie odmówi, jeśli pliki `STOP` albo `HARD_STOP` nadal istnieją. Na końcu uruchom silnik (`up -d`, jak w punkcie 8.1).

### 8.5 Stan `BLOCKED` pozycji

`BLOCKED` oznacza, że silnik nie potrafi **udowodnić**, co jest na koncie (np. inny rozmiar lub kierunek niż zapisany, pozycja, której nie otworzył, albo niejednoznaczna historia zamknięcia). Silnik nie zgaduje i nie dotyka takiej pozycji (poza awaryjnym spłaszczeniem pozycji, o której dowodnie wie, że jest jego). Bezpieczne rozwiązanie, zawsze w tej kolejności:

1. Otwórz aplikację Capital.com i sprawdź, **czy istnieje otwarta pozycja DE40**.
2. Jeśli **jest** (prawdziwa ekspozycja): zdecyduj, czy ją zamknąć. Zamknij ją ręcznie w aplikacji, jeśli nie ma być trzymana.
3. Zatrzymaj silnik (`docker compose -f docker/compose.yml stop`).
4. Gdy w aplikacji **nie ma** pozycji DE40: wyczyść stan lokalny poleceniem **[po zadaniu 14b]**. Polecenie samo sprawdza u brokera, że nie ma pozycji ani niewyjaśnionego zlecenia, prosi o potwierdzenie i zapisuje zdarzenie audytowe:

```bash
docker compose -f docker/compose.yml run --rm -it gw-stock-engine python -m gwstock reset-position --by wlasciciel
```

5. Uruchom silnik (`up -d`). Jeśli polecenie odmówi, bo broker nadal widzi pozycję, wróć do punktu 1.

### 8.6 Uwagi operatorskie

- **Nieczytelny stan kill switcha przy starcie.** Jeśli przy starcie dziennik był chwilowo nieczytelny, kill switch ustawia się na `SOFT` z powodem `KILL_STATE_UNREADABLE` i **zostaje miękki przez kolejne restarty**, aż go zdejmiesz `clear-kill`. To zamierzona ochrona: nie jest awarią sam w sobie, ale sprawdź przyczynę (miejsce na dysku, uprawnienia do `data/`).
- **Pozycja trzymana dłużej niż 14 dni.** Silnik pobiera historię w oknach nie dłuższych niż 14 dni. Jeśli Ty (właściciel) trzymasz własną pozycję DE40 dłużej niż 14 dni i zamkniesz ją, gdy silnik jest wyłączony, historia może stać się niejednoznaczna i silnik zgłosi głośny błąd historii (`HISTORY_AMBIGUOUS`). Wtedy potrzebna jest Twoja interwencja z Claude; nie próbuj tego obchodzić. Zasada praktyczna: nie handluj ręcznie DE40 na rachunku silnika.
- **Grudzień 2026.** Deutsche Börse zapowiedziała możliwe odstępstwo od godzin handlu w dniu **30.12.2026** (możliwe wcześniejsze zamknięcie). Kalendarz silnika tego jeszcze nie zawiera. Przed końcem grudnia poproś Claude o sprawdzenie komunikatu Deutsche Börse i dopisanie `2026-12-30` do `early_closes` w kalendarzu XETRA; jeśli aktualizacji jeszcze nie ma, rano 30.12.2026 wykonaj `touch data/STOP`.
- **Kalendarz wygasa po 31.12.2027.** Od 2028-01-01 silnik traktuje każdy dzień jako „wygasły kalendarz" i nie wchodzi w pozycje. Zadbaj o aktualizację tabeli świąt przed końcem 2027 roku.
- **Kopia zapasowa.** Katalog `data/` (dziennik SQLite, liczniki strat, stan kill switcha) kopiuj tylko po zatrzymaniu silnika: `docker compose -f docker/compose.yml stop`, potem `cp -a data data.kopia-$(date +%F)`.
- **Restart serwera.** Po restarcie VPS kontener startuje sam (polityka `unless-stopped`), chyba że był zatrzymany ręcznie. Po każdym restarcie serwera sprawdź `ps` i `status`.

---

## 9. Rozwiązywanie problemów

| Objaw | Sprawdź | Co zrobić |
|---|---|---|
| Kontener ciągle się restartuje (`Restarting`) | `docker inspect gw-stock-engine --format 'OOMKilled={{.State.OOMKilled}} kod={{.State.ExitCode}} restarty={{.RestartCount}}'` oraz `docker logs --tail 50 gw-stock-engine` | **Najpierw** `docker compose -f docker/compose.yml stop` (żeby nie zapętlać logowań do brokera). Kod 3: inna instancja (patrz `AlreadyRunning`). Kod 2: błąd konfiguracji lub pliku klucza (komunikat w logu, bez treści sekretów). `OOMKilled=true`: patrz następny wiersz |
| `OOMKilled=true` (brak pamięci) | `docker stats --no-stream`, `free -m`, plik `~/gw-pomiar-po.txt` | `stop`, zgłoś Claude z wynikiem pomiaru. **Nie podnoś limitu** `mem_limit` na własną rękę: serwer dzieli pamięć z botem z prawdziwymi pieniędzmi |
| Status `unhealthy` | `docker inspect gw-stock-engine --format '{{json .State.Health.Log}}'`; `status` (wiek tętna); `docker logs --tail 50 gw-stock-engine` | Kontrola zdrowia jest zielona, gdy tętno jest świeższe niż 120 s. Gdy nie jest, silnik wisi albo nie zapisuje do `data/` (miejsce na dysku, uprawnienia). `stop`, odczytaj logi, `up -d`; jeśli się powtarza, zgłoś |
| `refused: another engine instance holds /data/engine.lock` (kod 3, `AlreadyRunning`) | `docker ps`, czy działa inny kontener silnika albo `bind-account` / `smoke` | Zatrzymaj drugą instancję. Plik `engine.lock` zwalnia się sam po zakończeniu procesu: **nie usuwaj go ręcznie**. `bind-account`, `smoke` i `clear-kill` działają tylko przy zatrzymanym silniku |
| Błąd logowania, alarm `LOGIN_FAILED` | `tail -n 30 data/alerts.log` | **Nie ponawiaj logowania w pętli** (limit brokera: 1 logowanie na sekundę, a seria błędów grozi zablokowaniem klucza). Po 3 nieudanych próbach silnik sam przestaje się logować i włącza `SOFT` (`LOGIN_LOCKOUT`). Zatrzymaj silnik, odczekaj co najmniej 10 minut, sprawdź w Capital.com, czy klucz API nie został wymieniony lub wyłączony i czy hasło się zgadza, zweryfikuj plik klucza (punkt 1.3), wykonaj **jedną** próbę (punkt 1.8), dopiero potem `up -d`. Silnik zapomina serię błędów po restarcie; plik `data/LOGIN_RESET` zdejmuje blokadę logowań bez restartu |
| Silnik w `dry_run`, mimo że uzbrojony | `docker logs gw-stock-engine 2>&1 \| grep "mode:"` oraz `status` (linia `mode (files)`) | Silnik sam podaje powód. Możliwe powody: `live_allowed is not true` (sprawdź `grep live_allowed config/runtime.toml`); `data/ARM_LIVE does not exist`; `no bound account` (zrób `bind-account`); `no frozen pre-registration fingerprint` albo `fingerprint differs` (nie zrobiono `prereg --write` albo zmieniono pliki strategii, ryzyka lub kosztów); po starcie także kontrola rachunku u brokera (rachunek nieaktywny, saldo 0, waluta inna niż EUR, rachunek sesji inny niż powiązany). Po każdej zmianie plików: `stop`, potem `up -d` (tryb wybiera się przy starcie) |
| Zegar serwera się rozjechał | polecenie poniżej tabeli; `timedatectl` | Różnica powyżej 2 s jest podejrzana, powyżej 10 s wymaga naprawy synchronizacji czasu (NTP) przez administratora serwera. Do czasu naprawy `touch data/STOP` |
| `Permission denied` w `data/` albo przy kluczu | `ls -ln data`; `cat docker/.env`; wynik próby izolacji (punkt 1.10); `getenforce` | Numery właściciela w `ls -ln` muszą równać się `GW_UID` i `GW_GID`. Jeśli katalog `data` powstał jako `root`, jako `root` wykonaj `chown -R claude:claude /home/claude/gw-stock-engine/data`. Jeśli `getenforce` pokazuje `Enforcing` i uprawnienia są poprawne, zgłoś to: **nie dodawaj sam** oznaczeń `:z` do montowań (zmieniają etykiety plików w katalogu domowym) |
| `error while interpolating ... GW_UID` | `ls docker/.env` | Brak pliku `docker/.env`: wykonaj polecenie z punktu 1.5. Jednorazowo można też użyć `export GW_UID=$(id -u claude) GW_GID=$(id -g claude)` w tej samej sesji |
| `bind-account aborted: ... nothing written` | treść komunikatu | Uruchom polecenie jeszcze raz i wpisz poprawny numer pozycji oraz ostatnie 4 znaki numeru rachunku (punkt 1.11) |
| Brak alarmów i tętna po zmianie czasu | `docker logs --tail 50 gw-stock-engine` | Silnik liczy czas przez strefę czasową, zmiana czasu nie wymaga żadnych działań. Jeśli wątpisz, sprawdź punkt 3 w następnej sesji |

Pomiar różnicy zegara względem serwera Capital.com (zapytanie bez logowania, bez ryzyka dla klucza; wynik w milisekundach, zawiera opóźnienie sieci, więc kilkaset ms jest normalne):

```bash
S=$(curl -sS -m 10 https://api-capital.backend-capital.com/api/v1/time | grep -o '[0-9]\{13\}'); echo "różnica zegara serwera względem Capital.com [ms]: $(( $(date +%s%3N) - S ))"
```

---

## 10. Lista kontrolna przed uzbrojeniem

Uzbrajasz (punkt 6) dopiero, gdy **każda** pozycja jest zaznaczona. Nie ma „prawie".

- [ ] Testy w obrazie przeszły (`passed`, zero `failed` i `error`).
- [ ] Odczyty live tylko do odczytu przeszły: wszystkie `PASSED`, zero `SKIPPED`, także po `bind-account`.
- [ ] Pamięć zmierzona i w zielonych progach: po 10 minutach do 100 MiB, szczyt po pełnej sesji do 170 MiB, `available` serwera co najmniej 400 MB, `OOMKilled=false`, pozostałe kontenery bez zmian.
- [ ] `memory.swap.max` ma wartość `0` (kontener naprawdę nie używa swapu).
- [ ] Próba izolacji (punkt 1.10): `WYNIK: WSZYSTKO OK`.
- [ ] Co najmniej jedna pełna sesja `dry_run` bez rozbieżności i bez alarmów `ERROR` w `data/alerts.log`.
- [ ] Pierwsza świeca zweryfikowana z wykresem w aplikacji (punkt 3), na co najmniej dwóch dniach; znacznik czasu świecy potwierdzony.
- [ ] Ćwiczenia wykonane i kill switch zdjęty po każdym: `STOP`, `HARD_STOP`, `kill -9`, odcięcie sieci.
- [ ] `bind-account` wykonane, `data/account.json` istnieje, rachunek to ten z 40 € w EUR.
- [ ] Pre-rejestracja zamrożona [po zadaniu 15], od tego czasu żadnych zmian w `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml`.
- [ ] `smoke` zakończony `PASS smoke: N/N`, krok [6] (drugie zamknięcie) na `PASS`, stop obejrzany w aplikacji albo zgłoszony Claude.
- [ ] Po `smoke` usunięto `data/ARM_LIVE` i `live_allowed` wróciło do `false`; uzbrojenie na stałe jest osobną, świadomą decyzją.
- [ ] W aplikacji Capital.com nie ma żadnej otwartej pozycji DE40, a saldo zgadza się z oczekiwanym.
- [ ] Wiesz, jak rozbroić (punkt 6) i jak zatrzymać natychmiast (`touch data/STOP`, `docker compose -f docker/compose.yml stop`).
- [ ] Sprawdzono kalendarz: gdy zbliża się 30.12.2026, aktualizacja `early_closes` jest zrobiona (punkt 8.6).

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
