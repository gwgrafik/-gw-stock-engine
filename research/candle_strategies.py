"""Test historyczny reguł B1-B4 z opracowania "Świece, wybicia i reguły bota" na świecach M1 DE40, z kosztami.

TO JEST NARZĘDZIE DO ODRZUCANIA albo przyjmowania reguł, nie bot. Niczego nie wysyła ani nie handluje.
Uruchomienie: docker compose ... run --rm -T gw-stock-engine python - < research/candle_strategies.py
Dane: /data/m1_de40.csv z research/fetch_m1.py (kolumny bid/ask osobno; starszy plik tylko z mid: ask/bid z spreadu).

Zasady (ustalone z góry, żadnych poprawek po obejrzeniu wyniku):
- sygnał tylko ze świecy ZAKOŃCZONEJ; wejście po następnym kwotowaniu (otwarcie następnej świecy): long po ask, short po bid;
- poślizg 0,5 pkt przy wejściu i wyjściu, dodatkowy 1,0 pkt przy wyjściu stopem (config/costs_v1.toml);
- stop i cel w tej samej minucie: liczymy STOP (konserwatywnie); luka za stopem: wypełnienie po otwarciu świecy;
- wejście odrzucone, gdy: stop bliżej niż 0,01% ceny (reguła brokera DE40), odchylenie od zamknięcia sygnału > 0,5 ATR14,
  koszt round-trip (spread + 2 x poślizg) > 0,20 R;
- jedna pozycja naraz, okno sygnałów 09:15-17:00 Berlin, flatten 17:25, najwyżej 10 wejść dziennie i koniec dnia po -3 R;
- okres poza próbą = ostatnia 1/3 dni; parametry nigdy nie dobierane do wyniku.
Kryterium przyjęcia (z góry): POZA PRÓBĄ co najmniej 100 transakcji z co najmniej 15 różnych dni, netto > 0 przy kosztach x1,5
i profit factor > 1,2. Inaczej reguła jest ODRZUCONA albo ZA MAŁO DANYCH. Nawet przyjęta nie oznacza zysku na żywo.
"""
import csv, os, statistics
from collections import namedtuple
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CSV = Path(os.environ.get("GW_M1_CSV", "/data/m1_de40.csv"))
OUT = Path(os.environ.get("GW_TRADES_CSV", "/data/candle_trades.csv"))
BER = ZoneInfo("Europe/Berlin")
SLIP, STOP_EXTRA, STEP = 0.5, 1.0, 0.1
ATR_N, N_RANGE, MAX_DAY, DAY_STOP_R = 14, 15, 10, -3.0
POINT_EUR = 0.001
Bar = namedtuple("Bar", "t bo bh bl bc ao ah al ac")


def load():
    bars = []
    with CSV.open() as f:
        for r in csv.DictReader(f):
            t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if "bid_o" in r:
                v = [float(r[k]) for k in ("bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c")]
            else:
                sp = float(r["spread"]) or 1.8
                m = [float(r[k]) for k in ("open", "high", "low", "close")]
                v = [x - sp / 2 for x in m] + [x + sp / 2 for x in m]
            bars.append(Bar(t, *v))
    bars.sort(key=lambda b: b.t)
    return bars


def mid(b):  # (o, h, l, c) po środku
    return ((b.bo + b.ao) / 2, (b.bh + b.ah) / 2, (b.bl + b.al) / 2, (b.bc + b.ac) / 2)


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
    """Dla każdej świecy M1 i: (EMA20, EMA50, EMA50 sprzed 3 zakończonych M5) z M5 ZAKOŃCZONYCH do końca świecy i."""
    groups = {}
    for i, b in enumerate(bars):
        groups.setdefault(b.t.replace(minute=b.t.minute - b.t.minute % 5), []).append(i)
    starts = sorted(groups)
    closes, ready = [], {}
    for s in starts:
        ix = groups[s]
        if len(ix) == 5:
            closes.append(mid(bars[ix[-1]])[3]); ready[s] = len(closes) - 1
    e20, e50 = ema_series(closes, 20), ema_series(closes, 50)
    ctx = [None] * len(bars)
    for s, ix in groups.items():
        for i in ix:
            end = bars[i].t + timedelta(minutes=1)
            done = [x for x in starts if x in ready and x + timedelta(minutes=5) <= end]
            if len(done) >= 54:
                j = ready[done[-1]]; ctx[i] = (e20[j], e50[j], e50[j - 3])
    return ctx


