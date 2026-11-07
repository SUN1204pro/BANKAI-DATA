"""Bounded read-only SQLite execution, for SQL that the pipeline did not write itself.

Metrics in `src/metrics.py` run fixed queries. This sandbox stays for a future model-generated query path:
read-only URI mode, an authorizer blocking mutation and attachment, a deadline, and an output row limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from zalo_engine.postprocess import (
    normalize_comparison_string,
    normalize_comparison_string_compact,
    normalize_display_string,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SQLExecutionResult:
    """Result of executing a bounded read-only SQL query."""

    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error_code: str | None = None  # SYNTAX_ERROR | AUTHORIZER_DENIED | TIMEOUT | ROW_LIMIT | RUNTIME_ERROR
    error_message: str | None = None
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# SQLite Read-Only Authorizer & Deterministic Helper Functions
# ---------------------------------------------------------------------------

_ALLOWED_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "foreign_key_list",
        "database_list",
    }
)

_ALLOWED_ACTION_CODES = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
)
# Add SQLITE_RECURSIVE (33) if defined in the Python sqlite3 build
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTION_CODES = _ALLOWED_ACTION_CODES | {sqlite3.SQLITE_RECURSIVE}


def _readonly_authorizer(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    """SQLite authorizer callback permitting only read-only SELECT queries."""
    if action_code in _ALLOWED_ACTION_CODES:
        return sqlite3.SQLITE_OK
    if action_code == sqlite3.SQLITE_PRAGMA and arg1 and arg1.lower() in _ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _sql_norm_str(val: Any) -> str | None:
    if val is None:
        return None
    return normalize_comparison_string(str(val))


def _sql_norm_compact(val: Any) -> str | None:
    if val is None:
        return None
    return normalize_comparison_string_compact(str(val))


def _sql_iso_weekday(val: Any) -> int | None:
    if not val or not isinstance(val, str):
        return None
    try:
        return date.fromisoformat(val[:10]).isoweekday()
    except ValueError:
        return None


def _sql_days_between(d1: Any, d2: Any) -> int | None:
    if not d1 or not d2 or not isinstance(d1, str) or not isinstance(d2, str):
        return None
    try:
        dt1 = date.fromisoformat(d1[:10])
        dt2 = date.fromisoformat(d2[:10])
        return (dt1 - dt2).days
    except ValueError:
        return None


_CAPACITY_GB_RE = re.compile(r"\s+\d+\s*(?:GB|TB)\b.*$", re.IGNORECASE)


def _sql_phone_model_line(val: Any) -> str | None:
    """Rulebook §6 (Cửa hàng điện thoại):
    dòng máy = phần tên hàng đứng trước dung lượng `…GB`;
    tên không có phần `…GB` thì dòng máy là cả tên (`Nokia 105 4G`: `4G` là mạng, không phải dung lượng).
    """
    if not val or not isinstance(val, str):
        return None
    s = normalize_display_string(val)
    return _CAPACITY_GB_RE.sub("", s).strip()


def _sql_phone_brand(val: Any) -> str | None:
    """Rulebook §6 (Cửa hàng điện thoại):
    Hãng = chữ đầu tên hàng; máy `iPhone` là hãng Apple.
    """
    if not val or not isinstance(val, str):
        return None
    s = normalize_display_string(val)
    if not s:
        return None
    first_word = s.split()[0]
    if first_word.lower() == "iphone":
        return "Apple"
    return first_word


_TIME_HHMM_RE = re.compile(r"(?:^|\s|T)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?:\s|$)")
_EXCEL_SERIAL_TIME_RE = re.compile(r"^\d{4,6}\.(\d+)$")


def _sql_extract_time(val: Any) -> str | None:
    """Extract normalized `HH:MM:SS` time of day from time strings, ISO timestamps, or Excel serial floats."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    m = _TIME_HHMM_RE.search(s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3) or 0)
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    if _EXCEL_SERIAL_TIME_RE.match(s):
        try:
            frac = float(s) % 1.0
            total_sec = int(round(frac * 86400)) % 86400
            hh = total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        except ValueError:
            return None
    return None


