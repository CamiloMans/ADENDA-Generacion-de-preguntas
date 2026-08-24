# ICSARA API

FastAPI backend to process ICSARA PDFs asynchronously:

1. Upload PDF + `id_adenda` -> create job.
2. Worker parses ICSARA observations, classifies them, and runs a Claude review against the original PDF.
3. Worker uploads the reviewed JSON and audit artifacts to Google Drive.
4. Poll job status and download the reviewed JSON artifact.

## Features

- `POST /v1/jobs` (PDF upload)
- `GET /v1/jobs/{job_id}` (status/progress)
- `GET /v1/jobs/{job_id}/result` (artifact links + summary)
- `GET /v1/jobs/{job_id}/result/preguntas_clasificadas.json` (legacy direct file alias to the reviewed JSON)
- `GET /v1/jobs/{job_id}/artifacts/{filename}` (download)
- `DELETE /v1/jobs/{job_id}`
- API key auth via `X-API-Key`
- Redis queue + Celery worker
- PostgreSQL state persistence (Supabase compatible)
- Google Drive artifact storage for reviewed JSON, audit report, markdown report, and media
- Local disk used only as temporary staging during processing

## Required environment

Copy `.env.example` to `.env` and set:

- `API_KEYS`
- `DATABASE_URL` (Supabase/PostgreSQL URL)
- `REDIS_URL`
- `DATA_DIR`
- `GOOGLE_CLIENT_SECRET_FILE`
- `GOOGLE_TOKEN_FILE`
- `GOOGLE_DRIVE_PARENT_FOLDER_ID`
- `ANTHROPIC_ADENDA_VALIDACION_ICSARA_API_KEY`
- `ANTHROPIC_API_KEY` (legacy fallback)
- `ANTHROPIC_MODEL`
- `ANTHROPIC_PDF_DPI`
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

## Request contract

`POST /v1/jobs` uses `multipart/form-data` with:

- `file` (PDF)
- `id_adenda` (integer, required)
- `classify=true`
- `include_png=true`

## Job artifact names

- `{pdf_stem}_revisado.json`
- `auditoria_cambios.json`
- `verificacion_icsara.md`

## Cleanup expired jobs

```bash
python scripts/cleanup_expired_jobs.py
```
