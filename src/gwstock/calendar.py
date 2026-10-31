"""Kalendarz i godziny sesji giełdy (profil XETRA), z konwersją czasu lokalnego na UTC.

Uwaga: moduł nazywa się `gwstock.calendar`; importować go zawsze jako `gwstock.calendar`
(nie uruchamiać skryptów z katalogu `src/gwstock/`, bo przesłoniłby bibliotekę standardową `calendar`).

Zasady:
- decyzje czasowe opierają się na `zoneinfo` i profilu giełdy, bez stałych przesunięć UTC;
- rok spoza tabeli świąt to wygasły kalendarz: `CalendarExpiredError` (podklasa ValueError),
  który wywołujący ma zamienić na brak wejścia; nieznany rok nigdy nie jest traktowany jak dni sesyjne;
- godziny otwarcia, zamknięcia i spłaszczenia są zdefiniowane tylko dla dni sesyjnych (inaczej ValueError).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarExpiredError(ValueError):
    """Brak tabeli świąt dla roku: kalendarz wygasł, więc nie wolno handlować."""


@dataclass(frozen=True)
class ExchangeProfile:
    name: str
    tz: str
    open_local: time
    close_local: time
    holidays: Mapping[int, frozenset[date]]
    early_closes: Mapping[date, time]

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"tz: unknown time zone {self.tz!r}") from exc
        if not self.open_local < self.close_local:
            raise ValueError(
                f"open_local: must be earlier than close_local, got {self.open_local} / {self.close_local}"
            )
        for year, days in self.holidays.items():
            for d in days:
                if d.year != year:
                    raise ValueError(f"holidays: date {d} is listed under year {year}")
        for d, close in self.early_closes.items():
            if not self.open_local < close < self.close_local:
                raise ValueError(
                    f"early_closes: {d} close {close} must be between {self.open_local} and {self.close_local}"
                )


# Tabela świąt: dni robocze (pon.-pt.) bez sesji; weekendy i tak nie są dniami sesyjnymi.
#
# Źródło: oficjalny kalendarz handlowy Deutsche Börse (Xetra i Börse Frankfurt), „Non-trading days at
# Frankfurter Wertpapierbörse (FWB)”, pobrany 2026-10-31:
#   https://www.cashmarket.deutsche-boerse.com/cash-en/trading/trading-calendar-and-trading-hours
#   (dawny adres www.xetra.com/xetra-en/trading/trading-calendar-and-trading-hours przekierowuje tutaj);
#   pełna tabela także na https://www.eurexgroup.com/xetra-en/trading/trading-calendar-and-trading-hours;
#   PDF „Trading calendar 2026” (cashmarket.deutsche-boerse.com/resource/blob/4580826/
#   3e974decb414d6769cc2bc8e601f6c9a/data/xetra-trading-calendar-2026.pdf): „trading Mondays to Fridays in 2026,
#   with the exception of 1 January, 3 April, 6 April, 1 May, 24 December, 25 December and 31 December”.
#   2026: 1.01, Wielki Piątek 3.04, Poniedziałek Wielkanocny 6.04, 1.05, 24.12, 25.12, 31.12 (26.12 to sobota).
#   2027: 1.01, Wielki Piątek 26.03, Poniedziałek Wielkanocny 29.03, 24.12, 31.12
#         (1.05, 25.12 i 26.12 przypadają w weekend, więc ich tu nie ma).
# Strona zastrzega: „The overview is for information purposes only. Changes for future years are not excluded.”
# Wpis 31.12.2027 potwierdzają pełna tabela na eurexgroup.com („Friday 31 Dec 2027”) i tabela na
# live.deutsche-boerse.com („New Years Eve, 31 December”); strona cashmarket w pobranej postaci nie pokazywała
# komórek 31.12.2026/2027, ale PDF 2026 potwierdza 31.12.2026.
# W 2026 handel odbywa się w święta: Wniebowstąpienie 14.05, Zielone Świątki 25.05, Boże Ciało 4.06 (nie ma ich tu).
#
# Skrócone sesje: oficjalny kalendarz nie wskazuje żadnej na 2026-2027 (w 2025 było skrócone 30.12 do 14:00),
# więc `early_closes` jest puste. UWAGA: strona zastrzega, że „On December 30, 2026, deviating trading hours may
# apply” (komunikat „circular” zostanie opublikowany później). Przed 30.12.2026 sprawdzić komunikat o ostatniej
# sesji roku i w razie skrócenia dopisać `date(2026, 12, 30): time(<godzina z komunikatu>)` do `early_closes`.
#
# DO PRZEGLĄDU KWARTALNEGO: porównać tabelę z oficjalną stroną; dodać kolejne lata przed końcem 2027
# (rok spoza tabeli to wygasły kalendarz, czyli brak handlu).
XETRA = ExchangeProfile(
    name="XETRA",
    tz="Europe/Berlin",
    open_local=time(9, 0),
    close_local=time(17, 30),
    holidays=MappingProxyType(
        {
            2026: frozenset(
                {
                    date(2026, 1, 1),
                    date(2026, 4, 3),
                    date(2026, 4, 6),
                    date(2026, 5, 1),
                    date(2026, 12, 24),
                    date(2026, 12, 25),
                    date(2026, 12, 31),
                }
            ),
            2027: frozenset(
                {
                    date(2027, 1, 1),
                    date(2027, 3, 26),
                    date(2027, 3, 29),
                    date(2027, 12, 24),
                    date(2027, 12, 31),
                }
            ),
        }
    ),
    early_closes=MappingProxyType({}),
)


def _require_plain_date(d: date) -> None:
    # datetime jest podklasą date, ale nie równa się date: święto przeszłoby niezauważone
    if isinstance(d, datetime):
        raise TypeError(f"d: expected a date, got a datetime ({d!r})")


class ExchangeCalendar:
    def __init__(self, profile: ExchangeProfile) -> None:
        self._profile = profile
        self._tz = ZoneInfo(profile.tz)

    @property
    def profile(self) -> ExchangeProfile:
        return self._profile

    def holidays(self, year: int) -> frozenset[date]:
        try:
            return self._profile.holidays[year]
        except KeyError:
            raise CalendarExpiredError(
                f"holidays: no holiday table for year {year} in profile {self._profile.name} (calendar expired)"
            ) from None

    def is_session_day(self, d: date) -> bool:
        _require_plain_date(d)
        holidays = self.holidays(d.year)  # nieznany rok zawsze zgłasza błąd, także w weekend
        return d.weekday() < 5 and d not in holidays

    def open_time(self, d: date) -> time:
        self._require_session_day(d)
        return self._profile.open_local

    def close_time(self, d: date) -> time:
        self._require_session_day(d)
        return self._profile.early_closes.get(d, self._profile.close_local)

    def flatten_time(self, d: date, before_close_min: int) -> time:
        if before_close_min < 0:
            raise ValueError(f"before_close_min: must be >= 0, got {before_close_min}")
        close = datetime.combine(d, self.close_time(d))
        flatten = close - timedelta(minutes=before_close_min)
        if flatten <= datetime.combine(d, self.open_time(d)):
            raise ValueError(
                f"before_close_min: {before_close_min} min before close {close.time()} is not after open on {d}"
            )
        return flatten.time()

    def local_to_utc(self, d: date, t: time) -> datetime:
        _require_plain_date(d)
        if t.tzinfo is not None:
            raise ValueError(f"t: expected a naive local time, got {t!r}")
        naive = datetime.combine(d, t)
        first = naive.replace(tzinfo=self._tz, fold=0)
        second = naive.replace(tzinfo=self._tz, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError(f"t: local time {naive} is ambiguous or does not exist in {self._profile.tz}")
        return first.astimezone(timezone.utc)

    def to_local(self, dt_utc: datetime) -> datetime:
        if dt_utc.tzinfo is None or dt_utc.utcoffset() is None:
            raise ValueError("dt_utc: expected a timezone-aware datetime, got a naive one")
        return dt_utc.astimezone(self._tz)

    def _require_session_day(self, d: date) -> None:
        if not self.is_session_day(d):
            raise ValueError(f"d: {d} is not a session day of {self._profile.name}")
