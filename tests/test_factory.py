from app_review_insights.config import load_settings
from app_review_insights.factory import build_pipeline_services
from app_review_insights.pipeline.orchestrator import PipelineServices


def test_build_pipeline_services_fake_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    services = build_pipeline_services(load_settings(), use_fake_provider=True)
    assert isinstance(services, PipelineServices)
    assert services.batch_analyzer is None
    assert services.collector is not None
    assert services.finding_validator is not None
    assert services.traceability_validator is not None


def test_build_pipeline_services_live_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("MODEL_API_KEY", "sk-test")
    services = build_pipeline_services(load_settings(), use_fake_provider=False)
    assert services.batch_analyzer is not None
    assert services.consolidator is not None
