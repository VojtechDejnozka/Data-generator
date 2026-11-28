# Story2Data – kauzální a statistické jádro (`backend/app/causal/`)

Implementace podle [`Story2Data_CAUSAL_CORE_PROMPT.md`](Story2Data_CAUSAL_CORE_PROMPT.md). Jádro je obecný,
deklarativní SCM engine napojený na existující aplikaci (Pydantic kontrakty, FastAPI, job manager + SSE,
úložiště běhů, LLM klient s mock režimem). Frontend se neměnil; nové výsledky jsou dostupné přes API.

## 1. Pipeline a moduly

| fáze | modul | vstup → výstup |
|---|---|---|
| upřesnění příběhu, návrh SCM (LLM planner) | `planner.py` | `ScenarioInput(engine="scm")` → `PlannerResponse` se `ScenarioSpec.scm` |
| editovatelný ScenarioSpec | `contracts.py`, `editing.py` | `SCMSpec` + operace → nový spec, validace, invalidace |
| spustitelný SCM | `mechanisms.py`, `distributions.py`, `expressions.py`, `graph.py` | registr mechanismů, distribuce, bezpečné výrazy, DAG |
| generování čisté relační reality | `engine.py` | `generate_world(spec, seed)` → `CleanWorld` |
| validace (4 vrstvy) | `validation.py` | `validate_spec`, `validate_world` → `ValidationReport` (PASS / PASS_WITH_WARNINGS / FAIL) |
| ground truth | `ground_truth.py` | `build_manifest(world)` → manifest (zamýšlená i realizovaná GT) |
| pozorování a poškození | `corruption.py` | `observe(world, CorruptionConfig)` → pozorované tabulky + soukromý manifest |
| slepý detektiv | `detective/` | `analyze(observed_db, public_schema, DetectiveConfig)` → `DetectiveResult` |
| evaluace | `evaluation.py` | `evaluate(spec, manifest, result)` → `EvaluationResult` (R, D, metriky) |
| obtížnost | `difficulty.py` | manuál: jedna konfigurace; automat: `optimize(...)` → `OptimizationResult` |
| integrace | `service.py`, `mapping.py`, `sqlsafe.py` | úložiště běhu, joby, odvozené tabulky, read-only SQL |

Ilustrační signatury ze zadání (§18) odpovídají: `plan_story` (`planner.plan_story`), `validate_spec`,
`generate_world`, `validate_world`, `observe` (`corruption.observe` / `service.observe_and_store`),
`analyze` (`detective.analyze`), `evaluate`, `optimize_difficulty` (`difficulty.optimize` / `service.automatic_mode`).

## 2. Kontrakt `ScenarioSpec.scm` (`SCMSpec`)

Existující `ScenarioSpec` dostal volitelné pole `scm` (skryté ze schémat pro structured output, takže stávající
planner se nemění). Je-li `scm` vyplněné, je **jediným zdrojem pravdy**; `tables`, `story`, `evidence` a `dashboard`
se z něj odvozují (`mapping.py`), aby fungoval stávající data browser, report, dashboard i deterministické kontroly.
Serializace JSON (`model_dump_json`) i YAML (`to_yaml` / `from_yaml`), verze `meta.schema_version = "1.0"`.

* **meta** – scenario_id, name, description, domain, original_story, refined_story, language, seed, schema_version,
  assumptions, warnings; **time** – start, periods, frequency (D/W/M), time_column.
* **entities** (grain) – `base` (count), `cross` (kartézský součin entit, `temporal=true` = × čas), `child`
  (parent + `count` nebo `count_variable` → přesný počet podřízených řádků). Vztahy many-to-one přes
  `assignments` (balanced / uniform / distinct_per_parent = spojovací tabulka M:N). `primary_key`, `export`,
  `relationships` (popisné kardinality), `public_description` (neutrální text pro analytika).
* **variables** – variable_id (stabilní), name, semantic_type, data_type, unit, categories, entity (grain),
  column (mapování do tabulky; `null` = nematerializováno), parents (`variable`, `alias`, `lag`, `aggregation`),
  mechanism (`type` + `params`), initial_state, observability (observed / latent), role, constraints.
