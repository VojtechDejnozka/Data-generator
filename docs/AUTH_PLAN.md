# Story2Data – autentizace, uživatelské účty a nasazení (Supabase + Vercel)

Stav: **implementováno** (fáze 1–7, větev `feat/auth-supabase`); stav ověření viz kap. 10. Dokumentace Supabase a Vercelu ověřena 2026-11-28
(zdroje v kap. 9). Kde jsem něco ověřit nemohl, je to označené **[neověřeno]**.

---

## 1. Zjištěný stav (před změnou)

| oblast | stav |
|---|---|
| Backend | FastAPI (`backend/app/main.py`, ~60 endpointů pod `/api`), Python 3.13, žádná autentizace, CORS jen `localhost:5173`. |
| Joby | `app/jobs.py`: in-process `asyncio.create_task`, stav v paměti (`JobManager.jobs`), průběh přes SSE `GET /api/jobs/{id}/events` (nativní `EventSource`), zrušení `POST /api/jobs/{id}/cancel`. Restart procesu = ztráta jobů. |
| Typy jobů | `plan`, `replan`, `run`, `whatif`, `detective`, `observe`, `difficulty`, `doc_plan`, `doc_replan`, `doc_run`. LLM volají všechny kromě SCM `observe`/`difficulty` se statistickým detektivem. Synchronně volá LLM i `POST /api/runs/{id}/commentary`. |
| Pipeline | `app/pipeline.py::execute_run` – jeden dlouhý coroutine: GenerationPlan (STRONG) → master data (CHEAP) → simulace → LLM řádky → SQLite → kontroly → evidence → validace (STRONG) → až 2× repair → dashboard. Stav (`Generator`: dims, tabulky jako DataFrame, manifest) jen v paměti. SCM běhy (`causal/service.py::execute_causal_run`) a dokumenty (`documents/pipeline.py::execute_doc_run`) jsou samostatné. |
| Úložiště běhů | Adresář `DATA_DIR/{run_id}/` (`storage/runs.py`): `run.json` (spec, plán, verdikt, usage, llm_calls…), `data.sqlite`, `manifest.json` (ground truth), `checks/evidence/profile/dashboard/detective.json`, `llm_calls.jsonl`; SCM navíc `world/`, `worlds/{hash}/`, `observations/{obs}/observed.sqlite`, `private/corruption/`, `evaluations/`, `difficulty/`; dokumenty `documents/*` + `written.json`. Plánovací sezení v `DATA_DIR/_sessions/{sid}.json`. Přímý přístup k souborům je rozprostřený v ~40 místech (main, pipeline, causal/service, documents, reporting, detective). |
| Velikosti | 38 lokálních běhů, 1,4 GB. Klasický běh: ~1,1 mil. řádků, **`data.sqlite` ≈ 72 MB, gzip ≈ 20 MB**. SCM/dokumenty < 1 MB. |
| Doba běhu | Mock 3–5 s; reálné Gemini 40–113 s, jeden běh 494 s (pomalé LLM řádky). SCM < 1 s, dokumenty 35–115 s. |
| Gemini klíč | Jen backend (`config.py`, `GEMINI_API_KEY`), `llm/client.py` – `UsageTracker` sčítá tokeny a odhad ceny na běh. Cache odpovědí na disku (`CACHE_DIR`). |
| Mock | `LLM_MOCK=1` nebo chybějící klíč → fixture odpovědi (`llm/mock*.py`), tokeny odhadnuty `len/4`. |
| SQL | `storage/sql_guard.py::run_readonly` a `causal/sqlsafe.py` – read-only připojení (`mode=ro`), whitelist SELECT, timeout. Otevírá se vždy `db_path(run_id)` – izolace je jen přes run_id. |
| Presety | `presets/retail_demo.py` – vstupní preset (ne běh). `scripts/seed_demo.py` předgeneruje 3 demo běhy. |
| Frontend | Vite + React 19 + react-router 7, bez globálního stavu; API klient `src/api.ts` (`fetch` bez hlaviček) + `watchJob` (EventSource). Stahování přes `<a href="/api/...">` (export ZIP, SQLite, soubory dokumentů). |
| Testy | 156 pytest testů (mock), `tests/test_api.py` – E2E přes `TestClient` s pollingem jobu. |

