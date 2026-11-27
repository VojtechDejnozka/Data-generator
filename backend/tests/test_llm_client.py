import json
import types

import pytest
from google.genai import errors
from pydantic import BaseModel

from app.config import Settings
from app.llm.client import LLMClient, LLMError, UsageTracker, usage_scope
from tests.conftest import run


class Out(BaseModel):
    names: list[str]
    count: int


class FakeResp:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = types.SimpleNamespace(prompt_token_count=100, candidates_token_count=20,
                                                    thoughts_token_count=5)


class FakeSDK:
    """Scripted responses per model: list of Exception | str."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        outer = self

        class Models:
            async def generate_content(self, model, contents, config):
                outer.calls.append((model, contents, config))
                item = outer.script[model].pop(0)
                if isinstance(item, Exception):
                    raise item
                return FakeResp(item)

        self.aio = types.SimpleNamespace(models=Models())


def _client(tmp_path, script, **kw):
    s = Settings(gemini_api_key="k", strong_model="pro", strong_fallbacks=["flash"], cheap_model="lite",
                 cheap_fallbacks=[], llm_mock=False, cache_dir=tmp_path, **kw)
    c = LLMClient(s)
    c._client = FakeSDK(script)
    return c


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr("app.llm.client.asyncio.sleep", nosleep)


def test_quota_zero_falls_back_and_validation_retry(tmp_path):
    quota = errors.APIError(429, {"error": {"code": 429, "message": "Quota exceeded ... limit: 0, model: pro",
                                            "status": "RESOURCE_EXHAUSTED"}})
    c = _client(tmp_path, {"pro": [quota], "flash": ['{"names": "oops"}', '{"names": ["a"], "count": 1}']})
    tr = UsageTracker()
    with usage_scope(tr):
        out = run(c.structured(role="strong", task="t", prompt="p", schema=Out))
    assert out.names == ["a"]
    models = [m for m, _, _ in c._client.calls]
    assert models == ["pro", "flash", "flash"]
    # the retry prompt contains the validation error
    assert "previous answer was invalid" in c._client.calls[-1][1]
    rec = tr.calls[0]
    assert rec.model == "flash" and rec.prompt_tokens == 200 and rec.output_tokens == 50 and rec.cost_usd > 0
    # dead model is skipped next time
    assert c.models_for("strong") == ["flash"]


def test_backoff_on_503_then_success_and_cache(tmp_path):
    busy = errors.APIError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    c = _client(tmp_path, {"lite": [busy, busy, '{"names": ["x"], "count": 2}']})
    out = run(c.structured(role="cheap", task="t", prompt="p", schema=Out))
    assert out.count == 2 and len(c._client.calls) == 3
    # second identical call is served from the on-disk cache
    tr = UsageTracker()
    with usage_scope(tr):
        out2 = run(c.structured(role="cheap", task="t", prompt="p", schema=Out))
    assert out2 == out and len(c._client.calls) == 3 and tr.calls[0].cached
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_schema_rejected_falls_back_to_json_mode(tmp_path):
    bad = errors.APIError(400, {"error": {"code": 400, "message": "response schema too complex",
                                          "status": "INVALID_ARGUMENT"}})
    c = _client(tmp_path, {"lite": [bad, '```json\n{"names": [], "count": 0}\n```']})
    out = run(c.structured(role="cheap", task="t", prompt="p", schema=Out))
    assert out.count == 0
    cfg = c._client.calls[-1][2]
    assert cfg.response_json_schema is None and "JSON schema" in c._client.calls[-1][1]


def test_all_models_fail_raises(tmp_path):
    busy = errors.APIError(503, {"error": {"code": 503, "message": "overloaded"}})
    c = _client(tmp_path, {"pro": [busy] * 4, "flash": [busy] * 4})
    with pytest.raises(LLMError):
        run(c.structured(role="strong", task="t", prompt="p", schema=Out))


def test_mock_mode_uses_fixtures():
    from app.config import get_settings
    from app.llm.client import get_llm
    from app.presets.mock_data import MALE_FIRST
    from app.generation.scenarios.retail_supplier_delay import NamePools

    assert get_settings().mock
    out = run(get_llm().structured(role="cheap", task="dim_customer_names", prompt="x", schema=NamePools))
    assert out.male_first == MALE_FIRST
