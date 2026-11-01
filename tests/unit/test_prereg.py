"""Testy pre-rejestracji (spec §11): odcisk SHA-256 konfiguracji, zamrożony plik i `python -m gwstock.prereg`."""

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gwstock import prereg

REPO = Path(__file__).resolve().parents[2]
# Obraz testowy (Dockerfile, cel test) ustawia GW_TEST_IMAGE=1 i nie zawiera docs/ ani .gitattributes. Tylko wtedy pomijamy
# testy dokumentów; bez znacznika brak tych plików ma wywalać testy głośno.
needs_repo_docs = pytest.mark.skipif(
    os.environ.get("GW_TEST_IMAGE") == "1", reason="obraz testowy (GW_TEST_IMAGE=1) nie zawiera docs/ ani .gitattributes"
)
NOW = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)
CONFIG_NAMES = ("strategy_v1.toml", "risk_v1.toml", "costs_v1.toml")


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def reference_fingerprint(files: list[tuple[str, bytes]], adapter: str, instrument_class: str) -> str:
    """Niezależna implementacja kodowania z docstringu `prereg.fingerprint` (przypina format)."""
    h = hashlib.sha256()
    h.update(b"gwstock-prereg/1\n")
    h.update(len(files).to_bytes(8, "big"))
    for name, content in files:
        h.update(_field(name.encode("utf-8")))
        h.update(_field(content))
    h.update(_field(adapter.encode("utf-8")))
    h.update(_field(instrument_class.encode("utf-8")))
    return h.hexdigest()


@pytest.fixture
def config_dir(tmp_path) -> Path:
    """Kopia prawdziwych plików `config/*.toml` (cztery pliki, razem z runtime.toml)."""
    target = tmp_path / "config"
    shutil.copytree(REPO / "config", target)
    return target


