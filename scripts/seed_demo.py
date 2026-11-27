"""Pre-generate demo runs for a fast live demo: story run + baseline (story_intensity=0) + stronger delay.

Usage (from backend/):  .venv/bin/python ../scripts/seed_demo.py [--mock]
"""
import asyncio
import os
import sys
from pathlib import Path

if "--mock" in sys.argv:
    os.environ["LLM_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

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


async def main():
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
    print(f"\nDone.\n  story    {a}\n  baseline {b}\n  severe   {c}\n"
          f"Compare: http://127.0.0.1:5173/compare?a={a}&b={b}")


asyncio.run(main())
