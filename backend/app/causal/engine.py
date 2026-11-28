"""Clean-world generation: executes an approved ``SCMSpec`` over relational grains and time steps.

Generation order (spec §7.1): validate → resolve entity counts/identity → create fixed grains and
relations → execution plan (static pre-phase, per-step loop, static post-phase) → per node: exogenous
draws / mechanisms from the real parents → events & interventions → text/labels → materialise
tables → keep everything needed for validation and the ground-truth manifest.

Row layout: every grain stores key columns (own primary key, FK columns and all *ancestor* keys
needed for lookups, plus ``__t`` on temporal grains) and one array per variable. Temporal grains
with a fixed layout are stored t-major (row = t · n_units + unit). Child grains whose row count is
given by a variable grow chunk by chunk, so the number of child rows is *exactly* the realised count.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import mechanisms as M
from .contracts import EntitySpec, EventSpec, ParentRef, SCMSpec, VariableSpec
from .expressions import ExpressionError, evaluate
from .graph import ENTITY_NODE, ExecutionPlan, execution_plan, is_dynamic_entity, is_temporal_entity
from .rng import stream, uniforms

TIME_KEY = "__t"


class GenerationError(RuntimeError):
    pass


# =========================================================================== static key structure
def own_key_columns(spec: SCMSpec, eid: str) -> list[str]:
    """Exported key columns: primary key + FK columns of the entity."""
    e = spec.entity(eid)
    cols = [e.primary_key]
    if e.kind == "child" and e.parent:
        cols.append(spec.entity(e.parent).primary_key)
    if e.kind == "cross":
        cols += [spec.entity(m).primary_key for m in e.cross]
    cols += [a.column or spec.entity(a.entity).primary_key for a in e.assignments]
    return list(dict.fromkeys(cols))


def key_columns(spec: SCMSpec, eid: str, _seen: Optional[set] = None) -> set[str]:
    """All key columns available on rows of the grain (incl. ancestor keys, without ``__t``)."""
    _seen = _seen or set()
    if eid in _seen:
        raise GenerationError(f"entity reference cycle at {eid}")
    _seen = _seen | {eid}
    e = spec.entity(eid)
    if e is None:
        return set()
    cols = set(own_key_columns(spec, eid))
    refs = ([e.parent] if e.kind == "child" and e.parent else []) + (list(e.cross) if e.kind == "cross" else []) \
        + [a.entity for a in e.assignments]
    for r in refs:
        if spec.entity(r):
            cols |= key_columns(spec, r, _seen)
    return cols


def identity_columns(spec: SCMSpec, eid: str) -> set[str]:
    e = spec.entity(eid)
    if e.kind == "cross":
        return {spec.entity(m).primary_key for m in e.cross}
    return {e.primary_key}


def relation(spec: SCMSpec, src: str, tgt: str) -> Optional[str]:
    """How a variable on grain ``src`` can be read from grain ``tgt``: same | broadcast | aggregate."""
    if src == tgt:
        return "same"
    s_t, g_t = is_temporal_entity(spec, src), is_temporal_entity(spec, tgt)
    if identity_columns(spec, src) <= key_columns(spec, tgt) and (not s_t or g_t):
        return "broadcast"
    if identity_columns(spec, tgt) <= key_columns(spec, src) and (not g_t or s_t):
        return "aggregate"
    return None


# =========================================================================== storage
class GrowArray:
    def __init__(self, dtype: Any, capacity: int = 64):
        self.dtype = np.dtype(dtype)
        self.a = self._empty(max(capacity, 16))
        self.n = 0

    def _empty(self, k: int) -> np.ndarray:
        if self.dtype == object:
            return np.full(k, None, dtype=object)
        if self.dtype.kind == "f":
            return np.full(k, np.nan)
        return np.zeros(k, dtype=self.dtype)

    def extend(self, chunk: np.ndarray | int) -> None:
        k = chunk if isinstance(chunk, int) else len(chunk)
        if self.n + k > len(self.a):
            new = self._empty(max(2 * len(self.a), self.n + k))
            new[: self.n] = self.a[: self.n]
            self.a = new
        if not isinstance(chunk, int):
            self.a[self.n:self.n + k] = chunk
        self.n += k

    def view(self) -> np.ndarray:
        return self.a[: self.n]


def _dtype(v: VariableSpec) -> Any:
    return {"float": float, "int": np.int64, "bool": bool}.get(v.data_type, object)


@dataclass
class Grain:
    entity: EntitySpec
    temporal: bool
    dynamic: bool
    n_units: int = 0                      # rows per step (fixed temporal) / rows (static)
    keys: dict[str, GrowArray] = field(default_factory=dict)
    values: dict[str, GrowArray] = field(default_factory=dict)
    aux: dict[str, GrowArray] = field(default_factory=dict)
    steps: dict[int, tuple[int, int]] = field(default_factory=dict)  # dynamic temporal: t -> (start, stop)
    strides: dict[str, int] = field(default_factory=dict)            # cross: member pk -> stride
    created: bool = False

    @property
    def n(self) -> int:
        k = self.entity.primary_key
        return self.keys[k].n if k in self.keys else 0

    def key(self, col: str) -> np.ndarray:
        return self.keys[col].view()

    def slice(self, t: Optional[int]) -> tuple[int, int]:
        if not self.temporal or t is None:
            return 0, self.n
        if self.dynamic:
            return self.steps.get(t, (self.n, self.n))
        return t * self.n_units, (t + 1) * self.n_units

    def add_rows(self, keys: dict[str, np.ndarray]) -> tuple[int, int]:
        start = self.n
        k = len(next(iter(keys.values())))
        for c, arr in keys.items():
            if c not in self.keys:
                self.keys[c] = GrowArray(np.asarray(arr).dtype if np.asarray(arr).dtype != object else object)
                self.keys[c].extend(start)
            self.keys[c].extend(np.asarray(arr))
        for c, store in self.keys.items():
            if c not in keys:
                store.extend(k)
        for store in (*self.values.values(), *self.aux.values()):
            store.extend(k)
        return start, start + k

    def ensure_aux(self, name: str, dtype: Any) -> GrowArray:
        if name not in self.aux:
            g = GrowArray(dtype, self.n + 16)
            g.extend(self.n)
            self.aux[name] = g
        return self.aux[name]


# =========================================================================== results
@dataclass
class CleanWorld:
    spec: SCMSpec
    seed: int
    tables: dict[str, pd.DataFrame]       # exported tables: keys + all mapped variable columns (incl. latent)
    internals: dict[str, pd.DataFrame]    # per entity: keys, ancestor keys, __t, every variable, noise, event masks
    dates: list[str]
    realized_events: list[dict[str, Any]]
    noise_scale: dict[str, float]
    world_hash: str
    spec_hash: str
    timings: dict[str, float]

    def table_of_entity(self, eid: str) -> Optional[pd.DataFrame]:
        e = self.spec.entity(eid)
        return self.tables.get(e.name) if e else None

    def variable_values(self, vid: str) -> np.ndarray:
        v = self.spec.variable(vid)
        return self.internals[v.entity][vid].to_numpy()


def frame_hash(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update("|".join(map(str, df.columns)).encode())
    h.update(str(len(df)).encode())
    for c in df.columns:
        h.update(pd.util.hash_pandas_object(df[c], index=False).to_numpy().tobytes())
    return h.hexdigest()


def tables_hash(tables: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for name in sorted(tables):
        h.update(name.encode())
        h.update(frame_hash(tables[name]).encode())
    return h.hexdigest()[:24]


# =========================================================================== ramp profiles
def ramp_factor(ev: EventSpec, steps_since_start: int) -> float:
    if ev.ramp_profile == "step" or ev.ramp_periods <= 0:
        return 1.0
    x = min(1.0, max(0.0, (steps_since_start + 1) / ev.ramp_periods))
    if ev.ramp_profile == "linear":
        return x
    if ev.ramp_profile == "exponential":
        k = 3.0
        return (math.exp(k * x) - 1) / (math.exp(k) - 1)
    lo, hi = 1 / (1 + math.exp(5)), 1 / (1 + math.exp(-5))
    return (1 / (1 + math.exp(-10 * (x - 0.5))) - lo) / (hi - lo)


def _get_path(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            raise GenerationError(f"parameter path '{path}' not found")
    return cur


def _set_path(d: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    import copy
    out = copy.deepcopy(d)
    cur = out
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out


# =========================================================================== engine
class Engine:
    def __init__(self, spec: SCMSpec, seed: Optional[int] = None, noise_scale: Optional[dict[str, float]] = None,
                 extra_events: Optional[list[EventSpec]] = None):
        self.spec = spec
        self.seed = int(spec.meta.seed if seed is None else seed)
        self.noise_scale = dict(noise_scale or {})
        self.events = list(spec.events) + list(extra_events or [])
        for event in self.events:
            event.check_parameters()
        self.T = spec.time.periods if spec.time else 1
        self.dates = spec.time.dates() if spec.time else []
        self.grains: dict[str, Grain] = {}
        self._u_cache: dict[tuple, np.ndarray] = {}
        self._parsed: dict[str, Any] = {}
        self.realized: dict[str, dict[str, Any]] = {}
        self._time_env_cache: dict[int, dict[str, Any]] = {}
        self._ev_steps = {ev.event_id: (self._step(ev.start_time, 0), self._step(ev.end_time, self.T))
                          for ev in self.events}

    def _step(self, v, default):
        if self.spec.time is None:
            return default if v is None else (0 if default == 0 else self.T)
        return self.spec.time.step_of(v, default)

    # -------------------------------------------------------------------- grains
    def _entity_order(self) -> list[EntitySpec]:
        done: list[str] = []
        pending = {e.entity_id: e for e in self.spec.entities}
        while pending:
            ready = []
            for eid, e in pending.items():
                deps = ([e.parent] if e.kind == "child" and e.parent else []) + list(e.cross) + \
                       [a.entity for a in e.assignments]
                if all(d in done for d in deps):
                    ready.append(eid)
            if not ready:
                raise GenerationError(f"entity dependency cycle among {sorted(pending)}")
            for eid in sorted(ready):
                done.append(eid)
                pending.pop(eid)
        return [self.spec.entity(e) for e in done]

    def _gather_keys(self, target: Grain, idx: np.ndarray) -> dict[str, np.ndarray]:
        return {c: target.key(c)[idx] for c in target.keys if c != TIME_KEY}

    def _assign(self, e: EntitySpec, rows: int, parent_rows: Optional[np.ndarray], t: Optional[int]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for a in e.assignments:
            tgt = self.grains[a.entity]
            if tgt.temporal or tgt.dynamic or not tgt.created:
                raise GenerationError(f"{e.entity_id}: assignment target {a.entity} must be a static, fixed grain")
            m = tgt.n
            col = a.column or tgt.entity.primary_key
            rng = stream(self.seed, "assign", e.entity_id, col, -1 if t is None else t)
            if m == 0:
                raise GenerationError(f"{e.entity_id}: assignment target {a.entity} has no rows")
            if a.method == "uniform":
                idx = rng.integers(0, m, rows)
            elif a.method == "balanced":
                idx = np.resize(np.arange(m), rows)
                rng.shuffle(idx)
            else:  # distinct_per_parent
                if parent_rows is None:
                    raise GenerationError(f"{e.entity_id}: distinct_per_parent needs a child entity")
                idx = np.empty(rows, dtype=np.int64)
                starts = np.flatnonzero(np.r_[True, parent_rows[1:] != parent_rows[:-1]]) if rows else np.array([], int)
                ends = np.r_[starts[1:], rows]
                for s, en in zip(starts, ends):
                    k = en - s
                    if k > m:
                        raise GenerationError(f"{e.entity_id}: {k} distinct {a.entity} needed, only {m} exist")
                    idx[s:en] = rng.choice(m, size=k, replace=False)
            for c, arr in self._gather_keys(tgt, idx).items():
                out.setdefault(c, arr)
            out[col] = tgt.key(tgt.entity.primary_key)[idx]
        return out

    def _create_fixed(self, e: EntitySpec) -> None:
        g = self.grains[e.entity_id]
        pk = e.primary_key
        if e.kind == "base":
            n = int(e.count or 0)
            keys = {pk: np.arange(1, n + 1, dtype=np.int64)}
            _merge(keys, self._assign(e, n, None, None))
            g.n_units = n
            g.add_rows(keys)
        elif e.kind == "cross":
            members = [self.grains[m] for m in e.cross]
            for m in members:
                if m.temporal or m.dynamic:
                    raise GenerationError(f"{e.entity_id}: cross member {m.entity.entity_id} must be static and fixed")
            sizes = [m.n for m in members]
            n_units = int(np.prod(sizes)) if sizes else 0
            grids = np.meshgrid(*[np.arange(s) for s in sizes], indexing="ij")
            unit_keys: dict[str, np.ndarray] = {}
            stride = 1
            strides = {}
            for m, s in zip(reversed(members), reversed(sizes)):
                strides[m.entity.primary_key] = stride
                stride *= s
            g.strides = strides
            for m, grid in zip(members, grids):
                idx = grid.ravel()
                for c, arr in self._gather_keys(m, idx).items():
                    unit_keys.setdefault(c, arr)
            g.n_units = n_units
            reps = self.T if e.temporal else 1
            keys = {c: np.tile(arr, reps) for c, arr in unit_keys.items()}
            keys[pk] = np.arange(1, n_units * reps + 1, dtype=np.int64)
            if e.temporal:
                keys[TIME_KEY] = np.repeat(np.arange(self.T, dtype=np.int64), n_units)
            _merge(keys, self._assign(e, n_units * reps, None, None))
            g.add_rows(keys)
        else:  # fixed-count child of a fixed parent
            parent = self.grains[e.parent]
            c = int(e.count if e.count is not None else 1)
            prow = np.repeat(np.arange(parent.n), c)
            keys = {col: parent.key(col)[prow] for col in parent.keys}
            keys[parent.entity.primary_key] = parent.key(parent.entity.primary_key)[prow]
            keys[pk] = np.arange(1, len(prow) + 1, dtype=np.int64)
            _merge(keys, self._assign(e, len(prow), prow, None))
            g.n_units = parent.n_units * c
            g.add_rows(keys)
        g.created = True

    def _create_dynamic(self, e: EntitySpec, t: Optional[int]) -> None:
        g = self.grains[e.entity_id]
        parent = self.grains[e.parent]
        a, b = parent.slice(t if parent.temporal else None)
        prow_all = np.arange(a, b)
        if e.count_variable:
            cnt = parent.values[e.count_variable].view()[a:b]
            cnt = np.maximum(0, np.rint(np.nan_to_num(np.asarray(cnt, dtype=float)))).astype(np.int64)
        else:
            cnt = np.full(b - a, int(e.count if e.count is not None else 1), dtype=np.int64)
        prow = np.repeat(prow_all, cnt)
        start_pk = g.n + 1
        keys = {col: parent.key(col)[prow] for col in parent.keys}
        keys[e.primary_key] = np.arange(start_pk, start_pk + len(prow), dtype=np.int64)
        _merge(keys, self._assign(e, len(prow), prow, t))
        if len(prow) == 0:
            if t is not None:
                g.steps[t] = (g.n, g.n)
            g.created = True
            return
        s, en = g.add_rows(keys)
        if t is not None:
            g.steps[t] = (s, en)
        g.created = True

    def _setup(self) -> None:
        for e in self._entity_order():
            temporal = is_temporal_entity(self.spec, e.entity_id)
            dynamic = is_dynamic_entity(self.spec, e.entity_id)
            g = Grain(entity=e, temporal=temporal, dynamic=dynamic)
            self.grains[e.entity_id] = g
            for v in self.spec.variables_of(e.entity_id):
                g.values[v.variable_id] = GrowArray(_dtype(v))
            if not dynamic:
                self._create_fixed(e)
            else:
                # register empty key arrays so lookups work before the first chunk exists
                parent = self.grains[e.parent]
                for col in parent.keys:
                    g.keys[col] = GrowArray(parent.keys[col].dtype)
                g.keys[e.primary_key] = GrowArray(np.int64)
                for a in e.assignments:
                    tgt = self.grains[a.entity]
                    for col in tgt.keys:
                        if col not in g.keys:
                            g.keys[col] = GrowArray(tgt.keys[col].dtype)
                    col = a.column or tgt.entity.primary_key
                    g.keys.setdefault(col, GrowArray(np.int64))

    # -------------------------------------------------------------------- randomness
    def _uniforms(self, g: Grain, vid: str, k: int, t: Optional[int], a: int, b: int) -> np.ndarray:
        if g.dynamic and g.temporal:
            return uniforms(self.seed, b - a, "u", vid, k, t)
        key = (vid, k)
        if key not in self._u_cache or len(self._u_cache[key]) < g.n:
            self._u_cache[key] = uniforms(self.seed, g.n, "u", vid, k)
        return self._u_cache[key][a:b]

    def _group_uniforms(self, vid: str, keys: np.ndarray) -> np.ndarray:
        keys = np.asarray(keys, dtype=np.int64)
        m = int(keys.max()) + 1 if len(keys) else 1
        return uniforms(self.seed, m, "group", vid)[keys]

    # -------------------------------------------------------------------- alignment
    def _locate(self, s: Grain, keys: dict[str, np.ndarray], t: Optional[int]) -> np.ndarray:
        e = s.entity
        if e.kind == "cross":
            u = np.zeros(len(next(iter(keys.values()))) if keys else 0, dtype=np.int64)
            for col, stride in s.strides.items():
                u = u + (np.asarray(keys[col], dtype=np.int64) - 1) * stride
            return (t * s.n_units + u) if s.temporal else u
        return np.asarray(keys[e.primary_key], dtype=np.int64) - 1

    def _initial(self, src: VariableSpec, p: ParentRef, n: int) -> np.ndarray:
        init = src.initial_state if src.initial_state is not None else p.fill_value
        if src.data_type in ("category", "text"):
            return np.full(n, str(init), dtype=object)
        return np.full(n, float(init))

    def _parent_values(self, v: VariableSpec, p: ParentRef, g: Grain, a: int, b: int, t: Optional[int],
                       keys: dict[str, np.ndarray]) -> np.ndarray:
        src = self.spec.variable(p.variable)
        s = self.grains[src.entity]
        rel = relation(self.spec, src.entity, v.entity)
        n = b - a
        vals = s.values[src.variable_id].view()
        if rel == "same" and not p.aggregation:
            if p.lag == 0:
                return vals[a:b]
            if t is None or t - p.lag < 0:
                return self._initial(src, p, n)
            off = p.lag * g.n_units
            return vals[a - off:b - off]
        if rel == "broadcast" and not p.aggregation:
            if s.temporal:
                if t is None or t - p.lag < 0:
                    return self._initial(src, p, n)
                idx = self._locate(s, keys, t) - p.lag * s.n_units
            else:
                idx = self._locate(s, keys, None)
            return vals[idx]
        if rel in ("aggregate", "same") and p.aggregation:
            step = (t - p.lag) if (s.temporal and g.temporal) else None
            if step is not None and step < 0:
                return np.full(n, float(p.fill_value))
            sa, sb = s.slice(step) if (s.temporal and g.temporal) else (0, s.n)
            svals = vals[sa:sb]
            if rel == "same":
                local = np.arange(sb - sa)
            else:
                skeys = {c: s.key(c)[sa:sb] for c in identity_columns(self.spec, g.entity.entity_id)}
                g_start = g.slice(step)[0] if (g.temporal and step is not None) else 0
                local = self._locate(g, skeys, step) - g_start
            return _aggregate(p.aggregation, svals, local, n, p.fill_value)
        raise GenerationError(f"{v.variable_id}: cannot read {p.variable} from grain {src.entity} on grain {v.entity} "
                              f"(relation={rel}, aggregation={p.aggregation})")

    def _time_env(self, t: Optional[int], n: int) -> dict[str, Any]:
        tt = 0 if t is None else t
        if tt not in self._time_env_cache:
            if self.dates:
                d = dt.date.fromisoformat(self.dates[min(tt, len(self.dates) - 1)])
                env = {"t": float(tt), "dow": float(d.weekday()), "month": float(d.month),
                       "doy": float(d.timetuple().tm_yday)}
            else:
                env = {"t": float(tt), "dow": 0.0, "month": 1.0, "doy": 1.0}
            env["step_frac"] = tt / max(1, self.T - 1)
            self._time_env_cache[tt] = env
        return dict(self._time_env_cache[tt])

    # -------------------------------------------------------------------- evaluation
    def _parse(self, v: VariableSpec):
        if v.variable_id not in self._parsed:
            m = M.get(v.mechanism.type)
            self._parsed[v.variable_id] = (m, m.parse(v.mechanism.params))
        return self._parsed[v.variable_id]

    def _active(self, v: VariableSpec, g: Grain, t: Optional[int]) -> list[tuple[EventSpec, float]]:
        out = []
        for ev in self.events:
            if v.variable_id not in ev.affected_variables:
                continue
            s0, s1 = self._ev_steps[ev.event_id]
            if g.temporal:
                if t is None or not (s0 <= t < s1):
                    continue
                out.append((ev, ramp_factor(ev, t - s0)))
            else:
                out.append((ev, 1.0))
        return out

    def _event_mask(self, ev: EventSpec, keys: dict[str, np.ndarray], tenv: dict[str, Any], n: int) -> np.ndarray:
        if not ev.affected_entities:
            return np.ones(n, dtype=bool)
        env = {**tenv, **keys}
        try:
            return np.asarray(evaluate(ev.affected_entities, env, n)).astype(bool)
        except ExpressionError as e:
            raise GenerationError(f"event {ev.event_id}: invalid affected_entities filter – {e}") from e

    def _eval_var(self, v: VariableSpec, t: Optional[int]) -> None:
        g = self.grains[v.entity]
        a, b = g.slice(t)
        n = b - a
        if n <= 0:
            return
        keys = {c: g.key(c)[a:b] for c in g.keys}
        tenv = self._time_env(t, n)
        env: dict[str, Any] = dict(tenv)
        for p in v.parents:
            env[p.name] = self._parent_values(v, p, g, a, b, t, keys)
        m, base = self._parse(v)
        active = self._active(v, g, t)
        param_events = [(ev, r, self._event_mask(ev, keys, tenv, n)) for ev, r in active
                        if ev.intervention_type == "param_change"]
        value_events = [(ev, r, self._event_mask(ev, keys, tenv, n)) for ev, r in active
                        if ev.intervention_type != "param_change"]
        scale = float(self.noise_scale.get(v.variable_id, 1.0))
        mkeys = {**keys, "__pk__": keys[g.entity.primary_key]}
        vid = v.variable_id

        def run(idx: Optional[np.ndarray], params_obj) -> M.MechOutput:
            sub_env = env if idx is None else {k: (val[idx] if isinstance(val, np.ndarray) and val.ndim == 1
                                                    and len(val) == n else val) for k, val in env.items()}
            sub_keys = mkeys if idx is None else {k: val[idx] for k, val in mkeys.items()}
            k_n = n if idx is None else len(idx)

            def uf(k: int) -> np.ndarray:
                u = self._uniforms(g, vid, k, t, a, b)
                return u if idx is None else u[idx]

            ctx = M.MechContext(var=v, params=v.mechanism.params, env=sub_env, n=k_n, uniforms=uf,
                                group_uniforms=lambda ks: self._group_uniforms(vid, ks), noise_scale=scale,
                                keys=sub_keys)
            try:
                return m.generate(params_obj, ctx)
            except (ExpressionError, M.MechanismError, ValueError, FloatingPointError) as e:
                raise GenerationError(f"{vid} (step {t}): {e}") from e

        pevt = np.zeros(n, dtype=bool)
        if not param_events:
            out = run(None, base)
            values, aux = np.asarray(out.values), out.aux
        else:
            code = np.zeros(n, dtype=np.int64)
            for i, (_, _, mask) in enumerate(param_events):
                code |= (mask.astype(np.int64) << i)
            pevt = code > 0
            values = None
            aux: dict[str, np.ndarray] = {}
            for c in np.unique(code):
                idx = np.flatnonzero(code == c)
                params = dict(v.mechanism.params)
                for i, (ev, r, _) in enumerate(param_events):
                    if not (c >> i) & 1:
                        continue
                    ip = ev.intervention_parameters
                    path = ip.get("param")
                    if not path:
                        raise GenerationError(f"event {ev.event_id}: param_change needs 'param'")
                    cur = _get_path(params, path)
                    if "value" in ip:
                        new = ip["value"]
                    elif "delta" in ip:
                        new = float(cur) + float(ip["delta"])
                    else:
                        new = float(cur) * float(ip["factor"])
                    eff = float(cur) + r * (float(new) - float(cur)) if isinstance(cur, (int, float)) and \
                        isinstance(new, (int, float)) else (new if r >= 0.5 else cur)
                    params = _set_path(params, path, eff)
                po = m.parse(params)
                out = run(idx, po)
                vals = np.asarray(out.values)
                if values is None:
                    values = np.empty(n, dtype=vals.dtype if vals.dtype != object else object)
                    if values.dtype.kind == "f":
                        values[:] = np.nan
                if values.dtype != vals.dtype and values.dtype != object:
                    values = values.astype(np.result_type(values.dtype, vals.dtype))
                values[idx] = vals
                for k2, arr in out.aux.items():
                    arr = np.asarray(arr)
                    if k2 not in aux:
                        aux[k2] = np.full(n, np.nan) if arr.dtype.kind in "fiub" else np.full(n, None, dtype=object)
                    aux[k2][idx] = arr
        evt = np.zeros(n, dtype=bool)
        for ev, r, mask in value_events:
            if not mask.any():
                continue
            ip = ev.intervention_parameters
            values = np.asarray(values)
            if ev.intervention_type == "set_value":
                target = ip["value"]
                if v.data_type in ("float",) and isinstance(target, (int, float)):
                    new = (1 - r) * values.astype(float) + r * float(target)
                else:
                    u = uniforms(self.seed, n, "event", ev.event_id, vid, -1 if t is None else t)
                    hit = u < r
                    new = values.copy().astype(object) if v.data_type in ("category", "text") else values.copy()
                    new[hit] = target if v.data_type in ("category", "text") else (bool(target) if v.data_type == "bool"
                                                                                  else float(target))
                    mask = mask & hit
            elif ev.intervention_type == "shift":
                new = values.astype(float) + r * float(ip["delta"])
            elif ev.intervention_type == "scale":
                new = values.astype(float) * (1 + r * (float(ip["factor"]) - 1))
            else:  # seasonal
                amp, per = float(ip["amplitude"]), float(ip.get("period", 7.0))
                ph = float(ip.get("phase", 0.0))
                s = amp * math.sin(2 * math.pi * ((t or 0) + ph) / per)
                new = values.astype(float) * (1 + s) if ip.get("mode", "add") == "multiply" else values.astype(float) + s
            values = np.where(mask, new, values) if np.asarray(new).dtype != object else \
                np.where(mask, new, values.astype(object))
            evt |= mask
            rec = self.realized.setdefault(ev.event_id, {"rows": {}, "steps": set(), "entities": {}})
            rec["rows"][vid] = rec["rows"].get(vid, 0) + int(mask.sum())
            if t is not None:
                rec["steps"].add(int(t))
            self._record_entities(rec, keys, mask)
        for ev, r, mask in param_events:
            rec = self.realized.setdefault(ev.event_id, {"rows": {}, "steps": set(), "entities": {}})
            rec["rows"][vid] = rec["rows"].get(vid, 0) + int(mask.sum())
            if t is not None and mask.any():
                rec["steps"].add(int(t))
            self._record_entities(rec, keys, mask)
        values = self._finish(v, values)
        g.values[vid].view()[a:b] = values
        for name, arr in aux.items():
            arr = np.asarray(arr)
            store = g.ensure_aux(f"{vid}::{name}", float if arr.dtype.kind in "fiub" else object)
            store.view()[a:b] = arr
        if active:
            g.ensure_aux(f"{vid}::__evt", bool).view()[a:b] = evt
            g.ensure_aux(f"{vid}::__pevt", bool).view()[a:b] = pevt

    def _record_entities(self, rec: dict, keys: dict[str, np.ndarray], mask: np.ndarray) -> None:
        if not mask.any():
            return
        for e in self.spec.entities:
            if e.kind == "base" and e.primary_key in keys:
                ids = np.unique(keys[e.primary_key][mask])
                if len(ids) <= 200:
                    rec["entities"].setdefault(e.entity_id, set()).update(int(x) for x in ids)

    def _finish(self, v: VariableSpec, values: np.ndarray) -> np.ndarray:
        c = v.constraints
        if v.data_type in ("float", "int"):
            x = np.asarray(values, dtype=float)
            if c.clip and (c.min is not None or c.max is not None):
                x = np.clip(x, c.min if c.min is not None else -np.inf, c.max if c.max is not None else np.inf)
            if c.round is not None and v.data_type == "float":
                x = np.round(x, c.round)
            if v.data_type == "int":
                if not np.all(np.isfinite(x)):
                    raise GenerationError(f"{v.variable_id}: non-finite values for an integer variable")
                return np.rint(x).astype(np.int64)
            if not np.all(np.isfinite(x)):
                raise GenerationError(f"{v.variable_id}: produced non-finite values")
            return x
        if v.data_type == "bool":
            return np.asarray(values).astype(bool)
        return np.asarray(values, dtype=object).astype(str).astype(object)

    def _eval_node(self, node: str, t: Optional[int]) -> None:
        if node.startswith(ENTITY_NODE):
            e = self.spec.entity(node[len(ENTITY_NODE):])
            self._create_dynamic(e, t)
        else:
            self._eval_var(self.spec.variable(node), t)

    # -------------------------------------------------------------------- run
    def run(self) -> CleanWorld:
        t0 = time.time()
        try:
            plan: ExecutionPlan = execution_plan(self.spec)
        except ValueError as e:
            raise GenerationError(str(e)) from e
        self._setup()
        timings = {"setup_s": round(time.time() - t0, 3)}
        for node in plan.pre:
            self._eval_node(node, None)
        t1 = time.time()
        for t in range(self.T if plan.loop else 0):
            for node in plan.loop:
                self._eval_node(node, t)
        timings["loop_s"] = round(time.time() - t1, 3)
        for node in plan.post:
            self._eval_node(node, None)
        world = self._materialize()
        world.timings = {**timings, "total_s": round(time.time() - t0, 3)}
        return world

    def _materialize(self) -> CleanWorld:
        spec = self.spec
        tables: dict[str, pd.DataFrame] = {}
        internals: dict[str, pd.DataFrame] = {}
        tcol = spec.time.time_column if spec.time else "date"
        dates = np.asarray(self.dates, dtype=object)
        for e in spec.entities:
            g = self.grains[e.entity_id]
            data: dict[str, Any] = {c: arr.view().copy() for c, arr in g.keys.items()}
            for vid, arr in g.values.items():
                data[vid] = arr.view().copy()
            for name, arr in g.aux.items():
                data[f"__aux__{name}"] = arr.view().copy()
            internal = pd.DataFrame(data)
            internals[e.entity_id] = internal
            if not e.export:
                continue
            out: dict[str, Any] = {c: internal[c].to_numpy() for c in own_key_columns(spec, e.entity_id)}
            if g.temporal and TIME_KEY in internal:
                out[tcol] = dates[internal[TIME_KEY].to_numpy()] if len(dates) else internal[TIME_KEY].to_numpy()
            for v in spec.variables_of(e.entity_id):
                if v.column:
                    out[v.column] = internal[v.variable_id].to_numpy()
            tables[e.name] = pd.DataFrame(out)
        realized = []
        for ev in self.events:
            rec = self.realized.get(ev.event_id, {"rows": {}, "steps": set(), "entities": {}})
            steps = sorted(rec["steps"])
            realized.append({
                "event_id": ev.event_id, "kind": ev.kind, "description": ev.description,
                "intervention_type": ev.intervention_type, "affected_variables": ev.affected_variables,
                "affected_rows": rec["rows"], "affected_entities": {k: sorted(v) for k, v in rec["entities"].items()},
                "first_step": steps[0] if steps else None, "last_step": steps[-1] if steps else None,
                "first_date": self.dates[steps[0]] if steps and self.dates else None,
                "last_date": self.dates[steps[-1]] if steps and self.dates else None,
                "realized": bool(sum(rec["rows"].values())),
            })
        return CleanWorld(spec=spec, seed=self.seed, tables=tables, internals=internals, dates=list(self.dates),
                          realized_events=realized, noise_scale=dict(self.noise_scale),
                          world_hash=tables_hash({**tables, **{
                              f"__int__{k}": v[[c for c in v.columns if not c.startswith("__aux__")]]
                              for k, v in internals.items()}}),
                          spec_hash=spec.fingerprint(), timings={})


def _merge(keys: dict[str, np.ndarray], extra: dict[str, np.ndarray]) -> None:
    """Add assignment keys; existing (parent lineage) keys take precedence."""
    for c, arr in extra.items():
        keys.setdefault(c, arr)


def _aggregate(fn: str, vals: np.ndarray, local: np.ndarray, n: int, fill: float) -> np.ndarray:
    local = np.asarray(local, dtype=np.int64)
    ok = (local >= 0) & (local < n)
    local, vals = local[ok], np.asarray(vals)[ok]
    cnt = np.bincount(local, minlength=n).astype(float)
    if fn == "count":
        return cnt
    x = vals.astype(float)
    if fn == "sum":
        out = np.bincount(local, weights=x, minlength=n)
        return out
    if fn == "mean":
        s = np.bincount(local, weights=x, minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, s / np.where(cnt > 0, cnt, 1), fill)
    if fn == "any":
        s = np.bincount(local, weights=(x != 0).astype(float), minlength=n)
        return (s > 0).astype(float)
    out = np.full(n, np.inf if fn == "min" else -np.inf)
    (np.minimum if fn == "min" else np.maximum).at(out, local, x)
    return np.where(cnt > 0, out, fill)


def generate_world(spec: SCMSpec, seed: Optional[int] = None, noise_scale: Optional[dict[str, float]] = None,
                   extra_events: Optional[list[EventSpec]] = None) -> CleanWorld:
    return Engine(spec, seed=seed, noise_scale=noise_scale, extra_events=extra_events).run()
