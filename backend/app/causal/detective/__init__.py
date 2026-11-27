"""Blind detective: ``analyze(observed_db_path, public_schema, config)`` – the only entry point.

It receives a path to the separate observed database (opened read-only by ``sqlsafe.ReadOnlyDB``)
and the neutral public schema. Nothing else from the run (story, SCM, ground truth, clean world,
corruption manifest, target difficulty, previous evaluations) is passed in."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..contracts import DetectiveConfig
from ..results import DetectiveResult, PublicSchema
from ..sqlsafe import ReadOnlyDB
from .llm_agent import run_llm_detective
from .statistical import StatisticalDetective
from .tools import Toolbox


def make_toolbox(db_path: Path, schema: PublicSchema, config: DetectiveConfig) -> Toolbox:
    db = ReadOnlyDB(db_path, timeout_s=config.sql_timeout_s, max_queries=config.max_queries)
    return Toolbox(db, schema, max_rows=config.max_result_rows, sample_rows=config.sample_rows, alpha=config.alpha,
                   seed=config.seed)


async def analyze(db_path: Path, schema: PublicSchema, config: DetectiveConfig,
                  progress: Optional[Callable[[str, float], None]] = None, llm=None) -> DetectiveResult:
    tb = make_toolbox(db_path, schema, config)
    if config.kind == "llm":
        return await run_llm_detective(tb, config, progress, llm=llm)
    return StatisticalDetective(tb, config).run()


def analyze_sync(db_path: Path, schema: PublicSchema, config: DetectiveConfig) -> DetectiveResult:
    if config.kind == "llm":
        import asyncio
        return asyncio.run(analyze(db_path, schema, config))
    return StatisticalDetective(make_toolbox(db_path, schema, config), config).run()
