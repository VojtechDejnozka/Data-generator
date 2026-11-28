import json

from fastapi.testclient import TestClient

from app.causal import planner
from app.causal.service import scenario_spec
from app.main import app

from .causal_fixtures import linear_xy
from .test_causal_e2e import _wait


def test_reopen_download_and_replan_the_approved_scm(monkeypatch):
    base = linear_xy(10)
    story = "Poptávka ovlivňuje prodej v jednotlivých jednotkách."
    base.meta.original_story = story
    spec = scenario_spec(base)
    prompts = []

    class Fake:
        mock = False

        async def structured(self, *, schema, prompt, **kwargs):
            prompts.append(prompt)
            model = base.model_dump(mode="json")
            if len(prompts) == 1:
                model["entities"][0]["count"] = 25
            return schema.model_validate({"scm": model})

    monkeypatch.setattr(planner, "get_llm", lambda: Fake())
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"spec": spec.model_dump(mode="json")})
        rid = _wait(client, started.json()["job_id"])["run_id"]
        url = f"/api/runs/{rid}"
        saved = client.get(url).json()["spec"]["scm"]
        exported = client.get(url + "/scm.json")
        assert "attachment" in exported.headers["content-disposition"]
        assert json.loads(exported.content) == saved
        sid = client.post(url + "/review").json()["session_id"]
        plan_url = f"/api/plan/{sid}"
        review = client.get(plan_url).json()
        assert review["parent_run_id"] == rid and review["response"]["spec"]["scm"] == saved
        tables = client.post("/api/scm/validate", json=saved).json()["tables"]
        assert tables[0]["columns"][0]["pk"] is True
        edited = review["response"]["spec"]
        edited["scm"]["entities"][0]["count"] = 12
        job = client.post(plan_url + "/replan", json={"spec": edited, "feedback": "Zvyš počet jednotek na 25."})
        _wait(client, job.json()["job_id"])
        revised = client.get(plan_url).json()
        assert '"count": 12' in prompts[0] and story in prompts[0]
        assert revised["round"] == 1 and revised["response"]["spec"]["scm"]["entities"][0]["count"] == 25
        assert revised["response"]["spec"]["scm"]["meta"]["original_story"] == story
        assert client.get(url).json()["spec"]["scm"] == saved
        new_run = client.post("/api/runs", json={"spec": revised["response"]["spec"], "parent_run_id": rid})
        new_id = _wait(client, new_run.json()["job_id"])["run_id"]
        result = client.get(f"/api/runs/{new_id}").json()
        assert result["parent_run_id"] == rid and result["row_counts"]["units"] == 25
