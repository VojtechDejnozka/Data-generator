"""Effect primitives used by scenario modules.

Every primitive maps a day index / date array to a multiplicative or additive signal. Modules
compose them to drive mechanisms (demand, delays, prices) instead of writing final KPIs.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal, Sequence

import numpy as np
import pandas as pd

RampProfile = Literal["step", "linear", "exponential", "sigmoid"]


def day_index(dates: pd.DatetimeIndex, ref: dt.date | str) -> np.ndarray:
    """Days since `ref` (negative before it)."""
    ref_ts = pd.Timestamp(ref)
    return ((dates - ref_ts) / pd.Timedelta(days=1)).to_numpy(dtype=float)


def ramp(t: np.ndarray, duration: float, profile: RampProfile = "linear") -> np.ndarray:
    """0 before t=0, rises to 1 over `duration` days following `profile`."""
    t = np.asarray(t, dtype=float)
    x = np.clip(t / max(duration, 1e-9), 0.0, 1.0)
    if profile == "step":
        y = (t >= 0).astype(float)
    elif profile == "linear":
        y = x
    elif profile == "exponential":
        y = (np.exp(3 * x) - 1) / (np.exp(3) - 1)
    elif profile == "sigmoid":
        y = 1 / (1 + np.exp(-10 * (x - 0.5)))
        y = (y - 1 / (1 + np.exp(5))) / (1 / (1 + np.exp(-5)) - 1 / (1 + np.exp(5)))
    else:
        raise ValueError(f"unknown ramp profile {profile}")
    return np.where(t < 0, 0.0, np.clip(y, 0.0, 1.0))


def step(t: np.ndarray, magnitude: float, end: float | None = None) -> np.ndarray:
    """`magnitude` from t>=0 (until `end` days if given), else 0."""
    t = np.asarray(t, dtype=float)
    on = t >= 0
    if end is not None:
        on &= t < end
    return np.where(on, magnitude, 0.0)


def trend(t: np.ndarray, slope_per_year: float) -> np.ndarray:
    """Multiplicative drift: 1 + slope * years."""
    return 1.0 + slope_per_year * np.asarray(t, dtype=float) / 365.25


def yearly_seasonality(dates: pd.DatetimeIndex, amplitude: float, peak_doy: int = 200) -> np.ndarray:
    doy = dates.dayofyear.to_numpy()
    return 1.0 + amplitude * np.cos(2 * np.pi * (doy - peak_doy) / 365.25)


def weekday_profile(dates: pd.DatetimeIndex, weights: Sequence[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    return w[dates.day_of_week.to_numpy()]


def month_profile(dates: pd.DatetimeIndex, weights: Sequence[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    return w[dates.month.to_numpy() - 1]


def window(dates: pd.DatetimeIndex, start: str | dt.date, end: str | dt.date, inside: float, outside: float = 1.0) -> np.ndarray:
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    return np.where((dates >= s) & (dates <= e), inside, outside)


def lognormal_noise(rng: np.random.Generator, shape, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return np.ones(shape)
    return rng.lognormal(mean=-0.5 * sigma**2, sigma=sigma, size=shape)


@dataclass
class EffectSpec:
    """Declarative effect usable from configuration: kind + parameters."""

    kind: Literal["ramp", "step", "trend", "yearly", "weekday", "window"]
    params: dict = field(default_factory=dict)
    mode: Literal["mul", "add"] = "mul"


def apply_effects(base: np.ndarray, dates: pd.DatetimeIndex, effects: Sequence[EffectSpec]) -> np.ndarray:
    """Apply a list of declarative effects to a base series (broadcast over last axis = days)."""
    out = np.asarray(base, dtype=float).copy()
    for ef in effects:
        p = ef.params
        if ef.kind == "ramp":
            sig = p.get("magnitude", 1.0) * ramp(day_index(dates, p["start"]), p.get("duration", 30), p.get("profile", "linear"))
        elif ef.kind == "step":
            sig = step(day_index(dates, p["start"]), p.get("magnitude", 1.0), p.get("duration"))
        elif ef.kind == "trend":
            sig = trend(day_index(dates, p.get("start", dates[0])), p["slope_per_year"])
        elif ef.kind == "yearly":
            sig = yearly_seasonality(dates, p["amplitude"], p.get("peak_doy", 200))
        elif ef.kind == "weekday":
            sig = weekday_profile(dates, p["weights"])
        elif ef.kind == "window":
            sig = window(dates, p["start"], p["end"], p["inside"], p.get("outside", 1.0 if ef.mode == "mul" else 0.0))
        else:  # pragma: no cover
            raise ValueError(ef.kind)
        if ef.mode == "mul":
            out = out * (sig if ef.kind not in ("ramp", "step") else 1.0 + sig)
        else:
            out = out + sig
    return out


def streams(seed: int, *names: str) -> dict[str, np.random.Generator]:
    """Independent RNG streams per component: changing one mechanism never shifts another's draws."""
    import hashlib

    out = {}
    for name in names:
        sid = int(hashlib.md5(name.encode()).hexdigest()[:8], 16)
        out[name] = np.random.default_rng([seed, sid])
    return out
