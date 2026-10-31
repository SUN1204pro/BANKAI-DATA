from datetime import date, timedelta

import pytest

from datamate.lunar import CalendarFormatError, LunarCalendar, recurring_lunar_days, resolve_lunar


def fake_calendar(with_holiday_column: bool = True) -> LunarCalendar:
    """Synthetic 2024-2025 calendar shaped like train/calendar.csv.

    Lunar 20/11/2023 = 2024-01-01, month lengths alternate 30/29 (odd months 30), except
    lunar 11/2023 = 29 and 12/2023 = 30. This reproduces the real Tết dates:
    1/1/2024 = 2024-02-10 and 1/1/2025 = 2025-01-29. Not astronomically correct elsewhere.
    """
    holidays = {
        date(2024, 1, 1): "Tết Dương lịch",
        date(2024, 4, 18): "Giỗ Tổ Hùng Vương",
        date(2024, 9, 2): "Quốc khánh",
        date(2024, 9, 3): "Quốc khánh",
        date(2025, 9, 2): "Quốc khánh",
    }
    rows, d = [], date(2024, 1, 1)
    lday, lmonth, lyear = 20, 11, 2023
    while d <= date(2025, 12, 31):
        length = {(2023, 11): 29, (2023, 12): 30}.get((lyear, lmonth), 30 if lmonth % 2 else 29)
        row = {"date": d.isoformat(), "weekday": d.isoweekday(), "lunar_day": lday, "lunar_month": lmonth,
               "lunar_year": lyear, "lunar_leap": "false"}
        if with_holiday_column:
            row["holiday"] = holidays.get(d, "")
        rows.append(row)
        d += timedelta(days=1)
        lday += 1
        if lday > length:
            lday, lmonth = 1, lmonth + 1
            if lmonth > 12:
                lmonth, lyear = 1, lyear + 1
    return LunarCalendar.from_rows(rows)


def test_lookup_both_directions():
    cal = fake_calendar()
    assert cal.lunar_to_solar(1, 1, 2024) == date(2024, 2, 10)
    assert cal.lunar_to_solar(1, 1, 2025) == date(2025, 1, 29)
    ld = cal.solar_to_lunar(date(2024, 1, 1))
    assert (ld.day, ld.month, ld.year) == (20, 11, 2023)


def test_leap_month_does_not_overwrite_the_normal_month():
    rows = [
        {"date": "2025-07-25", "lunar_day": 1, "lunar_month": 6, "lunar_year": 2025, "lunar_leap": "false", "holiday": ""},
        {"date": "2025-08-23", "lunar_day": 1, "lunar_month": 6, "lunar_year": 2025, "lunar_leap": "true", "holiday": ""},
    ]
    cal = LunarCalendar.from_rows(rows)
    assert cal.lunar_to_solar(6, 1, 2025) == date(2025, 7, 25)
    assert cal.lunar_to_solar(6, 1, 2025, leap=True) == date(2025, 8, 23)


def test_tet_window_follows_the_rulebook():
    cal = fake_calendar()
    # 23 tháng Chạp (lunar 2023) .. mùng 10 tháng Giêng (lunar 2024)
    assert cal.tet_window(2024) == (date(2024, 2, 2), date(2024, 2, 19))
    assert cal.tet_window(2025) == (date(2025, 1, 22), date(2025, 2, 7))


def test_tet_uses_the_latest_season_that_has_started():
    cal = fake_calendar()
    (s,) = resolve_lunar("doanh thu dịp Tết", cal, date(2024, 3, 4))
    assert (s.start, s.end) == (date(2024, 2, 2), date(2024, 2, 19))
    (s,) = resolve_lunar("mùa Tết bán được gì", cal, date(2025, 6, 1))
    assert (s.start, s.end) == (date(2025, 1, 22), date(2025, 2, 7))
    # as-of before this year's season started and no earlier year in the calendar -> nothing, no crash
    assert resolve_lunar("dịp Tết", cal, date(2024, 1, 15)) == []


def test_tet_with_explicit_year_words():
    cal = fake_calendar()
    (s,) = resolve_lunar("Tết năm 2024 bán được bao nhiêu", cal, date(2025, 6, 1))
    assert s.start == date(2024, 2, 2)
    (s,) = resolve_lunar("tết năm ngoái", cal, date(2025, 6, 1))
    assert s.start == date(2024, 2, 2)
    (s,) = resolve_lunar("tết năm nay", cal, date(2025, 6, 1))
    assert s.start == date(2025, 1, 22)


def test_before_tet_is_an_equal_length_window_right_before_23_chap():
    cal = fake_calendar()
    main, before = resolve_lunar("doanh thu Tết 2024 so với trước Tết", cal, date(2024, 3, 4))
    assert (main.start, main.end) == (date(2024, 2, 2), date(2024, 2, 19))      # 18 days
    assert (before.start, before.end) == (date(2024, 1, 15), date(2024, 2, 1))  # 18 days before


def test_mung_n_tet_is_one_day():
    cal = fake_calendar()
    (s,) = resolve_lunar("mùng 1 Tết bán được bao nhiêu", cal, date(2024, 3, 1))
    assert s.start == s.end == date(2024, 2, 10)


def test_solar_new_year_is_not_the_lunar_tet():
    cal = fake_calendar()
    (s,) = resolve_lunar("tết dương lịch bán gì", cal, date(2024, 3, 4))
    assert s.start == s.end == date(2024, 1, 1)


def test_holidays_come_from_the_calendar_holiday_column():
    cal = fake_calendar()
    (s,) = resolve_lunar("Quốc khánh bán được bao nhiêu", cal, date(2024, 10, 15))
    assert (s.start, s.end) == (date(2024, 9, 2), date(2024, 9, 3))
    (s,) = resolve_lunar("Quốc khánh năm ngoái", cal, date(2025, 3, 1))
    assert (s.start, s.end) == (date(2024, 9, 2), date(2024, 9, 3))


def test_fallback_festivals_work_without_a_holiday_column():
    cal = fake_calendar(with_holiday_column=False)
    (s,) = resolve_lunar("tối Trung thu bán được gì", cal, date(2024, 10, 15))
    assert s.start == s.end == date(2024, 9, 18)  # lunar 15/8/2024 in the fake calendar


def test_recurring_days_and_occurrences():
    assert recurring_lunar_days("ngày rằm bán được bao nhiêu") == [15]
    assert recurring_lunar_days("mùng 1 hàng tháng") == [1]
    assert recurring_lunar_days("tết năm nay") == []
    cal = fake_calendar()
    assert cal.occurrences(15, date(2024, 3, 1), date(2024, 5, 1)) == [date(2024, 3, 25), date(2024, 4, 23)]


def test_loads_the_real_csv_layout(tmp_path):
    p = tmp_path / "calendar.csv"
    p.write_text(
        "date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n"
        "2024-01-01,1,20,11,2023,false,Tết Dương lịch\n"
        "2024-01-02,2,21,11,2023,false,\n",
        encoding="utf-8",
    )
    cal = LunarCalendar.load(p)
    assert cal.solar_to_lunar(date(2024, 1, 1)).holiday == "Tết Dương lịch"
    assert cal.solar_to_lunar(date(2024, 1, 2)).holiday == ""


def test_bad_calendar_raises_clear_error():
    with pytest.raises(CalendarFormatError):
        LunarCalendar.from_rows([{"foo": "bar"}])
