"""Document pipeline: renderers, fact checks, story injection, full mock run, source-run grounding, API."""
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.contracts.docs import DocInput, DocRunRequest, DocSpec, WrittenDocument, WrittenSection
from app.documents.pipeline import check_document, execute_doc_run, inject_story, mention_variants, plan_docs
from app.documents.render import doc_text, render
from app.jobs import Job
from app.storage import runs as store
from tests.conftest import run

DOC = WrittenDocument(title="Zpráva – Jiří Dvořák", sections=[
    WrittenSection(title="Údaje", pairs=[["Věk", "54"], ["Datum přijetí", "3. 2. 2025"]]),
    WrittenSection(title="Laboratoř", rows=[["Parametr", "Hodnota"], ["ALT", "1,4"]]),
    WrittenSection(title="Závěr", body="Pacient s diagnózou Gastritida. " * 10, items=["Kontrola za 14 dní"]),
])


@pytest.mark.parametrize("fmt,magic", [("pdf", b"%PDF"), ("html", b"<!doctype"), ("md", b"# "), ("txt", b"Zpr"),
                                       ("json", b"{"), ("eml", b"Subject")])
def test_renderers(fmt, magic):
    data = render(DOC, fmt, {"document_type": "zpráva"}, {"age": 54})
    assert data.startswith(magic) and len(data) > 100


def test_mention_variants_and_checks():
    assert "1,4" in mention_variants(1.4, "float") and "3. 2. 2025" in mention_variants("2025-02-03", "date")
    spec = DocSpec(name="t", document_type="zpráva", min_words=10, max_words=400,
                   facts=[{"name": "age", "type": "int", "must_mention": True},
                          {"name": "alt", "type": "float", "must_mention": True},
                          {"name": "admitted", "type": "date", "must_mention": True},
                          {"name": "diagnosis", "type": "text", "must_mention": True}],
                   sections=[{"title": "Údaje"}, {"title": "Laboratoř"}, {"title": "Závěr"}])
    ok = check_document(spec, DOC, {"age": 54, "alt": 1.4, "admitted": "2025-02-03", "diagnosis": "Gastritida"})
    assert ok["ok"], ok
    bad = check_document(spec, DOC, {"age": 61, "alt": 1.4, "admitted": "2025-02-03", "diagnosis": "Pneumonie"})
    assert not bad["ok"] and len(bad["issues"]) == 2


def test_story_injection_deterministic():
    spec = DocSpec(name="t", document_type="x", seed=5, facts=[{"name": "drug"}, {"name": "alt", "type": "float"}],
                   sections=[{"title": "A"}], story={"logline": "drug X raises ALT", "affected_share": 0.3,
                                                     "overrides": [{"field": "drug", "op": "set", "value": "X"},
                                                                   {"field": "alt", "op": "multiply", "value": "3"}]})
    df = pd.DataFrame({"doc_id": range(1, 21), "drug": ["A"] * 20, "alt": [0.5] * 20})
    out, aff = inject_story(spec, df)
    out2, aff2 = inject_story(spec, df)
    assert aff == aff2 and len(aff) == 6
    assert (out[out.doc_id.isin(aff)].drug == "X").all() and (out[out.doc_id.isin(aff)].alt == 1.5).all()
    assert (out[~out.doc_id.isin(aff)].alt == 0.5).all()


def test_medical_pdf_run_end_to_end():
    pr = run(plan_docs(DocInput(description="Propouštěcí zprávy pacientů interního oddělení", count=10, format="pdf")))
    assert pr.spec.story and pr.spec.count == 10
    job = Job(id="d", kind="docs")
    rid = run(execute_doc_run(DocRunRequest(spec=pr.spec), job))["run_id"]
    meta = store.load_run(rid)
    assert meta["kind"] == "documents" and meta["checks_summary"]["passed"] == 10
    files = list((store.run_path(rid) / "documents").glob("*.pdf"))
    assert len(files) == 10 and files[0].read_bytes()[:4] == b"%PDF"
    gt = store.load_json(rid, "manifest.json")
    facts = store.read_table(rid, "facts")
    aff = facts[facts.doc_id.isin(gt["affected_doc_ids"])]
    assert len(aff) == 2 and (aff.medication == "Novafen").all()
    assert "affected" not in " ".join(facts.columns)  # ground truth not leaked into the data


def test_documents_grounded_in_existing_run(retail_dataset, tmp_path):
    from app.generation.scenarios.base import GenContext
    from app.generation.scenarios.registry import RETAIL

    spec, dims = retail_dataset
    res = RETAIL.simulate(spec, dims, GenContext(seed=7, knobs=spec.knob_values()))
    src = store.new_run_id()
    store.run_path(src).mkdir(parents=True)
    store.write_sqlite(store.db_path(src), spec, res.tables)
    store.save_json(src, "run.json", {"run_id": src, "status": "done", "spec": spec.model_dump(mode="json")})
    pr = run(plan_docs(DocInput(description="Formální odpovědi zákazníkům na stížnosti", count=5, format="eml",
                                source_run_id=src, source_table="complaints")))
    assert [f.name for f in pr.spec.facts][:2] == ["complaint_id", "created_at"]
    rid = run(execute_doc_run(DocRunRequest(spec=pr.spec), Job(id="e", kind="docs")))["run_id"]
    facts = store.read_table(rid, "facts")
    src_c = res.tables["complaints"].set_index("complaint_id")
    for _, r in facts.iterrows():  # facts are real rows of the source table
        assert src_c.loc[r.complaint_id, "text"] == r.text
    assert (store.run_path(rid) / "documents" / "doc_0001.eml").read_bytes().startswith(b"Subject")


