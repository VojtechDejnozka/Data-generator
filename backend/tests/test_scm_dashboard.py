import asyncio
import sqlite3

from fastapi.testclient import TestClient

from app.causal import service
from app.causal.service import scenario_spec
from app.contracts.spec import Dashboard, Widget
from app.reporting import scm_dashboard
from app.main import app

from .causal_fixtures import linear_xy
from .test_causal_e2e import _wait


def test_dashboard_repairs_sql_using_original_story_and_schema(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE units (unit_id INTEGER PRIMARY KEY, x REAL, y REAL)")
        conn.execute("INSERT INTO units VALUES (1, 3, 8)")
    spec = scenario_spec(linear_xy(1))
    prompts = []

    class Fake:
        async def structured(self, **kwargs):
            prompts.append(kwargs["prompt"])
            column = "missing" if len(prompts) == 1 else "x"
            return Dashboard(title="Vývoj", widgets=[
                Widget(id="x", title="X", chart="kpi", sql=f"SELECT {column} AS value FROM units", y=["value"]),
                Widget(id="y", title="Y", chart="kpi", sql="SELECT y AS value FROM units", y=["value"]),
            ])

    monkeypatch.setattr(scm_dashboard, "get_llm", lambda: Fake())
    story = "Změna poptávky ovlivňuje počet nákupů."
    _, widgets, errors = asyncio.run(scm_dashboard.plan_scm_dashboard(spec, db, story))
    assert story in prompts[0] and '"units"' in prompts[0]
    assert "missing" in prompts[1]
    assert not errors and {widget["id"]: widget["rows"] for widget in widgets} == {"x": [[3.0]], "y": [[8.0]]}


def test_scm_dashboard_is_generated_at_run_and_can_be_refreshed(monkeypatch):
    class Fake:
        mock = False

        async def structured(self, **kwargs):
            return Dashboard(title="Příběhový dashboard", widgets=[
                Widget(id=col, title=col, chart="kpi", sql=f"SELECT AVG({col}) AS value FROM units", y=["value"])
                for col in ("x", "y")
            ])

    fake = Fake()
    monkeypatch.setattr(service, "get_llm", lambda: fake)
    monkeypatch.setattr(scm_dashboard, "get_llm", lambda: fake)
    spec = scenario_spec(linear_xy(10))
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"spec": spec.model_dump(mode="json")})
        rid = _wait(client, started.json()["job_id"])["run_id"]
        url = f"/api/runs/{rid}/dashboard"
        dashboard = client.get(url).json()
        assert dashboard["meta"]["source"] == "llm"
        assert len(dashboard["widgets"]) == 2
        refresh = client.post(url)
        refreshed = _wait(client, refresh.json()["job_id"])
        assert refreshed == client.get(url).json()
