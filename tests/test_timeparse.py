from datetime import date

import pytest

from datamate.timeparse import Conventions, describe, resolve

AS_OF = date(2026, 3, 4)  # Wednesday


def one(q, as_of=AS_OF, conv=None):
    spans = resolve(q, as_of, conv)
    assert len(spans) == 1, spans
    return spans[0]


def pair(q, as_of=AS_OF):
    return [(s.start, s.end) for s in resolve(q, as_of)]


# ---- rulebook section 2 -------------------------------------------------------------
def test_last_week_is_the_whole_previous_monday_to_sunday():
    s = one("sp bán dc nhiều nhất tuần rồi là gì")
    assert (s.start, s.end, s.kind) == (date(2026, 2, 23), date(2026, 3, 1), "week")


def test_this_week_runs_from_monday_to_today():
    s = one("tuần này bán được bao nhiêu")
    assert (s.start, s.end) == (date(2026, 3, 2), date(2026, 3, 4))


def test_this_month_runs_from_day_1_to_today():
    s = one("thang nay doanh thu bao nhieu")
    assert (s.start, s.end) == (date(2026, 3, 1), date(2026, 3, 4))


def test_last_month_alone_is_the_whole_month():
    s = one("tháng trước bán được bao nhiêu")
    assert (s.start, s.end) == (date(2026, 2, 1), date(2026, 2, 28))
    s = one("tháng trước bán được bao nhiêu", as_of=date(2026, 1, 15))
    assert (s.start, s.end) == (date(2025, 12, 1), date(2025, 12, 31))


def test_this_month_vs_last_month_uses_the_same_number_of_days():
    assert pair("doanh thu tháng này so với tháng trước") == [
        (date(2026, 3, 1), date(2026, 3, 4)),
        (date(2026, 2, 1), date(2026, 2, 4)),
    ]
    # last month shorter than today's day number -> whole last month
    assert pair("tháng này so với tháng trước", as_of=date(2026, 3, 30)) == [
        (date(2026, 3, 1), date(2026, 3, 30)),
        (date(2026, 2, 1), date(2026, 2, 28)),
    ]


def test_year_to_date_and_this_year():
    assert one("từ đầu năm đến nay doanh thu").end == AS_OF
    s = one("từ đầu năm bán được gì")
    assert (s.start, s.end) == (date(2026, 1, 1), AS_OF)
    s = one("cả năm nay")
    assert (s.start, s.end) == (date(2026, 1, 1), AS_OF)


def test_month_to_date_phrase():
    s = one("từ đầu tháng tới nay")
    assert (s.start, s.end) == (date(2026, 3, 1), AS_OF)


def test_last_n_days_include_today():
    s = one("7 ngày gần nhất bán được gì")
    assert (s.start, s.end) == (date(2026, 2, 26), date(2026, 3, 4))
    s = one("7 ngày qua bán được gì", conv=Conventions(rolling_includes_today=False))
    assert (s.start, s.end) == (date(2026, 2, 25), date(2026, 3, 3))


def test_rolling_months_does_not_also_match_last_month():
    assert one("3 tháng qua doanh thu").kind == "range"


def test_month_without_year_goes_to_last_year_when_it_starts_after_today():
    assert pair("doanh thu tháng 12") == [(date(2025, 12, 1), date(2025, 12, 31))]
    assert pair("doanh thu tháng 2") == [(date(2026, 2, 1), date(2026, 2, 28))]
    assert pair("doanh thu tháng 3") == [(date(2026, 3, 1), date(2026, 3, 31))]  # starts on/before today
    assert pair("doanh thu tháng 3 năm ngoái") == [(date(2025, 3, 1), date(2025, 3, 31))]
    assert pair("doanh thu tháng 5/2025".replace("5/2025", "5 năm 2025")) == [(date(2025, 5, 1), date(2025, 5, 31))]


def test_quarter_without_year_follows_the_same_rule():
    assert pair("quý 1") == [(date(2026, 1, 1), date(2026, 3, 31))]
    assert pair("quý 4") == [(date(2025, 10, 1), date(2025, 12, 31))]
    assert pair("quý 1 năm ngoái") == [(date(2025, 1, 1), date(2025, 3, 31))]
    assert pair("quý trước", as_of=date(2026, 2, 1)) == [(date(2025, 10, 1), date(2025, 12, 31))]


def test_explicit_days_and_ranges():
    s = one("ngày 15/02/2026 bán gì")
    assert s.start == s.end == date(2026, 2, 15)
    assert pair("ngày 15/12 bán gì") == [(date(2025, 12, 15), date(2025, 12, 15))]
    assert pair("hôm qua có bao nhiêu đơn") == [(date(2026, 3, 3), date(2026, 3, 3))]
    assert pair("từ 01/03 đến 15/03", as_of=date(2026, 3, 20)) == [(date(2026, 3, 1), date(2026, 3, 15))]
    assert pair("từ 20/12 đến 05/01") == [(date(2025, 12, 20), date(2026, 1, 5))]
    assert pair("từ ngày 01/02/2026 đến hết ngày 15/02/2026") == [(date(2026, 2, 1), date(2026, 2, 15))]


# ---- same period last year ------------------------------------------------------------
def test_same_period_last_year_keeps_both_spans():
    assert pair("doanh thu tháng này so với cùng kỳ năm ngoái") == [
        (date(2026, 3, 1), date(2026, 3, 4)),
        (date(2025, 3, 1), date(2025, 3, 4)),
    ]
    assert pair("doanh thu tháng 3 so với cùng kỳ năm ngoái") == [
        (date(2026, 3, 1), date(2026, 3, 31)),
        (date(2025, 3, 1), date(2025, 3, 31)),
    ]


def test_same_period_last_year_leap_day():
    spans = resolve("hôm nay cùng kỳ năm ngoái", date(2024, 2, 29))
    assert spans[1].start == date(2023, 2, 28)


# ---- misc ---------------------------------------------------------------------------
def test_phrase_keeps_the_users_wording():
    assert one("sp bán dc nhiều nhất tuần rồi là gì").phrase == "tuần rồi"
    assert resolve("tháng này so với cùng kỳ năm ngoái", AS_OF)[1].phrase == "cùng kỳ năm ngoái"


def test_week_start_convention():
    s = one("tuần rồi", conv=Conventions(week_start=6))  # Sunday-based weeks
    assert (s.start, s.end) == (date(2026, 2, 22), date(2026, 2, 28))


def test_no_time_expression():
    assert resolve("có bao nhiêu khách hàng", AS_OF) == []


def test_describe_lists_every_span():
    lines = describe(resolve("tuần rồi", AS_OF), AS_OF)
    assert lines == ['"tuần rồi" = 2026-02-23 to 2026-03-01 inclusive [week] (the whole previous week)']


@pytest.mark.parametrize("q", ["tháng 13", "ngày 31/02", "quý 5"])
def test_invalid_dates_are_ignored(q):
    assert resolve(q, AS_OF) == []
