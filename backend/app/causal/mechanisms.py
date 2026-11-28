"""Extensible registry of structural mechanisms ``X = f(PA, U)``.

Every mechanism provides

* a parameter schema (Pydantic model, ``params_model``),
* input / output compatibility rules (``output_types`` and ``check``),
* generation (``generate``) from aligned parent arrays and stored uniforms (common random numbers),
* a human readable description (``describe``).

Mechanistic validation lives separately in ``app.causal.validation`` and re-computes the expected
values through an independent path (relational joins on the materialised tables).

Linear predictors are written either as ``intercept`` + ``coefficients`` over *terms* or as a
restricted ``predictor`` expression. Term syntax: ``x`` (numeric parent), ``x=premium`` (indicator
of a category), ``x*z`` (interaction), ``x^2`` (power, constant exponent <= 10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Optional

import numpy as np
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import distributions as D
from .expressions import ExpressionError, check_expr, evaluate

# Names available to mechanism expressions in addition to the parent aliases.
TIME_NAMES = {"t", "dow", "month", "doy", "step_frac"}


class MechanismError(ValueError):
    pass


# =========================================================================== shared helpers
def _term_value(term: str, env: dict[str, Any], n: int) -> np.ndarray:
    parts = [p.strip() for p in term.split("*")]
    out = np.ones(n)
    for p in parts:
        if "=" in p:
            name, cat = (s.strip() for s in p.split("=", 1))
            if name not in env:
                raise MechanismError(f"term '{term}': unknown parent '{name}'")
            out = out * (np.asarray(env[name]).astype(str) == cat).astype(float)
        elif "^" in p:
            name, ex = (s.strip() for s in p.split("^", 1))
            if name not in env:
                raise MechanismError(f"term '{term}': unknown parent '{name}'")
            k = float(ex)
            if abs(k) > 10:
                raise MechanismError(f"term '{term}': exponent too large")
            out = out * np.power(np.asarray(env[name], dtype=float), k)
        else:
            if p not in env:
                raise MechanismError(f"term '{term}': unknown parent '{p}'")
            out = out * np.asarray(env[p], dtype=float)
    return out


def term_names(term: str) -> list[str]:
    names = []
    for p in term.split("*"):
        p = p.strip()
        names.append(p.split("=", 1)[0].split("^", 1)[0].strip())
    return names


class LinearPredictor(BaseModel):
    intercept: float = 0.0
    coefficients: dict[str, float] = Field(default_factory=dict, description="term -> coefficient")
    predictor: Optional[str] = Field(None, description="restricted expression used instead of the terms")

    def value(self, env: dict[str, Any], n: int) -> np.ndarray:
        if self.predictor:
            return np.asarray(evaluate(self.predictor, env, n), dtype=float)
        out = np.full(n, float(self.intercept))
        for term, coef in self.coefficients.items():
            out = out + float(coef) * _term_value(term, env, n)
        return out

    def names(self) -> set[str]:
        from .expressions import compile_expr
        if self.predictor:
            try:
                return set(compile_expr(self.predictor).names)
            except ExpressionError:
                return set()
        return {nm for t in self.coefficients for nm in term_names(t)}

    def check(self, allowed: set[str]) -> list[str]:
        if self.predictor:
            return check_expr(self.predictor, allowed)
        errs = []
        for term in self.coefficients:
            for nm in term_names(term):
                if nm not in allowed:
                    errs.append(f"term '{term}' references unknown parent '{nm}'")
        return errs

    def describe(self) -> str:
        if self.predictor:
            return self.predictor
        parts = [f"{self.intercept:g}"] + [f"{c:+g}·{t}" for t, c in self.coefficients.items()]
        return " ".join(parts)


@dataclass
class MechContext:
    """Inputs of one mechanism evaluation over ``n`` rows."""

    var: Any  # VariableSpec
    params: dict[str, Any]
    env: dict[str, Any]  # parent aliases + time names (+ row keys for labels)
    n: int
    uniforms: Callable[[int], np.ndarray]  # k-th uniform stream for these rows
    group_uniforms: Callable[[np.ndarray], np.ndarray] = lambda keys: np.full(len(keys), 0.5)
    noise_scale: float = 1.0
    keys: dict[str, np.ndarray] = field(default_factory=dict)


@dataclass
class MechOutput:
    values: np.ndarray
    aux: dict[str, np.ndarray] = field(default_factory=dict)


class MechanismType:
    id: ClassVar[str]
    title: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    output_types: ClassVar[set[str]]
    deterministic: ClassVar[bool] = False
    requires_parents: ClassVar[bool] = False

    def parse(self, params: dict[str, Any]) -> BaseModel:
        try:
            return self.params_model.model_validate(params or {})
        except ValidationError as e:
            raise MechanismError(f"{self.id}: invalid parameters – {e.errors()[0].get('msg')} at "
                                 f"{'.'.join(str(x) for x in e.errors()[0].get('loc', []))}") from e

    def referenced_names(self, p: BaseModel) -> set[str]:
        return set()

    def check(self, var: Any, parent_names: set[str], parent_types: dict[str, str]) -> list[str]:
        errs: list[str] = []
        try:
            p = self.parse(var.mechanism.params)
        except MechanismError as e:
            return [str(e)]
        if var.data_type not in self.output_types:
            errs.append(f"{var.variable_id}: mechanism {self.id} produces {sorted(self.output_types)}, "
                        f"variable data_type is {var.data_type}")
        if self.requires_parents and not parent_names:
            errs.append(f"{var.variable_id}: mechanism {self.id} needs at least one parent")
        errs += [f"{var.variable_id}: {e}" for e in self.check_params(p, var, parent_names | TIME_NAMES, parent_types)]
        return errs

    def check_params(self, p: BaseModel, var: Any, allowed: set[str], parent_types: dict[str, str]) -> list[str]:
        return []

    def generate(self, p: BaseModel, ctx: MechContext) -> MechOutput:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self, var: Any) -> str:
        return f"{var.variable_id} = {self.id}({', '.join(x.name for x in var.parents)})"

    def coefficient(self, p: BaseModel, parent: str) -> Optional[float]:
        coefs = getattr(p, "coefficients", None)
        if isinstance(coefs, dict) and parent in coefs:
            return float(coefs[parent])
        return None


def _noise(dist: Optional[D.Distribution], ctx: MechContext, k: int = 0) -> np.ndarray:
    if dist is None:
        return np.zeros(ctx.n)
    return np.asarray(D.sample(dist, ctx.uniforms(k), scale=ctx.noise_scale), dtype=float)


def _noise_aux(dist: Optional[D.Distribution], ctx: MechContext, eps: np.ndarray, det: np.ndarray) -> dict:
    aux = {"deterministic": np.asarray(det, dtype=float), "noise": eps}
    if dist is not None:
        aux["u0"] = ctx.uniforms(0)
    return aux


def _check_dist(dist: Optional[D.Distribution]) -> list[str]:
    return D.validate_distribution(dist) if dist is not None else []


# =========================================================================== mechanisms
class ExogenousParams(BaseModel):
    distribution: D.Distribution


class Exogenous(MechanismType):
    id = "exogenous"
    title = "Exogenous variable drawn from a distribution"
    params_model = ExogenousParams
    output_types = {"float", "int", "bool", "category"}

    def check_params(self, p, var, allowed, parent_types):
        errs = _check_dist(p.distribution)
        if var.parents:
            errs.append("exogenous variables have no parents (use a mechanism with parents)")
        if p.distribution.family == "categorical" and var.data_type != "category":
            errs.append("categorical distribution requires data_type 'category'")
        if p.distribution.family != "categorical" and var.data_type == "category":
            errs.append("data_type 'category' requires a categorical distribution")
        return errs

    def generate(self, p, ctx):
        u = ctx.uniforms(0)
        vals = D.sample(p.distribution, u, scale=ctx.noise_scale)
        return MechOutput(vals, {"u0": u})

    def describe(self, var):
        return f"{var.variable_id} ~ {self.parse(var.mechanism.params).distribution.describe()}"


class LinearParams(LinearPredictor):
    noise: Optional[D.Distribution] = Field(None, description="additive structural noise, e.g. normal(0, sd)")


class Linear(MechanismType):
    id = "linear"
    title = "Linear (in parameters) mechanism with additive noise"
    params_model = LinearParams
    output_types = {"float", "int"}
    requires_parents = True

    def check_params(self, p, var, allowed, parent_types):
        errs = p.check(allowed) + _check_dist(p.noise)
        for name in p.names():
            if parent_types.get(name) in ("category", "text") and not any(
                    "=" in t and t.split("=")[0].strip() == name for t in p.coefficients):
                errs.append(f"categorical parent '{name}' must be used as an indicator term '{name}=<category>'")
        return errs

    def generate(self, p, ctx):
        det = p.value(ctx.env, ctx.n)
        eps = _noise(p.noise, ctx)
        return MechOutput(det + eps, _noise_aux(p.noise, ctx, eps, det))

    def describe(self, var):
        p = self.parse(var.mechanism.params)
        return f"{var.variable_id} = {p.describe()}" + (f" + ε, ε ~ {p.noise.describe()}" if p.noise else "")


class Polynomial(Linear):
    id = "polynomial"
    title = "Polynomial terms (x^k) with additive noise"


class Interaction(Linear):
    id = "interaction"
    title = "Interaction terms (x*z) with additive noise"


class LaggedEffect(Linear):
    id = "lagged_effect"
    title = "Linear effect of lagged parents"

    def check(self, var, parent_names, parent_types):
        errs = super().check(var, parent_names, parent_types)
        if not any(pr.lag > 0 for pr in var.parents):
            errs.append(f"{var.variable_id}: lagged_effect needs at least one parent with lag >= 1")
        return errs


class ARParams(BaseModel):
    phi: float = Field(0.5, gt=-1, lt=1)
    mean: float = 0.0
    coefficients: dict[str, float] = Field(default_factory=dict)
    noise: Optional[D.Distribution] = None


class Autoregressive(MechanismType):
    id = "autoregressive"
    title = "AR(1): X_t = mean + phi (X_{t-1} - mean) + terms + noise"
    params_model = ARParams
    output_types = {"float", "int"}

    @staticmethod
    def self_alias(var) -> Optional[str]:
        return next((pr.name for pr in var.parents if pr.variable == var.variable_id and pr.lag >= 1), None)

    def check_params(self, p, var, allowed, parent_types):
        errs = _check_dist(p.noise)
        if not self.self_alias(var):
            errs.append("autoregressive needs a parent reference to itself with lag >= 1")
        for t in p.coefficients:
            errs += [f"term '{t}' references unknown parent '{n}'" for n in term_names(t) if n not in allowed]
        return errs

    def generate(self, p, ctx):
        prev = np.asarray(ctx.env[self.self_alias(ctx.var)], dtype=float)
        det = p.mean + p.phi * (prev - p.mean)
        for t, c in p.coefficients.items():
            det = det + c * _term_value(t, ctx.env, ctx.n)
        eps = _noise(p.noise, ctx)
        return MechOutput(det + eps, _noise_aux(p.noise, ctx, eps, det))


class ExpressionParams(BaseModel):
    expression: str
    noise: Optional[D.Distribution] = None


class DeterministicExpression(MechanismType):
    id = "deterministic_expression"
    title = "Restricted mathematical expression (optionally + noise)"
    params_model = ExpressionParams
    output_types = {"float", "int", "bool"}

    def check_params(self, p, var, allowed, parent_types):
        return check_expr(p.expression, allowed) + _check_dist(p.noise)

    def generate(self, p, ctx):
        det = evaluate(p.expression, ctx.env, ctx.n)
        if ctx.var.data_type == "bool":
            return MechOutput(det.astype(bool), {"deterministic": det.astype(float)})
        det = det.astype(float)
        eps = _noise(p.noise, ctx)
        return MechOutput(det + eps, _noise_aux(p.noise, ctx, eps, det))

    def describe(self, var):
        p = self.parse(var.mechanism.params)
        return f"{var.variable_id} = {p.expression}" + (f" + ε ~ {p.noise.describe()}" if p.noise else "")


class Case(BaseModel):
    when: str
    value: str


class ConditionalParams(BaseModel):
    cases: list[Case] = Field(min_length=1)
    default: str
    noise: Optional[D.Distribution] = None


class Conditional(MechanismType):
    id = "conditional"
    title = "Piecewise mechanism: first matching condition wins"
    params_model = ConditionalParams
    output_types = {"float", "int", "bool", "category"}

    def check_params(self, p, var, allowed, parent_types):
        errs = []
        for c in p.cases:
            errs += check_expr(c.when, allowed) + check_expr(c.value, allowed)
        return errs + check_expr(p.default, allowed) + _check_dist(p.noise)

    def generate(self, p, ctx):
        out = np.asarray(evaluate(p.default, ctx.env, ctx.n))
        out = out.astype(object) if ctx.var.data_type == "category" else out.astype(float)
        decided = np.zeros(ctx.n, dtype=bool)
        for c in p.cases:
            cond = np.asarray(evaluate(c.when, ctx.env, ctx.n)).astype(bool) & ~decided
            if cond.any():
                val = np.asarray(evaluate(c.value, ctx.env, ctx.n))
                out = np.where(cond, val, out)
                decided |= cond
        if ctx.var.data_type == "category":
            return MechOutput(out.astype(object))
        if ctx.var.data_type == "bool":
            return MechOutput(out.astype(bool), {"deterministic": out.astype(float)})
        det = out.astype(float)
        eps = _noise(p.noise, ctx)
        return MechOutput(det + eps, _noise_aux(p.noise, ctx, eps, det))


class ThresholdParams(LinearPredictor):
    threshold: float = 0.0
    noise: Optional[D.Distribution] = Field(None, description="noise added to the score before thresholding")


class Threshold(MechanismType):
    id = "threshold"
    title = "Binary threshold of a noisy score"
    params_model = ThresholdParams
    output_types = {"bool"}

    def check_params(self, p, var, allowed, parent_types):
        return p.check(allowed) + _check_dist(p.noise)

    def generate(self, p, ctx):
        score = p.value(ctx.env, ctx.n) + _noise(p.noise, ctx)
        aux = {"score": score}
        if p.noise is not None:
            aux["u0"] = ctx.uniforms(0)
        return MechOutput(score > p.threshold, aux)


class Logistic(MechanismType):
    id = "logistic"
    title = "p = sigmoid(linear predictor); X = 1[U < p]"
    params_model = LinearPredictor
    output_types = {"bool"}

    def check_params(self, p, var, allowed, parent_types):
        return p.check(allowed)

    def generate(self, p, ctx):
        eta = np.clip(p.value(ctx.env, ctx.n), -500, 500)
        prob = 1 / (1 + np.exp(-eta))
        u = ctx.uniforms(0)
        return MechOutput(u < prob, {"p": prob, "u0": u})

    def describe(self, var):
        return f"P({var.variable_id}=1) = sigmoid({self.parse(var.mechanism.params).describe()})"


class PoissonParams(LinearPredictor):
    link: str = Field("log", pattern="^(log|identity)$")
    dispersion: Optional[float] = Field(None, gt=0, description="set for a negative binomial (Var = m + m²/k)")
    max_rate: float = Field(1e6, gt=0)


class PoissonRegression(MechanismType):
    id = "poisson_regression"
    title = "Count with rate exp(predictor) (or identity link); optional negative binomial"
    params_model = PoissonParams
    output_types = {"int"}

    def check_params(self, p, var, allowed, parent_types):
        return p.check(allowed)

    def generate(self, p, ctx):
        eta = p.value(ctx.env, ctx.n)
        if p.link == "log":
            if np.any(eta > 50):
                raise MechanismError(f"{ctx.var.variable_id}: log-rate > 50 (overflow risk)")
            lam = np.exp(eta)
        else:
            lam = np.maximum(eta, 0.0)
        lam = np.minimum(lam, p.max_rate)
        u = ctx.uniforms(0)
        if p.dispersion:
            d = D.Distribution(family="negative_binomial", params={})
            vals = D.sample(d, u, overrides={"mean": np.maximum(lam, 1e-9), "dispersion": p.dispersion})
        else:
            vals = D.sample(D.Distribution(family="poisson", params={}), u, overrides={"rate": lam})
        return MechOutput(np.asarray(vals).astype(np.int64), {"lambda": lam, "u0": u})

    def describe(self, var):
        p = self.parse(var.mechanism.params)
        rate = f"exp({p.describe()})" if p.link == "log" else f"max(0, {p.describe()})"
        fam = f"NegBin(mean={rate}, k={p.dispersion:g})" if p.dispersion else f"Poisson({rate})"
        return f"{var.variable_id} ~ {fam}"


class CPTRow(BaseModel):
    when: dict[str, str] = Field(description="parent alias -> value (as string)")
    probs: dict[str, float]


class CPTParams(BaseModel):
    table: list[CPTRow] = Field(default_factory=list)
    default: dict[str, float] = Field(description="probabilities when no row matches")
    strength: float = Field(1.0, ge=0, le=1, description="alpha: 1 = CPT, 0 = reference distribution")
    reference: Optional[dict[str, float]] = Field(None, description="P_ref (default: the default row)")


class CategoricalCPT(MechanismType):
    id = "categorical_cpt"
    title = "Conditional probability table with strength interpolation"
    params_model = CPTParams
    output_types = {"category", "bool", "int"}

    def check_params(self, p, var, allowed, parent_types):
        errs = []
        cats = var.categories or list(p.default)
        for row in [p.default, *(r.probs for r in p.table), *( [p.reference] if p.reference else [])]:
            if set(row) - set(cats):
                errs.append(f"unknown categories {sorted(set(row) - set(cats))}")
            try:
                D.check_categorical(list(row), list(row.values()))
                if abs(sum(row.values()) - 1) > 1e-6:
                    errs.append(f"CPT probabilities sum to {sum(row.values()):.6g}, expected 1")
            except D.DistributionError as e:
                errs.append(str(e))
        for r in p.table:
            for alias, value in r.when.items():
                if alias not in allowed:
                    errs.append(f"CPT condition on unknown parent '{alias}'")
                elif parent_types.get(alias) in ("float", "int"):
                    try:
                        valid = np.isfinite(float(value))
                    except ValueError:
                        valid = False
                    if not valid:
                        errs.append(f"CPT condition for '{alias}' requires a finite number")
        return errs

    @staticmethod
    def categories(var, p) -> list[str]:
        return list(var.categories) if var.categories else list(p.default)

    def probs(self, p, var, env, n) -> np.ndarray:
        cats = self.categories(var, p)

        def vec(d: dict[str, float]) -> np.ndarray:
            v = np.array([float(d.get(c, 0.0)) for c in cats])
            return v / v.sum()

        out = np.tile(vec(p.default), (n, 1))
        matched = np.zeros(n, dtype=bool)
        for row in p.table:
            m = np.ones(n, dtype=bool)
            for k, val in row.when.items():
                arr = np.asarray(env[k])
                if arr.dtype == bool:
                    m &= arr == (str(val).lower() in ("1", "true", "yes"))
                elif arr.dtype.kind in "if":
                    m &= arr.astype(float) == float(val)
                else:
                    m &= arr.astype(str) == str(val)
            m &= ~matched
            out[m] = vec(row.probs)
            matched |= m
        if p.strength < 1:
            ref = vec(p.reference or p.default)
            out = p.strength * out + (1 - p.strength) * ref
        return out

    def generate(self, p, ctx):
        cats = self.categories(ctx.var, p)
        pr = self.probs(p, ctx.var, ctx.env, ctx.n)
        u = ctx.uniforms(0)
        vals = D.categorical_from_u(u, cats, pr)
        return MechOutput(_cast_category(vals, ctx.var.data_type), {"u0": u, **{f"p:{c}": pr[:, i] for i, c in enumerate(cats)}})


def _cast_category(vals: np.ndarray, data_type: str) -> np.ndarray:
    if data_type == "bool":
        return np.isin(vals.astype(str), ["1", "true", "True", "yes"])
    if data_type == "int":
        return vals.astype(float).astype(np.int64)
    return vals.astype(object)


class SoftmaxParams(BaseModel):
    logits: dict[str, LinearPredictor] = Field(description="category -> linear predictor (one may be omitted = 0)")


class CategoricalSoftmax(MechanismType):
    id = "categorical_softmax"
    title = "Multinomial logit over categories"
    params_model = SoftmaxParams
    output_types = {"category"}

    def check_params(self, p, var, allowed, parent_types):
        errs = []
        if not var.categories:
            errs.append("categorical_softmax needs variable.categories")
        errs += [f"unknown category {c}" for c in p.logits if c not in var.categories]
        for lp in p.logits.values():
            errs += lp.check(allowed)
        return errs

    def probs(self, p, var, env, n) -> np.ndarray:
        z = np.column_stack([p.logits[c].value(env, n) if c in p.logits else np.zeros(n) for c in var.categories])
        z = z - z.max(axis=1, keepdims=True)
        ez = np.exp(z)
        return ez / ez.sum(axis=1, keepdims=True)

    def generate(self, p, ctx):
        pr = self.probs(p, ctx.var, ctx.env, ctx.n)
        u = ctx.uniforms(0)
        return MechOutput(D.categorical_from_u(u, ctx.var.categories, pr),
                          {"u0": u, **{f"p:{c}": pr[:, i] for i, c in enumerate(ctx.var.categories)}})


class OrdinalParams(LinearPredictor):
    cutpoints: list[float] = Field(min_length=1)
    noise: D.Distribution = Field(default_factory=lambda: D.Distribution(family="normal", params={"sd": 1.0}))

    @model_validator(mode="after")
    def _sorted(self) -> "OrdinalParams":
        if any(b <= a for a, b in zip(self.cutpoints, self.cutpoints[1:])):
            raise ValueError("cutpoints must be strictly increasing")
        return self


class OrdinalThreshold(MechanismType):
    id = "ordinal_threshold"
    title = "Latent continuous score cut into ordered categories"
    params_model = OrdinalParams
    output_types = {"category", "int"}

    def check_params(self, p, var, allowed, parent_types):
        errs = p.check(allowed) + _check_dist(p.noise)
        if var.data_type == "category" and len(var.categories) != len(p.cutpoints) + 1:
            errs.append("ordinal_threshold needs len(categories) == len(cutpoints) + 1")
        return errs

    def generate(self, p, ctx):
        latent = p.value(ctx.env, ctx.n) + _noise(p.noise, ctx)
        level = np.searchsorted(np.asarray(p.cutpoints), latent, side="right")
        aux = {"latent": latent, "u0": ctx.uniforms(0)}
        if ctx.var.data_type == "category":
            return MechOutput(np.asarray(ctx.var.categories, dtype=object)[level], aux)
        return MechOutput(level.astype(np.int64) + 1, aux)


class GroupEffectParams(BaseModel):
    group_by: str = Field(description="key column of the grain (an FK of the grain)")
    distribution: D.Distribution
    base: Optional[LinearPredictor] = None


class GroupEffect(MechanismType):
    id = "group_effect"
    title = "Shared random effect per group (+ optional linear part)"
    params_model = GroupEffectParams
    output_types = {"float"}

    def check_params(self, p, var, allowed, parent_types):
        return _check_dist(p.distribution) + (p.base.check(allowed) if p.base else [])

    def generate(self, p, ctx):
        if p.group_by not in ctx.keys:
            raise MechanismError(f"{ctx.var.variable_id}: group key '{p.group_by}' is not a key of the grain")
        u = ctx.group_uniforms(np.asarray(ctx.keys[p.group_by]))
        eff = D.sample(p.distribution, u, scale=ctx.noise_scale)
        base = p.base.value(ctx.env, ctx.n) if p.base else np.zeros(ctx.n)
        return MechOutput(base + eff, {"deterministic": base, "noise": eff})


class AggregationParams(BaseModel):
    source: Optional[str] = Field(None, description="aggregated parent alias (default: the only parent)")
    factor: float = 1.0
    offset: float = 0.0


class Aggregation(MechanismType):
    id = "aggregation"
    title = "Aggregate of child rows (sum / mean / count / min / max / any)"
    params_model = AggregationParams
    output_types = {"float", "int", "bool"}
    deterministic = True
    requires_parents = True

    def check(self, var, parent_names, parent_types):
        errs = super().check(var, parent_names, parent_types)
        if not any(pr.aggregation for pr in var.parents):
            errs.append(f"{var.variable_id}: aggregation needs a parent reference with 'aggregation' set")
        return errs

    def check_params(self, p, var, allowed, parent_types):
        return [] if (p.source is None and len(var.parents) == 1) or p.source in allowed else \
            ["aggregation: set 'source' to one parent alias"]

    def generate(self, p, ctx):
        src = p.source or ctx.var.parents[0].name
        v = np.asarray(ctx.env[src], dtype=float) * p.factor + p.offset
        return MechOutput(v.astype(bool) if ctx.var.data_type == "bool" else v)


class LookupParams(BaseModel):
    source: Optional[str] = None
    mapping: dict[str, Any] = Field(default_factory=dict, description="source value (as string) -> value")
    default: Any = None


class Lookup(MechanismType):
    id = "lookup"
    title = "Copy / map a parent value (e.g. an attribute reached through a foreign key)"
    params_model = LookupParams
    output_types = {"float", "int", "bool", "category", "text"}
    deterministic = True
    requires_parents = True

    def check_params(self, p, var, allowed, parent_types):
        if p.source is None and len(var.parents) != 1:
            return ["lookup: set 'source' when there is more than one parent"]
        if p.source is not None and p.source not in allowed:
            return [f"lookup: unknown source '{p.source}'"]
        return []

    def generate(self, p, ctx):
        src = np.asarray(ctx.env[p.source or ctx.var.parents[0].name])
        if not p.mapping:
            return MechOutput(src.copy())
        keys = src.astype(str) if src.dtype.kind != "f" else np.array([f"{x:g}" for x in src], dtype=object)
        out = np.array([p.mapping.get(k, p.default) for k in keys], dtype=object)
        if ctx.var.data_type in ("float", "int"):
            out = out.astype(float)
        return MechOutput(out)


class TextParams(BaseModel):
    templates: dict[str, list[str]] = Field(description="selector value (or 'default') -> templates with {alias}")
    select_by: Optional[str] = Field(None, description="parent alias choosing the template group")
    vague_templates: list[str] = Field(default_factory=list, description="templates not revealing the cause")
    explicitness: float = Field(1.0, ge=0, le=1, description="share of rows using an explicit template")


class TextTemplate(MechanismType):
    id = "text_template"
    title = "Text generated from the structured state through templates"
    params_model = TextParams
    output_types = {"text"}

    def check_params(self, p, var, allowed, parent_types):
        import string
        errs = []
        if p.select_by and p.select_by not in allowed:
            errs.append(f"text_template: unknown select_by '{p.select_by}'")
        for tpls in [*p.templates.values(), p.vague_templates]:
            for t in tpls:
                for _, fname, _, _ in string.Formatter().parse(t):
                    if fname and fname not in allowed:
                        errs.append(f"text_template: placeholder '{{{fname}}}' is not a parent")
        if "default" not in p.templates and not p.select_by:
            errs.append("text_template: provide 'default' templates or select_by")
        return errs

    def generate(self, p, ctx):
        u_choice, u_explicit = ctx.uniforms(0), ctx.uniforms(1)
        sel = np.asarray(ctx.env[p.select_by]).astype(str) if p.select_by else np.full(ctx.n, "default", dtype=object)
        env_cols = {k: np.asarray(v) for k, v in ctx.env.items() if k not in TIME_NAMES}
        out = np.empty(ctx.n, dtype=object)
        for i in range(ctx.n):
            group = p.templates.get(sel[i]) or p.templates.get("default") or []
            if p.vague_templates and u_explicit[i] >= p.explicitness:
                group = p.vague_templates
            if not group:
                out[i] = ""
                continue
            tpl = group[min(int(u_choice[i] * len(group)), len(group) - 1)]
            row = {k: _fmt(v[i]) for k, v in env_cols.items() if np.ndim(v) == 1 and len(v) == ctx.n}
            try:
                out[i] = tpl.format(**row)
            except (KeyError, IndexError, ValueError):
                out[i] = tpl
        return MechOutput(out)


def _fmt(v: Any) -> str:
    if isinstance(v, (float, np.floating)):
        return f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}"
    return str(v)


class LabelParams(BaseModel):
    pattern: str = Field("{n}", description="label pattern with {n} = row number (primary key)")
    values: list[str] = Field(default_factory=list, description="explicit labels (cycled), e.g. LLM master data")


class Label(MechanismType):
    id = "label"
    title = "Deterministic label / name (master data)"
    params_model = LabelParams
    output_types = {"text", "category"}
    deterministic = True

    def generate(self, p, ctx):
        pk = ctx.keys.get("__pk__")
        n = np.asarray(pk if pk is not None else np.arange(1, ctx.n + 1))
        if p.values:
            return MechOutput(np.array([p.values[(int(i) - 1) % len(p.values)] for i in n], dtype=object))
        return MechOutput(np.array([p.pattern.format(n=int(i)) for i in n], dtype=object))


REGISTRY: dict[str, MechanismType] = {m.id: m for m in [
    Exogenous(), Linear(), Polynomial(), Interaction(), LaggedEffect(), Autoregressive(), DeterministicExpression(),
    Conditional(), Threshold(), Logistic(), PoissonRegression(), CategoricalCPT(), CategoricalSoftmax(),
    OrdinalThreshold(), GroupEffect(), Aggregation(), Lookup(), TextTemplate(), Label()]}


def get(mid: str) -> MechanismType:
    m = REGISTRY.get(mid)
    if m is None:
        raise MechanismError(f"unsupported mechanism type '{mid}' (supported: {sorted(REGISTRY)})")
    return m


def catalog() -> list[dict[str, Any]]:
    return [{"id": m.id, "title": m.title, "output_types": sorted(m.output_types), "deterministic": m.deterministic,
             "params_schema": m.params_model.model_json_schema()} for m in REGISTRY.values()]


def is_deterministic(var: Any) -> bool:
    m = REGISTRY.get(var.mechanism.type)
    if m is None:
        return False
    if m.deterministic:
        return True
    if m.id in ("deterministic_expression", "conditional"):
        return not var.mechanism.params.get("noise")
    return False


def coefficient_of(var: Any, parent: str) -> Optional[float]:
    m = REGISTRY.get(var.mechanism.type)
    if m is None:
        return None
    try:
        return m.coefficient(m.parse(var.mechanism.params), parent)
    except MechanismError:
        return None


def describe(var: Any) -> str:
    try:
        return get(var.mechanism.type).describe(var)
    except MechanismError as e:
        return f"{var.variable_id}: {e}"
