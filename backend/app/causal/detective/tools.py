"""Validated, parametrised analysis tools of the blind detective.

The toolbox receives ONLY the observed database (through ``ReadOnlyDB``) and the neutral public
schema. Every call is recorded by the harness as an ``EvidenceItem`` (E1, E2, …) – the evaluator
checks cited evidence ids against this log, so evidence cannot be invented. No tool executes
arbitrary Python / shell code; data sets are defined by guarded read-only SQL.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
from scipy import stats

from ..discovery import discover
from ..results import EvidenceItem, PublicSchema
from ..sqlsafe import DetectiveSQLError, ReadOnlyDB


class ToolError(ValueError):
    pass


def _q(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ToolError(f"invalid identifier {name!r}")
    return f'"{name}"'


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 6)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else round(float(v), 6)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


class Toolbox:
    TOOLS = ["inspect_schema", "list_tables", "describe_table", "profile_column", "execute_sql",
             "get_missingness_report", "get_duplicate_report", "check_join", "compute_correlation",
             "fit_linear_regression", "fit_logistic_regression", "test_independence", "analyze_time_series",
             "discover_causal_graph", "create_plot"]

    def __init__(self, db: ReadOnlyDB, schema: PublicSchema, max_rows: int = 200, sample_rows: int = 30000,
                 alpha: float = 0.01, seed: int = 0):
        self.db = db
        self.schema = schema
        self.max_rows = max_rows
        self.sample_rows = sample_rows
        self.alpha = alpha
        self.seed = seed
        self.log: list[EvidenceItem] = []
        self._tables = {t.name: t for t in schema.tables}

    # ------------------------------------------------------------------ harness
    def call(self, tool: str, **args: Any) -> EvidenceItem:
        if tool not in self.TOOLS:
            item = EvidenceItem(evidence_id=f"E{len(self.log) + 1}", tool=tool, args=args, ok=False,
                                summary=f"unknown tool {tool}")
            self.log.append(item)
            return item
        fn: Callable[..., Any] = getattr(self, "_" + tool)
        try:
            result = fn(**args)
            ok, summary = True, self._summarize(tool, result)
        except (ToolError, DetectiveSQLError, TypeError, ValueError, KeyError, np.linalg.LinAlgError) as e:
            result, ok, summary = None, False, f"{type(e).__name__}: {str(e)[:300]}"
        item = EvidenceItem(evidence_id=f"E{len(self.log) + 1}", tool=tool, args=args, ok=ok, summary=summary,
                            result=result)
        self.log.append(item)
        return item

    @staticmethod
    def _summarize(tool: str, result: Any) -> str:
        s = json.dumps(result, ensure_ascii=False, default=str)
        return s[:1500]

    def _table(self, name: str):
        if name not in self._tables:
            raise ToolError(f"unknown table {name}")
        return self._tables[name]

    def frame(self, query: str, limit: Optional[int] = None) -> tuple[pd.DataFrame, bool]:
        r = self.db.query(query, limit=limit or self.sample_rows)
        return pd.DataFrame(r.rows, columns=r.columns), r.truncated

    # ------------------------------------------------------------------ schema
    def _inspect_schema(self) -> dict[str, Any]:
        return self.schema.model_dump()

    def _list_tables(self) -> list[dict[str, Any]]:
        return [{"table": t.name, "rows": t.rows, "columns": [c.name for c in t.columns]} for t in self.schema.tables]

    def _describe_table(self, table: str) -> dict[str, Any]:
        t = self._table(table)
        T = _q(table)
        parts = ", ".join(f"SUM(CASE WHEN {_q(c.name)} IS NULL THEN 1 ELSE 0 END)" for c in t.columns)
        r = self.db.query(f"SELECT COUNT(*), {parts} FROM {T}", limit=1, internal=True)
        n = r.rows[0][0]
        return {"table": table, "rows": n, "primary_key": t.primary_key, "foreign_keys": t.foreign_keys,
                "columns": [{"name": c.name, "type": c.type, "description": c.description,
                             "null_share": round(r.rows[0][i + 1] / n, 4) if n else None}
                            for i, c in enumerate(t.columns)]}

    def _profile_column(self, table: str, column: str) -> dict[str, Any]:
        self._table(table)
        df, trunc = self.frame(f"SELECT {_q(column)} AS v FROM {_q(table)}", limit=200000)
        s = df["v"]
        out: dict[str, Any] = {"table": table, "column": column, "n": int(len(s)), "nulls": int(s.isna().sum()),
                               "distinct": int(s.nunique()), "truncated": trunc}
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().mean() > 0.9 and s.notna().any():
            q = num.quantile([0, 0.01, 0.25, 0.5, 0.75, 0.99, 1]).tolist()
            iqr = q[4] - q[2]
            outl = int(((num < q[2] - 3 * iqr) | (num > q[4] + 3 * iqr)).sum()) if iqr > 0 else 0
            out.update({"mean": _clean(num.mean()), "sd": _clean(num.std()), "quantiles": [_clean(x) for x in q],
                        "outliers_3iqr": outl, "non_numeric": int((s.notna() & num.isna()).sum())})
        else:
            out["top_values"] = {str(k): int(v) for k, v in s.value_counts().head(12).items()}
        return out

    def _execute_sql(self, query: str) -> dict[str, Any]:
        r = self.db.query(query, limit=self.max_rows)
        return {"columns": r.columns, "rows": [[_clean(v) for v in row] for row in r.rows], "truncated": r.truncated,
                "elapsed_ms": r.elapsed_ms}

    def _get_missingness_report(self) -> list[dict[str, Any]]:
        out = []
        for t in self.schema.tables:
            d = self._describe_table(t.name)
            for c in d["columns"]:
                if c["null_share"]:
                    out.append({"table": t.name, "column": c["name"], "null_share": c["null_share"]})
        return out

    def _get_duplicate_report(self) -> list[dict[str, Any]]:
        out = []
        for t in self.schema.tables:
            T = _q(t.name)
            item: dict[str, Any] = {"table": t.name}
            if t.primary_key:
                pk = _q(t.primary_key[0])
                r = self.db.query(f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {T}", limit=1, internal=True)
                item["duplicate_primary_keys"] = r.rows[0][0]
            other = [c.name for c in t.columns if c.name not in t.primary_key]
            if other:
                cols = ", ".join(_q(c) for c in other)
                r = self.db.query(f"SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) AS c FROM {T} GROUP BY {cols} "
                                  f"HAVING COUNT(*) > 1)", limit=1, internal=True)
                item["duplicate_rows_ignoring_pk"] = r.rows[0][0]
            out.append(item)
        return out

    def _check_join(self, left_table: str, left_column: str, right_table: str, right_column: str) -> dict[str, Any]:
        self._table(left_table), self._table(right_table)
        L, R, lc, rc = _q(left_table), _q(right_table), _q(left_column), _q(right_column)
        r1 = self.db.query(f"SELECT MAX(c), AVG(c), COUNT(*) FROM (SELECT COUNT(*) AS c FROM {R} GROUP BY {rc})",
                           limit=1, internal=True)
        r2 = self.db.query(f"SELECT COUNT(*) FROM {L} a LEFT JOIN (SELECT DISTINCT {rc} AS k FROM {R}) b "
                           f"ON a.{lc} = b.k WHERE a.{lc} IS NOT NULL AND b.k IS NULL", limit=1, internal=True)
        r3 = self.db.query(f"SELECT COUNT(*) FROM {L}", limit=1, internal=True)
        r4 = self.db.query(f"SELECT COUNT(*) FROM {L} a JOIN {R} b ON a.{lc} = b.{rc}", limit=1, internal=True)
        max_per_key = r1.rows[0][0] or 0
        return {"right_rows_per_key_max": max_per_key, "right_rows_per_key_mean": _clean(r1.rows[0][1]),
                "left_orphans": r2.rows[0][0], "left_rows": r3.rows[0][0], "joined_rows": r4.rows[0][0],
                "multiplication_factor": round((r4.rows[0][0] or 0) / max(1, r3.rows[0][0]), 4),
                "many_to_one": max_per_key <= 1}

    # ------------------------------------------------------------------ statistics
    def _numeric_frame(self, query: str, columns: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
        df, trunc = self.frame(query)
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ToolError(f"columns {missing} not in the query result {list(df.columns)}")
        X = df[columns].apply(pd.to_numeric, errors="coerce")
        n0 = len(X)
        X = X.dropna()
        return X, {"rows": n0, "used": len(X), "truncated": trunc}

    def _compute_correlation(self, query: str, columns: list[str], method: str = "pearson") -> dict[str, Any]:
        X, meta = self._numeric_frame(query, columns)
        if method not in ("pearson", "spearman"):
            raise ToolError("method must be pearson or spearman")
        C = X.corr(method=method)
        return {**meta, "method": method, "matrix": {a: {b: _clean(C.loc[a, b]) for b in columns} for a in columns}}

    def _design(self, df: pd.DataFrame, features: list[str]) -> tuple[np.ndarray, list[str]]:
        cols, names = [np.ones(len(df))], ["intercept"]
        for f in features:
            s = df[f]
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().mean() >= 0.95:
                cols.append(num.to_numpy(float))
                names.append(f)
            else:
                levels = [x for x in s.dropna().astype(str).value_counts().index[1:12]]
                for lv in levels:
                    cols.append((s.astype(str) == lv).to_numpy(float))
                    names.append(f"{f}={lv}")
        return np.column_stack(cols), names

    def _fit_linear_regression(self, query: str, target: str, features: list[str]) -> dict[str, Any]:
        df, trunc = self.frame(query)
        need = [target, *features]
        if any(c not in df.columns for c in need):
            raise ToolError(f"columns {[c for c in need if c not in df.columns]} missing in {list(df.columns)}")
        df = df[need].copy()
        df[target] = pd.to_numeric(df[target], errors="coerce")
        df = df.dropna()
        if len(df) < len(features) + 5:
            raise ToolError("too few complete rows")
        X, names = self._design(df, features)
        ok = np.isfinite(X).all(axis=1)
        X, y = X[ok], df[target].to_numpy(float)[ok]
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        dof = max(1, len(y) - X.shape[1])
        s2 = resid @ resid / dof
        cov = s2 * np.linalg.pinv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
        tval = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
        pv = 2 * stats.t.sf(np.abs(tval), dof)
        r2 = 1 - resid.var() / y.var() if y.var() > 0 else 0.0
        return {"n": int(len(y)), "truncated": trunc, "r2": _clean(r2), "target": target,
                "coefficients": {nm: {"estimate": _clean(b), "se": _clean(s), "p": _clean(p)}
                                 for nm, b, s, p in zip(names, beta, se, pv)}}

    def _fit_logistic_regression(self, query: str, target: str, features: list[str]) -> dict[str, Any]:
        df, trunc = self.frame(query)
        need = [target, *features]
        if any(c not in df.columns for c in need):
            raise ToolError("missing columns")
        df = df[need].copy()
        y = pd.to_numeric(df[target], errors="coerce")
        df = df[y.notna()]
        y = y[y.notna()]
        if set(np.unique(y)) - {0, 1}:
            raise ToolError("target must be binary 0/1")
        df = df.dropna()
        X, names = self._design(df, features)
        yv = pd.to_numeric(df[target]).to_numpy(float)
        beta = np.zeros(X.shape[1])
        for _ in range(50):
            p = 1 / (1 + np.exp(-np.clip(X @ beta, -30, 30)))
            W = p * (1 - p) + 1e-9
            H = X.T @ (X * W[:, None])
            step = np.linalg.pinv(H) @ (X.T @ (yv - p))
            beta += step
            if np.max(np.abs(step)) < 1e-8:
                break
        cov = np.linalg.pinv(H)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
        z = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
        pv = 2 * stats.norm.sf(np.abs(z))
        return {"n": int(len(yv)), "truncated": trunc, "target": target,
                "coefficients": {nm: {"estimate": _clean(b), "se": _clean(s), "p": _clean(q)}
                                 for nm, b, s, q in zip(names, beta, se, pv)}}

    def _test_independence(self, query: str, x: str, y: str, given: Optional[list[str]] = None) -> dict[str, Any]:
        given = given or []
        df, trunc = self.frame(query)
        cols = [x, y, *given]
        if any(c not in df.columns for c in cols):
            raise ToolError("missing columns")
        num = df[cols].apply(pd.to_numeric, errors="coerce")
        if num.notna().all(axis=0).all() or num.notna().mean().min() > 0.95:
            X = num.dropna()
            if given:
                Z = np.column_stack([np.ones(len(X)), X[given].to_numpy(float)])
                rx = X[x] - Z @ np.linalg.lstsq(Z, X[x].to_numpy(float), rcond=None)[0]
                ry = X[y] - Z @ np.linalg.lstsq(Z, X[y].to_numpy(float), rcond=None)[0]
                r = float(np.corrcoef(rx, ry)[0, 1])
            else:
                r = float(np.corrcoef(X[x], X[y])[0, 1])
            from ..discovery import fisher_z_p
            p = fisher_z_p(r, len(X), len(given))
            return {"test": "partial correlation (Fisher-z)", "r": _clean(r), "p": _clean(p), "n": int(len(X)),
                    "independent_at_alpha": p > self.alpha, "truncated": trunc}
        if given:
            raise ToolError("conditional test for categorical data is not supported")
        tab = pd.crosstab(df[x].astype(str), df[y].astype(str))
        chi2, p, dof, _ = stats.chi2_contingency(tab)
        return {"test": "chi-square", "chi2": _clean(chi2), "dof": int(dof), "p": _clean(p), "n": int(tab.values.sum()),
                "independent_at_alpha": p > self.alpha, "truncated": trunc}

    def _analyze_time_series(self, query: str, time_column: str, value_column: str,
                             group_column: Optional[str] = None, min_segment: int = 5) -> dict[str, Any]:
        df, trunc = self.frame(query, limit=500000)
        for c in [time_column, value_column] + ([group_column] if group_column else []):
            if c not in df.columns:
                raise ToolError(f"column {c} not in the query result")
        df[value_column] = pd.to_numeric(df[value_column], errors="coerce")
        groups = df[group_column].unique().tolist() if group_column else [None]
        if len(groups) > 60:
            raise ToolError("too many groups (max 60)")
        out = []
        for g in groups:
            sub = df if g is None else df[df[group_column] == g]
            ser = sub.groupby(time_column)[value_column].mean().sort_index()
            ser = ser.dropna()
            if len(ser) < 2 * min_segment:
                continue
            y = ser.to_numpy(float)
            best = (0.0, None, 0.0, 0.0)
            for k in range(min_segment, len(y) - min_segment):
                a, b = y[:k], y[k:]
                se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)) or 1e-9
                t = (b.mean() - a.mean()) / se
                if abs(t) > abs(best[0]):
                    best = (t, ser.index[k], a.mean(), b.mean())
            slope = float(np.polyfit(np.arange(len(y)), y, 1)[0])
            out.append({"group": _clean(g), "n_periods": int(len(y)), "trend_slope": _clean(slope),
                        "change_point": str(best[1]) if best[1] is not None else None,
                        "mean_before": _clean(best[2]), "mean_after": _clean(best[3]), "t_stat": _clean(best[0])})
        return {"time_column": time_column, "value_column": value_column, "group_column": group_column,
                "series": out, "truncated": trunc,
                "note": "change point = split maximising the two-sample t statistic of period means (exploratory; "
                        "max-statistic p-values are optimistic)"}

    def _discover_causal_graph(self, query: str, columns: list[str], alpha: Optional[float] = None,
                               unit_columns: Optional[dict[str, str]] = None, temporal: bool = False) -> dict[str, Any]:
        df, trunc = self.frame(query)
        n_eff = None
        if unit_columns:
            n_eff = {c: int(df[u].nunique()) for c, u in unit_columns.items() if u in df.columns and c in df.columns}
        res = discover(df, columns, alpha=alpha or self.alpha, n_eff=n_eff, max_rows=self.sample_rows, seed=self.seed,
                       temporal=temporal)
        res.pop("sepsets", None)
        res["truncated"] = trunc
        return res

    def _create_plot(self, **_: Any) -> dict[str, Any]:
        return {"status": "unsupported", "reason": "plots are not available to the detective in this build"}
