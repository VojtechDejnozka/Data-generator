"""Test helpers: a local ES256 signing key, its JWKS and Supabase-shaped access tokens."""
from __future__ import annotations

import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

ISS = "https://test-project.supabase.co/auth/v1"
JWKS_URL = "https://test-project.supabase.co/auth/v1/.well-known/jwks.json"


class KeyPair:
    def __init__(self, kid: str, alg: str = "ES256"):
        self.kid, self.alg = kid, alg
        self.private = ec.generate_private_key(ec.SECP256R1()) if alg == "ES256" else \
            rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        algo = jwt.algorithms.ECAlgorithm if self.alg == "ES256" else jwt.algorithms.RSAAlgorithm
        d = json.loads(algo.to_jwk(self.private.public_key()))
        d.update(kid=self.kid, alg=self.alg, use="sig")
        return d

    def sign(self, claims: dict, kid: str | None = None, headers: dict | None = None) -> str:
        return jwt.encode(claims, self.private, algorithm=self.alg,
                          headers={"kid": kid or self.kid, **(headers or {})})


def claims(sub: str | None = None, email: str = "a@example.com", **over) -> dict:
    now = int(time.time())
    c = {"iss": ISS, "aud": "authenticated", "sub": sub or str(uuid.uuid4()), "email": email, "role": "authenticated",
         "aal": "aal1", "session_id": str(uuid.uuid4()), "is_anonymous": False, "iat": now, "exp": now + 3600,
         "app_metadata": {"provider": "email"}, "user_metadata": {}}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}
