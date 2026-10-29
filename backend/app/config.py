from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Connection parameters are kept separate and handed to asyncpg as-is, never
    # interpolated into a URL: passwords containing @ # / ? : would corrupt a URL.
    postgres_host: str = "crypttube-db"
    postgres_port: int = 5432
    postgres_user: str = "goon"
    postgres_password: str = ""
    postgres_db: str = "goon"
    # Optional full DSN override (tools, unusual setups). Takes precedence when set.
    database_url: str | None = None
    generated_dir: Path = Path("/data/generated")
    media_dir: Path = Path("/media")
    uploads_dir: Path = Path("/uploads")  # user libraries (a Docker volume), one folder per library
    # Encryption of user uploads: a file holding the key (a Docker secret). Missing or
    # empty = off. See README, "Encrypting user uploads".
    uploads_key_file: str | None = "/run/secrets/uploads_key"
    run_migrations: bool = True
    db_pool_min: int = 2
    db_pool_max: int = 20
    # ingestion
    scan_settle_seconds: float = 10
    scan_probe_concurrency: int = 4   # files read/probed at once during a scan
    watcher_polling: bool = False
    watcher_quiet_seconds: float = 5
    watcher_max_wait_seconds: float = 60
    # auth: set true when served over HTTPS (reverse proxy with TLS)
    cookie_secure: bool = False
    # media delivery
    # Allow "set image from URL" (the app's only outbound request, made on explicit request).
    url_image_fetch: bool = True
    max_transcodes: int = 2
    # GPU encoder for live transcodes (README: "Hardware transcoding"); set by the
    # compose.vaapi.yaml / compose.nvidia.yaml overrides. Falls back to the CPU if it can't start.
    hw_accel: Literal["none", "vaapi", "nvenc"] = "none"
    hw_device: str = "/dev/dri/renderD128"   # VAAPI render node (another one with several GPUs)
    generate_concurrency: int = 2   # scenes one generate job works on at once
    generate_jobs: int = 1          # generate jobs at once (default; admins can change it in the UI)

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
