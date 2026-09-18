import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def _no_live_key_validation(monkeypatch):
    """Keep AppTest runs offline: fake the one-shot key validation as valid."""
    import app_review_insights.ui.main as ui_main

    monkeypatch.setattr(
        ui_main,
        "_validate_model_key",
        lambda settings, session_state=None: "valid",
    )


def test_build_services_uses_configured_database_and_fake_provider(tmp_path, monkeypatch):
    from app_review_insights.ui.main import build_services

    database_path = tmp_path / "configured" / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))

    services = build_services(use_fake_provider=True)

    assert services.repository.path == database_path
    assert services.repository.path.exists()
    assert services.batch_analyzer is None


def test_build_services_without_key_keeps_repository_available(tmp_path, monkeypatch):
    from app_review_insights.ui.main import build_services

    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    # 隔离录制文件：默认路径上一旦有录制，无密钥也会装配出回放 provider，
    # 这条用例要验的是「无密钥且无录制时没有模型」，不能依赖磁盘上有没有那个文件
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "no-recording.json"))

    services = build_services()

    assert services.repository.path == database_path
    assert services.repository.path.exists()
    assert services.batch_analyzer is None


def test_model_ready_honors_model_api_key_only(tmp_path, monkeypatch):
    """只配 MODEL_API_KEY（无 DEEPSEEK_API_KEY）时模型应视为可用。"""
    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import load_settings

    monkeypatch.chdir(tmp_path)  # 隔离项目根 .env
    monkeypatch.setenv("MODEL_API_KEY", "sk-custom-only")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_ENABLED", "true")

    settings = load_settings()
    state = ui_main._model_state(settings)
    assert state != "missing"
    assert settings.model_available is True


def test_model_ready_missing_both_keys(tmp_path, monkeypatch):
    """双 key 都为空时模型应标记为缺失。"""
    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import load_settings

    monkeypatch.chdir(tmp_path)  # 隔离项目根 .env
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_ENABLED", "true")

    settings = load_settings()
    state = ui_main._model_state(settings)
    assert state == "missing"
    assert settings.model_available is False


def test_build_services_with_key_wires_existing_deepseek_pipeline(tmp_path, monkeypatch):
    import app_review_insights.factory as factory
    import app_review_insights.ui.main as ui_main
    from app_review_insights.collectors import AppStoreCollector

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    provider = object()
    provider_factory = Mock(return_value=provider)
    analyze = Mock(return_value="batch-result")
    consolidate = Mock(return_value="consolidated-result")
    audit = Mock(return_value="audit-result")
    plan = Mock(return_value="requirements")
    generate_tests = Mock(return_value="test-cases")
    monkeypatch.setattr(
        factory.DeepSeekProvider,
        "from_settings",
        staticmethod(provider_factory),
    )
    monkeypatch.setattr(factory, "analyze_batch", analyze)
    monkeypatch.setattr(factory, "consolidate_findings", consolidate)
    monkeypatch.setattr(factory, "audit_finding_evidence", audit)
    monkeypatch.setattr(factory, "build_requirements", plan)
    monkeypatch.setattr(factory, "generate_test_cases", generate_tests)

    services = ui_main.build_services()

    assert isinstance(services.collector, AppStoreCollector)
    assert services.finding_validator is factory.validate_finding_drafts
    assert services.traceability_validator is factory.validate_traceability
    assert services.batch_analyzer(["review"], "goal") == "batch-result"
    assert services.consolidator(["batch-result"], "goal", ["review"]) == "consolidated-result"
    assert services.evidence_auditor(["finding"], ["review"], "goal") == "audit-result"
    assert services.requirement_builder(["finding"], "goal", 12) == "requirements"
    assert services.test_case_builder(["requirement"]) == "test-cases"
    provider_factory.assert_called_once()
    analyze.assert_called_once_with(provider, ["review"], "goal")
    consolidate.assert_called_once_with(provider, ["batch-result"], "goal", ["review"])
    audit.assert_called_once_with(provider, ["finding"], ["review"], "goal")
    plan.assert_called_once_with(provider, ["finding"], "goal", 12)
    generate_tests.assert_called_once_with(provider, ["requirement"])


