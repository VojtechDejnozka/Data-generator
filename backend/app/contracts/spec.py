"""ScenarioSpec – the central contract between planner, generator, validator and UI.

The models double as JSON schemas for OpenAI / Gemini structured output, so they are kept
reasonably flat: no free-form dicts, values that may be of several types are strings.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from ..causal.contracts import SCMSpec

IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

ColumnType = Literal["int", "float", "text", "date", "datetime", "bool"]
Strategy = Literal["simulation", "llm_rows", "llm_dimension"]
ChartType = Literal["line", "bar", "stacked_bar", "area", "scatter", "pie", "table", "kpi", "heatmap"]


class Column(BaseModel):
    name: str
    type: ColumnType = "text"
    description: str = ""
    pk: bool = False
    fk: Optional[str] = Field(None, description="Referenced 'table.column' or null")
    nullable: bool = False
    enum_values: list[str] = Field(default_factory=list, description="Allowed values for categorical text columns")
    min: Optional[float] = None
    max: Optional[float] = None

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        v = v.strip().lower()
        if not IDENT.match(v):
            raise ValueError(f"invalid column name '{v}' (use snake_case ascii)")
        return v

    @field_validator("fk")
    @classmethod
    def _fk(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, "", "null"):
            return None
        v = v.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", v):
            raise ValueError(f"fk must be 'table.column', got '{v}'")
        return v


class Table(BaseModel):
    name: str
    description: str = ""
    strategy: Strategy = "llm_rows"
    row_count: int = Field(0, description="Target row count (0 = decided by the simulator)")
    columns: list[Column]
    flags: list[str] = Field(default_factory=list, description="e.g. 'dimension', 'fact', 'event', 'story_affected'")

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        v = v.strip().lower()
        if not IDENT.match(v):
            raise ValueError(f"invalid table name '{v}'")
        return v

    @property
    def pk(self) -> list[str]:
        return [c.name for c in self.columns if c.pk]

    def column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)


class CausalStep(BaseModel):
    id: str
    mechanism: str = Field(description="What happens, e.g. 'Supplier X delivers late'")
    affected_entities: list[str] = Field(default_factory=list, description="e.g. 'suppliers:3', 'products of supplier 3'")
    tables: list[str] = Field(default_factory=list, description="Tables where this step is visible")
    start: Optional[str] = Field(None, description="ISO date or relative ('after step s1')")
    ramp_profile: str = Field("linear", description="step | linear | exponential | sigmoid | lagged")
    expected_impact: str = ""
    lag_days: int = 0


class Distractor(BaseModel):
    id: str
    name: str
    kind: str = Field("event", description="seasonality | event | noise | red_herring")
    description: str = ""
    tables: list[str] = Field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    entities: list[str] = Field(default_factory=list)


class Story(BaseModel):
    logline: str
    causal_chain: list[CausalStep]
    ground_truth_facts: list[str] = Field(default_factory=list)
    distractors: list[Distractor] = Field(default_factory=list)


class Knob(BaseModel):
    name: str
    label: str = ""
    type: Literal["int", "float", "enum", "date", "bool", "entity"] = "float"
    default: str = Field(description="Default value encoded as string")
    min: Optional[float] = None
    max: Optional[float] = None
    options: list[str] = Field(default_factory=list)
    description: str = ""
    applies_to: list[str] = Field(default_factory=list, description="Causal steps / tables affected")

    def parse(self, raw: object) -> object:
        """Convert a raw (string/number) value into the typed knob value, clamped to range."""
        if raw is None:
            raw = self.default
        if self.type in ("int", "entity"):
            v: object = int(float(raw))
        elif self.type == "float":
            v = float(raw)
        elif self.type == "bool":
            v = str(raw).strip().lower() in {"1", "true", "yes", "ano"}
        elif self.type == "date":
            v = dt.date.fromisoformat(str(raw)[:10]).isoformat()
        else:
            v = str(raw)
            if self.options and v not in self.options:
                raise ValueError(f"knob {self.name}: '{v}' not in {self.options}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if self.min is not None and v < self.min:
                v = type(v)(self.min)
            if self.max is not None and v > self.max:
                v = type(v)(self.max)
        return v


InvariantKind = Literal["fk", "unique", "not_null", "range", "compare", "row_count", "sql_zero_rows"]


class Invariant(BaseModel):
    id: str
    kind: InvariantKind
    description: str = ""
    table: Optional[str] = None
    column: Optional[str] = None
    ref: Optional[str] = Field(None, description="fk: referenced 'table.column'")
    other_column: Optional[str] = Field(None, description="compare: right-hand column")
    op: Optional[Literal[">=", "<=", ">", "<", "=", "!="]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    sql: Optional[str] = Field(None, description="sql_zero_rows: SELECT returning violating rows")
    severity: Literal["error", "warning"] = "error"


class EvidenceQuery(BaseModel):
    id: str
    title: str
    step_id: Optional[str] = Field(None, description="Causal step this evidence supports")
    sql: str = Field(description="Read-only SELECT; may use {{knob:name}} placeholders")
    expected: str = Field("", description="Expected behaviour if the story is present")
    chart: ChartType = "line"
    x: Optional[str] = None
    y: list[str] = Field(default_factory=list)
    series: Optional[str] = Field(None, description="Long-format column that splits series")
    caption: str = ""


class Widget(BaseModel):
    id: str
    title: str
    sql: str
    chart: ChartType = "line"
    x: Optional[str] = None
    y: list[str] = Field(default_factory=list)
    series: Optional[str] = None
    description: str = ""
    width: Literal[1, 2] = 1


class Dashboard(BaseModel):
    title: str = "Dashboard"
    widgets: list[Widget] = Field(default_factory=list)


class Meta(BaseModel):
    name: str
    domain: str = "retail"
    data_language: str = "cs"
    period_start: Optional[str] = Field(None, description="ISO date; null if the data has no time dimension")
    period_end: Optional[str] = Field(None, description="ISO date; null if the data has no time dimension")
    seed: int = 42
    archetype: Optional[str] = Field(None, description="Scenario module id if a simulator fits, else null")
    company: str = ""
    environment: str = ""

    @field_validator("period_start", "period_end")
    @classmethod
    def _date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, "", "null"):
            return None
        return dt.date.fromisoformat(v[:10]).isoformat()

    @property
    def has_time(self) -> bool:
        return bool(self.period_start and self.period_end)


class ScenarioSpec(BaseModel):
    meta: Meta
    tables: list[Table]
    story: Story
    knobs: list[Knob] = Field(default_factory=list)
    invariants: list[Invariant] = Field(default_factory=list)
    evidence: list[EvidenceQuery] = Field(default_factory=list)
    dashboard: Dashboard = Field(default_factory=Dashboard)
    # Optional executable structural causal model (app.causal). When present it is the single source
    # of truth for generation; `tables` are derived from it. Hidden from LLM structured-output schemas.
    scm: SkipJsonSchema[Optional[SCMSpec]] = None

    @model_validator(mode="after")
    def _consistency(self) -> "ScenarioSpec":
        names = [t.name for t in self.tables]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate tables: {sorted(dup)}")
        if bool(self.meta.period_start) != bool(self.meta.period_end):
            raise ValueError("set both period_start and period_end, or neither (no time dimension)")
        if self.meta.has_time and self.meta.period_end <= self.meta.period_start:
            raise ValueError("period_end must be after period_start")
        return self

    # -- helpers -------------------------------------------------------------
    def table(self, name: str) -> Optional[Table]:
        return next((t for t in self.tables if t.name == name), None)

    def knob(self, name: str) -> Optional[Knob]:
        return next((k for k in self.knobs if k.name == name), None)

    def knob_values(self, overrides: dict | None = None) -> dict[str, object]:
        overrides = overrides or {}
        return {k.name: k.parse(overrides.get(k.name, k.default)) for k in self.knobs}

    def structural_errors(self) -> list[str]:
        """Problems that make the spec ungeneratable (FK to unknown tables, missing PK, cycles)."""
        errs: list[str] = []
        by_name = {t.name: t for t in self.tables}
        for t in self.tables:
            if not t.pk:
                errs.append(f"table {t.name}: no primary key")
            for c in t.columns:
                if c.fk:
                    rt, rc = c.fk.split(".")
                    if rt not in by_name:
                        errs.append(f"{t.name}.{c.name}: FK to unknown table {rt}")
                    elif not by_name[rt].column(rc):
                        errs.append(f"{t.name}.{c.name}: FK to unknown column {c.fk}")
        try:
            self.topological_order()
        except ValueError as e:
            errs.append(str(e))
        return errs

    def topological_order(self) -> list[str]:
        known = {t.name for t in self.tables}
        pending = {
            t.name: {c.fk.split(".")[0] for c in t.columns if c.fk} & known - {t.name}
            for t in self.tables
        }
        order: list[str] = []
        while pending:
            ready = sorted(n for n, d in pending.items() if d <= set(order))
            if not ready:
                raise ValueError(f"FK cycle among tables: {sorted(pending)}")
            order.extend(ready)
            for n in ready:
                pending.pop(n)
        return order


def spec_json(spec: ScenarioSpec) -> str:
    return json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=1)
