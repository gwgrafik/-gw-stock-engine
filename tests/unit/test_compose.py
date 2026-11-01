"""Testy kontraktu wdrożenia: `docker/compose.yml`, `docker/Dockerfile`, `.dockerignore` i runbook (spec §13).

Uruchamiać z korzenia repozytorium (ścieżki względne). Testy czytają pliki jako tekst (PyYAML), bez demona Dockera.
Dwa testy dodatkowo wywołują `docker compose config` (sam interfejs CLI, bez demona), o ile wtyczka Compose jest
dostępna; w obrazie testowym (bez Dockera) są pomijane.

Czego pilnują: limity pamięci i izolacja kontenera (pamięć 256 MB bez swapu, system plików tylko do odczytu, brak
portów, wszystkie uprawnienia odebrane, użytkownik z GW_UID/GW_GID), klucz tylko do odczytu i bez wartości sekretów
w środowisku, Dockerfile bez `latest` i bez kopiowania całego repozytorium do obrazu produkcyjnego, oraz zgodność
runbooka dla właściciela z tymi ustawieniami.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(".")
COMPOSE = ROOT / "docker" / "compose.yml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
RUNBOOK = ROOT / "docs" / "ops" / "runbook.md"

SERVICE = "gw-stock-engine"
TEST_SERVICE = "gw-stock-test"
LIVE_SERVICE = "gw-stock-live-readonly"
GITIGNORE = ROOT / ".gitignore"
SOURCES = ROOT / "src" / "gwstock"
SECRET_MOUNT_TARGET = "/secrets/capitalcom.json"
DEFAULT_SECRET_SOURCE = "${GW_SECRET_FILE:-/home/claude/.config/gw/capitalcom.json}"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _svc(name: str = SERVICE) -> dict:
    return _compose()["services"][name]


# --- plan (Zadanie 16, krok 1) ---------------------------------------------------------------------------------------


def test_compose_enforces_memory_and_isolation() -> None:
    svc = _svc()
    assert svc["mem_limit"] == "256m" and svc["memswap_limit"] == "256m"
    assert svc["mem_reservation"] == "128m" and svc["oom_score_adj"] == 1000
    assert svc["read_only"] is True and "pids_limit" in svc
    assert any(SECRET_MOUNT_TARGET in v and v.endswith(":ro") for v in svc["volumes"])
    assert "ports" not in svc and svc.get("restart") == "unless-stopped"
    assert svc["healthcheck"]["test"]  # świeżość pliku tętna


# --- limity i izolacja --------------------------------------------------------------------------------------------------


def test_memory_limits_are_consistent_and_swap_is_off() -> None:
    svc = _svc()
    # memswap_limit == mem_limit oznacza brak swapu (Compose: [D1]); rezerwacja musi być niższa od limitu
    assert svc["memswap_limit"] == svc["mem_limit"]
    assert int(svc["mem_reservation"].removesuffix("m")) < int(svc["mem_limit"].removesuffix("m"))


def test_process_and_cpu_limits_are_bounded() -> None:
    svc = _svc()
    assert isinstance(svc["pids_limit"], int) and 8 <= svc["pids_limit"] <= 128
    assert 0 < float(svc["cpus"]) <= 1  # współdzielony VPS: silnik jednowątkowy, nie potrzebuje więcej


def test_all_capabilities_dropped_and_no_new_privileges() -> None:
    svc = _svc()
    assert svc["cap_drop"] == ["ALL"]
    assert "cap_add" not in svc
    assert "no-new-privileges:true" in svc["security_opt"]
    assert not svc.get("privileged", False)


def test_no_host_namespaces_devices_or_published_ports() -> None:
    svc = _svc()
    for forbidden in ("ports", "expose", "devices", "pid", "ipc", "uts", "userns_mode", "extra_hosts", "dns"):
        assert forbidden not in svc, forbidden
    assert svc.get("network_mode") not in ("host",) and "network_mode" not in svc


def test_user_comes_from_gw_uid_and_gw_gid_without_a_default() -> None:
    user = _svc()["user"]
    m = re.fullmatch(r"\$\{GW_UID:\?[^}]+\}:\$\{GW_GID:\?[^}]+\}", user)
    assert m, user  # `:?` = błąd, gdy zmienna nieustawiona: brak cichego powrotu do roota
    assert ":-" not in user


def test_root_filesystem_read_only_with_tmp_as_tmpfs() -> None:
    svc = _svc()
    assert svc["read_only"] is True
    # rozmiar w formie skróconej potwierdzony na Compose 5.6.1 + Docker 29.9.2 (`df -h /tmp`, zapis ponad limit = ENOSPC)
    assert svc["tmpfs"] == ["/tmp:size=32m"]
    assert svc["init"] is True


def test_restart_stop_and_logging_policy() -> None:
    svc = _svc()
    assert svc["restart"] == "unless-stopped"
    assert svc["stop_grace_period"] == "30s"
    logging = svc["logging"]
    assert logging["driver"] == "json-file"
    assert logging["options"] == {"max-size": "10m", "max-file": "3"}


def test_healthcheck_runs_gwstock_health() -> None:
    hc = _svc()["healthcheck"]
    assert hc["test"] == ["CMD", "python", "-m", "gwstock", "health"]
    assert (hc["interval"], hc["timeout"], hc["retries"], hc["start_period"]) == ("60s", "15s", 3, "120s")


# --- wolumeny, sekrety, środowisko -------------------------------------------------------------------------------------


def test_volumes_are_exactly_data_config_ro_and_secret_ro() -> None:
    # Ścieżki względne liczą się od katalogu pliku Compose (docker/), stąd `..`: działa z dowolnego katalogu roboczego.
    assert sorted(_svc()["volumes"]) == sorted(
        [
            "../data:/data",
            "../config:/app/config:ro",
            f"{DEFAULT_SECRET_SOURCE}:{SECRET_MOUNT_TARGET}:ro",
        ]
    )


def test_data_is_the_only_writable_mount() -> None:
    writable = [v for v in _svc()["volumes"] if not v.endswith(":ro")]
    assert writable == ["../data:/data"]


def test_no_absolute_host_path_other_than_the_documented_secret_default() -> None:
    for v in _svc()["volumes"]:
        source = v.split(":", 1)[0]
        assert not source.startswith("/") or v.startswith(DEFAULT_SECRET_SOURCE), v
    assert "/var/run/docker.sock" not in COMPOSE.read_text(encoding="utf-8")


def test_environment_holds_paths_only_and_no_secret_values() -> None:
    env = _svc()["environment"]
    assert env == {
        "GW_CONFIG_DIR": "/app/config",
        "GW_DATA_DIR": "/data",
        "GW_CAPITALCOM_KEY_FILE": SECRET_MOUNT_TARGET,
    }
    for name, value in env.items():
        assert str(value).startswith("/"), name  # tylko ścieżki
    forbidden = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY|IDENTIFIER|ACCOUNT", re.I)
    assert not [n for n in env if forbidden.search(n)]


def test_compose_text_contains_no_secret_looking_literals() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "env_file" not in text and "secrets:" not in text.replace("/secrets/", "")
    assert not re.search(r"(?i)(api_password|api_key|identifier)\s*[:=]\s*\S", text)


def test_image_and_build_target() -> None:
    svc = _svc()
    assert svc["image"] == "gw-stock-engine:local"
    assert svc["container_name"] == SERVICE  # stała nazwa: jedna instancja, stałe `docker logs` i `docker stats`
    build = svc["build"]
    assert build["context"] == ".." and build["dockerfile"] == "docker/Dockerfile" and build["target"] == "runtime"
    assert "profiles" not in svc  # `docker compose up -d` uruchamia silnik, nie usługę testową


def _assert_same_hardening(svc: dict) -> None:
    assert svc["profiles"] == ["test"]
    assert svc["build"]["target"] == "test" and svc["image"] == "gw-stock-engine:test"
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"] and "cap_add" not in svc
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["memswap_limit"] == svc["mem_limit"] and svc["oom_score_adj"] == 1000
    assert "pids_limit" in svc and "ports" not in svc and svc["restart"] == "no"
    assert re.fullmatch(r"\$\{GW_UID:\?[^}]+\}:\$\{GW_GID:\?[^}]+\}", svc["user"])
    assert svc["healthcheck"] == {"disable": True}
    assert svc["tmpfs"] == ["/tmp:size=128m"]
    assert not svc.get("privileged", False)


def test_unit_test_service_has_no_key_and_no_network() -> None:
    """Testy logiki: bez klucza (najmniejsze uprawnienia) i bez sieci (loopback wystarcza serwerom atrap)."""
    svc = _svc(TEST_SERVICE)
    _assert_same_hardening(svc)
    assert svc["network_mode"] == "none"
    assert "volumes" not in svc  # ani klucza, ani danych silnika
    assert "environment" not in svc or not any("KEY" in k or "SECRET" in k for k in svc["environment"])
    assert "command" not in svc  # domyślne CMD obrazu: pytest bez testów live


def test_live_readonly_service_has_the_key_read_only_and_runs_only_live_tests() -> None:
    svc = _svc(LIVE_SERVICE)
    _assert_same_hardening(svc)
    assert "network_mode" not in svc  # łączy się z Capital.com
    assert sorted(svc["volumes"]) == sorted(
        ["../data:/data:ro", f"{DEFAULT_SECRET_SOURCE}:{SECRET_MOUNT_TARGET}:ro"]
    )  # dane tylko do odczytu; testy nigdy nie piszą do danych silnika
    assert svc["environment"] == {"GW_DATA_DIR": "/data", "GW_CAPITALCOM_KEY_FILE": SECRET_MOUNT_TARGET}
    cmd = svc["command"]
    assert cmd[:3] == ["python", "-m", "pytest"] and cmd[cmd.index("-m", 3) + 1] == "integration_live_readonly"
    assert "not integration_live_readonly" not in " ".join(cmd)


def test_only_the_live_readonly_and_engine_services_mount_the_key() -> None:
    mounting = [
        name for name, svc in _compose()["services"].items()
        if any(SECRET_MOUNT_TARGET in v for v in svc.get("volumes", []))
    ]  # fmt: skip
    assert sorted(mounting) == sorted([SERVICE, LIVE_SERVICE])


# --- Dockerfile ---------------------------------------------------------------------------------------------------------


def _dockerfile_stages() -> list[dict]:
    """[{name, from_ref, instructions: [(INSTR, args)]}] w kolejności występowania (kontynuacje `\\` sklejone)."""
    logical: list[str] = []
    buf = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if buf and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        logical.append((buf + line).strip())
        buf = ""
    stages: list[dict] = []
    for entry in logical:
        instr, _, args = entry.partition(" ")
        instr = instr.upper()
        if instr == "FROM":
            m = re.fullmatch(r"(\S+)\s+AS\s+(\S+)", args.strip(), re.I)
            assert m, f"FROM bez nazwy etapu: {entry}"
            stages.append({"name": m.group(2).lower(), "from_ref": m.group(1), "instructions": []})
        else:
            if not stages:  # ARG przed pierwszym FROM
                continue
            stages[-1]["instructions"].append((instr, args.strip()))
    return stages


def _stage(name: str) -> dict:
    return next(s for s in _dockerfile_stages() if s["name"] == name)


def _stage_instructions(name: str, instr: str) -> list[str]:
    return [a for i, a in _stage(name)["instructions"] if i == instr]


def test_dockerfile_stage_order_and_default_target_is_runtime() -> None:
    names = [s["name"] for s in _dockerfile_stages()]
    assert names == ["base", "test", "runtime"]  # ostatni etap = domyślny cel `docker build`
    assert names[-1] == "runtime"


def test_base_image_is_python_312_slim_pinned_by_tag_and_digest() -> None:
    stages = _dockerfile_stages()
    base = stages[0]
    assert base["name"] == "base"
    assert re.fullmatch(r"python:3\.12\.\d+-slim@sha256:[0-9a-f]{64}", base["from_ref"]), base["from_ref"]
    for later in stages[1:]:
        assert later["from_ref"] == "base"  # jedna baza dla obu celów


def test_dockerfile_never_uses_latest_curl_or_secrets() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "latest" not in text.lower()
    assert not re.search(r"\b(curl|wget)\b", text)
    for instr in ("ARG", "ENV"):
        for args in (a for s in _dockerfile_stages() for i, a in s["instructions"] if i == instr):
            assert not re.search(r"(?i)password|secret|token|api_key|identifier", args), args


def test_base_environment_is_set_for_read_only_operation() -> None:
    env = " ".join(_stage_instructions("base", "ENV"))
    for needle in ("PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "HOME=/tmp", "PIP_NO_CACHE_DIR=1"):
        assert needle in env, needle
    assert _stage_instructions("base", "WORKDIR") == ["/app"]


def test_runtime_stage_installs_only_the_runtime_lock_and_src() -> None:
    instr = _stage("runtime")["instructions"]
    copies = [a for i, a in instr if i == "COPY"]
    joined = "\n".join(f"{i} {a}" for i, a in instr)
    assert "requirements.lock" in joined and "-r requirements.lock" in joined
    assert "requirements-dev.lock" not in joined
    assert any(re.search(r"\bsrc/?\s+/app/src/?$", c) for c in copies), copies
    for c in copies:
        assert not re.search(r"\sCOPY\s", c)
        # nigdy cały kontekst: `COPY . .`, `COPY ./ ...`, `COPY * ...`
        sources = [t for t in c.split() if not t.startswith("--")][:-1]
        assert not any(t in (".", "./", "*", "/") for t in sources), c
        assert not any(t.startswith(("tests", "config", "docs", "data", ".git")) for t in sources), c
    # runtime nie dostaje konfiguracji ani testów: konfigurację montuje Compose (tylko do odczytu)
    assert not any(t in joined for t in ("COPY tests", "COPY config", "COPY docs", "COPY docker"))


def test_runtime_stage_runs_as_non_root_and_starts_the_engine() -> None:
    users = _stage_instructions("runtime", "USER")
    assert users, "brak USER w etapie runtime"
    user = users[-1]
    assert user not in ("root", "0", "0:0") and re.fullmatch(r"\d+(:\d+)?", user), user
    assert int(user.split(":")[0]) >= 1000
    cmds = _stage_instructions("runtime", "CMD")
    assert cmds == ['["python", "-m", "gwstock"]'] or cmds == ['["python","-m","gwstock"]']
    assert not _stage_instructions("runtime", "ENTRYPOINT")


def test_runtime_verifies_timezone_database_and_package_import_at_build_time() -> None:
    run = "\n".join(_stage_instructions("runtime", "RUN") + _stage_instructions("base", "RUN"))
    assert "ZoneInfo" in run and "Europe/Berlin" in run  # kalendarz XETRA buduje strefę przy imporcie
    assert "gwstock" in run


def test_test_stage_has_dev_lock_tests_and_config_and_runs_pytest() -> None:
    instr = _stage("test")["instructions"]
    joined = "\n".join(f"{i} {a}" for i, a in instr)
    for needle in ("requirements-dev.lock", "tests", "config", "pyproject.toml", "docker"):
        assert needle in joined, needle
    cmd = _stage_instructions("test", "CMD")
    assert len(cmd) == 1
    assert "pytest" in cmd[0] and "not integration_live_readonly" in cmd[0]
    users = _stage_instructions("test", "USER")
    assert users and users[-1] not in ("root", "0")


# --- .dockerignore ------------------------------------------------------------------------------------------------------


def _ignore_patterns() -> list[str]:
    return [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_dockerignore_excludes_history_data_secrets_and_working_notes() -> None:
    patterns = _ignore_patterns()
    for needed in (".git", "data", "data*/", ".superpowers", ".env", "**/.env", "**/__pycache__", "*.sqlite*", "docs"):
        assert needed in patterns, needed
    assert any("capitalcom" in p or p.endswith(".json") for p in patterns)


def test_dockerignore_does_not_hide_what_the_build_needs() -> None:
    patterns = _ignore_patterns()
    for needed in ("src", "tests", "config", "docker", "requirements.lock", "requirements-dev.lock", "pyproject.toml"):
        assert needed not in patterns and f"{needed}/" not in patterns, needed


def test_gitignore_keeps_data_backups_and_the_uid_env_file_out_of_the_repo() -> None:
    """Kopie `data*` (np. data.kopia-...) i `docker/.env` nie mogą trafić do publicznego repozytorium."""
    if not GITIGNORE.exists():
        pytest.skip(".gitignore nie jest w obrazie testowym")
    patterns = [ln.strip() for ln in GITIGNORE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert "data/" in patterns and "data*/" in patterns
    assert ".env" in patterns  # wzorzec bez ukośnika dopasowuje `.env` na każdej głębokości, także docker/.env
    if shutil.which("git") and Path(".git").exists():
        for path in ("docker/.env", "data.kopia-2026-11-02/journal.sqlite", "data/journal.sqlite"):
            res = subprocess.run(["git", "check-ignore", "-q", path], capture_output=True, check=False)
            assert res.returncode == 0, f"git nie ignoruje {path}"


# --- docker compose config (tylko CLI, bez demona) --------------------------------------------------------------------


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


needs_compose_cli = pytest.mark.skipif(
    not _docker_compose_available(), reason="brak wtyczki `docker compose` (np. w obrazie testowym)"
)


def _compose_config(env_extra: dict[str, str | None], *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GW_UID", "GW_GID", "GW_SECRET_FILE")}
    env["DOCKER_HOST"] = "unix:///nonexistent-gw-test.sock"  # `config` nie może zależeć od demona
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args, "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


@needs_compose_cli
def test_compose_config_renders_with_uid_and_gid_and_resolves_paths() -> None:
    res = _compose_config({"GW_UID": "1234", "GW_GID": "5678"})
    assert res.returncode == 0, res.stderr
    svc = json.loads(res.stdout)["services"][SERVICE]
    assert svc["user"] == "1234:5678"
    assert svc["mem_limit"] == str(256 * 1024 * 1024) or svc["mem_limit"] == 256 * 1024 * 1024
    assert int(svc["memswap_limit"]) == 256 * 1024 * 1024 and int(svc["mem_reservation"]) == 128 * 1024 * 1024
    assert svc["read_only"] is True and svc["oom_score_adj"] == 1000
    mounts = {v["target"]: v for v in svc["volumes"]}
    repo = Path.cwd().resolve()
    assert mounts["/data"]["source"] == str(repo / "data") and not mounts["/data"].get("read_only")
    assert mounts["/app/config"]["source"] == str(repo / "config") and mounts["/app/config"]["read_only"] is True
    secret = mounts[SECRET_MOUNT_TARGET]
    assert secret["source"] == "/home/claude/.config/gw/capitalcom.json" and secret["read_only"] is True
    assert svc["build"]["context"] == str(repo) and svc["build"]["dockerfile"] == "docker/Dockerfile"
    assert "ports" not in svc


@needs_compose_cli
def test_compose_config_accepts_a_secret_path_override() -> None:
    res = _compose_config({"GW_UID": "1000", "GW_GID": "1000", "GW_SECRET_FILE": "/srv/x/key.json"})
    assert res.returncode == 0, res.stderr
    mounts = {v["target"]: v for v in json.loads(res.stdout)["services"][SERVICE]["volumes"]}
    assert mounts[SECRET_MOUNT_TARGET]["source"] == "/srv/x/key.json"


@needs_compose_cli
@pytest.mark.parametrize("missing", ["GW_UID", "GW_GID"])
def test_compose_config_refuses_to_render_without_uid_or_gid(missing: str) -> None:
    env: dict[str, str | None] = {"GW_UID": "1000", "GW_GID": "1000", missing: None}
    res = _compose_config(env)
    assert res.returncode != 0
    assert missing in res.stderr  # komunikat nazywa brakującą zmienną


# --- runbook ------------------------------------------------------------------------------------------------------------

needs_runbook = pytest.mark.skipif(not RUNBOOK.exists(), reason="docs/ nie ma w obrazie testowym")


def _runbook_blocks() -> list[tuple[str, str]]:
    text = RUNBOOK.read_text(encoding="utf-8")
    return re.findall(r"^```([a-z]*)\n(.*?)^```$", text, flags=re.S | re.M)


@needs_runbook
def test_runbook_commands_are_copy_paste_blocks() -> None:
    blocks = _runbook_blocks()
    assert len(blocks) >= 30
    for lang, body in blocks:
        assert lang == "bash", f"blok bez języka bash: {body[:60]!r}"
        assert body.strip(), "pusty blok"
        for line in body.splitlines():
            assert not line.startswith("$ "), f"znak zachęty w poleceniu: {line!r}"
            assert not line.startswith(">"), line


@needs_runbook
def test_runbook_contains_no_secrets_account_ids_or_emails() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "adres e-mail w runbooku"
    # wartość w cudzysłowie po nazwie pola JSON: `"api_key": "..."` (pytania `read -p "api_key: "` to nie wartości)
    assert not re.search(r"(?i)\"(api_password|api_key|identifier)\"\s*:\s*\"[^\"]+\"", text)
    assert not re.search(r"\b[0-9a-f]{32,}\b", text), "długi ciąg szesnastkowy (klucz?)"
    known_limits = ("268435456", "134217728")  # 256 MiB i 128 MiB w bajtach: oczekiwane wartości limitów pamięci
    scanned = text
    for number in known_limits:
        scanned = scanned.replace(number, "")
    assert not re.search(r"\b\d{9,}\b", scanned), "długa liczba (numer rachunku?)"


@needs_runbook
def test_runbook_uses_exact_operator_commands_and_safe_restart() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "clear-kill --by",
        "reset-position --by",
        "resolve-lost-trade --by",
        "python -m gwstock bind-account",
        "python -m gwstock status",
        "python -m gwstock.prereg --write",
        "python -m gwstock.report",
        "touch data/STOP",
        "touch data/HARD_STOP",
        "data/ARM_LIVE",
        "data/L1_PASSED",
        "docker compose -f docker/compose.yml stop",
        "docker stats --no-stream",
        "memory.swap.max",
        "CANDLE_STAMP_IS_PERIOD_START",
        "-gw-stock-engine.git",
        "/home/claude/gw-stock-engine",
        "/home/claude/.config/gw/capitalcom.json",
        "GW_UID",
    ):
        assert needle in text, needle
    # nigdy nakładania instancji: nie ma polecenia `up` bez poprzedzającego stop w procedurze restartu
    assert "docker compose -f docker/compose.yml restart" not in text
    assert "docker compose -f docker/compose.yml down" not in text.replace("nigdy `down`", "")


@needs_runbook
def test_runbook_compose_commands_always_name_the_compose_file() -> None:
    for _, body in _runbook_blocks():
        for line in body.splitlines():
            if re.match(r"^\s*docker compose\b", line):
                assert "-f docker/compose.yml" in line, line


@needs_runbook
def test_runbook_never_prints_the_secret_file() -> None:
    for _, body in _runbook_blocks():
        for line in body.splitlines():
            if "capitalcom.json" in line:
                assert not re.match(r"^\s*(cat|less|more|head|tail|nano|vi|vim|echo)\b", line), line


# --- runbook: zasady dla właściciela, który nie jest programistą (fix round 1) --------------------------------------

_RUNNABLE_WORDS = (
    "docker", "touch", "rm", "sed", "git", "tail", "grep", "cat", "mkdir", "cp", "chown", "chmod", "export", "free",
    "df", "stat", "curl", "su", "install", "ls", "cd", "python", "printf", "echo", "umask", "timedatectl", "getenforce",
)  # fmt: skip


def _runbook_prose() -> str:
    """Tekst runbooka bez zakwalifikowanych bloków kodu."""
    return re.sub(r"^```[a-z]*\n.*?^```$", "", RUNBOOK.read_text(encoding="utf-8"), flags=re.S | re.M)


@needs_runbook
def test_runbook_prose_has_no_runnable_commands_in_inline_code() -> None:
    """Każde polecenie jest w osobnym bloku do skopiowania; w tekście i tabelach tylko nazwy plików i słowa kluczowe."""
    spans = re.findall(r"`([^`\n]+)`", _runbook_prose())
    # dwa i więcej wyrazów zaczynających się od nazwy polecenia wygląda na polecenie do skopiowania; pojedyncze nazwy
    # (np. `clear-kill`, `status`) są dozwolone jako odwołania
    offenders = [s for s in spans if len(s.split()) >= 2 and s.split()[0] in _RUNNABLE_WORDS]
    assert offenders == []
    assert [s for s in spans if "\\|" in s or "|" in s] == []  # rurka w kodzie w tabeli psuje kopiowanie


@needs_runbook
def test_runbook_does_not_ask_for_a_terminal_flag_and_runs_compose_one_off_commands_plainly() -> None:
    for _, body in _runbook_blocks():
        for line in body.splitlines():
            if re.match(r"^\s*docker compose\b.*\brun\b", line):
                assert " -it " not in line and " -t " not in line, line  # Compose sam przydziela terminal


@needs_runbook
def test_runbook_backup_goes_outside_the_repository() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "~/gw-kopie" in text and "umask 077" in text
    assert "data.kopia" not in text  # kopia w repozytorium trafiłaby do publicznego repo i kontekstu budowy
    assert "cp -a data data" not in text


@needs_runbook
def test_runbook_login_recovery_uses_a_single_login_probe_and_counts_the_full_suite_correctly() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "-k test_accounts_listed" in text  # jedno logowanie
    assert "kilkanaście logowań" not in text and "8 logowań" in text
    assert "gw-stock-live-readonly" in text and "gw-stock-test" in text
    assert "nie więcej niż jedna próba logowania" in text.replace("\n", " ")


@needs_runbook
def test_runbook_update_procedure_has_memory_gates_before_and_after() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text[text.index("### 8.2"):text.index("### 8.3")]
    assert "free -m" in section and "docker stats --no-stream" in section
    assert section.index("free -m") < section.index("build gw-stock-engine") < section.rindex("docker stats --no-stream")
    assert "600 MB" in section and "500 MB" in section


@needs_runbook
def test_runbook_grep_of_live_allowed_does_not_print_the_comment_line() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "grep live_allowed" not in text
    assert text.count("grep '^live_allowed' config/runtime.toml") >= 2


@needs_runbook
def test_runbook_documents_the_decided_operator_semantics() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "smoke --hold",  # (d) pozycja próbna trzymana N sekund
        "4.3b",  # awaria wewnątrz kontenera: restart polityką
        "ARM_LIVE",
        "3 nowe próby",  # (b) licznik logowań jest w pamięci
        "nieudane logowanie",
        "gw-stock-live-readonly",
        "rmdir",  # katalog zamiast pliku klucza utworzony przez Dockera
        "early_closes",
    ):
        assert needle in text, needle
    assert "(kalendarz je zna)" not in text


# Zdania i nazwy z kodu, które runbook cytuje. Test wiąże je z tekstem źródeł: gdy kod je zmieni, runbook trzeba
# poprawić. Celowo NIE ma tu napisów, które ma jeszcze zmienić poprawka zadania 14 (treść alarmów kill switcha,
# wiersze kroków `smoke`, `PASS smoke`); sprawdzamy tylko te stabilne.
_QUOTED_FROM_SOURCES = {
    "cli.py": (
        "Number of the account to bind",
        "Type the last 4 characters of the chosen account id to confirm",
        "bind-account aborted",
        "nothing written",
        "mode (files):",
        "all arming conditions met",
        "last decision",
        "heartbeat: none",
        "journal: none yet",
        "unresolved orders:",
        "kill switch:",
        "mode: live (all four arming conditions met",
        "mode: dry_run, no orders will be sent",
        "OVERWRITE",
        "the API did not show the stop",
        "SMOKE_SECOND_CLOSE_UNEXPECTED",
        "Wpisz identyfikator zamknięcia (deal id) z alarmu UNMATCHED_CLOSED_DEAL, które jest tą transakcją:",
        "Wpisz SZACUNEK, aby zaksięgować oszacowaną stratę:",
        "Wpisz NIEWYKONANE, aby uznać zlecenie",
        "nic nie zmieniono",
        "zaksięgowano transakcję",
        "DOKŁADNIE z potwierdzenia",
        "[ESTIMATED]",
    ),
    "watchdog.py": ("another engine instance holds", "LOGIN_FAILED", "LOGIN_LOCKOUT", "QUOTE_SILENCE"),
    "risk.py": ("KILL_STATE_UNREADABLE", "HARD_STOP_FILE", "STOP_FILE", "kill switch escalated"),
    "app.py": ("engine started: mode", "NOT_SESSION", "DayReport", "NO_SIGNAL_DATA"),
}


@needs_runbook
@pytest.mark.skipif(not SOURCES.exists(), reason="brak src/gwstock")
def test_strings_quoted_in_the_runbook_still_exist_in_the_sources() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    missing_in_runbook = []
    missing_in_source = []
    for filename, phrases in _QUOTED_FROM_SOURCES.items():
        source = (SOURCES / filename).read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase not in runbook:
                missing_in_runbook.append(phrase)
            if phrase not in source:
                missing_in_source.append((filename, phrase))
    assert missing_in_source == [], "runbook cytuje napisy, których nie ma już w kodzie"
    assert missing_in_runbook == [], "lista cytatów w teście nie odpowiada runbookowi"
