# Story2Data – implementační prompt pro kauzální a statistické jádro

## 0. Rozsah úkolu a práce s existujícím repozitářem

Jsi seniorní Python developer a data scientist se znalostí strukturálních kauzálních modelů, pravděpodobnostního modelování, relačních dat, statistické validace a LLM agentů. Implementuj níže popsanou logiku do **již existující aplikace Story2Data** pro hackathon FIS.

Tento prompt specifikuje pouze kauzální a statistické jádro, generování a pozorování dat, nezávislou rekonstrukci příběhu a kalibraci analytické obtížnosti. **Nevytvářej novou aplikaci ani frontend. Nepřepisuj existující UI, routing, API, storage, autentizaci, LLM infrastrukturu ani projektovou strukturu.** Nepředepisuje se nový framework, adresářová struktura, instalace frontendu, deployment nebo nové obrazovky. Zachovej dosavadní funkcionalitu a kontrakty; potřebná rozšíření stávajících backendových rozhraní dělej minimálně a pokud možno zpětně kompatibilně. Pokud existující UI zatím některé vstupy či výsledky nepodporuje, poskytni je jako strukturovaná data dostupná přes existující integrační vrstvu a zdokumentuj jejich napojení; UI v rámci tohoto úkolu neupravuj.

- Nejprve prozkoumej relevantní části repozitáře: současný model scénáře, generátor, datové úložiště, LLM klient, validaci a detektiva, pokud existují. Identifikuj, co znovu použít a co rozšířit.
- Preferuj existující open-source knihovny a současné závislosti projektu; ověř jejich skutečné API, licence a kompatibilitu. Nepřidávej další framework jen kvůli této specifikaci.
- Implementuj funkční, testovatelnou logiku, nikoli pouze návrh či pseudokód. Kód a komentáře anglicky; respektuj styl repozitáře.
- Obecný engine musí vykonávat deklarativní konfiguraci. Žádný natvrdo implementovaný retail nebo jiný doménový simulátor.
- LLM navrhuje specifikaci, neprovádí hromadné numerické generování řádků. Nepoužívej neomezené spouštění LLM kódu.
- Pokud stávající projekt používá jiné názvy, přizpůsob níže navržené kontrakty, nikoli celou aplikaci. Zásadní je jejich význam a chování.
- Nedostupná funkcionalita musí být označena jako nepodporovaná, nikoli simulována natvrdo. Při nedostatku času implementuj nejdříve funkční vertikální průchod a rozšiřitelné registry mechanismů; další podporu popiš jako rozšíření.

## 1. Cíl logiky

Z libovolného slovního příběhu, volitelného datového schématu nebo ontologie vytvoř explicitní spustitelný SCM. Uživatel má být schopen schválit a měnit jeho graf, rovnice, distribuce a parametry. Z modelu vznikne čistá relační databáze, která projde mechanistickou, relační a relevantní statistickou validací. Teprve poté z ní vzniknou pozorovaná (případně poškozená) data. Nezávislý detektiv vidí jen pozorovanou databázi; evaluátor porovná jeho výsledky se skrytou ground truth.

Výchozí režim: uživatel zadá cílovou obtížnost 0–100 a systém iterativně hledá vhodné parametry poškození. Alternativa: uživatel přímo zadá parametry poškození a obtížnost se pouze vyhodnotí. V obou režimech musí jít reprodukovat a porovnat běhy. Cílové skóre není garantované a závisí na referenčním detektivovi, velikosti vzorku a informačním rozpočtu.

Odděl skutečnou realitu, přirozený strukturální šum, pozorovací šum, chyby datové kvality a analytickou dohledatelnost. Změna poškození nemá měnit čistou realitu. Změna SCM či strukturálního šumu vyžaduje novou generaci a validaci. Nízká dohledatelnost není automaticky chyba generátoru.


---

## 2. Hlavní workflow aplikace

Implementuj následující pipeline:

```text
Natural language input
        ↓
LLM clarification and scenario planning
        ↓
Editable ScenarioSpec
        ↓
Executable Structural Causal Model
        ↓
User review and approval
        ↓
Clean relational data generation
        ↓
Deterministic and statistical validation
        ↓
Immutable clean dataset + ground truth
        ↓
Difficulty configuration
        ↓
Observation and corruption engine
        ↓
Observed SQL database
        ↓
Independent AI Detective
        ↓
Ground-truth-based evaluation
        ↓
Difficulty estimation
        ↓
Accept dataset OR adjust parameters and iterate
        ↓
Structured results for existing application
```

Backendová logika musí podporovat opakované použití výsledků jednotlivých fází a regeneraci pouze těch částí pipeline, které byly změnou skutečně ovlivněny. V MVP lze při změně SCM regenerovat celý clean world; při změně corruption parametrů jej vždy znovu použij.

Rozlišuj zejména:

- Změnu SCM: vyžaduje nové generování reality a její validaci.
- Změnu strukturálního šumu: vyžaduje nové generování dotčených proměnných a jejich následků, případně celého datasetu, a validaci.
- Změnu pozorovacího šumu či datových chyb: nevyžaduje nové generování čisté reality.
- Změnu požadované obtížnosti: spouští optimalizaci vhodných parametrů.
- Změnu konfigurace detektiva: vyžaduje pouze opětovnou analytickou evaluaci.

---

## 3. Vstupní kontrakt a zpřesnění příběhu

Logika musí přijmout následující vstupy z existující aplikace (povinné nebo volitelné podle stávajícího kontraktu):

- název scénáře,
- slovní popis prostředí,
- popis příběhu nebo událostí,
- požadované kauzální souvislosti,
- počet entit nebo rozsah datasetu,
- časové období,
- frekvenci časových pozorování,
- jazyk generovaných textových hodnot,
- seed,
- volitelně datový model,
- volitelně SQL DDL,
- volitelně ontologii,
- případné další specifické požadavky.

Datový model může být:

- navržen LLM,
- dodán uživatelem,
- převzat z dodaného SQL schématu,
- odvozen z ontologie,
- kombinací výše uvedeného.

Pokud je dodána ontologie, použij podle potřeby RDFLib a případně pySHACL. V MVP postačuje základní extrakce tříd, vlastností, vztahů, datových typů a omezení.

Nepředpokládej, že ontologický vztah automaticky představuje kauzální vztah. Datová struktura a kauzální model jsou odlišné věci.

## Upřesnění pomocí LLM

LLM má nejprve analyzovat zadání, identifikovat nejasnosti a navrhnout:

- chybějící proměnné,
- relevantní entity,
- možné kauzální mechanismy,
- potenciální confoundery,
- časovou dynamiku,
- potřebné tabulky,
- vhodné pravděpodobnostní distribuce,
- omezení a invarianty,
- možné alternativní interpretace příběhu.

LLM nesmí bez upozornění měnit zásadní význam příběhu.

Vrať návrhy, předpoklady a případné otázky ve strukturovaném formátu pro existující schvalovací workflow. Pokud je zadání dostatečně jasné, nevyžaduj další otázky. Nepřidávej nové obrazovky.

---

## 4. ScenarioSpec – hlavní datový kontrakt

Vytvoř typovaný, verzovaný datový kontrakt `ScenarioSpec`.

Použij stávající typované kontrakty, ideálně Pydantic. Rozšiř je o chybějící položky; nezakládej paralelní nekompatibilní model scénáře.

ScenarioSpec musí být serializovatelný do JSON a ideálně také YAML.

Musí obsahovat minimálně následující části.

### 4.1 Metadata

- scenario_id
- name
- description
- domain
- original_story
- refined_story
- language
- seed
- time_range
- time_frequency
- schema_version
- assumptions
- warnings

### 4.2 Entity definitions

Pro každý typ entity:

- entity_id
- name
- description
- count nebo mechanismus určující počet entit
- primary_key
- attributes
- relationships
- cardinality
- temporal_scope

Podporuj minimálně vztahy:

- one-to-one,
- one-to-many,
- many-to-many prostřednictvím spojovací tabulky.

### 4.3 Table definitions

Pro každou tabulku:

