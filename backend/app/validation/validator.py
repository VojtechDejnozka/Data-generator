"""STRONG-model validation of a generated dataset from a compact profile + deterministic results."""
from __future__ import annotations

import json
from typing import Any

from ..contracts.io import ValidationVerdict
from ..contracts.spec import ScenarioSpec
from ..llm.client import get_llm

SYSTEM = """You are the validator of Story2Data. You receive a compact profile of a generated dataset (schema, row
counts, column statistics, sample rows), deterministic check results, evidence query results and the hidden ground
truth of what the generator injected. Judge:
1) consistency and plausibility of the data, 2) whether each causal step of the story is detectable from the evidence
(and distinguishable from distractors / the control group), 3) whether the data matches the spec and parameters.
Return verdict pass|warn|fail, a short Czech summary, per-step detectability and issues with severity. For fixable
problems propose a repair action: regenerate_table (only for llm_rows / llm_dimension tables, with instructions) or
adjust_param (only parameters listed as repairable, with a value inside the range). Never propose changing the
user's story knobs. If story_intensity is 0 the story is intentionally absent – that is a pass."""


async def validate_with_llm(spec: ScenarioSpec, knobs: dict[str, Any], profile: dict[str, Any],
                            checks_summary: dict[str, Any], failed_checks: list[dict], evidence: list[dict],
                            manifest: dict[str, Any], signals: list[dict], repairable: dict[str, tuple]) -> ValidationVerdict:
    gt = {k: manifest.get(k) for k in ("story", "realized", "distractors", "causal_chain")} if manifest else {}
    if manifest.get("monthly"):
        gt["monthly"] = manifest["monthly"]
    prompt = (
        f"## Story\n{spec.story.model_dump_json()}\n\n## Knob values\n{json.dumps(knobs, ensure_ascii=False)}\n\n"
        f"## Tables & strategies\n{json.dumps([(t.name, t.strategy, t.row_count) for t in spec.tables])}\n\n"
        f"## Data profile\n{json.dumps(profile, ensure_ascii=False, default=str)[:30000]}\n\n"
        f"## Deterministic checks\n{json.dumps(checks_summary)}\nFailed: {json.dumps(failed_checks, ensure_ascii=False, default=str)[:5000]}\n\n"
        f"## Evidence query results (compact)\n{json.dumps(evidence, ensure_ascii=False, default=str)[:30000]}\n\n"
        f"## Deterministic story signals\n{json.dumps(signals, ensure_ascii=False)}\n\n"
        f"## Ground truth manifest (hidden from end users)\n{json.dumps(gt, ensure_ascii=False, default=str)[:12000]}\n\n"
        f"## Repairable params (name: [min, max])\n{json.dumps(repairable)}\n"
    )
    return await get_llm().structured(
        role="strong", task="validate", prompt=prompt, schema=ValidationVerdict, system=SYSTEM,
        mock_context={"checks": checks_summary, "failed": failed_checks, "signals": signals, "knobs": knobs,
                      "evidence_ok": [e.get("ok", True) for e in evidence],
                      "steps": [s.id for s in spec.story.causal_chain],
                      "llm_tables": [t.name for t in spec.tables if t.strategy == "llm_rows"]},
    )


def deterministic_verdict(checks_summary: dict[str, Any], signals: list[dict], knobs: dict[str, Any]) -> ValidationVerdict:
    """Fallback when the STRONG model is unavailable – derived from checks and signals only."""
    from ..contracts.io import Issue, StepDetectability

    story_on = float(knobs.get("story_intensity", 1) or 0) > 0
    det = [StepDetectability(step_id=s["step_id"], detectable=s["detectable"], confidence=0.8 if s["detectable"] else 0.3,
                             evidence_ids=s.get("evidence_ids", []), comment=s["comment"]) for s in signals]
    issues = []
    if checks_summary.get("errors"):
        issues.append(Issue(id="checks", severity="critical", description=f"Selhalo {checks_summary['errors']} kontrol: "
                                                                          f"{', '.join(checks_summary['failed_ids'][:6])}"))
    if story_on and any(not s["detectable"] for s in signals):
        issues.append(Issue(id="weak_story", severity="major",
                            description="Některé kroky příběhu nejsou v datech dostatečně výrazné."))
    verdict = "fail" if checks_summary.get("errors") else ("warn" if issues else "pass")
    summary = ("Deterministické kontroly prošly. " if not checks_summary.get("errors") else "Kontroly selhaly. ") + (
        "Příběh je v datech dohledatelný." if story_on and all(s["detectable"] for s in signals)
        else ("Baseline bez příběhu (story_intensity = 0)." if not story_on else "Příběh je slabý."))
    return ValidationVerdict(verdict=verdict, summary=summary, story_detectability=det, issues=issues)
