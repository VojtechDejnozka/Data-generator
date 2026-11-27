import sqlite3

import pytest

from app.generation.scenarios.base import GenContext
from app.generation.scenarios.registry import RETAIL
from app.reporting.evidence import run_evidence
from app.storage import runs as store
from app.storage.sql_guard import SQLGuardError, render_placeholders, run_readonly, validate_select
from app.validation.checks import run_checks, summarize


@pytest.fixture()
def db(retail_dataset, tmp_path):
    spec, dims = retail_dataset
    kv = spec.knob_values()
    res = RETAIL.simulate(spec, dims, GenContext(seed=7, knobs=kv))
    path = tmp_path / "d.sqlite"
    store.write_sqlite(path, spec, res.tables)
    return path, spec, kv


def test_all_checks_pass(db):
    path, spec, kv = db
    res = run_checks(path, spec, kv)
    s = summarize(res)
    assert s["errors"] == 0, [r for r in res if not r.passed]
    assert s["fk_integrity_pct"] == 100.0
    assert len(spec.tables) >= 6


def test_broken_fk_is_detected(db):
    path, spec, kv = db
    conn = sqlite3.connect(path)
    conn.execute("UPDATE transaction_items SET product_id = 999999 WHERE transaction_item_id = 1")
    conn.execute("UPDATE deliveries SET delivered_date = '2000-01-01' WHERE delivery_id = 1")
    conn.commit()
    conn.close()
    res = {r.id: r for r in run_checks(path, spec, kv)}
    assert not res["fk:transaction_items.product_id"].passed
    assert not res["inv_delivery_after_plan"].passed
    assert not res["inv_delay_consistent"].passed


def test_evidence_queries_run(db):
    path, spec, kv = db
    ev = run_evidence(path, spec, kv)
    assert all(e["ok"] for e in ev), [e.get("error") for e in ev]
    assert all(e["rows"] for e in ev)


@pytest.mark.parametrize("sql", [
    "DELETE FROM suppliers", "SELECT 1; DROP TABLE suppliers", "PRAGMA table_info(x)",
    "ATTACH DATABASE 'x' AS y", "UPDATE suppliers SET name='x'", "WITH x AS (SELECT 1) INSERT INTO a SELECT * FROM x",
])
def test_guard_rejects_writes(sql):
    with pytest.raises(SQLGuardError):
        validate_select(sql)


def test_guard_allows_strings_with_keywords(db):
    path, _, _ = db
    r = run_readonly(path, "SELECT 'delete; drop' AS x, replace('a','a','b') AS y")
    assert r.rows == [["delete; drop", "b"]]


def test_readonly_connection_blocks_writes_even_if_text_check_bypassed(db):
    path, _, _ = db
    conn = store.sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM suppliers")
    conn.close()


def test_row_limit_and_placeholders(db):
    path, _, kv = db
    r = run_readonly(path, "SELECT * FROM transactions", limit=10)
    assert len(r.rows) == 10 and r.truncated
    sql = render_placeholders("SELECT {{knob:supplier_id}}, '{{meta:name}}'", {"supplier_id": 2}, {"name": "O'Neil"})
    assert sql == "SELECT 2, 'O''Neil'"
