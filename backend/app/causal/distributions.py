"""Probability distributions with an explicit, documented parametrisation.

All sampling goes through the inverse CDF of a stored uniform ``U`` (common random numbers): the
same ``U`` with a different parameter gives a comparable realisation (``epsilon = sigma * z``).
Parameters may be scalars or per-row arrays (e.g. a Poisson rate computed from parents).

Parametrisation (``Distribution.params``):

=================  ==========================================================================
family             parameters
=================  ==========================================================================
normal             ``mean`` (default 0), ``sd`` (> 0)
uniform            ``low``, ``high`` (high > low)
lognormal          ``log_mean`` and ``log_sd`` = mean/sd of log(X)  **or**  ``mean`` and ``sd`` of
                   X itself (converted:  s² = ln(1 + sd²/mean²),  mu = ln(mean) − s²/2)
exponential        ``rate`` (> 0) **or** ``mean`` (= 1/rate)
gamma              ``shape`` and ``scale`` **or** ``shape`` and ``rate`` **or** ``mean`` and ``sd``
bernoulli          ``p`` in [0, 1]
binomial           ``n`` (int >= 0), ``p``
poisson            ``rate`` (lambda >= 0)
negative_binomial  ``mean`` (> 0) and ``dispersion`` k (> 0): Var = mean + mean²/k
categorical        ``categories`` + ``probs`` (normalised if they sum to 1 ± 1e-6)
beta               ``alpha``, ``beta`` (> 0)
student_t          ``df`` (> 0), ``loc`` (default 0), ``scale`` (default 1)
constant           ``value``
=================  ==========================================================================
"""
from __future__ import annotations

from typing import Any, Literal, Optional

import numpy as np
from pydantic import BaseModel, Field
from scipy import stats

Family = Literal["normal", "uniform", "lognormal", "exponential", "gamma", "bernoulli", "binomial", "poisson",
                 "negative_binomial", "categorical", "beta", "student_t", "constant"]

DISCRETE = {"bernoulli", "binomial", "poisson", "negative_binomial", "categorical"}


class DistributionError(ValueError):
    pass


class Distribution(BaseModel):
    family: Family
    params: dict[str, float] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list, description="categorical only")
    probs: list[float] = Field(default_factory=list, description="categorical only")

    def describe(self) -> str:
        if self.family == "categorical":
            return "Categorical(" + ", ".join(f"{c}: {p:g}" for c, p in zip(self.categories, self.probs)) + ")"
        return f"{self.family}(" + ", ".join(f"{k}={v:g}" for k, v in self.params.items()) + ")"


def _p(params: dict[str, Any], name: str, default: Any = None) -> Any:
    v = params.get(name, default)
    if v is None:
        raise DistributionError(f"missing parameter '{name}'")
    return np.asarray(v, dtype=float)


def canonical(family: str, params: dict[str, Any]) -> dict[str, Any]:
    """Convert alternative parametrisations to the canonical one and validate the values."""
    p = dict(params)
    if family == "normal":
        sd = _p(p, "sd")
        if np.any(sd <= 0):
            raise DistributionError("normal: sd must be > 0")
        return {"mean": _p(p, "mean", 0.0), "sd": sd}
    if family == "uniform":
        lo, hi = _p(p, "low"), _p(p, "high")
        if np.any(hi <= lo):
            raise DistributionError("uniform: high must be > low")
        return {"low": lo, "high": hi}
    if family == "lognormal":
        if "log_mean" in p or "log_sd" in p:
            mu, s = _p(p, "log_mean"), _p(p, "log_sd")
        else:
            m, sd = _p(p, "mean"), _p(p, "sd")
            if np.any(m <= 0) or np.any(sd <= 0):
                raise DistributionError("lognormal: mean and sd of X must be > 0")
            s2 = np.log1p(sd ** 2 / m ** 2)
            mu, s = np.log(m) - s2 / 2, np.sqrt(s2)
        if np.any(s <= 0):
            raise DistributionError("lognormal: log_sd must be > 0")
        return {"log_mean": mu, "log_sd": s}
    if family == "exponential":
        rate = _p(p, "rate") if "rate" in p else 1.0 / _p(p, "mean")
        if np.any(rate <= 0):
            raise DistributionError("exponential: rate must be > 0")
        return {"rate": rate}
    if family == "gamma":
        if "mean" in p and "sd" in p and "shape" not in p:
            m, sd = _p(p, "mean"), _p(p, "sd")
            shape, scale = (m / sd) ** 2, sd ** 2 / m
        else:
            shape = _p(p, "shape")
            scale = _p(p, "scale") if "scale" in p else 1.0 / _p(p, "rate")
        if np.any(shape <= 0) or np.any(scale <= 0):
            raise DistributionError("gamma: shape and scale must be > 0")
        return {"shape": shape, "scale": scale}
    if family == "bernoulli":
        pr = _p(p, "p")
        if np.any((pr < 0) | (pr > 1)):
            raise DistributionError("bernoulli: p must be in [0, 1]")
        return {"p": pr}
    if family == "binomial":
        n, pr = _p(p, "n"), _p(p, "p")
        if np.any(n < 0) or np.any(n != np.round(n)) or np.any((pr < 0) | (pr > 1)):
            raise DistributionError("binomial: n must be a non-negative integer and p in [0, 1]")
        return {"n": n, "p": pr}
    if family == "poisson":
        rate = _p(p, "rate")
        if np.any(rate < 0) or not np.all(np.isfinite(rate)):
            raise DistributionError("poisson: rate must be finite and >= 0")
        return {"rate": rate}
    if family == "negative_binomial":
        m, k = _p(p, "mean"), _p(p, "dispersion")
        if np.any(m <= 0) or np.any(k <= 0):
            raise DistributionError("negative_binomial: mean and dispersion must be > 0")
        return {"mean": m, "dispersion": k}
    if family == "beta":
        a, b = _p(p, "alpha"), _p(p, "beta")
        if np.any(a <= 0) or np.any(b <= 0):
            raise DistributionError("beta: alpha and beta must be > 0")
        return {"alpha": a, "beta": b}
    if family == "student_t":
        df = _p(p, "df")
        scale = _p(p, "scale", 1.0)
        if np.any(df <= 0) or np.any(scale <= 0):
            raise DistributionError("student_t: df and scale must be > 0")
        return {"df": df, "loc": _p(p, "loc", 0.0), "scale": scale}
    if family == "constant":
        return {"value": _p(p, "value")}
    raise DistributionError(f"unsupported distribution family '{family}'")


