"""Powiadomienia silnika (spec §13): interfejs `Notifier` i plik alarmów.

Zasady:
- `Notifier.alert(level, message)` nigdy nie może być jedyną drogą informacji: wywołujący (kill switch) i tak
  zapisuje stan w dzienniku, a błąd notyfikatora go nie zatrzymuje;
- `FileNotifier` dopisuje jedną linię na alarm: `{iso-utc} {LEVEL} {message}`; znaki nowej linii w treści
  (i białe znaki w poziomie) są zamieniane, więc jedna linia to zawsze jeden alarm;
- `FileNotifier` NIGDY nie rzuca: gdy plik jest niedostępny (brak uprawnień, pełny dysk, katalog w miejscu pliku),
  linia trafia na stderr, a jeśli i to się nie uda, alarm jest po cichu gubiony (nie ma już dokąd go wysłać);
- czas pochodzi z wstrzykniętego zegara (domyślnie `SystemClock`), żeby testy były deterministyczne.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from gwstock.clock import Clock, SystemClock

# Wszystko, co `str.splitlines()` uznaje za koniec linii (poza samym "\r\n", który obsługuje osobny wariant).
_LINE_BREAKS = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
_WHITESPACE = re.compile(r"\s+")


@runtime_checkable
class Notifier(Protocol):
    """Odbiorca alarmów. `level` to krótki napis (np. SOFT, HARD, CLEAR, INFO), `message` to treść."""

    def alert(self, level: str, message: str) -> None: ...


class FileNotifier:
    """Dopisuje alarmy do pliku (jedna linia na alarm); nigdy nie rzuca."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self._path = Path(path)
        self._clock: Clock = clock if clock is not None else SystemClock()

    def alert(self, level: str, message: str) -> None:
        try:
            line = f"{self._timestamp()} {_clean_level(level)} {_LINE_BREAKS.sub(' ', str(message))}"
        except Exception:  # np. wyjątkowo zepsuty obiekt w `message`; alarm i tak ma wyjść
            line = f"{datetime.now(timezone.utc).isoformat()} {_clean_level(level)} <unprintable alert message>"
        try:
            self._append(line)
        except Exception as exc:
            _to_stderr(f"{line} [alert file {self._path} unavailable: {type(exc).__name__}]")

    def _timestamp(self) -> str:
        try:
            return self._clock.now_utc().astimezone(timezone.utc).isoformat()
        except Exception:  # zepsuty zegar nie może zgubić alarmu
            return datetime.now(timezone.utc).isoformat()

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # backslashreplace: znak, którego nie da się zakodować (np. samotny surogat), nie gubi całej linii
        with self._path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())  # alarm ma przeżyć awarię zasilania; brak fsync nie jest powodem do stderr
            except OSError:
                pass


def _clean_level(level: object) -> str:
    try:
        text = _WHITESPACE.sub("_", str(level).strip())
    except Exception:
        return "-"
    return text or "-"


def _to_stderr(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass  # stderr zamknięty: nie ma już dokąd wysłać alarmu
