"""Compact data profile for the STRONG validator – never the whole dataset."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..contracts.spec import ScenarioSpec


def _q(n: str) -> str:
    return '"' + n.replace('"', "") + '"'


def profile_db(db: Path, spec: ScenarioSpec, sample_rows: int = 4) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, Any] = {}
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in spec.tables:
            if t.name not in existing:
                out[t.name] = {"missing": True}
                continue
            T = _q(t.name)
            n = conn.execute(f"SELECT COUNT(*) FROM {T}").fetchone()[0]
            cols = {}
            for c in t.columns:
                C = _q(c.name)
                nulls = conn.execute(f"SELECT COUNT(*) FROM {T} WHERE {C} IS NULL").fetchone()[0]
                info: dict[str, Any] = {"type": c.type, "null_pct": round(100 * nulls / n, 1) if n else 0}
                if c.type in ("int", "float") and not c.pk:
                    mn, mx, avg = conn.execute(f"SELECT MIN({C}), MAX({C}), AVG({C}) FROM {T}").fetchone()
                    info.update(min=mn, max=mx, mean=round(avg, 2) if avg is not None else None)
                elif c.type in ("date", "datetime"):
                    mn, mx = conn.execute(f"SELECT MIN({C}), MAX({C}) FROM {T}").fetchone()
                    info.update(min=mn, max=mx)
                elif c.type == "text":
                    d = conn.execute(f"SELECT COUNT(DISTINCT {C}) FROM {T}").fetchone()[0]
                    info["distinct"] = d
                    if d <= 30:
                        info["top"] = conn.execute(
                            f"SELECT {C}, COUNT(*) FROM {T} GROUP BY 1 ORDER BY 2 DESC LIMIT 8").fetchall()
                cols[c.name] = info
            # deterministic, evenly spaced sample (keeps LLM prompts – and thus the cache – reproducible)
            offsets = sorted({int(i * max(n - 1, 0) / max(sample_rows - 1, 1)) for i in range(sample_rows)}) if n else []
            sample = [conn.execute(f"SELECT * FROM {T} LIMIT 1 OFFSET {o}").fetchone() for o in offsets]
            out[t.name] = {"rows": n, "strategy": t.strategy, "columns": cols,
                           "sample": [[(v[:120] if isinstance(v, str) else v) for v in r] for r in sample]}
    finally:
        conn.close()
    return out
