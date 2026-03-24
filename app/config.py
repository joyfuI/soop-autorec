from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_ROOT_DIR = Path("./data")
DB_PATH = str(DATA_ROOT_DIR / "app.db")
OUTPUT_ROOT_DIR = str(DATA_ROOT_DIR / "recordings")
TEMP_ROOT_DIR = str(DATA_ROOT_DIR / "tmp")
COOKIES_DIR = str(DATA_ROOT_DIR / "cookies")
STREAMLINK_BINARY = "streamlink"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = Field(default="Asia/Seoul", validation_alias="TZ")

    poll_interval_sec: int = Field(default=10, ge=1)
    offline_confirm_count: int = Field(default=3, ge=1)

    ffmpeg_binary: str = "ffmpeg"

    app_secret_key: str | None = None

    @property
    def db_path(self) -> str:
        return DB_PATH

    @property
    def output_root_dir(self) -> str:
        return OUTPUT_ROOT_DIR

    @property
    def temp_root_dir(self) -> str:
        return TEMP_ROOT_DIR

    @property
    def cookies_dir(self) -> str:
        return COOKIES_DIR

    @property
    def streamlink_binary(self) -> str:
        return STREAMLINK_BINARY

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