@pytest.fixture
def data_dir(tmp_path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def three_files(tmp_path) -> list[Path]:
    folder = tmp_path / "files"
    folder.mkdir()
    paths = []
    for name, content in (("a.toml", b"alpha = 1\n"), ("b.toml", b"beta = 2\n"), ("c.toml", b"gamma = 3\n")):
        (folder / name).write_bytes(content)
        paths.append(folder / name)
    return paths


# --- fingerprint ---------------------------------------------------------------------------------------------


def test_fingerprint_is_sha256_hex_and_deterministic(three_files):
    first = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
    again = prereg.fingerprint(list(three_files), "capitalcom-live", "index-cfd")
    assert first == again
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_fingerprint_matches_the_documented_encoding(three_files):
    expected = reference_fingerprint(
        [("a.toml", b"alpha = 1\n"), ("b.toml", b"beta = 2\n"), ("c.toml", b"gamma = 3\n")],
        "capitalcom-live",
        "index-cfd",
    )
    assert prereg.fingerprint(three_files, "capitalcom-live", "index-cfd") == expected


def test_fingerprint_changes_with_any_file_byte(three_files):
    base = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
    seen = {base}
    for path in three_files:
        original = path.read_bytes()
        path.write_bytes(original + b" ")  # jeden bajt więcej
        changed = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
        path.write_bytes(original)
        assert changed not in seen
        seen.add(changed)
    assert prereg.fingerprint(three_files, "capitalcom-live", "index-cfd") == base  # przywrócone: ten sam odcisk


def test_fingerprint_changes_with_a_single_flipped_bit(three_files):
    base = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
    original = three_files[1].read_bytes()
    three_files[1].write_bytes(bytes([original[0] ^ 1]) + original[1:])
    assert prereg.fingerprint(three_files, "capitalcom-live", "index-cfd") != base


def test_fingerprint_changes_with_file_order(three_files):
    forward = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
    swapped = prereg.fingerprint([three_files[1], three_files[0], three_files[2]], "capitalcom-live", "index-cfd")
    assert forward != swapped


def test_fingerprint_changes_with_adapter_and_instrument_class(three_files):
    base = prereg.fingerprint(three_files, "capitalcom-live", "index-cfd")
    assert prereg.fingerprint(three_files, "capitalcom-demo", "index-cfd") != base
    assert prereg.fingerprint(three_files, "capitalcom-live", "stock-cfd") != base
    assert prereg.fingerprint(three_files, "capitalcom-live", "index-cfd") == base


def test_fingerprint_changes_with_the_basename_of_a_file(tmp_path):
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    one.write_bytes(b"x = 1\n")
    two.write_bytes(b"x = 1\n")
    assert prereg.fingerprint([one], "a", "b") != prereg.fingerprint([two], "a", "b")


def test_fingerprint_ignores_directory_and_mtime(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right" / "deeper"
    left.mkdir()
    right.mkdir(parents=True)
    (left / "s.toml").write_bytes(b"x = 1\n")
    (right / "s.toml").write_bytes(b"x = 1\n")
    os.utime(right / "s.toml", (1, 1))  # inny czas modyfikacji
    assert prereg.fingerprint([left / "s.toml"], "a", "b") == prereg.fingerprint([right / "s.toml"], "a", "b")


def test_fingerprint_fields_are_length_prefixed_so_boundaries_cannot_shift(tmp_path):
    # nazwa+treść: ("x", "yz") kontra ("xy", "z"); adapter+klasa: ("ab", "c") kontra ("a", "bc")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "x").write_bytes(b"yz")
    (second / "xy").write_bytes(b"z")
    assert prereg.fingerprint([first / "x"], "a", "b") != prereg.fingerprint([second / "xy"], "a", "b")
    assert prereg.fingerprint([first / "x"], "ab", "c") != prereg.fingerprint([first / "x"], "a", "bc")


def test_fingerprint_with_no_files_still_covers_adapter_and_class():
    assert prereg.fingerprint([], "a", "b") != prereg.fingerprint([], "a", "c")


def test_fingerprint_missing_file_is_file_not_found_naming_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope.toml"):
        prereg.fingerprint([tmp_path / "nope.toml"], "a", "b")


# --- config_fingerprint ----------------------------------------------------------------------------------------


def test_config_fingerprint_covers_exactly_the_three_files_adapter_and_class(config_dir):
    files = [(name, (config_dir / name).read_bytes()) for name in CONFIG_NAMES]
    assert prereg.config_fingerprint(config_dir) == reference_fingerprint(files, "capitalcom-live", "index-cfd")
    assert prereg.config_fingerprint(config_dir) == prereg.fingerprint(
        [config_dir / name for name in CONFIG_NAMES], "capitalcom-live", "index-cfd"
    )


def test_runtime_toml_is_not_part_of_the_fingerprint(config_dir):
    base = prereg.config_fingerprint(config_dir)
    runtime = config_dir / "runtime.toml"
    runtime.write_text(runtime.read_text().replace("live_allowed = false", "live_allowed = true"))
    assert prereg.config_fingerprint(config_dir) == base
    runtime.unlink()  # brak runtime.toml też nie przeszkadza
    assert prereg.config_fingerprint(config_dir) == base


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_modifying_any_frozen_config_file_changes_the_fingerprint(config_dir, name):
    base = prereg.config_fingerprint(config_dir)
    with open(config_dir / name, "a", encoding="utf-8") as handle:
        handle.write("# komentarz zmienia bajty, więc i odcisk\n")
    assert prereg.config_fingerprint(config_dir) != base


def test_modified_risk_copy_changes_the_fingerprint_but_modified_runtime_copy_does_not(config_dir, tmp_path):
    base = prereg.config_fingerprint(config_dir)
    risk_copy = tmp_path / "risk_changed"
    shutil.copytree(config_dir, risk_copy)
    text = (risk_copy / "risk_v1.toml").read_text()
    (risk_copy / "risk_v1.toml").write_text(text.replace('max_loss_per_trade = "1.00"', 'max_loss_per_trade = "2.00"'))
    runtime_copy = tmp_path / "runtime_changed"
    shutil.copytree(config_dir, runtime_copy)
    (runtime_copy / "runtime.toml").write_text('live_allowed = true\naccount_id = "X"\n')
    assert prereg.config_fingerprint(risk_copy) != base
    assert prereg.config_fingerprint(runtime_copy) == base


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_config_fingerprint_missing_file_names_the_file(config_dir, name):
    (config_dir / name).unlink()
    with pytest.raises(FileNotFoundError, match=name):
        prereg.config_fingerprint(config_dir)


def test_file_hashes_are_sha256_per_basename(config_dir):
    hashes = prereg.file_hashes(config_dir)
    assert list(hashes) == list(CONFIG_NAMES)
    for name, digest in hashes.items():
        assert digest == hashlib.sha256((config_dir / name).read_bytes()).hexdigest()


# --- zgodność z wywołaniem w gwstock.cli -----------------------------------------------------------------------


def test_cli_lazy_import_contract_matches(config_dir, data_dir):
    from gwstock import cli

    assert cli.PREREG_FILE == prereg.PREREG_FILE
    assert cli.PREREG_ADAPTER == prereg.ADAPTER
    assert cli.PREREG_INSTRUMENT_CLASS == prereg.INSTRUMENT_CLASS
    assert tuple(cli.PREREG_CONFIG_FILES) == prereg.CONFIG_FILES
    # `cli.config_fingerprint` woła prereg.config_fingerprint(Path)
    assert cli.config_fingerprint(config_dir) == prereg.config_fingerprint(config_dir)
    # ścieżka zapasowa cli (fingerprint(paths, adapter, class)) daje ten sam odcisk
    paths = [config_dir / name for name in cli.PREREG_CONFIG_FILES]
    assert prereg.fingerprint(paths, cli.PREREG_ADAPTER, cli.PREREG_INSTRUMENT_CLASS) == prereg.config_fingerprint(
        config_dir
    )
    # zamrożony plik czytany przez cli to ten sam, który zapisujemy
    prereg.write_preregistration(config_dir, data_dir, NOW)
    assert cli.frozen_fingerprint(data_dir) == prereg.config_fingerprint(config_dir)


# --- frozen_fingerprint ----------------------------------------------------------------------------------------


def test_frozen_fingerprint_missing_file_is_none(data_dir):
    assert prereg.frozen_fingerprint(data_dir) is None


def test_frozen_fingerprint_reads_the_fingerprint_key(data_dir):
    (data_dir / "preregistration.json").write_text(json.dumps({"fingerprint": "abc123", "other": 1}))
    assert prereg.frozen_fingerprint(data_dir) == "abc123"


@pytest.mark.parametrize(
    "content",
    [b"{broken", b"", b"[]", b'"text"', b"{}", b'{"fingerprint": ""}', b'{"fingerprint": 5}', b'{"fingerprint": null}',
     b"\xff\xfe\x00 not utf-8"],
    ids=["broken-json", "empty", "list", "string", "no-key", "empty-value", "number", "null", "not-utf8"],
)  # fmt: skip
def test_frozen_fingerprint_malformed_is_value_error(data_dir, content):
    (data_dir / "preregistration.json").write_bytes(content)
    with pytest.raises(ValueError):
        prereg.frozen_fingerprint(data_dir)


def test_frozen_fingerprint_directory_in_place_of_the_file_is_value_error(data_dir):
    (data_dir / "preregistration.json").mkdir()
    with pytest.raises(ValueError):
        prereg.frozen_fingerprint(data_dir)


# --- write_preregistration -------------------------------------------------------------------------------------


def test_write_preregistration_writes_the_record(config_dir, data_dir):
    record = prereg.write_preregistration(config_dir, data_dir, NOW)
    assert set(record) == {"fingerprint", "files", "adapter", "instrument_class", "written_at"}
    assert record["fingerprint"] == prereg.config_fingerprint(config_dir)
    assert record["adapter"] == "capitalcom-live" and record["instrument_class"] == "index-cfd"
    assert record["written_at"] == "2026-11-02T07:00:00Z"
    assert list(record["files"]) == list(CONFIG_NAMES)  # tylko trzy pliki, bez runtime.toml
    for name, digest in record["files"].items():
        assert digest == hashlib.sha256((config_dir / name).read_bytes()).hexdigest()
    stored = json.loads((data_dir / "preregistration.json").read_text(encoding="utf-8"))
    assert stored == record
    assert prereg.frozen_fingerprint(data_dir) == record["fingerprint"]


def test_written_at_is_utc_even_for_a_foreign_offset(config_dir, data_dir):
    plus_two = datetime(2026, 11, 2, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    assert prereg.write_preregistration(config_dir, data_dir, plus_two)["written_at"] == "2026-11-02T07:00:00Z"


def test_write_preregistration_rejects_a_naive_time(config_dir, data_dir):
    with pytest.raises(ValueError, match="now"):
        prereg.write_preregistration(config_dir, data_dir, datetime(2026, 11, 2, 7, 0))
    assert not (data_dir / "preregistration.json").exists()


def test_write_is_atomic_mode_600_and_leaves_no_temporary_file(config_dir, data_dir):
    prereg.write_preregistration(config_dir, data_dir, NOW)
    path = data_dir / "preregistration.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in data_dir.iterdir()] == ["preregistration.json"]


def test_failed_replace_keeps_the_previous_file_and_cleans_up(config_dir, data_dir, monkeypatch):
    first = prereg.write_preregistration(config_dir, data_dir, NOW)
    (config_dir / "risk_v1.toml").write_text("# changed\n" + (config_dir / "risk_v1.toml").read_text())

    def failing_replace(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(prereg.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk says no"):
        prereg.write_preregistration(config_dir, data_dir, NOW, force=True)
    assert json.loads((data_dir / "preregistration.json").read_text()) == first
    assert [p.name for p in data_dir.iterdir()] == ["preregistration.json"]


def test_writing_the_same_configuration_again_keeps_the_original_record(config_dir, data_dir):
    first = prereg.write_preregistration(config_dir, data_dir, NOW)
    later = prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=3))
    assert later == first  # data zamrożenia nie jest przesuwana
    assert json.loads((data_dir / "preregistration.json").read_text()) == first


def test_changed_configuration_is_refused_as_a_new_variant(config_dir, data_dir):
    first = prereg.write_preregistration(config_dir, data_dir, NOW)
    before = (data_dir / "preregistration.json").read_bytes()
    (config_dir / "strategy_v1.toml").write_text("# zmiana\n" + (config_dir / "strategy_v1.toml").read_text())
    with pytest.raises(prereg.PreregistrationConflict) as excinfo:
        prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=1))
    assert "K+1" in str(excinfo.value) and "new variant" in str(excinfo.value)
    assert first["fingerprint"][:12] in str(excinfo.value)
    assert (data_dir / "preregistration.json").read_bytes() == before
    assert [p.name for p in data_dir.iterdir()] == ["preregistration.json"]


def test_force_overwrites_a_different_frozen_fingerprint(config_dir, data_dir):
    first = prereg.write_preregistration(config_dir, data_dir, NOW)
    (config_dir / "costs_v1.toml").write_text("# zmiana\n" + (config_dir / "costs_v1.toml").read_text())
    second = prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=1), force=True)
    assert second["fingerprint"] != first["fingerprint"]
    assert second["fingerprint"] == prereg.config_fingerprint(config_dir)
    assert prereg.frozen_fingerprint(data_dir) == second["fingerprint"]
    assert second["written_at"] == "2026-11-03T07:00:00Z"