def signals(name, bars, i, atr, m1ema, ctx, spread):
    """Zwraca (kierunek, stop, cel_lub_None, limit_minut) dla zakończonej świecy i, albo None."""
    m = [mid(b) for b in bars[i - N_RANGE - 1:i + 1]]
    o, h, l, c = m[-1]
    rng = h - l
    if rng <= 0:
        return None
    b = max(STEP, 0.25 * spread)
    prior = m[-N_RANGE - 1:-1]
    upper, lower = max(x[1] for x in prior), min(x[2] for x in prior)
    body, clv = abs(c - o) / rng, (c - l) / rng
    if name == "B1":
        if c > upper + b and body >= 0.60 and clv >= 0.75:
            return (1, min(l, upper - b), 1.5, 10)
        if c < lower - b and body >= 0.60 and clv <= 0.25:
            return (-1, max(h, lower + b), 1.5, 10)
    elif name == "B2":
        lo_w, up_w = (min(o, c) - l) / rng, (h - max(o, c)) / rng
        mid_prior = (upper + lower) / 2
        if l < lower - b and c > lower + b and lo_w >= 0.50 and clv >= 0.65:
            return (1, l - b, ("level", mid_prior), 10)
        if h > upper + b and c < upper - b and up_w >= 0.50 and clv <= 0.35:
            return (-1, h + b, ("level", mid_prior), 10)
    elif name == "B3" and ctx is not None and m1ema is not None:
        e20, e50, e50_old = ctx
        prev = m[-2]
        if e20 > e50 > e50_old and prev[2] <= e20 and c > prev[1] and c > m1ema:
            return (1, min(prev[2], l) - b, 2.0, 15)
        if e20 < e50 < e50_old and prev[1] >= e20 and c < prev[2] and c < m1ema:
            return (-1, max(prev[1], h) + b, 2.0, 15)
    elif name == "B4":
        p, q = m[-2], m[-3]  # p: świeca inside, q: poprzednia
        if p[1] < q[1] and p[2] > q[2]:
            if c > p[1] + b:  # potwierdzenie long ponad H[t-1]
                return (1, p[2] - b, 1.5, 5)
            if c < p[2] - b:
                return (-1, p[1] + b, 1.5, 5)
    return None


def run(name, bars, atr, ctx, m1ema):
    trades, skips = [], {}
    by_day = {}
    for i, b in enumerate(bars):
        by_day.setdefault(b.t.astimezone(BER).date(), []).append(i)
    for day, idx in by_day.items():
        n_day, r_day, k = 0, 0.0, 0
        while k < len(idx) - 1:
            i = idx[k]; k += 1
            tl = bars[i].t.astimezone(BER).time()
            if i < max(N_RANGE + 2, ATR_N + 1) or atr[i] is None or not (time(9, 15) <= tl < time(17, 0)):
                continue
            if n_day >= MAX_DAY or r_day <= DAY_STOP_R:
                break
            spread = bars[i].ac - bars[i].bc
            sig = signals(name, bars, i, atr[i], m1ema[i], ctx[i], spread)
            if sig is None:
                continue
            side, stop, tgt, limit_min = sig
            nb = bars[idx[k]]
            entry = (nb.ao + SLIP) if side == 1 else (nb.bo - SLIP)
            r_pts = (entry - stop) if side == 1 else (stop - entry)
            ref = mid(bars[i])[3]
            if r_pts < 0.0001 * entry:
                skips["STOP_TOO_CLOSE"] = skips.get("STOP_TOO_CLOSE", 0) + 1; continue
            if abs(entry - ref) > 0.5 * atr[i]:
                skips["SLIP_FILTER"] = skips.get("SLIP_FILTER", 0) + 1; continue
            cost = spread + 2 * SLIP
            if cost / r_pts > 0.20:
                skips["COST_OVER_0.20R"] = skips.get("COST_OVER_0.20R", 0) + 1; continue
            if isinstance(tgt, tuple):
                target = tgt[1]
                if abs(target - entry) < 1.2 * r_pts or (target - entry) * side <= 0:
                    skips["TARGET_TOO_NEAR"] = skips.get("TARGET_TOO_NEAR", 0) + 1; continue
            else:
                target = entry + side * tgt * r_pts
            exit_px = reason = None
            t0 = nb.t
            for j in range(k, len(idx)):
                b = bars[idx[j]]
                held = (b.t - t0).total_seconds() / 60
                if side == 1:
                    if b.bl <= stop:
                        exit_px, reason = min(stop, b.bo) - STOP_EXTRA - SLIP * 0, "STOP"
                    elif b.bh >= target:
                        exit_px, reason = target, "TARGET"
                    elif b.t.astimezone(BER).time() >= time(17, 25) or held >= limit_min:
                        exit_px, reason = b.bo - SLIP, "TIME" if held >= limit_min else "FLAT"
                else:
                    if b.ah >= stop:
                        exit_px, reason = max(stop, b.ao) + STOP_EXTRA + SLIP * 0, "STOP"
                    elif b.al <= target:
                        exit_px, reason = target, "TARGET"
                    elif b.t.astimezone(BER).time() >= time(17, 25) or held >= limit_min:
                        exit_px, reason = b.ao + SLIP, "TIME" if held >= limit_min else "FLAT"
                if exit_px is not None:
                    break
            if exit_px is None:  # koniec danych dnia przed wyjściem: wyjście po ostatnim zamknięciu, jawnie oznaczone
                b = bars[idx[-1]]
                exit_px, reason, j = (b.bc - SLIP if side == 1 else b.ac + SLIP), "END_OF_DATA", len(idx) - 1
            pnl = side * (exit_px - entry)
            trades.append((day, bars[idx[k]].t, side, entry, exit_px, reason, pnl, pnl / r_pts, cost, r_pts))
            n_day += 1; r_day += pnl / r_pts
            k = max(k, j + 1)
    return trades, skips


