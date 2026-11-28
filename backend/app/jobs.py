"""Persistent, resumable jobs executed in short stages.

A job is a row in the metadata store (``jobs`` table). Its work is split into named *stages*; each stage is
executed by exactly one request at a time under a lease (``lease_until`` / ``lease_holder``) and must be
idempotent: if the process dies mid-stage the lease expires and the stage runs again from the last persisted
state. This keeps every request well below serverless time limits (Vercel: 300 s on Hobby).

Who executes stages:
* ``POST /api/jobs/{id}/advance`` (the browser drives the job and receives progress as an SSE stream);
* locally (``JOB_AUTODRIVE=1``) a background task drives the job right after creation, ``advance`` then just
  follows its progress.

Stage handlers are registered per job kind with :func:`register` and receive the :class:`Job`. They read
their input from ``job.state`` and return the name of the next stage, or ``None`` when the job is finished
(``job.result`` set). Large intermediate data belongs in the run's working directory, not in ``job.state``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from .config import get_settings

log = logging.getLogger("jobs")

PHASES_RUN = ["plan", "dimensions", "simulation", "llm_rows", "write", "checks", "evidence", "validate", "repair",
              "dashboard", "done"]
MAX_EVENTS = 300
FLUSH_EVERY_S = 1.0
FINAL = ("done", "error")

StageFn = Callable[["Job"], Awaitable[Optional[str]]]


class UserFacingError(Exception):
    """An error whose message is safe and useful to show to the user."""


@dataclass
class JobKind:
    name: str
    first: str
    stages: dict[str, StageFn]
    llm: bool = True


REGISTRY: dict[str, JobKind] = {}


def register(kind: str, stages: dict[str, StageFn], first: Optional[str] = None, llm: bool = True) -> None:
    REGISTRY[kind] = JobKind(kind, first or next(iter(stages)), stages, llm)


@dataclass
class Job:
    id: str
    kind: str
    user_id: str = ""
    run_id: Optional[str] = None
    status: str = "queued"  # queued | running | done | error
    stage: str = ""
    phase: str = ""
    progress: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0
    result: Any = None
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    cancel_requested: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    touched: set[str] = field(default_factory=set)
    _dirty: bool = field(default=False, repr=False)

    def cancel(self) -> None:
        """Cooperative cancellation: long-running work polls ``cancelled``."""
        self.cancel_requested = True

    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def touch(self, run_id: Optional[str]) -> None:
        """Mark a run whose files this job changed (uploaded to storage after the stage)."""
        if run_id:
            self.touched.add(run_id)

    def emit(self, phase: str, message: str, progress: float | None = None, **extra: Any) -> None:
        self.phase = phase or self.phase
        if progress is not None:
            self.progress = max(self.progress, min(1.0, progress))
        extra.pop("traceback", None)
        if extra.get("run_id"):
            self.touch(extra["run_id"])
        self.events.append({"i": self.event_count, "t": round(time.time() - self.created, 2), "phase": self.phase,
                            "message": message, "progress": round(self.progress, 3), "status": self.status, **extra})
        self.event_count += 1
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]
        self._dirty = True

    def events_since(self, since: int) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("i", 0) >= since]

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage, "phase": self.phase,
                "progress": self.progress, "error": self.error, "result": self.result, "run_id": self.run_id,
                "cancel_requested": self.cancel_requested,
                "events": [{k: v for k, v in e.items() if k != "observation"} for e in self.events[-200:]]}

    # -- persistence --------------------------------------------------------------------
    def to_row(self) -> dict[str, Any]:
        return {"status": self.status, "stage": self.stage, "phase": self.phase, "progress": float(self.progress),
                "events": _jsonable(self.events[-MAX_EVENTS:]), "result": _jsonable(self.result),
                "error": self.error, "state": _jsonable({**self.state, "_event_count": self.event_count,
                                                         "_created": self.created}),
                "attempts": self.attempts, "run_id": self.run_id}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Job":
        state = dict(row.get("state") or {})
        events = list(row.get("events") or [])
        return cls(id=row["id"], kind=row["kind"], user_id=row.get("user_id") or "", run_id=row.get("run_id"),
                   status=row.get("status") or "queued", stage=row.get("stage") or "", phase=row.get("phase") or "",
                   progress=float(row.get("progress") or 0.0), events=events,
                   event_count=int(state.pop("_event_count", len(events))), result=row.get("result"),
                   error=row.get("error"), created=float(state.pop("_created", time.time())),
                   cancel_requested=bool(row.get("cancel_requested")), state=state,
                   attempts=int(row.get("attempts") or 0))


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, ensure_ascii=False, default=str))


def _meta():
    from .storage.meta import get_meta
    return get_meta()


def public_error(e: BaseException) -> str:
    """Message for the client. Internal details stay in the server log."""
    from fastapi import HTTPException

    from .llm.client import LLMError

    if isinstance(e, HTTPException):
        d = e.detail
        return d.get("message", str(d)) if isinstance(d, dict) else str(d)
    if isinstance(e, UserFacingError):
        return str(e)
    if isinstance(e, LLMError):
        return "Jazykový model není dostupný (všechny modely selhaly nebo vyčerpaly kvótu). Zkuste to později."
    if isinstance(e, (ValueError, KeyError)) or type(e).__name__ in ("CausalError", "GraphError", "ValidationError"):
        return f"{type(e).__name__}: {str(e)[:500]}"
    return "Interní chyba při zpracování úlohy."


class JobManager:
    def __init__(self) -> None:
        self.live: dict[str, Job] = {}  # jobs executing (or recently executed) in this process

    @property
    def jobs(self) -> dict[str, Job]:  # backwards compatibility
        return self.live

    # -- lifecycle ----------------------------------------------------------------------
    def start(self, kind: str, user_id: str, state: Optional[dict[str, Any]] = None,
              run_id: Optional[str] = None) -> Job:
        if kind not in REGISTRY:
            raise KeyError(f"unknown job kind {kind}")
        job = Job(id=secrets.token_hex(8), kind=kind, user_id=user_id, run_id=run_id, state=dict(state or {}),
                  stage=REGISTRY[kind].first)
        job.emit("start", "Úloha zařazena", 0.0)
        _meta().create_job({"id": job.id, "user_id": user_id, "kind": kind, **job.to_row()})
        self.live[job.id] = job
        self._gc()
        if get_settings().job_autodrive:
            job_task = asyncio.get_running_loop().create_task(self.drive(job.id))
            job.state.setdefault("_driver", "autodrive")
            setattr(job, "task", job_task)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        """Current view of a job (live object if this process runs it, else the stored row)."""
        if job_id in self.live:
            return self.live[job_id]
        row = _meta().get_job(job_id) if _valid_id(job_id) else None
        return Job.from_row(row) if row else None

    def get_for(self, job_id: str, user_id: str) -> Optional[Job]:
        job = self.get(job_id)
        if job is None or job.user_id != user_id:
            return None  # other users' jobs do not exist for the caller
        return job

    def request_cancel(self, job: Job) -> None:
        job.cancel()
        if job.id in self.live:
            self.live[job.id].cancel()
        _meta().update_job(job.id, {"cancel_requested": True})

    def _gc(self) -> None:
        cutoff = time.time() - 3600
        for jid in [j for j, job in self.live.items() if job.status in FINAL and job.created < cutoff]:
            self.live.pop(jid, None)

    # -- execution ----------------------------------------------------------------------
    async def drive(self, job_id: str) -> None:
        """Run stages until the job finishes (local autodrive)."""
        await asyncio.sleep(0)
        holder = "auto-" + secrets.token_hex(4)
        while True:
            job = self.get(job_id)
            if job is None or job.status in FINAL:
                return
            if not _meta().try_lease(job_id, holder, get_settings().lease_seconds):
                return  # someone else is executing it
            await self.run_stage(job, holder)

    async def run_stage(self, job: Job, holder: str) -> None:
        """Execute the job's current stage. The caller must hold the lease."""
        from . import quota
        from .auth.deps import Principal, set_principal, _principal
        from .llm.client import UsageTracker, usage_scope
        from .storage import runs as store
        from .storage.meta import get_meta
        from .storage.workspace import pinned, remote_enabled, sync_up

        kind = REGISTRY[job.kind]
        self.live[job.id] = job
        fresh = get_meta().get_job(job.id) or {}
        if fresh.get("cancel_requested"):
            job.cancel()
        settings = get_settings()
        owner_tok = store.set_owner(job.user_id)
        profile = get_meta().get_profile(job.user_id) or {}
        ptok = set_principal(Principal(id=job.user_id, email=profile.get("email", ""),
                                       role=profile.get("role") or "user"))
        is_admin = profile.get("role") == "admin"
        tracker = UsageTracker()
        flusher = asyncio.get_running_loop().create_task(self._flush_loop(job, holder))
        stage = job.stage or kind.first
        try:
            if job.cancelled:
                raise UserFacingError("Úloha byla zrušena.")
            await self._prepare_runs(job)
            if kind.llm and stage != kind.first:
                quota.check_tokens(job.user_id, is_admin)
            job.status = "running"
            job.stage = stage
            job.attempts += 1
            with pinned(job.run_id or job.id), usage_scope(tracker):
                nxt = await kind.stages[stage](job)
            if job.cancelled and nxt:
                raise UserFacingError("Úloha byla zrušena.")
            if nxt:
                job.stage = nxt
            else:
                job.status = "done"
                job.stage = ""
                job.emit("done", "Hotovo", 1.0)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = public_error(e)
            ref = secrets.token_hex(3)
            log.error("job %s (%s) stage %s failed [ref %s]: %s\n%s", job.id, job.kind, stage, ref, e,
                      traceback.format_exc())
            if job.error == "Interní chyba při zpracování úlohy.":
                job.error += f" (ref {ref})"
            job.emit("error", job.error)
        finally:
            flusher.cancel()
            store.reset_owner(owner_tok)
            _principal.reset(ptok)
            if tracker.calls:
                quota.record(job.user_id, kind=job.kind, summary=tracker.summary(), job_id=job.id,
                             run_id=(job.result or {}).get("run_id", "") if isinstance(job.result, dict) else "",
                             mock=settings.mock)
            if isinstance(job.result, dict) and job.result.get("run_id"):
                job.touch(job.result["run_id"])
            if remote_enabled():
                for rid in sorted(job.touched):
                    try:
                        await asyncio.to_thread(sync_up, rid, job.user_id)
                    except Exception as e:  # noqa: BLE001
                        log.exception("sync of run %s failed", rid)
                        if job.status != "error":
                            job.status, job.error = "error", "Uložení výsledků se nezdařilo. Zkuste krok zopakovat."
                            job.emit("error", job.error)
                        break
            job.touched.clear()
            get_meta().update_job(job.id, {**job.to_row(), "lease_until": None, "lease_holder": None})

    async def _prepare_runs(self, job: Job) -> None:
        """Re-authorise and download the runs this stage works on (any instance may execute any stage)."""
        from .storage.meta import get_meta
        from .storage.workspace import ensure_local, remote_enabled

        for rid in sorted({job.run_id, *job.state.get("read_runs", [])} - {None, ""}):
            meta = get_meta().get_run(rid, None)
            if meta is None:
                continue  # created by this job's first stage
            if meta.get("user_id") != job.user_id and not meta.get("is_preset"):
                raise UserFacingError("Běh nenalezen.")
            if remote_enabled():
                await asyncio.to_thread(ensure_local, meta)

    async def _flush_loop(self, job: Job, holder: str) -> None:
        settings = get_settings()
        last_renew = time.time()
        try:
            while True:
                await asyncio.sleep(FLUSH_EVERY_S)
                meta = _meta()
                if job._dirty:
                    job._dirty = False
                    fields = job.to_row()
                    fields.pop("state", None)  # state is persisted only at stage boundaries
                    await asyncio.to_thread(meta.update_job, job.id, fields)
                if time.time() - last_renew > settings.lease_seconds / 3:
                    last_renew = time.time()
                    ok = await asyncio.to_thread(meta.renew_lease, job.id, holder, settings.lease_seconds)
                    if not ok:
                        log.warning("lost lease on job %s", job.id)
                    row = await asyncio.to_thread(meta.get_job, job.id)
                    if row and row.get("cancel_requested"):
                        job.cancel()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("job flush loop failed for %s", job.id)

    # -- streaming advance ----------------------------------------------------------------
    async def advance(self, job: Job, since: int = 0, follow_s: float = 25.0) -> AsyncIterator[str]:
        """SSE frames: progress events, then a final frame ``{"final": bool, ...}``.

        Executes the next stage when the lease is free; otherwise follows whoever runs it for up to
        ``follow_s`` seconds. A non-final last frame (``"continue": true``) tells the client to call again."""
        def frame(obj: dict[str, Any]) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"

        def tail(j: Job) -> dict[str, Any]:
            final = j.status in FINAL
            return {"final": final, "continue": not final, "status": j.status, "stage": j.stage, "result": j.result,
                    "error": j.error, "progress": j.progress}

        def clean(e: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in e.items() if k not in ("traceback", "observation")}

        if job.status in FINAL:
            for e in job.events_since(since):
                yield frame(clean(e))
            yield frame(tail(job))
            return
        holder = "adv-" + secrets.token_hex(4)
        cursor = since
        if get_settings().job_autodrive:
            # local: a background driver owns execution (and survives the browser going away); just follow
            if job.id not in self.live or getattr(self.live[job.id], "task", None) is None \
                    or self.live[job.id].task.done():  # type: ignore[attr-defined]
                live = self.live.get(job.id) or job
                self.live[job.id] = live
                setattr(live, "task", asyncio.get_running_loop().create_task(self.drive(job.id)))
                job = live
        elif _meta().try_lease(job.id, holder, get_settings().lease_seconds):
            fresh = self.get(job.id) if job.id not in self.live else job
            job = fresh or job
            task = asyncio.get_running_loop().create_task(self.run_stage(job, holder))
            last_beat = time.time()
            while not task.done():
                await asyncio.sleep(0.25)
                for e in job.events_since(cursor):
                    yield frame(clean(e))
                    cursor = e["i"] + 1
                if time.time() - last_beat > 15:
                    last_beat = time.time()
                    yield ": ping\n\n"
            await task
            for e in job.events_since(cursor):
                yield frame(clean(e))
            yield frame(tail(job))
            return
        # follow mode
        deadline = time.time() + follow_s
        last_beat = time.time()
        current = job
        while time.time() < deadline:
            current = self.get(job.id) or current
            for e in current.events_since(cursor):
                yield frame(clean(e))
                cursor = e["i"] + 1
            if current.status in FINAL:
                break
            row = _meta().get_job(job.id) if job.id not in self.live else None
            if row is not None and not row.get("lease_holder"):
                break  # stage finished elsewhere; client calls again to run the next one
            if time.time() - last_beat > 15:
                last_beat = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(0.5 if job.id in self.live else 1.0)
        yield frame(tail(current))


def _valid_id(job_id: str) -> bool:
    return bool(job_id) and len(job_id) <= 64 and all(c.isalnum() or c in "-_" for c in job_id)


jobs = JobManager()