def scipy_dist(family: str, params: dict[str, Any]):
    """Frozen scipy distribution in canonical parametrisation (None for categorical/constant)."""
    c = canonical(family, params)
    if family == "normal":
        return stats.norm(loc=c["mean"], scale=c["sd"])
    if family == "uniform":
        return stats.uniform(loc=c["low"], scale=c["high"] - c["low"])
    if family == "lognormal":
        return stats.lognorm(s=c["log_sd"], scale=np.exp(c["log_mean"]))
    if family == "exponential":
        return stats.expon(scale=1.0 / c["rate"])
    if family == "gamma":
        return stats.gamma(a=c["shape"], scale=c["scale"])
    if family == "bernoulli":
        return stats.bernoulli(p=c["p"])
    if family == "binomial":
        return stats.binom(n=c["n"], p=c["p"])
    if family == "poisson":
        return stats.poisson(mu=c["rate"])
    if family == "negative_binomial":
        # scipy nbinom(n, p): mean = n(1-p)/p  ->  n = k, p = k / (k + mean)
        return stats.nbinom(n=c["dispersion"], p=c["dispersion"] / (c["dispersion"] + c["mean"]))
    if family == "beta":
        return stats.beta(a=c["alpha"], b=c["beta"])
    if family == "student_t":
        return stats.t(df=c["df"], loc=c["loc"], scale=c["scale"])
    return None


def check_categorical(categories: list[str], probs: list[float]) -> np.ndarray:
    if not categories:
        raise DistributionError("categorical: categories are empty")
    if len(set(categories)) != len(categories):
        raise DistributionError("categorical: duplicate categories")
    if len(probs) != len(categories):
        raise DistributionError("categorical: probs must have one value per category")
    p = np.asarray(probs, dtype=float)
    if np.any(p < 0) or not np.isfinite(p).all():
        raise DistributionError("categorical: probabilities must be >= 0")
    s = p.sum()
    if abs(s - 1) > 1e-6:
        if s <= 0 or abs(s - 1) > 0.05:
            raise DistributionError(f"categorical: probabilities sum to {s:.4f}, expected 1")
        p = p / s
    return p


def categorical_from_u(u: np.ndarray, categories: list[str], probs: np.ndarray) -> np.ndarray:
    """Inverse CDF of a categorical distribution. ``probs`` is (k,) or (n, k)."""
    probs = np.asarray(probs, dtype=float)
    cats = np.asarray(categories, dtype=object)
    if probs.ndim == 1:
        cum = np.cumsum(probs)
        cum[-1] = 1.0
        idx = np.searchsorted(cum, u, side="right")
    else:
        cum = np.cumsum(probs, axis=1)
        cum[:, -1] = 1.0
        idx = (u[:, None] >= cum).sum(axis=1)
    return cats[np.minimum(idx, len(cats) - 1)]


def sample(dist: Distribution, u: np.ndarray, overrides: Optional[dict[str, Any]] = None,
           scale: float = 1.0) -> np.ndarray:
    """Realisation from stored uniforms ``u``. ``scale`` multiplies the spread of zero-centred noise
    (normal sd, student_t scale) – used for structural-noise scaling with common random numbers."""
    params = {**dist.params, **(overrides or {})}
    fam = dist.family
    if fam == "categorical":
        return categorical_from_u(u, dist.categories, check_categorical(dist.categories, dist.probs))
    if fam == "constant":
        return np.broadcast_to(canonical(fam, params)["value"], u.shape).astype(float).copy()
    if fam == "normal":
        c = canonical(fam, params)
        return c["mean"] + c["sd"] * scale * stats.norm.ppf(u)
    if fam == "student_t":
        c = canonical(fam, params)
        return c["loc"] + c["scale"] * scale * stats.t.ppf(u, c["df"])
    d = scipy_dist(fam, params)
    out = d.ppf(u)
    if fam in DISCRETE:
        out = np.nan_to_num(out, nan=0.0, posinf=0.0)
    if scale != 1.0 and fam not in DISCRETE:
        m = d.mean()
        out = m + (out - m) * scale
    return out


def mean_of(dist: Distribution) -> float:
    if dist.family == "categorical":
        return float("nan")
    if dist.family == "constant":
        return float(dist.params.get("value", 0.0))
    return float(np.mean(scipy_dist(dist.family, dist.params).mean()))


def sd_of(dist: Distribution) -> float:
    if dist.family in ("categorical", "constant"):
        return 0.0
    return float(np.mean(scipy_dist(dist.family, dist.params).std()))


def validate_distribution(dist: Distribution) -> list[str]:
    try:
        if dist.family == "categorical":
            check_categorical(dist.categories, dist.probs)
        else:
            canonical(dist.family, dist.params)
    except DistributionError as e:
        return [str(e)]
    return []
