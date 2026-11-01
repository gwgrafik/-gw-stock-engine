"""Testy zegara i kalendarza giełdy (profil XETRA). Uruchamiać z korzenia repozytorium."""

import inspect
import sys
import time as wall_time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gwstock.calendar import XETRA, CalendarExpiredError, ExchangeCalendar, ExchangeProfile
from gwstock.clock import Clock, FakeClock, SystemClock

cal = ExchangeCalendar(XETRA)

UTC = timezone.utc


def _easter_sunday(year: int) -> date:
    """Niezależne obliczenie Wielkanocy (algorytm Meeusa/Jonesa/Butchera, kalendarz gregoriański)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _rule_based_weekday_holidays(year: int) -> frozenset[date]:
    easter = _easter_sunday(year)
    rules = {
        date(year, 1, 1),
        easter - timedelta(days=2),  # Wielki Piątek
        easter + timedelta(days=1),  # Poniedziałek Wielkanocny
        date(year, 5, 1),
        date(year, 12, 24),
        date(year, 12, 25),
        date(year, 12, 31),
    }
    return frozenset(d for d in rules if d.weekday() < 5)


# --- testy z briefu -------------------------------------------------------------------------------


def test_dst_boundary_eu_2026():
    assert cal.local_to_utc(date(2026, 10, 23), time(9, 0)) == datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)  # CEST
    assert cal.local_to_utc(date(2026, 10, 26), time(9, 0)) == datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)  # CET


def test_holidays_and_weekends_are_not_session_days():
    for d in (
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 31),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 11, 7),
    ):
        assert not cal.is_session_day(d)
    assert cal.is_session_day(date(2026, 11, 2))


def test_open_close_and_flatten_times():
    assert (cal.open_time(date(2026, 11, 2)), cal.close_time(date(2026, 11, 2))) == (time(9, 0), time(17, 30))
    assert cal.flatten_time(date(2026, 11, 2), 5) == time(17, 25)


def test_table_covers_2026_and_2027_and_is_consistent():
    for year in (2026, 2027):
        hol = cal.holidays(year)
        assert len(hol) >= 5 and all(d.weekday() < 5 for d in hol)


# --- tabela świąt: wartości z oficjalnego kalendarza i niezależna kontrola regułami -----------------


def test_holiday_table_matches_official_calendar_2026_2027():
    assert cal.holidays(2026) == frozenset(
        {
            date(2026, 1, 1),
            date(2026, 4, 3),
            date(2026, 4, 6),
            date(2026, 5, 1),
            date(2026, 12, 24),
            date(2026, 12, 25),
            date(2026, 12, 31),
        }
    )
    # 1 maja i 25 grudnia 2027 to soboty, więc nie ma ich w tabeli dni roboczych
    assert cal.holidays(2027) == frozenset(
        {
            date(2027, 1, 1),
            date(2027, 3, 26),
            date(2027, 3, 29),
            date(2027, 12, 24),
            date(2027, 12, 31),
        }
    )


@pytest.mark.parametrize("year", [2026, 2027])
def test_holiday_table_equals_rule_derived_dates(year):
    """Literówka w przepisanej tabeli nie przejdzie: porównanie z datami wyliczonymi z reguł (Wielkanoc)."""
    assert cal.holidays(year) == _rule_based_weekday_holidays(year)


def test_easter_helper_known_values():
    assert _easter_sunday(2026) == date(2026, 4, 5)
    assert _easter_sunday(2027) == date(2027, 3, 28)


def test_public_holidays_with_regular_trading_in_2026_are_session_days():
    """Oficjalny kalendarz: w 2026 handel odbywa się we Wniebowstąpienie, w Poniedziałek Zesłania Ducha Świętego
    i w Boże Ciało (daty liczone od Wielkanocy, nie przepisane)."""
    easter = _easter_sunday(2026)
    ascension = easter + timedelta(days=39)  # czwartek
    whit_monday = easter + timedelta(days=50)  # Poniedziałek Zesłania Ducha Świętego (Zielone Świątki to niedziela)
    corpus_christi = easter + timedelta(days=60)  # czwartek
    assert (ascension, whit_monday, corpus_christi) == (date(2026, 5, 14), date(2026, 5, 25), date(2026, 6, 4))
    assert (ascension.weekday(), whit_monday.weekday(), corpus_christi.weekday()) == (3, 0, 3)
    for d in (ascension, whit_monday, corpus_christi):
        assert cal.is_session_day(d)


def test_xetra_profile_has_no_early_closes_in_table_years():
    assert dict(XETRA.early_closes) == {}


def test_weekends_are_not_session_days():
    assert not cal.is_session_day(date(2026, 11, 7))  # sobota
    assert not cal.is_session_day(date(2026, 11, 8))  # niedziela
    assert cal.is_session_day(date(2026, 11, 6))  # piątek


def test_expired_calendar_raises_value_error():
    """Rok poza tabelą = wygasły kalendarz = brak handlu; nigdy po cichu „wszystkie dni sesyjne”."""
    with pytest.raises(ValueError):
        cal.is_session_day(date(2028, 3, 1))  # środa
    with pytest.raises(ValueError):
        cal.is_session_day(date(2028, 1, 1))  # sobota: wynik nie zależy od dnia tygodnia
    with pytest.raises(ValueError):
        cal.holidays(2028)
    with pytest.raises(ValueError):
        cal.holidays(2025)
    with pytest.raises(CalendarExpiredError):
        cal.is_session_day(date(2028, 3, 1))


def test_open_close_flatten_on_non_session_day_raise():
    for d in (date(2026, 12, 24), date(2026, 11, 7)):
        with pytest.raises(ValueError):
            cal.open_time(d)
        with pytest.raises(ValueError):
            cal.close_time(d)
        with pytest.raises(ValueError):
            cal.flatten_time(d, 5)


def test_datetime_instead_of_date_is_rejected():
    """datetime jest podklasą date, ale nie równa się mu: bez straży święto zostałoby uznane za dzień sesji."""
    with pytest.raises(TypeError):
        cal.is_session_day(datetime(2026, 12, 24, 10, 0))  # type: ignore[arg-type]


# --- strefa czasowa i zmiana czasu -------------------------------------------------------------------


def test_local_to_utc_rejects_a_datetime_instead_of_a_date():
    """To samo zabezpieczenie co w `is_session_day`: datetime nie jest datą, a jego składnik czasu zostałby zignorowany."""
    with pytest.raises(TypeError, match="datetime"):
        cal.local_to_utc(datetime(2026, 12, 24, 10, 0), time(9, 0))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="datetime"):
        cal.local_to_utc(datetime(2026, 11, 2, 0, 0, tzinfo=UTC), time(9, 0))  # type: ignore[arg-type]


def test_dst_boundary_eu_2027_spring_forward():
    # czas letni w UE zaczyna się 28.03.2027
    assert cal.local_to_utc(date(2027, 3, 26), time(9, 0)) == datetime(2027, 3, 26, 8, 0, tzinfo=UTC)  # CET
    assert cal.local_to_utc(date(2027, 3, 29), time(9, 0)) == datetime(2027, 3, 29, 7, 0, tzinfo=UTC)  # CEST


def test_local_to_utc_returns_aware_utc_and_close_follows_dst():
    close_cest = cal.local_to_utc(date(2026, 10, 23), time(17, 30))
    close_cet = cal.local_to_utc(date(2026, 10, 26), time(17, 30))
    assert close_cest == datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
    assert close_cet == datetime(2026, 10, 26, 16, 30, tzinfo=UTC)
    assert close_cest.utcoffset() == timedelta(0)
    assert close_cet.utcoffset() == timedelta(0)


def test_local_to_utc_rejects_nonexistent_and_ambiguous_local_times():
    with pytest.raises(ValueError):
        cal.local_to_utc(date(2027, 3, 28), time(2, 30))  # luka przy przejściu na czas letni
    with pytest.raises(ValueError):
        cal.local_to_utc(date(2026, 10, 25), time(2, 30))  # powtórzona godzina przy powrocie na czas zimowy


def test_local_to_utc_rejects_timezone_aware_time():
    with pytest.raises(ValueError):
        cal.local_to_utc(date(2026, 11, 2), time(9, 0, tzinfo=UTC))


def test_to_local_returns_aware_berlin_datetime():
    local = cal.to_local(datetime(2026, 10, 23, 7, 0, tzinfo=UTC))
    assert local.tzinfo is not None
    assert local.utcoffset() == timedelta(hours=2)
    assert local.replace(tzinfo=None) == datetime(2026, 10, 23, 9, 0)
    assert local == datetime(2026, 10, 23, 7, 0, tzinfo=UTC)
    assert local.tzinfo == ZoneInfo("Europe/Berlin")

    winter = cal.to_local(datetime(2026, 10, 26, 8, 0, tzinfo=UTC))
    assert winter.utcoffset() == timedelta(hours=1)
    assert winter.replace(tzinfo=None) == datetime(2026, 10, 26, 9, 0)


def test_to_local_rejects_naive_datetime():
    with pytest.raises(ValueError):
        cal.to_local(datetime(2026, 10, 23, 7, 0))


# --- flatten i skrócone sesje ------------------------------------------------------------------------


def test_flatten_time_rejects_negative_or_too_large_offset():
    with pytest.raises(ValueError):
        cal.flatten_time(date(2026, 11, 2), -1)
    with pytest.raises(ValueError):
        cal.flatten_time(date(2026, 11, 2), 8 * 60 + 30)  # równo otwarcie: za duże
    assert cal.flatten_time(date(2026, 11, 2), 0) == time(17, 30)


def _profile_with_early_close() -> ExchangeProfile:
    return ExchangeProfile(
        name="TEST",
        tz="Europe/Berlin",
        open_local=time(9, 0),
        close_local=time(17, 30),
        holidays={2026: frozenset({date(2026, 12, 25)})},
        early_closes={date(2026, 12, 30): time(14, 0)},
    )


def test_flatten_time_honours_early_close():
    c = ExchangeCalendar(_profile_with_early_close())
    assert c.close_time(date(2026, 12, 30)) == time(14, 0)
    assert c.flatten_time(date(2026, 12, 30), 5) == time(13, 55)
    # zwykły dzień tego samego profilu bez zmian
    assert c.close_time(date(2026, 12, 29)) == time(17, 30)
    assert c.flatten_time(date(2026, 12, 29), 5) == time(17, 25)


def test_profile_rejects_invalid_definitions():
    base = dict(
        name="TEST",
        tz="Europe/Berlin",
        open_local=time(9, 0),
        close_local=time(17, 30),
        holidays={2026: frozenset({date(2026, 12, 25)})},
        early_closes={},
    )
    with pytest.raises(ValueError, match="tz"):
        ExchangeProfile(**{**base, "tz": "Europe/Atlantis"})
    with pytest.raises(ValueError, match="open_local"):
        ExchangeProfile(**{**base, "open_local": time(17, 30), "close_local": time(9, 0)})
    with pytest.raises(ValueError, match="holidays"):
        ExchangeProfile(**{**base, "holidays": {2026: frozenset({date(2027, 1, 1)})}})  # data w złym roku
    with pytest.raises(ValueError, match="early_closes"):
        ExchangeProfile(**{**base, "early_closes": {date(2026, 12, 30): time(18, 0)}})  # po zamknięciu
    with pytest.raises(ValueError, match="early_closes"):
        ExchangeProfile(**{**base, "early_closes": {date(2026, 12, 30): time(9, 0)}})  # równo z otwarciem


def test_xetra_profile_definition():
    assert XETRA.name == "XETRA"
    assert XETRA.tz == "Europe/Berlin"
    assert (XETRA.open_local, XETRA.close_local) == (time(9, 0), time(17, 30))
    with pytest.raises(TypeError):
        XETRA.holidays[2026] = frozenset()  # type: ignore[index]  # stała nie może być zmieniona


def test_module_name_does_not_shadow_stdlib_calendar():
    """`import calendar` daje bibliotekę standardową, a `gwstock.calendar` to osobny moduł z tego pakietu."""
    import calendar as stdlib_calendar

    import gwstock.calendar as engine_calendar

    package_dir = Path(engine_calendar.__file__).resolve().parent
    assert hasattr(stdlib_calendar, "monthrange") and not hasattr(stdlib_calendar, "ExchangeCalendar")
    assert Path(stdlib_calendar.__file__).resolve().parent != package_dir  # stdlib nie jest plikiem z src/gwstock
    assert sys.modules["calendar"] is stdlib_calendar
    assert sys.modules["gwstock.calendar"] is engine_calendar
    assert engine_calendar is not stdlib_calendar
    assert engine_calendar.__name__ == "gwstock.calendar"
    assert str(package_dir) not in sys.path  # katalog pakietu nie jest na ścieżce, więc nic nie przesłania stdlib


# --- zegary ------------------------------------------------------------------------------------------


async def test_fake_clock_sleep_advances_time_without_waiting():
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    clock = FakeClock(start)
    assert clock.now_utc() == start
    assert clock.elapsed_since_start() == 0.0

    began = wall_time.monotonic()
    await clock.sleep(30)
    assert wall_time.monotonic() - began < 1.0  # nie czekał naprawdę

    assert clock.now_utc() == start + timedelta(seconds=30)
    assert clock.elapsed_since_start() == 30.0


def test_fake_clock_advance_accumulates_fractions():
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    clock = FakeClock(start)
    clock.advance(1.5)
    clock.advance(60)
    assert clock.now_utc() == start + timedelta(seconds=61.5)
    assert clock.elapsed_since_start() == 61.5


def test_fake_clock_rejects_going_back_in_time():
    clock = FakeClock(datetime(2026, 11, 2, 8, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.advance(-1)


async def test_fake_clock_sleep_of_non_positive_seconds_does_not_move_time():
    """Jak asyncio.sleep: ujemny lub zerowy czas to powrót bez zmiany zegara."""
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    clock = FakeClock(start)
    await clock.sleep(0)
    await clock.sleep(-5)
    assert clock.now_utc() == start


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_fake_clock_advance_rejects_non_finite_seconds(bad):
    """NaN nie jest ani ujemne, ani dodatnie: bez jawnej kontroli przeszłoby po cichu i nie przesunęło zegara."""
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    clock = FakeClock(start)
    with pytest.raises(ValueError, match="seconds"):
        clock.advance(bad)
    assert clock.now_utc() == start


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_fake_clock_sleep_rejects_non_finite_seconds(bad):
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    clock = FakeClock(start)
    with pytest.raises(ValueError, match="seconds"):
        await clock.sleep(bad)
    assert clock.now_utc() == start


def test_fake_clock_rejects_naive_start():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 11, 2, 8, 0))


def test_fake_clock_normalizes_start_to_utc():
    berlin = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    clock = FakeClock(berlin)
    assert clock.now_utc() == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert clock.now_utc().utcoffset() == timedelta(0)


def test_system_clock_now_is_aware_utc():
    now = SystemClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


async def test_system_clock_sleep_really_waits():
    began = wall_time.monotonic()
    await SystemClock().sleep(0.05)
    assert wall_time.monotonic() - began >= 0.04


def _protocol_members() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(Clock).items()
        if callable(member) and not name.startswith("_")
    }


@pytest.mark.parametrize("clock_cls", [SystemClock, FakeClock])
def test_clocks_satisfy_protocol_structurally(clock_cls):
    """Każda metoda protokołu `Clock` istnieje w implementacji z tymi samymi parametrami; `sleep` jest korutyną."""
    members = _protocol_members()
    assert set(members) == {"now_utc", "sleep"}  # nowa metoda protokołu ma zmusić do aktualizacji zegarów
    for name, proto_sig in members.items():
        impl = getattr(clock_cls, name)
        impl_sig = inspect.signature(impl)
        assert list(impl_sig.parameters) == list(proto_sig.parameters), (clock_cls.__name__, name)
        assert inspect.iscoroutinefunction(impl) is inspect.iscoroutinefunction(getattr(Clock, name)), name


async def test_clocks_work_through_the_protocol_type():
    async def uses(clock: Clock) -> datetime:
        before = clock.now_utc()
        await clock.sleep(0)
        after = clock.now_utc()
        assert after >= before
        return after

    for clock in (SystemClock(), FakeClock(datetime(2026, 11, 2, 8, 0, tzinfo=UTC))):
        now = await uses(clock)
        assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
