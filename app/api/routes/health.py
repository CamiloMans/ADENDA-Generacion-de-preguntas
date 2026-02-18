from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def health_ready() -> JSONResponse:
    settings = get_settings()
    checks: dict[str, str] = {"database": "ok", "redis": "ok"}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "error"

    try:
        r = Redis.from_url(settings.redis_url)
        if not r.ping():
            checks["redis"] = "error"
    except Exception:
        checks["redis"] = "error"

    if "error" in checks.values():
        return JSONResponse(status_code=503, content={"status": "error", **checks})
    return JSONResponse(status_code=200, content={"status": "ok", **checks})
