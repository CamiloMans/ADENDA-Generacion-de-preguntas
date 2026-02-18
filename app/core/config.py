from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ICSARA API"
    app_env: str = "dev"
    api_keys: str = "change-this-key"
    database_url: str = "sqlite:///./icsara.db"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("data/jobs")
    max_pdf_mb: int = 50
    job_ttl_days: int = 7
    celery_concurrency: int = 2
    log_level: str = "INFO"
    cors_origins: str = ""
    cors_allow_all: bool = False

    @property
    def api_key_set(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_pdf_bytes(self) -> int:
        return self.max_pdf_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
