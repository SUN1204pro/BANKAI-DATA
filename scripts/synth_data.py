#!/usr/bin/env python
"""Generate a tiny synthetic competition-like dataset for smoke tests and dev.

Layout written (the REAL layout may differ; see layout.py):
  <out>/data/questions.jsonl
  <out>/data/rulebook.md
  <out>/data/businesses/<biz_id>/{ban_hang.csv, README.md}
  <out>/gold.jsonl                 gold answers computed independently with plain Python
  <out>/oracle_sql.json            question -> reference SQL (TEST ONLY: lets a mock LLM "solve" it)

Gold answers are computed from explicit date arithmetic here, NOT from
datamate.timeparse, so the tests actually check the time resolver.

Usage: python scripts/synth_data.py --out /tmp/synth [--n-biz 2] [--seed 7]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

PRODUCTS = [
    ("Xi măng Hà Tiên PCB40 bao 50kg", 95000),
    ("Thép phi 10 cây 11.7m", 180000),
    ("Gạch ống 4 lỗ", 1500),
    ("Cát xây tô m3", 320000),
]
AS_OF = date(2026, 3, 4)  # a Wednesday


def last_week(as_of: date) -> tuple[date, date]:
    monday = as_of - timedelta(days=as_of.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def month_range(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def make_business(out: Path, biz_id: str, rng: random.Random) -> list[dict]:
    rows = []
    d = date(2026, 1, 1)
    n = 0
    while d <= AS_OF:
        for _ in range(rng.randint(1, 4)):
            n += 1
            name, price = rng.choice(PRODUCTS)
            rows.append({
                "ma_don": f"DH{n:04d}", "ngay": d, "ten_hang": name,
                "so_luong": rng.randint(1, 30), "don_gia": price,
                "trang_thai": rng.choice(["hoàn thành"] * 5 + ["hủy"]),
            })
        d += timedelta(days=1)

    bdir = out / "data" / "businesses" / biz_id
    bdir.mkdir(parents=True, exist_ok=True)
    with (bdir / "ban_hang.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Mã đơn", "Ngày", "Tên hàng", "Số lượng", "Đơn giá", "Trạng thái"])
        for r in rows:
            price = f"{r['don_gia']:,}".replace(",", ".")  # 95.000 : Vietnamese thousands dots
            w.writerow([r["ma_don"], r["ngay"].strftime("%d/%m/%Y"), r["ten_hang"], r["so_luong"], price, r["trang_thai"]])
    (bdir / "README.md").write_text(
        "# Cửa hàng vật liệu xây dựng\nBảng ban_hang ghi từng dòng hàng của mỗi đơn. Trạng thái 'hủy' là đơn đã hủy.\n",
        encoding="utf-8")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-biz", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    (out / "data" / "rulebook.md").write_text(
        "# Quy tắc\n\n- Doanh thu = tổng (số lượng x đơn giá) của các đơn có trạng thái 'hoàn thành'. Đơn 'hủy' không tính.\n"
        "- Tuần bắt đầu từ thứ Hai và kết thúc Chủ nhật.\n"
        "- Không có dữ liệu về giá vốn nên không thể tính lợi nhuận.\n",
        encoding="utf-8")

    questions, gold, oracle = [], [], {}
    for b in range(1, args.n_biz + 1):
        biz_id = f"biz_{b:03d}"
        rows = make_business(out, biz_id, rng)
        done = [r for r in rows if r["trang_thai"] == "hoàn thành"]
        lw0, lw1 = last_week(AS_OF)
        m0, m1 = month_range(AS_OF.year, AS_OF.month)
        pm0, pm1 = month_range(AS_OF.year, AS_OF.month - 1)

        qty = defaultdict(int)
        for r in done:
            if lw0 <= r["ngay"] <= lw1:
                qty[r["ten_hang"]] += r["so_luong"]
        top = max(qty.items(), key=lambda kv: (kv[1], kv[0]))[0]
        rev_month = sum(r["so_luong"] * r["don_gia"] for r in done if m0 <= r["ngay"] <= m1)
        cancelled_prev = sum(1 for r in rows if r["trang_thai"] == "hủy" and pm0 <= r["ngay"] <= pm1)

        specs = [
            ("sp bán dc nhiều nhất tuần rồi là gì", "string", top,
             f"SELECT ten_hang FROM ban_hang WHERE trang_thai='hoàn thành' AND ngay BETWEEN '{lw0}' AND '{lw1}' "
             f"GROUP BY ten_hang ORDER BY SUM(so_luong) DESC, ten_hang DESC LIMIT 1"),
            ("tháng này doanh thu bao nhiêu", "number", rev_month,
             f"SELECT SUM(so_luong*don_gia) FROM ban_hang WHERE trang_thai='hoàn thành' AND ngay BETWEEN '{m0}' AND '{m1}'"),
            ("thang truoc co bao nhieu don bi huy", "number", cancelled_prev,
             f"SELECT COUNT(*) FROM ban_hang WHERE trang_thai='hủy' AND ngay BETWEEN '{pm0}' AND '{pm1}'"),
            ("lợi nhuận tháng này là bao nhiêu", "unanswerable", None, None),
        ]
        for i, (text, typ, ans, sql) in enumerate(specs):
            qid = f"q_{b:03d}{i:02d}"
            questions.append({"id": qid, "business_id": biz_id, "question": text, "as_of_date": AS_OF.isoformat()})
            gold.append({"id": qid, "type": typ, "answer": ans})
            oracle[text] = (
                {"answerable": False, "reason": "no cost data", "answer_type": None, "sql": ""} if sql is None
                else {"answerable": True, "reason": "", "answer_type": typ, "sql": sql}
            )

    def dump(path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    dump(out / "data" / "questions.jsonl", questions)
    dump(out / "gold.jsonl", gold)
    (out / "oracle_sql.json").write_text(json.dumps(oracle, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(questions)} questions for {args.n_biz} businesses to {out}")


if __name__ == "__main__":
    main()
