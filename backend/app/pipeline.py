"""End-to-end run pipeline: plan → generate → write → checks → evidence → validate → repair → dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any, Optional

import pandas as pd

from .config import get_settings
from .contracts.io import GenerationPlan, RunRequest, ValidationVerdict
from .contracts.spec import ScenarioSpec
from .generation.llm_rows import generate_table
from .generation.scenarios.base import GenContext, ScenarioModule
from .generation.scenarios.registry import get_module
from .jobs import Job
from .llm.client import UsageTracker, usage_scope
from .planner import make_generation_plan
from .reporting.evidence import compact, fallback_dashboard, run_evidence, validate_widgets
from .storage import runs as store
from .validation.checks import run_checks, summarize
from .validation.profile import profile_db
from .validation.validator import deterministic_verdict, validate_with_llm

log = logging.getLogger("pipeline")
MAX_REPAIR_ITERATIONS = 2
VERDICT_RANK = {"pass": 0, "warn": 1, "fail": 2}


class Generator:
    """Holds generated state so the repair loop can regenerate only the problematic parts."""

    def __init__(self, spec: ScenarioSpec, plan: GenerationPlan, ctx: GenContext, module: Optional[ScenarioModule],
                 job: Job):
        self.spec, self.plan, self.ctx, self.module, self.job = spec, plan, ctx, module, job
        self.dims: dict[str, Any] | None = None
        self.tables: dict[str, pd.DataFrame] = {}
        self.manifest: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}

    def _ordered_packages(self):
        done: set[str] = set()
        pkgs = list(self.plan.work_packages)
        out = []
        while pkgs:
            ready = [p for p in pkgs if set(p.depends_on) <= done]
            if not ready:
                raise RuntimeError("plan dependency cycle")
            for p in ready:
                out.append(p)
                done.add(p.id)
                pkgs.remove(p)
        return out

    async def run(self, only: set[str] | None = None, rebuild_dims: bool = False, resimulate: bool = False) -> None:
        """Generate everything (only=None) or selectively regenerate tables in `only`."""
        for pkg in self._ordered_packages():
            if pkg.strategy == "llm_dimension" and pkg.module and self.module:
                if self.dims is None or rebuild_dims:
                    self.job.emit("dimensions", "Generuji master data (CHEAP model)", 0.05)
                    self.dims = await self.module.build_dimensions(self.spec, self.ctx)
                    resimulate = True
            elif pkg.strategy == "simulation" and self.module:
                if only is None or resimulate or self.manifest == {}:
                    self.job.emit("simulation", "Spouštím kauzální simulaci", 0.15)
                    res = await asyncio.to_thread(self.module.simulate, self.spec, self.dims, self.ctx)
                    self.tables.update(res.tables)
                    self.manifest = res.manifest
            else:
                for name in pkg.tables:
                    if only is not None and name not in only and name in self.tables:
                        continue
                    t = self.spec.table(name)
                    self.job.emit("llm_rows", f"Generuji tabulku {name} ({t.strategy}, CHEAP)", 0.8)
                    df, st = await generate_table(self.spec, t, self.tables, batch_size=pkg.batch_size,
                                                  notes=" ".join(filter(None, [pkg.instructions, self.ctx.notes.get(name, "")])),
                                                  progress=lambda m, f: self.job.emit("llm_rows", m))
                    self.tables[name] = df
                    self.stats[name] = st


async def _evaluate(run_id: str, spec: ScenarioSpec, gen: Generator, knobs: dict[str, Any], job: Job,
                    use_llm: bool = True) -> dict[str, Any]:
    db = store.db_path(run_id)
    job.emit("write", "Zapisuji SQLite databázi", 0.82)
    await asyncio.to_thread(store.write_sqlite, db, spec, gen.tables)
    job.emit("checks", "Deterministické kontroly", 0.85)
    checks = await asyncio.to_thread(run_checks, db, spec, knobs)
    summary = summarize(checks)
    job.emit("checks", f"Kontroly: {summary['passed']}/{summary['total']} prošlo, FK integrita {summary['fk_integrity_pct']} %")
    job.emit("evidence", "Spouštím důkazní dotazy", 0.88)
    evidence = await asyncio.to_thread(run_evidence, db, spec, knobs)
    signals = gen.module.story_signals(gen.manifest, knobs) if gen.module else []
    job.emit("validate", "Validace STRONG modelem (kompaktní profil dat)", 0.9)
    profile = await asyncio.to_thread(profile_db, db, spec)
    failed = [c.to_dict() for c in checks if not c.passed]
    verdict: ValidationVerdict
    validator = "strong"
    try:
        if not use_llm:
            raise RuntimeError("skipped")
        verdict = await validate_with_llm(spec, knobs, profile, summary, failed, [compact(e) for e in evidence],
                                          gen.manifest, signals, gen.module.repairable_params if gen.module else {})
    except Exception as e:  # noqa: BLE001
        validator = "deterministic"
        job.emit("validate", f"STRONG validace nedostupná ({str(e)[:120]}), použit deterministický verdikt")
        verdict = deterministic_verdict(summary, signals, knobs)
    # deterministic errors always override an optimistic LLM verdict
    if summary["errors"] and verdict.verdict == "pass":
        verdict = verdict.model_copy(update={"verdict": "warn"})
    job.emit("validate", f"Verdikt: {verdict.verdict} – {verdict.summary[:160]}")
    return {"checks": [c.to_dict() for c in checks], "checks_summary": summary, "evidence": evidence,
            "signals": signals, "verdict": verdict.model_dump(), "validator": validator, "profile": profile}


def _repair_actions(ev: dict[str, Any], spec: ScenarioSpec, module: Optional[ScenarioModule]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    verdict = ValidationVerdict.model_validate(ev["verdict"])
    llm_tables = {t.name for t in spec.tables if t.strategy in ("llm_rows", "llm_dimension")}
    for issue in verdict.issues:
        r = issue.repair
        if issue.severity == "minor":
            continue
        if r.action == "regenerate_table" and r.table in llm_tables:
            actions.append({"action": "regenerate_table", "table": r.table, "instructions": r.instructions or issue.description,
                            "issue": issue.id})
        elif r.action == "adjust_param" and module and r.param in module.repairable_params and r.value is not None:
            lo, hi = module.repairable_params[r.param]
            try:
                v = min(hi, max(lo, float(r.value)))
            except ValueError:
                continue
            actions.append({"action": "adjust_param", "param": r.param, "value": v, "issue": issue.id})
    # deterministic failures in LLM-generated tables -> regenerate them with the error list as notes
    for c in ev["checks"]:
        if not c["passed"] and c["severity"] == "error" and c.get("table") in llm_tables \
                and (not module or c["table"] not in module.dimension_tables()):
            if not any(a.get("table") == c["table"] for a in actions):
                actions.append({"action": "regenerate_table", "table": c["table"],
                                "instructions": f"Fix: {c['description']} ({c['violations']} violations)", "issue": c["id"]})
    return actions


def _score(ev: dict[str, Any]) -> tuple[int, int]:
    return VERDICT_RANK[ev["verdict"]["verdict"]], ev["checks_summary"]["errors"]


# =========================================================================== staged execution
# Each stage is one short, idempotent request (see jobs.py). Between stages the generator state is
# persisted in the run's private directory and the small bookkeeping in ``job.state``.
GEN_STATE = "private/_gen_state.pkl.gz"
GEN_BEST = "private/_gen_best.pkl.gz"
EV_FILE = "private/_ev.json"
EV_BEST = "private/_ev_best.json"
_LIVE: dict[str, tuple[int, "Generator"]] = {}  # run_id -> (attempt marker, generator) in this process


def _req(job: Job) -> RunRequest:
    return RunRequest.model_validate(job.state["request"])


def _run_doc(run_id: str) -> dict[str, Any]:
    return store.load_run(run_id) or {}


def _save_gen(run_id: str, gen: "Generator", ctx: GenContext, name: str = GEN_STATE) -> None:
    import gzip
    import pickle

    p = store.run_path(run_id) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    # Written and read only by the backend: users have no write access to run storage.
    with gzip.open(tmp, "wb", compresslevel=3) as f:
        pickle.dump({"dims": gen.dims, "tables": gen.tables, "manifest": gen.manifest, "stats": gen.stats,
                     "params": ctx.params, "notes": ctx.notes}, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(p)


def _load_gen(job: Job, spec: ScenarioSpec, name: str = GEN_STATE) -> tuple["Generator", GenContext]:
    import gzip
    import pickle

    run_id = job.state["run_id"]
    module = get_module(spec.meta.archetype)
    plan = GenerationPlan.model_validate(job.state["plan"])
    ctx = GenContext(seed=spec.meta.seed, knobs=job.state["knobs"], params=dict(job.state.get("base_params") or {}),
                     progress=lambda m, f: job.emit("", m, f))
    gen = Generator(spec, plan, ctx, module, job)
    live = _LIVE.get(run_id)
    if name == GEN_STATE and live and live[0] == job.state.get("gen_version"):
        src = live[1]
        gen.dims, gen.tables, gen.manifest, gen.stats = src.dims, src.tables, src.manifest, src.stats
        ctx.params, ctx.notes = dict(src.ctx.params), dict(src.ctx.notes)
        return gen, ctx
    p = store.run_path(run_id) / name
    if p.exists():
        with gzip.open(p, "rb") as f:
            d = pickle.load(f)  # noqa: S301 – trusted, backend-written file
        gen.dims, gen.tables, gen.manifest, gen.stats = d["dims"], d["tables"], d["manifest"], d["stats"]
        ctx.params, ctx.notes = d["params"], d["notes"]
    return gen, ctx


def _remember_gen(job: Job, gen: "Generator", ctx: GenContext) -> None:
    job.state["gen_version"] = job.state.get("gen_version", 0) + 1
    _save_gen(job.state["run_id"], gen, ctx)
    _LIVE[job.state["run_id"]] = (job.state["gen_version"], gen)
    while len(_LIVE) > 2:
        _LIVE.pop(next(iter(_LIVE)))


def _stage_spec(job: Job) -> tuple[ScenarioSpec, Optional[ScenarioModule]]:
    spec = ScenarioSpec.model_validate(job.state["spec"])
    return spec, get_module(spec.meta.archetype)


def _write_json(run_id: str, name: str, data: Any) -> None:
    p = store.run_path(run_id) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, default=store._json_default), encoding="utf-8")


def _read_json(run_id: str, name: str) -> Any:
    p = store.run_path(run_id) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _append_calls(job: Job, tracker: UsageTracker) -> None:
    job.state.setdefault("calls", []).extend(tracker.records())
    if tracker.responses:
        with open(store.run_path(job.state["run_id"]) / "llm_calls.jsonl", "a", encoding="utf-8") as f:
            for key, resp in tracker.responses.items():
                f.write(json.dumps({"cache_key": key, "response": resp}, ensure_ascii=False) + "\n")


async def stage_plan(job: Job) -> Optional[str]:
    req = _req(job)
    if req.spec.scm is not None:  # executable structural causal model -> generic causal engine (one stage)
        from .causal.service import execute_causal_run
        job.result = await execute_causal_run(req, job)
        return None
    settings = get_settings()
    spec = req.spec
    module = get_module(spec.meta.archetype)
    warnings: list[str] = []
    if module:
        spec, w = module.normalize_spec(spec)
        warnings += w
    errs = spec.structural_errors()
    if errs:
        raise ValueError("Specifikace není generovatelná: " + "; ".join(errs))
    knobs = spec.knob_values(req.knob_overrides)
    run_id = job.state.get("run_id") or store.new_run_id()
    job.run_id = run_id
    job.touch(run_id)
    run: dict[str, Any] = {
        "run_id": run_id, "label": req.label or ("What-if" if req.parent_run_id else "Běh"),
        "name": spec.meta.name, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "status": "running",
        "parent_run_id": req.parent_run_id, "knob_values": knobs, "knob_overrides": req.knob_overrides,
        "mock": settings.mock, "models": {"strong": settings.strong_model, "cheap": settings.cheap_model},
        "spec": spec.model_dump(mode="json"), "input": req.input.model_dump() if req.input else None,
        "job_id": job.id,
    }
    store.save_json(run_id, "run.json", run)
    (store.run_path(run_id) / "llm_calls.jsonl").write_text("", encoding="utf-8")
    job.emit("plan", f"Běh {run_id}", 0.01, run_id=run_id)
    tracker = UsageTracker()
    with usage_scope(tracker):
        job.emit("plan", "STRONG model sestavuje GenerationPlan", 0.02)
        plan, w = await make_generation_plan(spec)
    warnings += w
    for wmsg in w:
        job.emit("plan", wmsg)
    job.emit("plan", "Plán: " + ", ".join(f"{p.id}[{p.strategy}]" for p in plan.work_packages), 0.04)
    job.state.update(run_id=run_id, spec=spec.model_dump(mode="json"), knobs=knobs, warnings=warnings,
                     plan=plan.model_dump(mode="json"), t0=time.time(), calls=[], history=[], iteration=0)
    _append_calls(job, tracker)
    return "generate"


async def stage_generate(job: Job) -> Optional[str]:
    spec, _ = _stage_spec(job)
    job.touch(job.state["run_id"])
    gen, ctx = _load_gen(job, spec, name="__none__")  # fresh generator (idempotent re-run)
    tracker = UsageTracker()
    with usage_scope(tracker):
        await gen.run()
    _remember_gen(job, gen, ctx)
    _append_calls(job, tracker)
    return "evaluate"


async def stage_evaluate(job: Job) -> Optional[str]:
    spec, _ = _stage_spec(job)
    run_id = job.state["run_id"]
    job.touch(run_id)
    gen, ctx = _load_gen(job, spec)
    tracker = UsageTracker()
    with usage_scope(tracker):
        ev = await _evaluate(run_id, spec, gen, job.state["knobs"], job)
    _write_json(run_id, EV_FILE, ev)
    _write_json(run_id, EV_BEST, ev)
    _save_gen(run_id, gen, ctx, GEN_BEST)
    job.state.update(history=[{"iteration": 0, "verdict": ev["verdict"]["verdict"],
                               "errors": ev["checks_summary"]["errors"], "actions": []}],
                     iteration=0, best_iteration=0, best_score=list(_score(ev)))
    _append_calls(job, tracker)
    return "repair" if _req(job).repair else "finalize"


async def stage_repair(job: Job) -> Optional[str]:
    spec, module = _stage_spec(job)
    run_id = job.state["run_id"]
    job.touch(run_id)
    it = int(job.state.get("iteration", 0)) + 1
    ev = _read_json(run_id, EV_FILE)
    if it > MAX_REPAIR_ITERATIONS or (ev["verdict"]["verdict"] == "pass" and not ev["checks_summary"]["errors"]):
        return "finalize"
    actions = _repair_actions(ev, spec, module)
    if not actions:
        return "finalize"
    job.emit("repair", f"Repair iterace {it}: " + "; ".join(
        f"{a['action']} {a.get('table') or a.get('param')}" for a in actions), 0.9)
    gen, ctx = _load_gen(job, spec)
    only: set[str] = set()
    rebuild_dims = resim = False
    for a in actions:
        if a["action"] == "adjust_param":
            ctx.params[a["param"]] = a["value"]
            resim = True
        else:
            ctx.notes[a["table"]] = a["instructions"]
            if module and a["table"] in module.dimension_tables():
                rebuild_dims = True
            else:
                only.add(a["table"])
                # dependants of a regenerated table must be regenerated too (FK values change)
                only |= {t.name for t in spec.tables
                         if any(c.fk and c.fk.startswith(a["table"] + ".") for c in t.columns)
                         and t.strategy != "simulation"}
    tracker = UsageTracker()
    with usage_scope(tracker):
        await gen.run(only=only, rebuild_dims=rebuild_dims, resimulate=resim)
        ev = await _evaluate(run_id, spec, gen, job.state["knobs"], job)
    job.state["history"].append({"iteration": it, "verdict": ev["verdict"]["verdict"],
                                 "errors": ev["checks_summary"]["errors"], "actions": actions})
    _write_json(run_id, EV_FILE, ev)
    if list(_score(ev)) <= job.state["best_score"]:
        job.state.update(best_iteration=it, best_score=list(_score(ev)))
        _write_json(run_id, EV_BEST, ev)
        _save_gen(run_id, gen, ctx, GEN_BEST)
    job.state["iteration"] = it
    _remember_gen(job, gen, ctx)
    _append_calls(job, tracker)
    return "repair"


async def stage_finalize(job: Job) -> Optional[str]:
    spec, _ = _stage_spec(job)
    run_id = job.state["run_id"]
    job.touch(run_id)
    knobs = job.state["knobs"]
    ev = _read_json(run_id, EV_FILE)
    if job.state.get("best_iteration", 0) != job.state.get("iteration", 0):
        job.emit("repair", "Vracím nejlepší iteraci")
        best_ev = _read_json(run_id, EV_BEST)
        gen, ctx = _load_gen(job, spec, GEN_BEST)
        ev = await _evaluate(run_id, spec, gen, knobs, job, use_llm=False)
        ev["verdict"] = best_ev["verdict"]
    else:
        gen, ctx = _load_gen(job, spec)
    tracker = UsageTracker()
    job.emit("dashboard", "Validuji a vykresluji dashboard", 0.96)
    db = store.db_path(run_id)
    with usage_scope(tracker):
        widgets, werrs = await asyncio.to_thread(validate_widgets, db, spec.dashboard, knobs, spec.meta.model_dump())
        dash_source = "spec"
        if len(widgets) < 2:
            widgets, werrs2 = await asyncio.to_thread(validate_widgets, db, fallback_dashboard(spec), knobs,
                                                       spec.meta.model_dump())
            werrs += werrs2
            dash_source = "fallback"
    _append_calls(job, tracker)
    all_calls = UsageTracker.from_records(job.state.get("calls", []))
    row_counts = {k: int(len(v)) for k, v in gen.tables.items() if not k.startswith("_")}
    run = _run_doc(run_id)
    run.update({
        "status": "done", "plan": job.state["plan"], "warnings": job.state.get("warnings", []),
        "verdict": ev["verdict"], "validator": ev["validator"], "checks_summary": ev["checks_summary"],
        "signals": ev["signals"], "repair_history": job.state.get("history", []), "row_counts": row_counts,
        "params": ctx.params, "generation_stats": gen.stats, "usage": all_calls.summary(),
        "dashboard": {"source": dash_source, "errors": werrs, "title": spec.dashboard.title},
        "duration_s": round(time.time() - float(job.state.get("t0") or time.time()), 1),
        "llm_calls": all_calls.records(),
    })
    store.save_json(run_id, "run.json", run)
    store.save_json(run_id, "checks.json", ev["checks"])
    store.save_json(run_id, "evidence.json", ev["evidence"])
    store.save_json(run_id, "profile.json", ev["profile"])
    store.save_json(run_id, "dashboard.json", widgets)
    store.save_json(run_id, "manifest.json", gen.manifest)
    _dedupe_calls(run_id)
    for name in (GEN_STATE, GEN_BEST, EV_FILE, EV_BEST):
        (store.run_path(run_id) / name).unlink(missing_ok=True)
    _LIVE.pop(run_id, None)
    u = all_calls.summary()
    job.emit("done", f"Hotovo: {sum(row_counts.values()):,} řádků v {len(row_counts)} tabulkách, "
                     f"{u['total_calls']} LLM volání, odhad ${u['total_cost_usd']:.4f}", 1.0, run_id=run_id)
    job.result = {"run_id": run_id}
    return None


def _dedupe_calls(run_id: str) -> None:
    p = store.run_path(run_id) / "llm_calls.jsonl"
    if not p.exists():
        return
    seen: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            seen[json.loads(line)["cache_key"]] = line
    p.write_text("".join(v + "\n" for v in seen.values()), encoding="utf-8")


RUN_STAGES = {"plan": stage_plan, "generate": stage_generate, "evaluate": stage_evaluate, "repair": stage_repair,
              "finalize": stage_finalize}


def run_job_state(req: RunRequest, base_params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"request": req.model_dump(mode="json"), "base_params": base_params or {}}


async def execute_run(req: RunRequest, job: Job, base_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """All stages in-process (scripts, tests). The API runs the same stages as a resumable job."""
    job.state.update(run_job_state(req, base_params))
    stage: Optional[str] = "plan"
    while stage:
        stage = await RUN_STAGES[stage](job)
    return job.result
