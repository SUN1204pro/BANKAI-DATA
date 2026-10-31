import json
import unicodedata
from datetime import date
from pathlib import Path

from datamate.normalize import Normalized, norm
from datamate.understand import (LunarResolver, MoneyResolver, Query, SlangResolver, TimeResolver, understand)
from datamate.understand.slang import load_slang
from test_lunar import fake_calendar

AS_OF = date(2026, 3, 4)
TRAIN = Path(__file__).resolve().parents[1] / "train" / "questions.jsonl"


def run(q, resolvers=None):
    resolvers = resolvers or [MoneyResolver(), TimeResolver(), SlangResolver()]
    query = Query(q, AS_OF)
    return [(f.resolver, f.text) for f in understand(query, resolvers)]


# ---- positional normalization ---------------------------------------------------------
def test_normalized_text_equals_norm_on_every_train_question():
    qs = [json.loads(l)["question"] for l in TRAIN.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(qs) == 3000
    assert all(Normalized(q).text == norm(q) for q in qs)


def test_offsets_point_back_into_the_original_wording():
    q = "  Doanh   thu  THÁNG   này  "
    nz = Normalized(q)
    i = nz.text.index("thang nay")
    s, e = nz.to_raw(i, i + len("thang nay"))
    assert nz.raw[s:e] == "THÁNG   này"
    assert [(w.text, nz.raw[w.start:w.end]) for w in nz.words()][:2] == [("doanh", "Doanh"), ("thu", "thu")]


def test_decomposed_unicode_input_is_handled():
    q = unicodedata.normalize("NFD", "tháng này")
    nz = Normalized(q)
    assert nz.text == "thang nay" and nz.raw == "tháng này"


# ---- money vs "k" ---------------------------------------------------------------------
def test_5k_is_an_amount_not_the_abbreviation_k():
    assert run("đơn từ 5k trở lên") == [("money", "5k")]
    f = understand(Query("đơn từ 5k trở lên", AS_OF), [MoneyResolver()])[0]
    assert f.value == 5000


def test_amount_formats():
    vals = lambda q: [f.value for f in understand(Query(q, AS_OF), [MoneyResolver()])]
    assert vals("giá 50 k") == [50000]
    assert vals("giá 2,5tr") == [2_500_000]
    assert vals("giá 1.500k") == [1_500_000]
    assert vals("giá 3 tr") == [3_000_000]


def test_bare_k_is_khong_not_nghin():
    f = understand(Query("hàng k kê đơn", AS_OF), [MoneyResolver(), SlangResolver()])
    assert [(x.resolver, x.value) for x in f] == [("slang", "không")]


def test_k_after_a_period_number_is_not_money():
    assert run("tháng 7/2025 k kê đơn", [MoneyResolver(), SlangResolver()]) == [("slang", "k")]
    assert run("tháng 5 k kê đơn") == [("time", "tháng 5"), ("slang", "k")]
    assert run("bán 250ml, 5kg") == []  # units glued to the number are not "k"


# ---- slang.json -----------------------------------------------------------------------
def test_slang_comes_from_the_json_file_and_expands_known_abbreviations():
    table = load_slang()
    assert table["plain"]["dthu"] == "doanh thu"
    got = {f.line for f in understand(Query("dthu hnay bnhiu, ko dc", AS_OF), [SlangResolver()])}
    assert got == {"dthu = doanh thu", "hnay = hôm nay", "bnhiu = bao nhiêu", "ko = không", "dc = được"}


def test_slang_keys_are_accent_free_lowercase():
    for key in load_slang()["plain"] | load_slang()["after_number"]:
        assert key == norm(key) and key.isalnum()


# ---- resolvers do not step on each other ---------------------------------------------
def test_tet_resolver_takes_nam_2025_inside_tet_nam_2025():
    cal = fake_calendar()
    q = "doanh thu Tết năm 2025 so với năm 2025"
    rs = [MoneyResolver(), LunarResolver(cal), TimeResolver(), SlangResolver()]
    got = run(q, rs)
    assert got[0][0] == "lunar" and got[0][1].lower() == "tết năm 2025"
    assert got[1] == ("time", "năm 2025")  # the second, free-standing "năm 2025" is still a plain year
    assert len(got) == 2
    without_lunar = run(q, [TimeResolver()])
    assert [t for _, t in without_lunar] == ["năm 2025", "năm 2025"]


def test_findings_come_back_in_reading_order_with_original_wording():
    q = "Tháng trước dthu bnhiu"
    f = understand(Query(q, AS_OF), [TimeResolver(), SlangResolver()])
    assert [x.text for x in f] == ["Tháng trước", "dthu", "bnhiu"]
    assert [x.start for x in f] == sorted(x.start for x in f)
