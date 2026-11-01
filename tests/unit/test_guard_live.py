"""Strażnik live (zadanie 17): hook `PreToolUse` Claude Code, skan repozytorium w CI, ustawienia, agenci i workflow CI.

Strażnik to bariera przed WYPADKIEM (agent lub kod tworzy `data/ARM_LIVE` / `data/L1_PASSED`, zaszywa klucz API,
przestawia `live_allowed`), nie granica bezpieczeństwa: ostatecznie decyduje właściciel (spec §9, §14).

Ten plik zawiera celowo wzorce naruszeń (jako napisy), więc jest wyłączony ze skanu przez listę wyjątków w
`scripts/hooks/guard_live.py`. Testy czytają `scripts/`, `.claude/`, `.github/` i `docs/`, których nie ma w obrazie
testowym (`docs/` odfiltrowuje `.dockerignore`, etapy Dockerfile kopiują tylko wskazane ścieżki): tam cały plik jest
pomijany WYŁĄCZNIE przy znaczniku `GW_TEST_IMAGE=1` (ustawia go Dockerfile), a w CI biegnie w zadaniu `tests`.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "hooks" / "guard_live.py"
SETTINGS = ROOT / ".claude" / "settings.json"
AGENTS_DIR = ROOT / ".claude" / "agents"
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"

# Obraz testowy (Dockerfile, cel test) ustawia GW_TEST_IMAGE=1 i nie zawiera docs/, scripts/, .claude/ ani .github/. Tylko wtedy
# pomijamy cały plik; bez znacznika brak tych katalogów (sparse checkout, przeniesione docs/) wywala testy głośno.
IN_TEST_IMAGE = os.environ.get("GW_TEST_IMAGE") == "1"
pytestmark = pytest.mark.skipif(IN_TEST_IMAGE, reason="obraz testowy (GW_TEST_IMAGE=1): brak docs/, scripts/, .claude/, .github/")

NAMES = ("ARM_LIVE", "L1_PASSED")


@functools.cache
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_live_under_test", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def violates(text: str) -> bool:
    return guard().violates(text)


def with_names(templates: dict[str, str]) -> list:
    """Rozwija szablony (`@N@` = nazwa pliku uzbrojenia) dla obu nazw: ARM_LIVE i L1_PASSED."""
    return [
        pytest.param(template.replace("@N@", name), id=f"{slug}-{name}")
        for slug, template in templates.items()
        for name in NAMES
    ]


# --- cztery asercje z planu --------------------------------------------------------------------------------------


def test_guard_flags_arm_file_creation_and_hardcoded_keys():
    assert violates("open('data/ARM_LIVE', 'w').close()")
    assert violates("headers = {'X-CAP-API-KEY': 'abcd1234efgh5678'}")
    assert not violates("def f(): return 1")
    assert not violates("if arm_file.exists(): pass")


# --- tworzenie plików uzbrojenia: wzorce Pythona -------------------------------------------------------------------

PY_CREATION = {
    "open-w": "open('data/@N@', 'w').close()",
    "open-a": "f = open('data/@N@', 'a')",
    "open-x": 'open("data/@N@", "x")',
    "open-wb": "open('data/@N@', 'wb')",
    "open-r-plus": "open('data/@N@', 'r+')",
    "open-mode-kw": "open('data/@N@', mode='w')",
    "open-with": "with open(data_dir / '@N@', 'w') as fh:\n    fh.write('')",
    "open-str-wrapped": "open(str(data_dir / '@N@'), 'w')",
    "open-multiline": "open(\n    'data/@N@',\n    'w',\n)",
    "path-open-w": "Path('data/@N@').open('w')",
    "module-open-w": "async with aiofiles.open('data/@N@', 'w') as fh:\n    pass",
    "touch": "Path('data/@N@').touch()",
    "touch-division": "(Path('data') / '@N@').touch()",
    "touch-exist-ok": "(data_dir / '@N@').touch(exist_ok=True)",
    "write-text": "Path('data', '@N@').write_text('')",
    "write-bytes": "(d / '@N@').write_bytes(b'')",
    "mkdir": "(d / '@N@').mkdir()",
    "symlink-to": "(d / '@N@').symlink_to('/tmp/x')",
    "rename-to": "tmp.rename(d / '@N@')",
    "replace-to": "tmp.replace(d / '@N@')",
    "os-mknod": "os.mknod('data/@N@')",
    "os-mkfifo": "os.mkfifo('data/@N@')",
    "os-open-creat": "os.open('data/@N@', os.O_CREAT | os.O_WRONLY)",
    "os-open-concat": "fd = os.open('data/' + '@N@', os.O_CREAT)",
    "os-symlink": "os.symlink('/tmp/x', 'data/@N@')",
    "os-rename": "os.rename(tmp, 'data/@N@')",
    "os-replace": "os.replace(tmp, 'data/@N@')",
    "shutil-copy": "shutil.copy(src, 'data/@N@')",
    "shutil-copyfile": "shutil.copyfile(src, data_dir / '@N@')",
    "shutil-copy2": "shutil.copy2(src, 'data/@N@')",
    "shutil-move": "shutil.move(src, 'data/@N@')",
    "alias-variable": "p = data_dir / '@N@'\np.touch()",
    "alias-annotated": "p: Path = data_dir / '@N@'\np.touch()",
    "alias-multiline": "p = (\n    data_dir / '@N@'\n)\np.write_text('')",
    "alias-chain": "base = Path('data/@N@')\nq = base\nq.touch()",
    "alias-constant": "FILE = '@N@'\n(data_dir / FILE).touch()",
    "subprocess-list": "subprocess.run(['touch', 'data/@N@'], check=True)",
    "subprocess-shell": "subprocess.run('echo ok > data/@N@', shell=True)",
    "os-system": "os.system('touch data/@N@')",
    "embedded-python": "subprocess.run(['python3', '-c', \"open('data/@N@', 'w')\"])",
}


@pytest.mark.parametrize("text", with_names(PY_CREATION))
def test_python_creation_forms_are_flagged(text):
    assert violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "(data_dir / ARM_FILE).write_text('')",  # tak „uzbraja" symulację tests/fault/engine_sim.py
        "(data_dir / L1_PASSED_FILE).write_text('')",
        "(data_dir / ARM_FILE).touch()",
        "arm_file.touch()",
        "self.arm_file.write_text('')",
        "l1_file.write_text('')",
        "open(arm_file, 'w')",
        "ARM_FILE = Path('data') / 'ARM_LIVE'\nARM_FILE.touch()",
    ],
)
def test_arming_identifiers_count_as_the_file(text):
    assert violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "open('data/' + 'ARM_' + 'LIVE', 'w')",
        "open('data/' 'ARM_' 'LIVE', 'w')",
        "Path('data/L1_' 'PASSED').touch()",
        "Path('data/L1_' + 'PASSED').touch()",
    ],
)
def test_names_split_into_literals_are_still_found(text):
    assert violates(text)


# --- tworzenie plików uzbrojenia: powłoka -------------------------------------------------------------------------

SHELL_CREATION = {
    "touch": "touch data/@N@",
    "touch-flags": "touch -a data/@N@",
    "touch-quoted-var": 'touch "$DATA_DIR/@N@"',
    "bare-redirect": "> data/@N@",
    "bare-redirect-nospace": ">data/@N@",
    "colon-redirect": ": > data/@N@",
    "echo-redirect": "echo ok > data/@N@",
    "echo-append": "echo ok >> data/@N@",
    "printf-redirect": "printf '' > data/@N@",
    "fd-redirect": "echo ok 1>data/@N@",
    "cat-redirect": "cat /dev/null > data/@N@",
    "heredoc-redirect": "cat <<EOF > data/@N@\nEOF",
    "tee": "echo 1 | tee data/@N@",
    "tee-append": "echo 1 | tee -a data/@N@",
    "install": "install -m 600 /dev/null data/@N@",
    "ln": "ln -s /x data/@N@",
    "ln-force": "ln -sf /x data/@N@",
    "cp": "cp /dev/null data/@N@",
    "cp-template": "cp template data/@N@",
    "mv": "mv /tmp/x data/@N@",
    "dd": "dd if=/dev/zero of=data/@N@ count=0",
    "truncate": "truncate -s 0 data/@N@",
    "docker-run": 'docker run --rm -v "$PWD/data:/data" alpine touch /data/@N@',
    "docker-exec": "docker compose -f docker/compose.yml exec gw-stock-engine sh -c 'touch /data/@N@'",
    "python-c": "python3 -c \"open('data/@N@','w')\"",
    "after-other-lines": "set -e\ncd /srv\ntouch data/@N@",
    "yaml-run": "    steps:\n      - run: touch data/@N@",
    "continued-line": "touch \\\n  data/@N@",
    "systemd-unit": "ExecStartPre=/usr/bin/touch /data/@N@",
    "absolute-cp": "/bin/cp /dev/null data/@N@",
    "sudo-absolute-touch": "sudo /usr/bin/touch data/@N@",
}


@pytest.mark.parametrize("text", with_names(SHELL_CREATION))
def test_shell_creation_forms_are_flagged(text):
    assert violates(text)


# --- to NIE jest tworzenie: sprawdzanie, odczyt, usuwanie, proza ----------------------------------------------------

NOT_CREATION = {
    "exists": "(data_dir / '@N@').exists()",
    "os-path-exists": "os.path.exists('data/@N@')",
    "os-path-isfile": "os.path.isfile('data/@N@')",
    "is-file": "(d / '@N@').is_file()",
    "if-exists": "x = data_dir / '@N@'\nif x.exists():\n    pass",
    "alias-asserted": "p = d / '@N@'\nassert p.is_file()",
    "read-text": "Path('data/@N@').read_text()",
    "open-default": "open('data/@N@')",
    "open-r": "open('data/@N@', 'r')",
    "open-rb": "open('data/@N@', 'rb')",
    "open-mode-r": "open('data/@N@', mode='r')",
    "os-stat": "os.stat('data/@N@')",
    "unlink": "(d / '@N@').unlink()",
    "os-remove": "os.remove('data/@N@')",
    "os-unlink": "os.unlink('data/@N@')",
    "copy-from": "shutil.copy('data/@N@', 'backup')",
    "rename-from": "os.rename('data/@N@', 'old')",
    "constant": "FILE = '@N@'",
    "str-replace": "text.replace('@N@', 'X')",
    "assert-not-exists": "assert not (d / '@N@').exists()",
    "log": "log.info('%s missing', '@N@')",
    "error-message": "raise RuntimeError(f'data/@N@ does not exist (only the owner creates it)')",
    "docstring": '"""Plik data/@N@ tworzy wyłącznie właściciel."""',
    "python-comment": "x = 1  # touch data/@N@\n# open('data/@N@', 'w')",
    "shell-comment": "    # echo ok > data/@N@\n# touch data/@N@",
    "rm": "rm data/@N@",
    "rm-force": "rm -f data/@N@",
    "ls": "ls -l data/@N@",
    "grep": "grep -rn @N@ src tests",
    "cat": "cat data/@N@",
    "test-f": "test -f data/@N@",
    "bracket-test": "[ -f data/@N@ ] && echo armed",
    "echo-message": "echo '@N@ missing'",
    "echo-to-stderr": 'echo "@N@ missing" >&2',
    "subprocess-rm": "subprocess.run(['rm', '-f', 'data/@N@'])",
    "touch-other-file": "(d / 'x').touch()\nexists = (d / '@N@').exists()",
    "content-is-not-the-file": "name = (d / '@N@').read_text().strip()\n(d / name).touch()",
    "replace-with-empty": "text = text.replace('@N@', '')",
    "replace-with-empty-dq": 'cleaned = text.replace("@N@", "")',
    "replace-empty-then-touch-other": "x = ''\n(d / 'other').touch()\ny = (d / '@N@').exists()",
}


@pytest.mark.parametrize("text", with_names(NOT_CREATION))
def test_reading_checking_deleting_and_prose_are_not_flagged(text):
    assert not violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "if arm_file.exists(): pass",
        "def arm_file_exists(data_dir):\n    return (Path(data_dir) / ARM_FILE).is_file()",
        "ok = arm_file_exists(self.data_dir)",
        "if not arm_file_exists:\n    missing.append('no file')",
        "rule_size_allowed = (self.data_dir / L1_PASSED_FILE).exists()",
        "l1 = (data_dir / L1_PASSED_FILE).exists()",
        "ARM_FILE = 'ARM_LIVE'  # uzbrojenie live: tworzy WYŁĄCZNIE właściciel",
        "runtime.live_allowed is not True",
        "(d / 'x').write_text('')",
        "arm_live_status = 'off'",
    ],
)
def test_engine_style_checks_are_not_flagged(text):
    assert not violates(text)


# --- znane luki: strażnik to bariera przed wypadkiem, nie granica bezpieczeństwa ----------------------------------------------
# Każdy przypadek poniżej NIE jest dziś wykrywany. `xfail(strict=True)`: gdy ktoś poprawi strażnika, test przestanie
# przechodzić i trzeba zaktualizować listę luk (raport zadania 17, CLAUDE.md), zamiast po cichu zapomnieć o wyjątku.

KNOWN_GAPS = {
    "name-built-at-runtime": "n = 'ARM_' + suffix\n(d / n).touch()",
    "loop-variable": "for n in ('ARM_LIVE',):\n    (d / n).touch()",
    "getattr-call": "getattr(Path('data/ARM_LIVE'), 'touch')()",
    "chr-built-name": "(d / ''.join(map(chr, [65, 82, 77, 95, 76, 73, 86, 69]))).touch()",
    "glob-in-shell": "touch data/ARM_*",
    "base64-payload": "echo QVJNX0xJVkU= | base64 -d | xargs -I{} touch data/{}",
}


@pytest.mark.xfail(strict=True, reason="znana luka strażnika (best-effort)")
@pytest.mark.parametrize("text", KNOWN_GAPS.values(), ids=KNOWN_GAPS.keys())
def test_known_gap_is_still_a_gap(text):
    assert violates(text)


# --- zaszyte klucze API ---------------------------------------------------------------------------------------------

HARDCODED_KEYS = [
    "headers = {'X-CAP-API-KEY': 'abcd1234efgh5678'}",
    'headers = {"x-cap-api-key": "abcd1234efgh5678"}',
    "headers['X-CAP-API-KEY'] = 'abcd1234efgh5678'",
    "headers.update({'X-CAP-API-KEY': 'abcd1234efgh5678'})",
    "session.headers.add('X-CAP-API-KEY', 'abcd1234efgh5678')",
    "curl -H 'X-CAP-API-KEY: abcd1234efgh5678' https://api-capital.backend-capital.com/api/v1/session",
    'curl -H "X-CAP-API-KEY: abcd1234efgh5678" https://api-capital.backend-capital.com/api/v1/session',
    "X-CAP-API-KEY: abcd1234efgh5678",
    "api_key = 'abcd1234efgh5678'",
    'api_key="abcd1234efgh5678"',
    "API_KEY = 'ABCD1234EFGH5678'",
    "creds = Credentials(api_key='abcd1234efgh5678', identifier='x', api_password='y')",
    '{"api_key": "abcd1234efgh5678"}',
    'api_password: "Sup3rS3cretPass"',
    "api_password = 'Sup3rS3cretPass'",
    '"password": "Abcdef123456xyz"',
    "password = 'Abcdef123456xyz'",
    "ADMIN_PASSWORD = 'Abcdef123456xyz'",
    'api_key = "test-abcdef123456"',  # bez ścieżki prefiks `test-` nie jest wyjątkiem (wyjątek: pliki w tests/)
]


@pytest.mark.parametrize("text", HARDCODED_KEYS)
def test_hardcoded_keys_and_passwords_are_flagged(text):
    assert violates(text)


NOT_KEYS = [
    "headers = {'X-CAP-API-KEY': self._creds.api_key}",
    "headers = {'X-CAP-API-KEY': api_key}",
    "headers['X-CAP-API-KEY'] = creds.api_key",
    "session.headers.add('X-CAP-API-KEY', key)",
    'curl -H "X-CAP-API-KEY: $KEY" https://example.test',
    'curl -H "X-CAP-API-KEY: ${CAPITALCOM_API_KEY}" https://example.test',
    "curl -H 'X-CAP-API-KEY: <your key>' https://example.test",
    "curl -H 'X-CAP-API-KEY: <redacted>' https://example.test",
    "api_key = '<your-api-key>'",
    "api_key = '<redacted>'",
    "api_key = '…'",
    "api_key = '...'",
    "api_key = 'xxxxxxxxxxxxxxxx'",
    "api_key = 'XXXXXXXXXXXXXXXX'",
    "api_key = 'changeme'",
    "api_key = 'changeme-please-12345'",
    "api_key = 'placeholder-placeholder'",
    "api_key = ''",
    "api_key = 'abc'",
    "api_key = 'short123'",
    "password = os.environ['GW_PASSWORD']",
    "api_key = creds['api_key']",
    "'password': self._creds.api_password,",
    "f\"api_key={key}\"",
    "api_password = f'{value}abcdefghijklmnop'",
    "api_key = '%s' % value",
    '{"api_key": "<redacted>", "identifier": "<redacted>"}',
    "Credentials(api_key=<redacted>, identifier=<redacted>, api_password=<redacted>)",
    'LOGGED_HEADERS = ("X-CAP-API-KEY", "CST", "X-SECURITY-TOKEN")',
    "assert 'X-CAP-API-KEY' not in post.headers",
    "Klucz API idzie wyłącznie w nagłówku `X-CAP-API-KEY` i wyłącznie do POST /session.",
    'read -p "api_key: " KEY',
    'SECRET_KEYS = ("api_key", "identifier", "api_password")',
    'raise ValueError("secrets file /secrets/capitalcom.json: missing key api_key")',
    "# api_key = 'abcd1234efgh5678'",
    "pattern = re.compile(r'\"(api_password|api_key|identifier)\"\\s*:\\s*\"[^\"]+\"')",
]


@pytest.mark.parametrize("text", NOT_KEYS)
def test_references_placeholders_and_short_values_are_not_flagged(text):
    assert not violates(text)


def test_pathological_long_lines_do_not_blow_up_the_scan():
    """Regresja: wyrażenia o czasie kwadratowym wieszały skan na jednej długiej linii (hook ma limit 30 s)."""
    n = 20000
    texts = [
        "y = " + "abcdefghij" * n + "\n",
        "x = '" + "a_" * n + "'\n",
        "touch " * n + "\n",
        "f(" * n + ")" * n + "\n",
        "PASSWORD_" * n + "\n",
        "open(" * 2000 + ")" * 2000 + "\n",
        "(\n" * 30 + "x\n",
    ]
    started = time.monotonic()
    for text in texts:
        guard().scan_text(text)
    assert time.monotonic() - started < 10


# --- violates_in: ścieżki, przyrostki, wyjątki, live_allowed -------------------------------------------------------

SCANNED_FILES = [
    "src/gwstock/x.py",
    "scripts/run.sh",
    "scripts/run.bash",
    "config/x.toml",
    ".github/workflows/x.yml",
    "docker/x.yaml",
    "setup.cfg",
    "tox.ini",
    "docker/.env",
    "deploy.env",
    "docker/Dockerfile",
    "Makefile",
    # odwrócone: skanujemy wszystko poza prozą i binariami (przegląd M5)
    "scripts/arm",
    "docker/entrypoint",
    "Procfile",
    "justfile",
    "Containerfile",
    "scripts/run.zsh",
    "scripts/run.ps1",
    "deploy/gw.service",
    "deploy/gw.timer",
    "export/notes.json",
    "nb/analysis.ipynb",
    "notes/todo.txt",
    ".envrc",
    "src/gwstock/x.pyi",
]


@pytest.mark.parametrize("path", SCANNED_FILES)
def test_scanned_files_report_the_violation(path):
    reasons = guard().violates_in(path, "echo hi\ntouch data/ARM_LIVE\n")
    assert len(reasons) == 1
    assert "line 2" in reasons[0] and "ARM_LIVE" in reasons[0]


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CLAUDE.md",
        "docs/ops/runbook.md",
        ".claude/agents/risk-reviewer.md",
        ".claude/settings.json",
        "notes/readme.markdown",
        "image.png",
        "src/gwstock/__pycache__/x.cpython-312.pyc",
        "data/x.json",  # dane silnika (konto, dziennik), nie źródła
        "data/journal.sqlite",
    ],
)
def test_prose_binaries_and_engine_data_are_not_scanned(path):
    assert guard().violates_in(path, "touch data/ARM_LIVE\napi_key = 'abcd1234efgh5678'\n") == []


@pytest.mark.parametrize("path", ["docs/ops/helper.py", "docs/superpowers/plans/x.sh", "docs/x/config.toml"])
def test_docs_tree_is_prose_even_for_code_suffixes(path):
    assert guard().violates_in(path, "touch data/ARM_LIVE\n") == []


def test_the_key_rule_reports_the_key_and_the_line():
    [reason] = guard().violates_in("src/gwstock/x.py", "a = 1\nheaders = {'X-CAP-API-KEY': 'abcd1234efgh5678'}\n")
    assert "line 2" in reason and "X-CAP-API-KEY" in reason
    assert "abcd1234efgh5678" not in reason  # komunikat nie powtarza klucza (trafia do logów i rozmowy)


def test_a_clean_file_has_no_reasons():
    assert guard().violates_in("src/gwstock/x.py", "def f():\n    return 1\n") == []


@pytest.mark.parametrize("rel", sorted(("tests/unit/test_guard_live.py", "tests/fault/engine_sim.py",
                                        "tests/fault/test_modes.py", "tests/fault/test_engine_day.py",
                                        "scripts/hooks/guard_live.py")))
def test_the_allow_list_exempts_the_listed_paths_from_the_arming_rule(rel):
    text = "(data_dir / 'ARM_LIVE').write_text('')\n"
    assert guard().violates_in(rel, text) == []
    assert guard().violates_in(str(ROOT / rel), text) == []  # ścieżka bezwzględna z hooka, pod korzeniem repozytorium
    assert guard().violates_in(f"src/gwstock/{Path(rel).name}", text) != []  # ta sama treść gdzie indziej: naruszenie
    # plik o tej samej końcówce POZA repozytorium nie dostaje wyjątku (przegląd I3)
    assert guard().violates_in("/x/" + rel, text) != []
    assert guard().violates_in("C:\\project\\" + rel.replace("/", "\\"), text) != []


def test_the_allow_list_matches_whole_path_components_only():
    text = "(data_dir / 'ARM_LIVE').write_text('')\n"
    assert guard().violates_in("mytests/fault/engine_sim.py", text) != []
    assert guard().violates_in("tests/fault/engine_sim.py.bak.py", text) != []
    assert guard().violates_in("tests/fault/../../src/engine_sim.py", text) != []  # `..` jest rozwijane


def test_the_ci_workflow_and_settings_are_not_exempt_from_the_arming_rule():
    assert guard().violates_in(".github/workflows/ci.yml", "      - run: touch data/ARM_LIVE\n") != []


@pytest.mark.parametrize(
    "text",
    [
        "live_allowed = true\n",
        "live_allowed=true\n",
        "LIVE_ALLOWED = TRUE\n",
        "live_allowed    =    true\n",
        "\tlive_allowed = true # teraz\n",
        '"live_allowed" = true\n',
        "# komentarz\nlive_allowed = true\n# koniec\n",
    ],
)
@pytest.mark.parametrize("path", ["config/runtime.toml", "/srv/gw/config/runtime.toml", "C:\\gw\\config\\runtime.toml"])
def test_live_allowed_true_in_runtime_toml_is_a_violation(path, text):
    reasons = guard().violates_in(path, text)
    assert reasons and "live_allowed" in reasons[-1]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("config/runtime.toml", "live_allowed = false\n"),
        ("config/runtime.toml", "# live_allowed = true (tylko właściciel)\nlive_allowed = false\n"),
        ("config/runtime.toml", "live_allowed = true_story\n"),
        ("config/runtime.toml", 'account_id = "live_allowed = true"\nlive_allowed = false\n'),
        ("src/myconfig/runtime.toml", "live_allowed = true\n"),
        ("config/strategy_v1.toml", "live_allowed = true\n"),
        ("tests/unit/test_config.py", "(tmp_path / 'runtime.toml').write_text('live_allowed = true\\n')\n"),
        ("docs/ops/runbook.md", "live_allowed = true\n"),
    ],
)
def test_live_allowed_rule_is_limited_to_the_real_runtime_file(path, text):
    assert guard().violates_in(path, text) == []


def test_the_allow_list_has_no_stale_or_unneeded_entries():
    g = guard()
    for rel in sorted(g.ARMING_EXEMPT):
        assert (ROOT / rel).is_file(), f"{rel}: wpis na liście wyjątków bez pliku"
        findings = g.scan_text((ROOT / rel).read_text(encoding="utf-8"))
        assert any(f.rule == "arming" for f in findings), f"{rel}: wyjątek niepotrzebny (plik nic nie narusza)"
    assert g.KEY_EXEMPT == {"scripts/hooks/guard_live.py", "tests/unit/test_guard_live.py"}  # przegląd M1: adapter bez wyjątku
    for rel in sorted(g.KEY_EXEMPT - {"scripts/hooks/guard_live.py"}):  # nagłówek strażnika nie zawiera kluczy
        assert (ROOT / rel).is_file(), f"{rel}: wpis na liście wyjątków bez pliku"
        findings = g.scan_text((ROOT / rel).read_text(encoding="utf-8"), test_values_ok=True)
        assert any(f.rule == "key" for f in findings), f"{rel}: wyjątek niepotrzebny (plik nic nie narusza)"


def test_every_allow_listed_path_is_justified_in_the_header_of_the_guard():
    source = GUARD.read_text(encoding="utf-8")
    header = source[: source.index("from __future__")]
    for rel in sorted(guard().ARMING_EXEMPT | guard().KEY_EXEMPT):
        assert rel in header, f"{rel}: brak uzasadnienia w nagłówku guard_live.py"


def test_test_values_are_forgiven_only_under_tests():
    text = "api_key = 'test-abcdefghijkl'\n"
    assert guard().violates_in("src/gwstock/x.py", text) != []
    assert guard().violates_in("tests/unit/test_x.py", text) == []
    real_looking = "api_key = 'AbCd1234EfGh5678'\n"
    assert guard().violates_in("tests/unit/test_x.py", real_looking) != []  # prawdziwy klucz w testach też blokuje
    assert guard().violates_in("tests/unit/test_x.py", "KEY = 'TEST_ABCDEFGHIJKLMN'\napi_key = 'TEST_ABCDEFGHIJKLMN'\n") == []


def test_env_files_flag_unquoted_secret_assignments():
    g = guard()
    assert g.violates_in("docker/.env", "GW_UID=1000\nCAPITALCOM_API_KEY=abcd1234efgh5678\n") != []
    assert g.violates_in("docker/.env", "GW_PASSWORD=Abcdef123456xyz\n") != []
    assert g.violates_in("docker/.env", "GW_UID=1000\nGW_GID=1000\n") == []
    assert g.violates_in("docker/.env", "CAPITALCOM_API_KEY=<your key>\n") == []
    assert g.violates_in("src/gwstock/x.py", "PASSWORD = somevariablename123\n") == []  # w Pythonie to zmienna


# --- hook: stdin -> kod wyjścia ----------------------------------------------------------------------------------


def run_hook(
    payload=None, *, raw: str | None = None, cwd: Path | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    stdin = raw if raw is not None else json.dumps(payload)
    full_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}  # test nie dziedziczy cudzego korzenia
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(GUARD)], input=stdin, capture_output=True, text=True, cwd=cwd, env=full_env, timeout=30,
        check=False,
    )


def write_event(path: str, content: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def edit_event(path: str, old: str, new: str, **extra) -> dict:
    tool_input = {"file_path": path, "old_string": old, "new_string": new, **extra}
    return {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": tool_input}


def multi_event(path: str, edits: list) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit", "tool_input": {"file_path": path, "edits": edits}}


def test_write_with_a_violation_is_blocked_with_exit_2_and_a_bilingual_message():
    done = run_hook(write_event("/work/src/gwstock/x.py", "open('data/ARM_LIVE', 'w').close()\n"))
    assert done.returncode == 2
    assert done.stdout == ""
    assert "ARM_LIVE" in done.stderr and "line 1" in done.stderr
    assert "ZABLOKOWANO" in done.stderr and "BLOCKED" in done.stderr  # polski i angielski
    assert "właściciel" in done.stderr and "owner" in done.stderr


def test_write_without_a_violation_passes_silently():
    done = run_hook(write_event("/work/src/gwstock/x.py", "def f():\n    return (data / 'ARM_LIVE').exists()\n"))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_the_message_never_echoes_the_secret():
    done = run_hook(write_event("/work/src/gwstock/x.py", "api_key = 'AbCd1234EfGh5678'\n"))
    assert done.returncode == 2 and "AbCd1234EfGh5678" not in done.stderr and "api_key" in done.stderr


def test_edit_scans_the_new_string():
    blocked = run_hook(edit_event("/work/src/gwstock/x.py", "pass", "Path('data/L1_PASSED').touch()"))
    assert blocked.returncode == 2 and "L1_PASSED" in blocked.stderr
    assert run_hook(edit_event("/work/src/gwstock/x.py", "pass", "x = 1", replace_all=True)).returncode == 0


def test_edit_sees_an_alias_defined_earlier_in_the_file_on_disk(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("from pathlib import Path\narm_marker = Path('data') / 'ARM_LIVE'\n\n\ndef f():\n    pass\n")
    done = run_hook(edit_event(str(target), "    pass\n", "    arm_marker.touch()\n"))
    assert done.returncode == 2 and "ARM_LIVE" in done.stderr
    assert target.read_text().endswith("    pass\n")  # hook niczego nie zmienia na dysku


def test_edit_that_removes_a_violation_is_allowed(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("p = Path('data/ARM_LIVE')\np.touch()\nx = 1\n")
    assert run_hook(edit_event(str(target), "p.touch()\n", "p.exists()\n")).returncode == 0


def test_edit_with_an_old_string_missing_on_disk_falls_back_to_the_new_string(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")
    assert run_hook(edit_event(str(target), "nie ma tego", "touch data/ARM_LIVE")).returncode == 2
    assert run_hook(edit_event(str(tmp_path / "missing.py"), "a", "touch data/ARM_LIVE")).returncode == 2
    assert run_hook(edit_event(str(tmp_path / "missing.py"), "a", "x = 2")).returncode == 0


def test_multiedit_scans_every_edit():
    ok = {"old_string": "a", "new_string": "b = 1"}
    bad = {"old_string": "c", "new_string": "touch data/L1_PASSED"}
    assert run_hook(multi_event("/work/src/gwstock/x.py", [ok, ok])).returncode == 0
    assert run_hook(multi_event("/work/src/gwstock/x.py", [ok, bad])).returncode == 2
    assert run_hook(multi_event("/work/src/gwstock/x.py", [bad, ok])).returncode == 2
    assert run_hook(multi_event("/work/src/gwstock/x.py", [])).returncode == 0


def test_multiedit_edits_are_applied_in_order_against_the_file_on_disk(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("a = 1\nb = 2\n")
    first = {"old_string": "a = 1", "new_string": "marker = Path('data/ARM_LIVE')"}
    second = {"old_string": "b = 2", "new_string": "marker.touch()"}
    assert run_hook(multi_event(str(target), [first, second])).returncode == 2
    assert run_hook(multi_event(str(target), [first])).returncode == 0


def test_the_special_runtime_toml_rule_applies_to_hook_writes():
    assert run_hook(write_event("/work/config/runtime.toml", "live_allowed = true\n")).returncode == 2
    assert run_hook(write_event("/work/config/runtime.toml", "live_allowed = false\n")).returncode == 0
    done = run_hook(edit_event("/work/config/runtime.toml", "live_allowed = false", "live_allowed = true"))
    assert done.returncode == 2 and "live_allowed" in done.stderr


def test_edit_of_runtime_toml_is_judged_on_the_whole_resulting_file(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / "runtime.toml"
    target.write_text("# komentarz\nlive_allowed = false\n")
    assert run_hook(edit_event(str(target), "# komentarz", "# inny komentarz")).returncode == 0
    target.write_text("live_allowed = true\n")  # ktoś ustawił to poza hookiem; kolejna edycja nie może tego przepuścić
    assert run_hook(edit_event(str(target), "# brak", "# dopisek")).returncode == 0  # old_string nie pasuje: tylko new_string
    assert run_hook(edit_event(str(target), "live_allowed = true", "live_allowed = false")).returncode == 0


@pytest.mark.parametrize(
    "rel",
    [
        "tests/fault/engine_sim.py",
        "tests/fault/test_modes.py",
        "tests/fault/test_engine_day.py",
        "tests/unit/test_guard_live.py",
        "scripts/hooks/guard_live.py",
    ],
)
def test_allow_listed_paths_pass_the_hook(rel):
    assert run_hook(write_event(str(ROOT / rel), "(data_dir / ARM_FILE).write_text('')\n")).returncode == 0
    for foreign in ("/work/" + rel, "C:\\work\\" + rel.replace("/", "\\")):  # tylko pod korzeniem repozytorium
        assert run_hook(write_event(foreign, "(data_dir / ARM_FILE).write_text('')\n")).returncode == 2


@pytest.mark.parametrize(
    "path",
    [
        str(ROOT / "README.md"),
        str(ROOT / "docs/ops/runbook.md"),
        str(ROOT / "data/x.json"),
        "/work/README.md",
        "/work/docs/ops/runbook.md",
    ],
)
def test_unscanned_paths_pass_the_hook(path):
    assert run_hook(write_event(path, "touch data/ARM_LIVE\n")).returncode == 0


@pytest.mark.parametrize(
    "event",
    [
        {"tool_name": "Bash", "tool_input": {"command": "touch data/ARM_LIVE"}},
        {"tool_name": "Read", "tool_input": {"file_path": "/work/src/x.py"}},
        {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/work/n.ipynb", "new_source": "touch data/ARM_LIVE"}},
        {"tool_name": "mcp__github__create_or_update_file", "tool_input": {"path": "x.py", "content": "touch ARM_LIVE"}},
    ],
    ids=["bash", "read", "notebook-edit", "mcp"],
)
def test_other_tools_are_none_of_the_hooks_business(event):
    done = run_hook(event)
    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   \n",
        "to nie jest json",
        "{",
        "[1, 2, 3]",
        '"Write"',
        "null",
        "{}",
        '{"tool_name": "Write"}',
        '{"tool_name": 7, "tool_input": {}}',
        '{"tool_name": "Write", "tool_input": []}',
        '{"tool_name": "Write", "tool_input": {"content": "x = 1"}}',
        '{"tool_name": "Write", "tool_input": {"file_path": "", "content": "x = 1"}}',
        '{"tool_name": "Write", "tool_input": {"file_path": 5, "content": "x = 1"}}',
        '{"tool_name": "Write", "tool_input": {"file_path": "/w/x.py"}}',
        '{"tool_name": "Write", "tool_input": {"file_path": "/w/x.py", "content": 5}}',
        '{"tool_name": "Edit", "tool_input": {"file_path": "/w/x.py", "old_string": "a"}}',
        '{"tool_name": "MultiEdit", "tool_input": {"file_path": "/w/x.py"}}',
        '{"tool_name": "MultiEdit", "tool_input": {"file_path": "/w/x.py", "edits": "nie lista"}}',
        '{"tool_name": "MultiEdit", "tool_input": {"file_path": "/w/x.py", "edits": [1]}}',
        '{"tool_name": "MultiEdit", "tool_input": {"file_path": "/w/x.py", "edits": [{"old_string": "a"}]}}',
    ],
)
def test_malformed_hook_input_fails_closed(raw):
    done = run_hook(raw=raw)
    assert done.returncode == 2, done.stderr
    assert "fail-closed" in done.stderr and "ZABLOKOWANO" in done.stderr and "BLOCKED" in done.stderr


def test_invalid_utf8_on_stdin_fails_closed():
    done = subprocess.run([sys.executable, str(GUARD)], input=b"\xff\xfe{", capture_output=True, timeout=30, check=False)
    assert done.returncode == 2 and b"fail-closed" in done.stderr


def test_polish_text_and_unicode_content_are_handled():
    content = "# Zażółć gęślą jaźń — uzbrojenie tworzy właściciel\nx = 'ARM_LIVE sprawdzamy .exists()'\n"
    assert run_hook(write_event("/work/src/gwstock/x.py", content)).returncode == 0


def test_the_hook_does_not_depend_on_the_working_directory(tmp_path):
    done = run_hook(write_event("/work/src/gwstock/x.py", "touch data/ARM_LIVE\n"), cwd=tmp_path)
    assert done.returncode == 2 and "ZABLOKOWANO" in done.stderr and "line 1" in done.stderr
    assert run_hook(write_event("/work/src/gwstock/x.py", "x = 1\n"), cwd=tmp_path).returncode == 0


def test_running_on_a_terminal_without_input_prints_usage_and_does_not_block():
    done = subprocess.run([sys.executable, str(GUARD), "--help"], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0 and "--scan" in done.stdout


# --- tryb --scan (CI) -----------------------------------------------------------------------------------------------


def run_scan(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD), "--scan", *args], capture_output=True, text=True, cwd=cwd, timeout=120, check=False
    )


def test_scan_of_a_clean_tree_exits_0(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return (d / 'ARM_LIVE').exists()\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("touch data/ARM_LIVE\n")
    done = run_scan(str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr


def test_scan_reports_file_and_line_and_exits_1(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "arm.sh").write_text("#!/bin/sh\nset -e\ntouch data/ARM_LIVE\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "key.yml").write_text("a: 1\nheaders:\n  X-CAP-API-KEY: abcd1234efgh5678\n")
    done = run_scan(str(tmp_path))
    assert done.returncode == 1
    out = done.stdout + done.stderr
    assert re.search(r"scripts[/\\]arm\.sh:3: .*ARM_LIVE", out)
    assert re.search(r"key\.yml:3: .*X-CAP-API-KEY", out)
    assert "ok.py" not in out and "abcd1234efgh5678" not in out


def test_scan_skips_git_data_venv_and_note_directories(tmp_path):
    for name in (".git", ".superpowers", "data", "venv312", ".venv", "node_modules", "__pycache__", ".pytest_cache"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.py").write_text("touch data/ARM_LIVE\n")
    assert run_scan(str(tmp_path)).returncode == 0


def test_scan_applies_the_allow_list_by_path(tmp_path):
    (tmp_path / "tests" / "fault").mkdir(parents=True)
    (tmp_path / "tests" / "fault" / "engine_sim.py").write_text("(data_dir / ARM_FILE).write_text('')\n")
    assert run_scan(str(tmp_path)).returncode == 0
    (tmp_path / "tests" / "fault" / "other.py").write_text("(data_dir / ARM_FILE).write_text('')\n")
    assert run_scan(str(tmp_path)).returncode == 1


def test_scan_accepts_single_files_and_flags_runtime_toml(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "runtime.toml").write_text("live_allowed = true\n")
    done = run_scan(str(cfg / "runtime.toml"))
    assert done.returncode == 1 and "runtime.toml:1:" in done.stdout + done.stderr


def test_scan_of_a_missing_path_is_an_error_not_a_pass(tmp_path):
    done = run_scan(str(tmp_path / "nie-ma"))
    assert done.returncode == 2 and "nie-ma" in done.stderr


def test_scan_survives_unreadable_binary_content(tmp_path):
    (tmp_path / "blob.py").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    assert run_scan(str(tmp_path)).returncode == 0


def test_the_repository_itself_is_clean(tmp_path):
    """Test całego repozytorium: każde prawdziwe naruszenie w kodzie wywala build (CI robi to samo przed pytestem)."""
    done = run_scan(cwd=tmp_path)  # domyślny korzeń to repozytorium, niezależnie od katalogu roboczego
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_engine_sources_pass_the_text_guard_without_exemptions():
    g = guard()
    sources = sorted((ROOT / "src").rglob("*.py"))
    assert len(sources) > 20
    for path in sources:
        findings = g.scan_text(path.read_text(encoding="utf-8"))
        assert findings == [], f"{path.relative_to(ROOT)}: {findings}"
    cli_text = (ROOT / "src" / "gwstock" / "cli.py").read_text(encoding="utf-8")
    assert "ARM_FILE" in cli_text  # skan naprawdę widzi miejsca, które tylko sprawdzają


# --- .claude/settings.json --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_settings_register_the_pretooluse_guard(settings):
    groups = settings["hooks"]["PreToolUse"]
    assert len(groups) == 1
    [group] = groups
    for tool in ("Write", "Edit", "MultiEdit"):
        assert re.fullmatch(group["matcher"], tool), tool
    for tool in ("Read", "Bash", "Glob", "Grep"):
        assert not re.fullmatch(group["matcher"], tool), tool
    [hook] = group["hooks"]
    assert hook["type"] == "command"
    assert hook["onFailure"] == "block"  # dokumentacja hooków: błąd lub timeout hooka blokuje, zamiast przepuszczać
    assert isinstance(hook["timeout"], int) and 1 <= hook["timeout"] <= 60


def test_the_hook_command_points_at_an_existing_script(settings):
    [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
    assert hook["command"] == "python3" and "shell" not in hook  # postać exec: bez powłoki, bez cytowania
    [script] = hook["args"]
    assert script.startswith("${CLAUDE_PROJECT_DIR}/")
    assert (ROOT / script.removeprefix("${CLAUDE_PROJECT_DIR}/")) == GUARD and GUARD.is_file()


def test_the_configured_hook_command_blocks_end_to_end(settings):
    [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
    argv = [hook["command"].replace("python3", sys.executable)] + [
        a.replace("${CLAUDE_PROJECT_DIR}", str(ROOT)) for a in hook["args"]
    ]
    bad = subprocess.run(argv, input=json.dumps(write_event("/w/src/x.py", "touch data/ARM_LIVE")),
                         capture_output=True, text=True, timeout=30, check=False)
    good = subprocess.run(argv, input=json.dumps(write_event("/w/src/x.py", "x = 1")),
                          capture_output=True, text=True, timeout=30, check=False)
    assert (bad.returncode, good.returncode) == (2, 0)


def test_settings_have_only_hooks_and_permissions(settings):
    assert set(settings) <= {"$schema", "hooks", "permissions"}  # bez model, env i innych
    assert set(settings["hooks"]) == {"PreToolUse"}
    assert set(settings["permissions"]) == {"deny", "ask"}


DENY_RULE = re.compile(r"^(Read|Edit|Bash)\(.+\)$")


def test_deny_rules_are_well_formed_unique_and_cover_secrets_and_arming_files(settings):
    deny = settings["permissions"]["deny"]
    assert len(deny) == len(set(deny)) and all(DENY_RULE.match(rule) for rule in deny)
    for needed in (
        "Read(//home/claude/.config/gw/**)",  # `//` = ścieżka bezwzględna (dokumentacja uprawnień)
        "Read(~/.config/gw/**)",
        "Read(**/capitalcom*.json)",
        "Read(**/.env)",
        "Read(/data/account.json)",  # `/` = od korzenia projektu
        "Edit(**/ARM_LIVE)",
        "Edit(**/L1_PASSED)",
        "Bash(touch *ARM_LIVE*)",
        "Bash(touch *L1_PASSED*)",
        "Bash(*>*ARM_LIVE*)",
        "Bash(*>*L1_PASSED*)",
        "Bash(tee *ARM_LIVE*)",
        "Bash(tee *L1_PASSED*)",
        "Bash(*touch *ARM_LIVE*)",  # także za opakowaniem: sh -c, docker exec, sudo
        "Bash(*touch *L1_PASSED*)",
        "Bash(*python* -c*ARM_LIVE*)",
        "Bash(*python* -c*L1_PASSED*)",
        "Bash(sed -i*runtime.toml*)",
        "Bash(*>*runtime.toml*)",
        "Edit(/.claude/settings.local.json)",  # lokalny plik mógłby wyłączyć hooki (`disableAllHooks`)
    ):
        assert needed in deny, needed


def test_deny_rules_do_not_block_routine_work(settings):
    deny = " ".join(settings["permissions"]["deny"])
    assert "Bash(*ARM_LIVE*)" not in deny  # grep/ls/rm na tych plikach zostają dozwolone
    assert "Read(/data/**)" not in deny and "Read(**/*.py)" not in deny


def test_settings_file_passes_the_guard_it_configures():
    assert guard().violates_in(".claude/settings.json", SETTINGS.read_text(encoding="utf-8")) == []


# --- agenci tylko do odczytu --------------------------------------------------------------------------------------------

AGENTS = {
    "risk-reviewer": ("ARM_LIVE", "L1_PASSED", "min_size", "live_allowed"),
    "order-state-auditor": ("INTENT", "UNKNOWN", "record_trade"),
    "backtest-skeptic": ("K", "SHA-256", "docs/preregistration/v1.md"),
}
READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}


def split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: frontmatter musi zaczynać się w pierwszej linii"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path.name}: brak zamknięcia frontmatter"
    meta = yaml.safe_load(head)
    assert isinstance(meta, dict)
    return meta, body


def test_exactly_the_three_reviewer_agents_exist():
    assert sorted(p.name for p in AGENTS_DIR.glob("*.md")) == sorted(f"{name}.md" for name in AGENTS)


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_frontmatter_is_valid_and_read_only(name):
    meta, body = split_frontmatter(AGENTS_DIR / f"{name}.md")
    assert set(meta) <= {"name", "description", "tools", "model"}
    assert meta["name"] == name
    assert isinstance(meta["description"], str) and len(meta["description"]) >= 60
    tools = meta["tools"]
    listed = {t.strip() for t in tools.split(",")} if isinstance(tools, str) else set(tools)
    assert listed == READ_ONLY_TOOLS  # żadnego Write, Edit, MultiEdit, NotebookEdit, Bash ani narzędzi MCP
    assert len(body.strip()) >= 1500


PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:src|tests|docs|config|docker|scripts|\.claude|\.github)/[\w.*/-]*[\w*/])")


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_charters_cite_real_paths_and_the_real_invariants(name):
    _, body = split_frontmatter(AGENTS_DIR / f"{name}.md")
    tokens = sorted(set(PATH_TOKEN.findall(body)))
    assert len(tokens) >= 3
    missing = [t for t in tokens if not (list(ROOT.glob(t)) if "*" in t else [ROOT / t] if (ROOT / t).exists() else [])]
    assert missing == [], f"{name}: ścieżki, których nie ma w repozytorium: {missing}"
    for needle in AGENTS[name]:
        assert needle in body, f"{name}: brak odwołania do {needle}"


def test_agent_charters_forbid_changes_in_words_too():
    for name in AGENTS:
        _, body = split_frontmatter(AGENTS_DIR / f"{name}.md")
        assert "tylko do odczytu" in body.lower() and "nie " in body.lower()


def test_agent_charters_treat_repository_text_as_untrusted_data():
    for name in AGENTS:
        _, body = split_frontmatter(AGENTS_DIR / f"{name}.md")
        assert "niezaufane" in body and "nigdy instrukcj" in body, name  # przegląd M9


def test_agent_files_pass_the_guard():
    for path in AGENTS_DIR.glob("*.md"):
        assert guard().violates_in(str(path), path.read_text(encoding="utf-8")) == []


# --- .github/workflows/ci.yml ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


def triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # PyYAML czyta klucz `on` jako True


def all_steps(ci_doc: dict):
    for job_name, job in ci_doc["jobs"].items():
        for step in job["steps"]:
            yield job_name, step


def test_ci_triggers_and_least_privilege(ci):
    assert set(triggers(ci)) == {"push", "pull_request", "workflow_dispatch"}
    # push: main oraz gałęzie feat/** (praca przed PR też dostaje CI, w tym zadanie `image`); żadnych innych gałęzi
    assert triggers(ci)["push"] == {"branches": ["main", "feat/**"]}
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        assert "permissions" not in job  # żadne zadanie nie poszerza uprawnień


def test_ci_cancels_superseded_runs(ci):
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"  # main nie anuluje
    assert "github.ref" in ci["concurrency"]["group"] and "github.workflow" in ci["concurrency"]["group"]


def test_ci_has_no_secrets_and_no_privileged_triggers():
    raw = CI_FILE.read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "secrets" not in lowered and "pull_request_target" not in lowered
    assert "workflow_run" not in lowered and "id-token" not in lowered
    assert "github_token" not in lowered and "gh_token" not in lowered
    assert "environment:" not in lowered and "self-hosted" not in lowered
    assert "capitalcom" not in lowered and "api_key" not in lowered


def test_ci_jobs_have_timeouts_and_pinned_runners(ci):
    assert set(ci["jobs"]) == {"tests", "image"}
    for name, job in ci["jobs"].items():
        assert isinstance(job["timeout-minutes"], int) and 1 <= job["timeout-minutes"] <= 60, name
        assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), name  # bez ruchomego `ubuntu-latest`


def test_ci_external_actions_are_pinned_to_full_commit_shas():
    raw = CI_FILE.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", raw, flags=re.M)
    assert {u.split("@")[0] for u, _ in uses} == {"actions/checkout", "actions/setup-python"}
    for ref, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref}: wymagany pełny skrót commita"
        assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+\s*", rest), f"{ref}: po skrócie wymagany komentarz z tagiem"


def test_ci_tests_job_matrix_and_commands(ci):
    job = ci["jobs"]["tests"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.12", "3.13"]
    assert job["strategy"]["fail-fast"] is False
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert "python -m pip install -r requirements-dev.lock" in runs
    assert "python scripts/hooks/guard_live.py --scan" in runs
    pytest_cmd = next(r for r in runs if "pytest" in r)
    assert 'python -m pytest -m "not integration_live_readonly"' in pytest_cmd and "-W error" in pytest_cmd
    assert runs.index("python scripts/hooks/guard_live.py --scan") < runs.index(pytest_cmd)  # skan przed testami
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_ci_never_runs_the_live_readonly_tests():
    raw = CI_FILE.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if "integration_live_readonly" in line and not line.lstrip().startswith("#"):
            assert "not integration_live_readonly" in line, line
    assert "tests/integration_live_readonly" not in raw and "live_readonly/" not in raw


def test_ci_image_job_builds_the_test_target_and_runs_it_offline(ci):
    runs = [step["run"] for step in ci["jobs"]["image"]["steps"] if "run" in step]
    joined = "\n".join(runs)
    assert re.search(r"docker build .*-f docker/Dockerfile .*--target test", joined) or re.search(
        r"docker build .*--target test .*-f docker/Dockerfile", joined
    )
    assert re.search(r"docker run .*--network none .*gw-stock-engine:test", joined)
    assert re.search(r"--target runtime", joined)
    assert 'python -c "import gwstock; import gwstock.calendar"' in joined
    assert re.search(r"docker run .*--network none .*gw-stock-engine:runtime .*python -c", joined)


def test_ci_file_passes_the_guard_without_an_exemption():
    assert guard().violates_in(".github/workflows/ci.yml", CI_FILE.read_text(encoding="utf-8")) == []
    assert ".github/workflows/ci.yml" not in guard().ARMING_EXEMPT | guard().KEY_EXEMPT


# --- CLAUDE.md ---------------------------------------------------------------------------------------------------------------


def test_claude_md_documents_the_guard():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for needle in (
        "Strażnik",
        "python scripts/hooks/guard_live.py --scan",
        "ARM_LIVE",
        "L1_PASSED",
        "live_allowed",
        "scripts/hooks/guard_live.py",
        "tests/fault/engine_sim.py",
        "2.1.295",  # minimalna wersja dla onFailure (przegląd M2)
        "claude doctor",
        "/hooks",
        "korzeniu repozytorium",
        "zaufania",
        "chmurowych",
        "CODEOWNERS",
        "branch protection",
    ):
        assert needle in text, needle
    assert "tests/unit/test_capitalcom_adapter.py" not in text  # przegląd M1: bez wyjątku dla testów adaptera


# =====================================================================================================================
# Poprawki po przeglądzie (runda 1): I1-I4, M1-M9
# =====================================================================================================================

# --- I3: ścieżki są najpierw względem korzenia repozytorium --------------------------------------------------------------


def test_paths_outside_the_repository_get_the_full_rules_even_below_docs_or_tests():
    g = guard()
    violation = "open('data/ARM_LIVE', 'w')\n"
    for foreign in ("/srv/docs/gw/src/x.py", "/home/tests/gw/src/x.py", "/x/tests/fault/engine_sim.py", "/x/docs/y.py"):
        assert g.violates_in(foreign, violation) != [], foreign
    assert g.violates_in("/srv/docs/gw/src/x.py", "api_key = 'Zx9fLq2mTn8bVc4d'\n") != []
    assert g.violates_in("/home/tests/gw/src/x.py", "password = 'test-Zx9fLq2mTn8bVc4d'\n") != []  # nie jest testem repo
    # pod korzeniem repozytorium wyjątki działają jak dawniej (ścieżka bezwzględna z hooka)
    assert g.violates_in(str(ROOT / "docs/ops/x.py"), violation) == []
    assert g.violates_in(str(ROOT / "tests/fault/engine_sim.py"), "(d / ARM_FILE).touch()\n") == []
    assert g.violates_in(str(ROOT / "src/gwstock/x.py"), violation) != []
    assert g.violates_in(str(ROOT / "tests/unit/test_x.py"), "password = 'test-Zx9fLq2mTn8bVc4d'\n") == []
    # katalogi o nazwach z repozytorium, ale u korzenia systemu plików, też są poza nim
    for rooted in ("/tests/fault/engine_sim.py", "/docs/ops/x.py", "/data/export.json"):
        assert g.violates_in(rooted, "(d / ARM_FILE).touch()\n") != [], rooted
    assert g.violates_in("/tests/unit/test_x.py", "password = 'test-Zx9fLq2mTn8bVc4d'\n") != []
    # względna ścieżka z `..` wychodząca z repozytorium nie dziedziczy wyjątków
    assert g.violates_in("../tests/fault/engine_sim.py", "(d / ARM_FILE).touch()\n") != []


def test_the_hook_repro_from_the_review_is_blocked():
    for path in ("/srv/docs/gw/src/x.py", "/x/tests/fault/engine_sim.py", "/home/tests/gw/src/x.py"):
        done = run_hook(write_event(path, 'open("data/ARM_LIVE","w")\n'))
        assert done.returncode == 2, path
    assert run_hook(write_event("/srv/docs/gw/src/x.py", "api_key='Zx9fLq2mTn8bVc4d'\n")).returncode == 2


def test_a_checkout_below_a_directory_named_docs_or_tests_is_still_guarded(tmp_path):
    root = tmp_path / "docs" / "tests" / "gw"
    hook = root / "scripts" / "hooks" / "guard_live.py"
    hook.parent.mkdir(parents=True)
    shutil.copy(GUARD, hook)

    def run(rel: str, text: str) -> int:
        done = subprocess.run([sys.executable, str(hook)], input=json.dumps(write_event(str(root / rel), text)),
                              capture_output=True, text=True, timeout=30, check=False,
                              env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
        return done.returncode

    assert run("src/gwstock/x.py", "open('data/ARM_LIVE', 'w')\n") == 2
    assert run("src/gwstock/y.py", "api_key = 'test-Zx9fLq2mTn8bVc4d'\n") == 2  # `tests` w ścieżce nie czyni z src/ testu
    assert run("docs/ops/z.py", "touch data/ARM_LIVE\n") == 0  # docs/ liczone od korzenia: nadal proza
    assert run("tests/fault/engine_sim.py", "(data_dir / ARM_FILE).write_text('')\n") == 0


def test_claude_project_dir_is_honoured_only_when_it_holds_the_guard(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts" / "hooks").mkdir(parents=True)
    (root / "scripts" / "hooks" / "guard_live.py").write_text("# stand-in\n")
    other = tmp_path / "other"
    other.mkdir()
    event = write_event(str(root / "tests" / "fault" / "engine_sim.py"), "(data_dir / ARM_FILE).write_text('')\n")
    assert run_hook(event, env={"CLAUDE_PROJECT_DIR": str(root)}).returncode == 0  # korzeń z hooka jest spójny
    assert run_hook(event).returncode == 2  # bez zmiennej to plik spoza repozytorium
    assert run_hook(event, env={"CLAUDE_PROJECT_DIR": str(other)}).returncode == 2  # niespójny: bez strażnika w środku
    assert run_hook(event, env={"CLAUDE_PROJECT_DIR": ""}).returncode == 2
    assert run_hook(event, env={"CLAUDE_PROJECT_DIR": "relative/dir"}).returncode == 2


# --- pliki uzbrojenia zapisywane po nazwie (Write na data/ARM_LIVE) ------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_writing_an_arming_file_by_its_name_is_blocked(name):
    path = f"/work/data/{name}"
    events = [
        write_event(path, ""),
        edit_event(path, "a", "b"),
        multi_event(path, [{"old_string": "a", "new_string": "b"}]),
    ]
    for event in events:
        done = run_hook(event)
        assert done.returncode == 2 and name in done.stderr, event["tool_name"]
    assert run_hook(write_event(f"/work/data/{name}.bak", "")).returncode == 0  # inny plik
    assert run_hook(write_event(f"/work/data/not_{name}", "")).returncode == 0


def test_scan_flags_a_stray_arming_file_but_not_the_engine_data_directory(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ARM_LIVE").write_text("")  # runtime właściciela, pomijany jak cały katalog danych
    assert run_scan(str(tmp_path)).returncode == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "L1_PASSED").write_text("")
    done = run_scan(str(tmp_path))
    assert done.returncode == 1 and "L1_PASSED" in done.stdout


# --- M5: skanujemy wszystko poza prozą i binariami -----------------------------------------------------------------------


def test_content_with_nul_bytes_is_binary_whatever_the_suffix():
    assert guard().violates_in("export/blob.dat", "\x00\x01touch data/ARM_LIVE\n") == []
    assert guard().violates_in("export/blob.dat", "touch data/ARM_LIVE\n") != []


def test_scan_reads_extensionless_and_unusual_files_but_skips_binaries(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "arm").write_text("#!/bin/sh\ntouch data/ARM_LIVE\n")
    done = run_scan(str(tmp_path))
    assert done.returncode == 1 and re.search(r"arm:2: .*ARM_LIVE", done.stdout)
    (tmp_path / "scripts" / "arm").unlink()
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01touch data/ARM_LIVE\n\x00")
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01touch data/ARM_LIVE\n\x00")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n touch data/ARM_LIVE")
    (tmp_path / "notes.md").write_text("touch data/ARM_LIVE\n")
    assert run_scan(str(tmp_path)).returncode == 0


# --- M4: pusty literał nie znika --------------------------------------------------------------------------------------------


def test_empty_string_literals_survive_normalisation():
    assert not violates("p.replace('ARM_LIVE', '')")
    assert not violates('p.replace("L1_PASSED", "")')
    assert violates("open('data/' 'ARM_LIVE', 'w')")  # sklejanie sąsiednich literałów nadal działa
    assert violates("open('data/' + \"ARM_\" + 'LIVE', 'w')")  # także mieszane cudzysłowy


# --- I2: klucze bez cudzysłowu, hasła z symbolami, zmienne środowiskowe ----------------------------------------------------

HARDCODED_KEYS_FIX1 = [
    "export CAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d",
    "export CAPITALCOM_API_KEY='Zx9fLq2mTn8bVc4d'",
    "environment:\n  - CAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d",
    "environment:\n  CAPITALCOM_API_KEY: Zx9fLq2mTn8bVc4d",
    "env:\n  API_KEY: Zx9fLq2mTn8bVc4d",
    "ENV CAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d",
    "ENV CAPITALCOM_API_KEY Zx9fLq2mTn8bVc4d",
    "ARG API_PASSWORD=Tr0ub4dor3xKcdQz",
    "API_KEY := Zx9fLq2mTn8bVc4d",
    "api-key: Zx9fLq2mTn8bVc4d",
    "apiKey: Zx9fLq2mTn8bVc4d",
    "docker run -e CAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d img",
    "docker run --env API_PASSWORD=Tr0ub4dor3xKcdQz img",
    "docker build --build-arg API_KEY=Zx9fLq2mTn8bVc4d .",
    "docker login --password Tr0ub4dor3xKcdQz",
    "tool --api-key=Zx9fLq2mTn8bVc4d run",
    "CAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d python -m gwstock",
    "PASSWORD=hunter2hunter2xx",
    "secret: Zx9fLq2mTn8bVc4dxx",
    "password = 'Tr0ub4dor&3xKcdQ'",
    "password = 'correct-horse-battery-staple'",
    "api_password: 'correct horse battery'",
    '"password": "p@ss-w0rd!#2024xyz"',
    "os.environ['CAPITALCOM_API_KEY'] = 'Zx9fLq2mTn8bVc4d'",
    'os.environ["GW_API_PASSWORD"] = "Tr0ub4dor&3xKcdQ"',
    "os.environ.setdefault('X_API_KEY', 'Zx9fLq2mTn8bVc4d')",
    "os.putenv('X_API_KEY', 'Zx9fLq2mTn8bVc4d')",
    "monkeypatch.setenv('CAPITALCOM_API_KEY', 'Zx9fLq2mTn8bVc4d')",
    "monkeypatch.setenv('GW_API_PASSWORD', 'Tr0ub4dor&3xKcdQ')",
]


@pytest.mark.parametrize("text", HARDCODED_KEYS_FIX1)
def test_more_hardcoded_secret_forms_are_flagged(text):
    assert violates(text)


NOT_KEYS_FIX1 = [
    "export CAPITALCOM_API_KEY=$CAPITALCOM_API_KEY",
    'export API_KEY="${API_KEY}"',
    "- CAPITALCOM_API_KEY=${CAPITALCOM_API_KEY}",
    "API_KEY: ${{ secrets.API_KEY }}",
    "password: ${{ secrets.PW }}",
    "ENV API_KEY=",
    "ENV API_KEY=changeme",
    "export API_KEY=xxxxxxxxxxxxxxxx",
    "API_KEY=<your key>",
    'export API_KEY=""',
    "docker run -e CAPITALCOM_API_KEY img",
    "docker run -e CAPITALCOM_API_KEY=$KEY img",
    "docker login --password-stdin",
    "docker login --password $PW",
    "tool --api-key=${API_KEY} run",
    "export CAPITALCOM_API_KEY=$CAPITALCOMEXCHANGEKEY",
    "- GW_PASSWORD=${GW_PASSWORD_FROM_VAULT}",
    'PASSWORD="$(cat /run/secrets/password-file)"',
    'api_key = "${CAPITALCOMEXCHANGEKEY}"',
    "password = os.environ['GW_PASSWORD']",
    "password = os.environ.get('GW_PASSWORD', '')",
    "api_password = self._creds.api_password",
    "api_key = creds['api_key']",
    "os.environ['X_API_KEY'] = key",
    "os.environ['X_API_KEY'] = os.environ['Y_API_KEY']",
    "os.environ['GW_PASSWORD'] = ''",
    "monkeypatch.setenv('GW_CAPITALCOM_KEY_FILE', '/secrets/capitalcom.json')",
    "monkeypatch.setenv('GW_DATA_DIR', str(tmp_path / 'data'))",
    "monkeypatch.setenv('GW_API_KEY', '<redacted>')",
    "GW_CAPITALCOM_KEY_FILE: /secrets/capitalcom.json",
    "password_file: /run/secrets/password-file-path",
    "--password-file /run/secrets/password-file-path",
    "secretName: my-long-secret-name-here",
    "PASSWORD_MIN_LENGTH: 12345678901234",
    "password = 'short-pass'",
    "password = ''",
    "password: hunter2",
    "# export API_KEY=Zx9fLq2mTn8bVc4d",
]


@pytest.mark.parametrize("text", NOT_KEYS_FIX1)
def test_references_placeholders_and_lookups_stay_clean_in_the_new_secret_forms(text):
    assert not violates(text)


def test_unquoted_secret_assignments_are_flagged_by_file_type_and_python_stays_quoted_only():
    g = guard()
    for path in ("scripts/run.sh", "docker/compose.yml", ".github/workflows/x.yml", "docker/Dockerfile", "Makefile"):
        assert g.violates_in(path, "KEY=1\nCAPITALCOM_API_KEY=Zx9fLq2mTn8bVc4d\n") != [], path
    assert g.violates_in("src/gwstock/x.py", "PASSWORD = somevariablename123\n") == []
    assert g.violates_in("config/x.toml", "api_key = Zx9fLq2mTn8bVc4d\n") == []  # w TOML bez cudzysłowu to nie napis
    assert g.violates_in("config/x.toml", "api_key = 'Zx9fLq2mTn8bVc4d'\n") != []


def test_the_new_secret_forms_forgive_test_values_only_under_tests():
    g = guard()
    for text in (
        "monkeypatch.setenv('CAPITALCOM_API_KEY', 'test-Zx9fLq2mTn8bVc4d')\n",
        "os.environ['GW_API_PASSWORD'] = 'fake-Tr0ub4dor&3xKcdQ'\n",
        "password = 'mock-password-value-1'\n",
    ):
        assert g.violates_in("tests/unit/test_x.py", text) == [], text
        assert g.violates_in("src/gwstock/x.py", text) != [], text
    assert g.violates_in("tests/unit/test_x.py", "password = 'Tr0ub4dor&3xKcdQ'\n") != []


def test_fixture_names_with_a_test_prefix_are_forgiven_only_under_tests():
    g = guard()
    text = 'TEST_PASSWORD = "s3cr3t-api-pa55word"\n'
    assert g.violates_in("tests/unit/fake_server.py", text) == []
    assert g.violates_in("src/gwstock/x.py", text) != []
    assert g.violates_in("tests/unit/fake_server.py", 'MY_PASSWORD = "s3cr3t-api-pa55word"\n') != []
    assert g.violates_in("tests/unit/fake_server.py", 'TEST_PASSWORD = "Tr0ub4dor&3xKcdQ"\nPASSWORD = "Tr0ub4dor&3xKcdQ"\n') != []


def test_the_adapter_test_fixtures_are_scanned_like_any_other_file():
    path = "tests/unit/test_capitalcom_adapter.py"
    assert path not in guard().KEY_EXEMPT
    assert guard().violates_in(path, (ROOT / path).read_text(encoding="utf-8")) == []


# --- M6: tekstowa reguła dla live_allowed w skryptach --------------------------------------------------------------------------

RUNTIME_WRITERS = [
    "Path('config/runtime.toml').write_text('live_allowed = true\\n')",
    "open('config/runtime.toml', 'w').write('live_allowed = true')",
    "p = Path('config') / 'runtime.toml'\np.write_text('live_allowed = true\\n')",
    "RUNTIME = 'config/runtime.toml'\nwith open(RUNTIME, 'a') as fh:\n    fh.write('live_allowed = true\\n')",
    "sed -i 's/live_allowed = false/live_allowed = true/' config/runtime.toml",
    "sed -i 's/false/true/;s/live_allowed/live_allowed/' config/runtime.toml  # live_allowed",
    "echo 'live_allowed = true' >> config/runtime.toml",
    "printf 'live_allowed = true\\n' > config/runtime.toml",
    "printf 'live_allowed = true\\n' | tee config/runtime.toml",
    "python3 -c \"open('config/runtime.toml','w').write('live_allowed = true')\"",
    "doc['live_allowed'] = True",
    'table["live_allowed"] = true',
]


@pytest.mark.parametrize("text", RUNTIME_WRITERS)
@pytest.mark.parametrize("path", ["src/gwstock/flip.py", "scripts/flip.sh", ".github/workflows/flip.yml"])
def test_scripts_that_write_live_allowed_true_into_runtime_toml_are_flagged(path, text):
    reasons = guard().violates_in(path, text)
    assert reasons and any("live_allowed" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "text",
    [
        "echo 'live_allowed = false' >> config/runtime.toml",
        "grep '^live_allowed' config/runtime.toml",
        "cat config/runtime.toml",
        "# sed -i 's/false/true/' config/runtime.toml  live_allowed",
        "runtime = load_runtime(config_dir, data_dir)\nif runtime.live_allowed is not True:\n    missing.append('live_allowed')",
        "missing.append('live_allowed is not true in config/runtime.toml')",
        "p = Path('config/runtime.toml')\ntext = p.read_text()",
        "live_allowed = raw['live_allowed']",
        'raw = {"live_allowed": True}  # fixture w pamięci',
    ],
)
def test_reading_and_describing_live_allowed_is_not_a_flip(text):
    assert guard().violates_in("src/gwstock/x.py", text) == []
    assert guard().violates_in("scripts/x.sh", text) == [] or "sed -i" in text or ">>" in text


def test_tests_may_write_live_allowed_true_to_temporary_configs():
    text = "(tmp_path / 'runtime.toml').write_text('live_allowed = true\\n')\n"
    assert guard().violates_in("tests/unit/test_config.py", text) == []
    assert guard().violates_in(str(ROOT / "tests/fault/test_x.py"), text) == []
    assert guard().violates_in("src/gwstock/x.py", text) != []


@pytest.mark.parametrize("key", ["live_allowed", "'live_allowed'", '"live_allowed"'])
def test_every_toml_spelling_of_the_key_is_caught(key):
    assert guard().violates_in("config/runtime.toml", f"{key} = true\n") != []
    assert guard().violates_in("config/runtime.toml", f"{key}=true # teraz\n") != []
    assert guard().violates_in("config/runtime.toml", f"{key} = false\n") == []


# --- I1/M7: znacznik obrazu testowego -----------------------------------------------------------------------------------------


def test_the_test_stage_of_the_dockerfile_sets_the_marker_and_the_runtime_stage_does_not():
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    test_stage, runtime_stage = text.split("FROM base AS test")[1].split("FROM base AS runtime")
    assert re.search(r"^ENV GW_TEST_IMAGE=1$", test_stage, flags=re.M)
    assert "GW_TEST_IMAGE" not in runtime_stage and "GW_TEST_IMAGE" not in text.split("FROM base AS test")[0]


def test_tests_that_need_repository_files_skip_only_on_the_explicit_marker():
    for rel in ("tests/unit/test_guard_live.py", "tests/unit/test_prereg.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert 'os.environ.get("GW_TEST_IMAGE") == "1"' in source, rel
        assert "(ROOT / " + '"docs").is_dir()' not in source, rel  # żadnego cichego pomijania po braku katalogu


# --- M8: reszta CI -----------------------------------------------------------------------------------------------------------------


def test_dependabot_updates_the_pinned_actions_weekly():
    doc = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert doc["version"] == 2
    [update] = doc["updates"]
    assert update["package-ecosystem"] == "github-actions" and update["directory"] == "/"
    assert update["schedule"]["interval"] == "weekly"


def test_the_session_check_for_real_arming_files_lives_in_conftest():
    from tests.conftest import arming_files_created

    assert arming_files_created(Path("/nonexistent-data-dir"), frozenset()) == []
    data = ROOT / "tests" / "__no_such_data__"  # tylko ścieżka; nic nie tworzymy w repozytorium
    assert arming_files_created(data, frozenset()) == []
    source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'scope="session"' in source and "autouse=True" in source


def test_arming_files_created_during_the_run_are_reported_but_pre_existing_ones_are_not(tmp_path):
    from tests.conftest import arming_files_created

    assert arming_files_created(tmp_path, frozenset()) == []
    (tmp_path / "ARM_LIVE").write_text("")  # tak wygląda plik właściciela albo test, który zapomniał o tmp_path
    (tmp_path / "L1_PASSED").write_text("")
    assert arming_files_created(tmp_path, frozenset()) == ["ARM_LIVE", "L1_PASSED"]
    assert arming_files_created(tmp_path, frozenset({"ARM_LIVE"})) == ["L1_PASSED"]
    assert arming_files_created(tmp_path, frozenset({"ARM_LIVE", "L1_PASSED"})) == []


# --- I4: ochrona samego strażnika -----------------------------------------------------------------------------------------------

ASK_RULES = [
    "Edit(/scripts/hooks/**)",
    "Edit(/tests/unit/test_guard_live.py)",
    "Edit(/.github/**)",
    "Edit(/.claude/**)",
    "Edit(/config/runtime.toml)",
]


def test_editing_the_guard_ci_and_settings_needs_confirmation(settings):
    ask = settings["permissions"]["ask"]
    assert ask == ASK_RULES
    assert all(DENY_RULE.match(rule) for rule in ask)
    assert "Edit(/.claude/settings.local.json)" in settings["permissions"]["deny"]  # lokalny plik: zakaz, nie pytanie


def test_codeowners_names_the_repository_owner():
    lines = [ln.split() for ln in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert ["*", "@gwgrafik"] in lines  # właściciel z adresu origin: github.com/gwgrafik
    for pattern in ("/scripts/hooks/", "/.claude/", "/.github/", "/config/runtime.toml", "/tests/unit/test_guard_live.py"):
        assert [pattern, "@gwgrafik"] in lines, pattern
