import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest


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

    services = build_services()

    assert services.repository.path == database_path
    assert services.repository.path.exists()
    assert services.batch_analyzer is None


def test_build_services_with_key_wires_existing_deepseek_pipeline(tmp_path, monkeypatch):
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
        ui_main.DeepSeekProvider,
        "from_settings",
        staticmethod(provider_factory),
    )
    monkeypatch.setattr(ui_main, "analyze_batch", analyze)
    monkeypatch.setattr(ui_main, "consolidate_findings", consolidate)
    monkeypatch.setattr(ui_main, "audit_finding_evidence", audit)
    monkeypatch.setattr(ui_main, "build_requirements", plan)
    monkeypatch.setattr(ui_main, "generate_test_cases", generate_tests)

    services = ui_main.build_services()

    assert isinstance(services.collector, AppStoreCollector)
    assert services.finding_validator is ui_main.validate_finding_drafts
    assert services.traceability_validator is ui_main.validate_traceability
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

    app = AppTest.from_file(str(app_path)).run(timeout=10)

    assert not app.exception
    assert any("证据审阅工作台" in title.value for title in app.title)
    start_button = next(button for button in app.button if button.label == "开始分析")
    assert start_button.disabled is True
    review_limit = next(slider for slider in app.slider if slider.label == "评论数量")
    assert review_limit.min == 100
    assert review_limit.max == 500
    assert review_limit.step == 1


def test_input_mode_switches_to_mode_specific_fields(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    online = AppTest.from_file(str(app_path)).run(timeout=10)
    source = next(item for item in online.segmented_control if item.label == "数据来源")
    online_limit = next(item for item in online.slider if item.label == "评论数量")

    assert [item.label for item in online.text_input] == ["App 地址（URL）"]
    assert [item.label for item in online.file_uploader] == []
    assert online_limit.max == 500
    assert online_limit.step == 1

    json_mode = source.set_value("JSON 导入").run(timeout=10)
    json_limit = next(item for item in json_mode.slider if item.label == "评论数量")

    assert [item.label for item in json_mode.text_input] == []
    assert [item.label for item in json_mode.file_uploader] == ["JSON 评论文件"]
    assert json_limit.max == 1000
    assert json_limit.step == 1

    csv_source = next(item for item in json_mode.segmented_control if item.label == "数据来源")
    csv_mode = csv_source.set_value("CSV 导入").run(timeout=10)

    assert [item.label for item in csv_mode.text_input] == []
    assert [item.label for item in csv_mode.file_uploader] == ["CSV 评论文件"]


def test_explicit_model_disable_overrides_configured_key(tmp_path, monkeypatch):
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.setenv("MODEL_ENABLED", "false")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-but-disabled")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    app = AppTest.from_file(str(app_path)).run(timeout=10)
    start_button = next(button for button in app.button if button.label == "开始分析")

    assert start_button.disabled is True
    assert any("已显式禁用" in item.value for item in app.warning)


def test_online_input_error_is_friendly_and_does_not_create_run(tmp_path, monkeypatch):
    from app_review_insights.storage import RunRepository

    app_path = Path(__file__).parents[1] / "app.py"
    database_path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    app = AppTest.from_file(str(app_path)).run(timeout=10)

    start_button = next(button for button in app.button if button.label == "开始分析")
    app = start_button.click().run(timeout=10)

    assert not app.exception
    assert any("在线采集需要填写美国区 App Store URL" in error.value for error in app.error)
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

    app = AppTest.from_file(str(app_path)).run(timeout=10)

    assert not app.exception
    assert any(metric.label == "输入评论" and metric.value == "4" for metric in app.metric)
    resume_button = next(button for button in app.button if button.label == "继续分析")
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

    app = AppTest.from_file(str(app_path)).run(timeout=10)

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

    orchestrator_factory.assert_called_once_with(services, on_event=event_writer)
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
