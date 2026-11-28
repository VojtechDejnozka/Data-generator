"""scripts/migrate_local_runs.py: legacy run directories -> owner + metadata rows + object storage."""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_local_runs.py"
OWNER = "aaaaaaaa-0000-4000-a000-0000000000ff"


def _legacy(d: Path, rid: str, created: str, parent=None, status="done"):
    (d / rid).mkdir(parents=True)
    (d / rid / "run.json").write_text(json.dumps({"run_id": rid, "status": status, "created_at": created, "name": rid,
                                                  "parent_run_id": parent, "verdict": {"verdict": "pass"}}))
    (d / rid / "data.sqlite").write_bytes(b"SQLite format 3\x00" + b"0" * 1000)


def test_migrate(tmp_path, monkeypatch):
    from app import config
    from app.storage import blobs, meta

    legacy = tmp_path / "legacy"
    _legacy(legacy, "20260101-000000-aaaa", "2026-01-01T00:00:00")
    _legacy(legacy, "20260102-000000-bbbb", "2026-01-02T00:00:00", parent="20260101-000000-aaaa")
    _legacy(legacy, "20260103-000000-cccc", "2026-01-03T00:00:00", status="running")
    monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
    config.get_settings.cache_clear()
    store_meta = meta.LocalMetaStore(config.get_settings())
    meta.set_meta(store_meta)
    blobs.set_blobs(blobs.DirBlobStore(tmp_path / "bucket"))
    spec = importlib.util.spec_from_file_location("migrate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["x", "--owner-id", OWNER, "--data-dir", str(legacy), "--preset",
                                      "20260101-000000-aaaa"])
    assert mod.main() == 0
    a = store_meta.get_run("20260101-000000-aaaa", None)
    b = store_meta.get_run("20260102-000000-bbbb", None)
    assert a["user_id"] == OWNER and a["is_preset"] and "data.sqlite" in a["files"]
    assert b["parent_run_id"] == "20260101-000000-aaaa" and not b.get("is_preset")
    import datetime as dt
    expected = dt.datetime.fromisoformat("2026-01-01T00:00:00").astimezone(dt.timezone.utc)
    assert dt.datetime.fromisoformat(a["created_at"]) == expected  # original (local) creation time kept, as UTC
    assert store_meta.get_run("20260103-000000-cccc", None) is None  # unfinished runs are skipped
    assert (tmp_path / "bucket" / OWNER / "20260102-000000-bbbb" / "data.sqlite.gz").exists()
    assert mod.main() == 0  # idempotent
