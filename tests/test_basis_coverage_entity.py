import difflib
import random
from datetime import date, time

import pandas as pd

from datamate.context import build_context
from datamate.ground import Grounder, NgramIndex, classify
from datamate.ingest import event_date_range, load_business
from datamate.timeparse import Conventions, resolve
from datamate.understand import (AmbiguityResolver, BasisResolver, CoverageResolver, GroundResolver, Query,
                                 ShiftResolver, TimeResolver, understand)

AS_OF = date(2026, 3, 4)


def find(q, resolvers, as_of=AS_OF):
    return understand(Query(q, as_of), resolvers)


# ---- "tháng M/YYYY" must keep its year (the coverage check depends on it) ---------------
def test_month_and_quarter_with_slash_year_keep_the_year():
    s = resolve("doanh thu tháng 7/2025", date(2026, 9, 7))[0]
    assert (s.start, s.end) == (date(2025, 7, 1), date(2025, 7, 31))
    s = resolve("quý 2/2025", date(2026, 9, 7))[0]
    assert (s.start, s.end) == (date(2025, 4, 1), date(2025, 6, 30))


# ---- shifts ------------------------------------------------------------------------------
def test_shift_hours_and_morning_baseline():
    f = find("so ca chiều với ca sáng tuần rồi", [ShiftResolver(["don.gio_tao_time"])])
    by = {x.text: x for x in f}
    assert by["ca chiều"].value == (time(14, 0), time(21, 59))
    assert by["ca sáng"].value == (time(6, 0), time(13, 59))
    assert "14:00-21:59" in by["ca chiều"].line and "don.gio_tao_time" in by["ca chiều"].line
    assert "baseline" in by["ca sáng"].line and "baseline" not in by["ca chiều"].line


# ---- flow vs stock -----------------------------------------------------------------------
def basis(q, as_of=AS_OF):
    f = find(q, [TimeResolver(), BasisResolver("2025-01-01")], as_of)
    return [x for x in f if x.resolver == "basis"]


def test_balance_questions_are_stock_at_the_end_of_the_named_day():
    (b,) = basis("tính đến hết ngày 15/02/2026, bao nhiêu khách còn nợ cửa hàng?")
    assert b.kind == "stock" and b.value == date(2026, 2, 15) and "from the first day of the data" in b.line
    (b,) = basis("hết hôm nay trong kho còn mấy lon bia")
    assert b.kind == "stock" and b.value == AS_OF


def test_activity_questions_are_flow():
    assert [b.kind for b in basis("doanh thu hôm qua là bao nhiêu")] == ["flow"]
    # "đến hết hôm nay" is a range end here, not a balance
    assert [b.kind for b in basis("từ đầu tháng này đến hết hôm nay doanh thu bao nhiêu")] == ["flow"]
    assert [b.kind for b in basis("tháng này khách ghi nợ thêm bao nhiêu và trả nợ được bao nhiêu")] == ["flow"]
    assert basis("cửa hàng có bao nhiêu nhân viên") == []


# ---- coverage ----------------------------------------------------------------------------
RANGE = ("2025-04-01", "2026-02-28")


def coverage(q):
    f = find(q, [TimeResolver(), BasisResolver(), CoverageResolver(RANGE)])
    return [(x.kind, x.text) for x in f if x.resolver == "coverage"]


def test_period_before_the_first_data_day_is_flagged_unanswerable():
    got = coverage("dthu tháng 2/2025 là bao nhiêu")
    assert got == [("outside", "tháng 2/2025")]
    f = find("dthu tháng 2/2025", [TimeResolver(), CoverageResolver(RANGE)])
    assert "unanswerable" in f[-1].line and "2025-04-01" in f[-1].line


def test_partly_covered_period_is_computed_on_the_covered_part():
    assert coverage("doanh thu quý 1/2025") == [("outside", "quý 1/2025")]
    assert coverage("doanh thu từ 15/03/2025 đến 10/04/2025") == [("partial", "từ 15/03/2025 đến 10/04/2025")]
    assert coverage("doanh thu tháng 12/2025") == []


def test_balance_after_the_last_day_is_not_out_of_range():
    assert coverage("hết hôm nay còn nợ bao nhiêu") == []   # today = 2026-03-04, after the last day: nothing new happened
    assert coverage("doanh thu hôm nay") == [("outside", "hôm nay")]  # a flow with no records is not 0


def test_event_date_range_ignores_birthdays_expiry_and_catalogue_dates():
    tables = {
        "danhsachhoadon": pd.DataFrame({"thoi_gian": ["2025-03-02", "2026-01-09"]}),
        "danhsachkhachhang": pd.DataFrame({"ngay_sinh": ["1970-01-01"], "ngay_tao": ["2020-05-05"]}),
        "nhap": pd.DataFrame({"han_su_dung": ["2029-01-01"], "ngay_nhap": ["2025-03-01"]}),
    }
    kinds = {t: {c: "date" for c in df.columns} for t, df in tables.items()}
    assert event_date_range(tables, kinds) == ("2025-03-01", "2026-01-09")


