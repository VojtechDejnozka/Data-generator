"""Ground-truth manifest: approved SCM, realised graph & events, latent variables, replay info,
interventional effects, realised story facts, statistics, observability and identifiability.

Intended ground truth = the approved spec (``story_facts``). Realised ground truth = what actually
happened in this generated dataset (``realized_story_facts``): a fact is marked realised only if it
is visible in the clean world (correct sign and a non-trivial magnitude)."""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from . import mechanisms as M
from .contracts import EventSpec, SCMSpec, StoryFact, derive_edges
from .engine import TIME_KEY, CleanWorld, generate_world, relation
from .graph import descendants, is_temporal_entity, truth_graph
from .validation import _Frames


def _align(world: CleanWorld, frames: _Frames, source: str, target: str, lag: int = 0) -> Optional[np.ndarray]:
    """Source values aligned to the rows of the target grain (aggregated by mean if finer)."""
    spec = world.spec
    s, t = spec.variable(source), spec.variable(target)
    rel = relation(spec, s.entity, t.entity)
    if rel is None:
        return None
    from .contracts import ParentRef
    p = ParentRef(variable=source, lag=lag, aggregation="mean" if rel == "aggregate" else None)
    try:
        return frames.parent(t, p)
    except Exception:  # noqa: BLE001
        return None


def _align_common(world: CleanWorld, frames: _Frames, source: str, target: str) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """Both variables aggregated (mean) to the finest common coarser grain, e.g. weekly scores and a
    final result of the same student -> student level."""
    from .contracts import ParentRef, VariableSpec
    spec = world.spec
    s, t = spec.variable(source), spec.variable(target)
    best = None
    for e in spec.entities:
        rs, rt = relation(spec, s.entity, e.entity_id), relation(spec, t.entity, e.entity_id)
        if rs in ("aggregate", "same") and rt in ("aggregate", "same"):
            k = len(frames.keys(e.entity_id).columns)
            if best is None or k > best[0]:
                best = (k, e.entity_id)
    if best is None:
        return None
    probe = VariableSpec(variable_id="probe__", entity=best[1], mechanism={"type": "lookup"})
    try:
        out = []
        for vid in (source, target):
            agg = None if spec.variable(vid).entity == best[1] else "mean"
            out.append(frames.parent(probe, ParentRef(variable=vid, aggregation=agg)))
        return out[0], out[1]
    except Exception:  # noqa: BLE001
        return None


def _numeric(x: Any) -> np.ndarray:
    a = pd.Series(x)
    if a.dtype == object:
        num = pd.to_numeric(a, errors="coerce")
        if num.notna().mean() > 0.9:
            return num.to_numpy(float)
        return pd.factorize(a)[0].astype(float)
    return a.astype(float).to_numpy()


def edge_association(world: CleanWorld, frames: _Frames, source: str, target: str, lag: int = 0) -> dict[str, Any]:
    spec = world.spec
    x = _align(world, frames, source, target, lag)
    flipped = "target"
    if x is None:
        x = _align(world, frames, target, source, lag)
        flipped = "source"
        if x is None:
            pair = _align_common(world, frames, source, target)
            if pair is None:
                return {"corr": None, "n": 0, "note": "variables cannot be aligned"}
            x, y = pair
            flipped = "common grain"
        else:
            y = world.internals[spec.variable(source).entity][source].to_numpy()
    else:
        y = world.internals[spec.variable(target).entity][target].to_numpy()
    xa, ya = _numeric(x), _numeric(y)
    ok = np.isfinite(xa) & np.isfinite(ya)
    if ok.sum() < 10 or np.std(xa[ok]) == 0 or np.std(ya[ok]) == 0:
        return {"corr": None, "n": int(ok.sum()), "note": "constant or too few rows"}
    r = float(np.corrcoef(xa[ok], ya[ok])[0, 1])
    return {"corr": r, "n": int(ok.sum()), "aligned_on": flipped}


