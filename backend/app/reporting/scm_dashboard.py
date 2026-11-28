"""Propose and verify a dashboard for the user's original SCM story."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..contracts.spec import Dashboard, ScenarioSpec
from ..llm.client import LLMError, get_llm
from .evidence import validate_widgets


async def plan_scm_dashboard(spec: ScenarioSpec, db: Path, story: str,
                             *, refresh: bool = False) -> tuple[Dashboard, list[dict], list[str]]:
    prompt = (
        "Navrhni česky dashboard pro původní příběh níže. Vyber 2 až 6 grafů, které pomohou ověřit "
        "hlavní příčinu, důsledky a alternativní vysvětlení výslovně zmíněná v zadání. "
        "Kde to schéma umožňuje, ukaž vývoj v čase a porovnej zasaženou skupinu s kontrolou. "
        "Před spojením tabulek zvaž, co představuje jejich řádek, a použij odpovídající agregaci. "
        "Používej jen uvedené sloupce a skutečně známá ID. Pokud ID z příběhu neznáš, srovnej skupiny podle klíče. "
        "Vrať Dashboard s unikátními id. Každý graf obsahuje jeden SQLite SELECT, příslušné aliasy x, y, series "
        "a stručný popis toho, co graf měří. U časové řady vrať právě jeden řádek na kombinaci x a series. "
        "Formuluj názvy a popisy jako měření; neoznamuj předem, co dotaz prokáže.\n\n"
        f"Původní příběh: {json.dumps(story, ensure_ascii=False)}\n"
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
        valid = []
        for widget in widgets:
            if widget["rows"] and widget["id"] not in seen:
                valid.append(widget)
                seen.add(widget["id"])
            else:
                errors.append(f'{widget["id"]}: prázdný výsledek nebo duplicitní ID')
        if len(valid) < 2:
            errors.append("Dashboard má méně než dva použitelné grafy.")
        if best is None or len(valid) > len(best[1]):
            best = dashboard, valid, errors
        if len(valid) >= 2 and not errors:
            return dashboard, valid, errors
    return best
