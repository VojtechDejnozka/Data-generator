# Audit kauzální a statistické logiky – report

Audit podle [`Story2Data_CAUSAL_LOGIC_AUDIT.md`](Story2Data_CAUSAL_LOGIC_AUDIT.md), stav repozitáře 2026‑11‑27
(nekomitnuté změny v `backend/app/causal/` a navázaných souborech). Audit provedl stejný agent, který kauzální
jádro implementoval – proto jsou tvrzení opřena o reprodukovatelné experimenty, ne o popis kódu. Změny nebyly
implementovány (§16.4). Cesty jsou relativní k `backend/app/`.

## 1. Souhrn (§16.1)

| Otázka | Odpověď |
|---|---|
| Je pipeline funkční od vstupu po obtížnost? | **Ano v mock režimu** (planner = referenční fixture, detektiv = statistický referenční analytik). S reálným Gemini **neověřeno**: jediný reálný pokus o plánování selhal (kvóta + neplatný výstup); oprava je otestovaná jen proti simulovanému Gemini. |
| Je model spustitelný a editovatelný? | **Ano.** Editace mění `ScenarioSpec.scm`, ze kterého engine generuje (T01, `editing.apply_edits`). |
| Jsou relační data odvozena z mechanismů? | **Ano.** Počty potomků = realizované count proměnné, agregace, lookupy přes FK, stock‑flow (T03, T04). |
| Je čistá realita validována před poškozením? | **Ano**, 4 vrstvy; FAIL blokuje pozorování. **Ale** řádky pod událostmi (= příběhové řádky) se mechanisticky nepřepočítávají (A18). |
| Jsou odděleny strukturální šum a chyby měření? | **Ano** (T01, T05: šum měření nemění `Z_true` ani hash čisté reality). |
| Je detektiv nezávislý na ground truth? | **Ano na úrovni dat a nástrojů** (samostatná DB, read‑only SQL, neutrální schéma, testy prompt injection). Reálný LLM detektiv neověřen. |
| Je obtížnost empiricky měřena? | **Ano** – z recovery skóre detektiva vůči GT. **Ale** technické selhání LLM detektiva se převede na D = 100 (A35, FAIL). |
| Fungují oba režimy? | **Ano** (T09, T10). Rozsah dosažitelné obtížnosti je však s referenčním detektivem úzký (≈ 33 → 41), viz nález P1‑4. |
| Funguje iterativní optimalizace se zpětnou vazbou? | **Ano** – kandidáti se liší, výsledky ovlivňují další návrh, finální kandidát se nezávisle ověřuje, nedosažitelný cíl je pravdivě hlášen. |

## 2. Tabulka požadavků (§16.2)

