# Story2Data – kontrolní specifikace kauzální a statistické logiky

> **Účel dokumentu:** podklad pro kontrolu existujícího kódu aplikace pomocí Codexu nebo Claude Code. Dokument popisuje dohodnutou funkcionalitu a její technický význam, nikoli návrh nové aplikace. Vychází z diskuse týmu před vytvořením implementačních promptů, včetně následně schváleného automatického režimu kalibrace obtížnosti.

## 0. Instrukce pro kontrolujícího coding agenta

Prozkoumej existující repozitář a ověř, zda je níže popsaná logika skutečně implementována, správně propojena a funkční. **Nejde o požadavek na přestavbu aplikace, redesign frontendu ani vytvoření nového projektu. V této fázi primárně provádíš audit, nikoli implementaci změn.**

Nestačí nalézt třídu, endpoint, tlačítko, konfigurační klíč nebo komentář se správným názvem. Sleduj skutečný tok dat od vstupu až po výstup, včetně volaných funkcí, výpočtů, ukládání artefaktů a toho, zda výsledek předchozí fáze opravdu ovlivňuje následující fázi. Ověřuj, zda aplikace nepoužívá pouze demo fixtures, předvyplněné výsledky, mock odpovědi, hardcoded retail logiku nebo simulované skóre namísto deklarované funkcionality.

U jednotlivých bodů rozlišuj:

- **JÁDRO** – přímo dohodnutá funkcionalita nebo podmínka nezbytná pro její správnou realizaci.
- **DOPORUČENÍ** – technické řešení navržené v diskusi; alternativní implementace je v pořádku, pokud poskytuje stejnou vlastnost.
- **ROZŠÍŘENÍ** – užitečná pokročilá možnost, nikoli povinná součást prvního funkčního prototypu.

U každého požadavku uveď stav `PASS`, `PARTIAL`, `MISSING`, `FAIL` nebo `UNVERIFIED`:

- `PASS`: skutečně implementováno a ověřeno důkazem z kódu či funkčním testem.
- `PARTIAL`: existuje jen dílčí nebo omezená implementace; popiš její hranice.
- `MISSING`: funkcionalita v kódu chybí.
- `FAIL`: funkcionalita existuje, ale prokazatelně dává nesprávné výsledky nebo porušuje požadovanou logiku.
- `UNVERIFIED`: implementaci nelze spolehlivě ověřit (např. chybí API klíč, závislost nebo funkční testovací prostředí).

Odkazuj na konkrétní soubory, funkce, třídy a pokud možno řádky kódu. Spusť existující testy a pokud je to možné, proveď malé reprodukovatelné kontrolní experimenty. Testovací experimenty prováděj bez zásahu do uživatelských dat. Pokud jsou nákladná LLM volání nedostupná nebo nejsou schválena, použij existující mock pro kontrolu orchestrace a jasně odděl, co tím ověřeno není. Nezaměňuj úspěšný mock za ověřenou analytickou schopnost reálného modelu.

Není předepsána konkrétní projektová struktura ani povinná sada knihoven. Neoznačuj chybějící knihovnu za chybu, pokud aplikace stejnou logiku zajišťuje jinak. Nezaváděj nové požadavky na vzhled, frontend, deployment, poskytovatele LLM nebo monetizaci.

---

## 1. Základní koncepce – co má aplikace dělat

### 1.1 Hlavní myšlenka

Uživatel popíše příběh nebo svět, případně dodá datové schéma či ontologii. LLM zadání zpřesní a vytvoří **explicitní strukturální kauzální model (Structural Causal Model, SCM)**. Tento model říká, jaké proměnné existují, jakým způsobem se ovlivňují, jak silné jsou jejich vztahy a jakou přirozenou náhodností jsou ovlivněny. Uživatel může tento model schválit a upravit.

Z modelu se vygenerují konzistentní čistá relační data. Programová validace ověří, zda odpovídají modelu a skutečně realizují příběh. Teprve po této validaci se vytváří analytická verze dat, kterou lze záměrně zhoršit pomocí chybějících hodnot, duplikátů, chyb měření, záměn kategorií nebo hodnot a dalších definovaných operací.

Nezávislý AI detektiv dostane pouze analytickou databázi. Snaží se z ní rekonstruovat příběh a relevantní kauzální vztahy. Jeho výsledek se porovná se známou ground truth. Z tohoto porovnání se stanoví empirická analytická obtížnost.

Uživatel může buď zadat požadovanou obtížnost a nechat aplikaci iterativně hledat vhodné parametry poškození (**výchozí režim**), nebo přímo zadat parametry poškození a získat obtížnost jako jeden z výsledků (**manuální režim**).

### 1.2 Dohodnutý tok dat

```text
Slovní zadání (+ případné schéma / ontologie)
    ↓
LLM: zpřesnění zadání
    ↓
Explicitní spustitelný SCM + relační datový model
    ↓
Schválení / úprava modelu uživatelem
    ↓
Generování čisté reality
    ↓
Programová mechanistická, relační a statistická validace
    ↓
Validovaná čistá realita + oddělená ground truth
    ↓
Pozorovací vrstva bez poškození (baseline detektiva)
    ↓
Pozorovací vrstva s volitelným poškozením
    ↓
AI detektiv (bez SCM, zadání a ground truth)
    ↓
Vyhodnocení vůči ground truth + analytická obtížnost
    ↓
Manuální změna parametrů NEBO automatická optimalizační smyčka
    ↓
Finální analytický dataset + oddělené autorské výsledky
```

### 1.3 Zásadní rozlišení, která musí kód zachovat

| Veličina / vlastnost | Co znamená |
|---|---|
| Kauzální model | Pravidla, kterými se generuje skutečný svět. |
| Čistá data | Konkrétní realizace tohoto světa, včetně přirozené náhodnosti. |
| Validita | Zda konkrétní realizace odpovídá modelu a logickým pravidlům. |
| Pozorovaná data | Informace ze světa dostupné analytikovi; mohou být neúplné či chybné. |
| Ground truth | Znalost skutečného procesu vzniku dat, událostí a mechanismů. |
| Dohledatelnost | Nakolik lze příběh a vztahy rekonstruovat z dostupných dat. |
| Obtížnost | Empirické měření dohledatelnosti vzhledem k určitému detektivovi a protokolu. |

**Platný dataset nemusí být snadno analyzovatelný. Nízké skóre detektiva není samo o sobě důkazem, že generátor vytvořil nesprávná data.**

**[JÁDRO – A01]** Ověř, že všechny fáze skutečně existují a že jejich výstupy jsou správně předávány. Obzvlášť ověř, že validace čistých dat není nahrazena hodnocením detektiva a že skóre detektiva není pouze funkcí množství uměle vložených chyb.

---

## 2. Slovní zadání a jeho převod na model reality

**[JÁDRO – A02]** LLM zpracovává uživatelský popis a převádí jej na strukturovaný návrh reality. Může identifikovat nejasnosti a navrhnout doplnění. Volitelně respektuje dodané SQL schéma nebo ontologii, jsou-li podporované existující vstupní vrstvou aplikace.

Výstupem nemá být pouze slovní popis typu „marketing ovlivňuje prodeje“, ale strojově zpracovatelná specifikace, která obsahuje:

- seznam proměnných a jejich datové typy, význam a jednotky;
- rodiče každé proměnné (kauzální závislosti);
- strukturální funkci nebo podmíněnou distribuci každé závislé proměnné;
- parametry, koeficienty, rozdělení exogenní náhodnosti a jejich parametrizaci;
- potřebné entity, tabulky, primární a cizí klíče;
- granularitu proměnných (např. zákazník, objednávka, produkt × den);
- časové zpoždění, počáteční stav, události nebo změny mechanismu v čase, pokud je příběh potřebuje;
- informaci, které proměnné budou viditelné analytikovi a které mají zůstat latentní;
- předpoklady, rozsah dat a seed;
- klíčové skutečnosti příběhu, které mají být vygenerovány a později vyhodnoceny.

Není nutné, aby všechny informace byly v jediném JSON objektu. Rozhodující je jejich existence, konzistence a využití enginem. Databázové schéma a kauzální graf jsou rozdílné struktury. Cizí klíč mezi dvěma tabulkami sám o sobě neznamená kauzální vztah mezi jejich atributy. Totéž platí pro vztahy v ontologii.

### Příklad ilustrativní specifikace

```yaml
variables:
  - id: x
    grain: entity
    mechanism:
      type: distribution
      family: normal
      mean: 10
      sd: 2
  - id: y
    grain: entity
    parents: [x]
    mechanism:
      type: linear
      intercept: 0
      coefficients: {x: 5}
      noise: {family: normal, mean: 0, sd: 3}
  - id: z
    grain: entity
    parents: [y]
    mechanism:
      type: logistic_bernoulli
      intercept: -5
      coefficients: {y: 0.1}
```

