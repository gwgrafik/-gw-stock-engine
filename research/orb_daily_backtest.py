"""Test historyczny STRATEGII SILNIKA (orb_index_cfd v1) na świecach M1 DE40: pierwsza świeca 5 min (09:00-09:05 Berlin) wyznacza
kierunek (wzrostowa = long, spadkowa = short, doji = pominięcie), wejście na otwarciu 09:05, stop 0,10 x ATR14 (dzienny) zaokrąglony
w górę do 0,1 pkt, wyjście stopem albo 17:25. Jedna transakcja dziennie. Koszty jak w silniku: bid/ask z danych, poślizg 0,5 pkt,
+1,0 pkt przy wyjściu stopem (config/costs_v1.toml). Punkty odniesienia: zawsze long, zawsze short (te same czasy i stopy).
UWAGA: dzienne ATR liczone z M1 po dobie kalendarzowej Berlina (silnik używa świec dziennych brokera, granica doby może się różnić),
a próba to ok. 87 dni = ok. 87 transakcji: wynik jest informacją, nie dowodem.
Uruchomienie: docker compose ... run --rm -T gw-stock-engine python - < research/orb_daily_backtest.py   (plik /data/m1_de40.csv)
"""
import csv, math, os, statistics
from collections import namedtuple
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CSV = Path(os.environ.get("GW_M1_CSV", "/data/m1_de40.csv"))
OUT = Path(os.environ.get("GW_TRADES_CSV", "/data/orb_daily_trades.csv"))
BER = ZoneInfo("Europe/Berlin")
SLIP, STOP_EXTRA, STEP, POINT_EUR = 0.5, 1.0, 0.1, 0.001
Bar = namedtuple("Bar", "t bo bh bl bc ao ah al ac")


def load():
    bars, real = [], True
    with CSV.open() as f:
        for r in csv.DictReader(f):
            t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if "bid_o" in r:
                v = [float(r[k]) for k in ("bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c")]
            else:
                real = False
                sp = float(r["spread"]) or 1.8
                m = [float(r[k]) for k in ("open", "high", "low", "close")]
                v = [x - sp / 2 for x in m] + [x + sp / 2 for x in m]
            bars.append(Bar(t, *v))
    bars.sort(key=lambda b: b.t)
    return bars, real


def mid(b):
    return ((b.bo + b.ao) / 2, (b.bh + b.ah) / 2, (b.bl + b.al) / 2, (b.bc + b.ac) / 2)


def daily_atr(bars_by_day):
    days = sorted(bars_by_day)
    tr, prev_close, out = [], None, {}
    for d in days:
        m = [mid(b) for b in bars_by_day[d]]
        hi, lo, cl = max(x[1] for x in m), min(x[2] for x in m), m[-1][3]
        out[d] = statistics.mean(tr[-14:]) if len(tr) >= 14 else None  # ATR z 14 PRZEDOSTATNICH dni, bez bieżącego
        tr.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)) if prev_close is not None else hi - lo)
        prev_close = cl
    return out


def simulate(bars, side, stop_dist):
    """bars: świece dnia od 09:05 Berlin. Zwraca (wejście, wyjście, powód, koszt_łączny)."""
    b0 = bars[0]
    entry = (b0.ao + SLIP) if side == 1 else (b0.bo - SLIP)
    entry_cost = SLIP + (b0.ao - b0.bo) / 2
    stop = entry - side * stop_dist
    for b in bars:
        half = (b.ao - b.bo) / 2
        if b.t.astimezone(BER).time() >= time(17, 25):
            px = (b.bo - SLIP) if side == 1 else (b.ao + SLIP)
            return entry, px, "FLAT", entry_cost + SLIP + half
        if side == 1 and b.bl <= stop:
            return entry, min(stop, b.bo) - STOP_EXTRA, "STOP", entry_cost + STOP_EXTRA + half
        if side == -1 and b.ah >= stop:
            return entry, max(stop, b.ao) + STOP_EXTRA, "STOP", entry_cost + STOP_EXTRA + half
    b = bars[-1]
    return entry, (b.bc - SLIP) if side == 1 else (b.ac + SLIP), "END_OF_DATA", entry_cost + SLIP + (b.ac - b.bc) / 2


