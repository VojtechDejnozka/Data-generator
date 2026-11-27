"""Read-only SQL execution for AI-written / user-written queries.

Defense in depth:
1. textual check – a single SELECT/WITH statement, no forbidden keywords;
2. SQLite opened with `mode=ro` URI;
3. an authorizer that only permits read operations;
4. a progress handler enforcing a wall-clock timeout, plus a row limit.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|create|alter|vacuum|reindex|"
    r"load_extension|writefile|readfile)\b",
    re.I,
)
PLACEHOLDER = re.compile(r"\{\{\s*(knob|meta):([a-zA-Z0-9_]+)\s*\}\}")


class SQLGuardError(ValueError):
    pass


def _strip(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return sql.strip()


def _without_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_select(sql: str) -> str:
    s = _strip(sql)
    s = s.rstrip(";").strip()
    bare = _without_strings(s)
    if ";" in bare:
        raise SQLGuardError("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", bare, re.I):
        raise SQLGuardError("only SELECT / WITH queries are allowed")
    m = FORBIDDEN.search(bare)
    if m:
        raise SQLGuardError(f"forbidden keyword: {m.group(1)}")
    return s


def render_placeholders(sql: str, knobs: dict[str, Any], meta: dict[str, Any]) -> str:
    def lit(v: Any) -> str:
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return repr(v)
        return str(v).replace("'", "''")

    def sub(m: re.Match) -> str:
        src = knobs if m.group(1) == "knob" else meta
        key = m.group(2)
        if key not in src:
            raise SQLGuardError(f"unknown placeholder {m.group(0)}")
        return lit(src[key])

    return PLACEHOLDER.sub(sub, sql)


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action, arg1, arg2, dbname, source):
    if action in _ALLOWED:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int
    sql: str

    def to_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows, "truncated": self.truncated,
                "elapsed_ms": self.elapsed_ms, "sql": self.sql}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.set_authorizer(_authorizer)
    return conn


def run_readonly(db_path: Path, sql: str, *, limit: int = 1000, timeout_s: float = 5.0,
                 knobs: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> QueryResult:
    sql = render_placeholders(sql, knobs or {}, meta or {})
    sql = validate_select(sql)
    conn = connect_readonly(db_path)
    deadline = time.time() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    t0 = time.time()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise SQLGuardError(f"query timed out after {timeout_s}s") from e
        raise SQLGuardError(str(e)) from e
    except sqlite3.DatabaseError as e:
        raise SQLGuardError(str(e)) from e
    finally:
        conn.close()
    truncated = len(rows) > limit
    rows = [list(r) for r in rows[:limit]]
    return QueryResult(cols, rows, truncated, int((time.time() - t0) * 1000), sql)
