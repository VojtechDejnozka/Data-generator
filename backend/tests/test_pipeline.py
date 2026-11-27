"""Integration test of the whole pipeline (mock LLM) incl. planning, repair-free run, what-if and detective."""
from app.contracts.io import ReplanRequest, RunRequest, ScenarioInput
from app.detective import investigate
from app.jobs import Job
from app.pipeline import execute_run
from app.planner import default_plan, plan_scenario, replan
from app.presets.retail_demo import RETAIL_DEMO
from app.reporting.report import build_report, compare_runs
from app.storage import runs as store
from tests.conftest import run

SMALL = RETAIL_DEMO.model_copy(update={"entity_counts": {"stores": 4, "products": 60, "suppliers": 6, "customers": 200},
                                       "period_start": "2025-01-01", "period_end": "2025-12-31", "volume": "small"})


def test_full_pipeline_and_whatif():
    pr = run(plan_scenario(SMALL))
    assert pr.spec.meta.archetype == "retail_supplier_delay" and pr.questions and pr.suggestions
    pr2 = run(replan(ReplanRequest(spec=pr.spec, accepted_suggestions=["sg1"]), pr.suggestions, 1))
    assert pr2.spec.table("promotions") is not None

    job = Job(id="j1", kind="run")
    res = run(execute_run(RunRequest(spec=pr2.spec), job))
    rid = res["run_id"]
    meta = store.load_run(rid)
    assert meta["status"] == "done"
    assert meta["checks_summary"]["errors"] == 0, meta["checks_summary"]
    assert meta["checks_summary"]["fk_integrity_pct"] == 100.0
    assert meta["verdict"]["verdict"] == "pass"
    assert 32 <= meta["row_counts"]["promotions"] <= 40  # invalid rows are dropped
    assert len(meta["row_counts"]) >= 6
    assert store.db_path(rid).exists() and store.load_json(rid, "manifest.json")["story"]["supplier_id"] == 2
    phases = {e["phase"] for e in job.events}
    assert {"plan", "dimensions", "simulation", "checks", "validate", "dashboard", "done"} <= phases

    rep = build_report(rid)
    assert len(rep["steps"]) == 4 and all(s["evidence"] for s in rep["steps"])
    assert all(s["signal"]["detectable"] for s in rep["steps"])

    # what-if: baseline without story, same seed
    job2 = Job(id="j2", kind="whatif")
    rid2 = run(execute_run(RunRequest(spec=pr2.spec, knob_overrides={"story_intensity": "0"}, parent_run_id=rid,
                                      repair=False), job2, base_params=meta["params"]))["run_id"]
    cmp = compare_runs(rid, rid2)
    m = {x["key"]: x for x in cmp["metrics"]}
    assert m["visitors"]["diff"] == 0  # identical demand
    assert m["revenue_k_czk"]["b"] > m["revenue_k_czk"]["a"]  # no story -> more revenue
    assert m["story_supplier_delay"]["b"] < m["story_supplier_delay"]["a"]
    assert m["availability_complaints"]["b"] < m["availability_complaints"]["a"]
    assert "story_intensity" in cmp["changed_knobs"]

    # export works
    assert store.export_zip(rid)[:2] == b"PK"

    # detective finds the supplier blind
    det = run(investigate(rid))
    assert det["final"]["entity_id"] == 2 and det["score"]["score"] >= 80, det["score"]


def test_default_plan_covers_all_tables():
    pr = run(plan_scenario(SMALL))
    plan = default_plan(pr.spec)
    covered = {t for p in plan.work_packages for t in p.tables}
    assert covered == {t.name for t in pr.spec.tables}


def test_planner_falls_back_to_template_when_llm_fails(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client import LLMError

    async def boom(*a, **k):
        raise LLMError("quota")

    monkeypatch.setattr(llm_client.get_llm(), "structured", boom)
    pr = run(plan_scenario(SMALL))
    assert pr.spec.meta.archetype == "retail_supplier_delay" and "nedostupný" in pr.notes
    assert pr.spec.table("stores").row_count == 4


def test_minimal_input_planner_decides_rest():
    from app.contracts.io import ScenarioInput

    inp = ScenarioInput(story="Síti prodejen klesají tržby, protože mlékárna dodává pozdě.", volume="small", seed=7)
    pr = run(plan_scenario(inp))
    spec = pr.spec
    assert spec.meta.has_time and spec.meta.company  # decided by the (mock) planner
    assert spec.meta.seed == 7
    assert spec.table("stores").row_count == 5  # small size defaults
    ds = spec.knob("delay_start_date").default
    assert spec.meta.period_start < ds < spec.meta.period_end
    assert spec.story.causal_chain[0].start == ds


def test_timeless_llm_rows_scenario_runs():
    """A story without a time dimension: no period, no date columns, generic llm_rows path."""
    from app.contracts.spec import Column, Meta, ScenarioSpec, Story, CausalStep, Table, EvidenceQuery

    spec = ScenarioSpec(
        meta=Meta(name="Knihovna", domain="library", period_start=None, period_end=None, seed=3),
        tables=[
            Table(name="genres", strategy="llm_dimension", row_count=8, columns=[
                Column(name="genre_id", type="int", pk=True), Column(name="name", type="text")]),
            Table(name="books", strategy="llm_rows", row_count=40, columns=[
                Column(name="book_id", type="int", pk=True), Column(name="title", type="text"),
                Column(name="genre_id", type="int", fk="genres.genre_id"),
                Column(name="pages", type="int", min=50, max=1200),
                Column(name="format", type="text", enum_values=["vázaná", "brožovaná"])]),
        ],
        story=Story(logline="Detektivky jsou výrazně delší než ostatní žánry.",
                    causal_chain=[CausalStep(id="s1", mechanism="Detektivky mají více stran", tables=["books"])]),
        evidence=[EvidenceQuery(id="e1", title="Průměrná délka podle žánru", chart="bar", x="name", y=["avg_pages"],
                                sql="SELECT g.name, AVG(b.pages) AS avg_pages FROM books b "
                                    "JOIN genres g ON g.genre_id=b.genre_id GROUP BY 1")],
    )
    assert not spec.meta.has_time and spec.structural_errors() == []
    job = Job(id="t", kind="run")
    rid = run(execute_run(RunRequest(spec=spec), job))["run_id"]
    meta = store.load_run(rid)
    assert meta["status"] == "done" and meta["checks_summary"]["errors"] == 0, meta["checks_summary"]
    assert meta["row_counts"]["books"] >= 32 and meta["row_counts"]["genres"] == 8
    ev = store.load_json(rid, "evidence.json")
    assert ev[0]["ok"] and ev[0]["rows"]


def test_period_must_be_both_or_neither():
    import pytest
    from pydantic import ValidationError

    from app.contracts.io import ScenarioInput
    from app.generation.scenarios.registry import RETAIL

    data = RETAIL.default_spec(ScenarioInput(story="Testovací příběh")).model_dump()
    data["meta"]["period_end"] = None
    with pytest.raises(ValidationError):
        from app.contracts.spec import ScenarioSpec
        ScenarioSpec.model_validate(data)
