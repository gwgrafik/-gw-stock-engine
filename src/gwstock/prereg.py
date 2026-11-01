"""Pre-rejestracja (spec §11): odcisk SHA-256 konfiguracji, zamrożenie go w `data/preregistration.json` i kontrola.

Odcisk jest jednym z czterech warunków uzbrojenia `live` (spec §9): `gwstock.cli` porównuje `config_fingerprint`
z zamrożonym `frozen_fingerprint`. Zmiana któregokolwiek bajtu zamrożonych plików, adaptera albo klasy instrumentu
to NOWY WARIANT (K+1), więc `write_preregistration` odmawia nadpisania innego odcisku bez `force`.

Kodowanie odcisku (`fingerprint`), identyczne na każdej maszynie (bez ścieżek bezwzględnych i czasu modyfikacji):

    SHA-256( b"gwstock-prereg/1\\n" | liczba plików (8 bajtów, big-endian)
             | dla każdego pliku, w podanej kolejności: pole(NAZWA PLIKU utf-8) | pole(surowe bajty pliku)
             | pole(adapter utf-8) | pole(klasa instrumentu utf-8) )

gdzie `pole(x) = długość x (8 bajtów, big-endian) | x`. Prefiks długości wyklucza przesunięcie granic między polami.

W odcisku są `strategy_v1.toml`, `risk_v1.toml` i `costs_v1.toml` (+ adapter `capitalcom-live`, klasa `index-cfd`),
NIE ma `runtime.toml`: tam właściciel przełącza `live_allowed`, a to nie jest zmiana wariantu strategii.

Polecenie: `python -m gwstock.prereg --write | --check | --print [--force]` (katalogi z `GW_CONFIG_DIR`, `GW_DATA_DIR`).
Kody wyjścia `--check`: 0 zgodny, 1 niezgodny (albo błąd odczytu konfiguracji), 2 nic nie zamrożono. Błąd użycia
(argparse) to także 2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "ADAPTER", "INSTRUMENT_CLASS", "CONFIG_FILES", "PREREG_FILE", "PreregistrationConflict", "fingerprint",
    "config_fingerprint", "file_hashes", "frozen_fingerprint", "write_preregistration", "main",
]  # fmt: skip

ADAPTER = "capitalcom-live"
INSTRUMENT_CLASS = "index-cfd"
CONFIG_FILES = ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml")  # bez runtime.toml (ruling)
PREREG_FILE = "preregistration.json"
ENV_CONFIG_DIR = "GW_CONFIG_DIR"
ENV_DATA_DIR = "GW_DATA_DIR"
DEFAULT_CONFIG_DIR = "config"
DEFAULT_DATA_DIR = "data"
FORCE_WORD = "FORCE"

_FORMAT_TAG = b"gwstock-prereg/1\n"
_LENGTH_BYTES = 8


class PreregistrationConflict(Exception):
    """Zamrożona pre-rejestracja istnieje i różni się od bieżącej konfiguracji (albo jest nieczytelna)."""


# --- odcisk ------------------------------------------------------------------------------------------------------


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(_LENGTH_BYTES, "big") + data


def _read(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"pre-registration input file is missing: {path}") from exc


def _digest(files: Sequence[tuple[str, bytes]], adapter: str, instrument_class: str) -> str:
    for name, value in (("adapter", adapter), ("instrument_class", instrument_class)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name}: expected a non-empty string, got {value!r}")
    h = hashlib.sha256()
    h.update(_FORMAT_TAG)
    h.update(len(files).to_bytes(_LENGTH_BYTES, "big"))
    for name, content in files:
        h.update(_field(name.encode("utf-8")))
        h.update(_field(content))
    h.update(_field(adapter.encode("utf-8")))
    h.update(_field(instrument_class.encode("utf-8")))
    return h.hexdigest()


def fingerprint(paths: list[Path], adapter: str, instrument_class: str) -> str:
    """Odcisk SHA-256 (hex) plików w podanej kolejności, adaptera i klasy instrumentu; kodowanie w docstringu modułu.

    Brak pliku: `FileNotFoundError` z jego ścieżką.
    """
    return _digest([(Path(p).name, _read(Path(p))) for p in paths], adapter, instrument_class)


def _config_contents(config_dir: Path) -> list[tuple[str, bytes]]:
    """Zamrożone pliki konfiguracji odczytane RAZ (odcisk i skróty plików z tych samych bajtów)."""
    return [(name, _read(Path(config_dir) / name)) for name in CONFIG_FILES]


def config_fingerprint(config_dir: Path) -> str:
    """Odcisk konfiguracji v1: `strategy_v1.toml`, `risk_v1.toml`, `costs_v1.toml` + adapter + klasa instrumentu.

    Bez `runtime.toml`. Brak pliku: `FileNotFoundError` z jego nazwą. To wywołuje `gwstock.cli` przy wyborze trybu.
    """
    return _digest(_config_contents(config_dir), ADAPTER, INSTRUMENT_CLASS)


def file_hashes(config_dir: Path) -> dict[str, str]:
    """SHA-256 (hex) każdego zamrożonego pliku konfiguracji, kluczem nazwa pliku, w kolejności `CONFIG_FILES`."""
    return {name: hashlib.sha256(content).hexdigest() for name, content in _config_contents(config_dir)}


# --- zamrożony plik ----------------------------------------------------------------------------------------------


def _load_record(data_dir: Path) -> dict[str, Any] | None:
    """Zawartość `data/preregistration.json`; None, gdy pliku nie ma; `ValueError`, gdy jest nieczytelny lub zepsuty."""
    path = Path(data_dir) / PREREG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # w tym katalog w miejscu pliku i bajty spoza UTF-8
        raise ValueError(f"{path} is unreadable: {type(exc).__name__}: {exc}") from exc
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    value = record.get("fingerprint") if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} has no usable 'fingerprint' (expected a JSON object with a non-empty string)")
    return record


def frozen_fingerprint(data_dir: Path) -> str | None:
    """Zamrożony odcisk z `data/preregistration.json`; None, gdy pliku nie ma; `ValueError`, gdy jest zepsuty.

    Silnik traktuje błąd jak „nie zamrożono” (`dry_run`).
    """
    record = _load_record(data_dir)
    return None if record is None else record["fingerprint"]


def _atomic_write_600(path: Path, data: bytes) -> None:
    """Zapis atomowy z uprawnieniami 600: plik tymczasowy w tym samym katalogu, fsync, `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def write_preregistration(config_dir: Path, data_dir: Path, now: datetime, *, force: bool = False) -> dict[str, Any]:
    """Zamraża bieżącą konfigurację w `data_dir/preregistration.json` (atomowo, 600) i zwraca zapisany rekord.

    Rekord: `fingerprint`, `files` (skrót SHA-256 każdego pliku), `adapter`, `instrument_class`, `written_at` (UTC).
    Ta sama konfiguracja co zamrożona: nic się nie zmienia i wraca istniejący rekord (data zamrożenia nie
    przesuwa się). Inny zamrożony odcisk albo nieczytelny plik: `PreregistrationConflict`, chyba że `force=True`
    (zmieniona konfiguracja to nowy wariant, K+1); wtedy nowy rekord ma klucz `previous` (odcisk i data zamrożenia
    zastąpionego rekordu, zagnieżdżone aż do pierwszego), żeby ślad wariantów nie zginął.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now: expected a timezone-aware datetime, got {now!r}")
    contents = _config_contents(config_dir)  # brak pliku: błąd, zanim cokolwiek zostanie dotknięte
    current = _digest(contents, ADAPTER, INSTRUMENT_CLASS)
    path = Path(data_dir) / PREREG_FILE
    try:
        existing = _load_record(data_dir)
    except ValueError as exc:
        if not force:
            raise PreregistrationConflict(
                f"{path} exists but cannot be read ({exc}); it was NOT changed. Repair it or use --force."
            ) from exc
        existing = None
    previous = None
    if existing is not None:
        if existing["fingerprint"] == current:
            return existing
        if force:  # ślad wariantów: poprzedni odcisk i data zamrożenia (razem z jego własnym poprzednikiem)
            previous = {key: existing[key] for key in ("fingerprint", "written_at", "previous") if key in existing}
        else:
            raise PreregistrationConflict(
                f"the configuration differs from the frozen pre-registration (frozen {existing['fingerprint'][:12]}, "
                f"current {current[:12]}): a changed configuration is a new variant (K+1). {path} was NOT changed. "
                "Restore the frozen files, or record the new variant (K+1) in docs/preregistration/v1.md and use "
                "--force."
            )
    record: dict[str, Any] = {
        "fingerprint": current,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents},
        "adapter": ADAPTER,
        "instrument_class": INSTRUMENT_CLASS,
        "written_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if previous is not None:
        record["previous"] = previous
    _atomic_write_600(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return record


# --- polecenie ---------------------------------------------------------------------------------------------------


def _block(fingerprint_hex: str, files: dict[str, str], adapter: str, instrument_class: str, written_at: str | None):
    """Wiersze tabeli Markdown do wklejenia w `docs/preregistration/v1.md`."""
    lines = [
        "Blok do wklejenia w docs/preregistration/v1.md (w kontenerze dokumenty nie są zamontowane, wklej ręcznie):",
        "",
        "| Pozycja | SHA-256 |",
        "|---|---|",
        f"| Odcisk konfiguracji (fingerprint) | `{fingerprint_hex}` |",
    ]
    lines += [f"| `config/{name}` | `{digest}` |" for name, digest in files.items()]
    lines += [f"| Adapter | `{adapter}` |", f"| Klasa instrumentu | `{instrument_class}` |"]
    if written_at is not None:
        lines.append(f"| Zamrożono (UTC) | `{written_at}` |")
    return lines


def _say(lines: Sequence[str]) -> None:
    print("\n".join(lines))


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _print_current(config_dir: Path) -> int:
    contents = _config_contents(config_dir)
    current = _digest(contents, ADAPTER, INSTRUMENT_CLASS)
    files = {name: hashlib.sha256(content).hexdigest() for name, content in contents}
    _say(["current configuration (nothing written):", f"fingerprint: {current}", ""])
    _say(_block(current, files, ADAPTER, INSTRUMENT_CLASS, None))
    return 0


def _differing_files(record: dict[str, Any], contents: Sequence[tuple[str, bytes]]) -> list[str] | None:
    """Nazwy zamrożonych plików, których skrót różni się od bieżącego; None, gdy rekord nie ma skrótów plików."""
    stored = record.get("files")
    if not isinstance(stored, dict):
        return None
    return [name for name, content in contents if stored.get(name) != hashlib.sha256(content).hexdigest()]


def _check(config_dir: Path, data_dir: Path) -> int:
    path = Path(data_dir) / PREREG_FILE
    contents = _config_contents(config_dir)
    current = _digest(contents, ADAPTER, INSTRUMENT_CLASS)
    try:
        record = _load_record(data_dir)
    except ValueError as exc:
        _say([f"NOT FROZEN: {exc}", f"current: {current}"])
        return 2
    if record is None:
        _say([f"NOT FROZEN: no pre-registration in {path}; freeze it with: python -m gwstock.prereg --write",
              f"current: {current}"])  # fmt: skip
        return 2
    frozen = record["fingerprint"]
    _say([f"frozen:  {frozen}", f"current: {current}"])
    if frozen == current:
        _say(["OK: the configuration matches the frozen pre-registration"])
        return 0
    _say(["MISMATCH: the configuration differs from the frozen pre-registration; this is a new variant (K+1)"])
    differing = _differing_files(record, contents)
    if differing is None:
        _say(["the frozen record has no per-file hashes, so the differing file cannot be named"])
    elif differing:
        _say([f"files that differ: {', '.join(differing)}"])
    else:
        _say(["no frozen file differs: the adapter, the instrument class or the encoding changed"])
    return 1


def _write(config_dir: Path, data_dir: Path, now: datetime, force: bool) -> int:
    path = Path(data_dir) / PREREG_FILE
    try:
        before = frozen_fingerprint(data_dir)
    except ValueError:
        before = None
    try:
        record = write_preregistration(config_dir, data_dir, now)
    except PreregistrationConflict as exc:
        if not force:
            _err(f"refused: {exc}")
            return 1
        _err(f"refused unless forced: {exc}")
        print(f"This overwrites the frozen pre-registration ({before or 'unreadable file'}) and starts a NEW variant "
              f"(K+1). Type {FORCE_WORD} to continue:", file=sys.stderr, flush=True)  # fmt: skip
        if sys.stdin.readline().strip() != FORCE_WORD:
            _err("aborted: nothing was changed")
            return 1
        record = write_preregistration(config_dir, data_dir, now, force=True)
        _say([f"replaced fingerprint: {before or 'unreadable file'} (record it as variant K+1 in the document)"])
    if before == record["fingerprint"]:
        _say([f"pre-registration unchanged: {path} is already frozen with this fingerprint"])
    else:
        _say([f"pre-registration written: {path}"])
    _say([f"fingerprint: {record['fingerprint']}", f"adapter: {record['adapter']}",
          f"instrument class: {record['instrument_class']}", f"written_at: {record['written_at']}", ""])  # fmt: skip
    _say(_block(record["fingerprint"], record["files"], record["adapter"], record["instrument_class"],
                record["written_at"]))  # fmt: skip
    return 0


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Polecenie `python -m gwstock.prereg`; zwraca kod wyjścia (patrz docstring modułu)."""
    parser = argparse.ArgumentParser(
        prog="python -m gwstock.prereg",
        description="Pre-rejestracja: odcisk konfiguracji (strategy/risk/costs + adapter + klasa instrumentu).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="zamroź bieżącą konfigurację w data/preregistration.json")
    mode.add_argument("--check", action="store_true", help="0: odcisk = zamrożony, 1: inny, 2: nic nie zamrożono")
    mode.add_argument("--print", dest="show", action="store_true", help="wypisz bieżący odcisk, nic nie zapisując")
    parser.add_argument("--force", action="store_true", help="z --write: nadpisz inny odcisk (nowy wariant, K+1)")
    args = parser.parse_args(argv)
    if args.force and not args.write:
        parser.error("--force is valid only with --write")
    config_dir = Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR)
    data_dir = Path(os.environ.get(ENV_DATA_DIR) or DEFAULT_DATA_DIR)
    try:
        if args.write:
            return _write(config_dir, data_dir, now or datetime.now(timezone.utc), args.force)
        if args.check:
            return _check(config_dir, data_dir)
        return _print_current(config_dir)
    except OSError as exc:  # m.in. brak pliku konfiguracji (z nazwą) albo brak zapisu w data/
        _err(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
