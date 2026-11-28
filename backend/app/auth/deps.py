"""Request authentication: ASGI middleware + FastAPI dependencies.

Every ``/api/*`` path requires a valid Supabase access token except the explicit public allowlist
(health check, read-only share links, one-time download tickets). Being a middleware, protection is the
default for every new endpoint. The verified principal is stored on ``request.state.principal`` and in a
context variable for code that is not request-aware (storage ownership, usage accounting).
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..config import DEV_USER_ID, get_settings
from ..storage.meta import MetaStoreError, Viewer, get_meta
from .jwt import AuthError, JWTVerifier
from .signup import signup_allowed

log = logging.getLogger("auth")

PUBLIC_PREFIXES = ("/api/health", "/api/share/", "/api/download/")
PROFILE_TTL_S = 30.0


@dataclass(frozen=True)
class Principal:
    id: str
    email: str = ""
    role: str = "user"
    token: str = field(default="", repr=False)
    claims: dict[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def viewer(self) -> Viewer:
        return Viewer(id=self.id, token=self.token, is_admin=self.is_admin)


DEV_PRINCIPAL = Principal(id=DEV_USER_ID, email="dev@localhost", role="admin")

_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar("principal", default=None)


def current_principal() -> Optional[Principal]:
    return _principal.get()


def set_principal(p: Optional[Principal]):
    return _principal.set(p)


# --------------------------------------------------------------------------- verifier / profile cache
_verifier: Optional[JWTVerifier] = None
_verifier_key: Optional[tuple] = None
_profiles: dict[str, tuple[float, dict[str, Any]]] = {}
_plock = threading.Lock()


def get_verifier() -> JWTVerifier:
    global _verifier, _verifier_key
    s = get_settings()
    key = (s.jwks_url, s.jwt_issuer, s.jwt_audience, s.supabase_jwt_secret, s.is_production)
    if _verifier is None or _verifier_key != key:
        _verifier = JWTVerifier(jwks_url=s.jwks_url, issuer=s.jwt_issuer, audience=s.jwt_audience,
                                hs_secret=s.supabase_jwt_secret, allow_hs=not s.is_production)
        _verifier_key = key
    return _verifier


def set_verifier(v: Optional[JWTVerifier]) -> None:
    """Tests: inject a verifier with a local JWKS."""
    global _verifier, _verifier_key
    s = get_settings()
    _verifier = v
    _verifier_key = None if v is None else (s.jwks_url, s.jwt_issuer, s.jwt_audience, s.supabase_jwt_secret,
                                            s.is_production)


def invalidate_profile(uid: str) -> None:
    with _plock:
        _profiles.pop(uid, None)


def _profile(uid: str, email: str) -> dict[str, Any]:
    now = time.time()
    with _plock:
        hit = _profiles.get(uid)
        if hit and now - hit[0] < PROFILE_TTL_S:
            return hit[1]
    p = get_meta().ensure_profile(uid, email)
    with _plock:
        _profiles[uid] = (now, p)
    return p


def authenticate(authorization: str) -> Principal:
    """Verify the bearer token and the account state. Raises AuthError."""
    s = get_settings()
    if s.auth_disabled:
        get_meta().ensure_profile(DEV_USER_ID, DEV_PRINCIPAL.email)
        return DEV_PRINCIPAL
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError()
    token = token.strip()
    claims = get_verifier().verify(token)
    uid, email = str(claims["sub"]), str(claims.get("email") or "").lower()
    try:
        profile = _profile(uid, email)
    except MetaStoreError:
        raise AuthError("Účty jsou dočasně nedostupné", 503)
    if profile.get("status") == "blocked":
        raise AuthError("Účet je zablokovaný. Kontaktujte správce.", 403)
    role = profile.get("role") or "user"
    if role != "admin" and not signup_allowed(email, profile, s):
        raise AuthError("Tento účet zatím nemá přístup. Požádejte správce o pozvánku nebo schválení.", 403)
    return Principal(id=uid, email=email, role=role, token=token, claims=claims)


class AuthMiddleware:
    """Pure ASGI middleware (keeps context variables visible to endpoints and streaming bodies)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/api/") or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            principal = await run_in_threadpool(authenticate, headers.get("authorization", ""))
        except AuthError as e:
            body = json.dumps({"detail": str(e), "code": "auth"}, ensure_ascii=False).encode()
            extra = [(b"www-authenticate", b'Bearer realm="story2data"')] if e.status == 401 else []
            await send({"type": "http.response.start", "status": e.status,
                        "headers": [(b"content-type", b"application/json"), *extra]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["principal"] = principal
        tok = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(tok)


def current_user(request: Request) -> Principal:
    """FastAPI dependency – the principal verified by AuthMiddleware."""
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "Neplatné nebo chybějící přihlášení")
    return p


def require_admin(request: Request) -> Principal:
    p = current_user(request)
    if not p.is_admin:
        raise HTTPException(403, "Jen pro správce")
    return p
