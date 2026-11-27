"""Mock LLM: fixture responders keyed by task name.

Responders are plain functions `(prompt, context) -> dict` that return JSON compatible with the
requested schema. Size-dependent tasks (e.g. "generate 40 product names") compute deterministic
fixture data from bundled word lists so the full pipeline works offline.
"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

Responder = Callable[[str, dict[str, Any]], Any]
_REGISTRY: dict[str, Responder] = {}


def register_mock(task: str):
    def deco(fn: Responder) -> Responder:
        _REGISTRY[task] = fn
        return fn

    return deco


def mock_response(task: str, prompt: str, schema: type[BaseModel], context: dict[str, Any]) -> Any:
    from . import mock_handlers  # noqa: F401  (registers fixture responders)

    base = task.split(":")[0]
    fn = _REGISTRY.get(task) or _REGISTRY.get(base)
    if fn is None:
        raise KeyError(f"no mock fixture for task '{task}'")
    return fn(prompt, context)
