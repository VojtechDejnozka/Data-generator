# Story2Data – od příběhu k datům

Prototyp pro hackathon FIS. Z popisu firmy, prostředí a příběhu vytvoří **vzájemně propojený dataset**
(13 tabulek, ~1 mil. řádků), ve kterém je příběh dohledatelný ve **věcných a časových souvislostech**
mezi záznamy – a dodá k tomu **důkaz** (report krok za krokem, SQL, grafy, „detektiv“) i **what-if** porovnání.

Ukázkový scénář: síti prodejen klesají tržby, ale poptávka je stabilní – jeden dodavatel začne dodávat
stále později → výpadky zásob → neuskutečněné nákupy → stížnosti na nedostupnost zboží.

## Rychlý start

```bash
make setup                 # backend venv (uv / Python 3.11+) + npm install, vytvoří .env z .env.example
# do .env doplňte OPENAI_API_KEY a/nebo GEMINI_API_KEY – nebo použijte mock režim
make mock                  # offline: fixture odpovědi LLM, bez klíče
make dev                   # reálné LLM API (OpenAI / Gemini)
# UI: http://127.0.0.1:5173   API: http://127.0.0.1:8000/api/health
make test                  # backend testy (mock, vč. kauzálního jádra) + typecheck frontendu
make seed-mock             # předgeneruje 3 demo běhy (příběh, baseline, silné zpoždění) – mock
make seed                  # totéž s reálným LLM
```