Jde o ilustraci významu kontraktu, nikoli povinnou syntaxi.

**[JÁDRO – A03]** Model je možné uživatelsky upravit před generováním. Předmětem auditu není nový frontend, ale skutečnost, že již existující editace modelu změní **výpočetní reprezentaci**, ze které engine generuje data. Pouhá změna vizuální hrany, popisku nebo nastavení ve frontendu bez dopadu na engine nepostačuje.

**Kontrolní experiment:** změň koeficient u `y` z 5 na 2, ponech ostatní parametry a seed, znovu vygeneruj realitu. Ověř změnu `y` i jejích potomků `z`, nikoli pouze změnu uložené konfigurace.

**[DOPORUČENÍ – A04]** LLM má vracet validovanou strukturovanou konfiguraci (např. JSON Schema/Pydantic), ne nestrukturovaný text. Neznámé mechanismy, neplatné typy nebo neřešitelné závislosti musí být odhaleny před generováním. Pokud engine umožňuje obecné výrazy, nesmí spouštět libovolný kód z LLM prostřednictvím neomezeného `eval`/`exec`.

---

## 3. Strukturální kauzální model (SCM)

### 3.1 Skutečně spustitelný kauzální model

**[JÁDRO – A05]** Každá generovaná proměnná musí mít mechanismus odpovídající obecnému předpisu:

```text
X_i = f_i(PA_i, U_i)
```

kde `PA_i` jsou rodičovské proměnné a `U_i` je exogenní náhodnost. Proměnná bez rodičů vzniká například vzorkováním z rozdělení. Závislá proměnná se generuje podmíněně na již realizovaných rodičích. Deterministický mechanismus může být bez náhodnosti.

Nestačí:

- mít obrázek kauzálního grafu bez funkční vazby na generátor;
- používat pouze párové korelace;
- nechat LLM generovat každou tabulku samostatně s instrukcí, že má být „konzistentní“;
- nastavit konečný trend přímo a teprve poté vymyslet doprovodná data;
- vygenerovat všechny sloupce nezávisle a následně jim přiřadit kauzální interpretaci.

Kontroluj, že skutečné hodnoty rodičovských proměnných vstupují do výpočtu potomků. Koeficienty a distribuce musí být parametry skutečného vzorkování, nikoli pouze metadata.

### 3.2 Graf, pořadí výpočtu a čas

**[JÁDRO – A06]** V rámci jednoho časového kroku se mechanismy vykonávají v topologickém pořadí nebo jiným korektním způsobem. Běžný DAG nesmí mít simultánní cyklus. Nepodporované cykly je třeba explicitně odmítnout.

Časové příběhy mohou vyžadovat dynamické závislosti, například:

```text
X_t → Y_t
Y_t → X_(t+1)
```

Taková zpětná vazba je přípustná, pokud po rozvinutí v čase nevzniká simultánní cyklus. Je nutné mít počáteční stavy pro lagované proměnné a jednoznačné pořadí výpočtu. Budoucí hodnoty nesmí nechtěně ovlivnit minulost. Pokud aplikace podporuje pouze statický SCM, je to omezení, které má audit jasně uvést, nikoli označit časový příběh za funkční bez důkazů.

**[DOPORUČENÍ – A07]** Událost příběhu lze reprezentovat změnou hodnoty nebo mechanismu pro vymezené entity a období. Podporuj alespoň takové časové změny, které vyžaduje zvolený demo příběh: skok, postupný nástup, dočasný šok nebo změnu parametru. Změna u jedné entity nesmí bez definované vazby automaticky měnit všechny ostatní entity.

Intervence do proměnné (např. `do(X=x)`), podmínění na pozorované `X=x` a změna koeficientu mechanismu nejsou obecně stejná operace. Plná kauzální inference nebo kontrafaktuální API není povinné, ale pokud je implementace nabízí, ověř jejich správný význam.

### 3.3 Typy mechanismů

**[JÁDRO – A08]** Generování nesmí být omezeno na lineární vztahy spojitých proměnných. Je nutné umět realisticky reprezentovat alespoň spojité, diskrétní a kategorické proměnné a podmíněné vztahy. Následující přehled popisuje doporučené mechanismy; není povinnost implementovat každý uvedený typ, pokud jej scénář nevyžaduje.

| Proměnná | Doporučený mechanismus | Co ověřit |
|---|---|---|
| Spojitá | lineární/nelineární funkce + šum | Koeficienty, škála a distribuce jsou skutečně použity. |
| Nezáporná spojitá | lognormální, Gamma, vhodná transformace | Podpora nezápornosti a správná parametrizace. |
| Binární | logistický model + Bernoulli | Pravděpodobnost v [0,1], výstup 0/1. |
| Počty | Poisson nebo negativně binomické rozdělení | Nezáporné celočíselné hodnoty, validní parametry. |
| Nominální kategorie | CPT nebo softmax + kategorické vzorkování | Platné kategorie a součet pravděpodobností 1. |
| Ordinální | latentní skóre a uspořádané prahy | Zachování pořadí a platných kategorií. |
| Deterministická | výpočet z rodičů | Přesná nebo tolerovaná shoda s rovnicí. |
| Časová | předchozí stav a nové vstupy | Správný lag a inicializace. |
| Textová | podmíněná strukturovaným stavem světa | Obsah není v rozporu s realizovanými událostmi. |

**[DOPORUČENÍ]** Pro obecné použití jsou vhodné také nelinearity, interakce, min/max/clip, prahové a podmíněné funkce, skupinové efekty a explicitně definované společné příčiny. Nemají být doplněny jen proto, aby graf vypadal složitě.

### 3.4 Diskrétní a kategorické mechanismy podrobněji

Binární výsledek může vznikat takto:

```text
p = sigmoid(a + b * X)
U ~ Uniform(0, 1)
Y = 1 if U < p else 0
```

Koeficient `b` není procentní změna pravděpodobnosti; jde o koeficient v logitu. Ověř, že generátor nezaměňuje pravděpodobnost `p` za realizovaný výsledek `Y`.

U nominálních kategorií lze použít tabulku podmíněných pravděpodobností (CPT):

| Dostupnost | Spokojený | Neutrální | Nespokojený |
|---|---:|---:|---:|
| Ano | 0.80 | 0.15 | 0.05 |
| Ne | 0.20 | 0.30 | 0.50 |

Engine vybere správný řádek podle skutečně realizované hodnoty rodiče a následně vylosuje kategorii. Pro více rodičů může být vhodnější softmax nebo jiný parametrický model, protože CPT rychle narůstá. Nepřidávej numerický Gaussovský šum k ID kategorií jako univerzální způsob modelování náhodnosti.

**Kontrolní experiment:** vygeneruj dostatečně velký vzorek s oběma hodnotami dostupnosti. Porovnej podmíněné četnosti s CPT v přiměřené statistické toleranci. Změň CPT a ověř odpovídající změnu dat.

### 3.5 Textové proměnné

**[JÁDRO, pokud scénář obsahuje text – A09]** Textová data jsou podmíněna skutečnou vygenerovanou realitou. Například se nejprve vygeneruje zpoždění dodávky, dostupnost produktu, skutečnost, zda byla objednávka zrušena, a typ stížnosti. Teprve poté se generuje text stížnosti.

Text nesmí svévolně přidat novou skutečnost (např. tvrdit, že zákazník zrušil objednávku, pokud žádné zrušení nevzniklo). Případný záměrně chybný popis je pozorovací chyba a musí být oddělen od čisté reality.

Pro MVP lze použít šablony, Faker, LLM nebo jejich kombinaci. Není nutné trénovat jazykový model ani mít LLM volání pro každý řádek.

Je-li text sám příčinou dalších událostí, lze pro první prototyp reprezentovat jeho kauzálně relevantní význam pomocí strukturovaných atributů (téma, sentiment, typ sdělení apod.), které skutečně vstupují do navazujících mechanismů. Volný text pak vzniká jako jejich reprezentace.

Dohlédni na únik odpovědi: realistická zákaznická stížnost může být legitimním důkazem, ale texty nemají rutinně obsahovat interní popis SCM, identifikátory scénáře nebo explicitní řešení zkopírované z promptu.

---

## 4. Generování propojených relačních dat

**[JÁDRO – A10]** Výsledkem je SQL databáze s více vzájemně propojenými tabulkami. Data jsou jednou společnou realizací SCM, nikoli skupinou nezávisle vytvořených tabulek. Generátor je obecný – žádná doména nemá být nutnou podmínkou jeho fungování.