* **edges** – jen anotace. Hrany se **odvozují** z rodičů cílového uzlu (`derive_edges`); koeficient je read-only kopie.
* **events** – set_value | shift | scale | param_change (cesta v `mechanism.params`) | seasonal; filtr
  `affected_entities` nad klíči grainu (`"supplier_id == 1"`), start/end (datum nebo krok), ramp step/linear/
  exponential/sigmoid. Intervence do proměnné (hodnota po mechanismu) ≠ změna mechanismu (parametry před ním).
* **ground_truth** – story_facts (edge / event / root_cause / affected_entities / lag, důležitost, evidence_variables),
  key_causes/effects, control_groups, alternative_interpretations, effects_to_estimate.
* **difficulty** – `DifficultyConfig` (viz §6).

Mechanismy (`GET /api/scm/mechanisms`, každý má Pydantic schéma parametrů, pravidla typů, generování a popis):
`exogenous, linear, polynomial, interaction, lagged_effect, autoregressive, deterministic_expression, conditional,
threshold, logistic, poisson_regression (+ negative binomial), categorical_cpt (síla α), categorical_softmax,
ordinal_threshold, group_effect, aggregation, lookup, text_template, label`. Nepodporovaný typ → chyba validace.
Výrazy: omezený AST interpret (`expressions.py`), žádné `eval/exec`; dělení nulou, log/sqrt mimo doménu, přetečení
a ne-konečné hodnoty jsou chyby. Distribuce mají explicitní parametrizaci (docstring `distributions.py`).

## 3. Generování (`engine.py`)

* Fáze: statické uzly → časová smyčka (uzly v topologickém pořadí uvnitř kroku) → statické uzly závislé na čase
  (např. průměr týdenních skóre žáka). Cyklus v rámci kroku = odmítnutí; zpětná vazba jen přes `lag`.
* Granularity: lookup přes FK na hrubší grain (vč. předků přes více skoků), agregace z jemnějšího grainu
  (sum/mean/count/min/max/any, i s lagem), počet podřízených řádků podle proměnné (rows vznikají po krocích).
* Common random numbers: každý uzel bere vlastní uniformy `U` (inverzní CDF), `ε = σ·Φ⁻¹(U)`; RNG proudy
  `SeedSequence(seed, sha256(id komponenty))` pro entity, přiřazení, šum každé proměnné, události, každou
  corruption operaci. Změna jedné proměnné/operace nemění ostatní realizace (testováno).
* Čistý svět je neměnný: na disku `data.sqlite` + `world/internal.sqlite` + `world/world.json` (hash); při načtení
  se znovu vygeneruje ze schváleného specu a ověří se hash.

## 4. Validace (`validation.py`)

1. **spec** – reference, typy, parametry mechanismů, granularity/agregace, lagy, cykly, události, story facts, velikost.
2. **relační** – PK, FK, počty řádků fixních grainů, kardinalita child grainů = realizovaná count proměnná,
   časový index a shoda času s rodičem, domény a rozsahy.
3. **mechanistická** – nezávislý přepočet každého mechanismu z uložených uniform; rodiče se zarovnávají pandas
   joiny přes deklarované FK (ne indexovými mapami enginu). Neshoda = FAIL. Řádky pod událostí se nepřepočítávají
   (počet je uveden).
4. **statistická** – KS/χ²/kalibrace/odhad koeficientů, Holmova korekce; selhání je jen *upozornění*
   (velký strukturální šum nevede k FAIL). Realizovaná síla signálu (SNR) jako info.

Při FAIL nelze z čistého světa tvořit pozorování (`service._ensure_valid`). Opravy dat „aby test prošel“ nejsou.

## 5. Ground truth a pozorování

