"""Four separate validation layers (spec §8).

1. **Spec validation** – references, types, mechanisms, grains/aggregations, lags, acyclicity within a
   time step, events, story facts, dataset size.
2. **Relational validation** – primary keys, referential integrity, row counts, cardinality of child
   grains (= realised count variable), domains and ranges, time grid.
3. **Mechanistic validation** – recomputes every mechanism from the stored exogenous uniforms through
   an *independent* path: parents are aligned by pandas joins over the declared FK structure (not by
   the engine's index maps), and formulas are re-implemented here. Mismatches are hard errors.
4. **Statistical validation** – moments/KS of exogenous draws and residuals, calibration of binary
   and count mechanisms, category frequencies, coefficient recovery. Holm correction; failures are
   *warnings* only (correct data may fail a test; weak signal is not an error).

Validity is not the same as analytical recoverability – see ``app.causal.evaluation``.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats

from . import distributions as D
from . import mechanisms as M
from .contracts import SCMSpec, VariableSpec
from .engine import TIME_KEY, CleanWorld, identity_columns, key_columns, own_key_columns, relation
from .expressions import ExpressionError, check_expr, evaluate
from .graph import GraphError, check_lags, descendants, execution_plan, is_dynamic_entity, is_temporal_entity
from .results import ValidationReport

MAX_TOTAL_ROWS = 3_000_000
LARGE_MIN_ROWS = 100_000
STAT_FAMILY_ALPHA = 0.001


# =========================================================================== 1. spec validation
def estimate_rows(spec: SCMSpec) -> dict[str, Optional[int]]:
    T = spec.time.periods if spec.time else 1
    out: dict[str, Optional[int]] = {}

    def rows(eid: str, depth: int = 0) -> Optional[int]:
        if eid in out:
            return out[eid]
        e = spec.entity(eid)
        if not e or depth > 20:
            return None
        if e.kind == "base":
            r: Optional[int] = e.count or 0
        elif e.kind == "cross":
            sizes = [rows(m, depth + 1) for m in e.cross]
            r = int(np.prod([s or 0 for s in sizes])) * (T if e.temporal else 1) if all(s is not None for s in sizes) else None
        else:
            pr = rows(e.parent or "", depth + 1)
            r = None if e.count_variable or pr is None else pr * (e.count if e.count is not None else 1)
        out[eid] = r
        return r

    for e in spec.entities:
        rows(e.entity_id)
    return out


def require_large_plan(spec: SCMSpec) -> None:
    estimates = estimate_rows(spec)
    fixed_rows = sum(estimates[e.entity_id] or 0 for e in spec.entities if e.export)
    if fixed_rows < LARGE_MIN_ROWS:
        raise ValueError(f"Velký dataset vyžaduje alespoň {LARGE_MIN_ROWS:,} řádků v exportovaných tabulkách. "
                         f"Plán zaručuje pouze {fixed_rows:,}. Zvětšete počty entit nebo počet období.")
    exported = [e for e in spec.entities if e.export]
    substantial = [e for e in exported if (estimates[e.entity_id] or 0) >= 5000]
    if len(exported) < 5 or len(substantial) < 2:
        raise ValueError("Velký dataset potřebuje alespoň 5 věcně odlišných exportovaných tabulek "
                         "a alespoň dvě s 5 000 nebo více plánovanými řádky. Rozdělte fakta podle významu řádku.")


def validate_spec(spec: SCMSpec) -> ValidationReport:
    rep = ValidationReport()

    def err(cid: str, target: str, msg: str, fix: str = "", severity: str = "error") -> None:
        rep.add(check_id=cid, layer="spec", target=target, passed=False, severity=severity, explanation=msg,
                suggested_fix=fix)

    ids = [e.entity_id for e in spec.entities]
    names = [e.name for e in spec.entities]
    for dup in {x for x in ids if ids.count(x) > 1}:
        err("entity.duplicate", dup, f"duplicate entity_id {dup}")
    for dup in {x for x in names if names.count(x) > 1}:
        err("table.duplicate", dup, f"duplicate table name {dup}")
    pks = [e.primary_key for e in spec.entities]
    for dup in {x for x in pks if pks.count(x) > 1}:
        err("pk.duplicate", dup, f"primary key name {dup} used by several entities (key columns are joined by name)",
            "give each entity a unique primary key name")
    tcol = spec.time.time_column if spec.time else "date"
    by_id = {e.entity_id: e for e in spec.entities}
    for e in spec.entities:
        tgt = f"entity:{e.entity_id}"
        if e.kind == "base" and e.count is None:
            err("entity.count", tgt, "base entity needs 'count'")
        if e.kind == "cross":
            if not e.cross:
                err("entity.cross", tgt, "cross entity needs member entities")
            for m in e.cross:
                me = by_id.get(m)
                if not me:
                    err("entity.cross_member", tgt, f"unknown cross member {m}")
                elif is_dynamic_entity(spec, m) or (me.kind == "cross" and me.temporal):
                    err("entity.cross_member", tgt, f"cross member {m} must be a static grain with a fixed row count")
            if e.temporal and not spec.time:
                err("entity.time", tgt, "temporal grain requires a time configuration")
        if e.kind == "child":
            if not e.parent or e.parent not in by_id:
                err("entity.parent", tgt, f"child entity needs an existing parent (got {e.parent})")
            if e.count_variable:
                cv = spec.variable(e.count_variable)
                if not cv:
                    err("entity.count_variable", tgt, f"unknown count variable {e.count_variable}")
                elif cv.entity != e.parent:
                    err("entity.count_variable", tgt, f"count variable {cv.variable_id} must live on the parent grain {e.parent}")
                elif cv.data_type not in ("int", "bool"):
                    err("entity.count_variable", tgt, f"count variable {cv.variable_id} must be int or bool")
            elif e.count is None:
                err("entity.count", tgt, "child entity needs 'count' (rows per parent) or 'count_variable'")
        for a in e.assignments:
            te = by_id.get(a.entity)
            if not te:
                err("entity.assignment", tgt, f"assignment to unknown entity {a.entity}")
            elif is_dynamic_entity(spec, a.entity) or is_temporal_entity(spec, a.entity):
                err("entity.assignment", tgt, f"assignment target {a.entity} must be static with a fixed row count")
            if a.method == "distinct_per_parent" and e.kind != "child":
                err("entity.assignment", tgt, "distinct_per_parent is only valid for child entities")
    if rep.errors:
        return rep.finalize()
    try:
        keycols = {e.entity_id: key_columns(spec, e.entity_id) for e in spec.entities}
    except Exception as ex:  # noqa: BLE001
        err("entity.cycle", "entities", str(ex))
        return rep.finalize()

    vids = [v.variable_id for v in spec.variables]
    for dup in {x for x in vids if vids.count(x) > 1}:
        err("variable.duplicate", dup, f"duplicate variable_id {dup}")
    types = {v.variable_id: v.data_type for v in spec.variables}
    for v in spec.variables:
        tgt = f"variable:{v.variable_id}"
        e = by_id.get(v.entity)
        if not e:
            err("variable.entity", tgt, f"unknown entity {v.entity}")
            continue
        if v.column:
            reserved = set(own_key_columns(spec, v.entity)) | ({tcol} if is_temporal_entity(spec, v.entity) else set())
            if v.column in reserved:
                err("variable.reserved_column", tgt, f"column {v.column} clashes with a key/time column")
            others = [x.variable_id for x in spec.variables_of(v.entity) if x.column == v.column]
            if len(others) > 1:
                err("variable.column", tgt, f"column {v.column} used by several variables {others}")
        aliases = [p.name for p in v.parents]
        for dup in {x for x in aliases if aliases.count(x) > 1}:
            err("variable.alias", tgt, f"duplicate parent alias {dup}")
        ptypes: dict[str, str] = {}
        for p in v.parents:
            src = spec.variable(p.variable)
            if not src:
                err("variable.parent", tgt, f"unknown parent variable {p.variable}")
                continue
            if src.entity not in by_id:
                err("variable.parent", tgt, f"parent {p.variable} has an unknown entity {src.entity}")
                continue
            ptypes[p.name] = src.data_type
            rel = relation(spec, src.entity, v.entity)
            if rel is None:
                err("variable.grain", tgt, f"parent {p.variable} on grain {src.entity} cannot be reached from grain "
                                           f"{v.entity} (no FK path)", "add an assignment / FK path or aggregate")
            elif rel == "aggregate" and not p.aggregation:
                err("variable.aggregation", tgt, f"parent {p.variable} lives on a finer grain ({src.entity}); set 'aggregation'")
            elif rel == "broadcast" and p.aggregation:
                err("variable.aggregation", tgt, f"parent {p.variable} lives on a coarser grain; remove 'aggregation'")
            if p.aggregation and p.aggregation not in ("count",) and src.data_type in ("category", "text"):
                err("variable.aggregation", tgt, f"cannot aggregate non-numeric {p.variable} with {p.aggregation}")
            if p.variable == v.variable_id and p.lag == 0:
                err("variable.self", tgt, "a variable cannot be its own parent without a lag")
        try:
            mech = M.get(v.mechanism.type)
        except M.MechanismError as ex:
            err("mechanism.unsupported", tgt, str(ex), "use one of the supported mechanism types")
            continue
        for msg in mech.check(v, set(aliases), ptypes):
            err("mechanism.params", tgt, msg)
        if v.data_type == "category" and not v.categories and v.mechanism.type not in ("exogenous", "categorical_cpt",
                                                                                       "label", "lookup", "conditional"):
            err("variable.categories", tgt, "categorical variable needs 'categories'")
        if v.constraints.min is not None and v.constraints.max is not None and v.constraints.min > v.constraints.max:
            err("variable.constraints", tgt, "min > max")
        if v.data_type in ("category", "text") and v.parents and any(
                types.get(p.variable) in ("category", "text") and p.aggregation not in (None, "count") for p in v.parents):
            err("variable.types", tgt, "numeric aggregation of a categorical parent")
    for msg in check_lags(spec):
        err("graph.lag", "graph", msg)
    try:
        execution_plan(spec)
    except GraphError as ex:
        err("graph.cycle", "graph", str(ex), "break the loop with a lagged parent (X_t -> Y_t -> X_t+1)")
    for ev in spec.events:
        tgt = f"event:{ev.event_id}"
        try:
            ev.check_parameters()
        except ValueError as ex:
            err("event.params", tgt, str(ex))
            continue
        for vid in ev.affected_variables:
            v = spec.variable(vid)
            if not v:
                err("event.variable", tgt, f"unknown affected variable {vid}")
                continue
            if ev.affected_entities:
                allowed = keycols.get(v.entity, set()) | M.TIME_NAMES
                for msg in check_expr(ev.affected_entities, allowed):
                    err("event.filter", tgt, msg, "filter on key columns of the grain, e.g. '<fk_column> == 1'")
            if ev.intervention_type == "param_change":
                path = ev.intervention_parameters.get("param")
                cur: Any = v.mechanism.params
                ok = bool(path)
                for part in (path or "").split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        ok = False
                        break
                if not ok:
                    err("event.param", tgt, f"parameter path '{path}' does not exist in {vid}.mechanism.params")
            if ev.intervention_type in ("shift", "scale", "seasonal") and v.data_type not in ("float", "int"):
                err("event.type", tgt, f"{ev.intervention_type} requires a numeric variable ({vid} is {v.data_type})")
        if spec.time:
            s0 = spec.time.step_of(ev.start_time, 0)
            s1 = spec.time.step_of(ev.end_time, spec.time.periods)
            if s0 >= spec.time.periods or s1 <= s0:
                err("event.time", tgt, f"event window [{s0}, {s1}) is empty or outside the period", severity="warning")
    for f in spec.ground_truth.story_facts:
        tgt = f"fact:{f.fact_id}"
        for vid in [f.source, f.target, *f.evidence_variables]:
            if vid and not spec.variable(vid):
                err("fact.variable", tgt, f"unknown variable {vid}")
        if f.event_id and not spec.event(f.event_id):
            err("fact.event", tgt, f"unknown event {f.event_id}")
        for eid in f.entity_ids:
            if eid not in by_id:
                err("fact.entity", tgt, f"unknown entity {eid}")
        if f.kind == "edge" and not (f.source and f.target):
            err("fact.edge", tgt, "edge fact needs source and target")
        elif (f.kind in ("edge", "direction", "lag") and f.source in types and f.target in types
              and f.target not in descendants(spec, f.source)):
            err("fact.path", tgt, f"no directed causal path from {f.source} to {f.target}. "
                "Use the actual upstream cause as source and align the description with that relationship.")
    est = estimate_rows(spec)
    total = sum(r for r in est.values() if r)
    rep.add(check_id="size.rows", layer="spec", target="dataset", expected=f"<= {MAX_TOTAL_ROWS}",
            observed=str(total), passed=total <= MAX_TOTAL_ROWS,
            explanation="estimated rows of fixed grains (children with count variables not included)",
            suggested_fix="reduce entity counts or the number of time steps")
    if not rep.errors:
        rep.add(check_id="spec.ok", layer="spec", target="scm", passed=True, severity="info",
                explanation=f"{len(spec.entities)} entities, {len(spec.variables)} variables, {len(spec.events)} events")
    return rep.finalize()


# =========================================================================== helpers (independent alignment)
class _Frames:
    """Independent relational access: joins along declared FKs (not the engine's index maps)."""

    def __init__(self, world: CleanWorld):
        self.w = world
        self.spec = world.spec
        self._keys: dict[str, pd.DataFrame] = {}

    def own(self, eid: str) -> pd.DataFrame:
        df = self.w.internals[eid]
        cols = own_key_columns(self.spec, eid) + ([TIME_KEY] if TIME_KEY in df.columns else [])
        return df[cols]

    def keys(self, eid: str) -> pd.DataFrame:
        """Own keys + all ancestor keys obtained by merging referenced entities on FK columns."""
        if eid in self._keys:
            return self._keys[eid]
        e = self.spec.entity(eid)
        df = self.own(eid).copy()
        refs: list[tuple[str, str]] = []
        if e.kind == "child" and e.parent:
            refs.append((e.parent, self.spec.entity(e.parent).primary_key))
        if e.kind == "cross":
            refs += [(m, self.spec.entity(m).primary_key) for m in e.cross]
        refs += [(a.entity, a.column or self.spec.entity(a.entity).primary_key) for a in e.assignments]
        for ref, col in refs:
            rk = self.keys(ref).drop(columns=[TIME_KEY], errors="ignore")
            rpk = self.spec.entity(ref).primary_key
            rk = rk.rename(columns={rpk: col}) if col != rpk else rk
            extra = [c for c in rk.columns if c not in df.columns or c == col]
            df = df.merge(rk[extra], on=col, how="left", validate="many_to_one")
        if TIME_KEY not in df.columns and is_temporal_entity(self.spec, eid) and e.kind == "child":
            df[TIME_KEY] = self.keys(e.parent)[TIME_KEY].to_numpy()[
                df[self.spec.entity(e.parent).primary_key].to_numpy() - 1] if len(df) else []
        self._keys[eid] = df
        return df

    def parent(self, v: VariableSpec, p) -> np.ndarray:
        spec = self.spec
        src = spec.variable(p.variable)
        S, G = src.entity, v.entity
        rel = relation(spec, S, G)
        target = self.keys(G)
        sval = self.w.internals[S][src.variable_id]
        init = src.initial_state if src.initial_state is not None else p.fill_value
        if rel == "same" and not p.aggregation:
            if p.lag == 0:
                return sval.to_numpy()
            unit = [c for c in identity_columns(spec, G)] if spec.entity(G).kind == "cross" else None
            if unit is None:
                raise _NotVerifiable("lag on a non-cross grain")
            right = self.keys(S)[unit + [TIME_KEY]].assign(__v=sval.to_numpy())
            left = target[unit + [TIME_KEY]].assign(**{TIME_KEY: target[TIME_KEY] - p.lag})
            out = left.merge(right, on=unit + [TIME_KEY], how="left")["__v"]
            return out.where(out.notna(), init).to_numpy()
        if rel == "broadcast" and not p.aggregation:
            ids = sorted(identity_columns(spec, S))
            on = ids + ([TIME_KEY] if is_temporal_entity(spec, S) else [])
            right = self.keys(S)[on].assign(__v=sval.to_numpy())
            left = target[on].copy()
            if p.lag and TIME_KEY in on:
                left[TIME_KEY] = left[TIME_KEY] - p.lag
            out = left.merge(right, on=on, how="left", validate="many_to_one")["__v"]
            if p.lag:
                out = out.where(out.notna(), init)
            return out.to_numpy()
        if p.aggregation:
            ids = sorted(identity_columns(spec, G))
            temporal = is_temporal_entity(spec, S) and is_temporal_entity(spec, G)
            on = ids + ([TIME_KEY] if temporal else [])
            src_keys = self.keys(S) if rel != "same" else target
            right = src_keys[on].assign(__v=pd.to_numeric(sval, errors="coerce").to_numpy())
            fn = {"sum": "sum", "mean": "mean", "count": "count", "min": "min", "max": "max", "any": "max"}[p.aggregation]
            if p.aggregation == "count":
                right["__v"] = 1.0
                fn = "sum"
            if p.aggregation == "any":
                right["__v"] = (right["__v"] != 0).astype(float)
            agg = right.groupby(on, as_index=False)["__v"].agg(fn)
            left = target[on].copy()
            if p.lag and temporal:
                left[TIME_KEY] = left[TIME_KEY] - p.lag
            out = left.merge(agg, on=on, how="left")["__v"]
            return out.fillna(p.fill_value).to_numpy()
        raise _NotVerifiable(f"relation {rel}")


class _NotVerifiable(Exception):
    pass


def _time_env(world: CleanWorld, eid: str, frames: _Frames) -> dict[str, np.ndarray]:
    k = frames.keys(eid)
    n = len(k)
    if TIME_KEY not in k.columns or not world.dates:
        return {"t": np.zeros(n), "dow": np.zeros(n), "month": np.ones(n), "doy": np.ones(n), "step_frac": np.zeros(n)}
    T = len(world.dates)
    t = k[TIME_KEY].to_numpy()
    ti = np.clip(t, 0, T - 1)  # invalid time indices are reported by the relational layer
    d = pd.to_datetime(pd.Series(world.dates))
    return {"t": t.astype(float), "dow": d.dt.day_of_week.to_numpy()[ti].astype(float),
            "month": d.dt.month.to_numpy()[ti].astype(float), "doy": d.dt.day_of_year.to_numpy()[ti].astype(float),
            "step_frac": t / max(1, T - 1)}


def _terms(coefs: dict[str, float], intercept: float, env: dict[str, np.ndarray], n: int) -> np.ndarray:
    """Independent re-implementation of the term syntax."""
    out = np.full(n, float(intercept))
    for term, c in coefs.items():
        prod = np.ones(n)
        for part in term.split("*"):
            part = part.strip()
            if "=" in part:
                nm, cat = part.split("=", 1)
                prod *= (pd.Series(env[nm.strip()]).astype(str).to_numpy() == cat.strip())
            elif "^" in part:
                nm, ex = part.split("^", 1)
                prod *= np.asarray(env[nm.strip()], dtype=float) ** float(ex)
            else:
                prod *= np.asarray(env[part], dtype=float)
        out += float(c) * prod
    return out


def _lp(params: dict[str, Any], env: dict[str, np.ndarray], n: int) -> np.ndarray:
    if params.get("predictor"):
        return np.asarray(evaluate(params["predictor"], env, n), dtype=float)
    return _terms(params.get("coefficients", {}), params.get("intercept", 0.0), env, n)


def _ppf(dist: dict[str, Any], u: np.ndarray, scale: float = 1.0) -> np.ndarray:
    fam, par = dist["family"], dist.get("params", {})
    if fam == "normal":
        return float(par.get("mean", 0)) + float(par["sd"]) * scale * stats.norm.ppf(u)
    if fam == "student_t":
        return float(par.get("loc", 0)) + float(par.get("scale", 1)) * scale * stats.t.ppf(u, float(par["df"]))
    if fam == "constant":
        return np.full(len(u), float(par["value"]))
    if fam == "categorical":
        p = np.asarray(dist["probs"], dtype=float)
        p = p / p.sum()
        idx = np.minimum(np.searchsorted(np.cumsum(p), u, side="right"), len(p) - 1)
        return np.asarray(dist["categories"], dtype=object)[idx]
    d = D.scipy_dist(fam, par)
    x = d.ppf(u)
    if scale != 1.0 and fam not in D.DISCRETE:
        x = d.mean() + (x - d.mean()) * scale
    return x


def _finish_expected(v: VariableSpec, x: np.ndarray) -> np.ndarray:
    c = v.constraints
    if v.data_type in ("float", "int"):
        x = np.asarray(x, dtype=float)
        if c.clip:
            x = np.clip(x, c.min if c.min is not None else -np.inf, c.max if c.max is not None else np.inf)
        if v.data_type == "int":
            return np.rint(x)
        if c.round is not None:
            x = np.round(x, c.round)
        return x
    if v.data_type == "bool":
        return np.asarray(x).astype(bool)
    return np.asarray(x).astype(str)


def _expected(v: VariableSpec, env: dict[str, np.ndarray], aux: pd.DataFrame, n: int, scale: float,
              keys: pd.DataFrame) -> Optional[np.ndarray]:
    """Independent recomputation of the mechanism; None = not verifiable mechanistically."""
    mt, par = v.mechanism.type, v.mechanism.params
    a = lambda name: aux[f"__aux__{v.variable_id}::{name}"].to_numpy() if f"__aux__{v.variable_id}::{name}" in aux else None
    u0 = a("u0")
    if mt == "exogenous":
        return _ppf(par["distribution"], u0, scale) if u0 is not None else None
    if mt in ("linear", "polynomial", "interaction", "lagged_effect", "deterministic_expression", "conditional",
              "autoregressive"):
        if mt == "deterministic_expression":
            det = np.asarray(evaluate(par["expression"], env, n))
            if v.data_type == "bool":
                return det.astype(bool)
            det = det.astype(float)
        elif mt == "conditional":
            det = np.asarray(evaluate(par["default"], env, n)).astype(object)
            done = np.zeros(n, bool)
            for case in par["cases"]:
                c = np.asarray(evaluate(case["when"], env, n)).astype(bool) & ~done
                det = np.where(c, np.asarray(evaluate(case["value"], env, n)), det)
                done |= c
            if v.data_type == "category":
                return det.astype(str)
            det = det.astype(float)
            if v.data_type == "bool":
                return det.astype(bool)
        elif mt == "autoregressive":
            self_alias = next(p.name for p in v.parents if p.variable == v.variable_id and p.lag >= 1)
            det = float(par.get("mean", 0)) + float(par.get("phi", 0.5)) * (np.asarray(env[self_alias], float) - float(par.get("mean", 0)))
            det = det + _terms(par.get("coefficients", {}), 0.0, env, n)
        else:
            det = _lp(par, env, n)
        noise = par.get("noise")
        eps = _ppf(noise, u0, scale) if noise and u0 is not None else (np.zeros(n) if not noise else None)
        return None if eps is None else det + eps
    if mt == "logistic":
        p = 1 / (1 + np.exp(-np.clip(_lp(par, env, n), -500, 500)))
        return (u0 < p) if u0 is not None else None
    if mt == "threshold":
        score = _lp(par, env, n)
        if par.get("noise"):
            if u0 is None:
                return None
            score = score + _ppf(par["noise"], u0, scale)
        return score > float(par.get("threshold", 0.0))
    if mt == "poisson_regression":
        eta = _lp(par, env, n)
        lam = np.exp(eta) if par.get("link", "log") == "log" else np.maximum(eta, 0)
        lam = np.minimum(lam, float(par.get("max_rate", 1e6)))
        if u0 is None:
            return None
        if par.get("dispersion"):
            k = float(par["dispersion"])
            return stats.nbinom.ppf(u0, k, k / (k + np.maximum(lam, 1e-9)))
        return np.nan_to_num(stats.poisson.ppf(u0, lam), nan=0.0)
    if mt in ("categorical_cpt", "categorical_softmax"):
        mech = M.get(mt)
        p = mech.parse(par)
        probs = mech.probs(p, v, env, n)
        cats = mech.categories(v, p) if mt == "categorical_cpt" else v.categories
        idx = np.minimum((u0[:, None] >= np.cumsum(probs, axis=1)).sum(axis=1), len(cats) - 1)
        vals = np.asarray(cats, dtype=object)[idx]
        if v.data_type == "bool":
            return np.isin(vals.astype(str), ["1", "true", "True", "yes"])
        return vals.astype(str)
    if mt == "ordinal_threshold":
        latent = _lp(par, env, n) + _ppf(par.get("noise") or {"family": "normal", "params": {"sd": 1.0}}, u0, scale)
        lvl = np.searchsorted(np.asarray(par["cutpoints"], float), latent, side="right")
        return np.asarray(v.categories, dtype=object)[lvl].astype(str) if v.data_type == "category" else lvl + 1
    if mt == "lookup":
        src = env[par.get("source") or v.parents[0].name]
        if par.get("mapping"):
            m = par["mapping"]
            keys_s = [f"{x:g}" if isinstance(x, float) else str(x) for x in src]
            return np.asarray([m.get(k, par.get("default")) for k in keys_s], dtype=object)
        return np.asarray(src)
    if mt == "aggregation":
        src = env[par.get("source") or v.parents[0].name]
        return np.asarray(src, dtype=float) * float(par.get("factor", 1.0)) + float(par.get("offset", 0.0))
    return None


# =========================================================================== 2.–4. world validation
def validate_world(world: CleanWorld) -> ValidationReport:
    rep = ValidationReport()
    spec = world.spec
    frames = _Frames(world)
    _relational(world, frames, rep)
    stat_tests: list[dict[str, Any]] = []
    for v in spec.variables:
        try:
            _mechanistic(world, frames, v, rep, stat_tests)
        except _NotVerifiable as e:
            rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=v.variable_id, passed=True,
                    severity="info", explanation=f"not verifiable mechanistically ({e})")
        except Exception as e:  # noqa: BLE001 - a broken world must yield a FAIL report, not a crash
            rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=v.variable_id, passed=False,
                    severity="error", explanation=f"recomputation failed: {type(e).__name__}: {str(e)[:200]}")
    _holm(stat_tests, rep)
    return rep.finalize()


