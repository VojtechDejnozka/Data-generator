"""Difficulty calibration (spec §11, §14–15).

* ``manual`` – one corruption configuration → observed dataset → detective (repeats) → evaluation.
  Difficulty is only measured, never optimised.
* ``automatic`` (default) – searches the corruption parameters θ so that the measured difficulty
  approaches the target: ``θ* = argmin |D̂(θ) − D_target| + penalties``. Every candidate is built
  from the same clean reference world (never cumulatively), with common random numbers.

The search does not assume monotonicity: it explores along the scaled diagonal of the box, refines
by interpolation between candidates that bracket the target (a local, empirically supported
monotonicity) and keeps exploring random directions. The best screened candidates are re-evaluated
with independent detective runs (and corruption seed variants) before a result is returned.

The optimisation logic is pure: it receives an ``Evaluator`` – the service passes the real
observe → detective → evaluate chain, tests pass a deterministic mock.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import numpy as np

from .contracts import CorruptionConfig, CorruptionOp, DifficultyConfig, SCMSpec, SearchDimension
from .corruption import critical_columns, table_infos
from .graph import is_temporal_entity
from .results import Candidate, DifficultyEstimate, OptimizationResult, ValidationCheck
from .rng import stream

DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "missing_values_mcar": (0.0, 0.6), "missing_values_mar": (0.0, 0.6), "missing_values_mnar": (0.0, 0.6),
    "gaussian_measurement_noise": (0.0, 2.0), "multiplicative_measurement_noise": (0.0, 1.0),
    "outliers": (0.0, 0.1), "rounding": (0.0, 1.0), "category_misclassification": (0.0, 0.5),
    "exact_duplicates": (0.0, 0.3), "near_duplicates": (0.0, 0.3), "column_value_shuffle": (0.0, 0.8),
    "timestamp_shift": (0.0, 0.5), "text_typos": (0.0, 0.8), "text_truncation": (0.0, 0.8),
    "inconsistent_formatting": (0.0, 0.5), "incorrect_foreign_keys": (0.0, 0.3), "hidden_columns": (0.0, 0.5),
    "sampling": (0.0, 0.7), "row_deletion": (0.0, 0.5),
}
_KINDS = {
    "missing_values_mcar": {"float", "int", "bool", "category", "text"}, "missing_values_mar": {"float", "int", "bool", "category"},
    "missing_values_mnar": {"float", "int", "bool"}, "gaussian_measurement_noise": {"float", "int"},
    "multiplicative_measurement_noise": {"float", "int"}, "outliers": {"float", "int"}, "rounding": {"float"},
    "category_misclassification": {"category", "bool"}, "column_value_shuffle": {"float", "int", "bool", "category"},
    "text_typos": {"text"}, "text_truncation": {"text"}, "inconsistent_formatting": {"float", "category"},
    "hidden_columns": {"float", "int", "bool", "category", "text"},
}


# =========================================================================== search space
def expand_op(spec: SCMSpec, op: CorruptionOp, protected: set[str], allow_critical_hiding: bool = False) -> list[CorruptionOp]:
    """Concrete ops of a template op (``target_table == '*'``) on every eligible table."""
    infos = table_infos(spec)
    tables = [op.target_table] if op.target_table != "*" else sorted(infos)
    crit = critical_columns(spec)
    out = []
    for t in tables:
        info = infos.get(t)
        if not info or t in {p.split(".")[0] for p in protected if p.endswith(".*")}:
            continue
        cols = list(op.target_columns)
        kinds = _KINDS.get(op.type)
        if kinds is not None and not cols:
            cols = [c for c, k in info.types.items() if k in kinds and f"{t}.{c}" not in protected
                    and c != info.time_column]
            if op.type == "hidden_columns" and not allow_critical_hiding:
                cols = [c for c in cols if f"{t}.{c}" not in crit]
            if not cols:
                continue
        params = dict(op.params)
        if op.type == "missing_values_mar" and "driver" not in params:
            nums = [c for c, k in info.types.items() if k in ("float", "int") and c in cols]
            if len(cols) < 2 or not nums:
                continue
            params["driver"] = nums[0]
            cols = [c for c in cols if c != nums[0]]
        if op.type in ("exact_duplicates", "near_duplicates", "sampling", "row_deletion", "timestamp_shift",
                       "incorrect_foreign_keys"):
            ent = spec.entity(info.entity)
            fact = is_temporal_entity(spec, info.entity) or ent.kind == "child"
            if not fact:
                continue  # keep master data intact by default
            if op.type == "timestamp_shift" and not info.time_column:
                continue
            if op.type == "incorrect_foreign_keys" and not info.fks:
                continue
            cols = cols if op.type in ("timestamp_shift", "incorrect_foreign_keys") else []
        out.append(op.model_copy(update={"target_table": t, "target_columns": cols, "params": params,
                                         "op_id": f"{op.type}:{t}"}))
    return out


def build_search_space(spec: SCMSpec, dcfg: DifficultyConfig) -> list[SearchDimension]:
    if dcfg.search_space:
        dims = list(dcfg.search_space)
    else:
        dims = []
        for i, typ in enumerate(dcfg.allowed_corruption_types):
            lo, hi = dcfg.parameter_bounds.get(typ, DEFAULT_BOUNDS.get(typ, (0.0, 0.5)))
            dims.append(SearchDimension(dim_id=typ, op=CorruptionOp(type=typ, target_table="*", operation_order=i),
                                        low=lo, high=hi))
    out = []
    for d in dims:
        if d.dim_id in dcfg.locked_parameters:
            d = d.model_copy(update={"locked_value": dcfg.locked_parameters[d.dim_id]})
        out.append(d)
    return out


def config_from_theta(spec: SCMSpec, space: list[SearchDimension], theta: dict[str, float], dcfg: DifficultyConfig,
                      seed: Optional[int] = None) -> CorruptionConfig:
    protected = set(dcfg.protected_columns) | {f"{t}.*" for t in dcfg.protected_tables}
    ops: list[CorruptionOp] = []
    for d in space:
        val = float(theta.get(d.dim_id, 0.0))
        if val <= 0:
            continue
        template = d.op.model_copy(update={"probability": min(1.0, val)})
        if d.op.type in ("gaussian_measurement_noise", "multiplicative_measurement_noise"):
            # the intensity (relative sd) may exceed 1; probability stays a [0, 1] share
            template = template.model_copy(update={"params": {**d.op.params, "intensity": val}})
        ops.extend(expand_op(spec, template, protected))
    return CorruptionConfig(operations=ops, protected_columns=dcfg.protected_columns,
                            protected_tables=dcfg.protected_tables, seed=seed)


# =========================================================================== evaluator protocol
@dataclass
class CandidateEval:
    estimate: DifficultyEstimate
    realized_share: float = 0.0
    observability: float = 1.0
    analyzable: bool = True
    checks: list[ValidationCheck] = field(default_factory=list)
    observation_id: str = ""
    world_hash: str = ""
    realized: dict[str, Any] = field(default_factory=dict)
    n_evaluations: int = 0
    usage: dict[str, Any] = field(default_factory=dict)  # {"calls", "tokens", "cost_usd", "by_model"}


def sum_usage(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum LLM usage summaries ({calls, tokens, cost_usd, by_model[{role, model, calls, ...}]})."""
    by: dict[str, dict[str, Any]] = {}
    out = {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    for u in items:
        if not u:
            continue
        out["calls"] += int(u.get("calls", 0))
        out["tokens"] += int(u.get("tokens", 0))
        out["cost_usd"] = round(out["cost_usd"] + float(u.get("cost_usd", 0.0)), 6)
        for m in u.get("by_model", []):
            k = f"{m.get('role')}:{m.get('model')}"
            b = by.setdefault(k, {kk: (0 if isinstance(vv, (int, float)) else vv) for kk, vv in m.items()})
            for kk, vv in m.items():
                if isinstance(vv, (int, float)) and not isinstance(vv, bool):
                    b[kk] = round(b[kk] + vv, 6) if isinstance(vv, float) else b[kk] + vv
    out["by_model"] = list(by.values())
    return out


Evaluator = Callable[[CorruptionConfig, float, int, str, int], Awaitable[CandidateEval]]
ProgressFn = Callable[[str, float], None]


def _objective(est: DifficultyEstimate, target: float, share: float, dcfg: DifficultyConfig) -> float:
    if est.mean_difficulty is None:
        return math.inf
    unc = (est.std_difficulty or 0.0) / math.sqrt(max(1, est.n_evaluations))
    return abs(est.mean_difficulty - target) + dcfg.uncertainty_penalty * unc + dcfg.corruption_penalty * share


def _reject(ev: CandidateEval, dcfg: DifficultyConfig) -> str:
    if ev.estimate.status != "ok":
        return f"not evaluable: {ev.estimate.reason}"
    if not ev.analyzable:
        return "observed dataset is not analysable"
    if ev.realized_share > dcfg.maximum_corruption_budget + 1e-9:
        return f"corruption share {ev.realized_share:.3f} exceeds the budget {dcfg.maximum_corruption_budget}"
    if ev.observability < dcfg.minimum_observability - 1e-9:
        return f"story observability {ev.observability:.3f} below the minimum {dcfg.minimum_observability}"
    bad = [c for c in ev.checks if not c.passed and c.severity == "error"]
    if bad:
        return "; ".join(f"{c.check_id}: {c.explanation}" for c in bad)[:300]
    return ""


class _Proposer:
    """Bracketing / interpolation along directions + random exploration (no monotonicity assumed)."""

    def __init__(self, space: list[SearchDimension], dcfg: DifficultyConfig, seed: int):
        self.free = [d for d in space if d.locked_value is None]
        self.space = space
        self.rng = stream(seed, "difficulty-search")
        self.dcfg = dcfg
        self.k = 0
        self.s_max = 1.0  # scale beyond which candidates violated the budget / observability

    def theta(self, s: float, w: np.ndarray, noise: Optional[float] = None) -> dict[str, float]:
        th = {d.dim_id: float(d.locked_value) for d in self.space if d.locked_value is not None}
        for d, wi in zip(self.free, w):
            th[d.dim_id] = float(d.low + np.clip(s * wi, 0, 1) * (d.high - d.low))
        if self.dcfg.allow_structural_noise_optimization:
            lo, hi = self.dcfg.structural_noise_bounds
            th["__noise_scale"] = float(noise if noise is not None else lo)
        return th

    def next(self, history: list[tuple[float, np.ndarray, float, Optional[float], bool]], target: float) -> tuple[float, np.ndarray, Optional[float]]:
        """history: (s, w, difficulty, noise, rejected)."""
        self.k += 1
        n = len(self.free)
        diag = np.ones(n)
        ok = [h for h in history if not h[4] and h[2] is not None and np.allclose(h[1], diag)]
        noise = None
        if self.dcfg.allow_structural_noise_optimization:
            lo, hi = self.dcfg.structural_noise_bounds
            noise = float(lo + self.rng.random() * (hi - lo)) if self.k % 2 == 0 else lo
        explore = self.k % 3 == 0 and n > 1
        if not explore:
            s = 0.3 if not ok else float(np.clip(self._interp(ok, target), 0.0, 1.0))
            s = min(s, self.s_max)
            tried = {round(h[0], 4) for h in ok}
            if round(s, 4) in tried:  # stuck at the budget cap / in a non-monotone region
                if n > 1:
                    explore = True
                else:
                    s = float(np.clip(s + (self.rng.random() - 0.5) * 0.2, 0.0, self.s_max))
            if not explore:
                return s, diag, noise
        # exploration: random mixture of corruption types (re-weights the budget between them)
        w = np.clip(self.rng.dirichlet(np.ones(n)) * n, 0, 2.5)
        base = min(self._interp(ok, target) if ok else 0.3, self.s_max * 1.3)
        s = float(np.clip(base * (0.7 + 0.6 * self.rng.random()), 0.02, 1.0))
        return s, w, noise

    @staticmethod
    def _interp(ok, target: float) -> float:
        pts = sorted({(h[0], h[2]) for h in ok})
        below = [p for p in pts if p[1] <= target]
        above = [p for p in pts if p[1] > target]
        if below and above:
            # closest bracket in s
            best = None
            for a in below:
                for b in above:
                    gap = abs(a[0] - b[0])
                    if best is None or gap < best[0]:
                        best = (gap, a, b)
            _, a, b = best
            if b[1] == a[1]:
                return (a[0] + b[0]) / 2
            return a[0] + (target - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        if below:  # everything too easy -> go further out
            s, _ = max(below)
            return min(1.0, s + max(0.2, (1 - s) / 2))
        s, _ = min(above)
        return max(0.0, s / 2)


async def optimize(spec: SCMSpec, dcfg: DifficultyConfig, evaluator: Evaluator, run_id: str = "",
                   baseline: Optional[DifficultyEstimate] = None, progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> OptimizationResult:
    t_start = time.time()
    target, tol = dcfg.target_difficulty, dcfg.difficulty_tolerance
    space = build_search_space(spec, dcfg)
    say = progress or (lambda m, f: None)
    n_evals = 0
    usages: list[dict[str, Any]] = []
    history: list[Candidate] = []
    agent = (dcfg.final_detective or dcfg.screening_detective).model_dump()

    def budget_left() -> bool:
        if dcfg.evaluation_budget is not None and n_evals >= dcfg.evaluation_budget:
            return False
        if dcfg.token_budget is not None and sum_usage(usages)["tokens"] >= dcfg.token_budget:
            return False
        if dcfg.time_budget_s is not None and time.time() - t_start > dcfg.time_budget_s:
            return False
        return True

    res = OptimizationResult(run_id=run_id, status="target_not_reached", target_difficulty=target, tolerance=tol,
                             agent=agent)
    # ------------------------------------------------------------------ 1-2. clean baseline
    if baseline is None:
        say("Měřím baseline obtížnost (bez poškození)", 0.02)
        ev = await evaluator(CorruptionConfig(seed=dcfg.seed), 1.0, dcfg.screening_repeats, "baseline", 0)
        n_evals += ev.n_evaluations or dcfg.screening_repeats
        usages.append(ev.usage)
        baseline = ev.estimate
    res.baseline = baseline
    if baseline.status != "ok" or baseline.mean_difficulty is None:
        res.status, res.reason = "error", f"baseline not evaluable: {baseline.reason}"
        res.termination_reason = "baseline_not_evaluable"
        return res
    base_d = baseline.mean_difficulty
    if target < base_d - tol and not dcfg.allow_structural_noise_optimization:
        res.status = "unreachable_below_baseline"
        res.achieved_difficulty, res.achieved_std = base_d, baseline.std_difficulty
        res.selected_corruption = CorruptionConfig()
        res.termination_reason = "target below the clean baseline"
        res.reason = (f"Cílová obtížnost {target} je nižší než obtížnost čistých dat ({base_d:.1f}); samotným "
                      "poškozováním ji nelze snížit.")
        res.usage = sum_usage(usages)
        res.suggestions = ["Zvětšit velikost vzorku (počet entit / časových kroků).",
                           "Zesílit kauzální mechanismy nebo snížit strukturální šum (vyžaduje novou realitu).",
                           "Zpřístupnit latentní proměnné / zvýšit pozorovatelnost.",
                           "Použít silnějšího referenčního detektiva nebo větší evaluační rozpočet."]
        res.n_evaluations = n_evals
        return res
    # ------------------------------------------------------------------ 3-10. screening loop
    prop = _Proposer(space, dcfg, dcfg.seed)
    hist_pts: list[tuple[float, np.ndarray, float, Optional[float], bool]] = []
    finals: list[Candidate] = []
    finalized: set[str] = set()
    termination = "max_iterations"

    async def verify(c: Candidate) -> Candidate:
        """11. independent full evaluation (repeats + corruption seed variants) of a screened candidate."""
        nonlocal n_evals
        say(f"Finální ověření kandidáta {c.candidate_id} ({dcfg.evaluation_repeats}× nezávisle)", None)
        ev = await evaluator(c.corruption, c.structural_noise_scale, dcfg.evaluation_repeats, "final", 1)
        n_evals += ev.n_evaluations or dcfg.evaluation_repeats
        usages.append(ev.usage)
        reason = _reject(ev, dcfg)
        fc = c.model_copy(update={"candidate_id": c.candidate_id + "-final", "phase": "final", "estimate": ev.estimate,
                                  "observation_id": ev.observation_id, "realized_share": ev.realized_share,
                                  "rejected": bool(reason), "rejection_reason": reason,
                                  "objective": None if reason else _objective(ev.estimate, target, ev.realized_share,
                                                                              dcfg)})
        finals.append(fc)
        history.append(fc)
        finalized.add(c.candidate_id)
        if not reason:
            say(f"  ověřeno: {ev.estimate.mean_difficulty:.1f} ± {ev.estimate.std_difficulty or 0:.1f}", None)
        return fc

    if not prop.free:
        termination = "no free parameters"
    it = 0
    while prop.free and it < dcfg.max_iterations:
        it += 1
        if should_stop and should_stop():
            termination = "cancelled"
            break
        if not budget_left():
            termination = "budget_exhausted"
            break
        s, w, noise = prop.next(hist_pts, target)
        theta = prop.theta(s, w, noise)
        cfg = config_from_theta(spec, space, theta, dcfg, seed=dcfg.seed)
        scale = float(theta.get("__noise_scale", 1.0))
        say(f"Kandidát {it}/{dcfg.max_iterations}: " + ", ".join(f"{k}={v:.3f}" for k, v in theta.items()),
            0.05 + 0.75 * it / dcfg.max_iterations)
        try:
            ev = await evaluator(cfg, scale, dcfg.screening_repeats, "screening", 0)
        except Exception as e:  # noqa: BLE001
            termination = f"error: {type(e).__name__}: {str(e)[:200]}"
            res.status = "error"
            break
        n_evals += ev.n_evaluations or dcfg.screening_repeats
        usages.append(ev.usage)
        reason = _reject(ev, dcfg)
        cand = Candidate(candidate_id=f"c{it}", iteration=it, theta=theta, corruption=cfg, structural_noise_scale=scale,
                         world_hash=ev.world_hash, observation_id=ev.observation_id, realized_share=ev.realized_share,
                         estimate=ev.estimate, rejected=bool(reason), rejection_reason=reason,
                         objective=None if reason else _objective(ev.estimate, target, ev.realized_share, dcfg))
        history.append(cand)
        hist_pts.append((s, w, ev.estimate.mean_difficulty, noise, bool(reason) and ev.estimate.mean_difficulty is None))
        if reason:
            if ev.estimate.mean_difficulty is not None and ("budget" in reason or "observability" in reason):
                prop.s_max = min(prop.s_max, s * 0.6)
            say(f"  zamítnuto: {reason[:140]}", None)
            continue
        say(f"  obtížnost {ev.estimate.mean_difficulty:.1f} (cíl {target}), podíl poškození {ev.realized_share:.3f}", None)
        if abs(ev.estimate.mean_difficulty - target) <= tol and budget_left():
            fc = await verify(cand)
            if not fc.rejected and abs(fc.estimate.mean_difficulty - target) <= tol:
                termination = "tolerance_reached"
                break
            say("  finální ověření mimo toleranci – pokračuji v hledání", None)
    res.iterations = it
    # ------------------------------------------------------------------ 11. final evaluation of the best others
    valid = sorted([c for c in history if not c.rejected and c.phase == "screening"], key=lambda c: c.objective)
    if termination not in ("tolerance_reached", "cancelled") and res.status != "error":
        for c in [c for c in valid if c.candidate_id not in finalized][:max(0, dcfg.final_candidates - len(finals))]:
            if not budget_left() or (should_stop and should_stop()):
                break
            await verify(c)
    pool = [c for c in finals if not c.rejected] or valid
    res.history = history
    res.n_evaluations = n_evals
    res.usage = sum_usage(usages)
    res.termination_reason = termination
    if not pool:
        res.status = "cancelled" if termination == "cancelled" else ("error" if res.status == "error" else "target_not_reached")
        res.reason = "no valid candidate" + (f" ({termination})" if termination else "")
        return res
    best = min(pool, key=lambda c: c.objective)
    res.selected = best
    res.selected_corruption = best.corruption
    res.achieved_difficulty = best.estimate.mean_difficulty
    res.achieved_std = best.estimate.std_difficulty
    res.difficulty_delta = round(best.estimate.mean_difficulty - base_d, 3)
    res.realized_corruption = {"share": best.realized_share, "theta": best.theta}
    res.artifacts = {"observation_id": best.observation_id, "world_hash": best.world_hash}
    within = abs(best.estimate.mean_difficulty - target) <= tol
    if termination == "cancelled":
        res.status = "cancelled"
    elif within:
        res.status = "target_reached"
    elif termination == "budget_exhausted":
        res.status = "budget_exhausted"
    else:
        res.status = "target_not_reached"
    if best.phase != "final":
        res.reason = "výsledek je předběžný (bez finálních opakovaných běhů)"
    if not within and best.estimate.mean_difficulty < target:
        res.suggestions.append("Povolit další typy poškození nebo zvýšit maximální rozsah poškození.")
        if not dcfg.allow_structural_noise_optimization:
            res.suggestions.append("Povolit změny strukturálního šumu (vytvoří novou, auditovatelnou verzi reality).")
    return res