Manifest (`manifest.json`, stávající endpoint `/ground-truth`): SCM, odvozený graf a popisy mechanismů, pozorovatelný
pravdivý graf (latentní projekce) s identifikovatelností hran (compelled / undirected / feedback / confounded),
realizované události (kroky, entity, počty řádků), latentní proměnné, informace pro přehrání šumu, skutečné efekty
(přímé = koeficienty, celkové = intervence `do()` v modelu se stejnými náhodnými čísly), **realizované** story facts
(hrany ověřené intervencí, události diff-in-diff vůči kontrolní skupině), statistiky, validace.

Pozorování (`corruption.py`): projekce na pozorované proměnné (= baseline) + seřazené operace z registru 19 typů
(`GET /api/scm/corruptions`): MCAR/MAR/MNAR, gaussovský a multiplikativní šum, outliery, zaokrouhlení, záměna
kategorií (i confusion matrix), přesné a „near“ duplikáty, shuffle sloupce (i ve skupině), posun časů, překlepy,
zkrácení textu, nekonzistentní formát, chybné FK, skryté sloupce, sampling, mazání řádků. Každá operace má
target_table/columns, row_filter, probability (intenzitu), seed, optional_condition, protected_columns, order.
Manifest zásahů (řádková ID, původní/nové hodnoty, počty) je v `private/corruption/` a detektiv ho nedostane.
Po poškození se kontroluje: povolené operace, nedotčené chráněné sloupce, nezměněný clean world (hash),
analyzovatelnost, minimální pozorovatelnost příběhových sloupců, požadované vs. realizované množství.
Rozpočet poškození = buňky × min(1, intenzita) u šumu + přidané/odebrané řádky × hodnotové sloupce.
Pozorovaná DB je samostatný soubor bez PK/FK constraintů (s běžnými indexy na klíčích).

## 6. Detektiv, evaluace, obtížnost

**Izolace**: `detective.analyze` dostane jen cestu k pozorované DB (otevírá ji `sqlsafe.ReadOnlyDB`) a neutrální
`PublicSchema` (názvy, typy, očekávané PK/FK, `public_description`). Nikdy příběh, SCM, GT, clean world, manifest,
cílovou obtížnost ani předchozí evaluace. SQL: SQLGlot (jeden SELECT/WITH, žádné DDL/DML/PRAGMA/ATTACH),
textový guard, `mode=ro`, authorizer s allowlistem funkcí, timeout, limit řádků i dotazů. Nástroje (§12.2):
inspect_schema, list_tables, describe_table, profile_column, execute_sql, missingness/duplicate report,
check_join (multiplikace řádků), korelace, lineární a logistická regrese, test nezávislosti, analýza časových řad
(změnové body po skupinách), discover_causal_graph (PC-stable/Fisher-z s kontrolou předpokladů a efektivní velikostí
vzorku; causal-learn se použije, je-li nainstalován), create_plot = nepodporováno. Každé volání loguje harness (E1, E2…).

* Spotřeba: každá evaluace běží ve vlastním `usage_scope`; `evaluations/<id>.json` nese `usage` (volání, tokeny, cena)
  a `replay` info, `evaluations/<id>.llm_calls.jsonl` všechna volání se surovými odpověďmi. Součty jsou v
  `DifficultyEstimate.usage`, `ManualEvaluationResult.usage`, `OptimizationResult.usage` a `/causal` (`llm_usage_total`);
  optimalizace respektuje `token_budget`.
* `kind="statistical"` – deterministický referenční analytik nad stejnými nástroji (levný screening, testy).
* `kind="llm"` – agent nad stávajícím LLM klientem (OpenAI, role strong/cheap), nová konverzace a nonce pro každý
  běh, výstupy nástrojů v `<tool_output>` jako nedůvěryhodná data. **V mock režimu** se deleguje na statistického
  detektiva a výsledek nese `agent.mock = true`.

