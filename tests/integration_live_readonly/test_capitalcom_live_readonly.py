"""Testy na ŻYWYM koncie Capital.com: wyłącznie odczyty, uruchamia je ręcznie właściciel.

Uruchomienie (z korzenia repozytorium, na maszynie z plikiem sekretów):

    GW_CAPITALCOM_KEY_FILE=/home/claude/.config/gw/capitalcom.json python -m pytest -m integration_live_readonly -v

Bez pliku sekretów (ścieżka z `GW_CAPITALCOM_KEY_FILE`, domyślnie `/home/claude/.config/gw/capitalcom.json`)
wszystkie testy są pomijane. Rachunek związany: `data/account.json` (katalog z `GW_DATA_DIR`, domyślnie `data`);
bez niego adapter zostaje na rachunku domyślnym sesji.

Bezpieczeństwo:
- żadnych zleceń, zmian stopu ani zamknięć: strażnik na poziomie sesji HTTP przerywa KAŻDE żądanie inne niż GET,
  `POST /session` (logowanie) i `PUT /session` (przełączenie na rachunek związany przy logowaniu), zanim wyjdzie;
- testy niczego nie wypisują (żadnych kwot, identyfikatorów, tokenów); asercje porównują tylko kształt i reguły;
- logowania są rozsunięte o ponad 1 s (limit brokera na klucz API).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from collections.abc import AsyncIterator
from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from gwstock.broker.capitalcom import CapitalComBroker, load_credentials
from gwstock.clock import SystemClock
from gwstock.config import load_runtime
from gwstock.types import Candle, ClosedDeal, OpenPosition, WorkingOrder

KEY_FILE = Path(os.environ.get("GW_CAPITALCOM_KEY_FILE", "/home/claude/.config/gw/capitalcom.json"))
DATA_DIR = Path(os.environ.get("GW_DATA_DIR", "data"))
EPIC = "DE40"
_LOGIN_GAP_S = 1.2
_last_login = [0.0]


def _secrets_present() -> bool:
    try:
        return KEY_FILE.is_file()
    except OSError:  # np. brak prawa do katalogu: jak brak pliku
        return False


pytestmark = [
    pytest.mark.integration_live_readonly,
    pytest.mark.skipif(
        not _secrets_present(),
        reason="no Capital.com secrets file (GW_CAPITALCOM_KEY_FILE); live read-only tests are run by the owner",
    ),
]


class WriteAttempt(RuntimeError):
    """Test tylko do odczytu próbował wysłać żądanie zmieniające stan konta."""


async def _refuse_writes(session, context, params) -> None:
    path = params.url.path
    allowed = params.method == "GET" or (params.method in ("POST", "PUT") and path == "/api/v1/session")
    if not allowed:
        raise WriteAttempt(f"live read-only test refused to send {params.method} {path}")


@pytest.fixture
def bound_account() -> str | None:
    """Rachunek związany z `data/account.json` (None: brak powiązania, zostaje rachunek domyślny sesji)."""
    return load_runtime(Path("config"), DATA_DIR).account_id


@pytest.fixture
async def live(bound_account) -> AsyncIterator[CapitalComBroker]:
    creds = load_credentials(KEY_FILE)
    if bound_account is not None:
        creds = dataclasses.replace(creds, account_id=bound_account)
    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_refuse_writes)
    async with aiohttp.ClientSession(trace_configs=[trace]) as session:
        broker = CapitalComBroker(creds, session, SystemClock())
        await asyncio.sleep(max(0.0, _LOGIN_GAP_S - (time.monotonic() - _last_login[0])))
        try:
            await broker.login()
        finally:
            _last_login[0] = time.monotonic()
        yield broker


async def test_read_only_guard_refuses_an_order_before_it_is_sent(live):
    with pytest.raises(WriteAttempt):
        await live._request("DELETE", "/positions/guard-check-never-sent")


async def test_accounts_listed_and_bound_account_has_balance(live, bound_account):
    accounts = await live.list_accounts()
    assert accounts and len({a.account_id for a in accounts}) == len(accounts)
    active = await live.account()
    if bound_account is not None:
        assert active.account_id == bound_account
    assert (active.currency, active.status) == ("EUR", "ENABLED")
    assert active.equity > 0 and active.available <= active.equity


async def test_de40_rules_match_readout(live):
    rules = await live.instrument_rules(EPIC)
    assert (rules.min_size, rules.size_step, rules.stop_step, rules.margin_rate) == (
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("0.1"),
        Decimal("0.05"),
    )
    assert rules.stop_step != 0 and 0 < rules.min_stop_distance < 20  # 0,01% ceny ≈ 2,6 pkt


async def test_de40_quote_has_bid_and_offer(live):
    quote = await live.quote(EPIC)
    assert quote.epic == EPIC and 0 < quote.bid <= quote.ask
    assert quote.ts.utcoffset() == timedelta(0)


async def test_five_minute_candles_are_completed_and_on_boundaries(live):
    now = SystemClock().now_utc()
    candles = await live.candles_5m(EPIC, now - timedelta(days=3), now)  # obejmuje też weekend
    assert candles and all(isinstance(c, Candle) for c in candles)
    starts = [c.start for c in candles]
    assert starts == sorted(set(starts))
    for c in candles:
        assert c.start.tzinfo == timezone.utc and c.start.minute % 5 == 0 and c.start.second == c.start.microsecond == 0
        assert c.start + timedelta(minutes=5) <= now  # tylko zakończone
        assert c.low <= min(c.open, c.close) and max(c.open, c.close) <= c.high


async def test_daily_candles_at_least_15(live):
    now = SystemClock().now_utc()
    days = await live.daily_candles(EPIC, 15)
    assert len(days) == 15
    assert [d.start for d in days] == sorted({d.start for d in days})
    assert all(d.start + timedelta(days=1) <= now for d in days)


async def test_positions_and_working_orders_are_lists(live):
    positions = await live.list_positions()
    orders = await live.list_working_orders()
    assert isinstance(positions, list) and all(isinstance(p, OpenPosition) for p in positions)
    assert isinstance(orders, list) and all(isinstance(o, WorkingOrder) for o in orders)


async def test_closed_since_two_days_does_not_raise(live):
    closed = await live.closed_since(SystemClock().now_utc() - timedelta(days=2))
    assert isinstance(closed, list) and all(isinstance(c, ClosedDeal) for c in closed)
