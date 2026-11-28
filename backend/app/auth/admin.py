"""Supabase Auth Admin API calls (secret key, backend only): invitations and bans."""
from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from ..config import get_settings

log = logging.getLogger("auth.admin")
BAN_FOREVER = "876000h"  # ~100 years


def _headers() -> dict[str, str]:
    key = get_settings().supabase_secret_key
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):
        h["Authorization"] = f"Bearer {key}"
    return h


def _url(path: str) -> str:
    return f"{get_settings().supabase_url}/auth/v1{path}"


def invite(email: str, redirect_to: str = "") -> dict:
    """Send a Supabase invitation e-mail; invited users get profiles.approved = true (trigger)."""
    params = {"redirect_to": redirect_to} if redirect_to else None
    try:
        r = httpx.post(_url("/invite"), json={"email": email}, params=params, headers=_headers(), timeout=15)
    except httpx.HTTPError as e:
        log.error("invite failed: %s", e)
        raise HTTPException(503, "Pozvánku se nepodařilo odeslat.")
    if r.status_code == 422:
        raise HTTPException(409, "Uživatel s tímto e-mailem už existuje – schvalte ho v přehledu uživatelů.")
    if r.status_code >= 400:
        log.error("invite -> %s %s", r.status_code, r.text[:300])
        raise HTTPException(502, "Pozvánku se nepodařilo odeslat.")
    return {"invited": email, "id": (r.json() or {}).get("id")}


def set_ban(uid: str, banned: bool) -> None:
    """Block at the Auth level too: banned users cannot refresh sessions (existing JWTs expire within jwt_expiry)."""
    try:
        r = httpx.put(_url(f"/admin/users/{uid}"), json={"ban_duration": BAN_FOREVER if banned else "none"},
                      headers=_headers(), timeout=15)
        if r.status_code >= 400:
            log.error("ban %s -> %s %s", uid, r.status_code, r.text[:300])
    except httpx.HTTPError as e:
        log.error("ban %s failed: %s", uid, e)
