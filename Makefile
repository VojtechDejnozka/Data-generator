.PHONY: setup dev mock test seed seed-mock build

setup:            ## install backend (uv/venv) and frontend deps
	cd backend && (command -v uv >/dev/null && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt \
	  || (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt))
	cd frontend && npm install
	@[ -f .env ] || cp .env.example .env

dev:              ## run backend + frontend (real Gemini if key in .env)
	scripts/dev.sh

mock:             ## run backend + frontend with fixture LLM responses (no key needed)
	LLM_MOCK=1 scripts/dev.sh

test:             ## backend unit + integration tests (mock mode) and frontend typecheck
	cd backend && .venv/bin/python -m pytest -q tests
	cd frontend && npx tsc -b

seed:             ## pre-generate demo runs (story, baseline, severe)
	cd backend && .venv/bin/python ../scripts/seed_demo.py

seed-mock:
	cd backend && .venv/bin/python ../scripts/seed_demo.py --mock

build:
	cd frontend && npm run build