| ID | Priorita | Stav | Důkaz v kódu / testu | Co chybí nebo je špatně | Další krok |
|---|---|---|---|---|---|
| A01 | JÁDRO | PASS | `causal/service.py:368` `execute_causal_run` → `validate_world` → `observe_and_store:197` → `detective_on` → `evaluation.evaluate:339`; T09, T10 | Validace a skóre detektiva jsou oddělené artefakty (`world/validation.json` vs `evaluations/`). | – |
| A02 | JÁDRO | PARTIAL | `causal/planner.py:175` `plan_story`, kontrakt `SCMDraft:93` + `validate_spec`; `tests/test_causal_planner_llm.py` | Reálné LLM neověřeno (poslední reálný běh selhal). Mock vrací referenční scénář podle klíčových slov (označeno `MOCK`). SQL DDL/ontologie jen jako hinty; RDFLib není nainstalován. | Ověřit `plan_story` s modelem s volnou kvótou na 2–3 příbězích. |
| A03 | JÁDRO | PASS | `causal/editing.py:232`, `POST /api/scm/edit`, UI `ScmEditor.tsx`; T01 (koef. 5→2 změní `y` i `z`, `x` beze změny) | – | – |
| A04 | DOPORUČENÍ | PASS | Pydantic kontrakty; `expressions.py` (AST allowlist, žádné eval/exec); `test_expression_interpreter_allows_math_and_rejects_code` | – | – |
| A05 | JÁDRO | PASS | `engine.py:530` `_eval_var` (rodiče přes `_parent_values:449`, šum přes uložené U); T01 rezidua sd 2,94 (zadáno 3) | – | – |
| A06 | JÁDRO | PASS | `graph.py:106` `execution_plan` (cyklus v kroku → chyba, fáze pre/loop/post), lagy s `initial_state`; T06 cyklus odmítnut | – | – |
| A07 | DOPORUČENÍ | PASS | `EventSpec` (set_value/shift/scale/param_change/seasonal, ramp, filtr entit); `test_event_changes_mechanism_only_for_affected_entities_after_start`; do() vs. param_change odděleně | – | – |
| A08 | JÁDRO | PASS | `mechanisms.py` registr 19 typů; T02 (Bernoulli kalibrace z = −0,27, CPT četnosti 0,81/0,15/0,04 vs 0,80/0,15/0,05) | – | – |
| A09 | JÁDRO (text) | **FAIL** | retail šablony `scenarios/retail_supplier_disruption.json:731`; experiment: 4 z 292 stížností tvrdí chybějící produkt, ačkoli ten den nedostatek nebyl (kategorie „nedostupnost“ má i bez nedostatku p = 0,05, ř. 694); text „už několikátý den“ nemá oporu v datech | Validace text nekontroluje (`validation.py` text přeskakuje). | Šablony s tvrzením podmínit stavem (`select_by` na `had_shortage`, ne na kategorii) a přidat kontrolu textu vůči struktuře. |
| A10 | JÁDRO | PASS | SQLite s 6–8 tabulkami, jedna realizace; T12 třetí doména jen konfigurací | – | – |
| A11 | JÁDRO | PASS | `engine._create_dynamic:370`, agregace `_aggregate`; relační kontroly `validation.py:510`; T03 (změna parametru: 667 → 1 805 objednávek, PASS) | – | – |
| A12 | JÁDRO | PASS | 2 referenční domény + ad‑hoc nemocnice (T12); `test_engine_has_no_domain_specific_code` | – | – |
| A13 | JÁDRO | PASS *(opraveno po auditu)* | Svět: `rng.py` SeedSequence, `service.load_world` regeneruje a ověří hash. Planner: odpověď v session/specu. LLM detektiv: `service.detective_on_tracked` (vlastní `usage_scope`), `evaluations/<id>.llm_calls.jsonl` (každé volání s modelem, tokeny, cenou a surovou odpovědí), `service.replay_evaluation` + `POST /api/runs/{id}/evaluations/{eid}/replay` (přehrání bez API); `tests/test_causal_llm_usage.py` | – | – |
| A14 | JÁDRO | PASS | `ε = σ·Φ⁻¹(U)`; `test_structural_noise_change_propagates_only_to_descendants`; T01 sd = 300 → PASS | – | – |
| A15 | JÁDRO | PASS | `corruption.observe:592` pracuje s kopií projekce; T05 (šum na `y`: `z_true` beze změny, hash čisté reality beze změny) | – | – |
| A16 | JÁDRO | PASS | `validation.validate_world:491` (programově, bez LLM) | – | – |
| A17 | JÁDRO | PASS | `_relational:510` PK/FK/počty/kardinalita/čas/domény; stock‑flow ověřen mechanisticky; T06 | – | – |
| A18 | JÁDRO | PARTIAL | `_mechanistic:589` nezávislé zarovnání přes joiny (`_Frames:241`); T06 rozbitý stock‑flow odhalen | **Řádky pod událostmi se nepřepočítávají** (`validation.py:642`): retail 90 + 60 řádků, školství 1 320/7 680 řádků `weekly_score` – právě příběhové řádky. Výrazy a CPT se přepočítávají sdíleným kódem. | Přepočítat i řádky pod událostmi (aplikovat ramp/param_change nezávisle ve validátoru). |
| A19 | JÁDRO | PARTIAL | `_statistical:650` + Holm `_holm:746` (KS, kalibrace, CPT χ², koeficienty, Poisson průměr; vždy jen varování) | Chybí kontrola rozptylu u Poissona, nelineární/výrazové mechanismy a časová dynamika nemají statistický test, Monte Carlo reference chybí. | Doplnit podle priorit; ne blokující. |
| A20 | JÁDRO | PASS | `ground_truth.realize_story_facts:165` (události: diff‑in‑diff vůči kontrole; hrany: intervence + asociace zvlášť); nerealizované fakty ve verdiktu; nic se nedosazuje | Realizace hran se měří intervencí v modelu (vlastnost modelu), asociace je hlášena zvlášť. | – |
| A21 | JÁDRO | PASS | `world/`, `manifest.json`, `observations/<id>/`, `private/corruption/`; baseline = projekce bez latentních proměnných (`observed_projection:77`); `test_detective_cannot_reach_clean_world_ground_truth_or_latent_values` | – | – |
| A22 | JÁDRO | PASS | registr 19 operací, nulové poškození = baseline | – | – |
| A23 | DOPORUČENÍ | PASS | `op_missing_mar:233` (řízeno pozorovaným sloupcem), `op_missing_mnar:247` (pravá hodnota); testy MAR/MNAR; realizovaný podíl vykazován | Jmenovatel zahrnuje i buňky, které už předtím chyběly. | Zpřesnit jmenovatel. |
| A24 | JÁDRO | PASS | `op_exact_duplicates:368` (stejné PK, DB bez PK constraintu), near‑duplicates s novým ID; T09 +863 řádků | – | – |
| A25 | JÁDRO | PASS | cílení tabulka/sloupec, chráněné klíče, pořadí, seed, manifest s ID a původními hodnotami; `test_mcar_realised_amount_and_manifest` | – | – |
| A26 | JÁDRO | PARTIAL | Izolace: `observation_for_detective:222`, `sqlsafe.py`, nonce `llm_agent.py:101`; `test_untrusted_text_is_passed_as_data_to_the_llm` | Reálný LLM detektiv neověřen. Výchozí detektiv obtížnosti je statistický referenční analytik (`DetectiveConfig.kind = "statistical"`), ne LLM – výsledky to uvádějí (`agent.kind`). | Ověřit LLM detektiva s reálným API; rozhodnout, který detektiv je referenční. |
| A27 | JÁDRO | PASS | Nástroje `detective/tools.py` (SQL, profily, regrese, testy, časové řady, PC), harness log E1…En, evidence metrika kontroluje existenci důkazů | Platí pro statistického detektiva; LLM neověřen (A26). | – |
| A28 | DOPORUČENÍ | PARTIAL | `discovery.py` PC‑stable/Fisher‑z s kontrolou předpokladů a efektivní velikostí vzorku | Jen současný PC; žádná metoda pro čas (Tigramite) ani latentní proměnné (FCI). | P2. |
| A29 | JÁDRO | PASS | `results.DetectiveResult` (vztahy, události, efekty, důkazy, nejistota) | – | – |
| A30 | JÁDRO | PASS | `evaluation.evaluate:339` je volán až po `analyze`; GT nikdy nejde detektivovi | – | – |
| A31 | DOPORUČENÍ | PARTIAL | `graph_metrics:74` (P/R/F1 skeleton i směr, SHD, nadbytečné hrany snižují precision – `test_incomplete_extra_missing_and_wrong_orientation`) | Mapování proměnných je jen přesná shoda `tabulka.sloupec` (`VarMap:36`); sémanticky ekvivalentní názvy se počítají jako chyba. | Mapování s aliasy / normalizací názvů. |
| A32 | JÁDRO | PASS | `graph.truth_graph:307`, `cpdag_orientation:238` (compelled/undirected/feedback/confounded); `test_markov_equivalent_orientation_is_not_penalised`, `test_admitted_uncertainty_beats_wrong_confidence` | Identifikovatelnost na summary grafu s latentní projekcí je aproximace (dokumentováno). | – |
| A33 | JÁDRO | PASS | `D = 100(1−R)`, fakta i pravdivý graf z čisté reality (poškození nesnižuje jmenovatel) | Dostupnost metrik orientace a důkazů závisí na výstupu detektiva (bez nalezené identifikovatelné hrany / bez tvrzení → váha se přerozdělí). | P2: držet váhu s nulovým skóre. |
| A34 | JÁDRO | PASS | `service.manual_mode:290` (baseline + observed, stejný detektiv, `difficulty_delta`); T09 | – | – |
| A35 | DOPORUČENÍ | **FAIL** (část „technické selhání“) | `llm_agent.py:115` chytí `LLMError` a vrátí prázdnou odpověď (`:127`), ta se ohodnotí. Experiment: výpadek API → `status ok, difficulty 100.0`. Opakování, průměr, sd a `preliminary` jsou PASS. | Technické selhání se převádí na maximální obtížnost, což dokument výslovně zakazuje. | Při `LLMError` vrátit `not_evaluable` / vyhodit chybu a kandidáta v optimalizaci nehodnotit. |
| A36 | JÁDRO | PASS | `POST /api/runs/{id}/observe` (manuál, parametry beze změny – T09), `POST /api/runs/{id}/difficulty` (automat) | UI pro režimy neexistuje (mimo rozsah auditu). | – |
| A37 | JÁDRO | PASS | `editing.classify_change:187`; pozorování cachováno podle hashe světa a konfigurace; nový SCM = nový běh s vlastním manifestem | – | – |
| A38 | JÁDRO | PASS | `difficulty.optimize:240`, `_Proposer:168` (interpolace z historie, náhodné směry, strop rozpočtu); T10: 6 různých θ | – | – |
| A39 | JÁDRO | PASS | evaluátor vždy volá `observe(world, cfg)` nad čistou realitou; CRN indexované PK | – | – |
| A40 | DOPORUČENÍ / ROZŠÍŘENÍ | PASS | výchozí θ jen pozorovací; s povolením `world_variant:172` → 3 hashe světa, auditované adresáře `worlds/<hash>/` (experiment) | Rozšíření nemá vlastní automatický test. | Přidat test. |
| A41 | JÁDRO | PARTIAL | `_reject:153` (rozpočet, minimální pozorovatelnost příběhových sloupců, analyzovatelnost, chráněné sloupce); `unreachable_below_baseline:273` | Chybí explicitní minimální počet pozorování (sampling až 70 %); neidentifikovatelnost je v manifestu, ale ne ve výsledku optimalizace. | Doplnit `min_rows_share`; přenést `principally_non_identifiable` do výsledku. |
| A42 | JÁDRO | PASS | `OptimizationResult` (cíl, dosaženo, sd, tolerance/status, historie, zamítnutí, θ, `artifacts.observation_id`); `test_causal_optimization.py` | Dosažená obtížnost je průměr přes varianty seedu poškození; vrácené pozorování je varianta 0 (jedna z měřených). | Zdokumentovat v UI/API. |

