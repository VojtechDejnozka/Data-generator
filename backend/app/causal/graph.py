"""Causal graph utilities (NetworkX): within-step acyclicity, execution phases, summary graph,
latent projection and Markov-equivalence (CPDAG) for identifiability-aware evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import networkx as nx

from .contracts import SCMSpec

ENTITY_NODE = "@entity:"


class GraphError(ValueError):
    pass


def entity_node(eid: str) -> str:
    return ENTITY_NODE + eid


def entity_lineage(spec: SCMSpec, eid: str) -> list[str]:
    """Entity ids this entity is derived from (parent chain and cross members), nearest first."""
    out: list[str] = []
    stack = [eid]
    while stack:
        e = spec.entity(stack.pop())
        if not e:
            continue
        nxt = ([e.parent] if e.kind == "child" and e.parent else []) + (list(e.cross) if e.kind == "cross" else [])
        for n in nxt:
            if n not in out:
                out.append(n)
                stack.append(n)
    return out


def is_temporal_entity(spec: SCMSpec, eid: str) -> bool:
    e = spec.entity(eid)
    if not e:
        return False
    if e.kind == "cross":
        return e.temporal
    if e.kind == "child" and e.parent:
        return is_temporal_entity(spec, e.parent)
    return False


def is_dynamic_entity(spec: SCMSpec, eid: str) -> bool:
    """Rows depend on a count variable (or descend from such an entity)."""
    e = spec.entity(eid)
    if not e or e.kind != "child":
        return False
    return bool(e.count_variable) or is_dynamic_entity(spec, e.parent or "")


@dataclass
class ExecutionPlan:
    pre: list[str] = field(default_factory=list)   # static nodes before the time loop
    loop: list[str] = field(default_factory=list)  # temporal nodes, evaluated every step
    post: list[str] = field(default_factory=list)  # static nodes depending on temporal ones
    within_step: nx.DiGraph = field(default_factory=nx.DiGraph)
    full: nx.MultiDiGraph = field(default_factory=nx.MultiDiGraph)


def build_graphs(spec: SCMSpec) -> tuple[nx.DiGraph, nx.MultiDiGraph]:
    """(within-step DAG over lag-0 dependencies, full multigraph incl. lagged edges)."""
    g0 = nx.DiGraph()
    full = nx.MultiDiGraph()
    for v in spec.variables:
        g0.add_node(v.variable_id, kind="variable")
        full.add_node(v.variable_id, kind="variable")
    dynamic = [e for e in spec.entities if is_dynamic_entity(spec, e.entity_id)]
    for e in dynamic:
        n = entity_node(e.entity_id)
        g0.add_node(n, kind="entity")
        full.add_node(n, kind="entity")
        if e.count_variable:
            g0.add_edge(e.count_variable, n, lag=0)
            full.add_edge(e.count_variable, n, lag=0)
        if e.parent and is_dynamic_entity(spec, e.parent):
            g0.add_edge(entity_node(e.parent), n, lag=0)
            full.add_edge(entity_node(e.parent), n, lag=0)
    for v in spec.variables:
        for p in v.parents:
            full.add_edge(p.variable, v.variable_id, lag=p.lag, aggregation=p.aggregation)
            if p.lag == 0:
                g0.add_edge(p.variable, v.variable_id, lag=0)
    # rows of a dynamic entity must exist before anything on that grain is evaluated
    for e in dynamic:
        n = entity_node(e.entity_id)
        for v in spec.variables_of(e.entity_id):
            g0.add_edge(n, v.variable_id, lag=0)
            full.add_edge(n, v.variable_id, lag=0)
    return g0, full


def node_temporal(spec: SCMSpec, node: str) -> bool:
    if node.startswith(ENTITY_NODE):
        return is_temporal_entity(spec, node[len(ENTITY_NODE):])
    v = spec.variable(node)
    return bool(v and is_temporal_entity(spec, v.entity))


def execution_plan(spec: SCMSpec) -> ExecutionPlan:
    g0, full = build_graphs(spec)
    unknown = [n for n in g0.nodes if not n.startswith(ENTITY_NODE) and spec.variable(n) is None]
    if unknown:
        raise GraphError(f"edges reference unknown variables: {sorted(unknown)}")
    try:
        cycle = nx.find_cycle(g0)
        raise GraphError("cycle within one time step: " + " -> ".join(f"{a}" for a, _ in cycle) + f" -> {cycle[0][0]}"
                         + " (use a lagged parent to express feedback over time)")
    except nx.NetworkXNoCycle:
        pass
    order = list(nx.lexicographical_topological_sort(g0))
    temporal = {n: node_temporal(spec, n) for n in order}
    plan = ExecutionPlan(within_step=g0, full=full)
    post: set[str] = set()
    for n in order:
        if temporal[n]:
            bad = [a for a in nx.ancestors(g0, n) if a in post]
            if bad:
                raise GraphError(f"time-varying node {n} depends on static node(s) {sorted(bad)} that are computed from "
                                 "time-varying data; introduce a lag or make them temporal")
            plan.loop.append(n)
        elif any(temporal[a] for a in nx.ancestors(g0, n)):
            plan.post.append(n)
            post.add(n)
        else:
            plan.pre.append(n)
    return plan


def check_lags(spec: SCMSpec) -> list[str]:
    errs = []
    for v in spec.variables:
        for p in v.parents:
            if p.lag == 0:
                continue
            src = spec.variable(p.variable)
            if not src:
                continue
            if not is_temporal_entity(spec, v.entity):
                errs.append(f"{v.variable_id}: lagged parent {p.variable} requires a time-varying grain")
            if not is_temporal_entity(spec, src.entity):
                errs.append(f"{v.variable_id}: lagged parent {p.variable} is not time-varying")
            if is_dynamic_entity(spec, src.entity) and not p.aggregation:
                errs.append(f"{v.variable_id}: lagged read of {p.variable} (rows created per step) needs an aggregation")
            if is_dynamic_entity(spec, v.entity):
                errs.append(f"{v.variable_id}: lagged parents are not supported on grains created per step")
    return errs


# =========================================================================== summary graph / evaluation
@dataclass
class TruthGraph:
    nodes: list[str]
    directed: set[tuple[str, str]]          # X -> Y in the latent projection (summary over lags)
    bidirected: set[frozenset]              # X <-> Y (latent confounding)
    lagged: set[tuple[str, str]]            # directed edges implied (also) through a time lag
    orientation: dict[frozenset, str]       # skeleton edge -> 'compelled' | 'undirected' | 'feedback' | 'confounded'

    def skeleton(self) -> set[frozenset]:
        return {frozenset(e) for e in self.directed} | set(self.bidirected)

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "directed": sorted(map(list, self.directed)),
                "bidirected": sorted(sorted(e) for e in self.bidirected),
                "lagged": sorted(map(list, self.lagged)),
                "orientation": {"|".join(sorted(k)): v for k, v in sorted(self.orientation.items(), key=lambda x: sorted(x[0]))}}


def summary_digraph(spec: SCMSpec) -> tuple[nx.DiGraph, set[tuple[str, str]]]:
    g = nx.DiGraph()
    lagged: set[tuple[str, str]] = set()
    for v in spec.variables:
        g.add_node(v.variable_id)
    for v in spec.variables:
        for p in v.parents:
            if p.variable == v.variable_id:
                continue  # self-lag (autoregression) is not a cross-variable edge
            g.add_edge(p.variable, v.variable_id)
            if p.lag > 0:
                lagged.add((p.variable, v.variable_id))
    return g, lagged


def latent_projection(g: nx.DiGraph, observed: Iterable[str], lagged: set[tuple[str, str]]) -> tuple[set, set, set]:
    obs = set(observed)
    directed: set[tuple[str, str]] = set()
    lag_out: set[tuple[str, str]] = set()
    for x in obs:
        # BFS through latent nodes only
        stack = [(c, (x, c) in lagged) for c in g.successors(x)]
        seen = set()
        while stack:
            n, lg = stack.pop()
            if (n, lg) in seen:
                continue
            seen.add((n, lg))
            if n in obs:
                if n != x:
                    directed.add((x, n))
                    if lg:
                        lag_out.add((x, n))
                continue
            for c in g.successors(n):
                stack.append((c, lg or (n, c) in lagged))
    bidirected: set[frozenset] = set()
    latents = [n for n in g.nodes if n not in obs]
    for lat in latents:
        reach: set[str] = set()
        stack = list(g.successors(lat))
        seen = set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in obs:
                reach.add(n)
                continue
            stack.extend(g.successors(n))
        if any(p in obs for p in g.predecessors(lat)):
            # a latent with observed parents is a mediator, its confounding is covered by directed paths
            pass
        r = sorted(reach)
        for i in range(len(r)):
            for j in range(i + 1, len(r)):
                a, b = r[i], r[j]
                if (a, b) not in directed and (b, a) not in directed:
                    bidirected.add(frozenset((a, b)))
    return directed, bidirected, lag_out


def cpdag_orientation(nodes: list[str], directed: set[tuple[str, str]], bidirected: set[frozenset],
                      lagged: set[tuple[str, str]]) -> dict[frozenset, str]:
    """Which skeleton edges have an orientation identifiable from observational data (+ time order).

    Compelled = oriented in the CPDAG (v-structures + Meek rules R1–R3), or implied by a time lag.
    Edges that form a 2-cycle in the summary graph are 'feedback'; edges touching latent confounding
    are 'confounded' (no strict orientation scoring)."""
    out: dict[frozenset, str] = {}
    pair_dirs: dict[frozenset, set[tuple[str, str]]] = {}
    for a, b in directed:
        pair_dirs.setdefault(frozenset((a, b)), set()).add((a, b))
    adj = {n: set() for n in nodes}
    for k in pair_dirs:
        a, b = tuple(k)
        adj[a].add(b)
        adj[b].add(a)
    oriented: set[tuple[str, str]] = set()
    for k, dirs in pair_dirs.items():
        if len(dirs) == 2:
            out[k] = "feedback"
        elif next(iter(dirs)) in lagged:
            oriented.add(next(iter(dirs)))
    single = {next(iter(d)) for k, d in pair_dirs.items() if len(d) == 1}
    parents = {n: {a for a, b in single if b == n} for n in nodes}
    # v-structures
    for z in nodes:
        ps = sorted(parents[z])
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                x, y = ps[i], ps[j]
                if y not in adj[x]:
                    oriented.add((x, z))
                    oriented.add((y, z))

    def undirected(a: str, b: str) -> bool:
        return (a, b) in single or (b, a) in single

    changed = True
    while changed:
        changed = False
        for a, b in list(single):
            for x, y in ((a, b), (b, a)):
                if (x, y) in oriented or (y, x) in oriented or not undirected(x, y):
                    continue
                # R1: w -> x - y, w not adjacent to y  =>  x -> y
                if any((w, x) in oriented and y not in adj[w] and w != y for w in adj[x]):
                    oriented.add((x, y)); changed = True; continue
                # R2: x -> w -> y  =>  x -> y
                if any((x, w) in oriented and (w, y) in oriented for w in adj[x] & adj[y]):
                    oriented.add((x, y)); changed = True; continue
                # R3: x - w1 -> y, x - w2 -> y, w1, w2 non-adjacent  =>  x -> y
                ws = [w for w in adj[x] & adj[y] if (w, y) in oriented and (w, x) not in oriented and (x, w) not in oriented]
                if any(w2 not in adj[w1] for i, w1 in enumerate(ws) for w2 in ws[i + 1:]):
                    oriented.add((x, y)); changed = True
    for k, dirs in pair_dirs.items():
        if k in out:
            continue
        a, b = tuple(k)
        if k in bidirected:
            out[k] = "compelled" if next(iter(dirs)) in lagged else "confounded"
        elif (a, b) in oriented or (b, a) in oriented:
            out[k] = "compelled"
        else:
            out[k] = "undirected"
    for k in bidirected:
        out.setdefault(k, "confounded")
    return out


def truth_graph(spec: SCMSpec, observed: Optional[Iterable[str]] = None) -> TruthGraph:
    """Ground-truth graph over the *observable* variables (spec-level observability)."""
    g, lagged = summary_digraph(spec)
    if observed is None:
        observed = [v.variable_id for v in spec.variables
                    if v.observability == "observed" and v.column and (spec.entity(v.entity) or None)
                    and spec.entity(v.entity).export and v.role not in ("label", "text")]
    obs = sorted(set(observed))
    directed, bidirected, lag_out = latent_projection(g, obs, lagged)
    orientation = cpdag_orientation(obs, directed, bidirected, lag_out)
    return TruthGraph(obs, directed, bidirected, lag_out, orientation)


def descendants(spec: SCMSpec, vid: str) -> set[str]:
    g, _ = summary_digraph(spec)
    return nx.descendants(g, vid) if vid in g else set()