def test_force_records_the_previous_fingerprint_so_the_variant_trail_survives(config_dir, data_dir):
    first = prereg.write_preregistration(config_dir, data_dir, NOW)
    assert "previous" not in first  # pierwsze zamrożenie nie ma poprzednika
    (config_dir / "costs_v1.toml").write_text("# zmiana 1\n" + (config_dir / "costs_v1.toml").read_text())
    second = prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=1), force=True)
    assert second["previous"] == {"fingerprint": first["fingerprint"], "written_at": first["written_at"]}
    (config_dir / "costs_v1.toml").write_text("# zmiana 2\n" + (config_dir / "costs_v1.toml").read_text())
    third = prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=2), force=True)
    assert third["previous"] == {
        "fingerprint": second["fingerprint"], "written_at": second["written_at"], "previous": second["previous"],
    }  # cały ślad wariantów, nie tylko ostatni krok
    assert json.loads((data_dir / "preregistration.json").read_text()) == third
    # ta sama konfiguracja z --force nic nie zmienia i nie dopisuje poprzednika
    assert prereg.write_preregistration(config_dir, data_dir, NOW + timedelta(days=3), force=True) == third


def test_force_over_an_unreadable_file_has_no_previous(config_dir, data_dir):
    (data_dir / "preregistration.json").write_text("{broken")
    record = prereg.write_preregistration(config_dir, data_dir, NOW, force=True)
    assert "previous" not in record


