# Story2Data – od příběhu k datům

Prototyp pro hackathon FIS. Z popisu firmy, prostředí a příběhu vytvoří **vzájemně propojený dataset**
(13 tabulek, ~1 mil. řádků), ve kterém je příběh dohledatelný ve **věcných a časových souvislostech**
mezi záznamy – a dodá k tomu **důkaz** (report krok za krokem, SQL, grafy, „detektiv“) i **what-if** porovnání.

Ukázkový scénář: síti prodejen klesají tržby, ale poptávka je stabilní – jeden dodavatel začne dodávat
stále později → výpadky zásob → neuskutečněné nákupy → stížnosti na nedostupnost zboží.

## Rychlý start

```bash
make setup                 # backend venv (uv / Python 3.11+) + npm install, vytvoří .env z .env.example
# do .env doplňte GEMINI_API_KEY (Google AI Studio) – nebo použijte mock režim
make mock                  # offline: fixture odpovědi LLM, bez klíče
make dev                   # reálné Gemini API
# UI: http://127.0.0.1:5173   API: http://127.0.0.1:8000/api/health
make test                  # 39 backend testů (mock) + typecheck frontendu
make seed-mock             # předgeneruje 3 demo běhy (příběh, baseline, silné zpoždění) – mock
make seed                  # totéž s reálným Gemini
```

Požadavky: Python ≥ 3.11 (vyvíjeno na 3.13), Node ≥ 20. Pokud `npm install` hlásí chybu oprávnění
k `~/.npm`, použijte `npm install --cache /tmp/npm-cache`.

### Konfigurace (`.env`)

| proměnná | význam |
|---|---|
| `GEMINI_API_KEY` | klíč z Google AI Studio – **jen backend**, nikdy ne frontend/repo |
| `STRONG_MODEL` / `STRONG_FALLBACK_MODELS` | plánování, validace, detektiv (výchozí `gemini-4-carbon` → `gemini-3.8-flash,gemini-3.5-flash,gemini-3.5-flash-lite`) |
| `CHEAP_MODEL` / `CHEAP_FALLBACK_MODELS` | master data, texty, `llm_rows` (výchozí `gemini-3.5-flash-lite`) |
| `LLM_MOCK=1` | fixture odpovědi (vývoj, testy, demo bez klíče / při vyčerpaném limitu) |
| `LLM_MAX_CONCURRENCY`, `LLM_CACHE`, `LLM_TIMEOUT_S` | souběžnost, cache odpovědí, timeout |

Názvy modelů byly ověřeny 2026-11-27 přes `models.list()` a dokumentaci aistudio.google.com.
**Free tier má u modelů Gemini 4 kvótu 0** a modely Flash často vracejí 503 (přetížení). Klient proto
modely zkouší v pořadí fallbacku: 429 s `limit: 0` znamená okamžitý přechod na další model, 429/5xx
exponenciální backoff. Skutečně použitý model je u každého volání vidět v záložce *Log & tokeny*.

## Architektura

```
React (Vite, TS, Tailwind, ECharts) ──REST + SSE──▶ FastAPI
                                                     ├─ planner (STRONG)   → ScenarioSpec + dotazy + návrhy
                                                     ├─ GenerationPlan (STRONG, validovaný, fallback deterministický)
                                                     ├─ generování
                                                     │    llm_dimension (CHEAP): názvy, jména, města, šablony stížností
                                                     │    simulation: seedovaný kauzální simulátor (numpy)
                                                     │    llm_rows (CHEAP): obecná cesta po dávkách s validací
                                                     ├─ SQLite + deterministické kontroly + kompaktní profil
                                                     ├─ validace (STRONG) + repair smyčka (max 2 iterace)
                                                     ├─ důkazní dotazy, dashboard (read-only SQL)
                                                     └─ what-if, porovnání, detektiv
```

Detailní plán, datové kontrakty a předpoklady: [`docs/PLAN.md`](docs/PLAN.md).

### Proč hybridní generování
Transakční tabulky **negeneruje LLM** – bylo by to drahé, pomalé a příběh by se rozpadl. Příběh vzniká
**mechanismem** v simulátoru (`backend/app/generation/scenarios/retail_supplier_delay.py`):

1. poptávka (návštěvy, nákupní záměry) se vzorkuje **nezávisle na zásobách** – sezónnost, týdenní cyklus, šum;
2. prodejny objednávají periodicky do cílové zásoby; dodavatel X má od `delay_start_date` rostoucí
   zpoždění (`delay_severity`, `ramp_profile`, `ramp_days`, `story_intensity`);
3. záměr se uskuteční jen pokud je zboží na skladě; jinak chybí v účtence, nebo zákazník odejde bez
   nákupu (neuskutečněný nákup);
4. stížnosti vznikají z reálných nesplněných záměrů – pravděpodobnost roste s délkou výpadku – a text
   (šablony od CHEAP modelu) obsahuje skutečný produkt, prodejnu a počet dní.

Výsledný ukazatel (tržby) se **nikde nedosazuje**; „ztracené tržby“ existují jen ve skrytém
ground-truth manifestu. Distraktory: sezónnost (Vánoce, léto), rekonstrukce prodejny, nová konkurence
u jedné prodejny (skutečný lokální pokles poptávky), týdenní stávka jiného dodavatele, zdražení nápojů.
Kontrolní skupina = produkty ostatních dodavatelů ve stejných prodejnách.

