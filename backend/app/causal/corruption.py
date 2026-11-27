"""Observation & corruption engine (spec §10).

``observe(world, config)`` never touches the clean world. It (1) projects the clean tables onto the
*observed* variables (latent variables and noise realisations are dropped – this projection alone is
the clean baseline) and (2) applies an explicit, ordered list of corruption operations from an
extensible registry. Every intervention is recorded in a private corruption manifest.

Common random numbers: each operation draws its cell/row uniforms from a stream keyed by
(seed, operation type, table, column) and indexed by the primary key, so changing an intensity
only moves the threshold; the same cells are hit first across candidate configurations.
"""
from __future__ import annotations

import datetime as dt
import math
import string
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from .contracts import CorruptionConfig, CorruptionOp, SCMSpec
from .engine import CleanWorld, own_key_columns, tables_hash
from .expressions import ExpressionError, evaluate
from .graph import is_temporal_entity
from .rng import stream, uniforms
from .results import ValidationCheck

MAX_LOGGED_VALUES = 20000


class CorruptionError(ValueError):
    pass


@dataclass
class TableInfo:
    name: str
    entity: str
    pk: str
    keys: list[str]
    fks: dict[str, str]          # column -> referenced table
    time_column: Optional[str]
    types: dict[str, str]        # column -> float|int|bool|category|text|date|key
    categories: dict[str, list[str]] = field(default_factory=dict)


def table_infos(spec: SCMSpec) -> dict[str, TableInfo]:
    out = {}
    tcol = spec.time.time_column if spec.time else "date"
    for e in spec.entities:
        if not e.export:
            continue
        keys = own_key_columns(spec, e.entity_id)
        fks = {}
        if e.kind == "child" and e.parent:
            fks[spec.entity(e.parent).primary_key] = spec.entity(e.parent).name
        for m in e.cross:
            fks[spec.entity(m).primary_key] = spec.entity(m).name
        for a in e.assignments:
            fks[a.column or spec.entity(a.entity).primary_key] = spec.entity(a.entity).name
        types = {k: "key" for k in keys}
        temporal = is_temporal_entity(spec, e.entity_id)
        if temporal:
            types[tcol] = "date"
        cats = {}
        for v in spec.variables_of(e.entity_id):
            if v.column and v.observability == "observed":
                types[v.column] = v.data_type
                if v.categories:
                    cats[v.column] = list(v.categories)
        out[e.name] = TableInfo(e.name, e.entity_id, e.primary_key, keys, fks, tcol if temporal else None, types, cats)
    return out


def observed_projection(world: CleanWorld) -> dict[str, pd.DataFrame]:
    """Clean tables restricted to the observed variables (+ keys and the time column)."""
    infos = table_infos(world.spec)
    return {name: world.tables[name][[c for c in world.tables[name].columns if c in info.types]].copy()
            for name, info in infos.items() if name in world.tables}


