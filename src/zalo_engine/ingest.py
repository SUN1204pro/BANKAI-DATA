"""Lossless, schema-adaptive business data ingestion into per-business SQLite databases.

Implements the processing order and completion checks specified in
`BASELINE_PIPELINE_PLAN.md` and `.agents/skills/datamate-ingestion/SKILL.md`:
1. Coordinate-aware OpenXML XLSX reader, CSV reader, and SQLite reader.
2. Runtime metadata compilation from `README.md` (`src.metadata_compiler`).
3. Header detection (handling preamble banners and 2-row super-headers) and
   whole-row classification (`preamble`, `primary_header`, `repeated_header`,
   `subtotal`, `footer`, `blank`, `data`).
4. Logical table grouping across compatible chunk files and period sheets in
   chronological/documented sequence, with grain establishment.
5. Order-scoped group recovery, order-scoped fill-down, and order-scoped exact
   raw deduplication on sales/order tables that have an order ID column.
6. Subtotal reconciliation against deduplicated rows across all statuses.
7. Cell normalization, `_row_role` assignment, inline topping splitting into
   derived component rows, unit conversion (`_base_qty`), and cross-table
   return/status resolution.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from zalo_engine.column_inference import (
    arithmetic_holds_on_all_rows,
    find_documented_base_qty,
    infer_roles_from_values,
    match_catalog_column,
)
from zalo_engine.metadata_compiler import (
    BusinessMetadata,
    extract_context_year,
    infer_column_role,
    parse_readme_metadata,
    register_conversion_table_rows,
)
from zalo_engine.normalizers import (
    Diagnostic,
    normalize_order_status,
    parse_customer_field,
    parse_date_value,
    parse_inline_topping,
    parse_money,
    parse_quantity,
)
from zalo_engine.postprocess import (
    normalize_comparison_string,
    normalize_display_string,
    round_money_vnd,
)
from zalo_engine.text_norm import fold_key

_OPENXML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Built-in Excel date/time numFmtIds
_BUILTIN_DATE_NUMFMTS = (
    set(range(14, 23))
    | set(range(27, 37))
    | set(range(45, 48))
    | set(range(50, 59))
)

# Subtotal row label regex (anchored on full subtotal label shapes)
SUBTOTAL_LABEL_RE = re.compile(
    r"^(?:"
    r"(?:tổng|tong|cộng|cong)\s+ngày(?:\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)?"
    r"|(?:tổng|tong|cộng|cong)\s+tháng(?:\s+\d{1,2}(?:[/-]\d{2,4})?)?"
    r"|(?:tổng|tong|cộng|cong)\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|tổng\s+cộng|tong\s+cong|cộng|tổng"
    r")$",
    re.IGNORECASE,
)

_DAILY_SUBTOTAL_RE = re.compile(
    r"^(?:(?:tổng|tong|cộng|cong)\s+ngày\b|(?:tổng|tong|cộng|cong)\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?$)",
    re.IGNORECASE,
)

# Two-row super-header tokens
_SUPER_HEADER_TOKENS = {
    "đơn",
    "hàng",
    "tiền",
    "hàng hóa",
    "hàng hoá",
    "hóa đơn",
    "hoá đơn",
    "thông tin",
}


_INGEST_CODE_FILES = (
    "ingest.py",
    "metadata_compiler.py",
    "normalizers.py",
    "postprocess.py",
    "semantic.py",
    "text_norm.py",
    "column_inference.py",
)


def input_fingerprint(biz_dir: str | Path) -> str:
    """Content hash of a business folder, so a cached DB is reused only for identical input."""
    h = hashlib.sha256()
    for f in sorted(Path(biz_dir).iterdir(), key=lambda x: x.name):
        if f.is_file() and not f.name.startswith("."):
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()


def pipeline_fingerprint() -> str:
    """Hash of the ingestion code, so a DB built by older code is never silently reused."""
    h = hashlib.sha256()
    src_dir = Path(__file__).resolve().parent
    for name in _INGEST_CODE_FILES:
        h.update((src_dir / name).read_bytes())
    return h.hexdigest()


def is_db_current(db_path: str | Path, biz_dir: str | Path) -> bool:
    """True only if `db_path` was fully built from this input by this ingestion code."""
    p = Path(db_path)
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
        try:
            stored = dict(
                conn.execute(
                    "SELECT key, value FROM _business_metadata WHERE key IN ('input_hash', 'pipeline_hash')"
                ).fetchall()
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return (
        stored.get("pipeline_hash") == pipeline_fingerprint()
        and stored.get("input_hash") == input_fingerprint(biz_dir)
    )


@dataclass
class RawCell:
    """A single raw cell with coordinate and style metadata."""

    value: Any
    is_date_formatted: bool = False
    is_error_cell: bool = False


@dataclass
class RawSheetData:
    """Raw rows read from a single CSV file, XLSX worksheet, or SQLite table."""

    source_file: str
    source_sheet: str
    rows: list[tuple[int, list[RawCell]]]  # (1-based _source_row, cells)
    date1904: bool = False
    is_sqlite_source: bool = False
    sqlite_headers: list[str] | None = None


@dataclass
class SubtotalCheckResult:
    """A single subtotal reconciliation record."""

    logical_table: str
    source_file: str
    source_sheet: str
    source_row: int
    subtotal_label: str
    subtotal_kind: str  # 'day' or 'month'
    target_col: str
    expected_amount: int
    computed_amount: int
    delta: int


@dataclass
class SheetAccountingSummary:
    """Row accounting counts for one raw source sheet/CSV/table."""

    source_file: str
    source_sheet: str
    logical_table: str
    table_grain: str
    raw_row_count: int = 0
    kept_original_count: int = 0
    preamble_count: int = 0
    primary_header_count: int = 0
    repeated_header_count: int = 0
    subtotal_count: int = 0
    footer_count: int = 0
    blank_count: int = 0
    removed_duplicate_count: int = 0
    quarantined_count: int = 0
    derived_component_count: int = 0
    context_year: int | None = None
    context_month: int | None = None

    @property
    def accounted_total(self) -> int:
        return (
            self.kept_original_count
            + self.preamble_count
            + self.primary_header_count
            + self.repeated_header_count
            + self.subtotal_count
            + self.footer_count
            + self.blank_count
            + self.removed_duplicate_count
            + self.quarantined_count
        )


@dataclass
class IngestionReport:
    """Summary returned after ingesting a single business folder into SQLite."""

    business_id: str
    db_path: Path
    metadata: BusinessMetadata
    sheet_summaries: list[SheetAccountingSummary] = field(default_factory=list)
    subtotal_checks: list[SubtotalCheckResult] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Raw Readers (OpenXML XLSX, CSV, SQLite)
# ---------------------------------------------------------------------------

def _col_letters_to_index(ref: str) -> int | None:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    if not letters:
        return None
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _parse_xlsx_date_styles(z: zipfile.ZipFile) -> set[int]:
    """Return the set of 0-based `<cellXfs>` style indices that format numbers as dates."""
    if "xl/styles.xml" not in z.namelist():
        return set()
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except Exception:
        return set()

    custom_date_fmts: set[int] = set()
    for nf in root.findall(f".//{_OPENXML_NS}numFmt"):
        fmt_id_str = nf.attrib.get("numFmtId")
        code = nf.attrib.get("formatCode", "")
        if not fmt_id_str or not fmt_id_str.isdigit():
            continue
        fmt_id = int(fmt_id_str)
        # Strip quoted literals and color tags like [Red]
        cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]', "", code).lower()
        if any(tok in cleaned for tok in ("yy", "mm", "dd", "d/", "/m", "m/d")) and not re.search(r"[0#]", cleaned):
            custom_date_fmts.add(fmt_id)

    date_style_indices: set[int] = set()
    cell_xfs = root.find(f"{_OPENXML_NS}cellXfs")
    if cell_xfs is not None:
        for s_idx, xf in enumerate(cell_xfs.findall(f"{_OPENXML_NS}xf")):
            nf_str = xf.attrib.get("numFmtId", "0")
            if nf_str.isdigit():
                nf_id = int(nf_str)
                if nf_id in _BUILTIN_DATE_NUMFMTS or nf_id in custom_date_fmts:
                    date_style_indices.add(s_idx)
    return date_style_indices


def read_xlsx_workbook(xlsx_path: str | Path) -> list[RawSheetData]:
    """Read all worksheets from an `.xlsx` file using coordinate-aware OpenXML parsing."""
    p = Path(xlsx_path)
    sheets_out: list[RawSheetData] = []

    with zipfile.ZipFile(p, "r") as z:
        # 1. Shared strings
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            ss_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{_OPENXML_NS}si"):
                text = "".join(t.text or "" for t in si.iter(f"{_OPENXML_NS}t"))
                shared_strings.append(unicodedata.normalize("NFC", text))

        # 2. Styles & workbook epoch
        date_styles = _parse_xlsx_date_styles(z)
        wb_root = ET.fromstring(z.read("xl/workbook.xml"))
        wb_pr = wb_root.find(f"{_OPENXML_NS}workbookPr")
        date1904 = bool(
            wb_pr is not None and wb_pr.attrib.get("date1904", "0") in {"1", "true", "TRUE"}
        )

        # 3. Sheet relationships
        rels_map: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in z.namelist():
            rels_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rels_root.findall(f"{_PKG_REL_NS}Relationship"):
                r_id = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "")
                if target.startswith("/"):
                    target = target.lstrip("/")
                elif not target.startswith("xl/"):
                    target = f"xl/{target}"
                rels_map[r_id] = target

        # 4. Iterate sheets in workbook order
        sheets_el = wb_root.findall(f".//{_OPENXML_NS}sheet")
        for idx, sh_el in enumerate(sheets_el, start=1):
            sh_name = unicodedata.normalize("NFC", sh_el.attrib.get("name", f"Sheet{idx}"))
            r_id = sh_el.attrib.get(f"{_REL_NS}id", "")
            target_path = rels_map.get(r_id, f"xl/worksheets/sheet{idx}.xml")
            if target_path not in z.namelist():
                continue

            sh_root = ET.fromstring(z.read(target_path))
            raw_rows_by_num: dict[int, list[RawCell]] = {}
            max_col_seen = 0
            last_row_num = 0

            for r_el in sh_root.findall(f".//{_OPENXML_NS}row"):
                r_attr = r_el.attrib.get("r")
                row_num = int(r_attr) if (r_attr and r_attr.isdigit()) else (last_row_num + 1)
                last_row_num = max(last_row_num, row_num)

                col_cells: dict[int, RawCell] = {}
                cur_col = 0
                for c_el in r_el.findall(f"{_OPENXML_NS}c"):
                    ref = c_el.attrib.get("r", "")
                    coord_col = _col_letters_to_index(ref)
                    if coord_col is not None:
                        cur_col = coord_col
                    t_attr = c_el.attrib.get("t", "")
                    s_attr = c_el.attrib.get("s", "")
                    is_date_fmt = bool(s_attr.isdigit() and int(s_attr) in date_styles)

                    v_el = c_el.find(f"{_OPENXML_NS}v")
                    is_el = c_el.find(f"{_OPENXML_NS}is")
                    cell_val: Any = None
                    is_err = False

                    if t_attr == "s":
                        if v_el is not None and v_el.text is not None and v_el.text.strip():
                            s_idx = int(v_el.text.strip())
                            cell_val = shared_strings[s_idx] if 0 <= s_idx < len(shared_strings) else ""
                        else:
                            cell_val = ""
                        is_date_fmt = False
                    elif t_attr == "inlineStr" or is_el is not None:
                        if is_el is not None:
                            txt = "".join(t.text or "" for t in is_el.iter(f"{_OPENXML_NS}t"))
                            cell_val = unicodedata.normalize("NFC", txt)
                        else:
                            cell_val = ""
                        is_date_fmt = False
                    elif t_attr == "str":
                        cell_val = unicodedata.normalize("NFC", v_el.text or "") if v_el is not None else ""
                        is_date_fmt = False
                    elif t_attr == "b":
                        if v_el is not None and v_el.text is not None:
                            cell_val = "TRUE" if v_el.text.strip() in {"1", "true", "TRUE"} else "FALSE"
                        else:
                            cell_val = ""
                        is_date_fmt = False
                    elif t_attr == "e":
                        cell_val = (v_el.text or "#ERROR!").strip() if v_el is not None else "#ERROR!"
                        is_err = True
                        is_date_fmt = False
                    else:
                        if v_el is not None and v_el.text is not None and v_el.text.strip() != "":
                            cell_val = unicodedata.normalize("NFC", v_el.text.strip())
                        else:
                            cell_val = None

                    col_cells[cur_col] = RawCell(
                        value=cell_val,
                        is_date_formatted=is_date_fmt,
                        is_error_cell=is_err,
                    )
                    if cell_val is not None and str(cell_val).strip() != "":
                        max_col_seen = max(max_col_seen, cur_col + 1)
                    cur_col += 1

                if col_cells:
                    row_width = max(col_cells.keys()) + 1
                    row_list = [
                        col_cells.get(c, RawCell(value=None)) for c in range(row_width)
                    ]
                    raw_rows_by_num[row_num] = row_list
                else:
                    raw_rows_by_num[row_num] = []

            # Find last non-empty row so trailing formatting-only rows do not bloat the extent,
            # while every row from 1..last_non_empty_row is explicitly accounted for.
            last_non_empty = 0
            for r_num, r_cells in raw_rows_by_num.items():
                if any(c.value is not None and str(c.value).strip() != "" for c in r_cells):
                    last_non_empty = max(last_non_empty, r_num)

            expanded_rows: list[tuple[int, list[RawCell]]] = []
            for r_num in range(1, last_non_empty + 1):
                r_cells = raw_rows_by_num.get(r_num, [])
                if len(r_cells) < max_col_seen and r_cells:
                    r_cells = r_cells + [RawCell(value=None) for _ in range(max_col_seen - len(r_cells))]
                expanded_rows.append((r_num, r_cells))

            sheets_out.append(
                RawSheetData(
                    source_file=p.name,
                    source_sheet=sh_name,
                    rows=expanded_rows,
                    date1904=date1904,
                )
            )

    return sheets_out


def read_csv_file(csv_path: str | Path) -> RawSheetData:
    """Read a CSV file into RawSheetData with 1-based source row numbers."""
    p = Path(csv_path)
    raw_bytes = p.read_bytes()
    raw_text: str | None = None
    for enc in ("utf-8-sig", "utf-16", "cp1258"):
        try:
            raw_text = raw_bytes.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if raw_text is None:
        raise ValueError(f"Cannot decode CSV file: {p}")
    # io.StringIO keeps newlines inside quoted cells; str.splitlines() would split those rows.
    reader = csv.reader(io.StringIO(raw_text, newline=""))
    rows_out: list[tuple[int, list[RawCell]]] = []
    for idx, row in enumerate(reader, start=1):
        cells = [
            RawCell(
                value=unicodedata.normalize("NFC", c) if c != "" else None,
                is_date_formatted=False,
                is_error_cell=False,
            )
            for c in row
        ]
        rows_out.append((idx, cells))

    # Trim trailing completely empty rows if any, or keep all rows up to last non-empty
    last_non_empty = 0
    for idx, cells in rows_out:
        if any(c.value is not None and str(c.value).strip() != "" for c in cells):
            last_non_empty = idx
    rows_out = [pair for pair in rows_out if pair[0] <= last_non_empty]

    return RawSheetData(
        source_file=p.name,
        source_sheet=p.stem,
        rows=rows_out,
        date1904=False,
    )


def read_sqlite_file(sqlite_path: str | Path) -> list[RawSheetData]:
    """Read all user tables from a SQLite `.db`/`.sqlite` source file."""
    p = Path(sqlite_path)
    sheets_out: list[RawSheetData] = []
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        )
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            cur.execute(f'PRAGMA table_info("{tbl}")')
            col_info = cur.fetchall()
            headers = [unicodedata.normalize("NFC", str(c[1])) for c in col_info]

            cur.execute(f'SELECT * FROM "{tbl}"')
            db_rows = cur.fetchall()

            # Represent header as synthetic row 0? No: for SQLite tables, raw rows are the table records
            # (1..N), and sqlite_headers stores the schema headers directly so primary_header_count = 0.
            rows_out: list[tuple[int, list[RawCell]]] = []
            for r_idx, db_row in enumerate(db_rows, start=1):
                cells = [
                    RawCell(
                        value=unicodedata.normalize("NFC", str(v)) if isinstance(v, str) else v,
                        is_date_formatted=False,
                        is_error_cell=False,
                    )
                    for v in db_row
                ]
                rows_out.append((r_idx, cells))

            sheets_out.append(
                RawSheetData(
                    source_file=p.name,
                    source_sheet=tbl,
                    rows=rows_out,
                    date1904=False,
                    is_sqlite_source=True,
                    sqlite_headers=headers,
                )
            )
    finally:
        conn.close()
    return sheets_out


# ---------------------------------------------------------------------------
# 2. Header Detection & Whole-Row Classification
# ---------------------------------------------------------------------------

def _cell_str(cell: RawCell) -> str:
    if cell.value is None:
        return ""
    return normalize_display_string(str(cell.value))


def _is_row_blank(cells: list[RawCell]) -> bool:
    return not any(_cell_str(c) != "" for c in cells)


def _score_candidate_header_row(
    cells: list[RawCell],
    meta: BusinessMetadata,
    file_name: str,
    sheet_name: str,
) -> tuple[int, int, bool]:
    """Score a candidate header row: returns (role_or_gloss_matches, non_empty_count, is_super_header)."""
    non_empty = [_cell_str(c) for c in cells if _cell_str(c) != ""]
    if not non_empty:
        return (0, 0, False)

    norm_tokens = [normalize_comparison_string(s) for s in non_empty]
    if all(t in _SUPER_HEADER_TOKENS for t in norm_tokens) and len(non_empty) <= 5:
        return (0, len(non_empty), True)

    # Disqualify obvious data rows (e.g. ORD00001, pure numbers, dates)
    data_like_count = 0
    for s in non_empty:
        if re.match(r"^(?:ORD|HD|BILL|PN|PT|PC|DH|SO|INV)[0-9_-]+$", s, re.IGNORECASE):
            data_like_count += 1
        elif re.match(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?$", s) or re.match(r"^\d{4}-\d{2}-\d{2}", s):
            data_like_count += 1
        elif re.match(r"^-?\d+(?:[.,]\d+)*(?:đ|k|tr)?$", s, re.IGNORECASE):
            data_like_count += 1

    if data_like_count >= 2:
        return (-10, len(non_empty), False)

    matches = 0
    for s in non_empty:
        gloss = meta.lookup_column_gloss(file_name, sheet_name, s)
        role, _ = infer_column_role(s, gloss, stt_is_order_id=meta.stt_is_order_id)
        if gloss is not None or role != "other":
            matches += 1
        elif normalize_comparison_string(s) in {
            "bàn",
            "khu vực",
            "kênh",
            "kênh bán",
            "nguồn",
            "nhóm",
            "nhóm hàng",
            "loại hàng",
            "phân loại",
            "địa chỉ",
            "addr",
            "lô",
            "mã lô",
            "hoạt chất",
            "thuốc kê đơn",
            "thợ",
            "xe",
            "làm gì",
            "imei",
            "tên gọi khác",
            "ghi tắt",
        }:
            matches += 1

    return (matches, len(non_empty), False)


def detect_header_row(
    sheet: RawSheetData,
    meta: BusinessMetadata,
) -> tuple[int | None, list[str]]:
    """Find the 0-based index in `sheet.rows` of the primary header row and extract column names."""
    if sheet.is_sqlite_source and sheet.sqlite_headers is not None:
        return (None, list(sheet.sqlite_headers))

    if not sheet.rows:
        return (None, [])

    search_limit = min(len(sheet.rows), 15)
    best_idx = 0
    best_score = (-999, 0)

    scores: list[tuple[int, int, int, bool]] = []
    for idx in range(search_limit):
        _, cells = sheet.rows[idx]
        if _is_row_blank(cells):
            scores.append((idx, -999, 0, False))
            continue
        m_cnt, ne_cnt, is_super = _score_candidate_header_row(
            cells, meta, sheet.source_file, sheet.source_sheet
        )
        scores.append((idx, m_cnt, ne_cnt, is_super))
        if not is_super and (m_cnt, ne_cnt) > best_score:
            best_score = (m_cnt, ne_cnt)
            best_idx = idx

    # Check if best_idx is immediately preceded by a 2-row super-header or if best_idx itself
    # is followed by a stronger lower header row.
    if best_idx + 1 < search_limit:
        _, next_m, next_ne, next_super = scores[best_idx + 1]
        cur_m, cur_ne = best_score
        if not next_super and next_m >= 2 and (next_m > cur_m or (next_m == cur_m and next_ne > cur_ne)):
            best_idx = best_idx + 1

    _, hdr_cells = sheet.rows[best_idx]
    headers: list[str] = []
    for c_idx, c in enumerate(hdr_cells):
        s = _cell_str(c)
        headers.append(s if s else f"col_{c_idx + 1}")

    # Trim trailing synthetic `col_N` headers if no data row ever populates those trailing columns
    max_populated_col = len(headers)
    while max_populated_col > 0 and headers[max_populated_col - 1].startswith("col_"):
        col_i = max_populated_col - 1
        any_pop = any(
            col_i < len(r_cells) and _cell_str(r_cells[col_i]) != ""
            for _, r_cells in sheet.rows[best_idx + 1 :]
        )
        if not any_pop:
            max_populated_col -= 1
        else:
            break
    headers = headers[:max_populated_col]
    return (best_idx, headers)


def classify_row_type(
    row_idx_in_sheet: int,
    cells: list[RawCell],
    header_idx: int | None,
    norm_headers: list[str],
) -> tuple[str, str | None]:
    """Classify a single row into (`blank`, `preamble`, `primary_header`, `repeated_header`, `footer`, `subtotal`, `data`).

    Returns:
        (row_type, detail_or_label)
    """
    if _is_row_blank(cells):
        return ("blank", "empty_row")

    if header_idx is not None:
        if row_idx_in_sheet < header_idx:
            return ("preamble", "before_primary_header")
        if row_idx_in_sheet == header_idx:
            return ("primary_header", "primary_header")

    non_empty_pairs = [
        (i, _cell_str(c)) for i, c in enumerate(cells) if _cell_str(c) != ""
    ]
    if not non_empty_pairs:
        return ("blank", "empty_row")

    # 1. Check repeated header BEFORE subtotal (because some sheets have a column named `Cộng`)
    if norm_headers and len(non_empty_pairs) >= 2:
        c0_i, c0_s = non_empty_pairs[0]
        c1_i, c1_s = non_empty_pairs[1]
        if (
            (c0_i < len(norm_headers) and normalize_comparison_string(c0_s) == norm_headers[c0_i])
            or (c1_i < len(norm_headers) and normalize_comparison_string(c1_s) == norm_headers[c1_i])
        ):
            hdr_matches = 0
            for col_i, val_str in non_empty_pairs:
                if col_i < len(norm_headers) and normalize_comparison_string(val_str) == norm_headers[col_i]:
                    hdr_matches += 1
            if hdr_matches >= 2 and (hdr_matches / len(non_empty_pairs)) >= 0.6:
                return ("repeated_header", "mid_sheet_header")

    # 2. Check footer (`Ghi chú: ...`)
    if len(non_empty_pairs) <= 2:
        for _, val_str in non_empty_pairs:
            v_low = val_str.lower()
            if v_low.startswith("ghi chú:") or v_low.startswith("ghi chu:") or v_low.startswith("lưu ý:"):
                # Confirm no other cell on the row is a valid number
                other_numeric = [
                    s for _, s in non_empty_pairs
                    if s != val_str and parse_money(s).value is not None and parse_money(s).diagnostic is None
                ]
                if not other_numeric:
                    return ("footer", val_str)

    # 3. Check subtotal row
    subtotal_labels: list[tuple[int, str]] = []
    for col_i, val_str in non_empty_pairs:
        if SUBTOTAL_LABEL_RE.match(val_str):
            subtotal_labels.append((col_i, val_str))

    if len(subtotal_labels) == 1:
        lbl_col, lbl_str = subtotal_labels[0]
        other_cells = [(col_i, val_str) for col_i, val_str in non_empty_pairs if col_i != lbl_col]
        if other_cells:
            all_other_numeric = True
            for _, val_str in other_cells:
                m_res = parse_money(val_str)
                if m_res.value is None or m_res.diagnostic is not None:
                    all_other_numeric = False
                    break
            if all_other_numeric:
                return ("subtotal", lbl_str)

    return ("data", None)


# ---------------------------------------------------------------------------
# 3. Logical Table Grouping & Grain Inference
# ---------------------------------------------------------------------------

_PERIOD_SHEET_RE = re.compile(
    r"^(?:tháng\s*|thang\s*|t)?\s*(\d{1,2})[\s._/-]+(20\d{2})$"
    r"|^(20\d{2})[\s._/-]+(\d{1,2})$",
    re.IGNORECASE,
)

_FILE_PERIOD_SUFFIX_RE = re.compile(
    r"_(?:thang|t)?(\d{1,2})_(20\d{2})$"
    r"|_(20\d{2})$"
    r"|_(\d+)$",
    re.IGNORECASE,
)


def parse_period_from_name(name: str) -> tuple[int | None, int | None, int | None]:
    """Extract (year, month, chunk_index) from a sheet name or file stem."""
    s = normalize_display_string(name)
    m_sh = _PERIOD_SHEET_RE.match(s)
    if m_sh:
        if m_sh.group(1) and m_sh.group(2):
            return (int(m_sh.group(2)), int(m_sh.group(1)), None)
        return (int(m_sh.group(3)), int(m_sh.group(4)), None)

    m_f = re.search(r"(?:thang|tháng|t)\s*(\d{1,2})[\s._/-]+(20\d{2})", s, re.IGNORECASE)
    if m_f:
        return (int(m_f.group(2)), int(m_f.group(1)), None)

    m_ym = re.search(r"(?<!\d)(\d{1,2})[._/-](20\d{2})(?!\d)", s)
    if m_ym and 1 <= int(m_ym.group(1)) <= 12:
        return (int(m_ym.group(2)), int(m_ym.group(1)), None)

    m_y = re.search(r"_(20\d{2})$", s)
    if m_y:
        return (int(m_y.group(1)), None, None)

    m_c = re.search(r"_(\d+)$", s)
    if m_c:
        return (None, None, int(m_c.group(1)))

    return (None, None, None)


def canonical_logical_table_name(file_name: str, sheet_name: str, total_sheets_in_file: int) -> str:
    """Determine the candidate logical table name for a (file_name, sheet_name) pair."""
    stem = Path(file_name).stem
    base_stem = re.sub(
        r"(?:_(?:thang|t)?\d{1,2}_20\d{2}|_20\d{2}|_\d+)$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    sh_norm = normalize_comparison_string(sheet_name)

    # Generic sheet names or period-named sheets belong to the workbook's base_stem table
    if (
        _PERIOD_SHEET_RE.match(normalize_display_string(sheet_name))
        or sh_norm in {"sheet1", "sheet 1", "sổ bán hàng", "so ban hang"}
        or total_sheets_in_file == 1
    ):
        return base_stem
    return sheet_name


def infer_table_grain(
    logical_name: str,
    headers: list[str],
    roles: list[str],
    meta: BusinessMetadata,
) -> str:
    """Classify the table grain so fill-down, deduplication, and return rules apply only where valid."""
    ln_norm = normalize_comparison_string(logical_name)
    role_set = set(roles)
    hdr_norms = {normalize_comparison_string(h) for h in headers}

    # 1. Unit conversion / alias tables
    if (
        any(k in ln_norm for k in ("quydoi", "quy doi", "quy_doi", "donvitinh", "don vi tinh", "don_vi", "don vi", "ghi tắt", "ghi tat"))
        or "conversion_factor" in role_set
        or ("tên gọi khác" in hdr_norms or "ghi tắt" in hdr_norms)
    ):
        if "line_amount" not in role_set and "order_total" not in role_set:
            return "conversion"

    # 2. Lot allocation / lot return tables (e.g. DonHang_Lo, DanhSachHoaDonChiTietLo)
    if (
        any(k in ln_norm for k in ("donhang_lo", "donhang lo", "chitietlo", "chi_tiet_lo", "chi tiet lo", "chi tiet don lo"))
        or ("return_qty" in role_set and any(h in hdr_norms for h in ("lô", "mã lô", "so_lo", "so lo")))
    ):
        return "lot_allocation"

    # 3. Stock / inventory movement or snapshot tables
    if any(k in ln_norm for k in ("the_kho", "the kho", "thekho", "tonkho", "ton_kho", "ton kho", "baocaotonkho", "lohang", "lo_hang", "lo hang")) or "stock_qty" in role_set:
        return "stock"

    # 4. Payment / cashbook / debt ledger tables
    if any(k in ln_norm for k in ("thanh_toan", "thanh toan", "thanhtoan", "soquy", "so_quy", "so quy", "phieuthu", "phieu thu", "hanno", "han_no", "han no", "cong no", "cong_no", "công nợ", "sổ nợ", "trả góp")):
        return "payment"

    # 5. Delivery tables
    if any(k in ln_norm for k in ("giaohang", "giao_hang", "giao hang", "phiếu giao")):
        return "delivery"

    # 6. Purchase tables (including trade-in / used-device buyback tables)
    if any(k in ln_norm for k in ("nhaphang", "nhap_hang", "nhap hang", "phieu nhap", "chi tiet nhap", "thumaycu", "thu_may_cu", "thu may cu")):
        if "item_id" in role_set or "item_name" in role_set:
            return "purchase_line"
        return "purchase_header"

    # A table with an order key, a date and money is a transaction ledger whatever its
    # name contains: "BanHang_HoaDon" holds the substring "hang hoa" but is not a catalog.
    is_transaction_shape = (
        "order_id" in role_set
        and "date" in role_set
        and bool(role_set & {"line_amount", "order_total", "paid_amount"})
    )

    # 7. Catalog / entity tables (products, customers, medicines, staff, promotions, room/table info)
    if not is_transaction_shape and any(
        k in ln_norm
        for k in (
            "sanpham",
            "san_pham",
            "san pham",
            "hanghoa",
            "hang_hoa",
            "hang hoa",
            "hàng hóa",
            "danhmuc",
            "danh_muc",
            "danh muc",
            "khachhang",
            "khach_hang",
            "khach hang",
            "thuoc",
            "danhsachtho",
            "lichsugia",
            "lich su gia",
            "banggia",
            "bang gia",
            "khuyenmai",
            "phongban",
            "bảo hành",
        )
    ):
        return "catalog"

    # 8. Sales line vs Order header tables
    has_item = ("item_id" in role_set or "item_name" in role_set or "qty" in role_set)
    has_money = ("line_amount" in role_set or "order_total" in role_set or "unit_price" in role_set)
    if has_item and has_money:
        return "sales_line"
    if ("order_id" in role_set and has_money) or any(k in ln_norm for k in ("don_hang", "don hang", "donhang", "hoadon", "hoa_don", "hoa don", "ban_hang", "ban hang", "banhang", "sosuaxe")):
        if has_item or "sosuaxe" in ln_norm:
            return "sales_line"
        return "order_header"

    return "other"


# ---------------------------------------------------------------------------
# 4. Row Role Classification (ITEM, SERVICE_FEE, SHIPPING, ORDER_DISCOUNT)
# ---------------------------------------------------------------------------

def classify_sales_row_role(
    item_name: str | None,
    line_amount: int | None,
    qty: Decimal | None,
) -> str:
    """Assign `_row_role` (`ITEM`, `SERVICE_FEE`, `SHIPPING`, `ORDER_DISCOUNT`) per SKILL.md §Row roles."""
    if not item_name:
        return "ITEM"
    norm = normalize_comparison_string(item_name)

    if norm in {"phí phục vụ", "phi phuc vu"} or norm.startswith("phí phục vụ"):
        return "SERVICE_FEE"

    if norm in {"phí ship", "phi ship", "phí giao hàng", "phí vận chuyển"} or norm.startswith("phí ship"):
        return "SHIPPING"

    if (
        norm == "voucher"
        or norm.startswith("voucher ")
        or re.match(r"^dùng\s+\d+\s+điểm$", norm)
        or norm.startswith("giảm giá")
        or norm.startswith("chiết khấu")
    ):
        return "ORDER_DISCOUNT"

    return "ITEM"


# ---------------------------------------------------------------------------
# 5. Main Per-Business Ingestion Pipeline
# ---------------------------------------------------------------------------

def _sanitize_sql_identifier(name: str, used: set[str], fallback_idx: int) -> str:
    cleaned = normalize_display_string(name)
    cleaned = re.sub(r"[^\w\sÀ-ỹ]", "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{fallback_idx}_{cleaned}" if cleaned else f"col_{fallback_idx}"
    cand = cleaned
    counter = 2
    while cand.lower() in used or cand.startswith("_"):
        cand = f"{cleaned}_{counter}"
        counter += 1
    used.add(cand.lower())
    return cand


@dataclass
class _SheetExecutionUnit:
    sheet: RawSheetData
    header_idx: int | None
    headers: list[str]
    roles: list[str]
    primary_date_flags: list[bool]
    glosses: list[str | None]
    context_year: int | None
    context_month: int | None
    sort_key: tuple[Any, ...]


_INFERENCE_GRAINS = {"sales_line", "order_header", "purchase_line", "purchase_header", "other"}


def _data_rows(sheet: RawSheetData, header_idx: int | None, headers: list[str]) -> list[list[RawCell]]:
    norm = [normalize_comparison_string(h) for h in headers]
    return [
        cells[: len(headers)]
        for r_idx, (_, cells) in enumerate(sheet.rows)
        if classify_row_type(r_idx, cells, header_idx, norm)[0] == "data"
    ]


def _collect_catalog_evidence(raw_sheets: list[tuple[RawSheetData, int]], meta: BusinessMetadata) -> dict[str, set[str]]:
    """Product codes, product names and customer names listed in the business's own reference tables."""
    evidence: dict[str, set[str]] = {"item_id": set(), "item_name": set(), "customer": set()}
    for sheet, total_in_file in raw_sheets:
        hdr_idx, headers = detect_header_row(sheet, meta)
        roles = [
            infer_column_role(h, meta.lookup_column_gloss(sheet.source_file, sheet.source_sheet, h), stt_is_order_id=meta.stt_is_order_id)[0]
            for h in headers
        ]
        logical = canonical_logical_table_name(sheet.source_file, sheet.source_sheet, total_in_file)
        if infer_table_grain(logical, headers, roles, meta) not in {"catalog", "conversion", "stock"}:
            continue
        for r in _data_rows(sheet, hdr_idx, headers):
            for i, role in enumerate(roles):
                if role in evidence and i < len(r) and _cell_str(r[i]):
                    evidence[role].add(fold_key(_cell_str(r[i])))
    return evidence


