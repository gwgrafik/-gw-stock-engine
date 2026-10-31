# Notatki o Docker Compose (limity pamięci, izolacja, zdrowie, restart)

Odczyt dokumentacji: 2026-10-31. Dokument jest podstawą Zadania 16 (Docker, Compose, runbook) i pomiaru pamięci w Zadaniu 18. Wymóg pochodzi ze spec §13 i pytania otwartego 9 (rachunkowość swapu w jądrze VPS).

Dokument nie zawiera kluczy, haseł ani danych rachunku.

## 0. Źródła i znaczenie statusów

| Skrót | Źródło |
|---|---|
| [D1] | Compose Specification, usługi: <https://docs.docker.com/reference/compose-file/services/> (czytane z surowego markdownu strony: <https://github.com/docker/docs/blob/main/content/reference/compose-file/services.md>) |
| [D2] | Compose Specification, `deploy`: <https://docs.docker.com/reference/compose-file/deploy/> |
| [D3] | Compose Specification, rozszerzenia (jednostki bajtów i czasu): <https://docs.docker.com/reference/compose-file/extension/> |
| [D4] | Docker Engine, ograniczenia zasobów: <https://docs.docker.com/engine/containers/resource_constraints/> |
| [D5] | Docker Engine, metryki w czasie działania, sekcja "Running Docker on cgroup v2": <https://docs.docker.com/engine/containers/runmetrics/> |
| [D6] | Docker Engine, automatyczny start kontenerów (polityki restartu): <https://docs.docker.com/engine/containers/start-containers-automatically/> |
| [D7] | Dockerfile, `HEALTHCHECK`: <https://docs.docker.com/reference/dockerfile/#healthcheck> |
| [D8] | Docker Engine, rozwiązywanie problemów, "Kernel cgroup swap limit capabilities": <https://docs.docker.com/engine/daemon/troubleshoot/#kernel-cgroup-swap-limit-capabilities> |
| [D9] | `docker compose config`: <https://docs.docker.com/reference/cli/docker/compose/config/> |
| [D10] | Docker Engine, montowania tmpfs: <https://docs.docker.com/engine/storage/tmpfs/> |
| [D11] | Źródło demona Docker (moby), plik `daemon/daemon_unix.go` w wersji v28.5.2: <https://github.com/moby/moby/blob/v28.5.2/daemon/daemon_unix.go>. Tag `v28.5.2` istnieje (sprawdzone `git ls-remote --tags https://github.com/moby/moby`), a cytowane ostrzeżenia znalazłem w surowym pliku tego tagu. Źródło wtórne: pokazuje, co demon robi z wartościami, ale to nie jest dokumentacja |
| [D12] | Zgłoszenie docker/compose #13898 (wymiana z lipca 2026 o `memswap_limit` na cgroup v2): <https://github.com/docker/compose/issues/13898>. Źródło wtórne, wypowiedź opiekuna Compose |
| [D13] | Notatki wydania Compose v5.6.0 (2026-10-02): <https://github.com/docker/compose/releases/tag/v5.6.0> |
| [D14] | `docker container run`, sekcja "Mount volume read-only": <https://docs.docker.com/reference/cli/docker/container/run/> |

Status każdej pozycji, dokładnie jeden z dwóch napisów (te same co w `capitalcom-api-notes.md`):

- `udokumentowane`: oficjalna dokumentacja ([D1] do [D10]) wprost podaje to, o co pytamy.
- `do potwierdzenia w dry_run lub w odczytach live`: dokumentacja milczy albo podaje tylko część. Dla Compose rozstrzyga to pomiar i odczyt konfiguracji na VPS, zgodnie z procedurą z sekcji 4 i Zadaniem 18. Przy każdej takiej pozycji dopisuję polecenie, które ją rozstrzyga.

## 1. Pola z wymogu spec §13

Wymóg spec §13: kontener `python:3.12-slim`, użytkownik bez uprawnień, system plików tylko do odczytu z tmpfs i wolumenem `/data`; `mem_limit` 256 MB, `memswap_limit` równy `mem_limit` (kontener bez swapu), niższe `mem_reservation`, `pids_limit`, `oom_score_adj` +1000; healthcheck sprawdzający świeżość pliku tętna; restart `unless-stopped` (test w Zadaniu 16).

