"""Supabase access-token verification (JWKS, ES256/RS256, exp/aud/iss, algorithm confusion)."""
import time

import jwt
import pytest

from app.auth.jwt import AuthError, JWTVerifier
from tests.auth_helpers import ISS, JWKS_URL, KeyPair, claims


def make(keys, **kw):
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return {"keys": [k.jwk() for k in keys]}

    v = JWTVerifier(jwks_url=JWKS_URL, issuer=ISS, audience="authenticated", fetch=fetch, **kw)
    return v, calls


def test_valid_es256_token():
    k = KeyPair("k1")
    v, _ = make([k])
    c = v.verify(k.sign(claims(sub="u1")))
    assert c["sub"] == "u1" and c["role"] == "authenticated"


def test_valid_rs256_token():
    k = KeyPair("rsa1", "RS256")
    v, _ = make([k])
    assert v.verify(k.sign(claims(sub="u2")))["sub"] == "u2"


def test_expired_token():
    k = KeyPair("k1")
    v, _ = make([k])
    with pytest.raises(AuthError) as e:
        v.verify(k.sign(claims(exp=int(time.time()) - 120, iat=int(time.time()) - 4000)))
    assert "vypršelo" in str(e.value) and e.value.status == 401


def test_foreign_signature_same_kid():
    good, evil = KeyPair("k1"), KeyPair("k1")
    v, _ = make([good])
    with pytest.raises(AuthError):
        v.verify(evil.sign(claims()))


@pytest.mark.parametrize("over", [{"aud": "anon"}, {"aud": "someone-else"}, {"iss": "https://evil.supabase.co/auth/v1"},
                                  {"exp": None}, {"iat": None}, {"drop": "sub"}])
def test_wrong_or_missing_claims(over):
    k = KeyPair("k1")
    v, _ = make([k])
    drop = over.pop("drop", None)
    c = claims(**over)
    c.pop(drop, None)
    with pytest.raises(AuthError):
        v.verify(k.sign(c))


def test_anonymous_and_service_tokens_rejected():
    k = KeyPair("k1")
    v, _ = make([k])
    with pytest.raises(AuthError) as e:
        v.verify(k.sign(claims(is_anonymous=True)))
    assert e.value.status == 403
    with pytest.raises(AuthError):
        v.verify(k.sign(claims(role="service_role")))


def test_alg_none_and_hs256_confusion_rejected():
    k = KeyPair("k1")
    v, _ = make([k])
    unsigned = jwt.encode(claims(), key=None, algorithm="none")
    with pytest.raises(AuthError):
        v.verify(unsigned)
    # HS256 signed with the *public* key material (classic confusion attack) or any secret: not configured
    hs = jwt.encode(claims(), "x" * 32, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(AuthError):
        v.verify(hs)


def test_legacy_hs256_only_when_configured():
    secret = "super-secret-jwt-token-with-at-least-32-characters-long"
    tok = jwt.encode(claims(sub="legacy"), secret, algorithm="HS256")
    v, _ = make([], hs_secret=secret, allow_hs=True)
    assert v.verify(tok)["sub"] == "legacy"
    v_prod, _ = make([], hs_secret=secret, allow_hs=False)  # production: never
    with pytest.raises(AuthError):
        v_prod.verify(tok)


def test_jwks_cached_and_refreshed_on_rotation():
    old, new = KeyPair("old"), KeyPair("new")
    keys = [old]
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return {"keys": [k.jwk() for k in keys]}

    v = JWTVerifier(jwks_url=JWKS_URL, issuer=ISS, audience="authenticated", fetch=fetch)
    for _ in range(5):
        v.verify(old.sign(claims()))
    assert calls["n"] == 1  # cached
    keys.append(new)  # rotation: new kid appears in JWKS
    assert v.verify(new.sign(claims(sub="rot")))["sub"] == "rot"
    assert calls["n"] == 2
    with pytest.raises(AuthError):  # unknown kid again within 30 s: no refetch storm
        v.verify(KeyPair("zzz").sign(claims()))
    assert calls["n"] == 2


def test_symmetric_keys_in_jwks_ignored():
    k = KeyPair("k1")
    v = JWTVerifier(jwks_url=JWKS_URL, issuer=ISS, audience="authenticated",
                    fetch=lambda u: {"keys": [{"kty": "oct", "kid": "k1", "k": "c2VjcmV0" * 8, "alg": "HS256"}]})
    with pytest.raises(AuthError):
        v.verify(jwt.encode(claims(), "secret" * 8, algorithm="HS256", headers={"kid": "k1"}))
    with pytest.raises(AuthError):
        v.verify(k.sign(claims()))


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c", "x" * 9000])
def test_garbage_tokens(bad):
    v, _ = make([KeyPair("k1")])
    with pytest.raises(AuthError):
        v.verify(bad)