Největší technickou výzvou je propojit mechanismy s různou granularitou. Knihovna pro SCM typicky generuje jednu plochou tabulku, což samo o sobě nestačí.

| Příklad proměnné | Granularita |
|---|---|
| Příjem zákazníka | jeden zákazník |
| Počet objednávek | jeden zákazník za období |
| Hodnota objednávky | jedna objednávka |
| Zakoupené množství | jedna položka objednávky |
| Stav zásob | produkt × prodejna × den |

### 4.1 Mapování mezi proměnnými a tabulkami

**[JÁDRO – A11]** Engine musí podporovat skutečné přenosy hodnot mezi tabulkami přes klíče a odpovídající granularitu. Minimálně ve scénářích, které aplikace deklaruje jako podporované, musí správně fungovat:

- atribut entity ovlivňující atribut jejího potomka přes FK;
- konzistentní počty záznamů a kardinality;
- agregace podřízených záznamů, pokud jsou součástí mechanismu;
- časově správné propojení, pokud se model mění v čase.

Příklad: zákazník má vygenerovaný počet objednávek 3. V tabulce objednávek musí skutečně vzniknout tři příslušné řádky; jejich hodnoty mohou záviset na příjmu tohoto zákazníka. Pokud je zároveň uložena celková útrata zákazníka, musí být konzistentní s jeho objednávkami.

**Kontrolní experiment:** vytvoř jednoduchý model se dvěma tabulkami a proměnnou, která určuje počet potomků. Ověř počet potomků každé entity, validitu FK a agregace. Změň parametr mechanismu počtu potomků a ověř, že se změnil skutečný počet řádků.

Pozor na cyklus mezi tvorbou nových entit a agregací jejich hodnot. Engine jej musí řešit explicitními fázemi/časem nebo jej odmítnout. Prostá topologická posloupnost názvů sloupců není sama o sobě dostatečná, pokud ještě neexistují potřebné řádky.

### 4.2 Obecnost a doménová nezávislost

**[JÁDRO – A12]** Retail je pouze jeden možný scénář. Model musí být v rozumném deklarovaném rozsahu definovatelný konfigurací, nikoli výhradně prostřednictvím pevně zakódovaných retail proměnných a pravidel. Nevyžaduje se podpora všech matematicky představitelných procesů, ale nepodporované konstrukce nesmí být tiše ignorovány.

Pro ověření obecnosti doporučuji alespoň dvě malé odlišné konfigurace nad stejným enginem (např. zákazníci/objednávky a studenti/předměty/docházka). Pokud funguje jen preset, uveď rozsah jako omezenou doménovou implementaci.

**[DOPORUČENÍ]** LLM lze využít k návrhu modelu, generování pomocných atributů a textů. Samotné numerické vzorkování je vhodné provádět programově. Použití existujícího generativního nástroje je rovněž přijatelné, pokud lze ověřit, že respektuje schválené rovnice, vazby a reprodukovatelnost.

---

## 5. Reprodukovatelnost a oddělení druhů šumu

### 5.1 Reprodukovatelnost

**[JÁDRO – A13]** Stejná schválená konfigurace a seed musí umožnit přehrání stejné realizace světa v rámci deklarovaného prostředí a verze enginu. Pro LLM části, u nichž není seed dostatečnou garancí, je možné uložit jejich výstupy a přehrávat je.

**[DOPORUČENÍ]** Používej oddělené náhodné streamy (např. `numpy.random.Generator` a `SeedSequence`) pro entity, mechanismy a jednotlivé typy poškození. Identifikátory streamů musí být stabilní. Při stejné dimenzi dat a změně pouze parametru směrodatné odchylky je ideální využít stejnou standardizovanou realizaci šumu. Při změně počtu entit nemusí být vždy možné zachovat všechny řádky identické; rozsah reprodukovatelnosti zdokumentuj.

### 5.2 Strukturální šum – skutečná náhodnost reality

**[JÁDRO – A14]** Strukturální šum je součástí SCM. Například:

```text
Y_true = 5 * X_true + epsilon_structural
Z_true = 2 * Y_true + eta
```

Při zvýšení strukturálního šumu `Y` se může změnit `Y_true` i `Z_true`. Nejde pouze o změnu pozorovaných hodnot. Změna proto vyžaduje nové generování dotčené reality (nebo celého světa) a opětovnou validaci.

Vhodná realizace:

```text
u ~ Normal(0, 1)
epsilon_structural = sigma * u
```

Při what-if změně `sigma` lze ponechat stejné `u`. Tím se lépe oddělí efekt parametru od nové náhodné realizace.

Pro spojitý aditivní model může být užitečný poměr signálu k šumu:

```text
SNR = Var(f(parents)) / Var(epsilon_structural)
```

To není univerzální definice pro všechny modely. U heteroskedasticity, diskrétních a nelineárních mechanismů je potřeba interpretaci přizpůsobit.

**Důležité:** větší strukturální šum nesmí automaticky způsobit nevaliditu dat pouze proto, že odhadnutý koeficient není signifikantní. Slabý signál může být zcela správným důsledkem zadaného modelu.

### 5.3 Šum měření – chyba pozorování skutečné reality

**[JÁDRO – A15]** Chyba měření je oddělena od strukturálního šumu. Například:

```text
Y_observed = Y_true + epsilon_measurement
```

Pokud je v SCM definováno `Z_true = 2 * Y_true + eta`, pak změna pozorovaného `Y_observed` nesmí změnit již vzniklé `Z_true`. Výjimka by musela být výslovně definovaná ve světě (např. rozhodnutí ovlivněné chybným senzorem); pak je pozorování samo součástí skutečného kauzálního mechanismu.

### 5.4 Kategorické a textové proměnné

Při změně přirozené náhodnosti kategorií lze měnit CPT, parametry logistického modelu nebo jiný mechanismus. Například:

```text
P_alpha(Y | X) = alpha * P(Y | X) + (1-alpha) * P_ref(Y)
```

Při `alpha = 1` zůstává původní CPT; při `alpha = 0` je výsledek nezávislý na daném rodiči, pokud je `P_ref` pro všechny jeho hodnoty stejná. To **mění strukturální mechanismus**, nikoli pouze kvalitu pozorování. Taková operace není povinná pro MVP, ale pokud existuje, vyžaduje regeneraci a validaci.

Pozorovací chyba u kategorií může být vyjádřena maticí záměn `P(observed_category | true_category)`. Analogicky mohou být chybně zaznamenána binární data nebo posunuta ordinální hodnocení. Kategorická proměnná není číslo, ke kterému se automaticky přičítá šum.

U textu může přirozenou variabilitu představovat různý slovní popis stejného stavu. Pozorovacím poškozením mohou být překlepy, zkrácení textu, odstranění informace nebo jiná explicitně povolená chyba. Změna množství informací ve zveřejněném textu nemá měnit skutečnou událost.

**Kontrolní experiment:** vytvoř řetězec `X → Y → Z`. Zvyš strukturální šum `Y` a ověř změnu pravých hodnot `Y` a `Z`. Poté z původní čisté reality zvyš pouze chybu měření `Y` a ověř, že skutečné hodnoty `Y`, `Z` a hash čistých dat zůstaly nezměněny.

---

## 6. Validace čistých dat před poškozením

**[JÁDRO – A16]** Čistá data se validují programově, nikoli pouze prostřednictvím LLM posuzujícího několik náhodných řádků. Validace má zjistit, zda data odpovídají definované realitě. Změna SCM vyžaduje novou validaci.

### 6.1 Kontrola samotného modelu

Ověř existenci odkazovaných proměnných, správnou topologii, platné parametry distribucí, datové typy, mapování mezi entitami a tabulkami, časové indexy a podporované mechanismy. Neplatný model nesmí bez upozornění přejít do generování.

### 6.2 Relační a logické invarianty

**[JÁDRO – A17]** Čistá databáze musí respektovat schválený model:

- PK jsou unikátní a nechybí;
- FK ukazují na skutečně existující řádky (s výjimkou povolených NULL);
- kardinality a počty řádků odpovídají mechanismům;
- hodnoty a typy jsou v povolených doménách;
- časové vztahy a agregace dávají smysl;
- deterministické doménové vztahy skutečně platí.

Například v retail scénáři bez dalších skladových pohybů platí:

```text
inventory_t = inventory_(t-1) + deliveries_t - fulfilled_sales_t
```

Toto je příklad invarianty daného scénáře, nikoli retailový požadavek na obecný engine. Tržby mají být odvozeny z transakcí, pokud to model definuje. Nemají být samostatným natvrdo vloženým klesajícím číslem.

### 6.3 Mechanistická validace

**[JÁDRO – A18]** Ověř, že realizované hodnoty vznikly podle schválených funkcí a správných rodičů.

