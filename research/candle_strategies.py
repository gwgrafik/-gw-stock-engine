"""Test historyczny reguł B1-B4 (opracowanie "Świece, wybicia i reguły bota") na świecach DE40, z kosztami.

NARZĘDZIE DO ODRZUCANIA ALBO PRZYJMOWANIA REGUŁ, nie bot. Niczego nie handluje.
Uruchomienie: docker compose ... run --rm -T gw-stock-engine python - < research/candle_strategies.py
Dane: /data/m1_<epic>.csv z research/fetch_m1.py (kolumny bid/ask osobno; gdy ich nie ma, bid/ask rekonstruowane ze spreadu
zamknięcia: wynik jest wtedy przybliżony i skrypt to wypisuje).

WARIANTY ZADEKLAROWANE PRZED URUCHOMIENIEM (każda zmiana po obejrzeniu wyniku = nowy wariant):
  reguły: B1 B2 B3 B4 (B3 tylko na M1), interwał: M1 (N=15 świec, limity czasu 10/10/15/5 min) i M5 (N=12 świec, limity x5),
  okno sygnałów: ALL (09:15-17:00 Berlin) i SESJE (09:15-11:30 oraz 15:30-17:00 Berlin).  Razem 7 x 2 = 14 wariantów.
Zasady wykonania (wspólne): sygnał tylko ze świecy ZAKOŃCZONEJ, wejście na otwarciu następnej (long po ask, short po bid),
  poślizg 0,5 pkt na wejściu i wyjściu, +1,0 pkt przy wyjściu stopem (config/costs_v1.toml, tylko DE40); wyjście zaplanowane
  (czas, 17:25) na otwarciu świecy bez korzystania z jej zakresu; stop i cel w jednej świecy: STOP; luka za stopem: po otwarciu;
  koniec danych przed wyjściem: jawnie END_OF_DATA; sygnał z dziurawych danych (brak minut) jest pomijany (GAPPED_DATA).
Filtry: stop bliżej niż 0,01% ceny, odchylenie od zamknięcia sygnału > 0,5 ATR14, koszt round-trip > 0,20 R, cel bliżej niż 1,2 R (B2).
Jedna pozycja naraz, najwyżej 10 wejść dziennie, koniec dnia po -3 R.
PODZIAŁ CHRONOLOGICZNY: 60% dni = opracowanie, 20% = wybór, 20% = ocena końcowa. Część końcowa jest pokazywana TYLKO dla
  wariantów, które przeszły wybór (>= 30 transakcji, netto > 0 przy kosztach x1,5, PF > 1,2). Przyjęcie: ocena końcowa z
  >= 30 transakcji, netto > 0 przy x1,5 i PF > 1,2. Wynik NIE uwzględnia, że sprawdzono 14 wariantów: kilka "sukcesów" z 14 może
  być samym przypadkiem, więc przyjęcie oznacza tylko "warto badać dalej w dry_run", nie zysk.
Instrument inny niż DE40: ustaw GW_EPIC, GW_SLIP, GW_STOP_EXTRA, GW_STEP, GW_POINT_EUR (wartość punktu przy minimalnej wielkości).
"""
import csv, os, statistics
from collections import namedtuple
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EPIC = os.environ.get("GW_EPIC", "DE40")
CSV = Path(os.environ.get("GW_M1_CSV", f"/data/m1_{EPIC.lower()}.csv"))
OUT = Path(os.environ.get("GW_TRADES_CSV", f"/data/candle_trades_{EPIC.lower()}.csv"))
BER = ZoneInfo("Europe/Berlin")
if EPIC != "DE40" and not all(k in os.environ for k in ("GW_SLIP", "GW_STOP_EXTRA", "GW_STEP", "GW_POINT_EUR")):
    raise SystemExit("inny instrument niż DE40: ustaw GW_SLIP, GW_STOP_EXTRA, GW_STEP i GW_POINT_EUR (koszty z costs_v1.toml dotyczą DE40)")
SLIP = float(os.environ.get("GW_SLIP", "0.5")); STOP_EXTRA = float(os.environ.get("GW_STOP_EXTRA", "1.0"))
STEP = float(os.environ.get("GW_STEP", "0.1")); POINT_EUR = float(os.environ.get("GW_POINT_EUR", "0.001"))
ATR_N, MAX_DAY, DAY_STOP_R, MIN_STOP_FRAC = 14, 10, -3.0, 0.0001
Bar = namedtuple("Bar", "t bo bh bl bc ao ah al ac")
WINDOWS = {"ALL": [(time(9, 15), time(17, 0))], "SESJE": [(time(9, 15), time(11, 30)), (time(15, 30), time(17, 0))]}
TF = {"M1": dict(n=15, minutes=1, limits={"B1": 10, "B2": 10, "B3": 15, "B4": 5}),
      "M5": dict(n=12, minutes=5, limits={"B1": 50, "B2": 50, "B3": 75, "B4": 25})}


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


