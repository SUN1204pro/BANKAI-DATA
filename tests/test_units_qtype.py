from datetime import date

import pandas as pd

from datamate.context import build_context
from datamate.ingest import load_business
from datamate.normalize import norm
from datamate.prompts import render_user
from datamate.rules import RuleBook, parse_items
from datamate.timeparse import Conventions
from datamate.units import UnitCatalog
from datamate.understand import QuestionTypeResolver, Query, UnitResolver, rules_for, understand
from datamate.understand.qtype import load_topics
from datamate.understand.slang import expand_slang

AS_OF = date(2026, 3, 4)
RULEBOOK = """# Rulebook

## 1. Đơn hàng và doanh thu
| Luật | Mặc định |
|---|---|
| Đơn tính vào doanh thu | đơn **không huỷ** và **không trả hàng** |
| Tỉ lệ đơn huỷ trong kỳ | phần trăm đơn huỷ trong **mọi đơn đặt trong kỳ** |
| Đơn bị huỷ | đơn có trạng thái huỷ |

## 4. Công nợ
| Luật | Mặc định |
|---|---|
| Đơn được tính vào nợ | đơn không huỷ; **đơn trả hàng vẫn tính** nếu còn nợ |
| Tuổi nợ | số ngày từ **ngày đặt** |

## 9. Đáp án
- **Bằng nhau:** xếp theo mã tăng dần.
"""


# ---- rulebook parsing ------------------------------------------------------------------------
def test_parse_items_reads_table_rows_and_bold_bullets_but_not_headers():
    items = parse_items(RULEBOOK)
    assert [i.key for i in items] == ["Đơn tính vào doanh thu", "Tỉ lệ đơn huỷ trong kỳ", "Đơn bị huỷ",
                                      "Đơn được tính vào nợ", "Tuổi nợ", "Bằng nhau"]
    assert items[3].section == "4" and items[3].heading == "Công nợ"
    assert items[3].render().startswith("- Đơn được tính vào nợ [§4]:")


def test_every_row_pattern_of_topics_json_matches_a_row_of_the_real_rulebook():
    from pathlib import Path
    rb = RuleBook.load(Path(__file__).resolve().parents[1] / "train" / "rulebook.md")
    dead = [(n, p) for n, t in load_topics().items() for p in t["rows"] if not rb.pick([p])]
    assert dead == []


# ---- question types --------------------------------------------------------------------------
def kinds(q):
    return [f.kind for f in understand(Query(q, AS_OF), [QuestionTypeResolver()])]


def test_question_types():
    assert "debt" in kinds("tính đến hết ngày 15/02/2026 khách nào còn nợ nhiều nhất")
    assert "ranking" in kinds("top 3 sản phẩm bán chạy nhất tháng này")
    assert {"cancel", "ratio"} <= set(kinds("tỉ lệ đơn huỷ tháng 6/2025 là bao nhiêu %"))
    assert "compare" in kinds("doanh thu tháng này so với tháng trước")
    assert "sold_units" in kinds("tháng 3 bán được bao nhiêu thùng bia")
    assert "stock" in kinds("tồn kho bia cuối ngày hôm nay")
    assert "purchase" in kinds("giá nhập bình quân một hộp bánh") and "profit" not in kinds("giá nhập bình quân một hộp bánh")
    assert "profit" in kinds("hàng nào lời nhất tháng này")


def test_slang_is_read_as_full_words_and_positions_stay_in_the_original():
    assert "sold_units" in kinds("thang 3 ban dc bn thung bia")           # dc, bn
    f = understand(Query("ti le don huy la bnhiu %", AS_OF), [QuestionTypeResolver()])
    assert {x.kind for x in f} >= {"cancel", "ratio"}
    text, origin = expand_slang("ban dc bn")
    assert text == "ban duoc bao nhieu"
    assert origin[text.index("duoc")] == "ban dc bn".index("dc")        # the expansion points back at the word typed
    assert origin[text.index("nhieu")] == "ban dc bn".index("bn")
    f = understand(Query("tháng 3 bán đc bn thùng", AS_OF), [QuestionTypeResolver()])
    sold = next(x for x in f if x.kind == "sold_units")
    assert sold.text == "bán đc bn thùng"


def test_sheet_metal_ton_is_not_inventory():
    assert "stock" not in kinds("Doanh thu của Tôn lạnh Hoa Sen 0.4mm trong tháng 7 năm 2025")
    assert "stock" not in kinds("Thang 7 ton lanh hoa sen")


def test_bare_k_and_cancel_do_not_trigger_unrelated_topics():
    assert "compare" not in kinds("cho bảng 3 khách còn nợ nhìu nhất, kèm số tiền còn nợ")


# ---- rule reminders ----------------------------------------------------------------------------
def test_debt_and_revenue_get_different_order_rules():
    rb = RuleBook(RULEBOOK)
    debt = rules_for(["debt"], rb)
    rev = rules_for(["revenue"], rb)
    assert "đơn trả hàng vẫn tính" in debt and "Tuổi nợ" in debt and "doanh thu" not in debt
    assert "không trả hàng" in rev and "nợ" not in rev