def register_sql_helpers(conn: sqlite3.Connection) -> None:
    """Register deterministic scalar functions on a SQLite connection."""
    conn.create_function("NORM_STR", 1, _sql_norm_str, deterministic=True)
    conn.create_function("NORM_COMPACT", 1, _sql_norm_compact, deterministic=True)
    conn.create_function("ISO_WEEKDAY", 1, _sql_iso_weekday, deterministic=True)
    conn.create_function("DAYS_BETWEEN", 2, _sql_days_between, deterministic=True)
    conn.create_function("PHONE_MODEL_LINE", 1, _sql_phone_model_line, deterministic=True)
    conn.create_function("PHONE_BRAND", 1, _sql_phone_brand, deterministic=True)
    conn.create_function("EXTRACT_TIME", 1, _sql_extract_time, deterministic=True)


# ---------------------------------------------------------------------------
# Bounded Read-Only SQL Execution
# ---------------------------------------------------------------------------


def open_readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database in strict read-only URI mode with helpers registered."""
    p = Path(db_path).resolve()
    uri = f"{p.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    if hasattr(conn, "enable_load_extension"):
        try:
            conn.enable_load_extension(False)
        except sqlite3.Error:
            pass
    register_sql_helpers(conn)
    conn.set_authorizer(_readonly_authorizer)
    return conn


def execute_readonly_sql(
    db_path_or_conn: str | Path | sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] | dict[str, Any] = (),
    *,
    timeout_sec: float = 5.0,
    max_rows: int = 5000,
) -> SQLExecutionResult:
    """Execute a single read-only SQL query with authorizer, deadline, and row limits."""
    t0 = time.monotonic()
    sql_clean = (sql or "").strip()
    if not sql_clean:
        return SQLExecutionResult(
            ok=False,
            error_code="SYNTAX_ERROR",
            error_message="Empty SQL query.",
        )

    # Reject multi-statement queries
    semi_stripped = sql_clean.rstrip(";").strip()
    if ";" in semi_stripped:
        return SQLExecutionResult(
            ok=False,
            error_code="AUTHORIZER_DENIED",
            error_message="Multiple SQL statements are not allowed.",
        )

    owns_conn = False
    if isinstance(db_path_or_conn, (str, Path)):
        conn = open_readonly_connection(db_path_or_conn)
        owns_conn = True
    else:
        conn = db_path_or_conn
        conn.set_authorizer(_readonly_authorizer)

    deadline = t0 + timeout_sec
    timed_out = False

    def _progress_handler() -> int:
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    conn.set_progress_handler(_progress_handler, 1000)
    try:
        cur = conn.execute(semi_stripped, params)
        col_names = [desc[0] for desc in (cur.description or [])]
        fetched = cur.fetchmany(max_rows + 1)
        elapsed = (time.monotonic() - t0) * 1000.0
        if len(fetched) > max_rows:
            return SQLExecutionResult(
                ok=False,
                columns=col_names,
                error_code="ROW_LIMIT",
                error_message=f"Query exceeded max_rows={max_rows}.",
                elapsed_ms=elapsed,
            )
        return SQLExecutionResult(
            ok=True,
            columns=col_names,
            rows=list(fetched),
            elapsed_ms=elapsed,
        )
    except sqlite3.DatabaseError as exc:
        elapsed = (time.monotonic() - t0) * 1000.0
        msg = str(exc)
        if timed_out or "interrupted" in msg.lower():
            code = "TIMEOUT"
            msg = f"Query exceeded deadline of {timeout_sec:.2f}s."
        elif "not authorized" in msg.lower() or "attempt to write a readonly database" in msg.lower():
            code = "AUTHORIZER_DENIED"
        elif "syntax error" in msg.lower() or "no such table" in msg.lower() or "no such column" in msg.lower():
            code = "SYNTAX_ERROR"
        else:
            code = "RUNTIME_ERROR"
        return SQLExecutionResult(
            ok=False,
            error_code=code,
            error_message=msg,
            elapsed_ms=elapsed,
        )
    finally:
        conn.set_progress_handler(None, 0)
        if owns_conn:
            conn.close()
