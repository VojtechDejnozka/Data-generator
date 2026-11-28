"""Application settings loaded from environment / .env (repo root or backend/)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

# Repo-level .env first, backend/.env may override.
load_dotenv(REPO_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env", override=True)


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


DEV_USER_ID = "00000000-0000-4000-a000-00000000d0e0"  # owner of everything when AUTH_DISABLED=1


@dataclass
class Settings:
    gemini_api_key: str = ""
    strong_model: str = "gemini-4-carbon"
    strong_fallbacks: list[str] = field(default_factory=list)
    cheap_model: str = "gemini-3.5-flash-lite"
    cheap_fallbacks: list[str] = field(default_factory=list)
    llm_mock: bool = False
    llm_cache: bool = True
    max_concurrency: int = 4
    request_timeout_s: float = 120.0
    data_dir: Path = REPO_DIR / "runs"
    cache_dir: Path = REPO_DIR / ".cache" / "llm"
    # --- deployment
    vercel_env: str = ""          # production | preview | development (set by Vercel)
    app_env: str = ""             # APP_ENV=production marks a non-Vercel production deployment
    # --- Supabase / auth
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    supabase_jwt_secret: str = ""  # legacy HS256 (local CLI only); never honoured in production
    jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = "authenticated"
    auth_disabled: bool = False
    storage_bucket: str = "runs"
    workspace_dir: Path = REPO_DIR / "runs"
    workspace_cache_mb: int = 300
    # --- accounts / cost protection
    signup_mode: str = "email_allowlist"   # open | email_allowlist | invite_only
    signup_allowlist: list[str] = field(default_factory=list)
    quota_runs_per_day: int = 20
    quota_tokens_per_day: int = 2_000_000
    quota_count_mock: bool = False
    rate_limit_jobs_per_minute: int = 6
    # --- HTTP
    cors_origins: list[str] = field(default_factory=list)
    cors_origin_regex: str = ""
    max_request_bytes: int = 2_000_000
    # --- jobs
    job_autodrive: bool = True     # run stages in-process in the background (local); False on Vercel
    lease_seconds: int = 90

    @property
    def mock(self) -> bool:
        # Without a key we can only run in mock mode.
        return self.llm_mock or not self.gemini_api_key

    @property
    def is_production(self) -> bool:
        return self.vercel_env == "production" or self.app_env == "production"

    @property
    def on_vercel(self) -> bool:
        return bool(self.vercel_env)

    @property
    def use_supabase(self) -> bool:
        """Metadata in Supabase Postgres + datasets in Storage; otherwise local files (dev / tests)."""
        return bool(self.supabase_url)


class ConfigError(RuntimeError):
    pass


def assert_safe_config(s: "Settings") -> None:
    """Refuse to start with a dangerous configuration (called at app startup)."""
    if s.auth_disabled and (s.is_production or s.on_vercel):
        raise ConfigError("AUTH_DISABLED=1 is only allowed for local development (VERCEL_ENV/APP_ENV is set)")
    if s.is_production and not s.use_supabase:
        raise ConfigError("production requires SUPABASE_URL (local file storage is for development only)")
    if s.use_supabase and not s.supabase_secret_key:
        raise ConfigError("SUPABASE_SECRET_KEY is required when SUPABASE_URL is set")
    if s.use_supabase and not s.supabase_publishable_key:
        raise ConfigError("SUPABASE_PUBLISHABLE_KEY is required when SUPABASE_URL is set")
    if not s.auth_disabled and not (s.jwks_url or s.supabase_jwt_secret):
        raise ConfigError("configure SUPABASE_URL (JWKS) for authentication, or AUTH_DISABLED=1 for local dev")
    if s.signup_mode not in ("open", "email_allowlist", "invite_only"):
        raise ConfigError(f"invalid SIGNUP_MODE {s.signup_mode!r}")


def _resolve(p: str, default: Path) -> Path:
    if not p:
        return default
    path = Path(p)
    return path if path.is_absolute() else (BACKEND_DIR / path).resolve()


@lru_cache
def get_settings() -> Settings:
    s = Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        strong_model=os.getenv("STRONG_MODEL", "gemini-4-carbon").strip(),
        strong_fallbacks=_list(
            os.getenv("STRONG_FALLBACK_MODELS", "gemini-3.8-flash,gemini-3.5-flash,gemini-3.5-flash-lite")
        ),
        cheap_model=os.getenv("CHEAP_MODEL", "gemini-3.5-flash-lite").strip(),
        cheap_fallbacks=_list(os.getenv("CHEAP_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-3.6-flash")),
        llm_mock=_flag("LLM_MOCK"),
        llm_cache=_flag("LLM_CACHE", "1"),
        max_concurrency=int(os.getenv("LLM_MAX_CONCURRENCY", "4")),
        request_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "120")),
        data_dir=_resolve(os.getenv("DATA_DIR", ""), REPO_DIR / "runs"),
        cache_dir=_resolve(os.getenv("CACHE_DIR", ""), REPO_DIR / ".cache" / "llm"),
        vercel_env=os.getenv("VERCEL_ENV", "").strip(),
        app_env=os.getenv("APP_ENV", "").strip().lower(),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_publishable_key=(os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip(),
        supabase_secret_key=(os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
        supabase_jwt_secret=os.getenv("SUPABASE_JWT_SECRET", "").strip(),
        jwt_audience=os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated").strip(),
        auth_disabled=_flag("AUTH_DISABLED"),
        storage_bucket=os.getenv("SUPABASE_STORAGE_BUCKET", "runs").strip(),
        workspace_cache_mb=int(os.getenv("WORKSPACE_CACHE_MB", "300")),
        signup_mode=os.getenv("SIGNUP_MODE", "email_allowlist").strip().lower(),
        signup_allowlist=[x.lower() for x in _list(os.getenv("SIGNUP_ALLOWLIST"))],
        quota_runs_per_day=int(os.getenv("QUOTA_RUNS_PER_DAY", "20")),
        quota_tokens_per_day=int(os.getenv("QUOTA_TOKENS_PER_DAY", "2000000")),
        quota_count_mock=_flag("QUOTA_COUNT_MOCK"),
        rate_limit_jobs_per_minute=int(os.getenv("RATE_LIMIT_JOBS_PER_MINUTE", "6")),
        cors_origins=_list(os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")),
        cors_origin_regex=os.getenv("CORS_ORIGIN_REGEX", "").strip(),
        max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", "2000000")),
        lease_seconds=int(os.getenv("JOB_LEASE_SECONDS", "90")),
    )
    s.jwt_issuer = os.getenv("SUPABASE_JWT_ISSUER", "").strip() or (f"{s.supabase_url}/auth/v1" if s.supabase_url else "")
    s.jwks_url = os.getenv("SUPABASE_JWKS_URL", "").strip() or (
        f"{s.supabase_url}/auth/v1/.well-known/jwks.json" if s.supabase_url else "")
    s.job_autodrive = _flag("JOB_AUTODRIVE", "0" if s.on_vercel else "1")
    # Serverless file systems are ephemeral: with Supabase, run files are a cache of Storage.
    default_ws = (Path("/tmp/story2data/runs") if s.on_vercel else s.data_dir / "_workspace") if s.use_supabase \
        else s.data_dir
    s.workspace_dir = _resolve(os.getenv("WORKSPACE_DIR", ""), default_ws)
    if s.on_vercel and not os.getenv("CACHE_DIR"):
        s.cache_dir = Path("/tmp/story2data/llm-cache")
    if s.on_vercel and not os.getenv("DATA_DIR"):
        s.data_dir = Path("/tmp/story2data/data")
    for d in (s.data_dir, s.cache_dir, s.workspace_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