U `Y = 5X + epsilon` lze při uloženém `epsilon` zkontrolovat po řádcích `Y - (5X + epsilon) ≈ 0`. U deterministické rovnice se ověřuje přesná/tolerovaná shoda. U Bernoulli mechanismu lze při uloženém `U` ověřit shodu výstupu s indikátorem `U < p`. U kategorií lze obdobně zkontrolovat správné použití CPT a realizovaného vzorkování.

Pokud se realizace šumu neukládají, lze použít reprodukovatelný seed, výpočetní trace či jiný auditovatelný postup. Mechanistická validace nemá být pouze opakováním stejné potenciálně chybné funkce. Doporučuji nezávislé malé testovací příklady s ručně vypočteným očekávaným výsledkem.

### 6.4 Statistická validace

**[JÁDRO – A19]** Aplikace má zkontrolovat, zda statistické vlastnosti čistých dat dávají vzhledem k SCM smysl. Konkrétní testy se vybírají podle typu mechanismu, nikoli univerzálně pro všechny sloupce.

| Mechanismus | Příklad relevantní kontroly |
|---|---|
| Aditivní lineární model | Odhad koeficientů, rezidua, očekávané momenty, podmíněné průměry. |
| Normální šum | Porovnání rozdělení realizovaného šumu, momentů či kvantilů. |
| Bernoulli/logit | Kalibrace realizovaných výsledků vůči pravděpodobnostem. |
| Kategorická CPT | Podmíněné četnosti a jejich přirozená výběrová variabilita. |
| Poisson | Podmíněný průměr a rozptyl s ohledem na skutečné intenzity. |
| Nelineární vztah | Podmíněné predikce nebo referenční simulace. |
| Časový model | Konzistence lagů, průběhu událostí a případných změn stavu. |

Při jednoduchém lineárním modelu může být užitečné ověřit odhadnutý koeficient vůči skutečnému parametru a jeho nejistotě. V obecnějším SCM však regresní koeficient nemusí být roven kauzálnímu efektu. Záleží na dalších rodičích, confoundingu, specifikaci modelu a estimandu. Nepoužívej špatně specifikovanou regresi jako tvrdý test validity.

Statistické testy mohou zamítnout pravdivý předpoklad čistě náhodou. Proto:

- nevyžaduj statistickou významnost každé hrany jako podmínku validity;
- nevyžaduj přesnou shodu výběrových momentů s populačními;
- u malého vzorku uváděj nejistotu nebo nedostatečnou sílu testu;
- zohledni opakované testování a závislosti v časových/skupinových datech;
- rozlišuj tvrdé porušení rovnice či invarianty od atypické, ale možné realizace.

**[DOPORUČENÍ]** Pro složitější model lze použít Monte Carlo referenční rozdělení relevantních statistik. Opakované generování může pomoci určit, zda je konkrétní realizace plausibilní. Pokud stejná chybná implementace generuje jak testovaná, tak referenční data, samotná shoda jejich statistik neověřuje správnost implementace.

### 6.5 Zda se příběh opravdu realizoval

**[JÁDRO – A20]** Vedle správnosti mechanismů je třeba zkontrolovat, zda konkrétní realizace obsahuje slíbené klíčové události. Například scénář může mít nenulovou pravděpodobnost stížnosti, ale v malém vzorku nemusí vzniknout ani jedna. Model může být správný a přesto konkrétní dataset neposkytuje zamýšlený příběh v použitelné podobě.

Rozlišuj:

1. validitu mechanismu;
2. realizaci konkrétních příběhových skutečností;
3. schopnost detektiva je odhalit.

Report má umět zachytit situaci, kdy mechanismus prošel, ale slíbený příběh se v konkrétní realizaci nevyskytl. Nedoplňuj chybějící události uměle do výsledných dat bez změny modelu nebo řádné regenerace. Případnou regeneraci podmíněnou dosažením příběhu zdokumentuj jako výběr realizace, ne jako nepodmíněné náhodné vzorkování.

### 6.6 Výsledek validace

Doporučené rozlišení `PASS`, `PASS_WITH_WARNINGS`, `FAIL` (nebo ekvivalent). Tvrdá chyba modelu nebo invarianty blokuje označení čisté reality za validní. Statistické upozornění se zaznamená a podle jeho povahy se pokračuje nebo regeneruje. Neopravuj data prostým přepisováním hodnot za účelem dosažení příznivého p-value.

---

## 7. Oddělení čistého světa, ground truth a analytické databáze

**[JÁDRO – A21]** Musí existovat logicky oddělené artefakty s různým účelem:

| Artefakt | Obsah | Přístup detektiva |
|---|---|---|
| Clean world | Skutečně vygenerované čisté hodnoty, včetně případných latentních veličin. | Ne |
| Ground truth | Schválený SCM, mechanismy, parametry, realizované události, skutečné vztahy, auditní informace. | Ne |
| Clean observed baseline | Pouze pozorovatelné proměnné, bez dodatečného volitelného poškození. | Ano, v baseline běhu |
| Observed dataset | Pozorovatelné proměnné po aplikaci vybraného poškození. | Ano, v příslušném běhu |
| Corruption manifest | Záznam zásahů, použitých parametrů a případně původních hodnot. | Ne |

Clean observed baseline není nutně shodná s úplným clean world: latentní proměnné musí zůstat skryté i při nulovém množství dodatečných chyb. Jinak by se při porovnání obtížnosti současně měnil přístup detektiva k interním informacím.

Čistá realita se nesmí přepsat při změně parametrů pozorování. Je vhodné identifikovat artefakty prostřednictvím run ID, verzí a hashů. Ground truth musí být spojena se správnou konkrétní realizací (zejména po změně SCM či strukturálního šumu).

**[DOPORUČENÍ]** Analytickou databázi materializuj samostatně, aby detektiv nemohl přes SQL proniknout do tabulek s interními hodnotami. Uchovávej informaci potřebnou k rekonstrukci strukturálního šumu a ke kontrole realizovaných událostí.

---

## 8. Observation / Corruption Engine

**[JÁDRO – A22]** Po validaci skutečného světa se aplikuje samostatná vrstva volitelných chyb. Tím vznikne analyticky dostupný dataset, který může obsahovat chyby, ale jeho skutečný underlying world zůstává stejný. Je-li poškození nulové, výsledkem je čistá pozorovatelná baseline.

### 8.1 Dohodnuté a navržené operace

| Operace | Význam | Priorita |
|---|---|---|
| Chybějící hodnoty (NA) | Odstranění některých hodnot podle nastavené intenzity. | JÁDRO |
| Duplikáty | Opakované záznamy nebo duplicity logických entit. | JÁDRO |
| Záměna hodnot | Promíchání hodnot jednoho atributu mezi záznamy. | JÁDRO |
| Numerický šum měření | Nepřesně zaznamenané skutečné hodnoty. | JÁDRO pro numerické proměnné |
| Záměna kategorií | Nesprávně zaznamenané kategorie. | JÁDRO pro kategorické chyby |
| Outliers | Výrazně chybné pozorované hodnoty. | DOPORUČENÍ |
| Textové chyby | Překlepy, zkrácení, omezení dostupné informace. | DOPORUČENÍ |
| Posun timestampů | Chybně zaznamenaný čas události. | DOPORUČENÍ |
| Skrytí sloupců / odstranění řádků | Omezení pozorovatelnosti. | ROZŠÍŘENÍ |
| Chyby FK a klíčů | Nesprávné propojení záznamů. | ROZŠÍŘENÍ, explicitní opt-in |

Není nutné, aby první verze implementovala všechny pokročilé operace. Audit musí uvést, které jsou skutečně dostupné a zda jsou využity manuálním i automatickým režimem. Podpora samotného numerického šumu nestačí, pokud aplikace tvrdí, že umí zhoršovat i kategorická nebo textová data.

### 8.2 Chybějící hodnoty

Jednoduché náhodné odstranění hodnot reprezentuje MCAR, pokud je maska nezávislá na hodnotách. Pokročilejší mechanismy:

- MAR: pravděpodobnost chybění závisí na dostupných pozorovaných informacích (např. věk zákazníka je méně často vyplněný u určitého pozorovaného typu registrace).
- MNAR: pravděpodobnost chybění závisí na hodnotě, která není pozorována, například vyšší příjmy častěji chybí právě kvůli své skutečné výši.

**[DOPORUČENÍ – A23]** Podpora MCAR/MAR/MNAR je vhodná, ale nevyžaduje se kompletní implementace všech mechanismů v prvním MVP. Pokud aplikace používá názvy MAR a MNAR, musí mechanismy odpovídat jejich významu. Ověř, zda volba mechanismu skutečně ovlivňuje masku chybění.

