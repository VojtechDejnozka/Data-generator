import hashlib

import numpy as np
import pandas as pd
import pytest

from app.generation import effects
from app.generation.scenarios.base import GenContext
from app.generation.scenarios.registry import RETAIL


def _sim(retail_dataset, **knobs):
    spec, dims = retail_dataset
    kv = spec.knob_values({k: str(v) for k, v in knobs.items()})
    return RETAIL.simulate(spec, dims, GenContext(seed=7, knobs=kv))


def _h(df: pd.DataFrame) -> str:
    return hashlib.md5(pd.util.hash_pandas_object(df.astype(str), index=False).values.tobytes()).hexdigest()


@pytest.fixture(scope="module")
def story(retail_dataset):
    return _sim(retail_dataset)


@pytest.fixture(scope="module")
def baseline(retail_dataset):
    return _sim(retail_dataset, story_intensity=0)


def test_effect_primitives():
    t = np.array([-5, 0, 5, 10, 20])
    assert list(effects.ramp(t, 10, "linear")) == [0, 0, 0.5, 1, 1]
    assert effects.ramp(t, 10, "step")[1] == 1
    s = effects.ramp(np.array([0, 5, 10]), 10, "sigmoid")
    assert s[0] == pytest.approx(0, abs=1e-9) and s[2] == pytest.approx(1) and 0.4 < s[1] < 0.6
    e = effects.ramp(np.array([5]), 10, "exponential")
    assert e[0] < 0.5  # slow start
    dates = pd.date_range("2025-01-01", periods=14)
    assert effects.weekday_profile(dates, [1, 2, 3, 4, 5, 6, 7])[0] == 3  # 2025-01-01 is a Wednesday
    out = effects.apply_effects(np.ones(14), dates, [effects.EffectSpec("window", {"start": "2025-01-03",
                                                                                     "end": "2025-01-04", "inside": 0})])
    assert out[2] == 0 and out[3] == 0 and out[4] == 1


def test_rng_streams_independent():
    a = effects.streams(1, "x", "y")
    b = effects.streams(1, "y")
    assert a["y"].random() == b["y"].random()


def test_deterministic_same_seed(retail_dataset, story):
    again = _sim(retail_dataset)
    for name in ("transactions", "deliveries", "complaints", "inventory_snapshots"):
        assert _h(story.tables[name]) == _h(again.tables[name]), name


def test_demand_identical_across_knobs(story, baseline):
    # footfall is sampled independently from stock, so what-if runs share the same demand
    a, b = story.tables["footfall_daily"], baseline.tables["footfall_daily"]
    assert (a.visitors.to_numpy() == b.visitors.to_numpy()).all()


def test_story_mechanism_visible(story, retail_dataset):
    spec, _ = retail_dataset
    signals = RETAIL.story_signals(story.manifest, spec.knob_values())
    assert all(s["detectable"] for s in signals), signals
    r = story.manifest["realized"]
    assert r["affected_revenue_change_pct"] < r["control_revenue_change_pct"] - 8


def test_baseline_has_no_story(baseline, retail_dataset):
    spec, _ = retail_dataset
    signals = {s["step_id"]: s for s in RETAIL.story_signals(baseline.manifest, spec.knob_values({"story_intensity": "0"}))}
    assert not signals["s1"]["detectable"] and not signals["s2"]["detectable"]
    d = baseline.tables["deliveries"]
    sup = int(spec.knob_values()["supplier_id"])
    assert d[d.supplier_id == sup].delay_days.mean() < 1.0


def test_story_delays_only_affect_story_supplier(story, retail_dataset):
    spec, _ = retail_dataset
    sup = int(spec.knob_values()["supplier_id"])
    d = story.tables["deliveries"]
    late = d[d.planned_date >= story.manifest["story"]["full_severity_date"]]
    assert late[late.supplier_id == sup].delay_days.mean() > 3
    assert late[late.supplier_id != sup].delay_days.mean() < 1


def test_severity_knob_monotonic(retail_dataset, story):
    stronger = _sim(retail_dataset, delay_severity=10)
    assert stronger.manifest["realized"]["affected_revenue_change_pct"] < story.manifest["realized"]["affected_revenue_change_pct"]


def test_internal_consistency(story):
    t = story.tables
    tx, items = t["transactions"], t["transaction_items"]
    s = items.groupby("transaction_id").line_total.sum()
    assert np.allclose(tx.set_index("transaction_id").total_amount.loc[s.index], s, atol=0.05)
    d = t["deliveries"]
    assert (pd.to_datetime(d.delivered_date) >= pd.to_datetime(d.planned_date)).all()
    assert (t["inventory_snapshots"].on_hand_qty >= 0).all()
    assert t["complaints"][t["complaints"].category == "Nedostupnost zboží"].product_id.notna().all()
    # complaint texts were filled with real product names
    names = set(t["products"].name)
    txt = t["complaints"][t["complaints"].category == "Nedostupnost zboží"].text.head(30)
    assert any(any(n in x for n in names) for x in txt)
