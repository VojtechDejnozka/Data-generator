.PHONY: setup dev mock test test-rls seed seed-mock build supabase-start supabase-reset migrate-runs

setup:            ## install backend (uv/venv) and frontend deps
	cd backend && (command -v uv >/dev/null && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt \
	  || (python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt))
	cd frontend && npm install
	@[ -f .env ] || cp .env.example .env

dev:              ## run backend + frontend (real OpenAI/Gemini if a key is in .env)
	scripts/dev.sh

mock:             ## run backend + frontend with fixture LLM responses (no key needed)
	LLM_MOCK=1 scripts/dev.sh

test:             ## backend unit + integration tests (mock mode) and frontend typecheck
	cd backend && .venv/bin/python -m pytest -q tests
	cd frontend && npx tsc -b

test-rls:         ## RLS/Storage policy tests on a throwaway Postgres (PG_BIN=/path/to/pg/bin or initdb on PATH)
	cd backend && .venv/bin/python -m pytest -q tests/test_rls.py -rs

supabase-start:   ## local Supabase stack (Docker + Supabase CLI) – prints URL and keys for .env
	supabase start

supabase-reset:   ## re-apply supabase/migrations + supabase/seed.sql to the local stack
	supabase db reset

migrate-runs:     ## import legacy local runs into Supabase: make migrate-runs OWNER=admin@example.com
	cd backend && .venv/bin/python ../scripts/migrate_local_runs.py --owner-email $(OWNER)

seed:             ## pre-generate demo runs (story, baseline, severe)
	cd backend && .venv/bin/python ../scripts/seed_demo.py

seed-mock:
	cd backend && .venv/bin/python ../scripts/seed_demo.py --mock

build:
	cd frontend && npm run build
