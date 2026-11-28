"""Workspace <-> object storage sync, Supabase client request shapes, and resumable staged jobs.

``serverless`` simulates Vercel: no background driver (the client calls /advance per stage), files live in an
object store, and between stages the instance's /tmp and memory are wiped – every stage must recover its state
from storage + the jobs row.
"""
import gzip
import json
import shutil
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline import SMALL

A = str(uuid.UUID("aaaaaaaa-0000-4000-a000-00000000000a"))
B = str(uuid.UUID("bbbbbbbb-0000-4000-a000-00000000000b"))


# --------------------------------------------------------------------------- workspace sync
@pytest.fixture
def remote(tmp_path, monkeypatch):
    from app import config
    from app.storage import blobs

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    config.get_settings.cache_clear()
    store = blobs.DirBlobStore(tmp_path / "bucket")
    blobs.set_blobs(store)
    return store


def _mkrun(run_id: str, owner: str):
    from app.storage import runs as store
    from app.storage.meta import get_meta

    d = store.run_path(run_id)
    (d / "private").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"run_id": run_id, "status": "done"}))
    (d / "data.sqlite").write_bytes(b"SQLite format 3\x00" + b"x" * 5000)
    (d / "private" / "m.json").write_text("{}")
    get_meta().upsert_run({"id": run_id, "user_id": owner, "status": "done"})
    return d


def test_sync_up_and_ensure_local_roundtrip(remote, tmp_path):
    from app.storage import workspace
    from app.storage.meta import get_meta

    d = _mkrun("r1", A)
    idx = workspace.sync_up("r1", A)
    assert set(idx) == {"run.json", "data.sqlite", "private/m.json"} and idx["data.sqlite"]["gz"] is True
    obj = tmp_path / "bucket" / A / "r1" / "data.sqlite.gz"
    assert gzip.decompress(obj.read_bytes()).startswith(b"SQLite format 3")
    shutil.rmtree(d)  # new instance: empty /tmp
    workspace.ensure_local(get_meta().get_run("r1", None))
    assert (d / "data.sqlite").read_bytes().startswith(b"SQLite format 3") and (d / "private" / "m.json").exists()
    # change + delete propagate
    (d / "run.json").write_text(json.dumps({"run_id": "r1", "status": "error"}))
    (d / "private" / "m.json").unlink()
    idx2 = workspace.sync_up("r1", A)
    assert "private/m.json" not in idx2 and not (tmp_path / "bucket" / A / "r1" / "private" / "m.json").exists()
    assert idx2["run.json"]["sha"] != idx["run.json"]["sha"]


def test_sync_refuses_foreign_owner(remote):
    from app.storage import workspace

    _mkrun("r2", A)
    with pytest.raises(PermissionError):
        workspace.sync_up("r2", B)  # e.g. a preset viewer must never write (or re-own) the run


def test_eviction_keeps_dirty_and_pinned(remote, monkeypatch):
    from app import config
    from app.storage import workspace

    monkeypatch.setenv("WORKSPACE_CACHE_MB", "0")
    config.get_settings.cache_clear()
    d1 = _mkrun("old1", A)
    workspace.sync_up("old1", A)
    d2 = _mkrun("dirty", A)  # never synced
    import os
    os.utime(d1 / ".sync.json", (time.time() - 100, time.time() - 100))
    workspace.evict()
    assert not d1.exists() and d2.exists()


# --------------------------------------------------------------------------- Supabase clients (request shapes)
def _settings(**kw):
    from app.config import Settings
    s = Settings()
    s.supabase_url = "https://p.supabase.co"
    s.supabase_publishable_key = "sb_publishable_pub"
    s.supabase_secret_key = kw.get("secret", "sb_secret_sec")
    return s


def test_postgrest_reads_with_user_token_writes_with_secret():
    from app.storage.meta import SupabaseMetaStore, Viewer

    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": "r", "user_id": "u1", "is_preset": False}])
        return httpx.Response(201, json=[{"id": "j"}])

    m = SupabaseMetaStore(_settings(), transport=httpx.MockTransport(handler))
    assert m.get_run("r", Viewer(id="u1", token="USER.JWT.TOKEN"))["id"] == "r"
    assert m.get_run("r", Viewer(id="u2", token="OTHER.JWT.TOKEN")) is None  # code check on top of RLS
    r0 = seen[0]
    assert r0.headers["apikey"] == "sb_publishable_pub" and r0.headers["authorization"] == "Bearer USER.JWT.TOKEN"
    assert r0.url.params["id"] == "eq.r"
    m.upsert_run({"id": "r", "user_id": "u1", "summary": {}})
    w = seen[-1]
    assert w.headers["apikey"] == "sb_secret_sec" and "authorization" not in w.headers
    assert "merge-duplicates" in w.headers["prefer"]
    assert m.try_lease("j", "h1", 90) is True
    lease = seen[-1]
    assert lease.method == "PATCH" and "lease_until.is.null" in lease.url.params["or"]


def test_legacy_service_role_key_sent_as_bearer():
    from app.storage.meta import SupabaseMetaStore

    seen = []
    m = SupabaseMetaStore(_settings(secret="eyJ.legacy.jwt"),
                          transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=[])))
    m.get_profile("u")
    assert seen[0].headers["authorization"] == "Bearer eyJ.legacy.jwt"


