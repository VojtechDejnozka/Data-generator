"""Object storage for run files: Supabase Storage (private bucket) or a plain directory (tests).

Object keys are ``{owner_id}/{run_id}/{relpath}``; only the backend writes (secret key), see the storage
policies in supabase/migrations. Downloads for the browser use short-lived signed URLs, so neither the
secret key nor the user's access token ever appears in a URL.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import quote

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("blobs")


class BlobError(RuntimeError):
    pass


def _check_key(key: str) -> str:
    parts = key.split("/")
    if len(parts) < 3 or any(p in ("", ".", "..") for p in parts) or "\\" in key or len(key) > 900:
        raise BlobError("invalid object key")
    return key


class BlobStore(Protocol):
    def upload(self, key: str, src: Path, content_type: str = "application/octet-stream") -> None: ...
    def download(self, key: str, dest: Path) -> None: ...
    def delete(self, keys: list[str]) -> None: ...
    def signed_url(self, key: str, expires_s: int = 60, download_name: str = "") -> str: ...


class DirBlobStore:
    """Directory-backed store with the same semantics (used by tests of the sync logic)."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def upload(self, key: str, src: Path, content_type: str = "application/octet-stream") -> None:
        dest = self.root / _check_key(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def download(self, key: str, dest: Path) -> None:
        src = self.root / _check_key(key)
        if not src.exists():
            raise BlobError("object not found")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def delete(self, keys: list[str]) -> None:
        for k in keys:
            (self.root / _check_key(k)).unlink(missing_ok=True)

    def signed_url(self, key: str, expires_s: int = 60, download_name: str = "") -> str:
        return f"file://{self.root / _check_key(key)}"


class SupabaseBlobStore:
    def __init__(self, settings: Settings, transport: Optional[httpx.BaseTransport] = None):
        self.s = settings
        self.base = f"{settings.supabase_url}/storage/v1"
        self.http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), transport=transport)

    def _headers(self) -> dict[str, str]:
        key = self.s.supabase_secret_key
        h = {"apikey": key}
        if not key.startswith("sb_"):
            h["Authorization"] = f"Bearer {key}"
        return h

    def _url(self, *parts: str) -> str:
        return "/".join([self.base, *parts])

    def _obj(self, key: str) -> str:
        return quote(_check_key(key), safe="/")

    def upload(self, key: str, src: Path, content_type: str = "application/octet-stream") -> None:
        with open(src, "rb") as f:
            r = self.http.post(self._url("object", self.s.storage_bucket, self._obj(key)), content=f,
                               headers={**self._headers(), "content-type": content_type, "x-upsert": "true",
                                        "cache-control": "no-cache"})
        if r.status_code >= 400:
            log.error("storage upload %s -> %s %s", key, r.status_code, r.text[:300])
            raise BlobError(f"upload failed ({r.status_code})")

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with self.http.stream("GET", self._url("object", "authenticated", self.s.storage_bucket, self._obj(key)),
                              headers=self._headers()) as r:
            if r.status_code >= 400:
                log.error("storage download %s -> %s", key, r.status_code)
                raise BlobError(f"download failed ({r.status_code})")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(dest)

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 100):
            batch = [_check_key(k) for k in keys[i:i + 100]]
            r = self.http.request("DELETE", self._url("object", self.s.storage_bucket), json={"prefixes": batch},
                                  headers=self._headers())
            if r.status_code >= 400:
                log.error("storage delete -> %s %s", r.status_code, r.text[:300])
                raise BlobError(f"delete failed ({r.status_code})")

    def signed_url(self, key: str, expires_s: int = 60, download_name: str = "") -> str:
        r = self.http.post(self._url("object", "sign", self.s.storage_bucket, self._obj(key)),
                           json={"expiresIn": int(expires_s)}, headers=self._headers())
        if r.status_code >= 400:
            log.error("storage sign %s -> %s %s", key, r.status_code, r.text[:300])
            raise BlobError(f"sign failed ({r.status_code})")
        signed = r.json().get("signedURL") or r.json().get("signedUrl") or ""
        url = f"{self.base}{signed if signed.startswith('/') else '/' + signed}"
        if download_name:
            url += ("&" if "?" in url else "?") + "download=" + quote(download_name)
        return url


_blobs: Optional[BlobStore] = None
_blobs_key: Optional[tuple] = None
_override = False


def get_blobs() -> Optional[BlobStore]:
    """None = local mode: the workspace directory is the permanent store."""
    global _blobs, _blobs_key
    if _override:
        return _blobs
    s = get_settings()
    key = (s.use_supabase, s.supabase_url, s.storage_bucket)
    if _blobs_key != key:
        _blobs = SupabaseBlobStore(s) if s.use_supabase else None
        _blobs_key = key
    return _blobs


def set_blobs(store: Optional[BlobStore], override: bool = True) -> None:
    """Tests: force a store (``override=False`` restores configuration-based selection)."""
    global _blobs, _blobs_key, _override
    _blobs, _blobs_key, _override = store, None, override
