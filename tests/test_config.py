from pathlib import Path

from app_review_insights.config import Settings, load_settings


def test_settings_default_to_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(database_path=tmp_path / "runs.sqlite3")

    assert settings.deepseek_api_key == "test-key"
    assert settings.model_provider == "deepseek"
    assert settings.model_name == "deepseek-chat"
    assert settings.model_base_url == "https://api.deepseek.com"
    assert settings.model_enabled is True
    assert settings.database_path == Path(tmp_path / "runs.sqlite3")
    assert settings.default_review_limit == 500
    assert settings.batch_review_limit == 100


def test_settings_can_explicitly_disable_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ENABLED", "false")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-but-disabled")

    settings = Settings(database_path=tmp_path / "runs.sqlite3")

    assert settings.model_enabled is False
    assert settings.deepseek_api_key == "configured-but-disabled"


def test_load_settings_strips_notepad_bom_from_env_file(tmp_path, monkeypatch):
    from app_review_insights.config import load_settings

    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfDEEPSEEK_API_KEY=sk-bom-key-1234567890\nMODEL_PROVIDER=deepseek\n"
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.deepseek_api_key == "sk-bom-key-1234567890"
    # The BOM is stripped from the file so dotenv parsing is stable.
    assert env_file.read_bytes()[:3] != b"\xef\xbb\xbf"


def test_agent_and_webhook_settings_defaults(tmp_path, monkeypatch):
    # chdir 隔离：项目根 .env 可能含真实配置（如 SCHEDULER_ENABLED=true），
    # 仅 delenv 环境变量不够，需让 load_settings 读不到 .env 文件。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_TYPE", raising=False)
    monkeypatch.delenv("WEBHOOK_URLS", raising=False)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)
    settings = load_settings()
    assert settings.model_api_key == ""
    assert settings.webhook_type == ""
    assert settings.webhook_urls == []
    assert settings.scheduler_enabled is False
    assert settings.agent_max_review_rounds == 2
    assert settings.approval_required is False
    assert settings.agent_db_path.name == "agent.sqlite3"


def test_webhook_urls_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URLS", "https://a.example/hook,https://b.example/hook")
    settings = load_settings()
    assert settings.webhook_urls == [
        "https://a.example/hook",
        "https://b.example/hook",
    ]


def test_model_api_key_overrides_deepseek_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-custom")
    settings = load_settings()
    assert settings.effective_model_api_key == "sk-custom"


def test_webhook_urls_empty_string_parses_to_empty_list(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URLS", "")
    settings = load_settings()
    assert settings.webhook_urls == []


def test_effective_model_api_key_falls_back_to_deepseek_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    settings = load_settings()
    assert settings.effective_model_api_key == "sk-deepseek"


def test_model_available_reflects_key_and_enabled_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 隔离项目根目录下的本地 .env（gitignore，含真实 DEEPSEEK_API_KEY），
    # 否则 load_settings() 会从 .env 读到 key 导致本测试不可复现。
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.model_available is False


def test_demo_mode_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert load_settings().demo_mode == "auto"


def test_blank_record_path_becomes_none(monkeypatch):
    monkeypatch.setenv("MODEL_RECORD_PATH", "   ")
    assert load_settings().model_record_path is None


def test_demo_replay_active_follows_mode_and_key(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert load_settings().demo_replay_active is True

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert load_settings().demo_replay_active is False

    monkeypatch.setenv("DEMO_MODE", "replay")
    assert load_settings().demo_replay_active is True
