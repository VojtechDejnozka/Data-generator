"""SCM engine: graph, equations, reproducibility, RNG streams, CPTs, lags, interventions, noise."""
import numpy as np
import pytest
from scipy import stats

from app.causal.contracts import EventSpec, SCMSpec
from app.causal.engine import GenerationError, generate_world
from app.causal.expressions import ExpressionError, check_expr, evaluate
from app.causal.graph import GraphError, execution_plan
from app.causal.validation import validate_spec

from .causal_fixtures import linear_xy, normal, reference, temporal


# --------------------------------------------------------------------------- expressions
def test_expression_interpreter_allows_math_and_rejects_code():
    env = {"a": np.array([1.0, 4.0]), "b": np.array([2.0, 0.5]), "c": np.array(["x", "y"], dtype=object)}
    assert np.allclose(evaluate("min(a, b) + sqrt(a) * 2 - clip(b, 1, 1.5)", env, 2), [1 + 2 - 1.5, 0.5 + 4 - 1])
    assert list(evaluate("1 if c == 'x' else 0", env, 2)) == [1, 0]
    assert np.allclose(evaluate("sigmoid(0) + where(a > 2, 1, 0)", env, 2), [0.5, 1.5])
    for bad in ["__import__('os')", "a.__class__", "open('x')", "[a for a in b]", "lambda: 1", "a[0]", "a ** b",
                "exec('1')", "a ** 50"]:
        with pytest.raises(ExpressionError):
            evaluate(bad, env, 2)
    with pytest.raises(ExpressionError, match="division by zero"):
        evaluate("a / (b - 2)", env, 2)
    with pytest.raises(ExpressionError, match="log"):
        evaluate("log(a - 1)", env, 2)
    assert check_expr("a + q", {"a"}) and not check_expr("a + t", {"a", "t"})


# --------------------------------------------------------------------------- graph
def test_cycle_within_step_is_rejected_but_lag_is_allowed():
    spec = linear_xy(50)
    d = spec.model_dump()
    d["variables"][0]["parents"] = [{"variable": "w"}]
    d["variables"][0]["mechanism"] = {"type": "linear", "params": {"coefficients": {"w": 1.0}}}
    bad = SCMSpec.model_validate(d)
    with pytest.raises(GraphError, match="cycle"):
        execution_plan(bad)
    assert validate_spec(bad).status == "FAIL"
    # feedback over time through a lag is fine (stock-flow fixture)
    plan = execution_plan(temporal())
    assert plan.loop.index("supply") < plan.loop.index("sold") < plan.loop.index("stock")


def test_execution_phases_static_temporal_static():
    plan = execution_plan(reference("education_tutoring"))
    assert "ses" in plan.pre and "weekly_score" in plan.loop
    assert "avg_weekly_score" in plan.post and "final_score" in plan.post
    assert plan.post.index("avg_weekly_score") < plan.post.index("final_score")


# --------------------------------------------------------------------------- equations
def test_linear_equation_is_realised_exactly_and_statistically():
    w = generate_world(linear_xy(4000, coef=5.0, sd=1.0))
    df = w.internals["units"]
    u = df["__aux__y::u0"].to_numpy()
    expected = 5.0 * df["x"].to_numpy() + stats.norm.ppf(u)  # independent computation
    assert np.allclose(df["y"], expected)
    slope = np.polyfit(df["x"], df["y"], 1)[0]
    assert abs(slope - 5.0) < 0.1
    assert np.allclose(df["w"], 2 * df["y"])


def test_reproducibility_and_seed_sensitivity():
    a, b = generate_world(linear_xy(500)), generate_world(linear_xy(500))
    assert a.world_hash == b.world_hash
    c = generate_world(linear_xy(500), seed=999)
    assert c.world_hash != a.world_hash


