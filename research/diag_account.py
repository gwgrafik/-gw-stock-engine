"""Diagnostyka kont i instrumentu DE40 (TYLKO ODCZYT, jedno logowanie, bez zleceń, bez numerów kont i sekretów).
Uruchomienie: docker compose -f docker/compose.yml run --rm -T gw-stock-engine python - < research/diag_account.py
"""
import asyncio, os, re, sys
from pathlib import Path

import aiohttp

from gwstock.broker.capitalcom import LIVE_BASE_URL, load_credentials

SAFE = {"accountType", "accountName", "status", "preferred", "currency", "canTransferFrom", "canTransferTo"}


def mask(v):
    return re.sub(r"\d{6,}", "<NUM>", str(v))


async def main():
    creds = load_credentials(Path(os.environ.get("GW_CAPITALCOM_KEY_FILE", "/secrets/capitalcom.json")))
    base = LIVE_BASE_URL + "/api/v1"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post(base + "/session", headers={"X-CAP-API-KEY": creds.api_key},
                          json={"identifier": creds.identifier, "password": creds.api_password, "encryptedPassword": False}) as r:
            if r.status != 200:
                print("logowanie nieudane, kod", r.status); return 1
            hdr = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}
            sess = await r.json()
        print("== sesja: klucze odpowiedzi:", sorted(sess.keys()))
        for k in ("accountType", "hasActiveDemoAccounts", "hasActiveLiveAccounts", "trailingStopsEnabled", "dealingEnabled", "currencyIsoCode"):
            if k in sess:
                print(f"   {k}: {mask(sess[k])}")
        async with s.get(base + "/accounts", headers=hdr) as r:
            data = await r.json()
        print("== konta (bez numerów):")
        for i, a in enumerate(data.get("accounts", []), 1):
            bal = a.get("balance", {}) or {}
            print(f"  {i}) " + ", ".join(f"{k}={mask(a[k])}" for k in sorted(a) if k in SAFE) + f", saldo>0={float(bal.get('balance', 0) or 0) > 0}")
            print("     wszystkie klucze:", sorted(a.keys()))
        async with s.get(base + "/markets/DE40", headers=hdr) as r:
            m = await r.json()
        ins, snap = m.get("instrument", {}), m.get("snapshot", {})
        print("== DE40:", {k: mask(ins.get(k)) for k in ("epic", "name", "type", "currency", "marginFactor", "marginFactorUnit", "country")})
        print("   marketStatus:", snap.get("marketStatus"), "| marketModes:", snap.get("marketModes"))
        async with s.get(base + "/markets", headers=hdr, params={"searchTerm": "Germany 40"}) as r:
            found = (await r.json()).get("markets", [])
        print("== wyszukiwanie 'Germany 40' (epic, nazwa, typ, status):")
        for x in found[:10]:
            print("  ", x.get("epic"), "|", x.get("instrumentName"), "|", x.get("instrumentType"), "|", x.get("marketStatus"))
        async with s.delete(base + "/session", headers=hdr):
            pass
    return 0


sys.exit(asyncio.run(main()))
