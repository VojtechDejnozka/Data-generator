"""Execute evidence queries and dashboard widgets (read-only) and summarise their results."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts.spec import Dashboard, EvidenceQuery, ScenarioSpec, Widget
from ..storage.sql_guard import SQLGuardError, run_readonly


def run_evidence(db: Path, spec: ScenarioSpec, knobs: dict[str, Any], limit: int = 2000) -> list[dict[str, Any]]:
    meta = spec.meta.model_dump()
    out = []
    for ev in spec.evidence:
        item: dict[str, Any] = {"id": ev.id, "title": ev.title, "step_id": ev.step_id, "chart": ev.chart, "x": ev.x,
                                "y": ev.y, "series": ev.series, "expected": ev.expected, "caption": ev.caption,
                                "sql_template": ev.sql}
        try:
            res = run_readonly(db, ev.sql, limit=limit, timeout_s=15, knobs=knobs, meta=meta)
            item.update(res.to_dict())
            item["ok"] = True
        except SQLGuardError as e:
            item.update({"ok": False, "error": str(e), "columns": [], "rows": []})
        out.append(item)
    return out


def compact(result: dict[str, Any], max_rows: int = 40) -> dict[str, Any]:
    """Shrink a query result for LLM consumption (row cap, rounded numbers)."""
    rows = result.get("rows", [])
    step = max(1, len(rows) // max_rows) if len(rows) > max_rows else 1
    picked = rows[::step][:max_rows]

    def r(v):
        return round(v, 2) if isinstance(v, float) else v

    return {"id": result.get("id"), "title": result.get("title"), "ok": result.get("ok", True),
            "error": result.get("error"), "columns": result.get("columns"), "n_rows": len(rows),
            "rows": [[r(v) for v in row] for row in picked], "expected": result.get("expected")}


def validate_widgets(db: Path, dashboard: Dashboard, knobs: dict[str, Any], meta: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """Run each widget; keep those that execute and expose the declared columns."""
    ok: list[dict] = []
    errors: list[str] = []
    for w in dashboard.widgets:
        try:
            res = run_readonly(db, w.sql, limit=3000, timeout_s=10, knobs=knobs, meta=meta)
        except SQLGuardError as e:
            errors.append(f"{w.id}: {e}")
            continue
        cols = set(res.columns)
        needed = set(w.y) | ({w.x} if w.x else set()) | ({w.series} if w.series else set())
        if w.chart not in ("table",) and not needed <= cols:
            errors.append(f"{w.id}: missing columns {sorted(needed - cols)}")
            continue
        ok.append({**w.model_dump(), **res.to_dict()})
    return ok, errors


def fallback_dashboard(spec: ScenarioSpec) -> Dashboard:
    """Default dashboard derived from evidence queries."""
    return Dashboard(title=f"{spec.meta.name} – přehled", widgets=[
        Widget(id=f"w_{e.id}", title=e.title, sql=e.sql, chart=e.chart, x=e.x, y=e.y, series=e.series,
               description=e.expected) for e in spec.evidence
    ])
