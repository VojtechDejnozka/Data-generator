"""Observation & corruption engine."""
import numpy as np
import pandas as pd
import pytest

from app.causal.contracts import CorruptionConfig, CorruptionOp, SCMSpec
from app.causal.corruption import observe, observed_projection
from app.causal.engine import generate_world, tables_hash

from .causal_fixtures import normal, reference


@pytest.fixture(scope="module")
def world():
    return generate_world(reference("retail_supplier_disruption"))


def _op(t, table="inventory_snapshots", cols=(), p=0.2, **params):
    return CorruptionOp(type=t, target_table=table, target_columns=list(cols), probability=p, params=params)


def test_observed_projection_drops_latent_variables(world):
    proj = observed_projection(world)
    assert "demand" not in proj["inventory_snapshots"].columns
    assert set(world.tables["inventory_snapshots"].columns) >= set(proj["inventory_snapshots"].columns)
    o = observe(world, CorruptionConfig())
    assert o.realized["realized_share"] == 0 and not o.manifest["interventions"]


def test_reproducible_and_common_random_numbers(world):
    cfg = CorruptionConfig(operations=[_op("missing_values_mcar", cols=["units_sold"], p=0.1)])
    a, b = observe(world, cfg), observe(world, cfg)
    assert a.observed_hash == b.observed_hash
    hi = observe(world, CorruptionConfig(operations=[_op("missing_values_mcar", cols=["units_sold"], p=0.3)]))
    lo_missing = set(a.tables["inventory_snapshots"].loc[a.tables["inventory_snapshots"]["units_sold"].isna(), "snapshot_id"])
    hi_missing = set(hi.tables["inventory_snapshots"].loc[hi.tables["inventory_snapshots"]["units_sold"].isna(), "snapshot_id"])
    assert lo_missing <= hi_missing  # raising the rate only moves the threshold


def test_mcar_realised_amount_and_manifest(world):
    o = observe(world, CorruptionConfig(operations=[_op("missing_values_mcar", cols=["units_sold", "closing_stock"], p=0.2)]))
    per = o.realized["per_operation"][0]
    assert abs(per["realized_share"] - 0.2) < 0.02
    entry = o.manifest["interventions"][0]
    clean = world.tables["inventory_snapshots"].set_index("snapshot_id")[entry["column"]]
    assert entry["count"] == len(entry["row_ids"])
    assert np.allclose(clean.reindex(entry["row_ids"]).to_numpy(), np.asarray(entry["original"], float))
    assert all(v is None for v in entry["new"])


def test_mar_depends_on_observed_driver(world):
    o = observe(world, CorruptionConfig(operations=[_op("missing_values_mar", cols=["closing_stock"], p=0.2,
                                                         driver="units_sold", strength=2.0)]))
    df = o.tables["inventory_snapshots"]
    miss = df["closing_stock"].isna()
    assert df.loc[miss, "units_sold"].mean() > df.loc[~miss, "units_sold"].mean() + 0.5


def test_mnar_depends_on_the_true_value(world):
    o = observe(world, CorruptionConfig(operations=[_op("missing_values_mnar", cols=["sales_amount"], p=0.2, strength=2.0)]))
    obs = o.tables["inventory_snapshots"]
    clean = world.tables["inventory_snapshots"]
    miss = obs["sales_amount"].isna().to_numpy()
    assert clean["sales_amount"][miss].mean() > 1.5 * clean["sales_amount"][~miss].mean()


def test_duplicates_kinds(world):
    o = observe(world, CorruptionConfig(operations=[_op("exact_duplicates", p=0.1), _op("near_duplicates", "purchase_orders", p=0.1)]))
    inv = o.tables["inventory_snapshots"]
    assert inv["snapshot_id"].duplicated().sum() == o.realized["per_operation"][0]["rows_added"] > 1000
    po = o.tables["purchase_orders"]
    assert not po["order_id"].duplicated().any()  # near duplicates get a new technical id
    assert len(po) > len(world.tables["purchase_orders"])


def test_shuffle_keeps_marginal_but_breaks_dependence(world):
    o = observe(world, CorruptionConfig(operations=[_op("column_value_shuffle", cols=["sales_amount"], p=1.0)]))
    a = world.tables["inventory_snapshots"]
    b = o.tables["inventory_snapshots"]
    assert np.isclose(np.sort(a["sales_amount"]), np.sort(b["sales_amount"].astype(float))).all()
    assert abs(np.corrcoef(a["units_sold"], b["sales_amount"].astype(float))[0, 1]) < 0.1


def test_misclassification_measurement_noise_and_hidden_columns(world):
    o = observe(world, CorruptionConfig(operations=[
        _op("category_misclassification", "complaints", ["category"], p=0.3),
        _op("gaussian_measurement_noise", "store_daily", ["visits"], p=1.0, intensity=0.5),
        CorruptionOp(type="hidden_columns", target_table="inventory_snapshots", target_columns=["delivered_qty"], probability=1.0)]))
    c0, c1 = world.tables["complaints"]["category"], o.tables["complaints"]["category"]
    assert 0.2 < (c0 != c1).mean() < 0.4 and set(c1) <= set(c0)
    v0, v1 = world.tables["store_daily"]["visits"], pd.to_numeric(o.tables["store_daily"]["visits"])
    assert 0.3 < (v1 - v0).std() / v0.std() < 0.7
    assert "delivered_qty" not in o.tables["inventory_snapshots"].columns


def test_protected_columns_keys_and_clean_world_untouched(world):
    before = tables_hash(world.tables)
    cfg = CorruptionConfig(operations=[_op("missing_values_mcar", p=0.5), _op("column_value_shuffle", p=0.5)],
                           protected_columns=["inventory_snapshots.units_sold"])
    o = observe(world, cfg)
    inv = o.tables["inventory_snapshots"]
    assert inv["units_sold"].notna().all()
    assert inv["snapshot_id"].notna().all() and inv["store_id"].notna().all()  # keys protected by default
    assert np.array_equal(inv["units_sold"].astype(int), world.tables["inventory_snapshots"]["units_sold"])
    assert tables_hash(world.tables) == before
    checks = {c.check_id: c for c in o.checks}
    assert checks["clean_world_unchanged"].passed and checks["protected_untouched"].passed


def test_every_registered_operation_runs(world):
    from app.causal.corruption import REGISTRY
    ops = []
    for i, t in enumerate(REGISTRY):
        table, cols, params = "inventory_snapshots", [], {}
        if t.startswith("text") or t == "category_misclassification":
            table, cols = "complaints", ["text" if t.startswith("text") else "category"]
        if t == "missing_values_mar":
            cols, params = ["closing_stock"], {"driver": "units_sold"}
        if t == "hidden_columns":
            cols = ["delivered_qty"]
        ops.append(CorruptionOp(type=t, target_table=table, target_columns=cols, probability=0.05, params=params,
                                operation_order=i))
    o = observe(world, CorruptionConfig(operations=ops))
    assert len(o.realized["per_operation"]) == len(REGISTRY)
    assert o.analyzable
