"""Backend isolation between users: authentication on every route, ownership on every run-scoped endpoint."""
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline import SMALL

A = str(uuid.UUID("aaaaaaaa-0000-4000-a000-000000000001"))
B = str(uuid.UUID("bbbbbbbb-0000-4000-a000-000000000002"))
ADMIN = str(uuid.UUID("cccccccc-0000-4000-a000-000000000003"))
PUBLIC = ("/api/health", "/api/share/", "/api/download/")


def wait(c, job_id, h, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/jobs/{job_id}", headers=h).json()
        if s["status"] in ("done", "error"):
            assert s["status"] == "done", s["error"]
            return s["result"]
        time.sleep(0.1)
    raise TimeoutError


@pytest.fixture
def world(auth_env):
    """User A owns a finished run; B is another user; ADMIN can mark presets."""
    from app.main import app
    from app.storage.meta import get_meta

    c = TestClient(app)
    c.__enter__()  # one event loop for the whole test (background job drivers)
    ha, hb, hadm = auth_env.headers(A, "a@example.com"), auth_env.headers(B, "b@example.com"), \
        auth_env.headers(ADMIN, "admin@example.com")
    assert c.get("/api/me", headers=hadm).status_code == 200
    get_meta().update_profile(ADMIN, {"role": "admin"})
    from app.auth import deps
    deps._profiles.clear()
    sid = wait(c, c.post("/api/plan", json=SMALL.model_dump(), headers=ha).json()["job_id"], ha)["session_id"]
    spec = c.get(f"/api/plan/{sid}", headers=ha).json()["response"]["spec"]
    rid = wait(c, c.post("/api/runs", json={"spec": spec}, headers=ha).json()["job_id"], ha)["run_id"]
    yield {"c": c, "ha": ha, "hb": hb, "hadm": hadm, "rid": rid, "sid": sid, "spec": spec}
    c.__exit__(None, None, None)


def _concrete(path: str, rid: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda m: rid if m.group(0) == "{run_id}" else "x" * 40, path)


def test_every_non_public_route_requires_auth(auth_env):
    from app.main import app

    c = TestClient(app)
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path.startswith(PUBLIC):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = c.request(method, _concrete(path, "20260101-000000-abcdef12"), json={})
            assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
            checked += 1
    assert checked > 50


def test_garbage_and_foreign_tokens_rejected(auth_env):
    from app.main import app
    from tests.auth_helpers import KeyPair, claims

    c = TestClient(app)
    assert c.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/runs", headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    evil = KeyPair("test-key-1").sign(claims(sub=A))  # same kid, attacker's key
    assert c.get("/api/runs", headers={"Authorization": f"Bearer {evil}"}).status_code == 401
    assert c.get("/api/health").status_code == 200


def test_user_b_cannot_see_or_touch_user_a_run(world):
    c, ha, hb, rid, sid = world["c"], world["ha"], world["hb"], world["rid"], world["sid"]
    assert any(r["run_id"] == rid for r in c.get("/api/runs", headers=ha).json())
    assert not any(r["run_id"] == rid for r in c.get("/api/runs", headers=hb).json())
    for path in ("", "/report", "/dashboard", "/ground-truth", "/schema", "/llm-calls", "/tables/products",
                 "/detective", "/doc-sources", "/export.zip", "/db"):
        assert c.get(f"/api/runs/{rid}{path}", headers=hb).status_code == 404, path
    for path, body in (("/query", {"sql": "SELECT 1"}), ("/whatif", {"knob_overrides": {}}),
                       ("/detective", None), ("/commentary", None), ("/share", {"enabled": True}),
                       ("/download", {"what": "db"}), ("/download", {"what": "zip"})):
        assert c.post(f"/api/runs/{rid}{path}", json=body, headers=hb).status_code == 404, path
    assert c.delete(f"/api/runs/{rid}", headers=hb).status_code == 404
    assert c.get("/api/compare", params={"a": rid, "b": rid}, headers=hb).status_code == 404
    assert c.get(f"/api/plan/{sid}", headers=hb).status_code == 404
    assert c.post(f"/api/plan/{sid}/replan", json={"spec": world["spec"]}, headers=hb).status_code == 404
    # B cannot use A's run as a parent or a document source either
    assert c.post("/api/runs", json={"spec": world["spec"], "parent_run_id": rid}, headers=hb).status_code == 404
    assert c.post("/api/docs/plan", json={"description": "Faktury z dat běhu", "source_run_id": rid,
                                          "source_table": "orders"}, headers=hb).status_code == 404


def test_jobs_are_private(world):
    c, ha, hb = world["c"], world["ha"], world["hb"]
    jid = c.post("/api/plan", json=SMALL.model_dump(), headers=ha).json()["job_id"]
    assert c.get(f"/api/jobs/{jid}", headers=hb).status_code == 404
    assert c.post(f"/api/jobs/{jid}/cancel", headers=hb).status_code == 404
    assert c.post(f"/api/jobs/{jid}/advance", headers=hb).status_code == 404
    wait(c, jid, ha)


def test_owner_has_full_access_and_ids_from_token_only(world):
    c, ha, rid = world["c"], world["ha"], world["rid"]
    assert c.get(f"/api/runs/{rid}", headers=ha).json()["own"] is True
    q = c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) FROM suppliers"}, headers=ha).json()
    assert q["rows"] == [[6]]
    # a user_id smuggled into the body is ignored; the run belongs to the token's subject
    jid = c.post("/api/runs", json={"spec": world["spec"], "user_id": world["rid"]}, headers=ha)
    assert jid.status_code == 200
    rid2 = wait(c, jid.json()["job_id"], ha)["run_id"]
    from app.storage.meta import get_meta
    assert get_meta().get_run(rid2, None)["user_id"] == A


