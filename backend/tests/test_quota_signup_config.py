"""Cost protection (quotas, rate limit, usage), registration modes and refusal of unsafe configuration."""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline import SMALL

U = str(uuid.UUID("dddddddd-0000-4000-a000-000000000004"))


def wait(c, job_id, h, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/jobs/{job_id}", headers=h).json()
        if s["status"] in ("done", "error"):
            return s
        time.sleep(0.1)
    raise TimeoutError


@pytest.fixture
def client(auth_env):
    from app.main import app
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- quotas
def test_runs_per_day_quota(auth_env, client):
    auth_env.settings(QUOTA_RUNS_PER_DAY=1)
    h = auth_env.headers(U, "u@example.com")
    sid = wait(client, client.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"], h)["result"]["session_id"]
    spec = client.get(f"/api/plan/{sid}", headers=h).json()["response"]["spec"]
    s = wait(client, client.post("/api/runs", json={"spec": spec}, headers=h).json()["job_id"], h)
    assert s["status"] == "done"
    me = client.get("/api/me", headers=h).json()
    assert me["quota"]["runs_used"] == 1 and me["quota"]["runs_left"] == 0
    r = client.post("/api/runs", json={"spec": spec}, headers=h)
    assert r.status_code == 429
    assert "Denní limit 1 běhů" in r.json()["detail"]["message"] and r.json()["detail"]["quota"]["runs_left"] == 0
    # what-if counts as a run too
    assert client.post(f"/api/runs/{s['result']['run_id']}/whatif", json={"knob_overrides": {}}, headers=h).status_code == 429


def test_token_quota_and_usage_recorded(auth_env, client):
    from app.storage.meta import get_meta
    auth_env.settings(QUOTA_TOKENS_PER_DAY=10, QUOTA_COUNT_MOCK=1)
    h = auth_env.headers(U, "u@example.com")
    s = wait(client, client.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"], h)
    assert s["status"] == "done"
    rows = get_meta().usage_since(U, __import__("app.quota", fromlist=["x"]).day_start())
    assert rows and rows[0]["kind"] == "plan" and rows[0]["tokens"] > 10 and rows[0]["mock"] is True
    me = client.get("/api/me", headers=h).json()["quota"]
    assert me["tokens_left"] == 0
    r = client.post("/api/plan", json=SMALL.model_dump(), headers=h)
    assert r.status_code == 429 and "tokenů" in r.json()["detail"]["message"]
    assert client.post("/api/runs/x/commentary", headers=h).status_code == 404  # ownership first


def test_mock_tokens_not_counted_by_default(auth_env, client):
    auth_env.settings(QUOTA_TOKENS_PER_DAY=10)
    h = auth_env.headers(U, "u@example.com")
    wait(client, client.post("/api/plan", json=SMALL.model_dump(), headers=h).json()["job_id"], h)
    assert client.get("/api/me", headers=h).json()["quota"]["tokens_used"] == 0
    assert client.post("/api/plan", json=SMALL.model_dump(), headers=h).status_code == 200


def test_rate_limit(auth_env, client):
    auth_env.settings(RATE_LIMIT_JOBS_PER_MINUTE=2)
    h = auth_env.headers(U, "u@example.com")
    codes = [client.post("/api/plan", json=SMALL.model_dump(), headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    # other users are not affected
    assert client.post("/api/plan", json=SMALL.model_dump(), headers=auth_env.headers(str(uuid.uuid4()))).status_code == 200


def test_admin_exempt_from_daily_quota(auth_env, client):
    from app.auth import deps
    from app.storage.meta import get_meta
    auth_env.settings(QUOTA_TOKENS_PER_DAY=0)
    h = auth_env.headers(U, "boss@example.com")
    assert client.post("/api/plan", json=SMALL.model_dump(), headers=h).status_code == 429
    get_meta().update_profile(U, {"role": "admin"})
    deps._profiles.clear()
    assert client.post("/api/plan", json=SMALL.model_dump(), headers=h).status_code == 200
    assert client.get("/api/me", headers=h).json()["quota"]["unlimited"] is True


# --------------------------------------------------------------------------- registration modes
@pytest.mark.parametrize("mode,allow,email,ok", [
    ("open", "", "anyone@x.org", True),
    ("email_allowlist", "judge@fis.cz,@team.cz", "judge@fis.cz", True),
    ("email_allowlist", "judge@fis.cz,@team.cz", "Dev@Team.cz", True),
    ("email_allowlist", "judge@fis.cz,@team.cz", "stranger@gmail.com", False),
    ("email_allowlist", "@team.cz", "x@evilteam.cz", False),
    ("invite_only", "judge@fis.cz", "judge@fis.cz", False),
])
def test_signup_modes(auth_env, client, mode, allow, email, ok):
    auth_env.settings(SIGNUP_MODE=mode, SIGNUP_ALLOWLIST=allow)
    r = client.get("/api/runs", headers=auth_env.headers(str(uuid.uuid4()), email))
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 403 and "přístup" in r.json()["detail"]


def test_invite_only_admits_approved(auth_env, client):
    from app.auth import deps
    from app.storage.meta import get_meta
    auth_env.settings(SIGNUP_MODE="invite_only")
    h = auth_env.headers(U, "invited@x.org")
    assert client.get("/api/runs", headers=h).status_code == 403
    get_meta().update_profile(U, {"approved": True})  # admin approval / invitation
    deps._profiles.clear()
    assert client.get("/api/runs", headers=h).status_code == 200


def test_signup_mode_default_is_allowlist(monkeypatch):
    from app import config
    monkeypatch.delenv("SIGNUP_MODE", raising=False)
    config.get_settings.cache_clear()
    assert config.get_settings().signup_mode == "email_allowlist"


# --------------------------------------------------------------------------- configuration safety
@pytest.mark.parametrize("env", [{"VERCEL_ENV": "production"}, {"VERCEL_ENV": "preview"}, {"APP_ENV": "production"}])
def test_auth_disabled_refused_outside_local(monkeypatch, env):
    from app import config
    monkeypatch.setenv("AUTH_DISABLED", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    with pytest.raises(config.ConfigError, match="AUTH_DISABLED"):
        config.assert_safe_config(config.get_settings())


def test_app_refuses_to_start_with_bypass_in_production(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SUPABASE", "VERCEL", "APP_ENV"))}
    env.update(AUTH_DISABLED="1", VERCEL_ENV="production", DATA_DIR=str(tmp_path), CACHE_DIR=str(tmp_path / "c"),
               WORKSPACE_DIR=str(tmp_path / "w"))
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=Path(__file__).resolve().parents[1], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "AUTH_DISABLED=1 is only allowed" in r.stderr


@pytest.mark.parametrize("env,msg", [
    ({"APP_ENV": "production", "AUTH_DISABLED": "0", "SUPABASE_JWKS_URL": "https://x"}, "requires SUPABASE_URL"),
    ({"SUPABASE_URL": "https://p.supabase.co", "AUTH_DISABLED": "0", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_x"},
     "SUPABASE_SECRET_KEY"),
    ({"AUTH_DISABLED": "0"}, "configure SUPABASE_URL"),
    ({"SIGNUP_MODE": "everyone"}, "invalid SIGNUP_MODE"),
])
def test_other_unsafe_configs(monkeypatch, env, msg):
    from app import config
    for k in ("SUPABASE_JWKS_URL", "SUPABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    with pytest.raises(config.ConfigError, match=msg):
        config.assert_safe_config(config.get_settings())


def test_mock_mode_independent_of_auth(auth_env, client):
    """LLM_MOCK keeps working with auth on (and the Gemini key never reaches the client)."""
    h = auth_env.headers(U, "u@example.com")
    health = client.get("/api/health").json()
    assert health["mock"] is True and health["auth"] == "supabase"
    assert "key" not in str({k: v for k, v in health.items() if k != "has_key"}).lower()


def test_request_size_limit(auth_env, client):
    auth_env.settings(MAX_REQUEST_BYTES=1000)
    h = auth_env.headers(U, "u@example.com")
    r = client.post("/api/scm/import", json={"model": {"x": "y" * 5000}}, headers=h)
    assert r.status_code == 413
