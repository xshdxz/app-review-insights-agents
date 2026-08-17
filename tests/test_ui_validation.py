"""Unit tests for the one-shot model key validation."""

from app_review_insights.config import Settings
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm import provider as provider_module
from app_review_insights.ui.main import _validate_model_key


class FakeProvider:
    def __init__(self, error=None):
        self.error = error

    def generate(self, system_prompt, user_prompt, schema):
        if self.error is not None:
            raise self.error
        return schema.model_validate({"findings": [], "batch_limitations": []})


def test_key_validation_classifies_auth_failure_as_invalid(monkeypatch):
    monkeypatch.setattr(
        provider_module.DeepSeekProvider,
        "from_settings",
        staticmethod(
            lambda settings: FakeProvider(
                RecoverableModelError(
                    "Error code: 401 - Authentication Fails, Your api key is invalid"
                )
            )
        ),
    )

    result = _validate_model_key(Settings(deepseek_api_key="sk-x"), {})

    assert result == "invalid"


def test_key_validation_classifies_network_failure_as_unavailable(monkeypatch):
    monkeypatch.setattr(
        provider_module.DeepSeekProvider,
        "from_settings",
        staticmethod(lambda settings: FakeProvider(RecoverableModelError("Connection timed out"))),
    )

    result = _validate_model_key(Settings(deepseek_api_key="sk-x"), {})

    assert result == "unavailable"


def test_key_validation_returns_valid_on_success(monkeypatch):
    monkeypatch.setattr(
        provider_module.DeepSeekProvider,
        "from_settings",
        staticmethod(lambda settings: FakeProvider()),
    )

    result = _validate_model_key(Settings(deepseek_api_key="sk-x"), {})

    assert result == "valid"


def test_key_validation_caches_result_per_session(monkeypatch):
    calls = {"count": 0}

    class CountingProvider(FakeProvider):
        def generate(self, system_prompt, user_prompt, schema):
            calls["count"] += 1
            return super().generate(system_prompt, user_prompt, schema)

    monkeypatch.setattr(
        provider_module.DeepSeekProvider,
        "from_settings",
        staticmethod(lambda settings: CountingProvider()),
    )
    state: dict = {}

    assert _validate_model_key(Settings(deepseek_api_key="sk-x"), state) == "valid"
    assert _validate_model_key(Settings(deepseek_api_key="sk-x"), state) == "valid"

    assert calls["count"] == 1


def test_key_validation_skipped_without_key():
    result = _validate_model_key(Settings(deepseek_api_key=""), {})

    assert result is None