## 2. Cílová architektura

```
Browser (Vite SPA, supabase-js s publishable klíčem)
   │  1) přihlášení / registrace / reset ─────────────▶ Supabase Auth
   │  2) Authorization: Bearer <access_token>
   ▼
Vercel ─ služba "frontend" (statický build)   ─ služba "backend" (FastAPI, Python Fluid function)
                                                  │ ověří JWT přes JWKS (ES256/RS256), exp/aud/iss
                                                  │ čtení metadat: PostgREST s JWT uživatele  → RLS
                                                  │ zápis metadat/usage/storage: secret klíč (jen backend)
                                                  ▼
                                   Supabase Postgres (profiles, runs, jobs, plan_sessions, usage,
                                   app_admin…, RLS všude)  +  Storage bucket `runs` (soukromý)
                                                  │
                                   /tmp pracovní kopie běhu (LRU cache)  ── Gemini API
```

### 2.1 Klíčová rozhodnutí

1. **Ověření tokenu**: JWKS `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`, povolené jen
   asymetrické algoritmy `ES256`/`RS256` (`kid` z hlavičky), kontrola `exp`, `iat`, `aud == "authenticated"`,
   `iss == <SUPABASE_URL>/auth/v1`, `role == "authenticated"`, `is_anonymous == false`. JWKS cache 10 min,
   při neznámém `kid` jeden vynucený refresh (max 1× za 30 s). Legacy HS256 (lokální `supabase start`
   se starším CLI) jen pokud je explicitně nastaven `SUPABASE_JWT_SECRET` a `VERCEL_ENV != production`.
   `user_id = sub` z tokenu – nikdy z těla požadavku. Autorizace (role admin) jen z `profiles.role`
   (ne z `user_metadata`, ta je editovatelná uživatelem).
2. **Klíče**: frontend `VITE_SUPABASE_URL` + `VITE_SUPABASE_PUBLISHABLE_KEY` (`sb_publishable_…`, příp.
   legacy anon). Backend `SUPABASE_SECRET_KEY` (`sb_secret_…`, příp. legacy service_role) posílá v hlavičce
   `apikey` (legacy JWT navíc v `Authorization`). Secret klíč nikdy neopustí backend.
3. **Kdo čte a kdo píše**:
   - *čtení* metadat (seznam běhů, detail, kontrola vlastnictví) jde přes PostgREST **s JWT uživatele** →
     platí RLS; backend navíc ověřuje vlastnictví v kódu (obrana do hloubky);
   - *zápisy* (runs, jobs, usage, plan_sessions, storage) dělá backend secret klíčem **až po ověření
     vlastnictví**. Uživatel nemá na tabulky žádné INSERT/UPDATE/DELETE politiky (výjimka: vlastní
     `profiles.display_name`). Důvod: uživatel nesmí přímým voláním Data API vynulovat `usage`, přepsat
     verdikt, podstrčit stav jobu nebo nahrát soubor, který backend později načte.
4. **Datasety → soukromý Storage bucket `runs`**, cesta `{owner_id}/{run_id}/{relpath}`. SQLite soubory
   (`*.sqlite`) se ukládají gzip (`*.sqlite.gz`, 72 MB → 20 MB – vejde se do limitu 50 MB/soubor na Free
   plánu). Storage policies: SELECT jen vlastník (první složka = `auth.uid()`), presety (složka presetového
   vlastníka) a nikdo jiný; INSERT/UPDATE/DELETE pro `authenticated` **žádné** (jen backend). Backend drží
   **pracovní kopii** běhu v `/tmp/story2data/{run_id}` (LRU, limit `WORKSPACE_CACHE_MB`): před čtením
   `ensure_local()` stáhne chybějící soubory, po mutaci `sync_up()` nahraje změněné (index souborů
   s velikostí a sha256 v `runs.files`). Tím zůstává stávající souborový kód (SCM, dokumenty, report,
   detektiv, SQL guard) beze změny a izolace je vynucena na jediném místě – ve vrstvě, která vybírá
   pracovní kopii až po kontrole vlastnictví.
   *Alternativa (data v Postgresu)* zamítnuta: 1,1 mil. řádků × 13 tabulek na běh by na Free plánu
   (500 MB DB) vyčerpalo prostor po 3–4 bězích a SQL guard / detektiv / SCM pracují nad SQLite.
