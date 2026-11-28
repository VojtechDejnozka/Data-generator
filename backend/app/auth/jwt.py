"""Verification of Supabase access tokens.

Supabase signs user access tokens with an asymmetric signing key (ES256 recommended, RS256 supported);
the public keys are published at ``<SUPABASE_URL>/auth/v1/.well-known/jwks.json``. We verify the signature
locally against that JWK set (cached, refreshed on an unknown ``kid``) and check ``exp``/``iat``/``aud``/``iss``.
The legacy shared-secret HS256 scheme is accepted only when ``SUPABASE_JWT_SECRET`` is configured and the
deployment is not production (older local ``supabase start`` setups).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx
import jwt

log = logging.getLogger("auth")

ASYMMETRIC_ALGS = ("ES256", "RS256")
JWKS_TTL_S = 600          # Supabase's edge caches the JWKS for 10 minutes
JWKS_MIN_REFRESH_S = 30   # unknown kid -> at most one forced refresh per 30 s
LEEWAY_S = 30


class AuthError(Exception):
    """Token missing/invalid. The message is safe to show to the client."""

    def __init__(self, message: str = "Neplatné nebo chybějící přihlášení", status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass
class _Cache:
    keys: dict[str, Any]
    fetched_at: float


def _http_fetch(url: str) -> dict[str, Any]:
    r = httpx.get(url, timeout=5.0)
    r.raise_for_status()
    return r.json()


class JWTVerifier:
    def __init__(self, *, jwks_url: str, issuer: str, audience: str, hs_secret: str = "",
                 allow_hs: bool = False, fetch: Optional[Callable[[str], dict[str, Any]]] = None):
        self.jwks_url, self.issuer, self.audience = jwks_url, issuer, audience
        self.hs_secret = hs_secret if allow_hs else ""
        self._fetch = fetch or _http_fetch
        self._cache: Optional[_Cache] = None
        self._lock = threading.Lock()
        self._last_forced = 0.0

    # -- JWKS -----------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        doc = self._fetch(self.jwks_url)
        keys: dict[str, Any] = {}
        for jwk in doc.get("keys", []):
            if jwk.get("kty") not in ("EC", "RSA"):
                continue  # never accept symmetric ("oct") keys from a JWKS
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk)
            except (jwt.PyJWKError, KeyError) as e:
                log.warning("skipping unusable JWK %s: %s", jwk.get("kid"), e)
        return keys

    def _key(self, kid: str):
        now = time.time()
        with self._lock:
            if self._cache is None or now - self._cache.fetched_at > JWKS_TTL_S:
                self._cache = _Cache(self._load(), now)
            key = self._cache.keys.get(kid)
            if key is None and now - self._last_forced > JWKS_MIN_REFRESH_S:
                self._last_forced = now  # key rotation: refresh once
                self._cache = _Cache(self._load(), now)
                key = self._cache.keys.get(kid)
        return key

    def purge(self) -> None:
        with self._lock:
            self._cache = None

    # -- verification -----------------------------------------------------------
    def verify(self, token: str) -> dict[str, Any]:
        if not token or token.count(".") != 2 or len(token) > 8192:
            raise AuthError()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError()
        alg = header.get("alg")
        if alg in ASYMMETRIC_ALGS:
            if not self.jwks_url:
                raise AuthError()
            try:
                key = self._key(str(header.get("kid") or ""))
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
                log.error("JWKS fetch failed: %s", e)
                raise AuthError("Ověření přihlášení je dočasně nedostupné", 503)
            if key is None:
                raise AuthError()
            verify_key, algs = key, [alg]
        elif alg == "HS256" and self.hs_secret:
            verify_key, algs = self.hs_secret, ["HS256"]
        else:
            raise AuthError()  # "none", unexpected or disallowed algorithm
        try:
            claims = jwt.decode(token, verify_key, algorithms=algs, audience=self.audience,
                                issuer=self.issuer or None, leeway=LEEWAY_S,
                                options={"require": ["exp", "iat", "sub", "aud"] + (["iss"] if self.issuer else [])})
        except jwt.ExpiredSignatureError:
            raise AuthError("Přihlášení vypršelo, přihlaste se znovu")
        except jwt.PyJWTError:
            raise AuthError()
        if claims.get("role") != "authenticated" or claims.get("is_anonymous"):
            raise AuthError("Vyžadováno přihlášení uživatelským účtem", 403)
        return claims
