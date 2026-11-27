"""End-to-end in mock mode: Input → ScenarioSpec → approved spec → SCM generation → clean dataset →
validation → corruption → detective → evaluation → difficulty optimisation → structured results."""
import inspect
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _wait(c, job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/jobs/{job_id}").json()
        if s["status"] in ("done", "error"):
            assert s["status"] == "done", s["error"]
            return s["result"]
        time.sleep(0.2)
    raise TimeoutError


@pytest.mark.parametrize("story,domain", [
    ("Ve čtyřech školách běží pilotní doučování; doučovaní žáci se zlepšují, ale hlásí se spíš slabší žáci.", "education"),
    ("Tržby sítě prodejen klesají, přestože poptávka je stabilní; jeden dodavatel dodává stále později.", "retail"),
])
def test_scm_pipeline_end_to_end_via_api(story, domain):
    with TestClient(app) as c:
        assert len(c.get("/api/scm/mechanisms").json()) >= 16
        assert len(c.get("/api/scm/corruptions").json()) == 19
        # 1) natural language -> planner (mock) -> editable ScenarioSpec with an executable SCM
        sid = _wait(c, c.post("/api/plan", json={"story": story, "volume": "small", "seed": 7, "engine": "scm"})
                    .json()["job_id"])["session_id"]
        spec = c.get(f"/api/plan/{sid}").json()["response"]["spec"]
        assert spec["scm"]["meta"]["domain"] == domain
        assert any("MOCK" in w for w in spec["scm"]["meta"]["warnings"])  # mock planner is labelled
        v = c.post("/api/scm/validate", json=spec["scm"]).json()
        assert v["report"]["status"] == "PASS" and v["execution_plan"]["loop"]
        # 2) user edit through the contract (stable ids, validation, invalidation info)
        target = next(x for x in spec["scm"]["variables"] if x["column"] and x["observability"] == "observed")
        e = c.post("/api/scm/edit", json={"scm": spec["scm"], "operations": [
            {"op": "rename_variable", "target": target["variable_id"], "value": {"name": "Renamed"}}]}).json()
        assert e["report"]["status"] == "PASS" and e["change"]["kind"] == "presentation"
        spec = e["spec"]
        # 3) approval -> clean world + validation + ground truth (existing run endpoints keep working)
        rid = _wait(c, c.post("/api/runs", json={"spec": spec}).json()["job_id"])["run_id"]
        run = c.get(f"/api/runs/{rid}").json()
        assert run["engine"] == "scm" and run["causal"]["status"] in ("PASS", "PASS_WITH_WARNINGS")
        assert run["verdict"]["verdict"] in ("pass", "warn")
        assert c.get(f"/api/runs/{rid}/report").status_code == 200
        assert c.get(f"/api/runs/{rid}/schema").json()["tables"]
        gt = c.get(f"/api/runs/{rid}/ground-truth").json()
        assert gt["kind"] == "scm_ground_truth" and gt["realized_story_facts"]
        summ = c.get(f"/api/runs/{rid}/causal").json()
        assert summ["edges"] and summ["validation"]["world"]["status"] != "FAIL"
        # 4) manual mode: one corruption configuration, measured difficulty
        table = next(t["name"] for t in spec["tables"] if "fact" in t["flags"])
        man = _wait(c, c.post(f"/api/runs/{rid}/observe", json={
            "corruption": {"operations": [{"type": "missing_values_mcar", "target_table": table, "probability": 0.2}]},
            "repeats": 1}).json()["job_id"])
        assert man["mode"] == "manual" and man["observed"]["status"] == "ok"
        assert man["baseline"]["mean_difficulty"] is not None and man["observed"]["preliminary"] is True
        obs = c.get(f"/api/runs/{rid}/observations/{man['observation_id']}").json()
        assert obs["info"]["realized"]["realized_share"] > 0
        assert c.get(f"/api/runs/{rid}/observations/{man['observation_id']}/db").content[:15] == b"SQLite format 3"
        # 5) automatic mode (default): iterative search, independent final evaluation
        auto = _wait(c, c.post(f"/api/runs/{rid}/difficulty", json={
            "target_difficulty": man["baseline"]["mean_difficulty"] + 6, "difficulty_tolerance": 5, "max_iterations": 4,
            "evaluation_repeats": 2, "final_candidates": 1}).json()["job_id"])
        assert auto["mode"] == "automatic" and auto["status"] in ("target_reached", "target_not_reached",
                                                                   "budget_exhausted")
        assert auto["baseline"]["mean_difficulty"] is not None and auto["history"]
        assert auto["agent"]["kind"] == "statistical"
        stored = c.get(f"/api/runs/{rid}/difficulty/{auto['difficulty_id']}").json()
        assert stored["status"] == auto["status"]
        ev = summ_after = c.get(f"/api/runs/{rid}/causal").json()
        assert len(summ_after["evaluations"]) >= 3 and len(summ_after["difficulty_runs"]) == 2


def test_engine_has_no_domain_specific_code():
    from app.causal import corruption, difficulty, engine, evaluation, mechanisms, validation
    src = " ".join(inspect.getsource(m).lower() for m in (engine, mechanisms, validation, corruption, evaluation, difficulty))
    import re
    for word in ("retail", "supplier", "store_id", "student", "school", "tutoring", "inventory", "complaint"):
        assert not re.search(rf"\b{word}s?\b", src), word  # 'student_t' (distribution) is fine


def test_change_of_corruption_reuses_clean_world_and_keeps_its_hash():
    import asyncio
    from app.contracts.io import RunRequest
    from app.causal import service
    from app.causal.contracts import CorruptionConfig, CorruptionOp
    from app.jobs import Job

    spec = service.scenario_spec(service.load_reference_scenario("education_tutoring"))
    rid = asyncio.run(service.execute_causal_run(RunRequest(spec=spec), Job(id="t", kind="run")))["run_id"]
    w1, _ = service.load_world(rid)
    a = service.observe_and_store(rid, w1, CorruptionConfig())
    b = service.observe_and_store(rid, w1, CorruptionConfig(operations=[
        CorruptionOp(type="missing_values_mcar", target_table="weekly_progress", probability=0.3)]))
    assert a.world_hash == b.world_hash == w1.world_hash and a.observed_hash != b.observed_hash
    service._WORLDS.clear()
    w2, _ = service.load_world(rid)  # regenerated from the approved spec and verified against the stored hash
    assert w2.world_hash == w1.world_hash


@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY") or os.getenv("RUN_REAL_LLM_TESTS") != "1",
                    reason="real LLM integration test NOT executed (needs GEMINI_API_KEY and RUN_REAL_LLM_TESTS=1)")
