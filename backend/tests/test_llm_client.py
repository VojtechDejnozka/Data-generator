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


# --------------------------------------------------------------------------- Gemini + provider routing
class FakeGeminiResp:
    def __init__(self, text):
        self.text = text
        self.candidates = [types.SimpleNamespace(finish_reason=types.SimpleNamespace(name="STOP"))]
        self.usage_metadata = types.SimpleNamespace(prompt_token_count=80, candidates_token_count=20,
                                                    thoughts_token_count=10)


class FakeGemini:
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
                return FakeGeminiResp(item)

        self.aio = types.SimpleNamespace(models=Models())


def gemini_error(code, message):
    from google.genai import errors
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": "X"}})


def test_gemini_only_key_uses_gemini_with_schema(tmp_path):
    s = Settings(gemini_api_key="g", llm_provider="gemini", strong_model="gemini-4-carbon",
                 strong_fallbacks=["gemini-3.8-flash"], cheap_model="gemini-3.5-flash-lite", cheap_fallbacks=[],
                 llm_mock=False, cache_dir=tmp_path)
    assert not s.mock and s.providers == ["gemini"]
    c = LLMClient(s)
    c._gemini = FakeGemini({"gemini-4-carbon": [gemini_error(429, "Quota exceeded, limit: 0")],
                            "gemini-3.8-flash": ['{"names": ["g"], "count": 1}']})
    tr = UsageTracker()
    with usage_scope(tr):
        out = run(c.structured(role="strong", task="t", prompt="p", schema=Out, system="sys", temperature=0.3))
    assert out.names == ["g"]
    assert [m for m, _, _ in c._gemini.calls] == ["gemini-4-carbon", "gemini-3.8-flash"]
    cfg = c._gemini.calls[-1][2]
    assert cfg.response_json_schema["properties"]["count"]["type"] == "integer"
    assert cfg.system_instruction == "sys" and cfg.temperature == 0.3
    rec = tr.calls[0]
    assert rec.model == "gemini-3.8-flash" and rec.prompt_tokens == 80 and rec.output_tokens == 30
    assert rec.thoughts_tokens == 10 and rec.cost_usd > 0
    assert c.models_for("strong") == ["gemini-3.8-flash"]


def test_gemini_schema_rejected_falls_back_to_json_mode(tmp_path):
    s = Settings(gemini_api_key="g", cheap_model="gemini-3.5-flash-lite", cheap_fallbacks=[], cache_dir=tmp_path)
    c = LLMClient(s)
    c._gemini = FakeGemini({"gemini-3.5-flash-lite": [gemini_error(400, "Invalid JSON schema"),
                                                      '{"names": [], "count": 0}']})
    assert run(c.structured(role="cheap", task="t", prompt="p", schema=Out)).count == 0
    model, contents, cfg = c._gemini.calls[-1]
    assert cfg.response_json_schema is None and "JSON schema" in contents


def test_mixed_chain_routes_per_model_and_skips_providers_without_key(tmp_path):
    s = Settings(openai_api_key="k", gemini_api_key="g", strong_model="gpt-6-luna",
                 strong_fallbacks=["gemini-3.8-flash"], cache_dir=tmp_path)
    c = LLMClient(s)
    c._client = FakeSDK({"gpt-6-luna": [api_error(429, "Quota", code="insufficient_quota")]})
    c._gemini = FakeGemini({"gemini-3.8-flash": ['{"names": ["mix"], "count": 3}']})
    assert run(c.structured(role="strong", task="t", prompt="p", schema=Out)).names == ["mix"]
    assert len(c._client.calls) == 1 and len(c._gemini.calls) == 1
    # without a Gemini key the Gemini fallback is not even tried
    only_openai = LLMClient(Settings(openai_api_key="k", strong_model="gpt-6-luna",
                                     strong_fallbacks=["gemini-3.8-flash"], cache_dir=tmp_path))
    assert only_openai.models_for("strong") == ["gpt-6-luna"]


def test_settings_pick_provider_and_models_from_env(monkeypatch):
    from app import config

    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER", "STRONG_MODEL",
                "STRONG_FALLBACK_MODELS", "CHEAP_MODEL", "CHEAP_FALLBACK_MODELS", "OPENAI_STRONG_MODEL",
                "GEMINI_STRONG_MODEL", "OPENAI_CHEAP_MODEL", "GEMINI_CHEAP_MODEL", "OPENAI_STRONG_FALLBACK_MODELS",
                "GEMINI_STRONG_FALLBACK_MODELS", "OPENAI_CHEAP_FALLBACK_MODELS", "GEMINI_CHEAP_FALLBACK_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_MOCK", "0")

    def fresh():
        config.get_settings.cache_clear()
        try:
            return config.get_settings()
        finally:
            config.get_settings.cache_clear()

    monkeypatch.setenv("GEMINI_API_KEY", "g")
    s = fresh()
    assert s.llm_provider == "gemini" and s.strong_model.startswith("gemini") and not s.mock
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    s = fresh()
    assert s.llm_provider == "openai" and s.strong_model == "gpt-6-luna" and s.providers == ["openai", "gemini"]
    monkeypatch.setenv("STRONG_FALLBACK_MODELS", "gemini-3.8-flash")
    monkeypatch.setenv("OPENAI_STRONG_FALLBACK_MODELS", "")
    assert fresh().strong_fallbacks == []
    monkeypatch.delenv("STRONG_FALLBACK_MODELS")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_STRONG_MODEL", "gemini-4-neon")
    s = fresh()
    assert s.llm_provider == "gemini" and s.strong_model == "gemini-4-neon"
    assert s.cheap_model == config.DEFAULT_MODELS["gemini"]["CHEAP_MODEL"]
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("STRONG_MODEL", "gemini-3.8-flash")  # legacy generic variable still honoured
    monkeypatch.delenv("GEMINI_STRONG_MODEL")
    assert fresh().strong_model == "gemini-3.8-flash"
