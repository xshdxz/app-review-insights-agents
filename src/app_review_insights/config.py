from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    model_enabled: bool = Field(default=True, alias="MODEL_ENABLED")
    model_provider: str = Field(default="deepseek", alias="MODEL_PROVIDER")
    model_name: str = Field(default="deepseek-chat", alias="MODEL_NAME")
    model_base_url: str = Field(default="https://api.deepseek.com", alias="MODEL_BASE_URL")
    model_timeout_seconds: float = Field(default=60, alias="MODEL_TIMEOUT_SECONDS")
    model_max_retries: int = Field(default=2, alias="MODEL_MAX_RETRIES")
    model_max_tokens: int = Field(default=8192, alias="MODEL_MAX_TOKENS")
    database_path: Path = Field(default=Path("data/runs/runs.sqlite3"), alias="DATABASE_PATH")
    default_review_limit: int = Field(default=500, alias="DEFAULT_REVIEW_LIMIT")
    batch_review_limit: int = Field(default=100, alias="BATCH_REVIEW_LIMIT")
    batch_max_characters: int = Field(default=60000, alias="BATCH_MAX_CHARACTERS")


_ENV_FILE = Path(".env")
_UTF8_BOM = b"\xef\xbb\xbf"


def _strip_env_bom() -> None:
    """Windows Notepad saves UTF-8 with a BOM, which breaks dotenv parsing.

    Silently strip the BOM from .env before loading so the first key
    (DEEPSEEK_API_KEY) is parsed correctly.
    """
    if not _ENV_FILE.exists():
        return
    raw = _ENV_FILE.read_bytes()
    if raw.startswith(_UTF8_BOM):
        _ENV_FILE.write_bytes(raw[len(_UTF8_BOM) :])


def load_settings() -> Settings:
    _strip_env_bom()
    return Settings()
