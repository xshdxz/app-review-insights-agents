from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack, build_pipeline_services
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


def test_build_agent_stack_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    stack = build_agent_stack(use_fake_provider=True)
    assert stack.registry.names()
    assert stack.planner is not None
    assert stack.reviewer is not None
    assert stack.orchestrator is not None
    assert stack.webhook is not None


def test_build_agent_stack_default_plan_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    # 本机 .env 存在真实 DEEPSEEK_API_KEY，仅删环境变量仍会被 .env 兜底加载，
    # 导致 planner 走真实模型调用；显式关闭模型保证该用例确定且离线。
    monkeypatch.setenv("MODEL_ENABLED", "false")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    stack = build_agent_stack()
    plan = stack.planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"
