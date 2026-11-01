#!/usr/bin/env python3
"""Strażnik live / live guard (zadanie 17, spec §9 i §14). Czysta biblioteka standardowa, Python 3.9+.

Pilnuje trzech rzeczy w plikach zapisywanych przez agenta (hook `PreToolUse` Claude Code) oraz w całym repozytorium
(`--scan`, CI):

  1. Pliki uzbrojenia `data/ARM_LIVE` i `data/L1_PASSED` tworzy WYŁĄCZNIE właściciel. Kod ani agent nie mogą ich
     tworzyć ani zapisywać (`open(..., 'w')`, `Path.touch()`, `write_text`, `os.mknod`, `shutil.copy`, `touch`, `>`,
     `tee`, `cp`, `ln -s`, ...). Sprawdzanie (`.exists()`), odczyt, usuwanie i proza w komentarzach są dozwolone.
  2. Klucz API ani hasło nie mogą być zaszyte w kodzie (`X-CAP-API-KEY`, `api_key`, `api_password`, `password` z
     literałem co najmniej 12 znaków [A-Za-z0-9], który nie jest oczywistym wypełniaczem). Repozytorium jest publiczne.
  3. Zapis `live_allowed = true` do `config/runtime.toml` przestawia wyłącznie właściciel.

To bariera przed WYPADKIEM, nie granica bezpieczeństwa. Hook widzi tylko narzędzia Write, Edit i MultiEdit; polecenie
Bash, zmienna zbudowana z kawałków w czasie działania, `getattr`, kodowanie base64 czy aliasy z innego pliku go
omijają (lista luk: raport zadania 17). Ostateczną bramką jest właściciel, który sam tworzy `ARM_LIVE` i `L1_PASSED`.

KOD WYJŚCIA hooka (dokumentacja hooków Claude Code): 0 = przepuść, 2 = zablokuj (stderr trafia do modelu). Każdy inny
kod to błąd niezablokujący, dlatego w `.claude/settings.json` hook ma też `onFailure: "block"`.

DECYZJA fail-closed: wejście hooka, którego nie da się odczytać (puste, nie-JSON, brak `tool_name`, `file_path` albo
treści), BLOKUJE wywołanie (kod 2). Błędne wejście nie może być celowym obejściem (agent nie kontroluje stdin hooka),
więc fail-open byłby bezpieczny; fail-closed wybrano, bo spec §14 każe hookom być fail-closed, a zmiana formatu hooka
w przyszłej wersji Claude Code ma się ujawnić głośno (zablokowany zapis), a nie po cichu wyłączyć ochronę.
Koszt: błąd w tym skrypcie blokuje Write/Edit do czasu naprawy przez właściciela (Bash nie jest objęty hookiem).

LISTA WYJĄTKÓW (nie skanowane; ścieżki dopasowywane po całych członach od końca, `/` lub `\\`). Każdy wpis jest tu
uzasadniony, a test `test_the_allow_list_has_no_stale_or_unneeded_entries` pilnuje, żeby plik naprawdę coś naruszał:

  ARMING_EXEMPT (reguła tworzenia plików uzbrojenia nie obowiązuje):
    scripts/hooks/guard_live.py     sam strażnik: w opisie trzyma przykłady wzorców (np. `(data_dir / ARM_FILE).write_text`)
    tests/unit/test_guard_live.py   testy strażnika: dziesiątki napisów z naruszeniami jako dane testowe
    tests/fault/engine_sim.py       symulacja grająca rolę WŁAŚCICIELA: `arm()` tworzy plik uzbrojenia w tymczasowym
                                    katalogu `data_dir` (`(data_dir / ARM_FILE).write_text("")`); bez tego tryb live
                                    silnika nie dałby się przetestować
    tests/fault/test_modes.py       fixture `(data / ARMING_NAMES[0]).write_text("")` tworzy plik uzbrojenia w
                                    tymczasowym `data` jako właściciel; test `test_engine_code_never_creates_arm_file`
                                    trzyma w napisach wzorce tworzenia (`(Path('data') / 'ARM_LIVE').touch()`)
    tests/fault/test_engine_day.py  `(data_dir / "L1_PASSED").write_text("")` w tymczasowym katalogu: właściciel
                                    „zdaje" bramkę L1 w teście reguły wielkości
  KEY_EXEMPT (reguła zaszytych kluczy nie obowiązuje; reguła uzbrojenia nadal tak):
    scripts/hooks/guard_live.py     sam strażnik: wzorce kluczy w napisach
    tests/unit/test_guard_live.py   testy strażnika: przykłady zaszytych kluczy jako dane testowe
    tests/unit/test_capitalcom_adapter.py
                                    sztuczne dane logowania `SECRETS` (`key-ABCDEF123456`, ...) do pliku sekretów w
                                    tmp_path; nic z tego nie jest prawdziwym kluczem, a adapter ma ich wymagać w
                                    określonym kształcie
  Poza listą: `docs/**` i każdy plik poza przyrostkami .py .sh .bash .toml .yml .yaml .cfg .ini .env (oraz Dockerfile i
  Makefile), czyli proza (`*.md`), `.claude/settings.json` (zawiera wzorce reguł deny) i dane. Pliki w `tests/` mogą
  mieć sztuczne wartości zaczynające się od `test`, `fake`, `dummy` lub `mock`; prawdziwie wyglądający klucz w testach
  też blokuje. `.github/workflows/ci.yml` NIE jest wyjątkiem: przechodzi strażnika.

Użycie:
    python3 scripts/hooks/guard_live.py            czyta JSON hooka ze stdin (tryb hooka)
    python3 scripts/hooks/guard_live.py --scan     skanuje repozytorium (korzeń wyznaczony względem tego pliku)
    python3 scripts/hooks/guard_live.py --scan PATH [PATH ...]    skanuje wskazane pliki i katalogi (kod 1 = naruszenie)
"""