def _relational(world: CleanWorld, frames: _Frames, rep: ValidationReport) -> None:
    spec = world.spec
    T = len(world.dates) or 1
    est = estimate_rows(spec)
    for e in spec.entities:
        df = world.internals[e.entity_id]
        tgt = e.name
        pk = df[e.primary_key]
        dup = int(pk.duplicated().sum()) + int(pk.isna().sum())
        rep.add(check_id=f"pk:{e.name}", layer="relational", target=tgt, expected="unique, not null",
                observed=f"{dup} violations", passed=dup == 0, explanation=f"primary key {e.primary_key}")
        refs = []
        if e.kind == "child" and e.parent:
            refs.append((spec.entity(e.parent), spec.entity(e.parent).primary_key))
        if e.kind == "cross":
            refs += [(spec.entity(m), spec.entity(m).primary_key) for m in e.cross]
        refs += [(spec.entity(a.entity), a.column or spec.entity(a.entity).primary_key) for a in e.assignments]
        for ref, col in refs:
            ok = df[col].isin(world.internals[ref.entity_id][ref.primary_key])
            bad = int((~ok).sum())
            rep.add(check_id=f"fk:{e.name}.{col}", layer="relational", target=tgt, expected=f"{col} ⊆ {ref.name}.{ref.primary_key}",
                    observed=f"{bad} orphans", passed=bad == 0, explanation="referential integrity")
        expected = est.get(e.entity_id)
        if expected is not None:
            rep.add(check_id=f"rows:{e.name}", layer="relational", target=tgt, expected=str(expected),
                    observed=str(len(df)), passed=len(df) == expected, explanation="row count of a fixed grain")
        if e.kind == "child" and e.count_variable:
            parent = spec.entity(e.parent)
            pdf = world.internals[parent.entity_id]
            cnt = df.groupby(parent.primary_key).size()
            want = np.maximum(0, np.rint(pd.to_numeric(pdf[e.count_variable], errors="coerce").fillna(0).astype(float)))
            got = cnt.reindex(pdf[parent.primary_key]).fillna(0).to_numpy()
            bad = int((got != want.to_numpy()).sum())
            rep.add(check_id=f"cardinality:{e.name}", layer="relational", target=tgt,
                    expected=f"rows per {parent.name} = {e.count_variable}", observed=f"{bad} parents mismatch",
                    passed=bad == 0, explanation="number of child rows equals the realised count variable")
        if is_temporal_entity(spec, e.entity_id) and TIME_KEY in df:
            bad = int(((df[TIME_KEY] < 0) | (df[TIME_KEY] >= T)).sum())
            rep.add(check_id=f"time:{e.name}", layer="relational", target=tgt, expected=f"0 <= t < {T}",
                    observed=f"{bad} rows outside", passed=bad == 0, explanation="time index within the period")
            if e.kind == "child":
                pk_p = spec.entity(e.parent).primary_key
                parent_t = frames.keys(e.parent).set_index(pk_p)[TIME_KEY]
                mism = int((df[TIME_KEY].to_numpy() != parent_t.reindex(df[pk_p]).to_numpy()).sum())
                rep.add(check_id=f"time_parent:{e.name}", layer="relational", target=tgt,
                        expected="child time = parent time", observed=f"{mism} mismatches", passed=mism == 0,
                        explanation="no rows refer to a different time than their parent")
        for v in spec.variables_of(e.entity_id):
            col = df[v.variable_id]
            c = v.constraints
            if v.data_type in ("float", "int"):
                x = pd.to_numeric(col, errors="coerce")
                nonfinite = int((~np.isfinite(x.astype(float))).sum())
                if nonfinite:
                    rep.add(check_id=f"finite:{v.variable_id}", layer="relational", target=v.variable_id,
                            passed=False, observed=f"{nonfinite} non-finite", explanation="values must be finite")
                if c.min is not None or c.max is not None:
                    bad = int(((x < c.min) if c.min is not None else False).sum() if c.min is not None else 0) + \
                        int(((x > c.max).sum()) if c.max is not None else 0)
                    rep.add(check_id=f"range:{v.variable_id}", layer="relational", target=v.variable_id,
                            expected=f"[{c.min}, {c.max}]", observed=f"{bad} out of range", passed=bad == 0,
                            explanation="domain constraint")
            if v.data_type == "category" and v.categories:
                bad = int((~col.astype(str).isin(v.categories)).sum())
                rep.add(check_id=f"domain:{v.variable_id}", layer="relational", target=v.variable_id,
                        expected=f"⊆ {v.categories}", observed=f"{bad} unknown", passed=bad == 0,
                        explanation="categorical domain")
            if e.kind == "child" and e.count_variable == v.variable_id:
                pass
        for v in spec.variables:
            ce = next((x for x in spec.entities if x.kind == "child" and x.count_variable == v.variable_id), None)
            if ce and v.entity == e.entity_id:
                neg = int((pd.to_numeric(df[v.variable_id], errors="coerce") < 0).sum())
                if neg:
                    rep.add(check_id=f"count_nonneg:{v.variable_id}", layer="relational", target=v.variable_id,
                            passed=False, severity="warning", observed=f"{neg} negative counts (treated as 0)",
                            explanation="count variables should be non-negative", suggested_fix="add constraints min=0, clip")


