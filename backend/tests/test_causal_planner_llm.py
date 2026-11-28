"""SCM planner against scripted OpenAI responses."""
import json

import pytest

from app.causal import planner
from app.causal.planner import SCMDraft, explain_failure
from app.contracts.io import ScenarioInput
from app.llm.client import LLMError
from tests.conftest import run
from tests.test_llm_client import _client, api_error, fast_sleep  # noqa: F401  (fixture re-export)

from .causal_fixtures import reference


def scm_obj() -> dict:
    return reference("education_tutoring").model_dump(mode="json", exclude={"edges", "difficulty"})


def broken_child() -> dict:
    d = scm_obj()
    child = next(e for e in d["entities"] if e["entity_id"] == "final_results")
    child.pop("count")  # the exact slip from the log: child entity without count / count_variable
    return d


def test_draft_auto_repairs_and_tolerates_trailing_characters():
    d = SCMDraft.model_validate({"scm": broken_child()})
    assert d.to_spec().entity("final_results").count == 1
    assert any("final_results" in r for r in d.auto_repairs)
    legacy = SCMDraft.model_validate({"scm_json": json.dumps(scm_obj()) + '\n"}  ```'})
    assert legacy.to_spec().meta.scenario_id == "education_tutoring"


def test_draft_rejects_unexecutable_model_with_concrete_errors():
    d = scm_obj()
    d["variables"][0]["entity"] = "ghost"
    with pytest.raises(ValueError, match="unknown entity ghost"):
        SCMDraft.model_validate({"scm": d})


def test_large_draft_rejects_small_model_and_accepts_scaled_reference():
    repaired = planner.LargeSCMDraft.model_validate({"scm": scm_obj()})
    assert repaired.auto_repairs
    planner.require_large_plan(repaired.scm)
    for story in ("retail", "school"):
        plan = planner.mock_plan_scm("", {"input": {"story": story, "volume": "large"}})
        planner.LargeSCMDraft.model_validate(plan)


def test_draft_repairs_lookup_without_removing_finer_grain_aggregation():
    scm = scm_obj()
    weekly_score = next(var for var in scm["variables"] if var["variable_id"] == "weekly_score")
    parent = next(p for p in weekly_score["parents"] if p["variable"] == "prior_score")
    parent["aggregation"] = "mean"

    draft = SCMDraft.model_validate({"scm": scm})
    repaired = next(p for p in draft.scm.variable("weekly_score").parents if p.variable == "prior_score")
    assert repaired.aggregation is None and repaired.alias == "prior"
    assert draft.scm.variable("avg_weekly_score").parents[0].aggregation == "mean"
    assert any("weekly_score" in note and "prior_score" in note for note in draft.auto_repairs)


def test_planner_preserves_assignment_key_and_expression_on_column_collision():
    from app.causal.engine import generate_world
    from .causal_fixtures import linear_xy
    spec = linear_xy(10).model_dump()
    spec["entities"].append({"entity_id": "suppliers", "kind": "base", "count": 2})
    spec["entities"][0]["assignments"] = [{"entity": "suppliers", "column": "primary_supplier_id"}]
    key = "primary_supplier_id"
    spec["variables"].extend([
        {"variable_id": key, "entity": "units", "column": key, "mechanism": {
            "type": "exogenous", "params": {"distribution": {"family": "constant", "params": {"value": 7}}}}},
        {"variable_id": "double_value", "entity": "units", "column": "double_value",
         "parents": [{"variable": key}], "mechanism": {"type": "deterministic_expression",
                                                       "params": {"expression": "2 * primary_supplier_id"}}},
    ])
    draft = SCMDraft.model_validate({"scm": spec})
    table = generate_world(draft.to_spec()).tables["units"]
    assert set(table[key]) == {1, 2}
    assert set(table["primary_supplier_id_value"]) == {7}
    assert set(table["double_value"]) == {14}


