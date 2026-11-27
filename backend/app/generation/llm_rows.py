"""Generic `llm_rows` / `llm_dimension` path: the CHEAP model generates rows in batches.

Each batch gets: table definition, valid FK values (sampled with labels), its own time window (so
event tables follow the story timeline), the story steps relevant to the table and repair notes.
Output is constrained by a JSON schema built from the column definitions, then validated row by
row (types, enums, ranges, FK membership, PK uniqueness). A batch with too many invalid rows is
retried with the error list.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field, create_model

from ..contracts.spec import Column, ScenarioSpec, Table
from ..llm.client import LLMError, get_llm

PY_TYPES = {"int": int, "float": float, "text": str, "date": str, "datetime": str, "bool": bool}


def _auto_pk(table: Table) -> Optional[str]:
    pk = table.pk
    if len(pk) == 1 and table.column(pk[0]).type == "int":
        return pk[0]
    return None


def row_model(table: Table) -> type[BaseModel]:
    auto = _auto_pk(table)
    fields: dict[str, Any] = {}
    for c in table.columns:
        if c.name == auto:
            continue
        typ: Any = PY_TYPES[c.type]
        if c.type == "text" and c.enum_values:
            typ = Literal[tuple(c.enum_values)]  # type: ignore[valid-type]
        desc = c.description or ""
        if c.type == "date":
            desc += " (YYYY-MM-DD)"
        elif c.type == "datetime":
            desc += " (YYYY-MM-DD HH:MM:SS)"
        if c.fk:
            desc += f" (must be one of the provided {c.fk} values)"
        if c.nullable:
            fields[c.name] = (Optional[typ], Field(None, description=desc.strip()))
        else:
            fields[c.name] = (typ, Field(..., description=desc.strip()))
    Row = create_model(f"Row_{table.name}", **fields)  # type: ignore[call-overload]
    return create_model(f"Batch_{table.name}", rows=(list[Row], ...))  # type: ignore[call-overload]


def _fk_options(spec: ScenarioSpec, table: Table, existing: dict[str, pd.DataFrame], limit: int = 60) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    for c in table.columns:
        if not c.fk:
            continue
        rt, rc = c.fk.split(".")
        df = existing.get(rt)
        if df is None or rc not in df.columns or df.empty:
            continue
        label_col = next((x for x in ("name", "title", "last_name", "sku", "category") if x in df.columns), None)
        sample = df if len(df) <= limit else df.iloc[:: max(1, len(df) // limit)].head(limit)
        vals = sample[rc].tolist()
        opts[c.name] = {
            "ref": c.fk,
            "values": [{"id": _py(v), "label": str(lbl)[:40]} for v, lbl in
                       zip(vals, sample[label_col] if label_col else vals)],
            "all_ids": [_py(v) for v in df[rc].tolist()],
        }
    return opts


def _py(v: Any) -> Any:
    try:
        import numpy as np

        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
    except ImportError:  # pragma: no cover
        pass
    return v


def _windows(spec: ScenarioSpec, n: int) -> list[Optional[tuple[str, str]]]:
    if not spec.meta.has_time:
        return [None] * n
    a = dt.date.fromisoformat(spec.meta.period_start)
    b = dt.date.fromisoformat(spec.meta.period_end)
    span = (b - a).days + 1
    out = []
    for i in range(n):
        s = a + dt.timedelta(days=span * i // n)
        e = a + dt.timedelta(days=span * (i + 1) // n - 1)
        out.append((s.isoformat(), max(s, e).isoformat()))
    return out


def validate_rows(table: Table, rows: list[dict], fk_ids: dict[str, set], period: Optional[tuple[str, str]],
                  used_keys: set) -> tuple[list[dict], list[str]]:
    good, errors = [], []
    for i, r in enumerate(rows):
        err = None
        for c in table.columns:
            if c.name not in r:
                continue
            v = r[c.name]
            if v is None:
                if not c.nullable and not c.pk:
                    err = f"row {i}: {c.name} is null"
                continue
            if c.fk and c.name in fk_ids and v not in fk_ids[c.name]:
                err = f"row {i}: {c.name}={v} is not a valid {c.fk}"
            elif c.type in ("date", "datetime"):
                try:
                    d = dt.date.fromisoformat(str(v)[:10])
                    if c.type == "datetime":
                        dt.datetime.fromisoformat(str(v))
                    if period and not (period[0] <= d.isoformat() <= period[1]) and "date" in c.name \
                            and table.strategy == "llm_rows":
                        err = f"row {i}: {c.name}={v} outside scenario period {period}"
                except ValueError:
                    err = f"row {i}: {c.name}={v} is not ISO formatted"
            elif c.type in ("int", "float") and isinstance(v, (int, float)):
                if c.min is not None and v < c.min or c.max is not None and v > c.max:
                    err = f"row {i}: {c.name}={v} outside [{c.min}, {c.max}]"
            if err:
                break
        if err is None and table.pk and not _auto_pk(table):
            key = tuple(r.get(k) for k in table.pk)
            if key in used_keys:
                err = f"row {i}: duplicate primary key {key}"
            else:
                used_keys.add(key)
        if err:
            errors.append(err)
        else:
            good.append(r)
    return good, errors


async def generate_table(spec: ScenarioSpec, table: Table, existing: dict[str, pd.DataFrame], *,
                         batch_size: int = 25, notes: str = "", progress=None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Generate `table.row_count` rows with the CHEAP model; returns (df, stats)."""
    target = table.row_count or 30
    n_batches = max(1, math.ceil(target / batch_size))
    Batch = row_model(table)
    fk = _fk_options(spec, table, existing)
    fk_ids = {k: set(v["all_ids"]) for k, v in fk.items()}
    period = (spec.meta.period_start, spec.meta.period_end) if spec.meta.has_time else None
    windows = _windows(spec, n_batches)
    steps = [s.model_dump() for s in spec.story.causal_chain if table.name in s.tables] or \
            [s.model_dump() for s in spec.story.causal_chain]
    distractors = [d.model_dump() for d in spec.story.distractors if table.name in d.tables]
    llm = get_llm()
    stats = {"batches": n_batches, "invalid_rows": 0, "retries": 0, "errors": []}

    cols_desc = [{"name": c.name, "type": c.type, "description": c.description, "fk": c.fk, "nullable": c.nullable,
                  "enum": c.enum_values or None, "min": c.min, "max": c.max}
                 for c in table.columns if c.name != _auto_pk(table)]
    fk_prompt = {k: v["values"] for k, v in fk.items()}

    async def one(i: int) -> list[dict]:
        k = min(batch_size, target - i * batch_size)
        w = windows[i]
        base_prompt = (
            f"Generate exactly {k} rows for table '{table.name}' ({table.description}).\n"
            f"Data language: {spec.meta.data_language}. Company: {spec.meta.company[:300]}\n"
            + (f"Scenario period: {period[0]} .. {period[1]}. This batch {i + 1}/{n_batches} covers dates {w[0]} .. "
               f"{w[1]} (put date/time columns of event-like rows into this window).\n" if period else
               f"The data has no time dimension (batch {i + 1}/{n_batches}).\n")
            + f"Columns: {json.dumps(cols_desc, ensure_ascii=False)}\n"
            f"Valid foreign key values (id + label): {json.dumps(fk_prompt, ensure_ascii=False)[:6000]}\n"
            f"Story (make the data consistent with it, effects only for affected entities and after their start "
            f"date): {spec.story.logline}\nRelevant causal steps: {json.dumps(steps, ensure_ascii=False)[:3000]}\n"
            f"Distractors: {json.dumps(distractors, ensure_ascii=False)[:1500]}\n"
            f"Rows must be realistic and varied; do not repeat the same text. {notes}"
        )
        used: set = set()
        last_errors: list[str] = []
        for attempt in range(3):
            prompt = base_prompt
            if last_errors:
                prompt += "\nPrevious attempt had invalid rows, avoid: " + "; ".join(last_errors[:8])
            try:
                res = await llm.structured(
                    role="cheap", task=f"llm_rows:{table.name}", schema=Batch, prompt=prompt,
                    mock_context={"table": table.model_dump(), "n": k, "window": w, "fk": {c: v["all_ids"] for c, v in fk.items()},
                                  "batch": i, "attempt": attempt, "seed": spec.meta.seed},
                    use_cache=attempt == 0,
                )
            except LLMError as e:
                stats["errors"].append(str(e)[:200])
                return []
            rows = [r.model_dump() for r in res.rows]
            good, errs = validate_rows(table, rows, fk_ids, period, used)
            stats["invalid_rows"] += len(errs)
            if len(good) >= 0.8 * k or attempt == 2:
                if progress:
                    progress(f"{table.name}: dávka {i + 1}/{n_batches}", None)
                return good[:k]
            stats["retries"] += 1
            last_errors = errs
            used = set()
        return []

    batches = await asyncio.gather(*[one(i) for i in range(n_batches)])
    rows = [r for b in batches for r in b]
    df = pd.DataFrame(rows, columns=[c.name for c in table.columns if c.name != _auto_pk(table)])
    auto = _auto_pk(table)
    if auto:
        df.insert(0, auto, range(1, len(df) + 1))
        df = df[[c.name for c in table.columns]]
    stats["rows"] = len(df)
    return df, stats