U procent chybění jednoznačně definuj jmenovatel (např. počet původně nechybějících způsobilých buněk konkrétních sloupců). Reportuj nastavenou i skutečně realizovanou míru.

### 8.3 Duplikáty, klíče a shuffle

**[JÁDRO – A24]** Duplikáty musí skutečně zvýšit počet duplicitních záznamů nebo vytvořit definované logické duplicity. Pokud mají dva řádky stejný primární klíč, strict SQL databáze je nemusí přijmout. Je proto potřeba například staging databáze bez fyzického vynucení příslušného omezení nebo rozlišení technického ID a očekávaného logického klíče.

V čisté databázi se stále požaduje plná referenční integrita. Záměrná porušení klíčů patří pouze do pozorovací vrstvy a nesmí vznikat neočekávaně.

Pouhé přeházení pořadí řádků není poškozením relačních dat; SQL tabulka nemá zaručené pořadí. Záměnou se v dohodnutém významu myslí promíchání hodnot vybraného sloupce mezi záznamy, případně uvnitř skupin. Tím lze zachovat marginální rozdělení a narušit vztahy s ostatními atributy.

### 8.4 Cílení a evidence operací

**[JÁDRO – A25]** Poškození musí respektovat zvolené parametry a typy proměnných. Změna parametru musí mít skutečný dopad na observed dataset. Je vhodné podporovat cílení na tabulku/sloupec, chráněné klíče, explicitní pořadí operací, seed a záznam skutečně provedených zásahů.

**[DOPORUČENÍ]** Corruption manifest obsahuje typy operací, jejich parametry, skutečný rozsah a identifikaci dotčených řádků/buněk nebo reprodukovatelné informace potřebné k jejich získání. Není nutné duplikovat kompletní původní tabulky v logu, pokud je k dispozici neměnný clean world.

Při více operacích se mohou zásahy překrývat. Součet jednotlivých požadovaných procent se proto nemusí rovnat celkovému podílu poškozených buněk.

### 8.5 Kontrola po poškození

Nezamítej pozorovaná data pouze proto, že porušují invariant, který uživatel chtěl záměrně poškodit. Ověř však, že změny jsou povolené, že nebyl změněn clean world, že chráněné části zůstaly zachovány a že dataset zůstává v deklarovaném smyslu analyzovatelný.

**Kontrolní experiment:** spusť dvě konfigurace missingness nad stejným clean world. Ověř rozdílný podíl NA, stabilitu čistých dat a reprodukovatelnost obou variant. Dále ověř, že operace shuffle mění vztahy mezi sloupci, nikoli pouze pořadí řádků.

---

## 9. Nezávislý AI detektiv

**[JÁDRO – A26]** Po validaci a vytvoření analytické verze dat následuje skutečný nezávislý analytický proces. Detektiv má rekonstruovat, co se ve světě dělo, a pokud to dostupné informace dovolují, jaké byly relevantní příčiny, následky a mechanismy. Nemá ověřovat zadanou hypotézu s předem známým řešením.

### 9.1 Blind evaluation (bez znalosti původní reality)

Detektiv dostává pouze analytickou databázi, neutrální popis její struktury a obecnou instrukci provést analýzu. Může znát význam běžných sloupců a očekávané relační vazby, protože to může znát i skutečný analytik.

Nesmí mít přístup k:

- původnímu slovnímu příběhu ani jeho zpřesněné verzi;
- SCM, koeficientům, skutečným kauzálním hranám;
- latentním hodnotám z čistého světa;
- ground truth ani předem připraveným důkazním dotazům od generátoru;
- seznamu uměle poškozených buněk a jejich původním hodnotám;
- cílové obtížnosti a předem očekávanému skóre;
- předchozím řešením stejné úlohy z optimalizačních iterací.

Ověř nejen prompt, ale i skutečné argumenty předávané LLM, dostupné nástroje a úložiště. Detektiv nesmí umět načíst interní artefakty přes SQL nebo obecný přístup k souborům. V každé nezávislé evaluaci má začínat bez znalosti předchozího řešení. Obecné instrukce a neutrální schema metadata jsou povoleny; nápověda konkrétního řešení nikoli.

### 9.2 Skutečně prováděná analýza

**[JÁDRO – A27]** Detektiv musí analyzovat data, nikoli pouze hádat z názvů tabulek nebo generovat příběh bez dotazů. Má mít dostupné skutečné analytické nástroje, minimálně inspekci schématu, read-only SQL a základní statistické operace. Jeho tvrzení mají být propojitelná s vykonanými dotazy, jejich výsledky nebo statistickými výstupy.

**[DOPORUČENÍ]** Nástroje mohou zahrnovat deskriptivní statistiku, missingness a duplicity, korelace, regresi, časovou analýzu a causal discovery. LLM rozhoduje o analýze a interpretuje výsledky; numerické výpočty provádí kód. Při JOIN musí respektovat granularitu, aby např. pouhé rozmnožení řádků nezpůsobilo fiktivní vztah. SQL nástroje musí být omezené na bezpečné čtení observed databáze, bez přístupu k externím souborům či interním databázím.

### 9.3 Causal discovery jako doplněk

**[DOPORUČENÍ – A28]** V diskusi jsme navrhli kombinovat LLM s existujícími algoritmy pro učení kauzálních grafů (např. PC, GES, FCI, LiNGAM nebo Tigramite pro časová data). Nejde o požadavek spustit všechny. Pokud žádný takový algoritmus není integrován, ale detektiv provádí doloženou analýzu a navrhuje vztahy, je základní funkce detektiva stále možná; audit má uvést chybějící doporučené rozšíření zvlášť.

Důležité je, že většina těchto algoritmů očekává analytickou matici s odpovídajícími pozorovacími jednotkami a má konkrétní statistické předpoklady. Nelze bez rozmyslu použít PC s testem určeným pro spojitá i.i.d. data na smíšené, hierarchické a časové SQL tabulky. Pokud předpoklady nejsou splněny, musí systém výsledek interpretovat opatrně nebo použít vhodnější metodu.

### 9.4 Výstup detektiva

Výsledek musí být použitelný pro následné strojové porovnání. Ideálně obsahuje:

- rekonstruovaný příběh, klíčové události a jejich časový průběh;
- odhadnuté vztahy mezi identifikovatelnými proměnnými;
- orientaci hrany nebo explicitní nejistotu, pokud směr nelze určit;
- případně odhad efektu, je-li metodologicky oprávněný;
- zasažené entity a alternativní vysvětlení;
- odkazy na skutečné důkazy (dotazy, statistiky, grafy);
- nevyřešené otázky a omezení analýzy.

**[JÁDRO – A29]** Nestačí volný text typu „v datech je problém s dodavateli“, po kterém systém přidělí neurčité skóre. Výsledek musí být dostatečně strukturovaný, aby bylo možné hodnotit alespoň hlavní skutečnosti příběhu a relevantní vazby.

---

## 10. Evaluator – porovnání detektiva se skutečnou realitou

**[JÁDRO – A30]** Evaluator je logicky oddělený od detektiva. Teprve evaluator může znát ground truth. Porovnává skutečný výsledek analýzy s konkrétním schváleným a realizovaným světem.

### 10.1 Co se hodnotí

Dvě hlavní roviny jsou rekonstrukce příběhu a rekonstrukce kauzální struktury. Nejde nutně o přesné nalezení celého generativního modelu včetně všech pomocných proměnných.

- **Příběh:** zda detektiv našel hlavní událost, příčinu, následky, zasažené entity, časové souvislosti a relevantní důkazy.
- **Struktura:** které skutečné vztahy identifikoval, které opomenul a které si nesprávně domyslel.
- **Síla vztahu:** pokud odhaduje relevantní parametr nebo efekt, zda jde o srovnatelnou veličinu a jak je jeho odhad přesný.
- **Nejistota a důkazy:** zda tvrzení skutečně opřel o analýzu a nepřisuzuje kauzalitu tam, kde má pouze korelaci.

**[DOPORUČENÍ – A31]** Pro grafové hodnocení využij precision, recall, F1 pro skeleton i orientované hrany, případně Structural Hamming Distance (SHD). Nepenalizuj automaticky za nepozorované pomocné uzly nebo za latentní proměnné, které nebylo možné přímo rekonstruovat. Proměnné detektiva musí být mapovány na správné ID, sloupec a případně granularitu; sémanticky ekvivalentní názvy nemají být mechanicky považovány za chybu.

Hodnoť také falešně nalezené hrany; jinak by agent mohl získat vysoké skóre prostým vypsáním všech možných vztahů. U efektů nesrovnávej např. regresní koeficient s celkovým kauzálním efektem, pokud nejde o stejný estimand a jednotky. Pokročilé hodnocení efektů je volitelné, pokud základní detektiv efekty spolehlivě neodhaduje.