- table_name
- description
- grain
- columns
- primary_key
- foreign_keys
- unique_constraints
- nullable_columns
- data_types
- generation_dependencies

Grain musí jednoznačně popisovat, co reprezentuje jeden řádek.

Příklady:

- jeden zákazník,
- jedna objednávka,
- jeden produkt,
- jeden produkt v konkrétní prodejně a dni,
- jeden pacient při jedné návštěvě,
- jedna transakce.

Podpora různých granularit je zásadní.

### 4.4 Causal variables

Pro každou kauzální proměnnou:

- variable_id
- name
- description
- semantic_type
- data_type
- unit
- domain
- entity_scope
- grain
- table_mapping
- parent_variables
- mechanism
- distribution
- mechanism_parameters
- temporal_dependencies
- initial_state
- observability
- constraints
- noise_parameters

Proměnná může být:

- pozorovaná,
- latentní,
- odvozená,
- pomocná,
- deterministická,
- časově proměnná,
- konstantní na úrovni entity.

Ne všechny proměnné SCM musí mít přímo vlastní sloupec v exportované databázi.

### 4.5 Causal edges

Pro každou hranu definuj:

- source
- target
- relationship_description
- mechanism_reference
- optional_coefficient
- effect_direction
- lag
- entity_mapping
- aggregation
- identifiability_notes

Mechanismus musí být definován konzistentně na úrovni cílového uzlu. Koeficient v hraně nesmí představovat druhou, potenciálně odlišnou definici stejného mechanismu.

### 4.6 Events and interventions

Podporuj obecné události a změny mechanismu v čase:

- event_id
- affected_variables
- affected_entities
- start_time
- end_time
- intervention_type
- intervention_parameters
- ramp_profile

Příklady:

- skoková změna,
- postupný růst nebo pokles,
- dočasný šok,
- změna distribuce,
- změna koeficientu,
- změna pravděpodobnosti,
- změna hodnoty proměnné,
- sezónnost.

Rozlišuj intervenci do proměnné od změny samotného mechanismu nebo od prostého statistického podmínění.

### 4.7 Invariants

Definuj ověřitelná pravidla, například:

- platné primární klíče,
- referenční integrita,
- platné rozsahy hodnot,
- konzistentní časová posloupnost,
- součet podřízených záznamů odpovídá agregované hodnotě,
- deterministické rovnice,
- očekávané počty entit,
- doménová omezení.

### 4.8 Ground truth definitions

Definuj:

- očekávaný kauzální graf,
- mechanismy,
- klíčové příběhové události,
- hlavní příčiny a následky,
- očekávané směry efektů,
- časová zpoždění,
- zasažené entity,
- kontrolní skupiny, pokud existují,
- latentní proměnné,
- známé alternativní interpretace,
- identifikovatelnost jednotlivých vztahů.

Rozlišuj původní zamýšlenou ground truth a skutečnou ground truth realizovanou v konkrétním vygenerovaném datasetu.

### 4.9 Difficulty configuration

Obsahuje:

- mode: `automatic` nebo `manual`
- target_difficulty
- difficulty_tolerance
- allowed_corruption_types
- corruption_parameters
- parameter_bounds
- protected_columns
- protected_tables
- maximum_corruption_budget
- minimum_observability
- allow_structural_noise_optimization
- max_iterations
- evaluation_repeats
- token_budget
- evaluation_budget

Výchozí režim je `automatic`.

---

## 5. Structural Causal Model (SCM)

Kauzální model představuje skutečný proces vzniku dat.

Pro každý uzel platí obecný vztah:

`X_i = f_i(PA_i, U_i)`

kde `PA_i` jsou rodičovské proměnné, `f_i` je strukturální mechanismus a `U_i` představuje exogenní náhodnost.

Nepoužívej pouze korelační matici. Nepoužívej pouze slovní popis závislostí. Model musí být spustitelný.

Kauzální vztahy musí mít skutečný dopad na generované hodnoty.

### 5.1 Kauzální graf

Použij NetworkX nebo vhodnou existující alternativu.

Podporuj:

- orientovaný acyklický graf,
- více rodičovských proměnných,
- nelineární mechanismy,
- interakce,
- confoundery,
- latentní proměnné,
- deterministické vztahy,
- nezávislou i explicitně sdílenou exogenní náhodnost,
- časové závislosti.

V MVP nepodporuj simultánní algebraické cykly.

Zpětné vazby v čase reprezentuj pomocí lagovaných proměnných, například:

`X_t → Y_t → X_(t+1)`

Pokud v rámci jednoho časového kroku vznikne cyklus, musí být model odmítnut nebo upraven uživatelem.

### 5.2 Editace SCM – pouze logika a datový kontrakt

Existující aplikaci poskytni serializovatelný model uzlů, hran, mechanismů, parametrů, distribucí a mapování do tabulek. Při editaci uzlu, hrany či parametru aktualizuj jediný zdroj pravdy (ScenarioSpec), ověř konzistenci a vrať konkrétní validační chyby. Přidání či odebrání hrany musí být v souladu s deklarovanými rodiči mechanismu. Zajisti stabilní identifikátory při přejmenování a korektní invalidaci odvozených výsledků. Rozlišuj pozorované, latentní, deterministické a časové uzly v metadatech. Neimplementuj editor nebo vizualizační knihovnu; existující frontend využije tento kontrakt.

### 5.3 Podporované mechanismy

Vytvoř rozšiřitelný registr mechanismů.

Cílový registr mechanismů má podporovat následující typy. Pro první funkční průchod prioritizuj linear, deterministic_expression, conditional, logistic, poisson_regression, categorical_cpt, lookup, aggregation a lagged_effect; ostatní implementuj podle času a potřeb schválených scénářů. Nepodporované typy odmítni s jasnou chybou:

- linear
- polynomial
- interaction
- conditional
- threshold
- deterministic_expression
- logistic
- poisson_regression
- categorical_cpt
- categorical_softmax
- ordinal_threshold
- autoregressive
- lagged_effect
- group_effect
- aggregation
- lookup

Každý mechanismus musí mít:

- vlastní parametrické schéma,
- pravidla kompatibility vstupů a výstupů,
- implementaci generování,
- implementaci validace,
- uživatelsky čitelný popis.

Nepoužívej neomezené `eval()` nebo `exec()` nad výstupem LLM.

Pokud podporuješ obecné matematické výrazy, implementuj omezený AST interpreter, bezpečný expression parser nebo typovaný expression tree s explicitně povolenými operátory a funkcemi.

Podporuj například:

- sčítání, odčítání, násobení, dělení,
- mocniny s omezeními,
- min, max, clip,
- log, exp, sqrt,
- podmínky,
- porovnání,
- sigmoid,
- softmax.

Validuj domény funkcí, dělení nulou, neplatné hodnoty a numerickou stabilitu.

### 5.4 Distribuce

Využij NumPy a SciPy nebo vhodný pravděpodobnostní framework.

Podporuj minimálně:

- Normal
- Uniform
- LogNormal
- Exponential
- Gamma
- Bernoulli
- Binomial
- Poisson
- Negative Binomial
- Categorical
- Beta
- Student-t

Distribuce musí mít explicitně definovanou parametrizaci.

Například u lognormálního rozdělení musí být jasné, zda parametr představuje průměr logaritmu nebo průměr výsledné proměnné.

Uživatel nesmí být nucen zadávat všechny parametry ručně. LLM má navrhovat smysluplné hodnoty a předpoklady.

---

## 6. Různé typy proměnných a šumu

Nepředpokládej, že každý mechanismus lze vyjádřit jako lineární rovnice s normálním aditivním šumem.

### 6.1 Spojité proměnné

Podporuj například:

`Y = 5X + epsilon`

`epsilon ~ Normal(0, sigma²)`

Strukturální šum ovlivňuje skutečnou hodnotu proměnné i všechny její kauzální následky.

Při změně strukturálního šumu musí být znovu vygenerovány dotčené proměnné a jejich potomci.

Pro srovnatelnost běhů preferuj společné základní náhodné realizace, například:

`epsilon = sigma * z`

