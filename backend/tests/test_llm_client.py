import json
import types

import pytest
import httpx
from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings
from app.llm.client import LLMClient, LLMError, UsageTracker, usage_scope
from tests.conftest import run


class Out(BaseModel):
    names: list[str]
    count: int


def api_error(status, message, **fields):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return APIStatusError(message, response=response, body={"message": message, **fields})


class FakeResp:
    def __init__(self, text):
        self.output_text = text
        self.status = "completed"
        self.usage = types.SimpleNamespace(input_tokens=100, output_tokens=25,
                                           output_tokens_details=types.SimpleNamespace(reasoning_tokens=5))


class FakeSDK:
    """Scripted responses per model: list of Exception | str."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        outer = self

        class Responses:
            async def create(self, model, input, **options):
                outer.calls.append((model, input, options))
                item = outer.script[model].pop(0)
                if isinstance(item, Exception):
                    raise item
                return FakeResp(item)

        self.responses = Responses()


def _client(tmp_path, script, **kw):
    s = Settings(openai_api_key="k", strong_model="pro", strong_fallbacks=["flash"], cheap_model="lite",
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
    quota = api_error(429, "Quota exceeded", code="insufficient_quota")
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
    busy = api_error(503, "overloaded")
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
    bad = api_error(400, "response schema too complex")
    c = _client(tmp_path, {"lite": [bad, '```json\n{"names": [], "count": 0}\n```']})
    out = run(c.structured(role="cheap", task="t", prompt="p", schema=Out))
    assert out.count == 0
    cfg = c._client.calls[-1][2]
    assert cfg["text"]["format"]["type"] == "json_object" and "JSON schema" in c._client.calls[-1][1]


def test_all_models_fail_raises(tmp_path):
    busy = api_error(503, "overloaded")
    c = _client(tmp_path, {"pro": [busy] * 4, "flash": [busy] * 4})
    with pytest.raises(LLMError):
        run(c.structured(role="strong", task="t", prompt="p", schema=Out))


def test_luna_request_shape_with_openai_sdk(tmp_path):
    def respond(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == "gpt-6-luna" and payload["store"] is False
        assert payload["text"]["format"]["type"] == "json_schema"
        assert "temperature" not in payload
        return httpx.Response(200, json={
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"names":[],"count":0}'}]}],
        })

    async def check():
        client = LLMClient(Settings(openai_api_key="test", cache_dir=tmp_path))
        async with AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as sdk:
            client._client = sdk
            return await client.structured(role="strong", task="t", prompt="p", schema=Out, temperature=0.2)

    assert run(check()).count == 0


def test_mock_mode_uses_fixtures():
    from app.config import get_settings
    from app.llm.client import get_llm
    from app.presets.mock_data import MALE_FIRST
    from app.generation.scenarios.retail_supplier_delay import NamePools

    assert get_settings().mock
    out = run(get_llm().structured(role="cheap", task="dim_customer_names", prompt="x", schema=NamePools))
    assert out.male_first == MALE_FIRST
