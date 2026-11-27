"""Typed, versioned contracts of the causal core.

``SCMSpec`` is attached to the existing ``ScenarioSpec`` as its optional ``scm`` section (see
``app/contracts/spec.py``); it is the single source of truth for the executable structural causal
model. The relational tables of ``ScenarioSpec.tables`` are *derived* from it (see
``app.causal.mapping``), so the existing UI, data browser and checks keep working.

Design notes
------------
* Every variable defines its mechanism on the target node: ``X_i = f_i(PA_i, U_i)``. Causal edges are
  derived from the declared parents (``derive_edges``); ``edges`` only annotate them and must not
  contain a second, possibly different, definition of a coefficient.
* Grains: ``base`` entities have a fixed count; ``cross`` entities are cartesian products of other
  entities (optionally × time = panel); ``child`` entities have a parent entity and a number of rows
  per parent row given by a constant or by an integer variable on the parent grain.
* Time: ``TimeConfig`` defines discrete steps. Temporal grains carry the step index. Feedback loops
  are expressed through lagged parents only; a cycle inside one step is rejected.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .distributions import Distribution

SCHEMA_VERSION = "1.0"
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _ident(v: str, what: str) -> str:
    v = (v or "").strip()
    if not IDENT.match(v):
        raise ValueError(f"invalid {what} '{v}' (snake_case ascii, starts with a letter)")
    return v


# =========================================================================== metadata / time
class TimeConfig(BaseModel):
    start: str = Field(description="ISO date of the first step")
    periods: int = Field(ge=1, le=3660, description="number of time steps")
    frequency: Literal["D", "W", "M"] = "D"
    time_column: str = "date"

    @field_validator("start")
    @classmethod
    def _date(cls, v: str) -> str:
        return dt.date.fromisoformat(str(v)[:10]).isoformat()

    def dates(self) -> list[str]:
        d0 = dt.date.fromisoformat(self.start)
        out = []
        for i in range(self.periods):
            if self.frequency == "D":
                out.append(d0 + dt.timedelta(days=i))
            elif self.frequency == "W":
                out.append(d0 + dt.timedelta(weeks=i))
            else:
                y, m = divmod(d0.month - 1 + i, 12)
                out.append(dt.date(d0.year + y, m + 1, min(d0.day, 28)))
        return [d.isoformat() for d in out]

    def step_of(self, value: str | int | None, default: int) -> int:
        """Convert an ISO date or an integer step to a step index (clamped to the range)."""
        if value is None or value == "":
            return default
        if isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit()):
            return int(value)
        d = dt.date.fromisoformat(str(value)[:10])
        dates = [dt.date.fromisoformat(x) for x in self.dates()]
        for i, x in enumerate(dates):
            if x >= d:
                return i
        return len(dates)


class SCMMeta(BaseModel):
    scenario_id: str = "scenario"
    name: str = "Scenario"
    description: str = ""
    domain: str = ""
    original_story: str = ""
    refined_story: str = ""
    language: str = "cs"
    seed: int = 42
    schema_version: str = SCHEMA_VERSION
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# =========================================================================== entities
class Assignment(BaseModel):
    """Many-to-one relation of an entity to another entity, realised as an FK column."""

    entity: str = Field(description="referenced entity_id")
    column: Optional[str] = Field(None, description="FK column name (default: referenced primary key)")
    method: Literal["uniform", "balanced", "distinct_per_parent"] = "balanced"

    description: str = ""


class Relationship(BaseModel):
    """Descriptive relationship metadata (cardinalities are enforced by the grain definitions)."""

    target: str
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    via: Optional[str] = Field(None, description="junction entity for many-to-many")
    description: str = ""


class EntitySpec(BaseModel):
    entity_id: str
    name: str = Field("", description="table name (defaults to entity_id)")
    description: str = ""
    grain: str = Field("", description="what exactly one row represents")
    kind: Literal["base", "cross", "child"] = "base"
    count: Optional[int] = Field(None, ge=0, le=2_000_000, description="base: number of rows; child: rows per parent")
    count_variable: Optional[str] = Field(None, description="child: integer variable on the parent grain")
    parent: Optional[str] = Field(None, description="child: parent entity_id")
    cross: list[str] = Field(default_factory=list, description="cross: member entity_ids")
    temporal: bool = Field(False, description="cross: one row per member combination and time step")
    primary_key: str = ""
    assignments: list[Assignment] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    export: bool = Field(True, description="materialise as a table of the dataset")
    public_description: str = Field("", description="neutral description that may be shown to analysts")

    @field_validator("entity_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _ident(v, "entity_id")

    @model_validator(mode="after")
    def _defaults(self) -> "EntitySpec":
        if not self.name:
            self.name = self.entity_id
        _ident(self.name, "table name")
        if not self.primary_key:
            base = self.entity_id[:-1] if self.entity_id.endswith("s") else self.entity_id
            self.primary_key = f"{base}_id"
        _ident(self.primary_key, "primary key")
        return self

    @property
    def temporal_scope(self) -> str:
        return "time_varying" if self.temporal else "static"


# =========================================================================== variables
class ParentRef(BaseModel):
    variable: str
    alias: Optional[str] = Field(None, description="name used in the mechanism (default: variable id)")
    lag: int = Field(0, ge=0, le=365, description="time lag in steps (temporal grains)")
    aggregation: Optional[Literal["sum", "mean", "count", "min", "max", "any"]] = Field(
        None, description="required when the parent lives on a finer grain")
    fill_value: float = Field(0.0, description="aggregate value for rows without children / before the first step")

    @property
    def name(self) -> str:
        return self.alias or self.variable


class Mechanism(BaseModel):
    type: str = Field(description="mechanism id from the registry, e.g. linear, logistic, categorical_cpt")
    params: dict[str, Any] = Field(default_factory=dict)


class Constraints(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None
    clip: bool = Field(False, description="clip realisations into [min, max] (part of the mechanism)")
    round: Optional[int] = Field(None, description="round to this many decimals")


SemanticType = Literal["continuous", "count", "binary", "categorical", "ordinal", "text", "label"]
DataType = Literal["float", "int", "bool", "category", "text"]


class VariableSpec(BaseModel):
    variable_id: str
    name: str = ""
    description: str = ""
    public_description: str = Field("", description="neutral column description for analysts (no story)")
    semantic_type: SemanticType = "continuous"
    data_type: DataType = "float"
    unit: str = ""
    categories: list[str] = Field(default_factory=list, description="domain of categorical / ordinal variables")
    entity: str = Field(description="entity_id of the grain the variable lives on")
    column: Optional[str] = Field(None, description="column in the entity table; null = not materialised")
    parents: list[ParentRef] = Field(default_factory=list)
    mechanism: Mechanism
    initial_state: Optional[float | str] = Field(None, description="value of lagged reads before the first step")
    observability: Literal["observed", "latent"] = "observed"
    role: Literal["state", "auxiliary", "label", "text"] = "state"
    constraints: Constraints = Field(default_factory=Constraints)

    @field_validator("variable_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _ident(v, "variable_id")

    @model_validator(mode="after")
    def _defaults(self) -> "VariableSpec":
        if not self.name:
            self.name = self.variable_id
        if self.column is not None:
            _ident(self.column, "column")
        return self

    @property
    def is_deterministic(self) -> bool:
        from .mechanisms import is_deterministic
        return is_deterministic(self)


class CausalEdge(BaseModel):
    """Derived / annotated edge. The mechanism (and any coefficient) lives on the target node."""

    source: str
    target: str
    relationship_description: str = ""
    mechanism_reference: str = ""
    optional_coefficient: Optional[float] = Field(None, description="read-only copy of the target's coefficient")
    effect_direction: Literal["+", "-", "mixed", "unknown"] = "unknown"
    lag: int = 0
    entity_mapping: str = Field("", description="same_grain | lookup | aggregation")
    aggregation: Optional[str] = None
    identifiability_notes: str = ""


# =========================================================================== events
class EventSpec(BaseModel):
    event_id: str
    description: str = ""
    affected_variables: list[str]
    affected_entities: Optional[str] = Field(None, description="row filter expression, e.g. 'supplier_id == 2'")
    start_time: Optional[str | int] = Field(None, description="ISO date or step index (default: first step)")
    end_time: Optional[str | int] = Field(None, description="ISO date or step index, exclusive (default: open)")
    intervention_type: Literal["set_value", "shift", "scale", "param_change", "seasonal"]
    intervention_parameters: dict[str, Any] = Field(default_factory=dict)
    ramp_profile: Literal["step", "linear", "exponential", "sigmoid"] = "step"
    ramp_periods: int = Field(0, ge=0)
    kind: Literal["story", "distractor", "seasonality", "control"] = "story"


# =========================================================================== ground truth
class StoryFact(BaseModel):
    fact_id: str
    description: str
    importance: float = Field(1.0, ge=0)
    kind: Literal["edge", "event", "root_cause", "affected_entities", "lag", "direction"] = "edge"
    source: Optional[str] = Field(None, description="variable_id")
    target: Optional[str] = Field(None, description="variable_id")
    event_id: Optional[str] = None
    expected_direction: Optional[Literal["+", "-"]] = None
    expected_lag: Optional[int] = None
    entity_ids: dict[str, list[int]] = Field(default_factory=dict, description="entity_id -> affected ids")
    evidence_variables: list[str] = Field(default_factory=list,
                                          description="observed variables where an event is visible (default: derived)")
    tolerance_periods: int = 14


class ControlGroup(BaseModel):
    description: str
    entity: str
    filter: str


class GroundTruthSpec(BaseModel):
    story_facts: list[StoryFact] = Field(default_factory=list)
    key_causes: list[str] = Field(default_factory=list)
    key_effects: list[str] = Field(default_factory=list)
    control_groups: list[ControlGroup] = Field(default_factory=list)
    alternative_interpretations: list[str] = Field(default_factory=list)
    effects_to_estimate: list[tuple[str, str]] = Field(
        default_factory=list, description="(source, target) pairs whose interventional total effect is computed")


# =========================================================================== corruption / observation
CorruptionType = Literal[
    "missing_values_mcar", "missing_values_mar", "missing_values_mnar", "gaussian_measurement_noise",
    "multiplicative_measurement_noise", "outliers", "rounding", "category_misclassification", "exact_duplicates",
    "near_duplicates", "column_value_shuffle", "timestamp_shift", "text_typos", "text_truncation",
    "inconsistent_formatting", "incorrect_foreign_keys", "hidden_columns", "sampling", "row_deletion"]


class CorruptionOp(BaseModel):
    op_id: str = ""
    type: CorruptionType
    target_table: str = Field(description="table name ('*' only inside a search space template)")
    target_columns: list[str] = Field(default_factory=list)
    row_filter: Optional[str] = Field(None, description="expression over the observed table columns")
    probability: float = Field(0.0, ge=0, le=1, description="share of eligible cells/rows (or intensity)")
    params: dict[str, Any] = Field(default_factory=dict)
    seed: Optional[int] = Field(None, description="default: derived from the run seed and op identity")
    optional_condition: Optional[str] = None
    protected_columns: list[str] = Field(default_factory=list)
    operation_order: int = 0

    @model_validator(mode="after")
    def _id(self) -> "CorruptionOp":
        if not self.op_id:
            self.op_id = f"{self.type}:{self.target_table}:{','.join(self.target_columns)}"
        return self


class CorruptionConfig(BaseModel):
    operations: list[CorruptionOp] = Field(default_factory=list)
    protect_keys: bool = Field(True, description="never corrupt primary keys / FKs unless an op targets them explicitly")
    enforce_constraints: bool = Field(False, description="write the observed DB with PK/FK constraints")
    protected_tables: list[str] = Field(default_factory=list)
    protected_columns: list[str] = Field(default_factory=list, description="'table.column'")
    seed: Optional[int] = None

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]


# =========================================================================== detective / evaluation / difficulty
class DetectiveConfig(BaseModel):
    kind: Literal["statistical", "llm"] = "statistical"
    model_role: Literal["strong", "cheap"] = "strong"
    max_steps: int = Field(14, ge=1, le=60)
    max_queries: int = Field(400, ge=1, le=2000, description="tool-level SQL budget")
    sql_timeout_s: float = 10.0
    max_result_rows: int = 200
    alpha: float = Field(0.01, gt=0, lt=0.5, description="significance level used by discovery / tests")
    sample_rows: int = Field(30000, ge=100)
    seed: int = 0
    task: str = Field("Zjisti, co se v datech děje: klíčové události, zasažené entity, kauzální vztahy a jejich směr. "
                      "Každé tvrzení podlož výsledkem analytického nástroje.", description="generic analytic task")


class EvaluationConfig(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: {
        "story": 0.40, "skeleton": 0.25, "orientation": 0.10, "effects": 0.15, "evidence": 0.10})
    redistribution: Literal["proportional"] = "proportional"
    effect_relative_tolerance: float = 1.0
    timing_tolerance_periods: int = 14
    use_llm_judge: bool = False


class SearchDimension(BaseModel):
    """One tunable parameter of the corruption search space (the op's ``probability`` / intensity).

    ``op.target_table == "*"`` expands to every eligible exported table (see ``difficulty.expand_op``)."""

    dim_id: str = ""
    op: CorruptionOp
    low: float = 0.0
    high: float = 0.5
    locked_value: Optional[float] = Field(None, description="keep fixed, optimise only the others")

    @model_validator(mode="after")
    def _id(self) -> "SearchDimension":
        if not self.dim_id:
            self.dim_id = self.op.op_id if self.op.target_table != "*" else self.op.type
        return self


class DifficultyConfig(BaseModel):
    mode: Literal["automatic", "manual"] = "automatic"
    target_difficulty: float = Field(50.0, ge=0, le=100)
    difficulty_tolerance: float = Field(5.0, ge=0, le=50)
    allowed_corruption_types: list[CorruptionType] = Field(default_factory=lambda: [
        "missing_values_mcar", "gaussian_measurement_noise", "category_misclassification", "exact_duplicates",
        "column_value_shuffle"])
    corruption_parameters: CorruptionConfig = Field(default_factory=CorruptionConfig,
                                                    description="manual mode configuration / automatic start point")
    parameter_bounds: dict[str, tuple[float, float]] = Field(default_factory=dict, description="type -> (low, high)")
    search_space: list[SearchDimension] = Field(default_factory=list, description="explicit space (default: derived)")
    locked_parameters: dict[str, float] = Field(default_factory=dict,
                                                description="dimension id (e.g. corruption type) -> fixed value")
    protected_columns: list[str] = Field(default_factory=list, description="'table.column'")
    protected_tables: list[str] = Field(default_factory=list)
    maximum_corruption_budget: float = Field(0.35, ge=0, le=1, description="max share of modified cells")
    minimum_observability: float = Field(0.5, ge=0, le=1,
                                         description="min share of non-missing cells in story-critical columns")
    allow_structural_noise_optimization: bool = False
    structural_noise_bounds: tuple[float, float] = (1.0, 3.0)
    structural_noise_variables: list[str] = Field(default_factory=list)
    max_iterations: int = Field(8, ge=1, le=200)
    screening_repeats: int = Field(1, ge=1, le=10)
    evaluation_repeats: int = Field(3, ge=1, le=20)
    final_candidates: int = Field(2, ge=1, le=10)
    token_budget: Optional[int] = None
    evaluation_budget: Optional[int] = Field(None, description="max detective evaluations in total")
    time_budget_s: Optional[float] = None
    corruption_penalty: float = Field(10.0, ge=0, description="objective penalty per unit of corrupted share")
    uncertainty_penalty: float = Field(0.5, ge=0)
    screening_detective: DetectiveConfig = Field(default_factory=DetectiveConfig)
    final_detective: Optional[DetectiveConfig] = None
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    seed: int = 0


# =========================================================================== SCM spec
class SCMSpec(BaseModel):
    meta: SCMMeta = Field(default_factory=SCMMeta)
    time: Optional[TimeConfig] = None
    entities: list[EntitySpec]
    variables: list[VariableSpec]
    edges: list[CausalEdge] = Field(default_factory=list, description="annotations of derived edges")
    events: list[EventSpec] = Field(default_factory=list)
    invariants: list[dict[str, Any]] = Field(default_factory=list,
                                            description="extra invariants in the ScenarioSpec Invariant format")
    ground_truth: GroundTruthSpec = Field(default_factory=GroundTruthSpec)
    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)

    # -- lookups -------------------------------------------------------------
    def entity(self, eid: str) -> Optional[EntitySpec]:
        return next((e for e in self.entities if e.entity_id == eid), None)

    def variable(self, vid: str) -> Optional[VariableSpec]:
        return next((v for v in self.variables if v.variable_id == vid), None)

    def event(self, eid: str) -> Optional[EventSpec]:
        return next((e for e in self.events if e.event_id == eid), None)

    def variables_of(self, eid: str) -> list[VariableSpec]:
        return [v for v in self.variables if v.entity == eid]

    def column_of(self, vid: str) -> Optional[tuple[str, str]]:
        v = self.variable(vid)
        if not v or not v.column:
            return None
        e = self.entity(v.entity)
        return (e.name, v.column) if e else None

    def fingerprint(self) -> str:
        """Hash of everything that defines the clean reality (not difficulty / detective settings)."""
        d = self.model_dump(mode="json", exclude={"difficulty"})
        return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def to_yaml(self) -> str:
        import yaml
        return yaml.safe_dump(self.model_dump(mode="json"), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> "SCMSpec":
        import yaml
        return cls.model_validate(yaml.safe_load(text))


def derive_edges(spec: SCMSpec) -> list[CausalEdge]:
    """Edges implied by the declared parents (single source of truth = target mechanisms)."""
    from .mechanisms import coefficient_of

    ann = {(e.source, e.target): e for e in spec.edges}
    out: list[CausalEdge] = []
    for v in spec.variables:
        for p in v.parents:
            src = spec.variable(p.variable)
            mapping = "same_grain"
            if src and src.entity != v.entity:
                mapping = "aggregation" if p.aggregation else "lookup"
            coef = coefficient_of(v, p.name)
            direction = ann.get((p.variable, v.variable_id)).effect_direction if (p.variable, v.variable_id) in ann \
                else ("+" if coef is not None and coef > 0 else "-" if coef is not None and coef < 0 else "unknown")
            a = ann.get((p.variable, v.variable_id))
            out.append(CausalEdge(
                source=p.variable, target=v.variable_id, lag=p.lag, aggregation=p.aggregation,
                mechanism_reference=f"{v.variable_id}:{v.mechanism.type}", optional_coefficient=coef,
                effect_direction=direction, entity_mapping=mapping,
                relationship_description=a.relationship_description if a else "",
                identifiability_notes=a.identifiability_notes if a else ""))
    return out
