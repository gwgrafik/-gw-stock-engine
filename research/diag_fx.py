"""Diagnostyka walut instrumentu w USD na rachunku w EUR (TYLKO ODCZYT, jedno logowanie, bez zleceń, bez kluczy, haseł,
tokenów ani numerów kont w wyjściu). Wynik rozstrzyga niepewności z projektu pasm (sekcja 7 i 7.5, zadanie 16):
  1. instrument (domyślnie US500): waluta, lotSize, depozyt, pola walutowe i kursy w odpowiedzi, reguły handlu, notowanie;
  2. historia transakcji (GET /history/transactions) za ostatnie dni: waluty i kwoty wierszy TRADE, obecność wierszy
     CONVERSION i FX_COMMISSION, powiązanie pola `reference` z dealId z historii aktywności;
  3. historia aktywności (GET /history/activity, okna po 1 dobie): `details.currency` wierszy instrumentu, źródło zamknięcia;
  4. para EUR/USD: nazwa epiku (próba EURUSD, potem wyszukiwanie) i jej notowanie jako źródło przeliczeń;
  5. pola celu zysku (profit) w GET /positions (o ile są otwarte pozycje) i w GET /confirms (dla referencji z historii).
Czego odczyt NIE rozstrzygnie: jeśli na rachunku nigdy nie zamknięto transakcji na tym instrumencie, w historii nie ma wiersza
z jego wynikiem (wydruk powie to wprost); wtedy potrzebna jest próba na wielkości minimalnej (decyzja właściciela).

Uruchomienie (z korzenia repozytorium, silnik może działać obok; jedno logowanie, ok. 25 zapytań, ~10 s):
  docker compose -f docker/compose.yml run --rm -T -e GW_EPIC=US500 gw-stock-engine python - < research/diag_fx.py
Zmienne środowiskowe (wszystkie opcjonalne):
  GW_EPIC (instrument, domyślnie US500), GW_DAYS (ile dni historii, 1-60, domyślnie 14), GW_CONFIRMS (ile referencji sprawdzić
  w GET /confirms, 0-5, domyślnie 3), GW_FX_EPIC i GW_FX_SEARCH (epik pary i fraza wyszukiwania, domyślnie EURUSD),
  GW_DIAG_OUT (plik surowych odpowiedzi rynkowych, domyślnie /data/diag_fx_<epic>.json), GW_DATA_DIR (domyślnie /data;
  stąd czytany data/account.json, żeby porównać rachunek sesji z rachunkiem silnika), GW_CAPITALCOM_KEY_FILE.
Wydruk wklej z powrotem w całości: ma na końcu blok PODSUMOWANIE. Plik surowy (tylko odpowiedzi GET /markets, bez danych
rachunku i bez transakcji) zostaje w katalogu danych; NIE dodawaj go do repozytorium.
Klucz jest czytany z pliku sekretów kontenera; z całej odpowiedzi drukowane są tylko pola z listy, a każdy napis przechodzi
przez maskowanie (długie liczby, identyfikatory UUID, adresy e-mail, długie tokeny) i usuwanie znanych sekretów.
Parser odpowiedzi to funkcje czyste bez wejścia/wyjścia (testy: tests/unit/test_research_fx.py).
"""
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

FMT = "%Y-%m-%dT%H:%M:%S"
PAUSE_S = 0.3                      # odstęp między zapytaniami: ok. 3/s, poniżej limitu brokera (10/s)
TX_SPAN_DAYS, ACT_SPAN_DAYS = 7, 1  # historia aktywności: najwyżej 1 doba na zapytanie (notatki API 2.19)
ACT_TRUNCATION_ROWS = 20           # od tylu wierszy w oknie odpowiedź mogła zostać obcięta (limit nieudokumentowany)
CLOSING_SOURCES = {"SL", "TP", "CLOSE_OUT"}  # samotny wiersz z takim źródłem to zamknięcie (DEALER/SYSTEM bywają też czym innym)
FX_TYPES = ("CONVERSION", "FX_COMMISSION")
COST_TYPES = ("CONVERSION", "FX_COMMISSION", "TRADE_COMMISSION", "SWAP")
EPIC_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

# ---------------------------------------------------------------- narzędzia: maskowanie, formatowanie, JSON

_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4,12}(?![0-9A-Za-z])")
_EMAIL = re.compile(r"[^\s@,;:()\[\]{}<>]+@[^\s@,;()\[\]{}<>]+")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_LONG_NUM = re.compile(r"(?<![\d.])\d{6,}(?!\d)(?!\.\d)")  # całkowite od 6 cyfr; liczby z częścią ułamkową (ceny) zostają


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def _token(match):
    text = match.group(0)
    return "<TOKEN>" if re.search(r"\d", text) and re.search(r"[A-Za-z]", text) else text


_SECRETS = []  # sekrety bieżącego przebiegu (ustawia `run`); usuwane w `mask` PRZED maskowaniem wzorcami


def mask(value):
    """Napis bez znanych sekretów, numerów kont, identyfikatorów deali, adresów e-mail i długich tokenów (ceny i daty zostają).
    Znane sekrety usuwane są pierwsze: maskowanie wzorcami mogłoby je zamienić na fragment, którego nikt już nie rozpozna."""
    text = _UUID.sub("<ID>", scrub(str(value), _SECRETS))
    text = _EMAIL.sub("<EMAIL>", text)
    text = _TOKEN.sub(_token, text)
    return _LONG_NUM.sub("<NUM>", text)


def _dec(value):
    return format(value, "f") if value.is_finite() else str(value)


