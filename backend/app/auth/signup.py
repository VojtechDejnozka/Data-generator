"""Registration modes (SIGNUP_MODE) – who may use the app after signing up with Supabase Auth.

Supabase Auth itself accepts the sign-up (unless sign-ups are disabled in the dashboard); this check is
enforced by the backend on every request, so an account outside the allowlist can never start an LLM job.

* ``open``            – every confirmed account;
* ``email_allowlist`` – e-mails / ``@domain`` entries from SIGNUP_ALLOWLIST, or accounts approved by an admin;
* ``invite_only``     – only invited (``auth.users.invited_at``) or admin-approved accounts.
"""
from __future__ import annotations

from typing import Any

from ..config import Settings


def email_on_allowlist(email: str, allowlist: list[str]) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    domain = "@" + email.rsplit("@", 1)[1]
    return any(entry == email or (entry.startswith("@") and entry == domain) for entry in allowlist)


def signup_allowed(email: str, profile: dict[str, Any], s: Settings) -> bool:
    if profile.get("approved"):
        return True
    if s.signup_mode == "open":
        return True
    if s.signup_mode == "email_allowlist":
        return email_on_allowlist(email, s.signup_allowlist)
    return False  # invite_only