# =========================================================================== operation registry
@dataclass
class OpContext:
    op: CorruptionOp
    seed: int
    tables: dict[str, pd.DataFrame]
    infos: dict[str, TableInfo]
    clean: dict[str, pd.DataFrame]      # observed projection of the clean world (for MNAR true values)
    world: CleanWorld
    protected: set[str]                  # 'table.column'
    protect_keys: bool
    log: list[dict[str, Any]] = field(default_factory=list)
    cells_modified: int = 0
    rows_added: int = 0
    rows_removed: int = 0
    columns_hidden: int = 0
    eligible_cells: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def table(self) -> pd.DataFrame:
        return self.tables[self.op.target_table]

    @property
    def info(self) -> TableInfo:
        return self.infos[self.op.target_table]

    def u(self, *names: Any) -> np.ndarray:
        """Uniforms per row indexed by the primary key (stable under row insertions/deletions)."""
        df = self.table
        pk = pd.to_numeric(df[self.info.pk], errors="coerce").fillna(0).astype(np.int64).to_numpy() \
            if self.info.pk in df else np.arange(len(df))
        m = int(pk.max()) + 1 if len(pk) else 1
        base = uniforms(self.seed, m, "corrupt", self.op.type, self.op.target_table, *names)
        return base[np.clip(pk, 0, m - 1)]

    def rng(self, *names: Any) -> np.random.Generator:
        return stream(self.seed, "corrupt-rng", self.op.type, self.op.target_table, *names)

    def eligible_rows(self) -> np.ndarray:
        df = self.table
        mask = np.ones(len(df), dtype=bool)
        for expr in (self.op.row_filter, self.op.optional_condition):
            if expr:
                env = {c: df[c].to_numpy() for c in df.columns}
                try:
                    mask &= np.asarray(evaluate(expr, env, len(df))).astype(bool)
                except ExpressionError as e:
                    raise CorruptionError(f"{self.op.op_id}: invalid filter '{expr}' – {e}") from e
        return mask

    def columns(self, kinds: set[str], allow_keys: bool = False) -> list[str]:
        info = self.info
        cols = self.op.target_columns or [c for c, t in info.types.items()
                                          if t in kinds and t != "key" and c != info.time_column]
        out = []
        for c in cols:
            if c not in self.table.columns:
                self.notes.append(f"column {c} not in {info.name} (hidden or unknown) – skipped")
                continue
            t = info.types.get(c, "text")
            if (t == "key" or c == info.time_column and "date" not in kinds) and not allow_keys and self.protect_keys:
                self.notes.append(f"{c} is a key column and protected")
                continue
            if f"{info.name}.{c}" in self.protected or c in self.op.protected_columns:
                self.notes.append(f"{c} is protected")
                continue
            if t not in kinds and not (allow_keys and t == "key"):
                self.notes.append(f"{c} has type {t}, not supported by {self.op.type}")
                continue
            out.append(c)
        return out

    def record(self, column: Optional[str], rows: np.ndarray, old: Any = None, new: Any = None, kind: str = "cells") -> None:
        df = self.table
        ids = df[self.info.pk].to_numpy()[rows] if self.info.pk in df else rows
        entry: dict[str, Any] = {"op_id": self.op.op_id, "type": self.op.type, "table": self.op.target_table,
                                 "column": column, "kind": kind, "count": int(len(rows))}
        if len(rows) <= MAX_LOGGED_VALUES:
            entry["row_ids"] = [_py(x) for x in ids]
            if old is not None:
                entry["original"] = [_py(x) for x in np.asarray(old)]
            if new is not None:
                entry["new"] = [_py(x) for x in np.asarray(new)]
        else:
            entry["truncated"] = True
            entry["row_ids_head"] = [_py(x) for x in ids[:100]]
        self.log.append(entry)


def _py(v: Any) -> Any:
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


NUMERIC = {"float", "int"}
ANY_VALUE = {"float", "int", "bool", "category", "text", "date"}


def _set_cells(ctx: OpContext, col: str, rows: np.ndarray, new: Any) -> None:
    df = ctx.table
    if len(rows) == 0:
        return
    old = df[col].to_numpy()[rows].copy()
    s = df[col].astype(object) if not isinstance(new, np.ndarray) or new.dtype == object else df[col]
    if s.dtype != object and isinstance(new, np.ndarray) and new.dtype.kind == "f" and s.dtype.kind in "iub":
        s = s.astype(float)
    vals = s.to_numpy().copy()
    vals[rows] = new
    df[col] = vals
    ctx.cells_modified += len(rows)
    ctx.record(col, rows, old, new)


def _z(x: np.ndarray) -> np.ndarray:
    x = pd.to_numeric(pd.Series(x), errors="coerce").astype(float).to_numpy()
    sd = np.nanstd(x)
    return np.nan_to_num((x - np.nanmean(x)) / (sd if sd > 0 else 1.0))


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def op_missing_mcar(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    for col in ctx.columns(ANY_VALUE):
        ctx.eligible_cells += int(elig.sum())
        hit = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & ctx.table[col].notna().to_numpy())
        _set_cells(ctx, col, hit, np.full(len(hit), None, dtype=object))


