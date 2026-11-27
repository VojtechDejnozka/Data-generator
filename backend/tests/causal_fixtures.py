"""Small declarative SCM fixtures for the causal-core tests (no LLM, no domain code)."""
from __future__ import annotations

import json
from pathlib import Path

from app.causal.contracts import SCMSpec

SCEN = Path(__file__).resolve().parents[1] / "app" / "causal" / "scenarios"


def reference(name: str) -> SCMSpec:
    return SCMSpec.model_validate(json.loads((SCEN / f"{name}.json").read_text()))


def normal(sd: float, mean: float = 0.0) -> dict:
    return {"family": "normal", "params": {"mean": mean, "sd": sd}}


def linear_xy(n: int = 2000, coef: float = 5.0, sd: float = 1.0, seed: int = 1) -> SCMSpec:
    """Y = coef * X + eps on one grain; Z ~ N(0,1) unrelated; W = 2Y (descendant)."""
    return SCMSpec.model_validate({
        "meta": {"seed": seed},
        "entities": [{"entity_id": "units", "kind": "base", "count": n, "primary_key": "unit_id"}],
        "variables": [
            {"variable_id": "x", "entity": "units", "column": "x",
             "mechanism": {"type": "exogenous", "params": {"distribution": normal(1.0)}}},
            {"variable_id": "z", "entity": "units", "column": "z",
             "mechanism": {"type": "exogenous", "params": {"distribution": normal(1.0)}}},
            {"variable_id": "y", "entity": "units", "column": "y", "parents": [{"variable": "x"}],
             "mechanism": {"type": "linear", "params": {"intercept": 0, "coefficients": {"x": coef},
                                                         "noise": normal(sd)}}},
            {"variable_id": "w", "entity": "units", "column": "w", "parents": [{"variable": "y"}],
             "mechanism": {"type": "deterministic_expression", "params": {"expression": "2 * y"}}},
        ],
    })


def relational(seed: int = 3) -> SCMSpec:
    """customers -> orders (count from a Poisson variable) -> items; aggregation back to customers."""
    return SCMSpec.model_validate({
        "meta": {"seed": seed},
        "entities": [
            {"entity_id": "segments", "kind": "base", "count": 3, "primary_key": "segment_id"},
            {"entity_id": "customers", "kind": "base", "count": 300, "primary_key": "customer_id",
             "assignments": [{"entity": "segments", "method": "balanced"}]},
            {"entity_id": "orders", "kind": "child", "parent": "customers", "count_variable": "n_orders",
             "primary_key": "order_id"},
            {"entity_id": "products", "kind": "base", "count": 12, "primary_key": "product_id"},
            {"entity_id": "order_items", "kind": "child", "parent": "orders", "count": 2, "primary_key": "item_id",
             "assignments": [{"entity": "products", "method": "distinct_per_parent"}]},
        ],
        "variables": [
            {"variable_id": "segment_level", "entity": "segments", "column": "level",
             "mechanism": {"type": "exogenous", "params": {"distribution": normal(1.0)}}},
            {"variable_id": "wealth", "entity": "customers", "column": "wealth", "parents": [{"variable": "segment_level", "alias": "lvl"}],
             "mechanism": {"type": "linear", "params": {"intercept": 1, "coefficients": {"lvl": 0.5}, "noise": normal(0.3)}}},
            {"variable_id": "n_orders", "entity": "customers", "column": "n_orders_declared", "data_type": "int",
             "parents": [{"variable": "wealth"}],
             "mechanism": {"type": "poisson_regression", "params": {"intercept": 0.3, "coefficients": {"wealth": 0.4}}}},
            {"variable_id": "order_value", "entity": "orders", "column": "value", "parents": [{"variable": "wealth"}],
             "mechanism": {"type": "linear", "params": {"intercept": 100, "coefficients": {"wealth": 20},
                                                         "noise": normal(5)}}},
            {"variable_id": "price", "entity": "products", "column": "price",
             "mechanism": {"type": "exogenous", "params": {"distribution": {"family": "uniform", "params": {"low": 10, "high": 50}}}}},
            {"variable_id": "item_price", "entity": "order_items", "column": "price", "parents": [{"variable": "price"}],
             "mechanism": {"type": "lookup", "params": {}}},
            {"variable_id": "total_spent", "entity": "customers", "column": "total_spent",
             "parents": [{"variable": "order_value", "aggregation": "sum"}], "mechanism": {"type": "aggregation", "params": {}}},
            {"variable_id": "orders_count", "entity": "customers", "column": "orders_count", "data_type": "int",
             "parents": [{"variable": "order_value", "aggregation": "count"}], "mechanism": {"type": "aggregation", "params": {}}},
        ],
    })


def temporal(seed: int = 5, periods: int = 40) -> SCMSpec:
    """Stock-flow panel with a lagged state, an event on one entity and a categorical CPT."""
    return SCMSpec.model_validate({
        "meta": {"seed": seed},
        "time": {"start": "2025-01-01", "periods": periods, "frequency": "D"},
        "entities": [
            {"entity_id": "shops", "kind": "base", "count": 4, "primary_key": "shop_id"},
            {"entity_id": "shop_days", "kind": "cross", "cross": ["shops"], "temporal": True, "primary_key": "shop_day_id"},
        ],
        "variables": [
            {"variable_id": "demand", "entity": "shop_days", "column": "demand", "data_type": "int",
             "mechanism": {"type": "poisson_regression", "params": {"intercept": 1.6}}},
            {"variable_id": "supply", "entity": "shop_days", "column": "supply", "data_type": "int",
             "parents": [{"variable": "stock", "alias": "prev", "lag": 1}],
             "mechanism": {"type": "deterministic_expression", "params": {"expression": "max(10 - prev, 0)"}}},
            {"variable_id": "sold", "entity": "shop_days", "column": "sold", "data_type": "int",
             "parents": [{"variable": "demand"}, {"variable": "stock", "alias": "prev", "lag": 1}, {"variable": "supply"}],
             "mechanism": {"type": "deterministic_expression", "params": {"expression": "min(demand, prev + supply)"}}},
            {"variable_id": "stock", "entity": "shop_days", "column": "stock", "data_type": "int", "initial_state": 10,
             "parents": [{"variable": "stock", "alias": "prev", "lag": 1}, {"variable": "supply"}, {"variable": "sold"}],
             "mechanism": {"type": "deterministic_expression", "params": {"expression": "prev + supply - sold"}}},
            {"variable_id": "mood", "entity": "shop_days", "column": "mood", "data_type": "category",
             "categories": ["good", "bad"], "parents": [{"variable": "sold"}],
             "mechanism": {"type": "categorical_cpt", "params": {
                 "table": [{"when": {"sold": "0"}, "probs": {"good": 0.1, "bad": 0.9}}],
                 "default": {"good": 0.7, "bad": 0.3}}}},
            {"variable_id": "temp", "entity": "shop_days", "column": "temp",
             "parents": [{"variable": "temp", "alias": "prev", "lag": 1}], "initial_state": 0,
             "mechanism": {"type": "autoregressive", "params": {"phi": 0.8, "mean": 10, "noise": normal(1.0)}}},
        ],
        "events": [{"event_id": "boost", "affected_variables": ["demand"], "affected_entities": "shop_id == 2",
                    "start_time": 20, "intervention_type": "param_change",
                    "intervention_parameters": {"param": "intercept", "value": 2.6}}],
    })