`z ~ Normal(0, 1)`

Při změně sigma použij stejné `z`, pokud je to vzhledem k mechanismu možné.

Pro spojité mechanismy umožni zobrazit nebo parametrizovat signal-to-noise ratio.

### 6.2 Binární proměnné

Například:

`p = sigmoid(intercept + beta * X)`

`Y ~ Bernoulli(p)`

Přirozená náhodnost je zde součástí Bernoulliho rozdělení.

Pro reproducibilní varianty lze použít společnou realizaci:

`U ~ Uniform(0, 1)`

`Y = 1 if U < p else 0`

Oslabení beta je změnou skutečného kauzálního mechanismu.

Náhodná záměna pozorovaných hodnot 0 a 1 je naproti tomu chybou měření.

Tyto dvě operace nesmí být zaměňovány.

### 6.3 Kategorické proměnné

Podporuj:

- Conditional Probability Tables (CPT),
- multinomický logit / softmax,
- podmíněné kategorické distribuce.

Při použití CPT lze pro změnu síly vztahu použít interpolaci:

`P_alpha(Y | X) = alpha * P(Y | X) + (1-alpha) * P_ref(Y)`

kde `alpha = 1` reprezentuje původní mechanismus a `alpha = 0` společné referenční rozdělení nezávislé na X.

Toto je změna skutečného strukturálního mechanismu, nikoliv datová chyba.

Pro poškození pozorovaných kategorií používej samostatnou matici záměn – confusion matrix.

Například uživatel může určit, že 5 % skutečných kategorií bude chybně zaznamenáno.

### 6.4 Ordinální proměnné

Podporuj latentní spojitou proměnnou a prahový mechanismus.

Například spokojenost od 1 do 5 může vznikat z latentního skóre rozděleného pomocí prahových hodnot.

Chyby měření mohou znamenat posun o jednu kategorii nebo jinou definovanou záměnu.

### 6.5 Textové proměnné

Text nebudeme generovat nezávisle na skutečné realitě.

Nejprve vygeneruj strukturovaný stav a skutečné příběhové události.

Poté vygeneruj text, který je s tímto stavem konzistentní.

Příklad strukturovaného vstupu:

```json
{
  "delay_days": 7,
  "product_available": false,
  "customer_sentiment": "negative",
  "complaint_category": "availability"
}
```

Z tohoto vstupu může vzniknout text zákaznické stížnosti.

Preferuj kombinaci:

- šablon,
- Faker,
- podmíněného LLM generování,
- cache textových variant.

LLM nesmí do textu svévolně přidávat nové události, které nejsou podporovány strukturovanými daty.

U textu rozlišuj:

- přirozenou variabilitu vyjádření,
- míru konkrétnosti,
- nejednoznačnost,
- překlepy,
- chybějící části,
- nesouvisející informace,
- případné rozpory způsobené pozorovací chybou.

Pro řízení analytické obtížnosti umožni například nastavit, jak explicitně text popisuje skutečnou příčinu.

Pozor na ground truth leakage. Text nemá automaticky přímo prozrazovat řešení analytické úlohy.

Pokud má text ovlivňovat další proměnné, používej pro MVP jeho explicitní strukturované sémantické vlastnosti. Nevytvářej kruhovou závislost mezi volným textem a ostatními daty.

---

## 7. Generační engine

Preferovaná sada knihoven pro samotný engine (přizpůsob stávajícímu projektu):

- NetworkX pro graf a závislosti.
- NumPy/SciPy pro pravděpodobnostní vzorkování a mechanismy.
- pandas pro práci s tabulkami.
- Stávající SQL úložiště, případně DuckDB, pokud není žádné vhodné úložiště k dispozici.

Prověř možnost využití:

- DoWhy GCM,
- PyMC,
- pgmpy,
- Causally,
- PluRel,
- Snowfakery.

DoWhy, PyMC a pgmpy mohou být vhodné pro konkrétní typy mechanismů, ale jejich použití není povinné.

Nejdůležitější je, aby zvolený engine dokázal přímo generovat data z uživatelem definovaných strukturálních mechanismů bez nutnosti vlastnit trénovací dataset.

Nevyužívej generátor vyžadující nejprve naučení distribuce existujících dat jako hlavní řešení našeho problému.

### 7.1 Princip generování

1. Validuj SCM.
2. Vyřeš počty a identitu entit.
3. Vytvoř potřebné entity a vztahy.
4. Urči pořadí generování mechanismů.
5. Generuj exogenní proměnné.
6. Generuj závislé proměnné podle skutečných rodičů.
7. Vyhodnocuj časové závislosti ve správném pořadí.
8. Aplikuj události a intervence.
9. Vygeneruj pomocná a textová pole.
10. Materializuj relační tabulky.
11. Ulož ground truth a informace potřebné pro validaci.

Nevytvářej každou tabulku nezávislým LLM voláním.

Kauzálně závislé tabulky musí vznikat koordinovaně ze stejné reality.

### 7.2 Relační granularita

Toto je jedna z nejdůležitějších částí implementace.

Jednotlivé proměnné mohou existovat na různých úrovních:

- customer,
- order,
- order_item,
- store,
- product,
- store × product × day.

Engine musí umět řešit závislosti mezi různými granularitami.

Podporuj minimálně:

- přenos atributů rodičovské entity na podřízenou,
- lookup přes FK,
- agregaci podřízených entit,
- vytváření počtu podřízených záznamů podle mechanismu,
- skupinové náhodné efekty,
- časově podmíněné lookupy,
- lagované hodnoty.

Příklad:

Vlastnosti zákazníka ovlivňují počet jeho objednávek. Počet objednávek určuje skutečný počet řádků v tabulce objednávek. Hodnoty jednotlivých objednávek závisí na atributech zákazníka.

Není přípustné nezávisle vygenerovat počet objednávek a jiný počet řádků v tabulce objednávek.

Kauzální model může obsahovat závislosti na počtu řádků, nikoliv pouze na hodnotách sloupců.

Pokud vzniká závislost mezi agregací a tvorbou nových entit, použij explicitní fáze nebo časové kroky.

Nesmí dojít k použití budoucích hodnot při generování minulosti, pokud to není výslovně součástí schváleného modelu.

### 7.3 LLM generování dat

LLM používej primárně pro:

- návrh ScenarioSpec,
- návrh mechanismů a distribucí,
- textové hodnoty,
- sémantická master data,
- případné doplnění realistických názvů a popisů.

LLM nemá být primárním generátorem numerických transakčních řádků.

Numerické a strukturální hodnoty mají vznikat programovým vzorkováním podle schváleného modelu.

### 7.4 Reprodukovatelnost

Použij samostatné RNG streamy pro:

- vytváření entit,
- jednotlivé exogenní proměnné,
- strukturální šum,
- časové události,
- pozorovací šum,
- missingness,
- duplikáty,
- ostatní poškození.

Využij například NumPy `SeedSequence` a stabilní identifikátory proměnných a operací.

Zajisti, aby změna jedné proměnné nebo jednoho typu poškození zbytečně neměnila náhodné realizace všech ostatních částí.

Nepoužívej Python hash jako trvale stabilní identifikátor seedu.

LLM výstupy ukládej a cachuj. Rozlišuj plně deterministické části a části, jejichž reprodukovatelnost je zajištěna přehráním uložené odpovědi.

---

## 8. Validace čistých dat

Validace nesmí být zaměňována s analytickou dohledatelností.

Data mohou být správně vygenerována z SCM, přestože z nich není statisticky možné spolehlivě odhadnout všechny kauzální vztahy.

Implementuj čtyři samostatné validační vrstvy.

### 8.1 Spec validation

Použij Pydantic a NetworkX.

Kontroluj:

- povinné atributy,
- existenci odkazovaných proměnných,
- kompatibilitu datových typů,
- platnost distribucí,
- platnost pravděpodobností,
- existenci mapovaných tabulek a sloupců,
- acykličnost grafu v rámci časového kroku,
- řešitelnost závislostí,
- validitu granularit a agregací,
- velikost požadovaného datasetu.

