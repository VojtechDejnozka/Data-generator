# Fallback deployment of the API outside Vercel (Fly.io / Render / Cloud Run), see docs/AUTH_PLAN.md §2.1/7.
# The frontend stays on Vercel with VITE_API_BASE_URL pointing here; set CORS_ORIGINS accordingly.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV APP_ENV=production JOB_AUTODRIVE=1 WORKSPACE_DIR=/tmp/story2data/runs DATA_DIR=/tmp/story2data/data \
    CACHE_DIR=/tmp/story2data/llm-cache PORT=8000
USER nobody
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