**Evaluace** (deterministická): skeleton/directed P/R/F1, SHD, orientace jen na identifikovatelných hranách
(neorientovatelné hrany Markovovy třídy se nepenalizují, přiznaná nejistota má částečný kredit), story recovery
nad realizovanými fakty (proměnná, entity, čas, směr, role kořenové příčiny), efekty jen stejného typu
(přímý ↔ přímý, celkový ↔ celkový), kvalita důkazů (citované E-id musí existovat, uspět a týkat se proměnných)
a kalibrace (1 − Brier). `R = Σ wᵢ·scoreᵢ` (výchozí 0.40/0.25/0.10/0.15/0.10, nehodnotitelné metriky →
proporcionální přerozdělení), `D = 100·(1 − R)`; bez hodnotitelné metriky `status = not_evaluable`.

**Obtížnost**: baseline (bez poškození, stejná pravidla pozorovatelnosti) → `difficulty_delta`.
*Manual* (`POST /api/runs/{id}/observe`): jedna konfigurace, opakování, žádná optimalizace.
*Automatic* (`POST /api/runs/{id}/difficulty`, výchozí): prostor parametrů z povolených typů (nebo explicitní
`search_space`, `locked_parameters` = zachovat konkrétní parametry), návrhy po diagonále + interpolace mezi
kandidáty obklopujícími cíl + náhodné směry (monotonicita se nepředpokládá), každý kandidát z čistého světa se
stejnými náhodnými čísly, objective = |D − cíl| + penalizace nejistoty a rozsahu poškození, tvrdá omezení
(rozpočet, pozorovatelnost, analyzovatelnost, chráněné sloupce) kandidáta zamítnou. Kandidát v toleranci se
hned nezávisle ověří (`evaluation_repeats`, varianty seedu poškození); neprojde-li, hledání pokračuje. Konec:
tolerance / max_iterations / evaluation_budget / time_budget / zrušení (`POST /api/jobs/{id}/cancel`) / chyba.
Stavy: target_reached, target_not_reached (nejlepší validní výsledek), unreachable_below_baseline (+ návrhy),
budget_exhausted, cancelled, error. `allow_structural_noise_optimization` vytváří nové, validované a auditované
verze reality (`worlds/<hash>/`); výchozí optimalizace SCM nemění.

## 7. API (rozšíření stávajícího, zpětně kompatibilní)

| endpoint | popis |
|---|---|
| `POST /api/plan` s `engine: "scm"` | LLM návrh SCM (mock: referenční scénář, označeno v `meta.warnings`) |
| `POST /api/scm/import` | `{model, observation}` ověří verzi a spustitelnost SCM a vrátí plánovací relaci bez volání plánovače |
| `POST /api/plan/{sid}/replan` | u SCM session: uživatelské úpravy se validují; zpětná vazba → nový návrh |
| `POST /api/spec/validate`, `POST /api/runs` | pro spec se `scm` validují SCM; běh jde přes kauzální engine |
| `GET /api/scm/mechanisms`, `/api/scm/corruptions` | registry (schémata parametrů, význam intenzity, meze) |
| `GET /api/scm/scenarios[/{id}]` | referenční deklarativní scénáře (retail, školství) jako ScenarioSpec |
| `POST /api/scm/validate` | ValidationReport + odvozené hrany + popisy mechanismů + plán výpočtu |
| `POST /api/scm/edit` | `{scm, operations[]}` → nový SCM, chyby, varování, `change.kind/invalidates` |
| `GET /api/runs/{id}/causal` | souhrn: validace, hrany, pozorování, evaluace, běhy obtížnosti |
| `POST /api/runs/{id}/observe` | manuální režim (job) → `ManualEvaluationResult` |
| `POST /api/runs/{id}/difficulty` | automatický režim (job, průběh přes SSE) → `OptimizationResult` |
| `GET /api/runs/{id}/difficulty/{did}`, `/observations/{oid}[/db]`, `/evaluations/{eid}` | uložené výsledky |
| `GET /api/runs/{id}/evaluations/{eid}/llm-calls` | volání LLM detektiva v pořadí (model, tokeny, cena, surová odpověď) |
| `POST /api/runs/{id}/evaluations/{eid}/replay` | přehrání evaluace bez volání API (nástroje znovu nad stejnou pozorovanou DB) + porovnání |
| `POST /api/jobs/{id}/cancel` | kooperativní zrušení (optimalizace ho respektuje) |
| `POST /api/runs/{id}/detective` | u SCM běhů slepý detektiv nad pozorovanou baseline, výstup ve tvaru stávajícího UI |

