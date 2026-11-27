"""Convert Pydantic JSON schemas to a form Gemini structured output handles well.

* `$defs`/`$ref` are inlined (Gemini limits nesting/refs; inlining is the safest form).
* `Optional[X]` (anyOf X|null) collapses to X – the field simply stays non-required.
* Keywords that Gemini ignores or that only add branching (default, title, examples) are dropped.
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP = {"default", "title", "examples", "$schema", "discriminator"}


def _inline(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 40:
        raise ValueError("schema too deep / recursive")
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            merged = copy.deepcopy(defs[name])
            extra = {k: v for k, v in node.items() if k != "$ref"}
            merged.update(extra)
            return _inline(merged, defs, depth + 1)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP or k == "$defs":
                continue
            out[k] = _inline(v, defs, depth + 1)
        # Optional[X] -> X
        if "anyOf" in out:
            non_null = [b for b in out["anyOf"] if b.get("type") != "null"]
            if len(non_null) == 1:
                rest = {k: v for k, v in out.items() if k != "anyOf"}
                rest.update(non_null[0])
                out = rest
        return out
    if isinstance(node, list):
        return [_inline(v, defs, depth + 1) for v in node]
    return node


def gemini_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    return _inline(raw, defs)


def schema_from_json_schema(raw: dict[str, Any]) -> dict[str, Any]:
    return _inline(raw, raw.get("$defs", {}))
