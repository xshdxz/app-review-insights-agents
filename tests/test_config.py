from pathlib import Path

from app_review_insights.config import Settings


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