def test_independent_rng_streams():
    base = generate_world(linear_xy(300))
    d = linear_xy(300).model_dump()
    d["variables"].insert(0, {"variable_id": "extra", "entity": "units", "column": "extra",
                              "mechanism": {"type": "exogenous", "params": {"distribution": normal(3)}}})
    w2 = generate_world(SCMSpec.model_validate(d))
    for col in ("x", "z", "y", "w"):
        assert np.array_equal(base.internals["units"][col], w2.internals["units"][col])


def test_structural_noise_change_propagates_only_to_descendants():
    base = generate_world(linear_xy(500, sd=1.0))
    noisy = generate_world(linear_xy(500, sd=3.0))
    b, n = base.internals["units"], noisy.internals["units"]
    assert np.array_equal(b["x"], n["x"]) and np.array_equal(b["z"], n["z"])
    assert np.allclose(n["y"] - 5 * n["x"], 3 * (b["y"] - 5 * b["x"]))  # same z, scaled sigma (CRN)
    assert not np.allclose(b["w"], n["w"])
    # the same via the engine-level noise scale used by the optimiser
    scaled = generate_world(linear_xy(500, sd=1.0), noise_scale={"y": 3.0})
    assert np.allclose(scaled.internals["units"]["y"], n["y"])


def test_mechanism_change_affects_descendants_not_others():
    a = generate_world(linear_xy(300, coef=5.0))
    b = generate_world(linear_xy(300, coef=1.0))
    assert np.array_equal(a.internals["units"]["z"], b.internals["units"]["z"])
    assert not np.allclose(a.internals["units"]["w"], b.internals["units"]["w"])


def test_categorical_cpt_probabilities():
    w = generate_world(temporal(periods=400))
    df = w.internals["shop_days"]
    sold0 = df[df["sold"] == 0]
    other = df[df["sold"] > 0]
    assert len(other) > 300
    assert abs((other["mood"] == "good").mean() - 0.7) < 0.06
    if len(sold0) > 50:
        assert (sold0["mood"] == "bad").mean() > 0.75


def test_cpt_strength_interpolation_weakens_dependence():
    d = temporal(periods=300).model_dump()
    mood = next(v for v in d["variables"] if v["variable_id"] == "mood")
    mood["mechanism"]["params"]["strength"] = 0.0
    mood["mechanism"]["params"]["table"][0]["probs"] = {"good": 0.0, "bad": 1.0}
    df = generate_world(SCMSpec.model_validate(d)).internals["shop_days"]
    assert abs((df["mood"] == "good").mean() - 0.7) < 0.06  # alpha = 0 -> reference distribution


def test_lagged_stock_flow_and_autoregression():
    w = generate_world(temporal())
    df = w.internals["shop_days"].sort_values(["shop_id", "__t"])
    for _, g in df.groupby("shop_id"):
        prev = np.r_[10, g["stock"].to_numpy()[:-1]]
        assert np.array_equal(g["stock"].to_numpy(), prev + g["supply"].to_numpy() - g["sold"].to_numpy())
        assert np.all(g["sold"].to_numpy() <= prev + g["supply"].to_numpy())  # sales never exceed stock
        assert np.all(g["stock"].to_numpy() >= 0)
        tprev = np.r_[0, g["temp"].to_numpy()[:-1]]
        noise = g["__aux__temp::noise"].to_numpy()
        assert np.allclose(g["temp"], 10 + 0.8 * (tprev - 10) + noise)


def test_event_changes_mechanism_only_for_affected_entities_after_start():
    w = generate_world(temporal(periods=200))
    df = w.internals["shop_days"]
    lam = df["__aux__demand::lambda"]
    after, shop2 = df["__t"] >= 20, df["shop_id"] == 2
    assert np.allclose(lam[after & shop2], np.exp(2.6))
    assert np.allclose(lam[~(after & shop2)], np.exp(1.6))
    ev = next(e for e in w.realized_events if e["event_id"] == "boost")
    assert ev["first_step"] == 20 and ev["affected_entities"] == {"shops": [2]}


