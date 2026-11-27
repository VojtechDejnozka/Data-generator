"""Reproducible, independent random streams.

Every random component gets its own ``numpy.random.Generator`` derived from the run seed and a
*stable* textual identifier through ``SeedSequence(spawn_key=...)``. Python's ``hash()`` is salted
per process and therefore never used. Changing one variable or one corruption operation does not
shift the random draws of any other component.
"""
from __future__ import annotations

import hashlib

import numpy as np


def stable_words(*names: object) -> tuple[int, ...]:
    """Four uint32 words derived from the identifiers (sha256, process independent)."""
    h = hashlib.sha256("\x1f".join(str(n) for n in names).encode("utf-8")).digest()
    return tuple(int.from_bytes(h[i:i + 4], "little") for i in range(0, 16, 4))


def stream(seed: int, *names: object) -> np.random.Generator:
    """Independent generator for component ``names`` of a run with ``seed``."""
    ss = np.random.SeedSequence(entropy=int(seed) & 0xFFFFFFFFFFFFFFFF, spawn_key=stable_words(*names))
    return np.random.default_rng(ss)


def uniforms(seed: int, n: int, *names: object) -> np.ndarray:
    """``n`` uniforms in the open interval (0, 1) – the common random numbers of a component.

    Draws are prefix-stable: the first ``k`` values do not depend on ``n``.
    """
    u = stream(seed, *names).random(n)
    return np.clip(u, 1e-12, 1 - 1e-12)


def stable_int(*names: object, mod: int = 2**31 - 1) -> int:
    return stable_words(*names)[0] % mod