def test_cancel_rate_is_the_first_row_of_its_block_and_rows_are_not_repeated():
    rb = RuleBook(RULEBOOK)
    out = rules_for(["cancel", "order_count"], rb)      # both list "Đơn bị huỷ": shown once, under the first topic
    assert out.count("Đơn bị huỷ") == 1
    block = out.split("### ")[1]
    assert block.splitlines()[1].startswith("- Tỉ lệ đơn huỷ trong kỳ")


def test_unmatched_patterns_are_skipped_not_errors():
    assert rules_for(["debt"], RuleBook("# nothing useful\n")) == ""
    assert rules_for(["debt"], None) == ""


# ---- units -----------------------------------------------------------------------------------
README = """# Shop
## Quy ước
- **Quy đổi đơn vị**: số lượng ghi theo đơn vị của dòng:
  - Bia Tiger lon: 1 thùng = 24 lon
  - Cát xây tô: 1 xe 4m3 = 4 m3
  - Coca Cola 330ml: 1 lốc = 6 lon
  - Coca Cola 330ml: 1 thùng = 24 lon
"""


def test_readme_rules():
    cat = UnitCatalog.build(README, {})
    got = {(r.product, r.render()) for r in cat.rules}
    assert ("Bia Tiger lon", "1 thùng = 24 lon") in got
    assert ("Cát xây tô", "1 xe 4m3 = 4 m3") in got
    assert len(cat.rules) == 4


def test_conversion_table_by_name_and_by_code_and_converted_column():
    by_name = pd.DataFrame({"Tên sản phẩm": ["Pepsi"], "Đơn vị quy đổi": ["thùng"], "Tỉ lệ quy đổi": ["24"], "Đơn vị cơ bản": ["lon"]})
    master = pd.DataFrame({"Mã hàng": ["SP1", "SP2"], "Tên hàng": ["Bánh croissant", "Bánh mì"], "ĐVT": ["cái", "ổ"]})
    by_code = pd.DataFrame({"Mã hàng": ["SP1"], "ĐVT": ["hộp"], "Quy đổi": ["6"], "Giá bán": ["222000"]})
    sales = pd.DataFrame({"Tên hàng": ["Pepsi"], "Số lượng": ["2"], "Số lượng quy đổi": ["48"]})
    tables = {"quydoi": by_name, "sp": master, "dvt": by_code, "banhang": sales}
    origin = {t: {c: c for c in df.columns} for t, df in tables.items()}
    cat = UnitCatalog.build("", tables, origin)
    got = {(r.product, r.render()) for r in cat.rules}
    assert ("Pepsi", "1 thùng = 24 lon") in got
    assert ("Bánh croissant", "1 hộp = 6 cái") in got      # name and base unit looked up through the code
    assert cat.converted_columns == ["banhang.Số lượng quy đổi"]


def make_units_biz(tmp_path):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / "ban.csv").write_text(
        "ngay,ten_hang,so_luong,gia\n2025-06-01,Bia Tiger lon,2,15000\n2025-06-02,Bia Heineken lon,1,18000\n2025-06-03,Coca Cola 330ml,3,9000\n", encoding="utf-8")
    return load_business(tmp_path, "b")


def test_unit_hint_for_a_mentioned_product(tmp_path):
    biz = make_units_biz(tmp_path)
    ctx = build_context("tồn kho Bia Tiger lon hôm nay bao nhiêu thùng", AS_OF, biz, None, Conventions(), RuleBook(RULEBOOK))
    assert "Bia Tiger lon: 1 thùng = 24 lon" in ctx.sections["UNIT CONVERSIONS"]
    assert "Coca Cola" not in ctx.sections["UNIT CONVERSIONS"]


def test_product_without_rule_is_base_unit(tmp_path):
    biz = make_units_biz(tmp_path)
    ctx = build_context("bán được bao nhiêu lon Bia Heineken lon", AS_OF, biz, None, Conventions(), None)
    assert "Bia Heineken lon: no conversion rule" in ctx.sections["UNIT CONVERSIONS"]


def test_unit_named_without_product_lists_who_has_it(tmp_path):
    biz = make_units_biz(tmp_path)
    f = understand(Query("bán được bao nhiêu thùng", AS_OF), [UnitResolver(biz.units)])
    assert f and "1 thùng = 24" in f[0].line and "Bia Tiger lon" in f[0].line


# ---- prompt layout -----------------------------------------------------------------------------
def test_new_sections_reach_the_prompt_and_rules_are_last(tmp_path):
    biz = make_units_biz(tmp_path)
    q = "tính đến hết hôm nay khách còn nợ bao nhiêu, ca chiều, đơn từ 5k, tháng 1/2020"
    ctx = build_context(q, AS_OF, biz, None, Conventions(), RuleBook(RULEBOOK))
    user = render_user(q, AS_OF.isoformat(), ctx.sections)
    for title in ("TIME BASIS", "DATA COVERAGE", "SHIFTS", "AMOUNTS", "QUESTION TYPE", "RULES FOR THIS QUESTION"):
        assert f"## {title}\n" in user, title
    assert user.index("## QUESTION\n") < user.index("## RULES FOR THIS QUESTION")
    assert user.strip().splitlines()[-1].startswith("- ")      # the prompt ends with rulebook rows
    assert "## SCHEMA" in user and user.index("## SCHEMA") < user.index("## TIME BASIS")