def test_sql_cannot_reach_other_runs(world):
    """The SQL endpoint opens only the requested run's database, read-only (ATTACH is blocked)."""
    c, ha, rid = world["c"], world["ha"], world["rid"]
    r = c.post(f"/api/runs/{rid}/query", json={"sql": "ATTACH DATABASE '/tmp/x.sqlite' AS x"}, headers=ha)
    assert r.status_code == 400


def test_preset_readable_by_all_but_read_only(world):
    c, ha, hb, hadm, rid = world["c"], world["ha"], world["hb"], world["hadm"], world["rid"]
    assert c.post(f"/api/admin/runs/{rid}/preset", json={"is_preset": True}, headers=hb).status_code == 403
    assert c.post(f"/api/admin/runs/{rid}/preset", json={"is_preset": True}, headers=hadm).status_code == 200
    runs_b = c.get("/api/runs", headers=hb).json()
    item = next(r for r in runs_b if r["run_id"] == rid)
    assert item["is_preset"] and item["own"] is False
    assert c.get(f"/api/runs/{rid}/report", headers=hb).status_code == 200
    assert c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT 1 AS x"}, headers=hb).status_code == 200
    assert c.post(f"/api/runs/{rid}/detective", headers=hb).status_code == 403
    assert c.post(f"/api/runs/{rid}/share", json={"enabled": True}, headers=hb).status_code == 403
    assert c.delete(f"/api/runs/{rid}", headers=hb).status_code == 403
    # B may branch a what-if off the preset; the result is B's own run
    w = c.post(f"/api/runs/{rid}/whatif", json={"knob_overrides": {"delay_severity": "10"}}, headers=hb)
    rid_b = wait(c, w.json()["job_id"], hb)["run_id"]
    assert c.get(f"/api/runs/{rid_b}", headers=hb).json()["own"] is True
    assert c.get(f"/api/runs/{rid_b}", headers=ha).status_code == 404


def test_share_link_read_only(world):
    c, ha, hb, rid = world["c"], world["ha"], world["hb"], world["rid"]
    tok = c.post(f"/api/runs/{rid}/share", json={"enabled": True}, headers=ha).json()["share_token"]
    assert len(tok) >= 32
    s = c.get(f"/api/share/{tok}")  # no auth header at all
    assert s.status_code == 200 and s.json()["run_id"] == rid and "input" not in s.json()
    assert c.get(f"/api/share/{tok}/report").status_code == 200
    assert c.get(f"/api/share/{tok}/tables/products", params={"page_size": 3}).json()["rows"]
    assert c.post(f"/api/share/{tok}/query", json={"sql": "SELECT COUNT(*) FROM suppliers"}).json()["rows"] == [[6]]
    assert c.post(f"/api/share/{tok}/query", json={"sql": "DELETE FROM suppliers"}).status_code == 400
    assert c.get("/api/share/" + "x" * 43).status_code == 404
    # the share link does not grant API access to the run
    assert c.get(f"/api/runs/{rid}", headers=hb).status_code == 404
    c.post(f"/api/runs/{rid}/share", json={"enabled": False}, headers=ha)
    assert c.get(f"/api/share/{tok}").status_code == 404


def test_download_tickets_single_use(world):
    c, ha, rid = world["c"], world["ha"], world["rid"]
    url = c.post(f"/api/runs/{rid}/download", json={"what": "db"}, headers=ha).json()["url"]
    assert url.startswith("/api/download/") and "Bearer" not in url
    r = c.get(url)
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
    assert c.get(url).status_code == 404  # single use
    z = c.post(f"/api/runs/{rid}/download", json={"what": "zip"}, headers=ha).json()["url"]
    assert c.get(z).content[:2] == b"PK"


def test_blocked_user_rejected(world, auth_env):
    from app.storage.meta import get_meta
    c, hb, hadm = world["c"], world["hb"], world["hadm"]
    assert c.get("/api/me", headers=hb).status_code == 200  # profile created on first request
    users = c.get("/api/admin/users", headers=hadm).json()["users"]
    assert {A, B, ADMIN} <= {u["id"] for u in users}
    assert c.get("/api/admin/users", headers=hb).status_code == 403
    assert c.patch(f"/api/admin/users/{B}", json={"status": "blocked"}, headers=hadm).status_code == 200
    r = c.get("/api/runs", headers=hb)
    assert r.status_code == 403 and "zablokovan" in r.json()["detail"]
    assert get_meta().get_profile(B)["status"] == "blocked"
    assert c.patch(f"/api/admin/users/{ADMIN}", json={"status": "blocked"}, headers=hadm).status_code == 400