### 10.2 Neidentifikovatelnost

**[JÁDRO – A32]** Ze samotných observačních dat nelze obecně identifikovat přesnou strukturu všech kauzálních hran. Například při standardních předpokladech DAG a podmíněných nezávislostech mohou mít `X → Y → Z` a `X ← Y → Z` stejnou Markovovsky ekvivalentní strukturu.

Evaluator proto musí rozlišovat:

- existenci vztahu;
- jeho směr, pokud je identifikovatelný;
- nejistotu nebo legitimní alternativní vysvětlení;
- skutečnosti, které nejsou dostupnými daty rozlišitelné.

Pro pokročilejší grafové hodnocení lze použít CPDAG u vhodných předpokladů nebo reprezentace s latentními proměnnými. Plná automatická identifikace ekvivalenční třídy pro libovolný dynamický relační SCM není realistickou povinností MVP. Minimálně však aplikace nesmí prezentovat přesné DAG F1 jako bezvýhradně objektivní měřítko znalosti skutečné kauzality, pokud směry nelze z dat odvodit.

Odděl report o skutečné ground truth (co generátor ví) od toho, co lze oprávněně očekávat od detektiva. U skrytých veličin může být například smysluplné ocenit správně rozpoznané nejednoznačné vysvětlení místo požadavku na znalost konkrétního nepozorovaného uzlu.

### 10.3 Důkazní kvalita a případný LLM judge

Deterministicky vyhodnocuj vše, co lze strojově jednoznačně porovnat. Slovní rekonstrukci lze doplnit nezávislým LLM hodnotitelem, který zná skutečnost a dostane předem definovaný rubric. Takový hodnotitel není totéž co detektiv. Musí hodnotit skutečné tvrzení a případné důkazy, ne pouze podobnost textu či sebevědomý tón. Pokud se používá, uveď jeho model a způsob hodnocení.

**Kontrolní experiment:** předej evaluátoru perfektní, neúplnou a chybnou rekonstrukci téhož scénáře. Ověř, že perfektní dostane lepší skóre a že nadbytečné nepravdivé hrany nezvyšují hodnocení. Přidej neurčený směr u ekvivalentní struktury a zkontroluj, že aplikace nejistotu správně rozlišuje od jednoznačně nesprávného tvrzení.

---

## 11. Analytická obtížnost

**[JÁDRO – A33]** Obtížnost je empirická vlastnost analytické úlohy vzhledem k referenčnímu analytikovi/detektivovi, jeho nástrojům, dostupným informacím a rozpočtu. Není to pouhá míra poškození, počet NA ani součet nastavení sliderů.

Příklad: 30 % chybějících hodnot v nepodstatných sloupcích nemusí mít téměř žádný dopad, zatímco 5 % chybění právě u klíčových událostí může významně zhoršit rekonstrukci.

### 11.1 Výpočet

V diskusi jsme navrhli normalizované recovery skóre `R` v intervalu 0–1 a jeho převod na obtížnost:

```text
R = weighted_combination(recovery_metrics)
D = 100 * (1 - R)
```

Příklad dílčích složek: správnost příběhu, grafové precision/recall/F1, odhad identifikovatelných směrů, kvalita důkazů, případně přesnost efektů. Konkrétní váhy jsme nedefinovali jako vědecký standard; jde o konfigurovatelné produktové rozhodnutí. Audit má popsat skutečně použitý výpočet a jeho omezení, nikoli požadovat shodu s arbitrárními vahami z pozdějších implementačních promptů.

Pokud některou složku nelze v daném scénáři oprávněně hodnotit, řeš to konzistentně (např. předem definovaným přerozdělením vah). Pro porovnání variant stejného scénáře drž hodnoticí rubric a jmenovatele stabilní. Nedovol, aby poškozením zásadních sloupců klesl počet hodnocených faktů a skóre se tím uměle zlepšilo. U nedostupných nebo neidentifikovatelných informací odděleně vykazuj pokrytí, nejistotu a úspěšnost; neskrývej pokles informační dostupnosti.

Případný rychlý proxy odhad obtížnosti (SNR, množství chyb, skóre jednoduchého algoritmu) může být užitečný při hledání parametrů, ale nesmí být prezentován jako již naměřený výsledek nezávislého detektiva.

### 11.2 Čistá baseline

**[JÁDRO – A34]** Vyhodnoť detektiva i na čistých pozorovatelných datech před dodatečným poškozením. Tím vznikne baseline obtížnost a lze rozlišit přirozenou složitost příběhu od dopadu nekvalitních dat.

```text
D_clean    = 100 * (1 - R_clean)
D_observed = 100 * (1 - R_observed)
Delta_D    = D_observed - D_clean
```

Porovnávej stejný evaluační protokol, stejnou definici cílové úlohy, kompatibilní pozorovatelné proměnné a stejný model detektiva. Pokud cíleně skrýváš další sloupce, tuto změnu pozorovatelnosti zaznamenej.

Obtížnost nemusí s poškozením monotónně růst v každém jednotlivém běhu. Detektiv je stochastický, některé zásahy mohou náhodně odstranit distraktory a některé typy poškození mají nelineární dopady. Záporný rozdíl nezakazuj ani neupravuj uměle.

Pokud detektiv selže již na čistých datech, nemusí jít o chybu generátoru. Může být slabý kauzální signál, malý vzorek, nedostatečná pozorovatelnost, neidentifikovatelnost či slabina detektiva. Validace a baseline musí zůstat oddělené.

### 11.3 Opakované běhy a interpretace

**[DOPORUČENÍ – A35]** Jeden běh jednoho LLM neposkytuje objektivní univerzální obtížnost. Pokud je to možné, spusť více nezávislých evaluací za stejného protokolu, uveď průměr, variabilitu a počet běhů. Přesný počet opakování závisí na dostupném rozpočtu; pro finální kandidát jsou vhodná např. tři opakování jako praktický začátek, nikoli silná statistická garance.

Při jediném běhu označ hodnotu jako předběžný odhad. Výsledek závisí i na modelu LLM, dostupných nástrojích, promptu a počtu povolených dotazů. Vždy je vhodné uchovat konfiguraci evaluace. Technické selhání volání, timeout nebo vyčerpaný API limit nemá být automaticky převeden na obtížnost 100.

---

## 12. Dva režimy nastavení obtížnosti

**[JÁDRO – A36]** Oba následující režimy jsou skutečně dohodnuté funkcionality. Automatický režim je výchozí.

### 12.1 Manuální režim

Uživatel přímo určuje parametry poškození (např. míru NA, duplikátů, šumu měření, záměny kategorií a hodnot). Volitelně může v samostatné části upravit samotný SCM včetně strukturálního šumu.

Aplikace:

1. vezme validovanou čistou realitu;
2. aplikuje zadanou konfiguraci pozorování;
3. spustí detektiva a evaluator;
4. vrátí analytický dataset, naměřenou obtížnost a důkazy.

V tomto režimu **není cílová obtížnost optimalizačním parametrem**. Obtížnost je výsledkem. Aplikace nesmí bez souhlasu uživatele měnit zadaná procenta, aby dosáhla určitého skóre.

### 12.2 Automatický režim (výchozí)

Uživatel zadá cílovou obtížnost na stupnici 0–100, například 70/100. Nemusí vědět, jaká kombinace NA, duplikátů a šumu k ní povede.

Aplikace hledá takovou konfiguraci pozorovacího poškození, při níž empiricky naměřená obtížnost co nejlépe odpovídá cíli. Nejde o přímé mapování 70/100 na 70 % chybějících hodnot.

**[DOPORUČENÍ]** Volitelná omezení: které typy poškození jsou povoleny, maximální intenzity, chráněné sloupce, tolerance cíle, počet iterací a evaluační rozpočet. Pokročilé volby mají mít bezpečné defaulty.

### 12.3 Co se při změně parametrů znovu vykonává

**[JÁDRO – A37]** Audituj invalidaci závislých výsledků:

| Změna | Nutná reakce |
|---|---|
| Koeficient, kauzální hrana, strukturální mechanismus | Nové generování reality/dotčených potomků, validace, nová ground truth a evaluace. |
| Strukturální šum | Totéž; lze přehrát stejné exogenní náhodné realizace. |
| Pouze NA, duplikáty, pozorovací šum apod. | Nová analytická verze z původní čisté reality, evaluace; bez zbytečné regenerace reality. |
| Cílová obtížnost | Nová či navazující optimalizace povolených parametrů nad stejnou realitou. |
| Model nebo nástroje detektiva / scoring rubric | Nová nebo odpovídajícím způsobem označená evaluace. |

