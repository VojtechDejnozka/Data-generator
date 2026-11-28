"""Metadata store: profiles, run metadata, jobs, planning sessions and usage.

Two implementations with one interface:

* ``SupabaseMetaStore`` – Supabase Postgres through PostgREST. Reads made on behalf of a user carry the
  user's access token (RLS applies); writes use the secret key and happen only after the caller has
  checked ownership in code (users have no write policies at all, see supabase/migrations).
* ``LocalMetaStore`` – JSON files under ``DATA_DIR/_meta`` for local development and tests. It applies the
  same visibility rules in code (own rows + presets).

Run *contents* (spec, data, manifests) live in run files (see storage/workspace.py); this store holds only
what is needed to list, authorise and share runs.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

import httpx

from ..config import DEV_USER_ID, Settings, get_settings

log = logging.getLogger("meta")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat()


def parse_ts(v: Any) -> Optional[dt.datetime]:
    if not v:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class Viewer:
    """Who is asking. ``token`` is the user's access token (forwarded to PostgREST so RLS applies)."""

    id: str
    token: str = ""
    is_admin: bool = False


class MetaStoreError(RuntimeError):
    pass


RUN_FIELDS = ("id", "user_id", "kind", "engine", "label", "name", "status", "parent_run_id", "is_preset",
              "share_token", "summary", "files", "created_at", "updated_at")
JOB_FIELDS = ("id", "user_id", "kind", "run_id", "status", "stage", "phase", "progress", "state", "events",
              "result", "error", "cancel_requested", "lease_until", "lease_holder", "attempts", "created_at",
              "updated_at")


def visible(run: Optional[dict[str, Any]], viewer: Optional[Viewer]) -> bool:
    if run is None:
        return False
    if viewer is None:  # service context
        return True
    return run.get("user_id") == viewer.id or bool(run.get("is_preset"))