### 1.1 Przegląd i statusy

| Pole | Semantyka | Efekt na hoście | Wartość v1 | Status pozycji |
|---|---|---|---|---|
| `mem_limit` | opisana w [D1] | do zmierzenia na VPS (pkt 4) | `256m` | do potwierdzenia w dry_run lub w odczytach live |
| `memswap_limit` | opisana w [D1] | do zmierzenia na VPS (pkt 4) | `256m` | do potwierdzenia w dry_run lub w odczytach live |
| `mem_reservation` | opisana w [D1] | do zmierzenia na VPS (pkt 4) | `128m` (początkowa, do pomiaru) | do potwierdzenia w dry_run lub w odczytach live |
| `oom_score_adj` | opisana w [D1] | do zmierzenia na VPS (pkt 4) | `1000` | do potwierdzenia w dry_run lub w odczytach live |
| `pids_limit` | opisana w [D1] | do zmierzenia na VPS (pkt 4) | `64` (początkowa, do pomiaru) | do potwierdzenia w dry_run lub w odczytach live |
| `read_only` | opisana w [D1] | wynika wprost z opisu | `true` | udokumentowane |
| `tmpfs` | opisana w [D1] i [D10]; rozmiar w formie skróconej (`size=`) nie jest wymieniony w [D1] | do sprawdzenia `df -h /tmp` (pkt 1.8) | `/tmp` z ograniczeniem rozmiaru | do potwierdzenia w dry_run lub w odczytach live |
| `healthcheck` | opisana w [D1] i [D7] | wynika wprost z opisu | polecenie sprawdzające świeżość pliku tętna | udokumentowane |
| `restart` | opisana w [D1] i [D6] | wynika wprost z opisu | `unless-stopped` | udokumentowane |

Znaczenie kolumn: "Semantyka" to pytanie, czy pole istnieje i co oznacza w Compose; "Efekt na hoście" to pytanie, czy na konkretnym VPS (jądro, cgroup, wersja Docker Engine i runc) wartość faktycznie zadziała. Dla pól pamięci i procesów efekt zależy od hosta, więc jest do pomiaru. Napis statusu występuje tylko w kolumnie "Status pozycji" i jest najsłabszym ogniwem z semantyki i efektu.

### 1.2 `mem_limit`

Cytat [D1]: "`mem_limit` configures a limit on the amount of memory a container can allocate, set as a string expressing a byte value. When set, `mem_limit` must be consistent with the `limits.memory` attribute in the Deploy Specification."

- Wartość to napis z jednostką bajtów [D3]: "The supported units are `b` (bytes), `k` or `kb` (kilo bytes), `m` or `mb` (mega bytes) and `g` or `gb` (giga bytes)", np. `300m`. `256m` oznacza 256 MiB = 268 435 456 bajtów.
- Używamy **jednej** formy: `mem_limit` (nie `deploy.resources.limits.memory`), żeby uniknąć niezgodności opisanej w [D1]. [D2] pokazuje równoważne pole `deploy.resources.limits.memory`.
- Minimum demona: "If you set this option, the minimum allowed value is `6m` (6 megabytes)" [D4].
- Pamięć tmpfs liczy się do limitu: "Because tmpfs stores data in memory, that usage counts against the container's memory cgroup limit (`--memory` / Compose `mem_limit`)" [D10]. Zapis na `/tmp` zjada z tych 256 MB.

Semantyka jest opisana w [D1]. Efekt na VPS (czy limit jest egzekwowany w cgroup) rozstrzyga pomiar. Polecenia: `docker inspect <kontener> --format '{{.HostConfig.Memory}}'` (oczekiwane `268435456`) oraz wewnątrz kontenera `cat /sys/fs/cgroup/memory.max` (cgroup v2, oczekiwane `268435456`).

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.3 `memswap_limit`

Cytat [D1]: "`memswap_limit` defines the amount of memory the container is allowed to swap to disk. This is a modifier attribute that only has meaning if `memory` is also set." Reguły [D1] (identyczne w [D4] dla `--memory-swap`):

