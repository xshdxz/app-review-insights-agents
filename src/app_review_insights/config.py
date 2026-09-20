from pathlib import Path
from typing import Annotated, Any, Literal

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
    # 响应缓存：同一次实验重复跑时省成本。键含模型/温度/输出上限/Schema/请求文本，
    # 任何一项变了都不会命中——所以它是安全的，不是"把上一次的答案还给你"。
    model_cache_enabled: bool = Field(default=True, alias="MODEL_CACHE_ENABLED")
    model_cache_path: Path = Field(
        default=Path("data/cache/llm-cache.sqlite3"), alias="MODEL_CACHE_PATH"
    )
    model_cache_ttl_days: int = Field(default=7, alias="MODEL_CACHE_TTL_DAYS")
    # 预算熔断：0 表示不限制。超限时流水线停在检查点，可调高后续跑。
    model_budget_usd_per_run: float = Field(default=0.0, alias="MODEL_BUDGET_USD_PER_RUN")
    model_budget_usd_per_day: float = Field(default=0.0, alias="MODEL_BUDGET_USD_PER_DAY")
    # 演示模式：auto=有密钥用真实模型、无密钥回放录制；live=强制真实模型；replay=强制回放
    demo_mode: Literal["auto", "live", "replay"] = Field(default="auto", alias="DEMO_MODE")
    # 录制输出路径（留空 = 不录制；仅供 scripts/record_demo.py 使用）
    model_record_path: Path | None = Field(default=None, alias="MODEL_RECORD_PATH")
    # 回放读取的录制文件
    demo_replay_path: Path = Field(
        default=Path("data/recordings/demo-replay.json"), alias="DEMO_REPLAY_PATH"
    )
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

    @field_validator("model_record_path", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value: Any) -> Any:
        """空字符串等同于未配置——.env 里留空是最常见的写法。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @property
    def effective_model_api_key(self) -> str:
        return self.model_api_key or self.deepseek_api_key

    @property
    def model_available(self) -> bool:
        return self.model_enabled and bool(self.effective_model_api_key)

    @property
    def lease_timeout_seconds(self) -> float:
        """运行租约的兜底超时（秒）。

        只在**判不出持有者进程是否存活**时才用得上（跨主机、或没有租约信息的旧记录）。
        下界取"单次模型调用最坏耗时"的 2 倍：正在跑的批次绝不能因为心跳一时没刷新
        就被判成孤儿，否则会出现两个执行者同时写同一份检查点。

        刻意做成派生值而不是新环境变量：单机部署没有调参场景，而新增配置要同步
        config/.env.example/README 三处，多一个漂移点。
        """
        worst_call = self.model_timeout_seconds * (self.model_max_retries + 1)
        return max(300.0, worst_call * 2)

    @property
    def demo_replay_active(self) -> bool:
        """是否以回放方式运行。

        auto 且无密钥时走回放——这是无密钥部署的默认形态；
        replay 则无视密钥强制回放，供现场演示避免网络与模型波动。
        """
        if self.demo_mode == "replay":
            return True
        return self.demo_mode == "auto" and not self.model_available


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