### 8.2 Relational validation

Použij stávající SQL engine a případně Pandera.

Kontroluj:

- unikátní primární klíče,
- referenční integritu,
- datové typy,
- nullability,
- povolené hodnoty,
- kardinality,
- konzistenci počtu entit,
- agregace,
- časové podmínky.

### 8.3 Mechanistic validation

Kontroluj, zda hodnoty skutečně vznikly podle definovaného mechanismu.

Pokud je uložena realizace exogenního šumu, ověř numericky například:

`Y ≈ 5X + epsilon`

Pro deterministické mechanismy musí platit přesná nebo numericky tolerovaná shoda.

Pro diskrétní mechanismy ověř platnost pravděpodobností a konzistenci realizace s uloženou náhodnou veličinou, pokud je dostupná.

Ověření musí být oddělené od samotné generace tak, aby zachytilo implementační chyby.

Používej unit testy s nezávisle vypočtenými očekávanými výsledky. Samotné opětovné spuštění stejné potenciálně chybné implementace není dostatečná validace.

### 8.4 Statistical validation

Použij podle potřeby:

- SciPy,
- Statsmodels,
- scikit-learn,
- Monte Carlo simulace.

Příklady kontrol:

- očekávané momenty a kvantily,
- distribuce reziduí,
- očekávané podmíněné pravděpodobnosti,
- kalibrace binárních výsledků,
- četnosti kategorií,
- očekávané skupinové rozdíly,
- časové závislosti,
- odhad parametrů jednoduchých mechanismů,
- plausibilita realizované síly signálu.

Pro komplexnější mechanismy použij referenční simulace nebo vhodné simulační intervaly.

Nepoužívej jednu univerzální hranici p-value jako kritérium validity.

Neodmítej dataset pouze proto, že skutečný kauzální koeficient není statisticky významný.

Zohledni velikost vzorku, přirozenou náhodnost, počet testů a závislost mezi pozorováními.

Statistické testy mohou oprávněně selhat i u správně generovaných dat. Rozlišuj proto tvrdé mechanistické chyby od statistických upozornění.

Výsledek validace:

- PASS
- PASS_WITH_WARNINGS
- FAIL

Každá kontrola má obsahovat:

- ID kontroly,
- dotčený mechanismus nebo tabulku,
- očekávaný výsledek,
- pozorovaný výsledek,
- závažnost,
- vysvětlení,
- případně doporučenou opravu.

Při FAIL neposílej dataset do corruption enginu jako validní realitu.

Omezená repair smyčka může opravit ScenarioSpec nebo implementaci mechanismu. Po opravě musí následovat nové generování a validace.

Nesnaž se opravovat data ruční změnou výsledných hodnot pouze proto, aby statistický test vyšel.

---

## 9. Ground truth a oddělení datových vrstev

Každý běh musí mít minimálně tři oddělené artefakty.

## A. Clean world

Neměnná čistá databáze reprezentující vygenerovanou realitu.

Obsahuje všechny potřebné realizované hodnoty a interní informace.

## B. Ground truth manifest

Obsahuje:

- schválený SCM,
- skutečný kauzální graf,
- parametry mechanismů,
- realizované události,
- latentní proměnné,
- strukturální náhodnost nebo informace potřebné k jejímu přehrání,
- skutečné kauzální efekty, pokud je lze vypočítat,
- klíčové příběhové skutečnosti,
- realizované statistické vlastnosti,
- specifikaci pozorovatelnosti,
- validační výsledky.

## C. Observed dataset

Dataset, který vidí analytik nebo AI detektiv.

Může obsahovat:

- pouze pozorované proměnné,
- chyby měření,
- chybějící hodnoty,
- duplikáty,
- ostatní datové problémy.

Ground truth manifest ani clean world nesmí být dostupné detektivovi.

Vytvářej samostatnou analytickou databázi, nikoliv pouze view nad databází obsahující skryté tabulky.

Každý artefakt identifikuj hashem, verzí a run_id.

---

## 10. Observation and Corruption Engine

Tato vrstva reprezentuje proces, kterým skutečnou realitu pozorujeme a zaznamenáváme.

Rozlišuj tři typy náhodnosti.

## A. Structural noise

Přirozená náhodnost samotné reality.

Je součástí SCM a ovlivňuje skutečné hodnoty i kauzální následky.

## B. Measurement noise

Chyba měření skutečné hodnoty.

Například:

`Y_observed = Y_true + measurement_error`

Navazující mechanismy skutečné reality stále používají `Y_true`, pokud nebylo výslovně specifikováno, že rozhodnutí ve světě závisí na pozorované hodnotě.

## C. Data quality corruption

Chyby vzniklé při sběru, přenosu, ukládání nebo zpracování dat.

Tato vrstva nesmí zpětně měnit již vygenerovanou skutečnou realitu.

### 10.1 Podporované typy poškození

Vytvoř rozšiřitelný registr operací.

Cílový seznam podporovaných operací (prioritizace pro první průchod je v závěru):

- missing_values_mcar
- missing_values_mar
- missing_values_mnar
- gaussian_measurement_noise
- multiplicative_measurement_noise
- outliers
- rounding
- category_misclassification
- exact_duplicates
- near_duplicates
- column_value_shuffle
- timestamp_shift
- text_typos
- text_truncation
- inconsistent_formatting
- incorrect_foreign_keys
- hidden_columns
- sampling
- row_deletion

Využij podle potřeby:

- Jenga,
- PyGrinder,
- pandas,
- NumPy,
- SciPy.

Prověř aktuální API těchto knihoven. Pokud je konkrétní jednoduchou operaci výhodnější implementovat přímo v pandas/NumPy, udělej to.

### 10.2 Missingness mechanisms

Podporuj alespoň:

MCAR – hodnoty chybí nezávisle na hodnotách dat.

MAR – pravděpodobnost chybění je podmíněna jinými pozorovanými proměnnými.

MNAR – pravděpodobnost chybění závisí na skutečné hodnotě, která může být po aplikaci missingness skrytá, nebo na jiné nepozorované proměnné.

Umožni nastavovat intenzitu a podmínky poškození.

Například pravděpodobnost chybění příjmu může záviset na jeho skutečné velikosti.

### 10.3 Duplikáty a relační integrita

Rozlišuj:

- přesnou duplicitu záznamu,
- duplicitu stejné logické entity s novým technickým ID,
- near duplicate s mírně změněnými hodnotami.

Neaplikuj duplikace způsobem, který neumožní dataset uložit.

Pokud uživatel chce záměrně porušit PK/FK omezení, použij observed staging databázi, která tato omezení nevynucuje.

Metadata musí nadále popisovat očekávanou logickou strukturu.

V defaultním režimu chraň identifikátory entit a FK před nechtěným zničením.

### 10.4 Shuffle

Pouhé přeházení pořadí SQL řádků není smysluplné poškození.

Implementuj možnost promíchat hodnoty konkrétního sloupce mezi záznamy, případně pouze v rámci definované skupiny.

Tím lze zachovat marginální distribuci, ale narušit závislosti mezi proměnnými.

### 10.5 Rozsah a pořadí operací

Každá operace musí podporovat:

- target_table,
- target_columns,
- row_filter,
- probability nebo intensity,
- seed,
- optional_condition,
- protected_columns,
- operation_order.

Pořadí operací musí být explicitní a reprodukovatelné.

Důsledně rozlišuj požadované procento poškození od skutečně realizovaného procenta. Například různé operace se mohou překrývat.

### 10.6 Corruption manifest

Každý zásah zaznamenej.

Ulož:

- typ operace,
- parametry,
- seed,
- dotčené tabulky,
- identifikátory záznamů,
- dotčené sloupce,
- původní hodnoty nebo jejich interní reference,
- nové hodnoty,
- skutečný počet zásahů.

Corruption manifest nesmí být dostupný detektivovi.

Po aplikaci poškození nevyžaduj, aby observed dataset prošel všemi invarianty čisté reality.

Místo toho kontroluj, že:

