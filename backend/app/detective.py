"""F7 – "Detective": a blind analyst agent.

It gets ONLY the database schema (table/column names and types, row counts – no story, no
descriptions, no knobs) and a business question. Through a read-only SQL tool it investigates and
reports the root cause. The answer is then scored against the hidden ground-truth manifest – a
strong check that the story is really recoverable from the data.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .jobs import Job
from .llm.client import get_llm
from .storage import runs as store
from .storage.sql_guard import SQLGuardError, run_readonly

MAX_STEPS = 12

SYSTEM = """You are a senior data analyst investigating a company database. You can only see the schema and the
results of read-only SQLite SELECT queries you request. Work hypothesis-driven: check demand first, then supply
chain, inventory, sales mix and customer feedback; compare affected groups with unaffected ones and look at timing.
Each turn return either action="query" with ONE SQL query (aggregate! results are truncated to 40 rows), or
action="final" with your conclusion. Finish within the step budget."""

QUESTION = ("Vedení hlásí, že tržby v druhé polovině sledovaného období klesají. Zjisti z dat skutečnou příčinu: "
            "co se stalo, kterých entit se to týká, kdy to začalo a jaký je řetězec důsledků. Vyluč alternativní "
            "vysvětlení (např. pokles poptávky, sezónnost).")


class DetectiveFinal(BaseModel):
    root_cause: str = Field(description="One-paragraph root cause in Czech")
    entity_table: Optional[str] = Field(None, description="Table of the main culprit entity, e.g. 'suppliers'")
    entity_id: Optional[int] = Field(None, description="Primary key of the culprit entity")
    entity_name: Optional[str] = None
    start_date: Optional[str] = Field(None, description="When the problem started (YYYY-MM-DD)")
    causal_chain: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list, description="Alternative explanations ruled out")
    confidence: float = Field(0.5, ge=0, le=1)


class DetectiveStep(BaseModel):
    thought: str
    action: Literal["query", "final"]
    sql: Optional[str] = None
    final: Optional[DetectiveFinal] = None


def blind_schema(run_id: str) -> list[dict[str, Any]]:
    """Schema without any descriptions/flags – only what a real analyst would see in the DB."""
    conn = sqlite3.connect(f"file:{store.db_path(run_id)}?mode=ro", uri=True)
    try:
        out = []
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
            fks = [f"{r[3]} -> {r[2]}.{r[4]}" for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out.append({"table": name, "rows": n, "columns": cols, "foreign_keys": fks})
        return out
    finally:
        conn.close()


def _observe(run_id: str, sql: str) -> dict[str, Any]:
    try:
        r = run_readonly(store.db_path(run_id), sql, limit=40, timeout_s=15)
        rows = [[round(v, 3) if isinstance(v, float) else (v[:80] if isinstance(v, str) else v) for v in row]
                for row in r.rows]
        return {"ok": True, "columns": r.columns, "rows": rows, "truncated": r.truncated}
    except SQLGuardError as e:
        return {"ok": False, "error": str(e)[:300]}


async def investigate(run_id: str, job: Job | None = None) -> dict[str, Any]:
    schema = blind_schema(run_id)
    transcript: list[dict[str, Any]] = []
    final: Optional[DetectiveFinal] = None
    llm = get_llm()
    for step in range(MAX_STEPS):
        remaining = MAX_STEPS - step
        hist = json.dumps(transcript, ensure_ascii=False, default=str)
        prompt = (f"## Question\n{QUESTION}\n\n## Schema\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                  f"## Investigation so far\n{hist[-40000:]}\n\nSteps remaining: {remaining}. "
                  + ("You MUST return action='final' now." if remaining <= 1 else ""))
        res = await llm.structured(role="strong", task="detective", prompt=prompt, schema=DetectiveStep, system=SYSTEM,
                                   mock_context={"step": step, "observations": transcript, "run_id": run_id})
        if res.action == "final" or remaining <= 1:
            final = res.final or DetectiveFinal(root_cause=res.thought, confidence=0.2)
            transcript.append({"step": step, "thought": res.thought, "action": "final"})
            break
        obs = _observe(run_id, res.sql or "")
        transcript.append({"step": step, "thought": res.thought, "sql": res.sql, "observation": obs})
        if job:
            job.emit("detective", f"Krok {step + 1}: {res.thought[:140]}", (step + 1) / MAX_STEPS,
                     sql=res.sql, observation=obs)
    manifest = store.load_json(run_id, "manifest.json", {}) or {}
    score = score_answer(final, manifest, transcript)
    result = {"final": final.model_dump() if final else None, "transcript": transcript, "score": score,
              "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    store.save_json(run_id, "detective.json", result)
    return result


def score_answer(final: Optional[DetectiveFinal], manifest: dict[str, Any], transcript: list) -> dict[str, Any]:
    if not final or not manifest.get("story"):
        return {"score": 0, "checks": [], "note": "no answer or no ground truth"}
    st = manifest["story"]
    intensity = float(st.get("story_intensity", 1) or 0)
    text = " ".join([final.root_cause, *final.causal_chain]).lower()
    checks = []
    if intensity == 0:
        ok = final.entity_table != "suppliers" or final.confidence < 0.5
        checks.append({"name": "Baseline: žádný falešný viník", "ok": ok})
    else:
        checks.append({"name": "Správný viník (dodavatel)", "ok": final.entity_table == "suppliers"
                       and final.entity_id == st["supplier_id"],
                       "expected": f"{st['supplier_name']} (ID {st['supplier_id']})",
                       "got": f"{final.entity_name} (ID {final.entity_id})"})
        start_ok = False
        if final.start_date:
            try:
                delta = abs((dt.date.fromisoformat(final.start_date[:10]) -
                             dt.date.fromisoformat(st["delay_start_date"])).days)
                start_ok = delta <= 45
            except ValueError:
                pass
        checks.append({"name": "Začátek ±45 dní", "ok": start_ok, "expected": st["delay_start_date"],
                       "got": final.start_date})
        checks.append({"name": "Zmiňuje zpoždění dodávek", "ok": any(w in text for w in ("zpožd", "delay", "pozd"))})
        checks.append({"name": "Zmiňuje výpadky zásob", "ok": any(w in text for w in ("výpad", "zásob", "stockout", "sklad"))})
        checks.append({"name": "Zmiňuje stížnosti", "ok": any(w in text for w in ("stížn", "complain"))})
        ruled = " ".join(final.ruled_out).lower()
        checks.append({"name": "Vyloučil pokles poptávky", "ok": any(w in ruled for w in ("poptáv", "návštěv", "demand", "footfall"))})
    score = round(100 * sum(c["ok"] for c in checks) / len(checks))
    return {"score": score, "checks": checks, "queries": sum(1 for t in transcript if t.get("sql"))}
