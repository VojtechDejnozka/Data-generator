"""Run storage: each run = directory with data.sqlite + run.json + manifest.json + llm_calls.jsonl.

The directory lives in the workspace (storage/workspace.py): permanent in local mode, a cache of Supabase
Storage otherwise. Every ``run.json`` write also upserts the run's metadata row (owner, status, list summary)
in the metadata store; the owner comes from ``set_owner()`` (jobs) and is never taken from request bodies.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import io
import json
import re
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

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_owner", default=None)

SQL_TYPES = {"int": "INTEGER", "float": "REAL", "text": "TEXT", "date": "TEXT", "datetime": "TEXT", "bool": "INTEGER"}


def runs_dir() -> Path:
    return get_settings().workspace_dir


def new_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def check_run_id(run_id: str) -> str:
    if not run_id or not _RUN_ID.match(run_id):
        raise ValueError("invalid run id")
    return run_id


def run_path(run_id: str) -> Path:
    return runs_dir() / check_run_id(run_id)


def set_owner(user_id: str | None):
    """Owner of runs created in the current context (set by the job runner)."""
    return _owner.set(user_id)


def reset_owner(token) -> None:
    _owner.reset(token)


def current_owner() -> str | None:
    return _owner.get()


def summary_from_doc(r: dict[str, Any]) -> dict[str, Any]:
    """Fields the run list needs (kept in runs.summary so listing never opens run files)."""
    item = {k: r.get(k) for k in ("label", "status", "created_at", "parent_run_id", "knob_values", "verdict",
                                  "row_counts", "name", "mock")}
    item["kind"] = r.get("kind", "data")
    if isinstance(item.get("verdict"), dict):  # list view needs only the headline
        item["verdict"] = {k: item["verdict"].get(k) for k in ("verdict", "summary")}
    if item["kind"] == "documents":
        ds = r.get("doc_spec") or {}
        item.update(format=ds.get("format"), count=ds.get("count"), document_type=ds.get("document_type"),
                    source_run_id=ds.get("source_run_id"), source_table=ds.get("source_table"))
    if r.get("usage"):
        u = r["usage"]
        item["usage"] = {"total_tokens": u.get("total_tokens"), "total_cost_usd": u.get("total_cost_usd"),
                         "total_calls": u.get("total_calls")}
    return item


def meta_from_doc(r: dict[str, Any], owner: str) -> dict[str, Any]:
    status = r.get("status") if r.get("status") in ("running", "done", "error") else "running"
    created = r.get("created_at")
    if created and "+" not in str(created) and not str(created).endswith("Z"):
        try:  # run.json stores naive local time
            created = dt.datetime.fromisoformat(str(created)).astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            created = None
    out = {"id": r["run_id"], "user_id": owner, "kind": r.get("kind", "data"), "engine": r.get("engine"),
           "label": str(r.get("label") or "")[:200], "name": str(r.get("name") or "")[:200], "status": status,
           "parent_run_id": r.get("parent_run_id"), "summary": summary_from_doc(r)}
    if created:
        out["created_at"] = created
    return out


def _sync_meta(run: dict[str, Any]) -> None:
    from .meta import get_meta

    from ..config import DEV_USER_ID

    owner = run.get("user_id") or current_owner()
    if not owner:
        if get_settings().use_supabase:
            raise RuntimeError("run owner unknown (set_owner() not called)")
        owner = DEV_USER_ID  # local scripts/tests without an account context
    run["user_id"] = owner
    get_meta().upsert_run(meta_from_doc(run, owner))


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
    if name == "run.json" and isinstance(data, dict):
        _sync_meta(data)
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


def list_item(meta: dict[str, Any], viewer_id: str | None = None) -> dict[str, Any]:
    """Run-list entry from a metadata row."""
    item = dict(meta.get("summary") or {})
    item.update(run_id=meta["id"], status=meta.get("status") or item.get("status"),
                label=meta.get("label") or item.get("label"), name=meta.get("name") or item.get("name"),
                kind=meta.get("kind") or item.get("kind", "data"), is_preset=bool(meta.get("is_preset")),
                shared=bool(meta.get("share_token")), parent_run_id=meta.get("parent_run_id"))
    item["own"] = viewer_id is None or meta.get("user_id") == viewer_id
    item.setdefault("created_at", meta.get("created_at"))
    return item


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
