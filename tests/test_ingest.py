import sqlite3

import openpyxl

from datamate.execute import SqlError, run_sql
from datamate.ingest import load_business

import pytest


def test_excel_with_title_row_and_messy_values(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Doanh thu"
    ws.append(["BÁO CÁO BÁN HÀNG QUÝ 1", None, None])
    ws.append(["Ngày", "Mặt hàng", "Thành tiền"])
    ws.append(["05/01/2026", "Trà sữa trân châu", "35k"])
    ws.append(["06/01/2026", "Trà đào", "1.5tr"])
    ws.append(["07/01/2026", "Trà sữa trân châu", "40.000đ"])
    wb.save(tmp_path / "ban_hang.xlsx")
    (tmp_path / "README.md").write_text("Quán trà sữa", encoding="utf-8")

    biz = load_business(tmp_path, "biz_x")
    assert list(biz.tables) == ["ban_hang"]
    df = biz.tables["ban_hang"]
    assert list(df.columns) == ["ngay", "mat_hang", "thanh_tien"]
    assert biz.kinds["ban_hang"] == {"ngay": "date", "mat_hang": "text", "thanh_tien": "number"}
    assert df["ngay"].tolist() == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert df["thanh_tien"].tolist() == [35_000, 1_500_000, 40_000]
    assert 'orig header "Mặt hàng"' in biz.schema_card
    # accent-insensitive search works through the registered norm() function
    res = run_sql(biz.con, "SELECT COUNT(*) FROM ban_hang WHERE norm(mat_hang) LIKE '%tra sua%'")
    assert res.rows == [(2,)]


def test_sqlite_and_csv_sources_and_guardrails(tmp_path):
    con = sqlite3.connect(tmp_path / "shop.sqlite")
    con.execute("CREATE TABLE khach (ten TEXT, no REAL)")
    con.executemany("INSERT INTO khach VALUES (?, ?)", [("An", 0), ("Bình", 250000)])
    con.commit()
    con.close()
    (tmp_path / "ghi_chu.csv").write_text("sdt,ten\n0901234567,An\n", encoding="utf-8")

    biz = load_business(tmp_path, "biz_y")
    assert set(biz.tables) == {"ghi_chu", "khach"}
    assert biz.kinds["ghi_chu"]["sdt"] == "text"  # leading zero: not turned into a number
    assert run_sql(biz.con, "SELECT ten FROM khach WHERE no > 0").rows == [("Bình",)]

    with pytest.raises(SqlError):
        run_sql(biz.con, "DROP TABLE khach")
    with pytest.raises(SqlError):
        run_sql(biz.con, "SELECT 1; SELECT 2")
    with pytest.raises(SqlError):
        run_sql(biz.con, "SELECT * FROM does_not_exist")


def test_grounding_hints(tmp_path):
    (tmp_path / "sp.csv").write_text(
        "ten_hang,gia\nXi măng Hà Tiên PCB40 bao 50kg,95000\nThép phi 10 cây 11.7m,180000\nGạch ống 4 lỗ,1500\n",
        encoding="utf-8")
    biz = load_business(tmp_path, "biz_z")
    hints = biz.grounder.hints("xi mang ha tien con bao nhieu")
    assert hints and hints[0].value.startswith("Xi măng Hà Tiên")
    typo = biz.grounder.hints("thep phi 10 gia bao nhieu")
    assert typo and typo[0].value.startswith("Thép phi 10")


def test_time_of_day_is_kept_in_its_own_column(tmp_path):
    (tmp_path / "don.csv").write_text(
        "Mã đơn,Giờ tạo,Tiền\nA1,04/03/2026 09:30,35000\nA2,04/03/2026 15:45,40000\nA3,05/03/2026 22:10,10000\n",
        encoding="utf-8")
    biz = load_business(tmp_path, "biz_t")
    df = biz.tables["don"]
    assert list(df.columns) == ["ma_don", "gio_tao", "gio_tao_time", "tien"]
    assert df["gio_tao"].tolist() == ["2026-03-04", "2026-03-04", "2026-03-05"]
    assert df["gio_tao_time"].tolist() == ["09:30:00", "15:45:00", "22:10:00"]
    # "ca chiều" = created 14:00-21:59 (rulebook)
    res = run_sql(biz.con, "SELECT COUNT(*) FROM don WHERE gio_tao_time >= '14:00:00' AND gio_tao_time < '22:00:00'")
    assert res.rows == [(1,)]


def test_date_only_columns_get_no_time_column(tmp_path):
    (tmp_path / "don.csv").write_text("Ngày,Tiền\n04/03/2026,35000\n05/03/2026,40000\n", encoding="utf-8")
    biz = load_business(tmp_path, "biz_d")
    assert list(biz.tables["don"].columns) == ["ngay", "tien"]


def test_long_readme_is_not_cut(tmp_path):
    (tmp_path / "ban_hang.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Dữ liệu\n" + "dòng quy đổi 1 thùng = 24 lon\n" * 400 + "CUỐI-README", encoding="utf-8")
    assert load_business(tmp_path, "biz_r").readme.endswith("CUỐI-README")