## 3. Testy a reprodukovatelné důkazy (§16.3)

**Unit + integrační testy (mock LLM):** `cd backend && .venv/bin/python -m pytest -q tests` → **147 passed, 1 skipped**.
Přeskočený test `test_real_llm_planner_and_detective` vyžaduje `GEMINI_API_KEY` a `RUN_REAL_LLM_TESTS=1` – **neproveden**.

**Kontrolní experimenty** (skripty `audit1.py`, `audit2.py` ve scratchpadu; izolovaný `DATA_DIR`, `LLM_MOCK=1`):

| Exp. | Konfigurace / seed | Výsledek |
|---|---|---|
| T01 | X~N(10,2), Y=5X+ε(sd 3), Z=2Y+η, n=3000, seed 11 | koef. 5→2: `x` stejné, `y`, `z` změněné; reziduum sd 2,94; sklon Z~Y 1,999; sd 300 → PASS |
| T02 | Bernoulli z Y + CPT z ukázky auditu | kalibrace z = −0,27; četnosti 0,81/0,15/0,04 a 0,19/0,30/0,51; změna CPT → 0,588; záměna 20 % nemění čistá data |
| T03 | `relational()` fixture | změna interceptu počtu objednávek: 667 → 1 805 řádků, obě PASS |
| T04 | retail, intenzita události 1,0 vs 0,3 | pozdní prodeje dodavatele 1 / ostatních: 1,47/2,82 vs 2,42/2,82; stížnosti 588 vs 275; tržba = Σ prodejů |
| T05 | MCAR 10 % / 30 %, šum měření, shuffle | NA 8,7 % / 25,3 % (z toho klíče chráněny), reprodukovatelné, hash čisté reality beze změny, corr(x,y) 0,958 → 0,004 |
| T06 | rozbité FK, stock‑flow, CPT se součtem 0,5, cyklus, n=40 se sd 30 | všechny chyby odhaleny; atypická správná realizace PASS |
| T07 | falešné LLM + injekce v textu | `test_untrusted_text_is_passed_as_data_to_the_llm`: v kontextu není příběh, GT ani manifest; výstupy nástrojů v `<tool_output>` |
| T08 | perfektní / neúplná / chybná rekonstrukce | `test_causal_evaluation.py` (7 testů) |
| T09 | MCAR 10 % + duplikáty 5 % (retail) | realizováno 9,9 % a +863 řádků; parametry beze změny; D 33,50 → 33,56 (Δ 0,06, předběžné, n = 1) |
| T10 | cíl 70, 6 iterací, 2 finální opakování | 6 různých θ, 3 zamítnuti rozpočtem; `target_not_reached`, dosaženo 39,8, baseline 33,5 |
| T11 | nedosažitelný cíl; výpadek LLM | nedosažitelný cíl pravdivě hlášen (mock evaluátor i T10); **výpadek LLM → D = 100** (FAIL) |
| T12 | nemocnice (oddělení × den, pacienti dle počtu příchodů, událost triáž) | spec i svět PASS bez změny kódu |

