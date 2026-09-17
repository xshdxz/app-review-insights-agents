"""整轮运行的墙钟上限。

单次调用早有超时（模型 60s、采集 20s、Webhook 15s），但**整轮运行没有上限**：
输入异常大或模型持续变慢时，一次分析可以跑上几小时没人察觉。

超时必须停在检查点上且可续跑，而不是把已完成的工作丢掉。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app_review_insights.models import (
    AnalysisRequest,
    Review,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.repository import RunRepository


@pytest.fixture
def repo(tmp_path):
    return RunRepository(tmp_path / "runs.sqlite3")


def _services(repo: RunRepository) -> PipelineServices:
    return PipelineServices(repository=repo, batch_analyzer=None)


def _request() -> AnalysisRequest:
    return AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化")


def _reviews() -> list[Review]:
    return [
        Review(
            review_id=f"r-{index}",
            app_id="demo",
            content_original=f"评论内容 {index}",
            rating=2,
            published_at=datetime(2026, 6, 1, tzinfo=UTC),
            source="import",
        )
        for index in range(3)
    ]


# ── 超时行为 ─────────────────────────────────────────────────────────────────


def test_zero_duration_deadline_stops_before_any_work(repo):
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=0)

    run = orchestrator.start(_request(), imported_reviews=_reviews())

    assert run.status == RunStatus.TIMED_OUT
    assert run.last_error is not None
    assert "时长" in run.last_error


def test_timed_out_run_is_persisted_with_the_status(repo):
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=0)

    run = orchestrator.start(_request(), imported_reviews=_reviews())

    assert repo.get_run(run.run_id).status == RunStatus.TIMED_OUT


def test_timeout_records_an_event_explaining_why(repo):
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=0)

    run = orchestrator.start(_request(), imported_reviews=_reviews())

    messages = [event.message for event in repo.list_events(run.run_id)]
    assert any("exceeded" in message.lower() for message in messages)


def test_timeout_before_any_stage_output_leaves_nothing_to_corrupt(repo):
    """第一阶段的截止时间就超了：不应留下半截阶段输出。"""
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=0)

    run = orchestrator.start(_request(), imported_reviews=_reviews())

    assert repo.get_output(run.run_id, Stage.COLLECT) is None


# ── 默认不设上限 ─────────────────────────────────────────────────────────────


def test_no_deadline_configured_does_not_time_out(repo):
    """默认行为不能变：没配上限就不该有上限。"""
    orchestrator = AnalysisOrchestrator(_services(repo))

    assert orchestrator.max_duration_seconds is None


def test_generous_deadline_lets_the_run_proceed_past_the_first_stage(repo):
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=3600)

    run = orchestrator.start(_request(), imported_reviews=_reviews())

    # 采集阶段应当完成（JSON 导入无需网络），说明没被误判超时
    assert run.status != RunStatus.TIMED_OUT
    assert repo.get_output(run.run_id, Stage.COLLECT) is not None


# ── 可续跑 ───────────────────────────────────────────────────────────────────


def test_timed_out_run_can_be_resumed(repo):
    orchestrator = AnalysisOrchestrator(_services(repo), max_duration_seconds=0)
    run = orchestrator.start(_request(), imported_reviews=_reviews())

    # 超时发生在采集阶段之前，检查点里还没有评论，因此续跑要重新提供导入文件
    resumed = AnalysisOrchestrator(_services(repo), max_duration_seconds=3600).resume(
        run.run_id, imported_reviews=_reviews()
    )

    assert resumed.status != RunStatus.TIMED_OUT
    assert repo.get_output(run.run_id, Stage.COLLECT) is not None


def test_resume_still_ignores_completed_runs(repo):
    moment = datetime.now(UTC)
    repo.save_run(
        RunRecord(
            run_id="done",
            request=_request(),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            created_at=moment,
            updated_at=moment,
        )
    )

    resumed = AnalysisOrchestrator(_services(repo)).resume("done")

    assert resumed.status == RunStatus.COMPLETED


# ── 状态可展示 ───────────────────────────────────────────────────────────────


def test_timed_out_status_has_a_chinese_label_and_color():
    from app_review_insights.ui.components import STATUS_LABELS

    assert RunStatus.TIMED_OUT.value in STATUS_LABELS
