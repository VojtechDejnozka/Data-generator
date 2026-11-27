"""Evaluation metrics: graph recovery with identifiability, uncertainty, story, effects, redistribution."""
import pytest

from app.causal.contracts import EvaluationConfig, SCMSpec
from app.causal.engine import generate_world
from app.causal.evaluation import aggregate, evaluate
from app.causal.graph import truth_graph
from app.causal.ground_truth import build_manifest
from app.causal.results import DetectiveResult, EvidenceItem, InferredRelationship

from .causal_fixtures import normal


def collider_spec() -> SCMSpec:
    """x -> z <- y (v-structure: compelled), z -> w (compelled by Meek R1), a -> b (Markov-equivalent)."""
    lin = lambda coefs: {"type": "linear", "params": {"coefficients": coefs, "noise": normal(1.0)}}
    exo = {"type": "exogenous", "params": {"distribution": normal(1.0)}}
    return SCMSpec.model_validate({
        "meta": {"seed": 11},
        "entities": [{"entity_id": "rows", "kind": "base", "count": 1500, "primary_key": "row_id"}],
        "variables": [
            {"variable_id": "x", "entity": "rows", "column": "x", "mechanism": exo},
            {"variable_id": "y", "entity": "rows", "column": "y", "mechanism": exo},
            {"variable_id": "z", "entity": "rows", "column": "z", "parents": [{"variable": "x"}, {"variable": "y"}],
             "mechanism": lin({"x": 1.0, "y": 1.0})},
            {"variable_id": "w", "entity": "rows", "column": "w", "parents": [{"variable": "z"}], "mechanism": lin({"z": 2.0})},
            {"variable_id": "a", "entity": "rows", "column": "a", "mechanism": exo},
            {"variable_id": "b", "entity": "rows", "column": "b", "parents": [{"variable": "a"}], "mechanism": lin({"a": 1.5})},
        ],
        "ground_truth": {"story_facts": [
            {"fact_id": "f_zw", "description": "z zvyšuje w", "kind": "edge", "source": "z", "target": "w", "expected_direction": "+", "importance": 2},
            {"fact_id": "f_ab", "description": "a zvyšuje b", "kind": "edge", "source": "a", "target": "b", "expected_direction": "+"}],
            "effects_to_estimate": [["z", "w"]]},
    })


@pytest.fixture(scope="module")
def setup():
    spec = collider_spec()
    w = generate_world(spec)
    return spec, build_manifest(w)


def rel(s, t, typ="directed", conf=0.9, d="+", ev=("E1",)):
    return InferredRelationship(source=f"rows.{s}", target=f"rows.{t}", edge_type=typ, estimated_direction=d,
                                confidence=conf, evidence_ids=list(ev))


def result(rels, effects=()):
    log = [EvidenceItem(evidence_id="E1", tool="discover_causal_graph", args={"columns": [f"rows.{c}" for c in "xyzwab"]},
                        ok=True, result={"edges": []})]
    return DetectiveResult(inferred_relationships=rels, estimated_effects=list(effects), statistical_results=log)


def test_identifiability_of_the_truth_graph():
    tg = truth_graph(collider_spec())
    o = {tuple(sorted(k)): v for k, v in tg.orientation.items()}
    assert o[("x", "z")] == o[("y", "z")] == "compelled"
    assert o[("w", "z")] == "compelled"          # Meek rule R1
    assert o[("a", "b")] == "undirected"         # Markov equivalent


def test_perfect_reconstruction(setup):
    spec, man = setup
    from app.causal.results import EstimatedEffect
    eff = [EstimatedEffect(source="rows.z", target="rows.w", effect_type="direct", estimate=2.0, evidence_ids=["E1"]),
           EstimatedEffect(source="rows.z", target="rows.w", effect_type="total", estimate=2.0, evidence_ids=["E1"]),
           EstimatedEffect(source="rows.a", target="rows.b", effect_type="direct", estimate=1.5, evidence_ids=["E1"])]
    ev = evaluate(spec, man, result([rel("x", "z"), rel("y", "z"), rel("z", "w"), rel("a", "b", "undirected")], eff))
    assert ev.metrics["skeleton"].score == pytest.approx(1.0)
    assert ev.metrics["orientation"].score == pytest.approx(1.0)
    assert ev.metrics["effects"].score > 0.95
    assert ev.metrics["story"].score > 0.9
    assert ev.graph_metrics["shd"] == 0
    assert ev.difficulty < 10


