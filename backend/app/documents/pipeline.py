"""Document pipeline: plan (STRONG) → facts (CHEAP llm_rows or rows of an existing run) → deterministic
story injection → per-document writing (CHEAP, structured) → checks → repair → render → validate (STRONG).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sqlite3
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..config import get_settings
from ..contracts.docs import (DocInput, DocPlannerResponse, DocReplanRequest, DocRunRequest, DocSpec, FactField,
                              WrittenDocument)
from ..contracts.io import ValidationVerdict
from ..contracts.spec import CausalStep, Column, Meta, ScenarioSpec, Story, Table
from ..generation.llm_rows import generate_table
from ..jobs import Job
from ..llm.client import LLMError, UsageTracker, get_llm, usage_scope
from ..storage import runs as store
from .render import EXT, doc_text, render

log = logging.getLogger("documents")
MAX_REPAIR_ITERATIONS = 2

PLAN_SYSTEM = """You design collections of synthetic documents (reports, letters, e-mails, records…) for Story2Data.
Documents are written from structured FACTS (one row per document) so their content is consistent and checkable.
Design: document_type, facts (typed fields with realistic ranges/enums; mark identifiers, key numbers, dates and
diagnoses/decisions as must_mention), sections (title, kind text|list|key_values|table, instructions), style,
word range and optionally a hidden STORY: a pattern affecting a share of documents, expressed as fact overrides
(set / multiply / add on fact fields) plus a writer_hint describing how the text should reflect it subtly.
All people, organisations and records are fictional. Texts in the requested data language (Czech by default),
field names snake_case ASCII. Return questions only if essential and a few concrete suggestions."""


# --------------------------------------------------------------------------- planning
def _source_context(inp_or_spec) -> tuple[list[FactField], dict[str, Any]] | None:
    rid, table = inp_or_spec.source_run_id, inp_or_spec.source_table
    if not rid or not table:
        return None
    run = store.load_run(rid)
    if not run:
        raise ValueError(f"source run {rid} not found")
    spec = ScenarioSpec.model_validate(run["spec"])
    t = spec.table(table)
    if not t:
        raise ValueError(f"table {table} not in run {rid}")
    fields = [FactField(name=c.name, type=c.type, description=c.description, enum_values=c.enum_values,
                        min=c.min, max=c.max, must_mention=c.pk or c.name in ("name", "title", "text"))
              for c in t.columns]
    sample = store.read_table(rid, table).head(3).to_dict(orient="records")
    return fields, {"table": table, "description": t.description, "columns": [f.model_dump() for f in fields],
                    "sample_rows": sample}


def _post(resp: DocPlannerResponse, inp: Optional[DocInput]) -> DocPlannerResponse:
    spec = resp.spec
    upd: dict[str, Any] = {}
    if inp:
        upd.update(count=inp.count, format=inp.format, seed=inp.seed, source_run_id=inp.source_run_id,
                   source_table=inp.source_table, data_language=inp.data_language)
    src = _source_context(inp or spec)
    if src:
        fields, _ = src
        flags = {f.name: f.must_mention for f in spec.facts}
        upd["facts"] = [f.model_copy(update={"must_mention": flags.get(f.name, f.must_mention)}) for f in fields]
        if spec.story:
            names = {f.name for f in fields}
            upd["story"] = spec.story.model_copy(update={"overrides": [o for o in spec.story.overrides if o.field in names]})
    if upd:
        spec = DocSpec.model_validate({**spec.model_dump(), **upd})
    return resp.model_copy(update={"spec": spec})


async def plan_docs(inp: DocInput) -> DocPlannerResponse:
    src = _source_context(inp)
    prompt = (f"## Request\n{inp.description}\n\nDocuments: {inp.count} × {inp.format}, language {inp.data_language}.\n"
              + (f"\n## Facts come from an existing table (use exactly these fields as facts)\n"
                 f"{json.dumps(src[1], ensure_ascii=False, default=str)}\n" if src else "")
              + "\nReturn the DocPlannerResponse.")
    resp = await get_llm().structured(role="strong", task="doc_plan", prompt=prompt, schema=DocPlannerResponse,
                                      system=PLAN_SYSTEM, mock_context={"input": inp.model_dump(),
                                                                        "source": src[1] if src else None})
    return _post(resp, inp)


async def replan_docs(req: DocReplanRequest) -> DocPlannerResponse:
    prompt = (f"## Current document spec (keep user edits)\n{req.spec.model_dump_json()}\n\n## Feedback\n{req.feedback}"
              "\n\nReturn the updated DocPlannerResponse.")
    resp = await get_llm().structured(role="strong", task="doc_replan", prompt=prompt, schema=DocPlannerResponse,
                                      system=PLAN_SYSTEM, mock_context={"spec": req.spec.model_dump(mode="json"),
                                                                        "feedback": req.feedback})
    return _post(resp, None)


# --------------------------------------------------------------------------- facts
def facts_table(spec: DocSpec) -> Table:
    cols = [Column(name="doc_id", type="int", pk=True)]
    cols += [Column(name=f.name, type=f.type, description=f.description, enum_values=f.enum_values, min=f.min,
                    max=f.max, nullable=False) for f in spec.facts if f.name != "doc_id"]
    return Table(name="facts", description=f"Fakta pro dokumenty typu {spec.document_type}: {spec.description}",
                 strategy="llm_rows", row_count=spec.count, columns=cols)


def _wrapper(spec: DocSpec) -> ScenarioSpec:
    logline = spec.story.logline if spec.story else spec.description or spec.document_type
    return ScenarioSpec(meta=Meta(name=spec.name, domain="documents", data_language=spec.data_language,
                                  period_start=None, period_end=None, seed=spec.seed),
                        tables=[facts_table(spec)],
                        story=Story(logline=logline, causal_chain=[CausalStep(id="d1", mechanism=spec.description or logline,
                                                                              tables=["facts"])]))


async def build_facts(spec: DocSpec, job: Job) -> pd.DataFrame:
    names = [f.name for f in spec.facts if f.name != "doc_id"]
    if spec.source_run_id and spec.source_table:
        job.emit("facts", f"Fakta z tabulky {spec.source_table} běhu {spec.source_run_id}", 0.08)
        df = store.read_table(spec.source_run_id, spec.source_table)
        rng = np.random.default_rng([spec.seed, 7])
        idx = np.sort(rng.choice(len(df), size=min(spec.count, len(df)), replace=False))
        df = df.iloc[idx][[n for n in names if n in df.columns]].reset_index(drop=True)
        df.insert(0, "doc_id", range(1, len(df) + 1))
        return df
    job.emit("facts", f"CHEAP model generuje fakta pro {spec.count} dokumentů", 0.08)
    df, stats = await generate_table(_wrapper(spec), facts_table(spec), {}, batch_size=20,
                                     progress=lambda m, f: job.emit("facts", m))
    if stats.get("invalid_rows"):
        job.emit("facts", f"Vyřazeno {stats['invalid_rows']} nevalidních řádků faktů")
    return df


def _cast(value: str, typ: str) -> Any:
    if typ == "int":
        return int(float(value))
    if typ == "float":
        return float(value)
    if typ == "bool":
        return value.strip().lower() in ("1", "true", "ano", "yes")
    return value


def inject_story(spec: DocSpec, df: pd.DataFrame) -> tuple[pd.DataFrame, list[int]]:
    """Apply the story overrides to a seeded subset of documents (deterministic, recorded as ground truth)."""
    if not spec.story or spec.story.affected_share <= 0 or df.empty:
        return df, []
    rng = np.random.default_rng([spec.seed, 4242])
    k = max(1, round(spec.story.affected_share * len(df)))
    affected = sorted(int(x) for x in rng.choice(df.doc_id.to_numpy(), size=min(k, len(df)), replace=False))
    types = {f.name: f.type for f in spec.facts}
    df = df.copy()
    mask = df.doc_id.isin(affected)
    for o in spec.story.overrides:
        if o.field not in df.columns:
            continue
        t = types.get(o.field, "text")
        try:
            if o.op == "set":
                df[o.field] = df[o.field].astype(object)
                df.loc[mask, o.field] = _cast(o.value, t)
            elif t in ("int", "float"):
                cur = pd.to_numeric(df.loc[mask, o.field], errors="coerce")
                new = cur * float(o.value) if o.op == "multiply" else cur + float(o.value)
                df.loc[mask, o.field] = new.round(0).astype(int) if t == "int" else new.round(2)
        except (ValueError, TypeError) as e:
            log.warning("override %s failed: %s", o, e)
    return df, affected


# --------------------------------------------------------------------------- writing & checks
def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def mention_variants(v: Any, typ: str) -> list[str]:
    s = _fmt(v)
    out = {s.casefold()}
    if typ in ("float", "int") or re.fullmatch(r"-?\d+(\.\d+)?", s):
        out.add(s.replace(".", ",").casefold())
    if typ in ("date", "datetime") or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        try:
            d = dt.date.fromisoformat(s[:10])
            out |= {f"{d.day}.{d.month}.{d.year}", f"{d.day}. {d.month}. {d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}"}
        except ValueError:
            pass
    return list(out)


def check_document(spec: DocSpec, doc: WrittenDocument, facts: dict[str, Any]) -> dict[str, Any]:
    text = doc_text(doc)
    low = text.casefold()
    issues: list[str] = []
    titles = [s.title.casefold() for s in doc.sections]
    missing = [s.title for s in spec.sections if not any(s.title.casefold()[:12] in t or t in s.title.casefold()
                                                         for t in titles)]
    if missing:
        issues.append(f"chybí sekce: {', '.join(missing)}")
    types = {f.name: f.type for f in spec.facts}
    for f in spec.facts:
        if f.must_mention and f.name in facts and facts[f.name] not in (None, ""):
            if not any(v in low for v in mention_variants(facts[f.name], types[f.name])):
                issues.append(f"nezmiňuje {f.name} = {_fmt(facts[f.name])}")
    words = len(re.findall(r"\w+", text))
    if words < spec.min_words * 0.7:
        issues.append(f"příliš krátký ({words} slov < {spec.min_words})")
    if words > spec.max_words * 1.4:
        issues.append(f"příliš dlouhý ({words} slov > {spec.max_words})")
    if re.search(r"\{[a-z_]+\}|lorem ipsum|\[(jméno|name|doplnit)\]", low):
        issues.append("obsahuje nevyplněný placeholder")
    return {"ok": not issues, "issues": issues, "words": words}


async def write_document(spec: DocSpec, facts: dict[str, Any], affected: bool, notes: str = "") -> WrittenDocument:
    must = {f.name: _fmt(facts[f.name]) for f in spec.facts if f.must_mention and f.name in facts}
    sections = [s.model_dump() for s in spec.sections]
    story = (f"\nThis document is part of a hidden pattern: {spec.story.writer_hint or spec.story.logline}. Reflect it "
             "naturally through the facts; never say it is a pattern or a story." if affected and spec.story else "")
    prompt = (
        f"Write one fictional {spec.document_type} ({spec.description}). Language: {spec.data_language}. "
        f"Style: {spec.style or 'realistic, professional'}. Length {spec.min_words}–{spec.max_words} words.\n"
        f"Title hint: {spec.title_template}\n"
        f"Sections (keep these titles and kinds; key_values -> pairs, table -> rows with a header row, list -> items, "
        f"text -> body): {json.dumps(sections, ensure_ascii=False)}\n"
        f"FACTS (the document must be consistent with them, do not invent conflicting values): "
        f"{json.dumps(facts, ensure_ascii=False, default=str)}\n"
        f"These values must appear exactly as written: {json.dumps(must, ensure_ascii=False)}\n"
        + ("Also fill email_subject, email_from and email_to (fictional addresses on example domains).\n"
           if spec.format == "eml" else "")
        + story + (f"\nFix these problems from the previous version: {notes}" if notes else "")
    )
    return await get_llm().structured(
        role="cheap", task="doc_write", prompt=prompt, schema=WrittenDocument,
        mock_context={"spec": spec.model_dump(mode="json"), "facts": facts, "affected": affected, "notes": notes},
        use_cache=not notes,
    )


# --------------------------------------------------------------------------- validation
async def validate_docs(spec: DocSpec, docs: dict[int, WrittenDocument], checks: dict[int, dict], facts: pd.DataFrame,
                        affected: list[int]) -> tuple[ValidationVerdict, str]:
    ok = sum(c["ok"] for c in checks.values())
    summary = {"documents": len(docs), "checks_ok": ok, "failed": {i: c["issues"] for i, c in checks.items() if not c["ok"]}}
    sample_ids = sorted(docs)[:2] + [a for a in affected[:1] if a not in sorted(docs)[:2]]
    samples = [{"doc_id": i, "affected": i in affected, "facts": facts[facts.doc_id == i].to_dict(orient="records"),
                "text": doc_text(docs[i])[:3000]} for i in sample_ids if i in docs]
    story_stats = {}
    if spec.story:
        for o in spec.story.overrides:
            if o.field in facts and pd.api.types.is_numeric_dtype(facts[o.field]):
                m = facts.doc_id.isin(affected)
                story_stats[o.field] = {"affected_mean": round(float(facts.loc[m, o.field].mean()), 2),
                                        "others_mean": round(float(facts.loc[~m, o.field].mean()), 2)}
            elif o.field in facts:
                story_stats[o.field] = {"affected_top": facts.loc[facts.doc_id.isin(affected), o.field].astype(str)
                                        .value_counts().head(3).to_dict(),
                                        "others_top": facts.loc[~facts.doc_id.isin(affected), o.field].astype(str)
                                        .value_counts().head(3).to_dict()}
    prompt = (f"Validate a generated document collection.\nSpec: {spec.model_dump_json()}\nChecks: {json.dumps(summary, ensure_ascii=False)}"
              f"\nStory stats (affected vs others): {json.dumps(story_stats, ensure_ascii=False, default=str)}"
              f"\nSample documents: {json.dumps(samples, ensure_ascii=False, default=str)[:20000]}\n"
              "Judge realism, consistency of text with facts, coverage of sections and whether the hidden pattern is "
              "recoverable from the documents. Answer in Czech. story_detectability: one item with step_id 'story'.")
    try:
        v = await get_llm().structured(role="strong", task="doc_validate", prompt=prompt, schema=ValidationVerdict,
                                       mock_context={"summary": summary, "story": bool(spec.story),
                                                     "story_stats": story_stats})
        return v, "strong"
    except LLMError as e:
        verdict = "pass" if ok == len(docs) else ("warn" if ok >= 0.8 * len(docs) else "fail")
        return ValidationVerdict(verdict=verdict, summary=f"STRONG validace nedostupná ({str(e)[:80]}); "
                                                         f"{ok}/{len(docs)} dokumentů prošlo kontrolami."), "deterministic"


# --------------------------------------------------------------------------- run
async def execute_doc_run(req: DocRunRequest, job: Job) -> dict[str, Any]:
    spec = req.spec
    settings = get_settings()
    run_id = store.new_run_id()
    out_dir = store.run_path(run_id) / "documents"
    out_dir.mkdir(parents=True, exist_ok=True)
    tracker = UsageTracker()
    t0 = dt.datetime.now()
    run: dict[str, Any] = {"run_id": run_id, "kind": "documents", "label": req.label or spec.name, "name": spec.name,
                           "created_at": t0.isoformat(timespec="seconds"), "status": "running", "mock": settings.mock,
                           "doc_spec": spec.model_dump(mode="json"), "job_id": job.id,
                           "models": {"strong": settings.strong_model, "cheap": settings.cheap_model}}
    store.save_json(run_id, "run.json", run)
    job.emit("plan", f"Dokumentový běh {run_id}: {spec.count} × {spec.format}", 0.02, run_id=run_id)

    with usage_scope(tracker):
        facts = await build_facts(spec, job)
        facts, affected = inject_story(spec, facts)
        if affected:
            job.emit("story", f"Skrytý vzor vložen do {len(affected)} z {len(facts)} dokumentů (ground truth)", 0.2)
        rows = {int(r["doc_id"]): {k: (None if pd.isna(v) else v) for k, v in r.items()}
                for r in facts.to_dict(orient="records")}
        docs: dict[int, WrittenDocument] = {}
        checks: dict[int, dict] = {}
        done = 0

        async def one(i: int, notes: str = "") -> None:
            nonlocal done
            f = {k: v for k, v in rows[i].items() if k != "doc_id"} | {"doc_id": i}
            try:
                docs[i] = await write_document(spec, f, i in affected, notes)
                checks[i] = check_document(spec, docs[i], f)
            except LLMError as e:
                checks[i] = {"ok": False, "issues": [f"generování selhalo: {str(e)[:120]}"], "words": 0}
            done += 1
            job.emit("write", f"Dokument {i}: {'OK' if checks[i]['ok'] else '; '.join(checks[i]['issues'])[:120]}",
                     0.2 + 0.6 * done / max(len(rows), 1))

        job.emit("write", "CHEAP model píše dokumenty z faktů", 0.2)
        await asyncio.gather(*[one(i) for i in rows])
        history = [{"iteration": 0, "failed": sum(not c["ok"] for c in checks.values())}]
        for it in range(1, MAX_REPAIR_ITERATIONS + 1):
            bad = [i for i, c in checks.items() if not c["ok"]]
            if not bad:
                break
            job.emit("repair", f"Repair {it}: přepisuji {len(bad)} dokumentů s chybami", 0.8)
            done = 0
            await asyncio.gather(*[one(i, "; ".join(checks[i]["issues"])) for i in bad])
            history.append({"iteration": it, "failed": sum(not c["ok"] for c in checks.values())})

        job.emit("render", f"Vykresluji {len(docs)} souborů ({spec.format})", 0.85)
        index = []
        for i in sorted(docs):
            fname = f"doc_{i:04d}.{EXT[spec.format]}"
            data = render(docs[i], spec.format, {"document_type": spec.document_type}, rows[i])
            (out_dir / fname).write_bytes(data)
            index.append({"doc_id": i, "file": fname, "title": docs[i].title, "words": checks[i]["words"],
                          "checks_ok": checks[i]["ok"], "issues": checks[i]["issues"], "bytes": len(data)})
        store.save_json(run_id, "written.json", {str(i): d.model_dump() for i, d in docs.items()})

        # facts + document index as a queryable SQLite database (ground-truth flag NOT included)
        db = store.db_path(run_id)
        conn = sqlite3.connect(db)
        try:
            facts.to_sql("facts", conn, index=False)
            pd.DataFrame([{k: v for k, v in r.items() if k != "issues"} | {"issues": "; ".join(r["issues"])}
                          for r in index]).to_sql("documents", conn, index=False)
        finally:
            conn.close()

        job.emit("validate", "Validace STRONG modelem (vzorek dokumentů + statistiky)", 0.9)
        verdict, validator = await validate_docs(spec, docs, checks, facts, affected)
        job.emit("validate", f"Verdikt: {verdict.verdict} – {verdict.summary[:160]}")

    manifest = {"kind": "documents", "story": spec.story.model_dump() if spec.story else None,
                "affected_doc_ids": affected, "affected_count": len(affected), "total": len(facts),
                "source": {"run_id": spec.source_run_id, "table": spec.source_table} if spec.source_run_id else None}
    ok = sum(c["ok"] for c in checks.values())
    run.update({"status": "done", "documents": index, "checks_summary": {"total": len(checks), "passed": ok,
                                                                         "errors": len(checks) - ok},
                "verdict": verdict.model_dump(), "validator": validator, "repair_history": history,
                "row_counts": {"facts": len(facts), "documents": len(index)}, "usage": tracker.summary(),
                "llm_calls": tracker.records(), "duration_s": round((dt.datetime.now() - t0).total_seconds(), 1)})
    store.save_json(run_id, "run.json", run)
    store.save_json(run_id, "manifest.json", manifest)
    job.emit("done", f"Hotovo: {len(index)} dokumentů, {ok} prošlo kontrolami", 1.0, run_id=run_id)
    return {"run_id": run_id}