def _mechanistic(world: CleanWorld, frames: _Frames, v: VariableSpec, rep: ValidationReport,
                 stat_tests: list[dict[str, Any]]) -> None:
    spec = world.spec
    df = world.internals[v.entity]
    n = len(df)
    if n == 0:
        rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=v.variable_id, passed=True,
                severity="info", explanation="no rows")
        return
    if v.mechanism.type in ("label", "text_template", "group_effect"):
        if v.mechanism.type == "group_effect":
            par = v.mechanism.params
            keys = frames.keys(v.entity)
            noise = df.get(f"__aux__{v.variable_id}::noise")
            if noise is not None and par.get("group_by") in keys:
                spread = pd.Series(noise.to_numpy()).groupby(keys[par["group_by"]].to_numpy()).nunique().max()
                rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=v.variable_id,
                        expected="one effect per group", observed=f"max {spread} distinct", passed=spread <= 1)
                return
        rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=v.variable_id, passed=True,
                severity="info", explanation=f"{v.mechanism.type}: text/label values are not recomputed")
        return
    env = _time_env(world, v.entity, frames)
    for p in v.parents:
        env[p.name] = frames.parent(v, p)
    scale = float(world.noise_scale.get(v.variable_id, 1.0))
    keys = frames.keys(v.entity)
    exp = _expected(v, env, df, n, scale, keys)
    if exp is None:
        raise _NotVerifiable(f"mechanism {v.mechanism.type} without stored randomness")
    exp = _finish_expected(v, exp)
    evt = np.zeros(n, bool)
    for k in ("__evt", "__pevt"):
        col = f"__aux__{v.variable_id}::{k}"
        if col in df:
            evt |= df[col].fillna(False).astype(bool).to_numpy()
    actual = df[v.variable_id].to_numpy()
    mask = ~evt
    if v.data_type in ("float", "int"):
        a = actual.astype(float)[mask]
        e = np.asarray(exp, float)[mask]
        tol = 1e-6 * np.maximum(1, np.abs(e)) + (0.5 * 10 ** -(v.constraints.round or 0) + 1e-9
                                                  if v.constraints.round is not None else 0)
        bad = ~(np.isclose(a, e, rtol=0, atol=0) | (np.abs(a - e) <= tol))
    else:
        a = np.asarray(actual)[mask].astype(str)
        e = np.asarray(exp)[mask].astype(str)
        bad = a != e
        if v.mechanism.type in ("logistic", "categorical_cpt", "categorical_softmax"):
            pass
    nbad = int(bad.sum())
    rep.add(check_id=f"mech:{v.variable_id}", layer="mechanistic", target=f"{v.variable_id}:{v.mechanism.type}",
            expected="realisation = f(parents, stored noise)", observed=f"{nbad} of {int(mask.sum())} rows differ"
            + (f"; {int(evt.sum())} rows under events not recomputed" if evt.any() else ""),
            passed=nbad == 0, severity="error",
            explanation=M.describe(v),
            suggested_fix="" if nbad == 0 else "the generator does not implement the declared mechanism")
    _statistical(world, v, env, df, mask, scale, stat_tests)


