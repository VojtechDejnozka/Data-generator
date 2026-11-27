"""Read-only SQL execution for the blind detective (defence in depth, spec §19).

1. separate observed database file (the detective never receives a path – only this module does);
2. SQLGlot parse: exactly one statement, a SELECT / UNION / WITH query, no DDL/DML/PRAGMA/ATTACH nodes;
3. the textual guard of the existing app (``storage.sql_guard.validate_select``);
4. SQLite ``mode=ro`` URI connection with an authorizer that allows only reads and an explicit
   allowlist of functions (no load_extension, readfile, fts, etc.);
5. wall-clock timeout (progress handler), row limit, query budget.
SQLGlot is a parser, not the security boundary – the authorizer is.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import sqlglot
from sqlglot import exp

from ..storage.sql_guard import SQLGuardError, validate_select

ALLOWED_FUNCTIONS = {
    "count", "sum", "avg", "total", "min", "max", "abs", "round", "coalesce", "ifnull", "nullif", "iif", "length",
    "lower", "upper", "substr", "substring", "trim", "ltrim", "rtrim", "replace", "instr", "date", "time", "datetime",
    "julianday", "strftime", "typeof", "group_concat", "printf", "format", "like", "glob", "sign", "log", "ln",
    "log10", "log2", "exp", "sqrt", "power", "pow", "floor", "ceil", "ceiling", "unixepoch", "cast", "row_number",
    "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "ntile", "percent_rank", "cume_dist",
    "nth_value", "mod", "pi", "trunc", "char", "unicode", "quote", "string_agg", "concat", "concat_ws", "likely",
    "unlikely", "zeroblob_disabled",
}
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Pragma,
                   exp.Attach, exp.Detach, exp.Transaction, exp.Commit, exp.Rollback)


class DetectiveSQLError(SQLGuardError):
    pass


def check_sql(sql: str) -> str:
    try:
        trees = [t for t in sqlglot.parse(sql, read="sqlite") if t is not None]
    except sqlglot.errors.ParseError as e:
        raise DetectiveSQLError(f"SQL parse error: {str(e)[:200]}") from e
    if len(trees) != 1:
        raise DetectiveSQLError("exactly one SQL statement is allowed")
    root = trees[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        raise DetectiveSQLError(f"only SELECT queries are allowed (got {type(root).__name__})")
    for node in root.walk():
        n = node[0] if isinstance(node, tuple) else node
        if isinstance(n, FORBIDDEN_NODES):
            raise DetectiveSQLError(f"forbidden statement {type(n).__name__}")
        if isinstance(n, exp.Anonymous) and str(n.name).lower() not in ALLOWED_FUNCTIONS:
            raise DetectiveSQLError(f"function {n.name} is not allowed")
    return validate_select(sql)


def _authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
        return sqlite3.SQLITE_OK
    if hasattr(sqlite3, "SQLITE_RECURSIVE") and action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if str(arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


@dataclass
class SQLResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int


class ReadOnlyDB:
    """The only handle the detective tools have on the observed database."""

    def __init__(self, path: Path, timeout_s: float = 10.0, max_queries: int = 200):
        self._path = Path(path)
        self.timeout_s = timeout_s
        self.max_queries = max_queries
        self.queries = 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, check_same_thread=False)
        conn.set_authorizer(_authorizer)
        return conn

    def query(self, sql: str, limit: int = 1000, internal: bool = False) -> SQLResult:
        if not internal:
            self.queries += 1
            if self.queries > self.max_queries:
                raise DetectiveSQLError(f"query budget of {self.max_queries} exhausted")
        sql = check_sql(sql)
        conn = self._connect()
        deadline = time.time() + self.timeout_s
        conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 5000)
        t0 = time.time()
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise DetectiveSQLError(f"query timed out after {self.timeout_s}s") from e
            raise DetectiveSQLError(str(e)[:300]) from e
        except sqlite3.DatabaseError as e:
            raise DetectiveSQLError(str(e)[:300]) from e
        finally:
            conn.close()
        return SQLResult(cols, [list(r) for r in rows[:limit]], len(rows) > limit, int((time.time() - t0) * 1000))

    def tables(self) -> list[str]:
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        finally:
            conn.close()


def write_observed_db(path: Path, tables: dict[str, Any], types: dict[str, dict[str, str]],
                      pks: Optional[dict[str, str]] = None, enforce: bool = False) -> None:
    """Write observed tables. By default without PK/FK constraints (corruption may violate them)."""
    import numpy as np
    import pandas as pd

    sql_t = {"float": "REAL", "int": "INTEGER", "bool": "INTEGER", "key": "INTEGER", "category": "TEXT",
             "text": "TEXT", "date": "TEXT"}
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        for name, df in tables.items():
            cols = []
            for c in df.columns:
                piece = f'"{c}" {sql_t.get(types.get(name, {}).get(c, "text"), "")}'.rstrip()
                cols.append(piece)
            if enforce and pks and pks.get(name) in df.columns:
                cols.append(f'PRIMARY KEY ("{pks[name]}")')
            conn.execute(f'CREATE TABLE "{name}" ({", ".join(cols)})')
            ph = ",".join("?" * len(df.columns))

            def py(v):
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    return None
                if isinstance(v, (np.bool_, bool)):
                    return int(v)
                if isinstance(v, np.integer):
                    return int(v)
                if isinstance(v, np.floating):
                    return float(v)
                return v

            conn.executemany(f'INSERT INTO "{name}" VALUES ({ph})',
                             ([py(v) for v in r] for r in df.astype(object).where(pd.notna(df), None)
                              .itertuples(index=False, name=None)))
            # ordinary (non-unique) indexes on key / time columns, as in a real analytical database
            for c in df.columns:
                if types.get(name, {}).get(c) in ("key", "date"):
                    conn.execute(f'CREATE INDEX "ix_{name}_{c}" ON "{name}"("{c}")')
        conn.commit()
    finally:
        conn.close()
