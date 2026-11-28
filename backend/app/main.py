"""FastAPI application – REST + streaming API for the Story2Data frontend.

Security model (docs/AUTH_PLAN.md):
* AuthMiddleware authenticates every ``/api/*`` request with a Supabase access token, except the health check,
  read-only share links and one-time download tickets.
* Every run-scoped endpoint resolves the run through the metadata store *as the caller* (own runs + shared
  demo presets) before any file of the run is opened; foreign runs answer 404.
* Mutating analyses (detective, observations, difficulty, sharing, delete) require ownership.
* LLM-backed jobs pass the per-user rate limit and daily quotas first.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel as _BaseModel, Field as _Field

from . import job_handlers  # noqa: F401  (registers job kinds)
from . import quota
from .auth.deps import AuthMiddleware, Principal, current_user, invalidate_profile, require_admin
from .config import assert_safe_config, get_settings
from .contracts.io import (ObservationSettings, PlannerResponse, QueryRequest, ReplanRequest, RunRequest,
                           ScenarioInput, WhatIfRequest)
from .contracts.spec import ScenarioSpec
from .generation.scenarios.registry import catalog
from .job_handlers import create_session, load_session, plan_session_data
from .jobs import jobs, public_error
from .pipeline import run_job_state
from .presets.retail_demo import PRESETS
from .reporting.report import build_report, commentary, compare_runs
from .storage import runs as store
from .storage import workspace
from .storage.blobs import BlobError, get_blobs
from .storage.meta import get_meta
from .storage.sql_guard import SQLGuardError, run_readonly
from .storage.text_files import read_saved_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

_settings = get_settings()
assert_safe_config(_settings)

app = FastAPI(title="Story2Data", version="0.2.0",
              docs_url=None if _settings.use_supabase else "/api/docs",
              redoc_url=None, openapi_url=None if _settings.use_supabase else "/api/openapi.json")


class BodyLimitMiddleware:
    """Reject request bodies above MAX_REQUEST_BYTES (uploaded models, ontologies, specs)."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = get_settings().max_request_bytes
        headers = dict(scope.get("headers") or [])
        try:
            declared = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            declared = 0
        if declared > limit:
            return await _send_json(send, 413, {"detail": f"Požadavek je příliš velký (max {limit // 1000} kB)."})
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise HTTPException(413, "Požadavek je příliš velký.")
            return msg

        return await self.app(scope, limited, send)


async def _send_json(send, status: int, body: dict) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body, ensure_ascii=False).encode()})


# order: CORS (outermost) -> body limit -> auth -> app
app.add_middleware(AuthMiddleware)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=_settings.cors_origins,
                   allow_origin_regex=_settings.cors_origin_regex or None, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"],
                   max_age=600)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
    ref = secrets.token_hex(3)
    log.exception("unhandled error on %s %s [ref %s]", request.method, request.url.path, ref)
    return JSONResponse({"detail": f"Interní chyba serveru (ref {ref})."}, status_code=500)


@app.exception_handler(RequestValidationError)
async def _invalid(request: Request, exc: RequestValidationError):
    errs = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()[:10]]
    return JSONResponse({"detail": errs}, status_code=422)


# --------------------------------------------------------------------------- access helpers
class ShareGrant:
    """Read-only access to exactly one run, obtained from a share token."""

    def __init__(self, meta: dict[str, Any]):
        self.meta = meta
        self.run_id = meta["id"]
        self.id = ""


Who = Union[Principal, ShareGrant]


