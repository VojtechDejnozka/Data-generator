"""Report assembly, run comparison (what-if) and AI commentary."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ..contracts.spec import ScenarioSpec
from ..llm.client import get_llm
from ..storage import runs as store
from ..storage.sql_guard import SQLGuardError, run_readonly
from .evidence import compact


def build_report(run_id: str) -> dict[str, Any]:
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    spec = ScenarioSpec.model_validate(run["spec"])
    evidence = store.load_json(run_id, "evidence.json", [])
    checks = store.load_json(run_id, "checks.json", [])
    signals = {s["step_id"]: s for s in run.get("signals", [])}
    verdict = run.get("verdict") or {}
    det = {d["step_id"]: d for d in verdict.get("story_detectability", [])}
    by_step: dict[str, list] = {}
    for e in evidence:
        by_step.setdefault(e.get("step_id") or "_other", []).append(e)
    steps = []
    for s in spec.story.causal_chain:
        steps.append({"step": s.model_dump(), "signal": signals.get(s.id), "detectability": det.get(s.id),
                      "evidence": by_step.get(s.id, [])})
    return {
        "run": {k: run.get(k) for k in ("run_id", "label", "name", "created_at", "status", "parent_run_id",
                                         "knob_values", "mock", "models", "duration_s", "row_counts", "usage",
                                         "warnings", "repair_history", "validator", "params", "plan",
                                         "generation_stats")},
        "meta": spec.meta.model_dump(),
        "story": {"logline": spec.story.logline, "distractors": [d.model_dump() for d in spec.story.distractors],
                  "ground_truth_facts_hidden": len(spec.story.ground_truth_facts)},
        "knobs": [{**k.model_dump(), "value": run.get("knob_values", {}).get(k.name)} for k in spec.knobs],
        "quality": {"summary": run.get("checks_summary"), "checks": checks},
        "verdict": verdict,
        "steps": steps,
        "other_evidence": by_step.get("_other", []),
        "tables": [{"name": t.name, "description": t.description, "strategy": t.strategy,
                    "rows": run.get("row_counts", {}).get(t.name)} for t in spec.tables],
    }


# --------------------------------------------------------------------------- compare
METRICS = {
    "revenue_k_czk": ("Tržby celkem (tis. Kč)", "SELECT ROUND(SUM(total_amount)/1000.0,1) FROM transactions"),
    "transactions": ("Počet nákupů", "SELECT COUNT(*) FROM transactions"),
    "visitors": ("Návštěvnost celkem", "SELECT SUM(visitors) FROM footfall_daily"),
    "conversion_pct": ("Konverze návštěva → nákup (%)",
                       "SELECT ROUND(100.0*(SELECT COUNT(*) FROM transactions)/(SELECT SUM(visitors) FROM footfall_daily),2)"),
    "story_supplier_delay": ("Prům. zpoždění problémového dodavatele (dny)",
                             "SELECT ROUND(AVG(delay_days),2) FROM deliveries WHERE supplier_id={{knob:supplier_id}}"),
    "others_delay": ("Prům. zpoždění ostatních dodavatelů (dny)",
                     "SELECT ROUND(AVG(delay_days),2) FROM deliveries WHERE supplier_id!={{knob:supplier_id}}"),
    "story_oos_pct": ("Dny bez zásoby – produkty dodavatele (%)",
                      "SELECT ROUND(100.0*SUM(days_out_of_stock_7d)/(7.0*COUNT(*)),2) FROM inventory_snapshots i "
                      "JOIN products p ON p.product_id=i.product_id WHERE p.supplier_id={{knob:supplier_id}}"),
    "control_oos_pct": ("Dny bez zásoby – kontrolní skupina (%)",
                        "SELECT ROUND(100.0*SUM(days_out_of_stock_7d)/(7.0*COUNT(*)),2) FROM inventory_snapshots i "
                        "JOIN products p ON p.product_id=i.product_id WHERE p.supplier_id!={{knob:supplier_id}}"),
    "stockout_events": ("Události výpadku zásob", "SELECT COUNT(*) FROM stockout_events"),
    "availability_complaints": ("Stížnosti na nedostupnost",
                                "SELECT COUNT(*) FROM complaints WHERE category='Nedostupnost zboží'"),
    "complaints": ("Stížnosti celkem", "SELECT COUNT(*) FROM complaints"),
}


def _metric(run_id: str, sql: str, knobs: dict[str, Any], meta: dict[str, Any]) -> Any:
    try:
        r = run_readonly(store.db_path(run_id), sql, limit=1, timeout_s=10, knobs=knobs, meta=meta)
        return r.rows[0][0] if r.rows else None
    except SQLGuardError:
        return None


def compare_runs(a: str, b: str) -> dict[str, Any]:
    ra, rb = store.load_run(a), store.load_run(b)
    if not ra or not rb:
        raise KeyError("run not found")
    # Metrics use run A's knobs (same supplier) so they measure the same entities in both runs.
    ka = ra.get("knob_values", {})
    meta = ra["spec"]["meta"]
    metrics = []
    for key, (label, sql) in METRICS.items():
        va, vb = _metric(a, sql, ka, meta), _metric(b, sql, ka, meta)
        diff = (vb - va) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        pct = round(100.0 * diff / va, 1) if diff is not None and va else None
        metrics.append({"key": key, "label": label, "a": va, "b": vb, "diff": diff, "diff_pct": pct})
    ea = {e["id"]: e for e in store.load_json(a, "evidence.json", [])}
    eb = {e["id"]: e for e in store.load_json(b, "evidence.json", [])}
    evidence = [{"id": i, "title": ea[i]["title"], "chart": ea[i]["chart"], "x": ea[i]["x"], "y": ea[i]["y"],
                 "series": ea[i].get("series"), "a": {k: ea[i].get(k) for k in ("columns", "rows")},
                 "b": {k: eb[i].get(k) for k in ("columns", "rows")}} for i in ea if i in eb]
    changed = {k: {"a": ka.get(k), "b": rb.get("knob_values", {}).get(k)}
               for k in set(ka) | set(rb.get("knob_values", {})) if ka.get(k) != rb.get("knob_values", {}).get(k)}
    return {
        "a": {k: ra.get(k) for k in ("run_id", "label", "knob_values", "verdict", "signals", "created_at")},
        "b": {k: rb.get(k) for k in ("run_id", "label", "knob_values", "verdict", "signals", "created_at")},
        "changed_knobs": changed, "metrics": metrics, "evidence": evidence,
        "explanation": explain_diff(changed, metrics),
    }


def explain_diff(changed: dict[str, Any], metrics: list[dict]) -> list[str]:
    """Deterministic, mechanism-based explanation of the difference."""
    m = {x["key"]: x for x in metrics}
    out = []
    if changed:
        out.append("Změněné parametry: " + ", ".join(f"{k}: {v['a']} → {v['b']}" for k, v in changed.items()))
    order = [("story_supplier_delay", "zpoždění dodávek problémového dodavatele"),
             ("story_oos_pct", "podíl dní bez zásoby u jeho produktů"),
             ("conversion_pct", "konverze návštěv na nákup"), ("revenue_k_czk", "tržby"),
             ("availability_complaints", "stížnosti na nedostupnost")]
    for key, label in order:
        x = m.get(key)
        if x and x["diff"] is not None:
            direction = "vzrostl/a" if x["diff"] > 0 else ("klesl/a" if x["diff"] < 0 else "beze změny")
            out.append(f"{label.capitalize()} {direction}: {x['a']} → {x['b']}" + (f" ({x['diff_pct']:+} %)" if x["diff_pct"] is not None else ""))
    v = m.get("visitors")
    if v and v["diff_pct"] is not None and abs(v["diff_pct"]) < 1:
        out.append("Návštěvnost (poptávka) je v obou bězích téměř shodná – rozdíl tržeb vzniká jen mechanismem "
                   "zpoždění → výpadky zásob → neuskutečněné nákupy.")
    return out


# --------------------------------------------------------------------------- AI commentary
class Commentary(BaseModel):
    headline: str
    paragraphs: list[str]


async def commentary(run_id: str) -> dict[str, Any]:
    run = store.load_run(run_id)
    evidence = [compact(e, 25) for e in store.load_json(run_id, "evidence.json", [])]
    widgets = [compact({"id": w["id"], "title": w["title"], "columns": w.get("columns"), "rows": w.get("rows", [])}, 20)
               for w in store.load_json(run_id, "dashboard.json", [])]
    prompt = (
        "You are a data analyst. Write a concise Czech interpretation of these charts for a business audience: "
        "a headline and 3-6 short paragraphs. Describe only what the numbers show; mention control groups and "
        "distractors where visible. Do not invent numbers.\n\n"
        f"Story logline: {run['spec']['story']['logline']}\n"
        f"Evidence: {json.dumps(evidence, ensure_ascii=False, default=str)[:25000]}\n"
        f"Dashboard: {json.dumps(widgets, ensure_ascii=False, default=str)[:10000]}"
    )
    res = await get_llm().structured(role="strong", task="commentary", prompt=prompt, schema=Commentary,
                                     mock_context={"signals": run.get("signals", [])})
    return res.model_dump()
