"""Regresja skryptu badawczego research/orb_daily_backtest.py: konfiguracja sesji z env (strefa, godziny), bez sieci."""
import csv
import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "research" / "orb_daily_backtest.py"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="brak katalogu research/ (obraz testowy)")


def write_m1(path, tz, open_t, close_t, first_day, last_day):
    """Rosnąca cena bez rozrzutu w świecy (pierwsza świeca zawsze wzrostowa, stop nigdy nie dosięgnięty)."""
    zone = ZoneInfo(tz)
    rows, d, k = [], first_day, 0
    while d <= last_day:
        if d.weekday() < 5:
            t = datetime.combine(d, open_t, tzinfo=zone)
            end = datetime.combine(d, close_t, tzinfo=zone) + timedelta(minutes=10)
            while t <= end:
                px = 1000.0 + k * 0.02
                rows.append([t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), px, px, px, px, 0.2])
                t += timedelta(minutes=1)
                k += 1
        d += timedelta(days=1)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "open", "high", "low", "close", "spread"])
        w.writerows(rows)


def run(monkeypatch, tmp_path, csv_path, **env):
    out = tmp_path / "trades.csv"
    monkeypatch.setenv("GW_M1_CSV", str(csv_path))
    monkeypatch.setenv("GW_TRADES_CSV", str(out))
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location("orb_daily_backtest_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
    with out.open() as f:
        return [r for r in csv.DictReader(f) if r["wariant"] == "ORB stop=0.10ATR"]


def test_default_session_is_berlin_0900_across_the_october_dst_change(monkeypatch, tmp_path):
    p = tmp_path / "m1.csv"
    write_m1(p, "Europe/Berlin", time(9, 0), time(17, 25), date(2026, 9, 14), date(2026, 11, 6))
    rows = run(monkeypatch, tmp_path, p)
    days = {r["dzien"] for r in rows}
    assert any(d < "2026-10-25" for d in days) and any(d > "2026-10-26" for d in days)  # po zmianie czasu 25.10 nadal 09:00 lokalnie
    assert all(r["kierunek"] == "LONG" and r["powod"] == "FLAT" for r in rows)


def test_us_session_follows_new_york_time_across_the_us_dst_change(monkeypatch, tmp_path):
    p = tmp_path / "m1_us.csv"
    write_m1(p, "America/New_York", time(9, 30), time(15, 55), date(2026, 9, 14), date(2026, 11, 13))
    rows = run(monkeypatch, tmp_path, p, GW_TZ="America/New_York", GW_OPEN="09:30", GW_CLOSE="15:55", GW_POINT_EUR="0")
    days = {r["dzien"] for r in rows}
    assert any(d < "2026-11-01" for d in days) and any(d > "2026-11-02" for d in days)  # 1.11 USA wracają do czasu zimowego
    assert all(r["kierunek"] == "LONG" and r["powod"] == "FLAT" for r in rows)


def test_us_session_with_berlin_defaults_finds_no_trades(monkeypatch, tmp_path):
    p = tmp_path / "m1_us.csv"
    write_m1(p, "America/New_York", time(9, 30), time(15, 55), date(2026, 9, 14), date(2026, 11, 13))
    assert run(monkeypatch, tmp_path, p) == []  # bez GW_TZ/GW_OPEN okno 09:00 Berlin nie istnieje w danych USA: nic nie wchodzi


def test_trades_file_defaults_to_a_name_derived_from_the_csv(monkeypatch, tmp_path):
    p = tmp_path / "m1_us100.csv"
    write_m1(p, "America/New_York", time(9, 30), time(15, 55), date(2026, 9, 14), date(2026, 10, 30))
    monkeypatch.delenv("GW_TRADES_CSV", raising=False)
    monkeypatch.setenv("GW_M1_CSV", str(p))
    for key, val in dict(GW_TZ="America/New_York", GW_OPEN="09:30", GW_CLOSE="15:55", GW_POINT_EUR="0").items():
        monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location("orb_daily_backtest_under_test2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OUT == tmp_path / "orb_trades_m1_us100.csv"  # inny instrument nie nadpisuje /data/orb_daily_trades.csv (DE40)