def _meta_or_404(run_id: str, who: Who) -> dict[str, Any]:
    try:
        store.check_run_id(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run id")
    if isinstance(who, ShareGrant):
        if who.run_id != run_id:
            raise HTTPException(404, "run not found")
        meta = who.meta
    else:
        meta = get_meta().get_run(run_id, who.viewer)
    if not meta:
        raise HTTPException(404, "run not found")
    try:
        workspace.ensure_local(meta)
    except BlobError:
        raise HTTPException(503, "Data běhu jsou dočasně nedostupná.")
    return meta


def _run_or_404(run_id: str, who: Who) -> dict:
    meta = _meta_or_404(run_id, who)
    run = store.load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    run["is_preset"] = bool(meta.get("is_preset"))
    run["own"] = not isinstance(who, ShareGrant) and meta.get("user_id") == who.id
    if not isinstance(who, ShareGrant) and run["own"]:
        run["share_token"] = meta.get("share_token")
    run.pop("user_id", None)
    return run


def _own_run_or_404(run_id: str, user: Principal) -> dict:
    """Mutations: only the owner (presets are read-only for everybody else)."""
    meta = _meta_or_404(run_id, user)
    if meta.get("user_id") != user.id:
        raise HTTPException(403, "Ukázkový běh je jen pro čtení – vytvořte si vlastní běh nebo what-if.")
    run = store.load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


def _spec_of(run: dict) -> ScenarioSpec:
    return ScenarioSpec.model_validate(run["spec"])


def _start(kind: str, user: Principal, state: dict, run_id: Optional[str] = None) -> dict:
    quota.check_can_start(user.id, kind, user.is_admin)
    return {"job_id": jobs.start(kind, user.id, state, run_id=run_id).id}


def _session_or_404(sid: str, user: Principal) -> dict:
    s = load_session(sid, user.id)
    if s is None:
        raise HTTPException(404, "session not found")
    return s


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "mock": s.mock, "has_key": bool(s.gemini_api_key), "strong_model": s.strong_model,
            "strong_fallbacks": s.strong_fallbacks, "cheap_model": s.cheap_model, "cheap_fallbacks": s.cheap_fallbacks,
            "auth": "disabled" if s.auth_disabled else "supabase", "signup_mode": s.signup_mode,
            "storage": "supabase" if s.use_supabase else "local"}


@app.get("/api/me")
def me(user: Principal = Depends(current_user)) -> dict:
    p = get_meta().get_profile(user.id) or {}
    return {"id": user.id, "email": user.email or p.get("email"), "role": user.role,
            "display_name": p.get("display_name", ""), "quota": quota.status(user.id, user.is_admin),
            "signup_mode": get_settings().signup_mode, "auth_disabled": get_settings().auth_disabled}


class ProfilePatch(_BaseModel):
    display_name: str = _Field("", max_length=80)


@app.patch("/api/me")
def me_update(body: ProfilePatch, user: Principal = Depends(current_user)) -> dict:
    get_meta().update_profile(user.id, {"display_name": body.display_name.strip()})
    invalidate_profile(user.id)
    return {"ok": True}


@app.get("/api/presets")
def presets() -> list[dict]:
    return list(PRESETS.values())


@app.get("/api/modules")
def modules() -> list[dict]:
    return catalog()


# --------------------------------------------------------------------------- jobs
@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: Principal = Depends(current_user)) -> dict:
    job = jobs.get_for(job_id, user.id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str, user: Principal = Depends(current_user)) -> dict:
    job = jobs.get_for(job_id, user.id)
    if not job:
        raise HTTPException(404, "job not found")
    jobs.request_cancel(job)
    return {"id": job.id, "cancel_requested": True, "status": job.status}


def _sse(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/advance")
async def job_advance(job_id: str, since: int = Query(0, ge=0), user: Principal = Depends(current_user)):
    """Run the job's next stage (or follow it) and stream progress as SSE over a normal authenticated fetch."""
    job = jobs.get_for(job_id, user.id)
    if not job:
        raise HTTPException(404, "job not found")
    return _sse(jobs.advance(job, since=since))


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, since: int = Query(0, ge=0), user: Principal = Depends(current_user)):
    """Backwards-compatible alias of advance (GET, authenticated)."""
    return await job_advance(job_id, since, user)


# --------------------------------------------------------------------------- planning
@app.post("/api/plan")
async def start_plan(inp: ScenarioInput, user: Principal = Depends(current_user)) -> dict:
    return _start("plan", user, {"input": inp.model_dump(mode="json")})


@app.get("/api/plan/{sid}")
def get_session(sid: str, user: Principal = Depends(current_user)) -> dict:
    return _session_or_404(sid, user)


@app.post("/api/plan/{sid}/replan")
async def start_replan(sid: str, req: ReplanRequest, user: Principal = Depends(current_user)) -> dict:
    session = _session_or_404(sid, user)
    if session["round"] >= job_handlers.MAX_REPLAN_ROUNDS:
        raise HTTPException(400, f"maximum {job_handlers.MAX_REPLAN_ROUNDS} re-planning rounds reached")
    return _start("replan", user, {"session_id": sid, "request": req.model_dump(mode="json")})


@app.post("/api/spec/validate")
def validate_spec(spec: ScenarioSpec) -> dict:
    if spec.scm is not None:
        from .causal.validation import validate_spec as validate_scm
        rep = validate_scm(spec.scm)
        return {"errors": [f"{c.target}: {c.explanation}" for c in rep.errors], "order": [], "scm_report": rep.model_dump()}
    return {"errors": spec.structural_errors(), "order": spec.topological_order() if not spec.structural_errors() else []}


