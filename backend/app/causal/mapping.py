"""Mapping of the SCM onto the existing ``ScenarioSpec`` (tables, story) and onto the public schema
the detective may see. Keeps the existing UI / data browser / deterministic checks working."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..contracts.spec import CausalStep, Column, Meta, ScenarioSpec, Story, Table
from .contracts import SCMSpec
from .engine import own_key_columns
from .graph import is_temporal_entity
from .results import PublicColumn, PublicSchema, PublicTable

_TYPES = {"float": "float", "int": "int", "bool": "bool", "category": "text", "text": "text"}


def table_columns(scm: SCMSpec, eid: str, include_latent: bool = True) -> list[Column]:
    e = scm.entity(eid)
    cols: list[Column] = []
    keys = own_key_columns(scm, eid)
    fks: dict[str, str] = {}
    if e.kind == "child" and e.parent:
        p = scm.entity(e.parent)
        if p.export:
            fks[p.primary_key] = f"{p.name}.{p.primary_key}"
    if e.kind == "cross":
        for m in e.cross:
            me = scm.entity(m)
            if me.export:
                fks[me.primary_key] = f"{me.name}.{me.primary_key}"
    for a in e.assignments:
        te = scm.entity(a.entity)
        if te.export:
            fks[a.column or te.primary_key] = f"{te.name}.{te.primary_key}"
    for k in keys:
        cols.append(Column(name=k, type="int", pk=k == e.primary_key, fk=fks.get(k),
                           description="primární klíč" if k == e.primary_key else "cizí klíč"))
    if is_temporal_entity(scm, eid):
        cols.append(Column(name=scm.time.time_column if scm.time else "date", type="date", description="datum"))
    for v in scm.variables_of(eid):
        if not v.column or (not include_latent and v.observability != "observed"):
            continue
        cols.append(Column(name=v.column, type=_TYPES[v.data_type],
                           description=(v.description or v.public_description or v.name)
                           + (" [latentní]" if v.observability == "latent" else ""),
                           enum_values=list(v.categories) if v.data_type == "category" else [],
                           min=v.constraints.min, max=v.constraints.max))
    return cols


def scenario_tables(scm: SCMSpec) -> list[Table]:
    out = []
    for e in scm.entities:
        if not e.export:
            continue
        flags = ["scm", "fact" if is_temporal_entity(scm, e.entity_id) or e.kind == "child" else "dimension"]
        out.append(Table(name=e.name, description=f"{e.description or e.grain} (grain: {e.grain})".strip(),
                         strategy="simulation", row_count=0, columns=table_columns(scm, e.entity_id), flags=flags))
    return out


def scenario_spec_from_scm(scm: SCMSpec, base: Optional[ScenarioSpec] = None) -> ScenarioSpec:
    dates = scm.time.dates() if scm.time else []
    meta = Meta(name=scm.meta.name, domain=scm.meta.domain or "generic", data_language=scm.meta.language,
                period_start=dates[0] if dates else None, period_end=dates[-1] if len(dates) > 1 else None,
                seed=scm.meta.seed, archetype=None, company=scm.meta.description, environment="")
    steps = [CausalStep(id=f.fact_id, mechanism=f.description,
                        affected_entities=[f"{k}:{','.join(map(str, v))}" for k, v in f.entity_ids.items()],
                        tables=sorted({scm.column_of(x)[0] for x in [f.source, f.target, *f.evidence_variables]
                                       if x and scm.column_of(x)}),
                        start=str(scm.event(f.event_id).start_time) if f.event_id and scm.event(f.event_id) else None,
                        lag_days=f.expected_lag or 0)
             for f in scm.ground_truth.story_facts]
    story = Story(logline=scm.meta.refined_story or scm.meta.description or scm.meta.name, causal_chain=steps,
                  ground_truth_facts=[f.description for f in scm.ground_truth.story_facts])
    if base is not None:
        return base.model_copy(update={"meta": base.meta.model_copy(update={
            "period_start": meta.period_start, "period_end": meta.period_end, "seed": scm.meta.seed, "archetype": None}),
            "tables": scenario_tables(scm), "story": story, "knobs": [], "scm": scm})
    return ScenarioSpec(meta=meta, tables=scenario_tables(scm), story=story, scm=scm)


def public_schema(scm: SCMSpec, observed: dict[str, pd.DataFrame]) -> PublicSchema:
    """Neutral schema: names, types, expected PK/FK, public descriptions – nothing about the story."""
    tables = []
    for e in scm.entities:
        if not e.export or e.name not in observed:
            continue
        df = observed[e.name]
        cols = []
        typ = {c.name: c for c in table_columns(scm, e.entity_id)}
        pub = {v.column: v.public_description for v in scm.variables_of(e.entity_id) if v.column}
        fks = []
        for c in df.columns:
            spec_col = typ.get(c)
            cols.append(PublicColumn(name=c, type=spec_col.type if spec_col else str(df[c].dtype),
                                     description=pub.get(c, "") if spec_col and not spec_col.pk and not spec_col.fk
                                     else ("primary key" if spec_col and spec_col.pk else "foreign key" if spec_col and spec_col.fk else "")))
            if spec_col and spec_col.fk:
                fks.append({"column": c, "references": spec_col.fk})
        tables.append(PublicTable(name=e.name, description=e.public_description, columns=cols,
                                  primary_key=[e.primary_key] if e.primary_key in df.columns else [],
                                  foreign_keys=fks, rows=int(len(df))))
    return PublicSchema(tables=tables)


# =========================================================================== author-side evidence (existing report UI)
def _fk_edges(scm: SCMSpec) -> dict[str, list[tuple[str, str, str]]]:
    """entity -> [(fk column, referenced entity, referenced pk)] over exported entities."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for e in scm.entities:
        refs = []
        if e.kind == "child" and e.parent:
            p = scm.entity(e.parent)
            refs.append((p.primary_key, p.entity_id, p.primary_key))
        for m in e.cross:
            me = scm.entity(m)
            refs.append((me.primary_key, me.entity_id, me.primary_key))
        for a in e.assignments:
            te = scm.entity(a.entity)
            refs.append((a.column or te.primary_key, te.entity_id, te.primary_key))
        out[e.entity_id] = [r for r in refs if scm.entity(r[1]).export]
    return out


