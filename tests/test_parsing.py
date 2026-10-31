import pytest

from datamate.parsing import parse_date, parse_number


@pytest.mark.parametrize("text,expected", [
    ("250k", 250_000),
    ("1.5tr", 1_500_000),
    ("1,5tr", 1_500_000),
    ("2tr5", 2_500_000),
    ("1tr200", 1_200_000),
    ("1.200.000đ", 1_200_000),
    ("1,200,000", 1_200_000),
    ("1.200", 1_200),
    ("1,200", 1_200),
    ("25.000", 25_000),
    ("12.5", 12.5),
    ("1,5", 1.5),
    ("0.500", 0.5),
    ("(1.200)", -1_200),
    ("-35", -35),
    ("350 nghìn", 350_000),
    ("3 triệu", 3_000_000),
    (42, 42),
])
def test_parse_number(text, expected):
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["15%", "abc", "", "DH0001", "04/03/2026", "N/A", None])
def test_parse_number_rejects(text):
    assert parse_number(text) is None


@pytest.mark.parametrize("text,expected", [
    ("04/03/2026", (2026, 3, 4)),          # Vietnamese day-first
    ("2026-03-04", (2026, 3, 4)),
    ("2026-03-04 10:15:00", (2026, 3, 4)),
    ("4-3-2026", (2026, 3, 4)),
    ("04.03.26", (2026, 3, 4)),
    ("03/25/2026", (2026, 3, 25)),         # clearly month-first
])
def test_parse_date(text, expected):
    d = parse_date(text)
    assert (d.year, d.month, d.day) == expected


@pytest.mark.parametrize("text", ["31/02/2026", "hello", "", None, "99/99/2026"])
def test_parse_date_rejects(text):
    assert parse_date(text) is None
