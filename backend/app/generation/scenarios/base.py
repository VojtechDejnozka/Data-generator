"""Scenario module interface. A module implements one story archetype end-to-end:
canonical schema, knobs, evidence, CHEAP-generated master data and a causal simulator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import pandas as pd

from ...contracts.io import ScenarioInput
from ...contracts.spec import EvidenceQuery, Invariant, Knob, ScenarioSpec, Table, Widget

ProgressFn = Callable[[str, float], None]


@dataclass
class GenContext:
    """Everything a generator step needs besides the spec."""

    seed: int
    knobs: dict[str, Any]
    progress: ProgressFn = lambda msg, frac: None
    notes: dict[str, str] = field(default_factory=dict)  # per-table repair instructions
    params: dict[str, Any] = field(default_factory=dict)  # repairable internal params


@dataclass
class SimulationResult:
    tables: dict[str, pd.DataFrame]
    manifest: dict[str, Any]


class ScenarioModule(ABC):
    id: str
    title: str
    description: str
    domain: str
    #: internal parameters the validator may tune in the repair loop, with (min, max)
    repairable_params: dict[str, tuple[float, float]] = {}

    @abstractmethod
    def tables(self) -> list[Table]: ...

    @abstractmethod
    def knobs(self, inp: Optional[ScenarioInput] = None) -> list[Knob]: ...

    @abstractmethod
    def evidence(self) -> list[EvidenceQuery]: ...

    @abstractmethod
    def invariants(self) -> list[Invariant]: ...

    @abstractmethod
    def dashboard_widgets(self) -> list[Widget]: ...

    @abstractmethod
    def default_spec(self, inp: ScenarioInput) -> ScenarioSpec: ...

    @abstractmethod
    def dimension_tables(self) -> list[str]: ...

    @abstractmethod
    async def build_dimensions(self, spec: ScenarioSpec, ctx: GenContext) -> dict[str, pd.DataFrame]: ...

    @abstractmethod
    def simulate(self, spec: ScenarioSpec, dims: dict[str, pd.DataFrame], ctx: GenContext) -> SimulationResult: ...

    def story_signals(self, manifest: dict[str, Any], knobs: dict[str, Any]) -> list[dict[str, Any]]:
        """Optional quantified detectability signals per causal step."""
        return []

    async def post_text(self, spec: ScenarioSpec, result: SimulationResult, ctx: GenContext) -> None:
        """Optional: fill free-text columns with CHEAP-generated text after simulation."""

    def catalog_entry(self) -> dict[str, Any]:
        """Compact description shown to the STRONG planner."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "domain": self.domain,
            "tables": [
                {"name": t.name, "strategy": t.strategy, "columns": [c.name for c in t.columns]} for t in self.tables()
            ],
            "knobs": [{"name": k.name, "type": k.type, "default": k.default, "description": k.description}
                      for k in self.knobs()],
        }

    def normalize_spec(self, spec: ScenarioSpec) -> tuple[ScenarioSpec, list[str]]:
        """Force canonical schema for simulated/dimension tables; keep extra LLM tables.

        The simulator owns those tables, so their columns cannot be changed by the planner/user.
        Descriptions and row counts of dimension tables are preserved.
        """
        warnings: list[str] = []
        canon = {t.name: t for t in self.tables()}
        out_tables: list[Table] = []
        seen = set()
        for t in spec.tables:
            if t.name in canon:
                c = canon[t.name].model_copy(deep=True)
                extra = {col.name for col in t.columns} - {col.name for col in c.columns}
                if extra:
                    warnings.append(f"{t.name}: columns {sorted(extra)} are not produced by the simulator and were dropped")
                if t.description:
                    c.description = t.description
                if t.row_count and c.strategy == "llm_dimension":
                    c.row_count = t.row_count
                out_tables.append(c)
                seen.add(t.name)
            else:
                if t.strategy == "simulation":
                    warnings.append(f"{t.name}: not part of module {self.id}; switched to llm_rows")
                    t = t.model_copy(update={"strategy": "llm_rows"})
                out_tables.append(t)
        for name, t in canon.items():
            if name not in seen:
                warnings.append(f"{name}: required by the simulator, added back")
                out_tables.append(t.model_copy(deep=True))
        knob_names = {k.name for k in spec.knobs}
        knobs = list(spec.knobs) + [k for k in self.knobs() if k.name not in knob_names]
        spec = spec.model_copy(update={"tables": out_tables, "knobs": knobs})
        return spec, warnings