def _affected_mask(frames: _Frames, eid: str, entity_ids: dict[str, list[int]], spec: SCMSpec) -> Optional[np.ndarray]:
    keys = frames.keys(eid)
    mask = None
    for ent, ids in entity_ids.items():
        e = spec.entity(ent)
        if e and e.primary_key in keys:
            m = keys[e.primary_key].isin(ids).to_numpy()
            mask = m if mask is None else (mask & m)
    return mask


def event_did(world: CleanWorld, frames: _Frames, fact: StoryFact, var: str) -> dict[str, Any]:
    """Difference-in-differences of a variable (or its row rate for categorical / text) between
    affected and other entities, before vs after the event start."""
    spec = world.spec
    v = spec.variable(var)
    ev = spec.event(fact.event_id) if fact.event_id else None
    if not v or not ev or not spec.time or not is_temporal_entity(spec, v.entity):
        return {"did": None, "note": "not a time-varying variable / no event"}
    keys = frames.keys(v.entity)
    mask = _affected_mask(frames, v.entity, fact.entity_ids, spec)
    if mask is None:
        return {"did": None, "note": "affected entities not reachable from the grain"}
    start = spec.time.step_of(ev.start_time, 0)
    t = keys[TIME_KEY].to_numpy()
    post = t >= start
    if v.data_type in ("category", "text", "bool") and v.data_type != "bool":
        # rows per step (events such as complaints): count rate per affected / other unit
        df = pd.DataFrame({"t": t, "aff": mask})
        T = len(world.dates)
        rate = df.groupby(["aff", "t"]).size().unstack(0).reindex(range(T)).fillna(0)
        a = rate.get(True, pd.Series(0, index=rate.index))
        o = rate.get(False, pd.Series(0, index=rate.index))
        n_aff = max(1, len(fact.entity_ids) and 1)
        pre_a, post_a = a[a.index < start].mean(), a[a.index >= start].mean()
        pre_o, post_o = o[o.index < start].mean(), o[o.index >= start].mean()
        did = (post_a - pre_a) - (post_o - pre_o) * (a.mean() / o.mean() if o.mean() else 1)
        se = float(np.sqrt(a.var() / max(1, (a.index >= start).sum()) + a.var() / max(1, (a.index < start).sum()))) or 1.0
        return {"did": float(did), "z": float(did / se), "measure": "rows per step", "start_step": start}
    y = _numeric(world.internals[v.entity][var].to_numpy())
    groups = {}
    for name, m in (("aff_pre", mask & ~post), ("aff_post", mask & post), ("oth_pre", ~mask & ~post), ("oth_post", ~mask & post)):
        groups[name] = y[m]
    if any(len(g) < 3 for g in groups.values()):
        return {"did": None, "note": "empty group"}
    did = (groups["aff_post"].mean() - groups["aff_pre"].mean()) - (groups["oth_post"].mean() - groups["oth_pre"].mean())
    se = float(np.sqrt(sum(g.var(ddof=1) / len(g) for g in groups.values())))
    return {"did": float(did), "z": float(did / se) if se > 0 else 0.0, "measure": "mean", "start_step": start,
            "means": {k: float(g.mean()) for k, g in groups.items()}}


def default_evidence_variables(spec: SCMSpec, fact: StoryFact) -> list[str]:
    if fact.evidence_variables:
        return list(fact.evidence_variables)
    ev = spec.event(fact.event_id) if fact.event_id else None
    if not ev:
        return []
    out = []
    for vid in ev.affected_variables:
        v = spec.variable(vid)
        if v and v.observability == "observed" and v.column:
            out.append(vid)
        else:
            out += sorted(d for d in descendants(spec, vid) if spec.variable(d).observability == "observed"
                          and spec.variable(d).column)[:3]
    return out


