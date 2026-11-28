import datetime as dt
import asyncio
import sqlite3

import pytest
import numpy as np

from app.causal.detective.tools import Toolbox, _change_onset
from app.causal.contracts import DetectiveConfig
from app.causal.detective.statistical import StatisticalDetective
from app.causal.results import PublicSchema, KeyEvent, InferredRelationship
from app.causal.sqlsafe import ReadOnlyDB
from app import detective


def test_group_comparison_subtracts_common_trend(tmp_path):
    db = tmp_path / "panel.sqlite"
    day = dt.date(2025, 1, 1)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE panel (period TEXT, entity INTEGER, measure REAL)")
        conn.executemany("INSERT INTO panel VALUES (?, ?, ?)", [
            ((day + dt.timedelta(days=t)).isoformat(), group, 2 * t + group + ((5 + (-1)**t) if group == 2 and t >= 10 else 0))
            for t in range(20) for group in (1, 2, 3)
        ])
    toolbox = Toolbox(ReadOnlyDB(db), PublicSchema(tables=[]))
    evidence = toolbox.call("compare_group_changes", query="SELECT * FROM panel", time_column="period",
                            group_column="entity", value_column="measure", treated_groups=[2], start="2025-01-11")
    assert evidence.ok
    result = evidence.result
    assert result["control_change"] == 20
    assert result["affected_change"] == 25
    assert result["estimate"] == 5
    assert result["pre_gap_slope"] == pytest.approx(0)
    assert result["ci95"][0] < 5 < result["ci95"][1]


def test_onset_search_finds_start_of_ramp_and_ignores_base_trend():
    t = np.arange(32)
    baseline = 2 + 0.3 * t
    ramp = 8 * np.clip((t - 10 + 1) / 8, 0, 1)
    assert _change_onset(baseline, "+") is None
    assert _change_onset(baseline + ramp, "+") == 10


def test_classic_detective_can_use_regression_as_evidence(tmp_path, monkeypatch):
    db = tmp_path / "regression.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE data (x REAL, y REAL)")
        conn.executemany("INSERT INTO data VALUES (?, ?)", [(x, 3 * x + (-1)**x) for x in range(30)])
    monkeypatch.setattr(detective.store, "db_path", lambda _: db)
    monkeypatch.setattr(detective.store, "load_json", lambda *args: {})
    monkeypatch.setattr(detective.store, "save_json", lambda *args: None)

    class Fake:
        mock = False

        async def structured(self, *, prompt, mock_context, **kwargs):
            if mock_context["step"] == 0:
                return detective.DetectiveStep(
                    thought="Check regression", action="query", sql="SELECT x, y FROM data",
                    analysis="fit_linear_regression", analysis_args='{"target":"y","features":["x"]}',
                )
            assert '"evidence_id": "E1"' in prompt
            return detective.DetectiveStep(thought="Done", action="final")

    monkeypatch.setattr(detective, "get_llm", lambda: Fake())
    result = asyncio.run(detective.investigate("test"))
    assert result["transcript"][0]["observation"]["ok"]


def test_ranked_detective_prefers_supported_chain_over_earlier_distractor(tmp_path):
    db = tmp_path / "hypotheses.sqlite"
    day = dt.date(2025, 1, 1)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE panel (period TEXT, entity INTEGER, noise REAL, delay REAL, stockout REAL)")
        conn.executemany("INSERT INTO panel VALUES (?, ?, ?, ?, ?)", [
            ((day + dt.timedelta(days=t)).isoformat(), group,
             (1 + 2 * (-1)**t) if group == 2 and t >= 8 else 0,
             (7 + 0.2 * (-1)**t) if group == 2 and t >= 14 else 0,
             (5 + 0.2 * (-1)**t) if group == 2 and t >= 17 else 0)
            for t in range(32) for group in (1, 2, 3)
        ])

    def assess():
        toolbox = Toolbox(ReadOnlyDB(db), PublicSchema(tables=[]))
        detective = StatisticalDetective(toolbox, DetectiveConfig(rank_cause_candidates=True))
        events = []
        for variable, start in (("noise", 8), ("delay", 14), ("stockout", 17)):
            source = toolbox.call("analyze_time_series", query=f"SELECT period, entity, {variable} AS v FROM panel",
                                  time_column="period", group_column="entity", value_column="v")
            events.append(KeyEvent(description=variable, variable=f"panel.{variable}", entity="panel.entity",
                                   entity_ids=[2], direction="+", start_time=(day + dt.timedelta(days=start)).isoformat(),
                                   evidence_ids=[source.evidence_id]))
        relationships = [InferredRelationship(source="panel.delay", target="panel.stockout")]
        ranking = detective.rank_candidates(events, relationships)
        assert next(e.variable for e in events if e.role == "root_cause") == "panel.delay"
        assert ranking[0]["later_signals"] == ["panel.stockout"]
        return ranking

    assert assess() == assess()