from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ARMING_EXEMPT = frozenset({
    "scripts/hooks/guard_live.py",
    "tests/unit/test_guard_live.py",
    "tests/fault/engine_sim.py",
    "tests/fault/test_modes.py",
    "tests/fault/test_engine_day.py",
})  # fmt: skip
KEY_EXEMPT = frozenset({
    "scripts/hooks/guard_live.py",
    "tests/unit/test_guard_live.py",
    "tests/unit/test_capitalcom_adapter.py",
})  # fmt: skip

SCANNED_SUFFIXES = frozenset({".py", ".sh", ".bash", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".env"})
SCANNED_NAMES = frozenset({"Dockerfile", "Makefile"})
GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")
MIN_SECRET_RUN = 12  # tyle znaków [A-Za-z0-9] z rzędu w literale uznajemy za klucz
MAX_JOIN_LINES = 40  # ile fizycznych linii wolno skleić w jedną instrukcję (nawiasy, `\`)
MAX_EDIT_FILE_BYTES = 5_000_000
MAX_CALL_SPAN = 3000  # ile znaków nawiasu analizujemy w jednym wywołaniu (ochrona przed kwadratowym czasem)
MAX_RECEIVER_SPAN = 600
MAX_SHELL_SPAN = 300  # ile znaków wolno między poleceniem (`touch`, `cp`, `>`) a nazwą pliku

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_SKIP_TOP = ("data", ".git", ".superpowers")  # tylko na pierwszym poziomie skanowanego korzenia (`data*`)
SCAN_SKIP_ANYWHERE = frozenset({"__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache", ".tox"})


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str  # "arming" | "key" | "runtime"
    message: str


# --- wstępne przetwarzanie: logiczne linie ---------------------------------------------------------------------------


def _code_view(line: str) -> tuple[str, int]:
    """Linia bez komentarza `#` (poza napisami) i zmiana głębokości nawiasów (poza napisami)."""
    quote = ""
    depth = 0
    for i, ch in enumerate(line):
        if quote:
            if ch == "\\":
                continue
            if ch == quote and line[i - 1] != "\\":
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i], depth
    return line, depth


