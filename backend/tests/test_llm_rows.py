import pandas as pd

from app.contracts.spec import Column, Table
from app.generation.llm_rows import generate_table, row_model, validate_rows
from app.generation.scenarios.registry import RETAIL
from app.contracts.io import ScenarioInput
from tests.conftest import run

PROMO = Table(name="promotions", strategy="llm_rows", row_count=30, columns=[
    Column(name="promotion_id", type="int", pk=True),
    Column(name="product_id", type="int", fk="products.product_id"),
    Column(name="start_date", type="date"),
    Column(name="discount_pct", type="float", min=5, max=40),
    Column(name="channel", type="text", enum_values=["web", "leták"]),
])


def test_row_model_excludes_auto_pk_and_enforces_enum():
    M = row_model(PROMO)
    props = M.model_json_schema()["$defs"]["Row_promotions"]["properties"]
    assert "promotion_id" not in props and "channel" in props


def test_validate_rows_catches_bad_fk_range_date():
    rows = [{"product_id": 1, "start_date": "2025-02-01", "discount_pct": 10, "channel": "web"},
            {"product_id": 99, "start_date": "2025-02-01", "discount_pct": 10, "channel": "web"},
            {"product_id": 1, "start_date": "2030-01-01", "discount_pct": 10, "channel": "web"},
            {"product_id": 1, "start_date": "2025-02-01", "discount_pct": 90, "channel": "web"}]
    good, errs = validate_rows(PROMO, rows, {"product_id": {1, 2}}, ("2025-01-01", "2025-12-31"), set())
    assert len(good) == 1 and len(errs) == 3


def test_generate_table_mock():
    spec = RETAIL.default_spec(ScenarioInput(company="x", story="Testovací příběh"))
    spec = spec.model_copy(update={"tables": [*spec.tables, PROMO]})
    products = pd.DataFrame({"product_id": range(1, 21), "name": [f"P{i}" for i in range(1, 21)]})
    df, stats = run(generate_table(spec, PROMO, {"products": products}, batch_size=10))
    assert len(df) == 30 and list(df.columns)[0] == "promotion_id"
    assert df.product_id.isin(products.product_id).all()
    assert df.promotion_id.is_unique
