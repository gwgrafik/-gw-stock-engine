"""Kalendarz i godziny sesji giełdy (profile XETRA i NYSE), z konwersją czasu lokalnego na UTC.

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
# W 2026 handel odbywa się w święta: Wniebowstąpienie 14.05, Poniedziałek Zesłania Ducha Świętego 25.05,
# Boże Ciało 4.06 (nie ma ich tu).
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

# Profil NYSE (sesja zwykła zakładanych instrumentów USA, np. US500/US100): 09:30-16:00 czasu nowojorskiego.
# Strefę i zmiany czasu (w USA: druga niedziela marca i pierwsza niedziela listopada, czyli 1.11.2026 i 14.03.2027)
# obsługuje `zoneinfo`; tabela poniżej nie zawiera żadnych przesunięć UTC. Handel przed i po sesji jest poza zakresem.
#
# Źródło i stopień pewności: daty WYLICZONE Z REGUŁ kalendarza NYSE (4.11.2026, zadanie offline: oficjalnej strony
# NYSE „Holidays & Trading Hours” (nyse.com) nie otwierano, więc żadnej daty nie potwierdzono u źródła).
# Ogólna reguła przeniesień: święto w sobotę przesuwa się na piątek, w niedzielę na poniedziałek; jedyny wyjątek to
# Nowy Rok w sobotę, który NIE przesuwa się na piątek 31 grudnia poprzedniego roku (wtedy sesja się odbywa).
# Tabela nie zawiera zamknięć nadzwyczajnych (np. dni żałoby narodowej) ani skróceń ogłaszanych w trakcie roku.
#
# DO POTWIERDZENIA przez właściciela (nic z tej listy nie zostało sprawdzone u źródła, wpisy opierają się na regule):
#   1. Cała tabela, święta i skrócone sesje (2026-2027), do porównania z oficjalnym kalendarzem NYSE przed pierwszym
#      użyciem pasma na instrumencie USA w trybie innym niż dry_run.
#   2. Juneteenth 2027: 19.06.2027 to sobota, wpisano piątek 18.06.2027 (ogólna reguła sobota -> piątek; nie ma
#      precedensu od wprowadzenia święta, bo w latach 2022-2026 nie wypadło w sobotę).
#   3. Boże Narodzenie 2027: 25.12.2027 to sobota, wpisano piątek 24.12.2027 (ogólna reguła sobota -> piątek);
#      w konsekwencji 24.12.2027 nie jest skróconą sesją, tylko świętem.
#   4. Sesja 31.12.2027: nie wpisano święta, bo Nowy Rok 1.01.2028 wypada w sobotę (wyjątek od przeniesienia na piątek);
#      przy zmianie tej decyzji dopisać date(2027, 12, 31) do tabeli 2027.
#   5. Brak skróconej sesji 2.07.2026 i 2.07.2027: reguła „3 lipca” nie ma zastosowania (3.07.2026 to święto
#      przeniesione z 4 lipca, a 3.07.2027 to sobota), więc 2 lipca ma pełną sesję do 16:00.
#
# Święta 2026 (wszystkie dni robocze):
#   01.01 czwartek    Nowy Rok (1 stycznia)
#   19.01 poniedziałek Martin Luther King Jr. Day (trzeci poniedziałek stycznia)
#   16.02 poniedziałek Presidents' Day (trzeci poniedziałek lutego; 1.02.2026 to niedziela)
#   03.04 piątek      Wielki Piątek (dwa dni przed Wielkanocą 5.04.2026)
#   25.05 poniedziałek Memorial Day (ostatni poniedziałek maja; 31.05.2026 to niedziela)
#   19.06 piątek      Juneteenth (19 czerwca)
#   03.07 piątek      Dzień Niepodległości (4.07.2026 to sobota, więc przeniesienie na piątek)
#   07.09 poniedziałek Labor Day (pierwszy poniedziałek września; 1.09.2026 to wtorek)
#   26.11 czwartek    Thanksgiving (czwarty czwartek listopada; 1.11.2026 to niedziela)
#   25.12 piątek      Boże Narodzenie (25 grudnia)
# Święta 2027 (wszystkie dni robocze):
#   01.01 piątek      Nowy Rok (1 stycznia)
#   18.01 poniedziałek Martin Luther King Jr. Day (trzeci poniedziałek stycznia)
#   15.02 poniedziałek Presidents' Day (trzeci poniedziałek lutego; 1.02.2027 to poniedziałek)
#   26.03 piątek      Wielki Piątek (dwa dni przed Wielkanocą 28.03.2027)
#   31.05 poniedziałek Memorial Day (ostatni poniedziałek maja)
#   18.06 piątek      Juneteenth (19.06.2027 to sobota, przeniesienie na piątek; DO POTWIERDZENIA, punkt 2)
#   05.07 poniedziałek Dzień Niepodległości (4.07.2027 to niedziela, więc przeniesienie na poniedziałek)
#   06.09 poniedziałek Labor Day (pierwszy poniedziałek września; 1.09.2027 to środa)
#   25.11 czwartek    Thanksgiving (czwarty czwartek listopada; 1.11.2027 to poniedziałek)
#   24.12 piątek      Boże Narodzenie (25.12.2027 to sobota, przeniesienie na piątek; DO POTWIERDZENIA, punkt 3)
#
# Skrócone sesje (zamknięcie 13:00 czasu nowojorskiego, otwarcie bez zmian):
#   27.11.2026 piątek  dzień po Thanksgiving
#   24.12.2026 czwartek Wigilia w dzień sesji (25.12.2026 to piątek i święto)
#   26.11.2027 piątek  dzień po Thanksgiving
# Reguła 3 lipca (skrócona sesja, gdy 3.07 jest dniem sesji, a 4.07 dniem roboczym) nie daje wpisu w latach 2026-2027
# (DO POTWIERDZENIA, punkt 5). Wigilia 24.12.2027 jest przeniesionym świętem, nie skróconą sesją (punkt 3).
#
# DO PRZEGLĄDU KWARTALNEGO: dodać rok 2028 przed końcem 2027 (rok spoza tabeli to wygasły kalendarz, czyli brak
# handlu). Uwaga przy 2028: 1.01.2028 wypada w sobotę, więc wymaga decyzji z punktu 4.
NYSE = ExchangeProfile(
    name="NYSE",
    tz="America/New_York",
    open_local=time(9, 30),
    close_local=time(16, 0),
    holidays=MappingProxyType(
        {
            2026: frozenset(
                {
                    date(2026, 1, 1),  # Nowy Rok
                    date(2026, 1, 19),  # Martin Luther King Jr. Day: trzeci poniedziałek stycznia
                    date(2026, 2, 16),  # Presidents' Day: trzeci poniedziałek lutego
                    date(2026, 4, 3),  # Wielki Piątek: Wielkanoc 5.04.2026
                    date(2026, 5, 25),  # Memorial Day: ostatni poniedziałek maja
                    date(2026, 6, 19),  # Juneteenth: 19 czerwca (piątek)
                    date(2026, 7, 3),  # Dzień Niepodległości: 4 lipca w sobotę, przeniesienie na piątek
                    date(2026, 9, 7),  # Labor Day: pierwszy poniedziałek września
                    date(2026, 11, 26),  # Thanksgiving: czwarty czwartek listopada
                    date(2026, 12, 25),  # Boże Narodzenie: 25 grudnia (piątek)
                }
            ),
            2027: frozenset(
                {
                    date(2027, 1, 1),  # Nowy Rok (piątek)
                    date(2027, 1, 18),  # Martin Luther King Jr. Day: trzeci poniedziałek stycznia
                    date(2027, 2, 15),  # Presidents' Day: trzeci poniedziałek lutego
                    date(2027, 3, 26),  # Wielki Piątek: Wielkanoc 28.03.2027
                    date(2027, 5, 31),  # Memorial Day: ostatni poniedziałek maja
                    # Juneteenth: 19 czerwca w sobotę, więc piątek; DO POTWIERDZENIA przez właściciela (2)
                    date(2027, 6, 18),
                    date(2027, 7, 5),  # Dzień Niepodległości: 4 lipca w niedzielę, przeniesienie na poniedziałek
                    date(2027, 9, 6),  # Labor Day: pierwszy poniedziałek września
                    date(2027, 11, 25),  # Thanksgiving: czwarty czwartek listopada
                    # Boże Narodzenie: 25 grudnia w sobotę, więc piątek; DO POTWIERDZENIA przez właściciela (3)
                    date(2027, 12, 24),
                    # 31.12.2027 celowo bez wpisu (Nowy Rok 2028 w sobotę); DO POTWIERDZENIA przez właściciela (4)
                }
            ),
        }
    ),
    early_closes=MappingProxyType(
        {
            date(2026, 11, 27): time(13, 0),  # dzień po Thanksgiving
            date(2026, 12, 24): time(13, 0),  # Wigilia w dzień sesji (czwartek)
            date(2027, 11, 26): time(13, 0),  # dzień po Thanksgiving
        }
    ),
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