# --------------------------------------------------------------------------- runs
@app.post("/api/runs")
async def start_run(req: RunRequest, user: Principal = Depends(current_user)) -> dict:
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
    if req.parent_run_id:
        _meta_or_404(req.parent_run_id, user)
    return _start("run", user, run_job_state(req))


@app.get("/api/runs")
def runs_list(user: Principal = Depends(current_user)) -> list[dict]:
    return [store.list_item(m, user.id) for m in get_meta().list_runs(user.viewer)]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _detail(run_id, user)


def _detail(run_id: str, who: Who) -> dict:
    run = _run_or_404(run_id, who)
    run.pop("llm_calls", None)
    if isinstance(who, ShareGrant):
        run.pop("input", None)
        run.pop("job_id", None)
    return run


@app.delete("/api/runs/{run_id}")
def run_delete(run_id: str, user: Principal = Depends(current_user)) -> dict:
    meta = _meta_or_404(run_id, user)
    if meta.get("user_id") != user.id:
        raise HTTPException(403, "Smazat lze jen vlastní běh.")
    workspace.delete_remote(run_id, user.id, meta.get("files") or {})
    get_meta().delete_run(run_id)
    if not get_settings().use_supabase:
        import shutil
        shutil.rmtree(store.run_path(run_id), ignore_errors=True)
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/llm-calls")
def run_llm_calls(run_id: str, user: Principal = Depends(current_user)) -> list[dict]:
    return _run_or_404(run_id, user).get("llm_calls", [])


