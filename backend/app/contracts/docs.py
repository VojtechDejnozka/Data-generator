"""Contracts for the document pipeline (unstructured outputs: PDF reports, letters, e-mails, …).

Documents are written from structured *facts* (one row per document) so that content is consistent
and checkable; an optional story is injected deterministically into a seeded subset of facts.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .spec import IDENT, ColumnType

DocFormat = Literal["pdf", "html", "md", "txt", "json", "eml"]


class DocInput(BaseModel):
    description: str = Field(..., min_length=10, max_length=8000, description="What documents to generate")
    count: int = Field(20, ge=1, le=500)
    format: DocFormat = "pdf"
    seed: int = 42
    source_run_id: Optional[str] = Field(None, description="Ground facts in rows of an existing data run")
    source_table: Optional[str] = None
    data_language: str = "cs"


class FactField(BaseModel):
    name: str
    type: ColumnType = "text"
    description: str = ""
    enum_values: list[str] = Field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    must_mention: bool = Field(False, description="The value must appear verbatim in the document text")

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        v = v.strip().lower()
        if not IDENT.match(v):
            raise ValueError(f"invalid field name '{v}'")
        return v


class DocSection(BaseModel):
    title: str
    kind: Literal["text", "list", "key_values", "table"] = "text"
    instructions: str = ""


class FactOverride(BaseModel):
    field: str
    op: Literal["set", "multiply", "add"] = "set"
    value: str


class DocStory(BaseModel):
    """A hidden pattern injected into a seeded subset of documents (traceable via ground truth)."""

    logline: str
    affected_share: float = Field(0.25, ge=0, le=1)
    overrides: list[FactOverride] = Field(default_factory=list, description="Fact changes for affected documents")
    writer_hint: str = Field("", description="What the writer should reflect for affected documents (subtly)")


class DocSpec(BaseModel):
    name: str
    document_type: str = Field(description="e.g. 'lékařská zpráva', 'reklamační dopis'")
    description: str = ""
    data_language: str = "cs"
    format: DocFormat = "pdf"
    count: int = Field(20, ge=1, le=500)
    seed: int = 42
    style: str = Field("", description="Tone, author persona, formatting conventions")
    min_words: int = Field(120, ge=10, le=5000)
    max_words: int = Field(600, ge=20, le=10000)
    title_template: str = Field("{document_type} – {id}", description="Python format string over fact fields")
    facts: list[FactField]
    sections: list[DocSection]
    story: Optional[DocStory] = None
    source_run_id: Optional[str] = None
    source_table: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "DocSpec":
        names = [f.name for f in self.facts]
        if len(names) != len(set(names)):
            raise ValueError("duplicate fact fields")
        if not self.facts:
            raise ValueError("at least one fact field is required")
        if not self.sections:
            raise ValueError("at least one section is required")
        if self.min_words > self.max_words:
            raise ValueError("min_words must be <= max_words")
        if self.story:
            for o in self.story.overrides:
                if o.field not in names:
                    raise ValueError(f"story override on unknown field {o.field}")
        return self


class DocPlannerResponse(BaseModel):
    spec: DocSpec
    questions: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    notes: str = ""


class DocReplanRequest(BaseModel):
    spec: DocSpec
    feedback: str = Field("", max_length=4000)


# --------------------------------------------------------------------------- CHEAP writer output
class WrittenSection(BaseModel):
    title: str
    body: str = Field("", description="Paragraph text (for kind=text)")
    items: list[str] = Field(default_factory=list, description="Bullet items (for kind=list)")
    pairs: list[list[str]] = Field(default_factory=list, description="[label, value] pairs (key_values)")
    rows: list[list[str]] = Field(default_factory=list, description="Table rows incl. header row (table)")


class WrittenDocument(BaseModel):
    title: str
    sections: list[WrittenSection]
    email_subject: str = ""
    email_to: str = ""
    email_from: str = ""


class DocRunRequest(BaseModel):
    spec: DocSpec
    label: str = ""
