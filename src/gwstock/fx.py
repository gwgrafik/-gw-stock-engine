"""Przeliczenia walut pasm (projekt wielu pasm §7.3, R8): JEDEN protokół `FxView` dla wykonawcy i bramki portfelowej
oraz jego implementacja z notowania pary walutowej brokera (`QuoteFxView`).

Zasady (prawdziwe pieniądze, rachunek w EUR):
- kurs WYŁĄCZNIE z notowania brokera (`portfolio.toml`: `fx_quote_epic`, np. EURUSD), nie starszego niż
  `fx_quote_max_age_s` (wiek liczony od `Quote.ts`, chwili ostatniej zmiany pary; notowanie „z przyszłości” jest
  nieważne); brak ważnego kursu = None, NIGDY kurs zgadnięty ani stały;
- strona niekorzystna: kwota w walucie instrumentu (USD) → EUR przez `bid` pary EUR/USD, czyli większą kwotę EUR
  (ryzyko, wartość punktu i depozyt nigdy nie wychodzą zaniżone; `to_account`);
- szacunek wyniku zrealizowanego (`estimate`, księgowanie dwufazowe §7.4, źródło `ESTIMATED_FX`): strata przez `bid`
  (większa strata), zysk przez `ask` (mniejszy zysk), wynik zaokrąglony do centa W DÓŁ; to szacunek z kursu brokera,
  nie kwota rozliczenia brokera;
- `par_is_upper_bound`: ostatni znany `bid` (także nieaktualny) >= 1, czyli 1 jednostka waluty instrumentu jest warta
  najwyżej 1 EUR: kwota w walucie instrumentu jest wtedy górnym ograniczeniem kwoty w EUR (straże po wypełnieniu,
  które nie mogą czekać na kurs; wejść to nie odblokowuje).

Kurs służy wyłącznie kontrolom wejścia i szacunkowi; brak kursu blokuje WEJŚCIA, nigdy wyjścia (wyjścia nie pytają o
kurs).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import ROUND_FLOOR, ROUND_UP, Context, Decimal
from typing import Protocol, runtime_checkable

from gwstock.clock import Clock
from gwstock.types import Quote

__all__ = ["FxView", "QuoteFxView", "ESTIMATED_FX"]

ESTIMATED_FX = "ESTIMATED_FX"  # źródło rozliczenia fazy 2 z szacunku kursu brokera (`TradeFx.source`)

_PAIR = re.compile(r"[A-Z]{3}[A-Z]{3}")
_CENT = Decimal("0.01")
_AWAY = Context(prec=28, rounding=ROUND_UP)  # dzielenie zaokrąglane od zera: kwota co do modułu nie zaniżona
_DOWN = Context(prec=60, rounding=ROUND_FLOOR)  # szacunek wyniku: w dół (strata nie mniejsza, zysk nie większy)


@runtime_checkable
class FxView(Protocol):
    """Przeliczenia walut wstrzykiwane wykonawcy pasma w walucie obcej i bramce portfelowej (projekt §7.3).

    - `account_currency`: waluta rachunku (kod ISO 4217);
    - `to_account(amount, currency)`: kwota w `currency` w walucie rachunku po kursie NIEKORZYSTNYM (co do modułu nie
      mniejsza od rzeczywistej), z ważnego kursu; None (albo wyjątek): brak ważnego kursu;
    - `par_is_upper_bound(currency)`: czy ostatni znany kurs (także nieaktualny) daje 1 jednostkę `currency` za
      najwyżej 1 jednostkę waluty rachunku.
    """

    @property
    def account_currency(self) -> str: ...

    def to_account(self, amount: Decimal, currency: str) -> Decimal | None: ...

    def par_is_upper_bound(self, currency: str) -> bool: ...


class QuoteFxView:
    """`FxView` z notowania pary `pair_epic` (waluta bazowa = waluta rachunku, np. EURUSD dla rachunku EUR).

    Silnik podaje każde notowanie pary przez `note_quote` (odczyt brokera w pętli, z ponownym logowaniem); widok sam
    niczego nie czyta i nigdy nie rzuca przy przeliczeniu (brak kursu = None)."""

    def __init__(self, pair_epic: str, max_age_s: int, clock: Clock, account_currency: str = "EUR") -> None:
        if not isinstance(pair_epic, str) or not _PAIR.fullmatch(pair_epic):
            raise ValueError(f"QuoteFxView: pair epic must be six capital letters (e.g. EURUSD), got {pair_epic!r}")
        if pair_epic[:3] != account_currency:
            raise ValueError(
                f"QuoteFxView: the pair {pair_epic} must quote the account currency {account_currency} as its base"
            )
        if isinstance(max_age_s, bool) or not isinstance(max_age_s, int) or max_age_s <= 0:
            raise ValueError(f"QuoteFxView: max_age_s must be a positive integer, got {max_age_s!r}")
        self._pair = pair_epic
        self._quote_currency = pair_epic[3:]
        self._account = account_currency
        self._max_age = timedelta(seconds=max_age_s)
        self._clock = clock
        self._last: Quote | None = None

    @property
    def account_currency(self) -> str:
        return self._account

    @property
    def pair_epic(self) -> str:
        return self._pair

    @property
    def quote_currency(self) -> str:
        return self._quote_currency

    def note_quote(self, q: Quote) -> None:
        """Notowanie pary (inne instrumenty i notowanie bez sensu, np. bid <= 0 albo bid > ask, są pomijane)."""
        if not isinstance(q, Quote) or q.epic != self._pair:
            return
        if not (q.bid.is_finite() and q.ask.is_finite() and 0 < q.bid <= q.ask):
            return
        self._last = q

    def _fresh(self) -> Quote | None:
        q = self._last
        if q is None:
            return None
        age = self._clock.now_utc() - q.ts
        return q if timedelta(0) <= age <= self._max_age else None

    def to_account(self, amount: Decimal, currency: str) -> Decimal | None:
        if currency == self._account:
            return amount
        q = self._fresh()
        if currency != self._quote_currency or q is None:
            return None
        return _AWAY.divide(amount, q.bid)

    def estimate(self, amount: Decimal, currency: str) -> tuple[Decimal, Decimal | None] | None:
        """Szacunek wyniku w walucie rachunku: (kwota zaokrąglona do centa w dół, kurs efektywny) albo None (brak
        ważnego kursu). Strata po `bid`, zysk po `ask`; w walucie rachunku: (kwota, None)."""
        if currency == self._account:
            return amount, None
        q = self._fresh()
        if currency != self._quote_currency or q is None:
            return None
        rate = q.bid if amount < 0 else q.ask
        return _DOWN.divide(amount, rate).quantize(_CENT, rounding=ROUND_FLOOR), rate

    def par_is_upper_bound(self, currency: str) -> bool:
        if currency == self._account:
            return True
        q = self._last
        return currency == self._quote_currency and q is not None and q.bid >= 1
