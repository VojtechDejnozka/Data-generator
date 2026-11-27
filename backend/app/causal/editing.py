"""SCM edit operations (logic + contract only, spec §5.2).

Every edit updates the single source of truth (``SCMSpec``), is validated, and reports which derived
artefacts become invalid. Identifiers are stable: renaming changes ``name`` / ``column`` only, never
``variable_id``. Adding or removing an edge keeps the target mechanism's parents and parameters
consistent (coefficients are created / removed together with the parent reference).
"""
from __future__ import annotations

import copy
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import mechanisms as M
from .contracts import EntitySpec, EventSpec, ParentRef, SCMSpec, VariableSpec
from .graph import descendants
from .results import ValidationReport
from .validation import validate_spec

LINEAR_FAMILY = {"linear", "polynomial", "interaction", "lagged_effect", "logistic", "poisson_regression", "threshold",
                 "ordinal_threshold"}


class EditOp(BaseModel):
    op: Literal["update_variable", "add_variable", "remove_variable", "rename_variable", "add_edge", "remove_edge",
                "set_parameter", "set_mechanism", "add_entity", "update_entity", "remove_entity", "add_event",
                "update_event", "remove_event", "update_meta", "update_time"]
    target: Optional[str] = Field(None, description="variable_id / entity_id / event_id (target of an edge)")
    source: Optional[str] = Field(None, description="add/remove_edge: parent variable_id")
    path: Optional[str] = Field(None, description="set_parameter: dotted path in mechanism.params")
    value: Any = None
    lag: int = 0
    alias: Optional[str] = None
    aggregation: Optional[str] = None
    coefficient: Optional[float] = None
    cascade: bool = False


class EditResult(BaseModel):
    scm: SCMSpec
    applied: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    report: ValidationReport
    change: dict[str, Any] = Field(default_factory=dict)


