from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    generated_dir: Path = Path("/data/generated")
    media_dir: Path = Path("/media")
    run_migrations: bool = True
    db_pool_min: int = 2
    db_pool_max: int = 20
    # ingestion
    scan_settle_seconds: float = 10
    scan_probe_concurrency: int = 4
    watcher_polling: bool = False
    watcher_quiet_seconds: float = 5
    watcher_max_wait_seconds: float = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
