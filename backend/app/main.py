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
from .contracts.io import (PlannerResponse, QueryRequest, ReplanRequest, RunRequest, ScenarioInput, WhatIfRequest)
from .contracts.spec import ScenarioSpec
from .detective import investigate
from .generation.scenarios.registry import catalog
from .jobs import jobs
from .pipeline import execute_run
from .planner import plan_scenario, replan
from .presets.retail_demo import PRESETS
from .reporting.report import build_report, commentary, compare_runs
from .storage import runs as store
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
    (_sessions_dir() / f"{sid}.json").write_text(json.dumps(data, ensure_ascii=False, default=str))


def _load_session(sid: str) -> dict:
    if not sid.isalnum():
        raise HTTPException(400, "invalid session id")
    p = _sessions_dir() / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    return json.loads(p.read_text())


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
    return {"ok": True, "mock": s.mock, "has_key": bool(s.gemini_api_key), "strong_model": s.strong_model,
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
@app.post("/api/plan")
async def start_plan(inp: ScenarioInput) -> dict:
    async def work(job):
        job.emit("plan", "STRONG model navrhuje ScenarioSpec", 0.1)
        resp = await plan_scenario(inp)
        sid = secrets.token_hex(6)
        session = {"session_id": sid, "round": 0, "input": inp.model_dump(), "response": resp.model_dump(mode="json"),
                   "history": []}
        _save_session(sid, session)
        return {"session_id": sid}

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
        resp = await replan(req, prev.suggestions, session["round"] + 1)
        session["history"].append({"round": session["round"], "feedback": req.feedback, "answers": req.answers,
                                   "accepted": req.accepted_suggestions, "rejected": req.rejected_suggestions})
        session["round"] += 1
        session["response"] = resp.model_dump(mode="json")
        _save_session(sid, session)
        return {"session_id": sid}

    return {"job_id": jobs.start("replan", work).id}


@app.post("/api/spec/validate")
def validate_spec(spec: ScenarioSpec) -> dict:
    return {"errors": spec.structural_errors(), "order": spec.topological_order() if not spec.structural_errors() else []}


# --------------------------------------------------------------------------- runs
@app.post("/api/runs")
async def start_run(req: RunRequest) -> dict:
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
    return {"meta": run.get("dashboard", {}), "widgets": store.load_json(run_id, "dashboard.json", [])}


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
                           knobs=run.get("knob_values", {}), meta=run["spec"]["meta"])
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
    _run_or_404(run_id)
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
