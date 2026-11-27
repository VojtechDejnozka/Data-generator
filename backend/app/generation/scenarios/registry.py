"""Registry of scenario modules (story archetypes with a deterministic simulator)."""
from __future__ import annotations

from typing import Optional

from .base import ScenarioModule
from .retail_supplier_delay import MODULE as RETAIL

MODULES: dict[str, ScenarioModule] = {RETAIL.id: RETAIL}


def get_module(module_id: Optional[str]) -> Optional[ScenarioModule]:
    return MODULES.get(module_id or "")


def catalog() -> list[dict]:
    return [m.catalog_entry() for m in MODULES.values()]
