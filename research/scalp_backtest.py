"""Test historyczny: wybicie zakresu ostatnich N minut na DE40, z kosztami. Tylko odczyt /data/m1_de40.csv.

Reguły (ustalone z góry, nie dobierane do wyniku):
- sygnał na zamknięciu świecy M1: close > max(high) z N poprzednich zakończonych świec (long) albo < min(low) (short);
- okno wejść 09:15-17:00 czasu Berlina, jedna pozycja naraz, do MAX_PER_DAY wejść dziennie;
- wejście na otwarciu następnej świecy po stronie ask/bid + poślizg; stop = poziom wybicia -/+ 1 x ATR14(M1);
- wyjście: powrót zamknięcia do zakresu (po poziomie wybicia), stop (z dodatkowym poślizgiem) albo 17:25.
Koszty: spread z danych (połowa przy wejściu i przy wyjściu) + poślizg z config/costs_v1.toml.
Uruchomienie: docker compose ... run --rm -T gw-stock-engine python - < research/scalp_backtest.py
"""
import csv, os, statistics
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CSV = Path(os.environ.get("GW_M1_CSV", "/data/m1_de40.csv"))
OUTCSV = Path(os.environ.get("GW_TRADES_CSV", "/data/scalp_trades.csv"))
BER = ZoneInfo("Europe/Berlin")
SLIP, STOP_EXTRA, DEF_SPREAD = 0.5, 1.0, 1.8  # punkty (config/costs_v1.toml)
MAX_PER_DAY, ATR_N = 6, 14
POINT_EUR = 0.001  # wartość punktu przy wielkości 0,001


def load():
    bars = []
    with CSV.open() as f:
        for r in csv.DictReader(f):
            t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(BER)
            sp = float(r["spread"]) or DEF_SPREAD
            bars.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), sp))
    bars.sort(key=lambda b: b[0])
    return bars


def run(bars, n=15, cost_mult=1.0):
    trades, i = [], 0
    by_day = {}
    for k, b in enumerate(bars):
        by_day.setdefault(b[0].date(), []).append(k)
    for day, idx in by_day.items():
        count, pos = 0, None
        for pos_i, k in enumerate(idx):
            t, o, h, l, c, sp = bars[k]
            tl = t.time()
            if pos is not None:
                kind, entry, stop, level, et = pos
                half = sp * cost_mult / 2
                exit_px = reason = None
                if (kind == 1 and l <= stop) or (kind == -1 and h >= stop):
                    # luka: świeca otwarta już za stopem wypełnia się po otwarciu, nie po lepszej cenie stopu
                    fill = min(stop, o) if kind == 1 else max(stop, o)
                    exit_px = fill - kind * (STOP_EXTRA * cost_mult) - kind * half; reason = "STOP"
                elif tl >= time(17, 25):
                    exit_px = o - kind * half; reason = "FLAT"
                if exit_px is None:
                    back = c < level if kind == 1 else c > level
                    if back and pos_i + 1 < len(idx):
                        no = bars[idx[pos_i + 1]]
                        exit_px = no[1] - kind * (SLIP * cost_mult) - kind * (no[5] * cost_mult / 2); reason = "RANGE"
                if exit_px is not None:
                    trades.append((day, et, kind, entry, exit_px, reason, kind * (exit_px - entry)))
                    pos = None
                if pos is not None and pos_i == len(idx) - 1:  # koniec danych dnia przed 17:25: wyjście po ostatnim zamknięciu
                    kind, entry, stop, level, et = pos
                    px = c - kind * (sp * cost_mult / 2)
                    trades.append((day, et, kind, entry, px, "END_OF_DATA", kind * (px - entry))); pos = None
                continue
            if count >= MAX_PER_DAY or pos_i < n + ATR_N or not (time(9, 15) <= tl < time(17, 0)):
                continue
            prev = [bars[j] for j in idx[pos_i - n:pos_i]]
            hi, lo = max(p[2] for p in prev), min(p[3] for p in prev)
            win = [bars[j] for j in idx[pos_i - ATR_N:pos_i]]
            atr = statistics.mean(max(w[2] - w[3], abs(w[2] - bars[idx[pos_i - ATR_N + q - 1]][4]) if q else 0,
                                      abs(w[3] - bars[idx[pos_i - ATR_N + q - 1]][4]) if q else 0)
                                  for q, w in enumerate(win))
            kind = 1 if c > hi else -1 if c < lo else 0
            if kind and pos_i + 1 < len(idx):
                no = bars[idx[pos_i + 1]]
                entry = no[1] + kind * (SLIP * cost_mult) + kind * (no[5] * cost_mult / 2)
                level = hi if kind == 1 else lo
                pos = (kind, entry, level - kind * atr, level, no[0]); count += 1
    return trades


def stats(trades):
    if not trades:
        return "brak transakcji"
    pnl = [t[6] for t in trades]
    gross_w = sum(p for p in pnl if p > 0); gross_l = -sum(p for p in pnl if p < 0)
    cum = peak = dd = 0.0
    for p in pnl:
        cum += p; peak = max(peak, cum); dd = max(dd, peak - cum)
    pf = gross_w / gross_l if gross_l else float("inf")
    return (f"n={len(pnl)} netto={sum(pnl):+.1f} pkt (={sum(pnl) * POINT_EUR:+.2f} EUR przy 0,001) "
            f"śr/transakcję={statistics.mean(pnl):+.2f} pkt trafność={sum(p > 0 for p in pnl) / len(pnl):.0%} "
            f"PF={pf:.2f} maks.obsunięcie={dd:.1f} pkt")


def main():
    bars = load()
    days = sorted({b[0].date() for b in bars})
    print(f"świece: {len(bars)}, dni: {len(days)} ({days[0]} .. {days[-1]})")
    cut = days[int(len(days) * 2 / 3)]
    for n in (15,):
        tr = run(bars, n)
        ins = [t for t in tr if t[0] < cut]; oos = [t for t in tr if t[0] >= cut]
        print(f"\nMETODA N={n} (parametr z góry)")
        print("  całość       :", stats(tr)); print(f"  próba (do {cut}):", stats(ins)); print(f"  POZA PRÓBĄ   :", stats(oos))
        by = {}
        for t in tr:
            by[t[5]] = by.get(t[5], 0) + 1
        print("  wyjścia      :", by)
        print("  koszty x1.5  :", stats(run(bars, n, 1.5))); print("  koszty x2    :", stats(run(bars, n, 2.0)))
        with OUTCSV.open("w", newline="") as f:
            w = csv.writer(f); w.writerow(["dzien", "wejscie_berlin", "kierunek", "cena_wej", "cena_wyj", "powod", "wynik_pkt"])
            for t in tr:
                w.writerow([t[0], t[1].isoformat(), "LONG" if t[2] == 1 else "SHORT", round(t[3], 1), round(t[4], 1), t[5], round(t[6], 2)])
    print("\nKontrola odporności (NIE do wyboru parametru): N=10 / N=30")
    for n in (10, 30):
        print(f"  N={n}:", stats(run(bars, n)))
    tr = run(bars, 15)
    if len(tr) < 100:
        print(f"\nUWAGA: tylko {len(tr)} transakcji (<100): wynik jest poglądowy, nie dowodzi niczego.")


main()