def _prob_missing(ctx: OpContext, driver: np.ndarray) -> np.ndarray:
    strength = float(ctx.op.params.get("strength", 1.5))
    direction = 1.0 if ctx.op.params.get("direction", "high") == "high" else -1.0
    eta = _logit(ctx.op.probability) + direction * strength * _z(driver)
    p = 1 / (1 + np.exp(-eta))
    # rescale so that the average missing share equals the requested probability
    return np.clip(p * (ctx.op.probability / max(p.mean(), 1e-9)), 0, 1)


def op_missing_mar(ctx: OpContext) -> None:
    driver = ctx.op.params.get("driver")
    if not driver or driver not in ctx.table.columns:
        raise CorruptionError(f"{ctx.op.op_id}: MAR needs params.driver = an observed column of {ctx.op.target_table}")
    elig = ctx.eligible_rows()
    p = _prob_missing(ctx, ctx.table[driver].to_numpy())
    for col in ctx.columns(ANY_VALUE):
        if col == driver:
            continue
        ctx.eligible_cells += int(elig.sum())
        hit = np.flatnonzero(elig & (ctx.u(col) < p) & ctx.table[col].notna().to_numpy())
        _set_cells(ctx, col, hit, np.full(len(hit), None, dtype=object))


def op_missing_mnar(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    clean = ctx.clean[ctx.op.target_table].set_index(ctx.info.pk)
    pk = ctx.table[ctx.info.pk].to_numpy()
    for col in ctx.columns(NUMERIC | {"bool"}):
        src = ctx.op.params.get("driver_variable")
        if src:  # an unobserved driver (e.g. a latent variable of the same grain)
            v = ctx.world.spec.variable(src)
            if not v or ctx.world.spec.entity(v.entity).name != ctx.op.target_table:
                raise CorruptionError(f"{ctx.op.op_id}: driver_variable must live on {ctx.op.target_table}")
            internal = ctx.world.internals[v.entity].set_index(ctx.info.pk)[src]
            true_vals = internal.reindex(pk).to_numpy()
        else:
            true_vals = clean[col].reindex(pk).to_numpy()
        ctx.eligible_cells += int(elig.sum())
        p = _prob_missing(ctx, true_vals)
        hit = np.flatnonzero(elig & (ctx.u(col) < p) & ctx.table[col].notna().to_numpy())
        _set_cells(ctx, col, hit, np.full(len(hit), None, dtype=object))


def op_gaussian_noise(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    share = float(ctx.op.params.get("share", 1.0))
    for col in ctx.columns(NUMERIC):
        x = pd.to_numeric(ctx.table[col], errors="coerce").astype(float).to_numpy()
        sd = float(np.nanstd(x)) or 1.0
        rows = np.flatnonzero(elig & (ctx.u(col, "share") < share) & np.isfinite(x))
        ctx.eligible_cells += int(elig.sum())
        intensity = float(ctx.op.params.get("intensity", ctx.op.probability))
        if intensity <= 0 or len(rows) == 0:
            continue
        from scipy.stats import norm
        z = norm.ppf(ctx.u(col, "z"))[rows]
        new = x[rows] + intensity * sd * z
        if ctx.info.types[col] == "int":
            new = np.rint(new)
        _set_cells(ctx, col, rows, new)


def op_multiplicative_noise(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    for col in ctx.columns(NUMERIC):
        x = pd.to_numeric(ctx.table[col], errors="coerce").astype(float).to_numpy()
        rows = np.flatnonzero(elig & np.isfinite(x))
        ctx.eligible_cells += int(elig.sum())
        intensity = float(ctx.op.params.get("intensity", ctx.op.probability))
        if intensity <= 0 or len(rows) == 0:
            continue
        from scipy.stats import norm
        z = norm.ppf(ctx.u(col, "z"))[rows]
        new = x[rows] * np.exp(intensity * z)
        if ctx.info.types[col] == "int":
            new = np.rint(new)
        _set_cells(ctx, col, rows, new)


def op_outliers(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    factor = float(ctx.op.params.get("factor", 8.0))
    for col in ctx.columns(NUMERIC):
        x = pd.to_numeric(ctx.table[col], errors="coerce").astype(float).to_numpy()
        ctx.eligible_cells += int(elig.sum())
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & np.isfinite(x))
        sd = float(np.nanstd(x)) or 1.0
        sign = np.where(ctx.u(col, "sign")[rows] < 0.5, -1.0, 1.0) if ctx.op.params.get("two_sided", False) else 1.0
        new = np.nanmean(x) + sign * factor * sd * (1 + ctx.u(col, "mag")[rows])
        if ctx.info.types[col] == "int":
            new = np.rint(new)
        _set_cells(ctx, col, rows, new)


def op_rounding(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    digits = int(ctx.op.params.get("digits", -1))
    for col in ctx.columns(NUMERIC):
        x = pd.to_numeric(ctx.table[col], errors="coerce").astype(float).to_numpy()
        ctx.eligible_cells += int(elig.sum())
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & np.isfinite(x))
        new = np.round(x[rows], digits)
        changed = rows[new != x[rows]]
        _set_cells(ctx, col, changed, np.round(x[changed], digits))


def op_category_misclassification(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    confusion: dict[str, dict[str, float]] = ctx.op.params.get("confusion", {})
    for col in ctx.columns({"category", "bool"}):
        vals = ctx.table[col].to_numpy()
        cats = ctx.info.categories.get(col) or sorted({str(v) for v in vals if v is not None and not
                                                       (isinstance(v, float) and math.isnan(v))})
        is_bool = ctx.info.types[col] == "bool"
        if is_bool:
            cats = ["False", "True"]
        ctx.eligible_cells += int(elig.sum())
        present = pd.notna(ctx.table[col]).to_numpy()
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & present)
        if len(rows) == 0 or len(cats) < 2:
            continue
        uc = ctx.u(col, "to")[rows]
        new = []
        for v, q in zip(vals[rows], uc):
            cur = str(bool(v)) if is_bool else str(v)
            row = confusion.get(cur)
            if row:
                options, probs = list(row), np.asarray(list(row.values()), float)
                probs = probs / probs.sum()
            else:
                options = [c for c in cats if c != cur]
                probs = np.full(len(options), 1 / len(options))
            k = min(int(np.searchsorted(np.cumsum(probs), q, side="right")), len(options) - 1)
            new.append(options[k] == "True" if is_bool else options[k])
        _set_cells(ctx, col, rows, np.asarray(new, dtype=object))


def _append_rows(ctx: OpContext, rows_df: pd.DataFrame, kind: str, src_rows: np.ndarray) -> None:
    df = ctx.table
    ctx.tables[ctx.op.target_table] = pd.concat([df, rows_df], ignore_index=True)
    ctx.rows_added += len(rows_df)
    ctx.record(None, src_rows, kind=kind)


def op_exact_duplicates(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    rows = np.flatnonzero(elig & (ctx.u("row") < ctx.op.probability))
    ctx.eligible_cells += int(elig.sum())
    if len(rows):
        _append_rows(ctx, ctx.table.iloc[rows].copy(), "duplicate_rows", rows)


def op_near_duplicates(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    rows = np.flatnonzero(elig & (ctx.u("row") < ctx.op.probability))
    ctx.eligible_cells += int(elig.sum())
    if not len(rows):
        return
    dup = ctx.table.iloc[rows].copy()
    pk = ctx.info.pk
    if pk in dup and ctx.op.params.get("new_id", True):
        start = int(pd.to_numeric(ctx.table[pk], errors="coerce").max()) + 1
        dup[pk] = np.arange(start, start + len(dup))
    jitter = float(ctx.op.params.get("jitter", 0.02))
    for c, t in ctx.info.types.items():
        if c in dup and t in NUMERIC and f"{ctx.info.name}.{c}" not in ctx.protected:
            x = pd.to_numeric(dup[c], errors="coerce").astype(float)
            noise = 1 + jitter * (ctx.rng(c, "near").random(len(dup)) * 2 - 1)
            dup[c] = np.rint(x * noise) if t == "int" else np.round(x * noise, 2)
        if c in dup and t == "text":
            dup[c] = [(_typo(str(s), ctx.rng(c, "near", i)) if s is not None else s) for i, s in enumerate(dup[c])]
    _append_rows(ctx, dup, "near_duplicate_rows", rows)


def op_shuffle(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    group = ctx.op.params.get("group_by")
    for col in ctx.columns(ANY_VALUE):
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability))
        ctx.eligible_cells += int(elig.sum())
        if len(rows) < 2:
            continue
        vals = ctx.table[col].to_numpy()
        new = vals[rows].copy()
        rng = ctx.rng(col, "perm")
        if group and group in ctx.table.columns:
            g = ctx.table[group].to_numpy()[rows]
            for gv in pd.unique(g):
                idx = np.flatnonzero(g == gv)
                new[idx] = new[idx][rng.permutation(len(idx))]
        else:
            new = new[rng.permutation(len(new))]
        changed = new != vals[rows]
        _set_cells(ctx, col, rows[changed], new[changed])


def op_timestamp_shift(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    max_shift = int(ctx.op.params.get("max_shift", 3))
    cols = ctx.op.target_columns or ([ctx.info.time_column] if ctx.info.time_column else [])
    for col in cols:
        if col not in ctx.table or f"{ctx.info.name}.{col}" in ctx.protected:
            continue
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & ctx.table[col].notna().to_numpy())
        ctx.eligible_cells += int(elig.sum())
        q = ctx.u(col, "by")[rows]
        shift = np.where(q < 0.5, -1, 1) * (1 + (np.abs(q - 0.5) * 2 * max_shift).astype(int).clip(0, max_shift - 1))
        new = [(dt.date.fromisoformat(str(d)[:10]) + dt.timedelta(days=int(s))).isoformat()
               for d, s in zip(ctx.table[col].to_numpy()[rows], shift)]
        _set_cells(ctx, col, rows, np.asarray(new, dtype=object))


def _typo(s: str, rng: np.random.Generator) -> str:
    if len(s) < 2:
        return s
    i = int(rng.integers(0, len(s) - 1))
    k = rng.random()
    if k < 0.33:
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if k < 0.66:
        return s[:i] + s[i + 1:]
    return s[:i] + rng.choice(list(string.ascii_lowercase)) + s[i:]


def op_text_typos(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    per = int(ctx.op.params.get("typos_per_text", 2))
    for col in ctx.columns({"text"}):
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & ctx.table[col].notna().to_numpy())
        ctx.eligible_cells += int(elig.sum())
        new = []
        for r in rows:
            s = str(ctx.table[col].iat[r])
            g = ctx.rng(col, "typo", int(r))
            for _ in range(per):
                s = _typo(s, g)
            new.append(s)
        _set_cells(ctx, col, rows, np.asarray(new, dtype=object))


def op_text_truncation(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    for col in ctx.columns({"text"}):
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & ctx.table[col].notna().to_numpy())
        ctx.eligible_cells += int(elig.sum())
        q = ctx.u(col, "len")[rows]
        new = [str(s)[: max(1, int(len(str(s)) * (0.3 + 0.4 * x)))] for s, x in zip(ctx.table[col].to_numpy()[rows], q)]
        _set_cells(ctx, col, rows, np.asarray(new, dtype=object))


def op_inconsistent_formatting(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    for col in ctx.columns({"float", "int", "category", "text", "date"}):
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability) & ctx.table[col].notna().to_numpy())
        ctx.eligible_cells += int(elig.sum())
        t = ctx.info.types.get(col)
        vals = ctx.table[col].to_numpy()[rows]
        if t in NUMERIC:
            new = [f"{float(v):.2f}".replace(".", ",") for v in vals]
        elif t == "date":
            new = [dt.date.fromisoformat(str(v)[:10]).strftime("%d.%m.%Y") for v in vals]
        else:
            new = [f" {str(v).upper()} " for v in vals]
        _set_cells(ctx, col, rows, np.asarray(new, dtype=object))


def op_incorrect_fk(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    cols = ctx.op.target_columns or list(ctx.info.fks)
    for col in cols:
        if col not in ctx.info.fks or col not in ctx.table or f"{ctx.info.name}.{col}" in ctx.protected:
            ctx.notes.append(f"{col} is not an FK column / protected")
            continue
        ref = ctx.tables.get(ctx.info.fks[col])
        ref_ids = ref[ctx.infos[ctx.info.fks[col]].pk].dropna().unique() if ref is not None else np.array([])
        rows = np.flatnonzero(elig & (ctx.u(col) < ctx.op.probability))
        ctx.eligible_cells += int(elig.sum())
        if ctx.op.params.get("invalid", False) or len(ref_ids) < 2:
            new = np.asarray([-int(1 + i) for i in range(len(rows))], dtype=object)
        else:
            q = ctx.u(col, "to")[rows]
            new = np.asarray([int(ref_ids[min(int(x * len(ref_ids)), len(ref_ids) - 1)]) for x in q], dtype=object)
        cur = ctx.table[col].to_numpy()[rows]
        changed = new != cur
        _set_cells(ctx, col, rows[changed], new[changed])


def op_hidden_columns(ctx: OpContext) -> None:
    cols = ctx.columns(ANY_VALUE)
    hidden = [c for c in cols if ctx.op.probability >= 1.0 or
              uniforms(ctx.seed, 1, "corrupt", ctx.op.type, ctx.op.target_table, c, "hide")[0] < ctx.op.probability]
    df = ctx.table
    for col in hidden:
        ctx.columns_hidden += 1
        ctx.cells_modified += len(df)
        ctx.log.append({"op_id": ctx.op.op_id, "type": ctx.op.type, "table": ctx.op.target_table, "column": col,
                        "kind": "hidden_column", "count": int(len(df))})
    ctx.tables[ctx.op.target_table] = df.drop(columns=hidden)


def op_sampling(ctx: OpContext) -> None:
    keep = ctx.u("sample") >= ctx.op.probability
    _drop(ctx, ~keep, "sampled_out")


def op_row_deletion(ctx: OpContext) -> None:
    elig = ctx.eligible_rows()
    drop = elig & (ctx.u("delete") < ctx.op.probability)
    _drop(ctx, drop, "deleted_rows")


def _drop(ctx: OpContext, drop: np.ndarray, kind: str) -> None:
    rows = np.flatnonzero(drop)
    ctx.eligible_cells += len(drop)
    ctx.record(None, rows, kind=kind)
    ctx.rows_removed += len(rows)
    ctx.tables[ctx.op.target_table] = ctx.table.loc[~drop].reset_index(drop=True)


REGISTRY: dict[str, Callable[[OpContext], None]] = {
    "missing_values_mcar": op_missing_mcar, "missing_values_mar": op_missing_mar,
    "missing_values_mnar": op_missing_mnar, "gaussian_measurement_noise": op_gaussian_noise,
    "multiplicative_measurement_noise": op_multiplicative_noise, "outliers": op_outliers, "rounding": op_rounding,
    "category_misclassification": op_category_misclassification, "exact_duplicates": op_exact_duplicates,
    "near_duplicates": op_near_duplicates, "column_value_shuffle": op_shuffle, "timestamp_shift": op_timestamp_shift,
    "text_typos": op_text_typos, "text_truncation": op_text_truncation,
    "inconsistent_formatting": op_inconsistent_formatting, "incorrect_foreign_keys": op_incorrect_fk,
    "hidden_columns": op_hidden_columns, "sampling": op_sampling, "row_deletion": op_row_deletion,
}

INTENSITY_MEANING = {
    "missing_values_mcar": "share of cells set to NULL", "missing_values_mar": "average share of NULL cells",
    "missing_values_mnar": "average share of NULL cells",
    "gaussian_measurement_noise": "noise sd / column sd (params.intensity may exceed 1; params.share = affected cells)",
    "multiplicative_measurement_noise": "sd of log multiplicative error (params.intensity)", "outliers": "share of outlier cells",
    "rounding": "share of rounded cells", "category_misclassification": "share of recorded categories that are wrong",
    "exact_duplicates": "share of duplicated rows", "near_duplicates": "share of near-duplicated rows",
    "column_value_shuffle": "share of rows whose value is permuted", "timestamp_shift": "share of shifted timestamps",
    "text_typos": "share of texts with typos", "text_truncation": "share of truncated texts",
    "inconsistent_formatting": "share of reformatted cells", "incorrect_foreign_keys": "share of wrong FK values",
    "hidden_columns": "probability to hide each target column", "sampling": "share of rows not observed",
    "row_deletion": "share of eligible rows deleted",
}


# =========================================================================== observe
@dataclass
class Observation:
    tables: dict[str, pd.DataFrame]
    manifest: dict[str, Any]              # private – never shown to the detective
    realized: dict[str, Any]
    checks: list[ValidationCheck]
    analyzable: bool
    observed_hash: str


def critical_columns(spec: SCMSpec) -> set[str]:
    """'table.column' of observed variables used by story facts (for the observability budget)."""
    out = set()
    for f in spec.ground_truth.story_facts:
        for vid in [f.source, f.target, *f.evidence_variables]:
            col = spec.column_of(vid) if vid else None
            v = spec.variable(vid) if vid else None
            if col and v and v.observability == "observed":
                out.add(f"{col[0]}.{col[1]}")
    return out


def observe(world: CleanWorld, config: CorruptionConfig, seed: Optional[int] = None,
            allowed_types: Optional[list[str]] = None, minimum_observability: float = 0.0) -> Observation:
    spec = world.spec
    seed = int(config.seed if config.seed is not None else (world.seed if seed is None else seed))
    before_hash = tables_hash(world.tables)
    infos = table_infos(spec)
    clean = observed_projection(world)
    tables = {k: v.copy() for k, v in clean.items()}
    protected = set(config.protected_columns)
    for t in config.protected_tables:
        protected |= {f"{t}.{c}" for c in infos.get(t, TableInfo(t, "", "", [], {}, None, {})).types}
    log: list[dict[str, Any]] = []
    per_op: list[dict[str, Any]] = []
    checks: list[ValidationCheck] = []
    total_cells = sum(int(df.shape[0] * max(1, df.shape[1] - len(infos[n].keys))) for n, df in clean.items())
    ops = sorted(enumerate(config.operations), key=lambda x: (x[1].operation_order, x[0]))
    for _, op in ops:
        if allowed_types is not None and op.type not in allowed_types:
            checks.append(ValidationCheck(check_id=f"allowed:{op.op_id}", layer="observation", target=op.op_id,
                                          passed=False, severity="error",
                                          explanation=f"operation type {op.type} is not allowed"))
            continue
        if op.target_table not in tables:
            raise CorruptionError(f"{op.op_id}: unknown or non-exported table {op.target_table}")
        if op.target_table in config.protected_tables:
            checks.append(ValidationCheck(check_id=f"protected_table:{op.op_id}", layer="observation", target=op.op_id,
                                          passed=True, severity="info", explanation="table is protected – skipped"))
            continue
        fn = REGISTRY.get(op.type)
        if fn is None:
            raise CorruptionError(f"unsupported corruption type {op.type}")
        op_seed = int(op.seed if op.seed is not None else seed)
        ctx = OpContext(op=op, seed=op_seed, tables=tables, infos=infos, clean=clean, world=world,
                        protected=protected, protect_keys=config.protect_keys)
        fn(ctx)
        log += ctx.log
        per_op.append({"op_id": op.op_id, "type": op.type, "table": op.target_table, "columns": op.target_columns,
                       "requested": op.probability, "meaning": INTENSITY_MEANING.get(op.type, ""),
                       "params": op.params, "seed": op_seed, "order": op.operation_order,
                       "cells_modified": ctx.cells_modified, "rows_added": ctx.rows_added,
                       "rows_removed": ctx.rows_removed, "columns_hidden": ctx.columns_hidden,
                       "eligible_cells": ctx.eligible_cells,
                       "realized_share": round(ctx.cells_modified / ctx.eligible_cells, 6) if ctx.eligible_cells else 0.0,
                       "notes": sorted(set(ctx.notes))})
    # ----------------------------------------------------------------- post checks
    after_hash = tables_hash(world.tables)
    checks.append(ValidationCheck(check_id="clean_world_unchanged", layer="observation", target="clean world",
                                  expected=before_hash, observed=after_hash, passed=before_hash == after_hash,
                                  explanation="corruption must not modify the clean world"))
    prot_viol = 0
    for key in protected:
        t, c = key.split(".", 1)
        if t in tables and c in tables[t].columns and t in clean and c in clean[t].columns:
            pk = infos[t].pk
            a = clean[t].set_index(pk)[c]
            b = tables[t].drop_duplicates(subset=[pk]).set_index(pk)[c] if pk in tables[t] else None
            if b is not None:
                common = a.index.intersection(b.index)
                prot_viol += int((a.loc[common].astype(str) != b.loc[common].astype(str)).sum())
        elif t in clean and c in clean[t].columns and (t not in tables or c not in tables[t].columns):
            prot_viol += 1
    checks.append(ValidationCheck(check_id="protected_untouched", layer="observation", target="protected columns",
                                  observed=f"{prot_viol} violations", passed=prot_viol == 0,
                                  explanation="protected columns are unchanged"))
    for p in per_op:
        # measurement noise of small intensity does not destroy a cell: weight by the relative noise level
        w = 1.0
        if p["type"] in ("gaussian_measurement_noise", "multiplicative_measurement_noise"):
            w = min(1.0, float(p["params"].get("intensity", p["requested"])))
        p["weighted_cells"] = p["cells_modified"] * w
    modified = sum(p["weighted_cells"] for p in per_op) + sum(
        p["rows_added"] * max(1, clean[p["table"]].shape[1] - len(infos[p["table"]].keys)) +
        p["rows_removed"] * max(1, clean[p["table"]].shape[1] - len(infos[p["table"]].keys)) for p in per_op)
    share = modified / total_cells if total_cells else 0.0
    crit = critical_columns(spec)
    obs_share = []
    for key in sorted(crit):
        t, c = key.split(".", 1)
        n_clean = clean[t][c].notna().sum() if t in clean and c in clean[t] else 0
        n_obs = tables[t][c].notna().sum() if t in tables and c in tables[t] else 0
        obs_share.append(float(n_obs / n_clean) if n_clean else 1.0)
    observability = float(np.mean(obs_share)) if obs_share else 1.0
    analyzable = all(len(df) > 0 and len(df.columns) > len(infos[n].keys) for n, df in tables.items())
    checks.append(ValidationCheck(check_id="analyzable", layer="observation", target="observed dataset",
                                  passed=analyzable, explanation="every table has rows and at least one value column"))
    checks.append(ValidationCheck(check_id="minimum_observability", layer="observation", target="story-critical columns",
                                  expected=f">= {minimum_observability}", observed=f"{observability:.3f}",
                                  passed=observability >= minimum_observability,
                                  explanation="share of non-missing story-critical cells relative to the clean data"))
    for p in per_op:
        if p["type"] in ("missing_values_mcar", "category_misclassification", "exact_duplicates", "near_duplicates",
                         "outliers", "text_typos", "text_truncation", "incorrect_foreign_keys") and p["eligible_cells"]:
            checks.append(ValidationCheck(
                check_id=f"amount:{p['op_id']}", layer="observation", target=p["op_id"],
                expected=f"≈ {p['requested']:.3f}", observed=f"{p['realized_share']:.3f}",
                passed=True, severity="info", explanation="requested vs realised share (overlaps and already-missing "
                                                          "cells make them differ)"))
    realized = {"modified_cells_equivalent": int(modified), "total_cells": int(total_cells),
                "budget_rule": "cells × min(1, intensity) for measurement noise; added/removed rows × value columns",
                "realized_share": round(share, 6), "story_observability": round(observability, 4),
                "per_operation": per_op, "row_counts": {k: int(len(v)) for k, v in tables.items()}}
    manifest = {"seed": seed, "config": config.model_dump(mode="json"), "operations": per_op, "interventions": log,
                "world_hash": world.world_hash}
    return Observation(tables=tables, manifest=manifest, realized=realized, checks=checks, analyzable=analyzable,
                       observed_hash=tables_hash(tables))