- "If `memswap_limit` is set to a positive integer, then both `memory` and `memswap_limit` must be set. `memswap_limit` represents the total amount of memory and swap that can be used".
- "If `memswap_limit` is set to the same value as `memory`, and `memory` is set to a positive integer, the container does not have access to swap."
- "If `memswap_limit` is unset, and `memory` is set, the container can use as much swap as the `memory` setting, if the host has swap memory configured." (czyli domyślnie podwójny limit).
- "If `memswap_limit` is set to 0, the setting is ignored" oraz `-1` = swap bez limitu.
- Uwaga [D4]: "Inside the container, tools like `free` report the host's available swap, not what's available inside the container. Don't rely on the output of `free` or similar tools to determine whether swap is present." Pomiar swapu robimy plikami cgroup, nie `free`.

Wymóg spec §13: `memswap_limit` = `mem_limit` (`256m`), czyli brak swapu. Semantyka jest opisana w [D1] i [D4].

**Ryzyko egzekucji (to jest pytanie 9 ze spec):** demon odrzuca limit swapu, gdy jądro nie liczy swapu. W kodzie demona [D11]: gdy brak wsparcia, ostrzeżenie "Your kernel does not support swap limit capabilities or the cgroup is not mounted. Memory limited without swap." i ustawienie `MemorySwap = -1`; dokumentacja [D8] pokazuje to samo ostrzeżenie jako "WARNING: Your kernel does not support swap limit capabilities. Limitation discarded." i opisuje włączenie rachunkowości swapu parametrami jądra `cgroup_enable=memory swapaccount=1` (dotyczy cgroup v1; narzut według [D8]: "about 1% of the total available memory and a 10% overall performance degradation"). Wniosek z tego kodu: **na hoście bez rachunkowości swapu `memswap_limit` może zostać odrzucony (z ostrzeżeniem demona, które przy `docker compose up` łatwo przeoczyć), a kontener może używać swapu**.

