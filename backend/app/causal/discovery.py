"""Constraint-based causal discovery for the detective (PC with Fisher-z tests).

``causal-learn`` is preferred when installed (``pc`` with ``fisherz``); otherwise a compact, tested
NumPy implementation of PC-stable (skeleton, v-structures, Meek rules R1–R3) is used. Assumptions are
checked and reported – discovery is never applied blindly:

* linear-Gaussian CI tests (Fisher-z on partial correlations) – flagged for binary / few-level data,
* causal sufficiency (no latent confounders) – PC cannot represent them (FCI not implemented),
* i.i.d. rows – broadcast parent attributes create pseudo-replication; the effective sample size of a
  test is the minimum number of distinct source units of the involved columns,
* missing values are removed listwise (share reported), near-deterministic relations are reported
  and one of the columns is dropped (faithfulness violation).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Optional

import numpy as np
from scipy import stats


def _pcorr(C: np.ndarray, i: int, j: int, S: tuple[int, ...]) -> float:
    if not S:
        return float(C[i, j])
    idx = [i, j, *S]
    sub = C[np.ix_(idx, idx)]
    try:
        P = np.linalg.pinv(sub)
    except np.linalg.LinAlgError:
        return 0.0
    d = np.sqrt(abs(P[0, 0] * P[1, 1]))
    return float(-P[0, 1] / d) if d > 0 else 0.0


def fisher_z_p(r: float, n: int, k: int) -> float:
    r = min(max(r, -0.999999), 0.999999)
    dof = n - k - 3
    if dof <= 0:
        return 1.0
    z = 0.5 * np.log((1 + r) / (1 - r)) * np.sqrt(dof)
    return float(2 * stats.norm.sf(abs(z)))


def pc(data: np.ndarray, names: list[str], alpha: float = 0.01, n_eff: Optional[dict[str, int]] = None,
       max_cond: int = 3, forbidden_children: Optional[set[str]] = None) -> dict[str, Any]:
    """Returns {'edges': [{source, target, type: directed|undirected, p_max, partial_r}], 'sepsets', ...}."""
    n, p = data.shape
    C = np.corrcoef(data, rowvar=False)
    C = np.nan_to_num(C)
    adj = {i: set(range(p)) - {i} for i in range(p)}
    sep: dict[frozenset, tuple[int, ...]] = {}
    pmax: dict[frozenset, float] = {}
    neff = [int(min(n, (n_eff or {}).get(nm, n))) for nm in names]
    level = 0
    while level <= max_cond:
        snapshot = {i: set(a) for i, a in adj.items()}  # PC-stable
        removed = False
        for i in range(p):
            for j in sorted(snapshot[i]):
                if j < i or j not in adj[i]:
                    continue
                cands = sorted((snapshot[i] | snapshot[j]) - {i, j})
                if len(cands) < level:
                    continue
                for S in combinations(cands, level):
                    r = _pcorr(C, i, j, S)
                    m = min(neff[i], neff[j], *(neff[s] for s in S)) if S else min(neff[i], neff[j])
                    pv = fisher_z_p(r, m, len(S))
                    key = frozenset((i, j))
                    pmax[key] = max(pmax.get(key, 0.0), pv)
                    if pv > alpha:
                        adj[i].discard(j)
                        adj[j].discard(i)
                        sep[key] = S
                        removed = True
                        break
        level += 1
        if not removed and all(len(a) - 1 < level for a in adj.values()):
            break
    oriented: set[tuple[int, int]] = set()
    for k in range(p):
        nb = sorted(adj[k])
        for a, b in combinations(nb, 2):
            if b in adj[a]:
                continue
            s = sep.get(frozenset((a, b)), ())
            if k not in s:
                oriented.add((a, k))
                oriented.add((b, k))
    # background knowledge: e.g. columns that cannot be effects
    for nm in forbidden_children or set():
        if nm in names:
            k = names.index(nm)
            for a in adj[k]:
                if (a, k) not in oriented:
                    oriented.discard((a, k))
                    oriented.add((k, a))
    # resolve conflicting double orientations (a->b and b->a) as undirected
    conflicts = {(a, b) for (a, b) in oriented if (b, a) in oriented}
    oriented -= conflicts
    changed = True
    while changed:
        changed = False
        for a in range(p):
            for b in sorted(adj[a]):
                if (a, b) in oriented or (b, a) in oriented:
                    continue
                if any((w, a) in oriented and b not in adj[w] and w != b for w in adj[a]):
                    oriented.add((a, b)); changed = True; continue
                if any((a, w) in oriented and (w, b) in oriented for w in adj[a] & adj[b]):
                    oriented.add((a, b)); changed = True; continue
                ws = [w for w in adj[a] & adj[b] if (w, b) in oriented and (w, a) not in oriented and (a, w) not in oriented]
                if any(w2 not in adj[w1] for i_, w1 in enumerate(ws) for w2 in ws[i_ + 1:]):
                    oriented.add((a, b)); changed = True
    edges = []
    seen: set[frozenset] = set()
    for a in range(p):
        for b in sorted(adj[a]):
            k = frozenset((a, b))
            if k in seen:
                continue
            seen.add(k)
            if (a, b) in oriented:
                edges.append({"source": names[a], "target": names[b], "type": "directed"})
            elif (b, a) in oriented:
                edges.append({"source": names[b], "target": names[a], "type": "directed"})
            else:
                lo, hi = min(a, b), max(a, b)
                edges.append({"source": names[lo], "target": names[hi], "type": "undirected"})
    for e in edges:
        i, j = names.index(e["source"]), names.index(e["target"])
        e["r"] = round(float(C[i, j]), 4)
        e["p_max"] = round(pmax.get(frozenset((i, j)), 0.0), 6)
    return {"edges": edges, "n": n, "alpha": alpha, "method": "pc-stable/fisher-z (built-in)",
            "sepsets": {f"{names[min(k)]}|{names[max(k)]}": [names[s] for s in v] for k, v in sep.items()}}


def discover(df, columns: list[str], alpha: float = 0.01, n_eff: Optional[dict[str, int]] = None,
             max_rows: int = 30000, seed: int = 0, temporal: bool = False) -> dict[str, Any]:
    import pandas as pd

    notes: list[str] = []
    cols = [c for c in columns if c in df.columns]
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    n0 = len(X)
    X = X.dropna()
    if n0:
        miss = 1 - len(X) / n0
        if miss > 0:
            notes.append(f"listwise deletion removed {miss:.1%} of rows (missing values)")
    if len(X) > max_rows:
        X = X.sample(max_rows, random_state=seed)
        notes.append(f"random subsample of {max_rows} rows")
    keep = [c for c in cols if X[c].nunique() > 1]
    dropped = sorted(set(cols) - set(keep))
    if dropped:
        notes.append(f"constant columns dropped: {dropped}")
    X = X[keep]
    if len(keep) < 2:
        return {"status": "not_applicable", "reason": "fewer than two non-constant columns", "notes": notes, "edges": []}
    C = np.corrcoef(X.to_numpy(float), rowvar=False)
    det: list[tuple[str, str]] = []
    for i, j in combinations(range(len(keep)), 2):
        if abs(C[i, j]) > 0.999:
            det.append((keep[i], keep[j]))
    if det:
        notes.append(f"near-deterministic pairs {det}: faithfulness is violated; the second column of each pair is "
                     "dropped from the CI tests")
        keep = [c for c in keep if c not in {b for _, b in det}]
        X = X[keep]
    binary = [c for c in keep if X[c].nunique() <= 2]
    if binary:
        notes.append(f"binary columns {binary}: Fisher-z assumes linear-Gaussian relations (approximation)")
    notes.append("PC assumes causal sufficiency (no latent confounders); bidirected edges cannot be represented")
    if temporal:
        notes.append("rows have a time structure; contemporaneous PC ignores lags (orientation by time order is "
                     "not used)")
    if n_eff:
        small = {c: n_eff[c] for c in keep if c in n_eff and n_eff[c] < len(X)}
        if small:
            notes.append(f"effective sample sizes reduced for broadcast parent attributes: {small}")
    if len(X) < 10 * len(keep):
        notes.append(f"small sample ({len(X)} rows for {len(keep)} variables): low power")
    try:
        from causallearn.search.ConstraintBased.PC import pc as cl_pc  # type: ignore
        cg = cl_pc(X.to_numpy(float), alpha=alpha, indep_test="fisherz", show_progress=False)
        G = cg.G.graph
        edges = []
        for i in range(len(keep)):
            for j in range(i + 1, len(keep)):
                if G[i, j] == -1 and G[j, i] == 1:
                    edges.append({"source": keep[i], "target": keep[j], "type": "directed"})
                elif G[i, j] == 1 and G[j, i] == -1:
                    edges.append({"source": keep[j], "target": keep[i], "type": "directed"})
                elif G[i, j] != 0:
                    edges.append({"source": keep[i], "target": keep[j], "type": "undirected"})
        res = {"edges": edges, "n": len(X), "alpha": alpha, "method": "causal-learn pc/fisherz"}
        notes.append("causal-learn does not use the effective sample sizes")
    except ImportError:
        res = pc(X.to_numpy(float), keep, alpha=alpha, n_eff=n_eff)
    res["status"] = "ok"
    res["notes"] = notes
    res["columns"] = keep
    return res