Editor průběžně validuje aktuální SCM přes `POST /api/scm/validate` a při schválení ho posílá v
`POST /api/runs`. Pole `input.observation` spouští po validaci čistého světa pozorovací fázi. Při
`mode: "automatic"` pracuje s `target` (0–100) a stávajícím optimalizátorem. Při `mode: "manual"` aplikuje
`missing_percent` a `noise_percent` bez evaluace. Výsledek běhu odkazuje na vybranou pozorovanou databázi.
Samostatný endpoint `/observe` dál slouží k evaluaci již zadané konfigurace na existujícím běhu.

## 8. Knihovny

Použito: NumPy, SciPy (distribuce, testy), pandas, NetworkX (graf, pořadí), SQLGlot (parser SQL / DDL),
PyYAML, stávající Pydantic, FastAPI, SQLite a OpenAI SDK klient. Zváženo a nepoužito: DoWhy GCM / PyMC / pgmpy
(vyžadují fitování nebo nepokrývají relační granularitu a čas – vlastní registr nad NumPy/SciPy je jednodušší),
Snowfakery/PluRel (nepřináší kauzální mechanismy), Jenga/PyGrinder (operace jsou jednoduché v pandas a potřebujeme
CRN + manifest), Pandera (relační kontroly jsou přímo v pandas/SQLite), Optuna (adaptivní hledání stačí pro malé
rozpočty), causal-learn / RDFLib (volitelné – adaptér se použije, jsou-li nainstalovány; v prostředí nejsou).

## 9. Testy

`make test` – 83 nových testů v `backend/tests/test_causal_*.py` (+ původních 58): SCM (cykly, pořadí, rovnice
s nezávisle spočtenou hodnotou, reprodukovatelnost, nezávislé RNG proudy, CPT a síla α, lagy/stock-flow/AR,
intervence, změna šumu a propagace), relační engine (1:N, M:N, agregace, FK, granularita, čas), validace (správná
data, chybný mechanismus, porušený invariant, FK, čas, velký šum ≠ FAIL), corruption (reprodukovatelnost, CRN,
MCAR/MAR/MNAR, duplikáty, ochrana sloupců, clean world, manifest, všech 19 operací), evaluace (perfektní, neúplná,
nadbytečné/chybějící hrany, orientace, Markovova ekvivalence, nejistota, nehodnotitelné metriky, vymyšlené důkazy),
optimalizace s deterministickým mockem (cíl, tolerance, nedosažitelný cíl, pod baseline, nemonotónní funkce,
zrušení, rozpočet, zamítnutí, zamčené parametry), bezpečnost (zápis, ATTACH, PRAGMA, nebezpečné funkce i při
obejití parseru, přístup ke GT/clean world, prompt injection v datech) a end-to-end přes API v mock režimu pro
obě domény. Test s reálným LLM (`test_real_llm_planner_and_detective`) se spouští jen s `OPENAI_API_KEY` a
`RUN_REAL_LLM_TESTS=1` – **v této implementaci nebyl proveden**.

## 10. Známá omezení

* Kauzální discovery je contemporaneous PC (bez FCI/Tigramite); LLM detektiv nebyl ověřen s reálným API.
* Mechanismy s událostmi se na dotčených řádcích mechanisticky nepřepočítávají (jen počet); text/labely se
  nepřepočítávají vůbec. Lagy na grainu tvořeném po krocích (child s count proměnnou) nejsou podporované.
* Změna strukturálního šumu regeneruje celý svět (díky CRN mají nedotčené proměnné identické realizace).
* Story recovery hodnotí strukturované výstupy deterministicky; LLM judge není implementován (`use_llm_judge`).
* Ontologie jen základně a jen s RDFLib; bez něj je vstup předán jako text a označen jako nepodporovaný.
* Obtížnost je vlastnost dvojice dataset × referenční agent × rozpočet, ne absolutní vlastnost dat.