def fmt(value, limit=80):
    """Wartość do wydruku: liczby jawnie (bez zapisu naukowego), napisy zamaskowane, długie teksty przycięte."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return _dec(value)
    if isinstance(value, int):
        return "<NUM>" if len(str(abs(value))) >= 9 else str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        text = mask(value)
    elif isinstance(value, (list, tuple)):
        text = "[" + ", ".join(fmt(v, limit) for v in value[:8]) + (", …" if len(value) > 8 else "") + "]"
    elif isinstance(value, dict):
        items = list(value.items())
        text = "{" + ", ".join(f"{mask(k)}={fmt(v, limit)}" for k, v in items[:12]) + (", …" if len(items) > 12 else "") + "}"
    else:
        text = type(value).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scrub(text, secrets):
    """Usuwa z wydruku znane sekrety (najdłuższe najpierw; napisy krótsze niż 4 znaki są pomijane)."""
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "<SECRET>")
    return text


def _reject_constant(name):
    raise ValueError(name)


def parse_json(text):
    """JSON z liczbami jako Decimal; nieczytelny tekst, NaN, Infinity i wykładnik poza zakresem dają None."""
    if not isinstance(text, (str, bytes)) or not text:
        return None
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, ArithmeticError, RecursionError):
        return None


def _to_dec(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    return None


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def error_code(payload):
    code = as_dict(payload).get("errorCode")
    return code if isinstance(code, str) else None


# ---------------------------------------------------------------- konfiguracja i okna czasu

def _int_env(environ, key, default, low, high):
    try:
        value = int(str(environ.get(key, "")).strip())
    except ValueError:
        return default
    return max(low, min(high, value))


def load_config(environ):
    epic = (environ.get("GW_EPIC") or "US500").strip().upper()
    if not EPIC_RE.match(epic):
        epic = "US500"  # epik trafia do ścieżki zapytania i nazwy pliku: tylko znaki bezpieczne
    fx_epic = (environ.get("GW_FX_EPIC") or "EURUSD").strip()
    if not EPIC_RE.match(fx_epic):
        fx_epic = "EURUSD"
    data_dir = environ.get("GW_DATA_DIR") or "/data"
    return {
        "epic": epic, "fx_epic": fx_epic, "search": (environ.get("GW_FX_SEARCH") or "EURUSD").strip(),
        "days": _int_env(environ, "GW_DAYS", 14, 1, 60), "confirms": _int_env(environ, "GW_CONFIRMS", 3, 0, 5),
        "data_dir": data_dir, "out": environ.get("GW_DIAG_OUT") or f"{data_dir}/diag_fx_{epic.lower()}.json",
        "key_file": environ.get("GW_CAPITALCOM_KEY_FILE") or "/secrets/capitalcom.json",
    }


def window_ranges(end, days, span_days):
    """Okna (od, do) jako napisy UTC, od najstarszego; każde najwyżej `span_days` doby, sąsiednie zachodzą o 1 s
    (włączność granic w API nieopisana). Razem sięgają `days` doby wstecz od `end`; najstarsze sekundy (najwyżej tyle, ile
    jest okien) mogą zostać poza zakresem, bo nakładki zjadają po sekundzie, a okno na samą resztę byłoby stratą zapytania."""
    end = end.astimezone(timezone.utc).replace(microsecond=0)
    span, overlap = timedelta(days=span_days), timedelta(seconds=1)
    floor, to, windows = end - timedelta(days=days), end, []
    while to - floor > overlap * (len(windows) + 1):
        start = max(to - span, floor)
        windows.append((start.strftime(FMT), to.strftime(FMT)))
        to = start + overlap
    return windows[::-1]


def quote_age_s(stamp, now):
    """Wiek notowania w sekundach (UTC) albo None; wartość ujemna znaczy zegar brokera przed naszym."""
    if not isinstance(stamp, str):
        return None
    text = stamp.strip().rstrip("Z")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f", FMT):
        try:
            moment = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return Decimal(str((now - moment).total_seconds())).quantize(Decimal("0.001"))
    return None


# ---------------------------------------------------------------- parser: instrument

_CURRENCY_HINT = re.compile(r"curr|fx|exchange|convers|forex", re.I)


def find_currency_fields(root, prefix, depth=3):
    """Pola, których NAZWA wygląda na walutową lub kursową (curr, fx, exchange, convers, forex), z ścieżką."""
    found = {}
    for key, value in as_dict(root).items():
        path = f"{prefix}.{key}"
        if _CURRENCY_HINT.search(str(key)):
            found[path] = value
        elif isinstance(value, dict) and depth > 1:
            found.update(find_currency_fields(value, path, depth - 1))
    return found


def _rule(value):
    d = as_dict(value)
    if "value" in d or "unit" in d:
        return {"value": d.get("value"), "unit": d.get("unit")}
    return {"value": value, "unit": None}


def _currencies(raw):
    result = []
    for item in as_list(raw):
        d = as_dict(item)
        if not d:
            continue
        known = ("code", "symbol", "baseExchangeRate", "exchangeRate", "isDefault")
        entry = {k: d.get(k) for k in known}
        entry["other_keys"] = sorted(k for k in d if k not in known)
        result.append(entry)
    return result


def describe_market(payload):
    """GET /markets/{epic} → waluta, kursy, reguły handlu, notowanie, godziny (odporne na brakujące i błędne pola)."""
    root = as_dict(payload)
    ins, rules, snap = (as_dict(root.get(k)) for k in ("instrument", "dealingRules", "snapshot"))
    bid, offer = snap.get("bid"), snap.get("offer")
    low, high = _to_dec(bid), _to_dec(offer)
    hours = as_dict(ins.get("openingHours"))
    fee = as_dict(ins.get("overnightFee"))
    fields = {}
    for name, section in (("instrument", ins), ("snapshot", snap), ("dealingRules", rules)):
        fields.update(find_currency_fields(section, name))
    return {
        "readable": bool(ins or rules or snap),
        "epic": ins.get("epic"), "name": ins.get("name"), "type": ins.get("type"), "currency": ins.get("currency"),
        "lotSize": ins.get("lotSize"), "marginFactor": ins.get("marginFactor"), "marginFactorUnit": ins.get("marginFactorUnit"),
        "instrument_keys": sorted(ins), "snapshot_keys": sorted(snap),
        "currencies": _currencies(ins.get("currencies")), "currency_fields": fields,
        "rules": {k: _rule(v) for k, v in rules.items()},
        "quote": {"bid": bid, "offer": offer, "spread": (high - low) if low is not None and high is not None else None,
                  "marketStatus": snap.get("marketStatus"), "marketModes": snap.get("marketModes"),
                  "updateTimeUTC": snap.get("updateTimeUTC"), "updateTime": snap.get("updateTime"),
                  "delayTime": snap.get("delayTime"), "scalingFactor": snap.get("scalingFactor"),
                  "decimalPlacesFactor": snap.get("decimalPlacesFactor")},
        "hours": {"zone": hours.get("zone"), "days": {k: v for k, v in hours.items() if k != "zone" and isinstance(v, list)}},
        "overnight": {k: fee.get(k) for k in ("longRate", "shortRate", "swapChargeTimestamp", "swapChargeInterval")},
    }


def describe_accounts(login, bound_id=None):
    """Odpowiedź logowania → rachunki BEZ numerów i nazw: waluta, typ, status, czy aktywny w sesji, czy związany z silnikiem."""
    root = as_dict(login)
    current = root.get("currentAccountId")
    accounts = []
    for item in as_list(root.get("accounts")):
        d = as_dict(item)
        if not d:
            continue
        account_id = d.get("accountId")
        accounts.append({
            "type": d.get("accountType"), "currency": d.get("currency"), "status": d.get("status"),
            "preferred": d.get("preferred"), "active": account_id is not None and account_id == current,
            "bound": bound_id is not None and account_id == bound_id,
        })
    active = next((a for a in accounts if a["active"]), None)
    bound = next((a for a in accounts if a["bound"]), None)
    return {
        "count": len(accounts), "accounts": accounts, "session_currency": root.get("currencyIsoCode"),
        "session_type": root.get("accountType"), "active_currency": active["currency"] if active else None,
        "bound_found": bound is not None, "bound_currency": bound["currency"] if bound else None,
        "bound_is_active": bound["active"] if bound else None,
    }


# ---------------------------------------------------------------- parser: historia

def link_kind(reference, ids):
    """Czy `reference` wiersza transakcji da się powiązać z dealId/referencją z historii aktywności."""
    ref = "" if reference is None else str(reference).strip()
    if not ref:
        return "missing"
    known = [str(i) for i in ids if i]
    if ref in known:
        return "exact"
    if len(ref) >= 6 and any(ref in i or (len(i) >= 6 and i in ref) for i in known):
        return "partial"
    return "none"


def parse_transactions(payload):
    rows = []
    for item in as_list(as_dict(payload).get("transactions")):
        d = as_dict(item)
        if not d:
            continue
        rows.append({
            "date_utc": d.get("dateUtc") or d.get("dateUTC") or d.get("date"), "type": d.get("transactionType"),
            "note": d.get("note"), "reference": d.get("reference"), "size": d.get("size"), "currency": d.get("currency"),
            "status": d.get("status"), "instrument": d.get("instrumentName"), "keys": sorted(d),
        })
    return rows


def _kinds(rows, fields):
    counts = Counter(tuple(r.get(f) for f in fields) for r in rows)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], [str(x) for x in kv[0]]))
    return [{**dict(zip(fields, key)), "count": n} for key, n in ordered]


def _newest(rows):
    return sorted(rows, key=lambda r: str(r["date_utc"] or ""), reverse=True)


def summarize_transactions(rows, names, ids):
    wanted = {_norm(n) for n in names if n} - {""}
    own = [r for r in rows if _norm(r["instrument"]) in wanted]
    closed = [r for r in own if r["type"] == "TRADE" and "closed" in str(r["note"] or "").lower()]
    compact = ("date_utc", "type", "note", "currency", "size", "status", "instrument")
    return {
        "total": len(rows), "currencies": sorted({r["currency"] for r in rows if isinstance(r["currency"], str)}),
        "row_keys": sorted({k for r in rows for k in r["keys"]}),
        "kinds": _kinds(rows, ("type", "note", "currency", "status")),
        "fx_counts": {t: sum(1 for r in rows if r["type"] == t) for t in COST_TYPES},
        "fx_rows": [{f: r[f] for f in compact} for r in _newest(r for r in rows if r["type"] in FX_TYPES)][:10],
        "instrument_total": len(own),
        "instrument_currencies": sorted({r["currency"] for r in own if isinstance(r["currency"], str)}),
        "instrument_closed": [{**{f: r[f] for f in compact}, "link": link_kind(r["reference"], ids)} for r in _newest(closed)][:10],
    }


def parse_activities(payload):
    rows = []
    for item in as_list(as_dict(payload).get("activities")):
        d = as_dict(item)
        if not d:
            continue
        details = as_dict(d.get("details"))
        rows.append({
            "date_utc": d.get("dateUTC") or d.get("dateUtc"), "epic": d.get("epic"), "deal_id": d.get("dealId"),
            "source": d.get("source"), "type": d.get("type"), "status": d.get("status"),
            "currency": details.get("currency"), "direction": details.get("direction"), "level": details.get("level"),
            "size": details.get("size"), "deal_reference": details.get("dealReference"), "detail_keys": sorted(details),
        })
    return rows


def dedupe(rows):
    """Usuwa powtórzenia z nakładających się okien (kolejność pierwszych wystąpień zostaje)."""
    seen, result = set(), []
    for row in rows:
        key = repr(sorted(row.items(), key=lambda kv: kv[0]))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def summarize_activities(rows, epic):
    by_epic = {}
    for r in rows:
        if r["epic"] and r["currency"]:
            by_epic.setdefault(r["epic"], Counter())[r["currency"]] += 1
    own = [r for r in rows if r["epic"] == epic]
    groups = {}
    for index, r in enumerate(x for x in own if x["type"] == "POSITION" and x["status"] == "ACCEPTED"):
        groups.setdefault(r["deal_id"] or ("anon", index), []).append(r)
    closing = []
    for group in groups.values():
        group.sort(key=lambda r: str(r["date_utc"] or ""))
        last = group[-1]
        if len(group) >= 2 or last["source"] in CLOSING_SOURCES:
            closing.append(last)
    closing.sort(key=lambda r: str(r["date_utc"] or ""), reverse=True)
    return {
        "total": len(rows), "epic_total": len(own), "by_epic_currency": {e: dict(c) for e, c in by_epic.items()},
        "kinds": _kinds(own, ("source", "type", "status", "currency")),
        "closing": [{f: r[f] for f in ("date_utc", "source", "currency", "direction", "level", "size")} for r in closing][:10],
        "detail_keys": sorted({k for r in own for k in r["detail_keys"]}),
    }


def pick_references(rows, positions, epic, limit):
    """Do `GET /confirms`: referencje z historii aktywności (najpierw nasz instrument, najnowsze pierwsze), potem z pozycji."""
    if limit <= 0:
        return []
    candidates = [r["deal_reference"] for r in _newest(x for x in rows if x["epic"] == epic)]
    candidates += [r["deal_reference"] for r in _newest(x for x in rows if x["epic"] != epic)]
    candidates += [p.get("deal_reference") for p in positions]
    refs = []
    for ref in candidates:
        if isinstance(ref, str) and ref and ref not in refs:
            refs.append(ref)
    return refs[:limit]


# ---------------------------------------------------------------- parser: pozycje, potwierdzenia, para walutowa

_TARGET_KEY = re.compile(r"^(profit|limit)(level|distance|amount)$", re.I)
_RESULT_KEY = re.compile(r"profit|pnl|cash|^upl$", re.I)
_STOP_KEY = re.compile(r"^stop(level|distance|amount)$", re.I)


def split_profit_fields(record):
    """(pola CELU zysku: profit*/limit* Level/Distance/Amount, pola WYNIKU: profit, profitCurrency, upl, pnl, cash)."""
    target, result = {}, {}
    for key, value in as_dict(record).items():
        if _TARGET_KEY.match(str(key)):
            target[key] = value
        elif _RESULT_KEY.search(str(key)):
            result[key] = value
    return target, result


def describe_positions(payload):
    items = []
    for entry in as_list(as_dict(payload).get("positions")):
        pos = as_dict(as_dict(entry).get("position"))
        if not pos:
            continue
        market = as_dict(as_dict(entry).get("market"))
        target, result = split_profit_fields(pos)
        items.append({
            "epic": market.get("epic"), "direction": pos.get("direction"), "size": pos.get("size"),
            "currency": pos.get("currency"), "upl": pos.get("upl"), "level": pos.get("level"),
            "target_fields": target, "result_fields": result,
            "stop_fields": {k: v for k, v in pos.items() if _STOP_KEY.match(str(k))},
            "position_keys": sorted(pos), "market_keys": sorted(market), "deal_reference": pos.get("dealReference"),
        })
    return {"count": len(items), "items": items, "target_keys": sorted({k for i in items for k in i["target_fields"]})}


def describe_confirm(payload):
    d = as_dict(payload)
    target, result = split_profit_fields(d)
    return {
        "keys": sorted(d), "status": d.get("status"), "deal_status": d.get("dealStatus"), "reason": d.get("reason"),
        "epic": d.get("epic"), "direction": d.get("direction"), "level": d.get("level"), "size": d.get("size"),
        "affected": len(as_list(d.get("affectedDeals"))), "target_fields": target, "result_fields": result,
        "stop_fields": {k: v for k, v in d.items() if _STOP_KEY.match(str(k))},
    }


def parse_search(payload):
    rows = []
    for item in as_list(as_dict(payload).get("markets")):
        d = as_dict(item)
        if d:
            rows.append({"epic": d.get("epic"), "name": d.get("instrumentName"), "type": d.get("instrumentType"),
                         "status": d.get("marketStatus"), "bid": d.get("bid"), "offer": d.get("offer"),
                         "updateTimeUTC": d.get("updateTimeUTC")})
    return rows


def pick_fx_epic(rows, wanted="EURUSD"):
    """Epik pary: dokładny, potem po nazwie (EUR/USD) wśród CURRENCIES, potem najkrótszy epik zawierający nazwę pary."""
    key = _norm(wanted)
    named = [r for r in rows if isinstance(r.get("epic"), str)]
    for r in named:
        if r["epic"].upper() == str(wanted).upper():
            return r["epic"]
    currencies = [r for r in named if str(r.get("type") or "").upper() == "CURRENCIES"]
    for r in currencies:
        if _norm(r.get("name")) == key:
            return r["epic"]
    hits = sorted((r["epic"] for r in currencies if key in _norm(r["epic"])), key=lambda e: (len(e), e))
    return hits[0] if hits else None


def load_bound_account(path):
    """Numer rachunku związanego z silnikiem (data/account.json) albo None; służy wyłącznie do porównania, nie jest drukowany."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("account_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _json_default(obj):
    return float(obj) if isinstance(obj, Decimal) and obj.is_finite() else str(obj)


