import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from app.causal import service
from app.causal.contracts import SCMSpec, VariableSpec
from app.main import app
from app.storage import runs as store
from tests.causal_fixtures import linear_xy, temporal
from tests.test_causal_e2e import _wait


def _approve(client, spec, input_data=None):
    res = client.post('/api/runs', json={'spec': spec, 'input': input_data})
    assert res.status_code == 200, res.text
    run_id = _wait(client, res.json()['job_id'])['run_id']
    return client.get(f'/api/runs/{run_id}').json()


def _rows(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('SELECT * FROM units ORDER BY unit_id').fetchall()


def test_scm_json_round_trip_and_manual_observation(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('Import and manual generation must not call the planner or detective')

    monkeypatch.setattr('app.causal.planner.get_llm', unexpected)
    monkeypatch.setattr(service, 'analyze', unexpected)
    monkeypatch.setattr(service, 'automatic_mode', unexpected)
    original_observe = service.observe_and_store
    calls = []

    def observe_once(*args, **kwargs):
        calls.append(args[2])
        return original_observe(*args, **kwargs)

    monkeypatch.setattr(service, 'observe_and_store', observe_once)
    source = linear_xy(128, coef=7, seed=52)
    source.variables.append(VariableSpec.model_validate({
        'variable_id': 'note', 'entity': 'units', 'column': 'note', 'data_type': 'text', 'parents': [{'variable': 'x'}],
        'mechanism': {'type': 'text_template', 'params': {'templates': {'default': ['Hodnota {x}', 'Záznam {x}']}}},
    }))
    original_spec = service.scenario_spec(source).model_dump(mode='json')
    with TestClient(app) as c:
        original = _approve(c, original_spec)
        exported = json.dumps(original['spec']['scm'], ensure_ascii=False)
        imported = c.post('/api/scm/import', json={'model': json.loads(exported), 'observation': {
            'mode': 'manual', 'missing_percent': 10, 'noise_percent': 30,
        }})
        assert imported.status_code == 200, imported.text
        session = c.get(f"/api/plan/{imported.json()['session_id']}").json()
        assert SCMSpec.model_validate(session['response']['spec']['scm']) == source
        run = _approve(c, session['response']['spec'], session['input'])
        a, b = original['run_id'], run['run_id']
        assert original['causal']['world_hash'] == run['causal']['world_hash']
        assert _rows(store.db_path(a)) == _rows(store.db_path(b))
        obs = run['observation']
        assert set(obs) == {'mode', 'status', 'observation_id'}
        assert obs['mode'] == 'manual' and obs['status'] == 'applied'
        assert len(calls) == 1
        ops = {op.type: op for op in calls[0].operations}
        assert ops['missing_values_mcar'].probability == 0.1
        assert ops['gaussian_measurement_noise'].params['intensity'] == 0.3
        assert 'unit_id' not in ops['gaussian_measurement_noise'].target_columns
        assert 'note' not in ops['gaussian_measurement_noise'].target_columns
        obs_path = store.run_path(b) / 'observations' / obs['observation_id'] / 'observed.sqlite'
        observed = _rows(obs_path)
        clean = _rows(store.db_path(b))
        assert [row[0] for row in observed] == [row[0] for row in clean]
        assert any(None in row[1:] for row in observed)
        assert any(ov[1] != cv[1] for ov, cv in zip(observed, clean) if ov[1] is not None)
        assert c.get(f"/api/runs/{b}/observations/{obs['observation_id']}/db").content.startswith(b'SQLite format 3')
        assert not (store.run_path(b) / 'evaluations').exists()
        edited_spec = session['response']['spec']
        target = next(v for v in edited_spec['scm']['variables'] if v['variable_id'] == 'y')
        target['mechanism']['params']['coefficients']['x'] = 2
        edited = _approve(c, edited_spec)
        edited_rows = _rows(store.db_path(edited['run_id']))
        assert edited['causal']['world_hash'] != run['causal']['world_hash']
        assert edited_rows != clean


def test_import_rejects_unsupported_version_and_invalid_cpt():
    with TestClient(app) as c:
        model = temporal().model_dump(mode='json')
        model['meta']['schema_version'] = '99.0'
        version = c.post('/api/scm/import', json={'model': model})
        assert version.status_code == 422 and '99.0' in version.json()['detail']
        model = temporal().model_dump(mode='json')
        mood = next(v for v in model['variables'] if v['variable_id'] == 'mood')
        mood['mechanism']['params']['default'] = {'good': 0.7, 'bad': 0.31}
        check = c.post('/api/scm/validate', json=model).json()
        assert check['report']['status'] == 'FAIL'
        invalid = c.post('/api/scm/import', json={'model': model})
        assert invalid.status_code == 422 and 'CPT probabilities' in invalid.json()['detail']


def test_automatic_uses_optimizer_and_keeps_selected_observation(monkeypatch):
    from app.causal.difficulty import CandidateEval
    from tests.test_causal_optimization import est

    phases = []

    def make_evaluator(run_id, cfg, progress=None):
        assert cfg.target_difficulty == 75
        assert not cfg.allow_structural_noise_optimization
        world, _ = service.load_world(run_id)

        async def evaluate(corruption, scale, repeats, phase, variant):
            phases.append(phase)
            assert scale == 1.0
            info = service.observe_and_store(run_id, world, corruption)
            return CandidateEval(estimate=est(40 if phase == 'baseline' else 60, repeats),
                                 observation_id=info.observation_id, world_hash=world.world_hash, n_evaluations=repeats)

        return evaluate

    monkeypatch.setattr(service, 'make_evaluator', make_evaluator)
    spec = service.scenario_spec(linear_xy(100)).model_dump(mode='json')
    with TestClient(app) as c:
        run = _approve(c, spec, {'story': 'Zkušební scénář pro optimalizaci.', 'engine': 'scm',
                                 'observation': {'mode': 'automatic', 'target': 75}})
        obs = run['observation']
        assert obs['status'] == 'target_not_reached' and obs['achieved'] == 60
        assert not obs['used_baseline'] and obs['observation_id']
        assert {'baseline', 'screening', 'final'} <= set(phases)
        assert c.get(f"/api/runs/{run['run_id']}/observations/{obs['observation_id']}").status_code == 200
