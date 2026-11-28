"""LLM detective calls are recorded per evaluation (tokens, cost, raw responses) and can be replayed
without calling the API; the optimiser enforces the token budget (audit A13)."""
import asyncio
import json

import pytest

from app.config import Settings
from app.contracts.io import RunRequest
from app.causal import service
from app.causal.contracts import CorruptionConfig, DetectiveConfig, DifficultyConfig, EvaluationConfig
from app.causal.detective import llm_agent
from app.jobs import Job
from app.llm.client import LLMClient
from tests.test_llm_client import FakeSDK, fast_sleep  # noqa: F401

TOOL = json.dumps({"thought": "count schools", "action": "tool", "tool": "execute_sql",
                   "args_json": json.dumps({"query": "SELECT COUNT(*) AS n FROM schools"})})
FINAL = json.dumps({"thought": "done", "action": "final", "final": {
    "story_summary": "Docházka zvyšuje průběžné skóre.",
    "inferred_relationships": [{"source": "weekly_progress.attendance_rate", "target": "weekly_progress.weekly_score",
                                "edge_type": "directed", "estimated_direction": "+", "confidence": 0.8,
                                "evidence_ids": ["E1"]}]}})


def fake_client(tmp_path, n_runs: int = 1) -> LLMClient:
    s = Settings(openai_api_key="k", strong_model="gpt-6-luna", strong_fallbacks=[], cheap_model="x",
                 cheap_fallbacks=[], llm_mock=False, cache_dir=tmp_path)
    c = LLMClient(s)
    c._client = FakeSDK({"gpt-6-luna": [TOOL, FINAL] * n_runs})
    return c


@pytest.fixture
def run_id():
    spec = service.scenario_spec(service.load_reference_scenario("education_tutoring"))
    return asyncio.run(service.execute_causal_run(RunRequest(spec=spec), Job(id="t", kind="run")))["run_id"]


def test_llm_detective_calls_are_recorded_accounted_and_replayable(tmp_path, monkeypatch, run_id):
    client = fake_client(tmp_path, n_runs=2)
    monkeypatch.setattr(llm_agent, "get_llm", lambda: client)
    world, manifest = service.load_world(run_id)
    ev = asyncio.run(service.evaluate_configuration(run_id, world, manifest, CorruptionConfig(),
                                                    DetectiveConfig(kind="llm", max_steps=4), EvaluationConfig(), repeats=2))
    # accounting: 2 repeats x 2 calls; tokens from the fake usage metadata (100 + 20 + 5 per call)
    assert ev.usage["calls"] == 4 and ev.usage["tokens"] == 4 * 125 and ev.usage["cost_usd"] > 0
    assert ev.estimate.usage == ev.usage and ev.estimate.agent["mock"] is False
    eid = ev.estimate.evaluation_ids[0]
    d = service.store.run_path(run_id) / "evaluations"
    doc = json.loads((d / f"{eid}.json").read_text(encoding="utf-8"))
    calls = [json.loads(x) for x in (d / f"{eid}.llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c["task"] for c in calls] == ["causal_detective", "causal_detective"]
    assert calls[0]["model"] == "gpt-6-luna" and calls[0]["response"]["tool"] == "execute_sql"
    assert doc["usage"]["tokens"] == 250 and doc["replay"]["llm_calls"] == 2
    # replay: no SDK call is made (the fake script is exhausted), tools re-run on the stored observation
    n_sdk = len(client._client.calls)
    rep = asyncio.run(service.replay_evaluation(run_id, eid))
    assert rep["identical"] and rep["llm_calls_replayed"] == 2 and rep["difficulty_replayed"] == rep["difficulty_original"]
    assert len(client._client.calls) == n_sdk
    summary = service.causal_summary(run_id)
    assert summary["llm_usage_total"]["calls"] == 4
    assert all(e["usage"]["tokens"] == 250 for e in summary["evaluations"])


def test_statistical_evaluations_replay_and_cost_nothing(run_id):
    world, manifest = service.load_world(run_id)
    ev = asyncio.run(service.evaluate_configuration(run_id, world, manifest, CorruptionConfig(), DetectiveConfig(),
                                                    EvaluationConfig()))
    assert ev.usage["calls"] == 0 and ev.usage["tokens"] == 0
    rep = asyncio.run(service.replay_evaluation(run_id, ev.estimate.evaluation_ids[0]))
    assert rep["identical"] and rep["llm_calls_recorded"] == 0


def test_interactive_detective_checks_evidence_before_accepting_a_conclusion(run_id, monkeypatch, tmp_path):
    client = fake_client(tmp_path)
    incomplete = json.loads(FINAL)
    qualified = json.loads(FINAL)
    qualified["final"]["limitations"] = ["The observational comparison cannot identify the effect."]
    client._client = FakeSDK({"gpt-6-luna": [FINAL, TOOL, json.dumps(qualified)]})
    monkeypatch.setattr(llm_agent, "get_llm", lambda: client)
    world, _ = service.load_world(run_id)
    selected = service.observe_and_store(run_id, world, CorruptionConfig(seed=1234))
    stored = service.store.load_run(run_id)
    stored["observation"] = {"observation_id": selected.observation_id}
    service.store.save_json(run_id, "run.json", stored)
    report = asyncio.run(service.legacy_detective_view(run_id, kind="llm"))
    assert report["observation_id"] == selected.observation_id
    prompts = [prompt for _, prompt, _ in client._client.calls]
    assert len(prompts) == 3
    assert "Preliminary statistical leads" in prompts[0] and "evidence_ids" in prompts[0]
    assert "Your draft claims relationships" in prompts[1]
    assert report["final"]["root_cause"] == incomplete["final"]["story_summary"]
    assert report["score"]["queries"] > 1
    replay = asyncio.run(service.replay_evaluation(run_id, report["score"]["evaluation_id"]))
    assert replay["identical"]


def test_manual_mode_reports_total_usage(tmp_path, monkeypatch, run_id):
    monkeypatch.setattr(llm_agent, "get_llm", lambda: fake_client(tmp_path, n_runs=2))
    out = asyncio.run(service.manual_mode(run_id, CorruptionConfig(), DetectiveConfig(kind="llm", max_steps=4),
                                          EvaluationConfig()))
    assert out["usage"]["calls"] == 4 and out["baseline"]["usage"]["calls"] == 2 and out["observed"]["usage"]["calls"] == 2


def test_token_budget_stops_the_optimisation():
    from app.causal.difficulty import optimize
    from tests.test_causal_optimization import SPEC, MockEvaluator

    class Costly(MockEvaluator):
        async def __call__(self, cfg, scale, repeats, phase, variant):
            ev = await super().__call__(cfg, scale, repeats, phase, variant)
            ev.usage = {"calls": repeats, "tokens": 1000 * repeats, "cost_usd": 0.01 * repeats, "by_model": []}
            return ev

    r = asyncio.run(optimize(SPEC, DifficultyConfig(target_difficulty=95, max_iterations=50, token_budget=3500,
                                                    maximum_corruption_budget=1.0), Costly(lambda l: 30 + 10 * l)))
    assert r.termination_reason == "budget_exhausted"
    assert 3500 <= r.usage["tokens"] < 3500 + 3 * 1000 + 1  # stops before starting a new candidate
    assert r.usage["calls"] == r.n_evaluations