Ověřeno jen v mock režimu: orchestrace planneru a LLM detektiva. Neověřeno: analytická schopnost reálného LLM detektiva
a kvalita reálných návrhů SCM.

## 4. Prioritizace nálezů (§16.4)

**P0 – blokující**
1. **A35 – technické selhání LLM = obtížnost 100.** `detective/llm_agent.py:115–127` převede `LLMError` na prázdnou
   odpověď, evaluátor z ní spočítá D = 100 a optimalizace s ní pracuje jako s měřením. *Minimální oprava:* při
   `LLMError` výjimku propustit (nebo vrátit `DetectiveResult` s příznakem selhání) a v `service.evaluate_configuration`
   vrátit `DifficultyEstimate(status="not_evaluable", reason=…)`; `difficulty._reject` takového kandidáta zamítne.

**P1 – důležité**
1. **A09 – texty tvrdí skutečnosti bez opory v datech** (retail konfigurace). *Oprava:* vybírat šablony podle
   `had_shortage` (resp. strukturovaného stavu), ne podle vylosované kategorie; odstranit „několikátý den“ nebo ho
   podložit proměnnou délky výpadku; přidat do validace kontrolu, že text odpovídá řídicím proměnným.
2. **A18 – příběhové řádky se mechanisticky neověřují.** *Oprava:* ve validátoru nezávisle aplikovat aktivní události
   (ramp × param_change / set_value / shift) a přepočítat i tyto řádky.