def test_malformed_existing_file_is_not_overwritten_without_force(config_dir, data_dir):
    path = data_dir / "preregistration.json"
    path.write_text("{broken")
    with pytest.raises(prereg.PreregistrationConflict):
        prereg.write_preregistration(config_dir, data_dir, NOW)
    assert path.read_text() == "{broken"
    record = prereg.write_preregistration(config_dir, data_dir, NOW, force=True)
    assert prereg.frozen_fingerprint(data_dir) == record["fingerprint"]


def test_missing_config_file_writes_nothing(config_dir, data_dir):
    (config_dir / "costs_v1.toml").unlink()
    with pytest.raises(FileNotFoundError, match="costs_v1.toml"):
        prereg.write_preregistration(config_dir, data_dir, NOW)
    assert list(data_dir.iterdir()) == []


# --- polecenie --------------------------------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, config_dir, data_dir):
    monkeypatch.setenv("GW_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))


def test_cli_write_prints_a_paste_ready_block_and_exits_0(env, config_dir, data_dir, capsys):
    assert prereg.main(["--write"], now=NOW) == 0
    out = capsys.readouterr().out
    record = json.loads((data_dir / "preregistration.json").read_text())
    assert f"fingerprint: {record['fingerprint']}" in out
    assert "| Odcisk konfiguracji (fingerprint) | `" + record["fingerprint"] + "` |" in out
    for name in CONFIG_NAMES:
        assert f"| `config/{name}` | `{record['files'][name]}` |" in out
    assert "| Adapter | `capitalcom-live` |" in out
    assert "| Klasa instrumentu | `index-cfd` |" in out
    assert "runtime.toml" not in out  # runtime.toml nie jest w bloku
    assert "docs/preregistration/v1.md" in out


def test_cli_write_twice_is_idempotent_and_changed_config_exits_1(env, config_dir, data_dir, capsys):
    assert prereg.main(["--write"], now=NOW) == 0
    stored = (data_dir / "preregistration.json").read_bytes()
    assert prereg.main(["--write"], now=NOW + timedelta(days=1)) == 0
    assert (data_dir / "preregistration.json").read_bytes() == stored
    (config_dir / "risk_v1.toml").write_text("# zmiana\n" + (config_dir / "risk_v1.toml").read_text())
    capsys.readouterr()
    assert prereg.main(["--write"], now=NOW + timedelta(days=2)) == 1
    captured = capsys.readouterr()
    assert "K+1" in captured.err and "new variant" in captured.err
    assert (data_dir / "preregistration.json").read_bytes() == stored


def test_cli_force_needs_the_word_force_on_stdin(env, config_dir, data_dir, monkeypatch, capsys):
    prereg.main(["--write"], now=NOW)
    old = prereg.frozen_fingerprint(data_dir)
    (config_dir / "risk_v1.toml").write_text("# zmiana\n" + (config_dir / "risk_v1.toml").read_text())
    for answer in ("force\n", "yes\n", "\n", ""):
        monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
        assert prereg.main(["--write", "--force"], now=NOW) == 1
        assert prereg.frozen_fingerprint(data_dir) == old
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("FORCE\n"))
    assert prereg.main(["--write", "--force"], now=NOW + timedelta(days=1)) == 0
    out = capsys.readouterr().out
    assert prereg.frozen_fingerprint(data_dir) == prereg.config_fingerprint(config_dir) != old
    assert old in out  # poprzedni odcisk wypisany do zapisu w pre-rejestracji (K+1)


