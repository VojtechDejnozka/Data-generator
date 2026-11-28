"""Test configuration: always mock mode, isolated data/cache dirs."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Never load the developer's .env (it may hold the production Supabase URL + secret key, or a Gemini key).
os.environ["STORY2DATA_NO_DOTENV"] = "1"
# Existing tests run as the local dev user; auth tests switch AUTH_DISABLED off explicitly.
os.environ["AUTH_DISABLED"] = "1"
for _k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY",
           "SUPABASE_ANON_KEY", "SUPABASE_JWKS_URL", "SUPABASE_JWT_SECRET", "GEMINI_API_KEY", "VERCEL_ENV", "APP_ENV"):
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("JOB_AUTODRIVE", "1")
    monkeypatch.setenv("RATE_LIMIT_JOBS_PER_MINUTE", "1000")
    from app import config
    from app.llm import client
    from app.storage import blobs, meta

    config.get_settings.cache_clear()
    client.reset_llm()
    meta.set_meta(None)
    blobs.set_blobs(None, override=False)
    # hard guard: a test must never talk to a real Supabase project
    _s = config.get_settings()
    assert not _s.supabase_secret_key and not _s.gemini_api_key and \
        (not _s.supabase_url or _s.supabase_url == "https://p.supabase.co"), "tests must not use real credentials"
    from app.auth import deps
    deps._profiles.clear()
    deps.set_verifier(None)
    yield
    config.get_settings.cache_clear()
    client.reset_llm()
    meta.set_meta(None)
    blobs.set_blobs(None, override=False)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def retail_dataset(tmp_path_factory):
    """Dimensions + simulation once per session (mock master data)."""
    os.environ["LLM_MOCK"] = "1"
    os.environ["CACHE_DIR"] = str(tmp_path_factory.mktemp("cache"))
    from app import config
    from app.llm import client

    config.get_settings.cache_clear()
    client.reset_llm()
    from app.contracts.io import ScenarioInput
    from app.generation.scenarios.base import GenContext
    from app.generation.scenarios.registry import RETAIL

    inp = ScenarioInput(company="Test", environment="Česko", story="Testovací příběh",
                        entity_counts={"stores": 5, "products": 80, "customers": 300},
                        period_start="2025-01-01", period_end="2025-12-31", volume="small")
    spec = RETAIL.default_spec(inp)
    dims = asyncio.run(RETAIL.build_dimensions(spec, GenContext(seed=7, knobs={})))
    return spec, dims


class AuthEnv:
    """Auth switched ON with a local JWKS; ``token(user)`` returns a valid bearer header."""

    def __init__(self, monkeypatch):
        from tests.auth_helpers import ISS, JWKS_URL, KeyPair, claims

        from app import config
        from app.auth import deps
        from app.auth.jwt import JWTVerifier

        self.mp = monkeypatch
        self.key = KeyPair("test-key-1")
        self.jwks = {"keys": [self.key.jwk()]}
        self.fetches = 0
        monkeypatch.setenv("AUTH_DISABLED", "0")
        monkeypatch.setenv("SUPABASE_JWKS_URL", JWKS_URL)
        monkeypatch.setenv("SUPABASE_JWT_ISSUER", ISS)
        monkeypatch.setenv("SIGNUP_MODE", "open")
        config.get_settings.cache_clear()

        def fetch(url):
            assert url == JWKS_URL
            self.fetches += 1
            return self.jwks

        self.verifier = JWTVerifier(jwks_url=JWKS_URL, issuer=ISS, audience="authenticated", fetch=fetch)
        deps.set_verifier(self.verifier)
        deps._profiles.clear()
        self._claims = claims

    def token(self, uid: str, email: str = "", **over) -> str:
        return self.key.sign(self._claims(sub=uid, email=email or f"{uid[:4]}@example.com", **over))

    def headers(self, uid: str, email: str = "", **over) -> dict:
        return {"Authorization": f"Bearer {self.token(uid, email, **over)}"}

    def settings(self, **env):
        from app import config
        for k, v in env.items():
            self.mp.setenv(k, str(v))
        config.get_settings.cache_clear()
        from app.auth import deps
        deps._profiles.clear()


@pytest.fixture
def auth_env(monkeypatch):
    env = AuthEnv(monkeypatch)
    yield env