def test_streamlit_page_starts_without_model_key(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert not app.exception
    assert any("证据审阅工作台" in title.value for title in app.title)
    start_button = next(button for button in app.button if button.label == "开始分析")
    # 计划书规定：未配置密钥时按钮禁用（所有模式）。
    assert start_button.disabled is True
    review_limit = next(slider for slider in app.slider if slider.label == "评论数量")
    assert review_limit.min == 100
    assert review_limit.max == 1000
    assert review_limit.step == 1


def test_input_mode_switches_to_mode_specific_fields(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    online = AppTest.from_file(str(app_path)).run(timeout=30)
    source = next(item for item in online.segmented_control if item.label == "数据来源")
    online_limit = next(item for item in online.slider if item.label == "评论数量")

    assert [item.label for item in online.text_input] == ["App 地址（URL）"]
    assert [item.label for item in online.file_uploader] == []
    assert online_limit.max == 1000
    assert online_limit.step == 1

    json_mode = source.set_value("JSON 导入").run(timeout=30)
    json_limit = next(item for item in json_mode.slider if item.label == "评论数量")

    assert [item.label for item in json_mode.text_input] == []
    assert [item.label for item in json_mode.file_uploader] == ["JSON 评论文件"]
    assert json_limit.max == 1000
    assert json_limit.step == 1

    csv_source = next(item for item in json_mode.segmented_control if item.label == "数据来源")
    csv_mode = csv_source.set_value("CSV 导入").run(timeout=30)

    assert [item.label for item in csv_mode.text_input] == []
    assert [item.label for item in csv_mode.file_uploader] == ["CSV 评论文件"]


def test_input_form_keeps_values_across_reruns(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)
    url_input = next(item for item in app.text_input if item.label == "App 地址（URL）")
    url_input.set_value("https://apps.apple.com/us/app/example/id123456789")
    goal = next(item for item in app.text_area if item.label == "分析目标")
    goal.set_value("自定义分析目标")
    app = app.run(timeout=30)

    assert (
        next(item for item in app.text_input if item.label == "App 地址（URL）").value
        == "https://apps.apple.com/us/app/example/id123456789"
    )
    assert (
        next(item for item in app.text_area if item.label == "分析目标").value == "自定义分析目标"
    )


def test_explicit_model_disable_overrides_configured_key(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("MODEL_ENABLED", "false")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-but-disabled")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)
    start_button = next(button for button in app.button if button.label == "开始分析")

    assert start_button.disabled is True
    assert any("已显式禁用" in item.value for item in app.warning)


def test_model_status_banner_hidden_when_configured(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert not any("模型状态" in item.value for item in app.success)
    assert not any("模型状态" in item.value for item in app.info)


def test_model_status_banner_hidden_when_verified(tmp_path, monkeypatch):
    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import Settings

    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)
    settings = Settings(deepseek_api_key="test-key")
    app.session_state["model_verified_fingerprint"] = ui_main._model_config_fingerprint(settings)
    app = app.run(timeout=30)

    assert not any("模型状态" in item.value for item in app.success)
    assert not any("模型状态" in item.value for item in app.info)


def test_model_status_banner_warns_when_key_missing(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert any("未配置" in item.value for item in app.warning)


def test_model_verification_is_bound_to_current_config():
    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import Settings

    settings = Settings(
        deepseek_api_key="key-one",
        model_provider="deepseek",
        model_name="deepseek-chat",
        model_base_url="https://api.deepseek.com",
    )
    state = {
        "model_verified_fingerprint": ui_main._model_config_fingerprint(settings),
    }

    assert ui_main._model_state(settings, state) == "verified"
    assert (
        ui_main._model_state(
            settings.model_copy(update={"deepseek_api_key": "key-two"}),
            state,
        )
        == "configured"
    )
    assert (
        ui_main._model_state(
            settings.model_copy(update={"model_name": "deepseek-reasoner"}),
            state,
        )
        == "configured"
    )
    assert (
        ui_main._model_state(
            settings.model_copy(update={"model_base_url": "https://example.invalid"}),
            state,
        )
        == "configured"
    )


def test_record_model_success_stores_only_current_config_fingerprint():
    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import Settings

    repository = Mock()
    repository.get_output.return_value = {"findings": []}
    settings = Settings(deepseek_api_key="key-one")
    state = {}

    ui_main._record_model_success(repository, "run-1", settings, state)

    assert state == {
        "model_verified_fingerprint": ui_main._model_config_fingerprint(settings),
    }
    assert settings.deepseek_api_key not in repr(state)


def test_online_input_error_is_friendly_and_does_not_create_run(tmp_path, monkeypatch):
    from app_review_insights.storage import RunRepository

    app_path = Path(__file__).parents[1] / "app.py"
    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    app = AppTest.from_file(str(app_path)).run(timeout=30)

    start_button = next(button for button in app.button if button.label == "开始分析")
    app = start_button.click().run(timeout=30)

    assert not app.exception
    assert any("在线采集需要填写 App Store URL" in error.value for error in app.error)
    assert RunRepository(database_path).list_runs() == []


def test_waiting_run_keeps_saved_output_visible_without_model_key(tmp_path, monkeypatch):
    from app_review_insights.models import (
        AnalysisRequest,
        RunRecord,
        RunStatus,
        SourceType,
        Stage,
        StageEvent,
    )
    from app_review_insights.storage import RunRepository

    app_path = Path(__file__).parents[1] / "app.py"
    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    repository = RunRepository(database_path)
    now = datetime.now(UTC)
    run = RunRecord(
        run_id="waiting-run",
        request=AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="审阅订阅体验问题",
        ),
        current_stage=Stage.ANALYZE_BATCHES,
        status=RunStatus.WAITING,
        current_batch=1,
        total_batches=2,
        coverage_ratio=0.5,
        last_error="模型暂时不可用",
        created_at=now,
        updated_at=now,
    )
    repository.save_run(run)
    repository.save_output(
        run.run_id,
        Stage.CLEAN,
        {
            "reviews": [],
            "stats": {
                "input_count": 4,
                "output_count": 3,
                "exact_duplicates": 1,
                "near_duplicates": 0,
                "empty_removed": 0,
            },
        },
    )
    repository.add_event(
        run.run_id,
        StageEvent(
            stage=Stage.ANALYZE_BATCHES,
            status=RunStatus.WAITING,
            message="Batch analysis paused for retry",
            created_at=now,
        ),
    )

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert not app.exception
    assert any(metric.label == "输入评论" and metric.value == "4" for metric in app.metric)
    resume_button = next(button for button in app.button if button.label == "继续分析")
    # 计划书规定：未配置密钥时按钮禁用，配置密钥后同一 run_id 续跑。
    assert resume_button.disabled is True


def test_safe_record_table_drops_private_unapproved_fields():
    from app_review_insights.ui.components import _records_frame

    frame = _records_frame(
        [{"review_id": "r-1", "api_key": "must-not-render"}],
        ["review_id"],
    )

    assert list(frame.columns) == ["review_id"]
    assert "must-not-render" not in frame.to_json()


def test_imported_reviews_are_capped_by_requested_limit():
    from app_review_insights.models import AnalysisRequest, SourceType
    from app_review_insights.ui.main import _prepare_imported_reviews

    payload = json.dumps(
        [
            {
                "review_id": f"r-{index}",
                "content": f"review content {index}",
                "rating": 3,
                "published_at": "2026-08-01T10:00:00Z",
            }
            for index in range(101)
        ]
    ).encode()
    upload = Mock()
    upload.name = "reviews.json"
    upload.getvalue.return_value = payload
    request = AnalysisRequest(
        source_type=SourceType.JSON,
        analysis_goal="审阅导入评论问题",
        review_limit=100,
    )

    reviews = _prepare_imported_reviews(request, upload)

    assert len(reviews) == 100


def test_online_collection_shortfall_is_disclosed_as_run_limitation():
    from app_review_insights.models import (
        AnalysisRequest,
        RunRecord,
        RunStatus,
        SourceType,
        Stage,
    )
    from app_review_insights.ui.components import _run_limitations

    now = datetime.now(UTC)
    run = RunRecord(
        run_id="short-online-run",
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="审阅在线评论问题",
            app_url="https://apps.apple.com/us/app/example/id123456789",
            review_limit=500,
        ),
        current_stage=Stage.COMPLETE,
        status=RunStatus.COMPLETED,
        coverage_ratio=1,
        created_at=now,
        updated_at=now,
    )

    limitations = _run_limitations(run, {"stats": {"input_count": 50}})

    assert limitations == ["在线采集目标 500 条，实际获得 50 条；样本短缺会降低证据覆盖度。"]


def test_review_id_collision_is_disclosed_as_run_limitation():
    from app_review_insights.models import AnalysisRequest, SourceType
    from app_review_insights.ui.components import _run_limitations

    run = AnalysisRequest(
        source_type=SourceType.CSV,
        analysis_goal="审阅导入评论问题",
    )

    limitations = _run_limitations(
        type("Run", (), {"request": run})(),
        {"stats": {"input_count": 2, "review_id_collisions": 1}},
    )

    assert limitations == [
        "发现 1 个重复评论 ID 对应不同正文；已稳定重命名，后续证据链使用重命名后的唯一 ID。"
    ]


def test_completed_run_displays_evidence_validation_and_prd_metadata(
    tmp_path,
    monkeypatch,
):
    from app_review_insights.models import (
        AnalysisRequest,
        RunRecord,
        RunStatus,
        SourceType,
        Stage,
    )
    from app_review_insights.storage import RunRepository

    app_path = Path(__file__).parents[1] / "app.py"
    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    repository = RunRepository(database_path)
    now = datetime.now(UTC)
    run = RunRecord(
        run_id="completed-evidence-run",
        request=AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="审阅订阅体验问题",
        ),
        current_stage=Stage.COMPLETE,
        status=RunStatus.COMPLETED,
        coverage_ratio=1,
        created_at=now,
        updated_at=now,
    )
    repository.save_run(run)
    repository.save_output(
        run.run_id,
        Stage.CLEAN,
        {
            "reviews": [
                {
                    "review_id": "r-1",
                    "content_original": "The renewal date is unclear.",
                    "content_summary_zh": "用户不清楚续费日期。",
                }
            ],
            "stats": {"input_count": 1, "output_count": 1},
        },
    )
    repository.save_output(
        run.run_id,
        Stage.VALIDATE_FINDINGS,
        {
            "findings": [
                {
                    "finding_id": "F-001",
                    "title": "续费日期不清晰",
                    "problem_statement": "用户无法理解续费日期。",
                    "topic_label": "订阅",
                    "supporting_review_ids": ["r-1"],
                    "conflicting_review_ids": [],
                    "support_count": 1,
                    "conflict_count": 0,
                    "confidence": 0.6,
                    "evidence_status": "validated",
                    "model_reasoning_summary": "评论直接描述续费日期问题。",
                    "schema_validated": True,
                    "reference_validated": True,
                    "semantic_validated": True,
                    "evidence_assessments": [
                        {
                            "review_id": "r-1",
                            "role": "supporting",
                            "rationale_zh": "原文直接支持该问题。",
                        }
                    ],
                    "limitations": ["样本仅包含一条支持评论。"],
                }
            ],
            "report": {"valid": True, "issues": []},
        },
    )
    repository.save_output(
        run.run_id,
        Stage.PLAN,
        {
            "requirements": [
                {
                    "requirement_id": "REQ-001",
                    "finding_ids": ["F-001"],
                    "title": "明确展示续费日期",
                    "user_problem": "用户无法理解续费日期。",
                    "objective": "在购买前明确告知续费日期。",
                    "scope": ["订阅确认页"],
                    "non_goals": [],
                    "functional_rules": ["购买前展示续费日期"],
                    "edge_cases": [],
                    "acceptance_criteria": ["续费日期清晰可见"],
                    "success_metrics": ["减少续费日期相关投诉"],
                    "impact": 4,
                    "complexity": "low",
                    "priority_score": 4,
                    "target_version": "V1.0",
                    "source_review_ids": ["r-1"],
                    "assumptions": ["需要确认订阅页是否支持灰度发布。"],
                }
            ]
        },
    )
    repository.save_output(run.run_id, Stage.GENERATE_TESTS, {"test_cases": []})
    repository.save_output(
        run.run_id,
        Stage.VALIDATE_TRACEABILITY,
        {"valid": True, "issues": []},
    )

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert not app.exception
    markdown_values = [item.value for item in app.markdown]
    assert "**Schema 校验**：已通过" in markdown_values
    assert "**引用存在性**：已通过" in markdown_values
    assert "**证据语义**：已复核" in markdown_values
    assert "**局限说明**" in markdown_values
    assert "**目标版本**：V1.0" in markdown_values
    assert "**业务假设**" in markdown_values
    evidence_frame = next(item.value for item in app.dataframe if "复核理由" in item.value.columns)
    assert evidence_frame.iloc[0]["评论原文"] == "The renewal date is unclear."
    assert evidence_frame.iloc[0]["中文摘要"] == "用户不清楚续费日期。"
    assert evidence_frame.iloc[0]["复核理由"] == "原文直接支持该问题。"


def test_resume_analysis_passes_the_existing_run_id(monkeypatch):
    import app_review_insights.ui.main as ui_main

    resumed_run = Mock(run_id="existing-run")
    orchestrator = Mock()
    orchestrator.resume.return_value = resumed_run
    orchestrator_factory = Mock(return_value=orchestrator)
    monkeypatch.setattr(ui_main, "AnalysisOrchestrator", orchestrator_factory)
    services = Mock()
    event_writer = Mock()

    result = ui_main._resume_analysis(services, "existing-run", event_writer)

    # 只断言关键参数，避免每次给编排器加配置都来改这条断言
    orchestrator_factory.assert_called_once()
    assert orchestrator_factory.call_args.args == (services,)
    assert orchestrator_factory.call_args.kwargs["on_event"] is event_writer
    orchestrator.resume.assert_called_once_with("existing-run")
    assert result.run_id == "existing-run"


def test_root_entrypoint_still_exposes_callable_main():
    app_path = Path(__file__).parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("app_smoke_entry", app_path)

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)


