import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from evaluate import match, score  # noqa: E402


def g(t, a=None, **kw):
    return {"id": "q", "type": t, "answer": a, **kw}


def p(t, a=None, **kw):
    return {"id": "q", "type": t, "answer": a, **kw}


def test_scoring_is_plus_one_minus_one_zero():
    assert score(p("number", 5), g("number", 5)) == 1
    assert score(p("number", 6), g("number", 5)) == -1
    assert score({"id": "q", "type": "abstain"}, g("number", 5)) == 0
    assert score(None, g("number", 5)) == 0                                       # missing line = abstain


def test_number_and_percent_are_exact_to_the_stored_value():
    assert match(p("number", 4820000), g("number", 4820000.0))
    assert not match(p("number", 4820000.5), g("number", 4820000))
    assert match(p("percent", 12.4), g("percent", 12.4))
    assert not match(p("percent", 0.124), g("percent", 12.4))                     # fraction instead of points
    assert not match(p("number", "4820000"), g("number", 4820000))                # wrong JSON type = wrong format
    assert not match(p("percent", 12.4), g("number", 12.4))                       # wrong type name


def test_strings_ignore_case_spaces_punctuation_but_not_diacritics():
    assert match(p("string", "sting  dâu 330ml"), g("string", "Sting dâu 330ml"))
    assert match(p("string", "Sting dâu, 330ml."), g("string", "Sting dâu 330ml"))
    assert not match(p("string", "Sting dầu 330ml"), g("string", "Sting dâu 330ml"))
    assert match(p("string", "SD330"), g("string", "Sting dâu 330ml", alias=["SD330"]))   # any accepted spelling / code
    assert not match(p("string", ["Sting dâu 330ml"]), g("string", "Sting dâu 330ml"))


def test_dates_booleans_unanswerable():
    assert match(p("date", "2026-08-15"), g("date", "2026-08-15"))
    assert not match(p("date", "15/08/2026"), g("date", "2026-08-15"))
    assert match(p("date", "2025-09"), g("date", "2025-09"))
    assert match(p("boolean", True), g("boolean", True))
    assert not match(p("boolean", 1), g("boolean", True))
    assert score({"id": "q", "type": "unanswerable", "reason": "x"}, g("unanswerable")) == 1
    assert score({"id": "q", "type": "unanswerable", "reason": 5}, g("unanswerable")) == -1   # reason must be a string
    assert score(p("number", 0), g("unanswerable")) == -1                          # 0 is not "no data"
    assert score({"id": "q", "type": "unanswerable"}, g("number", 0)) == -1
    assert score({"id": "q", "type": "abstain"}, g("unanswerable")) == 0


def test_lists_need_the_same_order():
    assert match(p("list", ["a", "b"]), g("list", ["a", "b"]))
    assert not match(p("list", ["b", "a"]), g("list", ["a", "b"]))
    assert match(p("list", [3, 10]), g("list", [3.0, 10]))
    assert not match(p("list", ["a"]), g("list", ["a", "b"]))
    assert not match(p("list", "a"), g("list", ["a"]))


def test_tables_ignore_row_order_and_match_columns_by_name_then_position():
    gold = g("table", {"columns": ["khach", "no"], "rows": [["An", 100], ["Bình", 250]]})
    assert match(p("table", {"columns": ["khach", "no"], "rows": [["Bình", 250], ["An", 100]]}), gold)
    assert match(p("table", {"columns": ["no", "khach"], "rows": [[100, "An"], [250, "Bình"]]}), gold)      # same names, other order
    assert match(p("table", {"columns": ["ten", "tien"], "rows": [["An", 100], ["Bình", 250]]}), gold)      # other names -> by position
    assert not match(p("table", {"columns": ["khach", "no"], "rows": [["An", 100]]}), gold)
    assert not match(p("table", {"columns": ["khach", "no"], "rows": [["An", "100"], ["Bình", 250]]}), gold)
