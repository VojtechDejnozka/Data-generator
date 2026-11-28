"""Per-run working copy on the local (possibly ephemeral) file system, synchronised with object storage.

The generator, SQL guard, SCM engine, detective and document renderer all work on files of a run directory.
In local mode that directory is the permanent store. With Supabase the directory under WORKSPACE_DIR is only
a cache (``/tmp`` on Vercel):

* ``ensure_local(meta)`` downloads files whose sha256 differs from the run's storage index (``runs.files``);
* ``sync_up(run_id, owner)`` uploads new/changed files (``*.sqlite`` gzip-compressed), removes deleted ones and
  stores the new index;
* least-recently-used run directories are evicted above WORKSPACE_CACHE_MB.

Ownership is checked by the caller *before* ``ensure_local`` – this module never decides who may read a run.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..config import get_settings
from .blobs import BlobError, get_blobs
from .meta import get_meta

log = logging.getLogger("workspace")

SYNC_FILE = ".sync.json"
SKIP_SUFFIXES = (".tmp", ".part", ".lock")
GZIP_SUFFIXES = (".sqlite",)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_pinned: dict[str, int] = {}


def _lock(run_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(run_id, threading.RLock())


def remote_enabled() -> bool:
    return get_blobs() is not None


def run_dir(run_id: str) -> Path:
    return get_settings().workspace_dir / run_id


def object_key(owner: str, run_id: str, rel: str, gz: bool = False) -> str:
    return f"{owner}/{run_id}/{rel}" + (".gz" if gz else "")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_state(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / SYNC_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(d: Path, state: dict[str, Any]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (SYNC_FILE + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(d / SYNC_FILE)


def _local_files(d: Path) -> dict[str, Path]:
    out = {}
    if not d.exists():
        return out
    for p in d.rglob("*"):
        if p.is_file() and p.name != SYNC_FILE and not p.name.endswith(SKIP_SUFFIXES):
            out[p.relative_to(d).as_posix()] = p
    return out


class pinned:
    """Keep a run directory from being evicted while it is in use."""

    def __init__(self, run_id: str):
        self.run_id = run_id

    def __enter__(self):
        with _locks_guard:
            _pinned[self.run_id] = _pinned.get(self.run_id, 0) + 1
        return self

    def __exit__(self, *exc):
        with _locks_guard:
            _pinned[self.run_id] -= 1
            if _pinned[self.run_id] <= 0:
                _pinned.pop(self.run_id, None)


# --------------------------------------------------------------------------- download
def ensure_local(meta: dict[str, Any]) -> Path:
    """Make the working copy of an (already authorised) run current. Returns its directory."""
    run_id = meta["id"]
    d = run_dir(run_id)
    blobs = get_blobs()
    if blobs is None:
        return d
    index: dict[str, Any] = meta.get("files") or {}
    with _lock(run_id):
        state = _read_state(d)
        changed = False
        for rel, info in index.items():
            target = d / rel
            if state.get(rel, {}).get("sha") == info.get("sha") and target.exists():
                continue
            key = object_key(meta["user_id"], run_id, rel, info.get("gz", False))
            tmp = target.with_name(target.name + ".dl.part")
            blobs.download(key, tmp)
            if info.get("gz"):
                with gzip.open(tmp, "rb") as src, open(target.with_name(target.name + ".tmp"), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                tmp.unlink(missing_ok=True)
                target.with_name(target.name + ".tmp").replace(target)
            else:
                tmp.replace(target)
            st = target.stat()
            state[rel] = {"sha": info.get("sha"), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            changed = True
        for rel in [r for r in state if r not in index]:  # deleted remotely
            (d / rel).unlink(missing_ok=True)
            state.pop(rel, None)
            changed = True
        if changed or not (d / SYNC_FILE).exists():
            _write_state(d, state)
        else:
            (d / SYNC_FILE).touch()
    evict(exclude={run_id})
    return d


# --------------------------------------------------------------------------- upload
def sync_up(run_id: str, owner: str) -> Optional[dict[str, Any]]:
    """Upload changed files of a run and persist the storage index. Returns the index (None in local mode)."""
    blobs = get_blobs()
    if blobs is None:
        return None
    d = run_dir(run_id)
    with _lock(run_id):
        state = _read_state(d)
        meta = get_meta().get_run(run_id, None) or {}
        if meta and meta.get("user_id") != owner:
            # never upload into (or re-own) another user's run, e.g. a shared preset
            raise PermissionError(f"run {run_id} is not owned by {owner}")
        index: dict[str, Any] = dict(meta.get("files") or {})
        files = _local_files(d)
        for rel, p in files.items():
            st = p.stat()
            prev = state.get(rel)
            if prev and prev.get("size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns and rel in index:
                continue
            sha = _sha256(p)
            gz = rel.endswith(GZIP_SUFFIXES)
            if not (prev and prev.get("sha") == sha and index.get(rel, {}).get("sha") == sha):
                src = p
                if gz:
                    src = p.with_name(p.name + ".gz.tmp")
                    with open(p, "rb") as fin, gzip.open(src, "wb", compresslevel=6) as fout:
                        shutil.copyfileobj(fin, fout, 1 << 20)
                try:
                    blobs.upload(object_key(owner, run_id, rel, gz), src,
                                 "application/gzip" if gz else _content_type(rel))
                finally:
                    if gz:
                        src.unlink(missing_ok=True)
            index[rel] = {"sha": sha, "size": st.st_size, "gz": gz}
            state[rel] = {"sha": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        gone = [rel for rel in index if rel not in files]
        if gone:
            try:
                blobs.delete([object_key(owner, run_id, rel, index[rel].get("gz", False)) for rel in gone])
            except BlobError as e:
                log.warning("could not delete stale objects of %s: %s", run_id, e)
            for rel in gone:
                index.pop(rel, None)
                state.pop(rel, None)
        _write_state(d, state)
        get_meta().upsert_run({"id": run_id, "user_id": owner, "files": index})
    return index


def delete_remote(run_id: str, owner: str, index: dict[str, Any]) -> None:
    blobs = get_blobs()
    if blobs is not None and index:
        blobs.delete([object_key(owner, run_id, rel, info.get("gz", False)) for rel, info in index.items()])
    if blobs is not None:
        shutil.rmtree(run_dir(run_id), ignore_errors=True)


def _content_type(rel: str) -> str:
    return {"json": "application/json", "jsonl": "application/x-ndjson", "html": "text/html", "pdf": "application/pdf",
            "md": "text/markdown", "txt": "text/plain", "eml": "message/rfc822", "zip": "application/zip"}.get(
        rel.rsplit(".", 1)[-1].lower(), "application/octet-stream")


# --------------------------------------------------------------------------- eviction
def _dir_size(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def _dirty(d: Path) -> bool:
    """True when the directory holds changes that were not uploaded yet (never evict those)."""
    state = _read_state(d)
    for rel, p in _local_files(d).items():
        st = p.stat()
        prev = state.get(rel)
        if not prev or prev.get("size") != st.st_size or prev.get("mtime_ns") != st.st_mtime_ns:
            return True
    return False


def evict(exclude: set[str] = frozenset()) -> None:
    if get_blobs() is None:
        return
    root = get_settings().workspace_dir
    cap = get_settings().workspace_cache_mb * 1024 * 1024
    dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    sizes = {p: _dir_size(p) for p in dirs}
    total = sum(sizes.values())
    if total <= cap:
        return

    def last_used(p: Path) -> float:
        s = p / SYNC_FILE
        return s.stat().st_mtime if s.exists() else 0.0

    for p in sorted(dirs, key=last_used):
        if total <= cap:
            break
        if p.name in exclude or p.name in _pinned or time.time() - last_used(p) < 5 or _dirty(p):
            continue
        with _lock(p.name):
            shutil.rmtree(p, ignore_errors=True)
        total -= sizes[p]