@pytest.mark.parametrize("params", [{"shift": 4.5}, {}, {"delta": "4.5"}, {"delta": True}, {"delta": np.inf}])
def test_invalid_shift_fails_validation_and_execution(params):
    spec = linear_xy(5)
    event = EventSpec(event_id="supplier_delay", affected_variables=["x"], intervention_type="shift",
                      intervention_parameters=params)
    spec.events.append(event)
    errors = validate_spec(spec).errors
    assert any(e.target == "event:supplier_delay" and "'delta'" in e.explanation for e in errors)
    with pytest.raises(ValueError, match="supplier_delay.*delta"):
        generate_world(spec)


@pytest.mark.parametrize(("kind", "params"), [
    ("scale", {"scale": 2}), ("set_value", {}), ("seasonal", {"period": 7}),
    ("seasonal", {"amplitude": 2, "period": 0}), ("param_change", {"param": "intercept"}),
    ("param_change", {"param": "intercept", "value": 1, "factor": 2}),
])
def test_invalid_intervention_parameters_are_reported(kind, params):
    spec = linear_xy(5)
    spec.events.append(EventSpec(event_id="bad_event", affected_variables=["y"], intervention_type=kind,
                                 intervention_parameters=params))
    assert any(e.check_id == "event.params" for e in validate_spec(spec).errors)


def test_shift_changes_only_selected_rows_by_delta():
    spec = linear_xy(10)
    before = generate_world(spec).internals["units"]
    spec.events.append(EventSpec(event_id="supplier_delay", affected_variables=["x"], affected_entities="unit_id == 3",
                                 intervention_type="shift", intervention_parameters={"delta": 4.5}))
    assert not validate_spec(spec).errors
    after = generate_world(spec).internals["units"]
    selected = before["unit_id"] == 3
    assert selected.sum() == 1
    assert np.allclose(after["x"] - before["x"], 4.5 * selected)


def test_set_value_intervention_vs_measurement_error_are_distinct():
    spec = linear_xy(400)
    do = EventSpec(event_id="do_x", affected_variables=["x"], intervention_type="set_value",
                   intervention_parameters={"value": 1.0})
    w = generate_world(spec, extra_events=[do])
    df = w.internals["units"]
    assert np.allclose(df["x"], 1.0)
    assert np.allclose(df["y"], 5.0 + stats.norm.ppf(df["__aux__y::u0"]))  # downstream sees the intervened value


def test_distribution_parametrisations():
    from app.causal.distributions import Distribution, sample
    u = np.clip(np.random.default_rng(0).random(200000), 1e-12, 1 - 1e-12)
    x = sample(Distribution(family="lognormal", params={"mean": 60, "sd": 25}), u)
    assert abs(x.mean() - 60) < 0.5 and abs(x.std() - 25) < 0.6
    g = sample(Distribution(family="gamma", params={"mean": 3, "sd": 1}), u)
    assert abs(g.mean() - 3) < 0.02 and abs(g.std() - 1) < 0.02
    nb = sample(Distribution(family="negative_binomial", params={"mean": 4, "dispersion": 2}), u)
    assert abs(nb.mean() - 4) < 0.05 and abs(nb.var() - (4 + 16 / 2)) < 0.3


def test_invalid_specs_are_rejected_with_specific_errors():
    d = linear_xy(10).model_dump()
    d["variables"][2]["mechanism"] = {"type": "deterministic_expression", "params": {"expression": "__import__('os')"}}
    d["variables"][3]["mechanism"] = {"type": "neural_net", "params": {}}
    d["variables"][1]["parents"] = [{"variable": "ghost"}]
    rep = validate_spec(SCMSpec.model_validate(d))
    msgs = " | ".join(c.explanation for c in rep.errors)
    assert rep.status == "FAIL"
    assert "not allowed" in msgs and "unsupported mechanism" in msgs and "unknown parent" in msgs


def test_generation_errors_are_reported_not_hidden():
    d = linear_xy(10).model_dump()
    d["variables"][3]["mechanism"] = {"type": "deterministic_expression", "params": {"expression": "log(y - 1000)"}}
    with pytest.raises(GenerationError, match="log"):
        generate_world(SCMSpec.model_validate(d))
