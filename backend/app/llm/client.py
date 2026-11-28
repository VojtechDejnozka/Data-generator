"""LLM client with two roles (STRONG / CHEAP) on top of the official OpenAI and google-genai SDKs.

Each model is routed to its provider by name (``gemini-*`` -> Gemini API, otherwise OpenAI Responses API),
so fallback chains may even mix providers; models whose provider has no API key are skipped.

Features: structured output (JSON schema derived from Pydantic), validation + retry with the error
message, exponential backoff on 429/5xx, model fallback chains (free-tier keys get quota 0 on
some models), bounded concurrency, on-disk cache by input hash, token/cost accounting per run,
and a mock mode with fixture responders.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings, get_settings, provider_for_model
from ..storage.text_files import read_saved_text
from .pricing import estimate_cost
from .schema_utils import gemini_schema

log = logging.getLogger("llm")
T = TypeVar("T", bound=BaseModel)
Role = Literal["strong", "cheap"]

MAX_VALIDATION_RETRIES = 2
MAX_TRANSPORT_ATTEMPTS = 4


class LLMError(RuntimeError):
    pass


class _ApiError(Exception):
    """Provider-neutral HTTP error from an LLM API."""

    def __init__(self, code: int, msg: str, *, quota_zero: bool = False, schema_rejected: bool = False):
        super().__init__(msg)
        self.code, self.msg, self.quota_zero, self.schema_rejected = code, msg, quota_zero, schema_rejected


class _NetworkError(Exception):
    pass


@dataclass
class _Reply:
    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0  # incl. thinking/reasoning tokens
    thoughts_tokens: int = 0
    incomplete: str = ""    # non-empty when the response was cut off


# --------------------------------------------------------------------------- usage accounting
@dataclass
class CallRecord:
    role: str
    task: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    cached: bool = False
    ok: bool = True
    attempts: int = 1
    error: str = ""
    cache_key: str = ""


@dataclass
class UsageTracker:
    calls: list[CallRecord] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)  # cache_key -> raw JSON (for replay)
    #: enclosing tracker (e.g. the job-level tracker used for quotas); every record propagates upwards
    parent: Optional["UsageTracker"] = field(default=None, repr=False, compare=False)

    def add(self, rec: CallRecord, response: Any | None = None) -> None:
        self.calls.append(rec)
        if response is not None and rec.cache_key:
            self.responses[rec.cache_key] = response
        if self.parent is not None:
            self.parent.add(rec, None)

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> "UsageTracker":
        t = cls()
        t.calls = [CallRecord(**r) for r in records]
        return t

    def summary(self) -> dict[str, Any]:
        by: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            key = f"{c.role}:{c.model}"
            b = by.setdefault(key, {"role": c.role, "model": c.model, "calls": 0, "cached": 0, "failed": 0,
                                    "prompt_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "cost_usd": 0.0})
            b["calls"] += 1
            b["cached"] += int(c.cached)
            b["failed"] += int(not c.ok)
            b["prompt_tokens"] += c.prompt_tokens
            b["output_tokens"] += c.output_tokens
            b["thoughts_tokens"] += c.thoughts_tokens
            b["cost_usd"] = round(b["cost_usd"] + c.cost_usd, 6)
        return {
            "total_calls": len(self.calls),
            "total_cost_usd": round(sum(c.cost_usd for c in self.calls), 6),
            "total_tokens": sum(c.prompt_tokens + c.output_tokens for c in self.calls),
            "by_model": list(by.values()),
        }

    def records(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.calls]

    def records_since(self, n: int) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.calls[n:]]


_tracker: contextvars.ContextVar[Optional[UsageTracker]] = contextvars.ContextVar("usage_tracker", default=None)


@contextlib.contextmanager
def usage_scope(tracker: UsageTracker):
    outer = _tracker.get()
    if outer is not None and outer is not tracker and tracker.parent is None:
        tracker.parent = outer
    token = _tracker.set(tracker)
    try:
        yield tracker
    finally:
        _tracker.reset(token)


def current_tracker() -> Optional[UsageTracker]:
    return _tracker.get()


# --------------------------------------------------------------------------- client
def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if m:
            return json.loads(m.group(1))
        raise


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = None  # OpenAI SDK (lazy)
        self._gemini = None  # google-genai SDK (lazy)
        self._sems: dict[int, asyncio.Semaphore] = {}
        self._dead_models: dict[str, float] = {}  # model -> until timestamp

    # -- infra ---------------------------------------------------------------
    @property
    def mock(self) -> bool:
        return self.settings.mock

    def _sdk(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key,
                                       timeout=self.settings.request_timeout_s, max_retries=0)
        return self._client

    def _gemini_sdk(self):
        if self._gemini is None:
            from google import genai
            from google.genai import types

            self._gemini = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(self.settings.request_timeout_s * 1000)),
            )
        return self._gemini

    def _sem(self) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._sems:
            self._sems[loop_id] = asyncio.Semaphore(self.settings.max_concurrency)
        return self._sems[loop_id]

    def models_for(self, role: Role) -> list[str]:
        s = self.settings
        chain = [s.strong_model, *s.strong_fallbacks] if role == "strong" else [s.cheap_model, *s.cheap_fallbacks]
        now = time.time()
        seen: list[str] = []
        for m in chain:
            if m and m not in seen and self._dead_models.get(m, 0) < now and s.api_key_for(provider_for_model(m)):
                seen.append(m)
        return seen or [chain[0]]

    def _cache_path(self, key: str):
        return self.settings.cache_dir / f"{key}.json"

    # -- public API ------------------------------------------------------------
    async def structured(
        self,
        *,
        role: Role,
        task: str,
        prompt: str,
        schema: type[T],
        system: str | None = None,
        mock_context: dict[str, Any] | None = None,
        temperature: float | None = None,
        use_cache: bool = True,
        json_mode: bool = False,
        max_validation_retries: int | None = None,
    ) -> T:
        """Call the model for `role` and return a validated instance of `schema`.

        ``json_mode=True`` skips the API response schema and puts the JSON schema into the prompt instead –
        for contracts with free-form parameter objects that structured output cannot express.
        ``max_validation_retries`` overrides the default number of repair rounds with the error message."""
        schema_json = json.dumps(schema.model_json_schema(), sort_keys=True)
        models = [f"{provider_for_model(m)}:{m}" for m in self.models_for(role)]
        key = _hash("llm-v2", *models, role, task, system or "", prompt, schema_json, str(temperature),
                    "json" if json_mode else "")
        tracker = current_tracker()

        if self.mock:
            from .mock import mock_response

            t0 = time.time()
            raw = mock_response(task, prompt, schema, mock_context or {})
            obj = schema.model_validate(raw)
            rec = CallRecord(role=role, task=task, model="mock", latency_s=round(time.time() - t0, 3),
                             cache_key=key, prompt_tokens=len(prompt) // 4,
                             output_tokens=len(json.dumps(raw, ensure_ascii=False)) // 4)
            if tracker:
                tracker.add(rec, raw)
            return obj

        if use_cache and self.settings.llm_cache and self._cache_path(key).exists():
            try:
                cached = json.loads(read_saved_text(self._cache_path(key)))
                obj = schema.model_validate(cached["response"])
                if tracker:
                    tracker.add(CallRecord(role=role, task=task, model=cached.get("model", "?"), cached=True,
                                           cache_key=key), cached["response"])
                return obj
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
                pass  # regenerate an incomplete or stale cache entry

        async with self._sem():
            obj, raw, rec = await self._call_with_retries(role, task, prompt, schema, system, temperature, key,
                                                          json_mode, max_validation_retries)
        if tracker:
            tracker.add(rec, raw)
        if self.settings.llm_cache:
            self._cache_path(key).write_text(json.dumps({"model": rec.model, "task": task, "response": raw},
                                                        ensure_ascii=False), encoding="utf-8")
        return obj

    # -- provider adapters -------------------------------------------------------
    async def _call_openai(self, model: str, prompt: str, system: str | None, schema: type[BaseModel],
                           schema_dict: dict[str, Any] | None, temperature: float | None) -> _Reply:
        from openai import APIConnectionError, APIStatusError

        fmt: dict[str, Any] = {"type": "json_object"}
        if schema_dict is not None:
            fmt = {"type": "json_schema", "name": schema.__name__, "schema": schema_dict, "strict": False}
        options: dict[str, Any] = {}
        if system:
            options["instructions"] = system
        if temperature is not None and model.startswith(("gpt-4", "gpt-3.5")):
            options["temperature"] = temperature
        try:
            resp = await self._sdk().responses.create(model=model, input=prompt, text={"format": fmt},
                                                      store=False, **options)
        except APIStatusError as e:
            msg = str(e)
            code = e.status_code
            raise _ApiError(code, msg, quota_zero=code == 429 and e.code == "insufficient_quota",
                            schema_rejected=code == 400 and ("schema" in msg.lower() or e.param == "text.format"))
        except APIConnectionError as e:
            raise _NetworkError(f"{type(e).__name__} {e}") from e
        usage = resp.usage
        reply = _Reply(text=resp.output_text or "")
        if usage:
            reply.prompt_tokens = usage.input_tokens or 0
            reply.output_tokens = usage.output_tokens or 0
            if usage.output_tokens_details:
                reply.thoughts_tokens = usage.output_tokens_details.reasoning_tokens or 0
        if resp.status != "completed":
            reply.incomplete = getattr(resp.incomplete_details, "reason", None) or resp.status or "incomplete"
        return reply

    async def _call_gemini(self, model: str, prompt: str, system: str | None,
                           schema_dict: dict[str, Any] | None, temperature: float | None) -> _Reply:
        import httpx
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema_dict,
            temperature=temperature,
        )
        try:
            resp = await self._gemini_sdk().aio.models.generate_content(model=model, contents=prompt, config=cfg)
        except errors.APIError as e:
            msg = str(e)
            code = getattr(e, "code", 0) or 0
            raise _ApiError(code, msg, quota_zero=code == 429 and "limit: 0" in msg,
                            schema_rejected=code == 400 and "schema" in msg.lower())
        except httpx.TransportError as e:
            raise _NetworkError(f"{type(e).__name__} {e}") from e
        usage = resp.usage_metadata
        thoughts = (usage.thoughts_token_count or 0) if usage else 0
        reply = _Reply(text=resp.text or "", thoughts_tokens=thoughts,
                       prompt_tokens=(usage.prompt_token_count or 0) if usage else 0,
                       output_tokens=((usage.candidates_token_count or 0) + thoughts) if usage else 0)
        cands = getattr(resp, "candidates", None) or []
        reason = getattr(cands[0], "finish_reason", None) if cands else None
        reason = getattr(reason, "name", reason)
        if reason and str(reason) not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            reply.incomplete = str(reason)
        return reply

    # -- internals ---------------------------------------------------------------
    async def _call_with_retries(self, role, task, prompt, schema, system, temperature, key, json_mode=False,
                                 max_validation_retries=None):
        errors_seen: list[str] = []
        total = CallRecord(role=role, task=task, model="", cache_key=key, attempts=0)
        t0 = time.time()

        retries = MAX_VALIDATION_RETRIES if max_validation_retries is None else max_validation_retries
        for model in self.models_for(role):
            provider = provider_for_model(model)
            if not self.settings.api_key_for(provider):
                errors_seen.append(f"{model}: missing {provider.upper()}_API_KEY")
                continue
            schema_dict = gemini_schema(schema) if provider == "gemini" else schema.model_json_schema()
            use_schema = not json_mode
            feedback: list[str] = []
            transport_attempt = 0
            validation_attempt = 0
            while True:
                total.attempts += 1
                full_prompt = prompt
                if not use_schema:
                    full_prompt += "\n\nReturn ONLY JSON matching this JSON schema:\n" + json.dumps(schema_dict)
                if feedback:
                    full_prompt += (
                        "\n\nYour previous answer was invalid. Fix these problems and answer again:\n- "
                        + "\n- ".join(feedback[-2:])
                    )
                try:
                    if provider == "gemini":
                        reply = await self._call_gemini(model, full_prompt, system,
                                                        schema_dict if use_schema else None, temperature)
                    else:
                        reply = await self._call_openai(model, full_prompt, system, schema,
                                                        schema_dict if use_schema else None, temperature)
                except _ApiError as e:
                    msg, code = e.msg, e.code
                    errors_seen.append(f"{model}: {code} {msg[:160]}")
                    log.warning("LLM %s %s attempt %d failed: %s", model, task, transport_attempt, msg[:200])
                    if e.quota_zero:
                        self._dead_models[model] = time.time() + 3600
                        break  # no quota on this model at all -> next model
                    if e.schema_rejected and use_schema:
                        use_schema = False  # schema too complex -> plain JSON mode
                        continue
                    if code in (404, 403):
                        self._dead_models[model] = time.time() + 3600
                        break
                    if code in (408, 429, 500, 502, 503, 504) and transport_attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                        delay = min(30.0, 2 ** (transport_attempt + 1)) + random.random()
                        m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
                        if m and float(m.group(1)) < 30:
                            delay = float(m.group(1)) + 0.5
                        transport_attempt += 1
                        await asyncio.sleep(delay)
                        continue
                    break  # give up on this model
                except (_NetworkError, asyncio.TimeoutError, OSError) as e:
                    errors_seen.append(f"{model}: {type(e).__name__} {e}")
                    if transport_attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                        transport_attempt += 1
                        await asyncio.sleep(2 ** transport_attempt)
                        continue
                    break

                total.model = model
                total.prompt_tokens += reply.prompt_tokens
                total.output_tokens += reply.output_tokens
                total.thoughts_tokens += reply.thoughts_tokens
                total.cost_usd = round(total.cost_usd + estimate_cost(model, reply.prompt_tokens, reply.output_tokens), 6)
                try:
                    if reply.incomplete:
                        raise ValueError(f"{provider} response did not complete ({reply.incomplete})")
                    raw = _extract_json(reply.text)
                    obj = schema.model_validate(raw)
                except (json.JSONDecodeError, ValidationError, ValueError) as e:
                    err = str(e)[:1500]
                    errors_seen.append(f"{model}: invalid output: {err[:200]}")
                    if validation_attempt < retries:
                        validation_attempt += 1
                        feedback.append(err)
                        continue
                    break
                total.latency_s = round(time.time() - t0, 2)
                total.error = "; ".join(errors_seen[-3:])
                return obj, raw, total

        total.ok = False
        total.latency_s = round(time.time() - t0, 2)
        total.error = "; ".join(errors_seen[-4:])
        tracker = current_tracker()
        if tracker:
            tracker.add(total)
        raise LLMError(f"{task}: all models failed – {total.error}")


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm(settings: Settings | None = None) -> LLMClient:
    global _client
    _client = LLMClient(settings)
    return _client