def test_resume_hides_old_error_and_shows_recovery_state(tmp_path, monkeypatch):
    import app_review_insights.ui.main as ui_main
    from app_review_insights.models import (
        AnalysisRequest,
        RunRecord,
        RunStatus,
        SourceType,
        Stage,
    )
    from app_review_insights.storage import RunRepository

    app_path = Path(__file__).parents[1] / "app.py"
    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    repository = RunRepository(database_path)
    now = datetime.now(UTC)
    waiting = RunRecord(
        run_id="waiting-run",
        request=AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="测试目标",
        ),
        current_stage=Stage.ANALYZE_BATCHES,
        status=RunStatus.WAITING,
        current_batch=0,
        total_batches=1,
        coverage_ratio=0.0,
        last_error="模型调用失败，可从检查点继续：401 [REDACTED]",
        created_at=now,
        updated_at=now,
    )
    repository.save_run(waiting)
    repository.save_output(
        waiting.run_id,
        Stage.CLEAN,
        {"reviews": [], "stats": {"input_count": 1, "output_count": 1}},
    )
    completed = waiting.model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "current_stage": Stage.COMPLETE,
            "coverage_ratio": 1,
            "last_error": None,
        }
    )

    def fake_resume(services, run_id, event_writer=None):
        services.repository.save_run(completed)
        return completed

    monkeypatch.setattr(ui_main, "_resume_analysis", fake_resume)

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert any("模型调用失败" in item.value for item in app.warning)
    resume_btn = next(button for button in app.button if button.label == "继续分析")
    assert resume_btn.disabled is False

    # Simulate clicking 继续分析: pending resume state hides the old error.
    app.session_state["pending_resume"] = "waiting-run"
    app = app.run(timeout=30)

    assert not any("模型调用失败" in item.value for item in app.warning)
    assert not any("等待模型恢复" in item.value for item in app.warning)
    assert not any(button.label == "继续分析" for button in app.button)


def test_invalid_key_shows_warning_but_keeps_start_clickable(tmp_path, monkeypatch):
    import app_review_insights.ui.main as ui_main

    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-invalid-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setattr(
        ui_main,
        "_validate_model_key",
        lambda settings, session_state=None: "invalid",
    )

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert any("密钥无效" in item.value for item in app.warning)
    start_button = next(button for button in app.button if button.label == "开始分析")
    # 密钥无效时仍可开始分析，流程会在模型环节暂停并保留检查点。
    assert start_button.disabled is False


def test_unavailable_model_shows_warning_but_keeps_start_clickable(tmp_path, monkeypatch):
    import app_review_insights.ui.main as ui_main

    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-some-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setattr(
        ui_main,
        "_validate_model_key",
        lambda settings, session_state=None: "unavailable",
    )

    app = AppTest.from_file(str(app_path)).run(timeout=30)

    assert any("无法连接模型服务" in item.value for item in app.warning)
    start_button = next(button for button in app.button if button.label == "开始分析")
    assert start_button.disabled is False