def _set(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    if value is None:
        d.pop(parts[-1], None)
    else:
        d[parts[-1]] = value


def _references(scm: SCMSpec, vid: str) -> list[str]:
    out = [f"{v.variable_id}.parents" for v in scm.variables if any(p.variable == vid for p in v.parents) and v.variable_id != vid]
    out += [f"entity {e.entity_id}.count_variable" for e in scm.entities if e.count_variable == vid]
    out += [f"event {e.event_id}" for e in scm.events if vid in e.affected_variables]
    out += [f"fact {f.fact_id}" for f in scm.ground_truth.story_facts if vid in (f.source, f.target, *f.evidence_variables)]
    return out


def _apply(scm: SCMSpec, op: EditOp, warnings: list[str]) -> SCMSpec:
    d = scm.model_dump(mode="json")
    vars_ = {v["variable_id"]: v for v in d["variables"]}
    if op.op in ("update_variable", "rename_variable", "set_parameter", "set_mechanism", "add_edge", "remove_edge"):
        if op.target not in vars_:
            raise ValueError(f"unknown variable {op.target}")
    if op.op == "add_variable":
        v = VariableSpec.model_validate(op.value)
        if v.variable_id in vars_:
            raise ValueError(f"variable {v.variable_id} already exists")
        d["variables"].append(v.model_dump(mode="json"))
    elif op.op == "update_variable":
        upd = dict(op.value or {})
        if "variable_id" in upd and upd["variable_id"] != op.target:
            raise ValueError("variable_id is a stable identifier and cannot be changed (rename via 'name')")
        vars_[op.target].update(upd)
    elif op.op == "rename_variable":
        val = op.value if isinstance(op.value, dict) else {"name": op.value}
        for k in ("name", "column"):
            if k in val:
                vars_[op.target][k] = val[k]
    elif op.op == "remove_variable":
        refs = _references(scm, op.target or "")
        if refs and not op.cascade:
            raise ValueError(f"variable {op.target} is referenced by {refs}; remove those first or use cascade")
        d["variables"] = [v for v in d["variables"] if v["variable_id"] != op.target]
        if op.cascade:
            for v in d["variables"]:
                gone = [p for p in v["parents"] if p["variable"] == op.target]
                v["parents"] = [p for p in v["parents"] if p["variable"] != op.target]
                for p in gone:
                    _drop_terms(v, p.get("alias") or p["variable"])
            d["events"] = [e for e in d["events"] if op.target not in e["affected_variables"]]
            for f in d["ground_truth"]["story_facts"]:
                f["evidence_variables"] = [x for x in f.get("evidence_variables", []) if x != op.target]
            d["ground_truth"]["story_facts"] = [f for f in d["ground_truth"]["story_facts"]
                                                if op.target not in (f.get("source"), f.get("target"))]
    elif op.op == "add_edge":
        if op.source not in vars_:
            raise ValueError(f"unknown source variable {op.source}")
        v = vars_[op.target]
        alias = op.alias or op.source
        if any((p.get("alias") or p["variable"]) == alias for p in v["parents"]):
            raise ValueError(f"{op.target} already has a parent named {alias}")
        v["parents"].append(ParentRef(variable=op.source, alias=op.alias, lag=op.lag,
                                      aggregation=op.aggregation).model_dump(mode="json"))
        params = v["mechanism"]["params"]
        if v["mechanism"]["type"] in LINEAR_FAMILY and not params.get("predictor"):
            params.setdefault("coefficients", {})[alias] = float(op.coefficient if op.coefficient is not None else 0.0)
            if op.coefficient is None:
                warnings.append(f"edge {op.source} -> {op.target} added with coefficient 0 (no effect yet)")
        else:
            warnings.append(f"{op.target}: mechanism {v['mechanism']['type']} must reference '{alias}' to make the "
                            "new edge effective (edit the expression / table)")
    elif op.op == "remove_edge":
        v = vars_[op.target]
        gone = [p for p in v["parents"] if p["variable"] == op.source and (op.alias is None or p.get("alias") == op.alias)]
        if not gone:
            raise ValueError(f"{op.source} is not a parent of {op.target}")
        v["parents"] = [p for p in v["parents"] if p not in gone]
        for p in gone:
            _drop_terms(v, p.get("alias") or p["variable"])
    elif op.op == "set_parameter":
        if not op.path:
            raise ValueError("set_parameter needs 'path'")
        _set(vars_[op.target]["mechanism"]["params"], op.path, op.value)
    elif op.op == "set_mechanism":
        M.get((op.value or {}).get("type", ""))
        vars_[op.target]["mechanism"] = op.value
    elif op.op == "add_entity":
        e = EntitySpec.model_validate(op.value)
        if any(x["entity_id"] == e.entity_id for x in d["entities"]):
            raise ValueError(f"entity {e.entity_id} exists")
        d["entities"].append(e.model_dump(mode="json"))
    elif op.op == "update_entity":
        ent = next((x for x in d["entities"] if x["entity_id"] == op.target), None)
        if not ent:
            raise ValueError(f"unknown entity {op.target}")
        upd = dict(op.value or {})
        if "entity_id" in upd and upd["entity_id"] != op.target:
            raise ValueError("entity_id is a stable identifier")
        ent.update(upd)
    elif op.op == "remove_entity":
        used = [v["variable_id"] for v in d["variables"] if v["entity"] == op.target]
        deps = [x["entity_id"] for x in d["entities"] if x.get("parent") == op.target or op.target in x.get("cross", [])
                or any(a["entity"] == op.target for a in x.get("assignments", []))]
        if (used or deps) and not op.cascade:
            raise ValueError(f"entity {op.target} is used by variables {used} / entities {deps}")
        d["entities"] = [x for x in d["entities"] if x["entity_id"] != op.target]
        d["variables"] = [v for v in d["variables"] if v["entity"] != op.target]
    elif op.op == "add_event":
        e = EventSpec.model_validate(op.value)
        if any(x["event_id"] == e.event_id for x in d["events"]):
            raise ValueError(f"event {e.event_id} exists")
        d["events"].append(e.model_dump(mode="json"))
    elif op.op == "update_event":
        ev = next((x for x in d["events"] if x["event_id"] == op.target), None)
        if not ev:
            raise ValueError(f"unknown event {op.target}")
        ev.update(dict(op.value or {}))
    elif op.op == "remove_event":
        d["events"] = [x for x in d["events"] if x["event_id"] != op.target]
    elif op.op == "update_meta":
        d["meta"].update(dict(op.value or {}))
    elif op.op == "update_time":
        if d.get("time") is None:
            raise ValueError("the model has no time axis")
        d["time"].update(dict(op.value or {}))
    return SCMSpec.model_validate(d)


def _drop_terms(v: dict[str, Any], alias: str) -> None:
    params = v["mechanism"]["params"]
    coefs = params.get("coefficients")
    if isinstance(coefs, dict):
        for term in list(coefs):
            if alias in M.term_names(term):
                coefs.pop(term)


def classify_change(old: SCMSpec, new: SCMSpec) -> dict[str, Any]:
    """Which pipeline phases must be redone."""
    if old.fingerprint() == new.fingerprint():
        if old.difficulty.model_dump() != new.difficulty.model_dump():
            return {"kind": "difficulty", "invalidates": ["optimisation"], "regenerate_world": False}
        return {"kind": "none", "invalidates": [], "regenerate_world": False}
    o = {v.variable_id: v for v in old.variables}
    n = {v.variable_id: v for v in new.variables}
    changed = sorted(set(o) ^ set(n) | {k for k in o.keys() & n.keys() if o[k].model_dump() != n[k].model_dump()})
    structure_same = (old.model_dump(include={"entities", "events", "time"}) ==
                      new.model_dump(include={"entities", "events", "time"}))
    noise_only = structure_same and changed and all(
        k in o and k in n and _strip_noise(o[k]) == _strip_noise(n[k]) for k in changed)
    presentation_only = structure_same and changed and all(
        k in o and k in n and _strip_presentation(o[k]) == _strip_presentation(n[k]) for k in changed)
    affected = set(changed)
    for k in changed:
        if k in n:
            affected |= descendants(new, k)
    if presentation_only:
        return {"kind": "presentation", "changed_variables": changed, "regenerate_world": False,
                "invalidates": ["observations", "evaluations", "optimisation"],
                "note": "neutral descriptions shown to analysts changed"}
    return {"kind": "structural_noise" if noise_only else "scm", "changed_variables": changed,
            "affected_variables": sorted(affected), "regenerate_world": True,
            "invalidates": ["world", "ground_truth", "validation", "observations", "evaluations", "optimisation"],
            "note": "the clean world must be regenerated and validated; with common random numbers the "
                    "non-affected variables keep identical realisations"}


def _strip_noise(v: VariableSpec) -> dict[str, Any]:
    d = copy.deepcopy(v.model_dump(mode="json"))
    d["mechanism"]["params"].pop("noise", None)
    if d["mechanism"]["type"] == "exogenous":
        d["mechanism"]["params"].get("distribution", {}).get("params", {}).pop("sd", None)
    return d


def _strip_presentation(v: VariableSpec) -> dict[str, Any]:
    d = v.model_dump(mode="json")
    for k in ("name", "description", "public_description", "unit"):
        d.pop(k, None)
    return d


def apply_edits(scm: SCMSpec, ops: list[EditOp]) -> EditResult:
    cur = scm
    applied, errors, warnings = [], [], []
    for i, op in enumerate(ops):
        try:
            cur = _apply(cur, op, warnings)
            applied.append(f"{i}:{op.op}:{op.target or ''}")
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"{i}:{op.op}:{op.target or ''}: {e}")
    rep = validate_spec(cur)
    for v in cur.variables:
        used = mechanism_names(v)
        if used is None:
            continue
        for p in v.parents:
            if p.name not in used and not (v.mechanism.type == "autoregressive" and p.variable == v.variable_id):
                warnings.append(f"{v.variable_id}: parent '{p.name}' is declared but not used by the mechanism")
    return EditResult(scm=cur, applied=applied, errors=errors, warnings=warnings, report=rep,
                      change=classify_change(scm, cur))