def join_path(scm: SCMSpec, src: str, key: str) -> Optional[tuple[list[str], str]]:
    """SQL joins from entity ``src`` (alias t0) to a table holding column ``key``; returns (joins, qualified key)."""
    edges = _fk_edges(scm)
    if key in own_key_columns(scm, src):
        return [], f't0."{key}"'
    frontier = [(src, "t0", [])]
    seen = {src}
    for depth in range(4):
        nxt = []
        for eid, alias, joins in frontier:
            for col, ref, rpk in edges.get(eid, []):
                if ref in seen:
                    continue
                seen.add(ref)
                a = f"j{depth}_{len(nxt)}"
                j = joins + [f'JOIN "{scm.entity(ref).name}" {a} ON {alias}."{col}" = {a}."{rpk}"']
                if key in own_key_columns(scm, ref):
                    return j, f'{a}."{key}"'
                nxt.append((ref, a, j))
        frontier = nxt
    return None


def author_evidence(scm: SCMSpec) -> list:
    """Evidence queries for the existing report / dashboard (author view, never shown to the detective)."""
    from ..contracts.spec import EvidenceQuery
    from .ground_truth import default_evidence_variables

    out = []
    if not scm.time:
        return out
    tcol = scm.time.time_column
    period = f"strftime('%Y-%m', t0.\"{tcol}\")" if scm.time.frequency == "D" else f't0."{tcol}"'
    for f in scm.ground_truth.story_facts:
        if not f.entity_ids:
            continue
        ent, ids = next(iter(f.entity_ids.items()))
        key = scm.entity(ent).primary_key if scm.entity(ent) else None
        for vid in default_evidence_variables(scm, f)[:3]:
            v = scm.variable(vid)
            e = scm.entity(v.entity) if v else None
            if not v or not v.column or not e or not e.export or not is_temporal_entity(scm, e.entity_id) or not key:
                continue
            jp = join_path(scm, e.entity_id, key)
            if not jp:
                continue
            joins, qkey = jp
            idl = ", ".join(str(int(i)) for i in ids)
            if v.data_type in ("category", "text"):
                val = "COUNT(*)"
                ylabel = f"počet řádků {e.name}"
            else:
                val = f'AVG(t0."{v.column}")'
                ylabel = f"průměr {v.column}"
            sql = (f"SELECT {period} AS period, CASE WHEN {qkey} IN ({idl}) THEN 'zasažené' ELSE 'kontrolní' END AS grp, "
                   f"ROUND({val}, 3) AS value FROM \"{e.name}\" t0 {' '.join(joins)} GROUP BY 1, 2 ORDER BY 1, 2")
            out.append(EvidenceQuery(id=f"ev_{f.fact_id}_{vid}"[:60], title=f"{f.description} – {e.name}.{v.column}",
                                     step_id=f.fact_id, sql=sql, chart="line", x="period", y=["value"], series="grp",
                                     expected=f"{ylabel}: zasažené ({ent} {idl}) vs. kontrolní skupina"))
    return out