- byly aplikovány pouze povolené operace,
- nebyly porušeny chráněné podmínky,
- nebyl změněn clean world,
- dataset lze analyzovat,
- množství poškození odpovídá konfiguraci.

---

## 11. Nastavení analytické obtížnosti

Implementuj dva rovnocenné režimy.

Výchozí režim je automatický.

### 11.1 Automatic mode – výchozí

Uživatel nemusí rozumět statistickým parametrům.

Nastaví požadovanou obtížnost na stupnici 0–100.

Například:

„Chci dataset s obtížností 70/100.“

Zároveň může volitelně:

- povolit nebo zakázat jednotlivé typy poškození,
- chránit důležité tabulky a sloupce,
- stanovit maximální rozsah poškození,
- omezit počet iterací,
- určit toleranci cílové obtížnosti.

Následně systém iterativně hledá vhodnou kombinaci parametrů.

### 11.2 Manual mode

Uživatel přímo nastavuje například:

- procento NA,
- typ missingness,
- procento duplikátů,
- chyby měření,
- podíl chybných kategorií,
- outliers,
- shuffle rate,
- chybné časové údaje,
- míru textového poškození,
- skrytí vybraných proměnných.

Volitelně může také upravit strukturální šum a další parametry SCM.

V tomto režimu není požadovaná obtížnost cílem optimalizace.

Obtížnost je pouze jedním z výsledků následné evaluace datasetu.

Uživatel může ručně upravovat parametry a spouštět nové evaluace.

### 11.3 Přepínání režimů

Po dokončení automatické optimalizace vrať nalezené parametry a výsledky ve strukturované podobě.

Logika musí podporovat tyto následné operace:

- přijmout výsledek,
- změnit požadovanou obtížnost,
- přejít do manuálního režimu,
- ručně upravit nalezenou konfiguraci,
- zachovat konkrétní parametry a optimalizovat pouze ostatní.

Při přechodu z automatického do manuálního režimu zachovej aktuální konfiguraci.

### 11.4 Zachování reality

Ve výchozím automatickém režimu optimalizuj pouze pozorovací a corruption parametry.

Neměň skutečný kauzální příběh, sílu jeho mechanismů ani přirozenou náhodnost bez výslovného souhlasu uživatele.

Podporuj pokročilou konfigurační volbu:

„Povolit změny strukturálního šumu.“

Pokud je povolena, může optimalizátor měnit vybrané parametry SCM v uživatelem schváleném rozsahu.

Každá taková změna ale vyžaduje nové generování a validaci reality.

Nezaměňuj:

- vytváření obtížněji pozorovatelné stejné reality,
- vytváření jiné reality se slabším kauzálním signálem.

---

## 12. AI Detective

Detektiv je nezávislý analytický agent.

Jeho cílem je zjistit, co se v datech děje, rekonstruovat klíčové příběhové události a podle dostupných důkazů navrhnout kauzální vysvětlení.

Implementuj pomocí PydanticAI nebo alternativního vhodného open-source agent frameworku.

LLM provider musí být konfigurovatelný.

Výchozí implementace může využívat Google Gemini.

### 12.1 Blind evaluation

Detektiv nesmí obdržet:

- původní příběh,
- ScenarioSpec,
- skutečný SCM,
- původní kauzální graf,
- parametry generování,
- ground truth,
- clean world,
- corruption manifest,
- požadovanou obtížnost,
- výsledky předchozích evaluací stejného scénáře,
- předem vytvořené důkazní SQL dotazy.

Nesmí mít přístup k souborům nebo databázím, které tyto informace obsahují.

Detektiv může dostat:

- observed database,
- názvy tabulek,
- datové typy,
- očekávané PK/FK vztahy,
- neutrální popisy sloupců,
- obecné zadání analytického úkolu.

Názvy a popisy nesmí zbytečně prozrazovat vložený příběh.

Každý nezávislý evaluační běh musí mít novou konverzaci a nesdílet předchozí analytické závěry.

### 12.2 Nástroje detektiva

Implementuj omezené a validované nástroje:

```text
inspect_schema()
list_tables()
describe_table(table)
profile_column(table, column)
execute_sql(query)
get_missingness_report()
get_duplicate_report()
compute_correlation(...)
fit_linear_regression(...)
fit_logistic_regression(...)
test_independence(...)
analyze_time_series(...)
discover_causal_graph(...)
create_plot(...)
```

Nepotřebuješ implementovat všechny nástroje v první fázi.

Minimální funkční sada:

- inspekce schématu,
- read-only SQL,
- deskriptivní statistiky,
- základní regresní analýza,
- jednoduchá detekce anomálií,
- kauzální discovery nástroj.

Detektiv musí být schopen spojovat tabulky a vytvářet analytické datasety.

Při analýze musí zohledňovat grain, skupinovou závislost a časovou strukturu.

Připojení tabulek nesmí být prováděno bez kontroly možné multiplikace řádků při JOIN.

### 12.3 Causal discovery

Prověř využití:

- causal-learn,
- pgmpy,
- LiNGAM,
- Tigramite.

Pro MVP preferuj causal-learn a vybrané metody PC/GES/FCI podle jejich předpokladů.

Tigramite využij, pokud je potřeba analyzovat časové závislosti.

Causal discovery nesmí být aplikováno automaticky bez ohledu na vlastnosti datasetu.

Agent musí zohlednit:

- spojité a diskrétní proměnné,
- velikost vzorku,
- časové závislosti,
- latentní confoundery,
- chybějící hodnoty,
- deterministické vztahy,
- předpoklady použité metody.

Pokud metoda není pro daná data vhodná, musí to agent uvést.

Detektiv nesmí automaticky interpretovat korelaci jako kauzální efekt.

### 12.4 Strukturovaný výstup detektiva

Výsledek musí obsahovat:

- story_summary,
- key_events,
- inferred_causal_graph,
- inferred_relationships,
- estimated_effects,
- alternative_explanations,
- supporting_evidence,
- executed_queries,
- statistical_results,
- discovered_data_quality_issues,
- limitations,
- unresolved_questions,
- uncertainty.

Pro každý nalezený vztah:

- source,
- target,
- edge_type,
- estimated_direction,
- estimated_strength,
- optional_lag,
- confidence,
- evidence_ids,
- identification_status.

Edge_type musí umožnit reprezentovat nejen orientovanou hranu, ale také neurčitý směr, obousměrnou vazbu ve smyslu latentního confoundingu nebo jinou relevantní nejistotu.

Každé zásadní tvrzení musí být podloženo konkrétním analytickým výsledkem.

Pouhý sebevědomý slovní komentář nestačí.

---

## 13. Evaluation Engine

Evaluator má přístup ke ground truth, ale není součástí kontextu detektiva.

Jeho cílem je objektivně porovnat výsledky analýzy s realizovanou skutečností.

Preferuj deterministické metriky před subjektivním LLM hodnocením.

### 13.1 Graph recovery metrics

Implementuj:

- edge precision,
- edge recall,
- edge F1,
- skeleton precision,
- skeleton recall,
- skeleton F1,
- orientation accuracy,
- Structural Hamming Distance (SHD).

Podle potřeby využij NetworkX, scikit-learn a existující implementace metrik z Causal Discovery Toolbox.

Zajisti konzistentní mapování detektivem nalezených proměnných na skutečné proměnné SCM.

Neodměňuj detektiva za přidávání všech možných hran.

### 13.2 Identifiability

Původní DAG nemusí být jednoznačně identifikovatelný z observačních dat.

Ne vždy je správné požadovat přesnou orientaci všech hran.

Pokud je to možné a odpovídá to předpokladům:

- porovnávej skeleton,
- porovnávej CPDAG nebo relevantní reprezentaci Markovovsky ekvivalentních grafů,
- zohledňuj latentní proměnné,
- hodnotitelné a nehodnotitelné vztahy vykazuj odděleně.

Pokud nejsou předpoklady pro přesné posouzení identifikovatelnosti splněny, nevydávej přísné skóre orientace za objektivní pravdu.

Nezapočítávej skryté proměnné jako běžné zmeškané pozorované uzly.