Bez Supabase (`SUPABASE_URL` prázdné) běží aplikace v **lokálním režimu bez přihlášení** (`AUTH_DISABLED=1`,
nastaví `scripts/dev.sh` sám): běhy jsou soubory v `runs/` a patří lokálnímu vývojáři. Přihlášení, uživatelské
účty, kvóty a nasazení na Vercel popisuje kapitola [Účty a nasazení](#účty-přihlášení-a-nasazení-supabase--vercel).

Požadavky: Python ≥ 3.11 (vyvíjeno na 3.13), Node ≥ 20. Pokud `npm install` hlásí chybu oprávnění
k `~/.npm`, použijte `npm install --cache /tmp/npm-cache`.

### Konfigurace (`.env`)

| proměnná | význam |
|---|---|
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | klíč OpenAI a/nebo Gemini (stačí jeden) – **jen backend**, nikdy ne frontend/repo |
| `LLM_PROVIDER` | `auto` (výchozí: OpenAI, je-li jeho klíč, jinak Gemini) \| `openai` \| `gemini` – určuje výchozí modely |
| `STRONG_MODEL` / `STRONG_FALLBACK_MODELS` | plánování, validace a detektiv (výchozí `gpt-6-luna`, resp. `gemini-4-carbon` + flash fallbacky) |
| `CHEAP_MODEL` / `CHEAP_FALLBACK_MODELS` | master data, texty, `llm_rows` (výchozí `gpt-6-luna`, resp. `gemini-3.5-flash-lite`) |
| `OPENAI_*` / `GEMINI_*` varianty výše | např. `GEMINI_STRONG_MODEL` – mají přednost před obecnou proměnnou pro zvoleného providera |
| `LLM_MOCK=1` | fixture odpovědi (vývoj, testy, demo bez klíče / při vyčerpaném limitu) |
| `LLM_MAX_CONCURRENCY`, `LLM_CACHE`, `LLM_TIMEOUT_S` | souběžnost, cache odpovědí, timeout |
| `SUPABASE_*`, `VITE_SUPABASE_*`, `SIGNUP_*`, `QUOTA_*`, … | účty a nasazení – viz [tabulka proměnných](#proměnné-prostředí) |

Každý model se směruje podle názvu: `gemini-*` → Gemini API (google-genai), ostatní → OpenAI Responses API. Fallback seznam tak může kombinovat oba providery; modely, k jejichž providerovi chybí klíč, se přeskočí. Při dočasných chybách klient požadavek opakuje.


## Účty, přihlášení a nasazení (Supabase + Vercel)

Návrh a rozhodnutí: [`docs/AUTH_PLAN.md`](docs/AUTH_PLAN.md). Shrnutí:

- **Supabase Auth** – e-mail + heslo, reset hesla, odhlášení; volitelně magic link a Google OAuth
  (`VITE_AUTH_MAGIC_LINK=1`, `VITE_AUTH_GOOGLE=1` + zapnutý provider). Frontend používá jen publishable klíč.
- **Backend** ověřuje `Authorization: Bearer <access_token>` u každého `/api/*` požadavku (podpis přes JWKS
  projektu, ES256/RS256, `exp`/`aud`/`iss`). Veřejné jsou jen `/api/health`, sdílené odkazy `/api/share/…`
  a jednorázové stahovací tickety. `user_id` se bere výhradně z tokenu.
- **Vlastnictví** – každý běh, job, plánovací sezení a záznam spotřeby patří uživateli. Cizí běh vrací 404
  (API i Storage); ukázkové běhy (`is_preset`) čte každý přihlášený, měnit je nikdo kromě vlastníka/admina.
  Sdílení běhu read-only odkazem `/s/<token>` (tlačítko *Sdílet* v detailu běhu, lze zrušit).
- **Data** – metadata v Postgresu s RLS na všech tabulkách (`supabase/migrations/`), soubory běhů
  v soukromém Storage bucketu `runs` (`{user_id}/{run_id}/…`, SQLite gzip). Backend drží pracovní kopii
  v `/tmp` (LRU cache) a po každém kroku ji synchronizuje.
- **Ochrana nákladů** – denní kvóty běhů a tokenů na uživatele, rate limit úloh za minutu, evidence v tabulce
  `usage`, zbývající kvóta v hlavičce UI; registrace `open | email_allowlist | invite_only` (výchozí allowlist).
- **Dlouhé úlohy** – pipeline je rozdělená na krátké obnovitelné kroky (`plan → generate → evaluate → repair →
  finalize`); každý krok je jeden request `POST /api/jobs/{id}/advance` (průběh se streamuje přes fetch-SSE
  s hlavičkou Authorization). Přerušený krok se po vypršení zámku spustí znovu.

### 1. Supabase projekt
1. Vytvořte projekt na supabase.com. V *Project Settings → API Keys* zkopírujte **Project URL**,
   **publishable key** (`sb_publishable_…`) a **secret key** (`sb_secret_…`; legacy `anon` / `service_role`
   fungují také).
2. Migrace (tabulky, RLS, trigger profilů, bucket `runs` a jeho policies):
   ```bash
   supabase login
   supabase link --project-ref <ref>
   supabase db push                    # aplikuje supabase/migrations/*.sql
   ```
   `supabase/seed.sql` je **jen pro lokální stack** (veřejná hesla demo účtů) – na hostovaný projekt ho nespouštějte.
3. *Authentication → Sign In / Providers*: Email zapnutý, **Confirm email zapnuté**. Volitelně Google
   (client ID/secret z Google Cloud) a magic link. Pro `SIGNUP_MODE=invite_only` vypněte *Allow new users to sign up*.
4. *Authentication → URL Configuration*:
   - **Site URL**: produkční doména, např. `https://story2data.vercel.app`
   - **Redirect URLs**: `https://story2data.vercel.app/**`, `https://*-<vercel-team-slug>.vercel.app/**`
     (preview deploymenty), `http://localhost:5173/**`
   - Frontend posílá `redirectTo` (`/prihlaseni`, `/auth/nastaveni-hesla`); pokud upravujete e-mailové šablony,
     používejte `{{ .RedirectTo }}` / `{{ .ConfirmationURL }}`, ne pevné `{{ .SiteURL }}`.
5. Admin účet: zaregistrujte se a v SQL editoru spusťte
   `update public.profiles set role = 'admin', approved = true where email = 'vy@example.com';`
6. Demo preset a existující lokální běhy:
   ```bash
   cd backend
   .venv/bin/python ../scripts/seed_demo.py --mock --owner-email vy@example.com --preset   # sdílený demo běh
   .venv/bin/python ../scripts/migrate_local_runs.py --owner-email vy@example.com --dry-run  # pak bez --dry-run
   ```

### 2. Lokální vývoj se Supabase
```bash
make supabase-start      # Supabase CLI + Docker; vypíše API URL a klíče
make supabase-reset      # migrace + seed: demo@story2data.local / demo-heslo-123, admin@story2data.local / admin-heslo-123
# .env: SUPABASE_URL=http://127.0.0.1:54321, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY,
#       VITE_SUPABASE_URL, VITE_SUPABASE_PUBLISHABLE_KEY, AUTH_DISABLED=0
make dev                 # nebo make mock (LLM_MOCK=1 funguje nezávisle na přihlášení)
```

### 3. Vercel
- Projekt importujte z repozitáře; `vercel.json` definuje dvě **Services** (beta): `frontend` (Vite, `frontend/`)
  a `backend` (FastAPI, `backend/`, entrypoint `app.main:app`, `maxDuration` 300 s). `/api/*` jde na backend,
  ostatní na SPA (fallback na `index.html`).
- *Settings → Environment Variables* – nastavte pro **Production**, **Preview** i **Development**
  (`vercel env pull` stáhne Development do `.env.local`). Secret klíče označte jako *Sensitive*.
- Preview deploymenty: přidejte jejich doménu do Supabase Redirect URLs (viz výše) a do `CORS_ORIGIN_REGEX`
  jen pokud API voláte z jiného originu (při Services je vše na jedné doméně, CORS se neuplatní).
- Hobby plán: max. 300 s na request – každý krok pipeline musí doběhnout do 5 min (typicky 5–110 s).
  Velmi pomalé generování přes reálné Gemini může krok `generate` prodloužit (viděno až 494 s celý běh):
  na Pro plánu zvyšte `maxDuration` na 800, nebo nasaďte backend mimo Vercel (`backend/Dockerfile`,
  frontend s `VITE_API_BASE_URL`).
- Konfigurace Services (pole `functions` uvnitř služby, SPA rewrite) vychází z dokumentace z 2026-11-28 a **nebyla
  ověřena reálným deployem** – při prvním nasazení zkontrolujte build log a `GET /api/health`.

### 4. Průběžné nasazení z `main`
- **Vercel**: projekt `story2data` je napojený na GitHub repozitář; každý push do `main` = produkční deploy
  (https://story2data.vercel.app). Proměnné prostředí jsou nastavené jen pro *Production*.
- **Supabase**: GitHub integrace projektu (*Project Settings → Integrations → GitHub*, „Deploy to production“
  z větve `main`, Supabase directory `supabase`) aplikuje nové soubory ze `supabase/migrations/` při pushi do `main`.
  Nové migrace vytvářejte jako nový soubor `supabase/migrations/<YYYYMMDDHHMMSS>_<nazev>.sql`; existující soubory
  už neměňte (verze `20261128080552` odpovídá migraci aplikované v produkci).

### Proměnné prostředí

| proměnná | kde | význam |
|---|---|---|
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | backend (secret) | klíč OpenAI a/nebo Gemini (`LLM_PROVIDER` vybírá výchozí modely) |
| `SUPABASE_URL` | backend | URL projektu; prázdné = lokální režim bez účtů |
| `SUPABASE_PUBLISHABLE_KEY` | backend | publishable/anon klíč (dotazy jménem uživatele, platí RLS) |
| `SUPABASE_SECRET_KEY` | backend (secret) | secret/service_role klíč – jen zápisy po kontrole vlastnictví, Storage, Admin API |
| `SUPABASE_STORAGE_BUCKET` | backend | bucket s daty běhů (výchozí `runs`) |
| `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER`, `SUPABASE_JWT_AUDIENCE` | backend | volitelné přepsání (odvozeno z `SUPABASE_URL`) |
| `SUPABASE_JWT_SECRET` | backend (secret) | jen legacy HS256 lokálně, v produkci ignorováno |
| `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY` | frontend (veřejné) | Supabase klient v prohlížeči |
| `VITE_AUTH_GOOGLE`, `VITE_AUTH_MAGIC_LINK` | frontend | zobrazit volitelné způsoby přihlášení |
| `VITE_API_BASE_URL` | frontend | jen při backendu na jiném originu |
| `SIGNUP_MODE`, `SIGNUP_ALLOWLIST` | backend | `open` / `email_allowlist` (e-maily, `@domény`) / `invite_only` |
| `QUOTA_RUNS_PER_DAY`, `QUOTA_TOKENS_PER_DAY`, `QUOTA_COUNT_MOCK` | backend | denní limity na uživatele (UTC), mock tokeny se standardně nepočítají |
| `RATE_LIMIT_JOBS_PER_MINUTE` | backend | max. spuštěných úloh za minutu na uživatele |
| `CORS_ORIGINS`, `CORS_ORIGIN_REGEX` | backend | povolené originy (localhost, produkce, preview) |
| `MAX_REQUEST_BYTES` | backend | limit velikosti požadavku (výchozí 2 MB) |
| `WORKSPACE_DIR`, `WORKSPACE_CACHE_MB` | backend | pracovní kopie běhů (na Vercelu `/tmp`) |
| `JOB_AUTODRIVE` | backend | 1 = kroky úloh běží na pozadí (lokálně), 0 = řídí je klient (Vercel) |
| `AUTH_DISABLED` | backend | **jen lokálně**; aplikace odmítne start, je-li nastaven `VERCEL_ENV` nebo `APP_ENV=production` |

Všechny proměnné bez prefixu `VITE_` zůstávají na serveru; do `VITE_*` nikdy nedávejte tajemství.

### Ověření end-to-end
Automaticky (mock LLM, bez Supabase): `tests/test_e2e_auth.py` projde registraci (token z allowlistu) →
plán → schválení specu → generování po krocích přes `/advance` → report → what-if → porovnání → sdílení →
odhlášení; `tests/test_storage_jobs.py` totéž se smazáním `/tmp` a paměti mezi kroky (simulace serverless).
Ručně proti `supabase start` + `make mock`: registrace na `/registrace` → přihlášení → *Nový scénář* →
*Schválit a generovat* → report → what-if → *Odhlásit*; druhým účtem ověřte, že první běh není vidět
(ani přes `/runs/<id>`), a že `GET /storage/v1/object/runs/<cizí user_id>/…` s tokenem druhého uživatele vrací chybu.

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

### Vstup
Uživatel zadává jen **popis příběhu**, **velikost dat** (malý / střední / velký) a **seed**. Zbytek navrhne STRONG
model a v review je vše editovatelné: popis firmy a prostředí, počty entit, intenzita příběhu, knoby a také
**zda data mají časovou osu**. Příběhy, které nejsou o vývoji v čase, mají `period_start/period_end = null` a
generují se bez časových oken (cesta `llm_rows`). Simulátor `retail_supplier_delay` je časový, takže období
vždy doplní (model ho zvolí podle příběhu, jinak 2025-01 – 2026-03).

### Datové kontrakty
`backend/app/contracts/` – `ScenarioSpec` (meta, tables, story/causal_chain/distractors, knobs,
invariants, evidence, dashboard), `PlannerResponse`, `GenerationPlan`, `ValidationVerdict`. Všechna
LLM volání používají structured output (`text.format` odvozené z Pydantic) + validaci +
retry s chybovou hláškou (max 2×). Když API (OpenAI / Gemini) schéma odmítne, klient přepne na JSON mode se schématem
v promptu. Pokud plánovaný příběh odpovídá modulu, STRONG vrací jen kompaktní `ArchetypePlan`
(úpravy šablony), což je rychlejší a spolehlivější než generování celé specifikace.

### Bezpečnost a robustnost
- API klíč jen v `.env` na backendu (`.env` je v `.gitignore`), frontend volá jen `/api`.
- Přihlášení přes Supabase, autorizace na backendu i v RLS (viz [Účty a nasazení](#účty-přihlášení-a-nasazení-supabase--vercel));
  SQL z UI/detektivu se otevírá až po kontrole vlastnictví a jen nad databází daného běhu.
- Chyby: uživatel vidí srozumitelnou zprávu, detail (traceback) jen v logu backendu s referenčním kódem.
- SQL od AI/uživatele: textová kontrola (jediný SELECT/WITH), SQLite `mode=ro`, authorizer povolující
  jen čtení, časový limit (progress handler) a limit řádků.
- STRONG validátor dostává **kompaktní profil** (statistiky, vzorky, výsledky kontrol a důkazních dotazů),
  nikdy celý dataset.
- Omezená souběžnost, backoff, cache odpovědí podle hashe vstupu (`.cache/llm`), log tokenů a odhad
  ceny na běh, uložení všech LLM odpovědí do `runs/<id>/llm_calls.jsonl` → běh lze přehrát.
- Reprodukovatelnost: nezávislé RNG proudy na komponentu (`[seed, stream]`), takže změna knobu
  neposune náhodnost jiných částí. What-if běhy mají **identickou poptávku**.

## Kauzální jádro – spustitelný SCM (`backend/app/causal/`)

- V **Novém scénáři** zvolte SCM a popište příběh. Soubory SQL a Turtle (`.sql`, `.ttl`, každý do 50 000 znaků)
  dávají plánovači podpůrný kontext. Počty tabulek, kauzálních proměnných, instancí základních entit a
  řádků jsou orientační. Klasický scénář zůstává dostupný ve stejném formuláři.
- Existující SCM v JSON lze otevřít přímo po kontrole formátu a spustitelnosti. Editor používá model současně
  pro graf, parametry mechanismů a export. Exportovaný JSON obsahuje seed a verzi formátu. Stejný model
  a verze enginu dávají při stejném seedu stejná čistá data včetně textů ze šablon.
- Po validaci čistých dat **automatický režim** zkouší různé konfigurace pozorovacích chyb a vybere nejlepší
  dostupnou podle odhadu analytické obtížnosti. **Ruční režim** vytvoří jednu pozorovanou kopii bez evaluace.
  Procento NA je pravděpodobnost chybějící hodnoty v každé způsobilé buňce. Pozorovací šum je normální chyba
  se směrodatnou odchylkou zadanou jako procento odchylky čistého číselného sloupce. SQLite s pozorovanými
  daty má na výsledku samostatný odkaz. Report, dashboard a prohlížeč pracují s čistými daty.

Kontrakty, API a omezení popisuje [dokumentace kauzálního jádra](docs/CAUSAL_CORE.md).

## Generátor dokumentů (záložka *Dokumenty*)
Stejná pipeline pro nestrukturované výstupy – lékařské zprávy, dopisy, e-maily, posudky, reporty…
Formáty **PDF, HTML, Markdown, TXT, JSON, e-mail (.eml)**.

1. **Zadání:** popis požadovaných dokumentů, počet, formát, seed; volitelně **navázání na tabulku existujícího
   datového běhu** (1 řádek = 1 dokument, např. stížnosti → formální odpovědi zákazníkům).
2. **Plán (STRONG):** typ dokumentu, **fakta** (typovaná pole; „musí zaznít“ = hodnota musí být v textu),
   **sekce** (text / seznam / klíč–hodnota / tabulka), styl, rozsah slov a volitelně **skrytý vzor**
   (např. „pacienti na léku X mají zvýšené ALT“) jako změny faktů u části dokumentů. Vše editovatelné v review.
3. **Fakta:** CHEAP model přes cestu `llm_rows` (validace typů, rozsahů, výčtů), nebo řádky zdrojové tabulky.
4. **Skrytý vzor** se vkládá deterministicky (seed) do vybraných dokumentů; jejich ID jsou jen v ground truth.
5. **Psaní (CHEAP):** každý dokument vzniká strukturovaně z faktů. **Kontroly:** všechny sekce, doslovné
   zmínění povinných faktů (včetně variant zápisu čísel a dat), délka, žádné placeholdery. Chybné dokumenty se
   přepíšou s chybovou hláškou (max. 2 iterace).
6. **Vykreslení + validace (STRONG)** na vzorku dokumentů a statistikách zasažených vs. ostatních.
7. **Výstup:** náhled a stažení dokumentů, fakta jako SQLite/SQL, ZIP export, ground truth na vyžádání.

### Soubory z dat běhu (záložka *Soubory z dat* v běhu)
Z libovolné tabulky vygenerovaného datasetu lze vytvořit soubory – 1 řádek = 1 soubor: z objednávek
**faktury**, z dodávek **dodací listy**, z transakcí **účtenky**, ze stížností **odpovědi zákazníkům**…
(tlačítko „📄 Soubory z tabulky“ je i v data browseru). Řádek se automaticky obohatí:
- o údaje z navázaných tabulek přes FK (dodavatel, prodejna, zákazník…),
- o **položky** z podřízené tabulky (`purchase_order_lines`, `transaction_items`) včetně názvů produktů,
- o **částky spočítané deterministicky z dat** (řádkové částky, základ, DPH 21 %, celkem). U nákupních dokladů
  se ukazují nákupní ceny, u prodejních nikdy interní náklady.
Tabulka položek a součty se do dokumentu vykreslí přímo z databáze (sekce typu `data_table`), LLM píše jen
okolní text. Volitelný filtr (např. jen `supplier_id = 2`) a seedovaný výběr řádků. Příběh dat se do souborů
přenese sám (faktury problémového dodavatele mají např. pozdější dodání).

Každý dokument nese viditelné označení **„SYNTETICKÝ DOKUMENT“** (u PDF v patičce), aby nemohl být zaměněn
za skutečný záznam. PDF používá písmo DejaVu Sans (licence v `backend/app/documents/fonts/LICENSE_DEJAVU`).

## Výstupy běhu (`runs/<run_id>/`, se Supabase ve Storage `runs/{user_id}/{run_id}/`)
`data.sqlite`, `run.json` (spec, plán, knoby, verdikt, kontroly, signály, spotřeba tokenů, repair
historie), `evidence.json`, `checks.json`, `dashboard.json`, `manifest.json` (ground truth),
`llm_calls.jsonl`, `detective.json`. Export z UI: CSV (ZIP) a SQLite.

## Demo skript (3–5 minut)

Předem: `make seed` (nebo `make seed-mock`) a `make dev`. Otevřete http://127.0.0.1:5173.

1. **Zadání (30 s)** – *Nový scénář* → *⚡ Preset*. Uživatel zadává jen **popis příběhu, velikost dat a seed**;
   firmu, prostředí, počty entit, intenzitu i to, zda data potřebují časovou osu (a jaké období),
   rozhodne LLM. *Navrhnout scénář* → průběh (SSE).
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
Účty a bezpečnost: ověření JWT (platný, expirovaný, cizí podpis, špatné `aud`/`iss`, `alg=none`, rotace JWKS),
401 na každé neveřejné routě, izolace dvou uživatelů (běhy, joby, sezení, SQL, stahování, sdílení, presety),
kvóty a rate limit, režimy registrace, odmítnutí `AUTH_DISABLED` v produkci, synchronizace se Storage,
obnovení kroků jobu na „nové instanci“, migrační skript. `make test-rls` spustí migrace na dočasném
Postgresu (s náhradou schémat `auth`/`storage`) a ověří RLS a Storage policies (potřebuje `PG_BIN` nebo `initdb`).

## Známá omezení
- Jeden plně implementovaný simulátor (maloobchod / dodavatel). Jiné příběhy jdou cestou `llm_rows`,
  která je slabší (konzistence zajišťují validace a repair).
- Distraktory přidané v review jsou v simulačním archetypu jen dokumentační, pokud je modul nesimuluje;
  tabulky/sloupce přidané do `llm_rows` tabulek se generují.
- Úlohy jsou perzistentní a rozdělené na kroky; při `JOB_AUTODRIVE=0` (Vercel) běží jen dokud je stránka
  otevřená – po návratu na stránku běhu se pokračuje od posledního dokončeného kroku.
- Zablokovaný uživatel nemůže volat API okamžitě, ale už vydaný access token platí do `exp` (výchozí 1 h)
  pro přímé čtení vlastních řádků přes Data API.
- Supabase Free: soubor max. 50 MB (klasický běh ≈ 20 MB po gzip), 1 GB Storage celkem.
- Mock detektiv i mock validátor jsou deterministické: detektiv pouští pevnou sadu SQL dotazů, ale
  závěr odvozuje z jejich skutečných výsledků; validátor vychází z kontrol a měřených signálů.