def aggregate(bars, minutes):
    if minutes == 1:
        return bars
    groups = {}
    for b in bars:
        groups.setdefault(b.t.replace(minute=b.t.minute - b.t.minute % minutes), []).append(b)
    out = []
    for s in sorted(groups):
        g = groups[s]
        if len(g) == minutes and all((g[k + 1].t - g[k].t) == timedelta(minutes=1) for k in range(minutes - 1)):
            out.append(Bar(s, g[0].bo, max(x.bh for x in g), min(x.bl for x in g), g[-1].bc,
                           g[0].ao, max(x.ah for x in g), min(x.al for x in g), g[-1].ac))
    return out


def atr_series(bars):
    m = [mid(b) for b in bars]
    tr = [m[0][1] - m[0][2]] + [max(m[i][1] - m[i][2], abs(m[i][1] - m[i - 1][3]), abs(m[i][2] - m[i - 1][3]))
                                for i in range(1, len(m))]
    return [None if i < ATR_N else statistics.mean(tr[i - ATR_N + 1:i + 1]) for i in range(len(m))]


def ema_series(values, n):
    k, out, e = 2 / (n + 1), [], None
    for v in values:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def m5_context(bars):
    """Dla każdej świecy M1: (EMA20, EMA50, EMA50 sprzed 3 zakończonych M5) z M5 ZAKOŃCZONYCH do końca tej świecy."""
    m5 = aggregate(bars, 5)
    closes = [mid(b)[3] for b in m5]
    e20, e50 = ema_series(closes, 20), ema_series(closes, 50)
    ends = [b.t + timedelta(minutes=5) for b in m5]
    ctx, j = [None] * len(bars), -1
    for i, b in enumerate(bars):
        end = b.t + timedelta(minutes=1)
        while j + 1 < len(ends) and ends[j + 1] <= end:
            j += 1
        if j >= 53:
            ctx[i] = (e20[j], e50[j], e50[j - 3])
    return ctx


def signals(name, n, limits, bars, i, m1ema, ctx, spread):
    """(kierunek, stop, cel albo ("level", cena), limit_minut) dla zakończonej świecy i; None gdy brak sygnału."""
    seg = bars[i - n - 1:i + 1]
    m = [mid(x) for x in seg]
    o, h, l, c = m[-1]
    rng = h - l
    if rng <= 0:
        return None
    b = max(STEP, 0.25 * spread)
    prior = m[-n - 1:-1]  # dokładnie n poprzednich zakończonych świec (bez bieżącej)
    upper, lower = max(x[1] for x in prior), min(x[2] for x in prior)
    body, clv = abs(c - o) / rng, (c - l) / rng
    lim = limits[name]
    if name == "B1":
        if c > upper + b and body >= 0.60 and clv >= 0.75:
            return (1, min(l, upper - b), 1.5, lim)
        if c < lower - b and body >= 0.60 and clv <= 0.25:
            return (-1, max(h, lower + b), 1.5, lim)
    elif name == "B2":
        lo_w, up_w, mid_prior = (min(o, c) - l) / rng, (h - max(o, c)) / rng, (upper + lower) / 2
        if l < lower - b and c > lower + b and lo_w >= 0.50 and clv >= 0.65:
            return (1, l - b, ("level", mid_prior), lim)
        if h > upper + b and c < upper - b and up_w >= 0.50 and clv <= 0.35:
            return (-1, h + b, ("level", mid_prior), lim)
    elif name == "B3" and ctx is not None and m1ema is not None:
        e20_m5, e50, e50_old = ctx  # filtr trendu: M5 EMA20 > EMA50 i EMA50 rośnie względem 3 zakończonych M5 wcześniej
        prev = m[-2]
        ema1 = m1ema  # EMA20 liczona na M1 (świeca t-1 dotyka EMA20 z M1)
        if e20_m5 > e50 > e50_old and prev[2] <= ema1 <= prev[1] and c > prev[1] and c > ema1:
            return (1, min(prev[2], l) - b, 2.0, lim)
        if e20_m5 < e50 < e50_old and prev[2] <= ema1 <= prev[1] and c < prev[2] and c < ema1:
            return (-1, max(prev[1], h) + b, 2.0, lim)
    elif name == "B4":
        inside, mother = m[-2], m[-3]  # inside bar = świeca t-1; potwierdza zakończona świeca t ponad/poniżej ŚWIECY NADRZĘDNEJ
        if inside[1] < mother[1] and inside[2] > mother[2]:
            if c > mother[1] + b:
                return (1, inside[2] - b, 1.5, lim)
            if c < mother[2] - b:
                return (-1, inside[1] + b, 1.5, lim)
    return None


