import pytest

from datamate.fmt import canon_key, format_answer, make_line, round2


def fa(t, cols, rows):
    return format_answer(t, cols, rows)


def test_numbers_money_stays_integer_and_others_get_two_decimals():
    assert fa("number", ["x"], [(89526400,)]) == ("number", 89526400)
    assert fa("number", ["x"], [(89526400.0,)]) == ("number", 89526400)
    assert fa("number", ["x"], [(18.6,)]) == ("number", 18.6)
    assert fa("number", ["x"], [(18.6666666,)]) == ("number", 18.67)
    assert fa("percent", ["x"], [(12.345,)]) == ("percent", 12.35)   # half up
    assert fa("percent", ["x"], [(-3.0,)]) == ("percent", -3)


def test_round2_half_up_not_bankers():
    assert round2(0.125) == 0.13
    assert round2(2.675) == 2.68
    assert round2(5.0) == 5 and isinstance(round2(5.0), int)


def test_text_in_a_number_column_is_an_error_the_agent_can_repair():
    with pytest.raises(ValueError):
        fa("number", ["x"], [("abc",)])
    with pytest.raises(ValueError):
        fa("number", ["x"], [(None,)])
    with pytest.raises(ValueError):
        fa("number", ["x"], [])


def test_date_day_and_month_forms():
    assert fa("date", ["d"], [("2026-08-15",)]) == ("date", "2026-08-15")
    assert fa("date", ["d"], [("2025-09",)]) == ("date", "2025-09")
    assert fa("date", ["d"], [("15/08/2026",)]) == ("date", "2026-08-15")


def test_boolean_string_and_inferred_types():
    assert fa("boolean", ["b"], [(1,)]) == ("boolean", True)
    assert fa("boolean", ["b"], [(0,)]) == ("boolean", False)
    assert fa("string", ["s"], [("  Sting dâu 330ml ",)]) == ("string", "Sting dâu 330ml")
    assert fa(None, ["s"], [("Cà phê",)]) == ("string", "Cà phê")
    assert fa("bogus", ["n"], [(5,)]) == ("number", 5)


def test_lists_keep_order_and_one_item_is_still_a_list():
    assert fa("list", ["ten"], [("Cà phê sữa",)]) == ("list", ["Cà phê sữa"])
    assert fa("list", ["ten"], [("B",), ("A",)]) == ("list", ["B", "A"])
    assert fa("list", ["thung", "chai"], [(3, 10)]) == ("list", [3, 10])   # stock as [big units, loose units]


def test_table_shape():
    t, a = fa("table", ["khach_hang", "so_tien_no"], [("Chị Lan", 3200000), ("Anh Bình", 150000.0)])
    assert t == "table"
    assert a == {"columns": ["khach_hang", "so_tien_no"], "rows": [["Chị Lan", 3200000], ["Anh Bình", 150000]]}


def test_submission_line_shapes():
    assert make_line("q1", "number", 5) == {"id": "q1", "type": "number", "answer": 5}
    assert make_line("q2", "abstain") == {"id": "q2", "type": "abstain"}
    assert make_line("q3", "unanswerable", reason="no cost data") == {"id": "q3", "type": "unanswerable", "reason": "no cost data"}
    assert make_line("q4", "unanswerable", reason="  ") == {"id": "q4", "type": "unanswerable"}


def test_vote_keys():
    assert canon_key("number", 5) == canon_key("number", 5.0)
    assert canon_key("string", "Sting dâu") == canon_key("string", "sting dau")
    assert canon_key("list", ["a", "b"]) != canon_key("list", ["b", "a"])          # order matters for lists
    t1 = {"columns": ["a", "b"], "rows": [["x", 1], ["y", 2]]}
    t2 = {"columns": ["a", "b"], "rows": [["y", 2], ["x", 1]]}
    assert canon_key("table", t1) == canon_key("table", t2)                         # row order does not