def mechanism_names(v: VariableSpec) -> Optional[set[str]]:
    """Names referenced by a mechanism (None if unknown)."""
    from .expressions import ExpressionError, compile_expr
    import string
    p = v.mechanism.params
    t = v.mechanism.type

    def expr(s: Optional[str]) -> set[str]:
        try:
            return set(compile_expr(s).names) if s else set()
        except ExpressionError:
            return set()
    if t in LINEAR_FAMILY or t == "autoregressive":
        out = expr(p.get("predictor"))
        for term in (p.get("coefficients") or {}):
            out |= set(M.term_names(term))
        return out
    if t == "deterministic_expression":
        return expr(p.get("expression"))
    if t == "conditional":
        out = expr(p.get("default"))
        for c in p.get("cases", []):
            out |= expr(c.get("when")) | expr(c.get("value"))
        return out
    if t == "categorical_cpt":
        return {k for row in p.get("table", []) for k in row.get("when", {})}
    if t in ("lookup", "aggregation"):
        return {p.get("source") or (v.parents[0].name if v.parents else "")}
    if t == "text_template":
        out = {p["select_by"]} if p.get("select_by") else set()
        for tpls in [*(p.get("templates") or {}).values(), p.get("vague_templates") or []]:
            for tpl in tpls:
                out |= {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
        return out
    if t == "categorical_softmax":
        out = set()
        for lp in (p.get("logits") or {}).values():
            out |= expr(lp.get("predictor"))
            for term in lp.get("coefficients", {}):
                out |= set(M.term_names(term))
        return out
    if t == "group_effect":
        b = p.get("base") or {}
        return {n for term in b.get("coefficients", {}) for n in M.term_names(term)} | expr(b.get("predictor"))
    return None