Schopnost přiznat nejistotu musí být součástí evaluace.

### 13.3 Story recovery metrics

Hodnoť zejména:

- identifikaci klíčových událostí,
- hlavní příčinu,
- hlavní následky,
- posloupnost událostí,
- správnou identifikaci zasažených entit,
- odhad směru efektů,
- odhad časového zpoždění,
- falešná vysvětlení,
- kvalitu předložených důkazů.

Před generováním vytvoř strukturované story facts s důležitostí jednotlivých skutečností.

Po generování ověř, že se tyto skutečnosti skutečně realizovaly.

Pro subjektivní části lze použít nezávislého LLM judge se strukturovaným hodnoticím rubrikem.

LLM judge nesmí nahrazovat deterministické kontroly.

Ukládej identitu modelu, jeho verzi a hodnoticí prompt.

### 13.4 Effect recovery

Pokud je detektivem odhadovaný efekt srovnatelný se skutečným efektem SCM, vypočítej jeho chybu.

Rozlišuj:

- přímý efekt,
- celkový efekt,
- podmíněný efekt,
- průměrný kauzální efekt.

Nesrovnávej automaticky regresní koeficient s celkovým kauzálním efektem.

U nelineárních mechanismů lze skutečné efekty odhadovat opakovanými intervencemi v generativním modelu.

### 13.5 Evidence quality

Kontroluj:

- zda uváděný SQL dotaz skutečně existuje,
- zda se dotaz provedl,
- zda jeho výsledek podporuje tvrzení,
- zda se vztahuje ke správným entitám,
- zda nebyly zaměněny různé granularity,
- zda nejsou kauzální závěry silnější než dostupné důkazy.

---

## 14. Definice analytické obtížnosti

Obtížnost není procento chybějících hodnot.

Dataset s 30 % NA může být jednodušší než dataset s 5 % NA, pokud chybějící hodnoty v druhém případě zasahují zásadní proměnné.

Obtížnost definujeme empiricky prostřednictvím schopnosti referenčního analytika nebo agenta rekonstruovat příběh.

### 14.1 Recovery score

Vytvoř normalizované dílčí skóre v rozsahu 0–1.

Navržené výchozí váhy:

- 40 %: story recovery,
- 25 %: graph skeleton recovery,
- 10 %: identifiable edge orientation recovery,
- 15 %: effect recovery,
- 10 %: evidence quality and calibrated uncertainty.

Výchozí váhy jsou produktovým rozhodnutím, nikoliv vědecky validovaným univerzálním standardem.

Ulož je do konfigurovatelného `EvaluationConfig`.

Pokud některá metrika není pro daný scénář relevantní nebo ji nelze hodnotit, přerozděl její váhu mezi dostupné metriky podle explicitního pravidla.

Recovery score:

`R = sum(w_i * score_i)`

kde váhy dostupných metrik mají součet 1.

Obtížnost:

`D = 100 * (1 - R)`

Interpretace:

- D = 0: referenční agent rekonstruoval všechny hodnocené skutečnosti.
- D = 100: referenční agent nedokázal rekonstruovat hodnocené skutečnosti.

Nejde o absolutní vlastnost datasetu nezávislou na analytikovi.

Výsledek musí uvádět, pomocí jakého agenta, modelu, nástrojů a evaluačního rozpočtu byla obtížnost stanovena.

### 14.2 Clean baseline

Před kalibrací obtížnosti vyhodnoť baseline dataset bez dodatečného poškození, ale se stejnými pravidly pozorovatelnosti jako pro detektiva. Latentní proměnné a interní realizace šumu nesmí být dostupné ani v baseline.

Získej:

- clean_recovery_score,
- clean_difficulty.

Poté vyhodnoť poškozenou verzi:

- observed_recovery_score,
- observed_difficulty.

Vypočítej:

`difficulty_delta = observed_difficulty - clean_difficulty`

Tím oddělíš přirozenou obtížnost scénáře od dodatečné obtížnosti způsobené zhoršenou pozorovatelností a kvalitou dat.

Nízká úspěšnost detektiva již na čistých datech není automaticky důkaz chyby generátoru.

Může jít o:

- složitý příběh,
- slabý přirozený signál,
- neidentifikovatelnou strukturu,
- nedostatečný vzorek,
- slabinu použitého analytického agenta.

Pokud chce uživatel dosáhnout obtížnosti nižší než clean baseline, samotným poškozováním dat toho zpravidla nelze spolehlivě dosáhnout. Nabídni úpravu SCM, velikosti vzorku, pozorovatelnosti nebo dostupných informací.

### 14.3 Opakované evaluace

LLM detektiv je stochastický.

Proto podporuj více nezávislých běhů.

Ukládej:

- mean difficulty,
- standard deviation,
- jednotlivá skóre,
- počet evaluací,
- model agenta,
- agent configuration.

Pro finální vyhodnocení preferuj alespoň tři nezávislé běhy, pokud to rozpočet umožňuje.

Během hledání parametrů lze používat levnější aproximace nebo méně evaluací.

Pokud existuje pouze jeden běh, prezentuj skóre jako předběžný odhad.

Nevracej falešnou statistickou přesnost.

---

## 15. Automatická optimalizace obtížnosti

Toto je jedna z hlavních přidaných hodnot aplikace.

Uživatel definuje cílovou obtížnost místo konkrétních parametrů poškození.

Hledáme konfiguraci theta, která minimalizuje rozdíl mezi naměřenou a požadovanou obtížností.

`theta* = argmin |D_hat(theta) - D_target|`

Při optimalizaci respektuj omezení:

- validita clean world,
- povolené druhy poškození,
- chráněné tabulky a sloupce,
- maximální rozsah poškození,
- minimální množství relevantních informací,
- zachování analyzovatelnosti,
- omezení velikosti datasetu,
- časový a finanční rozpočet.

### 15.1 Objective function

Použij například:

```text
objective =
    abs(mean_difficulty - target_difficulty)
    + uncertainty_penalty
    + excessive_corruption_penalty
    + constraint_violation_penalty
```

Penalty za porušení tvrdého omezení musí být dostatečná, případně kandidáta úplně odmítni.

Neoptimalizuj pouze samotnou obtížnost.

Preferuj dataset, který dosáhne požadované obtížnosti s co nejmenším zbytečným poškozením, pokud uživatel neurčí jinak.

### 15.2 Optimalizační algoritmus

Využij podle potřeby Optuna.

Pro MVP lze začít:

- grid search,
- random search,
- jednoduchou adaptivní strategií,
- Optuna TPE sampler.

Obtížnost nemusí být monotónní funkcí množství poškození.

Nepoužívej bez dalšího ověření binární vyhledávání, které monotonicitu předpokládá.

Pokud lze monotonicitu konkrétní jednorozměrné konfigurace empiricky prokázat, může být jednoduché intervalové hledání vhodné.

### 15.3 Průběh optimalizace

1. Vygeneruj a validuj clean world.
2. Změř jeho baseline obtížnost.
3. Inicializuj prostor povolených parametrů.
4. Navrhni kandidátní konfiguraci.
5. Vytvoř observed dataset z původního clean world.
6. Ověř povolené zásahy a analyzovatelnost.
7. Spusť levnější evaluační fázi.
8. Vypočítej předběžné skóre obtížnosti.
9. Aktualizuj optimalizátor.
10. Opakuj až do dosažení tolerance nebo rozpočtového limitu.
11. Nejlepší kandidáty vyhodnoť nezávislými plnohodnotnými běhy.
12. Vrať nejlepší nalezený validní dataset.

Každý kandidát musí vznikat z čisté referenční databáze.

Neaplikuj poškození kumulativně na předchozího kandidáta.

Pro levnější předběžnou evaluaci můžeš používat:

- graph recovery algoritmy,
- SNR,
- množství chybějících informací,
- dostupnost klíčových proměnných,
- menší LLM model.

Tyto proxy nejsou finálním skóre a nesmí být zaměňovány za skutečnou úspěšnost referenčního detektiva.

### 15.4 Common random numbers