# =========================================================================== statistical layer
def _statistical(world: CleanWorld, v: VariableSpec, env: dict[str, np.ndarray], df: pd.DataFrame,
                 mask: np.ndarray, scale: float, tests: list[dict[str, Any]]) -> None:
    mt, par = v.mechanism.type, v.mechanism.params
    y = df[v.variable_id].to_numpy()[mask]
    n = int(mask.sum())
    if n < 30:
        return
    col = lambda name: df[f"__aux__{v.variable_id}::{name}"].to_numpy()[mask] \
        if f"__aux__{v.variable_id}::{name}" in df else None
    if mt == "exogenous":
        dist = par["distribution"]
        fam = dist["family"]
        if fam == "constant":
            return
        if fam == "categorical":
            p = np.asarray(dist["probs"], float)
            p = p / p.sum()
            obs = np.array([(y.astype(str) == c).sum() for c in dist["categories"]])
            _add_test(tests, v, "category_frequencies", stats.chisquare(obs, p * n).pvalue,
                      f"expected {dict(zip(dist['categories'], np.round(p * n, 1)))}", f"observed {dict(zip(dist['categories'], obs))}")
        elif fam in D.DISCRETE:
            d = D.scipy_dist(fam, dist.get("params", {}))
            z = (y.astype(float).mean() - d.mean()) / (d.std() / np.sqrt(n) + 1e-12)
            _add_test(tests, v, "mean", 2 * stats.norm.sf(abs(z)), f"mean {d.mean():.3f}", f"mean {y.astype(float).mean():.3f}")
        elif v.constraints.round is None or v.constraints.round >= 2:
            d = D.scipy_dist(fam, dist.get("params", {}))
            if scale == 1.0:
                _add_test(tests, v, "ks_distribution", stats.kstest(y.astype(float), d.cdf).pvalue,
                          D.Distribution.model_validate(dist).describe(), f"mean {y.astype(float).mean():.3f}")
    if mt in ("linear", "polynomial", "interaction", "lagged_effect") and par.get("noise"):
        resid = col("noise")
        nd = par["noise"]
        if resid is not None and nd["family"] == "normal":
            sd = float(nd["params"]["sd"]) * scale
            _add_test(tests, v, "residual_ks", stats.kstest(resid, stats.norm(nd["params"].get("mean", 0), sd).cdf).pvalue,
                      f"N({nd['params'].get('mean', 0)}, {sd:.3g})", f"sd {np.std(resid):.3g}")
            det = col("deterministic")
            if det is not None and np.var(resid) > 0:
                snr = float(np.var(det) / np.var(resid))
                tests.append({"info": True, "var": v.variable_id, "name": "signal_to_noise",
                              "observed": f"SNR = {snr:.3g}"})
        if par.get("coefficients") and not par.get("predictor") and v.constraints.round is None and not v.constraints.clip:
            terms = list(par["coefficients"])
            try:
                X = np.column_stack([np.ones(n)] + [_terms({t: 1.0}, 0.0, {k: np.asarray(val)[mask] if np.ndim(val) and len(val) == len(mask) else val for k, val in env.items()}, n) for t in terms])
                beta, *_ = np.linalg.lstsq(X, y.astype(float), rcond=None)
                resid2 = y.astype(float) - X @ beta
                s2 = resid2 @ resid2 / max(1, n - X.shape[1])
                cov = s2 * np.linalg.pinv(X.T @ X)
                for i, t in enumerate(terms, start=1):
                    se = float(np.sqrt(max(cov[i, i], 0)))
                    if se == 0:
                        continue
                    z = (beta[i] - par["coefficients"][t]) / se
                    _add_test(tests, v, f"coef[{t}]", 2 * stats.norm.sf(abs(z)), f"{par['coefficients'][t]:g}",
                              f"{beta[i]:.4g} ± {se:.2g}")
            except (np.linalg.LinAlgError, KeyError, ValueError):
                pass
    if mt == "logistic":
        p = None
        try:
            p = 1 / (1 + np.exp(-np.clip(_lp(par, {k: (np.asarray(x)[mask] if np.ndim(x) and len(x) == len(mask) else x)
                                                   for k, x in env.items()}, n), -500, 500)))
        except (ExpressionError, KeyError):
            pass
        if p is not None:
            var = float(np.sum(p * (1 - p)))
            if var > 0:
                z = (y.astype(float).sum() - p.sum()) / np.sqrt(var)
                _add_test(tests, v, "calibration", 2 * stats.norm.sf(abs(z)), f"E[sum] = {p.sum():.1f}",
                          f"sum = {y.astype(float).sum():.0f}")
    if mt == "poisson_regression":
        lam = col("lambda")
        if lam is not None and lam.sum() > 0:
            k = par.get("dispersion")
            var = float(np.sum(lam + (lam ** 2 / k if k else 0)))
            z = (y.astype(float).sum() - lam.sum()) / np.sqrt(var)
            _add_test(tests, v, "count_calibration", 2 * stats.norm.sf(abs(z)), f"E[sum] = {lam.sum():.1f}",
                      f"sum = {y.astype(float).sum():.0f}")
    if mt in ("categorical_cpt", "categorical_softmax"):
        cats = v.categories or list(par.get("default", {}))
        exp = np.array([np.nansum(col(f"p:{c}")) if col(f"p:{c}") is not None else 0 for c in cats])
        if exp.sum() > 0:
            obs = np.array([(y.astype(str) == c).sum() for c in cats])
            keep = exp > 0
            exp_k = exp[keep] * obs[keep].sum() / exp[keep].sum()
            _add_test(tests, v, "category_frequencies", stats.chisquare(obs[keep], exp_k).pvalue,
                      f"expected {dict(zip(np.asarray(cats)[keep], np.round(exp_k, 1)))}",
                      f"observed {dict(zip(np.asarray(cats)[keep], obs[keep]))}")