3. **A02/A26 – reálné LLM neověřeno** (planner i detektiv). *Oprava:* jeden ověřovací běh s modelem s volnou kvótou;
   rozhodnout, zda referenčním detektivem obtížnosti je LLM, nebo statistický analytik.
4. **Úzký rozsah obtížnosti.** S referenčním detektivem a rozpočtem 35 % se D pohybuje zhruba mezi 33 a 41 (T09, T10);
   cíle typu 70 jsou nedosažitelné. Není to chyba výpočtu, ale produktově zásadní. *Oprava:* povolit cílené poškození
   příběhových sloupců (dnes se rozkládá rovnoměrně na všechny tabulky), zvážit vyšší rozpočet nebo citlivější
   referenčního detektiva; zkontrolovat, zda metriky nejsou dominovány částmi, které poškození nemění.
5. **A19 – neúplná statistická validace** (Poisson rozptyl, nelineární a časové mechanismy).
6. ~~**A13 – volání LLM detektiva se neukládají**~~ – **opraveno**: záznam volání, součty spotřeby v manuálním i
   automatickém režimu, přehrání evaluace bez API a vynucení `DifficultyConfig.token_budget` (dříve nepoužité pole).

**P2 – rozšíření**
1. A31 – mapování názvů proměnných detektiva jen přesnou shodou.
2. A28 – causal discovery bez časových metod a FCI.
3. A33 – váha orientace a důkazů se přerozděluje podle výstupu detektiva.
4. A41 – explicitní minimum pozorování; neidentifikovatelnost do výsledku optimalizace.
5. A42 – zdokumentovat, že dosažená obtížnost je průměr přes varianty seedu poškození.
6. A40 – automatický test optimalizace strukturálního šumu; A23 – přesnější jmenovatel podílu chybění.

## 5. Závěr vůči §17

Jádro dohodnuté vize je implementováno a v mock režimu demonstrovatelné: spustitelný, editovatelný SCM → konzistentní
relační realita s programovou validací → oddělené pozorované databáze → slepý detektiv → hodnocení vůči GT → manuální
i automatický režim bez skryté změny příběhu. Před prohlášením „hotovo“ je nutné opravit **P0 (A35)** a ověřit reálné
LLM (P1‑3); nálezy A09 a A18 se týkají věrohodnosti dat a validace příběhových řádků.