Při porovnávání konfigurací pokud možno zachovej stejnou čistou realitu a konzistentní základní náhodné realizace corruption operací.

Například předem vygeneruj náhodné hodnoty U pro jednotlivé buňky a při různých missingness rates používej příslušný práh.

Tím budou kandidátní konfigurace lépe porovnatelné a omezí se vliv náhodného výběru poškozených buněk.

Finální kandidát ale ověř i s nezávislými seed variantami, pokud to rozpočet umožňuje.

### 15.5 Ochrana proti trivializaci obtížnosti

Optimalizátor nesmí dosáhnout obtížnosti 100 tím, že:

- odstraní všechny řádky,
- skryje všechny důležité sloupce,
- vymaže celý příběh z pozorovaných dat,
- zničí všechny možnosti propojení tabulek,
- překročí povolené množství poškození.

Zaveď minimální observability budget.

Chraň informace zásadní pro rekonstrukci příběhu, pokud uživatel výslovně nechce vytvořit neidentifikovatelný scénář.

Rozlišuj obtížný dataset od analyticky nepoužitelného datasetu.

### 15.6 Ukončení optimalizace

Optimalizaci ukonči, pokud:

- bylo dosaženo požadované tolerance,
- byl dosažen maximální počet iterací,
- byl vyčerpán časový nebo tokenový rozpočet,
- došlo k technické chybě,
- uživatel proces ukončil.

Výchozí konfigurace může být například:

- target difficulty: 50,
- tolerance: ±5 bodů,
- maximum iterations: 8,
- screening evaluations per candidate: 1,
- final evaluation repeats: 3.

Všechny tyto parametry musí být konfigurovatelné.

Pokud cílová obtížnost není dosažitelná v rámci povolených parametrů, vrať nejlepší dostupný výsledek a vrať explicitní stav, že cíle nebylo dosaženo.

Nesnaž se výsledek uměle upravit na požadované skóre.

---

## 16. Knihovny a způsob integrace

Preferuj existující závislosti. Níže jsou doporučení pro samotnou logiku, nikoli příkaz nahradit technologický stack projektu.

| Úloha | Preferovaný nástroj | Volitelné alternativy |
|---|---|---|
| Datové kontrakty | Pydantic / stávající typovaný model | — |
| Kauzální graf, pořadí výpočtů | NetworkX | — |
| SCM, distribuce a RNG | NumPy, SciPy | DoWhy GCM, pgmpy, PyMC, Causally |
| Relační materializace | pandas + stávající SQL úložiště | DuckDB, Snowfakery, PluRel |
| Statistická validace | SciPy, Statsmodels | Pandera, scikit-learn |
| Corruption | NumPy, pandas | Jenga, PyGrinder |
| Causal discovery | causal-learn | pgmpy, LiNGAM, Tigramite |
| Skórování grafu | NetworkX, scikit-learn | Causal Discovery Toolbox |
| Kalibrace | jednoduchý adaptivní search | Optuna |
| SQL bezpečnost | stávající bezpečný SQL executor | SQLGlot pro parsování, nikoli jako jediná bezpečnostní hranice |
| LLM | stávající klient a provider | PydanticAI pouze pokud chybí vhodná orchestrace |
| Ontologie | RDFLib | pySHACL |
| Testy | pytest | Hypothesis |

Neinstaluj všechny knihovny. Nejdříve ověř malým testem, zda zvolená alternativa umožňuje přesně definovat mechanismy bez trénovacích dat a zda podporuje potřebné typy proměnných. PluRel a Snowfakery použij jen pokud skutečně zjednoduší mapování SCM na relační strukturu. Pokud je jednodušší několik funkcí nad NumPy/SciPy, preferuj je. Znovu použij stávající databázi, klienta LLM a způsob správy běhů. Nepoužívej proprietární generátor jako povinnou součást.


---

## 17. LLM role a jejich izolace

Využij existující LLM infrastrukturu. Logicky odděl role plánovače (návrh a upřesnění ScenarioSpec), případného reviewera (kontrola modelu), textového generátoru (podmíněný text), detektiva (slepá analytická rekonstrukce) a případného judge (hodnocení oproti ground truth).

Strojově zpracovatelné výstupy mají být typované a validované, s omezeným retry. Používej existující konfiguraci modelů, cache, mock a nákladové limity, pokud jsou dostupné; minimálně umožni vložit testovací implementaci detektiva pro reprodukovatelné testy optimalizace. Mock hodnocení musí být výslovně označeno v návratových datech, nikoli vydáváno za reálnou analýzu. Nepřidávej nový LLM provider ani nepřepisuj klienta, pokud to není nezbytné.


---

## 18. Integrační rozhraní a návratové kontrakty

Následující signatury jsou ilustrační, použij názvy a konvence existujícího projektu:

```python
plan_story(input_spec) -> ScenarioSpec
validate_spec(spec) -> ValidationReport
generate_world(spec, seed) -> CleanWorld
validate_world(world, spec) -> ValidationReport
observe(world, corruption_config, seed) -> ObservedDataset
analyze(observed_dataset, detective_config) -> DetectiveResult
evaluate(result, ground_truth, evaluation_config) -> EvaluationResult
optimize_difficulty(world, difficulty_config) -> OptimizationResult
```

`CleanWorld` obsahuje referenci na neměnnou realitu, ground truth a validační stav. `ObservedDataset` obsahuje referenci na samostatná pozorovaná data; corruption manifest je neveřejný a detektiv jej nedostane. `EvaluationResult` obsahuje recovery score, difficulty, dostupné dílčí metriky, důkazy, nastavení referenčního detektiva a nejistotu. `OptimizationResult` obsahuje cílovou a dosaženou obtížnost, toleranci, baseline, počet evaluací, historii kandidátů, vybranou konfiguraci, realizovaný rozsah poškození, důvod ukončení, stav dosažení cíle a identifikátory výsledných artefaktů.

Poskytni dvě oddělené cesty: manual = jedno nastavení a evaluace; automatic (default) = iterativní hledání konfigurace. Obě musí používat stejné transformační a evaluační funkce. Vracené výsledky musí být využitelné stávajícím frontendem, bez jeho implementace. Zachovej oddělený kontext detektiva a evaluátoru. Poskytni callback či existující mechanismus pro hlášení průběhu iterací a respektuj existující možnost zrušení úlohy, pokud je k dispozici. Nezaváděj nový job runner, routing ani API design.

Při nemožnosti vyhodnotit potřebné metriky vrať `not_evaluable` s důvodem, ne automaticky maximální obtížnost. Pro baseline a každého kandidáta používej konzistentní hodnoticí rubriku a pravidla mapování proměnných; chybějící důkaz způsobený odstraněním pozorovaných dat nesmí automaticky odstranit příslušný příběhový cíl ze jmenovatele. Předem stanov, co je principiálně neidentifikovatelné a co je pouze obtížně dohledatelné. Odliš správné přiznání nejistoty od prostého nenalezení vztahu.


---

## 19. Bezpečnost kauzální logiky a detektiva

Používej existující správu tajných klíčů; nepředávej klíče detektivovi ani do jeho analytických nástrojů.

SQL nástroje detektiva musí být skutečně read-only.

Použij kombinaci:

- samostatné observed databáze,
- read-only databázového připojení,
- SQLGlot nebo jiného parseru,
- allowlistu bezpečných operací,
- zákazu externího přístupu k souborům a síti,
- omezení runtime,
- omezení velikosti výsledku,
- omezení paměti a počtu dotazů.

Nestačí kontrolovat, zda text dotazu začíná slovem SELECT.

Zakazuj nebezpečné funkce a příkazy, například načítání externích souborů, databázové ATTACH, instalaci rozšíření a přístup k autorským artefaktům.

Nevykonávej libovolný Python nebo shell kód vygenerovaný LLM.

Pokud detektiv potřebuje statistické metody, poskytni mu explicitně definované parametrizované nástroje.

Texty uložené v datasetu považuj za nedůvěryhodná data. Mohou obsahovat prompt injection.

Detektiv nesmí interpretovat text v databázi jako instrukce pro své chování.