def write_raw(path, data):
    """Surowe odpowiedzi RYNKOWE (GET /markets) do pliku w katalogu danych; zwraca komunikat do wydruku."""
    try:
        Path(path).write_text(json.dumps(data, default=_json_default, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        return f"nie zapisano surowych odpowiedzi rynkowych ({type(exc).__name__})"
    return f"surowe odpowiedzi rynkowe (bez danych rachunku) zapisano w {path}; nie dodawaj tego pliku do repozytorium"


# ---------------------------------------------------------------- zbieranie (tylko GET, przez wstrzyknięte `get`)

class _Abort(Exception):
    pass


def _section(status, payload, parser):
    ok = status == 200
    return {"status": status, "error": None if ok else error_code(payload), "info": parser(payload) if ok else None}


async def collect(get, cfg, now, login=None, bound_id=None):
    """Czyta dane brokera wyłącznie przez `get(path, params) -> (status, payload)`; 401/403/429 przerywa dalsze zapytania."""
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    epic, days = cfg["epic"], cfg["days"]
    rep = {"config": {k: cfg[k] for k in ("epic", "fx_epic", "search", "days", "confirms")}, "accounts": describe_accounts(login, bound_id),
           "aborted": None, "requests": 0, "raw": {}, "market": None, "transactions": None, "activity": None, "fx": None,
           "positions": None, "confirms": []}
    tx = {"statuses": [], "rows": [], "span": None}
    act = {"statuses": [], "rows": [], "filter": "used", "span": None, "truncated": 0}

    async def call(path, params=None):
        status, payload = await get(path, params)
        rep["requests"] += 1
        if status in (401, 403, 429):
            raise _Abort(f"kod HTTP {status} na {path}; dalsze zapytania wstrzymane bez ponawiania")
        return status, payload

    try:
        status, payload = await call(f"/markets/{epic}")
        rep["market"] = _section(status, payload, describe_market)
        if status == 200:
            rep["raw"][epic] = payload

        windows = window_ranges(now, days, TX_SPAN_DAYS)
        tx["span"] = (windows[0][0], windows[-1][1])
        for start, end in windows:
            status, payload = await call("/history/transactions", {"from": start, "to": end})
            tx["statuses"].append(status)
            if status == 200:
                tx["rows"] += parse_transactions(payload)

        windows = window_ranges(now, days, ACT_SPAN_DAYS)
        act["span"] = (windows[0][0], windows[-1][1])
        use_filter = True
        for start, end in windows:
            params = {"from": start, "to": end, "detailed": "true"}
            if use_filter:
                params["filter"] = f"epic=={epic}"
            status, payload = await call("/history/activity", params)
            if status == 400 and use_filter:  # składnia filtra nie potwierdzona (C7b): jedno ponowienie bez niego
                use_filter, act["filter"] = False, "rejected"
                params.pop("filter")
                status, payload = await call("/history/activity", params)
            act["statuses"].append(status)
            if status == 200:
                parsed = parse_activities(payload)
                act["rows"] += parsed
                act["truncated"] += 1 if len(parsed) >= ACT_TRUNCATION_ROWS else 0

        fx = {"epic": None, "direct_status": None, "search_status": None, "candidates": [], "candidate": None, "status": None,
              "error": None, "info": None, "age_s": None}
        rep["fx"] = fx
        status, payload = await call(f"/markets/{cfg['fx_epic']}")
        fx["direct_status"] = status
        chosen = cfg["fx_epic"] if status == 200 else None
        if status != 200:
            found_status, found = await call("/markets", {"searchTerm": cfg["search"]})
            fx["search_status"] = found_status
            fx["candidates"] = parse_search(found) if found_status == 200 else []
            chosen = pick_fx_epic(fx["candidates"], cfg["fx_epic"])
            fx["candidate"] = next((c for c in fx["candidates"] if c["epic"] == chosen), None)
            if chosen and chosen != cfg["fx_epic"]:  # ten sam epik już dał błąd: nie pytamy drugi raz
                status, payload = await call(f"/markets/{chosen}")
        fx["epic"], fx["status"] = chosen, status if chosen else None
        if chosen and status == 200:
            fx["info"] = describe_market(payload)
            fx["age_s"] = quote_age_s(fx["info"]["quote"]["updateTimeUTC"], now)
            rep["raw"][chosen] = payload
        elif chosen:
            fx["error"] = error_code(payload)

        status, payload = await call("/positions")
        rep["positions"] = _section(status, payload, describe_positions)

        items = rep["positions"]["info"]["items"] if rep["positions"]["info"] else []
        for ref in pick_references(dedupe(act["rows"]), items, epic, cfg["confirms"]):
            status, payload = await call("/confirms/" + quote(ref, safe=""))
            rep["confirms"].append(_section(status, payload, describe_confirm))
    except _Abort as stop:
        rep["aborted"] = str(stop)

    act_rows, tx_rows = dedupe(act["rows"]), dedupe(tx["rows"])
    info = (rep["market"] or {}).get("info") or {}
    own = [r for r in act_rows if r["epic"] == epic]
    ids = [r["deal_id"] for r in own] + [r["deal_reference"] for r in own]
    if act["statuses"]:
        rep["activity"] = {"statuses": act["statuses"], "filter": act["filter"], "span": act["span"], "truncated": act["truncated"],
                           "summary": summarize_activities(act_rows, epic)}
    if tx["statuses"]:
        rep["transactions"] = {"statuses": tx["statuses"], "span": tx["span"],
                               "summary": summarize_transactions(tx_rows, [epic, info.get("name")], ids)}
    return rep


# ---------------------------------------------------------------- wydruk

def keys_text(keys, limit=60):
    """Pełna lista nazw pól (do wykrywania nieznanych pól walutowych), przycięta dopiero od `limit` nazw."""
    names = [mask(k) for k in keys]
    return "[" + ", ".join(names[:limit]) + (f", … (+{len(names) - limit})" if len(names) > limit else "") + "]"


def kv(pairs, sep=" | "):
    return sep.join(f"{k}={fmt(v)}" for k, v in pairs if v is not None)


def _codes(statuses):
    return ", ".join(f"{code} x{n}" for code, n in sorted(Counter(statuses).items(), key=lambda kv: str(kv[0])))


def _failed(section):
    return f"  odpowiedź: kod {fmt(section['status'])}" + (f", errorCode={fmt(section['error'])}" if section["error"] else "")


def _rules_text(rules):
    return "; ".join(f"{k}={fmt(r['value'])}" + (f" {fmt(r['unit'])}" if r["unit"] else "") for k, r in rules.items())


def _quote_line(q, age=None):
    pairs = [("bid", q["bid"]), ("offer", q["offer"]), ("spread", q["spread"]), ("status", q["marketStatus"]),
             ("tryby", q["marketModes"]), ("czas UTC", q["updateTimeUTC"]), ("czas lokalny brokera", q["updateTime"]),
             ("delayTime", q["delayTime"])]
    if age is not None:
        pairs.append(("wiek notowania [s]", age))
    return "  notowanie: " + kv(pairs)


def _render_accounts(acc):
    lines = ["== 0. rachunek i sesja"]
    if not acc["count"]:
        return lines + ["  brak listy rachunków w odpowiedzi logowania"]
    lines.append(f"  rachunków w logowaniu: {acc['count']} | waluta sesji (currencyIsoCode): {fmt(acc['session_currency'])} | typ sesji: {fmt(acc['session_type'])}")
    for i, a in enumerate(acc["accounts"], 1):
        flags = (" [aktywny w sesji]" if a["active"] else "") + (" [związany z silnikiem]" if a["bound"] else "")
        lines.append(f"  {i}) " + kv([("typ", a["type"]), ("waluta", a["currency"]), ("status", a["status"]), ("preferowany", a["preferred"])], ", ") + flags)
    lines.append(f"  waluta rachunku aktywnego w sesji: {fmt(acc['active_currency'])}")
    if acc["bound_found"]:
        lines.append(f"  rachunek związany z silnikiem (data/account.json): waluta {fmt(acc['bound_currency'])}; jest aktywny w sesji: {'tak' if acc['bound_is_active'] else 'nie'}")
        if not acc["bound_is_active"]:
            lines.append("  UWAGA: aktywny rachunek sesji różni się od rachunku związanego z silnikiem; odczyty poniżej dotyczą rachunku AKTYWNEGO w sesji (skrypt niczego nie przełącza)")
    else:
        lines.append("  rachunek związany z silnikiem (data/account.json): brak pliku albo numeru nie ma na liście logowania")
    return lines


def _render_market(section, epic):
    lines = [f"== 1. instrument {fmt(epic)} (GET /markets/{fmt(epic)})"]
    if section is None:
        return lines + ["  nie pobrano"]
    if section["info"] is None:
        return lines + [_failed(section)]
    m = section["info"]
    lines.append("  " + kv([("epic", m["epic"]), ("nazwa", m["name"]), ("typ", m["type"]), ("waluta instrumentu", m["currency"]),
                            ("lotSize", m["lotSize"]), ("marginFactor", None if m["marginFactor"] is None else f"{fmt(m['marginFactor'])} {fmt(m['marginFactorUnit'])}")]))
    if m["currencies"]:
        lines.append("  waluty instrumentu (instrument.currencies):")
        for c in m["currencies"]:
            lines.append("    " + fmt(c["code"]) + " " + kv([("symbol", c["symbol"]), ("baseExchangeRate", c["baseExchangeRate"]), ("exchangeRate", c["exchangeRate"]),
                                                          ("isDefault", c["isDefault"]), ("inne klucze", c["other_keys"] or None)], " "))
    else:
        lines.append("  brak pola instrument.currencies (kursu w danych instrumentu nie ma)")
    extra = {k: v for k, v in m["currency_fields"].items() if k not in ("instrument.currency", "instrument.currencies")}
    lines.append("  inne pola walutowe/kursowe: " + ("; ".join(f"{k}={fmt(v)}" for k, v in extra.items()) if extra else "brak"))
    lines.append("  reguły handlu: " + (_rules_text(m["rules"]) or "brak"))
    lines.append(_quote_line(m["quote"]))
    hours = m["hours"]
    lines.append(f"  godziny otwarcia (strefa {fmt(hours['zone'])}): " + ("; ".join(f"{k}={fmt(v)}" for k, v in hours["days"].items()) or "brak"))
    # znacznik czasu naliczenia swapu pomijamy: nie dotyczy waluty, a jako długa liczba byłby maskowany
    lines.append("  finansowanie nocne: " + (kv([(k, v) for k, v in m["overnight"].items() if k != "swapChargeTimestamp"], ", ") or "brak"))
    lines.append("  klucze instrument: " + keys_text(m["instrument_keys"]))
    lines.append("  klucze snapshot: " + keys_text(m["snapshot_keys"]))
    return lines


def _render_transactions(section, days):
    lines = [f"== 2. historia transakcji (GET /history/transactions, ~{days} dni)"]
    if section is None:
        return lines + ["  nie pobrano"]
    s = section["summary"]
    lines.append(f"  zakres UTC: {fmt(section['span'][0])} .. {fmt(section['span'][1])} | kody HTTP: {_codes(section['statuses'])}")
    lines.append(f"  wierszy: {s['total']} | waluty w wierszach: {', '.join(map(fmt, s['currencies'])) or 'brak'}")
    lines.append("  pola wiersza (klucze): " + keys_text(s["row_keys"]))
    if s["kinds"]:
        lines.append("  rodzaje (typ | opis | waluta | status): liczba")
        lines += [f"    {fmt(k['type'])} | {fmt(k['note'])} | {fmt(k['currency'])} | {fmt(k['status'])}: {k['count']}" for k in s["kinds"][:15]]
    lines.append("  " + ", ".join(f"{t}: {n}" for t, n in s["fx_counts"].items()))
    for r in s["fx_rows"]:
        lines.append("    wiersz kosztu/przewalutowania: " + kv([(k, r[k]) for k in ("date_utc", "type", "note", "currency", "size", "instrument")]))
    lines.append(f"  wiersze wybranego instrumentu: {s['instrument_total']} | ich waluty: {', '.join(map(fmt, s['instrument_currencies'])) or 'brak'}")
    for r in s["instrument_closed"]:
        lines.append("    zamknięcie: " + kv([("data UTC", r["date_utc"]), ("opis", r["note"]), ("waluta", r["currency"]), ("size", r["size"]),
                                              ("status", r["status"]), ("powiązanie reference z dealId", r["link"])]))
    return lines


def _render_activity(section, epic):
    lines = [f"== 3. historia aktywności (GET /history/activity, okna po 1 dobie, filtr epic=={fmt(epic)})"]
    if section is None:
        return lines + ["  nie pobrano"]
    s = section["summary"]
    lines.append(f"  zakres UTC: {fmt(section['span'][0])} .. {fmt(section['span'][1])} | kody HTTP: {_codes(section['statuses'])} | filtr: {section['filter']}")
    if section["truncated"]:
        lines.append(f"  UWAGA: {section['truncated']} okien miało co najmniej {ACT_TRUNCATION_ROWS} wierszy, odpowiedź mogła zostać obcięta")
    lines.append(f"  wierszy: {s['total']} | z wybranym instrumentem: {s['epic_total']}")
    lines.append("  details.currency według instrumentu: " + ("; ".join(f"{fmt(e)}: " + ", ".join(f"{fmt(c)} x{n}" for c, n in cur.items()) for e, cur in s["by_epic_currency"].items()) or "brak"))
    if s["kinds"]:
        lines.append("  rodzaje wierszy instrumentu (źródło | typ | status | waluta): liczba")
        lines += [f"    {fmt(k['source'])} | {fmt(k['type'])} | {fmt(k['status'])} | {fmt(k['currency'])}: {k['count']}" for k in s["kinds"][:10]]
    for c in s["closing"]:
        lines.append("    zamknięcie: " + kv([(k, c[k]) for k in ("date_utc", "source", "currency", "direction", "level", "size")]))
    lines.append("  klucze details wierszy instrumentu: " + keys_text(s["detail_keys"]))
    return lines


def _render_fx(fx, wanted):
    lines = ["== 4. para walutowa EUR/USD (notowanie jako źródło przeliczeń)"]
    if fx is None:
        return lines + ["  nie pobrano"]
    lines.append(f"  próba bezpośrednia GET /markets/{fmt(wanted)}: kod {fmt(fx['direct_status'])}")
    if fx["direct_status"] != 200:
        lines.append(f"  wyszukiwanie GET /markets?searchTerm=…: kod {fmt(fx['search_status'])}; kandydaci:")
        lines += [f"    {fmt(c['epic'])} | {fmt(c['name'])} | {fmt(c['type'])} | {fmt(c['status'])} | bid={fmt(c['bid'])} offer={fmt(c['offer'])}" for c in fx["candidates"][:8]] or ["    brak"]
    if fx["epic"] is None:
        return lines + ["  epik pary: NIE USTALONO (sprawdź listę kandydatów albo ustaw GW_FX_EPIC)"]
    lines.append(f"  epik pary: {fmt(fx['epic'])}")
    if fx["info"] is None:
        lines.append(f"  odczyt notowania nieudany: kod {fmt(fx['status'])}" + (f", errorCode={fmt(fx['error'])}" if fx["error"] else ""))
        row = fx["candidate"]
        if row and row["bid"] is not None:
            lines.append("  notowanie z wyszukiwania: " + kv([("bid", row["bid"]), ("offer", row["offer"]), ("status", row["status"]), ("czas UTC", row["updateTimeUTC"])]))
        return lines
    m = fx["info"]
    lines.append("  " + kv([("nazwa", m["name"]), ("typ", m["type"]), ("waluta", m["currency"])]))
    lines.append(_quote_line(m["quote"], fx["age_s"]))
    return lines


def _profit_lines(prefix, record):
    lines = []
    for label, key in (("pola celu zysku", "target_fields"), ("pola stopu", "stop_fields"), ("pola wyniku", "result_fields")):
        value = record[key]
        lines.append(f"{prefix}{label}: " + (", ".join(f"{k}={fmt(v)}" for k, v in value.items()) if value else "brak kluczy"))
    return lines


def _render_positions(section):
    lines = ["== 5. otwarte pozycje (GET /positions)"]
    if section is None:
        return lines + ["  nie pobrano"]
    if section["info"] is None:
        return lines + [_failed(section)]
    info = section["info"]
    if not info["count"]:
        return lines + ["  brak otwartych pozycji: pól celu zysku w GET /positions nie da się sprawdzić bez otwartej pozycji"]
    for i, p in enumerate(info["items"][:10], 1):
        lines.append(f"  {i}) " + kv([("epic", p["epic"]), ("kierunek", p["direction"]), ("wielkość", p["size"]), ("waluta", p["currency"]),
                                      ("upl", p["upl"]), ("poziom", p["level"])], ", "))
        lines += _profit_lines("     ", p)
        lines.append("     klucze position: " + keys_text(p["position_keys"]))
    return lines


def _render_confirms(items, wanted):
    lines = [f"== 6. potwierdzenia (GET /confirms/{{dealReference}}, do {wanted} referencji z historii i pozycji)"]
    if not items:
        return lines + ["  brak referencji do sprawdzenia (albo GW_CONFIRMS=0)"]
    for i, c in enumerate(items, 1):
        if c["info"] is None:
            lines.append(f"  {i}) kod {fmt(c['status'])}" + (f", errorCode={fmt(c['error'])}" if c["error"] else ""))
            continue
        d = c["info"]
        lines.append(f"  {i}) kod {fmt(c['status'])}, " + kv([("status", d["status"]), ("dealStatus", d["deal_status"]), ("reason", d["reason"]),
                                                             ("epic", d["epic"]), ("kierunek", d["direction"]), ("affectedDeals", d["affected"])], ", "))
        lines += _profit_lines("     ", d)
        lines.append("     klucze: " + keys_text(d["keys"]))
    return lines


def _currency_verdict(rep):
    account = rep["accounts"]["bound_currency"] or rep["accounts"]["active_currency"]
    info = (rep["market"] or {}).get("info") or {}
    instrument = info.get("currency")
    differs = "nie ustalono" if not account or not instrument else ("tak" if account != instrument else "nie")
    return account, instrument, differs


def _summary(rep):
    epic = rep["config"]["epic"]
    lines = ["== PODSUMOWANIE (wklej całość z powrotem)"]
    if rep["aborted"]:
        lines.append(f"  przerwano: {fmt(rep['aborted'])}; poniższe dane są niepełne")
    account, instrument, differs = _currency_verdict(rep)
    lines.append(f"  waluta rachunku: {fmt(account)} | waluta instrumentu {fmt(epic)}: {fmt(instrument)} | różne: {differs}")
    info = (rep["market"] or {}).get("info") or {}
    if info.get("currencies"):
        lines.append("  kursy w danych instrumentu (instrument.currencies): " + "; ".join(
            f"{fmt(c['code'])} exchangeRate={fmt(c['exchangeRate'])} baseExchangeRate={fmt(c['baseExchangeRate'])}" for c in info["currencies"]))
    else:
        lines.append("  kursy w danych instrumentu: brak pola instrument.currencies" if info else "  kursy w danych instrumentu: instrument nieodczytany")
    tx = (rep["transactions"] or {}).get("summary")
    act = (rep["activity"] or {}).get("summary")
    if tx:
        lines.append("  wiersze przewalutowania w historii transakcji: " + ", ".join(f"{t}: {n}" for t, n in tx["fx_counts"].items()) + f" (okno ~{rep['config']['days']} dni)")
    closed_tx, closed_act = (len(tx["instrument_closed"]) if tx else 0), (len(act["closing"]) if act else 0)
    closed_currencies = sorted({r["currency"] for r in tx["instrument_closed"] if isinstance(r["currency"], str)}) if tx else []
    lines.append(f"  zamknięte transakcje {fmt(epic)}: w historii transakcji {closed_tx}"
                 + (f" (waluty: {', '.join(map(fmt, closed_currencies))}; najnowsza kwota size={fmt(tx['instrument_closed'][0]['size'])})" if closed_currencies else "")
                 + f", w historii aktywności {closed_act}"
                 + (f" (details.currency: {', '.join(sorted({fmt(c['currency']) for c in act['closing']}))})" if act and act["closing"] else ""))
    if not closed_tx and not closed_act:
        lines.append(f"  brak zamkniętych transakcji {fmt(epic)} w oknie: odczyt NIE rozstrzyga waluty wyniku ani kursu rozliczenia; potrzebna próba na wielkości minimalnej (decyzja właściciela, pytanie 6 w projekcie)")
    positions = (rep["positions"] or {}).get("info")
    if positions and positions["count"]:
        lines.append(f"  pola celu zysku w GET /positions (pozycji: {positions['count']}): " + (", ".join(positions["target_keys"]) or "brak kluczy celu"))
    else:
        lines.append("  pola celu zysku w GET /positions: brak otwartych pozycji, nie do sprawdzenia")
    confirms = [c for c in rep["confirms"] if c["info"] is not None]
    if confirms:
        keys = sorted({k for c in confirms for k in c["info"]["target_fields"]})
        results = sorted({k for c in confirms for k in c["info"]["result_fields"]})
        lines.append(f"  pola celu zysku w GET /confirms (odczytano {len(confirms)} z {len(rep['confirms'])}): " + (", ".join(keys) or "brak kluczy celu")
                     + " | pola wyniku: " + (", ".join(results) or "brak"))
    else:
        lines.append(f"  pola celu zysku w GET /confirms: nie odczytano żadnego potwierdzenia (próbowano {len(rep['confirms'])})")
    fx = rep["fx"]
    if fx and fx["info"]:
        q = fx["info"]["quote"]
        lines.append(f"  para EUR/USD: epic={fmt(fx['epic'])} bid={fmt(q['bid'])} offer={fmt(q['offer'])} status={fmt(q['marketStatus'])} wiek notowania [s]={fmt(fx['age_s'])}")
    elif fx and fx["candidate"] and fx["candidate"]["bid"] is not None:
        c = fx["candidate"]
        lines.append(f"  para EUR/USD (z wyszukiwania, odczyt szczegółów nieudany): epic={fmt(c['epic'])} bid={fmt(c['bid'])} offer={fmt(c['offer'])} status={fmt(c['status'])}")
    else:
        lines.append("  para EUR/USD: epiku albo notowania nie ustalono")
    if info:
        lines.append(f"  reguły handlu {fmt(epic)}: " + (_rules_text({k: v for k, v in info["rules"].items() if k in ('minDealSize', 'maxDealSize', 'minSizeIncrement', 'minStepDistance', 'minStopOrProfitDistance', 'maxStopOrProfitDistance')}) or "brak"))
    lines.append(f"  zapytań do brokera: {rep['requests']}")
    return lines


def render(rep):
    cfg = rep["config"]
    lines = ["== diag_fx: tylko odczyt (jedno logowanie, żadnych zleceń; numery, identyfikatory i klucze ukryte) =="]
    if rep["aborted"]:
        lines.append(f"  przerwano: {fmt(rep['aborted'])}")
    lines += _render_accounts(rep["accounts"])
    lines += _render_market(rep["market"], cfg["epic"])
    lines += _render_transactions(rep["transactions"], cfg["days"])
    lines += _render_activity(rep["activity"], cfg["epic"])
    lines += _render_fx(rep["fx"], cfg["fx_epic"])
    lines += _render_positions(rep["positions"])
    lines += _render_confirms(rep["confirms"], cfg["confirms"])
    return lines + _summary(rep)


# ---------------------------------------------------------------- przebieg: jedno logowanie, odczyty, wylogowanie

async def run(session, base, creds, cfg, out=print, now=None, sleep=asyncio.sleep):
    secrets = [creds.api_key, creds.identifier, creds.api_password]

    def emit(line):
        out(scrub(str(line), secrets))

    async with session.post(base + "/session", headers={"X-CAP-API-KEY": creds.api_key},
                            json={"identifier": creds.identifier, "password": creds.api_password, "encryptedPassword": False}) as resp:
        if resp.status != 200:
            emit(f"logowanie nieudane, kod {resp.status} - nie ponawiam (ryzyko blokady)")
            return 1
        cst, security = resp.headers.get("CST"), resp.headers.get("X-SECURITY-TOKEN")
        login = parse_json(await resp.text())
    if not cst or not security:
        emit("logowanie bez tokenów sesji w odpowiedzi (brak CST lub X-SECURITY-TOKEN) - przerywam")
        return 1
    hdr = {"CST": cst, "X-SECURITY-TOKEN": security}
    bound_id = load_bound_account(Path(cfg["data_dir"]) / "account.json")
    root = as_dict(login)
    secrets += [cst, security, bound_id, root.get("currentAccountId"), root.get("clientId")]
    secrets += [as_dict(a).get("accountId") for a in as_list(root.get("accounts"))]
    _SECRETS[:] = secrets

    async def get(path, params=None):
        await sleep(PAUSE_S)
        async with session.get(base + path, headers=hdr, params=params) as reply:
            return reply.status, parse_json(await reply.text())

    code = 1
    try:
        report = await collect(get, cfg, now or datetime.now(timezone.utc), login, bound_id)
        for line in render(report):
            emit(line)
        emit(write_raw(Path(cfg["out"]), report["raw"]))
        code = 0
    except Exception as exc:  # treść wyjątku mogłaby zawierać dane: drukujemy tylko jego typ
        emit(f"błąd w trakcie odczytu: {type(exc).__name__} (szczegóły pominięto)")
    finally:
        try:
            async with session.delete(base + "/session", headers=hdr):
                pass
        except Exception:
            pass
        _SECRETS.clear()
    return code


def main():
    import aiohttp

    from gwstock.broker.capitalcom import LIVE_BASE_URL, load_credentials

    cfg = load_config(os.environ)
    try:
        creds = load_credentials(Path(cfg["key_file"]))
    except ValueError as exc:  # komunikat nie zawiera treści pliku sekretów
        print("klucz niedostępny:", exc)
        return 1

    async def go():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            return await run(session, LIVE_BASE_URL + "/api/v1", creds, cfg)

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
