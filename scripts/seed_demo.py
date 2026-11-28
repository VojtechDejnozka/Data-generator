"""Pre-generate demo runs for a fast live demo: story run + baseline (story_intensity=0) + stronger delay.

Usage (from backend/):  .venv/bin/python ../scripts/seed_demo.py [--mock] [--owner-email E] [--preset]

Local mode: runs belong to the local dev user. With Supabase configured, --owner-email is required (the account
must exist, e.g. admin@story2data.local from supabase/seed.sql); --preset marks the runs as shared, read-only
demo presets visible to every signed-in user.
"""
import asyncio
import os
import sys
from pathlib import Path

if "--mock" in sys.argv:
    os.environ["LLM_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.contracts.io import RunRequest  # noqa: E402
from app.jobs import Job  # noqa: E402
from app.pipeline import execute_run  # noqa: E402
from app.planner import plan_scenario  # noqa: E402
from app.presets.retail_demo import RETAIL_DEMO  # noqa: E402
from app.storage import runs as store  # noqa: E402


def printer(job: Job):
    seen = 0

    def flush():
        nonlocal seen
        for e in job.events[seen:]:
            print(f"  [{e['phase']}] {e['message']}")
        seen = len(job.events)
    return flush


async def one(req: RunRequest, params=None) -> str:
    job = Job(id="seed", kind="run")
    task = asyncio.create_task(execute_run(req, job, base_params=params))
    flush = printer(job)
    while not task.done():
        await asyncio.sleep(0.5)
        flush()
    flush()
    return task.result()["run_id"]


def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


async def main():
    owner = None
    if get_settings().use_supabase:
        from app.storage.meta import get_meta
        email = _arg("--owner-email")
        if not email:
            sys.exit("--owner-email is required when SUPABASE_URL is set")
        rows = get_meta()._req("GET", "/profiles", params={"email": f"eq.{email.lower()}", "select": "id"})
        if not rows:
            sys.exit(f"no profile for {email}")
        owner = rows[0]["id"]
        store.set_owner(owner)
    print("Planning demo scenario…")
    pr = await plan_scenario(RETAIL_DEMO)
    print("Story run…")
    a = await one(RunRequest(spec=pr.spec, label="Demo: příběh (intenzita 1)", input=RETAIL_DEMO))
    params = store.load_run(a)["params"]
    print("Baseline run…")
    b = await one(RunRequest(spec=pr.spec, knob_overrides={"story_intensity": "0"}, parent_run_id=a,
                             label="Demo: baseline bez příběhu", repair=False), params)
    print("Stronger delay run…")
    c = await one(RunRequest(spec=pr.spec, knob_overrides={"delay_severity": "10"}, parent_run_id=a,
                             label="Demo: zpoždění 10 dní", repair=False), params)
    from app.storage.meta import get_meta
    from app.storage.workspace import sync_up
    for rid in (a, b, c):
        meta = get_meta().get_run(rid, None)
        if owner:
            sync_up(rid, owner)
        if "--preset" in sys.argv:
            get_meta().upsert_run({"id": rid, "user_id": meta["user_id"], "is_preset": True})
    print(f"\nDone.\n  story    {a}\n  baseline {b}\n  severe   {c}\n"
          f"Compare: http://127.0.0.1:5173/compare?a={a}&b={b}")


asyncio.run(main())