Přepnutí do manuálního režimu po dokončení automatické optimalizace by mělo umožnit pracovat s nalezenou konfigurací. Parametry a jejich hodnoty mají být dostupné uživateli nebo dalšímu volajícímu kódu. Pokud změna SCM způsobí novou realitu, nesmí se skórovat podle starého manifestu.

---

## 13. Iterativní automatická kalibrace obtížnosti

### 13.1 Optimalizační problém

**[JÁDRO – A38]** Automatický režim musí obsahovat skutečnou zpětnou vazbu z evaluace do generování dalších kandidátů či jejich výběru. Hledá se parametrizace `theta`:

```text
theta* = argmin_theta |estimated_difficulty(theta) - target_difficulty|
```

Přitom `theta` zahrnuje parametry povolených operací pozorovací vrstvy. Vhodné je doplnit penalizaci nadměrného poškození, nejistoty a porušení omezení. Přesná volba algoritmu není předepsaná: může jít o systematické hledání v mřížce, random search, adaptivní vyhledávání nebo Optuna. Důležité je, že jednotlivé konfigurace jsou skutečně zkoušeny a jejich naměřené výsledky ovlivňují výběr dalšího postupu či nejlepšího kandidáta.

### 13.2 Očekávaný průběh

```text
world = generate_and_validate(approved_scm, seed)
truth = private_ground_truth(world)
baseline = evaluate(detect(clean_observation(world)), truth)

if mode == manual:
    observed = corrupt(world, user_parameters)
    result = evaluate(detect(observed), truth)
    return observed, result

if mode == automatic:
    best = baseline_candidate_if_admissible
    while budget_remains:
        theta = propose_configuration(previous_results, target, constraints)
        observed = corrupt(world, theta)
        if violates_constraints(observed):
            reject_candidate()
            continue
        result = evaluate(detect(observed), truth)
        record(theta, result)
        update_best_candidate()
        if target_tolerance_reached:
            break_or_confirm()
    confirm_best_candidate_if_budget_allows()
    return best_observed_dataset, best_parameters, measured_score, history
```

Toto je popis logiky, nikoli povinné názvy funkcí.

**[JÁDRO – A39]** Každý kandidát vzniká z původní čisté reality (včetně její pozorovací projekce), nikoli kumulativním poškozováním předchozího kandidáta. Jinak by nastavení 10 % NA po předchozích 20 % NA neodpovídalo požadované konfiguraci.

Pokud kandidát vzniká pouze přepočtem observed datasetu, opakovaně negeneruj a nevaliduj čistý SCM svět. Při změně strukturálního šumu se situace mění – viz následující bod.

### 13.3 Strukturální šum v automatické optimalizaci

**[DOPORUČENÍ – A40]** Výchozí automatický režim má měnit pouze pozorování reality. Nesmí tiše oslabovat kauzální příběh, odstraňovat jeho mechanismy ani zvyšovat přirozený strukturální šum ve skutečném světě.

**[ROZŠÍŘENÍ]** Uživatel může výslovně povolit hledání v parametrech strukturálního šumu. Každý takový kandidát ale představuje jinou realizaci či jiný SCM parametr a vyžaduje regeneraci, validaci a odpovídající ground truth. Pokud tato pokročilá možnost není implementována, není to samo o sobě chyba základní automatické optimalizace.

### 13.4 Nákladovost a nejistota

**[DOPORUČENÍ]** Lze používat levné screeningové metriky, causal-discovery baseline nebo levnější model během hledání a dražšího detektiva až při potvrzení finálního kandidáta. Pokud se tak děje, je třeba rozlišovat screeningové skóre od skutečné cílové metriky. Finální obtížnost má odpovídat deklarovanému referenčnímu detektivovi, ne jinému proxy modelu.

Při porovnávání konfigurací je užitečné využívat společné náhodné realizace. Například pro MCAR předem vytvořit `U ~ Uniform(0,1)` pro způsobilé buňky a při změně podílu `p` aplikovat masku `U < p`. Kandidáti pak nejsou zbytečně ovlivněni jiným náhodným výběrem buněk. Nezávislé ověření nejlepšího kandidáta může odhalit, že byl vybrán jen díky náhodně příznivému běhu detektiva (selection bias).

Není důvod předpokládat monotónní vztah mezi množstvím chyb a obtížností. Pokud kód používá binární vyhledávání, audituj, zda je předpoklad monotonicity pro dané omezení oprávněný. Jinak má hledání tolerovat nemonotónní odezvu.

### 13.5 Omezení a nedosažitelný cíl

**[JÁDRO – A41]** Optimalizace nesmí dosahovat obtížnosti pouze tím, že z dat odstraní veškeré relevantní informace, všechny řádky nebo znemožní spojení tabulek, pokud to uživatel výslovně nežádá. Záměrem je vytvořit obtížný, ale stále použitelný analytický problém.

Doporučená omezení: minimální počet pozorování, maximální množství chyb, chráněné identifikátory, minimální pokrytí relevantních sloupců a zachování základní analyzovatelnosti. Neexistuje univerzální hranice, která zaručí kauzální identifikovatelnost. Pokud je příběh z dat principiálně neidentifikovatelný, uveď to místo pouhého tvrzení „velmi obtížné“.

Optimalizace má konečný rozpočet (např. počet iterací, čas, počet LLM volání). Musí vrátit nejlepší nalezenou validní konfiguraci nebo explicitní informaci o selhání. Pokud nebylo dosaženo cílové tolerance, nelze zobrazit cílové číslo jako dosažené.

Příklad: čistá pozorovatelná data mají odhad obtížnosti 65. Uživatel požaduje 20. Poškozováním nemusí být možné úlohu spolehlivě zjednodušit. Systém má vysvětlit omezení a případně nabídnout změnu modelu, síly signálu, dostupnosti informací nebo rozsahu vzorku. Nemá garantovat dosažení libovolného čísla.

### 13.6 Skutečný výsledek optimalizace

**[JÁDRO – A42]** Výstup má obsahovat minimálně požadovanou obtížnost, dosaženou obtížnost, nalezenou konfiguraci a informaci, zda byla dosažena tolerance. Pro audit je vhodné zaznamenat historii kandidátů, jejich parametry, skóre, zamítnutí a vybraný nejlepší kandidát. Ověř, že exportovaný/vrácený dataset je skutečně kandidátem, pro nějž bylo uvedené skóre naměřeno.

**Kontrolní experiment:** s deterministickým testovacím evaluátorem simuluj nemonotónní skóre kandidátů, ověř výběr nejlepšího kandidáta, ukončení při toleranci a chování při nedosažitelném cíli. Tento test prokazuje algoritmickou orchestraci, nikoli skutečnou analytickou obtížnost. Tu ověř samostatným průchodem reálného detektiva, pokud je dostupný.

---

## 14. Doporučené knihovny (nikoli povinné závislosti)

V původní technické diskusi byly navrženy následující nástroje. Při auditu posuzuj především jejich skutečné použití a funkčnost, nikoli samotnou přítomnost v requirements. Kompatibilita konkrétních verzí ani integrace do existujícího repozitáře zde nejsou ověřeny.

| Oblast | Preferovaná jednoduchá varianta | Další kandidáti a omezení |
|---|---|---|
| Strukturované modely | Pydantic | Jiná typová validace je v pořádku. |
| Graf a závislosti | NetworkX | Topologické řazení a kontrola cyklů; sama negeneruje realitu. |
| Vzorkování a mechanismy | NumPy, SciPy | DoWhy GCM, PyMC, pgmpy, Causally; typicky plochá data, nutné mapování na relační strukturu. |
| Časové mechanismy | Vlastní vykonávání časových kroků | Tigramite podle vhodnosti. |
| Relační struktura | pandas + SQL engine | PluRel, Snowfakery; ověř možnost přesného řízení vlastních mechanismů. |
| SQL | DuckDB / SQLite | Zachovej existující funkční databázový backend. |
| Datová validace | SQL, NumPy, SciPy, Statsmodels | Pandera pro schémata; nenahrazuje všechny kauzální/statistické kontroly. |
| Chybějící hodnoty a poškození | NumPy/pandas | Jenga, PyGrinder; ověř podporované mechanismy a aktuální API. |
| LLM agent | Existující orchestrace | PydanticAI je jedna z možností. |
| Causal discovery | causal-learn | pgmpy, LiNGAM, Tigramite podle předpokladů a typu dat. |
| Grafové metriky | NetworkX a běžné metriky precision/recall | CDT metrics podle kompatibility. |
| Optimalizace | Jednoduchý adaptivní/grid/random search | Optuna pro větší parametrický prostor. |
| Pomocné texty | Šablony, Faker, LLM | Textová data musí vycházet ze skutečné reality. |

