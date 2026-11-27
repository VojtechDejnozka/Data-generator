import time

from fastapi.testclient import TestClient

from app.main import app
from tests.test_pipeline import SMALL


def _wait(c, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/jobs/{job_id}").json()
        if s["status"] in ("done", "error"):
            assert s["status"] == "done", s["error"]
            return s["result"]
        time.sleep(0.2)
    raise TimeoutError


def test_api_end_to_end():
    with TestClient(app) as c:
        assert c.get("/api/health").json()["mock"] is True
        assert c.get("/api/presets").json()[0]["id"] == "retail_supplier_delay"
        sid = _wait(c, c.post("/api/plan", json=SMALL.model_dump()).json()["job_id"])["session_id"]
        sess = c.get(f"/api/plan/{sid}").json()
        spec = sess["response"]["spec"]
        rid = _wait(c, c.post("/api/runs", json={"spec": spec}).json()["job_id"])["run_id"]
        assert c.get(f"/api/runs/{rid}").json()["status"] == "done"
        rep = c.get(f"/api/runs/{rid}/report").json()
        assert rep["verdict"]["verdict"] == "pass"
        t = c.get(f"/api/runs/{rid}/tables/products", params={"page_size": 5, "sort": "unit_price", "desc": True}).json()
        assert len(t["rows"]) == 5 and t["fks"]["supplier_id"] == "suppliers.supplier_id"
        f = c.get(f"/api/runs/{rid}/tables/products", params={"filter_col": "supplier_id", "filter_val": "2"}).json()
        assert f["total"] > 0
        assert c.get(f"/api/runs/{rid}/tables/products", params={"sort": "x; drop"}).status_code == 400
        q = c.post(f"/api/runs/{rid}/query", json={"sql": "SELECT COUNT(*) AS n FROM suppliers"}).json()
        assert q["rows"] == [[6]]
        assert c.post(f"/api/runs/{rid}/query", json={"sql": "DELETE FROM suppliers"}).status_code == 400
        assert len(c.get(f"/api/runs/{rid}/dashboard").json()["widgets"]) >= 4
        assert c.get(f"/api/runs/{rid}/ground-truth").json()["story"]["supplier_id"] == 2
        sch = c.get(f"/api/runs/{rid}/schema").json()
        assert len(sch["edges"]) > 10
        rid2 = _wait(c, c.post(f"/api/runs/{rid}/whatif", json={"knob_overrides": {"delay_severity": "10"}}).json()["job_id"])["run_id"]
        cmp = c.get("/api/compare", params={"a": rid, "b": rid2}).json()
        assert cmp["changed_knobs"]["delay_severity"]["b"] == 10.0
        assert c.post(f"/api/runs/{rid}/commentary").json()["paragraphs"]
        assert c.get(f"/api/runs/{rid}/export.zip").content[:2] == b"PK"
        with c.stream("GET", f"/api/jobs/{c.post(f'/api/runs/{rid}/detective').json()['job_id']}/events") as s:
            body = "".join(s.iter_text())
        assert '"final": true' in body
