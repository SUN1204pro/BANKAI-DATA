"""Safe SQL execution against the in-memory business database."""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass


class SqlError(Exception):
    pass


@dataclass
class SqlResult:
    cols: list[str]
    rows: list[tuple]
    truncated: bool = False


_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def _single_statement(sql: str) -> str:
    sql = _COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    in_q: str | None = None
    for ch in sql:
        if in_q:
            if ch == in_q:
                in_q = None
        elif ch in ("'", '"'):
            in_q = ch
        elif ch == ";":
            raise SqlError("only one SQL statement is allowed")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise SqlError("only SELECT / WITH queries are allowed")
    return sql


def run_sql(con: sqlite3.Connection, sql: str, timeout_s: float = 20.0, max_rows: int = 1000) -> SqlResult:
    sql = _single_statement(sql)
    deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.Error as e:
        raise SqlError(str(e)) from e
    finally:
        con.set_progress_handler(None, 0)
    return SqlResult(cols, [tuple(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