Není nutné použít DoWhy nebo jiný rozsáhlý framework, pokud vlastní tenká výpočetní vrstva nad NetworkX a NumPy/SciPy korektně vykonává schválený model. Stejně tak není nutné použít Optuna, pokud jednoduchý algoritmus skutečně řeší dohodnutý optimalizační problém. Nepovažuj samotné volání LLM za důkaz existence SCM nebo statistické validace.

---

## 15. Doporučené end-to-end kontrolní experimenty

Následující experimenty jsou podklady pro audit; nejsou požadavkem na nové UI ani novou rozsáhlou testovací infrastrukturu. Použij odpovídající rozhraní existujícího projektu.

### T01 – Lineární řetězec a přirozený šum

Model `X → Y → Z`, např. `X ~ Normal(10,2²)`, `Y=5X+epsilon`, `Z=2Y+eta`. Vygeneruj vzorek se známým seedem. Ověř mechanistickou shodu a relevantní statistické kontroly. Změň koeficient z 5 na 2. Ověř propagaci do `Y` i `Z`. Zvyš strukturální šum a ověř, že není automaticky odmítnut jen kvůli slabšímu signálu.

### T02 – Binární a kategorické mechanismy

Použij Bernoulli podmíněné spojitou proměnnou a kategorickou CPT. Ověř platné hodnoty, správné podmíněné pravděpodobnosti, reakci na změnu parametrů a odlišnost strukturální změny od následné záměny pozorovaných kategorií.

### T03 – Relační vazby a granularita

Vytvoř rodičovské entity, podmíněný počet potomků, atribut potomků závislý na rodiči a agregaci potomků. Ověř počty, FK, agregaci a reakci na změnu parametru. Doplň časový krok, pokud jej engine podporuje.

### T04 – Skutečná událost v příběhu

Zvol malý příběh s explicitním řetězcem příčin a následků. Ověř, že událost existuje na úrovni skutečných záznamů a že efekt nevzniká pouze přímým dosazením konečného KPI. Změň intenzitu události a sleduj skutečné potomky. Je-li hlavní příběh retailový, zkontroluj i tok zásob, realizované prodeje a navazující stížnosti podle schváleného modelu.

### T05 – Oddělení reality a pozorování

Nad jednou čistou realitou vygeneruj čistou pozorovatelnou baseline a dvě různé poškozené varianty. Ověř, že původní čisté hodnoty i interní ground truth zůstaly stejné. Opakuj jednu variantu se stejným seedem a zkontroluj reprodukovatelnost.

### T06 – Kvalita validátoru

Úmyslně vytvoř porušenou FK, chybný deterministický vztah, neplatnou CPT a simultánní cyklus. Ověř jejich odhalení. Zkontroluj, že statisticky atypická, ale mechanisticky správná realizace nezpůsobí automaticky stejný typ tvrdého selhání.

### T07 – Skutečný blind detektiv

Spusť detektiva nad observed databází a prohlédni skutečně předaný kontext a dostupné nástroje. Zkontroluj nepřítomnost původního příběhu, SCM a manifestu. Ověř, že analytické dotazy proběhly a tvrzení jsou s nimi propojena. Pokud je k dispozici pouze mock, zaznamenej omezení.

### T08 – Správnost scoringu

Použij předem připravené perfektní, částečné a nesprávné výsledky detektiva. Ověř, že skóre rozlišuje jejich kvalitu, že penalizuje falešné závěry a nezaměňuje neidentifikovatelný směr za jednoznačnou chybu. Zkontroluj, že obtížnost vychází z recovery skóre, nikoli z procenta NA.

### T09 – Manuální režim

Nastav konkrétní poškození (např. MCAR 10 % a duplikáty 5 % ve způsobilé tabulce). Ověř skutečné změny dat, provedenou evaluaci a výsledné skóre. Systém nemá tyto parametry svévolně měnit.

### T10 – Automatický režim

Zadej cílovou obtížnost, např. 70, s omezením počtu iterací. Ověř, že engine navrhuje a skutečně zkouší odlišné konfigurace, měří jejich výsledky, ukládá historii, vybírá nejlepší kandidát a vrací jeho dataset. U běhu, který náhodou splní cíl v prvním pokusu, ověř samostatně pomocí testovacího evaluátoru, že další iterace skutečně fungují.

### T11 – Nedosažitelný cíl a omezení

Nastav nemožný nebo prakticky nedosažitelný cíl v omezeném parametrickém prostoru. Ověř ukončení, návrat nejlepšího kandidáta a pravdivé hlášení nedosaženého cíle. Zkontroluj ochranu proti kompletnímu vymazání dat a proti převodu technického selhání na vysokou obtížnost.

### T12 – Obecnost

Spusť odlišný scénář s jinými názvy entit, tabulek a mechanismů bez změn doménového kódu. Ověř, že generování, validace a evaluace používají stejnou obecnou logiku. Pokud funguje pouze demonstrační konfigurace, přiznej toto omezení.

---

## 16. Požadovaný výstup auditu

Vrať konkrétní report, který umožní týmu rozhodnout, co ještě implementovat. Neuváděj pouze obecné dojmy typu „většina hotová“.

### 16.1 Souhrn

- Je základní dohodnutá pipeline skutečně funkční od vstupu po obtížnost?
- Je model reality skutečně spustitelný a editovatelný, nebo pouze popisný?
- Jsou relační data skutečně odvozena z kauzálních mechanismů?
- Je čistá realita programově validována před poškozením?
- Jsou správně odděleny strukturální šum a pozorovací chyby?
- Je detektiv skutečně nezávislý na ground truth?
- Je obtížnost empiricky měřena?
- Funguje automatický výchozí režim a manuální režim?
- Funguje iterativní optimalizace s reálnou zpětnou vazbou?

### 16.2 Tabulka požadavků

Vyplň pro položky A01–A42 z tohoto dokumentu (a podle potřeby pro další dílčí body):

| ID | Priorita | Stav | Důkaz v kódu / testu | Co chybí nebo je špatně | Doporučený další krok |
|---|---|---|---|---|---|
| A01 | JÁDRO | ... | `path:line`, test | ... | ... |

Je-li některá podmíněná funkcionalita (např. text) pro aktuální scénáře nerelevantní, uveď důvod a neoznačuj neprovedený test jako úspěšný. Doporučené technologie a rozšíření nezapočítávej mezi blokující nedostatky, pokud nejsou potřebné pro základní funkcionalitu.

### 16.3 Testy a reprodukovatelné důkazy

Uveď spuštěné příkazy, výsledky, použitou konfiguraci, případné seedy a omezení. Rozlišuj testy logiky, mock integrační test a skutečnou evaluaci s LLM. Pokud test nešlo spustit, uveď důvod. Jestliže funkce existuje, ale není volána v hlavní pipeline, označ ji jako neintegrovanou.

### 16.4 Prioritizace nedostatků

Rozděl nálezy na:

- **P0 – blokující:** nefungující SCM nebo relační generování, chybějící validace, leakage, falešné skóre, nefunkční automatická optimalizace, přepis čisté reality apod.
- **P1 – důležité:** neúplná statistická validace, nedostatečné pokrytí typů proměnných, slabá evidence, nereprodukovatelnost, omezená izolace náhodnosti, nestabilní evaluace.
- **P2 – rozšíření:** širší katalog mechanismů a poškození, pokročilá causal discovery, více detektorů, komplexní kalibrace, pokročilé měření identifikovatelnosti.

Pro každý významný nález navrhni nejmenší smysluplnou úpravu existující architektury, nikoli automaticky přepsání celého projektu. V této fázi změny neimplementuj, pokud k tomu nedostaneš další instrukci.

---

## 17. Finální kritérium souladu s dohodnutou vizí

Aplikace splňuje dohodnuté jádro, pokud je možné demonstrovat následující:

> Uživatel popíše příběh a schválí skutečný spustitelný model jeho reality. Z tohoto modelu vzniknou statisticky a relačně konzistentní čistá data, jejichž správnost je programově ověřena. Ze stejné reality lze vytvářet různě kvalitní analytické databáze. Nezávislý detektiv se pokouší odhalit příběh pouze z dostupných dat a jeho úspěšnost se hodnotí proti známé ground truth. Uživatel buď nastaví konkrétní datové chyby a získá naměřenou obtížnost, nebo zadá cílovou obtížnost a aplikace iterativně hledá a ověřuje odpovídající konfiguraci, aniž by potají měnila skutečný příběh.

To je podstatou projektu. Konkrétní frontend, názvy tříd, API, knihovny, formát ukládání a implementační detaily se mohou lišit, pokud je tato logika zachována a skutečně funguje.
