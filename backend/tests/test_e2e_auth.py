"""End-to-end journey with auth on (mock LLM), driven exactly like the browser does it:
sign-up (allowlisted e-mail) -> sign-in -> plan -> approve spec -> staged generation via /advance (SSE)
-> report -> what-if -> compare -> share -> sign-out (no token => 401).

Supabase Auth itself (password hashing, e-mail confirmation, session refresh) is outside the backend; the
browser-level flow against a local `supabase start` is described in README (Ověření end-to-end).
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline import SMALL

USER = str(uuid.uuid4())


def advance_until_done(c, job_id, h):
    since = 0
    for _ in range(30):
        with c.stream("POST", f"/api/jobs/{job_id}/advance", params={"since": since}, headers=h) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            frames = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: ")]
        since = max([f["i"] + 1 for f in frames if "i" in f] + [since])
        if frames[-1]["final"]:
            assert frames[-1]["status"] == "done", frames[-1]["error"]
            return frames[-1]["result"]
    raise AssertionError("job did not finish")


@pytest.mark.parametrize("autodrive", ["0", "1"])
def test_full_journey(auth_env, monkeypatch, autodrive):
    from app import config
    monkeypatch.setenv("JOB_AUTODRIVE", autodrive)
    auth_env.settings(SIGNUP_MODE="email_allowlist", SIGNUP_ALLOWLIST="@porota.cz")
    config.get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        # "registration": Supabase issues a token; the first request creates the profile
        h = auth_env.headers(USER, "Ana@Porota.cz")
        me = c.get("/api/me", headers=h).json()
        assert me["email"] == "ana@porota.cz" and me["role"] == "user" and me["quota"]["runs_left"] == 20
        # a non-allowlisted account is turned away
        assert c.get("/api/me", headers=auth_env.headers(str(uuid.uuid4()), "x@gmail.com")).status_code == 403
        # new scenario -> plan
        sid = advance_until_done(c, c.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"], h)["session_id"]
        session = c.get(f"/api/plan/{sid}", headers=h).json()
        # approve spec -> generation
        jid = c.post("/api/runs", json={"spec": session["response"]["spec"], "input": session["input"]}, headers=h).json()["job_id"]
        rid = advance_until_done(c, jid, h)["run_id"]
        assert c.get("/api/runs", headers=h).json()[0]["run_id"] == rid
        rep = c.get(f"/api/runs/{rid}/report", headers=h).json()
        assert rep["verdict"]["verdict"] == "pass"
        assert c.get(f"/api/runs/{rid}/dashboard", headers=h).json()["widgets"]
        assert c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) FROM stores"}, headers=h).json()["rows"]
        # what-if + compare
        rid2 = advance_until_done(c, c.post(f"/api/runs/{rid}/whatif", json={"knob_overrides": {"delay_severity": "10"}},
                                            headers=h).json()["job_id"], h)["run_id"]
        assert c.get("/api/compare", params={"a": rid, "b": rid2}, headers=h).status_code == 200
        assert c.get("/api/me", headers=h).json()["quota"]["runs_used"] == 2
        # share with the jury
        tok = c.post(f"/api/runs/{rid}/share", json={"enabled": True}, headers=h).json()["share_token"]
        assert c.get(f"/api/share/{tok}/report").status_code == 200
        # sign-out: the browser drops the session; without a token nothing is visible
        assert c.get("/api/runs").status_code == 401
        assert c.get(f"/api/runs/{rid}/report").status_code == 401
