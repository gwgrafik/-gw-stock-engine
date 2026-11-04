"""Regresja skryptu badawczego research/candle_strategies.py (tylko odczyt, bez sieci)."""
import importlib.util
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "research" / "candle_strategies.py"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="brak katalogu research/ (obraz testowy)")


@pytest.fixture(scope="module")
def cs():
    spec = importlib.util.spec_from_file_location("candle_strategies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T0 = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)  # 09:00 Berlin


def bar(cs, k, o, h, l, c, sp=1.8):
    return cs.Bar(T0 + timedelta(minutes=k), o - sp / 2, h - sp / 2, l - sp / 2, c - sp / 2,
                  o + sp / 2, h + sp / 2, l + sp / 2, c + sp / 2)


def flat(cs, n, px=24000.0):
    return [bar(cs, k, px, px + 0.5, px - 0.5, px) for k in range(n)]


def test_range_uses_exactly_n_prior_closed_bars(cs):
    bars = flat(cs, 40)
    bars[30 - 16] = bar(cs, 14, 24000, 24500, 23999.5, 24000)  # świeca i-16: poza zakresem N=15
    bars[30] = bar(cs, 30, 24000, 24010, 23999.8, 24009.8)  # silne zamknięcie ponad zakres
    sig = cs.signals("B1", 15, cs.TF["M1"]["limits"], bars, 30, None, None, 1.8)
    assert sig is not None and sig[0] == 1  # gdyby zakres miał 16 świec, upper = 24500 i sygnału by nie było


def test_b4_confirms_against_the_mother_bar_not_the_inside_bar(cs):
    bars = flat(cs, 40)
    bars[28] = bar(cs, 28, 24000, 24020, 23990, 24005)  # nadrzędna: H=24020
    bars[29] = bar(cs, 29, 24005, 24012, 23995, 24008)  # inside: H=24012 < 24020
    bars[30] = bar(cs, 30, 24008, 24016, 24006, 24015)  # zamknięcie ponad inside (24012), ale poniżej nadrzędnej (24020)
    assert cs.signals("B4", 15, cs.TF["M1"]["limits"], bars, 30, None, None, 1.8) is None
    bars[30] = bar(cs, 30, 24008, 24030, 24006, 24025)  # ponad ekstremum świecy nadrzędnej
    sig = cs.signals("B4", 15, cs.TF["M1"]["limits"], bars, 30, None, None, 1.8)
    assert sig is not None and sig[0] == 1


def test_b3_touch_uses_the_m1_ema_not_the_m5_ema(cs):
    bars = flat(cs, 40)
    bars[29] = bar(cs, 29, 24000, 24001, 23999, 24000)  # t-1 dotyka EMA20(M1) = 24000
    bars[30] = bar(cs, 30, 24000, 24006, 24000, 24005)
    ctx = (24500.0, 24400.0, 24300.0)  # trend M5 w górę, ale EMA20(M5) daleko od ceny: dotknięcie ma dotyczyć M1
    sig = cs.signals("B3", 15, cs.TF["M1"]["limits"], bars, 30, 24000.0, ctx, 1.8)
    assert sig is not None and sig[0] == 1


def test_gap_through_the_stop_fills_at_the_open_and_end_of_data_is_flagged(cs):
    bars = [bar(cs, k, 24000, 24002, 23998, 24000) for k in range(40)]  # ATR ok. 4-5 pkt, żeby filtr poślizgu nie wyciął wejścia
    bars.append(bar(cs, 40, 24000, 24022, 23999, 24020))  # wybicie ponad zakres
    bars += [bar(cs, 41 + k, 24020, 24020.3, 24019.7, 24020) for k in range(2)]  # bez ruchu: ani stop, ani cel
    bars.append(bar(cs, 43, 23620, 23621, 23619, 23620))  # luka w dół o 400 pkt, dane urywają się
    atr = cs.atr_series(bars)
    tr, _ = cs.run("B1", "M1", "ALL", bars, atr, None, None)
    assert tr, "oczekiwano transakcji z wybicia"
    reasons = {t[5] for t in tr}
    assert reasons <= {"STOP", "TARGET", "TIME", "FLAT", "END_OF_DATA"}
    stops = [t for t in tr if t[5] == "STOP"]
    assert stops, "luka przez stop ma dać wyjście STOP"
    for t in stops:  # wypełnienie po otwarciu luki
        assert t[4] <= 23620 - 1.0 + 1e-9  # po otwarciu luki minus poślizg stopu, nie po cenie stopu (ok. 23999)


def test_gapped_data_is_skipped(cs):
    bars = flat(cs, 20) + [cs.Bar(b.t + timedelta(minutes=30), *b[1:]) for b in flat(cs, 25)]
    atr = cs.atr_series(bars)
    tr, skips = cs.run("B1", "M1", "ALL", bars, atr, None, None)
    assert tr == []


def test_session_defaults_stay_berlin(cs):
    assert cs.WINDOWS["ALL"] == [(time(9, 15), time(17, 0))] and cs.WINDOWS["SESJE"] == [(time(9, 15), time(11, 30)), (time(15, 30), time(17, 0))]
    assert cs.FLAT == time(17, 25) and str(cs.TZ) == "Europe/Berlin"


def test_session_comes_from_env_for_another_market(monkeypatch):
    env = dict(GW_EPIC="US100", GW_SLIP="0.55", GW_STOP_EXTRA="1.1", GW_STEP="0.1", GW_POINT_EUR="0", GW_TZ="America/New_York",
               GW_WIN_ALL="09:45-15:30", GW_WIN_SESJE="09:45-11:45,14:00-15:30", GW_FLAT="15:55")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location("candle_strategies_us", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.WINDOWS["ALL"] == [(time(9, 45), time(15, 30))]
    assert mod.WINDOWS["SESJE"] == [(time(9, 45), time(11, 45)), (time(14, 0), time(15, 30))]
    assert mod.FLAT == time(15, 55) and str(mod.TZ) == "America/New_York"
