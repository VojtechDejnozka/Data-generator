"""Import runs created before accounts existed (DATA_DIR/<run_id>/) into Supabase.

Every run gets an owner, a `runs` metadata row and its files in the private Storage bucket
({owner}/{run_id}/…, *.sqlite gzip-compressed). Selected runs can be marked as shared demo presets.
Idempotent: unchanged files (same sha256) are not uploaded again.

Usage (from backend/, with SUPABASE_URL + SUPABASE_SECRET_KEY + SUPABASE_PUBLISHABLE_KEY in .env):
  .venv/bin/python ../scripts/migrate_local_runs.py --owner-email admin@example.com [--preset RUN_ID ...] [--dry-run]
  .venv/bin/python ../scripts/migrate_local_runs.py --owner-email admin@example.com --only RUN_ID --only RUN_ID

The owner must have signed in once (or been seeded) so that their profile row exists.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner-email", help="e-mail of the account that will own the runs")
    ap.add_argument("--owner-id", help="auth user id (alternative to --owner-email)")
    ap.add_argument("--preset", action="append", default=[], help="run id to mark as shared demo preset")
    ap.add_argument("--only", action="append", default=[], help="migrate only these run ids")
    ap.add_argument("--data-dir", help="legacy runs directory (default: DATA_DIR / ../runs)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.config import REPO_DIR, get_settings

    data_dir = Path(args.data_dir or os.getenv("DATA_DIR") or REPO_DIR / "runs").resolve()
    # Upload the legacy directories in place: the workspace *is* the legacy data dir for this script.
    os.environ["WORKSPACE_DIR"] = str(data_dir)
    get_settings.cache_clear()
    s = get_settings()
    if not s.use_supabase:
        print("SUPABASE_URL is not configured – nothing to migrate to.", file=sys.stderr)
        return 2

    from app.storage import runs as store
    from app.storage.meta import get_meta
    from app.storage.workspace import sync_up

    meta = get_meta()
    owner = args.owner_id
    if not owner:
        if not args.owner_email:
            ap.error("--owner-email or --owner-id is required")
        rows = meta._req("GET", "/profiles", params={"email": f"eq.{args.owner_email.lower()}", "select": "id"})  # noqa: SLF001
        if not rows:
            print(f"No profile for {args.owner_email} – sign in once (or run supabase seed) first.", file=sys.stderr)
            return 2
        owner = rows[0]["id"]

    docs = []
    for p in data_dir.glob("*/run.json"):
        if p.parent.name.startswith("_") or (args.only and p.parent.name not in args.only):
            continue
        try:
            docs.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"skip {p.parent.name}: unreadable run.json")
    docs.sort(key=lambda d: d.get("created_at") or "")  # parents before their what-ifs
    migrated: set[str] = set()
    for doc in docs:
        rid = doc.get("run_id")
        if not rid or doc.get("status") != "done":
            print(f"skip {rid}: status {doc.get('status')}")
            continue
        try:
            store.check_run_id(rid)
        except ValueError:
            print(f"skip {rid}: invalid id")
            continue
        existing = meta.get_run(rid, None)
        if existing and existing.get("user_id") != owner:
            print(f"skip {rid}: already owned by another account")
            continue
        row = store.meta_from_doc(doc, owner)
        if row.get("parent_run_id") and row["parent_run_id"] not in migrated and not meta.get_run(row["parent_run_id"], None):
            row["parent_run_id"] = None
        size = sum(f.stat().st_size for f in (data_dir / rid).rglob("*") if f.is_file())
        print(f"{'would migrate' if args.dry_run else 'migrating'} {rid} ({size / 1e6:.1f} MB)"
              + (" [preset]" if rid in args.preset else ""))
        if args.dry_run:
            continue
        meta.upsert_run({**row, "is_preset": rid in args.preset})
        sync_up(rid, owner)
        migrated.add(rid)
    print(f"done: {len(migrated)} run(s) migrated to owner {owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
