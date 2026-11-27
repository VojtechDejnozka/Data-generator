"""Restricted, vectorised expression interpreter for SCM mechanisms and filters.

LLM-proposed formulas are never passed to ``eval``/``exec``. They are parsed with ``ast`` and only an
explicit allowlist of node types, operators and functions is interpreted over numpy arrays.

Supported: numbers, strings, booleans, variable names, ``+ - * / // % **`` (constant exponent,
|exponent| <= 10), unary ``- + not``, ``and``/``or``, comparisons (incl. ``in`` with a constant
tuple/list), ``a if cond else b`` and the functions in :data:`FUNCTIONS`.

Domain errors (division by zero, log of a non-positive value, sqrt of a negative value, overflow,
non-finite results) raise :class:`ExpressionError` instead of silently producing NaN/inf.
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Mapping

import numpy as np

MAX_LEN = 2000
MAX_DEPTH = 40
MAX_POW = 10
CONSTANTS = {"pi": math.pi, "e": math.e, "true": True, "false": False}


class ExpressionError(ValueError):
    pass


def _arr(x: Any) -> np.ndarray:
    return np.asarray(x)


def _num(x: Any) -> np.ndarray:
    a = np.asarray(x)
    if a.dtype == object:
        try:
            a = a.astype(float)
        except (TypeError, ValueError) as e:
            raise ExpressionError("numeric operation on a non-numeric value") from e
    if a.dtype == bool:
        a = a.astype(float)
    return a


def _check_finite(a: np.ndarray, what: str) -> np.ndarray:
    if a.dtype.kind == "f" and not np.all(np.isfinite(a)):
        raise ExpressionError(f"{what} produced non-finite values (overflow or invalid domain)")
    return a


def _log(x):
    a = _num(x)
    if np.any(a <= 0):
        raise ExpressionError(f"log of a non-positive value ({int(np.sum(a <= 0))} rows); use log(max(x, eps))")
    return np.log(a)


def _sqrt(x):
    a = _num(x)
    if np.any(a < 0):
        raise ExpressionError(f"sqrt of a negative value ({int(np.sum(a < 0))} rows)")
    return np.sqrt(a)


def _exp(x):
    a = _num(x)
    if np.any(a > 700):
        raise ExpressionError("exp overflow (argument > 700)")
    return np.exp(a)


def _sigmoid(x):
    a = np.clip(_num(x), -500, 500)
    return 1.0 / (1.0 + np.exp(-a))


def _clip(x, lo, hi):
    return np.clip(_num(x), _num(lo), _num(hi))


def _safe_div(a, b, default=0.0):
    a, b = _num(a), _num(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(b == 0, _num(default), a / np.where(b == 0, 1, b))
    return out


def _where(c, a, b):
    return np.where(_arr(c).astype(bool), a, b)


def _minimum(*xs):
    if len(xs) < 2:
        raise ExpressionError("min() needs at least two arguments (element-wise)")
    out = _num(xs[0])
    for x in xs[1:]:
        out = np.minimum(out, _num(x))
    return out


def _maximum(*xs):
    if len(xs) < 2:
        raise ExpressionError("max() needs at least two arguments (element-wise)")
    out = _num(xs[0])
    for x in xs[1:]:
        out = np.maximum(out, _num(x))
    return out


def _softmax_prob(i, *logits):
    """Probability of the i-th (0-based constant) of several logits – element-wise softmax."""
    if not logits:
        raise ExpressionError("softmax_prob needs logits")
    m = _maximum(*logits) if len(logits) > 1 else _num(logits[0])
    ex = [np.exp(_num(z) - m) for z in logits]
    k = int(_num(i).flat[0])
    if not 0 <= k < len(ex):
        raise ExpressionError("softmax_prob index out of range")
    return ex[k] / sum(ex)


FUNCTIONS: dict[str, tuple[Callable[..., Any], int, int]] = {
    # name: (implementation, min args, max args)
    "min": (_minimum, 2, 8), "max": (_maximum, 2, 8), "clip": (_clip, 3, 3),
    "log": (_log, 1, 1), "exp": (_exp, 1, 1), "sqrt": (_sqrt, 1, 1), "abs": (lambda x: np.abs(_num(x)), 1, 1),
    "sigmoid": (_sigmoid, 1, 1), "logit": (lambda p: _log(_num(p)) - _log(1 - _num(p)), 1, 1),
    "floor": (lambda x: np.floor(_num(x)), 1, 1), "ceil": (lambda x: np.ceil(_num(x)), 1, 1),
    "round": (lambda x, d=0: np.round(_num(x), int(_num(d).flat[0])), 1, 2),
    "where": (_where, 3, 3), "safe_div": (_safe_div, 2, 3), "softmax_prob": (_softmax_prob, 2, 9),
    "sin": (lambda x: np.sin(_num(x)), 1, 1), "cos": (lambda x: np.cos(_num(x)), 1, 1),
    "float": (lambda x: _num(x).astype(float), 1, 1), "int": (lambda x: np.trunc(_num(x)), 1, 1),
}

_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_CMPOPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn)


@dataclass(frozen=True)
class Compiled:
    source: str
    tree: ast.Expression
    names: frozenset[str]
    functions: frozenset[str]


def _validate(node: ast.AST, depth: int, names: set[str], funcs: set[str]) -> None:
    if depth > MAX_DEPTH:
        raise ExpressionError("expression nested too deeply")
    if isinstance(node, ast.Expression):
        return _validate(node.body, depth + 1, names, funcs)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool)) or node.value is None:
            raise ExpressionError(f"constant of type {type(node.value).__name__} is not allowed")
        return
    if isinstance(node, ast.Name):
        names.add(node.id)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BINOPS):
            raise ExpressionError(f"operator {type(node.op).__name__} is not allowed")
        if isinstance(node.op, ast.Pow):
            exp = node.right
            if isinstance(exp, ast.UnaryOp) and isinstance(exp.op, ast.USub) and isinstance(exp.operand, ast.Constant):
                val = -exp.operand.value
            elif isinstance(exp, ast.Constant):
                val = exp.value
            else:
                raise ExpressionError("the exponent of ** must be a numeric constant")
            if not isinstance(val, (int, float)) or abs(val) > MAX_POW:
                raise ExpressionError(f"exponent must be a constant with |exponent| <= {MAX_POW}")
        _validate(node.left, depth + 1, names, funcs)
        _validate(node.right, depth + 1, names, funcs)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            raise ExpressionError("unary operator not allowed")
        return _validate(node.operand, depth + 1, names, funcs)
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate(v, depth + 1, names, funcs)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _CMPOPS):
                raise ExpressionError("comparison operator not allowed")
        _validate(node.left, depth + 1, names, funcs)
        for c, op in zip(node.comparators, node.ops):
            if isinstance(op, (ast.In, ast.NotIn)):
                if not isinstance(c, (ast.Tuple, ast.List)) or not all(isinstance(e, ast.Constant) for e in c.elts):
                    raise ExpressionError("'in' requires a constant tuple or list")
            else:
                _validate(c, depth + 1, names, funcs)
        return
    if isinstance(node, ast.IfExp):
        for n in (node.test, node.body, node.orelse):
            _validate(n, depth + 1, names, funcs)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            fname = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise ExpressionError(f"function '{fname}' is not allowed")
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed")
        _, lo, hi = FUNCTIONS[node.func.id]
        if not lo <= len(node.args) <= hi:
            raise ExpressionError(f"{node.func.id}() takes {lo}–{hi} arguments")
        funcs.add(node.func.id)
        for a in node.args:
            _validate(a, depth + 1, names, funcs)
        return
    raise ExpressionError(f"syntax element {type(node).__name__} is not allowed")


@lru_cache(maxsize=4096)
def compile_expr(source: str) -> Compiled:
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("empty expression")
    if len(source) > MAX_LEN:
        raise ExpressionError(f"expression longer than {MAX_LEN} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e.msg}") from e
    names: set[str] = set()
    funcs: set[str] = set()
    _validate(tree, 0, names, funcs)
    return Compiled(source, tree, frozenset(names - set(CONSTANTS)), frozenset(funcs))


def check_expr(source: str, allowed_names: set[str]) -> list[str]:
    """Static check: returns a list of problems (empty = OK)."""
    try:
        c = compile_expr(source)
    except ExpressionError as e:
        return [str(e)]
    unknown = sorted(c.names - allowed_names)
    return [f"unknown name(s) {unknown} in '{source}' (allowed: {sorted(allowed_names)})"] if unknown else []


def _eval(node: ast.AST, env: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        raise ExpressionError(f"unknown name '{node.id}'")
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        op = node.op
        if isinstance(op, ast.Add):
            if isinstance(a, str) or isinstance(b, str):
                raise ExpressionError("string concatenation is not supported")
            return _num(a) + _num(b)
        if isinstance(op, ast.Sub):
            return _num(a) - _num(b)
        if isinstance(op, ast.Mult):
            return _num(a) * _num(b)
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
            bb = _num(b)
            if np.any(bb == 0):
                raise ExpressionError(f"division by zero ({int(np.sum(bb == 0))} rows); use safe_div(a, b, default)")
            aa = _num(a)
            if isinstance(op, ast.Div):
                return aa / bb
            return aa // bb if isinstance(op, ast.FloorDiv) else aa % bb
        if isinstance(op, ast.Pow):
            base, ex = _num(a), float(_num(b))
            if ex != int(ex) and np.any(base < 0):
                raise ExpressionError("fractional power of a negative value")
            if ex < 0 and np.any(base == 0):
                raise ExpressionError("negative power of zero")
            return _check_finite(np.power(base.astype(float), ex), "power")
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        if isinstance(node.op, ast.USub):
            return -_num(v)
        if isinstance(node.op, ast.UAdd):
            return _num(v)
        return np.logical_not(_arr(v).astype(bool))
    if isinstance(node, ast.BoolOp):
        vals = [_arr(_eval(v, env)).astype(bool) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            out = np.logical_and(out, v) if isinstance(node.op, ast.And) else np.logical_or(out, v)
        return out
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        result: Any = True
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                consts = [e.value for e in comp.elts]  # type: ignore[attr-defined]
                r = np.isin(_arr(left), np.asarray(consts, dtype=_arr(left).dtype if _arr(left).dtype != object else object))
                if isinstance(op, ast.NotIn):
                    r = ~r
                right = left
            else:
                right = _eval(comp, env)
                la, ra = _arr(left), _arr(right)
                if la.dtype == object or ra.dtype == object or isinstance(left, str) or isinstance(right, str):
                    la, ra = la.astype(object), ra.astype(object)
                fn = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
                      ast.Eq: np.equal, ast.NotEq: np.not_equal}[type(op)]
                r = fn(la, ra)
            result = np.logical_and(result, r)
            left = right
        return result
    if isinstance(node, ast.IfExp):
        cond = _arr(_eval(node.test, env)).astype(bool)
        return np.where(cond, _eval(node.body, env), _eval(node.orelse, env))
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[node.func.id][0]  # type: ignore[attr-defined]
        return fn(*[_eval(a, env) for a in node.args])
    raise ExpressionError(f"cannot evaluate {type(node).__name__}")  # pragma: no cover


def evaluate(source: str, env: Mapping[str, Any], n: int | None = None) -> np.ndarray:
    """Evaluate ``source`` over arrays in ``env``; the result is broadcast to length ``n`` if given."""
    c = compile_expr(source)
    missing = [x for x in c.names if x not in env]
    if missing:
        raise ExpressionError(f"unknown name(s) {sorted(missing)} in '{source}'")
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            out = np.asarray(_eval(c.tree, env))
        except FloatingPointError as e:
            raise ExpressionError(f"numeric error in '{source}': {e}") from e
    if out.dtype.kind == "f":
        _check_finite(out, f"'{source}'")
    if n is not None:
        out = np.broadcast_to(out, (n,)).copy() if out.ndim == 0 or out.shape != (n,) else out
    return out
