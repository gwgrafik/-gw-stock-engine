"""Pobiera świece MINUTE DE40 z Capital.com (tylko odczyt) do /data/m1_de40.csv. Jedno logowanie.

Uruchomienie (z korzenia repozytorium, silnik może działać obok):
  docker compose -f docker/compose.yml run --rm -T gw-stock-engine python - < research/fetch_m1.py
Klucz jest czytany z pliku sekretów kontenera; nic z niego nie trafia do wyjścia ani do CSV.
"""
import asyncio, csv, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from gwstock.broker.capitalcom import LIVE_BASE_URL, load_credentials

EPIC = "DE40"
MAX_DAYS = int(os.environ.get("GW_FETCH_DAYS", "120"))
WINDOW = timedelta(minutes=960)  # < 1000 punktów
OUT = Path("/data/m1_de40.csv")
FMT = "%Y-%m-%dT%H:%M:%S"


def mid(p):
    return (float(p["bid"]) + float(p["ask"])) / 2


async def main():
    creds = load_credentials(Path(os.environ.get("GW_CAPITALCOM_KEY_FILE", "/secrets/capitalcom.json")))
    base = LIVE_BASE_URL + "/api/v1"
    rows = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post(base + "/session", headers={"X-CAP-API-KEY": creds.api_key},
                          json={"identifier": creds.identifier, "password": creds.api_password,
                                "encryptedPassword": False}) as r:
            if r.status != 200:
                print("logowanie nieudane, kod", r.status, "- nie ponawiam (ryzyko blokady)")
                return 1
            hdr = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        floor = end - timedelta(days=MAX_DAYS)
        empty = 0
        while end > floor and empty < 8:
            start = end - WINDOW
            q = {"resolution": "MINUTE", "from": start.strftime(FMT), "to": end.strftime(FMT), "max": 1000}
            await asyncio.sleep(0.4)
            async with s.get(f"{base}/prices/{EPIC}", headers=hdr, params=q) as r:
                if r.status == 429:
                    await asyncio.sleep(5)
                    continue
                if r.status != 200:
                    print("błąd odczytu cen, kod", r.status, "okno", q["from"]); break
                data = await r.json()
            prices = data.get("prices", [])
            n0 = len(rows)
            for p in prices:
                t = p["snapshotTimeUTC"][:19]
                o, h, l, c = (p[k] for k in ("openPrice", "highPrice", "lowPrice", "closePrice"))
                sp = float(c["ask"]) - float(c["bid"])
                rows[t] = (t, mid(o), mid(h), mid(l), mid(c), round(sp, 2))
            empty = empty + 1 if len(rows) == n0 else 0
            end = start
        async with s.delete(base + "/session", headers=hdr):
            pass
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts_utc", "open", "high", "low", "close", "spread"])
        for t in sorted(rows):
            w.writerow(rows[t])
    ts = sorted(rows)
    print(f"zapisano {len(rows)} świec MINUTE do {OUT}; zakres {ts[0] if ts else '-'} .. {ts[-1] if ts else '-'}")
    return 0


sys.exit(asyncio.run(main()))
