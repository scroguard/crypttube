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


@lru_cache
def get_settings() -> Settings:
    return Settings()
