"""Stage handlers for every job kind (registered with app.jobs.register).

Handlers re-create their inputs from ``job.state`` (JSON), so a stage can run in any process/instance.
The owner of a job is ``job.user_id``; runs a stage reads are listed in ``job.state["read_runs"]`` and are
re-authorised (own or preset) and downloaded to the workspace by the job runner before the stage starts.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Optional

from .contracts.io import PlannerResponse, ReplanRequest, ScenarioInput
from .jobs import Job, UserFacingError, register
from .pipeline import RUN_STAGES
from .storage.meta import get_meta

MAX_REPLAN_ROUNDS = 4


# --------------------------------------------------------------------------- planning sessions
def create_session(user_id: str, data: dict[str, Any], kind: str = "data") -> str:
    sid = secrets.token_hex(8)
    data = {**data, "session_id": sid}
    get_meta().save_session({"id": sid, "user_id": user_id, "kind": kind, "data": data})
    return sid


def save_session(sid: str, user_id: str, data: dict[str, Any], kind: str = "data") -> None:
    get_meta().save_session({"id": sid, "user_id": user_id, "kind": kind, "data": data})


def load_session(sid: str, user_id: str) -> Optional[dict[str, Any]]:
    if not sid or len(sid) > 64 or not sid.replace("-", "").replace("_", "").isalnum():
        return None
    row = get_meta().get_session(sid)
    if not row or row.get("user_id") != user_id:
        return None  # other users' sessions do not exist for the caller
    return row.get("data") or {}


def plan_session_data(inp: ScenarioInput, resp: PlannerResponse) -> dict[str, Any]:
    return {"round": 0, "input": inp.model_dump(), "response": resp.model_dump(mode="json"), "history": []}


# --------------------------------------------------------------------------- scenario planning
async def plan_stage(job: Job) -> None:
    from .planner import plan_scenario

    inp = ScenarioInput.model_validate(job.state["input"])
    job.emit("plan", "STRONG model navrhuje ScenarioSpec" + (" se spustitelným SCM" if inp.engine == "scm" else ""), 0.1)
    if inp.engine == "scm":
        from .causal.planner import plan_story
        resp = await plan_story(inp)
    else:
        resp = await plan_scenario(inp)
    job.result = {"session_id": create_session(job.user_id, plan_session_data(inp, resp))}


async def replan_stage(job: Job) -> None:
    from .planner import replan

    sid = job.state["session_id"]
    session = load_session(sid, job.user_id)
    if session is None:
        raise UserFacingError("Plánovací sezení nenalezeno.")
    if session["round"] >= MAX_REPLAN_ROUNDS:
        raise UserFacingError(f"Dosažen maximální počet {MAX_REPLAN_ROUNDS} kol přeplánování.")
    req = ReplanRequest.model_validate(job.state["request"])
    prev = PlannerResponse.model_validate(session["response"])
    job.emit("plan", "Přeplánování se zpětnou vazbou", 0.1)
    if req.spec.scm is not None or prev.spec.scm is not None:
        resp = await replan_scm(session, req, prev)
    else:
        resp = await replan(req, prev.suggestions, session["round"] + 1)
    session["history"].append({"round": session["round"], "feedback": req.feedback, "answers": req.answers,
                               "accepted": req.accepted_suggestions, "rejected": req.rejected_suggestions})
    session["round"] += 1
    session["response"] = resp.model_dump(mode="json")
    save_session(sid, job.user_id, session)
    job.result = {"session_id": sid}


async def replan_scm(session: dict, req: ReplanRequest, prev: PlannerResponse) -> PlannerResponse:
    """SCM sessions: user edits are kept as-is (validated); free-text feedback triggers a new LLM proposal."""
    from .causal.planner import plan_story
    from .causal.service import scenario_spec
    from .causal.validation import validate_spec as validate_scm

    scm = req.spec.scm or prev.spec.scm
    if req.feedback.strip() or req.answers:
        inp = ScenarioInput.model_validate(session["input"])
        revised = await plan_story(inp, current=scm, feedback=req.feedback, answers=req.answers)
        fields = {"entities", "time", "variables", "events", "ground_truth", "invariants"}
        if scm.model_dump(include=fields) == revised.spec.scm.model_dump(include=fields):
            raise UserFacingError("Přeplánování model nezměnilo. Zkuste konkrétnější úpravu.")
        return revised
    rep = validate_scm(scm)
    return PlannerResponse(spec=scenario_spec(scm, req.spec), questions=[], suggestions=[],
                           notes=f"Uživatelské úpravy SCM ověřeny: {rep.status} "
                                 + "; ".join(c.explanation for c in rep.errors[:5]))


# --------------------------------------------------------------------------- analyses on an existing run
async def detective_stage(job: Job) -> None:
    from .detective import investigate
    from .storage import runs as store

    run_id = job.state["run_id"]
    job.touch(run_id)
    run = store.load_run(run_id) or {}
    if run.get("engine") == "scm":  # blind causal detective on the observed baseline (never the clean world)
        from .causal.service import legacy_detective_view
        job.result = await legacy_detective_view(run_id, job)
    else:
        job.result = await investigate(run_id, job)


async def observe_stage(job: Job) -> None:
    from .causal.contracts import CorruptionConfig, DetectiveConfig, EvaluationConfig
    from .causal.service import manual_mode

    run_id, r = job.state["run_id"], job.state["request"]
    job.touch(run_id)
    job.result = await manual_mode(
        run_id, CorruptionConfig.model_validate(r["corruption"]), DetectiveConfig.model_validate(r["detective"]),
        EvaluationConfig.model_validate(r["evaluation"]), r["repeats"], r["with_baseline"],
        progress=lambda m, f=None: job.emit("difficulty", m, f))


async def difficulty_stage(job: Job) -> None:
    from .causal.contracts import DifficultyConfig
    from .causal.service import automatic_mode

    run_id = job.state["run_id"]
    job.touch(run_id)
    job.result = await automatic_mode(run_id, DifficultyConfig.model_validate(job.state["request"]),
                                      progress=lambda m, f=None: job.emit("difficulty", m, f),
                                      should_stop=lambda: job.cancelled)


async def dashboard_stage(job: Job) -> None:
    from .causal.service import refresh_scm_dashboard

    run_id = job.state["run_id"]
    job.touch(run_id)
    job.result = await refresh_scm_dashboard(run_id, job)


# --------------------------------------------------------------------------- documents
async def doc_plan_stage(job: Job) -> None:
    from .contracts.docs import DocInput
    from .documents.pipeline import plan_docs

    inp = DocInput.model_validate(job.state["input"])
    job.emit("plan", "STRONG model navrhuje specifikaci dokumentů", 0.1)
    resp = await plan_docs(inp)
    sid = create_session(job.user_id, {"kind": "documents", "round": 0, "input": inp.model_dump(),
                                       "response": resp.model_dump(mode="json")}, kind="documents")
    job.result = {"session_id": sid}


async def doc_replan_stage(job: Job) -> None:
    from .contracts.docs import DocReplanRequest
    from .documents.pipeline import replan_docs

    sid = job.state["session_id"]
    session = load_session(sid, job.user_id)
    if session is None:
        raise UserFacingError("Plánovací sezení nenalezeno.")
    if session["round"] >= MAX_REPLAN_ROUNDS:
        raise UserFacingError(f"Dosažen maximální počet {MAX_REPLAN_ROUNDS} kol přeplánování.")
    job.emit("plan", "Přeplánování dokumentů", 0.1)
    resp = await replan_docs(DocReplanRequest.model_validate(job.state["request"]))
    session["round"] += 1
    session["response"] = resp.model_dump(mode="json")
    save_session(sid, job.user_id, session, kind="documents")
    job.result = {"session_id": sid}


async def doc_run_stage(job: Job) -> None:
    from .contracts.docs import DocRunRequest
    from .documents.pipeline import execute_doc_run

    job.result = await execute_doc_run(DocRunRequest.model_validate(job.state["request"]), job)


def _single(fn):
    async def stage(job: Job) -> Optional[str]:
        await fn(job)
        return None
    return stage


register("plan", {"plan": _single(plan_stage)})
register("replan", {"replan": _single(replan_stage)})
register("run", RUN_STAGES, first="plan")
register("whatif", RUN_STAGES, first="plan")
register("detective", {"detective": _single(detective_stage)})
register("observe", {"observe": _single(observe_stage)})
register("difficulty", {"difficulty": _single(difficulty_stage)})
register("dashboard", {"dashboard": _single(dashboard_stage)})
register("doc_plan", {"plan": _single(doc_plan_stage)})
register("doc_replan", {"replan": _single(doc_replan_stage)})
register("doc_run", {"run": _single(doc_run_stage)})

__all__ = ["create_session", "load_session", "save_session", "plan_session_data", "json"]