def test_json_mode_and_repair_rounds_with_fake_openai(tmp_path):
    quota = api_error(429, "Rate limit")
    bad_json = '{"scm": {"meta": {"name": "x"}, "entities": [ {broken'
    invalid = json.dumps({"scm": {**scm_obj(), "variables": [{**scm_obj()["variables"][0], "entity": "ghost"}]}})
    good = json.dumps({"scm": broken_child(), "assumptions": ["a"]})
    c = _client(tmp_path, {"pro": [quota, quota, quota, quota], "flash": [bad_json, invalid, good]})
    draft = run(c.structured(role="strong", task="plan_scm", prompt="story", schema=SCMDraft, json_mode=True,
                             max_validation_retries=4))
    assert draft.to_spec().entity("final_results").count == 1
    calls = c._client.calls
    flash_calls = [x for x in calls if x[0] == "flash"]
    assert len(flash_calls) == 3
    assert all(cfg["text"]["format"]["type"] == "json_object" for _, _, cfg in flash_calls)       # JSON mode, no response schema
    assert "JSON schema" in flash_calls[0][1]                                       # schema is in the prompt
    assert "unknown entity ghost" in flash_calls[2][1]                              # concrete error fed back


def test_plan_story_reports_failures_readably(monkeypatch):
    log = ("plan_scm: all models failed – gpt-6-sol: 429 429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}; "
           "gpt-6-luna: invalid output: 1 validation error for SCMDraft Value error, SCM is not executable: "
           "entity:sales: child entity needs 'count' (rows per parent) or 'count_variable'")
    msg = explain_failure(log)
    assert "Vyčerpaná kvóta: gpt-6-sol." in msg and "gpt-6-luna" in msg and "count_variable" in msg

    class Failing:
        async def structured(self, **kw):
            raise LLMError(log)

    monkeypatch.setattr(planner, "get_llm", lambda: Failing())
    with pytest.raises(RuntimeError, match="OpenAI nevrátilo spustitelný kauzální model"):
        run(planner.plan_story(ScenarioInput(story="Příběh o třech nemocnicích a čekacích dobách.", engine="scm")))


def test_auto_repairs_are_reported_in_the_spec(monkeypatch):
    class Fake:
        async def structured(self, *, schema, **kw):
            return schema.model_validate({"scm": broken_child()})

    monkeypatch.setattr(planner, "get_llm", lambda: Fake())
    resp = run(planner.plan_story(ScenarioInput(story="Školy a doučování, pilot ve škole 2.", engine="scm", seed=9)))
    assert resp.spec.scm.meta.seed == 9
    assert any("automatická oprava" in w and "final_results" in w for w in resp.spec.scm.meta.warnings)


def test_story_planner_receives_both_uploaded_contexts_and_complexity(monkeypatch):
    sql = "CREATE TABLE blue_shop (id INTEGER PRIMARY KEY);"
    ontology = "@prefix shop: <https://example.test/shop#> . shop:Order shop:pairedWith shop:Store ."

    class Fake:
        async def structured(self, *, schema, system, prompt, **kwargs):
            assert sql in prompt and ontology in prompt
            assert '"tables": 9' in prompt and '"variables": 12' in prompt and '"rows": 1234' in prompt
            assert "do not establish causality" in system
            return schema.model_validate({"scm": scm_obj()})

    monkeypatch.setattr(planner, "get_llm", lambda: Fake())
    spec = run(planner.plan_story(ScenarioInput(
        story="Školy a doučování v několika třídách.", engine="scm", sql_schema=sql, ontology=ontology,
        complexity={"tables": 9, "variables": 12, "rows": 1234},
    ))).spec.scm
    assert len(spec.entities) == len(scm_obj()["entities"])


def test_story_planner_excludes_unsourced_claims_before_validating_them(monkeypatch):
    data = scm_obj()
    requested = dict(data["ground_truth"]["story_facts"][0], source_quote="Pilotní doučování zlepšuje výsledky.")
    extra = {"fact_id": "invented", "description": "Oprava cesty snížila návštěvnost.",
             "source": "access_restriction", "target": "footfall", "source_quote": "Oprava přístupové cesty"}
    data["ground_truth"]["story_facts"] = [requested, extra]

    class Fake:
        async def structured(self, *, schema, **kwargs):
            return schema.model_validate({"scm": data})

    monkeypatch.setattr(planner, "get_llm", lambda: Fake())
    story = "Pilotní doučování zlepšuje výsledky. Přidej matoucí událost."
    response = run(planner.plan_story(ScenarioInput(story=story, engine="scm")))
    assert [fact.fact_id for fact in response.spec.scm.ground_truth.story_facts] == [requested["fact_id"]]