Dodatkowo [D12]: użytkownik zgłosił, że przy Engine 27.2.0 i runc 1.1.13 na cgroup v2 `docker inspect` pokazywał `MemorySwap` równe `Memory`, ale plik `memory.swap.max` miał wartość `max` (swap nie był ograniczony). Opiekun Compose odpowiedział, że Compose tylko przekazuje `mem_limit`/`memswap_limit` do `HostConfig.Memory`/`MemorySwap` (tak samo jak `docker run`), a poprawki obsługi swapu na cgroup v2 weszły w runc 1.2.0 (#4188) i 1.2.1 (#4501), więc na nowych wersjach (Engine 29.x, runc 1.3.x) `memory.swap.max` było `0`. Zasada: `docker inspect` pokazuje **zamówioną** konfigurację, nie stan cgroup; o egzekucji rozstrzyga plik cgroup.

Efekt na VPS rozstrzyga pomiar. Polecenia na VPS: `docker info 2>&1 | grep -iE 'cgroup|swap|warning'` (wersja cgroup, ostrzeżenie "No swap limit support"), `docker inspect <kontener> --format '{{.HostConfig.MemorySwap}}'`, wewnątrz kontenera `cat /sys/fs/cgroup/memory.swap.max` (oczekiwane `0`) i `cat /sys/fs/cgroup/memory.swap.current`.

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.4 `mem_reservation`

Cytat [D1]: "`mem_reservation` configures a reservation on the amount of memory a container can allocate, set as a string expressing a byte value. When set, `mem_reservation` must be consistent with the `reservations.memory` attribute in the Deploy Specification."

- To limit miękki: "Allows you to specify a soft limit smaller than `--memory` which is activated when Docker detects contention or low memory on the host machine. If you use `--memory-reservation`, it must be set lower than `--memory` for it to take precedence. Because it is a soft limit, it doesn't guarantee that the container doesn't exceed the limit." [D4]. Spec §13 wymaga "niższe `mem_reservation`" niż `mem_limit`; test Zadania 16 oczekuje `128m`.
- Demon odrzuca rezerwację większą od limitu: "Minimum memory limit can not be less than memory reservation limit, see usage" [D11]; minimum rezerwacji 6 MB [D11].
- Efekt jest "miękki": nie gwarantuje pamięci. Semantyka jest opisana w [D1] i [D4]. Efekt w cgroup (na cgroup v2 oczekiwane odwzorowanie na `memory.low`, **bez potwierdzenia w przeczytanych stronach**) rozstrzyga pomiar. Polecenia: `docker inspect <kontener> --format '{{.HostConfig.MemoryReservation}}'` (oczekiwane `134217728`) i wewnątrz kontenera `cat /sys/fs/cgroup/memory.low`.

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.5 `oom_score_adj`

Cytat [D1]: "`oom_score_adj` tunes the preference for containers to be killed by platform in case of memory starvation. Value must be within -1000,1000 range."

- Kontekst [D4]: "Docker attempts to mitigate these risks by adjusting the OOM priority on the Docker daemon so that it's less likely to be killed than other processes on the system. The OOM priority on containers isn't adjusted." Oraz ostrzeżenie: "You shouldn't try to circumvent these safeguards by manually setting `--oom-score-adj` to an extreme negative number on the daemon or a container". Nasze `+1000` to kierunek przeciwny (kontener ma być zabijany **pierwszy**), więc nie jest tym, przed czym ostrzega dokumentacja; zgodnie ze spec §13 chroni to współdzielony VPS i inne klucze. Skutkiem zabicia procesu bota jest brak spłaszczenia pozycji, ale stop siedzi u brokera, a po restarcie działa uzgadnianie (spec §13).
- Dokumentacja nie opisuje, jak `oom_score_adj` zachowuje się przy OOM **w obrębie cgroup** (osiągnięcie `mem_limit`): wtedy jądro i tak zabija procesy w tej cgroup, a `oom_score_adj` rozstrzyga głównie o wyborze ofiary przy braku pamięci na hoście. Tego dokumentacja Docker nie rozstrzyga.
- Na cgroup v2 flaga `--oom-kill-disable` jest ignorowana ("The `docker run` flag `--oom-kill-disable` is discarded on v2" [D5]); nie używamy jej.

Semantyka jest opisana w [D1]. Efekt (czy wartość `1000` faktycznie trafia do procesu) rozstrzyga pomiar. Polecenia: `docker inspect <kontener> --format '{{.HostConfig.OomScoreAdj}}'` (oczekiwane `1000`) i wewnątrz kontenera `cat /proc/1/oom_score_adj` (oczekiwane `1000`).

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.6 `pids_limit`

Cytat [D1]: "`pids_limit` tunes a container's PIDs limit. Set to -1 for unlimited PIDs. ... When set, `pids_limit` must be consistent with the `pids` attribute in the Deploy Specification." [D2]: "`pids` tunes a container's PIDs limit, set as an integer."

- Przy braku wsparcia w jądrze demon odrzuca limit: "Your kernel does not support PIDs limit capabilities or the cgroup is not mounted. PIDs limit discarded." [D11].
- Wartość `64` jest hojna dla jednoprocesowego silnika asyncio z jednym wątkiem zapisu SQLite i procesem sprawdzającym healthcheck; to wartość początkowa do zmierzenia w Zadaniu 18 (`cat /sys/fs/cgroup/pids.current` w kontenerze), nie ustalenie z dokumentacji.

Semantyka jest opisana w [D1] i [D2]. Efekt (czy limit działa na tym jądrze) rozstrzyga pomiar. Polecenia: `docker inspect <kontener> --format '{{.HostConfig.PidsLimit}}'` (oczekiwane `64`) i wewnątrz kontenera `cat /sys/fs/cgroup/pids.max` (oczekiwane `64`).

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.7 `read_only`

Cytat [D1]: "`read_only` configures the service container to be created with a read-only filesystem." Rozszerzenie w [D14]: "The `--read-only` flag mounts the container's root filesystem as read only prohibiting writes to locations other than the specified volumes for the container." (sekcja "Mount volume read-only").

- Zapisywalne miejsca w v1: wolumen `/data` (SQLite w trybie WAL zapisuje pliki obok bazy, więc cały katalog `/data` musi być zapisywalny), `tmpfs` na `/tmp`. Montowania `config` i sekretu są `:ro`. Kod Python nie powinien zapisywać `__pycache__` poza zapisywalnymi miejscami (ustawić `PYTHONDONTWRITEBYTECODE=1` w obrazie albo skompilować w czasie budowy).

Status: `udokumentowane` (semantyka i efekt są jednoznaczne; test integracyjny: próba zapisu poza `/data` i `/tmp` w uruchomionym kontenerze musi się nie udać).

### 1.8 `tmpfs`

Cytat [D1]: "`tmpfs` mounts a temporary file system inside the container. It can be a single value or a list." Składnia skrócona:

```yaml
tmpfs:
  - /tmp
```

Opcje wymienione w [D1] dla formy skróconej: `mode`, `uid`, `gid`. Dłuższa lista opcji, w tym `size`, jest w [D10] dla `--tmpfs`: "`size` Specifies the size of the tmpfs mount, for example, `size=64m`." oraz `mode`, `noexec`, `nosuid`. Przykład z [D10]: `docker run --tmpfs /data:noexec,size=1024,mode=1777`.

- Zalecenie: ograniczyć rozmiar, bo "If unset, the default maximum size of a tmpfs volume is 50% of the host's total RAM" [D10], a dane na tmpfs liczą się do limitu pamięci kontenera: "Data on a tmpfs mount counts toward the container memory limit. A large `size=` value does not raise that limit." [D10]. Przy `mem_limit` 256 MB sensowny rozmiar `/tmp` to kilkanaście MB.
- Wariant długi (udokumentowany w [D1], sekcja `volumes`): `type: tmpfs`, `target: /tmp`, `tmpfs: { size: <bajty lub jednostka>, mode: <ósemkowo> }` (opcja `mode` od Compose 2.14.0). Wariant krótki z opcjami (`/tmp:size=16m,mode=1777`) jest zgodny z flagą `--tmpfs` z [D10], ale [D1] dla formy Compose wymienia tylko `mode`, `uid`, `gid`: czy Compose przekazuje `size` w formie krótkiej bez zmian, rozstrzyga `docker compose config` i `docker inspect` (`{{.HostConfig.Tmpfs}}`). Plan Zadania 16 sprawdza tekst `tmpfs: /tmp` w pliku; jeżeli test ma pozostać wąski, użyć formy krótkiej z `size`, a rozmiar zweryfikować poleceniem `docker exec <kontener> df -h /tmp`.
- Zawartość tmpfs znika po restarcie kontenera ([D10]); w `/tmp` nie ma nic, czego silnik potrzebuje po restarcie (stan w `/data`).

Semantyka jest opisana w [D1] i [D10]. Rozmiar w formie krótkiej (`size=`) nie jest wymieniony w [D1] dla formy Compose, więc jego respektowanie sprawdza polecenie `docker exec <kontener> df -h /tmp`.

Status pozycji: `do potwierdzenia w dry_run lub w odczytach live`.

### 1.9 `healthcheck`

Cytat [D1]: "The `healthcheck` attribute declares a check that's run to determine whether or not the service containers are 'healthy'. It works in the same way, and has the same default values, as the HEALTHCHECK Dockerfile instruction set by the service's Docker image."

Pola [D1]:

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost"]
  interval: 1m30s
  timeout: 10s
  retries: 3
  start_period: 40s
  start_interval: 5s
```

- `test`: lista (pierwszy element `NONE`, `CMD` lub `CMD-SHELL`) albo napis (równoważny `CMD-SHELL`). "`NONE` disables the healthcheck"; alternatywnie `disable: true`.
- Czasy to czasy trwania ([D3]); `start_interval` od Compose 2.20.2 [D1], a po stronie silnika "requires Docker Engine version 25.0 or later" [D7].
- Domyślne wartości [D7]: `interval` 30s, `timeout` 30s, `start_period` 0s, `start_interval` 5s, `retries` 3. Kod wyjścia polecenia: `0` zdrowy, `1` niezdrowy, `2` zarezerwowany, nie używać [D7]. Przy przekroczeniu `timeout` polecenie jest przerywane sygnałem SIGKILL [D7]. Wyjście (do 4096 bajtów) zapisuje się w stanie zdrowia i jest dostępne w `docker inspect` [D7].
- Status zdrowia to `starting`, `healthy`, `unhealthy` [D7]. **Dokumentacja Compose ani [D6] nie mówią, że `unhealthy` powoduje restart kontenera.** Polityka `restart` reaguje na wyjście procesu, nie na zdrowie [D6]. Healthcheck służy więc do obserwacji (`docker ps`, alarm), a nie do samonaprawy; za zakończenie nieudanej sesji odpowiada sam proces (watchdog, spec §13).
- Dla silnika: polecenie sprawdzające świeżość pliku tętna (np. `["CMD", "python", "-m", "gwstock.healthcheck"]`, kod 1, gdy plik starszy niż próg). Samo uruchomienie interpretera Pythona co 30 s kosztuje chwilową pamięć, więc `interval` nie mniejszy niż 30s, a pamięć szczytowa polecenia liczy się do limitu kontenera.

Status: `udokumentowane`.

### 1.10 `restart`

Cytat [D1]: "`restart` defines the policy that the platform applies on container termination." Wartości [D1]: `no` (domyślna), `always`, `on-failure[:max-retries]`, `unless-stopped` ("restarts the container irrespective of the exit code but stops restarting when the service is stopped or removed").

Szczegóły [D6]:

- "A restart policy only takes effect after a container starts successfully. In this case, starting successfully means that the container is up for at least 10 seconds and Docker has started monitoring it. This prevents a container which doesn't start at all from going into a restart loop."
- "If you manually stop a container, the restart policy is ignored until the Docker daemon restarts or the container is manually restarted."
- `unless-stopped`: "Similar to `always`, except that when the container is stopped (manually or otherwise), it isn't restarted even after Docker daemon restarts."
- Odstęp między kolejnymi restartami (rosnące opóźnienie) nie jest opisany w przeczytanych stronach. Dla silnika to ważne przy limicie logowania `POST /session` (1/s, `capitalcom-api-notes.md` sekcja 1.2): proces po starcie sam serializuje logowania, a runbook opisuje restart "zatrzymaj, potem uruchom" (spec §13).

Wartość v1: `restart: unless-stopped` (test Zadania 16). Status: `udokumentowane`.

### 1.11 Pola dodatkowe, przydatne w Zadaniu 16 (opcjonalne)

Wszystkie `udokumentowane` w [D1] (semantyka), wartości do decyzji Zadania 16:

| Pole | Cytat [D1] | Zastosowanie |
|---|---|---|
| `user` | "`user` overrides the user used to run the container process. The default is set by the image ... If it's not set, then `root`." | Użytkownik bez uprawnień (spec §13) |
| `init` | "`init` runs an init process (PID 1) inside the container that forwards signals and reaps processes." | Poprawna obsługa SIGTERM i zombie |
| `stop_grace_period` | "how long Compose must wait when attempting to stop a container ... before sending SIGKILL"; domyślnie 10 s | Czas na czyste zamknięcie połączeń i zapis tętna |
| `stop_signal` | "If unset containers are stopped by Compose by sending `SIGTERM`." | Domyślny SIGTERM wystarcza |
| `cap_drop` | "`cap_drop` specifies container capabilities to drop" | `ALL` |
| `security_opt` | `no-new-privileges` (składnia `no-new-privileges`, `=true` lub `:true`) | Brak podnoszenia uprawnień |
| `logging` | sterownik i opcje logów | Rotacja logów, by dysk współdzielonego VPS się nie zapełnił |

## 2. Zachowanie przy cgroup v2

Cytaty [D5] (sekcja "Running Docker on cgroup v2"):

- "Docker supports cgroup v2 since Docker 20.10. Running Docker on cgroup v2 also requires the following conditions to be satisfied: containerd: v1.4 or later; runc: v1.0.0-rc91 or later; Kernel: v4.15 or later (v5.2 or later is recommended)".
- "The default cgroup driver (`dockerd --exec-opt native.cgroupdriver`) is `systemd` on v2, `cgroupfs` on v1."
- "The default cgroup namespace mode (`docker run --cgroupns`) is `private` on v2, `host` on v1." Wniosek (nie wprost z dokumentacji): wewnątrz kontenera na cgroup v2 pliki limitów powinny leżeć pod `/sys/fs/cgroup/` (własna przestrzeń nazw cgroup), co upraszcza odczyty z sekcji 4.
- "The `docker run` flag `--oom-kill-disable` is discarded on v2."

Status powyższych: `udokumentowane`.

Wnioski dla pól pamięci:

1. `mem_limit`, `mem_reservation`, `memswap_limit`, `pids_limit`, `oom_score_adj` są w Compose zwykłymi polami przekazywanymi do silnika [D1, D12]; Compose nie ma własnej logiki cgroup.
2. Czy limit zadziała, zależy od hosta (jądro, rachunkowość swapu, wersje Engine i runc), co pokazują [D8], [D11], [D12]. Dokumentacja Docker **nie zawiera** zdania o tym, że na cgroup v2 swap zawsze jest rozliczany; nie wolno tego zakładać. Wersja cgroup i runc na VPS nie jest znana.
3. `oom_score_adj` dotyczy hosta (wybór ofiary przy braku pamięci), nie zastępuje `mem_limit`.

Status: do potwierdzenia w dry_run lub w odczytach live (cała sekcja 2 dotyczy efektu na VPS). Rozstrzyga ją sekcja 4.

## 3. Wersja Compose

Spec §13 odnosi się do "Compose v5.6.0". Wydanie v5.6.0 istnieje i ma datę 2026-10-02 [D13]; wśród zmian jest "Feat(compose): warn on unsupported compose-file attributes" (#14196), czyli nowsza wersja ostrzega o nieobsługiwanych atrybutach. Strona [D1] jest opisem aktualnej Compose Specification i nie podaje wersji dla każdego pola; jawnie wersje podano tylko dla `start_interval` (Compose 2.20.2) i `tmpfs.mode` w formie długiej (2.14.0). Notatki wydania v5.6.0 nie wspominają o zmianach w `mem_limit`, `memswap_limit`, `mem_reservation`, `oom_score_adj`, `pids_limit`. Wersja Compose zainstalowana na VPS: do potwierdzenia w dry_run lub w odczytach live (`docker compose version`).

## 4. Walidacja na VPS (polecenia)

Zasada ze spec §13: na współdzielonym VPS nic nie jest uruchamiane bez pomiaru pamięci i zgody właściciela. Polecenia z punktów 1 i 2 niczego nie uruchamiają; reszta wymaga zgody.

1. **Wersje i host (tylko odczyt):**
   - `docker compose version`
   - `docker version` i `docker info 2>&1 | grep -iE 'cgroup|runc|swap|warning'` (wersja cgroup, sterownik, ostrzeżenie "No swap limit support" [D4])
   - `stat -fc %T /sys/fs/cgroup` (wynik `cgroup2fs` oznacza cgroup v2; polecenie z wiedzy ogólnej o Linuksie, nie z przeczytanej dokumentacji Docker)
   - `cat /proc/swaps` (czy host w ogóle ma swap)

2. **Walidacja pliku Compose bez uruchamiania [D9]:**
   - `docker compose -f docker/compose.yml config -q`: "Only validate the configuration, don't print anything" (kod wyjścia różny od zera przy błędzie)
   - `docker compose -f docker/compose.yml config`: renderuje model w postaci kanonicznej ("Parse, resolve and render compose file in canonical format"); sprawdzić, że w usłudze są `mem_limit`, `memswap_limit`, `mem_reservation`, `oom_score_adj`, `pids_limit`, `read_only`, `tmpfs`, `healthcheck`, `restart`. Opcja `--format json` daje wynik do porównania skryptem. Wartości mogą być wypisane jako liczby bajtów, nie jako `256m`.
   - Czy `config` zgłosi nieznany atrybut: nowsze wydania Compose ostrzegają o nieobsługiwanych atrybutach [D13]; stare mogą go po cichu zignorować, więc po `up` obowiązkowo wykonać odczyty z pkt 3 w sekcji 4.

3. **Po uruchomieniu (po zgodzie właściciela), kontener `gw-stock-engine`:**

   | Co sprawdzić | Polecenie | Oczekiwane |
   |---|---|---|
   | zamówiona konfiguracja | `docker inspect gw-stock-engine --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.MemoryReservation}} {{.HostConfig.OomScoreAdj}} {{.HostConfig.PidsLimit}} {{.HostConfig.ReadonlyRootfs}}'` | `268435456 268435456 134217728 1000 64 true` |
   | limit pamięci w cgroup | `docker exec gw-stock-engine cat /sys/fs/cgroup/memory.max` | `268435456` |
   | swap w cgroup | `docker exec gw-stock-engine cat /sys/fs/cgroup/memory.swap.max` | `0` (inaczej `memswap_limit` nie działa) |
   | użyty swap | `docker exec gw-stock-engine cat /sys/fs/cgroup/memory.swap.current` | `0` |
   | rezerwacja | `docker exec gw-stock-engine cat /sys/fs/cgroup/memory.low` | `134217728` |
   | limit procesów | `docker exec gw-stock-engine cat /sys/fs/cgroup/pids.max` | `64` |
   | `oom_score_adj` procesu | `docker exec gw-stock-engine cat /proc/1/oom_score_adj` | `1000` |
   | tylko do odczytu | `docker exec gw-stock-engine sh -c 'touch /app/x'` | odmowa; zapis w `/data` i `/tmp` działa |
   | rozmiar tmpfs | `docker exec gw-stock-engine df -h /tmp` | rozmiar zgodny z konfiguracją |
   | zdrowie | `docker inspect gw-stock-engine --format '{{.State.Health.Status}}'` | `healthy` |
   | zużycie pamięci | `docker stats --no-stream gw-stock-engine` | pole "MEM USAGE / LIMIT" [D5] |

   Polecenia `docker inspect --format` i pliki `memory.*`, `pids.max` są wiedzą ogólną o Docker i cgroup v2, nie cytatem z przeczytanych stron; dlatego ich wynik jest właśnie tym, co ma potwierdzić Zadanie 18. Jeśli `memory.swap.max` nie jest `0`, nie wdrażać; wtedy sprawdzić wersję runc (poprawki w 1.2.1 wg [D12]) i rachunkowość swapu w jądrze (parametry z [D8] dotyczą cgroup v1), a decyzję podjąć z właścicielem (spec pyt. 9 i 16).