def in_window(tl, window):
    return any(a <= tl < z for a, z in WINDOWS[window])


def run(name, tf, window, bars, atr, ctx, m1ema):
    cfg = TF[tf]; n, step_min = cfg["n"], cfg["minutes"]
    trades, skips, by_day = [], {}, {}
    for i, b in enumerate(bars):
        by_day.setdefault(b.t.astimezone(BER).date(), []).append(i)

    def skip(why):
        skips[why] = skips.get(why, 0) + 1

    for day, idx in by_day.items():
        n_day, r_day, k = 0, 0.0, 0
        while k < len(idx) - 1:
            i = idx[k]; k += 1
            tl = bars[i].t.astimezone(BER).time()
            if i < n + 2 or atr[i] is None or not in_window(tl, window):
                continue
            if n_day >= MAX_DAY or r_day <= DAY_STOP_R:
                break
            if (bars[i].t - bars[i - n - 1].t) > timedelta(minutes=(n + 1) * step_min + 2 * step_min):
                skip("GAPPED_DATA"); continue
            spread = bars[i].ac - bars[i].bc
            sig = signals(name, n, cfg["limits"], bars, i, m1ema[i] if m1ema else None, ctx[i] if ctx else None, spread)
            if sig is None:
                continue
            side, stop, tgt, limit_min = sig
            nb = bars[idx[k]]
            entry = (nb.ao + SLIP) if side == 1 else (nb.bo - SLIP)
            entry_cost = SLIP + (nb.ao - nb.bo) / 2
            r_pts = (entry - stop) if side == 1 else (stop - entry)
            if r_pts < MIN_STOP_FRAC * entry:
                skip("STOP_TOO_CLOSE"); continue
            if abs(entry - mid(bars[i])[3]) > 0.5 * atr[i]:
                skip("SLIP_FILTER"); continue
            cost = spread + 2 * SLIP
            if cost / r_pts > 0.20:
                skip("COST_OVER_0.20R"); continue
            if isinstance(tgt, tuple):
                target = tgt[1]
                if (target - entry) * side <= 0 or abs(target - entry) < 1.2 * r_pts:
                    skip("TARGET_TOO_NEAR"); continue
            else:
                target = entry + side * tgt * r_pts
            exit_px = reason = None; exit_cost = 0.0; last = k
            for j in range(k, len(idx)):
                b = bars[idx[j]]; last = j
                held = (b.t - nb.t).total_seconds() / 60
                half = (b.ao - b.bo) / 2
                if b.t.astimezone(BER).time() >= time(17, 25) or held >= limit_min:  # wyjście na OTWARCIU, bez zakresu tej świecy
                    reason = "TIME" if held >= limit_min else "FLAT"
                    exit_px = (b.bo - SLIP) if side == 1 else (b.ao + SLIP); exit_cost = SLIP + half
                elif side == 1 and b.bl <= stop:
                    exit_px = min(stop, b.bo) - STOP_EXTRA; reason = "STOP"; exit_cost = STOP_EXTRA + half
                elif side == -1 and b.ah >= stop:
                    exit_px = max(stop, b.ao) + STOP_EXTRA; reason = "STOP"; exit_cost = STOP_EXTRA + half
                elif side == 1 and b.bh >= target:
                    exit_px, reason, exit_cost = target, "TARGET", half
                elif side == -1 and b.al <= target:
                    exit_px, reason, exit_cost = target, "TARGET", half
                if exit_px is not None:
                    break
            if exit_px is None:  # koniec danych dnia przed wyjściem: jawnie oznaczone
                b = bars[idx[-1]]; last = len(idx) - 1
                exit_px, reason = (b.bc - SLIP if side == 1 else b.ac + SLIP), "END_OF_DATA"; exit_cost = SLIP + (b.ac - b.bc) / 2
            pnl = side * (exit_px - entry)
            trades.append((day, nb.t, side, entry, exit_px, reason, pnl, pnl / r_pts, cost, r_pts, pnl + entry_cost + exit_cost))
            n_day += 1; r_day += pnl / r_pts
            k = max(k, last + 1)
    return trades, skips


