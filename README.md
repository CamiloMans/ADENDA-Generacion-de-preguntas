# ICSARA API

FastAPI backend to process ICSARA PDFs asynchronously:

1. Upload PDF -> create job.
2. Worker extracts questions/tables/figures.
3. Optional classification by taxonomy.
4. Poll job status and download artifacts.

## Features

- `POST /v1/jobs` (PDF upload)
- `GET /v1/jobs/{job_id}` (status/progress)
- `GET /v1/jobs/{job_id}/result` (artifact links + summary)
- `GET /v1/jobs/{job_id}/result/preguntas_clasificadas.json` (direct file response)
- `GET /v1/jobs/{job_id}/artifacts/{filename}` (download)
- `DELETE /v1/jobs/{job_id}`
- API key auth via `X-API-Key`
- Redis queue + Celery worker
- PostgreSQL state persistence (Supabase compatible)
- Local disk artifact storage with TTL cleanup

## Required environment

Copy `.env.example` to `.env` and set:

- `API_KEYS`
- `DATABASE_URL` (Supabase/PostgreSQL URL)
- `REDIS_URL`
- `DATA_DIR`
- `CORS_ALLOW_ALL` (`true` for temporary wildcard CORS, otherwise use `CORS_ORIGINS`)

## Run locally (without Docker)

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Worker:

```bash
celery -A app.tasks.celery_app:celery_app worker --loglevel=INFO --concurrency=2
```

## Run with Docker Compose

```bash
docker compose up -d --build
```

## Run production profile (VM, port 8080)

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Deployment runbook:

- `deploy/vm/README.md`
- `deploy/vm/deploy.sh`
- `deploy/systemd/icsara-stack.service`

Optional Nginx reverse proxy:

```bash
docker compose -f docker-compose.yml -f deploy/nginx/docker-compose.nginx.yml up -d --build
```

## Job artifact names

- `preguntas.json`
- `preguntas.txt`
- `chapters_hinges.json`
- `texto_total.txt`
- `preguntas_clasificadas.json`
- `preguntas_clasificadas_detalle.json`
- `outputs_png.zip`

## Cleanup expired jobs

```bash
python scripts/cleanup_expired_jobs.py
```
