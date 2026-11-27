"""Test configuration: always mock mode, isolated data/cache dirs."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    from app import config
    from app.llm import client

    config.get_settings.cache_clear()
    client.reset_llm()
    yield
    config.get_settings.cache_clear()
    client.reset_llm()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def retail_dataset(tmp_path_factory):
    """Dimensions + simulation once per session (mock master data)."""
    os.environ["LLM_MOCK"] = "1"
    os.environ["CACHE_DIR"] = str(tmp_path_factory.mktemp("cache"))
    from app import config
    from app.llm import client

    config.get_settings.cache_clear()
    client.reset_llm()
    from app.contracts.io import ScenarioInput
    from app.generation.scenarios.base import GenContext
    from app.generation.scenarios.registry import RETAIL

    inp = ScenarioInput(company="Test", environment="Česko", story="x",
                        entity_counts={"stores": 5, "products": 80, "customers": 300},
                        period_start="2025-01-01", period_end="2025-12-31", volume="small")
    spec = RETAIL.default_spec(inp)
    dims = asyncio.run(RETAIL.build_dimensions(spec, GenContext(seed=7, knobs={})))
    return spec, dims
