"""Result contracts of the causal core (validation, observation, detective, evaluation, optimisation)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .contracts import CorruptionConfig, DetectiveConfig, EvaluationConfig

Severity = Literal["error", "warning", "info"]
Status = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL"]


# =========================================================================== validation
class ValidationCheck(BaseModel):
    check_id: str
    layer: Literal["spec", "relational", "mechanistic", "statistical", "story", "observation"]
    target: str = Field("", description="mechanism / variable / table the check is about")
    expected: str = ""
    observed: str = ""
    severity: Severity = "error"
    passed: bool = True
    explanation: str = ""
    suggested_fix: str = ""


class ValidationReport(BaseModel):
    status: Status = "PASS"
    checks: list[ValidationCheck] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    def add(self, **kw: Any) -> ValidationCheck:
        c = ValidationCheck(**kw)
        self.checks.append(c)
        return c

    def finalize(self) -> "ValidationReport":
        self.status = "FAIL" if self.errors else ("PASS_WITH_WARNINGS" if self.warnings else "PASS")
        by_layer: dict[str, dict[str, int]] = {}
        for c in self.checks:
            b = by_layer.setdefault(c.layer, {"total": 0, "failed_errors": 0, "failed_warnings": 0})
            b["total"] += 1
            if not c.passed:
                b["failed_errors" if c.severity == "error" else "failed_warnings"] += 1
        self.summary = {"status": self.status, "errors": len(self.errors), "warnings": len(self.warnings),
                        "checks": len(self.checks), "by_layer": by_layer}
        return self

    def merge(self, other: "ValidationReport") -> "ValidationReport":
        self.checks += other.checks
        return self.finalize()


# =========================================================================== observed data (public)
class PublicColumn(BaseModel):
    name: str
    type: str
    description: str = ""
    nullable: bool = True


class PublicTable(BaseModel):
    name: str
    description: str = ""
    columns: list[PublicColumn]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[dict[str, str]] = Field(default_factory=list, description="{column, references}")
    rows: int = 0


class PublicSchema(BaseModel):
    """Everything the detective may know besides the observed database itself."""

    tables: list[PublicTable]
    notes: list[str] = Field(default_factory=lambda: [
        "Primary / foreign keys describe the expected logical structure; the observed data may violate them."])


class ObservedDatasetInfo(BaseModel):
    observation_id: str
    run_id: str
    world_hash: str
    observed_hash: str
    corruption_fingerprint: str
    config: CorruptionConfig
    baseline: bool = False
    realized: dict[str, Any] = Field(default_factory=dict, description="realised corruption amounts (author view)")
    checks: list[ValidationCheck] = Field(default_factory=list)
    analyzable: bool = True
    created_at: str = ""


# =========================================================================== detective
EdgeType = Literal["directed", "undirected", "bidirected", "uncertain", "none"]


class InferredRelationship(BaseModel):
    source: str = Field(description="'table.column'")
    target: str = Field(description="'table.column'")
    edge_type: EdgeType = "directed"
    estimated_direction: Literal["+", "-", "mixed", "unknown"] = "unknown"
    estimated_strength: Optional[float] = None
    optional_lag: Optional[int] = None
    confidence: float = Field(0.5, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    identification_status: Literal["identified", "partially_identified", "not_identified", "unknown"] = "unknown"
    note: str = ""


class KeyEvent(BaseModel):
    description: str
    role: Literal["root_cause", "consequence", "distractor", "other"] = "other"
    variable: Optional[str] = Field(None, description="'table.column' where the event is visible")
    entity: Optional[str] = Field(None, description="'table.column' key identifying affected entities, e.g. 'products.supplier_id'")
    entity_ids: list[int] = Field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    direction: Literal["+", "-", "unknown"] = "unknown"
    confidence: float = Field(0.5, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class EstimatedEffect(BaseModel):
    source: str
    target: str
    effect_type: Literal["direct", "total", "conditional", "ate", "association"] = "direct"
    estimate: float
    standard_error: Optional[float] = None
    adjustment_set: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    evidence_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    summary: str = ""
    result: Any = None


class DetectiveResult(BaseModel):
    story_summary: str = ""
    key_events: list[KeyEvent] = Field(default_factory=list)
    cause_candidates: list[dict[str, Any]] = Field(default_factory=list)
    inferred_causal_graph: dict[str, Any] = Field(default_factory=dict, description="{nodes, edges}")
    inferred_relationships: list[InferredRelationship] = Field(default_factory=list)
    estimated_effects: list[EstimatedEffect] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list, description="evidence ids backing the summary")
    executed_queries: list[str] = Field(default_factory=list)
    statistical_results: list[EvidenceItem] = Field(default_factory=list, description="harness-recorded tool log")
    discovered_data_quality_issues: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    agent: dict[str, Any] = Field(default_factory=dict, description="kind, model, tools, budget, mock flag")


# =========================================================================== evaluation
class MetricScore(BaseModel):
    name: str
    score: Optional[float] = None
    status: Literal["ok", "not_evaluable"] = "ok"
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    evaluation_id: str = ""
    observation_id: str = ""
    status: Literal["ok", "not_evaluable"] = "ok"
    reason: str = ""
    recovery_score: Optional[float] = None
    difficulty: Optional[float] = None
    metrics: dict[str, MetricScore] = Field(default_factory=dict)
    weights_used: dict[str, float] = Field(default_factory=dict)
    graph_metrics: dict[str, Any] = Field(default_factory=dict)
    story_facts: list[dict[str, Any]] = Field(default_factory=list)
    effects: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    detective: dict[str, Any] = Field(default_factory=dict)
    config: EvaluationConfig = Field(default_factory=EvaluationConfig)


class DifficultyEstimate(BaseModel):
    """Aggregate of repeated, independent detective evaluations of one observed configuration."""

    status: Literal["ok", "not_evaluable"] = "ok"
    reason: str = ""
    mean_difficulty: Optional[float] = None
    std_difficulty: Optional[float] = None
    mean_recovery: Optional[float] = None
    scores: list[float] = Field(default_factory=list)
    n_evaluations: int = 0
    preliminary: bool = True
    agent: dict[str, Any] = Field(default_factory=dict)
    evaluation_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict, description="LLM calls / tokens / cost of these evaluations")


class ManualEvaluationResult(BaseModel):
    mode: Literal["manual"] = "manual"
    run_id: str
    observation_id: str
    corruption: CorruptionConfig
    realized_corruption: dict[str, Any] = Field(default_factory=dict)
    baseline: Optional[DifficultyEstimate] = None
    observed: DifficultyEstimate
    difficulty_delta: Optional[float] = None
    detective_config: DetectiveConfig
    usage: dict[str, Any] = Field(default_factory=dict, description="total LLM usage (baseline + observed)")


class Candidate(BaseModel):
    candidate_id: str
    iteration: int
    theta: dict[str, float]
    corruption: CorruptionConfig
    structural_noise_scale: float = 1.0
    world_hash: str = ""
    observation_id: str = ""
    realized_share: float = 0.0
    estimate: Optional[DifficultyEstimate] = None
    objective: Optional[float] = None
    rejected: bool = False
    rejection_reason: str = ""
    phase: Literal["screening", "final"] = "screening"


class OptimizationResult(BaseModel):
    mode: Literal["automatic"] = "automatic"
    run_id: str
    status: Literal["target_reached", "target_not_reached", "unreachable_below_baseline", "cancelled", "error",
                    "budget_exhausted"]
    reason: str = ""
    target_difficulty: float
    tolerance: float
    achieved_difficulty: Optional[float] = None
    achieved_std: Optional[float] = None
    baseline: Optional[DifficultyEstimate] = None
    difficulty_delta: Optional[float] = None
    n_evaluations: int = 0
    iterations: int = 0
    history: list[Candidate] = Field(default_factory=list)
    selected: Optional[Candidate] = None
    selected_corruption: Optional[CorruptionConfig] = None
    realized_corruption: dict[str, Any] = Field(default_factory=dict)
    termination_reason: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    agent: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict, description="total LLM usage of the optimisation")
