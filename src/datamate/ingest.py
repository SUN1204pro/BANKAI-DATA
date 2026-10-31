"""Load a business data package (CSV / Excel / SQLite) into a clean in-memory SQLite DB.

The data directory is mounted read-only in the competition, so nothing is
written next to the sources: every table is cleaned in pandas and copied into
`sqlite3.connect(":memory:")`.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .ground import Grounder
from .units import UnitCatalog
from .normalize import norm, unaccent
from .parsing import parse_date, parse_number, parse_time

log = logging.getLogger("datamate.ingest")

DATA_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".sqlite", ".sqlite3", ".db"}


@dataclass
class Business:
    id: str
    readme: str
    tables: dict[str, pd.DataFrame]
    kinds: dict[str, dict[str, str]]       # table -> column -> number | date | text
    origin: dict[str, dict[str, str]]      # table -> clean column -> original header
    sources: dict[str, str]                # table -> source file name
    con: sqlite3.Connection
    schema_card: str = ""
    grounder: Grounder | None = None
    units: UnitCatalog | None = None
    date_range: tuple[str, str] | None = None  # first/last day with transactions (ISO), see event_date_range
    warnings: list[str] = field(default_factory=list)

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ naming
def sanitize(name: str, fallback: str = "col") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", norm(name)).strip("_")
    if not s:
        s = fallback
    if s[0].isdigit():
        s = f"{fallback}_{s}"
    return s


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


# ----------------------------------------------------------------- reading
def _read_csv(path: Path) -> pd.DataFrame:
    last: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1258", "latin-1"):
        for sep in (None, ","):
            try:
                return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=enc, sep=sep, engine="python")
            except Exception as e:  # noqa: BLE001
                last = e
    raise last  # type: ignore[misc]


def _detect_header(df: pd.DataFrame, max_scan: int = 10) -> pd.DataFrame:
    """Excel sheets often start with a title row; use the first mostly-text row as header."""
    best = 0
    for i in range(min(max_scan, len(df))):
        row = df.iloc[i]
        non_null = row.dropna()
        if len(non_null) >= max(2, int(0.6 * df.shape[1])) and all(isinstance(v, str) for v in non_null):
            best = i
            break
    header = [str(v).strip() if v is not None and v == v else f"col_{j}" for j, v in enumerate(df.iloc[best])]
    body = df.iloc[best + 1:].copy()
    body.columns = header
    return body.reset_index(drop=True)


def _read_excel(path: Path) -> dict[str, pd.DataFrame]:
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    return {name: _detect_header(df) for name, df in sheets.items() if not df.empty}


def _read_sqlite(path: Path) -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: pd.read_sql_query(f'SELECT * FROM "{n}"', con) for n in names}
    finally:
        con.close()


# ---------------------------------------------------------------- cleaning
def _clean_cell(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def clean_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """Return (clean df with sanitized names, kinds, origin names)."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
            continue
        df[c] = s.astype(object).map(_clean_cell)
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

    kinds: dict[str, str] = {}
    cols: dict[str, pd.Series] = {}
    times: dict[str, pd.Series] = {}   # date column -> its time-of-day part (only kept if the data has one)
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            cols[c], kinds[c] = s.dt.strftime("%Y-%m-%d").astype(object), "date"
            times[c] = s.map(parse_time)
            continue
        if pd.api.types.is_bool_dtype(s):
            cols[c], kinds[c] = s.astype(int), "number"
            continue
        if pd.api.types.is_numeric_dtype(s):
            cols[c], kinds[c] = s, "number"
            continue
        nn = s.dropna()
        if nn.empty:
            cols[c], kinds[c] = s, "text"
            continue
        leading_zero = nn.map(lambda v: isinstance(v, str) and len(v) > 1 and v.isdigit() and v.startswith("0")).any()
        nums = nn.map(parse_number)
        if not leading_zero and nums.notna().mean() >= 0.9:
            vals = s.map(parse_number)
            ok = vals.dropna()
            if len(ok) == len(vals) and (ok % 1 == 0).all() and ok.abs().max() < 1e15:
                vals = vals.astype("int64")
            else:
                vals = vals.astype("float64")
            cols[c], kinds[c] = vals, "number"
            continue
        dates = nn.map(parse_date)
        if dates.notna().mean() >= 0.9:
            cols[c] = s.map(lambda v: (lambda d: d.isoformat() if d else None)(parse_date(v)))
            kinds[c] = "date"
            times[c] = s.map(parse_time)
            continue
        cols[c], kinds[c] = s.map(lambda v: None if v is None else str(v)), "text"

    # Rulebook: "ca sáng / ca chiều" depends on the order's time of day, so keep it as <date col>_time.
    entries: list[tuple[str, pd.Series, str, str]] = []  # (raw name, values, kind, original header)
    for c in df.columns:
        entries.append((sanitize(c), cols[c], kinds[c], c))
        tm = times.get(c)
        if tm is not None and tm.notna().sum() >= 0.3 * max(int(cols[c].notna().sum()), 1):
            entries.append((sanitize(c) + "_time", tm, "text", f"{c} (giờ trong ngày)"))
    new_names = _dedupe([e[0] for e in entries])
    out = pd.DataFrame({n: e[1].values for n, e in zip(new_names, entries)})
    kinds_clean = {n: e[2] for n, e in zip(new_names, entries)}
    origin = {n: e[3] for n, e in zip(new_names, entries)}
    return out, kinds_clean, origin