def test_storage_signed_url_and_upload():
    from app.storage.blobs import BlobError, SupabaseBlobStore

    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if "/object/sign/" in req.url.path:
            return httpx.Response(200, json={"signedURL": "/object/sign/runs/u/r/data.sqlite.gz?token=abc"})
        return httpx.Response(200, json={})

    b = SupabaseBlobStore(_settings(), transport=httpx.MockTransport(handler))
    url = b.signed_url("u/r/data.sqlite.gz", 60, "x.sqlite.gz")
    assert url == "https://p.supabase.co/storage/v1/object/sign/runs/u/r/data.sqlite.gz?token=abc&download=x.sqlite.gz"
    assert json.loads(seen[0].content) == {"expiresIn": 60}
    with pytest.raises(BlobError):
        b.signed_url("../etc/passwd", 60)
    with pytest.raises(BlobError):
        b.signed_url("u/../r", 60)


# --------------------------------------------------------------------------- resumable staged jobs
@pytest.fixture
def serverless(auth_env, remote, monkeypatch, tmp_path):
    from app import config
    monkeypatch.setenv("JOB_AUTODRIVE", "0")
    config.get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c, tmp_path / "ws"


def _new_instance(ws):
    """Forget everything a serverless instance keeps between requests."""
    from app import pipeline
    from app.jobs import jobs
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir()
    pipeline._LIVE.clear()
    jobs.live.clear()


def _drive(c, job_id, h, ws, max_calls=20):
    stages, since = [], 0
    for _ in range(max_calls):
        with c.stream("POST", f"/api/jobs/{job_id}/advance", params={"since": since}, headers=h) as r:
            assert r.status_code == 200
            frames = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        tail = frames[-1]
        since = max([f["i"] + 1 for f in frames if "i" in f] + [since])
        stages.append(tail["stage"])
        if tail["final"]:
            assert tail["status"] == "done", tail["error"]
            return tail["result"], stages
        _new_instance(ws)
    raise AssertionError("job did not finish")


def test_classic_run_resumes_across_instances(serverless, auth_env):
    c, ws = serverless
    h = auth_env.headers(A, "a@example.com")
    jid = c.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"]
    assert c.get(f"/api/jobs/{jid}", headers=h).json()["status"] == "queued"  # nothing runs without advance
    sid = _drive(c, jid, h, ws)[0]["session_id"]
    spec = c.get(f"/api/plan/{sid}", headers=h).json()["response"]["spec"]
    res, stages = _drive(c, c.post("/api/runs", json={"spec": spec}, headers=h).json()["job_id"], h, ws)
    assert stages[:3] == ["generate", "evaluate", "repair"] and stages[-1] == ""
    rid = res["run_id"]
    _new_instance(ws)
    rep = c.get(f"/api/runs/{rid}/report", headers=h).json()  # downloaded again from storage
    assert rep["verdict"]["verdict"] == "pass"
    assert c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) FROM suppliers"}, headers=h).json()["rows"] == [[6]]
    run = c.get(f"/api/runs/{rid}", headers=h).json()
    assert run["usage"]["total_calls"] > 0 and run["status"] == "done"
    from app.storage.meta import get_meta
    files = get_meta().get_run(rid, None)["files"]
    assert "data.sqlite" in files and not any(k.startswith("private/_gen") for k in files)  # scratch removed
    # what-if from a fresh instance, then compare
    _new_instance(ws)
    rid2 = _drive(c, c.post(f"/api/runs/{rid}/whatif", json={"knob_overrides": {"delay_severity": "10"}},
                            headers=h).json()["job_id"], h, ws)[0]["run_id"]
    _new_instance(ws)
    assert c.get("/api/compare", params={"a": rid, "b": rid2}, headers=h).json()["changed_knobs"]["delay_severity"]["b"] == 10.0
    d = c.post(f"/api/runs/{rid}/download", json={"what": "db"}, headers=h).json()
    assert d["filename"].endswith(".sqlite.gz") and d["url"].startswith("file://")


def test_stage_rerun_after_crash(serverless, auth_env):
    """A stage whose instance died (lease expired, nothing persisted) is executed again."""
    from app.storage.meta import get_meta, iso, utcnow
    import datetime as dt

    c, ws = serverless
    h = auth_env.headers(A, "a@example.com")
    jid = c.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"]
    get_meta().update_job(jid, {"lease_until": iso(utcnow() + dt.timedelta(seconds=60)), "lease_holder": "dead"})
    with c.stream("POST", f"/api/jobs/{jid}/advance", headers=h) as r:  # follows the (dead) holder, then gives up
        tail = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: ")][-1]
    assert tail["final"] is False and tail["continue"] is True
    get_meta().update_job(jid, {"lease_until": iso(utcnow() - dt.timedelta(seconds=1))})  # lease expired
    res, _ = _drive(c, jid, h, ws)
    assert res["session_id"]


def test_cancel_between_stages(serverless, auth_env):
    c, ws = serverless
    h = auth_env.headers(A, "a@example.com")
    sid = _drive(c, c.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"], h, ws)[0]["session_id"]
    spec = c.get(f"/api/plan/{sid}", headers=h).json()["response"]["spec"]
    jid = c.post("/api/runs", json={"spec": spec}, headers=h).json()["job_id"]
    with c.stream("POST", f"/api/jobs/{jid}/advance", headers=h) as r:
        list(r.iter_lines())
    c.post(f"/api/jobs/{jid}/cancel", headers=h)
    with c.stream("POST", f"/api/jobs/{jid}/advance", headers=h) as r:
        tail = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: ")][-1]
    assert tail["final"] and tail["status"] == "error" and "zrušena" in tail["error"]