## 5. Szkic fragmentu `docker/compose.yml` (do Zadania 16, wartości początkowe)

To szkic z samych pól opisanych powyżej; wartości `mem_reservation`, `pids_limit`, `stop_grace_period` i rozmiar tmpfs są wyjściowe i podlegają pomiarowi. Montowanie `./data:/data` i tylko-do-odczytu klucza jest zgodne z planem (Zadanie 16) i spec §13; montowanie `./config:/app/config:ro` jest propozycją do zatwierdzenia w Zadaniu 16.

```yaml
services:
  gw-stock-engine:
    image: gw-stock-engine:<tag>
    user: "10001:10001"
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777   # rozmiar w formie skróconej: do sprawdzenia (pkt 1.8)
    volumes:
      - ./config:/app/config:ro
      - ./data:/data
      - <ścieżka-na-VPS>/capitalcom.json:/secrets/capitalcom.json:ro
    mem_limit: 256m
    memswap_limit: 256m
    mem_reservation: 128m
    oom_score_adj: 1000
    pids_limit: 64
    restart: unless-stopped
    init: true
    stop_grace_period: 30s
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    healthcheck:
      test: ["CMD", "python", "-m", "gwstock.healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

Brak sekcji `ports` (silnik nie wystawia nic na zewnątrz; test Zadania 16). Nazwa modułu `gwstock.healthcheck` jest propozycją: w planie nie ma jeszcze takiego modułu, jest tętno w pliku (Zadanie 14); Zadanie 16 musi dodać polecenie sprawdzające jego świeżość.

## 6. Co zostaje do ustalenia

| Pozycja | Status | Gdzie rozstrzygnąć |
|---|---|---|
| Czy `memswap_limit` = `mem_limit` realnie wyłącza swap na VPS (`memory.swap.max` = `0`) | do potwierdzenia w dry_run lub w odczytach live | Zadanie 18, sekcja 4 pkt 3 |
| Wersja cgroup, runc i Engine na VPS | do potwierdzenia w dry_run lub w odczytach live | Sekcja 4 pkt 1 |
| Czy `mem_reservation` trafia do `memory.low` | do potwierdzenia w dry_run lub w odczytach live | Sekcja 4 pkt 3 |
| Czy rozmiar tmpfs w formie krótkiej jest respektowany | do potwierdzenia w dry_run lub w odczytach live | `df -h /tmp` |
| Pamięć szczytowa procesu pod limitem (cel ok. 100 MB to założenie) | do potwierdzenia w dry_run lub w odczytach live | `docker stats` w `dry_run`, Zadanie 18 |
| Odstęp między restartami `unless-stopped` | do potwierdzenia w dry_run lub w odczytach live | Obserwacja w teście awarii; nie opisany w przeczytanych stronach |
| Wartości `pids_limit`, `mem_reservation`, `stop_grace_period` | do potwierdzenia w dry_run lub w odczytach live | Pomiar w `dry_run` |
