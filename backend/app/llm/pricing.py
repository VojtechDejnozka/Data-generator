"""Approximate list prices (USD per 1M tokens, paid tier, text). Used only for cost *estimates* in run logs.

Gemini prices from aistudio.google.com/docs/pricing (checked 2026-11-27; output includes thinking tokens),
OpenAI prices are estimated standard text prices.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-6-luna": (0.10, 0.50),
    # Gemini
    "gemini-4-carbon": (2.00, 10.00),  # rises to 4.00/20.00 from 2026-12-01
    "gemini-4-neon": (3.00, 15.00),
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "mock": (0.0, 0.0),
}
_DEFAULT = (1.0, 5.0)


def estimate_cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, _DEFAULT)
    return round(prompt_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