def _apply_value_inference(
    sheet: RawSheetData,
    hdr_idx: int | None,
    headers: list[str],
    roles: list[str],
    p_dates: list[bool],
    glosses: list[str | None],
    ctx_year: int | None,
    logical: str,
    meta: BusinessMetadata,
    catalog_evidence: dict[str, set[str]],
) -> tuple[list[str], list[bool], bool]:
    """Propose roles for unexplained columns, keep only those with independent support, and log both outcomes."""
    if not headers or infer_table_grain(logical, headers, roles, meta) not in _INFERENCE_GRAINS:
        return roles, p_dates, False
    if all(r != "other" or g for r, g in zip(roles, glosses)):
        return roles, p_dates, False

    rows = _data_rows(sheet, hdr_idx, headers)
    if not rows:
        return roles, p_dates, False
    new_roles, reasons = infer_roles_from_values(headers, roles, glosses, rows, context_year=ctx_year)

    if not arithmetic_holds_on_all_rows(new_roles, rows):
        reasons.append("UNRESOLVED amount: quantity × price = amount fails on rows outside the sample")
        new_roles = [
            old if (new in {"qty", "unit_price", "line_amount"} and old == "other") else new
            for old, new in zip(roles, new_roles)
        ]

    for role in ("item_id", "item_name", "customer"):
        if role not in new_roles:
            hit = match_catalog_column(rows, new_roles, glosses, catalog_evidence[role])
            if hit is not None:
                new_roles[hit] = role
                reasons.append(f"{headers[hit]} -> {role}: values match the business's own list")

    def documented_factor(item: str, unit: str) -> Decimal | None:
        # Only a rule the source documents counts. "No rule" must not read as factor 1 here.
        factor, how = meta.resolve_unit_factor(fold_unit(unit), item_name=item)
        return factor if how in {"name_unit_match", "code_unit_match"} else None

    if "base_qty" not in new_roles:
        hit = find_documented_base_qty(rows, new_roles, glosses, documented_factor)
        if hit is not None:
            new_roles[hit] = "base_qty"
            reasons.append(f"{headers[hit]} -> base_qty: equals quantity × documented conversion on every row")

    if infer_table_grain(logical, headers, new_roles, meta) == "other":
        new_roles = list(roles)  # Nothing transactional was established, so nothing is committed.
        reasons = [r for r in reasons if r.startswith("UNRESOLVED")]

    source = f"{sheet.source_file}:{sheet.source_sheet}"
    for reason in reasons:
        code = "ROLE_UNRESOLVED" if reason.startswith("UNRESOLVED") else "ROLE_INFERRED_FROM_VALUES"
        meta.diagnostics.append(Diagnostic(code, f"{source}: {reason}", source, severity="warning"))
    new_dates = [
        was or (new == "date" and old != "date")
        for was, old, new in zip(p_dates, roles, new_roles)
    ]
    return new_roles, new_dates, new_roles != roles


