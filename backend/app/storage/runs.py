"""Run storage: each run = directory with data.sqlite + run.json + manifest.json + llm_calls.jsonl."""
from __future__ import annotations

import datetime as dt
import io
import json
import secrets
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..config import get_settings
from ..contracts.spec import ScenarioSpec, Table
from .text_files import read_saved_text

SQL_TYPES = {"int": "INTEGER", "float": "REAL", "text": "TEXT", "date": "TEXT", "datetime": "TEXT", "bool": "INTEGER"}


def runs_dir() -> Path:
    return get_settings().data_dir


def new_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def run_path(run_id: str) -> Path:
    if not run_id or "/" in run_id or ".." in run_id:
        raise ValueError("invalid run id")
    return runs_dir() / run_id


def db_path(run_id: str) -> Path:
    return run_path(run_id) / "data.sqlite"


def ddl_for(table: Table) -> str:
    cols = []
    for c in table.columns:
        piece = f'"{c.name}" {SQL_TYPES[c.type]}'
        if not c.nullable and not c.pk:
            piece += " NOT NULL"
        cols.append(piece)
    pk = table.pk
    if pk:
        cols.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")")
    for c in table.columns:
        if c.fk:
            rt, rc = c.fk.split(".")
            cols.append(f'FOREIGN KEY ("{c.name}") REFERENCES "{rt}"("{rc}")')
    return f'CREATE TABLE "{table.name}" (\n  ' + ",\n  ".join(cols) + "\n)"


def _coerce(df: pd.DataFrame, table: Table) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for c in table.columns:
        s = df[c.name] if c.name in df.columns else pd.Series([None] * len(df), index=df.index)
        if c.type == "bool":
            s = s.map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else int(bool(v)))
        out[c.name] = s
    return out


def write_sqlite(path: Path, spec: ScenarioSpec, tables: dict[str, pd.DataFrame]) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        for name in spec.topological_order():
            t = spec.table(name)
            df = tables.get(name)
            if t is None or df is None:
                continue
            conn.execute(ddl_for(t))
            df = _coerce(df, t)
            cols = [c.name for c in t.columns]
            placeholders = ",".join("?" * len(cols))
            records = df.astype(object).where(pd.notna(df), None).itertuples(index=False, name=None)
            conn.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})',
                             ([_py(v) for v in r] for r in records))
        # helpful indexes on FK columns for evidence queries / data browser
        for t in spec.tables:
            if t.name not in tables:
                continue
            for c in t.columns:
                if c.fk or c.type in ("date", "datetime"):
                    conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{t.name}_{c.name}" ON "{t.name}"("{c.name}")')
        conn.commit()
    finally:
        conn.close()


def _py(v: Any) -> Any:
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.bool_):
        return int(v)
    return v


def read_table(run_id: str, name: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db_path(run_id)}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(f'SELECT * FROM "{name}"', conn)
    finally:
        conn.close()


def save_json(run_id: str, name: str, data: Any) -> None:
    p = run_path(run_id)
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")


def load_json(run_id: str, name: str, default: Any = None) -> Any:
    p = run_path(run_id) / name
    if not p.exists():
        return default
    return json.loads(read_saved_text(p))


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (dt.date, dt.datetime, pd.Timestamp)):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(type(o))


def list_runs() -> list[dict[str, Any]]:
    out = []
    for p in sorted(runs_dir().glob("*/run.json"), reverse=True):
        try:
            r = json.loads(read_saved_text(p))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        item = {k: r.get(k) for k in ("run_id", "label", "status", "created_at", "parent_run_id",
                                      "knob_values", "verdict", "row_counts", "name", "mock")}
        item["kind"] = r.get("kind", "data")
        if item["kind"] == "documents":
            ds = r.get("doc_spec") or {}
            item.update(format=ds.get("format"), count=ds.get("count"), document_type=ds.get("document_type"),
                        source_run_id=ds.get("source_run_id"), source_table=ds.get("source_table"))
        out.append(item)
    return out


def load_run(run_id: str) -> Optional[dict[str, Any]]:
    return load_json(run_id, "run.json")


def export_zip(run_id: str) -> bytes:
    buf = io.BytesIO()
    conn = sqlite3.connect(f"file:{db_path(run_id)}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                df = pd.read_sql_query(f'SELECT * FROM "{n}"', conn)
                z.writestr(f"{n}.csv", df.to_csv(index=False))
            run = load_run(run_id) or {}
            z.writestr("spec.json", json.dumps(run.get("spec"), ensure_ascii=False, indent=1))
            z.writestr("README.txt", f"Story2Data run {run_id}\nTables: {', '.join(names)}\n"
                                     "Ground truth manifest is intentionally not included (see manifest.json in the run "
                                     "directory or the UI).\n")
    finally:
        conn.close()
    return buf.getvalue()