5. **Lokální režim bez Supabase**: když `SUPABASE_URL` chybí, použije se `LocalMetaStore`
   (JSON soubory v `DATA_DIR/_meta`) a `LocalBlobStore` (adresář) – se stejným rozhraním a stejnými
   kontrolami vlastnictví v kódu. Používají ho testy (dva uživatelé, testovací JWKS) a `AUTH_DISABLED=1`.
6. **Pipeline po krocích (stage)**: job je řádek v tabulce `jobs` (`stage`, `status`, `state`, `lease_until`,
   `events`). Každá fáze je **samostatný request** `POST /api/jobs/{id}/advance`, který provede jednu fázi
   (pod zámkem `lease_until`) a streamuje její průběh jako SSE přes `fetch` (s hlavičkou `Authorization`).
   Frontend opakuje `advance`, dokud job neskončí. Přerušený request → po vypršení leasu se fáze spustí
   znovu (idempotentní). Klasický běh má fáze:
   `plan` → `generate` → `evaluate` → (`repair_1` → `repair_2`) → `finalize`; mezi fázemi je stav
   v `jobs.state` (malý JSON) a v pracovní kopii běhu (`private/_gen_state.pkl.gz`, `private/_ev.json`),
   která se po každé fázi synchronizuje do Storage. Generování je deterministické (seed), takže opakování
   fáze dá stejný výsledek. Ostatní joby (SCM běh, dokumenty, detektiv, observe, difficulty, plánování)
   jsou jednofázové – každý se vejde do jednoho requestu. Lokálně (`JOB_AUTODRIVE=1`, výchozí mimo Vercel)
   může backend fáze řídit i sám na pozadí – frontend pak `advance` volá stejně a jen se připojí k průběhu.
   *Proč ne fronta/worker*: na Hobby plánu je jediný garantovaný mechanismus request; `waitUntil` pro
   Python není dokumentovaný, Vercel Workflow/Queues by znamenaly přepis pipeline. Pickle stavu je bezpečný,
   protože do Storage smí zapisovat jen backend.
7. **Vercel**: projekt se dvěma **Services** (beta): `frontend` (root `frontend/`, Vite) a `backend` (root
   `backend/`, entrypoint `app.main:app`), rewrite `/api/(.*)` → backend, zbytek → frontend s SPA fallbackem.
   `maxDuration` 300 s (Hobby max). Na Pro lze zvýšit na 800 s.
   *Fallback*, pokud Services nebo limity nevyhoví (např. jedna fáze > 300 s na Hobby): backend jako
   samostatný kontejner (Fly.io / Render / Cloud Run, `backend/Dockerfile`), frontend dál na Vercelu
   s `VITE_API_BASE_URL`. Kód je na to připravený (CORS, absolutní base URL, žádná závislost na Vercelu).
8. **Kvóty a rate limit**: tabulka `usage` (záznam na každou dokončenou LLM fázi: tokeny, cena, volání,
   model, mock). Před spuštěním LLM jobu a před každou LLM fází se kontroluje
   `runs_today < QUOTA_RUNS_PER_DAY` a `tokens_today < QUOTA_TOKENS_PER_DAY` (UTC den; admin bez limitu).
   Rate limit `RATE_LIMIT_LLM_PER_MINUTE` počítá joby uživatele za posledních 60 s v DB (funguje i napříč
   instancemi). V mock režimu se tokeny do kvóty nepočítají (`QUOTA_COUNT_MOCK=0`), běhy ano.
   Překročení → HTTP 429 s českou zprávou a `quota` objektem; UI zobrazuje zbývající kvótu v hlavičce.