def fold_unit(unit: str) -> str:
    return normalize_display_string(unit).lower()


def ingest_business_folder(
    biz_dir: str | Path,
    db_path: str | Path,
) -> IngestionReport:
    """Ingest all data files in `biz_dir` into a normalized SQLite database at `db_path`."""
    b_path = Path(biz_dir)
    final_db = Path(db_path)
    final_db.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the target and rename at the end so a crash never leaves a partial DB
    # that a later run would mistake for a finished one.
    out_db = final_db.with_name(final_db.name + ".building")
    for stale in (out_db, Path(f"{out_db}-wal"), Path(f"{out_db}-shm")):
        if stale.exists():
            stale.unlink()

    meta = parse_readme_metadata(b_path / "README.md", business_id=b_path.name)
    report = IngestionReport(
        business_id=b_path.name,
        db_path=final_db,
        metadata=meta,
    )

    # 1. Discover and read all raw files in deterministic order
    raw_sheets: list[ tuple[RawSheetData, int] ] = []
    for f_path in sorted(b_path.iterdir(), key=lambda x: x.name):
        if f_path.name.startswith(".") or f_path.name.lower() == "readme.md":
            continue
        suffix = f_path.suffix.lower()
        if suffix == ".xlsx":
            wb_sheets = read_xlsx_workbook(f_path)
            for s in wb_sheets:
                raw_sheets.append((s, len(wb_sheets)))
        elif suffix == ".csv":
            raw_sheets.append((read_csv_file(f_path), 1))
        elif suffix in {".db", ".sqlite", ".sqlite3"}:
            db_sheets = read_sqlite_file(f_path)
            for s in db_sheets:
                raw_sheets.append((s, len(db_sheets)))

    catalog_evidence = _collect_catalog_evidence(raw_sheets, meta)

    # 2. Detect headers, roles, context years/months, and group into logical tables
    logical_groups: dict[str, list[_SheetExecutionUnit]] = {}
    grain_by_logical: dict[str, str] = {}

    for sheet, total_in_file in raw_sheets:
        hdr_idx, headers = detect_header_row(sheet, meta)
        # Preamble text above header for context year extraction
        preamble_texts: list[str] = []
        if hdr_idx is not None and hdr_idx > 0:
            for r_i in range(hdr_idx):
                _, r_cells = sheet.rows[r_i]
                preamble_texts.append(" ".join(_cell_str(c) for c in r_cells if _cell_str(c)))
        title_text = " | ".join(preamble_texts)

        sh_y, sh_m, sh_chunk = parse_period_from_name(sheet.source_sheet)
        f_y, f_m, f_chunk = parse_period_from_name(Path(sheet.source_file).stem)
        ctx_year = extract_context_year(
            sheet_name=sheet.source_sheet,
            file_name=sheet.source_file,
            title_rows_text=title_text,
            fallback_year=sh_y or f_y or meta.coverage_year,
        )
        ctx_month = sh_m or f_m

        glosses: list[str | None] = []
        roles: list[str] = []
        p_dates: list[bool] = []
        for h in headers:
            g = meta.lookup_column_gloss(sheet.source_file, sheet.source_sheet, h)
            role, is_p = infer_column_role(h, g, stt_is_order_id=meta.stt_is_order_id)
            glosses.append(g)
            roles.append(role)
            p_dates.append(is_p)

        cand_logical = canonical_logical_table_name(
            sheet.source_file, sheet.source_sheet, total_in_file
        )
        roles, p_dates, inferred = _apply_value_inference(
            sheet, hdr_idx, headers, roles, p_dates, glosses, ctx_year, cand_logical, meta, catalog_evidence
        )
        grain = infer_table_grain(cand_logical, headers, roles, meta)

        # Ensure non-sales tables do not accidentally treat generic `Trạng thái` as order `status`
        if grain not in {"sales_line", "order_header"}:
            roles = [
                ("purchase_status" if grain in {"purchase_header", "purchase_line"} else "entity_status")
                if r == "status"
                else r
                for r in roles
            ]

        # Disambiguate generic `discount` role and unglossed `Tổng tiền` on `sales_line` vs `order_header`
        if grain == "order_header":
            roles = ["order_discount" if r == "discount" else r for r in roles]
        elif grain == "sales_line":
            roles = ["line_discount" if r == "discount" else r for r in roles]
            if "line_amount" not in roles:
                for idx_r, (h_r, g_r, r_r) in enumerate(zip(headers, glosses, roles)):
                    if (
                        r_r == "order_total"
                        and g_r is None
                        and normalize_comparison_string(h_r) in {"tổng tiền", "tong tien", "tong_tien"}
                    ):
                        roles[idx_r] = "line_amount"
                        break

        # Validate compatibility if cand_logical already exists (check column count and normalized headers)
        group_key = cand_logical
        if group_key in logical_groups:
            existing_u = logical_groups[group_key][0]
            ex_hdr_norm = [normalize_comparison_string(h) for h in existing_u.headers]
            cur_hdr_norm = [normalize_comparison_string(h) for h in headers]
            # Equal headers do not prove equal meaning when roles came from values.
            roles_differ = inferred and existing_u.roles != roles
            if ex_hdr_norm != cur_hdr_norm or grain_by_logical[group_key] != grain or roles_differ:
                group_key = f"{cand_logical}__{Path(sheet.source_file).stem}__{sheet.source_sheet}"

        logical_groups.setdefault(group_key, [])
        grain_by_logical[group_key] = grain

        # Chronological sort key for period sheets / chunks
        eff_y = sh_y or f_y or 9999
        eff_m = sh_m or f_m or 99
        eff_c = sh_chunk or f_chunk or 0
        sort_key = (eff_y, eff_m, eff_c, sheet.source_file, len(logical_groups[group_key]))

        logical_groups[group_key].append(
            _SheetExecutionUnit(
                sheet=sheet,
                header_idx=hdr_idx,
                headers=headers,
                roles=roles,
                primary_date_flags=p_dates,
                glosses=glosses,
                context_year=ctx_year,
                context_month=ctx_month,
                sort_key=sort_key,
            )
        )

    for g_key in logical_groups:
        logical_groups[g_key].sort(key=lambda u: u.sort_key)

    # 3. First pass on `conversion` and `catalog` tables to populate unit conversions & item aliases
    for g_key, units in logical_groups.items():
        grain = grain_by_logical[g_key]
        if grain in {"conversion", "catalog"}:
            for u in units:
                norm_hdrs = [normalize_comparison_string(h) for h in u.headers]
                data_rows_raw: list[list[Any]] = []
                for r_idx, (_, cells) in enumerate(u.sheet.rows):
                    r_type, _ = classify_row_type(r_idx, cells, u.header_idx, norm_hdrs)
                    if r_type == "data":
                        data_rows_raw.append([_cell_str(c) or None for c in cells[: len(u.headers)]])
                register_conversion_table_rows(
                    meta,
                    u.headers,
                    data_rows_raw,
                    source_name=f"{u.sheet.source_file}:{u.sheet.source_sheet}",
                )

    # 4. Second pass: collect cross-table return evidence (lot return quantities, stock card returns)
    # Keyed by raw order ID (e.g. 'ORD000123') -> list of (evidence_source, return_date_or_None)
    external_order_returns: dict[str, list[tuple[str, str | None]]] = {}
    for g_key, units in logical_groups.items():
        grain = grain_by_logical[g_key]
        if grain == "lot_allocation":
            for u in units:
                norm_hdrs = [normalize_comparison_string(h) for h in u.headers]
                oid_idx = next((i for i, r in enumerate(u.roles) if r == "order_id"), None)
                ret_idx = next((i for i, r in enumerate(u.roles) if r == "return_qty"), None)
                if oid_idx is None or ret_idx is None:
                    continue
                for r_idx, (_, cells) in enumerate(u.sheet.rows):
                    r_type, _ = classify_row_type(r_idx, cells, u.header_idx, norm_hdrs)
                    if r_type != "data":
                        continue
                    oid_val = _cell_str(cells[oid_idx]) if oid_idx < len(cells) else ""
                    ret_val = _cell_str(cells[ret_idx]) if ret_idx < len(cells) else ""
                    if oid_val and ret_val:
                        q_res = parse_quantity(ret_val)
                        if q_res.value is not None and q_res.value > 0:
                            external_order_returns.setdefault(oid_val, []).append(
                                (f"lot_return:{u.sheet.source_file}", None)
                            )
        elif grain == "stock":
            # Check if stock ledger records customer return vouchers (e.g. the_kho with Loaigd='NHAP_TRA' or GhiChu mentioning trả)
            for u in units:
                norm_hdrs = [normalize_comparison_string(h) for h in u.headers]
                for r_idx, (_, cells) in enumerate(u.sheet.rows):
                    r_type, _ = classify_row_type(r_idx, cells, u.header_idx, norm_hdrs)
                    if r_type != "data":
                        continue
                    # Stock card returns have their own voucher code (e.g. PT...), preserved in the stock table itself

    # 5. Open SQLite database and create audit tables
    conn = sqlite3.connect(str(out_db))
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE _ingested_tables (
            source_file TEXT,
            source_sheet TEXT,
            logical_table TEXT,
            table_grain TEXT,
            raw_row_count INTEGER,
            kept_original_count INTEGER,
            preamble_count INTEGER,
            primary_header_count INTEGER,
            repeated_header_count INTEGER,
            subtotal_count INTEGER,
            footer_count INTEGER,
            blank_count INTEGER,
            removed_duplicate_count INTEGER,
            quarantined_count INTEGER,
            derived_component_count INTEGER,
            context_year INTEGER,
            context_month INTEGER
        );

        CREATE TABLE _ingested_columns (
            logical_table TEXT,
            col_index INTEGER,
            raw_header TEXT,
            sql_col_name TEXT,
            role TEXT,
            is_primary_date INTEGER,
            readme_gloss TEXT
        );

        CREATE TABLE _ingested_row_audit (
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            logical_table TEXT,
            disposition TEXT,
            reason TEXT,
            canonical_source_file TEXT,
            canonical_source_sheet TEXT,
            canonical_source_row INTEGER
        );

        CREATE TABLE _subtotal_reconciliation (
            logical_table TEXT,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            subtotal_label TEXT,
            subtotal_kind TEXT,
            target_col TEXT,
            expected_amount INTEGER,
            computed_amount INTEGER,
            delta INTEGER
        );

        CREATE TABLE _ingestion_diagnostics (
            code TEXT,
            severity TEXT,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            column_name TEXT,
            raw_value TEXT,
            message TEXT
        );

        CREATE TABLE _business_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE _unit_rules (
            item_name TEXT,
            item_name_norm TEXT,
            item_code TEXT,
            from_unit TEXT,
            factor TEXT,
            to_unit TEXT,
            source TEXT
        );

        CREATE TABLE _item_aliases (
            alias_norm TEXT PRIMARY KEY,
            canonical_name TEXT
        );
        """
    )

    # Save business metadata flags
    meta_kv = {
        "business_id": meta.business_id,
        "coverage_end_date": meta.coverage_end_date or "",
        "coverage_year": str(meta.coverage_year or ""),
        "stt_is_order_id": "1" if meta.stt_is_order_id else "0",
        "notes_has_status": "1" if meta.notes_has_status else "0",
        "notes_has_return": "1" if meta.notes_has_return else "0",
        "negative_qty_is_return": "1" if meta.negative_qty_is_return else "0",
        "inline_topping_enabled": "1" if meta.inline_topping_enabled else "0",
        "non_item_rows_enabled": "1" if meta.non_item_rows_enabled else "0",
        "service_fee_row_enabled": "1" if meta.service_fee_row_enabled else "0",
        "unit_rules_count": str(len(meta.unit_rules)),
        "readme_text": meta.readme_text,
        "input_hash": input_fingerprint(b_path),
        "pipeline_hash": pipeline_fingerprint(),
    }
    cur.executemany(
        "INSERT INTO _business_metadata (key, value) VALUES (?, ?)",
        list(meta_kv.items()),
    )

    cur.executemany(
        "INSERT INTO _unit_rules VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r.item_name, r.item_name_norm, r.item_code, r.from_unit_norm, str(r.factor), r.to_unit_norm, r.source)
            for r in meta.unit_rules
        ],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO _item_aliases VALUES (?, ?)",
        list(meta.alias_norm_to_canonical_name.items()),
    )

    for d in meta.diagnostics:
        report.diagnostics.append(d)
        cur.execute(
            "INSERT INTO _ingestion_diagnostics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (d.code, d.severity, "README.md", "", 0, "", "", d.message),
        )

    # 6. Process each logical table group
    used_table_names: set[str] = set()
    for g_key, units in logical_groups.items():
        grain = grain_by_logical[g_key]
        sql_table = _sanitize_sql_identifier(g_key, used_table_names, len(used_table_names) + 1)

        first_u = units[0]
        headers = first_u.headers
        roles = first_u.roles
        p_dates = first_u.primary_date_flags
        glosses = first_u.glosses
        norm_hdrs = [normalize_comparison_string(h) for h in headers]

        used_cols: set[str] = set()
        sql_cols: list[str] = []
        for c_i, h in enumerate(headers):
            sc = _sanitize_sql_identifier(h, used_cols, c_i + 1)
            sql_cols.append(sc)
            cur.execute(
                "INSERT INTO _ingested_columns VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sql_table, c_i, h, sc, roles[c_i], 1 if p_dates[c_i] else 0, glosses[c_i] or ""),
            )

        _process_logical_table_group(
            cur=cur,
            report=report,
            meta=meta,
            sql_table=sql_table,
            grain=grain,
            units=units,
            headers=headers,
            norm_hdrs=norm_hdrs,
            sql_cols=sql_cols,
            roles=roles,
            p_dates=p_dates,
            external_order_returns=external_order_returns,
        )

    conn.commit()
    conn.close()
    out_db.replace(final_db)
    return report


_ITEM_LEVEL_ROLES = {
    "line_seq",
    "item_id",
    "item_name",
    "qty",
    "base_qty",
    "return_qty",
    "unit",
    "base_unit",
    "conversion_factor",
    "unit_price",
    "cost_price",
    "line_amount",
    "line_discount",
    "stock_qty",
}

_ITEM_LEVEL_HEADER_NORMS = {
    "imei",
    "lô",
    "mã lô",
    "so_lo",
    "mặt hàng",
    "phân loại",
    "nhóm hàng",
    "loại hàng",
    "nhóm hàng hóa",
    "loại sản phẩm",
    "hoạt chất",
    "thuốc kê đơn",
    "làm gì",
}

_ORDER_AMOUNT_ROLES = {
    "order_total",
    "order_discount",
    "shipping_fee",
    "paid_amount",
    "debt_amount",
}

_MONEY_ROLES = {
    "line_amount",
    "order_total",
    "unit_price",
    "cost_price",
    "line_discount",
    "order_discount",
    "discount",
    "shipping_fee",
    "paid_amount",
    "debt_amount",
}


@dataclass
class _ProcessedDataRow:
    unit_idx: int
    source_file: str
    source_sheet: str
    source_row: int
    context_year: int | None
    context_month: int | None
    date1904: bool
    raw_cells: list[RawCell]
    filled_strs: list[str]
    effective_order_key: str
    raw_order_id: str
    is_derived_component: bool = False
    parent_source_row: int | None = None
    override_item_name: str | None = None
    override_qty: Decimal | None = None
    override_unit_price: int | None = None
    override_line_amount: int | None = None


def _process_logical_table_group(
    *,
    cur: sqlite3.Cursor,
    report: IngestionReport,
    meta: BusinessMetadata,
    sql_table: str,
    grain: str,
    units: list[_SheetExecutionUnit],
    headers: list[str],
    norm_hdrs: list[str],
    sql_cols: list[str],
    roles: list[str],
    p_dates: list[bool],
    external_order_returns: dict[str, list[tuple[str, str | None]]],
) -> None:
    """Process all sheets/chunks belonging to one logical table and write to SQLite."""
    num_cols = len(headers)
    is_sales_or_order = grain in {"sales_line", "order_header"}
    order_id_idx = next((i for i, r in enumerate(roles) if r == "order_id"), None)
    has_order_grouping = is_sales_or_order and (order_id_idx is not None)

    # Identify which columns are order-level inherited vs item-level
    is_inherited_col: list[bool] = []
    for i in range(num_cols):
        if not has_order_grouping:
            is_inherited_col.append(False)
        elif roles[i] in _ITEM_LEVEL_ROLES or norm_hdrs[i] in _ITEM_LEVEL_HEADER_NORMS:
            is_inherited_col.append(False)
        else:
            is_inherited_col.append(True)

    # Track order state across rows (and across continuation chunks when applicable)
    current_order_key: str | None = None
    current_raw_oid: str = ""
    current_order_inherited: list[str] = [""] * num_cols
    current_order_seen_tuples: dict[tuple[str, ...], tuple[str, str, int]] = {}
    current_order_amounts: dict[int, tuple[int, int]] = {}  # col_idx -> (parsed_vnd, first_source_row)

    # Subtotal accumulators (col_idx -> integer VND sum)
    day_accum: dict[int, int] = {}
    month_accum: dict[int, int] = {}

    kept_rows: list[_ProcessedDataRow] = []
    audit_records: list[tuple[Any, ...]] = []

    def _log_diag(
        code: str,
        severity: str,
        sf: str,
        ss: str,
        sr: int,
        col_n: str,
        raw_v: str,
        msg: str,
    ) -> None:
        d = Diagnostic(
            code=code,
            message=msg,
            raw=raw_v,
            severity=severity,
            context={
                "source_file": sf,
                "source_sheet": ss,
                "source_row": sr,
                "column": col_n,
                "raw": raw_v,
            },
        )
        report.diagnostics.append(d)
        cur.execute(
            "INSERT INTO _ingestion_diagnostics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, severity, sf, ss, sr, col_n, raw_v, msg),
        )

    for u_idx, u in enumerate(units):
        sf = u.sheet.source_file
        ss = u.sheet.source_sheet
        summary = SheetAccountingSummary(
            source_file=sf,
            source_sheet=ss,
            logical_table=sql_table,
            table_grain=grain,
            raw_row_count=len(u.sheet.rows),
            context_year=u.context_year,
            context_month=u.context_month,
        )

        # If this unit is a distinct monthly sheet (not a `_1`/`_2` chunk continuation),
        # reset subtotal accumulators and order state at the sheet boundary.
        is_period_sheet = bool(_PERIOD_SHEET_RE.match(normalize_display_string(ss)))
        if u_idx > 0 and is_period_sheet:
            day_accum.clear()
            month_accum.clear()
            current_order_key = None
            current_raw_oid = ""
            current_order_inherited = [""] * num_cols
            current_order_seen_tuples.clear()
            current_order_amounts.clear()

        for r_idx, (src_row_num, cells) in enumerate(u.sheet.rows):
            # Pad or slice cells to num_cols for data access, but classify using full cells
            r_type, r_detail = classify_row_type(r_idx, cells, u.header_idx, norm_hdrs)

            if r_type == "blank":
                summary.blank_count += 1
                audit_records.append((sf, ss, src_row_num, sql_table, "blank", "empty_row", None, None, None))
                continue
            if r_type == "preamble":
                summary.preamble_count += 1
                audit_records.append((sf, ss, src_row_num, sql_table, "preamble", r_detail or "preamble", None, None, None))
                continue
            if r_type == "primary_header":
                summary.primary_header_count += 1
                audit_records.append((sf, ss, src_row_num, sql_table, "primary_header", "primary_header", None, None, None))
                continue
            if r_type == "repeated_header":
                summary.repeated_header_count += 1
                audit_records.append((sf, ss, src_row_num, sql_table, "repeated_header", "mid_sheet_header", None, None, None))
                # Do NOT close current_order_key across a mid-sheet repeated header!
                continue
            if r_type == "footer":
                summary.footer_count += 1
                audit_records.append((sf, ss, src_row_num, sql_table, "footer", r_detail or "footer", None, None, None))
                continue
            if r_type == "subtotal":
                summary.subtotal_count += 1
                lbl_str = r_detail or "subtotal"
                audit_records.append((sf, ss, src_row_num, sql_table, "subtotal", lbl_str, None, None, None))

                sub_kind = "day" if _DAILY_SUBTOTAL_RE.match(lbl_str) else "month"
                non_empty_pairs = [
                    (i, _cell_str(c)) for i, c in enumerate(cells[:num_cols]) if _cell_str(c) != ""
                ]
                numeric_Targets = [
                    (i, val_s)
                    for i, val_s in non_empty_pairs
                    if not SUBTOTAL_LABEL_RE.match(val_s)
                ]
                # Filter to monetary columns if any monetary column is populated on the subtotal row
                money_targets = [
                    (i, val_s) for i, val_s in numeric_Targets if roles[i] in _MONEY_ROLES
                ]
                targets_to_check = money_targets if money_targets else numeric_Targets

                # For sales_line tables with multiple numeric cells, prioritize line_amount;
                # for order_header tables, check the primary order total / goods value column or all money columns.
                if grain == "sales_line":
                    la_targets = [(i, val_s) for i, val_s in targets_to_check if roles[i] == "line_amount"]
                    if la_targets:
                        targets_to_check = la_targets
                elif grain == "order_header" and len(targets_to_check) > 1:
                    ot_targets = [(i, val_s) for i, val_s in targets_to_check if roles[i] in {"order_total", "line_amount"}]
                    if ot_targets:
                        targets_to_check = [ot_targets[0]]

                for col_i, val_s in targets_to_check:
                    m_res = parse_money(val_s)
                    if m_res.value is not None:
                        exp_val = round_money_vnd(m_res.value)
                        comp_val = (
                            day_accum.get(col_i, 0)
                            if sub_kind == "day"
                            else month_accum.get(col_i, 0)
                        )
                        chk = SubtotalCheckResult(
                            logical_table=sql_table,
                            source_file=sf,
                            source_sheet=ss,
                            source_row=src_row_num,
                            subtotal_label=lbl_str,
                            subtotal_kind=sub_kind,
                            target_col=headers[col_i],
                            expected_amount=exp_val,
                            computed_amount=comp_val,
                            delta=comp_val - exp_val,
                        )
                        report.subtotal_checks.append(chk)
                        cur.execute(
                            "INSERT INTO _subtotal_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                chk.logical_table,
                                chk.source_file,
                                chk.source_sheet,
                                chk.source_row,
                                chk.subtotal_label,
                                chk.subtotal_kind,
                                chk.target_col,
                                chk.expected_amount,
                                chk.computed_amount,
                                chk.delta,
                            ),
                        )

                if sub_kind == "day":
                    day_accum.clear()
                else:
                    month_accum.clear()
                    day_accum.clear()
                continue

            # Data row processing
            padded_cells = [
                cells[i] if i < len(cells) else RawCell(value=None)
                for i in range(num_cols)
            ]
            raw_strs = [_cell_str(c) for c in padded_cells]

            # Log Excel formula error cells if any
            for c_i, rc in enumerate(padded_cells):
                if rc.is_error_cell:
                    err_sev = "warning" if roles[c_i] == "other" else "error"
                    _log_diag(
                        "EXCEL_ERROR_CELL",
                        err_sev,
                        sf,
                        ss,
                        src_row_num,
                        headers[c_i],
                        raw_strs[c_i],
                        f"Excel formula error cell {raw_strs[c_i]!r}",
                    )

            if has_order_grouping and order_id_idx is not None:
                row_oid = raw_strs[order_id_idx]
                if row_oid == "":
                    # Continuation row of the current open order
                    if current_order_key is None:
                        current_raw_oid = f"__BLANK_ORD_{sf}_{ss}_{src_row_num}"
                        current_order_key = f"{ss}::{current_raw_oid}"
                        current_order_inherited = [
                            raw_strs[i] if is_inherited_col[i] else "" for i in range(num_cols)
                        ]
                        current_order_seen_tuples.clear()
                        current_order_amounts.clear()
                elif row_oid != current_raw_oid or current_order_key is None:
                    # Start of a brand-new order: initialize inherited fields strictly from this row
                    current_raw_oid = row_oid
                    scope_prefix = f"{ss}::" if (meta.stt_is_order_id or is_period_sheet) else ""
                    current_order_key = f"{scope_prefix}{row_oid}"
                    current_order_inherited = [
                        raw_strs[i] if is_inherited_col[i] else "" for i in range(num_cols)
                    ]
                    current_order_seen_tuples.clear()
                    current_order_amounts.clear()

                # Fill blank order-level columns from the current order's first row
                filled_strs: list[str] = []
                for i in range(num_cols):
                    if raw_strs[i] != "":
                        filled_strs.append(raw_strs[i])
                    elif is_inherited_col[i]:
                        filled_strs.append(current_order_inherited[i])
                    else:
                        filled_strs.append("")

                # Check order-scoped exact duplicate (using raw strings + filled inherited strings)
                # Wait: check both raw tuple and filled tuple so identical rows in the same order are caught
                dedup_key = tuple(filled_strs)
                if dedup_key in current_order_seen_tuples:
                    orig_sf, orig_ss, orig_sr = current_order_seen_tuples[dedup_key]
                    summary.removed_duplicate_count += 1
                    audit_records.append(
                        (
                            sf,
                            ss,
                            src_row_num,
                            sql_table,
                            "removed_duplicate",
                            "exact_duplicate_within_order",
                            orig_sf,
                            orig_ss,
                            orig_sr,
                        )
                    )
                    continue
                current_order_seen_tuples[dedup_key] = (sf, ss, src_row_num)

                # Validate repeated order-level monetary columns within the same order
                # (skipping negative-quantity return rows whose order total is intentionally negated)
                row_qty_idx = next((idx_q for idx_q, r_q in enumerate(roles) if r_q == "qty"), None)
                is_neg_qty_row = False
                if row_qty_idx is not None and raw_strs[row_qty_idx] != "":
                    q_chk = parse_quantity(raw_strs[row_qty_idx])
                    if q_chk.value is not None and q_chk.value < 0:
                        is_neg_qty_row = True

                if not is_neg_qty_row:
                    for i in range(num_cols):
                        if roles[i] in _ORDER_AMOUNT_ROLES and raw_strs[i] != "":
                            m_ord = parse_money(raw_strs[i])
                            if m_ord.value is not None and m_ord.diagnostic is None:
                                v_vnd = round_money_vnd(m_ord.value)
                                if i in current_order_amounts:
                                    prev_vnd, prev_row = current_order_amounts[i]
                                    if prev_vnd != v_vnd:
                                        _log_diag(
                                            "CONFLICTING_ORDER_TOTAL",
                                            "error",
                                            sf,
                                            ss,
                                            src_row_num,
                                            headers[i],
                                            raw_strs[i],
                                            f"Order {current_raw_oid!r} has conflicting {headers[i]!r}: {v_vnd} at row {src_row_num} vs {prev_vnd} at row {prev_row}",
                                        )
                                else:
                                    current_order_amounts[i] = (v_vnd, src_row_num)

                eff_order_key = current_order_key
                eff_raw_oid = current_raw_oid
            else:
                filled_strs = list(raw_strs)
                eff_raw_oid = raw_strs[order_id_idx] if (order_id_idx is not None and order_id_idx < num_cols) else ""
                eff_order_key = eff_raw_oid or f"{sf}:{ss}:row_{src_row_num}"

            # Row is kept as an original record
            summary.kept_original_count += 1
            audit_records.append((sf, ss, src_row_num, sql_table, "kept_original", "kept", sf, ss, src_row_num))

            # Update subtotal accumulators for monetary/numeric columns on this kept original row
            for i in range(num_cols):
                if raw_strs[i] != "" and (
                    roles[i] in _MONEY_ROLES
                    or roles[i] in {"qty", "base_qty", "return_qty", "stock_qty", "other"}
                ):
                    m_sub = parse_money(raw_strs[i])
                    if m_sub.value is not None and m_sub.diagnostic is None:
                        val_int = round_money_vnd(m_sub.value)
                        day_accum[i] = day_accum.get(i, 0) + val_int
                        month_accum[i] = month_accum.get(i, 0) + val_int

            p_row = _ProcessedDataRow(
                unit_idx=u_idx,
                source_file=sf,
                source_sheet=ss,
                source_row=src_row_num,
                context_year=u.context_year,
                context_month=u.context_month,
                date1904=u.sheet.date1904,
                raw_cells=padded_cells,
                filled_strs=filled_strs,
                effective_order_key=eff_order_key,
                raw_order_id=eff_raw_oid,
                is_derived_component=False,
                parent_source_row=None,
            )

            # Check inline topping splitting (when enabled or when item_name matches inline topping pattern)
            item_name_idx = next((i for i, r in enumerate(roles) if r == "item_name"), None)
            line_amt_idx = next((i for i, r in enumerate(roles) if r == "line_amount"), None)
            qty_idx = next((i for i, r in enumerate(roles) if r == "qty"), None)
            uprice_idx = next((i for i, r in enumerate(roles) if r == "unit_price"), None)

            if (
                grain == "sales_line"
                and item_name_idx is not None
                and line_amt_idx is not None
                and "+" in filled_strs[item_name_idx]
            ):
                raw_item_s = filled_strs[item_name_idx]
                la_res = parse_money(filled_strs[line_amt_idx]) if filled_strs[line_amt_idx] else None
                q_res = parse_quantity(filled_strs[qty_idx]) if (qty_idx is not None and filled_strs[qty_idx]) else None
                up_res = parse_money(filled_strs[uprice_idx]) if (uprice_idx is not None and filled_strs[uprice_idx]) else None

                top_res = parse_inline_topping(
                    raw_item_s,
                    line_qty=q_res.value if q_res else 1,
                    line_amount=la_res.value if la_res else None,
                )
                if top_res.diagnostic is not None:
                    _log_diag(
                        top_res.diagnostic.code,
                        top_res.diagnostic.severity,
                        sf,
                        ss,
                        src_row_num,
                        headers[item_name_idx],
                        raw_item_s,
                        top_res.diagnostic.message,
                    )
                if top_res.value and len(top_res.value) > 1:
                    main_comp = top_res.value[0]
                    p_row.override_item_name = main_comp["name"]
                    p_row.override_qty = main_comp["qty"]
                    p_row.override_unit_price = (
                        round_money_vnd(main_comp["amount"] / main_comp["qty"])
                        if (main_comp["amount"] is not None and main_comp["qty"])
                        else None
                    )
                    p_row.override_line_amount = (
                        round_money_vnd(main_comp["amount"])
                        if main_comp["amount"] is not None
                        else None
                    )
                    kept_rows.append(p_row)

                    for extra_comp in top_res.value[1:]:
                        summary.derived_component_count += 1
                        derived_row = _ProcessedDataRow(
                            unit_idx=u_idx,
                            source_file=sf,
                            source_sheet=ss,
                            source_row=src_row_num,
                            context_year=u.context_year,
                            context_month=u.context_month,
                            date1904=u.sheet.date1904,
                            raw_cells=padded_cells,
                            filled_strs=list(filled_strs),
                            effective_order_key=eff_order_key,
                            raw_order_id=eff_raw_oid,
                            is_derived_component=True,
                            parent_source_row=src_row_num,
                            override_item_name=extra_comp["name"],
                            override_qty=extra_comp["qty"],
                            override_unit_price=(
                                round_money_vnd(extra_comp["amount"] / extra_comp["qty"])
                                if (extra_comp["amount"] is not None and extra_comp["qty"])
                                else None
                            ),
                            override_line_amount=(
                                round_money_vnd(extra_comp["amount"])
                                if extra_comp["amount"] is not None
                                else None
                            ),
                        )
                        kept_rows.append(derived_row)
                    continue

            kept_rows.append(p_row)

        report.sheet_summaries.append(summary)
        cur.execute(
            "INSERT INTO _ingested_tables VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                summary.source_file,
                summary.source_sheet,
                summary.logical_table,
                summary.table_grain,
                summary.raw_row_count,
                summary.kept_original_count,
                summary.preamble_count,
                summary.primary_header_count,
                summary.repeated_header_count,
                summary.subtotal_count,
                summary.footer_count,
                summary.blank_count,
                summary.removed_duplicate_count,
                summary.quarantined_count,
                summary.derived_component_count,
                summary.context_year,
                summary.context_month,
            ),
        )

    if audit_records:
        cur.executemany(
            "INSERT INTO _ingested_row_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            audit_records,
        )

    # 7. Order-level status & return aggregation across all rows of each effective_order_key
    status_idx = next((i for i, r in enumerate(roles) if r == "status"), None)
    notes_idx = next((i for i, r in enumerate(roles) if r == "notes"), None)
    qty_idx = next((i for i, r in enumerate(roles) if r == "qty"), None)

    order_status_map: dict[str, str] = {}
    order_returned_map: dict[str, int] = {}

    if is_sales_or_order:
        # Collect status and return signals per effective_order_key
        by_order: dict[str, list[_ProcessedDataRow]] = {}
        for pr in kept_rows:
            if not pr.is_derived_component:
                by_order.setdefault(pr.effective_order_key, []).append(pr)

        for ord_key, ord_rows in by_order.items():
            first_r = ord_rows[0]
            raw_oid = first_r.raw_order_id

            # 1) Determine base status from status column or inherited notes
            base_status = "COMPLETED"
            if status_idx is not None:
                raw_st = first_r.filled_strs[status_idx]
                st_res = normalize_order_status(raw_st, from_notes=False)
                if st_res.diagnostic is not None:
                    _log_diag(
                        st_res.diagnostic.code,
                        st_res.diagnostic.severity,
                        first_r.source_file,
                        first_r.source_sheet,
                        first_r.source_row,
                        headers[status_idx],
                        raw_st,
                        st_res.diagnostic.message,
                    )
                base_status = st_res.value or "UNKNOWN"
            elif notes_idx is not None:
                raw_note = first_r.filled_strs[notes_idx]
                st_res = normalize_order_status(raw_note, from_notes=True)
                base_status = st_res.value or "COMPLETED"

            # Check if any subsequent line in the same order has a conflicting status or return note
            has_return_signal = (base_status == "RETURNED")
            for r in ord_rows:
                if notes_idx is not None and r.filled_strs[notes_idx]:
                    n_res = normalize_order_status(r.filled_strs[notes_idx], from_notes=True)
                    if n_res.value == "RETURNED":
                        has_return_signal = True
                    elif n_res.value == "CANCELLED" and base_status == "COMPLETED":
                        base_status = "CANCELLED"
                # Negative quantity on an ITEM row when negative_qty_is_return is enabled (or negative item qty)
                if qty_idx is not None and r.filled_strs[qty_idx]:
                    q_res = parse_quantity(r.filled_strs[qty_idx])
                    if q_res.value is not None and q_res.value < 0:
                        item_idx = next((i for i, ro in enumerate(roles) if ro == "item_name"), None)
                        i_name = r.filled_strs[item_idx] if item_idx is not None else ""
                        r_role = classify_sales_row_role(i_name, None, q_res.value)
                        if r_role == "ITEM":
                            has_return_signal = True

            # Check external lot return evidence (e.g. DonHang_Lo / DanhSachHoaDonChiTietLo)
            if raw_oid and raw_oid in external_order_returns:
                has_return_signal = True

            if has_return_signal and base_status != "CANCELLED":
                eff_st = "RETURNED"
            else:
                eff_st = base_status

            order_status_map[ord_key] = eff_st
            order_returned_map[ord_key] = 1 if eff_st == "RETURNED" else 0

    # 8. Create SQLite table with raw columns + companion normalized columns
    col_defs: list[str] = [f'"{sc}" TEXT' for sc in sql_cols]
    for sc, r in zip(sql_cols, roles):
        if r in _MONEY_ROLES:
            col_defs.append(f'"{sc}__norm" INTEGER')
        elif r in {"qty", "base_qty", "return_qty", "stock_qty", "conversion_factor"}:
            col_defs.append(f'"{sc}__norm" REAL')
        elif r in {"date", "secondary_date"}:
            col_defs.append(f'"{sc}__norm" TEXT')

    std_companion_cols = [
        ("_source_file", "TEXT"),
        ("_source_sheet", "TEXT"),
        ("_source_row", "INTEGER"),
        ("_is_derived_component", "INTEGER"),
        ("_parent_source_row", "INTEGER"),
        ("_order_id", "TEXT"),
        ("_raw_order_id", "TEXT"),
        ("_order_date", "TEXT"),
        ("_order_year_month", "TEXT"),
        ("_status_raw", "TEXT"),
        ("_status_norm", "TEXT"),
        ("_effective_order_status", "TEXT"),
        ("_order_is_returned", "INTEGER"),
        ("_is_revenue_eligible", "INTEGER"),
        ("_row_role", "TEXT"),
        ("_customer_raw", "TEXT"),
        ("_customer_name", "TEXT"),
        ("_customer_phone", "TEXT"),
        ("_customer_id", "TEXT"),
        ("_is_walk_in", "INTEGER"),
        ("_item_id", "TEXT"),
        ("_item_name", "TEXT"),
        ("_qty", "REAL"),
        ("_unit", "TEXT"),
        ("_unit_factor", "REAL"),
        ("_base_qty", "REAL"),
        ("_unit_price", "INTEGER"),
        ("_line_amount", "INTEGER"),
        ("_line_discount", "INTEGER"),
        ("_order_total", "INTEGER"),
        ("_order_discount", "INTEGER"),
        ("_shipping_fee", "INTEGER"),
        ("_paid_amount", "INTEGER"),
        ("_debt_amount", "INTEGER"),
        ("_cost_price", "INTEGER"),
    ]
    for c_name, c_type in std_companion_cols:
        col_defs.append(f'"{c_name}" {c_type}')

    cur.execute(f'CREATE TABLE "{sql_table}" ({", ".join(col_defs)})')

    # 9. Normalize cells for each kept row and batch insert
    primary_date_idx = next((i for i, is_p in enumerate(p_dates) if is_p), None)
    if primary_date_idx is None:
        primary_date_idx = next((i for i, r in enumerate(roles) if r in {"date", "secondary_date"}), None)

    cust_idx = next((i for i, r in enumerate(roles) if r == "customer"), None)
    cust_id_idx = next((i for i, r in enumerate(roles) if r == "customer_id"), None)
    phone_idx = next((i for i, r in enumerate(roles) if r == "phone"), None)
    item_id_idx = next((i for i, r in enumerate(roles) if r == "item_id"), None)
    item_name_idx = next((i for i, r in enumerate(roles) if r == "item_name"), None)
    unit_idx = next((i for i, r in enumerate(roles) if r == "unit"), None)
    base_qty_idx = next((i for i, r in enumerate(roles) if r == "base_qty"), None)
    uprice_idx = next((i for i, r in enumerate(roles) if r == "unit_price"), None)
    cost_idx = next((i for i, r in enumerate(roles) if r == "cost_price"), None)
    line_amt_idx = next((i for i, r in enumerate(roles) if r == "line_amount"), None)
    line_disc_idx = next((i for i, r in enumerate(roles) if r in {"line_discount", "discount"}), None)
    ord_tot_idx = next((i for i, r in enumerate(roles) if r == "order_total"), None)
    ord_disc_idx = next((i for i, r in enumerate(roles) if r == "order_discount"), None)
    ship_idx = next((i for i, r in enumerate(roles) if r == "shipping_fee"), None)
    paid_idx = next((i for i, r in enumerate(roles) if r == "paid_amount"), None)
    debt_idx = next((i for i, r in enumerate(roles) if r == "debt_amount"), None)

    insert_rows: list[list[Any]] = []
    for pr in kept_rows:
        sf = pr.source_file
        ss = pr.source_sheet
        sr = pr.source_row
        f_strs = pr.filled_strs

        # Raw column values (with filled order-level values so queries on raw column names also work,
        # or overridden component values for derived inline-topping rows)
        row_out: list[Any] = []
        for i in range(num_cols):
            val_s = f_strs[i]
            if pr.is_derived_component:
                if i == item_name_idx and pr.override_item_name is not None:
                    val_s = pr.override_item_name
                elif i == qty_idx and pr.override_qty is not None:
                    val_s = str(pr.override_qty)
                elif i == uprice_idx and pr.override_unit_price is not None:
                    val_s = str(pr.override_unit_price)
                elif i == line_amt_idx and pr.override_line_amount is not None:
                    val_s = str(pr.override_line_amount)
            row_out.append(val_s if val_s != "" else None)

        # Per-column `__norm` values
        col_norm_vals: dict[int, Any] = {}
        for i, r in enumerate(roles):
            val_s = f_strs[i]
            if r in _MONEY_ROLES:
                if val_s == "":
                    col_norm_vals[i] = None
                else:
                    m_res = parse_money(val_s)
                    if m_res.diagnostic is not None and not pr.is_derived_component:
                        _log_diag(
                            m_res.diagnostic.code,
                            m_res.diagnostic.severity,
                            sf,
                            ss,
                            sr,
                            headers[i],
                            val_s,
                            m_res.diagnostic.message,
                        )
                    col_norm_vals[i] = round_money_vnd(m_res.value) if m_res.value is not None else None
                row_out.append(col_norm_vals[i])
            elif r in {"qty", "base_qty", "return_qty", "stock_qty", "conversion_factor"}:
                if val_s == "":
                    col_norm_vals[i] = None
                else:
                    q_res = parse_quantity(val_s)
                    if q_res.diagnostic is not None and not pr.is_derived_component:
                        _log_diag(
                            q_res.diagnostic.code,
                            q_res.diagnostic.severity,
                            sf,
                            ss,
                            sr,
                            headers[i],
                            val_s,
                            q_res.diagnostic.message,
                        )
                    col_norm_vals[i] = float(q_res.value) if q_res.value is not None else None
                row_out.append(col_norm_vals[i])
            elif r in {"date", "secondary_date"}:
                if val_s == "":
                    col_norm_vals[i] = None
                else:
                    d_res = parse_date_value(
                        val_s,
                        context_year=pr.context_year,
                        is_excel_date=(i < len(pr.raw_cells) and pr.raw_cells[i].is_date_formatted),
                        epoch_1904=pr.date1904,
                        allow_serial=True,
                    )
                    if d_res.diagnostic is not None and not pr.is_derived_component:
                        _log_diag(
                            d_res.diagnostic.code,
                            d_res.diagnostic.severity,
                            sf,
                            ss,
                            sr,
                            headers[i],
                            val_s,
                            d_res.diagnostic.message,
                        )
                    col_norm_vals[i] = d_res.value
                row_out.append(col_norm_vals[i])

        # Standard companion columns
        order_date = col_norm_vals.get(primary_date_idx) if primary_date_idx is not None else None
        if is_sales_or_order and order_date is None and primary_date_idx is not None and not pr.is_derived_component:
            _log_diag(
                "MISSING_PRIMARY_DATE",
                "error",
                sf,
                ss,
                sr,
                headers[primary_date_idx],
                f_strs[primary_date_idx],
                "Sales/order row is missing a valid normalized date after fill-down",
            )
        order_ym = order_date[:7] if (order_date and len(order_date) >= 7) else None

        # Status
        status_raw = (
            f_strs[status_idx]
            if status_idx is not None
            else (f_strs[notes_idx] if notes_idx is not None else None)
        )
        if status_idx is not None:
            st_norm = normalize_order_status(f_strs[status_idx], from_notes=False).value or "UNKNOWN"
        elif notes_idx is not None:
            st_norm = normalize_order_status(f_strs[notes_idx], from_notes=True).value or "COMPLETED"
        else:
            st_norm = "COMPLETED"

        eff_ord_status = order_status_map.get(pr.effective_order_key, st_norm)
        ord_is_returned = order_returned_map.get(pr.effective_order_key, 1 if eff_ord_status == "RETURNED" else 0)

        # Item & quantities & amounts (applying inline topping overrides if present)
        item_id_val = f_strs[item_id_idx] if (item_id_idx is not None and f_strs[item_id_idx] != "") else None
        if pr.override_item_name is not None:
            item_name_val: str | None = pr.override_item_name
        else:
            item_name_val = (
                f_strs[item_name_idx]
                if (item_name_idx is not None and f_strs[item_name_idx] != "")
                else None
            )

        if pr.override_qty is not None:
            qty_dec: Decimal | None = pr.override_qty
            unit_val: str | None = (
                f_strs[unit_idx] if (unit_idx is not None and f_strs[unit_idx] != "") else None
            )
        elif qty_idx is not None and f_strs[qty_idx] != "":
            q_parsed = parse_quantity(f_strs[qty_idx])
            qty_dec = q_parsed.value
            inline_u = q_parsed.meta.get("unit")
            col_u = f_strs[unit_idx] if (unit_idx is not None and f_strs[unit_idx] != "") else None
            unit_val = col_u or inline_u
        else:
            qty_dec = None
            unit_val = f_strs[unit_idx] if (unit_idx is not None and f_strs[unit_idx] != "") else None

        uprice_val = (
            pr.override_unit_price
            if pr.override_unit_price is not None
            else (col_norm_vals.get(uprice_idx) if uprice_idx is not None else None)
        )
        line_amt_val = (
            pr.override_line_amount
            if pr.override_line_amount is not None
            else (col_norm_vals.get(line_amt_idx) if line_amt_idx is not None else None)
        )
        line_disc_val = col_norm_vals.get(line_disc_idx) if line_disc_idx is not None else None
        ord_tot_val = col_norm_vals.get(ord_tot_idx) if ord_tot_idx is not None else None
        ord_disc_val = col_norm_vals.get(ord_disc_idx) if ord_disc_idx is not None else None
        ship_val = col_norm_vals.get(ship_idx) if ship_idx is not None else None
        paid_val = col_norm_vals.get(paid_idx) if paid_idx is not None else None
        debt_val = col_norm_vals.get(debt_idx) if debt_idx is not None else None
        cost_val = col_norm_vals.get(cost_idx) if cost_idx is not None else None

        # Unit conversion -> _unit_factor and _base_qty
        preconv_base_qty = (
            col_norm_vals.get(base_qty_idx)
            if (base_qty_idx is not None and not pr.is_derived_component and pr.override_qty is None)
            else None
        )
        unit_factor_dec, _ = meta.resolve_unit_factor(
            unit_val,
            item_code=item_id_val,
            item_name=item_name_val,
        )
        if preconv_base_qty is not None:
            base_qty_val: float | None = float(preconv_base_qty)
            unit_factor_val: float | None = (
                float(unit_factor_dec)
                if unit_factor_dec is not None
                else (float(preconv_base_qty) / float(qty_dec) if (qty_dec and qty_dec != 0) else 1.0)
            )
        elif qty_dec is not None and unit_factor_dec is not None:
            unit_factor_val = float(unit_factor_dec)
            base_qty_val = float(qty_dec * unit_factor_dec)
        else:
            unit_factor_val = float(unit_factor_dec) if unit_factor_dec is not None else None
            base_qty_val = float(qty_dec) if qty_dec is not None else None

        # Row role & revenue eligibility
        row_role = classify_sales_row_role(item_name_val, line_amt_val, qty_dec)
        is_neg_return_line = bool(qty_dec is not None and qty_dec < 0 and row_role == "ITEM")
        is_rev_eligible = (
            1
            if (
                eff_ord_status in {"COMPLETED", "SHIPPING"}
                and row_role in {"ITEM", "SERVICE_FEE"}
                and not is_neg_return_line
            )
            else 0
        )

        # Customer normalization
        cust_raw = f_strs[cust_idx] if (cust_idx is not None and f_strs[cust_idx] != "") else None
        cust_id_val = f_strs[cust_id_idx] if (cust_id_idx is not None and f_strs[cust_id_idx] != "") else None
        explicit_phone = f_strs[phone_idx] if (phone_idx is not None and f_strs[phone_idx] != "") else None
        if cust_raw is not None or cust_idx is not None:
            c_res = parse_customer_field(cust_raw)
            cust_name = c_res.meta.get("customer_name")
            cust_phone = explicit_phone or c_res.meta.get("customer_phone")
            is_walk_in = 1 if (c_res.meta.get("is_walk_in") and not cust_id_val) else 0
        else:
            cust_name = None
            cust_phone = explicit_phone
            is_walk_in = 0 if cust_id_val else 1

        row_out.extend(
            [
                sf,
                ss,
                sr,
                1 if pr.is_derived_component else 0,
                pr.parent_source_row,
                pr.effective_order_key,
                pr.raw_order_id or None,
                order_date,
                order_ym,
                status_raw,
                st_norm,
                eff_ord_status,
                ord_is_returned,
                is_rev_eligible,
                row_role,
                cust_raw,
                cust_name,
                cust_phone,
                cust_id_val,
                is_walk_in,
                item_id_val,
                item_name_val,
                float(qty_dec) if qty_dec is not None else None,
                unit_val,
                unit_factor_val,
                base_qty_val,
                uprice_val,
                line_amt_val,
                line_disc_val,
                ord_tot_val,
                ord_disc_val,
                ship_val,
                paid_val,
                debt_val,
                cost_val,
            ]
        )
        insert_rows.append(row_out)

    if insert_rows:
        placeholders = ", ".join(["?"] * len(col_defs))
        cur.executemany(
            f'INSERT INTO "{sql_table}" VALUES ({placeholders})',
            insert_rows,
        )
    # Create helpful indexes on sales/order tables
    cur.execute(f'CREATE INDEX IF NOT EXISTS "idx_{sql_table}_order_date" ON "{sql_table}" ("_order_date")')
    cur.execute(f'CREATE INDEX IF NOT EXISTS "idx_{sql_table}_order_ym" ON "{sql_table}" ("_order_year_month")')
    cur.execute(f'CREATE INDEX IF NOT EXISTS "idx_{sql_table}_order_id" ON "{sql_table}" ("_order_id")')
