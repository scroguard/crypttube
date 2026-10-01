from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Connection parameters are kept separate and handed to asyncpg as-is, never
    # interpolated into a URL: passwords containing @ # / ? : would corrupt a URL.
    postgres_host: str = "goonstreamer-db"
    postgres_port: int = 5432
    postgres_user: str = "goon"
    postgres_password: str = ""
    postgres_db: str = "goon"
    # Optional full DSN override (tools, unusual setups). Takes precedence when set.
    database_url: str | None = None
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
    # auth: set true when served over HTTPS (reverse proxy with TLS)
    cookie_secure: bool = False
    # media delivery
    max_transcodes: int = 2
    generate_concurrency: int = 2

    def db_params(self, database: str | None = None) -> dict:
        """Keyword arguments for asyncpg.connect / create_pool; `database` overrides the db name."""
        if self.database_url:
            params: dict = {"dsn": self.database_url}
        else:
            params = {"host": self.postgres_host, "port": self.postgres_port, "user": self.postgres_user,
                      "password": self.postgres_password, "database": self.postgres_db}
        if database:
            params["database"] = database
        return params


@lru_cache
def get_settings() -> Settings:
    return Settings()
