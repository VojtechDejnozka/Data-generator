"""Ground-truth based evaluation of a detective result (spec §13–14).

Deterministic metrics only (no LLM judge by default):

* graph – skeleton / directed precision, recall, F1, SHD against the *observable* truth graph (latent
  projection; latent variables are not counted as missed nodes). Orientation is scored only on edges
  whose orientation is identifiable (CPDAG-compelled or time-ordered); feedback edges accept both
  directions; Markov-equivalent (undirected) edges are excluded from the orientation score and an
  honest "undirected" answer earns partial credit on identifiable ones.
* story – realised story facts weighted by importance (edges, events, root cause, affected entities,
  timing); unrealised facts are reported but excluded; facts are never removed because the observed
  data lost evidence.
* effects – detective estimates vs. true direct (coefficients) / total (interventional) effects of the
  same type; a regression coefficient is never compared with a total effect of a different type.
* evidence – cited evidence ids must exist in the harness tool log, have succeeded and involve the
  claimed variables; confidence calibration (1 − Brier) rewards admitted uncertainty.

Recovery ``R = Σ w_i score_i`` over evaluable metrics (weights renormalised proportionally),
difficulty ``D = 100 (1 − R)``. Not evaluable → ``status = not_evaluable`` (never "maximum difficulty").
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from typing import Any, Optional

import numpy as np

from .contracts import EvaluationConfig, SCMSpec
from .graph import truth_graph
from .results import DetectiveResult, DifficultyEstimate, EvaluationResult, MetricScore


# =========================================================================== variable mapping
class VarMap:
    def __init__(self, spec: SCMSpec):
        self.spec = spec
        self.col2var: dict[str, str] = {}
        self.table_vars: dict[str, list[str]] = {}
        self.fk_entity: dict[str, str] = {}
        for v in spec.variables:
            e = spec.entity(v.entity)
            if v.column and e and e.export and v.observability == "observed":
                self.col2var[f"{e.name}.{v.column}"] = v.variable_id
                self.table_vars.setdefault(e.name, []).append(v.variable_id)
        for e in spec.entities:
            # 'table.fk_column' and 'table.pk' -> entity of the referenced key
            for ent in spec.entities:
                self.fk_entity[f"{e.name}.{ent.primary_key}"] = ent.entity_id

    def var(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        base = name.split("=", 1)[0].strip()
        return self.col2var.get(base)

    def table(self, name: Optional[str]) -> Optional[str]:
        return name.split(".", 1)[0] if name and "." in name else None

    def entity(self, key: Optional[str]) -> Optional[str]:
        return self.fk_entity.get(key or "")


def _prf(found: set, truth: set) -> dict[str, Optional[float]]:
    tp = len(found & truth)
    p = tp / len(found) if found else None
    r = tp / len(truth) if truth else None
    f1 = (2 * p * r / (p + r)) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "found": len(found), "truth": len(truth)}


# =========================================================================== graph
def graph_metrics(spec: SCMSpec, result: DetectiveResult, vm: VarMap) -> tuple[dict[str, Any], MetricScore, MetricScore]:
    tg = truth_graph(spec)
    truth_skel = tg.skeleton()
    found_skel: set = set()
    found_dir: set = set()
    marks: dict[frozenset, tuple[str, Optional[tuple[str, str]]]] = {}
    unmapped = 0
    for r in result.inferred_relationships:
        if r.edge_type == "none":
            continue
        a, b = vm.var(r.source), vm.var(r.target)
        if not a or not b or a == b:
            unmapped += 1
            continue
        k = frozenset((a, b))
        found_skel.add(k)
        if r.edge_type == "directed":
            found_dir.add((a, b))
            marks[k] = ("directed", (a, b))
        else:
            marks.setdefault(k, (r.edge_type, None))
    skel = _prf(found_skel, truth_skel)
    if found_skel or unmapped:
        tp = skel["tp"]
        skel["precision"] = tp / (len(found_skel) + unmapped) if (found_skel or unmapped) else None
        p, rr = skel["precision"], skel["recall"]
        skel["f1"] = 2 * p * rr / (p + rr) if p and rr else 0.0
    skel["unmapped_edges"] = unmapped
    directed = _prf(found_dir, set(tg.directed))
    # orientation on identifiable edges
    scores, details = [], []
    honest_on_unidentifiable = []
    for k in truth_skel & found_skel:
        status = tg.orientation.get(k, "undirected")
        mark, d = marks.get(k, ("undirected", None))
        truth_dirs = {e for e in tg.directed if frozenset(e) == k}
        if status == "compelled":
            s = 1.0 if d in truth_dirs else (0.5 if mark in ("undirected", "uncertain", "bidirected") else 0.0)
            scores.append(s)
            details.append({"edge": sorted(k), "truth": sorted(map(list, truth_dirs)), "found": mark, "score": s})
        elif status == "feedback":
            scores.append(1.0)
        else:
            honest_on_unidentifiable.append(mark in ("undirected", "uncertain", "bidirected"))
    # structural hamming distance between truth (CPDAG marks) and detective graph
    shd = 0
    for k in truth_skel | found_skel:
        if (k in truth_skel) != (k in found_skel):
            shd += 1
            continue
        status = tg.orientation.get(k, "undirected")
        mark, d = marks.get(k, ("undirected", None))
        truth_dirs = {e for e in tg.directed if frozenset(e) == k}
        if status == "compelled" and d not in truth_dirs:
            shd += 1
        elif status == "undirected" and mark == "directed":
            shd += 1
    metrics = {"skeleton": skel, "directed": directed, "shd": shd + unmapped,
               "orientation_details": details, "truth_graph": tg.to_dict(),
               "identifiable_edges": sum(1 for s in tg.orientation.values() if s == "compelled"),
               "unidentifiable_marked_uncertain": (float(np.mean(honest_on_unidentifiable))
                                                   if honest_on_unidentifiable else None)}
    if not truth_skel:
        sk = MetricScore(name="skeleton", status="not_evaluable", reason="the observable truth graph has no edges")
    else:
        sk = MetricScore(name="skeleton", score=float(skel["f1"] or 0.0), details=skel)
    if not scores:
        orient = MetricScore(name="orientation", status="not_evaluable",
                             reason="no identifiable edge of the truth graph was recovered")
    else:
        orient = MetricScore(name="orientation", score=float(np.mean(scores)),
                             details={"n": len(scores), "accuracy_strict": float(np.mean([s == 1 for s in scores]))})
    return metrics, sk, orient


# =========================================================================== story
def _date(s: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _period_days(spec: SCMSpec) -> int:
    return {"D": 1, "W": 7, "M": 30}[spec.time.frequency] if spec.time else 1


def story_metric(spec: SCMSpec, manifest: dict[str, Any], result: DetectiveResult, vm: VarMap,
                 cfg: EvaluationConfig) -> tuple[MetricScore, list[dict[str, Any]]]:
    realized = {r["fact_id"]: r for r in manifest.get("realized_story_facts", [])}
    events = {e["event_id"]: e for e in manifest.get("realized_events", [])}
    rows = []
    num = den = 0.0
    rels = []
    for r in result.inferred_relationships:
        a, b = vm.var(r.source), vm.var(r.target)
        if a and b:
            rels.append((a, b, r))
    from .ground_truth import default_evidence_variables
    for f in spec.ground_truth.story_facts:
        rec = {"fact_id": f.fact_id, "importance": f.importance, "kind": f.kind}
        rz = realized.get(f.fact_id, {})
        if not rz.get("realized"):
            rec.update(status="not_realized", score=None)
            rows.append(rec)
            continue
        if f.kind in ("edge", "direction", "lag"):
            sv, tv = spec.variable(f.source), spec.variable(f.target)
            if not (sv and tv and sv.observability == "observed" and tv.observability == "observed" and sv.column and tv.column):
                rec.update(status="not_evaluable", score=None, reason="endpoint is latent / not materialised")
                rows.append(rec)
                continue
            best = 0.0
            for a, b, r in rels:
                if {a, b} != {f.source, f.target}:
                    continue
                s = 0.6
                if f.expected_direction and r.estimated_direction == f.expected_direction:
                    s += 0.2
                if r.edge_type == "directed" and (a, b) == (f.source, f.target):
                    s += 0.2
                elif r.edge_type in ("undirected", "uncertain"):
                    s += 0.1
                if f.expected_lag and r.optional_lag == f.expected_lag:
                    s = min(1.0, s + 0.1)
                best = max(best, min(1.0, s))
            rec.update(status="ok", score=best)
        else:
            ev = events.get(f.event_id or "", {})
            t0 = _date(ev.get("first_date"))
            evars = set(default_evidence_variables(spec, f))
            etables = {spec.entity(spec.variable(v).entity).name for v in evars if spec.variable(v)}
            tol = f.tolerance_periods * _period_days(spec)
            best, best_d = 0.0, None
            for k in result.key_events:
                kv = vm.var(k.variable)
                s_var = 0.35 if kv in evars else (0.15 if vm.table(k.variable) in etables else 0.0)
                ent = vm.entity(k.entity)
                truth_ids = set()
                for eid, ids in f.entity_ids.items():
                    if eid == ent:
                        truth_ids = set(ids)
                s_ent = 0.0
                if truth_ids and k.entity_ids:
                    s_ent = 0.3 * len(truth_ids & set(k.entity_ids)) / len(truth_ids | set(k.entity_ids))
                elif not f.entity_ids:
                    s_ent = 0.3
                s_time = 0.0
                kd = _date(k.start_time)
                if t0 and kd:
                    delta = abs((kd - t0).days)
                    s_time = 0.25 if delta <= tol else max(0.0, 0.25 * (1 - (delta - tol) / (2 * tol)))
                elif not t0:
                    s_time = 0.25
                s_dir = 0.1 if (f.expected_direction and k.direction == f.expected_direction) or \
                    (spec.variable(kv) and spec.variable(kv).data_type in ("category", "text") and k.direction == "+") else 0.0
                s = s_var + s_ent + s_time + s_dir
                if f.kind == "root_cause" and k.role != "root_cause":
                    s *= 0.6
                if s > best:
                    best, best_d = s, {"variable": k.variable, "entity": k.entity, "ids": k.entity_ids,
                                       "start": k.start_time, "role": k.role}
            rec.update(status="ok", score=round(best, 4), matched=best_d)
        num += f.importance * rec["score"]
        den += f.importance
        rows.append(rec)
    if den == 0:
        return MetricScore(name="story", status="not_evaluable", reason="no realised, evaluable story facts"), rows
    return MetricScore(name="story", score=num / den, details={"facts": len(rows)}), rows


# =========================================================================== effects
def effect_metric(spec: SCMSpec, manifest: dict[str, Any], result: DetectiveResult, vm: VarMap,
                  cfg: EvaluationConfig) -> tuple[MetricScore, list[dict[str, Any]]]:
    truth = manifest.get("true_effects", [])
    key_pairs = {tuple(p) for p in spec.ground_truth.effects_to_estimate}
    key_pairs |= {(f.source, f.target) for f in spec.ground_truth.story_facts if f.kind == "edge" and f.source and f.target}
    changed = {v for e in spec.events if e.intervention_type == "param_change" for v in e.affected_variables}
    evaluable = []
    for t in truth:
        if t.get("estimate") is None or (t["source"], t["target"]) not in key_pairs:
            continue
        sv, tv = spec.variable(t["source"]), spec.variable(t["target"])
        if not (sv and tv and sv.observability == "observed" and tv.observability == "observed" and sv.column and tv.column):
            continue
        if t["effect_type"] == "direct" and t["target"] in changed:
            continue  # coefficient differs between rows (event) – no single true value
        evaluable.append(t)
    if not evaluable:
        return MetricScore(name="effects", status="not_evaluable", reason="no comparable true effect"), []
    est = []
    for e in result.estimated_effects:
        a, b = vm.var(e.source), vm.var(e.target)
        if a and b:
            est.append((a, b, e))
    rows, scores = [], []
    compatible = {"direct": {"direct"}, "total": {"total", "ate"}, "ate": {"ate", "total"}}
    for t in evaluable:
        cands = [e for a, b, e in est if (a, b) == (t["source"], t["target"])]
        same = [e for e in cands if e.effect_type in compatible.get(t["effect_type"], {t["effect_type"]})]
        rec = {"source": t["source"], "target": t["target"], "type": t["effect_type"], "truth": t["estimate"]}
        if not cands:
            rec.update(score=0.0, status="missing")
        elif not same:
            e = cands[0]
            sign = np.sign(e.estimate) == np.sign(t["estimate"])
            rec.update(score=0.25 if sign else 0.0, status="type_mismatch", estimate=e.estimate, estimate_type=e.effect_type)
        else:
            e = same[0]
            tv = t["estimate"]
            if np.sign(e.estimate) != np.sign(tv) and tv != 0:
                s = 0.0
            else:
                rel = abs(e.estimate - tv) / max(abs(tv), 1e-9)
                s = max(0.0, 1 - rel / cfg.effect_relative_tolerance)
            rec.update(score=round(s, 4), status="ok", estimate=e.estimate)
        scores.append(rec["score"])
        rows.append(rec)
    return MetricScore(name="effects", score=float(np.mean(scores)), details={"n": len(scores)}), rows


# =========================================================================== evidence
def evidence_metric(spec: SCMSpec, result: DetectiveResult, vm: VarMap, tg_skeleton: set) -> tuple[MetricScore, dict]:
    log = {e.evidence_id: e for e in result.statistical_results}
    claims = []
    for r in result.inferred_relationships:
        claims.append(("rel", r.source, r.target, r.evidence_ids, r.confidence, r))
    for k in result.key_events:
        claims.append(("event", k.variable, k.entity, k.evidence_ids, k.confidence, k))
    if not claims:
        return MetricScore(name="evidence", status="not_evaluable", reason="the detective made no claims"), {}
    support, invalid_ids, overclaims = [], 0, 0
    for kind, a, b, ids, conf, obj in claims:
        items = [log[i] for i in ids if i in log]
        invalid_ids += len([i for i in ids if i not in log])
        ok_items = [i for i in items if i.ok]
        if not ok_items:
            support.append(0.0)
            continue
        names = [x.split("=", 1)[0] for x in (a, b) if x]
        cols = [n.split(".", 1)[-1] for n in names]
        def mentions(item) -> bool:
            blob = json.dumps(item.args, ensure_ascii=False, default=str) + json.dumps(item.result, ensure_ascii=False, default=str)[:200000]
            return all((n in blob) or (c in blob) for n, c in zip(names, cols))
        s = 1.0 if any(mentions(i) for i in ok_items) else 0.5
        if kind == "rel":
            analytic = {"discover_causal_graph", "fit_linear_regression", "fit_logistic_regression", "test_independence"}
            if obj.edge_type == "directed" and obj.identification_status == "identified" and conf > 0.8 and \
                    not any(i.tool in analytic for i in ok_items):
                s *= 0.5
                overclaims += 1
        support.append(s)
    briers = []
    for r in result.inferred_relationships:
        a, b = vm.var(r.source), vm.var(r.target)
        outcome = 1.0 if a and b and frozenset((a, b)) in tg_skeleton else 0.0
        briers.append((r.confidence - outcome) ** 2)
    calib = 1 - float(np.mean(briers)) if briers else None
    score = 0.5 * float(np.mean(support)) + (0.5 * calib if calib is not None else 0.5 * float(np.mean(support)))
    details = {"claims": len(claims), "mean_support": float(np.mean(support)), "invalid_evidence_ids": invalid_ids,
               "overclaims": overclaims, "calibration": calib, "brier": float(np.mean(briers)) if briers else None}
    return MetricScore(name="evidence", score=score, details=details), details


# =========================================================================== combine
def evaluate(spec: SCMSpec, manifest: dict[str, Any], result: DetectiveResult,
             cfg: Optional[EvaluationConfig] = None, observation_id: str = "") -> EvaluationResult:
    cfg = cfg or EvaluationConfig()
    vm = VarMap(spec)
    gm, sk, orient = graph_metrics(spec, result, vm)
    story, facts = story_metric(spec, manifest, result, vm, cfg)
    eff, effects = effect_metric(spec, manifest, result, vm, cfg)
    evid, evd = evidence_metric(spec, result, vm, truth_graph(spec).skeleton())
    metrics = {"story": story, "skeleton": sk, "orientation": orient, "effects": eff, "evidence": evid}
    avail = {k: cfg.weights.get(k, 0.0) for k, m in metrics.items() if m.status == "ok" and m.score is not None}
    total_w = sum(avail.values())
    out = EvaluationResult(evaluation_id=secrets.token_hex(6), observation_id=observation_id, metrics=metrics,
                           graph_metrics=gm, story_facts=facts, effects=effects, evidence=evd, config=cfg,
                           detective=result.agent)
    if total_w <= 0:
        out.status, out.reason = "not_evaluable", "no metric could be evaluated for this scenario"
        return out
    weights = {k: w / total_w for k, w in avail.items()}
    R = sum(weights[k] * metrics[k].score for k in weights)
    out.weights_used = {k: round(w, 4) for k, w in weights.items()}
    out.recovery_score = round(float(R), 6)
    out.difficulty = round(100 * (1 - float(R)), 3)
    return out


def aggregate(evals: list[EvaluationResult], agent: dict[str, Any] | None = None) -> DifficultyEstimate:
    ok = [e for e in evals if e.status == "ok" and e.difficulty is not None]
    if not ok:
        return DifficultyEstimate(status="not_evaluable", reason=(evals[0].reason if evals else "no evaluations"),
                                  n_evaluations=len(evals), agent=agent or {})
    d = np.array([e.difficulty for e in ok])
    return DifficultyEstimate(
        mean_difficulty=round(float(d.mean()), 3),
        std_difficulty=round(float(d.std(ddof=1)), 3) if len(d) > 1 else None,
        mean_recovery=round(float(np.mean([e.recovery_score for e in ok])), 6),
        scores=[float(x) for x in d], n_evaluations=len(ok), preliminary=len(ok) < 3,
        agent=agent or ok[0].detective, evaluation_ids=[e.evaluation_id for e in ok],
        observation_ids=sorted({e.observation_id for e in ok}))
