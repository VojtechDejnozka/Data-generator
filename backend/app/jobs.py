"""In-process async job manager with progress events (consumed over SSE)."""
from __future__ import annotations

import asyncio
import secrets
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

PHASES_RUN = ["plan", "dimensions", "simulation", "llm_rows", "write", "checks", "evidence", "validate", "repair",
              "dashboard", "done"]


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error
    phase: str = ""
    progress: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    created: float = field(default_factory=time.time)

    def emit(self, phase: str, message: str, progress: float | None = None, **extra: Any) -> None:
        self.phase = phase or self.phase
        if progress is not None:
            self.progress = max(self.progress, min(1.0, progress))
        self.events.append({"i": len(self.events), "t": round(time.time() - self.created, 2), "phase": self.phase,
                            "message": message, "progress": round(self.progress, 3), "status": self.status, **extra})

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status, "phase": self.phase,
                "progress": self.progress, "error": self.error, "result": self.result,
                "events": self.events[-200:]}


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def start(self, kind: str, fn: Callable[[Job], Awaitable[Any]]) -> Job:
        job = Job(id=secrets.token_hex(6), kind=kind)
        self.jobs[job.id] = job

        async def runner() -> None:
            job.status = "running"
            job.emit("start", "Úloha spuštěna", 0.0)
            try:
                job.result = await fn(job)
                job.status = "done"
                job.emit("done", "Hotovo", 1.0)
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                job.emit("error", job.error, None, traceback=traceback.format_exc()[-3000:])

        job.task = asyncio.create_task(runner())  # type: ignore[attr-defined]
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)


jobs = JobManager()
