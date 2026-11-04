"""Testy profilu giełdy NYSE w `gwstock.calendar` (święta, skrócone sesje, czas letni i zimowy w USA).

Uruchamiać z korzenia repozytorium. Daty świąt są sprawdzane dwoma niezależnymi sposobami: wpisane jawnie w testach
(z nazwą święta) oraz wyliczone tu od nowa z reguł (n-ty dzień tygodnia miesiąca, Wielkanoc, przeniesienia).

Zakres: wyłącznie profil NYSE; zachowanie XETRA pilnują `test_clock_calendar.py` i jedna kontrola na końcu.
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from gwstock.calendar import NYSE, XETRA, CalendarExpiredError, ExchangeCalendar

cal = ExchangeCalendar(NYSE)

UTC = timezone.utc

# --- niezależne wyliczenie z reguł --------------------------------------------------------------------


def _easter_sunday(year: int) -> date:
    """Wielkanoc (algorytm Meeusa/Jonesa/Butchera, kalendarz gregoriański)."""
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


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ty (od 1) dzień tygodnia `weekday` (0 = poniedziałek) w miesiącu."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Przeniesienie święta na dzień roboczy: sobota na piątek, niedziela na poniedziałek."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _rule_based_holidays(year: int) -> frozenset[date]:
    new_year = date(year, 1, 1)
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day: trzeci poniedziałek stycznia
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day: trzeci poniedziałek lutego
        _easter_sunday(year) - timedelta(days=2),  # Wielki Piątek
        _last_weekday(year, 5, 0),  # Memorial Day: ostatni poniedziałek maja
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Dzień Niepodległości
        _nth_weekday(year, 9, 0, 1),  # Labor Day: pierwszy poniedziałek września
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving: czwarty czwartek listopada
        _observed(date(year, 12, 25)),  # Boże Narodzenie
    }
    if new_year.weekday() != 5:  # NYSE: Nowy Rok w sobotę NIE jest przenoszony na piątek 31 grudnia
        days.add(_observed(new_year))
    return frozenset(days)


def _rule_based_early_closes(year: int) -> frozenset[date]:
    holidays = _rule_based_holidays(year)

    def is_session_day(d: date) -> bool:
        return d.weekday() < 5 and d not in holidays

    days = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # dzień po Thanksgiving
    christmas_eve = date(year, 12, 24)
    if is_session_day(christmas_eve):  # w 2027 Wigilia jest przeniesionym świętem, więc bez skróconej sesji
        days.add(christmas_eve)
    july_3 = date(year, 7, 3)
    if is_session_day(july_3) and date(year, 7, 4).weekday() < 5:  # 4 lipca w dzień roboczy po 3 lipca
        days.add(july_3)
    return frozenset(days)


# --- profil ------------------------------------------------------------------------------------------


def test_nyse_profile_definition():
    assert NYSE.name == "NYSE"
    assert NYSE.tz == "America/New_York"
    assert (NYSE.open_local, NYSE.close_local) == (time(9, 30), time(16, 0))
    assert set(NYSE.holidays) == {2026, 2027}
    with pytest.raises(TypeError):
        NYSE.holidays[2026] = frozenset()  # type: ignore[index]  # stała nie może być zmieniona
    with pytest.raises(TypeError):
        NYSE.early_closes[date(2026, 11, 27)] = time(12, 0)  # type: ignore[index]


def test_nyse_and_xetra_are_independent_profiles():
    """Dodanie NYSE nie zmienia XETRA (zachowanie XETRA pilnuje `test_clock_calendar.py`)."""
    assert XETRA.name == "XETRA" and XETRA.tz == "Europe/Berlin"
    assert (XETRA.open_local, XETRA.close_local) == (time(9, 0), time(17, 30))
    assert set(XETRA.holidays) == {2026, 2027} and dict(XETRA.early_closes) == {}
    assert NYSE.holidays is not XETRA.holidays and NYSE.early_closes is not XETRA.early_closes
    assert date(2026, 11, 26) in NYSE.holidays[2026] and date(2026, 11, 26) not in XETRA.holidays[2026]


# --- dzień zwykły i weekend --------------------------------------------------------------------------


def test_ordinary_day_open_close_and_flatten():
    d = date(2026, 11, 3)  # wtorek
    assert cal.is_session_day(d)
    assert (cal.open_time(d), cal.close_time(d)) == (time(9, 30), time(16, 0))
    assert cal.flatten_time(d, 5) == time(15, 55)
    assert cal.flatten_time(d, 0) == time(16, 0)


@pytest.mark.parametrize("d", [date(2026, 11, 7), date(2026, 11, 8), date(2027, 3, 6), date(2027, 3, 7)])
def test_weekends_are_not_session_days(d):
    assert d.weekday() >= 5
    assert not cal.is_session_day(d)


def test_weekdays_around_a_weekend_are_session_days():
    assert cal.is_session_day(date(2026, 11, 6))  # piątek
    assert cal.is_session_day(date(2026, 11, 9))  # poniedziałek


def test_open_close_flatten_on_non_session_day_raise():
    for d in (date(2026, 11, 7), date(2026, 11, 26), date(2026, 12, 25)):
        with pytest.raises(ValueError):
            cal.open_time(d)
        with pytest.raises(ValueError):
            cal.close_time(d)
        with pytest.raises(ValueError):
            cal.flatten_time(d, 5)


# --- święta 2026 (każde z osobna, z nazwą i regułą) -----------------------------------------------------

HOLIDAYS_2026 = [
    (date(2026, 1, 1), "Nowy Rok (1 stycznia, czwartek)"),
    (date(2026, 1, 19), "Martin Luther King Jr. Day (trzeci poniedziałek stycznia)"),
    (date(2026, 2, 16), "Presidents' Day (trzeci poniedziałek lutego)"),
    (date(2026, 4, 3), "Wielki Piątek (Wielkanoc 5.04.2026)"),
    (date(2026, 5, 25), "Memorial Day (ostatni poniedziałek maja)"),
    (date(2026, 6, 19), "Juneteenth (19 czerwca, piątek)"),
    (date(2026, 7, 3), "Dzień Niepodległości, 4 lipca w sobotę przeniesiony na piątek"),
    (date(2026, 9, 7), "Labor Day (pierwszy poniedziałek września)"),
    (date(2026, 11, 26), "Thanksgiving (czwarty czwartek listopada)"),
    (date(2026, 12, 25), "Boże Narodzenie (25 grudnia, piątek)"),
]


@pytest.mark.parametrize(("d", "name"), HOLIDAYS_2026, ids=[n for _, n in HOLIDAYS_2026])
def test_each_2026_holiday_is_not_a_session_day(d, name):
    assert d.weekday() < 5, name  # święto wpisane w tabelę musi być dniem roboczym
    assert not cal.is_session_day(d), name
    assert d in cal.holidays(2026)
    with pytest.raises(ValueError):
        cal.open_time(d)


def test_2026_table_is_exactly_the_ten_listed_holidays():
    assert cal.holidays(2026) == frozenset(d for d, _ in HOLIDAYS_2026)
    assert len(HOLIDAYS_2026) == 10


def test_2026_table_matches_rule_based_computation():
    assert cal.holidays(2026) == _rule_based_holidays(2026)


def test_days_next_to_2026_holidays_are_session_days():
    for d in (
        date(2026, 1, 2),
        date(2026, 1, 20),
        date(2026, 2, 17),
        date(2026, 4, 2),
        date(2026, 4, 6),  # Poniedziałek Wielkanocny: NYSE pracuje
        date(2026, 5, 26),
        date(2026, 6, 18),
        date(2026, 7, 2),
        date(2026, 7, 6),
        date(2026, 9, 8),
        date(2026, 11, 25),
        date(2026, 12, 28),
    ):
        assert cal.is_session_day(d), d


def test_us_federal_days_without_nyse_closure_are_session_days():
    """Columbus Day i Veterans Day to święta federalne, ale NYSE wtedy handluje (rynek obligacji nie)."""
    assert cal.is_session_day(date(2026, 10, 12))  # drugi poniedziałek października
    assert cal.is_session_day(date(2026, 11, 11))  # środa
    assert cal.is_session_day(date(2027, 10, 11))
    assert cal.is_session_day(date(2027, 11, 11))


def test_new_years_eve_2026_is_a_regular_session_day():
    """W odróżnieniu od XETRA, NYSE handluje 31 grudnia (2026: czwartek), i to w pełnym wymiarze."""
    d = date(2026, 12, 31)
    assert cal.is_session_day(d)
    assert cal.close_time(d) == time(16, 0)


# --- święta 2027 -------------------------------------------------------------------------------------

HOLIDAYS_2027 = [
    (date(2027, 1, 1), "Nowy Rok (1 stycznia, piątek)"),
    (date(2027, 1, 18), "Martin Luther King Jr. Day (trzeci poniedziałek stycznia)"),
    (date(2027, 2, 15), "Presidents' Day (trzeci poniedziałek lutego)"),
    (date(2027, 3, 26), "Wielki Piątek (Wielkanoc 28.03.2027)"),
    (date(2027, 5, 31), "Memorial Day (ostatni poniedziałek maja)"),
    (date(2027, 6, 18), "Juneteenth, 19 czerwca w sobotę przeniesiony na piątek"),
    (date(2027, 7, 5), "Dzień Niepodległości, 4 lipca w niedzielę przeniesiony na poniedziałek"),
    (date(2027, 9, 6), "Labor Day (pierwszy poniedziałek września)"),
    (date(2027, 11, 25), "Thanksgiving (czwarty czwartek listopada)"),
    (date(2027, 12, 24), "Boże Narodzenie, 25 grudnia w sobotę przeniesione na piątek"),
]


@pytest.mark.parametrize(("d", "name"), HOLIDAYS_2027, ids=[n for _, n in HOLIDAYS_2027])
def test_each_2027_holiday_is_not_a_session_day(d, name):
    assert d.weekday() < 5, name
    assert not cal.is_session_day(d), name
    assert d in cal.holidays(2027)


def test_2027_table_is_exactly_the_ten_listed_holidays_and_matches_rules():
    assert cal.holidays(2027) == frozenset(d for d, _ in HOLIDAYS_2027)
    assert cal.holidays(2027) == _rule_based_holidays(2027)


def test_new_year_2028_on_saturday_does_not_close_friday_2027_12_31():
    """NYSE nie przenosi Nowego Roku z soboty na piątek: 31.12.2027 to zwykły dzień sesji (patrz kod)."""
    assert date(2028, 1, 1).weekday() == 5
    assert cal.is_session_day(date(2027, 12, 31))
    assert cal.close_time(date(2027, 12, 31)) == time(16, 0)


def test_days_next_to_2027_holidays_are_session_days():
    for d in (date(2027, 6, 17), date(2027, 6, 21), date(2027, 7, 2), date(2027, 7, 6), date(2027, 12, 23)):
        assert cal.is_session_day(d), d


def test_tables_have_only_weekday_holidays_with_matching_years():
    for year in (2026, 2027):
        hol = cal.holidays(year)
        assert len(hol) == 10
        assert all(d.year == year and d.weekday() < 5 for d in hol)


# --- skrócone sesje (zamknięcie 13:00 czasu nowojorskiego) -----------------------------------------------


@pytest.mark.parametrize(
    ("d", "name"),
    [
        (date(2026, 11, 27), "dzień po Thanksgiving 2026"),
        (date(2026, 12, 24), "Wigilia 2026 (czwartek, dzień sesji)"),
        (date(2027, 11, 26), "dzień po Thanksgiving 2027"),
    ],
)
def test_early_close_sessions_close_at_1300(d, name):
    assert cal.is_session_day(d), name
    assert cal.open_time(d) == time(9, 30)
    assert cal.close_time(d) == time(13, 0), name
    assert cal.flatten_time(d, 5) == time(12, 55)
    assert cal.flatten_time(d, 0) == time(13, 0)


def test_early_close_table_is_exactly_three_days_and_matches_rules():
    assert dict(NYSE.early_closes) == {
        date(2026, 11, 27): time(13, 0),
        date(2026, 12, 24): time(13, 0),
        date(2027, 11, 26): time(13, 0),
    }
    assert frozenset(NYSE.early_closes) == _rule_based_early_closes(2026) | _rule_based_early_closes(2027)


def test_early_closes_fall_on_session_days_and_before_the_regular_close():
    for d, close in NYSE.early_closes.items():
        assert cal.is_session_day(d), d
        assert d not in cal.holidays(d.year)
        assert NYSE.open_local < close < NYSE.close_local


def test_no_early_close_on_july_3_or_christmas_eve_when_those_are_not_early_session_days():
    # 3.07.2026 to święto (przeniesione 4 lipca), 3.07.2027 to sobota: reguła 3 lipca nie daje skróconej sesji
    assert date(2026, 7, 3) not in NYSE.early_closes and date(2027, 7, 3) not in NYSE.early_closes
    # 24.12.2027 to święto (przeniesione 25 grudnia), więc nie ma ani sesji, ani skróconej sesji
    assert not cal.is_session_day(date(2027, 12, 24)) and date(2027, 12, 24) not in NYSE.early_closes
    # dzień przed 3.07.2026 i 5.07.2027 ma pełną sesję (DO POTWIERDZENIA w kodzie)
    assert cal.close_time(date(2026, 7, 2)) == time(16, 0)
    assert cal.close_time(date(2027, 7, 2)) == time(16, 0)


def test_regular_days_before_and_after_early_closes_have_full_session():
    assert cal.close_time(date(2026, 11, 25)) == time(16, 0)  # środa przed Thanksgiving
    assert cal.close_time(date(2026, 12, 23)) == time(16, 0)
    assert cal.close_time(date(2026, 12, 28)) == time(16, 0)
    assert cal.close_time(date(2027, 11, 29)) == time(16, 0)


# --- strefa czasowa i zmiana czasu --------------------------------------------------------------------


def test_us_fall_back_on_2026_11_01_open_in_utc():
    """Czas zimowy w USA od 1.11.2026 (niedziela): otwarcie 09:30 ET to 13:30 UTC przed zmianą, 14:30 UTC po."""
    assert date(2026, 11, 1).weekday() == 6
    before = cal.local_to_utc(date(2026, 10, 30), cal.open_time(date(2026, 10, 30)))  # piątek, EDT
    after = cal.local_to_utc(date(2026, 11, 2), cal.open_time(date(2026, 11, 2)))  # poniedziałek, EST
    assert before == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert after == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    assert before.utcoffset() == timedelta(0) and after.utcoffset() == timedelta(0)


def test_us_fall_back_2026_close_and_flatten_in_utc():
    assert cal.local_to_utc(date(2026, 10, 30), cal.close_time(date(2026, 10, 30))) == datetime(
        2026, 10, 30, 20, 0, tzinfo=UTC
    )
    assert cal.local_to_utc(date(2026, 11, 2), cal.close_time(date(2026, 11, 2))) == datetime(
        2026, 11, 2, 21, 0, tzinfo=UTC
    )
    assert cal.local_to_utc(date(2026, 11, 2), cal.flatten_time(date(2026, 11, 2), 5)) == datetime(
        2026, 11, 2, 20, 55, tzinfo=UTC
    )
    # skrócona sesja po zmianie czasu: 13:00 EST to 18:00 UTC
    assert cal.local_to_utc(date(2026, 11, 27), cal.close_time(date(2026, 11, 27))) == datetime(
        2026, 11, 27, 18, 0, tzinfo=UTC
    )


def test_us_spring_forward_2026_and_2027_open_in_utc():
    # 8.03.2026 i 14.03.2027 (drugie niedziele marca): z 14:30 UTC (EST) na 13:30 UTC (EDT)
    assert cal.local_to_utc(date(2026, 3, 6), time(9, 30)) == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert cal.local_to_utc(date(2026, 3, 9), time(9, 30)) == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert cal.local_to_utc(date(2027, 3, 12), time(9, 30)) == datetime(2027, 3, 12, 14, 30, tzinfo=UTC)
    assert cal.local_to_utc(date(2027, 3, 15), time(9, 30)) == datetime(2027, 3, 15, 13, 30, tzinfo=UTC)


def test_us_and_eu_clock_changes_differ_so_offset_to_berlin_varies():
    """26-30.10.2026: Berlin już w CET (od 25.10), Nowy Jork jeszcze w EDT (do 1.11): różnica 5 h, nie 6 h."""
    berlin = ExchangeCalendar(XETRA)
    ny_open = cal.local_to_utc(date(2026, 10, 28), time(9, 30))
    assert ny_open == datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    assert berlin.to_local(ny_open).time() == time(14, 30)  # CET
    ny_open_later = cal.local_to_utc(date(2026, 11, 3), time(9, 30))
    assert berlin.to_local(ny_open_later).time() == time(15, 30)  # po zmianie w USA znów 6 h różnicy


def test_to_local_returns_new_york_time():
    local = cal.to_local(datetime(2026, 10, 30, 13, 30, tzinfo=UTC))
    assert local.utcoffset() == timedelta(hours=-4)
    assert local.replace(tzinfo=None) == datetime(2026, 10, 30, 9, 30)
    winter = cal.to_local(datetime(2026, 11, 2, 14, 30, tzinfo=UTC))
    assert winter.utcoffset() == timedelta(hours=-5)
    assert winter.replace(tzinfo=None) == datetime(2026, 11, 2, 9, 30)


def test_local_to_utc_rejects_nonexistent_and_ambiguous_new_york_times():
    with pytest.raises(ValueError):
        cal.local_to_utc(date(2026, 3, 8), time(2, 30))  # luka przy przejściu na czas letni w USA
    with pytest.raises(ValueError):
        cal.local_to_utc(date(2026, 11, 1), time(1, 30))  # powtórzona godzina przy powrocie na czas zimowy


# --- flatten_time ------------------------------------------------------------------------------------


def test_flatten_time_is_five_minutes_before_close():
    assert cal.flatten_time(date(2026, 11, 3), 5) == time(15, 55)
    assert cal.flatten_time(date(2026, 11, 27), 5) == time(12, 55)  # skrócona sesja
    assert cal.flatten_time(date(2027, 11, 26), 5) == time(12, 55)


def test_flatten_time_rejects_negative_or_too_large_offset():
    with pytest.raises(ValueError):
        cal.flatten_time(date(2026, 11, 3), -1)
    with pytest.raises(ValueError):
        cal.flatten_time(date(2026, 11, 3), 6 * 60 + 30)  # równo otwarcie 09:30: za duże
    with pytest.raises(ValueError):
        cal.flatten_time(date(2026, 11, 27), 3 * 60 + 30)  # skrócona sesja 9:30-13:00: równo otwarcie
    assert cal.flatten_time(date(2026, 11, 3), 6 * 60 + 29) == time(9, 31)


# --- wygaśnięcie kalendarza ---------------------------------------------------------------------------


def test_expired_calendar_raises_calendar_expired_error():
    """Rok poza tabelą = wygasły kalendarz = brak handlu; nigdy po cichu „wszystkie dni sesyjne”."""
    with pytest.raises(CalendarExpiredError):
        cal.is_session_day(date(2028, 3, 1))  # środa
    with pytest.raises(CalendarExpiredError):
        cal.is_session_day(date(2028, 1, 1))  # sobota: wynik nie zależy od dnia tygodnia
    with pytest.raises(CalendarExpiredError):
        cal.is_session_day(date(2025, 12, 31))  # rok przed tabelą
    with pytest.raises(CalendarExpiredError):
        cal.holidays(2028)
    with pytest.raises(CalendarExpiredError):
        cal.holidays(2025)
    with pytest.raises(ValueError):  # CalendarExpiredError jest podklasą ValueError
        cal.open_time(date(2028, 3, 1))


def test_last_session_day_of_the_last_table_year_still_works():
    assert cal.is_session_day(date(2027, 12, 30))
    assert cal.is_session_day(date(2027, 12, 31))
    with pytest.raises(CalendarExpiredError):
        cal.is_session_day(date(2028, 1, 3))  # pierwszy poniedziałek po tabeli
