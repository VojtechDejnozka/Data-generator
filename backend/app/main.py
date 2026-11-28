"""FastAPI application – REST + SSE API for the Story2Data frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from .config import get_settings
from .contracts.io import (ObservationSettings, PlannerResponse, QueryRequest, ReplanRequest, RunRequest, ScenarioInput,
                           WhatIfRequest)
from .contracts.spec import ScenarioSpec
from .detective import investigate
from .generation.scenarios.registry import catalog
from .jobs import jobs
from .pipeline import execute_run
from .planner import plan_scenario, replan
from .presets.retail_demo import PRESETS
from .reporting.report import build_report, commentary, compare_runs
from .storage import runs as store
from .storage.text_files import read_saved_text
from .storage.sql_guard import SQLGuardError, run_readonly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="Story2Data", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])

MAX_REPLAN_ROUNDS = 4


# --------------------------------------------------------------------------- helpers
def _sessions_dir():
    d = get_settings().data_dir / "_sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_session(sid: str, data: dict) -> None:
    (_sessions_dir() / f"{sid}.json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


def _load_session(sid: str) -> dict:
    if not sid.isalnum():
        raise HTTPException(400, "invalid session id")
    p = _sessions_dir() / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    return json.loads(read_saved_text(p))


def _run_or_404(run_id: str) -> dict:
    try:
        run = store.load_run(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run id")
    if not run:
        raise HTTPException(404, "run not found")
    return run


def _spec_of(run: dict) -> ScenarioSpec:
    return ScenarioSpec.model_validate(run["spec"])


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "mock": s.mock, "has_key": bool(s.openai_api_key), "strong_model": s.strong_model,
            "strong_fallbacks": s.strong_fallbacks, "cheap_model": s.cheap_model, "cheap_fallbacks": s.cheap_fallbacks}


@app.get("/api/presets")
def presets() -> list[dict]:
    return list(PRESETS.values())


@app.get("/api/modules")
def modules() -> list[dict]:
    return catalog()


# --------------------------------------------------------------------------- jobs
@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel()
    return {"id": job.id, "cancel_requested": True, "status": job.status}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    async def stream():
        i = 0
        while True:
            while i < len(job.events):
                ev = {k: v for k, v in job.events[i].items() if k not in ("traceback", "observation")}
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                i += 1
            if job.status in ("done", "error") and i >= len(job.events):
                final = {"final": True, "status": job.status, "result": job.result, "error": job.error}
                yield f"data: {json.dumps(final, ensure_ascii=False, default=str)}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- planning
def _create_plan_session(inp: ScenarioInput, resp: PlannerResponse) -> dict:
    sid = secrets.token_hex(6)
    _save_session(sid, {"session_id": sid, "round": 0, "input": inp.model_dump(),
                        "response": resp.model_dump(mode="json"), "history": []})
    return {"session_id": sid}


@app.post("/api/plan")
async def start_plan(inp: ScenarioInput) -> dict:
    async def work(job):
        job.emit("plan", "STRONG model navrhuje ScenarioSpec" + (" se spustitelným SCM" if inp.engine == "scm" else ""), 0.1)
        if inp.engine == "scm":
            from .causal.planner import plan_story
            resp = await plan_story(inp)
        else:
            resp = await plan_scenario(inp)
        return _create_plan_session(inp, resp)

    return {"job_id": jobs.start("plan", work).id}


@app.get("/api/plan/{sid}")
def get_session(sid: str) -> dict:
    return _load_session(sid)


@app.post("/api/plan/{sid}/replan")
async def start_replan(sid: str, req: ReplanRequest) -> dict:
    session = _load_session(sid)
    if session["round"] >= MAX_REPLAN_ROUNDS:
        raise HTTPException(400, f"maximum {MAX_REPLAN_ROUNDS} re-planning rounds reached")
    prev = PlannerResponse.model_validate(session["response"])

    async def work(job):
        job.emit("plan", "Přeplánování se zpětnou vazbou", 0.1)
        if req.spec.scm is not None or prev.spec.scm is not None:
            resp = await _replan_scm(session, req, prev)
        else:
            resp = await replan(req, prev.suggestions, session["round"] + 1)
        session["history"].append({"round": session["round"], "feedback": req.feedback, "answers": req.answers,
                                   "accepted": req.accepted_suggestions, "rejected": req.rejected_suggestions})
        session["round"] += 1
        session["response"] = resp.model_dump(mode="json")
        _save_session(sid, session)
        return {"session_id": sid}

    return {"job_id": jobs.start("replan", work).id}


async def _replan_scm(session: dict, req: ReplanRequest, prev: PlannerResponse) -> PlannerResponse:
    """SCM sessions: user edits are kept as-is (validated); free-text feedback triggers a new LLM proposal."""
    from .causal.planner import plan_story
    from .causal.service import scenario_spec

    scm = req.spec.scm or prev.spec.scm
    if req.feedback.strip() or req.answers:
        inp = ScenarioInput.model_validate(session["input"])
        extra = "\n\nZpětná vazba uživatele: " + req.feedback + (
            "\nOdpovědi: " + json.dumps(req.answers, ensure_ascii=False) if req.answers else "")
        inp = inp.model_copy(update={"story": (inp.story + extra)[:8000], "engine": "scm",
                                     "custom_model": (inp.custom_model + "\n\nCurrent SCM JSON (keep user edits):\n"
                                                      + scm.model_dump_json())[:8000]})
        return await plan_story(inp)
    from .causal.validation import validate_spec as validate_scm
    rep = validate_scm(scm)
    return PlannerResponse(spec=scenario_spec(scm, req.spec), questions=[], suggestions=[],
                           notes=f"Uživatelské úpravy SCM ověřeny: {rep.status} "
                                 + "; ".join(c.explanation for c in rep.errors[:5]))


@app.post("/api/spec/validate")
def validate_spec(spec: ScenarioSpec) -> dict:
    if spec.scm is not None:
        from .causal.validation import validate_spec as validate_scm
        rep = validate_scm(spec.scm)
        return {"errors": [f"{c.target}: {c.explanation}" for c in rep.errors], "order": [], "scm_report": rep.model_dump()}
    return {"errors": spec.structural_errors(), "order": spec.topological_order() if not spec.structural_errors() else []}


# --------------------------------------------------------------------------- runs
@app.post("/api/runs")
async def start_run(req: RunRequest) -> dict:
    if req.spec.scm is not None:
        from .causal.validation import validate_spec as validate_scm
        rep = validate_scm(req.spec.scm)
        if rep.status == "FAIL":
            raise HTTPException(422, {"errors": [f"{c.target}: {c.explanation}" for c in rep.errors],
                                      "scm_report": rep.model_dump()})
    else:
        errs = req.spec.structural_errors()
        if errs:
            raise HTTPException(422, {"errors": errs})
    return {"job_id": jobs.start("run", lambda job: execute_run(req, job)).id}


@app.get("/api/runs")
def runs_list() -> list[dict]:
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    run = _run_or_404(run_id)
    run.pop("llm_calls", None)
    return run


@app.get("/api/runs/{run_id}/llm-calls")
def run_llm_calls(run_id: str) -> list[dict]:
    return _run_or_404(run_id).get("llm_calls", [])


@app.get("/api/runs/{run_id}/report")
def run_report(run_id: str) -> dict:
    _run_or_404(run_id)
    return build_report(run_id)


@app.get("/api/runs/{run_id}/dashboard")
def run_dashboard(run_id: str) -> dict:
    run = _run_or_404(run_id)
    widgets = store.load_json(run_id, "dashboard.json", [])
    meta = run.get("dashboard", {})
    if not widgets and run.get("engine") == "scm" and store.db_path(run_id).is_file():
        from .causal.service import run_scm, scenario_spec
        from .reporting.evidence import validate_widgets
        scm, _ = run_scm(run_id)
        spec = scenario_spec(scm)
        widgets, errors = validate_widgets(store.db_path(run_id), spec.dashboard, {}, spec.meta.model_dump())
        meta = {**meta, "errors": errors}
    return {"meta": meta, "widgets": widgets}


@app.post("/api/runs/{run_id}/dashboard")
async def start_scm_dashboard(run_id: str) -> dict:
    run = _run_or_404(run_id)
    if run.get("engine") != "scm" or run.get("status") != "done":
        raise HTTPException(409, "Dashboard přes AI lze vytvořit pro dokončený SCM běh.")
    from .causal.service import refresh_scm_dashboard

    return {"job_id": jobs.start("dashboard", lambda job: refresh_scm_dashboard(run_id, job)).id}


@app.get("/api/runs/{run_id}/ground-truth")
def run_ground_truth(run_id: str) -> dict:
    _run_or_404(run_id)
    return store.load_json(run_id, "manifest.json", {}) or {}


@app.get("/api/runs/{run_id}/schema")
def run_schema(run_id: str) -> dict:
    run = _run_or_404(run_id)
    spec = _spec_of(run)
    tables = []
    edges = []
    for t in spec.tables:
        tables.append({"name": t.name, "description": t.description, "strategy": t.strategy, "flags": t.flags,
                       "rows": run.get("row_counts", {}).get(t.name),
                       "columns": [c.model_dump() for c in t.columns]})
        for c in t.columns:
            if c.fk:
                rt, rc = c.fk.split(".")
                edges.append({"from": t.name, "from_col": c.name, "to": rt, "to_col": rc})
    return {"tables": tables, "edges": edges, "order": spec.topological_order()}


@app.get("/api/runs/{run_id}/tables/{table}")
def run_table(run_id: str, table: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
              sort: Optional[str] = None, desc: bool = False, filter_col: Optional[str] = None,
              filter_val: Optional[str] = None, filter_op: str = "eq") -> dict:
    run = _run_or_404(run_id)
    spec = _spec_of(run)
    t = spec.table(table)
    if not t:
        raise HTTPException(404, "table not found")
    cols = [c.name for c in t.columns]
    where, params = "", []
    if filter_col:
        if filter_col not in cols:
            raise HTTPException(400, "unknown filter column")
        if filter_op == "contains":
            where, params = f'WHERE CAST("{filter_col}" AS TEXT) LIKE ?', [f"%{filter_val or ''}%"]
        elif filter_op in ("gte", "lte"):
            where, params = f'WHERE "{filter_col}" {">=" if filter_op == "gte" else "<="} ?', [filter_val]
        else:
            where, params = f'WHERE "{filter_col}" = ?', [filter_val]
    order = ""
    if sort:
        if sort not in cols:
            raise HTTPException(400, "unknown sort column")
        order = f'ORDER BY "{sort}" {"DESC" if desc else "ASC"}'
    elif t.pk:
        order = "ORDER BY " + ", ".join(f'"{c}"' for c in t.pk)
    conn = sqlite3.connect(f"file:{store.db_path(run_id)}?mode=ro", uri=True)
    try:
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}" {where}', params).fetchone()[0]
        cur = conn.execute(f'SELECT * FROM "{table}" {where} {order} LIMIT ? OFFSET ?',
                           [*params, page_size, (page - 1) * page_size])
        rows = [list(r) for r in cur.fetchall()]
        columns = [d[0] for d in cur.description]
    finally:
        conn.close()
    fks = {c.name: c.fk for c in t.columns if c.fk}
    referenced_by = [{"table": x.name, "column": c.name, "ref": c.fk.split(".")[1]}
                     for x in spec.tables for c in x.columns if c.fk and c.fk.startswith(table + ".")]
    return {"table": table, "columns": columns, "rows": rows, "total": total, "page": page, "page_size": page_size,
            "fks": fks, "pk": t.pk, "referenced_by": referenced_by}


@app.post("/api/runs/{run_id}/query")
def run_query(run_id: str, req: QueryRequest) -> dict:
    run = _run_or_404(run_id)
    try:
        res = run_readonly(store.db_path(run_id), req.sql, limit=req.limit, timeout_s=10,
                           knobs=run.get("knob_values", {}), meta=(run.get("spec") or {}).get("meta", {}))
    except SQLGuardError as e:
        raise HTTPException(400, str(e))
    return res.to_dict()


@app.post("/api/runs/{run_id}/whatif")
async def start_whatif(run_id: str, req: WhatIfRequest) -> dict:
    run = _run_or_404(run_id)
    spec = _spec_of(run)
    overrides = {**run.get("knob_overrides", {}), **req.knob_overrides}
    for k, v in req.knob_overrides.items():
        knob = spec.knob(k)
        if not knob:
            raise HTTPException(400, f"unknown knob {k}")
        try:
            knob.parse(v)
        except ValueError as e:
            raise HTTPException(400, str(e))
    label = req.label or "What-if: " + ", ".join(f"{k}={v}" for k, v in req.knob_overrides.items())
    rr = RunRequest(spec=spec, knob_overrides=overrides, parent_run_id=run_id, label=label, repair=False)
    # same internal params as the parent (incl. repaired ones) -> differences come only from the knobs
    params = run.get("params", {})
    return {"job_id": jobs.start("whatif", lambda job: execute_run(rr, job, base_params=params)).id}


@app.get("/api/compare")
def compare(a: str, b: str) -> dict:
    _run_or_404(a)
    _run_or_404(b)
    return compare_runs(a, b)


@app.post("/api/runs/{run_id}/commentary")
async def run_commentary(run_id: str) -> dict:
    _run_or_404(run_id)
    try:
        return await commentary(run_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"AI komentář není dostupný: {e}")


@app.post("/api/runs/{run_id}/detective")
async def start_detective(run_id: str) -> dict:
    run = _run_or_404(run_id)
    if run.get("engine") == "scm":  # blind causal detective on the observed baseline (never the clean world)
        from .causal.service import legacy_detective_view
        return {"job_id": jobs.start("detective", lambda job: legacy_detective_view(run_id, job)).id}
    return {"job_id": jobs.start("detective", lambda job: investigate(run_id, job)).id}


@app.get("/api/runs/{run_id}/detective")
def get_detective(run_id: str) -> Any:
    _run_or_404(run_id)
    return store.load_json(run_id, "detective.json", None)


@app.get("/api/runs/{run_id}/export.zip")
def export(run_id: str) -> Response:
    _run_or_404(run_id)
    data = store.export_zip(run_id)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="story2data-{run_id}.zip"'})


@app.get("/api/runs/{run_id}/db")
def export_db(run_id: str) -> FileResponse:
    _run_or_404(run_id)
    return FileResponse(store.db_path(run_id), media_type="application/vnd.sqlite3",
                        filename=f"story2data-{run_id}.sqlite")


# =========================================================================== documents (unstructured outputs)
from .contracts.docs import DocInput, DocPlannerResponse, DocReplanRequest, DocRunRequest, WrittenDocument  # noqa: E402
from .documents.pipeline import execute_doc_run, plan_docs, replan_docs  # noqa: E402
from .documents.render import MIME, doc_text, preview_html  # noqa: E402


def _doc_run_or_404(run_id: str) -> dict:
    run = _run_or_404(run_id)
    if run.get("kind") != "documents":
        raise HTTPException(404, "not a document run")
    return run


@app.post("/api/docs/plan")
async def docs_start_plan(inp: DocInput) -> dict:
    if inp.source_run_id:
        _run_or_404(inp.source_run_id)

    async def work(job):
        job.emit("plan", "STRONG model navrhuje specifikaci dokumentů", 0.1)
        resp = await plan_docs(inp)
        sid = secrets.token_hex(6)
        _save_session(sid, {"session_id": sid, "kind": "documents", "round": 0, "input": inp.model_dump(),
                            "response": resp.model_dump(mode="json")})
        return {"session_id": sid}

    return {"job_id": jobs.start("doc_plan", work).id}


@app.get("/api/docs/plan/{sid}")
def docs_session(sid: str) -> dict:
    return _load_session(sid)


@app.post("/api/docs/plan/{sid}/replan")
async def docs_replan(sid: str, req: DocReplanRequest) -> dict:
    session = _load_session(sid)
    if session["round"] >= MAX_REPLAN_ROUNDS:
        raise HTTPException(400, f"maximum {MAX_REPLAN_ROUNDS} re-planning rounds reached")

    async def work(job):
        job.emit("plan", "Přeplánování dokumentů", 0.1)
        resp = await replan_docs(req)
        session["round"] += 1
        session["response"] = resp.model_dump(mode="json")
        _save_session(sid, session)
        return {"session_id": sid}

    return {"job_id": jobs.start("doc_replan", work).id}


@app.post("/api/docs/runs")
async def docs_start_run(req: DocRunRequest) -> dict:
    return {"job_id": jobs.start("doc_run", lambda job: execute_doc_run(req, job)).id}


@app.get("/api/docs/runs/{run_id}")
def docs_run(run_id: str) -> dict:
    run = _doc_run_or_404(run_id)
    run.pop("llm_calls", None)
    return run


@app.get("/api/docs/runs/{run_id}/doc/{doc_id}")
def docs_document(run_id: str, doc_id: int) -> dict:
    _doc_run_or_404(run_id)
    written = store.load_json(run_id, "written.json", {}) or {}
    if str(doc_id) not in written:
        raise HTTPException(404, "document not found")
    doc = WrittenDocument.model_validate(written[str(doc_id)])
    conn = sqlite3.connect(f"file:{store.db_path(run_id)}?mode=ro", uri=True)
    try:
        cur = conn.execute("SELECT * FROM facts WHERE doc_id = ?", (doc_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        conn.close()
    return {"doc_id": doc_id, "facts": dict(zip(cols, row)) if row else {}, "html": preview_html(doc),
            "text": doc_text(doc), "document": doc.model_dump()}


@app.get("/api/docs/runs/{run_id}/files/{fname}")
def docs_file(run_id: str, fname: str, download: bool = False) -> FileResponse:
    run = _doc_run_or_404(run_id)
    if not any(d["file"] == fname for d in run.get("documents", [])):
        raise HTTPException(404, "file not found")
    fmt = run["doc_spec"]["format"]
    return FileResponse(store.run_path(run_id) / "documents" / fname, media_type=MIME[fmt],
                        filename=fname if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/docs/runs/{run_id}/export.zip")
def docs_export(run_id: str) -> Response:
    import io
    import zipfile

    run = _doc_run_or_404(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in run.get("documents", []):
            z.write(store.run_path(run_id) / "documents" / d["file"], f"documents/{d['file']}")
        facts = store.read_table(run_id, "facts")
        z.writestr("facts.csv", facts.to_csv(index=False))
        z.writestr("doc_spec.json", json.dumps(run["doc_spec"], ensure_ascii=False, indent=1))
        z.writestr("README.txt", "Syntetické dokumenty vygenerované nástrojem Story2Data. Neobsahují skutečné osobní "
                                 "údaje. Ground truth (skrytý vzor) není součástí exportu.\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="story2data-docs-{run_id}.zip"'})


@app.get("/api/runs/{run_id}/doc-sources")
def doc_sources(run_id: str) -> list[dict]:
    """Tables of a data run that can be turned into files (1 row = 1 document) + suggested document type."""
    from .documents.sources import inspect_source, suggest_document

    run = _run_or_404(run_id)
    if run.get("kind") == "documents":
        raise HTTPException(400, "document runs cannot be a source")
    spec = _spec_of(run)
    out = []
    for t in spec.tables:
        info = inspect_source(run_id, t.name)
        out.append({"table": t.name, "description": t.description, "rows": run.get("row_counts", {}).get(t.name),
                    "suggestion": suggest_document(t.name),
                    "joins": [f"{fk.name} → {rt.name}" for fk, rt, _ in info.parents],
                    "items_from": info.child[0].name if info.child else None,
                    "columns": [c.name for c in t.columns]})
    out.sort(key=lambda x: (x["items_from"] is None, x["table"]))
    return out


# =========================================================================== causal core (SCM engine)
from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402

from .causal.contracts import (CorruptionConfig, DetectiveConfig, DifficultyConfig, EvaluationConfig,  # noqa: E402
                               SCMSpec)


class SCMEditRequest(_BaseModel):
    scm: SCMSpec
    operations: list[dict] = _Field(default_factory=list)


class ObserveRequest(_BaseModel):
    """Manual mode: one corruption configuration, measured difficulty (no optimisation)."""

    corruption: CorruptionConfig = _Field(default_factory=CorruptionConfig)
    detective: DetectiveConfig = _Field(default_factory=DetectiveConfig)
    evaluation: EvaluationConfig = _Field(default_factory=EvaluationConfig)
    repeats: int = _Field(1, ge=1, le=10)
    with_baseline: bool = True


class SCMImportRequest(_BaseModel):
    model: dict
    observation: ObservationSettings = _Field(default_factory=ObservationSettings)


@app.post("/api/scm/import")
def scm_import(req: SCMImportRequest) -> dict:
    from .causal.contracts import SCHEMA_VERSION
    from .causal.service import scenario_spec
    from .causal.validation import validate_spec as validate_scm
    from pydantic import ValidationError

    try:
        scm = SCMSpec.model_validate(req.model.get("scm", req.model), extra="forbid")
    except ValidationError as exc:
        details = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in exc.errors()[:5]]
        raise HTTPException(422, "Neplatná struktura SCM. " + "; ".join(details)) from exc
    if scm.meta.schema_version != SCHEMA_VERSION:
        raise HTTPException(422, f"Verzi SCM {scm.meta.schema_version} nelze načíst. Podporovaná verze je {SCHEMA_VERSION}.")
    try:
        rep = validate_scm(scm)
    except RecursionError as exc:
        raise HTTPException(422, "SCM obsahuje cyklus v definicích entit.") from exc
    if rep.errors:
        raise HTTPException(422, "SCM nelze spustit. "
                            + "; ".join(f"{c.target}: {c.explanation}" for c in rep.errors[:8]))
    story = scm.meta.original_story.strip()
    inp = ScenarioInput(story=story[:8000] if len(story) >= 10 else "Importovaný kauzální model.",
                        seed=scm.meta.seed, engine="scm", observation=req.observation)
    return _create_plan_session(inp, PlannerResponse(spec=scenario_spec(scm)))


def _scm_run_or_400(run_id: str) -> dict:
    run = _run_or_404(run_id)
    if run.get("engine") != "scm":
        raise HTTPException(400, "not an SCM run")
    return run


@app.get("/api/scm/mechanisms")
def scm_mechanisms() -> list[dict]:
    from .causal.mechanisms import catalog as mech_catalog
    return mech_catalog()


@app.get("/api/scm/corruptions")
def scm_corruptions() -> list[dict]:
    from .causal.corruption import INTENSITY_MEANING, REGISTRY
    from .causal.difficulty import DEFAULT_BOUNDS
    return [{"type": t, "intensity": INTENSITY_MEANING.get(t, ""), "default_bounds": DEFAULT_BOUNDS.get(t)} for t in REGISTRY]


@app.get("/api/scm/scenarios")
def scm_scenarios() -> list[dict]:
    from .causal.service import list_reference_scenarios
    return list_reference_scenarios()


@app.get("/api/scm/scenarios/{sid}")
def scm_scenario(sid: str) -> dict:
    from .causal.service import CausalError, load_reference_scenario, scenario_spec
    try:
        return scenario_spec(load_reference_scenario(sid)).model_dump(mode="json")
    except CausalError as e:
        raise HTTPException(404, str(e))


@app.post("/api/scm/validate")
def scm_validate(scm: SCMSpec) -> dict:
    from .causal.contracts import derive_edges
    from .causal.graph import GraphError, execution_plan
    from .causal.mechanisms import describe
    from .causal.validation import validate_spec as validate_scm
    rep = validate_scm(scm)
    plan = None
    try:
        p = execution_plan(scm)
        plan = {"pre": p.pre, "loop": p.loop, "post": p.post}
    except (GraphError, ValueError):
        pass
    return {"report": rep.model_dump(), "edges": [e.model_dump() for e in derive_edges(scm)],
            "mechanisms": {v.variable_id: describe(v) for v in scm.variables}, "execution_plan": plan}


@app.post("/api/scm/edit")
def scm_edit(req: SCMEditRequest) -> dict:
    from .causal.editing import EditOp, apply_edits
    from .causal.service import scenario_spec
    try:
        ops = [EditOp.model_validate(o) for o in req.operations]
    except ValueError as e:
        raise HTTPException(422, str(e))
    res = apply_edits(req.scm, ops)
    out = res.model_dump(mode="json")
    if res.report.status != "FAIL":
        out["spec"] = scenario_spec(res.scm).model_dump(mode="json")
    return out


@app.get("/api/runs/{run_id}/causal")
def run_causal(run_id: str) -> dict:
    from .causal.service import causal_summary
    _scm_run_or_400(run_id)
    return causal_summary(run_id)


@app.post("/api/runs/{run_id}/observe")
async def run_observe(run_id: str, req: ObserveRequest) -> dict:
    from .causal.service import manual_mode
    _scm_run_or_400(run_id)
    if req.detective.kind == "llm" and req.repeats > 5:
        raise HTTPException(400, "at most 5 repeats with the LLM detective")
    return {"job_id": jobs.start("observe", lambda job: manual_mode(
        run_id, req.corruption, req.detective, req.evaluation, req.repeats, req.with_baseline,
        progress=lambda m, f=None: job.emit("difficulty", m, f))).id}


@app.post("/api/runs/{run_id}/difficulty")
async def run_difficulty(run_id: str, cfg: DifficultyConfig) -> dict:
    from .causal.service import automatic_mode
    _scm_run_or_400(run_id)

    async def work(job):
        return await automatic_mode(run_id, cfg, progress=lambda m, f=None: job.emit("difficulty", m, f),
                                    should_stop=lambda: job.cancelled)

    return {"job_id": jobs.start("difficulty", work).id}


@app.get("/api/runs/{run_id}/difficulty/{did}")
def run_difficulty_result(run_id: str, did: str) -> dict:
    _scm_run_or_400(run_id)
    if not did.replace("-", "").isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "difficulty" / f"{did}.json"
    if not p.exists():
        raise HTTPException(404, "not found")
    return json.loads(read_saved_text(p))


@app.get("/api/runs/{run_id}/observations/{obs_id}")
def run_observation(run_id: str, obs_id: str) -> dict:
    from .causal.service import CausalError, _obs_dir
    _scm_run_or_400(run_id)
    try:
        d = _obs_dir(run_id, obs_id)
    except CausalError as e:
        raise HTTPException(400, str(e))
    if not (d / "info.json").exists():
        raise HTTPException(404, "observation not found")
    return {"info": json.loads(read_saved_text(d / "info.json")),
            "schema": json.loads(read_saved_text(d / "schema.json"))}


@app.get("/api/runs/{run_id}/observations/{obs_id}/db")
def run_observation_db(run_id: str, obs_id: str) -> FileResponse:
    from .causal.service import CausalError, _obs_dir
    _scm_run_or_400(run_id)
    try:
        d = _obs_dir(run_id, obs_id)
    except CausalError as e:
        raise HTTPException(400, str(e))
    if not (d / "observed.sqlite").exists():
        raise HTTPException(404, "observation not found")
    return FileResponse(d / "observed.sqlite", media_type="application/vnd.sqlite3",
                        filename=f"story2data-{run_id}-{obs_id}.sqlite")


@app.post("/api/runs/{run_id}/evaluations/{eval_id}/replay")
async def run_evaluation_replay(run_id: str, eval_id: str) -> dict:
    from .causal.service import CausalError, replay_evaluation
    _scm_run_or_400(run_id)
    try:
        return await replay_evaluation(run_id, eval_id)
    except CausalError as e:
        raise HTTPException(400, str(e))


@app.get("/api/runs/{run_id}/evaluations/{eval_id}/llm-calls")
def run_evaluation_llm_calls(run_id: str, eval_id: str) -> list[dict]:
    _scm_run_or_400(run_id)
    if not eval_id.isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "evaluations" / f"{eval_id}.llm_calls.jsonl"
    return [json.loads(x) for x in read_saved_text(p).splitlines() if x.strip()] if p.exists() else []


@app.get("/api/runs/{run_id}/evaluations/{eval_id}")
def run_evaluation(run_id: str, eval_id: str) -> dict:
    _scm_run_or_400(run_id)
    if not eval_id.isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "evaluations" / f"{eval_id}.json"
    if not p.exists():
        raise HTTPException(404, "evaluation not found")
    return json.loads(read_saved_text(p))