def realize_story_facts(world: CleanWorld) -> list[dict[str, Any]]:
    spec = world.spec
    frames = _Frames(world)
    out = []
    events = {e["event_id"]: e for e in world.realized_events}
    for f in spec.ground_truth.story_facts:
        rec: dict[str, Any] = {"fact_id": f.fact_id, "kind": f.kind, "importance": f.importance,
                               "description": f.description, "realized": False, "evidence": {}}
        if f.kind in ("edge", "direction", "lag") and f.source and f.target:
            a = edge_association(world, frames, f.source, f.target, f.expected_lag or 0)
            rec["evidence"] = {"association": a}
            try:
                eff = interventional_effect(spec, world, f.source, f.target)
            except Exception as e:  # noqa: BLE001
                eff = {"estimate": None, "error": str(e)[:200]}
            rec["evidence"]["interventional"] = eff
            est = eff.get("estimate")
            if est is not None and np.isfinite(est):
                rec["realized"] = abs(est) > 1e-9 and (f.expected_direction is None or
                                                       (est > 0) == (f.expected_direction == "+"))
            r = a.get("corr")
            rec["observational_sign_agrees"] = None if r is None or f.expected_direction is None else \
                bool((r > 0) == (f.expected_direction == "+"))
        elif f.event_id:
            ev = events.get(f.event_id, {})
            rec["event"] = {k: ev.get(k) for k in ("first_date", "last_date", "first_step", "affected_entities", "realized")}
            vars_ = default_evidence_variables(spec, f)
            res = {vid: event_did(world, frames, f, vid) for vid in vars_}
            rec["evidence"] = res
            visible = [vid for vid, r in res.items() if r.get("z") is not None and abs(r["z"]) > 2 and
                       (f.expected_direction is None or (r["did"] > 0) == (f.expected_direction == "+")
                        or spec.variable(vid).data_type in ("category", "text"))]
            rec["visible_in"] = visible
            rec["realized"] = bool(ev.get("realized")) and bool(visible)
        out.append(rec)
    return out


def interventional_effect(spec: SCMSpec, world: CleanWorld, source: str, target: str,
                          delta: Optional[float] = None) -> dict[str, Any]:
    """Total effect per unit of ``source`` on the mean of ``target``: do(source := source + delta)
    with the same seed (common random numbers), finite difference."""
    sv = spec.variable(source)
    te = spec.variable(target).entity

    def mean_under(ev: EventSpec) -> float:
        alt = generate_world(spec, seed=world.seed, noise_scale=world.noise_scale, extra_events=[ev])
        return float(pd.to_numeric(alt.internals[te][target], errors="coerce").astype(float).mean())

    if sv.data_type == "bool":
        hi = mean_under(EventSpec(event_id="__do1__", affected_variables=[source], intervention_type="set_value",
                                  intervention_parameters={"value": True}, kind="control"))
        lo = mean_under(EventSpec(event_id="__do0__", affected_variables=[source], intervention_type="set_value",
                                  intervention_parameters={"value": False}, kind="control"))
        return {"source": source, "target": target, "effect_type": "ate", "estimate": hi - lo, "delta": 1.0,
                "method": "E[Y | do(X=1)] - E[Y | do(X=0)] with common random numbers"}
    if sv.data_type not in ("float", "int"):
        return {"source": source, "target": target, "effect_type": "total", "estimate": None,
                "note": "interventions on categorical variables are not evaluated"}
    x = pd.to_numeric(world.internals[sv.entity][source], errors="coerce")
    if delta is None:
        sd = float(x.std()) if x.notna().any() else 1.0
        delta = 1.0 if sv.data_type == "int" else max(1e-6, 0.25 * sd)
    base = float(pd.to_numeric(world.internals[te][target], errors="coerce").astype(float).mean())
    new = mean_under(EventSpec(event_id="__do__", affected_variables=[source], intervention_type="shift",
                               intervention_parameters={"delta": delta}, kind="control"))
    return {"source": source, "target": target, "effect_type": "total", "estimate": (new - base) / delta,
            "delta": delta, "method": "interventional finite difference with common random numbers"}