_LITERAL_JOIN = re.compile(r"""(["'])\s*(?:\+\s*)?["']""")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(numer pierwszej linii, instrukcja): sklejone `\\`, otwarte nawiasy, połączone literały `'a' + 'b'`."""
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        start = i
        stripped, depth = _code_view(lines[i])
        parts = [stripped]
        joined = 1
        while joined < MAX_JOIN_LINES and i + 1 < len(lines) and (parts[-1].rstrip().endswith("\\") or depth > 0):
            if parts[-1].rstrip().endswith("\\"):
                parts[-1] = parts[-1].rstrip()[:-1]
            elif not lines[i + 1].strip():
                break
            i += 1
            joined += 1
            nxt, delta = _code_view(lines[i])
            parts.append(nxt)
            depth = max(depth + delta, 0)
        statement = " ".join(p.strip() for p in parts)
        statement = _LITERAL_JOIN.sub("", statement)
        if statement.strip():
            out.append((start + 1, statement))
        i += 1
    return out


# --- rozpoznawanie nazw plików uzbrojenia ------------------------------------------------------------------------------

_BASE_MARKERS = (
    r"ARM_LIVE",
    r"L1_PASSED",
    r"(?<![A-Za-z0-9])(?:ARM_FILE|L1_FILE|L1_PASSED_FILE|ARM_LIVE_FILE|ARM_PATH|L1_PATH)(?![A-Za-z0-9_])",
)


@functools.lru_cache(maxsize=64)
def _marker_re(aliases: frozenset[str]) -> re.Pattern[str]:
    parts = list(_BASE_MARKERS)
    parts += [rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9_])" for a in sorted(aliases)]
    return re.compile("|".join(parts), re.IGNORECASE)


def _marker_name(matched: str, aliases: dict[str, str]) -> str:
    for alias, name in aliases.items():
        if matched.lower() == alias.lower():
            return name
    return "L1_PASSED" if "l1" in matched.lower() else "ARM_LIVE"


_ASSIGN = re.compile(
    r"^\s*(?:export\s+|local\s+|readonly\s+)?(?P<lhs>[A-Za-z_][\w.]*)\s*(?::\s*[^=]+?)?\s*=(?!=)\s*(?P<rhs>.+)$"
)
_CHECK_ONLY = re.compile(
    r"\.(?:exists|is_file|is_dir|is_symlink|stat|lstat|read_text|read_bytes|unlink)\s*\(|(?:isfile|exists)\s*\(", re.I
)


# --- wywołania Pythona: odbiorca i argumenty ---------------------------------------------------------------------------


def _balanced(s: str, open_idx: int) -> str:
    """Zawartość nawiasu otwartego w `s[open_idx]` (do zamknięcia lub końca tekstu)."""
    depth = 0
    quote = ""
    end = min(len(s), open_idx + MAX_CALL_SPAN)
    for i in range(open_idx, end):
        ch = s[i]
        if quote:
            if ch == quote and s[i - 1] != "\\":
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return s[open_idx + 1 : i]
    return s[open_idx + 1 : end]


def _split_args(args: str) -> list[str]:
    out: list[str] = []
    depth = 0
    quote = ""
    cur: list[str] = []
    for i, ch in enumerate(args):
        if quote:
            cur.append(ch)
            if ch == quote and args[i - 1] != "\\":
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _receiver(s: str, dot_idx: int) -> str:
    """Wyrażenie przed kropką w `s[dot_idx]`: `(Path('d') / 'x')`, `Path('d')`, `self.arm_file`, `p`."""
    i = dot_idx
    floor = max(0, dot_idx - MAX_RECEIVER_SPAN)
    while i > floor:
        c = s[i - 1]
        if c in ")]}":
            depth = 0
            j = i - 1
            while j >= floor:
                if s[j] in ")]}":
                    depth += 1
                elif s[j] in "([{":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            i = max(j, floor)
        elif c.isalnum() or c in "_.":
            i -= 1
        elif c in "\"'":
            j = s.rfind(c, floor, i - 1)
            i = j if j >= 0 else i - 1
        else:
            break
    return s[i:dot_idx]


_CALL = re.compile(r"(?<!\w)(?P<name>[A-Za-z_]\w*)\s*\(")
_PURE_DOTTED = re.compile(r"^[A-Za-z_][\w.]*$")
_LITERAL = re.compile(r"""(["'])([^"'\n]*)\1""")
_OS_FLAGS = re.compile(r"\bO_(?:CREAT|WRONLY|RDWR|APPEND|TRUNC|EXCL)\b")
_LIST_VERB = re.compile(r"""["'](?:touch|tee|cp|mv|install|ln|dd|truncate|rsync)["']""")

_PATH_WRITE_METHODS = frozenset({"touch", "write_text", "write_bytes", "mkdir", "symlink_to", "hardlink_to", "link_to"})
_OS_FIRST_ARG = frozenset({"mknod", "mkfifo", "mkdir", "makedirs", "truncate"})
_DEST_SECOND = frozenset({
    ("os", "rename"), ("os", "replace"), ("os", "symlink"), ("os", "link"), ("shutil", "copy"), ("shutil", "copy2"),
    ("shutil", "copyfile"), ("shutil", "copytree"), ("shutil", "move"), ("", "copyfile"), ("", "copy2"), ("", "copytree"),
})  # fmt: skip
_SUBPROCESS_FUNCS = frozenset({
    "run", "call", "check_call", "check_output", "Popen", "system", "popen", "create_subprocess_exec",
    "create_subprocess_shell", "execv", "execvp", "execl", "execlp", "spawnv", "spawnl",
})  # fmt: skip


_INTERESTING_CALLS = (
    {"open", "rename", "replace"} | _PATH_WRITE_METHODS | _OS_FIRST_ARG | {n for _, n in _DEST_SECOND} | _SUBPROCESS_FUNCS
)


def _write_mode(args: str) -> bool:
    """Czy w argumentach jest literał trybu zapisu (`'w'`, `'ab'`, `'r+'`, `'x'`)."""
    for _, content in _LITERAL.findall(args):
        if re.fullmatch(r"[rwxabtU+]{1,4}", content) and re.search(r"[wax+]", content):
            return True
    return False


def _call_hit(statement: str, mark: re.Pattern[str]) -> re.Match[str] | None:
    """Pierwsze trafienie znacznika pliku uzbrojenia w miejscu, gdzie wywołanie tworzy lub zapisuje plik."""

    def has(text: str) -> re.Match[str] | None:
        return mark.search(text)

    for m in _CALL.finditer(statement):
        name = m.group("name")
        if name not in _INTERESTING_CALLS:
            continue
        args_text = _balanced(statement, m.end() - 1)
        args = _split_args(args_text)
        start = m.start("name")
        recv = _receiver(statement, start - 1) if start > 0 and statement[start - 1] == "." else ""
        pure = bool(recv) and bool(_PURE_DOTTED.match(recv))
        recv_last = recv.split(".")[-1] if pure else ""
        module = recv_last if (not recv or pure) else None

        if name == "open":
            if module == "os":
                if args and _OS_FLAGS.search(args_text) and (hit := has(args[0])):
                    return hit
            elif _write_mode(args_text):
                kw_file = next((a for a in args if a.replace(" ", "").startswith("file=")), "")
                if hit := has(recv) or has(args[0] if args else "") or has(kw_file):
                    return hit
        elif name in _PATH_WRITE_METHODS or name in _OS_FIRST_ARG:
            if module == "os" and name in _OS_FIRST_ARG:
                if args and (hit := has(args[0])):
                    return hit
            elif recv and name in _PATH_WRITE_METHODS and (hit := has(recv)):
                return hit
            elif recv and name == "truncate" and (hit := has(recv)):
                return hit
        if name in ("rename", "replace") and recv and module != "os" and len(args) == 1 and (hit := has(args[0])):
            return hit
        if (module or "", name) in _DEST_SECOND and len(args) >= 2 and (hit := has(args[1])):
            return hit
        if name in _SUBPROCESS_FUNCS and module in ("", "subprocess", "os", "asyncio") and _LIST_VERB.search(args_text):
            if hit := has(args_text):
                return hit
    return None


# --- powłoka ------------------------------------------------------------------------------------------------------------

_VERBS = r"(?:touch|tee|install|ln|cp|mv|dd|truncate|rsync|mknod|mkfifo)"


@functools.lru_cache(maxsize=64)
def _shell_res(mark_pattern: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    path_chars = rf"""[^\s"';|&<>]{{0,{MAX_SHELL_SPAN}}}"""
    redirect = re.compile(rf"""(?<![-=])(?:\d*>>?|&>>?|>\|)\s*["']?{path_chars}(?:{mark_pattern})""", re.IGNORECASE)
    verb = re.compile(rf"""(?<![\w./-]){_VERBS}(?![\w-])[^\n;|&,]{{0,{MAX_SHELL_SPAN}}}?(?:{mark_pattern})""", re.IGNORECASE)
    return redirect, verb


def _shell_hit(statement: str, mark: re.Pattern[str]) -> re.Match[str] | None:
    """Polecenia powłoki tworzące plik: przekierowanie `>`/`>>` do pliku oraz `touch`, `tee`, `cp`, `ln`, ..."""
    redirect, verb = _shell_res(mark.pattern)
    return redirect.search(statement) or verb.search(statement)


# --- klucze API ----------------------------------------------------------------------------------------------------------

_Q = r"""\\?["']"""
_KEY_HEADER_QUOTED = re.compile(rf"""(?P<k>X-CAP-API-KEY){_Q}?\s*\]?\s*[:=,]\s*[rRbBuU]{{0,2}}{_Q}(?P<v>[^"'\\\n]*)""", re.I)
_KEY_HEADER_RAW = re.compile(r"""(?P<k>X-CAP-API-KEY):\s*(?P<v>[^\s"'\\]+)""", re.I)
_KEY_NAMED = re.compile(
    rf"""(?P<k>api_key|api_password|password){_Q}?\s*[:=]\s*[rRbBuU]{{0,2}}{_Q}(?P<v>[^"'\\\n]*)""",
    re.I,
)
_KEY_ENV = re.compile(
    r"""^\s*(?:export\s+)?(?:[A-Z0-9]+_)*(?P<k>API_KEY|API_PASSWORD|PASSWORD)\s*=\s*["']?(?P<v>[^"'\s#]+)""", re.I
)
_PLACEHOLDER = re.compile(r"<|>|…|\.\.\.|[{}$%]|x{4,}|\*{3,}|change[-_ ]?me|placeholder|redacted|your[-_ ]|example|dummy|todo", re.I)
_TEST_VALUE = re.compile(r"^(?:test|fake|dummy|mock)[-_]", re.I)


def _secret_literal(value: str, test_values_ok: bool) -> bool:
    if not re.search(rf"[A-Za-z0-9]{{{MIN_SECRET_RUN},}}", value) or _PLACEHOLDER.search(value):
        return False
    return not (test_values_ok and _TEST_VALUE.match(value))


def _key_hit(statement: str, *, test_values_ok: bool, env: bool) -> str | None:
    patterns = [_KEY_HEADER_QUOTED, _KEY_HEADER_RAW, _KEY_NAMED] + ([_KEY_ENV] if env else [])
    for pattern in patterns:
        for m in pattern.finditer(statement):
            if _secret_literal(m.group("v"), test_values_ok):
                return m.group("k")
    return None


# --- skan tekstu ---------------------------------------------------------------------------------------------------------


def scan_text(
    text: str, *, arming: bool = True, key: bool = True, test_values_ok: bool = False, env: bool = False
) -> list[Finding]:
    """Naruszenia w tekście (jedno na instrukcję i regułę). Nigdy nie zawiera wartości klucza w komunikacie."""
    findings: list[Finding] = []
    aliases: dict[str, str] = {}
    for line, statement in _logical_lines(text):
        mark = _marker_re(frozenset(aliases))
        if arming:
            hit = _call_hit(statement, mark) or _shell_hit(statement, mark)
            if hit:
                found_marker = mark.search(hit.group(0))
                name = _marker_name(found_marker.group(0) if found_marker else "ARM_LIVE", aliases)
                findings.append(
                    Finding(
                        line,
                        "arming",
                        f"zapis lub utworzenie pliku uzbrojenia {name}: tworzy go wyłącznie właściciel "
                        f"(creates or writes the owner-only arming file {name})",
                    )
                )
            assign = _ASSIGN.match(statement)
            if assign:
                lhs, rhs = assign.group("lhs"), assign.group("rhs")
                found = mark.search(rhs)
                if found and not _CHECK_ONLY.search(rhs):
                    aliases[lhs] = _marker_name(found.group(0), aliases)
                elif lhs in aliases:
                    del aliases[lhs]
        if key:
            what = _key_hit(statement, test_values_ok=test_values_ok, env=env)
            if what:
                findings.append(
                    Finding(
                        line,
                        "key",
                        f"zaszyty klucz lub hasło ({what}); klucz leży poza repozytorium, w ~/.config/gw/capitalcom.json "
                        f"(hard-coded secret in {what}: the repo is public)",
                    )
                )
    return findings


def violates(text: str) -> bool:
    """Prawda, gdy tekst tworzy lub zapisuje ARM_LIVE / L1_PASSED albo zawiera zaszyty klucz API (bez kontekstu ścieżki)."""
    return bool(scan_text(text))


# --- skan z uwzględnieniem ścieżki ---------------------------------------------------------------------------------------

_RUNTIME_TRUE = re.compile(r"""^\s*"?live_allowed"?\s*=\s*true(?![\w-])""", re.I)


def _normalize(path: str) -> str:
    norm = posixpath.normpath(path.replace("\\", "/"))
    return norm[2:] if norm.startswith("./") else norm


def _tail_match(norm: str, rel: str) -> bool:
    return norm == rel or norm.endswith("/" + rel)


def _is_scanned_name(norm: str) -> bool:
    name = posixpath.basename(norm)
    if name in SCANNED_NAMES or name.startswith("Dockerfile.") or name == ".env" or name.startswith(".env."):
        return True
    return posixpath.splitext(name)[1].lower() in SCANNED_SUFFIXES


def _is_env_file(norm: str) -> bool:
    name = posixpath.basename(norm)
    return name == ".env" or name.endswith(".env") or name.startswith(".env.")


def _under_tests(norm: str) -> bool:
    return norm.startswith("tests/") or "/tests/" in norm


def scan_path_text(path: str, text: str) -> list[Finding]:
    norm = _normalize(path)
    findings: list[Finding] = []
    prose = norm.startswith("docs/") or "/docs/" in norm
    if not prose and _is_scanned_name(norm):
        arming = not any(_tail_match(norm, rel) for rel in ARMING_EXEMPT)
        key = not any(_tail_match(norm, rel) for rel in KEY_EXEMPT)
        findings += scan_text(
            text, arming=arming, key=key, test_values_ok=_under_tests(norm), env=_is_env_file(norm)
        )
    if _tail_match(norm, "config/runtime.toml"):
        for number, raw in enumerate(text.splitlines(), start=1):
            if _RUNTIME_TRUE.match(_code_view(raw)[0]):
                findings.append(
                    Finding(
                        number,
                        "runtime",
                        "live_allowed = true w config/runtime.toml przestawia wyłącznie właściciel "
                        "(only the owner flips live_allowed)",
                    )
                )
    return sorted(findings, key=lambda f: (f.line, f.rule))


def violates_in(path: str, text: str) -> list[str]:
    """Czytelne powody naruszeń dla pliku o danej ścieżce (pusta lista = można zapisać)."""
    return [f"line {f.line}: {f.message}" for f in scan_path_text(path, text)]


# --- tryb hooka ----------------------------------------------------------------------------------------------------------

_BLOCK_WHY = (
    "Dlaczego / why: pliki uzbrojenia data/ARM_LIVE i data/L1_PASSED oraz live_allowed = true w config/runtime.toml\n"
    "ustawia wyłącznie właściciel, a klucze API nie trafiają do repozytorium (jest publiczne).\n"
    "Only the owner creates the arming files and flips live_allowed; API keys never go into this public repo."
)


def _block(lines: list[str]) -> int:
    sys.stderr.write("\n".join(lines) + "\n")
    return 2


def _fail_closed(reason: str) -> int:
    return _block(
        [
            "ZABLOKOWANO / BLOCKED by guard_live (fail-closed): nie da się odczytać wejścia hooka "
            "(cannot read the hook input).",
            f"Powód / reason: {reason}",
            "Strażnik blokuje, gdy nie umie ocenić zapisu. Napraw scripts/hooks/guard_live.py lub poproś właściciela.",
            "The guard blocks when it cannot judge a write. Fix scripts/hooks/guard_live.py or ask the owner.",
        ]
    )


def _read_existing(path: str) -> str | None:
    try:
        file = Path(path)
        if file.is_file() and file.stat().st_size <= MAX_EDIT_FILE_BYTES:
            return file.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    return None


def _apply_edits(existing: str | None, edits: list[tuple[str, str, bool]]) -> str:
    """Wynikowa treść pliku po edycjach; gdy któregoś `old_string` nie ma na dysku, sama suma `new_string`."""
    if existing is not None:
        result = existing
        for old, new, replace_all in edits:
            if not old or old not in result:
                break
            result = result.replace(old, new) if replace_all else result.replace(old, new, 1)
        else:
            return result
    return "\n".join(new for _, new, _ in edits)


def _hook_text(tool: str, tool_input: dict, file_path: str) -> str | None:
    """Tekst do oceny dla danego narzędzia albo None, gdy kształt wejścia jest błędny."""
    if tool == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    if tool == "Edit":
        old, new = tool_input.get("old_string"), tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        return _apply_edits(_read_existing(file_path), [(old, new, tool_input.get("replace_all") is True)])
    edits = tool_input.get("edits")
    if not isinstance(edits, list):
        return None
    parsed: list[tuple[str, str, bool]] = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("old_string"), str):
            return None
        if not isinstance(edit.get("new_string"), str):
            return None
        parsed.append((edit["old_string"], edit["new_string"], edit.get("replace_all") is True))
    return _apply_edits(_read_existing(file_path), parsed) if parsed else ""


def hook_main(raw: bytes) -> int:
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _fail_closed(f"stdin nie jest poprawnym JSON-em UTF-8 / stdin is not valid UTF-8 JSON ({type(exc).__name__})")
    if not isinstance(event, dict) or not isinstance(event.get("tool_name"), str):
        return _fail_closed("brak pola tool_name / no tool_name in the hook input")
    tool = event["tool_name"]
    if tool not in GUARDED_TOOLS:
        return 0
    tool_input = event.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return _fail_closed(f"{tool}: brak tool_input.file_path / missing tool_input.file_path")
    text = _hook_text(tool, tool_input, file_path)
    if text is None:
        return _fail_closed(f"{tool}: nieoczekiwany kształt tool_input / unexpected tool_input shape")
    reasons = violates_in(file_path, text)
    if not reasons:
        return 0
    return _block(
        [f"ZABLOKOWANO / BLOCKED by guard_live: {tool} {file_path}"]
        + [f"  - {reason}" for reason in reasons]
        + [_BLOCK_WHY, "Usuń tę zmianę. / Drop this change. Wyjątki testowe: nagłówek scripts/hooks/guard_live.py."]
    )


# --- tryb --scan ---------------------------------------------------------------------------------------------------------


def _iter_files(root: Path, top: Path):
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        at_top = here == top
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SCAN_SKIP_ANYWHERE
            and not d.endswith(".egg-info")
            and d != ".venv"
            and not d.startswith("venv")
            and not (at_top and (d in SCAN_SKIP_TOP or d.startswith("data")))
        )
        for name in sorted(files):
            if _is_scanned_name(name):
                yield here / name


def _display(file: Path) -> str:
    try:
        return str(file.resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        return str(file)


def scan_paths(paths: list[Path]) -> tuple[int, int]:
    """Skanuje pliki i katalogi; zwraca (liczba naruszeń, liczba przeskanowanych plików)."""
    violations = scanned = 0
    for target in paths:
        files = [target] if target.is_file() else list(_iter_files(target, target))
        for file in files:
            try:
                text = file.read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                sys.stderr.write(f"{file}: nie można odczytać / cannot read ({exc.__class__.__name__})\n")
                violations += 1
                continue
            scanned += 1
            for finding in scan_path_text(file.as_posix(), text):
                print(f"{_display(file)}:{finding.line}: {finding.message}")
                violations += 1
    return violations, scanned


def scan_main(args: list[str]) -> int:
    targets = [Path(a) for a in args] or [REPO_ROOT]
    for target in targets:
        if not target.exists():
            sys.stderr.write(f"guard_live --scan: brak ścieżki / no such path: {target}\n")
            return 2
    violations, scanned = scan_paths(targets)
    if violations:
        sys.stderr.write(
            f"guard_live --scan: {violations} naruszeń w {scanned} plikach / {violations} violation(s) in {scanned} files\n"
        )
        return 1
    print(f"guard_live --scan: OK, {scanned} plików / files scanned")
    return 0


USAGE = (
    "guard_live.py: strażnik live (hook PreToolUse Claude Code i skan repozytorium)\n"
    "  python3 scripts/hooks/guard_live.py            JSON hooka na stdin; kod 2 = zablokuj\n"
    "  python3 scripts/hooks/guard_live.py --scan [PATH ...]   skan repozytorium lub wskazanych ścieżek; kod 1 = naruszenie\n"
)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if args and args[0] == "--scan":
        return scan_main(args[1:])
    if args:
        sys.stderr.write(USAGE)
        return 2
    if sys.stdin.isatty():
        print(USAGE)
        return 0
    try:
        return hook_main(sys.stdin.buffer.read())
    except Exception as exc:  # noqa: BLE001 - nieoczekiwany błąd strażnika też blokuje (fail-closed)
        return _fail_closed(f"nieoczekiwany błąd / unexpected error: {type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main())
