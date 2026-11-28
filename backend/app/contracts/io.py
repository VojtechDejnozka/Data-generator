"""Contracts for user input, planner output, generation plan and validation verdict."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .spec import CausalStep, Column, Distractor, Knob, ScenarioSpec, Table


# --------------------------------------------------------------------------- input
class ComplexityTargets(BaseModel):
    tables: Optional[int] = Field(None, ge=1)
    variables: Optional[int] = Field(None, ge=1)
    entity_instances: Optional[int] = Field(None, ge=1)
    rows: Optional[int] = Field(None, ge=1)


class ObservationSettings(BaseModel):
    mode: Literal["automatic", "manual"] = "automatic"
    target: float = Field(50.0, ge=0, le=100)
    missing_percent: float = Field(0.0, ge=0, le=100)
    noise_percent: float = Field(0.0, ge=0, allow_inf_nan=False)


class ScenarioInput(BaseModel):
    """Story, planning hints and optional settings for the observed dataset."""

    story: str = Field(..., min_length=10, max_length=8000)
    volume: Literal["small", "medium", "large"] = "medium"
    seed: int = 42
    company: str = Field("", max_length=4000)
    environment: str = Field("", max_length=4000)
    entity_counts: dict[str, int] = Field(default_factory=dict)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    story_intensity: Optional[float] = Field(None, ge=0, le=2)
    custom_model: str = Field("", max_length=8000, description="Optional user data model / ontology")
    sql_schema: str = Field("", max_length=50000)
    ontology: str = Field("", max_length=50000)
    complexity: Optional[ComplexityTargets] = None
    observation: Optional[ObservationSettings] = None
    data_language: str = "cs"
    preset: Optional[str] = None
    engine: Literal["module", "scm"] = Field(
        "module", description="'scm' = plan an executable structural causal model (app.causal) instead of a module")


# --------------------------------------------------------------------------- planner
class Question(BaseModel):
    id: str
    question: str
    why: str = ""
    default_answer: str = ""


class ColumnPatch(BaseModel):
    table: str
    column: Column


class Suggestion(BaseModel):
    id: str
    kind: Literal["add_table", "add_column", "add_distractor", "add_causal_step", "add_knob", "other"]
    title: str
    description: str = ""
    table: Optional[Table] = None
    column: Optional[ColumnPatch] = None
    distractor: Optional[Distractor] = None
    causal_step: Optional[CausalStep] = None
    knob: Optional[Knob] = None


class PlannerResponse(BaseModel):
    spec: ScenarioSpec
    questions: list[Question] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    notes: str = ""


class ReplanRequest(BaseModel):
    spec: ScenarioSpec
    feedback: str = Field("", max_length=4000)
    answers: dict[str, str] = Field(default_factory=dict)
    accepted_suggestions: list[str] = Field(default_factory=list)
    rejected_suggestions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- plan
class WorkPackage(BaseModel):
    id: str
    tables: list[str]
    strategy: Literal["simulation", "llm_rows", "llm_dimension"]
    depends_on: list[str] = Field(default_factory=list)
    module: Optional[str] = Field(None, description="Scenario module id for strategy=simulation")
    batch_size: int = Field(25, ge=1, le=200)
    instructions: str = ""


class GenerationPlan(BaseModel):
    work_packages: list[WorkPackage]
    notes: str = ""


# --------------------------------------------------------------------------- validation
class RepairAction(BaseModel):
    action: Literal["none", "regenerate_table", "adjust_param"] = "none"
    table: Optional[str] = None
    param: Optional[str] = None
    value: Optional[str] = None
    instructions: str = ""


class Issue(BaseModel):
    id: str
    severity: Literal["critical", "major", "minor"]
    table: Optional[str] = None
    description: str
    suggested_fix: str = ""
    repair: RepairAction = Field(default_factory=RepairAction)


class StepDetectability(BaseModel):
    step_id: str
    detectable: bool
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    comment: str = ""


class ValidationVerdict(BaseModel):
    verdict: Literal["pass", "warn", "fail"]
    summary: str
    story_detectability: list[StepDetectability] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


# --------------------------------------------------------------------------- runs
class RunRequest(BaseModel):
    spec: ScenarioSpec
    knob_overrides: dict[str, str] = Field(default_factory=dict)
    parent_run_id: Optional[str] = None
    label: str = ""
    repair: bool = True
    input: Optional[ScenarioInput] = None


class WhatIfRequest(BaseModel):
    knob_overrides: dict[str, str]
    label: str = ""


class QueryRequest(BaseModel):
    sql: str = Field(..., max_length=20000)
    limit: int = Field(500, ge=1, le=5000)
