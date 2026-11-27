"""Facts for documents grounded in an existing data run.

One row of the source table = one document. The row is enriched generically from the schema:
* FK parents are joined in (e.g. purchase_orders.supplier_id -> supplier_name, supplier_city, …),
* child tables become line items (e.g. purchase_order_lines -> items with product name and prices),
* amounts are computed deterministically (line amounts, total, VAT) – never by the LLM.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..contracts.docs import FactField
from ..contracts.spec import Column, ScenarioSpec, Table
from ..storage import runs as store

VAT_RATE = 0.21
MAX_PARENT_COLS = 5
MAX_ITEMS = 60
ITEMS_FIELD = "items"


@dataclass
class SourceInfo:
    spec: ScenarioSpec
    table: Table
    pk: Optional[str]
    parents: list[tuple[Column, Table, list[Column]]]       # (fk column, referenced table, picked columns)
    child: Optional[tuple[Table, Column]]                     # (child table, its FK column to us)
    child_parents: list[tuple[Column, Table, list[Column]]]


def _prefix(fk_col: str) -> str:
    return re.sub(r"_id$", "", fk_col)


_PREFIX_CS = {"supplier": "Dodavatel", "store": "Prodejna", "customer": "Zákazník", "product": "Produkt",
              "po": "Objednávka", "transaction": "Transakce", "patient": "Pacient", "employee": "Zaměstnanec"}


def _label(fk_col: str) -> str:
    p = _prefix(fk_col)
    return _PREFIX_CS.get(p, p.replace("_", " ").capitalize())


def _descriptive(t: Table, n: int = MAX_PARENT_COLS) -> list[Column]:
    cols = [c for c in t.columns if not c.pk and not c.fk and c.type in ("text", "int", "float", "date")]
    # names first, then prices/costs (needed for amounts), then the rest in schema order
    def rank(c: Column) -> int:
        if c.name in ("name", "title", "sku", "first_name", "last_name"):
            return 0
        return 1 if ("price" in c.name or "cost" in c.name) else 2
    cols.sort(key=rank)
    return cols[:n]


def inspect_source(run_id: str, table: str) -> SourceInfo:
    run = store.load_run(run_id)
    if not run or "spec" not in run:
        raise ValueError(f"data run {run_id} not found")
    spec = ScenarioSpec.model_validate(run["spec"])
    t = spec.table(table)
    if not t:
        raise ValueError(f"table {table} not in run {run_id}")
    pk = t.pk[0] if len(t.pk) == 1 else None
    parents = []
    for c in t.columns:
        if c.fk:
            rt = spec.table(c.fk.split(".")[0])
            if rt and rt.name != t.name:
                parents.append((c, rt, _descriptive(rt)))
    child = None
    child_parents: list = []
    if pk:
        # only composition tables (order lines, receipt items…) become line items – not every referencing table
        singular = re.sub(r"ies$", "y", t.name) if t.name.endswith("ies") else re.sub(r"s$", "", t.name)
        cands = [(ct, c) for ct in spec.tables for c in ct.columns if c.fk == f"{t.name}.{pk}" and ct.name != t.name
                 and ct.name.startswith(singular)
                 and re.search(r"lines?$|items?$|details?$|positions?$|polozk", ct.name)]
        if cands:
            child = cands[0]
            ct = child[0]
            for c in ct.columns:
                if c.fk and c is not child[1]:
                    rt = spec.table(c.fk.split(".")[0])
                    if rt:
                        child_parents.append((c, rt, _descriptive(rt, 4)))
    return SourceInfo(spec, t, pk, parents, child, child_parents)


def _price_key(item: dict, purchase: bool) -> Optional[str]:
    keys = list(item)
    if "unit_price" in item:
        return "unit_price"
    cost = next((k for k in keys if k.endswith("unit_cost")), None)
    price = next((k for k in keys if k.endswith("unit_price")), None)
    return (cost or price) if purchase else (price or cost)


def _qty_key(item: dict) -> Optional[str]:
    for k in ("quantity", "qty", "qty_received", "qty_ordered"):
        if k in item:
            return k
    return next((k for k in item if "qty" in k or "quantity" in k), None)


def is_purchase(info: SourceInfo) -> bool:
    """Purchase documents (we buy from a supplier) show cost prices and add VAT on top; sales documents use
    VAT-inclusive retail prices."""
    return any(fk.fk.startswith("suppliers.") for fk, _, _ in info.parents) or "purchase" in info.table.name


def source_fields(info: SourceInfo) -> list[FactField]:
    t = info.table
    fields: list[FactField] = []
    seen: set[str] = set()

    def add(f: FactField) -> None:
        if f.name not in seen:  # never duplicate an existing column (e.g. transactions.total_amount)
            seen.add(f.name)
            fields.append(f)

    for c in t.columns:
        add(FactField(name=c.name, type=c.type, description=c.description, enum_values=c.enum_values,
                      min=c.min, max=c.max, must_mention=c.pk or c.type in ("date", "datetime")))
    for fk, rt, cols in info.parents:
        for c in cols:
            add(FactField(name=f"{_prefix(fk.name)}_{c.name}", type=c.type,
                          description=f"{_label(fk.name)} – {c.description or c.name}",
                          must_mention=c.name in ("name", "title")))
    if info.child:
        add(FactField(name=ITEMS_FIELD, type="text", description=f"Položky z tabulky {info.child[0].name} (JSON) – "
                      "vykreslí se deterministicky v sekci typu data_table"))
        add(FactField(name="items_count", type="int", description="Počet položek"))
        if _has_amounts(info):
            add(FactField(name="net_amount", type="float", description="Základ daně – bez DPH (Kč)", must_mention=True))
            add(FactField(name="vat_amount", type="float", description=f"DPH {int(VAT_RATE * 100)} % (Kč)", must_mention=True))
            add(FactField(name="total_with_vat", type="float", description="Celkem vč. DPH (Kč)", must_mention=True))
    return fields


def _has_amounts(info: SourceInfo) -> bool:
    if not info.child:
        return False
    names = {c.name for c in info.child[0].columns}
    names |= {f"{_prefix(fk.name)}_{c.name}" for fk, _, cols in info.child_parents for c in cols}
    return any("price" in n or "cost" in n or n == "line_total" for n in names) and \
        any("qty" in n or "quantity" in n or n == "line_total" for n in names)


def _q(n: str) -> str:
    return '"' + n.replace('"', "") + '"'


def build_source_facts(run_id: str, table: str, count: int, seed: int, filter_col: Optional[str] = None,
                       filter_val: Optional[str] = None) -> tuple[list[FactField], pd.DataFrame]:
    info = inspect_source(run_id, table)
    t = info.table
    conn = sqlite3.connect(f"file:{store.db_path(run_id)}?mode=ro", uri=True)
    try:
        where, params = "", []
        if filter_col:
            if not t.column(filter_col):
                raise ValueError(f"unknown filter column {filter_col}")
            where, params = f"WHERE {_q(filter_col)} = ?", [filter_val]
        key = info.pk or "rowid"
        ids = [r[0] for r in conn.execute(f"SELECT {_q(key) if info.pk else 'rowid'} FROM {_q(t.name)} {where}", params)]
        if not ids:
            raise ValueError("no source rows match the filter")
        rng = np.random.default_rng([seed, 7])
        pick = sorted(rng.choice(len(ids), size=min(count, len(ids)), replace=False))
        chosen = [ids[i] for i in pick]
        ph = ",".join("?" * len(chosen))
        base = pd.read_sql_query(f"SELECT * FROM {_q(t.name)} WHERE {_q(key) if info.pk else 'rowid'} IN ({ph})",
                                 conn, params=chosen)
        if info.pk:
            base = base.set_index(info.pk).loc[chosen].reset_index()
        # FK parents
        for fk, rt, cols in info.parents:
            rc = fk.fk.split(".")[1]
            vals = [v for v in base[fk.name].dropna().unique().tolist()]
            if not vals or not cols:
                continue
            ref = pd.read_sql_query(
                f"SELECT {_q(rc)}, {', '.join(_q(c.name) for c in cols)} FROM {_q(rt.name)} "
                f"WHERE {_q(rc)} IN ({','.join('?' * len(vals))})", conn, params=vals)
            ref = ref.rename(columns={c.name: f"{_prefix(fk.name)}_{c.name}" for c in cols}).rename(columns={rc: fk.name})
            ref = ref[[fk.name] + [c for c in ref.columns if c != fk.name and c not in base.columns]]
            base = base.merge(ref, on=fk.name, how="left")
        # child items
        if info.child:
            ct, fkc = info.child
            items = pd.read_sql_query(f"SELECT * FROM {_q(ct.name)} WHERE {_q(fkc.name)} IN ({ph})", conn, params=chosen)
            for fk, rt, cols in info.child_parents:
                rc = fk.fk.split(".")[1]
                vals = items[fk.name].dropna().unique().tolist()
                if not vals or not cols:
                    continue
                ref = pd.read_sql_query(
                    f"SELECT {_q(rc)}, {', '.join(_q(c.name) for c in cols)} FROM {_q(rt.name)} "
                    f"WHERE {_q(rc)} IN ({','.join('?' * len(vals))})", conn, params=vals)
                ref = ref.rename(columns={c.name: f"{_prefix(fk.name)}_{c.name}" for c in cols}).rename(columns={rc: fk.name})
                ref = ref[[fk.name] + [c for c in ref.columns if c != fk.name and c not in items.columns]]
                items = items.merge(ref, on=fk.name, how="left")
            purchase = is_purchase(info)
            # drop technical ids; purchase docs show cost prices, sales docs never reveal internal costs
            drop = [c.name for c in ct.columns if c.pk] + [fkc.name] + [fk.name for fk, _, _ in info.child_parents]
            has_cost = [c for c in items.columns if c.endswith("unit_cost")]
            has_price = [c for c in items.columns if c.endswith("unit_price") and c != "unit_price"]
            if purchase and has_cost:
                drop += has_price
            elif not purchase:
                drop += has_cost
            full = {k: g.to_dict(orient="records") for k, g in items.groupby(fkc.name)}
            keep_cols = [c for c in items.columns if c not in set(drop)]
            item_json, counts, totals = [], [], []
            for pk_val in base[info.pk]:
                rows_full = full.get(pk_val, [])
                total = 0.0
                rows = []
                for r in rows_full:
                    out = {k: r[k] for k in keep_cols}
                    if "line_total" in r and r["line_total"] is not None:
                        amount = float(r["line_total"])
                    else:
                        q, p = _qty_key(r), _price_key(r, purchase)
                        amount = float(r[q]) * float(r[p]) if q and p and r.get(q) is not None and r.get(p) is not None else 0.0
                        if q and p:
                            out["line_amount"] = round(amount, 2)
                    total += amount
                    rows.append(out)
                rows = rows[:MAX_ITEMS]
                item_json.append(json.dumps(rows, ensure_ascii=False, default=str))
                counts.append(len(rows))
                totals.append(round(total, 2))
            base[ITEMS_FIELD] = item_json
            if "items_count" not in base.columns:
                base["items_count"] = counts
            if _has_amounts(info):
                if purchase:  # cost prices are net -> VAT on top
                    net = totals
                    gross = [round(x * (1 + VAT_RATE), 2) for x in net]
                else:  # retail prices include VAT -> extract it
                    gross = totals
                    net = [round(x / (1 + VAT_RATE), 2) for x in gross]
                base["net_amount"] = [round(x, 2) for x in net]
                base["vat_amount"] = [round(g - n, 2) for g, n in zip(gross, net)]
                base["total_with_vat"] = [round(x, 2) for x in gross]
    finally:
        conn.close()
    fields = [f for f in source_fields(info) if f.name in base.columns]
    df = base[[f.name for f in fields]].copy()
    df.insert(0, "doc_id", range(1, len(df) + 1))
    return fields, df


def describe_source(run_id: str, table: str) -> dict[str, Any]:
    """Compact description for the STRONG planner: fields + one fully enriched sample row."""
    info = inspect_source(run_id, table)
    fields, sample = build_source_facts(run_id, table, 1, 0)
    row = sample.iloc[0].to_dict()
    if ITEMS_FIELD in row:
        items = json.loads(row[ITEMS_FIELD])
        row[ITEMS_FIELD] = items[:3] + ([f"… celkem {len(items)} položek"] if len(items) > 3 else [])
    return {"table": table, "description": info.table.description,
            "fields": [f.model_dump() for f in fields], "sample_row": row,
            "has_items": bool(info.child), "items_from": info.child[0].name if info.child else None}


SUGGESTIONS = [
    (r"lines?$|items?$", "Doklad k jedné položce (spíše použijte nadřazenou tabulku – položky se připojí automaticky)"),
    (r"purchase_order|^orders?$|objedn", "Faktura (daňový doklad) k objednávce – dodavatel, odběratel, položky, DPH, splatnost"),
    (r"deliver", "Dodací list k dodávce – dodavatel, místo dodání, plánované a skutečné datum, potvrzení převzetí"),
    (r"^transactions?$|receipt|sale", "Účtenka / zjednodušený daňový doklad k nákupu – prodejna, položky, celkem, způsob platby"),
    (r"complaint|stiz", "Formální odpověď zákazníkovi na stížnost – omluva, vysvětlení, další postup"),
    (r"customer", "Uvítací dopis do věrnostního programu pro zákazníka"),
    (r"supplier", "Roční hodnocení dodavatele – spolehlivost, kvalita, doporučení"),
    (r"store", "Měsíční provozní report prodejny pro vedení"),
    (r"product", "Produktový list – popis, cena, kategorie, dodavatel"),
    (r"stockout", "Interní hlášení výpadku zásob pro nákup"),
]


def suggest_document(table: str) -> str:
    for pat, text in SUGGESTIONS:
        if re.search(pat, table):
            return text
    return f"Dokument ke každému záznamu tabulky {table} (shrnutí údajů pro adresáta)"