# ---- entity types, ambiguity, n-gram index -------------------------------------------------
def test_classify_column_types():
    assert classify("sp", "ten_hang") == "product"
    assert classify("don", "ten_khach_hang") == "customer"
    assert classify("don", "nhan_vien_ban") == "staff"
    assert classify("sp", "nhom_hang") == "category"
    assert classify("sp", "loai_san_pham") == "category"
    assert classify("don", "hinh_thuc_thanh_toan") == "payment"
    assert classify("don", "cot_la", values=["Tiền mặt", "Chuyển khoản", "Momo"]) == "payment"
    assert classify("nhan_vien", "ten") == "staff" and classify("khach_hang", "ho_ten") == "customer"
    assert classify("don", "so_luong_san_pham") == "other"


def grounder(tmp_path):
    (tmp_path / "sp.csv").write_text(
        "ten_hang,nhom_hang\nBia Tiger lon,Bia\nBia Tiger chai,Bia\nBia Tiger Bạc lon,Bia\nCà phê muối,Cà phê\nTrà đào,Trà\n",
        encoding="utf-8")
    (tmp_path / "don.csv").write_text(
        "ten_khach_hang,nhan_vien,hinh_thuc_thanh_toan\nNguyễn Văn An,Lê Hoa,Tiền mặt\nTrần Bình,Lê Hoa,Chuyển khoản\n",
        encoding="utf-8")
    return load_business(tmp_path, "b")


def test_hints_are_typed(tmp_path):
    g = grounder(tmp_path).grounder
    types = {h.value: h.etype for h in g.hints("nguyen van an mua tien mat do le hoa ban, ca phe muoi")}
    assert types["Nguyễn Văn An"] == "customer" and types["Tiền mặt"] == "payment"
    assert types["Lê Hoa"] == "staff" and types["Cà phê muối"] == "product"


def test_ambiguous_phrase_lists_the_options(tmp_path):
    biz = grounder(tmp_path)
    q = "bia tiger bán được bao nhiêu"
    amb = [x for x in find(q, [AmbiguityResolver(biz.grounder)]) if x.kind == "same_column"]
    assert len(amb) == 1
    names = {h.value for h in amb[0].value}
    assert names == {"Bia Tiger lon", "Bia Tiger chai", "Bia Tiger Bạc lon"} or {"Bia Tiger lon", "Bia Tiger chai"} <= names
    assert "fits" in amb[0].line and "Bia Tiger chai" in amb[0].line
    # a complete match is not ambiguous
    assert [x for x in find("bia tiger bac lon", [AmbiguityResolver(biz.grounder)]) if x.kind == "same_column"] == []


def test_dates_are_not_matched_against_cell_values(tmp_path):
    (tmp_path / "t.csv").write_text("ten_hang,gia\nTổng tháng 2/2025,0\nBánh mì,15000\n", encoding="utf-8")
    biz = load_business(tmp_path, "b")
    got = find("tháng 2/2025 bán bánh mì", [TimeResolver(), GroundResolver(biz.grounder)])
    assert [x.text for x in got if x.resolver == "entity"] == ["bánh mì"]


def test_sibling_monthly_tables_are_listed_once(tmp_path):
    for m in ("01", "02", "03"):
        (tmp_path / f"don_{m}.csv").write_text("ten_hang,gia\nBánh mì,15000\n", encoding="utf-8")
    hints = load_business(tmp_path, "b").grounder.hints("banh mi")
    assert len(hints) == 1 and hints[0].also == 2 and "(+2 same-named tables)" in hints[0].render()


def test_ngram_index_matches_difflib():
    rnd = random.Random(1)
    letters = "abcdeghilmnoprstuy"
    vocab = sorted({"".join(rnd.choice(letters) for _ in range(rnd.randint(4, 9))) for _ in range(3000)})
    idx = NgramIndex(vocab)
    for w in rnd.sample(vocab, 150):
        typo = w[:2] + w[3:] if rnd.random() < .5 else w + rnd.choice(letters)
        want = (difflib.get_close_matches(typo, vocab, n=1, cutoff=0.84) or [None])[0]
        assert idx.closest(typo) == want, (typo, want)


# ---- all together through build_context ------------------------------------------------------
def test_context_sections(tmp_path):
    (tmp_path / "don.csv").write_text(
        "Mã đơn,Giờ tạo,Còn nợ\nA1,04/03/2026 09:30,0\nA2,05/04/2025 15:45,10\n", encoding="utf-8")
    biz = load_business(tmp_path, "b")
    ctx = build_context("tính đến hết hôm nay còn nợ bao nhiêu, ca chiều, dthu tháng 1/2025", AS_OF, biz, None, Conventions())
    s = ctx.sections
    assert "STOCK" in s["TIME BASIS"]
    assert "14:00-21:59" in s["SHIFTS"] and "don.gio_tao_time" in s["SHIFTS"]
    assert "unanswerable" in s["DATA COVERAGE"] and "2025-04-05" in s["DATA COVERAGE"]
