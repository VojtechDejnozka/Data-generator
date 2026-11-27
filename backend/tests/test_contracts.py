import pytest
from pydantic import ValidationError

from app.contracts.io import ScenarioInput
from app.contracts.spec import Column, Knob, ScenarioSpec
from app.generation.scenarios.registry import RETAIL
from app.llm.schema_utils import gemini_schema
from app.contracts.io import PlannerResponse


def _spec():
    return RETAIL.default_spec(ScenarioInput(company="x", story="y"))


def test_default_spec_is_structurally_valid():
    spec = _spec()
    assert spec.structural_errors() == []
    order = spec.topological_order()
    assert order.index("suppliers") < order.index("products") < order.index("transaction_items")
    assert len(spec.tables) >= 6


def test_fk_cycle_detected():
    spec = _spec()
    t = spec.table("suppliers")
    t.columns.append(Column(name="best_product", fk="products.product_id"))
    assert any("cycle" in e for e in spec.structural_errors())


def test_invalid_names_rejected():
    with pytest.raises(ValidationError):
        Column(name="Bad Name")
    with pytest.raises(ValidationError):
        Column(name="x", fk="nodot")


def test_knob_parse_and_clamp():
    k = Knob(name="sev", type="float", default="4", min=0, max=10)
    assert k.parse("20") == 10.0 and k.parse(None) == 4.0
    e = Knob(name="p", type="enum", default="linear", options=["linear", "step"])
    with pytest.raises(ValueError):
        e.parse("bogus")
    assert Knob(name="b", type="bool", default="true").parse("false") is False


def test_period_validation():
    data = _spec().model_dump()
    data["meta"]["period_end"] = data["meta"]["period_start"]
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(data)


def test_gemini_schema_inlines_refs():
    s = gemini_schema(PlannerResponse)
    text = str(s)
    assert "$ref" not in text and "$defs" not in text
    # Optional fields collapse to the non-null branch
    col = s["properties"]["spec"]["properties"]["tables"]["items"]["properties"]["columns"]["items"]
    assert col["properties"]["fk"]["type"] == "string"