def true_effects(world: CleanWorld) -> list[dict[str, Any]]:
    spec = world.spec
    out = []
    for v in spec.variables:
        for p in v.parents:
            coef = M.coefficient_of(v, p.name)
            if coef is not None and not p.aggregation and v.mechanism.type in (
                    "linear", "polynomial", "interaction", "lagged_effect", "autoregressive") and \
                    "*" not in p.name and "^" not in p.name:
                out.append({"source": p.variable, "target": v.variable_id, "effect_type": "direct", "estimate": coef,
                            "lag": p.lag, "scale": "per unit of source on the target grain",
                            "note": "coefficient of a linear mechanism" + ("; changed by events" if any(
                                v.variable_id in e.affected_variables and e.intervention_type == "param_change"
                                for e in spec.events) else "")})
    for src, tgt in spec.ground_truth.effects_to_estimate:
        try:
            out.append(interventional_effect(spec, world, src, tgt))
        except Exception as e:  # noqa: BLE001
            out.append({"source": src, "target": tgt, "effect_type": "total", "estimate": None, "error": str(e)[:200]})
    return out


def realized_statistics(world: CleanWorld) -> dict[str, Any]:
    out = {}
    for v in world.spec.variables:
        s = world.internals[v.entity][v.variable_id]
        if v.data_type in ("float", "int", "bool"):
            x = pd.to_numeric(s, errors="coerce").astype(float)
            out[v.variable_id] = {"n": int(x.notna().sum()), "mean": float(x.mean()) if len(x) else None,
                                  "sd": float(x.std()) if len(x) > 1 else None,
                                  "min": float(x.min()) if len(x) else None, "max": float(x.max()) if len(x) else None}
        elif v.data_type == "category":
            out[v.variable_id] = {"n": int(len(s)), "frequencies": s.value_counts().head(20).to_dict()}
    return out


def build_manifest(world: CleanWorld, validation: dict[str, Any] | None = None, with_effects: bool = True) -> dict[str, Any]:
    spec = world.spec
    tg = truth_graph(spec)
    edges = derive_edges(spec)
    latent = [v.variable_id for v in spec.variables if v.observability == "latent" or not v.column]
    observed_cols = {v.variable_id: f"{spec.entity(v.entity).name}.{v.column}" for v in spec.variables
                     if v.observability == "observed" and v.column and spec.entity(v.entity).export}
    ident = {}
    for k, status in tg.orientation.items():
        a, b = sorted(k)
        ident[f"{a}|{b}"] = {
            "compelled": "orientation identifiable (v-structure / Meek rules / time order)",
            "undirected": "orientation NOT identifiable from observational data (Markov equivalent)",
            "feedback": "feedback over time (both directions exist in the summary graph)",
            "confounded": "latent confounding – adjacency may be visible, orientation not strictly scorable",
        }[status]
    manifest = {
        "kind": "scm_ground_truth",
        "schema_version": spec.meta.schema_version,
        "scenario_id": spec.meta.scenario_id,
        "spec_hash": world.spec_hash,
        "world_hash": world.world_hash,
        "seed": world.seed,
        "replay": {"seed": world.seed, "noise_scale": world.noise_scale,
                   "note": "every random component uses SeedSequence(seed, sha256(component id)); rerun the engine "
                           "with the approved spec and this seed to replay the structural noise exactly"},
        "story": {"original": spec.meta.original_story, "refined": spec.meta.refined_story},
        "scm": spec.model_dump(mode="json"),
        "causal_graph": {"nodes": [v.variable_id for v in spec.variables],
                         "edges": [e.model_dump() for e in edges],
                         "mechanisms": {v.variable_id: M.describe(v) for v in spec.variables}},
        "observed_truth_graph": tg.to_dict(),
        "identifiability": ident,
        "principally_non_identifiable": [f"{a}|{b}" for (a, b) in [tuple(sorted(k)) for k, s in tg.orientation.items()
                                                                     if s in ("undirected", "confounded")]],
        "latent_variables": latent,
        "observed_columns": observed_cols,
        "realized_events": world.realized_events,
        "story_facts": [f.model_dump() for f in spec.ground_truth.story_facts],
        "realized_story_facts": realize_story_facts(world),
        "true_effects": true_effects(world) if with_effects else [],
        "realized_statistics": realized_statistics(world),
        "control_groups": [c.model_dump() for c in spec.ground_truth.control_groups],
        "alternative_interpretations": spec.ground_truth.alternative_interpretations,
        "validation": validation or {},
        "row_counts": {k: int(len(v)) for k, v in world.tables.items()},
    }
    return manifest
