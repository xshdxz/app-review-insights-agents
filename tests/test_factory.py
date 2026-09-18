from datetime import UTC, datetime

import pytest

from app_review_insights.config import load_settings
from app_review_insights.errors import InputDataError
from app_review_insights.factory import build_agent_stack, build_pipeline_services
from app_review_insights.llm.recording import (
    RECORDING_MODE,
    RecordingDocument,
    write_recording,
)
from app_review_insights.models import Review
from app_review_insights.pipeline.orchestrator import PipelineServices
from app_review_insights.rag.answer import RagAnswerer


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


def test_build_agent_stack_rag_wired(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    stack = build_agent_stack(use_fake_provider=True)
    assert isinstance(stack.rag, RagAnswerer)


def test_index_run_cleaned(tmp_path, monkeypatch):
    from app_review_insights.factory import index_run_cleaned
    from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType, Stage
    from app_review_insights.storage.repository import RunRepository

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    stack = build_agent_stack(use_fake_provider=True)
    run_repo = RunRepository(load_settings().database_path)
    run_repo.save_run(
        RunRecord(
            run_id="r1",
            request=AnalysisRequest(
                source_type=SourceType.ONLINE,
                analysis_goal="goal",
                app_url="https://apps.apple.com/us/app/x/id1",
            ),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    run_repo.save_output(
        "r1",
        Stage.CLEAN,
        {
            "reviews": [
                Review(
                    review_id="v1",
                    app_id="x",
                    content_original="订阅太贵",
                    rating=1,
                    published_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source="fixture",
                ).model_dump(mode="json")
            ],
            "stats": {},
        },
    )
    count = index_run_cleaned(stack, "r1")
    assert count == 1
    assert stack.agent_repository.app_ids() == ["x"]


def test_index_run_cleaned_no_output_returns_zero(tmp_path, monkeypatch):
    from app_review_insights.factory import index_run_cleaned

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    stack = build_agent_stack(use_fake_provider=True)
    assert index_run_cleaned(stack, "no-such-run") == 0


def test_index_run_cleaned_with_embeddings(tmp_path, monkeypatch):
    from app_review_insights.factory import index_run_cleaned
    from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType, Stage
    from app_review_insights.storage.repository import RunRepository

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    stack = build_agent_stack(use_fake_provider=True)

    # 注入 fake embedding store（不真正调用 API）
    class _FakeEmbeddingStore:
        def embed_texts(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    stack.embedding_store = _FakeEmbeddingStore()
    run_repo = RunRepository(load_settings().database_path)
    run_repo.save_run(
        RunRecord(
            run_id="r1",
            request=AnalysisRequest(
                source_type=SourceType.ONLINE,
                analysis_goal="goal",
                app_url="https://apps.apple.com/us/app/x/id1",
            ),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    run_repo.save_output(
        "r1",
        Stage.CLEAN,
        {
            "reviews": [
                Review(
                    review_id="v1",
                    app_id="x",
                    content_original="订阅太贵",
                    rating=1,
                    published_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source="fixture",
                ).model_dump(mode="json")
            ],
            "stats": {},
        },
    )
    count = index_run_cleaned(stack, "r1")
    assert count == 1
    hits = stack.agent_repository.search_embeddings([1.0, 0.0, 0.0], app_ids=None, limit=10)
    assert hits and hits[0]["review_id"] == "v1"


def _empty_recording(path) -> None:
    write_recording(
        RecordingDocument(
            mode=RECORDING_MODE,
            is_live=False,
            recorded_at=datetime(2026, 9, 17, tzinfo=UTC),
            model="deepseek-chat",
            input_fingerprint="fp",
            entries=[],
        ),
        path,
    )


def test_auto_without_key_and_without_recording_yields_no_analyzer(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert build_pipeline_services(load_settings()).batch_analyzer is None


def test_replay_mode_without_recording_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "missing.json"))
    # 断言的是 factory 自己抛的那条消息；load_recording 的「录制文件不存在」
    # 在此路径上不可达（factory 先做了 .exists() 检查）
    with pytest.raises(InputDataError, match="需要录制文件"):
        build_pipeline_services(load_settings())


def test_live_mode_without_key_raises(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(InputDataError, match="需要可用的模型密钥"):
        build_pipeline_services(load_settings())


def test_replay_mode_wires_the_replay_provider(tmp_path, monkeypatch):
    path = tmp_path / "rec.json"
    _empty_recording(path)
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(path))

    services = build_pipeline_services(load_settings())

    assert services.batch_analyzer is not None
    assert services.consolidator is not None
    assert services.evidence_auditor is not None


def test_recording_requires_input_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("MODEL_RECORD_PATH", str(tmp_path / "rec.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    with pytest.raises(InputDataError, match="输入指纹"):
        build_pipeline_services(load_settings())
