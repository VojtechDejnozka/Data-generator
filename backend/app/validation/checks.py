"""Deterministic data checks: schema-derived (PK, FK, not-null, enum, range, row count) + spec invariants."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.spec import Invariant, ScenarioSpec
from ..storage.sql_guard import SQLGuardError, run_readonly


@dataclass
class CheckResult:
    id: str
    kind: str
    description: str
    passed: bool
    severity: str = "error"
    table: str | None = None
    violations: int = 0
    sample: list[Any] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _q(name: str) -> str:
    return '"' + name.replace('"', "") + '"'


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _sample(conn: sqlite3.Connection, sql: str, n: int = 5) -> list[Any]:
    return [list(r) for r in conn.execute(sql + f" LIMIT {n}").fetchall()]


def run_checks(db: Path, spec: ScenarioSpec, knobs: dict[str, Any] | None = None) -> list[CheckResult]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: list[CheckResult] = []
    meta = spec.meta.model_dump()
    try:
        for t in spec.tables:
            if t.name not in existing:
                out.append(CheckResult(id=f"exists:{t.name}", kind="exists", table=t.name, passed=False,
                                       description=f"Tabulka {t.name} existuje"))
                continue
            T = _q(t.name)
            n_rows = _count(conn, f"SELECT COUNT(*) FROM {T}")
            # primary key uniqueness / not null
            if t.pk:
                cols = ", ".join(_q(c) for c in t.pk)
                dup = _count(conn, f"SELECT COUNT(*) FROM (SELECT {cols} FROM {T} GROUP BY {cols} HAVING COUNT(*) > 1)")
                nulls = _count(conn, f"SELECT COUNT(*) FROM {T} WHERE " + " OR ".join(f"{_q(c)} IS NULL" for c in t.pk))
                out.append(CheckResult(id=f"pk:{t.name}", kind="unique", table=t.name, passed=dup == 0 and nulls == 0,
                                       violations=dup + nulls, description=f"Primární klíč {t.name}({', '.join(t.pk)}) je unikátní"))
            for c in t.columns:
                C = _q(c.name)
                if c.fk:
                    rt, rc = c.fk.split(".")
                    if rt not in existing:
                        continue
                    sql = (f"SELECT a.{C} FROM {T} a LEFT JOIN {_q(rt)} b ON b.{_q(rc)} = a.{C} "
                           f"WHERE a.{C} IS NOT NULL AND b.{_q(rc)} IS NULL")
                    v = _count(conn, f"SELECT COUNT(*) FROM ({sql})")
                    out.append(CheckResult(id=f"fk:{t.name}.{c.name}", kind="fk", table=t.name, passed=v == 0,
                                           violations=v, sample=_sample(conn, sql) if v else [],
                                           description=f"Referenční integrita {t.name}.{c.name} → {c.fk}"))
                if not c.nullable and not c.pk:
                    v = _count(conn, f"SELECT COUNT(*) FROM {T} WHERE {C} IS NULL")
                    if v:
                        out.append(CheckResult(id=f"notnull:{t.name}.{c.name}", kind="not_null", table=t.name,
                                               passed=False, violations=v,
                                               description=f"{t.name}.{c.name} nemá prázdné hodnoty"))
                if c.enum_values:
                    vals = ",".join("'" + v.replace("'", "''") + "'" for v in c.enum_values)
                    sql = f"SELECT DISTINCT {C} FROM {T} WHERE {C} IS NOT NULL AND {C} NOT IN ({vals})"
                    v = _count(conn, f"SELECT COUNT(*) FROM {T} WHERE {C} IS NOT NULL AND {C} NOT IN ({vals})")
                    out.append(CheckResult(id=f"enum:{t.name}.{c.name}", kind="enum", table=t.name, passed=v == 0,
                                           violations=v, sample=_sample(conn, sql) if v else [],
                                           description=f"{t.name}.{c.name} obsahuje jen povolené hodnoty"))
                if c.min is not None or c.max is not None:
                    cond = []
                    if c.min is not None:
                        cond.append(f"{C} < {c.min}")
                    if c.max is not None:
                        cond.append(f"{C} > {c.max}")
                    v = _count(conn, f"SELECT COUNT(*) FROM {T} WHERE {' OR '.join(cond)}")
                    if v:
                        out.append(CheckResult(id=f"range:{t.name}.{c.name}", kind="range", table=t.name,
                                               passed=False, violations=v, severity="warning",
                                               description=f"{t.name}.{c.name} v rozsahu [{c.min}, {c.max}]"))
                if c.type in ("date", "datetime"):
                    n = 10 if c.type == "date" else 19
                    v = _count(conn, f"SELECT COUNT(*) FROM {T} WHERE {C} IS NOT NULL AND "
                                     f"(length({C}) != {n} OR date(substr({C},1,10)) IS NULL)")
                    if v:
                        out.append(CheckResult(id=f"format:{t.name}.{c.name}", kind="format", table=t.name,
                                               passed=False, violations=v,
                                               description=f"{t.name}.{c.name} má platný formát data"))
            if t.row_count and t.strategy != "simulation":
                tol = 0 if t.strategy == "llm_dimension" else 0.2
                ok = abs(n_rows - t.row_count) <= tol * t.row_count
                out.append(CheckResult(id=f"rows:{t.name}", kind="row_count", table=t.name, passed=ok,
                                       severity="error" if t.strategy == "llm_dimension" else "warning",
                                       violations=0 if ok else abs(n_rows - t.row_count),
                                       description=f"Počet řádků {t.name}: {n_rows} (cíl {t.row_count})"))
            if n_rows == 0:
                out.append(CheckResult(id=f"empty:{t.name}", kind="row_count", table=t.name, passed=False,
                                       description=f"Tabulka {t.name} není prázdná"))
        for inv in spec.invariants:
            out.append(_invariant(conn, db, inv, spec, existing, knobs or {}, meta))
    finally:
        conn.close()
    return out


def _invariant(conn, db, inv: Invariant, spec: ScenarioSpec, existing: set[str], knobs, meta) -> CheckResult:
    base = dict(id=inv.id, kind=inv.kind, description=inv.description or inv.id, severity=inv.severity, table=inv.table)
    try:
        if inv.kind == "sql_zero_rows":
            res = run_readonly(db, f"SELECT * FROM ({inv.sql}) LIMIT 1000", limit=1000, timeout_s=20, knobs=knobs,
                               meta=meta)
            n = len(res.rows)
            return CheckResult(**base, passed=n == 0, violations=n, sample=res.rows[:5])
        if inv.table not in existing:
            return CheckResult(**base, passed=False, error=f"unknown table {inv.table}")
        T, C = _q(inv.table), _q(inv.column or "")
        if inv.kind == "compare":
            sql = f"SELECT * FROM {T} WHERE NOT ({C} {inv.op} {_q(inv.other_column or '')})"
        elif inv.kind == "range":
            cond = []
            if inv.min is not None:
                cond.append(f"{C} < {inv.min}")
            if inv.max is not None:
                cond.append(f"{C} > {inv.max}")
            sql = f"SELECT * FROM {T} WHERE " + (" OR ".join(cond) or "0")
        elif inv.kind == "not_null":
            sql = f"SELECT * FROM {T} WHERE {C} IS NULL"
        elif inv.kind == "unique":
            sql = f"SELECT {C} FROM {T} GROUP BY {C} HAVING COUNT(*) > 1"
        elif inv.kind == "fk":
            rt, rc = (inv.ref or ".").split(".")
            sql = (f"SELECT a.{C} FROM {T} a LEFT JOIN {_q(rt)} b ON b.{_q(rc)} = a.{C} "
                   f"WHERE a.{C} IS NOT NULL AND b.{_q(rc)} IS NULL")
        elif inv.kind == "row_count":
            t = spec.table(inv.table)
            n = _count(conn, f"SELECT COUNT(*) FROM {T}")
            lo = inv.min if inv.min is not None else (t.row_count if t and t.row_count else 1)
            hi = inv.max if inv.max is not None else (t.row_count if t and t.row_count else float("inf"))
            ok = lo <= n <= hi
            return CheckResult(**base, passed=ok, violations=0 if ok else 1, sample=[n])
        else:  # pragma: no cover
            return CheckResult(**base, passed=False, error="unknown kind")
        n = _count(conn, f"SELECT COUNT(*) FROM ({sql})")
        return CheckResult(**base, passed=n == 0, violations=n, sample=_sample(conn, sql) if n else [])
    except (sqlite3.Error, SQLGuardError, ValueError) as e:
        return CheckResult(**base, passed=False, error=str(e)[:300])


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    errors = [r for r in results if not r.passed and r.severity == "error"]
    warnings = [r for r in results if not r.passed and r.severity == "warning"]
    fk = [r for r in results if r.kind == "fk"]
    return {
        "total": len(results), "passed": sum(r.passed for r in results), "errors": len(errors),
        "warnings": len(warnings),
        "fk_integrity_pct": round(100.0 * sum(r.passed for r in fk) / len(fk), 1) if fk else 100.0,
        "failed_ids": [r.id for r in results if not r.passed],
    }
