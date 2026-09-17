from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    model_api_key: str = Field(default="", alias="MODEL_API_KEY")
    # 预算熔断：0 表示不限制。超限时流水线停在检查点，可调高后续跑。
    model_budget_usd_per_run: float = Field(default=0.0, alias="MODEL_BUDGET_USD_PER_RUN")
    model_budget_usd_per_day: float = Field(default=0.0, alias="MODEL_BUDGET_USD_PER_DAY")
    database_path: Path = Field(default=Path("data/runs/runs.sqlite3"), alias="DATABASE_PATH")
    default_review_limit: int = Field(default=500, alias="DEFAULT_REVIEW_LIMIT")
    batch_review_limit: int = Field(default=100, alias="BATCH_REVIEW_LIMIT")
    batch_max_characters: int = Field(default=60000, alias="BATCH_MAX_CHARACTERS")
    agent_db_path: Path = Field(default=Path("data/agent/agent.sqlite3"), alias="AGENT_DB_PATH")
    webhook_type: str = Field(default="", alias="WEBHOOK_TYPE")
    # NoDecode 保留原始 CSV 字符串：pydantic-settings 默认会对复杂类型做 JSON
    # 解码，空值或逗号分隔的 WEBHOOK_URLS 会抛 SettingsError 导致启动失败；
    # 配合下方 mode="before" 校验器手工按逗号拆分。
    webhook_urls: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="WEBHOOK_URLS")
    scheduler_enabled: bool = Field(default=False, alias="SCHEDULER_ENABLED")
    # 日志：LOG_FORMAT=json 时输出 JSON Lines（带 run_id 关联 ID），便于采集器索引
    # 整轮运行的墙钟上限（秒；0 = 不限制）。单次调用超时之外的第二道保险。
    run_max_duration_seconds: float = Field(default=0, alias="RUN_MAX_DURATION_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="text", alias="LOG_FORMAT")
    # 数据保留策略：events 每阶段写一条、报告每次监控新增一份，不清理只涨不跌。
    retention_days: int = Field(default=90, alias="RETENTION_DAYS")
    events_keep_per_run: int = Field(default=50, alias="EVENTS_KEEP_PER_RUN")
    reports_keep_per_app: int = Field(default=20, alias="REPORTS_KEEP_PER_APP")
    maintenance_enabled: bool = Field(default=True, alias="MAINTENANCE_ENABLED")
    maintenance_interval_seconds: float = Field(default=86400, alias="MAINTENANCE_INTERVAL_SECONDS")
    worker_health_host: str = Field(default="0.0.0.0", alias="WORKER_HEALTH_HOST")
    worker_health_port: int = Field(default=9100, alias="WORKER_HEALTH_PORT")
    agent_max_review_rounds: int = Field(default=2, alias="AGENT_MAX_REVIEW_ROUNDS")
    approval_required: bool = Field(default=False, alias="APPROVAL_REQUIRED")
    embedding_enabled: bool = Field(default=False, alias="EMBEDDING_ENABLED")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_base_url: str = Field(default="https://api.openai.com/v1", alias="EMBEDDING_BASE_URL")
    embedding_api_key: str = Field(default="", alias="EMBEDDING_API_KEY")
    embedding_local_model_path: str = Field(default="", alias="EMBEDDING_LOCAL_MODEL_PATH")
    social_x_endpoint: str = Field(default="", alias="SOCIAL_X_ENDPOINT")

    @field_validator("webhook_urls", mode="before")
    @classmethod
    def _split_webhook_urls(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def effective_model_api_key(self) -> str:
        return self.model_api_key or self.deepseek_api_key

    @property
    def model_available(self) -> bool:
        return self.model_enabled and bool(self.effective_model_api_key)


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