def test_cli_force_without_conflict_does_not_prompt(env, data_dir, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert prereg.main(["--write", "--force"], now=NOW) == 0
    assert prereg.frozen_fingerprint(data_dir) is not None


def test_cli_force_is_valid_only_with_write(env, capsys):
    with pytest.raises(SystemExit) as excinfo:
        prereg.main(["--check", "--force"])
    assert excinfo.value.code == 2
    assert "--force" in capsys.readouterr().err


def test_cli_exactly_one_mode_is_required(env, capsys):
    for argv in ([], ["--write", "--check"], ["--print", "--check"]):
        with pytest.raises(SystemExit) as excinfo:
            prereg.main(argv)
        assert excinfo.value.code == 2


def test_cli_check_exit_codes(env, config_dir, data_dir, capsys):
    assert prereg.main(["--check"]) == 2  # nic nie zamrożone
    assert "NOT FROZEN" in capsys.readouterr().out
    prereg.main(["--write"], now=NOW)
    capsys.readouterr()
    assert prereg.main(["--check"]) == 0
    out = capsys.readouterr().out
    fingerprint = prereg.config_fingerprint(config_dir)
    assert f"frozen:  {fingerprint}" in out and f"current: {fingerprint}" in out and "OK" in out
    (config_dir / "costs_v1.toml").write_text("# zmiana\n" + (config_dir / "costs_v1.toml").read_text())
    assert prereg.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and f"frozen:  {fingerprint}" in out


def test_cli_check_names_the_files_that_differ(env, config_dir, capsys):
    prereg.main(["--write"], now=NOW)
    for name in ("risk_v1.toml", "strategy_v1.toml"):
        (config_dir / name).write_text("# zmiana\n" + (config_dir / name).read_text())
    capsys.readouterr()
    assert prereg.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "files that differ: strategy_v1.toml, risk_v1.toml" in out.splitlines()  # kolejność jak w CONFIG_FILES
    assert "costs_v1.toml" not in out


def test_cli_check_when_no_file_differs_or_the_record_has_no_file_hashes(env, config_dir, data_dir, capsys):
    prereg.main(["--write"], now=NOW)
    record = json.loads((data_dir / "preregistration.json").read_text())
    record["fingerprint"] = "0" * 64  # odcisk inny (np. adapter albo klasa), skróty plików te same
    (data_dir / "preregistration.json").write_text(json.dumps(record))
    capsys.readouterr()
    assert prereg.main(["--check"]) == 1
    message = "no frozen file differs: the adapter, the instrument class or the encoding changed"
    assert message in capsys.readouterr().out
    (data_dir / "preregistration.json").write_text(json.dumps({"fingerprint": "0" * 64}))
    assert prereg.main(["--check"]) == 1
    assert "the frozen record has no per-file hashes" in capsys.readouterr().out


def test_cli_check_ignores_runtime_toml(env, config_dir, capsys):
    prereg.main(["--write"], now=NOW)
    (config_dir / "runtime.toml").write_text("live_allowed = true\n")
    assert prereg.main(["--check"]) == 0


def test_cli_check_with_an_unreadable_frozen_file_counts_as_not_frozen(env, data_dir, capsys):
    (data_dir / "preregistration.json").write_text("{broken")
    assert prereg.main(["--check"]) == 2
    assert "NOT FROZEN" in capsys.readouterr().out


def test_cli_print_writes_nothing(env, config_dir, data_dir, capsys):
    assert prereg.main(["--print"]) == 0
    out = capsys.readouterr().out
    assert f"fingerprint: {prereg.config_fingerprint(config_dir)}" in out
    assert list(data_dir.iterdir()) == []


def test_cli_missing_config_file_exits_1_with_the_file_name(env, config_dir, capsys):
    (config_dir / "risk_v1.toml").unlink()
    for argv in (["--write"], ["--check"], ["--print"]):
        assert prereg.main(argv, now=NOW) == 1
        assert "risk_v1.toml" in capsys.readouterr().err


def test_cli_defaults_are_config_and_data_relative_to_the_working_directory(monkeypatch, tmp_path, config_dir, capsys):
    monkeypatch.delenv("GW_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GW_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # tu leżą ./config (kopia) i ./data
    (tmp_path / "data").mkdir()
    assert prereg.main(["--write"], now=NOW) == 0
    assert (tmp_path / "data" / "preregistration.json").is_file()


def test_python_dash_m_gwstock_prereg_runs(tmp_path, config_dir):
    data = tmp_path / "data_sub"
    data.mkdir()
    env = {**os.environ, "GW_CONFIG_DIR": str(config_dir), "GW_DATA_DIR": str(data), "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "gwstock.prereg", "--print"], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert prereg.config_fingerprint(config_dir) in result.stdout
    assert "Warning" not in result.stderr
    checked = subprocess.run(
        [sys.executable, "-m", "gwstock.prereg", "--check"], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert checked.returncode == 2


# --- .gitattributes ----------------------------------------------------------------------------------------------


@needs_repo_docs
def test_gitattributes_pins_line_endings_of_the_frozen_config_files():
    # odcisk liczy SUROWE bajty, więc CRLF (np. z autocrlf) zmieniłby go bez zmiany treści
    path = REPO / ".gitattributes"
    assert path.is_file()
    rules = [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and line[0] != "#"]
    assert ["config/*.toml", "text", "eol=lf"] in rules
    for name in CONFIG_NAMES:  # i same pliki w repozytorium nie mają CRLF ani BOM
        raw = (REPO / "config" / name).read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")


@needs_repo_docs
def test_preregistration_document_matches_what_the_tools_do():
    text = (REPO / "docs" / "preregistration" / "v1.md").read_text(encoding="utf-8")
    prefix = "docker compose -f docker/compose.yml"
    assert f"{prefix} run --rm -T gw-stock-engine python -m gwstock.prereg --write" in text
    assert f"{prefix} run --rm -T gw-stock-engine python -m gwstock.prereg --check" in text
    assert f"{prefix} exec -T gw-stock-engine python -m gwstock.report --csv /data/ledger.csv" in text
    assert "nie jest mierzony w v1" in text  # poślizg stopu przy wyjściu
    assert "config/*.toml text eol=lf" in text
    assert "previous" in text  # ślad wariantów po --force


NEW_WORDING = "v1 mierzy poślizg wejścia i spread; koszty wyjść STOP są niezmierzone (raport tylko liczy takie wyjścia)"
OLD_WORDING = "spread i poślizg stopów zmierzone i porównane z modelem"
SAMPLES_CLAUSE = "próbki kosztu to transakcje wejścia ORAZ próby smoke dowolnej wielkości"


@needs_repo_docs
def test_cost_wording_is_aligned_in_spec_preregistration_and_runbook():
    spec = (REPO / "docs/superpowers/specs/2026-10-31-engine-design-v2.md").read_text(encoding="utf-8")
    prereg_doc = (REPO / "docs/preregistration/v1.md").read_text(encoding="utf-8")
    runbook = (REPO / "docs/ops/runbook.md").read_text(encoding="utf-8")
    for name, text in (("spec", spec), ("prereg", prereg_doc), ("runbook", runbook)):
        assert OLD_WORDING not in text, name  # raport nie mierzy poślizgu stopu, więc zdanie obiecywało za dużo
        assert NEW_WORDING in text, name
    assert spec.count(NEW_WORDING) == 1 and prereg_doc.count(NEW_WORDING) == 1 and runbook.count(NEW_WORDING) == 1
    for text in (prereg_doc, runbook):
        assert SAMPLES_CLAUSE in text  # licznik 5 z 5 nie znaczy jednej próbki na sesję
    assert "eksport CSV" in runbook.split(SAMPLES_CLAUSE)[1][:200]


@needs_repo_docs
def test_preregistration_document_states_the_known_limits_of_the_l1_numbers():
    text = (REPO / "docs" / "preregistration" / "v1.md").read_text(encoding="utf-8")
    assert "cztery liczby" not in text  # są pięć: cztery warunki bramki i liczba próbek kosztu
    assert "pięć wielkości" in text
    # gdy wszystkie wejścia o wielkości minimalnej nie mają użytecznego kontekstu, próg przesuwa się dalej
    assert "przesuwa się na kolejną wielkość" in text and "ostrożnie" in text
