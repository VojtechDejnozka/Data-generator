"""Fixture responders for mock mode. Each returns JSON valid for the schema of its task."""
from __future__ import annotations

import hashlib
import random
from typing import Any

from ..presets import mock_data as md
from .mock import register_mock


def _rng(*parts: Any) -> random.Random:
    h = hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


# --------------------------------------------------------------------------- dimensions
@register_mock("dim_suppliers")
def _suppliers(prompt: str, ctx: dict) -> dict:
    items = []
    used: set[str] = set()
    for sid, cats in ctx["suppliers"].items():
        names = md.SUPPLIER_NAMES.get(cats[0], [f"Dodavatel {sid} s.r.o."])
        name = next((n for n in names if n not in used), f"{names[0].split()[0]} {sid} s.r.o.")
        used.add(name)
        city = md.CITIES[(int(sid) * 3) % len(md.CITIES)][0]
        slug = "".join(ch for ch in name.lower().split()[0] if ch.isalpha())
        items.append({"supplier_id": int(sid), "name": name, "city": city,
                      "contact_email": f"obchod@{slug[:12] or 'dodavatel'}.cz"})
    return {"items": items}


@register_mock("dim_products")
def _products(prompt: str, ctx: dict) -> dict:
    cat, n, off = ctx["category"], ctx["n"], ctx.get("offset", 0)
    lo, hi = ctx["price_range"]
    words = md.PRODUCT_WORDS.get(cat, [cat])
    r = _rng(cat, ctx.get("supplier_id"), off)
    sizes = ["250 g", "500 g", "1 kg", "1 l", "0,5 l", "150 g", "400 g", "2 l", "6 ks", "200 g"]
    items = []
    for i in range(n):
        k = off + i
        base = words[k % len(words)]
        brand = md.BRANDS[(k // len(words) + ctx.get("supplier_id", 0)) % len(md.BRANDS)]
        items.append({"name": f"{base} {brand} {sizes[(k * 7) % len(sizes)]}",
                      "unit_price": round(r.uniform(lo, hi * 0.8), 1)})
    return {"items": items}


@register_mock("dim_stores")
def _stores(prompt: str, ctx: dict) -> dict:
    items = []
    for i in range(ctx["n"]):
        city, region = md.CITIES[i % len(md.CITIES)] if i < len(md.CITIES) else md.CITIES[(i * 7) % len(md.CITIES)]
        suffix = md.STORE_SUFFIX[(i // len(md.CITIES)) % len(md.STORE_SUFFIX)] if i >= len(md.CITIES) else \
            md.STORE_SUFFIX[i % len(md.STORE_SUFFIX)]
        items.append({"name": f"Dobrý Košík {city} {suffix}", "city": city, "region": region})
    return {"items": items}


@register_mock("dim_customer_names")
def _names(prompt: str, ctx: dict) -> dict:
    return {"male_first": md.MALE_FIRST, "female_first": md.FEMALE_FIRST, "male_last": md.MALE_LAST,
            "female_last": md.FEMALE_LAST}


@register_mock("complaint_templates")
def _templates(prompt: str, ctx: dict) -> dict:
    return {"templates": [{"category": c, "text": t} for c, ts in md.COMPLAINT_TEMPLATES.items() for t in ts]}


# --------------------------------------------------------------------------- planning
@register_mock("plan_spec")
def _plan(prompt: str, ctx: dict) -> dict:
    from ..contracts.io import ScenarioInput
    from ..generation.scenarios.registry import MODULES

    inp = ScenarioInput.model_validate(ctx["input"])
    module = MODULES.get(inp.preset or "") or next(iter(MODULES.values()))
    spec = module.default_spec(inp).model_dump(mode="json")
    return {
        "spec": spec,
        "questions": [
            {"id": "q1", "question": "Má zpoždění dodavatele zasáhnout všechny prodejny, nebo jen jeden region?",
             "why": "Určuje, zda kontrolní skupinou budou i prodejny, nebo jen produkty ostatních dodavatelů.",
             "default_answer": "Všechny prodejny – kontrolní skupinou jsou produkty ostatních dodavatelů."},
            {"id": "q2", "question": "Mají zákazníci po opakovaných výpadcích přecházet ke konkurenci (pokles návštěvnosti)?",
             "why": "Zadání říká, že poptávka zůstává stabilní; churn by tento předpoklad porušil.",
             "default_answer": "Ne, návštěvnost zůstává stabilní."},
        ],
        "suggestions": [
            {"id": "sg1", "kind": "add_table", "title": "Přidat tabulku marketingových akcí (promotions)",
             "description": "Slevové akce jako další distraktor – generuje CHEAP model (llm_rows) s vazbou na produkty.",
             "table": {"name": "promotions", "description": "Marketingové slevové akce na produkty",
                       "strategy": "llm_rows", "row_count": 40, "flags": ["event"],
                       "columns": [
                           {"name": "promotion_id", "type": "int", "pk": True, "description": "ID akce"},
                           {"name": "product_id", "type": "int", "fk": "products.product_id", "description": "Produkt v akci"},
                           {"name": "title", "type": "text", "description": "Název akce (česky)"},
                           {"name": "start_date", "type": "date", "description": "Začátek akce"},
                           {"name": "end_date", "type": "date", "description": "Konec akce (7–14 dní po začátku)"},
                           {"name": "discount_pct", "type": "float", "min": 5, "max": 40, "description": "Sleva v %"},
                           {"name": "channel", "type": "text", "enum_values": ["leták", "web", "aplikace"],
                            "description": "Kanál"}]}},
            {"id": "sg2", "kind": "add_table", "title": "Přidat tabulku vedoucích prodejen (store_managers)",
             "description": "Master data o vedoucích prodejen – umožní analytikům ověřit hypotézu 'špatné vedení'.",
             "table": {"name": "store_managers", "description": "Vedoucí prodejen", "strategy": "llm_rows",
                       "row_count": 10, "flags": ["dimension"],
                       "columns": [
                           {"name": "manager_id", "type": "int", "pk": True},
                           {"name": "store_id", "type": "int", "fk": "stores.store_id"},
                           {"name": "full_name", "type": "text", "description": "Jméno a příjmení"},
                           {"name": "since_date", "type": "date", "description": "Ve funkci od"},
                           {"name": "experience_years", "type": "int", "min": 0, "max": 35}]}},
            {"id": "sg3", "kind": "add_causal_step", "title": "Doplnit krok: reakce nákupu (urgence u dodavatele)",
             "description": "Po měsíci výpadků nákupní oddělení urgují dodavatele – zaznamenat v poznámkách.",
             "causal_step": {"id": "s5", "mechanism": "Nákupní oddělení eskaluje problém u dodavatele",
                             "affected_entities": ["suppliers"], "tables": [], "start": "after s2",
                             "ramp_profile": "step", "expected_impact": "Dokumentační krok (bez dopadu na data)"}},
        ],
        "notes": "Mock planner: použita šablona modulu " + module.id,
    }


@register_mock("replan_spec")
def _replan(prompt: str, ctx: dict) -> dict:
    spec = ctx["spec"]
    note = "Mock přeplánování: zpětná vazba zapracována do popisu." if ctx.get("feedback") else "Mock přeplánování."
    if ctx.get("feedback"):
        spec["meta"]["environment"] = (spec["meta"].get("environment", "") + " | Zpětná vazba: " + ctx["feedback"])[:3900]
    return {"spec": spec, "questions": [], "suggestions": [], "notes": note}


@register_mock("generation_plan")
def _gen_plan(prompt: str, ctx: dict) -> dict:
    return ctx["plan"]


# --------------------------------------------------------------------------- validation
@register_mock("validate")
def _validate(prompt: str, ctx: dict) -> dict:
    checks, signals, knobs = ctx["checks"], ctx["signals"], ctx["knobs"]
    story_on = float(knobs.get("story_intensity", 1) or 0) > 0
    issues = []
    if checks.get("errors"):
        issues.append({"id": "i_checks", "severity": "critical",
                       "description": f"Selhaly deterministické kontroly: {', '.join(checks['failed_ids'][:5])}",
                       "suggested_fix": "Přegenerovat dotčené tabulky.", "repair": {"action": "none"}})
    if not all(ctx.get("evidence_ok", [])):
        issues.append({"id": "i_evidence", "severity": "major", "description": "Některé důkazní dotazy selhaly.",
                       "repair": {"action": "none"}})
    weak = [s for s in signals if not s["detectable"]]
    if story_on and weak:
        issues.append({"id": "i_weak", "severity": "major",
                       "description": "Slabý signál v krocích: " + ", ".join(s["step_id"] for s in weak),
                       "suggested_fix": "Snížit pojistnou zásobu, aby se zpoždění projevilo výpadky.",
                       "repair": {"action": "adjust_param", "param": "safety_stock_days", "value": "1.5"}})
    det = [{"step_id": s["step_id"], "detectable": s["detectable"],
            "confidence": 0.9 if s["detectable"] else (0.2 if story_on else 0.95),
            "evidence_ids": s.get("evidence_ids", []),
            "comment": s["comment"] if story_on else "Baseline: krok záměrně chybí. " + s["comment"]} for s in signals]
    if not signals:
        det = [{"step_id": sid, "detectable": True, "confidence": 0.5, "comment": "Bez simulátoru – ověřeno vzorkem."}
               for sid in ctx.get("steps", [])]
    verdict = "fail" if checks.get("errors") else ("warn" if issues else "pass")
    if not story_on:
        summary = "Baseline bez příběhu: data jsou konzistentní, žádný kauzální řetězec není přítomen (očekáváno)."
    elif verdict == "pass":
        summary = ("Data jsou konzistentní (100 % referenční integrita, invarianty prošly) a všechny kroky "
                   "kauzálního řetězce jsou dohledatelné, zasažené entity se jasně liší od kontrolní skupiny.")
    else:
        summary = "Data vyžadují pozornost: " + "; ".join(i["description"] for i in issues)
    return {"verdict": verdict, "summary": summary, "story_detectability": det, "issues": issues}


# --------------------------------------------------------------------------- generic rows
@register_mock("llm_rows")
def _rows(prompt: str, ctx: dict) -> dict:
    import datetime as dt

    t = ctx["table"]
    r = _rng(t["name"], ctx["batch"], ctx["attempt"], ctx.get("seed"))
    a = dt.date.fromisoformat(ctx["window"][0])
    b = dt.date.fromisoformat(ctx["window"][1])
    span = max((b - a).days, 0)
    rows = []
    pk = [c["name"] for c in t["columns"] if c.get("pk")]
    auto = pk[0] if len(pk) == 1 and next(c for c in t["columns"] if c["name"] == pk[0])["type"] == "int" else None
    for i in range(ctx["n"]):
        row: dict[str, Any] = {}
        for c in t["columns"]:
            name, typ = c["name"], c["type"]
            if name == auto:
                continue
            if c.get("fk") and ctx["fk"].get(name):
                row[name] = r.choice(ctx["fk"][name])
            elif c.get("enum_values"):
                row[name] = r.choice(c["enum_values"])
            elif typ == "int":
                lo, hi = int(c.get("min") or 0), int(c.get("max") or 100)
                row[name] = r.randint(lo, hi)
            elif typ == "float":
                lo, hi = float(c.get("min") or 0), float(c.get("max") or 100)
                row[name] = round(r.uniform(lo, hi), 2)
            elif typ == "bool":
                row[name] = r.random() < 0.5
            elif typ == "date":
                d = a + dt.timedelta(days=r.randint(0, span))
                if "end" in name and "start_date" in row:
                    d = dt.date.fromisoformat(row["start_date"]) + dt.timedelta(days=r.randint(7, 14))
                row[name] = d.isoformat()
            elif typ == "datetime":
                d = a + dt.timedelta(days=r.randint(0, span))
                row[name] = f"{d.isoformat()} {r.randint(7, 20):02d}:{r.randint(0, 59):02d}:00"
            else:
                if "name" in name:
                    row[name] = f"{r.choice(md.FEMALE_FIRST + md.MALE_FIRST)} {r.choice(md.MALE_LAST)}"
                else:
                    row[name] = f"{t['name']} – {name} {ctx['batch'] * 100 + i + 1}"
        rows.append(row)
    return {"rows": rows}


@register_mock("commentary")
def _commentary(prompt: str, ctx: dict) -> dict:
    paras = []
    for s in ctx.get("signals", []):
        paras.append(("✔ " if s["detectable"] else "✖ ") + s["comment"])
    if not paras:
        paras = ["Grafy ukazují vývoj klíčových metrik v čase."]
    return {"headline": "Pokles tržeb vysvětluje zpoždění jednoho dodavatele, ne poptávka."
            if any(s["detectable"] for s in ctx.get("signals", [])) else "Bez výrazného příběhu v datech.",
            "paragraphs": paras}


# --------------------------------------------------------------------------- detective (scripted analyst)
_DETECTIVE_SCRIPT = [
    ("Nejdřív ověřím, jestli tržby opravdu klesají a jestli klesá i poptávka (návštěvnost).",
     "SELECT substr(t.month,1,7) AS month, t.revenue, f.visitors FROM (SELECT substr(ts,1,7) AS month, "
     "ROUND(SUM(total_amount)) AS revenue FROM transactions GROUP BY 1) t JOIN (SELECT substr(date,1,7) AS month, "
     "SUM(visitors) AS visitors FROM footfall_daily GROUP BY 1) f USING(month) ORDER BY month"),
    ("Návštěvnost je stabilní, tržby klesají – podívám se na zpoždění dodávek podle dodavatele (1. vs 2. polovina).",
     "WITH mid AS (SELECT date(MIN(planned_date), '+' || CAST((julianday(MAX(planned_date))-julianday(MIN(planned_date)))/2 "
     "AS INT) || ' days') AS m FROM deliveries) SELECT d.supplier_id, s.name, ROUND(AVG(CASE WHEN d.planned_date < mid.m "
     "THEN d.delay_days END),2) AS delay_first_half, ROUND(AVG(CASE WHEN d.planned_date >= mid.m THEN d.delay_days END),2) "
     "AS delay_second_half FROM deliveries d JOIN suppliers s ON s.supplier_id=d.supplier_id CROSS JOIN mid "
     "GROUP BY d.supplier_id ORDER BY delay_second_half - delay_first_half DESC"),
    ("Jeden dodavatel výrazně zhoršil dochvilnost. Kdy to začalo? Měsíční průměr jeho zpoždění.",
     "SELECT substr(planned_date,1,7) AS month, ROUND(AVG(delay_days),2) AS avg_delay FROM deliveries "
     "WHERE supplier_id = {top} GROUP BY 1 ORDER BY 1"),
    ("Ověřím dopad na zásoby: podíl dní bez zásoby podle dodavatele produktu.",
     "SELECT p.supplier_id, ROUND(100.0*SUM(i.days_out_of_stock_7d)/(7.0*COUNT(*)),1) AS oos_pct FROM inventory_snapshots i "
     "JOIN products p ON p.product_id=i.product_id WHERE i.snapshot_date >= (SELECT date(MAX(snapshot_date),'-120 days') "
     "FROM inventory_snapshots) GROUP BY 1 ORDER BY 2 DESC"),
    ("A stížnosti – které kategorie a produkty kterého dodavatele?",
     "SELECT c.category, SUM(p.supplier_id = {top}) AS on_top_supplier_products, COUNT(*) AS total FROM complaints c "
     "LEFT JOIN products p ON p.product_id=c.product_id GROUP BY 1 ORDER BY total DESC"),
]


@register_mock("detective")
def _detective(prompt: str, ctx: dict) -> dict:
    step, obs = ctx["step"], ctx["observations"]
    top = None
    top_name = None
    if len(obs) >= 2 and obs[1]["observation"].get("ok") and obs[1]["observation"]["rows"]:
        row = obs[1]["observation"]["rows"][0]
        if (row[3] or 0) - (row[2] or 0) >= 1.0:
            top, top_name = row[0], row[1]
    if step < len(_DETECTIVE_SCRIPT) and not (step >= 2 and top is None):
        thought, sql = _DETECTIVE_SCRIPT[step]
        return {"thought": thought, "action": "query", "sql": sql.replace("{top}", str(top or -1))}
    if top is None:
        return {"thought": "Žádný dodavatel nevykazuje systematické zhoršení.", "action": "final",
                "final": {"root_cause": "V datech není patrná jednoznačná provozní příčina; vývoj tržeb odpovídá "
                                        "sezónnosti a náhodným výkyvům.", "entity_table": None, "entity_id": None,
                          "causal_chain": [], "ruled_out": ["zpoždění dodavatelů"], "confidence": 0.3}}
    start = None
    monthly = obs[2]["observation"].get("rows", []) if len(obs) > 2 else []
    for m, d in monthly:
        if d is not None and d >= 1.0:
            start = f"{m}-01"
            break
    return {"thought": "Řetězec je konzistentní: zpoždění → výpadky zásob → méně nákupů → stížnosti.", "action": "final",
            "final": {"root_cause": f"Příčinou poklesu tržeb je dodavatel {top_name} (ID {top}), jehož dodávky se od "
                                    f"{start or 'druhé poloviny období'} systematicky zpožďují. Jeho produkty pak chybí "
                                    "na skladě, zákazníci je nemohou koupit a roste počet stížností na nedostupnost.",
                      "entity_table": "suppliers", "entity_id": top, "entity_name": top_name, "start_date": start,
                      "causal_chain": ["Rostoucí zpoždění dodávek dodavatele", "Výpadky zásob jeho produktů",
                                       "Neuskutečněné nákupy a nižší tržby", "Více stížností na nedostupnost zboží"],
                      "ruled_out": ["Pokles poptávky – návštěvnost je stabilní", "Sezónnost – kontrolní produkty drží úroveň"],
                      "confidence": 0.85}}


@register_mock("plan_archetype")
def _plan_archetype(prompt: str, ctx: dict) -> dict:
    full = _plan(prompt, ctx)
    inp = ctx["input"]
    return {
        "use_archetype": True,
        "name": "Síť prodejen – zpožďující se dodavatel",
        "logline": full["spec"]["story"]["logline"],
        "knob_defaults": [{"name": "story_intensity", "value": str(inp.get("story_intensity", 1.0))}],
        "row_counts": [{"name": k, "value": str(v)} for k, v in (inp.get("entity_counts") or {}).items()],
        "questions": full["questions"],
        "suggestions": full["suggestions"],
        "notes": "Mock planner (archetype mode): šablona modulu retail_supplier_delay upravena podle zadání.",
    }
