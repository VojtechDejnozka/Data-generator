"""SCM editing contract, serialisation, ground-truth realisation, planner hints."""
import numpy as np

from app.causal.contracts import SCMSpec, derive_edges
from app.causal.editing import EditOp, apply_edits, classify_change
from app.causal.engine import generate_world
from app.causal.ground_truth import build_manifest

from .causal_fixtures import linear_xy, reference


def test_json_and_yaml_roundtrip():
    spec = reference("retail_supplier_disruption")
    assert SCMSpec.model_validate_json(spec.model_dump_json()) == spec
    assert SCMSpec.from_yaml(spec.to_yaml()) == spec


def test_edges_are_derived_from_target_mechanisms():
    edges = {(e.source, e.target): e for e in derive_edges(linear_xy(10))}
    assert edges[("x", "y")].optional_coefficient == 5.0 and edges[("x", "y")].effect_direction == "+"
    assert ("y", "w") in edges and edges[("y", "w")].mechanism_reference == "w:deterministic_expression"


def test_add_and_remove_edge_keep_parents_and_coefficients_consistent():
    spec = linear_xy(10)
    r = apply_edits(spec, [EditOp(op="add_edge", source="z", target="y", coefficient=-2.0)])
    y = r.scm.variable("y")
    assert [p.variable for p in y.parents] == ["x", "z"] and y.mechanism.params["coefficients"]["z"] == -2.0
    assert r.report.status == "PASS" and r.change["kind"] == "scm" and r.change["regenerate_world"]
    assert set(r.change["affected_variables"]) == {"y", "w"}
    r2 = apply_edits(r.scm, [EditOp(op="remove_edge", source="z", target="y")])
    assert "z" not in r2.scm.variable("y").mechanism.params["coefficients"]
    # removing a parent that an expression still uses gives a concrete validation error
    r3 = apply_edits(spec, [EditOp(op="remove_edge", source="y", target="w")])
    assert r3.report.status == "FAIL" and any("unknown name" in c.explanation for c in r3.report.errors)
    # an edge into an expression mechanism that the expression ignores is flagged
    r4 = apply_edits(spec, [EditOp(op="add_edge", source="z", target="w")])
    assert any("not used by the mechanism" in w or "must reference" in w for w in r4.warnings)


def test_rename_keeps_stable_ids_and_noise_change_is_classified():
    spec = linear_xy(10)
    r = apply_edits(spec, [EditOp(op="rename_variable", target="y", value={"name": "Výstup"}),
                           EditOp(op="update_variable", target="y", value={"variable_id": "other"})])
    assert r.scm.variable("y").name == "Výstup" and r.errors and "stable identifier" in r.errors[0]
    assert r.change["kind"] == "presentation" and not r.change["regenerate_world"]
    n = apply_edits(spec, [EditOp(op="set_parameter", target="y", path="noise.params.sd", value=4.0)])
    assert n.change["kind"] == "structural_noise" and "w" in n.change["affected_variables"]
    d = spec.model_copy(update={"difficulty": spec.difficulty.model_copy(update={"target_difficulty": 80})})
    assert classify_change(spec, d)["kind"] == "difficulty"


def test_remove_variable_requires_cascade():
    spec = linear_xy(10)
    r = apply_edits(spec, [EditOp(op="remove_variable", target="x")])
    assert r.errors and "referenced" in r.errors[0]
    r2 = apply_edits(spec, [EditOp(op="remove_variable", target="w")])
    assert not r2.errors and r2.scm.variable("w") is None


def test_intended_vs_realized_ground_truth():
    spec = reference("education_tutoring")
    m = build_manifest(generate_world(spec))
    facts = {f["fact_id"]: f for f in m["realized_story_facts"]}
    tut = facts["edge_tutoring_score"]
    # causal effect exists (interventional, CRN) although the raw association is negative (selection on SES)
    assert tut["realized"] and tut["evidence"]["interventional"]["estimate"] > 0
    assert tut["evidence"]["association"]["corr"] < 0 and tut["observational_sign_agrees"] is False
    assert facts["pilot_effect"]["realized"] and "weekly_score" in facts["pilot_effect"]["visible_in"]
    assert "ses" in m["latent_variables"]
    assert any(k for k, v in m["identifiability"].items() if "NOT identifiable" in v or "latent" in v)
    eff = {(e["source"], e["target"], e["effect_type"]): e["estimate"] for e in m["true_effects"]}
    assert eff[("attendance_rate", "weekly_score", "direct")] == 15.0
    assert eff[("attendance_rate", "weekly_score", "total")] > 15.0  # + path through homework


def test_unrealized_fact_is_marked():
    d = linear_xy(500, coef=0.0).model_dump()
    d["ground_truth"] = {"story_facts": [{"fact_id": "f", "description": "x raises y", "kind": "edge", "source": "x",
                                          "target": "y", "expected_direction": "+"}]}
    m = build_manifest(generate_world(SCMSpec.model_validate(d)))
    assert m["realized_story_facts"][0]["realized"] is False


def test_story_fact_needs_a_causal_path_but_it_can_be_indirect():
    from app.causal.validation import validate_spec

    spec = linear_xy(10)
    data = spec.model_dump()
    data["ground_truth"]["story_facts"] = [
        {"fact_id": "indirect", "description": "x affects w via y", "source": "x", "target": "w"},
        {"fact_id": "unconnected", "description": "z affects w", "source": "z", "target": "w"},
    ]
    errors = validate_spec(SCMSpec.model_validate(data)).errors
    assert [(e.check_id, e.target) for e in errors] == [("fact.path", "fact:unconnected")]


def test_planner_schema_hints_from_ddl_and_ontology():
    from app.causal.planner import schema_hints
    h = schema_hints("CREATE TABLE a (id INTEGER PRIMARY KEY, v REAL); CREATE TABLE b (id INTEGER, a_id INTEGER, "
                     "FOREIGN KEY (a_id) REFERENCES a(id));")
    assert h["kind"] == "sql_ddl" and {t["table"] for t in h["tables"]} == {"a", "b"}
    assert h["tables"][1]["foreign_keys"][0]["columns"] == ["a_id"]
    o = schema_hints("@prefix ex: <http://x#> . ex:A a owl:Class .")
    assert o["kind"] == "ontology"


def test_parameter_event_entity_and_time_edits():
    spec = reference("retail_supplier_disruption")
    r = apply_edits(spec, [
        EditOp(op="set_parameter", target="delay_days", path="predictor", value="log(base_delay) + 3.0 * disruption"),
        EditOp(op="update_event", target="supplier_disruption", value={"ramp_periods": 10, "start_time": "2025-02-01"}),
        EditOp(op="update_entity", target="stores", value={"count": 6}),
        EditOp(op="update_time", value={"periods": 90})])
    assert not r.errors and r.report.status == "PASS_WITH_WARNINGS" and r.change["kind"] == "scm"
    # the shortened period no longer contains the distractor event (starts 2025-04-01) -> warning, not failure
    assert any(c.target == "event:competitor_opening" for c in r.report.warnings)
    assert r.scm.time.periods == 90 and r.scm.entity("stores").count == 6
    assert r.scm.event("supplier_disruption").ramp_periods == 10
    bad = apply_edits(spec, [EditOp(op="set_parameter", target="unit_price", path="distribution.params.sd", value=-5)])
    assert bad.report.status == "FAIL"
