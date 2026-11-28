"""Per-user cost protection for the (paid / rate-limited) LLM API key.

* daily quotas (UTC day): generation runs (QUOTA_RUNS_PER_DAY) and LLM tokens (QUOTA_TOKENS_PER_DAY);
* rate limit: at most RATE_LIMIT_JOBS_PER_MINUTE started jobs per user in any 60 s window – counted in the
  metadata store, so it holds across serverless instances;
* every LLM stage records its usage (calls, tokens, cost) in the ``usage`` table.

Admins are exempt from quotas (not from the rate limit). In mock mode tokens are not counted unless
QUOTA_COUNT_MOCK=1 (runs still are).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from fastapi import HTTPException

from .config import get_settings
from .storage.meta import get_meta, utcnow

log = logging.getLogger("quota")

GENERATION_KINDS = ("run", "whatif", "doc_run")
LLM_KINDS = ("plan", "replan", "run", "whatif", "doc_plan", "doc_replan", "doc_run", "detective", "observe",
             "difficulty", "commentary")


class QuotaExceeded(HTTPException):
    def __init__(self, message: str, status: dict[str, Any], code: int = 429):
        super().__init__(code, {"message": message, "quota": status})


def day_start(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def status(user_id: str, is_admin: bool = False) -> dict[str, Any]:
    s = get_settings()
    since = day_start()
    meta = get_meta()
    rows = meta.usage_since(user_id, since)
    tokens = sum(int(r.get("tokens") or 0) for r in rows if s.quota_count_mock or not r.get("mock"))
    runs = meta.count_jobs_since(user_id, since, GENERATION_KINDS)
    return {
        "unlimited": is_admin,
        "runs_used": runs, "runs_limit": s.quota_runs_per_day,
        "runs_left": None if is_admin else max(0, s.quota_runs_per_day - runs),
        "tokens_used": tokens, "tokens_limit": s.quota_tokens_per_day,
        "tokens_left": None if is_admin else max(0, s.quota_tokens_per_day - tokens),
        "resets_at": (since + dt.timedelta(days=1)).isoformat(),
        "rate_limit_per_minute": s.rate_limit_jobs_per_minute,
    }


def check_rate_limit(user_id: str) -> None:
    s = get_settings()
    if s.rate_limit_jobs_per_minute <= 0:
        return
    n = get_meta().count_jobs_since(user_id, utcnow() - dt.timedelta(seconds=60))
    if n >= s.rate_limit_jobs_per_minute:
        raise QuotaExceeded(f"Příliš mnoho požadavků – nejvýše {s.rate_limit_jobs_per_minute} úloh za minutu. "
                            "Zkuste to za chvíli znovu.", {"rate_limit_per_minute": s.rate_limit_jobs_per_minute})


def check_can_start(user_id: str, kind: str, is_admin: bool = False) -> None:
    """Raise 429 before creating a job that would exceed the rate limit or the daily quota."""
    check_rate_limit(user_id)
    if is_admin or kind not in LLM_KINDS:
        return
    st = status(user_id)
    if kind in GENERATION_KINDS and st["runs_left"] <= 0:
        raise QuotaExceeded(f"Denní limit {st['runs_limit']} běhů je vyčerpán. Limit se obnoví o půlnoci UTC.", st)
    if st["tokens_left"] <= 0:
        raise QuotaExceeded(f"Denní limit {st['tokens_limit']:,} tokenů je vyčerpán. Limit se obnoví o půlnoci UTC."
                            .replace(",", " "), st)


def check_tokens(user_id: str, is_admin: bool = False) -> None:
    """Between stages of a running job: stop when the token budget is used up."""
    if is_admin:
        return
    st = status(user_id)
    if st["tokens_left"] <= 0:
        raise QuotaExceeded("Denní limit tokenů byl během úlohy vyčerpán – úloha byla zastavena.", st)


def record(user_id: str, *, kind: str, summary: dict[str, Any], job_id: str = "", run_id: str = "",
           mock: bool = False) -> None:
    calls = int(summary.get("total_calls") or 0)
    if not calls:
        return
    try:
        get_meta().add_usage({"user_id": user_id, "job_id": job_id or None, "run_id": run_id or None, "kind": kind,
                              "calls": calls, "tokens": int(summary.get("total_tokens") or 0),
                              "cost_usd": round(float(summary.get("total_cost_usd") or 0.0), 6), "mock": bool(mock)})
    except Exception:  # noqa: BLE001 – accounting must not lose the user's result; log loudly
        log.exception("failed to record usage for user %s job %s", user_id, job_id)