def stats(tr, mult=1.0):
    if not tr:
        return "brak transakcji"
    pnl = [t[6] - (mult - 1) * t[8] for t in tr]
    gross = sum(t[10] for t in tr)
    w = sum(p for p in pnl if p > 0); lo = -sum(p for p in pnl if p < 0)
    cum = peak = dd = 0.0
    for p in pnl:
        cum += p; peak = max(peak, cum); dd = max(dd, peak - cum)
    wins = [p for p in pnl if p > 0]
    return (f"n={len(pnl)} BRUTTO={gross:+.0f} NETTO={sum(pnl):+.0f} pkt ({sum(pnl) * POINT_EUR:+.2f} EUR) śr={statistics.mean(pnl):+.2f} "
            f"traf.={len(wins) / len(pnl):.0%} PF={(w / lo) if lo else float('inf'):.2f} DD={dd:.0f}")


def passes(tr, min_n=30):
    if len(tr) < min_n:
        return False
    p = [t[6] - 0.5 * t[8] for t in tr]
    w = sum(x for x in p if x > 0); lo = -sum(x for x in p if x < 0)
    return sum(p) > 0 and lo > 0 and w / lo > 1.2


def main():
    raw, real = load()
    days = sorted({b.t.astimezone(BER).date() for b in raw})
    print(f"{EPIC}: świece M1: {len(raw)}, dni: {len(days)} ({days[0]} .. {days[-1]}); bid/ask z danych: {'TAK' if real else 'NIE (rekonstrukcja ze spreadu, wynik przybliżony)'}")
    c1, c2 = days[int(len(days) * 0.6)], days[int(len(days) * 0.8)]
    print(f"podział: opracowanie < {c1} | wybór {c1} .. < {c2} | ocena końcowa >= {c2} (otwierana tylko dla kandydatów)")
    cache, variants, candidates, accepted = {}, 0, 0, 0
    with OUT.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["wariant", "dzien", "wejscie_utc", "kierunek", "cena_wej", "cena_wyj", "powod", "wynik_pkt", "wynik_R", "brutto_pkt"])
        for tf in ("M1", "M5"):
            if tf not in cache:
                bars = aggregate(raw, TF[tf]["minutes"])
                cache[tf] = (bars, atr_series(bars), m5_context(bars) if tf == "M1" else None,
                             ema_series([mid(b)[3] for b in bars], 20) if tf == "M1" else None)
            bars, atr, ctx, m1ema = cache[tf]
            for window in ("ALL", "SESJE"):
                for name in ("B1", "B2", "B3", "B4"):
                    if name == "B3" and tf != "M1":
                        continue
                    variants += 1
                    tr, skips = run(name, tf, window, bars, atr, ctx, m1ema)
                    label = f"{name}/{tf}/{window}"
                    tra = [t for t in tr if t[0] < c1]; val = [t for t in tr if c1 <= t[0] < c2]; fin = [t for t in tr if t[0] >= c2]
                    print(f"\n=== {label}: {len(tr)} transakcji; pominięte: {skips}")
                    print("  opracowanie:", stats(tra)); print("  wybór      :", stats(val)); print("  wybór x1.5 :", stats(val, 1.5))
                    if passes(val):
                        candidates += 1
                        print("  >>> KANDYDAT (przeszedł wybór). OCENA KOŃCOWA:", stats(fin), "|", stats(fin, 1.5))
                        if passes(fin):
                            accepted += 1; print("  >>> PRZYJĘTA do dalszych prób (nie dowód zysku)")
                        else:
                            print("  >>> ODRZUCONA w ocenie końcowej")
                    else:
                        print("  odrzucona w wyborze (ocena końcowa nieotwarta)")
                    for t in tr:
                        w.writerow([label, t[0], t[1].isoformat(), "LONG" if t[2] == 1 else "SHORT", round(t[3], 1), round(t[4], 1), t[5], round(t[6], 2), round(t[7], 2), round(t[10], 2)])
    print(f"\nPODSUMOWANIE: wariantów {variants}, kandydatów po wyborze {candidates}, przyjętych po ocenie końcowej {accepted}.")
    print("Uwaga: przy 14 sprawdzonych wariantach pojedynczy 'sukces' może być przypadkiem. Przyjęta reguła = kandydat do dry_run, nie zysk.")


if __name__ == "__main__":
    main()
