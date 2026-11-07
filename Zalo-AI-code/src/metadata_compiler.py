"""Runtime metadata and unit-conversion compiler from business README.md and tables.

Derives column semantic roles, business conventions, date contexts, and unit conversion
factors purely from runtime metadata (never hardcoding business_id or question_id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import re
from typing import Any
import unicodedata

from src.normalizers import Diagnostic, parse_date_value, parse_quantity
from src.postprocess import normalize_comparison_string, normalize_display_string
from src.text_norm import fold_key


@dataclass(frozen=True)
class UnitConversionRule:
    """A single unit conversion rule mapping (item, from_unit) -> factor * base_unit."""

    item_name: str | None
    item_name_norm: str | None
    item_code: str | None
    from_unit_norm: str
    factor: Decimal
    to_unit_norm: str | None = None
    is_chained_resolved: bool = True
    source: str = "readme"


@dataclass
class TableSchemaMetadata:
    """Compiled column roles and glosses for a specific file/sheet/table."""

    target_name: str
    column_glosses: dict[str, str] = field(default_factory=dict)
    column_roles: dict[str, str] = field(default_factory=dict)


@dataclass
class BusinessMetadata:
    """Compiled runtime metadata for one business folder."""

    business_id: str
    readme_path: Path | None
    readme_text: str
    coverage_end_date: str | None = None
    coverage_year: int | None = None
    declared_files: list[str] = field(default_factory=list)
    declared_tables: list[str] = field(default_factory=list)
    table_glosses: dict[str, dict[str, str]] = field(default_factory=dict)
    global_col_glosses: dict[str, str] = field(default_factory=dict)
    conventions: list[tuple[str, str]] = field(default_factory=list)

    # Behavioral flags derived from README.md text
    stt_is_order_id: bool = False
    notes_has_status: bool = False
    notes_has_return: bool = False
    negative_qty_is_return: bool = False
    inline_topping_enabled: bool = False
    non_item_rows_enabled: bool = False
    service_fee_row_enabled: bool = False
    conversion_source_hint: str | None = None

    # Unit conversions and item alias maps
    unit_rules: list[UnitConversionRule] = field(default_factory=list)
    # Lookup maps: (item_key, from_unit_norm) -> Decimal factor
    factor_by_code_unit: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    factor_by_name_unit: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    base_unit_by_code: dict[str, str] = field(default_factory=dict)
    base_unit_by_name: dict[str, str] = field(default_factory=dict)
    code_to_name_norm: dict[str, str] = field(default_factory=dict)
    name_norm_to_code: dict[str, str] = field(default_factory=dict)
    alias_norm_to_canonical_name: dict[str, str] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def resolve_unit_factor(
        self,
        unit_norm: str | None,
        *,
        item_code: str | None = None,
        item_name: str | None = None,
    ) -> tuple[Decimal | None, str]:
        """Resolve multiplier to convert `unit_norm` into base units for the given item.

        Returns:
            (factor_or_None, resolution_source)
        """
        if not unit_norm:
            return Decimal(1), "no_unit_default_1"

        u_norm = normalize_display_string(unit_norm).lower()

        code_key = normalize_display_string(item_code).upper() if item_code else None
        name_key = normalize_comparison_string(item_name) if item_name else None

        # Resolve through alias if needed
        if name_key and name_key in self.alias_norm_to_canonical_name:
            canon_name = self.alias_norm_to_canonical_name[name_key]
            name_key = normalize_comparison_string(canon_name)

        if code_key and not name_key and code_key in self.code_to_name_norm:
            name_key = self.code_to_name_norm[code_key]
        if name_key and not code_key and name_key in self.name_norm_to_code:
            code_key = self.name_norm_to_code[name_key]

        # 1. Exact lookup by (item_code, unit_norm)
        if code_key and (code_key, u_norm) in self.factor_by_code_unit:
            return self.factor_by_code_unit[(code_key, u_norm)], "code_unit_match"

        # 2. Exact lookup by (item_name_norm, unit_norm)
        if name_key and (name_key, u_norm) in self.factor_by_name_unit:
            return self.factor_by_name_unit[(name_key, u_norm)], "name_unit_match"

        # 3. Check if unit_norm is already the known base unit for this item
        if code_key and self.base_unit_by_code.get(code_key) == u_norm:
            return Decimal(1), "is_base_unit_by_code"
        if name_key and self.base_unit_by_name.get(name_key) == u_norm:
            return Decimal(1), "is_base_unit_by_name"

        # 4. If item has no conversion rules at all, its recorded unit is its base unit (factor 1)
        has_any_rule_for_item = False
        if code_key and any(k[0] == code_key for k in self.factor_by_code_unit):
            has_any_rule_for_item = True
        if name_key and any(k[0] == name_key for k in self.factor_by_name_unit):
            has_any_rule_for_item = True

        if not has_any_rule_for_item:
            return Decimal(1), "item_without_conversion_rules"

        # 5. If the item has conversion rules for larger units (e.g. thùng/lốc) and the current
        # unit is a standard base unit not in the large-unit map (e.g. lon/chai/gói/hộp/cái/kg/m3/bao),
        # check if any rule for this item has `to_unit_norm == u_norm` or if base_unit was not explicitly stored.
        for r in self.unit_rules:
            if (code_key and r.item_code == code_key) or (name_key and r.item_name_norm == name_key):
                if r.to_unit_norm == u_norm:
                    return Decimal(1), "matched_rule_to_unit"

        # If base_unit wasn't explicitly specified in the conversion table (e.g. DanhSachDonViTinh only lists
        # large units like thùng/lốc without a base_unit column), any unit not listed as a large unit for that
        # product is the base unit (unless it looks like an unknown large unit).
        if code_key and code_key not in self.base_unit_by_code:
            return Decimal(1), "unlisted_unit_assumed_base"
        if name_key and name_key not in self.base_unit_by_name:
            return Decimal(1), "unlisted_unit_assumed_base"

        return None, "unresolved_unit"

    def lookup_column_gloss(
        self,
        file_name: str,
        sheet_or_table_name: str | None,
        col_name: str,
    ) -> str | None:
        """Look up the README column gloss for a specific (file, sheet/table, col_name)."""
        c_disp = normalize_display_string(col_name)
        c_norm = normalize_comparison_string(col_name)

        def _match_in_map(col_map: dict[str, str]) -> str | None:
            if c_disp in col_map:
                return col_map[c_disp]
            for k, v in col_map.items():
                if normalize_comparison_string(k) == c_norm:
                    return v
            return None

        # 1. Exact or stem match on sheet_or_table_name (primary) or file_name (secondary) in table_glosses
        candidates: list[str] = []
        if sheet_or_table_name:
            candidates.append(sheet_or_table_name)
        if file_name and file_name not in candidates:
            candidates.append(file_name)

        matched_target = False
        for cand in candidates:
            cand_norm = normalize_comparison_string(cand)
            cand_stem = normalize_comparison_string(Path(cand).stem)
            cand_base = re.sub(r"_[0-9]+$", "", cand_stem)
            for target_key, col_map in self.table_glosses.items():
                t_norm = normalize_comparison_string(target_key)
                t_stem = normalize_comparison_string(Path(target_key).stem)
                t_base = re.sub(r"_[0-9]+$", "", t_stem)
                if cand_norm == t_norm or cand_stem == t_stem or (cand_base and cand_base == t_base):
                    matched_target = True
                    found = _match_in_map(col_map)
                    if found is not None:
                        return found

        # 2. Only fall back to global_col_glosses when the README has no `## Cột trong ...`
        # sections at all (e.g. inline definitions in `## Quy ước`), or when the column name
        # is not an ambiguous generic header like `Trạng thái` across different table types.
        if not self.table_glosses or (not matched_target and c_norm not in {"trạng thái", "trang_thai", "status"}):
            return _match_in_map(self.global_col_glosses)
        return None


# ---------------------------------------------------------------------------
# README.md Parser
# ---------------------------------------------------------------------------

_COVERAGE_DATE_RE = re.compile(
    r"Dữ liệu có đến hết\s+(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_FILE_BULLET_RE = re.compile(r"^\s*-\s*`([^`]+)`")
_COL_SECTION_SHEET_RE = re.compile(
    r"^##\s+Cột trong\s+`([^`]+)`\s*,\s*sheet\s+`([^`]+)`\s*$",
    re.IGNORECASE,
)
_COL_SECTION_RE = re.compile(r"^##\s+Cột trong\s+`?([^`\n]+)`?\s*$", re.IGNORECASE)
_COL_GLOSS_BULLET_RE = re.compile(r"^\s*-\s*\*\*(.+?)\*\*:\s*(.+?)\s*$")
_INLINE_UNIT_RULE_RE = re.compile(
    r"^\s*-\s*(.+?):\s*1\s+(.+?)\s*=\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$"
)
_PROSE_BUNDLE_RULE_RE = re.compile(
    r"^(.+?)\s+bán theo\s+[^,]+,\s*1\s+(.+?)\s*=\s*(\d+(?:[.,]\d+)?)\s+(.+?)(?:;|$)",
    re.IGNORECASE,
)
_CONV_SOURCE_RE = re.compile(r"hệ số quy đổi ở\s+(?:bảng\s+)?`([^`]+)`", re.IGNORECASE)
_INLINE_BOLD_DEF_RE = re.compile(r"\*\*([^*]+)\*\*\s+là\s+([^.;\n]+)")
_INLINE_BACKTICK_DEF_RE = re.compile(r"`([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+)`\s+là\s+([^.;\n]+)")


def parse_readme_metadata(readme_path: str | Path, business_id: str = "") -> BusinessMetadata:
    """Parse a business README.md file into structured BusinessMetadata."""
    p = Path(readme_path)
    if not p.exists():
        return BusinessMetadata(
            business_id=business_id or p.parent.name,
            readme_path=None,
            readme_text="",
        )

    text = unicodedata.normalize("NFC", p.read_text(encoding="utf-8"))
    biz_id = business_id or p.parent.name
    meta = BusinessMetadata(
        business_id=biz_id,
        readme_path=p,
        readme_text=text,
    )

    # 1. Coverage end date
    m_cov = _COVERAGE_DATE_RE.search(text)
    if m_cov:
        d_res = parse_date_value(m_cov.group(1), allow_serial=False)
        if d_res.value:
            meta.coverage_end_date = d_res.value
            meta.coverage_year = int(d_res.value[:4])

    # 2. Behavioral flags from README prose
    text_lower = text.lower().replace("huỷ", "hủy")
    if "các dòng cùng **stt** là một đơn" in text_lower or "số thứ tự đơn" in text_lower:
        meta.stt_is_order_id = True
    if "đơn hủy hoặc đang giao được ghi ở đây" in text_lower or "không có cột trạng thái riêng" in text_lower:
        meta.notes_has_status = True
    if "ghi khách trả lại" in text_lower or "đơn khách trả lại hàng ghi ở đây" in text_lower:
        meta.notes_has_return = True
    if "hàng trả ghi số lượng âm" in text_lower:
        meta.negative_qty_is_return = True
    if "topping có thể ghi chung ô với món" in text_lower:
        meta.inline_topping_enabled = True
    if "dòng không phải hàng ghi voucher" in text_lower:
        meta.non_item_rows_enabled = True
    if 'dòng hàng "phí phục vụ"' in text_lower:
        meta.service_fee_row_enabled = True

    m_src = _CONV_SOURCE_RE.search(text)
    if m_src:
        meta.conversion_source_hint = m_src.group(1).strip()

    # 3. Line-by-line section parsing
    current_section = ""
    current_col_target: str | None = None
    in_unit_conversions = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            in_unit_conversions = False
            m_col_sheet = _COL_SECTION_SHEET_RE.match(stripped)
            if m_col_sheet:
                current_col_target = m_col_sheet.group(2).strip()
                meta.table_glosses.setdefault(current_col_target, {})
            else:
                m_col_sec = _COL_SECTION_RE.match(stripped)
                if m_col_sec:
                    current_col_target = m_col_sec.group(1).strip()
                    meta.table_glosses.setdefault(current_col_target, {})
                else:
                    current_col_target = None
            continue

        if current_section.lower() == "file":
            m_f = _FILE_BULLET_RE.match(stripped)
            if m_f:
                meta.declared_files.append(m_f.group(1).strip())
            continue

        if current_section.lower() == "bảng":
            for tbl in re.findall(r"`([^`]+)`", stripped):
                meta.declared_tables.append(tbl.strip())
            continue

        if current_col_target is not None:
            m_cg = _COL_GLOSS_BULLET_RE.match(stripped)
            if m_cg:
                col_name = normalize_display_string(m_cg.group(1))
                gloss = normalize_display_string(m_cg.group(2))
                meta.table_glosses[current_col_target][col_name] = gloss
                meta.global_col_glosses.setdefault(col_name, gloss)
                # Also check if gloss mentions inline topping or non-item rows
                g_low = gloss.lower()
                if "voucher" in g_low and "phí ship" in g_low:
                    meta.non_item_rows_enabled = True
            continue

        if current_section.lower() == "quy ước":
            if stripped.startswith("- **Quy đổi đơn vị**:"):
                in_unit_conversions = True
                continue

            if in_unit_conversions and re.match(r"^\s{2,}-\s+", line):
                m_u = _INLINE_UNIT_RULE_RE.match(stripped)
                if m_u:
                    item_name = normalize_display_string(m_u.group(1))
                    from_unit = normalize_display_string(m_u.group(2)).lower()
                    factor = Decimal(m_u.group(3).replace(",", "."))
                    to_unit = normalize_display_string(m_u.group(4)).lower()
                    _register_unit_rule(
                        meta,
                        UnitConversionRule(
                            item_name=item_name,
                            item_name_norm=normalize_comparison_string(item_name),
                            item_code=None,
                            from_unit_norm=from_unit,
                            factor=factor,
                            to_unit_norm=to_unit,
                            source="readme_bullet",
                        ),
                    )
                continue
            else:
                in_unit_conversions = False

            m_cg = _COL_GLOSS_BULLET_RE.match(stripped)
            if m_cg:
                topic = normalize_display_string(m_cg.group(1))
                body = normalize_display_string(m_cg.group(2))
                meta.conventions.append((topic, body))

                # Check for prose unit bundle rule (e.g. "Ống PVC ... bán theo bó, 1 bó = 10 cây")
                m_prose = _PROSE_BUNDLE_RULE_RE.match(body)
                if m_prose:
                    items_str, from_u, fac_str, to_u = m_prose.groups()
                    from_unit = normalize_display_string(from_u).lower()
                    factor = Decimal(fac_str.replace(",", "."))
                    to_unit = normalize_display_string(to_u).lower()
                    for part in items_str.split(","):
                        item_name = normalize_display_string(part)
                        if item_name:
                            _register_unit_rule(
                                meta,
                                UnitConversionRule(
                                    item_name=item_name,
                                    item_name_norm=normalize_comparison_string(item_name),
                                    item_code=None,
                                    from_unit_norm=from_unit,
                                    factor=factor,
                                    to_unit_norm=to_unit,
                                    source="readme_prose",
                                ),
                            )

                # Extract inline `**<Col>** là <gloss>` definitions inside Quy ước bullets
                for m_def in _INLINE_BOLD_DEF_RE.finditer(body):
                    c_name = normalize_display_string(m_def.group(1))
                    c_gloss = normalize_display_string(m_def.group(2))
                    meta.global_col_glosses.setdefault(c_name, c_gloss)

                # Extract inline `` `table.col` là <gloss> `` definitions
                for m_bdef in _INLINE_BACKTICK_DEF_RE.finditer(body):
                    tbl_col, c_gloss = m_bdef.group(1), normalize_display_string(m_bdef.group(2))
                    if "." in tbl_col:
                        t_name, c_name = tbl_col.split(".", 1)
                        meta.table_glosses.setdefault(t_name, {})[c_name] = c_gloss
                        meta.global_col_glosses.setdefault(c_name, c_gloss)

    return meta


def _register_unit_rule(meta: BusinessMetadata, rule: UnitConversionRule) -> None:
    meta.unit_rules.append(rule)
    if rule.item_code:
        code_key = normalize_display_string(rule.item_code).upper()
        meta.factor_by_code_unit[(code_key, rule.from_unit_norm)] = rule.factor
        if rule.to_unit_norm:
            meta.base_unit_by_code[code_key] = rule.to_unit_norm
            meta.factor_by_code_unit.setdefault((code_key, rule.to_unit_norm), Decimal(1))
    if rule.item_name_norm:
        meta.factor_by_name_unit[(rule.item_name_norm, rule.from_unit_norm)] = rule.factor
        if rule.to_unit_norm:
            meta.base_unit_by_name[rule.item_name_norm] = rule.to_unit_norm
            meta.factor_by_name_unit.setdefault((rule.item_name_norm, rule.to_unit_norm), Decimal(1))


def register_conversion_table_rows(
    meta: BusinessMetadata,
    headers: list[str],
    rows: list[list[Any]],
    source_name: str = "table",
) -> None:
    """Ingest unit conversion rules from a conversion sheet/table (including chained rules)."""
    if not headers or not rows:
        return

    norm_headers = [normalize_comparison_string(h) for h in headers]

    # Identify column indices in the conversion table
    name_idx: int | None = None
    code_idx: int | None = None
    from_unit_idx: int | None = None
    base_unit_idx: int | None = None
    factor_idx: int | None = None
    alias_idx: int | None = None
    inline_rule_idx: int | None = None

    for idx, nh in enumerate(norm_headers):
        if nh in {"tên sản phẩm", "tên món", "hàng", "tên hàng", "sản phẩm", "món"}:
            name_idx = idx
        elif nh in {"mã hàng", "mã sp", "mã vạch", "mã sản phẩm", "ma_sp", "ma_hang", "mahanghoa", "mã h hóa", "mã sku", "sku", "mã vạch", "mã món"}:
            code_idx = idx
        elif nh in {"đơn vị quy đổi", "đvt", "đơn vị", "quy cách", "đv nhập", "đv lớn", "loại đv", "ten_don_vi", "donvitinh"}:
            from_unit_idx = idx
        elif nh in {"đơn vị cơ bản", "đơn vị gốc", "đv nhỏ", "don_vi_co_ban"}:
            base_unit_idx = idx
        elif nh in {"tỉ lệ quy đổi", "quy đổi", "bằng", "hệ số", "hệ số qđ", "he_so_quy_doi", "hesoquydoi"}:
            # Check if column values look like "1 thùng = 6 bịch" (inline rule) vs numeric/quantity factor
            sample_vals = [str(r[idx]).strip() for r in rows if idx < len(r) and r[idx] is not None and str(r[idx]).strip()]
            if any("=" in v for v in sample_vals):
                inline_rule_idx = idx
            else:
                factor_idx = idx
        elif nh in {"tên gọi khác", "ghi tắt"}:
            alias_idx = idx

    # First pass: collect raw rules and aliases
    pending_chained: list[tuple[str | None, str | None, str, Decimal, str, str | None]] = []

    for r in rows:
        if not any(c is not None and str(c).strip() for c in r):
            continue

        item_name = (
            normalize_display_string(str(r[name_idx]))
            if name_idx is not None and name_idx < len(r) and r[name_idx] is not None and str(r[name_idx]).strip()
            else None
        )
        item_name_norm = normalize_comparison_string(item_name) if item_name else None
        item_code = (
            normalize_display_string(str(r[code_idx])).upper()
            if code_idx is not None and code_idx < len(r) and r[code_idx] is not None and str(r[code_idx]).strip()
            else None
        )

        if item_code and item_name_norm:
            meta.code_to_name_norm[item_code] = item_name_norm
            meta.name_norm_to_code[item_name_norm] = item_code

        # Register item aliases (e.g. 'Tên gọi khác': 'similac 1, similac số 1' or 'Ghi tắt')
        if alias_idx is not None and alias_idx < len(r) and r[alias_idx] is not None and item_name:
            raw_aliases = str(r[alias_idx]).strip()
            if raw_aliases:
                for part in raw_aliases.split(","):
                    al_norm = normalize_comparison_string(part)
                    if al_norm:
                        meta.alias_norm_to_canonical_name[al_norm] = item_name

        # Case A: inline conversion string column (e.g. DanhMuc 'Quy đổi': '1 thùng = 6 bịch')
        if inline_rule_idx is not None and inline_rule_idx < len(r) and r[inline_rule_idx] is not None:
            cell_str = normalize_display_string(str(r[inline_rule_idx]))
            if from_unit_idx is not None and from_unit_idx < len(r) and r[from_unit_idx] is not None:
                base_u = normalize_display_string(str(r[from_unit_idx])).lower()
                if item_name_norm and base_u:
                    meta.base_unit_by_name[item_name_norm] = base_u
                if item_code and base_u:
                    meta.base_unit_by_code[item_code] = base_u
            if cell_str:
                for seg in re.split(r"[;,\n]+", cell_str):
                    m_in = re.match(r"^\s*1\s+(.+?)\s*=\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$", seg.strip())
                    if m_in:
                        f_u = normalize_display_string(m_in.group(1)).lower()
                        fac = Decimal(m_in.group(2).replace(",", "."))
                        t_u = normalize_display_string(m_in.group(3)).lower()
                        _register_unit_rule(
                            meta,
                            UnitConversionRule(
                                item_name=item_name,
                                item_name_norm=item_name_norm,
                                item_code=item_code,
                                from_unit_norm=f_u,
                                factor=fac,
                                to_unit_norm=t_u,
                                source=source_name,
                            ),
                        )
            continue

        # Case B: standard conversion table (from_unit, factor, optional base_unit)
        if from_unit_idx is None or factor_idx is None:
            continue
        if from_unit_idx >= len(r) or factor_idx >= len(r):
            continue

        raw_from = r[from_unit_idx]
        raw_factor = r[factor_idx]
        if raw_from is None or raw_factor is None:
            continue

        from_u = normalize_display_string(str(raw_from)).lower()
        to_u = (
            normalize_display_string(str(r[base_unit_idx])).lower()
            if base_unit_idx is not None and base_unit_idx < len(r) and r[base_unit_idx] is not None and str(r[base_unit_idx]).strip()
            else None
        )

        q_res = parse_quantity(raw_factor)
        if q_res.value is None or q_res.diagnostic is not None:
            continue

        fac_val = q_res.value
        inter_unit = q_res.meta.get("unit_norm")

        if inter_unit and to_u and inter_unit != to_u:
            # Chained conversion (e.g. from_u='hộp', factor='15 vỉ', to_u='viên')
            pending_chained.append((item_name, item_name_norm, item_code, from_u, fac_val, inter_unit, to_u))
        else:
            _register_unit_rule(
                meta,
                UnitConversionRule(
                    item_name=item_name,
                    item_name_norm=item_name_norm,
                    item_code=item_code,
                    from_unit_norm=from_u,
                    factor=fac_val,
                    to_unit_norm=to_u,
                    source=source_name,
                ),
            )

    # Second pass: resolve chained rules (e.g. 1 hộp = 15 vỉ, 1 vỉ = 12 viên -> 1 hộp = 180 viên)
    for item_name, item_name_norm, item_code, from_u, fac_val, inter_unit, to_u in pending_chained:
        inter_factor, _ = meta.resolve_unit_factor(
            inter_unit,
            item_code=item_code,
            item_name=item_name,
        )
        if inter_factor is not None:
            resolved_factor = fac_val * inter_factor
            _register_unit_rule(
                meta,
                UnitConversionRule(
                    item_name=item_name,
                    item_name_norm=item_name_norm,
                    item_code=item_code,
                    from_unit_norm=from_u,
                    factor=resolved_factor,
                    to_unit_norm=to_u,
                    is_chained_resolved=True,
                    source=f"{source_name}_chained",
                ),
            )
        else:
            meta.diagnostics.append(
                Diagnostic(
                    "UNRESOLVED_CHAINED_UNIT",
                    f"Could not resolve intermediate unit {inter_unit!r} for {item_name or item_code!r}",
                    {"item": item_name or item_code, "from_unit": from_u, "intermediate": inter_unit},
                    severity="error",
                )
            )


# ---------------------------------------------------------------------------
# Column Semantic Role Inference
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _header_in(col_name: str, c_norm: str, options: set[str]) -> bool:
    """Match a header against known names. A header typed without diacritics may match by letters alone.

    One that has diacritics must match them too: "Hãng" (brand) is not "Hàng" (goods).
    """
    if c_norm in options or any(c_norm == normalize_comparison_string(o) for o in options):
        return True
    if not col_name.isascii():
        return False
    key = fold_key(_CAMEL_RE.sub(" ", col_name))
    return any(key == fold_key(o) or key.replace(" ", "") == fold_key(o).replace(" ", "") for o in options)


def infer_column_role(
    col_name: str,
    gloss: str | None = None,
    *,
    stt_is_order_id: bool = False,
) -> tuple[str, bool]:
    """Infer the semantic role of a column from its README gloss (primary) and header name (secondary).

    Returns:
        (role, is_primary_date)
    """
    c_norm = normalize_comparison_string(col_name)
    g_norm = normalize_comparison_string(gloss) if gloss else ""

    # 1. Explicit sequence / line number check first (disambiguates `TT dòng` from `Thành tiền`)
    if c_norm in {"tt dòng", "no", "no."} or "số thứ tự dòng" in g_norm:
        return "line_seq", False
    if c_norm == "stt":
        if stt_is_order_id or "số thứ tự đơn" in g_norm or "cùng stt là một đơn" in g_norm:
            return "order_id", False
        return "line_seq", False

    # Exclude duration / day-count columns before any date keyword checks
    if (
        c_norm.startswith("số ngày")
        or g_norm.startswith("số ngày")
        or c_norm in {"kỳ hạn", "hạn góp ngày", "số ngày được nợ"}
    ):
        return "other", False

    # Check explicit price headers/glosses before date checks (so glosses like
    # "giá bán áp dụng từ ngày đó" are not misclassified as secondary_date)
    if (
        c_norm
        in {
            "đơn giá",
            "giá bán",
            "giá",
            "giá sản phẩm",
            "giá niêm yết",
            "giá nhập",
            "giá mới",
            "giá cũ",
            "giá thu",
            "giá lẻ",
            "giá hiện tại",
            "đơn giá đ",
            "giá 1 đv",
            "don_gia",
            "gia_ban",
        }
        or g_norm
        in {
            "đơn giá",
            "giá bán hiện tại",
            "giá vốn",
            "giá nhập một đơn vị theo đơn vị nhập của dòng",
            "đơn giá một máy một cái",
        }
        or g_norm.startswith(("giá bán", "giá trước", "giá nhập", "đơn giá"))
    ):
        if "giá vốn" in g_norm or "giá vốn" in c_norm:
            return "cost_price", False
        return "unit_price", False

    # 2. Gloss-driven classification (authoritative when present)
    if g_norm:
        if "cột trống" in g_norm:
            return "empty_col", False

        if "trạng thái phiếu nhập" in g_norm:
            return "purchase_status", False

        if "trạng thái hoạt động" in g_norm or "còn kinh doanh" in g_norm:
            return "entity_status", False

        if "trạng thái hóa đơn điện tử" in g_norm or "trạng thái hđđt" in g_norm:
            return "einvoice_status", False

        if any(
            p in g_norm
            for p in (
                "mã đơn",
                "mã hóa đơn",
                "mã hoá đơn",
                "số hoá đơn",
                "số hóa đơn",
                "số thứ tự đơn",
                "mã phiếu",
                "số phiếu",
            )
        ):
            return "order_id", False

        if any(
            p in g_norm
            for p in (
                "ngày bán",
                "thời điểm đặt đơn",
                "ngày đặt",
                "ngày mua",
                "ngày nhập",
                "thời điểm thu",
                "ngày thu",
                "ngày chi",
                "ngày giao",
                "ngày nhận",
                "ngày sửa",
                "ngày bắt đầu áp dụng giá",
                "ngày áp dụng",
            )
        ):
            return "date", True

        if any(
            p in g_norm
            for p in (
                "hạn thanh toán",
                "ngày phải trả",
                "hạn sử dụng",
                "ngày tạo",
                "ngày đăng ký",
                "từ ngày",
                "đến ngày",
                "ngày trả",
                "ngày góp",
            )
        ):
            return "secondary_date", False

        if "giờ đặt đơn" in g_norm or "giờ tạo đơn" in g_norm:
            return "time", False

        if "trạng thái đơn" in g_norm or g_norm == "trạng thái phiếu":
            return "status", False

        if g_norm.startswith("ghi chú"):
            return "notes", False

        if any(p in g_norm for p in ("tên khách", "khách không lưu")):
            return "customer", False

        if "mã khách" in g_norm:
            return "customer_id", False

        if "số điện thoại" in g_norm:
            return "phone", False

        if "mã hàng" in g_norm or "mã sản phẩm" in g_norm:
            return "item_id", False

        if any(p in g_norm for p in ("tên hàng", "hàng bán")):
            return "item_name", False

        if "số lượng đã đổi ra đơn vị cơ bản" in g_norm or "số lượng nhập đơn vị cơ bản" in g_norm or "số lượng bán lấy từ lô đơn vị cơ bản" in g_norm:
            return "base_qty", False

        if "số lượng khách trả lại" in g_norm:
            return "return_qty", False

        if g_norm in {"số lượng", "số lượng nhập", "số lượng trống ở dòng không phải hàng"} or g_norm.startswith("số lượng"):
            if "ngay được nợ" not in g_norm and "khách" not in g_norm:
                return "qty", False

        if g_norm in {"đơn vị tính", "đơn vị nhập", "đơn vị lớn thùng hộp tấn"}:
            return "unit", False

        if g_norm == "đơn vị cơ bản":
            return "base_unit", False

        if g_norm.startswith("hệ số"):
            return "conversion_factor", False

        if g_norm in {"đơn giá", "giá bán hiện tại", "giá vốn", "giá nhập một đơn vị theo đơn vị nhập của dòng", "đơn giá một máy một cái"} or "giá bán áp dụng" in g_norm:
            if "giá vốn" in g_norm:
                return "cost_price", False
            return "unit_price", False

        if any(
            p in g_norm
            for p in (
                "thành tiền của dòng",
                "thành tiền sau giảm",
                "tiền nhập của cả dòng",
            )
        ):
            return "line_amount", False

        if any(
            p in g_norm
            for p in (
                "tổng tiền hàng của cả đơn",
                "tổng tiền hàng của đơn",
                "tiền hàng của đơn",
                "tổng tiền đơn",
                "tổng tiền của cả phiếu",
                "tổng tiền cả phiếu",
                "tổng tiền phiếu nhập",
                "số khách phải trả cho cả đơn",
            )
        ):
            return "order_total", False

        if "tiền giảm của cả dòng" in g_norm:
            return "line_discount", False

        if "giảm giá của cả đơn" in g_norm or "tiền bớt cho cả đơn" in g_norm:
            return "order_discount", False

        if g_norm == "giảm giá":
            return "discount", False

        if "phí giao hàng" in g_norm:
            return "shipping_fee", False

        if any(
            p in g_norm
            for p in (
                "khách đã trả",
                "số tiền khách trả",
                "đã thanh toán",
            )
        ):
            return "paid_amount", False

        if any(p in g_norm for p in ("còn nợ", "số khách còn nợ")):
            return "debt_amount", False

        if "tồn kho" in g_norm or "tồn cuối" in g_norm or "tồn thực" in g_norm:
            return "stock_qty", False

    # 3. Header-name fallback when column is not explicitly in ## Cột trong
    if _header_in(col_name, c_norm, {
        "mã đơn",
        "mã đơn hàng",
        "mã hóa đơn",
        "mã hoá đơn",
        "số hoá đơn",
        "số hóa đơn",
        "order id",
        "mã giao dịch",
        "mã bill",
        "mã phiếu",
        "số phiếu",
        "mã nhập hàng",
        "mã phiếu nhập",
        "phiếu thu",
        "mã phiếu thu",
        "maphieuthu",
        "số ct",
        "soct",
        "ma_don",
        "ma_hd",
        "so_hd",
        "ma_phieu",
        "mã chứng từ", "mã gd", "số hđ",
        "mahoadon",
        "đơn số",
        "ma don",
        "số đơn",
        "mã đơn",
        "maphieunhap",
        "mã đơn nhập",
    }):
        return "order_id", False

    if _header_in(col_name, c_norm, {
        "ngày",
        "ngày bán",
        "ngày đặt",
        "ngày đặt hàng",
        "ngày mua",
        "ngày nhập",
        "ngày chi",
        "ngày thu",
        "thời gian",
        "thời gian thu",
        "ngày tháng",
        "ngày giao",
        "ngày giao hàng",
        "ngày hđ",
        "ngày tt",
        "ngaygiothu",
        "thoigianlap",
        "ngaynhapkho",
        "ngày nhận",
        "ngày sửa",
        "ngày áp dụng",
        "ngay_ban",
        "ngay_dat",
        "ngay_nhap",
        "ngay_hd",
        "ngay_giao",
        "thoi_gian",
        "ngày giao dịch", "ngày lập", "ngày chở", "thời điểm",
        "ngay_ap_dung",
    }):
        return "date", True

    if _header_in(col_name, c_norm, {
        "hạn thanh toán",
        "hạn trả",
        "han tra",
        "ngày phải trả",
        "hsd",
        "hạn sử dụng",
        "ngày sinh",
        "ngày đăng ký",
        "ngày tạo",
        "thời gian tạo",
        "từ ngày",
        "đến ngày",
        "ngày trả",
        "ngày góp",
    }):
        return "secondary_date", False

    if _header_in(col_name, c_norm, {"giờ", "giờ tạo đơn", "giờ đặt", "giờ bán"}):
        return "time", False

    if _header_in(col_name, c_norm, {"trạng thái phiếu", "trạng thái nhập"}):
        return "purchase_status", False

    if _header_in(col_name, c_norm, {"trạng thái hđđt", "trạng thái hóa đơn điện tử", "trạng thái hoá đơn điện tử"}):
        return "einvoice_status", False

    if _header_in(col_name, c_norm, {
        "trạng thái",
        "trạng thái đơn",
        "trạng thái đơn hàng",
        "trạng thái xử lý",
        "trạng thái giao",
        "trang_thai_giao",
        "tt đơn",
        "tiến độ",
        "tình trạng",
        "tình trạng đơn",
        "tinhtrang",
        "ghi chú tt",
        "xử lý",
        "kết quả",
        "status",
        "trang_thai",
    }):
        return "status", False

    if _header_in(col_name, c_norm, {"ghi chú", "note", "mẫu ghi chú", "ghi_chu"}):
        return "notes", False

    if _header_in(col_name, c_norm, {
        "khách hàng",
        "tên khách hàng",
        "tên kh",
        "khách",
        "khách hàng tên",
        "khách hàng mua",
        "bên mua",
        "đối tác",
        "ten_kh",
    }):
        return "customer", False

    if _header_in(col_name, c_norm, {"mã kh", "mã khách", "mã khách hàng", "mã kh mua", "customer id", "ma_kh", "ma_khach", "makhachhang", "số kh", "mã khách mua", "mã khách lẻ sỉ"}):
        return "customer_id", False

    if _header_in(col_name, c_norm, {"số điện thoại", "điện thoại", "sđt", "sdt", "so_dien_thoai", "phone", "di động"}):
        return "phone", False

    if _header_in(col_name, c_norm, {
        "mã hàng",
        "ma hang",
        "mã hàng hóa",
        "mã hàng hoá",
        "mã h hóa",
        "mã sp",
        "ma_sp",
        "mã sản phẩm",
        "mã món",
        "mã hh",
        "mã vạch",
        "mã barcode",
        "ma_hang_hoa",
        "mã sku", "id sp", "sku", "mã mặt hàng", "mã sản phẩm bán", "mã hàng bán",
        "mahanghoa",
    }):
        return "item_id", False

    if _header_in(col_name, c_norm, {
        "tên hàng",
        "tên hàng hóa",
        "tên hàng hoá",
        "hàng hóa",
        "hàng hoá",
        "tên sản phẩm",
        "sản phẩm",
        "hàng",
        "chi tiết hàng",
        "nội dung",
        "món",
        "tên món",
        "product",
        "máy",
        "máy thu",
        "ten_sp",
        "ten_hang",
        "ten_hang_hoa",
        "tên mặt hàng", "tên sp", "hàng bán",
        "tenhanghoa",
    }):
        return "item_name", False

    if _header_in(col_name, c_norm, {
        "quy ra đv lẻ",
        "số lượng quy đổi",
        "sl cơ bản",
        "sl đv cơ bản",
        "sl quy ra lẻ",
        "slquydoi",
        "slnhapquydoi",
        "số lượng lẻ", "số lượng đvt cơ bản", "sl quy đổi", "sl lẻ",
        "sl nhập đvt cơ bản",
    }):
        return "base_qty", False

    if _header_in(col_name, c_norm, {"sl trả đvt cơ bản", "sl khách trả", "sltraquydoi", "số lượng trả", "số lượng trả đvt cơ bản"}):
        return "return_qty", False

    if _header_in(col_name, c_norm, {"số lượng", "so_luong", "sl", "slg", "số lượng sản phẩm", "sl nhập", "sl thực giao", "số lượng giao", "số lượng hàng", "sl bán", "số lượng bán", "s l", "số lượng nhập"}):
        return "qty", False

    if _header_in(col_name, c_norm, {"đơn vị tính", "đơn vị", "đvt", "đ vị", "đ.vị", "đvt nhập", "đơn vị quy đổi", "quy cách", "donvitinh", "đv nhập", "unit", "đv lớn", "đv tính"}):
        return "unit", False

    if _header_in(col_name, c_norm, {"đơn vị cơ bản", "đơn vị gốc", "đv gốc", "đv nhỏ", "donvicoso"}):
        return "base_unit", False

    if _header_in(col_name, c_norm, {"đơn giá", "giá bán", "giá", "giá sản phẩm", "giá niêm yết", "giá nhập", "giá mới", "giá cũ", "giá thu", "don_gia", "gia_ban", "giadonvi", "giabanle", "dongianhap", "giá đơn vị", "price"}):
        return "unit_price", False

    if _header_in(col_name, c_norm, {"giá vốn", "giá vốn lúc bán", "giá vốn hiện tại", "gia_von", "giavonlucban"}):
        return "cost_price", False

    if _header_in(col_name, c_norm, {
        "thành tiền",
        "thành tiền đ",
        "thanh tien",
        "thanh_tien",
        "trigia",
        "so tien",
        "số tiền",
        "tiền chi",
        "giá trị",
        "tiền thu",
        "số tiền thu",
        "sotienthu",
        "tiền", "amount", "trị giá", "t tiền", "tiền bán",
    }):
        return "line_amount", False

    if _header_in(col_name, c_norm, {
        "tổng tiền hàng",
        "tongtienhang",
        "tiền hàng trước giảm",
        "tổng sau giảm giá",
        "tổng tiền",
        "tong tien",
        "tổng phiếu",
        "tổng thanh toán",
        "tongthanhtoan",
        "tonggiatrinhap",
        "tổng đơn",
        "tổng bill",
        "phải thu",
        "cần trả ncc",
        "tien hang",
        "tong_tien",
    }):
        return "order_total", False

    if _header_in(col_name, c_norm, {"giảm giá hđ", "giảm giá đơn", "bớt"}):
        return "order_discount", False

    if _header_in(col_name, c_norm, {"giảm giá", "giảm đ", "khuyến mãi", "ck", "chietkhau", "chiết khấu", "tiền giảm"}):
        return "discount", False

    if _header_in(col_name, c_norm, {"phí giao", "phí ship", "phí vận chuyển", "phivanchuyen"}):
        return "shipping_fee", False

    if _header_in(col_name, c_norm, {"khách đã trả", "đã thu", "khách trả", "đã trả", "đã góp", "trả trước", "da tra"}):
        return "paid_amount", False

    if _header_in(col_name, c_norm, {"còn nợ", "còn phải thu", "conphaithu", "nợ", "chưa trả", "công nợ", "còn lại", "dư nợ"}):
        return "debt_amount", False

    if _header_in(col_name, c_norm, {"tồn cuối kỳ", "tồn cuối", "tồn thực", "ton kho", "tồn kho", "toncuoi"}):
        return "stock_qty", False

    if _header_in(col_name, c_norm, {"tỉ lệ quy đổi", "hệ số", "hệ số qđ", "hesoquydoi"}):
        return "conversion_factor", False

    if _header_in(col_name, c_norm, {"thứ tự"}):
        return "line_seq", False

    return "other", False


_YEAR_4_RE = re.compile(r"(?<!\d)(202[0-9])(?!\d)")


def extract_context_year(
    sheet_name: str | None = None,
    file_name: str | None = None,
    title_rows_text: str | None = None,
    fallback_year: int | None = None,
) -> int | None:
    """Infer a 4-digit context year for yearless d/m dates from sheet title, sheet name, or file name."""
    for candidate in (title_rows_text, sheet_name, file_name):
        if candidate:
            m = _YEAR_4_RE.search(candidate)
            if m:
                return int(m.group(1))
    return fallback_year