def _add_test(tests: list[dict[str, Any]], v: VariableSpec, name: str, p: float, expected: str, observed: str) -> None:
    tests.append({"var": v.variable_id, "name": name, "p": float(p) if np.isfinite(p) else 1.0,
                  "expected": expected, "observed": observed})


def _holm(tests: list[dict[str, Any]], rep: ValidationReport) -> None:
    real = [t for t in tests if not t.get("info")]
    m = len(real)
    order = sorted(range(m), key=lambda i: real[i]["p"])
    reject = [False] * m
    for rank, i in enumerate(order):
        if real[i]["p"] <= STAT_FAMILY_ALPHA / (m - rank):
            reject[i] = True
        else:
            break
    for t, rj in zip(real, reject):
        rep.add(check_id=f"stat:{t['var']}:{t['name']}", layer="statistical", target=t["var"], expected=t["expected"],
                observed=f"{t['observed']} (p = {t['p']:.3g})", passed=not rj, severity="warning",
                explanation=f"Holm-corrected family alpha {STAT_FAMILY_ALPHA}; rows of panels are not independent, "
                            "so a rejection is a warning, never a failure",
                suggested_fix="" if not rj else "inspect the mechanism / sample size; do not edit values to pass")
    for t in tests:
        if t.get("info"):
            rep.add(check_id=f"stat:{t['var']}:{t['name']}", layer="statistical", target=t["var"],
                    observed=t["observed"], passed=True, severity="info", explanation="realised signal strength")
