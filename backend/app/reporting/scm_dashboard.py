"""Propose and verify a dashboard for the user's original SCM story."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from ..contracts.spec import Dashboard, ScenarioSpec
from ..llm.client import LLMError, get_llm
from .evidence import validate_widgets


async def plan_scm_dashboard(spec: ScenarioSpec, db: Path, story: str,
                             *, events: Sequence[dict] = (), refresh: bool = False) -> tuple[Dashboard, list[dict], list[str]]:
    event_context = []
    for event in events:
        if event.get("kind") == "story":
            event_context.append({key: event.get(key) for key in ("description", "first_date", "affected_entities")})
    prompt = (
        "Navrhni česky důkazní dashboard pro původní příběh níže. Nejprve si určete hlavní tvrzení z příběhu, "
        "pak vyberte 2 až 6 panelů, které je společně prověří. Seřaďte panely od hlavního výsledku přes příčinu "
        "a jednotlivé důsledky až po konkurenční vysvětlení výslovně zmíněné v zadání. "
        "Porovnejte zasaženou skupinu s kontrolou před událostí a po ní, pokud to data dovolují. "
        "Například při poklesu tržeb a stabilní návštěvnosti ukažte obě měřítka ve stejných obdobích, "
        "porovnejte změny u dodavatelů a zkontrolujte souběh s výpadky zásob. "
        "Volte typ podle otázky: line pro trend, bar pro srovnání skupin nebo změny před/po, heatmap pro skupina × období, "
        "stacked_bar pro složení společného celku, scatter pro dvojici číselných měření, table pro konkrétní případy. "
        "Pokud to příběh umožňuje, kombinujte aspoň dva typy analytických grafů. "
        "KPI přidejte jen pokud shrnuje přímo relevantní rozdíl nebo změnu. Vynechte obecné počty řádků a duplicity. "
        "Před spojením tabulek zvaž, co představuje jejich řádek, a použij odpovídající agregaci. "
        "Používej jen uvedené sloupce a skutečně známá ID. Pokud ID z příběhu neznáš, srovnej skupiny podle klíče. "
        "Nepoužívej sloupce označené jako latentní. Uveď měrné jednotky přímo v názvu nebo v aliasu. "
        "Použij width=2 pro hlavní časový graf a široké matice, width=1 pro kratší porovnání. "
        "Vrať Dashboard s unikátními id. Každý graf obsahuje jeden SQLite SELECT, příslušné aliasy x, y, series "
        "a v description jednou větou vysvětli, které konkrétní tvrzení příběhu graf prověřuje a co srovnává. "
        "Pro kpi vrať jedno číslo v jednom řádku. Pro heatmap vyplň x a series. "
        "Pro scatter vrať číselné x a y. Použij právě jednu metriku y u scatter nebo s vyplněným series. "
        "Pro ostatní grafy vrať právě jeden řádek na kombinaci x a series. "
        "Výsledky formuluj jako měření, bez předem daného závěru.\n\n"
        f"Původní příběh: {json.dumps(story, ensure_ascii=False)}\n"
        f"Kontext hlavních událostí simulace (datum a mapování skupin, bez výsledků měření): "
        f"{json.dumps(event_context, ensure_ascii=False)}\n"
        "Relační schéma (jména, typy, popis, klíče a význam řádku):\n"
        + json.dumps([table.model_dump(include={"name", "description", "columns"}) for table in spec.tables],
                     ensure_ascii=False)
    )
    client = get_llm()
    best = None
    errors = []
    for _ in range(2):
        request = prompt
        if best is not None:
            request += ("\nOprav předchozí dashboard podle výsledku ověření:\n" + "\n".join(errors)
                        + "\nPředchozí návrh:\n" + best[0].model_dump_json())
        try:
            dashboard = await client.structured(role="strong", task="scm_dashboard", prompt=request,
                                                schema=Dashboard, use_cache=not refresh)
        except LLMError:
            if best is None:
                raise
            break
        dashboard = dashboard.model_copy(update={"widgets": dashboard.widgets[:6]})
        widgets, errors = await asyncio.to_thread(validate_widgets, db, dashboard, {}, spec.meta.model_dump())
        seen = set()
        queries = set()
        valid = []
        for widget in widgets:
            query = " ".join(widget["sql"].split())
            problem = widget_problem(widget)
            if problem or widget["id"] in seen or query in queries:
                errors.append(f'{widget["id"]}: {problem or "duplicitní ID nebo dotaz"}')
                continue
            valid.append(widget)
            seen.add(widget["id"])
            queries.add(query)
        if len(valid) < 2:
            errors.append("Dashboard má méně než dva použitelné grafy.")
        charts = [widget["chart"] for widget in valid if widget["chart"] not in ("kpi", "table")]
        diverse = len(set(charts)) >= 2
        if len(charts) >= 3 and not diverse:
            errors.append("Zvaž doplnění jiného typu grafu pro srovnání skupin nebo období.")
        if best is None or (len(valid), diverse) >= best_score:
            best = dashboard, valid, errors
            best_score = len(valid), diverse
        if len(valid) >= 2 and not errors:
            return dashboard, valid, errors
    return best


def widget_problem(widget: dict) -> str:
    rows = widget["rows"]
    if not rows:
        return "dotaz nevrátil řádky"
    chart, columns = widget["chart"], widget["columns"]
    if chart == "table":
        return ""
    measures = widget["y"]
    if not measures or any(not any(isinstance(row[columns.index(col)], (int, float)) for row in rows)
                           for col in measures):
        return "metriky y musí obsahovat číselná data"
    if chart == "kpi":
        return "" if len(rows) == len(measures) == 1 else "KPI potřebuje jeden řádek a jednu metriku"
    x = widget["x"]
    group = widget["series"]
    if not x:
        return "graf potřebuje osu x"
    if chart == "heatmap" and not group:
        return "tepelná mapa potřebuje skupinu series"
    if (group or chart == "scatter") and len(measures) != 1:
        return "graf se skupinami nebo bodový graf potřebuje jednu metriku y"
    xi = columns.index(x)
    if chart == "scatter":
        yi = columns.index(measures[0])
        has_pair = any(isinstance(row[xi], (int, float)) and isinstance(row[yi], (int, float)) for row in rows)
        return "" if has_pair else "bodový graf potřebuje dvojice číselných hodnot x a y"
    si = columns.index(group) if group else None
    keys = {(row[xi], row[si] if si is not None else None) for row in rows}
    return "" if len(keys) == len(rows) else "dotaz musí vrátit jednu hodnotu pro každou kombinaci osy x a skupiny"
