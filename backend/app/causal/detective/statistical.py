"""Deterministic reference detective (no LLM).

A generic, scripted analyst that uses exactly the same sandboxed tools as the LLM detective:
schema inspection → data-quality reports → join checks → per-table analytic datasets over many-to-one
FK joins → PC discovery with effective sample sizes → adjusted regressions → per-group change-point
scans of every time series. The interactive SCM detective ranks multiple group changes against controls.

It is cheap enough for difficulty screening and makes optimisation tests reproducible. Results are
labelled ``agent.kind = "statistical"`` – it is a reference analyst, not an LLM.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import numpy as np

from ..contracts import DetectiveConfig
from ..results import DetectiveResult, EstimatedEffect, InferredRelationship, KeyEvent, PublicSchema
from .tools import Toolbox, _q

MAX_PC_COLUMNS = 12
MAX_GROUPS = 50
EVENT_T = 5.0
MAX_CAUSE_CANDIDATES = 12


class SchemaGraph:
    def __init__(self, schema: PublicSchema):
        self.tables = {t.name: t for t in schema.tables}
        self.fks = {t.name: {fk["column"]: fk["references"].split(".") for fk in t.foreign_keys} for t in schema.tables}
        self.children = defaultdict(list)
        for t, fks in self.fks.items():
            for col, (rt, rc) in fks.items():
                self.children[rt].append((t, col, rc))

    def paths(self, table: str, max_hops: int = 3) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
        """Reachable parent tables with join chains [(alias_from, fk_col, parent_table, parent_pk)]."""
        out = []
        frontier = [(table, [])]
        seen = {table}
        for _ in range(max_hops):
            nxt = []
            for t, path in frontier:
                for col, (rt, rc) in self.fks.get(t, {}).items():
                    if rt in seen or rt not in self.tables:
                        continue
                    seen.add(rt)
                    p = path + [(t, col, rt, rc)]
                    out.append((rt, p))
                    nxt.append((rt, p))
            frontier = nxt
        return out


class StatisticalDetective:
    def __init__(self, toolbox: Toolbox, config: DetectiveConfig):
        self.tb = toolbox
        self.cfg = config
        self.g = SchemaGraph(toolbox.schema)
        self.issues: list[str] = []
        self.limitations: list[str] = []
        self.col_kind: dict[str, dict[str, str]] = {}
        self.dedupe: dict[str, bool] = {}
        self.queries: list[str] = []
        self.outcome_scans: list[tuple[str, str, dict[str, Any]]] = []

    # ------------------------------------------------------------------ helpers
    def call(self, tool: str, **args: Any):
        item = self.tb.call(tool, **args)
        if "query" in args:
            self.queries.append(args["query"])
        return item

    def src(self, table: str) -> str:
        """Table reference; deduplicated tables are read from a materialised CTE (see ``sql``)."""
        t = self.g.tables[table]
        if self.dedupe.get(table) and t.primary_key:
            return _q(f"dedup_{table}")
        return _q(table)

    def sql(self, body: str) -> str:
        """Prefix the CTEs of deduplicated tables referenced in ``body`` (keeps the first row per key)."""
        ctes = []
        for table, on in self.dedupe.items():
            t = self.g.tables.get(table)
            if on and t and t.primary_key and f'"dedup_{table}"' in body:
                pk = _q(t.primary_key[0])
                ctes.append(f'"dedup_{table}" AS MATERIALIZED (SELECT * FROM {_q(table)} WHERE rowid IN '
                            f"(SELECT MIN(rowid) FROM {_q(table)} GROUP BY {pk}))")
        return ("WITH " + ", ".join(ctes) + " " + body) if ctes else body

    def classify(self) -> None:
        for t in self.g.tables.values():
            kinds = {}
            keys = set(t.primary_key) | {fk["column"] for fk in t.foreign_keys}
            for c in t.columns:
                if c.name in keys:
                    kinds[c.name] = "key"
                elif c.type == "date":
                    kinds[c.name] = "time"
                elif c.type in ("float", "int", "bool"):
                    kinds[c.name] = "numeric"
                else:
                    prof = self.call("profile_column", table=t.name, column=c.name)
                    d = (prof.result or {}).get("distinct", 0) if prof.ok else 0
                    if prof.ok and "mean" in (prof.result or {}) and (prof.result or {}).get("non_numeric", 0) < 0.05 * max(1, prof.result["n"]):
                        kinds[c.name] = "numeric"
                        self.issues.append(f"{t.name}.{c.name}: numbers stored as text / inconsistent formatting")
                    elif 2 <= d <= 12:
                        kinds[c.name] = "categorical"
                    else:
                        kinds[c.name] = "text"
            self.col_kind[t.name] = kinds

    def value_expr(self, alias: str, col: str, kind: str) -> str:
        c = f"{alias}.{_q(col)}"
        if kind == "numeric":
            return f"CAST(REPLACE({c}, ',', '.') AS REAL)" if self.col_kind else c
        return c

    # ------------------------------------------------------------------ steps
    def quality(self) -> None:
        self.call("inspect_schema")
        miss = self.call("get_missingness_report")
        for m in (miss.result or [])[:30]:
            if m["null_share"] > 0.005:
                self.issues.append(f"{m['table']}.{m['column']}: {m['null_share']:.1%} missing values")
        dup = self.call("get_duplicate_report")
        for d in dup.result or []:
            if d.get("duplicate_primary_keys"):
                self.issues.append(f"{d['table']}: {d['duplicate_primary_keys']} duplicated primary keys "
                                   "(deduplicated by keeping the first row)")
                self.dedupe[d["table"]] = True
            if d.get("duplicate_rows_ignoring_pk"):
                self.issues.append(f"{d['table']}: {d['duplicate_rows_ignoring_pk']} rows duplicated under another id")

    def analytic_dataset(self, table: str) -> Optional[tuple[str, list[str], dict[str, str], bool]]:
        kinds = self.col_kind[table]
        select, cols, units = [], [], {}
        t = self.g.tables[table]
        pk = t.primary_key[0] if t.primary_key else None
        for c, k in kinds.items():
            if k == "numeric":
                name = f"{table}.{c}"
                select.append(f"{self.value_expr('t0', c, k)} AS {_q_alias(name)}")
                cols.append(name)
                if pk:
                    units[name] = f"{table}.{pk}"
        joins = []
        for i, (col, (rt, rc)) in enumerate(self.g.fks.get(table, {}).items(), start=1):
            if rt not in self.g.tables:
                continue
            chk = self.call("check_join", left_table=table, left_column=col, right_table=rt, right_column=rc)
            if not chk.ok:
                continue
            if not chk.result.get("many_to_one"):
                self.limitations.append(f"join {table}.{col} -> {rt}.{rc} is not many-to-one in the observed data "
                                        "(duplicates); parent rows were deduplicated before joining")
                self.dedupe[rt] = True
            if chk.result.get("left_orphans"):
                self.issues.append(f"{table}.{col}: {chk.result['left_orphans']} values without a matching {rt} row")
            alias = f"p{i}"
            joins.append(f"LEFT JOIN {self.src(rt)} {alias} ON t0.{_q(col)} = {alias}.{_q(rc)}")
            select.append(f"t0.{_q(col)} AS {_q_alias(table + '.' + col)}")
            for c, k in self.col_kind[rt].items():
                if k == "numeric":
                    name = f"{rt}.{c}"
                    select.append(f"{self.value_expr(alias, c, k)} AS {_q_alias(name)}")
                    cols.append(name)
                    units[name] = f"{table}.{col}"
        if pk:
            select.append(f"t0.{_q(pk)} AS {_q_alias(table + '.' + pk)}")
        if len(cols) < 2:
            return None
        temporal = any(k == "time" for k in kinds.values())
        q = self.sql(f"SELECT {', '.join(select)} FROM {self.src(table)} t0 {' '.join(joins)}")
        return q, cols[:MAX_PC_COLUMNS * 2], units, temporal

    def discovery(self) -> tuple[list[InferredRelationship], list[EstimatedEffect]]:
        edges: dict[frozenset, dict[str, Any]] = {}
        datasets: dict[str, tuple[str, list[str]]] = {}
        for table in self.g.tables:
            ds = self.analytic_dataset(table)
            if not ds:
                continue
            q, cols, units, temporal = ds
            own = [c for c in cols if c.startswith(table + ".")]
            other = [c for c in cols if not c.startswith(table + ".")]
            cols = (own + other)[:MAX_PC_COLUMNS]
            item = self.call("discover_causal_graph", query=q, columns=cols, alpha=self.cfg.alpha,
                             unit_columns={c: units[c] for c in cols if c in units}, temporal=temporal)
            if not item.ok or (item.result or {}).get("status") != "ok":
                continue
            datasets[table] = (q, cols)
            for note in item.result.get("notes", []):
                self.limitations.append(f"{table}: {note}")
            for e in item.result["edges"]:
                key = frozenset((e["source"], e["target"]))
                native = sum(x.startswith(table + ".") for x in key)
                prev = edges.get(key)
                if prev is None or native > prev["native"]:
                    edges[key] = {**e, "native": native, "evidence": item.evidence_id, "table": table,
                                  "votes": defaultdict(int, (prev or {}).get("votes", {}))}
                edges[key]["votes"][(e["source"], e["target"], e["type"])] += 1
        rels: list[InferredRelationship] = []
        effects: list[EstimatedEffect] = []
        parents: dict[str, list[str]] = defaultdict(list)
        for e in edges.values():
            if e["type"] == "directed":
                parents[e["target"]].append(e["source"])
        for e in edges.values():
            r = abs(e.get("r", 0.0))
            conf = float(np.clip(0.45 + 0.5 * min(1.0, r * 2.5) - 20 * e.get("p_max", 0.0), 0.3, 0.95))
            ev = [e["evidence"]]
            direction, strength = ("+" if e.get("r", 0) >= 0 else "-"), None
            q, cols = datasets.get(e["table"], (None, []))
            if q and (e["type"] == "directed" or self.cfg.outcome_aware_ranking):
                adj = [p for p in parents[e["target"]] if p != e["source"] and p in cols][:4]
                reg = self.call("fit_linear_regression", query=q, target=e["target"], features=[e["source"], *adj])
                if reg.ok:
                    coef = reg.result["coefficients"].get(e["source"], {})
                    if coef.get("estimate") is not None:
                        strength = coef["estimate"]
                        direction = "+" if strength >= 0 else "-"
                        ev.append(reg.evidence_id)
                        effect_type = "association" if self.cfg.outcome_aware_ranking else ("direct" if adj else "total")
                        effects.append(EstimatedEffect(source=e["source"], target=e["target"],
                                                       effect_type=effect_type,
                                                       estimate=float(strength), standard_error=coef.get("se"),
                                                       adjustment_set=adj, evidence_ids=[reg.evidence_id]))
            rels.append(InferredRelationship(
                source=e["source"], target=e["target"], edge_type=e["type"], estimated_direction=direction,
                estimated_strength=strength, confidence=round(conf, 3), evidence_ids=ev,
                identification_status="partially_identified" if e["type"] == "directed" else "not_identified",
                note=f"PC on {e['table']} (r = {e.get('r')})"))
        return rels, effects

    def group_keys(self, table: str) -> list[tuple[str, str, list[str]]]:
        """(name 'table.column', select expression, join clauses) of low-cardinality grouping keys."""
        out = []
        for col, (rt, rc) in self.g.fks.get(table, {}).items():
            out.append((f"{table}.{col}", f"t0.{_q(col)}", []))
        for rt, path in self.g.paths(table):
            joins, alias = [], "t0"
            for i, (frm, col, ptab, ppk) in enumerate(path, start=1):
                a = f"j{i}"
                joins.append(f"JOIN {self.src(ptab)} {a} ON {alias}.{_q(col)} = {a}.{_q(ppk)}")
                alias = a
            for col, (r2, rc2) in self.g.fks.get(rt, {}).items():
                out.append((f"{rt}.{col}", f"{alias}.{_q(col)}", list(joins)))
        uniq, seen = [], set()
        for name, expr, joins in out:
            if name in seen:
                continue
            seen.add(name)
            try:
                r = self.tb.db.query(self.sql(f"SELECT COUNT(DISTINCT {expr}) FROM {self.src(table)} t0 "
                                              f"{' '.join(joins)}"), limit=1, internal=True)
            except Exception as e:  # noqa: BLE001 - a failing helper query only skips this grouping
                self.limitations.append(f"grouping {name} skipped: {str(e)[:120]}")
                continue
            if 2 <= (r.rows[0][0] or 0) <= MAX_GROUPS:
                uniq.append((name, expr, joins))
        return uniq

    def events(self) -> list[KeyEvent]:
        found: list[dict[str, Any]] = []
        for table, kinds in self.col_kind.items():
            tcol = next((c for c, k in kinds.items() if k == "time"), None)
            if not tcol:
                continue
            series = [(f"{table}.{c}", self.value_expr("t0", c, "numeric")) for c, k in kinds.items() if k == "numeric"]
            series.append((f"{table}.__rows", "1"))
            for c, k in kinds.items():
                if k == "categorical":
                    prof = self.call("profile_column", table=table, column=c)
                    for lv in list((prof.result or {}).get("top_values", {}))[:4]:
                        series.append((f"{table}.{c}={lv}", f"CASE WHEN t0.{_q(c)} = '{lv.replace(chr(39), '')}' THEN 1 ELSE 0 END"))
            for gname, gexpr, joins in self.group_keys(table):
                for vname, vexpr in series:
                    agg = "SUM" if vname.endswith("__rows") or "=" in vname else "AVG"
                    q = self.sql(f"SELECT t0.{_q(tcol)} AS t, {gexpr} AS g, {agg}({vexpr}) AS v FROM {self.src(table)} t0 "
                                 f"{' '.join(joins)} WHERE {gexpr} IS NOT NULL GROUP BY 1, 2")
                    item = self.call("analyze_time_series", query=q, time_column="t", value_column="v",
                                     group_column="g")
                    if not item.ok:
                        continue
                    if self.cfg.outcome_aware_ranking and self.is_revenue(vname):
                        self.outcome_scans.append((vname, gname, item.args))
                    ss = item.result["series"]
                    if len(ss) < 2:
                        continue
                    ts = np.array([abs(s["t_stat"] or 0) for s in ss])
                    for s in ss:
                        tt = abs(s["t_stat"] or 0)
                        others = np.median([x for x in ts if x != tt] or [0])
                        if tt >= EVENT_T and tt > 3 * max(others, 1.0):
                            found.append({"variable": vname, "entity": gname, "group": s["group"],
                                          "start": s["change_point"], "t": s["t_stat"], "n_groups": len(ss),
                                          "before": s["mean_before"], "after": s["mean_after"],
                                          "evidence": item.evidence_id})
        # consolidate: one event per (variable, entity key) with all flagged groups
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for f in found:
            k = (f["variable"], f["entity"])
            m = merged.setdefault(k, {**f, "groups": [], "starts": []})
            m["groups"].append(f["group"])
            m["starts"].append(f["start"])
            if abs(f["t"]) > abs(m["t"]):
                m.update(t=f["t"], before=f["before"], after=f["after"])
        events = []
        for (var, ent), m in merged.items():
            if len(m["groups"]) > max(1, m["n_groups"] // 2):
                continue  # not group-specific
            start = sorted(s for s in m["starts"] if s)[0] if any(m["starts"]) else None
            direction = "+" if (m["after"] or 0) > (m["before"] or 0) else "-"
            ids = []
            for g in m["groups"]:
                try:
                    ids.append(int(float(g)))
                except (TypeError, ValueError):
                    pass
            events.append(KeyEvent(description=f"{var}: change {m['before']:.3g} → {m['after']:.3g} for "
                                               f"{ent} in {m['groups']} from {start}",
                                   variable=var, entity=ent, entity_ids=sorted(ids), start_time=start,
                                   direction=direction, confidence=float(min(0.95, 0.5 + abs(m["t"]) / 40)),
                                   evidence_ids=[m["evidence"]], role="consequence"))
        if not events:
            return []
        if self.cfg.rank_cause_candidates:
            for event in events:
                event.role = "other"
            return events
        # root cause: the earliest group-specific change on an entity-level grouping (few groups);
        # ties are broken by the coarser grouping and the stronger signal
        n_groups = {(m["variable"], m["entity"]): m["n_groups"] for m in merged.values()}
        events.sort(key=lambda e: (e.start_time or "9999", n_groups.get((e.variable, e.entity), 99), -e.confidence))
        coarse = [e for e in events if n_groups.get((e.variable, e.entity), 99) <= 12] or events
        root = coarse[0]
        root.role = "root_cause"
        return events

    @staticmethod
    def is_revenue(variable: str) -> bool:
        name = variable.rsplit(".", 1)[-1].lower().split("_")
        return bool({"revenue", "turnover", "trzby"} & set(name))

    def compare_event(self, event: KeyEvent):
        if not event.entity_ids or not event.start_time:
            return None
        scan = next((item for item in self.tb.log if item.evidence_id in event.evidence_ids
                     and item.tool == "analyze_time_series"), None)
        if not scan:
            return None
        options = {"refine_direction": event.direction} if self.cfg.outcome_aware_ranking else {}
        comparison = self.call("compare_group_changes",
                               **{key: scan.args[key] for key in ("query", "time_column", "group_column", "value_column")},
                               treated_groups=event.entity_ids, start=event.start_time, **options)
        if comparison.ok:
            event.evidence_ids.append(comparison.evidence_id)
            if self.cfg.outcome_aware_ranking:
                event.start_time = comparison.result["start"]
                event.description = (f"{event.variable}: změna oproti kontrole {comparison.result['estimate']:+.3g}, "
                                     f"skupina {event.entity} {event.entity_ids}, odhad začátku {event.start_time}")
        return comparison

    def rank_candidates(self, events: list[KeyEvent], relationships: list[InferredRelationship]) -> list[dict[str, Any]]:
        """Compare several observed changes before picking an explanation."""
        def group_key(entity: str | None) -> tuple[str, ...]:
            table, _, column = (entity or "").partition(".")
            return tuple(self.g.fks.get(table, {}).get(column, (table, column)))

        def same_group(first: KeyEvent, second: KeyEvent) -> bool:
            return group_key(first.entity) == group_key(second.entity) and bool(set(first.entity_ids) & set(second.entity_ids))

        def same_signal(first: dict, second: dict) -> bool:
            return all(first[k] == second[k] for k in ("variable", "start", "estimate", "ci95"))

        links: dict[str, set[str]] = defaultdict(set)
        for rel in relationships:
            links[rel.source].add(rel.target)
            if rel.edge_type != "directed":
                links[rel.target].add(rel.source)

        candidates = sorted(events, key=lambda e: (-e.confidence, e.start_time or "", e.variable or "",
                                                   e.entity or "", e.entity_ids))[:MAX_CAUSE_CANDIDATES]
        ranked = []
        for event in candidates:
            item = self.compare_event(event)
            if not item or not item.ok:
                continue
            result = item.result
            estimate = abs(result["estimate"])
            interval = result["ci95"]
            excludes_zero = bool(interval and interval[0] * interval[1] > 0)
            matches_direction = estimate > 0 and (result["estimate"] > 0) == (event.direction == "+")
            pre_drift = abs(result["pre_gap_slope"]) * (result["periods_before"] - 1)
            supported = matches_direction and (excludes_zero or interval is None) and pre_drift < estimate
            strength = min(8.0, 2 * estimate / (interval[1] - interval[0])) if interval and interval[1] > interval[0] else 2.0
            score = strength / (1 + pre_drift / estimate) if supported else 0.0
            ranked.append((event, {"variable": event.variable, "entity": event.entity, "entity_ids": event.entity_ids,
                                   "start": event.start_time, "estimate": result["estimate"], "ci95": interval,
                                   "score": score, "supported": supported, "evidence_ids": [item.evidence_id],
                                   "later_signals": [], "concurrent_signals": []}))
        for event, row in ranked:
            if not row["supported"]:
                continue
            neighbours = links.get(event.variable, set())
            linked = neighbours | {target for node in neighbours for target in links[node]}
            for other, outcome in ranked:
                if not (outcome["supported"] and other.variable != event.variable
                        and other.variable in linked and same_group(event, other)):
                    continue
                if row["start"] < outcome["start"]:
                    row["later_signals"].append(other.variable)
                    row["evidence_ids"].append(outcome["evidence_ids"][0])
                elif self.cfg.outcome_aware_ranking and row["start"] == outcome["start"]:
                    row["concurrent_signals"].append(other.variable)
            if self.cfg.outcome_aware_ranking:
                row["outcome"] = None
                if self.is_revenue(event.variable):
                    # The outcome itself can corroborate a hypothesis, but cannot explain its own decline.
                    continue
                compatible = [(variable, args) for variable, entity, args in self.outcome_scans if
                              group_key(event.entity) == group_key(entity)]
                checked = []
                for variable, args in sorted(compatible, key=lambda pair: (pair[0] not in linked, pair[0]))[:2]:
                    result = self.call("compare_group_changes", **args, treated_groups=event.entity_ids,
                                       start=event.start_time, window_periods=8)
                    if result.ok:
                        checked.append((variable, result))
                if checked:
                    variable, evidence = min(checked, key=lambda pair: (
                        not (pair[1].result["ci95"] and pair[1].result["ci95"][1] < 0),
                        pair[0] not in linked, pair[0]))
                    result = evidence.result
                    support = bool(result["ci95"] and result["ci95"][1] < 0)
                    row["outcome"] = {"variable": variable, "estimate": result["estimate"],
                                      "ci95": result["ci95"], "supported": support}
                    row["evidence_ids"].append(evidence.evidence_id)
                    if support:
                        row["score"] += 3 if variable in linked else 1
                    else:
                        row["score"] *= 0.6
        for _, row in ranked:
            if not row["supported"]:
                continue
            equivalents = [other for _, other in ranked if other["supported"] and same_signal(row, other)]
            row["later_signals"] = sorted({signal for other in equivalents for signal in other["later_signals"]})
            row["concurrent_signals"] = sorted({signal for other in equivalents for signal in other["concurrent_signals"]})
            row["evidence_ids"] = list(dict.fromkeys([*row["evidence_ids"], *(
                evidence for other in equivalents for evidence in other["evidence_ids"]
            )]))
            row["score"] = round(row["score"] + min(4, 2 * len(row["later_signals"])), 3)

        ranked.sort(key=lambda pair: (-pair[1]["score"], len(pair[0].entity_ids)))
        causes = [pair for pair in ranked if not (self.cfg.outcome_aware_ranking and self.is_revenue(pair[0].variable))]
        if causes and causes[0][1]["supported"]:
            winner, best = causes[0]
            rival = next((row for _, row in causes[1:] if not same_signal(best, row)
                          and row["evidence_ids"][0] not in best["evidence_ids"]), None)
            if not rival or best["score"] - rival["score"] >= 0.5:
                winner.role = "root_cause"
                best["selected"] = True
                winner.evidence_ids = list(dict.fromkeys([*winner.evidence_ids, *best["evidence_ids"]]))
                for other, row in ranked[1:]:
                    if not same_signal(best, row) and row["evidence_ids"][0] in best["evidence_ids"]:
                        other.role = "consequence"
        return [row for _, row in ranked]

    # ------------------------------------------------------------------ run
    def run(self) -> DetectiveResult:
        self.quality()
        self.classify()
        rels, effects = self.discovery()
        events = self.events()
        candidates = self.rank_candidates(events, rels) if self.cfg.rank_cause_candidates else []
        root = next((e for e in events if e.role == "root_cause"), None)
        if self.cfg.compare_detected_groups and not self.cfg.rank_cause_candidates and root:
            self.compare_event(root)
        summary = []
        if root:
            summary.append(f"Pravděpodobná výchozí událost: {root.description}.")
        elif self.cfg.rank_cause_candidates:
            summary.append("Dostupné důkazy nestačí k výběru jedné hlavní příčiny.")
        for e in [x for x in events if x.role != "root_cause"][:6]:
            summary.append(f"Další zjištěná změna: {e.description}.")
        strong = sorted(rels, key=lambda r: -r.confidence)[:6]
        if strong:
            summary.append("Nejsilnější vztahy: " + "; ".join(
                f"{r.source} {'→' if r.edge_type == 'directed' else '—'} {r.target} ({r.estimated_direction})" for r in strong))
        nodes = sorted({x for r in rels for x in (r.source, r.target)})
        return DetectiveResult(
            story_summary=" ".join(summary) or "Nebyla nalezena žádná výrazná událost ani vztah.",
            key_events=events,
            cause_candidates=candidates,
            inferred_causal_graph={"nodes": nodes, "edges": [{"source": r.source, "target": r.target,
                                                              "type": r.edge_type} for r in rels]},
            inferred_relationships=rels, estimated_effects=effects,
            alternative_explanations=["Korelace mezi sloupci rodičovských tabulek mohou být způsobeny "
                                      "nepozorovanými confoundery (PC předpokládá kauzální úplnost)."],
            supporting_evidence=[e for ev in events for e in ev.evidence_ids][:20],
            executed_queries=self.queries[:200],
            statistical_results=list(self.tb.log),
            discovered_data_quality_issues=sorted(set(self.issues)),
            limitations=sorted(set(self.limitations))[:40],
            unresolved_questions=["Orientace neorientovaných hran není z observačních dat identifikovatelná."]
            if any(r.edge_type == "undirected" for r in rels) else [],
            uncertainty="Závěry jsou založené na testech podmíněné nezávislosti a změnových bodech; "
                        "neorientované hrany a efekty bez úpravy o confoundery jsou nejisté.",
            agent={"kind": "statistical", "model": None, "mock": False, "tools": Toolbox.TOOLS,
                   "budget": {"max_queries": self.cfg.max_queries, "sample_rows": self.cfg.sample_rows},
                   "tool_calls": len(self.tb.log), "alpha": self.cfg.alpha, "seed": self.cfg.seed})


def _q_alias(name: str) -> str:
    return '"' + name.replace('"', "") + '"'
