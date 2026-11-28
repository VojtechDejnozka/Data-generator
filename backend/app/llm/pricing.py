"""Estimated standard OpenAI text prices in USD per million tokens."""
from __future__ import annotations

PRICES = {"gpt-6-luna": (0.10, 0.50), "mock": (0.0, 0.0)}
_DEFAULT = (1.0, 5.0)


def estimate_cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, _DEFAULT)
    return round(prompt_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