class MetaStore(Protocol):
    # profiles
    def get_profile(self, uid: str) -> Optional[dict[str, Any]]: ...
    def ensure_profile(self, uid: str, email: str) -> dict[str, Any]: ...
    def list_profiles(self) -> list[dict[str, Any]]: ...
    def update_profile(self, uid: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]: ...
    # runs
    def get_run(self, run_id: str, viewer: Optional[Viewer]) -> Optional[dict[str, Any]]: ...
    def list_runs(self, viewer: Viewer) -> list[dict[str, Any]]: ...
    def upsert_run(self, row: dict[str, Any]) -> None: ...
    def run_by_share_token(self, token: str) -> Optional[dict[str, Any]]: ...
    def delete_run(self, run_id: str) -> None: ...
    # jobs
    def create_job(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def get_job(self, job_id: str) -> Optional[dict[str, Any]]: ...
    def update_job(self, job_id: str, fields: dict[str, Any]) -> None: ...
    def try_lease(self, job_id: str, holder: str, seconds: int) -> bool: ...
    def renew_lease(self, job_id: str, holder: str, seconds: int) -> bool: ...
    def release_lease(self, job_id: str, holder: str) -> None: ...
    def count_jobs_since(self, uid: str, since: dt.datetime, kinds: Optional[Iterable[str]] = None) -> int: ...
    # planning sessions
    def save_session(self, row: dict[str, Any]) -> None: ...
    def get_session(self, sid: str) -> Optional[dict[str, Any]]: ...
    # usage
    def add_usage(self, row: dict[str, Any]) -> None: ...
    def usage_since(self, uid: Optional[str], since: dt.datetime) -> list[dict[str, Any]]: ...


# =========================================================================== local
class LocalMetaStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "_meta"
        for d in ("profiles", "runs", "jobs", "sessions"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- file helpers ------------------------------------------------------------
    @staticmethod
    def _safe(key: str) -> str:
        if not key or not all(c.isalnum() or c in "-_" for c in key) or len(key) > 64:
            raise MetaStoreError("invalid id")
        return key

    def _path(self, table: str, key: str) -> Path:
        return self.root / table / f"{self._safe(key)}.json"

    def _read(self, table: str, key: str) -> Optional[dict[str, Any]]:
        try:
            p = self._path(table, key)
        except MetaStoreError:
            return None
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write(self, table: str, key: str, row: dict[str, Any]) -> None:
        p = self._path(table, key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(p)

    def _all(self, table: str) -> list[dict[str, Any]]:
        out = []
        for p in (self.root / table).glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return out

    # -- profiles ------------------------------------------------------------------
    def get_profile(self, uid: str) -> Optional[dict[str, Any]]:
        return self._read("profiles", uid)

    def ensure_profile(self, uid: str, email: str) -> dict[str, Any]:
        with self._lock:
            p = self._read("profiles", uid)
            if p is None:
                p = {"id": uid, "email": (email or "").lower(), "display_name": "", "role": "user",
                     "status": "active", "approved": False, "created_at": iso(utcnow())}
                if uid == DEV_USER_ID:
                    p.update(role="admin", approved=True, email=p["email"] or "dev@localhost")
                self._write("profiles", uid, p)
            return p

    def list_profiles(self) -> list[dict[str, Any]]:
        return sorted(self._all("profiles"), key=lambda r: r.get("created_at") or "")

    def update_profile(self, uid: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            p = self._read("profiles", uid)
            if p is None:
                return None
            p.update(fields)
            self._write("profiles", uid, p)
            return p

    # -- runs ------------------------------------------------------------------------
    def _legacy_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Run directories created before accounts existed belong to the local dev user."""
        from . import runs as store

        try:
            p = self.settings.data_dir / store.check_run_id(run_id) / "run.json"
        except ValueError:
            return None
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return store.meta_from_doc(doc, owner=DEV_USER_ID)

    def get_run(self, run_id: str, viewer: Optional[Viewer]) -> Optional[dict[str, Any]]:
        row = self._read("runs", run_id) or self._legacy_run(run_id)
        return row if visible(row, viewer) else None

    def list_runs(self, viewer: Viewer) -> list[dict[str, Any]]:
        rows = {r["id"]: r for r in self._all("runs")}
        if viewer.id == DEV_USER_ID:
            for p in self.settings.data_dir.glob("*/run.json"):
                rid = p.parent.name
                if rid not in rows and not rid.startswith("_"):
                    legacy = self._legacy_run(rid)
                    if legacy:
                        rows[rid] = legacy
        out = [r for r in rows.values() if visible(r, viewer)]
        return sorted(out, key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)

    def upsert_run(self, row: dict[str, Any]) -> None:
        with self._lock:
            cur = self._read("runs", row["id"]) or {}
            cur.update({k: v for k, v in row.items() if k in RUN_FIELDS})
            cur.setdefault("created_at", iso(utcnow()))
            cur["updated_at"] = iso(utcnow())
            self._write("runs", row["id"], cur)

    def run_by_share_token(self, token: str) -> Optional[dict[str, Any]]:
        if not token:
            return None
        for r in self._all("runs"):
            if r.get("share_token") and r["share_token"] == token:
                return r
        return None

    def delete_run(self, run_id: str) -> None:
        try:
            self._path("runs", run_id).unlink(missing_ok=True)
        except MetaStoreError:
            pass

    # -- jobs --------------------------------------------------------------------------
    def create_job(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = iso(utcnow())
            full = {"status": "queued", "stage": "", "phase": "", "progress": 0.0, "state": {}, "events": [],
                    "result": None, "error": None, "cancel_requested": False, "lease_until": None,
                    "lease_holder": None, "attempts": 0, "created_at": now, "updated_at": now, **row}
            self._write("jobs", row["id"], full)
            return full

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._read("jobs", job_id)

    def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            cur = self._read("jobs", job_id)
            if cur is None:
                return
            cur.update(fields)
            cur["updated_at"] = iso(utcnow())
            self._write("jobs", job_id, cur)

    def try_lease(self, job_id: str, holder: str, seconds: int) -> bool:
        with self._lock:
            cur = self._read("jobs", job_id)
            if cur is None:
                return False
            until = parse_ts(cur.get("lease_until"))
            if until and until > utcnow() and cur.get("lease_holder") != holder:
                return False
            cur.update(lease_until=iso(utcnow() + dt.timedelta(seconds=seconds)), lease_holder=holder,
                       updated_at=iso(utcnow()))
            self._write("jobs", job_id, cur)
            return True

    def renew_lease(self, job_id: str, holder: str, seconds: int) -> bool:
        with self._lock:
            cur = self._read("jobs", job_id)
            if cur is None or cur.get("lease_holder") != holder:
                return False
            cur["lease_until"] = iso(utcnow() + dt.timedelta(seconds=seconds))
            self._write("jobs", job_id, cur)
            return True

    def release_lease(self, job_id: str, holder: str) -> None:
        with self._lock:
            cur = self._read("jobs", job_id)
            if cur is not None and cur.get("lease_holder") == holder:
                cur.update(lease_until=None, lease_holder=None)
                self._write("jobs", job_id, cur)

    def count_jobs_since(self, uid: str, since: dt.datetime, kinds: Optional[Iterable[str]] = None) -> int:
        ks = set(kinds) if kinds else None
        n = 0
        for j in self._all("jobs"):
            if j.get("user_id") != uid or (ks and j.get("kind") not in ks):
                continue
            t = parse_ts(j.get("created_at"))
            if t and t >= since:
                n += 1
        return n

    # -- sessions ----------------------------------------------------------------------
    def save_session(self, row: dict[str, Any]) -> None:
        with self._lock:
            cur = self._read("sessions", row["id"]) or {"created_at": iso(utcnow())}
            cur.update(row)
            cur["updated_at"] = iso(utcnow())
            self._write("sessions", row["id"], cur)

    def get_session(self, sid: str) -> Optional[dict[str, Any]]:
        return self._read("sessions", sid)

    # -- usage ----------------------------------------------------------------------
    def add_usage(self, row: dict[str, Any]) -> None:
        with self._lock:
            with open(self.root / "usage.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"created_at": iso(utcnow()), **row}, default=str) + "\n")

    def usage_since(self, uid: Optional[str], since: dt.datetime) -> list[dict[str, Any]]:
        p = self.root / "usage.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = parse_ts(r.get("created_at"))
            if (uid is None or r.get("user_id") == uid) and t and t >= since:
                out.append(r)
        return out


# =========================================================================== Supabase (PostgREST)
class SupabaseMetaStore:
    def __init__(self, settings: Settings, transport: Optional[httpx.BaseTransport] = None):
        self.settings = settings
        self.http = httpx.Client(base_url=f"{settings.supabase_url}/rest/v1", timeout=15.0, transport=transport)

    # -- headers ------------------------------------------------------------------------
    def _service_headers(self) -> dict[str, str]:
        key = self.settings.supabase_secret_key
        h = {"apikey": key}
        if not key.startswith("sb_"):  # legacy service_role JWT must also be sent as bearer
            h["Authorization"] = f"Bearer {key}"
        return h

    def _user_headers(self, viewer: Viewer) -> dict[str, str]:
        return {"apikey": self.settings.supabase_publishable_key, "Authorization": f"Bearer {viewer.token}"}

    def _headers(self, viewer: Optional[Viewer]) -> dict[str, str]:
        return self._user_headers(viewer) if viewer is not None and viewer.token else self._service_headers()

    def _req(self, method: str, path: str, *, viewer: Optional[Viewer] = None, params: Any = None,
             json_body: Any = None, prefer: str = "") -> Any:
        headers = self._headers(viewer)
        if prefer:
            headers["Prefer"] = prefer
        try:
            r = self.http.request(method, path, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as e:
            log.error("PostgREST %s %s failed: %s", method, path, e)
            raise MetaStoreError("metadata store unavailable") from e
        if r.status_code >= 400:
            log.error("PostgREST %s %s -> %s %s", method, path, r.status_code, r.text[:500])
            raise MetaStoreError(f"metadata store error {r.status_code}")
        if not r.content:
            return None
        return r.json()

    def _one(self, rows: Any) -> Optional[dict[str, Any]]:
        return rows[0] if isinstance(rows, list) and rows else None

    # -- profiles ------------------------------------------------------------------------
    def get_profile(self, uid: str) -> Optional[dict[str, Any]]:
        return self._one(self._req("GET", "/profiles", params={"id": f"eq.{uid}", "select": "*"}))

    def ensure_profile(self, uid: str, email: str) -> dict[str, Any]:
        p = self.get_profile(uid)
        if p is None:  # created by the auth.users trigger; this is only a fallback
            p = self._one(self._req("POST", "/profiles", json_body={"id": uid, "email": (email or "").lower()},
                                    prefer="return=representation,resolution=ignore-duplicates"))
            p = p or self.get_profile(uid) or {"id": uid, "email": email, "role": "user", "status": "active",
                                               "approved": False}
        return p

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._req("GET", "/profiles", params={"select": "*", "order": "created_at.asc"}) or []

    def update_profile(self, uid: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        return self._one(self._req("PATCH", "/profiles", params={"id": f"eq.{uid}"}, json_body=fields,
                                   prefer="return=representation"))

    # -- runs ----------------------------------------------------------------------------
    def get_run(self, run_id: str, viewer: Optional[Viewer]) -> Optional[dict[str, Any]]:
        row = self._one(self._req("GET", "/runs", viewer=viewer, params={"id": f"eq.{run_id}", "select": "*"}))
        return row if visible(row, viewer) else None  # RLS + the same rule in code

    def list_runs(self, viewer: Viewer) -> list[dict[str, Any]]:
        rows = self._req("GET", "/runs", viewer=viewer, params={
            "select": "id,user_id,kind,engine,label,name,status,parent_run_id,is_preset,share_token,summary,created_at",
            "order": "created_at.desc", "limit": "500"}) or []
        return [r for r in rows if visible(r, viewer)]

    def upsert_run(self, row: dict[str, Any]) -> None:
        # created_at is kept when given (migrated runs keep their original time); updated_at is set by trigger
        body = {k: v for k, v in row.items() if k in RUN_FIELDS and k != "updated_at"}
        self._req("POST", "/runs", params={"on_conflict": "id"}, json_body=body,
                  prefer="resolution=merge-duplicates,return=minimal")

    def run_by_share_token(self, token: str) -> Optional[dict[str, Any]]:
        if not token:
            return None
        return self._one(self._req("GET", "/runs", params={"share_token": f"eq.{token}", "select": "*"}))

    def delete_run(self, run_id: str) -> None:
        self._req("DELETE", "/runs", params={"id": f"eq.{run_id}"})

    # -- jobs ----------------------------------------------------------------------------
    def create_job(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._one(self._req("POST", "/jobs", json_body=row, prefer="return=representation")) or row

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._one(self._req("GET", "/jobs", params={"id": f"eq.{job_id}", "select": "*"}))

    def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        self._req("PATCH", "/jobs", params={"id": f"eq.{job_id}"}, json_body=fields, prefer="return=minimal")

    def try_lease(self, job_id: str, holder: str, seconds: int) -> bool:
        now = utcnow()
        # single atomic UPDATE … WHERE lease is free/expired (or already ours)
        rows = self._req("PATCH", "/jobs", params={
            "id": f"eq.{job_id}",
            "or": f'(lease_until.is.null,lease_until.lt."{iso(now)}",lease_holder.eq.{holder})'},
            json_body={"lease_until": iso(now + dt.timedelta(seconds=seconds)), "lease_holder": holder},
            prefer="return=representation")
        return bool(rows)

    def renew_lease(self, job_id: str, holder: str, seconds: int) -> bool:
        rows = self._req("PATCH", "/jobs", params={"id": f"eq.{job_id}", "lease_holder": f"eq.{holder}"},
                         json_body={"lease_until": iso(utcnow() + dt.timedelta(seconds=seconds))},
                         prefer="return=representation")
        return bool(rows)

    def release_lease(self, job_id: str, holder: str) -> None:
        self._req("PATCH", "/jobs", params={"id": f"eq.{job_id}", "lease_holder": f"eq.{holder}"},
                  json_body={"lease_until": None, "lease_holder": None}, prefer="return=minimal")

    def count_jobs_since(self, uid: str, since: dt.datetime, kinds: Optional[Iterable[str]] = None) -> int:
        params = {"user_id": f"eq.{uid}", "created_at": f"gte.{iso(since)}", "select": "id", "limit": "1000"}
        if kinds:
            params["kind"] = "in.(" + ",".join(kinds) + ")"
        return len(self._req("GET", "/jobs", params=params) or [])

    # -- sessions ----------------------------------------------------------------------
    def save_session(self, row: dict[str, Any]) -> None:
        self._req("POST", "/plan_sessions", params={"on_conflict": "id"}, json_body=row,
                  prefer="resolution=merge-duplicates,return=minimal")

    def get_session(self, sid: str) -> Optional[dict[str, Any]]:
        return self._one(self._req("GET", "/plan_sessions", params={"id": f"eq.{sid}", "select": "*"}))

    # -- usage ----------------------------------------------------------------------------
    def add_usage(self, row: dict[str, Any]) -> None:
        self._req("POST", "/usage", json_body=row, prefer="return=minimal")

    def usage_since(self, uid: Optional[str], since: dt.datetime) -> list[dict[str, Any]]:
        params = {"created_at": f"gte.{iso(since)}", "select": "user_id,tokens,calls,cost_usd,mock,kind,created_at",
                  "limit": "10000"}
        if uid:
            params["user_id"] = f"eq.{uid}"
        return self._req("GET", "/usage", params=params) or []


# =========================================================================== singleton
_store: Optional[MetaStore] = None
_store_key: Optional[tuple] = None


def get_meta() -> MetaStore:
    global _store, _store_key
    s = get_settings()
    key = (s.use_supabase, str(s.data_dir), s.supabase_url)
    if _store is None or _store_key != key:
        _store = SupabaseMetaStore(s) if s.use_supabase else LocalMetaStore(s)
        _store_key = key
    return _store


def set_meta(store: Optional[MetaStore]) -> None:
    """Tests: inject a store (None resets to the configured one)."""
    global _store, _store_key
    _store = store
    _store_key = None if store is None else (get_settings().use_supabase, str(get_settings().data_dir),
                                             get_settings().supabase_url)