9. **Registrace** `SIGNUP_MODE = open | email_allowlist | invite_only` (výchozí `email_allowlist`):
   vynucuje backend při každém požadavku (účet mimo allowlist/pozvánku dostane 403) podle env konfigurace;
   trigger `handle_new_user` založí profil a nastaví `profiles.approved = true` pro pozvané (`auth.users.invited_at`).
   Admin může účet schválit (`approved`) i mimo allowlist. Allowlist: `SIGNUP_ALLOWLIST`
   (e-maily a `@domena.cz`). `invite_only`: aktivní jsou jen pozvaní (admin endpoint volá Supabase
   `invite`) – doporučeno i vypnout „Allow new users to sign up“ v dashboardu. Admin může účet zablokovat
   (`profiles.status = 'blocked'`), backend ho pak odmítá; pro okamžité odříznutí od Data API admin akce
   navíc volá Supabase Admin API `ban_duration` (smazání uživatele nezneplatní již vydané tokeny).
10. **Sdílení**: `runs.share_token` (32 B náhodný, URL-safe). Veřejné endpointy `GET /api/share/{token}/…`
    vrací jen read-only data daného běhu (detail, report, dashboard, schéma, tabulky, ground truth)
    a `POST …/query` přes stejný SQL guard; žádné joby, žádné LLM. Odvolání = nový/NULL token.
11. **Presety**: běhy s `is_preset = true` (vlastník = systémový/demo účet) čte každý přihlášený,
    zapisovat do nich smí jen admin (backend kontroluje roli; RLS nedává zápis nikomu kromě service role).
12. **Stahování**: `<a href>` bez hlavičky nejde → `POST /api/runs/{id}/download` vrátí krátkodobou URL:
    se Supabase **signed URL** objektu ve Storage (60 s; SQLite jako `.sqlite.gz`), lokálně jednorázový
    ticket `/api/download/{ticket}` (60 s, single-use). Žádné access tokeny v URL. Řeší i limit 4,5 MB
    na odpověď funkce na Vercelu.

## 3. Datový model (supabase/migrations)

| tabulka | sloupce (hlavní) | RLS (authenticated) |
|---|---|---|
| `profiles` | `id uuid pk → auth.users`, `email`, `display_name`, `role ('user'|'admin')`, `status ('active'|'blocked')`, `approved bool`, `created_at` | SELECT vlastní (admin všechny); UPDATE vlastní jen `display_name` (sloupcový GRANT) |
| `runs` | `id text pk`, `user_id`, `kind`, `engine`, `label`, `name`, `status`, `parent_run_id`, `is_preset`, `share_token`, `summary jsonb` (verdikt, row_counts, knob_values, mock, usage…), `files jsonb`, `created_at`, `updated_at` | SELECT vlastní nebo `is_preset` |
| `jobs` | `id text pk`, `user_id`, `kind`, `run_id`, `status`, `stage`, `phase`, `progress`, `state jsonb`, `events jsonb`, `result jsonb`, `error`, `cancel_requested`, `lease_until`, `lease_holder`, `attempts`, `created_at`, `updated_at` | SELECT vlastní |
| `plan_sessions` | `id text pk`, `user_id`, `kind`, `data jsonb`, `created_at`, `updated_at` | SELECT vlastní |
| `usage` | `id bigint`, `user_id`, `job_id`, `run_id`, `kind`, `calls`, `tokens`, `cost_usd`, `mock`, `created_at` | SELECT vlastní (admin všechny) |
| storage `runs` | `{user_id}/{run_id}/…` | SELECT vlastní složka nebo presetový běh |