def test_incomplete_extra_missing_and_wrong_orientation(setup):
    spec, man = setup
    full = evaluate(spec, man, result([rel("x", "z"), rel("y", "z"), rel("z", "w"), rel("a", "b", "undirected")]))
    missing = evaluate(spec, man, result([rel("x", "z"), rel("z", "w")]))
    assert missing.graph_metrics["skeleton"]["recall"] == pytest.approx(0.5)
    extra = evaluate(spec, man, result([rel("x", "z"), rel("y", "z"), rel("z", "w"), rel("a", "b", "undirected"),
                                        rel("x", "a", conf=0.9), rel("y", "b", conf=0.9), rel("w", "a", conf=0.9)]))
    assert extra.graph_metrics["skeleton"]["precision"] < 0.6
    assert extra.difficulty > full.difficulty           # adding edges is not rewarded
    wrong = evaluate(spec, man, result([rel("z", "x"), rel("z", "y"), rel("w", "z"), rel("a", "b", "undirected")]))
    assert wrong.metrics["orientation"].score == 0.0
    assert wrong.graph_metrics["shd"] == 3
    assert wrong.difficulty > full.difficulty


def test_markov_equivalent_orientation_is_not_penalised(setup):
    spec, man = setup
    base = [rel("x", "z"), rel("y", "z"), rel("z", "w")]
    a = evaluate(spec, man, result(base + [rel("a", "b")]))
    b = evaluate(spec, man, result(base + [rel("b", "a")]))
    assert a.metrics["orientation"].score == b.metrics["orientation"].score == 1.0


def test_admitted_uncertainty_beats_wrong_confidence(setup):
    spec, man = setup
    honest = evaluate(spec, man, result([rel("x", "z", "undirected", 0.6), rel("y", "z", "undirected", 0.6),
                                         rel("z", "w", "undirected", 0.6)]))
    wrong = evaluate(spec, man, result([rel("z", "x"), rel("z", "y"), rel("w", "z")]))
    assert honest.metrics["orientation"].score == 0.5 > wrong.metrics["orientation"].score
    assert honest.recovery_score > wrong.recovery_score
    # overconfident false edges hurt calibration
    over = evaluate(spec, man, result([rel("x", "z"), rel("x", "w", conf=0.99), rel("a", "z", conf=0.99)]))
    assert over.evidence["calibration"] < honest.evidence["calibration"]


def test_not_evaluable_metrics_are_redistributed(setup):
    spec, man = setup
    ev = evaluate(spec, man, result([rel("x", "z")]))  # no effects claimed -> effects score 0, still evaluable
    assert abs(sum(ev.weights_used.values()) - 1) < 1e-9
    spec2 = spec.model_copy(update={"ground_truth": spec.ground_truth.model_copy(update={
        "story_facts": [], "effects_to_estimate": []})})
    man2 = {**man, "realized_story_facts": [], "true_effects": []}
    ev2 = evaluate(spec2, man2, result([rel("x", "z")]))
    assert ev2.metrics["story"].status == "not_evaluable" and ev2.metrics["effects"].status == "not_evaluable"
    assert set(ev2.weights_used) == {"skeleton", "orientation", "evidence"}
    w = EvaluationConfig().weights
    assert ev2.weights_used["skeleton"] == pytest.approx(w["skeleton"] / (w["skeleton"] + w["orientation"] + w["evidence"]), abs=1e-3)
    empty = evaluate(spec2, man2, DetectiveResult())
    assert empty.status == "ok" and empty.metrics["evidence"].status == "not_evaluable"
    agg = aggregate([empty])
    assert agg.n_evaluations == 1 and agg.preliminary


def test_invented_evidence_ids_do_not_count(setup):
    spec, man = setup
    real = evaluate(spec, man, result([rel("x", "z", ev=("E1",))]))
    fake = evaluate(spec, man, result([rel("x", "z", ev=("E99",))]))
    assert fake.evidence["mean_support"] < real.evidence["mean_support"]
    assert fake.evidence["invalid_evidence_ids"] == 1