def test_real_llm_planner_and_detective(monkeypatch):  # pragma: no cover - needs network and a key
    import asyncio
    monkeypatch.setenv("LLM_MOCK", "0")
    from app import config
    from app.llm import client
    config.get_settings.cache_clear()
    client.reset_llm()
    from app.causal.planner import plan_story
    from app.contracts.io import ScenarioInput
    resp = asyncio.run(plan_story(ScenarioInput(story="Ve třech nemocnicích se po zavedení nového triážního systému "
                                                      "zkrátila čekací doba, ale jen na urgentních odděleních.",
                                                volume="small", engine="scm")))
    assert resp.spec.scm is not None


def test_existing_ui_endpoints_work_for_scm_runs():
    with TestClient(app) as c:
        spec = c.get("/api/scm/scenarios/retail_supplier_disruption").json()
        rid = _wait(c, c.post("/api/runs", json={"spec": spec}).json()["job_id"])["run_id"]
        t = c.get(f"/api/runs/{rid}/tables/inventory_snapshots", params={"page_size": 5}).json()
        assert len(t["rows"]) == 5 and t["fks"]["product_id"] == "products.product_id"
        assert c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) FROM stores"}).json()["rows"] == [[4]]
        rep = c.get(f"/api/runs/{rid}/report").json()
        assert rep["steps"] and any(s["evidence"] for s in rep["steps"])
        assert c.get(f"/api/runs/{rid}/dashboard").json()["widgets"]
        assert c.get(f"/api/runs/{rid}/export.zip").content[:2] == b"PK"
        det = _wait(c, c.post(f"/api/runs/{rid}/detective").json()["job_id"])
        assert 0 <= det["score"]["score"] <= 100 and det["final"]["root_cause"]
        assert det["agent"]["mock"] is True  # mock mode -> statistical delegate, labelled
        assert c.get(f"/api/runs/{rid}/detective").json()["observation_id"].startswith("obs-")