def stats(tr, mult=1.0):
    if not tr:
        return "brak transakcji"
    pnl = [t[6] - (mult - 1) * t[8] for t in tr]
    w = sum(p for p in pnl if p > 0); l = -sum(p for p in pnl if p < 0)
    cum = peak = dd = 0.0
    for p in pnl:
        cum += p; peak = max(peak, cum); dd = max(dd, peak - cum)
    wins = [p for p in pnl if p > 0]; losses = [p for p in pnl if p <= 0]
    return (f"n={len(pnl)} netto={sum(pnl):+.1f} pkt ({sum(pnl) * POINT_EUR:+.2f} EUR/0,001) śr={statistics.mean(pnl):+.2f} "
            f"traf.={len(wins) / len(pnl):.0%} śr.wygr.={statistics.mean(wins) if wins else 0:.1f} "
            f"śr.str.={statistics.mean(losses) if losses else 0:.1f} PF={(w / l) if l else float('inf'):.2f} DD={dd:.1f} pkt")


def verdict(oos):
    days = len({t[0] for t in oos})
    pnl15 = [t[6] - 0.5 * t[8] for t in oos]
    w = sum(p for p in pnl15 if p > 0); l = -sum(p for p in pnl15 if p < 0)
    if len(oos) < 100 or days < 15:
        return f"ZA MAŁO DANYCH (poza próbą: {len(oos)} transakcji w {days} dniach)"
    return "PRZYJĘTA do dalszych prób" if sum(pnl15) > 0 and l and w / l > 1.2 else "ODRZUCONA"


def main():
    bars = load()
    days = sorted({b.t.astimezone(BER).date() for b in bars})
    atr, m1ema, ctx = atr_series(bars), ema_series([mid(b)[3] for b in bars], 20), m5_context(bars)
    print(f"świece M1: {len(bars)}, dni: {len(days)} ({days[0]} .. {days[-1]}); kolumny bid/ask: {'tak' if True else ''}")
    cut = days[int(len(days) * 2 / 3)]
    with OUT.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["strategia", "dzien", "wejscie_utc", "kierunek", "cena_wej", "cena_wyj", "powod", "wynik_pkt", "wynik_R"])
        for name in ("B1", "B2", "B3", "B4"):
            tr, skips = run(name, bars, atr, ctx, m1ema)
            ins = [t for t in tr if t[0] < cut]; oos = [t for t in tr if t[0] >= cut]
            print(f"\n=== {name}: {len(tr)} transakcji; pominięte: {skips}")
            print("  próba      :", stats(ins)); print(f"  POZA PRÓBĄ :", stats(oos))
            print("  poza próbą, koszty x1.5:", stats(oos, 1.5)); print("  poza próbą, koszty x2  :", stats(oos, 2.0))
            reasons = {}
            for t in tr:
                reasons[t[5]] = reasons.get(t[5], 0) + 1
            print("  wyjścia:", reasons, "| WERDYKT:", verdict(oos))
            for t in tr:
                w.writerow([name, t[0], t[1].isoformat(), "LONG" if t[2] == 1 else "SHORT", round(t[3], 1), round(t[4], 1), t[5], round(t[6], 2), round(t[7], 2)])
    print("\nWERDYKT liczy się tylko z okresu poza próbą. Przyjęcie reguły nie oznacza zysku na żywo.")


main()