def stats(rows, mult=1.0):
    if not rows:
        return "brak transakcji"
    pnl = [r["pnl"] - (mult - 1) * r["cost"] for r in rows]
    gross = sum(r["pnl"] + r["cost"] for r in rows)
    w = sum(p for p in pnl if p > 0); lo = -sum(p for p in pnl if p < 0)
    cum = peak = dd = 0.0
    for p in pnl:
        cum += p; peak = max(peak, cum); dd = max(dd, peak - cum)
    wins = [p for p in pnl if p > 0]; losses = [p for p in pnl if p <= 0]
    return (f"n={len(pnl)} BRUTTO={gross:+.0f} NETTO={sum(pnl):+.0f} pkt ({sum(pnl) * POINT_EUR:+.2f} EUR/0,001) śr={statistics.mean(pnl):+.1f} "
            f"traf.={len(wins) / len(pnl):.0%} śr.wygr.={statistics.mean(wins) if wins else 0:.0f} śr.str.={statistics.mean(losses) if losses else 0:.0f} "
            f"PF={(w / lo) if lo else float('inf'):.2f} DD={dd:.0f} pkt")


def main():
    raw, real = load()
    by_day = {}
    for b in raw:
        by_day.setdefault(b.t.astimezone(BER).date(), []).append(b)
    atr = daily_atr(by_day)
    print(f"świece M1: {len(raw)}, dni: {len(by_day)}; bid/ask z danych: {'TAK' if real else 'NIE (rekonstrukcja, wynik przybliżony)'}")
    res = {"ORB": [], "ZAWSZE_LONG": [], "ZAWSZE_SHORT": []}; skips = {}
    for d in sorted(by_day):
        day = by_day[d]
        first = [b for b in day if time(9, 0) <= b.t.astimezone(BER).time() < time(9, 5)]
        after = [b for b in day if b.t.astimezone(BER).time() >= time(9, 5)]
        if d.weekday() >= 5 or len(first) != 5 or not after or after[0].t.astimezone(BER).time() != time(9, 5):
            skips["BRAK_DANYCH_LUB_WEEKEND"] = skips.get("BRAK_DANYCH_LUB_WEEKEND", 0) + 1; continue
        if atr[d] is None:
            skips["BRAK_ATR14"] = skips.get("BRAK_ATR14", 0) + 1; continue
        o, c = mid(first[0])[0], mid(first[-1])[3]
        stop_dist = math.ceil(round(0.10 * atr[d] / STEP, 6)) * STEP
        for name, side in (("ORB", 1 if c > o else -1 if c < o else 0), ("ZAWSZE_LONG", 1), ("ZAWSZE_SHORT", -1)):
            if side == 0:
                skips["DOJI"] = skips.get("DOJI", 0) + 1; continue
            e, x, why, cost = simulate(after, side, stop_dist)
            res[name].append(dict(day=d, side=side, entry=e, exit=x, why=why, pnl=side * (x - e), cost=cost, stop=stop_dist))
    print("pominięte dni:", skips)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["wariant", "dzien", "kierunek", "cena_wej", "cena_wyj", "powod", "wynik_pkt", "stop_pkt"])
        for k, rows in res.items():
            for r in rows:
                w.writerow([k, r["day"], "LONG" if r["side"] == 1 else "SHORT", round(r["entry"], 1), round(r["exit"], 1), r["why"], round(r["pnl"], 1), round(r["stop"], 1)])
    for k, rows in res.items():
        print(f"\n== {k}\n  koszty x1  :", stats(rows)); print("  koszty x1.5:", stats(rows, 1.5))
        reasons = {}
        for r in rows:
            reasons[r["why"]] = reasons.get(r["why"], 0) + 1
        print("  wyjścia:", reasons)
        if k == "ORB":
            for side, nm in ((1, "LONG"), (-1, "SHORT")):
                print(f"  tylko {nm}:", stats([r for r in rows if r["side"] == side]))
            months = {}
            for r in rows:
                months.setdefault(r["day"].strftime("%Y-%m"), []).append(r["pnl"])
            print("  miesięcznie (netto pkt):", {m: round(sum(v)) for m, v in sorted(months.items())})
    print("\nOdczyt: jeśli ORB nie jest wyraźnie lepsze od ZAWSZE_LONG/ZAWSZE_SHORT, kolor pierwszej świecy nic nie wnosi.")
    print("Ok. 87 transakcji nie dowodzi niczego: odchylenie wyniku dziennego przekracza zwykle sam średni wynik.")


if __name__ == "__main__":
    main()
