"""Row Level Security + Storage policies, executed against a real (throwaway) Postgres.

The Supabase migrations are applied on top of tests/sql/supabase_shim.sql (roles, auth.uid(), storage
schema). Requires Postgres binaries: set PG_BIN=/path/to/pg/bin or have `initdb` on PATH; otherwise the
module is skipped. Alternatively TEST_DATABASE_URL may point to an empty scratch database.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))
SHIM = Path(__file__).parent / "sql" / "supabase_shim.sql"

A = str(uuid.UUID("aaaaaaaa-0000-4000-a000-000000000001"))
B = str(uuid.UUID("bbbbbbbb-0000-4000-a000-000000000002"))
ADMIN = str(uuid.UUID("cccccccc-0000-4000-a000-000000000003"))


def _pg_bin() -> Path | None:
    if os.getenv("PG_BIN"):
        return Path(os.environ["PG_BIN"])
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def dsn(tmp_path_factory):
    if os.getenv("TEST_DATABASE_URL"):
        yield os.environ["TEST_DATABASE_URL"]
        return
    pg_bin = _pg_bin()
    if not pg_bin:
        pytest.skip("no Postgres binaries (set PG_BIN or TEST_DATABASE_URL)")
    data = tmp_path_factory.mktemp("pgdata")
    subprocess.run([str(pg_bin / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust"], check=True,
                   capture_output=True)
    port = _free_port()
    proc = subprocess.Popen([str(pg_bin / "postgres"), "-D", str(data), "-p", str(port), "-c", "unix_socket_directories=",
                             "-c", "listen_addresses=127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    for _ in range(100):
        try:
            psycopg.connect(url).close()
            break
        except psycopg.OperationalError:
            if proc.poll() is not None:
                raise RuntimeError("postgres failed to start: " + proc.stderr.read().decode()[-2000:])
            time.sleep(0.1)
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="module")
def db(dsn):
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(SHIM.read_text())
    for m in MIGRATIONS:
        conn.execute(m.read_text())
    for uid, email in ((A, "a@example.com"), (B, "b@example.com"), (ADMIN, "admin@example.com")):
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, email))
    conn.execute("update public.profiles set role = 'admin' where id = %s", (ADMIN,))
    # service role (backend) writes the data
    for uid, rid in ((A, "run-a"), (B, "run-b")):
        conn.execute("insert into public.runs (id, user_id, name) values (%s, %s, %s)", (rid, uid, rid))
        conn.execute("insert into public.jobs (id, user_id, kind, run_id) values (%s, %s, 'run', %s)",
                     ("job-" + rid, uid, rid))
        conn.execute("insert into public.plan_sessions (id, user_id) values (%s, %s)", ("ses-" + rid, uid))
        conn.execute("insert into public.usage (user_id, tokens) values (%s, 100)", (uid,))
        conn.execute("insert into storage.objects (bucket_id, name) values ('runs', %s)", (f"{uid}/{rid}/data.sqlite.gz",))
    conn.execute("insert into public.runs (id, user_id, name, is_preset) values ('preset-1', %s, 'demo', true)", (ADMIN,))
    conn.execute("insert into storage.objects (bucket_id, name) values ('runs', %s)", (f"{ADMIN}/preset-1/data.sqlite.gz",))
    conn.execute("insert into public.runs (id, user_id, name) values ('admin-private', %s, 'x')", (ADMIN,))
    conn.execute("insert into storage.objects (bucket_id, name) values ('runs', %s)", (f"{ADMIN}/admin-private/x.json",))
    yield conn
    conn.close()


class As:
    """Run statements as an API role with the given JWT claims, inside a rolled-back transaction."""

    def __init__(self, db, uid: str | None, role: str = "authenticated"):
        self.db, self.uid, self.role = db, uid, role

    def __enter__(self):
        self.tx = self.db.transaction(force_rollback=True)
        self.tx.__enter__()
        claims = {"sub": self.uid, "role": self.role} if self.uid else {"role": self.role}
        self.db.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        self.db.execute(f"set local role {self.role}")
        return self

    def rows(self, sql: str, params=()):
        return self.db.execute(sql, params).fetchall()

    def __exit__(self, *exc):
        return self.tx.__exit__(None, None, None) if exc[0] is None else self.tx.__exit__(*exc)


def test_rls_enabled_on_every_public_table(db):
    rows = db.execute("""select c.relname, c.relrowsecurity,
                                (select count(*) from pg_policies p where p.schemaname = 'public' and p.tablename = c.relname)
                         from pg_class c join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'public' and c.relkind = 'r'""").fetchall()
    assert rows
    for name, rls, policies in rows:
        assert rls, f"RLS disabled on {name}"
        assert policies > 0, f"no policies on {name}"


def test_profile_created_by_trigger(db):
    rows = db.execute("select id::text, email, role, approved from public.profiles order by email").fetchall()
    assert [r[1] for r in rows] == ["a@example.com", "admin@example.com", "b@example.com"]
    assert all(r[3] is False for r in rows)
    db.execute("insert into auth.users (id, email, invited_at) values (gen_random_uuid(), 'Inv@Example.com', now())")
    assert db.execute("select approved from public.profiles where email = 'inv@example.com'").fetchone() == (True,)


@pytest.mark.parametrize("table,ids", [
    ("runs", {"run-a", "preset-1"}),
    ("jobs", {"job-run-a"}),
    ("plan_sessions", {"ses-run-a"}),
])
def test_user_a_sees_only_own_rows(db, table, ids):
    with As(db, A) as s:
        assert {r[0] for r in s.rows(f"select id from public.{table}")} == ids


def test_user_b_cannot_read_user_a_run_by_id(db):
    with As(db, B) as s:
        assert s.rows("select * from public.runs where id = 'run-a'") == []
        assert s.rows("select * from public.usage where user_id = %s", (A,)) == []
        assert s.rows("select * from public.profiles where id = %s", (A,)) == []


def test_anon_sees_nothing(db):
    with As(db, None, "anon") as s:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            s.rows("select * from public.runs")


@pytest.mark.parametrize("sql", [
    "insert into public.runs (id, user_id) values ('evil', '{uid}')",
    "update public.runs set name = 'x' where id = 'run-a'",
    "delete from public.runs where id = 'run-a'",
    "insert into public.usage (user_id, tokens) values ('{uid}', 0)",
    "delete from public.usage",
    "update public.jobs set status = 'done'",
    "update public.profiles set role = 'admin' where id = '{uid}'",
    "update public.profiles set approved = true where id = '{uid}'",
])
def test_users_cannot_write_protected_data(db, sql):
    with As(db, A) as s:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            s.rows(sql.format(uid=A))


def test_user_can_rename_self_only(db):
    with As(db, A) as s:
        s.db.execute("update public.profiles set display_name = 'Alice' where id = %s", (A,))
        assert s.rows("select display_name from public.profiles where id = %s", (A,)) == [("Alice",)]
        cur = s.db.execute("update public.profiles set display_name = 'pwned' where id = %s", (B,))
        assert cur.rowcount == 0


def test_admin_reads_all_profiles_and_usage(db):
    with As(db, ADMIN) as s:
        assert {A, B, ADMIN} <= {str(r[0]) for r in s.rows("select id from public.profiles")}
        assert len(s.rows("select id from public.usage")) == 2
        # but admins still only see their own (and preset) runs through the Data API
        assert {r[0] for r in s.rows("select id from public.runs")} == {"preset-1", "admin-private"}


def test_storage_isolation(db):
    with As(db, A) as s:
        names = {r[0] for r in s.rows("select name from storage.objects where bucket_id = 'runs'")}
        assert names == {f"{A}/run-a/data.sqlite.gz", f"{ADMIN}/preset-1/data.sqlite.gz"}
    with As(db, B) as s:
        assert s.rows("select name from storage.objects where name like %s", (f"{A}/%",)) == []


@pytest.mark.parametrize("sql", [
    "insert into storage.objects (bucket_id, name) values ('runs', '{uid}/run-a/private/_gen_state.pkl.gz')",
    "update storage.objects set name = name || 'x' where name like '{uid}/%'",
    "delete from storage.objects where name like '{uid}/%'",
])
def test_storage_users_cannot_write_even_own_folder(db, sql):
    with As(db, A) as s:
        try:
            cur = s.db.execute(sql.format(uid=A))
        except psycopg.errors.InsufficientPrivilege:
            return
        assert cur.rowcount == 0  # UPDATE/DELETE silently match nothing without a policy


def test_preset_path_must_match_preset_owner(db):
    # a non-preset object placed under another user's folder with a preset run id is NOT readable
    db.execute("insert into storage.objects (bucket_id, name) values ('runs', %s)", (f"{B}/preset-1/fake.json",))
    with As(db, A) as s:
        assert s.rows("select name from storage.objects where name = %s", (f"{B}/preset-1/fake.json",)) == []


def test_bucket_is_private(db):
    assert db.execute("select public, file_size_limit from storage.buckets where id = 'runs'").fetchone() == (False, 52428800)