# ------------------------------------------------------------ schema card
def _short(v, n: int = 40) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def build_schema_card(biz: Business) -> str:
    lines: list[str] = []
    for tname, df in biz.tables.items():
        dup = int(df.duplicated().sum())
        head = f"TABLE {tname} ({len(df)} rows" + (f", {dup} fully duplicated rows" if dup else "") + f") [source: {biz.sources.get(tname, '?')}]"
        lines.append(head)
        for col in df.columns:
            s = df[col]
            kind = biz.kinds[tname][col]
            nulls = int(s.isna().sum())
            orig = biz.origin[tname].get(col, col)
            tag = f'orig header "{orig}"' if orig != col else ""
            if kind == "number":
                desc = f"number; min {s.min():g}, max {s.max():g}" if s.notna().any() else "number; all null"
            elif kind == "date":
                desc = f"date (ISO text); {s.dropna().min()} .. {s.dropna().max()}" if s.notna().any() else "date; all null"
            else:
                vc = s.dropna().value_counts()
                nd = len(vc)
                if nd <= 12:
                    desc = f"text; {nd} distinct: " + ", ".join(f"'{_short(v)}'" for v in vc.index)
                else:
                    desc = f"text; {nd} distinct, e.g. " + ", ".join(f"'{_short(v)}'" for v in vc.index[:6])
            extras = ", ".join(x for x in (tag, f"{nulls} nulls" if nulls else "") if x)
            lines.append(f"  - {col} [{desc}]" + (f" ({extras})" if extras else ""))
        lines.append("")
    return "\n".join(lines).strip()


# ----------------------------------------------------------- data coverage
# Date columns that are not transactions: birthdays, expiry/due dates, price effective dates, record creation.
_NON_EVENT_COL = re.compile(r"sinh|hansu|hsd|khaitruong|nhanviec|apdung|taoluc|capnhat|ngaytao$")
_MASTER_TABLE = re.compile(r"khach|sanpham|hanghoa|nhanvien|chinhanh|danhmuc|quydoi|thuoc$|banggia|lichsugia|lohang")


def event_date_range(tables: dict[str, pd.DataFrame], kinds: dict[str, dict[str, str]]) -> tuple[str, str] | None:
    """Earliest and latest transaction date in the package (the "khoảng ngày có dữ liệu của shop", rulebook §7)."""
    lo = hi = None
    for t, df in tables.items():
        if _MASTER_TABLE.search(t.replace("_", "")):
            continue  # product / customer / price lists: their dates say when a record was made, not when business happened
        for c, kind in kinds[t].items():
            cc = c.replace("_", "")
            if kind != "date" or _NON_EVENT_COL.search(cc) or cc.startswith("han"):
                continue
            s = df[c].dropna()
            if s.empty:
                continue
            a, b = s.min(), s.max()
            lo = a if lo is None or a < lo else lo
            hi = b if hi is None or b > hi else hi
    return (lo, hi) if lo else None


# -------------------------------------------------------------------- main
def _register_functions(con: sqlite3.Connection) -> None:
    con.create_function("unaccent", 1, lambda x: None if x is None else unaccent(x), deterministic=True)
    con.create_function("norm", 1, lambda x: None if x is None else norm(x), deterministic=True)


def load_business(directory: str | Path, business_id: str) -> Business:
    directory = Path(directory)
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    readme = ""
    for p in files:
        if p.name.lower() in ("readme.md", "readme.txt"):
            readme = p.read_text(encoding="utf-8", errors="replace")[:30000]
            break

    raw: list[tuple[str, str, pd.DataFrame]] = []  # (table name, source, df)
    warnings: list[str] = []
    for p in files:
        suf = p.suffix.lower()
        if suf not in DATA_SUFFIXES:
            continue
        try:
            if suf in (".csv", ".tsv"):
                raw.append((p.stem, p.name, _read_csv(p)))
            elif suf in (".xlsx", ".xlsm", ".xls"):
                sheets = _read_excel(p)
                for sname, df in sheets.items():
                    tname = p.stem if len(sheets) == 1 else f"{p.stem}_{sname}"
                    raw.append((tname, f"{p.name}:{sname}", df))
            else:
                for tname, df in _read_sqlite(p).items():
                    raw.append((tname, f"{p.name}:{tname}", df))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"could not read {p.name}: {e}")
            log.warning("could not read %s: %s", p, e)

    con = sqlite3.connect(":memory:")
    _register_functions(con)
    tables: dict[str, pd.DataFrame] = {}
    kinds: dict[str, dict[str, str]] = {}
    origin: dict[str, dict[str, str]] = {}
    sources: dict[str, str] = {}
    names = _dedupe([sanitize(t, "table") for t, _, _ in raw])
    for tname, (_, src, df) in zip(names, raw):
        clean, k, o = clean_frame(df)
        if clean.empty:
            continue
        clean.to_sql(tname, con, index=False)
        tables[tname], kinds[tname], origin[tname], sources[tname] = clean, k, o, src
    con.execute("PRAGMA query_only = ON")

    biz = Business(business_id, readme, tables, kinds, origin, sources, con, warnings=warnings)
    biz.schema_card = build_schema_card(biz)
    biz.grounder = Grounder(tables, origin=origin)
    biz.date_range = event_date_range(tables, kinds)
    biz.units = UnitCatalog.from_business(biz)
    return biz
