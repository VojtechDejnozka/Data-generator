"""Automatic difficulty optimisation with a deterministic mock evaluator (spec §21.6)."""
import asyncio
import math

import pytest

from app.causal.contracts import DifficultyConfig
from app.causal.difficulty import CandidateEval, build_search_space, config_from_theta, optimize
from app.causal.results import DifficultyEstimate

from .causal_fixtures import reference

SPEC = reference("education_tutoring")


def est(d, n=1, std=None):
    return DifficultyEstimate(mean_difficulty=d, std_difficulty=std, n_evaluations=n, scores=[d] * n,
                              mean_recovery=1 - d / 100, preliminary=n < 3, agent={"kind": "mock", "mock": True})


class MockEvaluator:
    """D(θ) = f(corruption level) – deterministic; level = mean requested intensity of all ops."""

    def __init__(self, f, share=lambda lvl: lvl * 0.3, base=30.0):
        self.f, self.share, self.base, self.calls = f, share, base, []

    async def __call__(self, cfg, scale, repeats, phase, variant):
        self.calls.append((phase, repeats))
        if phase == "baseline" or not cfg.operations:
            return CandidateEval(estimate=est(self.base, repeats), n_evaluations=repeats)
        lvl = sum(o.params.get("intensity", o.probability) for o in cfg.operations) / len(cfg.operations)
        return CandidateEval(estimate=est(self.f(lvl), repeats, 0.5 if repeats > 1 else None),
                             realized_share=self.share(lvl), n_evaluations=repeats)


def run(dcfg, ev, **kw):
    return asyncio.run(optimize(SPEC, dcfg, ev, run_id="t", **kw))


def test_reaches_target_within_tolerance_and_verifies_independently():
    ev = MockEvaluator(lambda l: 30 + 120 * l)
    r = run(DifficultyConfig(target_difficulty=60, difficulty_tolerance=3, max_iterations=10,
                             maximum_corruption_budget=1.0), ev)
    assert r.status == "target_reached"
    assert abs(r.achieved_difficulty - 60) <= 3
    assert r.selected.phase == "final" and r.selected.estimate.n_evaluations == 3
    assert ("final", 3) in ev.calls
    assert r.baseline.mean_difficulty == 30 and r.difficulty_delta == pytest.approx(r.achieved_difficulty - 30)
    assert len(r.history) >= 2  # really tries several candidates


def test_unreachable_target_returns_best_valid_with_reason():
    ev = MockEvaluator(lambda l: 30 + 10 * l)  # can never exceed ~40
    r = run(DifficultyConfig(target_difficulty=90, max_iterations=5, maximum_corruption_budget=1.0), ev)
    assert r.status == "target_not_reached"
    assert r.achieved_difficulty is not None and r.achieved_difficulty < 45
    assert r.selected is not None and not r.selected.rejected
    assert r.suggestions


def test_target_below_clean_baseline():
    r = run(DifficultyConfig(target_difficulty=10), MockEvaluator(lambda l: 50, base=50))
    assert r.status == "unreachable_below_baseline" and r.suggestions
    assert r.achieved_difficulty == 50


def test_non_monotone_objective_is_handled():
    f = lambda l: 30 + 60 * math.sin(math.pi * min(l, 1.0) * 1.5) ** 2  # rises, falls, rises
    r = run(DifficultyConfig(target_difficulty=70, difficulty_tolerance=4, max_iterations=14,
                             maximum_corruption_budget=1.0), MockEvaluator(f))
    assert r.achieved_difficulty is not None
    assert r.status in ("target_reached", "target_not_reached")
    assert min(abs(c.estimate.mean_difficulty - 70) for c in r.history if c.estimate) < 15


def test_cancellation_and_evaluation_budget():
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    r = run(DifficultyConfig(target_difficulty=95, max_iterations=20, maximum_corruption_budget=1.0),
            MockEvaluator(lambda l: 30 + 10 * l), should_stop=stop)
    assert r.status == "cancelled" and r.termination_reason == "cancelled"
    ev = MockEvaluator(lambda l: 30 + 10 * l)
    r2 = run(DifficultyConfig(target_difficulty=95, max_iterations=50, evaluation_budget=4,
                              maximum_corruption_budget=1.0), ev)
    assert r2.n_evaluations <= 4 + 3  # stops before starting a new candidate once the budget is used
    assert r2.termination_reason == "budget_exhausted"


def test_unusable_candidates_are_rejected_and_never_selected():
    # every candidate with level > 0.1 exceeds the corruption budget
    ev = MockEvaluator(lambda l: 30 + 200 * l, share=lambda l: 0.0 if l <= 0.1 else 0.9)
    r = run(DifficultyConfig(target_difficulty=80, max_iterations=8, maximum_corruption_budget=0.35), ev)
    assert any(c.rejected and "budget" in c.rejection_reason for c in r.history)
    assert r.selected is None or not r.selected.rejected
    if r.selected:
        assert r.selected.realized_share <= 0.35


def test_locked_parameters_are_kept_and_search_space_is_derived():
    dcfg = DifficultyConfig(locked_parameters={"missing_values_mcar": 0.2})
    space = build_search_space(SPEC, dcfg)
    assert {d.dim_id for d in space} == set(dcfg.allowed_corruption_types)
    assert next(d for d in space if d.dim_id == "missing_values_mcar").locked_value == 0.2
    ev = MockEvaluator(lambda l: 30 + 50 * l)
    r = run(dcfg.model_copy(update={"target_difficulty": 55, "maximum_corruption_budget": 1.0}), ev)
    assert all(c.theta["missing_values_mcar"] == 0.2 for c in r.history)
    cfg = config_from_theta(SPEC, space, {"missing_values_mcar": 0.2}, dcfg)
    assert {o.target_table for o in cfg.operations} and all(o.type == "missing_values_mcar" for o in cfg.operations)


def test_mock_results_are_labelled():
    r = run(DifficultyConfig(target_difficulty=50, maximum_corruption_budget=1.0), MockEvaluator(lambda l: 30 + 80 * l))
    assert r.baseline.agent["mock"] is True