def test_docs_api():
    from app.main import app

    with TestClient(app) as c:
        jid = c.post("/api/docs/plan", json={"description": "Propouštěcí zprávy pacientů", "count": 4, "format": "html"}).json()["job_id"]
        for _ in range(100):
            s = c.get(f"/api/jobs/{jid}").json()
            if s["status"] == "done":
                break
            time.sleep(0.1)
        sid = s["result"]["session_id"]
        spec = c.get(f"/api/docs/plan/{sid}").json()["response"]["spec"]
        jid = c.post("/api/docs/runs", json={"spec": spec}).json()["job_id"]
        for _ in range(200):
            s = c.get(f"/api/jobs/{jid}").json()
            if s["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert s["status"] == "done", s
        rid = s["result"]["run_id"]
        r = c.get(f"/api/docs/runs/{rid}").json()
        f = r["documents"][0]["file"]
        assert c.get(f"/api/docs/runs/{rid}/files/{f}").text.startswith("<!doctype")
        assert c.get(f"/api/docs/runs/{rid}/files/../run.json").status_code == 404
        assert c.get(f"/api/docs/runs/{rid}/doc/1").json()["facts"]["doc_id"] == 1
        assert c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) FROM facts"}).json()["rows"] == [[4]]
        assert c.get(f"/api/docs/runs/{rid}/export.zip").content[:2] == b"PK"
        assert any(x["kind"] == "documents" for x in c.get("/api/runs").json())


def _source_run(retail_dataset):
    from app.generation.scenarios.base import GenContext
    from app.generation.scenarios.registry import RETAIL

    spec, dims = retail_dataset
    res = RETAIL.simulate(spec, dims, GenContext(seed=7, knobs=spec.knob_values()))
    src = store.new_run_id()
    store.run_path(src).mkdir(parents=True)
    store.write_sqlite(store.db_path(src), spec, res.tables)
    store.save_json(src, "run.json", {"run_id": src, "status": "done", "spec": spec.model_dump(mode="json"),
                                      "row_counts": {k: len(v) for k, v in res.tables.items()}})
    return src, res


def test_invoices_from_purchase_orders(retail_dataset):
    import json as _json
    import sqlite3

    from app.documents.sources import build_source_facts

    src, res = _source_run(retail_dataset)
    fields, facts = build_source_facts(src, "purchase_orders", 5, 1, "supplier_id", "2")
    names = [f.name for f in fields]
    assert {"supplier_name", "store_name", "items", "total_amount", "vat_amount", "total_with_vat"} <= set(names)
    assert (facts.supplier_id == 2).all()
    conn = sqlite3.connect(store.db_path(src))
    for _, r in facts.iterrows():
        expected = conn.execute(
            "SELECT ROUND(SUM(l.qty_received * p.unit_cost), 2) FROM purchase_order_lines l "
            "JOIN products p ON p.product_id = l.product_id WHERE l.po_id = ?", (int(r.po_id),)).fetchone()[0]
        assert abs(r.total_amount - expected) < 0.05  # amounts computed from the data, not by an LLM
        assert abs(r.total_with_vat - round(r.total_amount * 1.21, 2)) < 0.02
        items = _json.loads(r["items"])
        assert len(items) == r.items_count and "product_id" not in items[0] and "product_name" in items[0]
        assert not any(k.endswith("unit_price") for k in items[0])  # purchase invoice shows cost prices only
    conn.close()

    pr = run(plan_docs(DocInput(description="Faktury od dodavatelů k objednávkám", count=3, format="pdf",
                                source_run_id=src, source_table="purchase_orders",
                                source_filter_col="supplier_id", source_filter_val="2")))
    assert any(s.kind == "data_table" for s in pr.spec.sections) and pr.spec.story is None
    rid = run(execute_doc_run(DocRunRequest(spec=pr.spec), Job(id="i", kind="docs")))["run_id"]
    meta = store.load_run(rid)
    assert meta["checks_summary"]["passed"] == 3, meta["documents"]
    written = store.load_json(rid, "written.json")
    sec = next(s for s in written["1"]["sections"] if s["rows"])
    assert sec["rows"][0][0] in ("Objednáno", "Dodáno", "Kód", "Položka") and len(sec["rows"]) > 2
    assert sec["pairs"][-1][0] == "Celkem k úhradě" and sec["pairs"][-1][1].endswith("Kč")


def test_receipts_hide_internal_cost(retail_dataset):
    import json as _json

    from app.documents.sources import build_source_facts

    src, _ = _source_run(retail_dataset)
    fields, facts = build_source_facts(src, "transactions", 3, 1)
    items = _json.loads(facts["items"].iloc[0])
    assert not any("cost" in k for k in items[0]) and "line_total" in items[0]


def test_doc_sources_endpoint(retail_dataset):
    from app.main import app

    src, _ = _source_run(retail_dataset)
    with TestClient(app) as c:
        s = {x["table"]: x for x in c.get(f"/api/runs/{src}/doc-sources").json()}
    assert s["purchase_orders"]["items_from"] == "purchase_order_lines"
    assert "Faktura" in s["purchase_orders"]["suggestion"]
    assert s["transactions"]["items_from"] == "transaction_items"
