"""Integration of the causal core with the existing run storage and job manager.

Run directory (``runs/<run_id>/``) – existing files stay meaningful for the current UI:

* ``data.sqlite``                    clean world tables (author view; the existing data browser shows it)
* ``run.json``                       run metadata incl. ``engine = "scm"`` and the ``causal`` section
* ``manifest.json``                  ground-truth manifest (existing "Ground truth" endpoint)
* ``checks.json`` / ``evidence.json`` / ``dashboard.json``  existing report artefacts
* ``world/world.json``, ``world/internal.sqlite``, ``world/validation.json``  immutable clean world
* ``worlds/<world_hash>/``           audited variants of reality (structural-noise optimisation only)
* ``observations/<obs_id>/observed.sqlite`` + ``schema.json`` + ``info.json``   what the detective may see
* ``private/corruption/<obs_id>.json`` corruption manifest – never given to the detective
* ``evaluations/<eval_id>.json``     detective result + ground-truth evaluation
* ``difficulty/<id>.json``           manual / automatic difficulty results

Phase reuse: a change of corruption parameters reuses the clean world; a change of detective
configuration reuses the observation; a change of the SCM / structural noise creates a new world.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import secrets
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from ..contracts.io import Issue, ObservationSettings, RunRequest, ValidationVerdict
from ..llm.client import LLMError, UsageTracker, usage_scope
from ..contracts.spec import Dashboard, ScenarioSpec, Widget
from ..storage import runs as store
from ..storage.text_files import read_saved_text
from .contracts import CorruptionConfig, CorruptionOp, DetectiveConfig, DifficultyConfig, EvaluationConfig, SCMSpec
from .corruption import observe, table_infos
from .detective import analyze
from .difficulty import CandidateEval, expand_op, optimize, sum_usage
from .engine import CleanWorld, generate_world
from .evaluation import aggregate, evaluate
from .ground_truth import build_manifest
from .mapping import author_evidence, public_schema, scenario_spec_from_scm
from .results import (DetectiveResult, DifficultyEstimate, ManualEvaluationResult, ObservedDatasetInfo,
                      OptimizationResult, PublicSchema, ValidationReport)
from .sqlsafe import write_observed_db
from .validation import validate_spec, validate_world

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
_WORLDS: "OrderedDict[str, tuple[CleanWorld, dict[str, Any]]]" = OrderedDict()
_MAX_CACHED = 6


class CausalError(ValueError):
    pass


# =========================================================================== reference scenarios
def list_reference_scenarios() -> list[dict[str, Any]]:
    out = []
    for p in sorted(SCENARIO_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append({"id": p.stem, "name": d["meta"]["name"], "domain": d["meta"].get("domain"),
                    "description": d["meta"].get("description")})
    return out


def load_reference_scenario(sid: str) -> SCMSpec:
    p = SCENARIO_DIR / f"{sid}.json"
    if not sid.replace("_", "").isalnum() or not p.exists():
        raise CausalError(f"unknown reference scenario {sid}")
    return SCMSpec.model_validate_json(p.read_text(encoding="utf-8"))


def scenario_spec(scm: SCMSpec, base: Optional[ScenarioSpec] = None) -> ScenarioSpec:
    spec = scenario_spec_from_scm(scm, base)
    ev = author_evidence(scm)
    return spec.model_copy(update={"evidence": ev, "dashboard": Dashboard(title=scm.meta.name, widgets=[
        Widget(id=f"w_{e.id}"[:60], title=e.title, sql=e.sql, chart=e.chart, x=e.x, y=e.y, series=e.series,
               description=e.expected, width=2) for e in ev])})


# =========================================================================== worlds
@dataclass
class GeneratedWorld:
    world: CleanWorld
    spec_report: ValidationReport
    world_report: ValidationReport
    manifest: dict[str, Any]


def generate_validated(scm: SCMSpec, seed: Optional[int] = None, noise_scale: Optional[dict[str, float]] = None,
                       with_effects: bool = True) -> GeneratedWorld:
    rep = validate_spec(scm)
    if rep.status == "FAIL":
        raise CausalError("ScenarioSpec/SCM není spustitelný: " + "; ".join(c.explanation for c in rep.errors[:8]))
    world = generate_world(scm, seed=seed, noise_scale=noise_scale)
    wrep = validate_world(world)
    manifest = build_manifest(world, validation={"spec": rep.summary, "world": wrep.summary}, with_effects=with_effects)
    return GeneratedWorld(world, rep, wrep, manifest)


def _write_internal(path: Path, world: CleanWorld) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        for eid, df in world.internals.items():
            out = df.copy()
            for c in out.columns:
                if out[c].dtype == object:
                    out[c] = out[c].astype(str).where(out[c].notna(), None)
            out.to_sql(eid, conn, index=False)
        conn.commit()
    finally:
        conn.close()


def persist_world(run_id: str, gw: GeneratedWorld, spec: ScenarioSpec, subdir: str = "world") -> dict[str, Any]:
    w = gw.world
    base = store.run_path(run_id)
    if subdir == "world":
        store.write_sqlite(store.db_path(run_id), spec, w.tables)
        store.save_json(run_id, "manifest.json", gw.manifest)
    else:
        (base / subdir).mkdir(parents=True, exist_ok=True)
        (base / subdir / "manifest.json").write_text(json.dumps(gw.manifest, ensure_ascii=False, default=str), encoding="utf-8")
    _write_internal(base / subdir / "internal.sqlite", w)
    meta = {"world_hash": w.world_hash, "spec_hash": w.spec_hash, "seed": w.seed, "noise_scale": w.noise_scale,
            "timings": w.timings, "row_counts": {k: int(len(v)) for k, v in w.tables.items()},
            "status": gw.world_report.status, "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    (base / subdir).mkdir(parents=True, exist_ok=True)
    (base / subdir / "world.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (base / subdir / "validation.json").write_text(json.dumps(
        {"spec": gw.spec_report.model_dump(), "world": gw.world_report.model_dump()}, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def _remember(key: str, world: CleanWorld, manifest: dict[str, Any]) -> None:
    _WORLDS[key] = (world, manifest)
    _WORLDS.move_to_end(key)
    while len(_WORLDS) > _MAX_CACHED:
        _WORLDS.popitem(last=False)


def run_scm(run_id: str) -> tuple[SCMSpec, dict[str, Any]]:
    run = store.load_run(run_id)
    if not run or run.get("engine") != "scm" or not (run.get("spec") or {}).get("scm"):
        raise CausalError("run is not an SCM run")
    return SCMSpec.model_validate(run["spec"]["scm"]), run


def load_world(run_id: str) -> tuple[CleanWorld, dict[str, Any]]:
    """Re-create the immutable clean world from the approved spec + seed and verify its hash."""
    scm, run = run_scm(run_id)
    meta = json.loads(read_saved_text(store.run_path(run_id) / "world" / "world.json"))
    key = f"{run_id}:{meta['world_hash']}"
    if key in _WORLDS:
        _WORLDS.move_to_end(key)
        return _WORLDS[key]
    world = generate_world(scm, seed=meta["seed"], noise_scale=meta.get("noise_scale") or None)
    if world.world_hash != meta["world_hash"]:
        raise CausalError(f"clean world of run {run_id} could not be reproduced (hash {world.world_hash} != "
                          f"{meta['world_hash']}) – library versions changed?")
    manifest = store.load_json(run_id, "manifest.json", {})
    _remember(key, world, manifest)
    return world, manifest


def world_variant(run_id: str, scale: float) -> tuple[CleanWorld, dict[str, Any], str]:
    """Reality with scaled structural noise (explicitly allowed only): new, validated, audited world."""
    scm, run = run_scm(run_id)
    vars_ = (run.get("difficulty_noise_variables") or
             [v.variable_id for v in scm.variables if v.mechanism.params.get("noise") or v.mechanism.type == "group_effect"])
    noise = {v: float(scale) for v in vars_}
    key = f"{run_id}:noise:{scale:.4f}"
    if key in _WORLDS:
        w, m = _WORLDS[key]
        return w, m, w.world_hash
    gw = generate_validated(scm, seed=scm.meta.seed, noise_scale=noise)
    if gw.world_report.status == "FAIL":
        raise CausalError(f"world variant with noise scale {scale} failed validation")
    persist_world(run_id, gw, scenario_spec(scm), subdir=f"worlds/{gw.world.world_hash}")
    _remember(key, gw.world, gw.manifest)
    return gw.world, gw.manifest, gw.world.world_hash


# =========================================================================== observations
def _obs_dir(run_id: str, obs_id: str) -> Path:
    if not obs_id.replace("-", "").isalnum():
        raise CausalError("invalid observation id")
    return store.run_path(run_id) / "observations" / obs_id


def observe_and_store(run_id: str, world: CleanWorld, config: CorruptionConfig, minimum_observability: float = 0.0,
                      allowed: Optional[list[str]] = None) -> ObservedDatasetInfo:
    fp = hashlib.sha256((world.world_hash + config.model_dump_json() + str(minimum_observability)
                         + json.dumps(allowed)).encode()).hexdigest()[:12]
    obs_id = f"obs-{fp}"
    d = _obs_dir(run_id, obs_id)
    if (d / "info.json").exists():
        return ObservedDatasetInfo.model_validate_json(read_saved_text(d / "info.json"))
    o = observe(world, config, allowed_types=allowed, minimum_observability=minimum_observability)
    infos = table_infos(world.spec)
    write_observed_db(d / "observed.sqlite", o.tables, {k: v.types for k, v in infos.items()},
                      {k: v.pk for k, v in infos.items()}, enforce=config.enforce_constraints)
    schema = public_schema(world.spec, o.tables)
    (d / "schema.json").write_text(schema.model_dump_json(indent=1), encoding="utf-8")
    priv = store.run_path(run_id) / "private" / "corruption"
    priv.mkdir(parents=True, exist_ok=True)
    (priv / f"{obs_id}.json").write_text(json.dumps(o.manifest, ensure_ascii=False, default=str), encoding="utf-8")
    info = ObservedDatasetInfo(observation_id=obs_id, run_id=run_id, world_hash=world.world_hash,
                               observed_hash=o.observed_hash, corruption_fingerprint=config.fingerprint(),
                               config=config, baseline=not config.operations, realized=o.realized, checks=o.checks,
                               analyzable=o.analyzable, created_at=dt.datetime.now().isoformat(timespec="seconds"))
    (d / "info.json").write_text(info.model_dump_json(indent=1), encoding="utf-8")
    return info


def observation_for_detective(run_id: str, obs_id: str) -> tuple[Path, PublicSchema]:
    """The ONLY two things the detective receives: the observed DB path and the neutral schema."""
    d = _obs_dir(run_id, obs_id)
    return d / "observed.sqlite", PublicSchema.model_validate_json(read_saved_text(d / "schema.json"))


def usage_of(tracker: UsageTracker) -> dict[str, Any]:
    u = tracker.summary()
    return {"calls": u["total_calls"], "tokens": u["total_tokens"], "cost_usd": u["total_cost_usd"],
            "by_model": u["by_model"]}


async def detective_on_tracked(run_id: str, obs_id: str, cfg: DetectiveConfig,
                               progress: Optional[Callable[[str, float], None]] = None
                               ) -> tuple[DetectiveResult, UsageTracker]:
    """Run the detective inside its own usage scope: every LLM call (tokens, cost, model, raw response)
    is recorded for accounting and replay."""
    db, schema = observation_for_detective(run_id, obs_id)
    tracker = UsageTracker()
    with usage_scope(tracker):
        if cfg.kind == "statistical":
            from .detective import analyze_sync
            res = await asyncio.to_thread(analyze_sync, db, schema, cfg)
        else:
            res = await analyze(db, schema, cfg, progress)
    res.agent["usage"] = usage_of(tracker)
    return res, tracker


async def detective_on(run_id: str, obs_id: str, cfg: DetectiveConfig,
                       progress: Optional[Callable[[str, float], None]] = None) -> DetectiveResult:
    return (await detective_on_tracked(run_id, obs_id, cfg, progress))[0]


def _eval_dir(run_id: str) -> Path:
    d = store.run_path(run_id) / "evaluations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_evaluation(run_id: str, ev, result: DetectiveResult, tracker: Optional[UsageTracker] = None,
                     detective_cfg: Optional[DetectiveConfig] = None, world: Optional[CleanWorld] = None) -> None:
    """``evaluations/<id>.json`` (evaluation + detective result + usage + replay info) and
    ``evaluations/<id>.llm_calls.jsonl`` (one line per LLM call, in order, with the raw response)."""
    d = _eval_dir(run_id)
    det = result.model_dump(mode="json")
    for item in det.get("statistical_results", []):
        s = json.dumps(item.get("result"), ensure_ascii=False, default=str)
        if len(s) > 20000:
            item["result"] = {"truncated": True, "preview": s[:2000]}
    calls = []
    if tracker is not None:
        for i, rec in enumerate(tracker.calls):
            calls.append({"i": i, "role": rec.role, "task": rec.task, "model": rec.model, "ok": rec.ok,
                          "cached": rec.cached, "prompt_tokens": rec.prompt_tokens, "output_tokens": rec.output_tokens,
                          "thoughts_tokens": rec.thoughts_tokens, "cost_usd": rec.cost_usd, "latency_s": rec.latency_s,
                          "attempts": rec.attempts, "error": rec.error, "cache_key": rec.cache_key,
                          "response": tracker.responses.get(rec.cache_key) if rec.ok else None})
        with open(d / f"{ev.evaluation_id}.llm_calls.jsonl", "w", encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    payload = {"evaluation": ev.model_dump(mode="json"), "detective": det,
               "usage": usage_of(tracker) if tracker is not None else {},
               "replay": {"detective_config": detective_cfg.model_dump() if detective_cfg else None,
                          "observation_id": ev.observation_id,
                          "world_hash": world.world_hash if world else None,
                          "world_seed": world.seed if world else None,
                          "noise_scale": world.noise_scale if world else None,
                          "llm_calls": len(calls)}}
    (d / f"{ev.evaluation_id}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


async def evaluate_configuration(run_id: str, world: CleanWorld, manifest: dict[str, Any], corruption: CorruptionConfig,
                                 detective_cfg: DetectiveConfig, eval_cfg: EvaluationConfig, repeats: int = 1,
                                 seed_variants: bool = False, minimum_observability: float = 0.0,
                                 allowed: Optional[list[str]] = None,
                                 progress: Optional[Callable[[str, Optional[float]], None]] = None) -> CandidateEval:
    evals, first, usages = [], None, []
    for i in range(repeats):
        cfg_i = corruption
        if seed_variants and i > 0:
            cfg_i = corruption.model_copy(update={"seed": (corruption.seed or world.seed) + 1000 * i})
        info = observe_and_store(run_id, world, cfg_i, minimum_observability, allowed)
        first = first or info
        dcfg = detective_cfg.model_copy(update={"seed": detective_cfg.seed + i})
        res, tracker = await detective_on_tracked(run_id, info.observation_id, dcfg)
        ev = evaluate(world.spec, manifest, res, eval_cfg, observation_id=info.observation_id)
        store_evaluation(run_id, ev, res, tracker, dcfg, world)
        evals.append(ev)
        usages.append(usage_of(tracker))
        if progress:
            progress(f"  evaluace {i + 1}/{repeats}: obtížnost {ev.difficulty}", None)
    est = aggregate(evals, agent={**(evals[0].detective if evals else {}), "config": detective_cfg.model_dump()})
    est.usage = sum_usage(usages)
    r = first.realized if first else {}
    return CandidateEval(estimate=est, realized_share=float(r.get("realized_share", 0.0)),
                         observability=float(r.get("story_observability", 1.0)), analyzable=bool(first and first.analyzable),
                         checks=list(first.checks) if first else [], observation_id=first.observation_id if first else "",
                         world_hash=world.world_hash, realized=r, n_evaluations=len(evals), usage=est.usage)


def _ensure_valid(run_id: str) -> None:
    run = store.load_run(run_id) or {}
    if (run.get("causal") or {}).get("status") == "FAIL":
        raise CausalError("clean world failed validation – it cannot be used as reality for observation")


def _save_difficulty(run_id: str, kind: str, payload: dict[str, Any]) -> str:
    did = f"{kind}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    d = store.run_path(run_id) / "difficulty"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{did}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    return did


async def manual_mode(run_id: str, corruption: CorruptionConfig, detective_cfg: DetectiveConfig,
                      eval_cfg: EvaluationConfig, repeats: int = 1, with_baseline: bool = True,
                      progress: Optional[Callable[[str, Optional[float]], None]] = None) -> dict[str, Any]:
    _ensure_valid(run_id)
    world, manifest = load_world(run_id)
    say = progress or (lambda m, f: None)
    baseline = None
    if with_baseline:
        say("Baseline (čistá data, pouze pravidla pozorovatelnosti)", 0.1)
        b = await evaluate_configuration(run_id, world, manifest, CorruptionConfig(seed=corruption.seed), detective_cfg,
                                         eval_cfg, repeats, progress=say)
        baseline = b.estimate
    say("Pozorovaný dataset s poškozením", 0.5)
    ev = await evaluate_configuration(run_id, world, manifest, corruption, detective_cfg, eval_cfg, repeats,
                                      progress=say)
    delta = None
    if baseline and baseline.mean_difficulty is not None and ev.estimate.mean_difficulty is not None:
        delta = round(ev.estimate.mean_difficulty - baseline.mean_difficulty, 3)
    res = ManualEvaluationResult(run_id=run_id, observation_id=ev.observation_id, corruption=corruption,
                                 realized_corruption=ev.realized, baseline=baseline, observed=ev.estimate,
                                 difficulty_delta=delta, detective_config=detective_cfg,
                                 usage=sum_usage([baseline.usage if baseline else {}, ev.estimate.usage]))
    out = res.model_dump(mode="json")
    out["checks"] = [c.model_dump() for c in ev.checks]
    out["difficulty_id"] = _save_difficulty(run_id, "manual", out)
    return out


def make_evaluator(run_id: str, dcfg: DifficultyConfig, progress=None):
    world, manifest = load_world(run_id)

    async def evaluator(config: CorruptionConfig, scale: float, repeats: int, phase: str, variant: int) -> CandidateEval:
        w, m = world, manifest
        if abs(scale - 1.0) > 1e-9:
            w, m, _ = await asyncio.to_thread(world_variant, run_id, scale)
        det = dcfg.final_detective if (phase == "final" and dcfg.final_detective) else dcfg.screening_detective
        return await evaluate_configuration(run_id, w, m, config, det, dcfg.evaluation, repeats,
                                            seed_variants=phase == "final", minimum_observability=dcfg.minimum_observability,
                                            allowed=list(dcfg.allowed_corruption_types) if phase != "baseline" else None,
                                            progress=progress)

    return evaluator


async def automatic_mode(run_id: str, dcfg: DifficultyConfig, progress=None,
                         should_stop: Optional[Callable[[], bool]] = None, evaluator=None) -> dict[str, Any]:
    _ensure_valid(run_id)
    scm, run = run_scm(run_id)
    if dcfg.structural_noise_variables:
        run["difficulty_noise_variables"] = dcfg.structural_noise_variables
        store.save_json(run_id, "run.json", run)
    evaluator = evaluator or make_evaluator(run_id, dcfg, progress)
    res: OptimizationResult = await optimize(scm, dcfg, evaluator, run_id=run_id, progress=progress,
                                             should_stop=should_stop)
    out = res.model_dump(mode="json")
    out["difficulty_id"] = _save_difficulty(run_id, "auto", out)
    return out


# =========================================================================== run integration
async def _prepare_observation(run_id: str, world: CleanWorld, cfg: ObservationSettings, job) -> dict[str, Any]:
    if cfg.mode == "manual":
        operations = []
        if cfg.missing_percent:
            operations.extend(expand_op(world.spec, CorruptionOp(type="missing_values_mcar", target_table="*",
                                                                 probability=cfg.missing_percent / 100), set()))
        if cfg.noise_percent:
            operations.extend(expand_op(world.spec, CorruptionOp(type="gaussian_measurement_noise", target_table="*",
                                                                 params={"intensity": cfg.noise_percent / 100,
                                                                         "scale_from_clean": True}), set()))
        job.emit("difficulty", "Aplikuji pozorovací chyby", 0.85)
        info = await asyncio.to_thread(observe_and_store, run_id, world, CorruptionConfig(operations=operations))
        return {"mode": "manual", "status": "applied", "observation_id": info.observation_id}

    job.emit("difficulty", "Hledám poškození odpovídající požadované obtížnosti", 0.82)
    dcfg = DifficultyConfig(target_difficulty=cfg.target, seed=world.seed)
    result = await automatic_mode(
        run_id, dcfg, should_stop=lambda: job.cancelled,
        progress=lambda message, fraction=None: job.emit("difficulty", message,
                                                        None if fraction is None else 0.82 + 0.16 * fraction),
    )
    observation_id = (result.get("artifacts") or {}).get("observation_id")
    if not observation_id:
        info = await asyncio.to_thread(observe_and_store, run_id, world, CorruptionConfig(seed=dcfg.seed),
                                       dcfg.minimum_observability)
        observation_id = info.observation_id
    achieved = result.get("achieved_difficulty")
    if achieved is None and (result.get("baseline") or {}).get("status") == "ok":
        achieved = result["baseline"].get("mean_difficulty")
    return {"mode": "automatic", "status": result["status"], "target": cfg.target, "achieved": achieved,
            "reason": result["reason"], "observation_id": observation_id, "difficulty_id": result["difficulty_id"],
            "used_baseline": not (result.get("artifacts") or {}).get("observation_id")}


def _verdict(rep_spec: ValidationReport, rep_world: ValidationReport, manifest: dict[str, Any]) -> ValidationVerdict:
    status = rep_world.status if rep_spec.status != "FAIL" else "FAIL"
    issues = [Issue(id=c.check_id[:60], severity="critical", table=c.target, description=f"{c.explanation} {c.observed}",
                    suggested_fix=c.suggested_fix) for c in (rep_spec.errors + rep_world.errors)[:20]]
    issues += [Issue(id=c.check_id[:60], severity="minor", table=c.target, description=f"{c.expected} vs {c.observed}")
               for c in rep_world.warnings[:20]]
    realized = manifest.get("realized_story_facts", [])
    not_real = [r["fact_id"] for r in realized if not r.get("realized")]
    summary = (f"SCM validace: {status} – {rep_world.summary.get('checks')} kontrol, {len(rep_world.errors)} chyb, "
               f"{len(rep_world.warnings)} upozornění. Realizované příběhové skutečnosti: "
               f"{len(realized) - len(not_real)}/{len(realized)}" + (f" (nerealizováno: {not_real})" if not_real else ""))
    from ..contracts.io import StepDetectability
    det = [StepDetectability(step_id=r["fact_id"], detectable=bool(r.get("realized")), confidence=0.9 if r.get("realized") else 0.2,
                             comment=json.dumps(r.get("visible_in") or r.get("evidence", {}).get("interventional", {}),
                                                ensure_ascii=False, default=str)[:300]) for r in realized]
    return ValidationVerdict(verdict={"PASS": "pass", "PASS_WITH_WARNINGS": "warn", "FAIL": "fail"}[status],
                             summary=summary, story_detectability=det, issues=issues)


async def execute_causal_run(req: RunRequest, job) -> dict[str, Any]:
    """Generation path for specs that carry an SCM (called from ``pipeline.execute_run``)."""
    from ..reporting.evidence import run_evidence, validate_widgets
    from ..validation.checks import run_checks, summarize

    from ..config import get_settings
    scm = req.spec.scm
    rep = validate_spec(scm)
    if rep.status == "FAIL":
        raise ValueError("SCM není spustitelný: " + "; ".join(f"{c.target}: {c.explanation}" for c in rep.errors[:8]))
    spec = scenario_spec(scm, req.spec)
    run_id = store.new_run_id()
    t0 = dt.datetime.now()
    settings = get_settings()
    run: dict[str, Any] = {
        "run_id": run_id, "label": req.label or "SCM běh", "name": scm.meta.name, "engine": "scm",
        "created_at": t0.isoformat(timespec="seconds"), "status": "running", "parent_run_id": req.parent_run_id,
        "knob_values": {}, "knob_overrides": {}, "mock": settings.mock,
        "models": {"strong": settings.strong_model, "cheap": settings.cheap_model},
        "spec": spec.model_dump(mode="json"), "input": req.input.model_dump() if req.input else None, "job_id": job.id,
    }
    store.save_json(run_id, "run.json", run)
    job.emit("plan", f"Běh {run_id} (kauzální SCM engine)", 0.02, run_id=run_id)
    job.emit("simulation", f"Generuji čistý svět: {len(scm.entities)} entit, {len(scm.variables)} proměnných, "
                           f"{len(scm.events)} událostí", 0.1)
    gw = await asyncio.to_thread(generate_validated, scm)
    w = gw.world
    job.emit("simulation", f"Čistý svět: {sum(len(t) for t in w.tables.values()):,} řádků, hash {w.world_hash[:12]}", 0.5)
    job.emit("validate", f"Validace: spec {gw.spec_report.status}, svět {gw.world_report.status} "
                         f"({len(gw.world_report.errors)} chyb, {len(gw.world_report.warnings)} upozornění)", 0.6)
    job.emit("write", "Zapisuji clean world, ground truth a interní realizace", 0.7)
    meta = await asyncio.to_thread(persist_world, run_id, gw, spec)
    db = store.db_path(run_id)
    checks = await asyncio.to_thread(run_checks, db, spec, {})
    summary = summarize(checks)
    evidence = await asyncio.to_thread(run_evidence, db, spec, {})
    widgets, werrs = await asyncio.to_thread(validate_widgets, db, spec.dashboard, {}, spec.meta.model_dump())
    verdict = _verdict(gw.spec_report, gw.world_report, gw.manifest)
    status = gw.world_report.status if gw.spec_report.status != "FAIL" else "FAIL"
    observation = req.input.observation if req.input else None
    run.update({
        "status": "running" if observation else "done", "verdict": verdict.model_dump(),
        "validator": "scm-deterministic", "checks_summary": summary,
        "signals": [], "repair_history": [], "row_counts": meta["row_counts"], "params": {}, "warnings": scm.meta.warnings,
        "generation_stats": {"timings": w.timings}, "usage": {"total_calls": 0, "total_cost_usd": 0, "total_tokens": 0,
                                                              "by_model": []},
        "dashboard": {"source": "scm", "errors": werrs, "title": spec.dashboard.title},
        "duration_s": round((dt.datetime.now() - t0).total_seconds(), 1), "llm_calls": [],
        "causal": {"status": status, "world_hash": w.world_hash, "spec_hash": w.spec_hash, "seed": w.seed,
                   "validation": {"spec": gw.spec_report.summary, "world": gw.world_report.summary},
                   "realized_story_facts": [{k: r[k] for k in ("fact_id", "realized")} for r in
                                            gw.manifest.get("realized_story_facts", [])]},
    })
    store.save_json(run_id, "run.json", run)
    store.save_json(run_id, "checks.json", [c.to_dict() for c in checks])
    store.save_json(run_id, "evidence.json", evidence)
    store.save_json(run_id, "dashboard.json", widgets)
    store.save_json(run_id, "profile.json", {})
    _remember(f"{run_id}:{w.world_hash}", w, gw.manifest)
    if observation:
        try:
            run["observation"] = (await _prepare_observation(run_id, w, observation, job) if status != "FAIL"
                                  else {"mode": observation.mode, "status": "invalid_world"})
        except Exception:
            run["status"] = "error"
            store.save_json(run_id, "run.json", run)
            raise
        run["status"] = "done"
        run["duration_s"] = round((dt.datetime.now() - t0).total_seconds(), 1)
        store.save_json(run_id, "run.json", run)
    job.emit("done", f"Hotovo: SCM svět {status}, {sum(meta['row_counts'].values()):,} řádků", 1.0, run_id=run_id)
    return {"run_id": run_id}


def causal_summary(run_id: str) -> dict[str, Any]:
    scm, run = run_scm(run_id)
    base = store.run_path(run_id)
    obs = []
    for p in sorted((base / "observations").glob("*/info.json")):
        i = json.loads(read_saved_text(p))
        obs.append({k: i.get(k) for k in ("observation_id", "baseline", "observed_hash", "analyzable", "created_at")}
                   | {"realized_share": (i.get("realized") or {}).get("realized_share")})
    evals = []
    for p in sorted((base / "evaluations").glob("*.json")):
        doc = json.loads(read_saved_text(p))
        e = doc["evaluation"]
        evals.append({**{k: e.get(k) for k in ("evaluation_id", "observation_id", "status", "recovery_score", "difficulty")},
                      "detective": (doc.get("detective") or {}).get("agent", {}).get("kind"),
                      "usage": {k: (doc.get("usage") or {}).get(k) for k in ("calls", "tokens", "cost_usd")}})
    diffs = []
    for p in sorted((base / "difficulty").glob("*.json")):
        d = json.loads(read_saved_text(p))
        diffs.append({"difficulty_id": p.stem, "mode": d.get("mode"), "status": d.get("status"),
                      "target": d.get("target_difficulty"), "achieved": d.get("achieved_difficulty")
                      if d.get("mode") == "automatic" else (d.get("observed") or {}).get("mean_difficulty")})
    validation_path = base / "world" / "validation.json"
    validation = json.loads(read_saved_text(validation_path)) if validation_path.exists() else {}
    from .contracts import derive_edges
    from .mechanisms import describe
    return {"run_id": run_id, "causal": run.get("causal"), "scm": scm.model_dump(mode="json"),
            "edges": [e.model_dump() for e in derive_edges(scm)],
            "mechanisms": {v.variable_id: describe(v) for v in scm.variables},
            "validation": validation, "observations": obs, "evaluations": evals, "difficulty_runs": diffs,
            "llm_usage_total": sum_usage([json.loads(read_saved_text(p)).get("usage") or {}
                                          for p in sorted((base / "evaluations").glob("*.json"))])}


async def legacy_detective_view(run_id: str, job=None, kind: str = "llm") -> dict[str, Any]:
    """The existing "Detective" tab for SCM runs: blind detective on the *observed* baseline (never on the
    clean world), evaluated against the ground truth, returned in the shape the current UI renders."""
    _ensure_valid(run_id)
    world, manifest = load_world(run_id)
    info = observe_and_store(run_id, world, CorruptionConfig())
    cfg = DetectiveConfig(kind=kind)  # in mock mode the LLM detective delegates to the statistical one (labelled)
    res, tracker = await detective_on_tracked(run_id, info.observation_id, cfg,
                                              progress=(lambda m, f=None: job.emit("detective", m, f)) if job else None)
    ev = evaluate(world.spec, manifest, res, EvaluationConfig(), observation_id=info.observation_id)
    store_evaluation(run_id, ev, res, tracker, cfg, world)
    transcript = []
    for item in res.statistical_results:
        transcript.append({"step": len(transcript), "thought": f"{item.tool} {json.dumps(item.args, ensure_ascii=False)[:160]}",
                           "sql": item.args.get("query"), "observation": {"ok": item.ok, "summary": item.summary[:400]}})
        if job:
            job.emit("detective", f"{item.evidence_id} {item.tool}: {item.summary[:120]}", None, sql=item.args.get("query"))
    root = next((k for k in res.key_events if k.role == "root_cause"), None)
    checks = [{"name": f"{name}", "ok": (m.score or 0) >= 0.5, "expected": "1.0", "got":
               (f"{m.score:.2f}" if m.score is not None else m.reason)} for name, m in ev.metrics.items()]
    checks += [{"name": f"fakt: {f['fact_id']}", "ok": (f.get("score") or 0) >= 0.5, "expected": f.get("kind"),
                "got": f.get("status") if f.get("score") is None else f"{f['score']:.2f}"} for f in ev.story_facts]
    out = {
        "final": {"root_cause": res.story_summary, "entity_table": (root.entity or "").split(".")[0] if root else None,
                  "entity_id": root.entity_ids[0] if root and root.entity_ids else None,
                  "start_date": root.start_time if root else None,
                  "causal_chain": [f"{r.source} {'→' if r.edge_type == 'directed' else '—'} {r.target} "
                                   f"({r.estimated_direction}, conf {r.confidence:.2f})"
                                   for r in sorted(res.inferred_relationships, key=lambda r: -r.confidence)[:10]],
                  "ruled_out": res.alternative_explanations, "confidence": round(1 - (ev.difficulty or 100) / 100, 3)},
        "transcript": transcript,
        "score": {"score": round(100 * (ev.recovery_score or 0)), "checks": checks,
                  "queries": len(res.executed_queries), "difficulty": ev.difficulty, "evaluation_id": ev.evaluation_id},
        "agent": res.agent, "observation_id": info.observation_id, "usage": usage_of(tracker),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    store.save_json(run_id, "detective.json", out)
    return out


# =========================================================================== replay of an evaluation
class ReplayLLM:
    """Feeds the recorded responses of an evaluation back to the detective, in order (no API call)."""

    mock = False

    def __init__(self, calls: list[dict[str, Any]]):
        self.calls = [c for c in calls if c.get("ok") and c.get("response") is not None]
        self.used = 0
        models = sorted({c["model"] for c in self.calls}) or ["replay"]
        self.settings = type("S", (), {"strong_model": models[0], "cheap_model": models[0]})()
        self._models = models

    def models_for(self, role: str) -> list[str]:
        return list(self._models)

    async def structured(self, *, task: str, schema, **_: Any):
        if self.used >= len(self.calls):
            raise LLMError(f"replay: recorded responses exhausted after {self.used} calls")
        c = self.calls[self.used]
        if c["task"] != task:
            raise LLMError(f"replay: expected task {c['task']}, got {task}")
        self.used += 1
        return schema.model_validate(c["response"])


def _world_for(run_id: str, info: dict[str, Any]) -> tuple[CleanWorld, dict[str, Any]]:
    world, manifest = load_world(run_id)
    if not info.get("world_hash") or info["world_hash"] == world.world_hash:
        return world, manifest
    scm, _ = run_scm(run_id)  # a structural-noise variant of reality
    w = generate_world(scm, seed=info.get("world_seed"), noise_scale=info.get("noise_scale") or None)
    if w.world_hash != info["world_hash"]:
        raise CausalError("the world of this evaluation could not be reproduced")
    m = json.loads(read_saved_text(store.run_path(run_id) / "worlds" / w.world_hash / "manifest.json"))
    return w, m


async def replay_evaluation(run_id: str, eval_id: str) -> dict[str, Any]:
    """Re-run a stored evaluation without calling the LLM: the detective receives the recorded responses,
    its tools run again on the same observed database, the result is re-scored and compared."""
    if not eval_id.isalnum():
        raise CausalError("invalid evaluation id")
    d = _eval_dir(run_id)
    p = d / f"{eval_id}.json"
    if not p.exists():
        raise CausalError("evaluation not found")
    doc = json.loads(read_saved_text(p))
    info = doc.get("replay") or {}
    if not info.get("detective_config"):
        raise CausalError("this evaluation was stored without replay information")
    cfg = DetectiveConfig.model_validate(info["detective_config"])
    calls = []
    lp = d / f"{eval_id}.llm_calls.jsonl"
    if lp.exists():
        calls = [json.loads(x) for x in read_saved_text(lp).splitlines() if x.strip()]
    db, schema = observation_for_detective(run_id, info["observation_id"])
    replay_llm = ReplayLLM(calls)
    if cfg.kind == "llm" and doc["detective"].get("agent", {}).get("mock"):
        from .detective import analyze_sync
        res = await asyncio.to_thread(analyze_sync, db, schema, cfg)   # mock mode delegated to the statistical one
    elif cfg.kind == "llm":
        res = await analyze(db, schema, cfg, llm=replay_llm)
    else:
        from .detective import analyze_sync
        res = await asyncio.to_thread(analyze_sync, db, schema, cfg)
    world, manifest = _world_for(run_id, info)
    orig_ev = doc["evaluation"]
    ev = evaluate(world.spec, manifest, res, EvaluationConfig.model_validate(orig_ev.get("config") or {}),
                  observation_id=info["observation_id"])
    keys = ("story_summary", "key_events", "inferred_relationships", "estimated_effects")
    new = res.model_dump(mode="json")
    diffs = [k for k in keys if json.dumps(new.get(k), sort_keys=True, default=str)
             != json.dumps(doc["detective"].get(k), sort_keys=True, default=str)]
    return {"evaluation_id": eval_id, "identical": not diffs and ev.difficulty == orig_ev.get("difficulty"),
            "differences": diffs, "difficulty_original": orig_ev.get("difficulty"), "difficulty_replayed": ev.difficulty,
            "llm_calls_recorded": len(calls), "llm_calls_replayed": replay_llm.used,
            "detective_kind": cfg.kind, "note": "no API call was made; tools re-ran on the stored observed database"}