Knihovna efektových primitiv (`effects.py`: ramp/step/trend/sezónnost/okno/šum) a rozhraní
`ScenarioModule` umožňují přidat další archetyp. Příběhy bez modulu jdou cestou `llm_rows`
(CHEAP generuje řádky po dávkách s platnými FK, časovým oknem dávky a kroky příběhu; výstup je
validovaný proti JSON schématu a pravidlům a chybné dávky se opakují).

### Datové kontrakty
`backend/app/contracts/` – `ScenarioSpec` (meta, tables, story/causal_chain/distractors, knobs,
invariants, evidence, dashboard), `PlannerResponse`, `GenerationPlan`, `ValidationVerdict`. Všechna
LLM volání používají structured output (`response_json_schema` odvozené z Pydantic) + validaci +
retry s chybovou hláškou (max 2×). Když Gemini schéma odmítne, klient přepne na JSON mode se schématem
v promptu. Pokud plánovaný příběh odpovídá modulu, STRONG vrací jen kompaktní `ArchetypePlan`
(úpravy šablony), což je rychlejší a spolehlivější než generování celé specifikace.

### Bezpečnost a robustnost
- API klíč jen v `.env` na backendu (`.env` je v `.gitignore`), frontend volá jen `/api`.
- SQL od AI/uživatele: textová kontrola (jediný SELECT/WITH), SQLite `mode=ro`, authorizer povolující
  jen čtení, časový limit (progress handler) a limit řádků.
- STRONG validátor dostává **kompaktní profil** (statistiky, vzorky, výsledky kontrol a důkazních dotazů),
  nikdy celý dataset.
- Omezená souběžnost, backoff, cache odpovědí podle hashe vstupu (`.cache/llm`), log tokenů a odhad
  ceny na běh, uložení všech LLM odpovědí do `runs/<id>/llm_calls.jsonl` → běh lze přehrát.
- Reprodukovatelnost: nezávislé RNG proudy na komponentu (`[seed, stream]`), takže změna knobu
  neposune náhodnost jiných částí. What-if běhy mají **identickou poptávku**.

## Výstupy běhu (`runs/<run_id>/`)
`data.sqlite`, `run.json` (spec, plán, knoby, verdikt, kontroly, signály, spotřeba tokenů, repair
historie), `evidence.json`, `checks.json`, `dashboard.json`, `manifest.json` (ground truth),
`llm_calls.jsonl`, `detective.json`. Export z UI: CSV (ZIP) a SQLite.

## Demo skript (3–5 minut)

Předem: `make seed` (nebo `make seed-mock`) a `make dev`. Otevřete http://127.0.0.1:5173.

1. **Zadání (30 s)** – *Nový scénář* → *⚡ Preset*. Ukažte popis firmy, příběh a parametry (prodejny,
   produkty, dodavatelé, období, objem, intenzita, seed). *Navrhnout scénář* → průběh (SSE).
2. **Review specu (45 s)** – archetyp `retail_supplier_delay`, kauzální řetězec s1–s4 na časové ose,
   distraktory, 13 tabulek (fialová = simulace, modrá/zelená = LLM), knoby, invarianty, důkazní dotazy.
   Přijměte návrh „promotions“ (tabulka přes `llm_rows`) → *Schválit a generovat*. Fáze pipeline běží živě.
3. **Důkaz v datech (90 s)** – záložka *Report & důkaz*: všechny kontroly prošly, 100 % FK integrita, verdikt validace. Projděte
   kroky: (1) zpoždění roste jen u mlékárny, stávka jiného dodavatele je krátký výkyv; (2) dny bez
   zásoby jen u jejích produktů; (3) návštěvnost stabilní, konverze klesá, index tržeb zasažené
   skupiny ~65–70 vs. kontrolní ~100, jediná prodejna s poklesem návštěvnosti je ta s konkurencí;
   (4) stížnosti na nedostupnost řádově vzrostou jen u produktů mlékárny, u ostatních zůstávají na stejné úrovni. *Data browser*: klik
   na FK `product_id` ve stížnosti → produkt → dodavatel; ERD.
4. **Detektiv (30 s)** – slepý agent dostane jen schéma; přes SQL najde dodavatele a začátek problému;
   tabulka shody s ground truth. *Ground truth* → odhalit manifest.
5. **What-if (60 s)** – *What-if* → „Baseline bez příběhu“ nebo „Silnější zpoždění“ → porovnání:
   návštěvnost identická (0 %), zpoždění/výpadky/stížnosti/tržby se mění vysvětlitelně; překryté grafy
   (A plná, B přerušovaná).

## Testy
`make test` – unit testy kontraktů, LLM klienta (fallback, backoff, validační retry, cache, JSON mode),
efektových primitiv, simulátoru (determinismus, nezávislost poptávky, monotónnost knobů, baseline),
kontrol a SQL guardu, `llm_rows`; integrační test celé pipeline (plán → replan → běh → what-if →
porovnání → export → detektiv) a API test v mock režimu.

## Známá omezení
- Jeden plně implementovaný simulátor (maloobchod / dodavatel). Jiné příběhy jdou cestou `llm_rows`,
  která je slabší (konzistence zajišťují validace a repair).
- Distraktory přidané v review jsou v simulačním archetypu jen dokumentační, pokud je modul nesimuluje;
  tabulky/sloupce přidané do `llm_rows` tabulek se generují.
- Úlohy běží in-process (restart serveru ukončí běžící úlohy; hotové běhy zůstávají na disku).
- Mock detektiv i mock validátor jsou deterministické: detektiv pouští pevnou sadu SQL dotazů, ale
  závěr odvozuje z jejich skutečných výsledků; validátor vychází z kontrol a měřených signálů.