Všechny tabulky: `enable row level security`, explicitní `GRANT` (od 2026-10-30 nejsou nové tabulky
v `public` automaticky vystavené Data API), `service_role` má plná práva. Pomocné funkce
`public.is_admin()` (SECURITY DEFINER, `search_path = ''`, jen čte `profiles.role` pro `auth.uid()`).
Trigger `on_auth_user_created` → `public.handle_new_user()` vytvoří profil. Seed: demo + admin účet
(jen lokální `supabase db reset`), preset běh se nahraje skriptem `scripts/seed_supabase.py`.

## 4. Změny po souborech

| soubor | změna |
|---|---|
| `supabase/config.toml`, `supabase/migrations/*.sql`, `supabase/seed.sql` | schéma, RLS, storage bucket + policies, trigger, seed |
| `backend/app/config.py` | Supabase/auth/kvóty/CORS proměnné, `assert_safe_config()` – odmítne `AUTH_DISABLED=1` v produkci, ověří přítomnost klíčů |
| `backend/app/auth/` (nový) | `jwt.py` (JWKS verifier), `deps.py` (`current_user`, `require_admin`, `Principal`), `signup.py` (režimy registrace) |
| `backend/app/storage/meta.py` (nový) | `MetaStore` rozhraní + `LocalMetaStore` + `SupabaseMetaStore` (PostgREST přes httpx) |
| `backend/app/storage/blobs.py` (nový) | `BlobStore` + `LocalBlobStore` + `SupabaseBlobStore` (Storage REST, signed URLs) |
| `backend/app/storage/workspace.py` (nový) | pracovní kopie běhu: `ensure_local`, `sync_up`, LRU, gzip SQLite |
| `backend/app/storage/runs.py` | `run_path` míří do pracovní kopie; `list_runs` z MetaStore; vlastník z kontextu |
| `backend/app/jobs.py` | perzistentní joby (MetaStore), fáze, lease, `advance` (SSE), události, zrušení |
| `backend/app/job_handlers.py` (nový) | handlery fází pro každý druh jobu (plánování, běh, what-if, detektiv, observe, difficulty, dokumenty) |
| `backend/app/auth/admin.py` (nový) | Supabase Admin API: pozvánky, ban |
| `backend/app/pipeline.py` | `execute_run` rozdělen na fáze; stav fáze serializovatelný |
| `backend/app/quota.py` (nový) | kvóty, rate limit, záznam usage |
| `backend/app/main.py` | všechny endpointy za `Depends(current_user)`, kontrola vlastnictví (`_run_or_404(run_id, user)`), share router, admin router, `/api/me`, download tickety, CORS z konfigurace, generické chybové zprávy |
| `backend/tests/` | `test_auth_jwt.py`, `test_isolation.py`, `test_quota_signup_config.py`, `test_storage_jobs.py`, `test_rls.py` (SQL nad reálným Postgresem, skip bez něj), `test_migrate_script.py`, E2E `test_e2e_auth.py` |
| `frontend/src/auth/*` (nový) | `supabase.ts`, `AuthProvider` (session, refresh, odhlášení při vypršení), `RequireAuth` |
| `frontend/src/pages/AuthPages.tsx`, `AccountPages.tsx` | přihlášení, registrace, zapomenuté heslo, nastavení hesla, účet, správa uživatelů, sdílený běh – česky |
| `frontend/src/api.ts` | `Authorization` hlavička, 401 → odhlášení, `watchJob` přes `advance` (fetch-SSE), downloady, share režim |
| `vercel.json`, `.env.example`, `README.md`, `scripts/migrate_local_runs.py`, `scripts/seed_demo.py`, `backend/Dockerfile` | nasazení, konfigurace, migrace dat, demo preset, záložní nasazení |

## 5. Migrace existujících dat

`scripts/migrate_local_runs.py --owner-email ja@example.com [--preset RUN_ID …] [--dry-run]`:
projde `DATA_DIR/*/run.json`, každému běhu nastaví vlastníka (podle e-mailu přes Admin API), nahraje
soubory do Storage (`*.sqlite` gzip), vytvoří řádek `runs` a vybrané běhy označí jako preset.
Plánovací sezení (`_sessions`) se nemigrují (dočasná data). Nedokončené běhy (`status = running`)
se přeskočí. Skript je idempotentní (upsert, přeskočí soubory se stejným sha256).