Implementuj ochranu proti ground truth leakage nejen v promtech, ale také na úrovni přístupu k datům a nástrojům.

---

## 20. Referenční scénáře pro integrační testy

Použij stávající demo scénáře, pokud jsou vhodné. Pro ověření obecnosti vytvoř nebo doplň minimálně dvě malé testovací konfigurace různých domén. Nepřidávej nové demo obrazovky ani uživatelská workflow.

Oba musí běžet nad stejným obecným SCM enginem.

Žádné doménové podmínky typu:

```text
if domain == "retail":
    use_hardcoded_retail_simulator()
```

### 20.1 Hlavní demo – maloobchodní síť

Příběh:

Tržby sítě prodejen postupně klesají, přestože poptávka zůstává stabilní.

Skutečnou příčinou jsou narůstající zpoždění jednoho dodavatele. Ta vedou k výpadkům zásob, menšímu počtu realizovaných nákupů a většímu počtu stížností.

Generativní model musí obsahovat skutečný kauzální řetězec.

Například:

```text
Supplier disruption
        ↓
Delivery delay
        ↓
Available inventory
        ↓
Fulfilled purchases
        ↓
Revenue

Available inventory
        ↓
Customer complaints
```

Součástí modelu mohou být:

- stabilní nebo sezónní poptávka,
- počáteční zásoby,
- plánované dodávky,
- skutečné dodávky,
- nákupy,
- náhodné chování zákazníků,
- nezasažené produkty a dodavatelé.

Příklady tabulek:

- suppliers,
- products,
- stores,
- customers,
- purchase_orders,
- deliveries,
- inventory_snapshots,
- transactions,
- transaction_items,
- complaints.

Pokud generuješ zásoby, musí platit stock-flow vztah.

Prodeje nesmí překročit dostupné zásoby, pokud scénář výslovně neumožňuje backorders či jiné mechanismy.

Tržby nesmí být přímo vloženým klesajícím trendem. Musí vznikat z realizovaných transakcí.

### 20.2 Doplňkový scénář z jiné domény

Vytvoř scénář například ze školství, dopravy, marketingu nebo veřejných služeb.

Musí obsahovat:

- spojité proměnné,
- kategorické proměnné,
- diskrétní proměnné,
- alespoň jednu podmíněnou nebo nelineární závislost,
- více propojených tabulek,
- parametrizovatelný strukturální šum.

Jeho cílem je prokázat, že architektura není závislá na retailu.

Oba scénáře ukládej jako deklarativní JSON/YAML konfigurace, nikoliv speciální implementace generačního enginu.

---

## 21. Testování logiky

Implementuj unit testy, integrační testy a podle potřeby property-based testy.

### 21.1 SCM

Testuj:

- detekci cyklů,
- topologické pořadí,
- správné strukturální rovnice,
- reprodukovatelnost,
- nezávislé RNG streamy,
- kategorické pravděpodobnosti,
- lagované mechanismy,
- intervenci,
- změnu strukturálního šumu,
- propagaci změn na potomky.

### 21.2 Relační engine

Testuj:

- one-to-many vztahy,
- konzistentní počty entit,
- agregace,
- referenční integritu,
- různou granularitu,
- join mapping,
- časově podmíněné vztahy.

### 21.3 Validace

Testuj:

- správně vygenerovaný dataset,
- úmyslně chybný strukturální mechanismus,
- porušený invariant,
- chybnou FK vazbu,
- nesprávný časový průběh,
- velký strukturální šum, který nesmí automaticky vést k FAIL.

### 21.4 Corruption engine

Testuj:

- reprodukovatelnost,
- skutečně realizované množství poškození,
- MCAR,
- MAR,
- MNAR,
- duplikáty,
- chráněné sloupce,
- neporušenost clean world,
- správné logování zásahů.

### 21.5 Evaluace

Testuj:

- perfektní rekonstrukci,
- neúplnou rekonstrukci,
- nadbytečné hrany,
- chybějící hrany,
- nesprávnou orientaci,
- Markovovsky ekvivalentní grafy,
- správné vyhodnocení nejistoty,
- nehodnotitelné metriky.

### 21.6 Optimalizace

Testuj pomocí deterministického mock evaluátoru:

- dosažení cílové obtížnosti,
- tolerance,
- nedosažitelný cíl,
- nemonotónní objective,
- přerušení optimalizace,
- dodržení rozpočtu,
- zamítnutí nepoužitelného kandidáta,
- návrat nejlepšího nalezeného kandidáta.

### 21.7 Bezpečnost

Testuj:

- zákaz zápisu prostřednictvím detektiva,
- zákaz přístupu k ground truth,
- zákaz přístupu k clean world,
- nebezpečné SQL dotazy,
- externí soubory,
- prompt injection v textových polích,
- neplatné nebo nebezpečné výrazy SCM.

### 21.8 End-to-end

V mock režimu musí projít:

```text
Input
→ ScenarioSpec
→ Approved spec
→ SCM generation
→ Clean dataset
→ Validation
→ Corruption
→ Detective
→ Evaluation
→ Difficulty optimization
→ Structured results
```

Dále proveď integrační test s reálným LLM API, pokud je dostupný API klíč. Pokud dostupný není, označ skutečný LLM integrační test jako neprovedený, nikoliv jako úspěšný.

---

## 22. Doporučené pořadí implementace a kritéria hotovo

1. Prozkoumej stávající generátor a kontrakty, identifikuj minimální změny.
2. Implementuj a otestuj SCM a materializaci relačních dat nezávisle na LLM pomocí deklarativních fixtures.
3. Implementuj validaci clean world, ground truth a reprodukovatelnost.
4. Implementuj pozorovací vrstvu a corruption manifest. Začni MCAR, duplikáty, numerickou chybou měření, záměnou kategorií a shuffle; další mechanismy přidávej přes registr.
5. Napoj nebo rozšiř existující detektivní logiku a vytvoř evaluátor s korektní prací s identifikovatelností.
6. Implementuj manuální režim a automatický výchozí režim kalibrace. Začni jednoduchým omezeným hledáním nad malým prostorem parametrů a ověř nezávislé finální hodnocení.
7. Napoj schválený LLM návrh SCM na engine a výsledky vrať přes existující integrační vrstvu.
8. Spusť relevantní existující i nové testy, zdokumentuj změněné kontrakty a známá omezení. Neimplementuj frontend, nový REST design ani deployment.

Kritéria hotovo pro tuto logiku:

- Schválený, editovatelný a spustitelný SCM generuje propojené tabulky bez doménově specifického kódu.
- Změna mechanismu ovlivní jeho potomky; změna pozorování nezmění hash čisté reality.
- Čisté tabulky projdou relační a mechanistickou validací; statistické odchylky se posuzují s ohledem na náhodnost a sílu testu.
- Latentní hodnoty, SCM, původní příběh a corruption manifest nejsou detektivovi dostupné ani v baseline.
- Manuální režim vrací naměřenou obtížnost bez optimalizace. Automatický režim opravdu zkouší více kandidátů podle rozpočtu, nikoli jen přepočítává procento chyb na skóre.
- Výchozí optimalizace nemění schválený SCM. Pokud je výslovně povolena změna strukturálního šumu, vyžaduje novou validaci a auditovatelnou verzi reality.
- Skóre vychází z rekonstrukce příběhu a kauzálních vztahů, nikoli jen z procenta poškození. Neidentifikovatelnou orientaci netrestá jako prokazatelnou chybu.
- Nedosažitelný cíl vrací nejlepší validní výsledek se skutečným skóre, nejistotou a důvodem ukončení.
- Testy ověřují dvě různé domény, režimy obtížnosti, konzistenci, změnu šumu, izolaci detektiva a reprodukovatelnost.
- Stávající aplikace zůstává funkční a její frontend není předmětem změny.

Na konci stručně vypiš, které moduly a kontrakty jsi skutečně změnil, které knihovny jsi použil, jaké testy prošly, co nebylo možné otestovat a co ještě potřebuje napojení ve stávající aplikaci. Neprohlašuj za implementované či ověřené věci, které nejsou funkční.

