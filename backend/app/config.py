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

    @property
    def mock(self) -> bool:
        # Without a key we can only run in mock mode.
        return self.llm_mock or not self.gemini_api_key


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
    )
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    return s