## 6. Rizika a otevřené body

| riziko | dopad | opatření |
|---|---|---|
| Fáze `generate` s reálným Gemini > 300 s (viděno 494 s) | timeout na Hobby | fáze `llm_rows` je opakovatelná (cache odpovědí v `/tmp` + Storage), Pro → `maxDuration` 800 s, jinak fallback backend mimo Vercel (kap. 2.1/7) |
| `/tmp` na Vercelu je dočasný a omezený (velikost **[neověřeno]**, obvykle 500 MB) | opakované stahování 20 MB | LRU cache, `WORKSPACE_CACHE_MB=300`; soubory se stahují líně |
| Vercel Services jsou beta | změna konfigurace | fallback: samostatný backend; vše je jen v `vercel.json` |
| Velikost Python bundle (numpy, pandas, scipy) | build fail > 500 MB | `excludeFiles` (testy), dev závislosti v `requirements-dev.txt`; případně `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` |
| Free plán Supabase: 50 MB/soubor, 1 GB Storage | ~50 klasických běhů | gzip, admin přehled velikostí, mazání běhů |
| Lokálně nebylo možné spustit Supabase CLI ani Vercel CLI (chybí Docker/CLI) | migrace a deploy neověřené end-to-end proti skutečné službě | RLS testy proti reálnému Postgresu s shim schématem `auth`/`storage`; PostgREST/Storage klienti testovaní proti fake transportu; postup ověření v README |
| JWT zůstává platný do `exp` i po zablokování | blokovaný uživatel může do 1 h číst svá data přes Data API | backend kontroluje `profiles.status` při každém požadavku; Admin API ban |
| Pickle stavu generátoru | RCE, pokud by útočník zapsal do Storage | zápis do bucketu jen secret klíčem; testy storage policies |

Předpoklady (bez dotazu): Vercel Hobby plán; Supabase Free plán; jediný region; e-mailové potvrzení
registrace zapnuté (výchozí Supabase); Google OAuth a magic link jsou volitelné přes
`VITE_AUTH_GOOGLE=1` / `VITE_AUTH_MAGIC_LINK=1` (provider se zapíná v Supabase dashboardu).

## 7. Nasazení

- **Vercel**: `vercel.json` se `services` (frontend + backend), env proměnné pro Production / Preview /
  Development (`vercel env add`), `CORS_ORIGINS` + `CORS_ORIGIN_REGEX` pro `https://*-<team>.vercel.app`.
- **Supabase**: Site URL = produkční doména; Redirect URLs: `https://<prod>/**`,
  `https://*-<team>.vercel.app/**`, `http://localhost:5173/**`. Frontend posílá `redirectTo`
  (`window.location.origin + /auth/reset` atd.), e-mailové šablony mají používat `{{ .RedirectTo }}`.
- Migrace: `supabase link --project-ref <ref>` → `supabase db push`.

## 8. Fáze implementace

1. Analýza + tento plán. 2. Supabase schéma/RLS/Storage/seed + RLS testy. 3. Backend auth, vlastnictví,
MetaStore, kvóty, rate limit. 4. Storage + pracovní kopie, data browser/dashboard/what-if/downloady.
5. Joby po fázích (`advance`), pipeline rozdělená na fáze. 6. Frontend auth, chráněné routy, kvóty,
sdílení, admin. 7. Vercel, env, README, migrační skript, finální testy a bezpečnostní kontrola.

## 9. Ověřené zdroje (2026-11-28)

