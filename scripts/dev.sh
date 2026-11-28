#!/usr/bin/env bash
# Start backend (FastAPI :8000) and frontend (Vite :5173) together. Ctrl+C stops both.
# Usage: scripts/dev.sh            # uses .env (real OpenAI if OPENAI_API_KEY is set)
#        LLM_MOCK=1 scripts/dev.sh # offline mock mode
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -x backend/.venv/bin/python ] || { echo "Run 'make setup' first."; exit 1; }
[ -d frontend/node_modules ] || { echo "Run 'make setup' first."; exit 1; }

# Local dev without Supabase: no accounts (everything belongs to a local dev user).
if [ -z "${SUPABASE_URL:-}" ] && ! grep -qE '^SUPABASE_URL=.+' .env 2>/dev/null; then
  export AUTH_DISABLED="${AUTH_DISABLED:-1}"
  echo "SUPABASE_URL not set -> AUTH_DISABLED=$AUTH_DISABLED (local dev mode, no login)"
fi

(cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app) &
BACK=$!
(cd frontend && exec npm run dev -- --host 127.0.0.1) &
FRONT=$!
trap 'kill $BACK $FRONT 2>/dev/null || true' INT TERM EXIT
echo "Backend  http://127.0.0.1:8000/api/health"
echo "Frontend http://127.0.0.1:5173"
wait