@app.get("/api/runs/{run_id}/report")
def run_report(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _report(run_id, user)


def _report(run_id: str, who: Who) -> dict:
    _meta_or_404(run_id, who)
    return build_report(run_id)


@app.get("/api/runs/{run_id}/dashboard")
def run_dashboard(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _dashboard(run_id, user)


def _dashboard(run_id: str, who: Who) -> dict:
    run = _run_or_404(run_id, who)
    return {"meta": run.get("dashboard", {}), "widgets": store.load_json(run_id, "dashboard.json", [])}


@app.get("/api/runs/{run_id}/ground-truth")
def run_ground_truth(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _ground_truth(run_id, user)


def _ground_truth(run_id: str, who: Who) -> dict:
    _meta_or_404(run_id, who)
    return store.load_json(run_id, "manifest.json", {}) or {}


@app.get("/api/runs/{run_id}/schema")
def run_schema(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _schema(run_id, user)


def _schema(run_id: str, who: Who) -> dict:
    run = _run_or_404(run_id, who)
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
              filter_val: Optional[str] = None, filter_op: str = "eq",
              user: Principal = Depends(current_user)) -> dict:
    return _table(run_id, table, page, page_size, sort, desc, filter_col, filter_val, filter_op, user)


def _table(run_id: str, table: str, page: int, page_size: int, sort: Optional[str], desc: bool,
           filter_col: Optional[str], filter_val: Optional[str], filter_op: str, who: Who) -> dict:
    run = _run_or_404(run_id, who)
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
def run_query(run_id: str, req: QueryRequest, user: Principal = Depends(current_user)) -> dict:
    return _query(run_id, req, user)


def _query(run_id: str, req: QueryRequest, who: Who) -> dict:
    run = _run_or_404(run_id, who)
    try:  # read-only connection to THIS run's database only (opened after the ownership check above)
        res = run_readonly(store.db_path(run_id), req.sql, limit=req.limit, timeout_s=10,
                           knobs=run.get("knob_values", {}), meta=(run.get("spec") or {}).get("meta", {}))
    except SQLGuardError as e:
        raise HTTPException(400, str(e))
    return res.to_dict()


@app.post("/api/runs/{run_id}/whatif")
async def start_whatif(run_id: str, req: WhatIfRequest, user: Principal = Depends(current_user)) -> dict:
    run = _run_or_404(run_id, user)
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
    return _start("whatif", user, run_job_state(rr, run.get("params", {})))


@app.get("/api/compare")
def compare(a: str, b: str, user: Principal = Depends(current_user)) -> dict:
    _meta_or_404(a, user)
    _meta_or_404(b, user)
    return compare_runs(a, b)


@app.post("/api/runs/{run_id}/commentary")
async def run_commentary(run_id: str, user: Principal = Depends(current_user)) -> dict:
    from .llm.client import UsageTracker, usage_scope

    _meta_or_404(run_id, user)
    quota.check_rate_limit(user.id)
    quota.check_tokens(user.id, user.is_admin)
    tracker = UsageTracker()
    try:
        with usage_scope(tracker):
            return await commentary(run_id)
    except Exception as e:  # noqa: BLE001
        log.warning("commentary for %s failed: %s", run_id, e)
        raise HTTPException(503, "AI komentář není dostupný. Zkuste to později.")
    finally:
        quota.record(user.id, kind="commentary", summary=tracker.summary(), run_id=run_id, mock=get_settings().mock)


@app.post("/api/runs/{run_id}/detective")
async def start_detective(run_id: str, user: Principal = Depends(current_user)) -> dict:
    _own_run_or_404(run_id, user)
    return _start("detective", user, {"run_id": run_id}, run_id=run_id)


@app.get("/api/runs/{run_id}/detective")
def get_detective(run_id: str, user: Principal = Depends(current_user)) -> Any:
    _meta_or_404(run_id, user)
    return store.load_json(run_id, "detective.json", None)


# --------------------------------------------------------------------------- sharing
class ShareRequest(_BaseModel):
    enabled: bool = True


@app.post("/api/runs/{run_id}/share")
def run_share(run_id: str, body: ShareRequest, user: Principal = Depends(current_user)) -> dict:
    meta = _meta_or_404(run_id, user)
    if meta.get("user_id") != user.id:
        raise HTTPException(403, "Sdílet lze jen vlastní běh.")
    token = secrets.token_urlsafe(32) if body.enabled else None
    get_meta().upsert_run({"id": run_id, "user_id": user.id, "share_token": token})
    return {"share_token": token}


# --------------------------------------------------------------------------- downloads
class DownloadRequest(_BaseModel):
    what: str = _Field(..., pattern="^(db|zip|obs_db|doc_file|docs_zip)$")
    obs_id: Optional[str] = _Field(None, max_length=80)
    file: Optional[str] = _Field(None, max_length=200)
    inline: bool = False


_tickets: dict[str, tuple[float, Path, str, str, bool]] = {}
_tickets_lock = threading.Lock()
TICKET_TTL_S = 60


def _ticket(path: Path, filename: str, media_type: str, inline: bool) -> str:
    t = secrets.token_urlsafe(24)
    with _tickets_lock:
        now = time.time()
        for k in [k for k, v in _tickets.items() if v[0] < now]:
            _tickets.pop(k, None)
        _tickets[t] = (now + TICKET_TTL_S, path, filename, media_type, inline)
    return f"/api/download/{t}"


@app.get("/api/download/{ticket}")
def download_ticket(ticket: str):
    """One-time, short-lived local download (the ticket is the credential – no access token in URLs)."""
    with _tickets_lock:
        item = _tickets.get(ticket)
        if item and not item[4]:  # attachments are single use; inline previews may be re-requested by the viewer
            _tickets.pop(ticket, None)
    if not item or item[0] < time.time() or not item[1].exists():
        raise HTTPException(404, "Odkaz ke stažení vypršel.")
    _, path, filename, media_type, inline = item
    return FileResponse(path, media_type=media_type, filename=None if inline else filename,
                        content_disposition_type="inline" if inline else "attachment")


def _export_zip_file(run_id: str, run: dict, what: str) -> Path:
    data = _docs_zip(run_id, run) if what == "docs_zip" else store.export_zip(run_id)
    out = Path(tempfile.mkdtemp(prefix="s2d-export-", dir=get_settings().data_dir)) / f"{run_id}.zip"
    out.write_bytes(data)
    return out


@app.post("/api/runs/{run_id}/download")
def run_download(run_id: str, req: DownloadRequest, user: Principal = Depends(current_user)) -> dict:
    from .documents.render import MIME

    meta = _meta_or_404(run_id, user)
    run = store.load_run(run_id) or {}
    if req.what == "db":
        rel, name, mt = "data.sqlite", f"story2data-{run_id}.sqlite", "application/vnd.sqlite3"
    elif req.what == "obs_db":
        from .causal.service import CausalError, _obs_dir
        try:
            d = _obs_dir(run_id, req.obs_id or "")
        except CausalError as e:
            raise HTTPException(400, str(e))
        rel, name, mt = f"observations/{d.name}/observed.sqlite", f"story2data-{run_id}-{d.name}.sqlite", \
            "application/vnd.sqlite3"
    elif req.what == "doc_file":
        if run.get("kind") != "documents" or not any(d["file"] == req.file for d in run.get("documents", [])):
            raise HTTPException(404, "file not found")
        rel, name, mt = f"documents/{req.file}", str(req.file), MIME[run["doc_spec"]["format"]]
    else:  # zip / docs_zip – built on demand
        if req.what == "docs_zip" and run.get("kind") != "documents":
            raise HTTPException(404, "not a document run")
        name = f"story2data-{'docs-' if req.what == 'docs_zip' else ''}{run_id}.zip"
        path = _export_zip_file(run_id, run, req.what)
        blobs = get_blobs()
        if blobs is None:
            return {"url": _ticket(path, name, "application/zip", False), "filename": name}
        key = f"{user.id}/_exports/{run_id}-{req.what}.zip"
        try:
            blobs.upload(key, path, "application/zip")
            return {"url": blobs.signed_url(key, 120, name), "filename": name}
        finally:
            path.unlink(missing_ok=True)
    local = store.run_path(run_id) / rel
    if not local.exists():
        raise HTTPException(404, "file not found")
    blobs = get_blobs()
    if blobs is None:
        return {"url": _ticket(local, name, mt, req.inline), "filename": name}
    info = (meta.get("files") or {}).get(rel)
    if not info:
        raise HTTPException(404, "file not found")
    if info.get("gz"):
        name += ".gz"
    key = workspace.object_key(meta["user_id"], run_id, rel, info.get("gz", False))
    return {"url": blobs.signed_url(key, 120, "" if req.inline else name), "filename": name,
            "compressed": bool(info.get("gz"))}


# legacy direct-download URLs (authenticated fetch only; the UI uses /download tickets)
@app.get("/api/runs/{run_id}/export.zip")
def export(run_id: str, user: Principal = Depends(current_user)) -> Response:
    _meta_or_404(run_id, user)
    data = store.export_zip(run_id)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="story2data-{run_id}.zip"'})


@app.get("/api/runs/{run_id}/db")
def export_db(run_id: str, user: Principal = Depends(current_user)) -> FileResponse:
    _meta_or_404(run_id, user)
    return FileResponse(store.db_path(run_id), media_type="application/vnd.sqlite3",
                        filename=f"story2data-{run_id}.sqlite")


# =========================================================================== documents (unstructured outputs)
from .contracts.docs import DocInput, DocReplanRequest, DocRunRequest, WrittenDocument  # noqa: E402
from .documents.render import MIME, doc_text, preview_html  # noqa: E402


def _doc_run_or_404(run_id: str, who: Who) -> dict:
    run = _run_or_404(run_id, who)
    if run.get("kind") != "documents":
        raise HTTPException(404, "not a document run")
    return run


@app.post("/api/docs/plan")
async def docs_start_plan(inp: DocInput, user: Principal = Depends(current_user)) -> dict:
    if inp.source_run_id:
        _meta_or_404(inp.source_run_id, user)
    return _start("doc_plan", user, {"input": inp.model_dump(mode="json"),
                                     "read_runs": [inp.source_run_id] if inp.source_run_id else []})


@app.get("/api/docs/plan/{sid}")
def docs_session(sid: str, user: Principal = Depends(current_user)) -> dict:
    return _session_or_404(sid, user)


@app.post("/api/docs/plan/{sid}/replan")
async def docs_replan(sid: str, req: DocReplanRequest, user: Principal = Depends(current_user)) -> dict:
    session = _session_or_404(sid, user)
    if session["round"] >= job_handlers.MAX_REPLAN_ROUNDS:
        raise HTTPException(400, f"maximum {job_handlers.MAX_REPLAN_ROUNDS} re-planning rounds reached")
    src = (session.get("input") or {}).get("source_run_id")
    return _start("doc_replan", user, {"session_id": sid, "request": req.model_dump(mode="json"),
                                       "read_runs": [src] if src else []})


@app.post("/api/docs/runs")
async def docs_start_run(req: DocRunRequest, user: Principal = Depends(current_user)) -> dict:
    src = getattr(req.spec, "source_run_id", None)
    if src:
        _meta_or_404(src, user)
    return _start("doc_run", user, {"request": req.model_dump(mode="json"), "read_runs": [src] if src else []})


@app.get("/api/docs/runs/{run_id}")
def docs_run(run_id: str, user: Principal = Depends(current_user)) -> dict:
    run = _doc_run_or_404(run_id, user)
    run.pop("llm_calls", None)
    return run


@app.get("/api/docs/runs/{run_id}/doc/{doc_id}")
def docs_document(run_id: str, doc_id: int, user: Principal = Depends(current_user)) -> dict:
    _doc_run_or_404(run_id, user)
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
def docs_file(run_id: str, fname: str, download: bool = False, user: Principal = Depends(current_user)) -> FileResponse:
    run = _doc_run_or_404(run_id, user)
    if not any(d["file"] == fname for d in run.get("documents", [])):
        raise HTTPException(404, "file not found")
    fmt = run["doc_spec"]["format"]
    return FileResponse(store.run_path(run_id) / "documents" / fname, media_type=MIME[fmt],
                        filename=fname if download else None,
                        content_disposition_type="attachment" if download else "inline")


def _docs_zip(run_id: str, run: dict) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in run.get("documents", []):
            z.write(store.run_path(run_id) / "documents" / d["file"], f"documents/{d['file']}")
        facts = store.read_table(run_id, "facts")
        z.writestr("facts.csv", facts.to_csv(index=False))
        z.writestr("doc_spec.json", json.dumps(run["doc_spec"], ensure_ascii=False, indent=1))
        z.writestr("README.txt", "Syntetické dokumenty vygenerované nástrojem Story2Data. Neobsahují skutečné osobní "
                                 "údaje. Ground truth (skrytý vzor) není součástí exportu.\n")
    return buf.getvalue()


@app.get("/api/docs/runs/{run_id}/export.zip")
def docs_export(run_id: str, user: Principal = Depends(current_user)) -> Response:
    run = _doc_run_or_404(run_id, user)
    return Response(_docs_zip(run_id, run), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="story2data-docs-{run_id}.zip"'})


@app.get("/api/runs/{run_id}/doc-sources")
def doc_sources(run_id: str, user: Principal = Depends(current_user)) -> list[dict]:
    """Tables of a data run that can be turned into files (1 row = 1 document) + suggested document type."""
    from .documents.sources import inspect_source, suggest_document

    run = _run_or_404(run_id, user)
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
from .causal.contracts import (CorruptionConfig, DetectiveConfig, DifficultyConfig, EvaluationConfig,  # noqa: E402
                               SCMSpec)


class SCMEditRequest(_BaseModel):
    scm: SCMSpec
    operations: list[dict] = _Field(default_factory=list, max_length=500)


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
def scm_import(req: SCMImportRequest, user: Principal = Depends(current_user)) -> dict:
    from pydantic import ValidationError

    from .causal.contracts import SCHEMA_VERSION
    from .causal.service import scenario_spec
    from .causal.validation import validate_spec as validate_scm

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
    return {"session_id": create_session(user.id, plan_session_data(inp, PlannerResponse(spec=scenario_spec(scm))))}


def _scm_run_or_400(run_id: str, user: Who, own: bool = False) -> dict:
    run = _own_run_or_404(run_id, user) if own else _run_or_404(run_id, user)
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
def run_causal(run_id: str, user: Principal = Depends(current_user)) -> dict:
    return _causal(run_id, user)


def _causal(run_id: str, who: Who) -> dict:
    from .causal.service import causal_summary
    _scm_run_or_400(run_id, who)
    return causal_summary(run_id)


@app.post("/api/runs/{run_id}/observe")
async def run_observe(run_id: str, req: ObserveRequest, user: Principal = Depends(current_user)) -> dict:
    _scm_run_or_400(run_id, user, own=True)
    if req.detective.kind == "llm" and req.repeats > 5:
        raise HTTPException(400, "at most 5 repeats with the LLM detective")
    state = {"run_id": run_id, "request": req.model_dump(mode="json")}
    if req.detective.kind != "llm":  # statistical detective: no LLM cost -> rate limit only
        quota.check_rate_limit(user.id)
        return {"job_id": jobs.start("observe", user.id, state, run_id=run_id).id}
    return _start("observe", user, state, run_id=run_id)


@app.post("/api/runs/{run_id}/difficulty")
async def run_difficulty(run_id: str, cfg: DifficultyConfig, user: Principal = Depends(current_user)) -> dict:
    _scm_run_or_400(run_id, user, own=True)
    return _start("difficulty", user, {"run_id": run_id, "request": cfg.model_dump(mode="json")}, run_id=run_id)


@app.get("/api/runs/{run_id}/difficulty/{did}")
def run_difficulty_result(run_id: str, did: str, user: Principal = Depends(current_user)) -> dict:
    _scm_run_or_400(run_id, user)
    if not did.replace("-", "").isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "difficulty" / f"{did}.json"
    if not p.exists():
        raise HTTPException(404, "not found")
    return json.loads(read_saved_text(p))


@app.get("/api/runs/{run_id}/observations/{obs_id}")
def run_observation(run_id: str, obs_id: str, user: Principal = Depends(current_user)) -> dict:
    from .causal.service import CausalError, _obs_dir
    _scm_run_or_400(run_id, user)
    try:
        d = _obs_dir(run_id, obs_id)
    except CausalError as e:
        raise HTTPException(400, str(e))
    if not (d / "info.json").exists():
        raise HTTPException(404, "observation not found")
    return {"info": json.loads(read_saved_text(d / "info.json")),
            "schema": json.loads(read_saved_text(d / "schema.json"))}


@app.get("/api/runs/{run_id}/observations/{obs_id}/db")
def run_observation_db(run_id: str, obs_id: str, user: Principal = Depends(current_user)) -> FileResponse:
    from .causal.service import CausalError, _obs_dir
    _scm_run_or_400(run_id, user)
    try:
        d = _obs_dir(run_id, obs_id)
    except CausalError as e:
        raise HTTPException(400, str(e))
    if not (d / "observed.sqlite").exists():
        raise HTTPException(404, "observation not found")
    return FileResponse(d / "observed.sqlite", media_type="application/vnd.sqlite3",
                        filename=f"story2data-{run_id}-{obs_id}.sqlite")


@app.post("/api/runs/{run_id}/evaluations/{eval_id}/replay")
async def run_evaluation_replay(run_id: str, eval_id: str, user: Principal = Depends(current_user)) -> dict:
    from .causal.service import CausalError, replay_evaluation
    _scm_run_or_400(run_id, user, own=True)
    try:
        return await replay_evaluation(run_id, eval_id)
    except CausalError as e:
        raise HTTPException(400, str(e))


@app.get("/api/runs/{run_id}/evaluations/{eval_id}/llm-calls")
def run_evaluation_llm_calls(run_id: str, eval_id: str, user: Principal = Depends(current_user)) -> list[dict]:
    _scm_run_or_400(run_id, user)
    if not eval_id.isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "evaluations" / f"{eval_id}.llm_calls.jsonl"
    return [json.loads(x) for x in read_saved_text(p).splitlines() if x.strip()] if p.exists() else []


@app.get("/api/runs/{run_id}/evaluations/{eval_id}")
def run_evaluation(run_id: str, eval_id: str, user: Principal = Depends(current_user)) -> dict:
    _scm_run_or_400(run_id, user)
    if not eval_id.isalnum():
        raise HTTPException(400, "invalid id")
    p = store.run_path(run_id) / "evaluations" / f"{eval_id}.json"
    if not p.exists():
        raise HTTPException(404, "evaluation not found")
    return json.loads(read_saved_text(p))


# =========================================================================== public read-only share links
_share_hits: dict[str, list[float]] = {}
SHARE_RATE_PER_MIN = 120


def _share(token: str) -> ShareGrant:
    if not token or len(token) < 32 or len(token) > 128:
        raise HTTPException(404, "Odkaz neexistuje nebo byl zrušen.")
    meta = get_meta().run_by_share_token(token)
    if not meta or not secrets.compare_digest(str(meta.get("share_token") or ""), token):
        raise HTTPException(404, "Odkaz neexistuje nebo byl zrušen.")
    now = time.time()  # per-link rate limit (only valid links are tracked, so the dict stays small)
    hits = [t for t in _share_hits.get(token, []) if now - t < 60]
    if len(hits) >= SHARE_RATE_PER_MIN:
        raise HTTPException(429, "Příliš mnoho požadavků na sdílený odkaz.")
    _share_hits[token] = hits + [now]
    return ShareGrant(meta)


@app.get("/api/share/{token}")
def share_detail(token: str) -> dict:
    g = _share(token)
    out = _detail(g.run_id, g)
    out["shared"] = True
    return out


@app.get("/api/share/{token}/report")
def share_report(token: str) -> dict:
    g = _share(token)
    return _report(g.run_id, g)


@app.get("/api/share/{token}/dashboard")
def share_dashboard(token: str) -> dict:
    g = _share(token)
    return _dashboard(g.run_id, g)


@app.get("/api/share/{token}/ground-truth")
def share_ground_truth(token: str) -> dict:
    g = _share(token)
    return _ground_truth(g.run_id, g)


@app.get("/api/share/{token}/schema")
def share_schema(token: str) -> dict:
    g = _share(token)
    return _schema(g.run_id, g)


@app.get("/api/share/{token}/causal")
def share_causal(token: str) -> dict:
    g = _share(token)
    return _causal(g.run_id, g)


@app.get("/api/share/{token}/tables/{table}")
def share_table(token: str, table: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                sort: Optional[str] = None, desc: bool = False, filter_col: Optional[str] = None,
                filter_val: Optional[str] = None, filter_op: str = "eq") -> dict:
    g = _share(token)
    return _table(g.run_id, table, page, page_size, sort, desc, filter_col, filter_val, filter_op, g)


@app.post("/api/share/{token}/query")
def share_query(token: str, req: QueryRequest) -> dict:
    g = _share(token)
    return _query(g.run_id, req.model_copy(update={"limit": min(req.limit, 1000)}), g)


# =========================================================================== admin
class AdminUserPatch(_BaseModel):
    status: Optional[str] = _Field(None, pattern="^(active|blocked)$")
    role: Optional[str] = _Field(None, pattern="^(user|admin)$")
    approved: Optional[bool] = None


class InviteRequest(_BaseModel):
    email: str = _Field(..., min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PresetRequest(_BaseModel):
    is_preset: bool = True


@app.get("/api/admin/users")
def admin_users(admin: Principal = Depends(require_admin)) -> dict:
    meta = get_meta()
    today = quota.day_start()
    usage_today = meta.usage_since(None, today)
    by_user: dict[str, dict[str, Any]] = {}
    for r in usage_today:
        b = by_user.setdefault(r["user_id"], {"tokens": 0, "calls": 0, "cost_usd": 0.0})
        b["tokens"] += int(r.get("tokens") or 0)
        b["calls"] += int(r.get("calls") or 0)
        b["cost_usd"] += float(r.get("cost_usd") or 0)
    users = []
    for p in meta.list_profiles():
        u = by_user.get(p["id"], {"tokens": 0, "calls": 0, "cost_usd": 0.0})
        users.append({**{k: p.get(k) for k in ("id", "email", "display_name", "role", "status", "approved",
                                                 "created_at")},
                      "today": {**u, "cost_usd": round(u["cost_usd"], 4),
                                "runs": meta.count_jobs_since(p["id"], today, quota.GENERATION_KINDS)}})
    return {"users": users, "signup_mode": get_settings().signup_mode,
            "totals_today": {"tokens": sum(u["today"]["tokens"] for u in users),
                             "cost_usd": round(sum(u["today"]["cost_usd"] for u in users), 4)}}


@app.patch("/api/admin/users/{uid}")
def admin_update_user(uid: str, body: AdminUserPatch, admin: Principal = Depends(require_admin)) -> dict:
    if uid == admin.id and (body.status == "blocked" or body.role == "user"):
        raise HTTPException(400, "Nemůžete zablokovat ani degradovat sám sebe.")
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Nic ke změně.")
    p = get_meta().update_profile(uid, fields)
    if p is None:
        raise HTTPException(404, "user not found")
    invalidate_profile(uid)
    if "status" in fields and get_settings().use_supabase:
        from .auth.admin import set_ban
        set_ban(uid, fields["status"] == "blocked")
    return {k: p.get(k) for k in ("id", "email", "role", "status", "approved")}


@app.post("/api/admin/invite")
def admin_invite(body: InviteRequest, request: Request, admin: Principal = Depends(require_admin)) -> dict:
    if not get_settings().use_supabase:
        raise HTTPException(400, "Pozvánky vyžadují Supabase Auth.")
    from .auth.admin import invite
    origin = request.headers.get("origin") or ""
    return invite(body.email.strip().lower(), redirect_to=f"{origin}/auth/nastaveni-hesla" if origin else "")


@app.post("/api/admin/runs/{run_id}/preset")
def admin_preset(run_id: str, body: PresetRequest, admin: Principal = Depends(require_admin)) -> dict:
    meta = get_meta().get_run(run_id, None)
    if not meta:
        raise HTTPException(404, "run not found")
    get_meta().upsert_run({"id": run_id, "user_id": meta["user_id"], "is_preset": body.is_preset})
    return {"id": run_id, "is_preset": body.is_preset}


__all__ = ["app", "public_error"]