- Supabase: JWT signing keys (`/docs/guides/auth/signing-keys`), JWT claims (`/docs/guides/auth/jwt-fields`),
  API keys (`/docs/guides/api/api-keys` – `sb_publishable_…`/`sb_secret_…` v hlavičce `apikey`, legacy
  klíče deprecated „early 2027“), Storage limity (`/docs/guides/storage/uploads/file-limits` – Free 50 MB),
  Storage access control + helper funkce (`storage.foldername`), Redirect URLs (wildcardy `*`, `**`),
  changelog (2026-04-28: nové tabulky v `public` nejsou vystavené Data API bez GRANT, vynuceno 2026-10-30).
- Vercel: FastAPI framework (`/docs/frameworks/backend/fastapi` – entrypoint `app/main.py`, Python 3.12–3.14,
  bundle 500 MB, streaming odpovědí), Services (beta, `services` + rewrites na službu), limity (Hobby 300 s,
  Pro 800 s / 1800 s beta, 4,5 MB body mimo streaming), systémové proměnné (`VERCEL_ENV`,
  `VERCEL_URL`, `VERCEL_BRANCH_URL`, `VERCEL_PROJECT_PRODUCTION_URL`).

## 10. Stav implementace a ověření (2026-11-28)

| oblast | ověřeno | jak |
|---|---|---|
| Ověření JWT (ES256/RS256, exp, aud, iss, cizí podpis, `alg=none`, HS256 confusion, rotace JWKS) | ano | `tests/test_auth_jwt.py` |
| Každá neveřejná routa vrací 401 bez tokenu | ano | `tests/test_isolation.py` (enumerace `app.routes`) |
| Izolace uživatelů A/B na backendu (běhy, joby, sezení, SQL, stahování, sdílení, presety, what-if z presetu) | ano | `tests/test_isolation.py` |
| RLS + Storage policies (izolace A/B, zákaz zápisů, preset, admin) | ano, na Postgresu 16 se shim schématy `auth`/`storage` | `tests/test_rls.py` (`make test-rls`) – odhalilo a opravilo nejednoznačný sloupec v preset policy |
| Kvóty, rate limit, usage, režimy registrace, blokace účtu, limit velikosti požadavku | ano | `tests/test_quota_signup_config.py`, `test_isolation.py` |
| Dev bypass odmítnutý v produkci / preview | ano | `tests/test_quota_signup_config.py` (i start procesu) |
| Obnovitelné fáze jobu, synchronizace se Storage, nová instance mezi fázemi, pád uprostřed fáze, zrušení | ano (Storage simulován adresářem) | `tests/test_storage_jobs.py` |
| Tvar požadavků na PostgREST / Storage (klíče v hlavičkách, filtry, signed URL) | ano (mock transport) | `tests/test_storage_jobs.py` |
| E2E (registrace → plán → schválení → generování po fázích → report → what-if → sdílení → odhlášení) | ano na úrovni API (mock LLM, testovací tokeny) | `tests/test_e2e_auth.py` |
| Frontend: build, typecheck, render obrazovek (auth vypnutý i zapnutý), sdílený náhled | ano | `npm run build`, headless Chrome smoke test |
| `supabase start` / `db reset` / `db push`, `config.toml`, `seed.sql` proti skutečnému CLI | **ne** | chybí Docker a Supabase CLI |
| Supabase Auth v prohlížeči (registrace, potvrzení e-mailu, reset hesla, OAuth) | **ne** | vyžaduje Supabase projekt; ruční postup v README |
| Deploy na Vercel (Services beta, `functions` uvnitř služby, SPA rewrite, velikost Python bundle) | **ne** | chybí Vercel CLI/projekt |

Odchylky od původního plánu: stav účtu je jen `active`/`blocked` + příznak `approved` (místo `pending`);
režim registrace vynucuje backend z env konfigurace (DB hook „before user created“ nepoužit, pro `invite_only`
doporučeno vypnout registraci v dashboardu); lokálně (`JOB_AUTODRIVE=1`) fáze řídí background driver a
`/advance` jen sleduje průběh, na Vercelu (`JOB_AUTODRIVE=0`) je řídí klient; běh se statusem `running` se po
otevření stránky běhu sám obnoví.
