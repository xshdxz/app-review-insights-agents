"""并发互斥：同一 App 同时只允许一个运行。

手动点一次"开始分析"、定时任务同时触发一次——两次分析各烧一遍模型额度，
得到的还是同一份结论。这不是正确性问题，是**白花钱**的问题。

安全底线：超过阈值未更新的运行视为进程崩溃留下的孤儿，不参与互斥；
否则一次崩溃会永久卡死这个 App 的后续分析。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app_review_insights.errors import ConcurrentRunError
from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType, Stage
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.repository import RunRepository

APP = "https://apps.apple.com/us/app/workout/id839285684"
OTHER_APP = "https://apps.apple.com/us/app/other/id1"


@pytest.fixture
def repo(tmp_path):
    return RunRepository(tmp_path / "runs.sqlite3")


def _run(
    run_id: str,
    app_url: str | None = APP,
    *,
    status: RunStatus = RunStatus.RUNNING,
    age_minutes: int = 0,
) -> RunRecord:
    moment = datetime.now(UTC) - timedelta(minutes=age_minutes)
    # 模型约束：completed 必须配 COMPLETE 阶段
    stage = Stage.COMPLETE if status == RunStatus.COMPLETED else Stage.ANALYZE_BATCHES
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="分析订阅转化",
            app_url=app_url,
        ),
        current_stage=stage,
        status=status,
        created_at=moment,
        updated_at=moment,
    )


def _orchestrator(repo: RunRepository) -> AnalysisOrchestrator:
    return AnalysisOrchestrator(PipelineServices(repository=repo, batch_analyzer=None))


def _request(app_url: str | None = APP) -> AnalysisRequest:
    return AnalysisRequest(
        source_type=SourceType.ONLINE if app_url else SourceType.JSON,
        analysis_goal="分析订阅转化",
        app_url=app_url,
    )


# ── 活跃运行查询 ─────────────────────────────────────────────────────────────


def test_find_active_run_returns_in_flight_run(repo):
    repo.save_run(_run("r1"))

    assert repo.find_active_run(APP).run_id == "r1"


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PARTIAL])
def test_find_active_run_ignores_terminal_runs(repo, status):
    repo.save_run(_run("r1", status=status))

    assert repo.find_active_run(APP) is None


def test_find_active_run_ignores_other_apps(repo):
    repo.save_run(_run("r1", OTHER_APP))

    assert repo.find_active_run(APP) is None


def test_find_active_run_ignores_stale_runs(repo):
    """崩溃留下的 RUNNING 不能永久占位。"""
    repo.save_run(_run("r1", age_minutes=600))

    assert repo.find_active_run(APP, stale_after_minutes=60) is None
    assert repo.find_active_run(APP, stale_after_minutes=1000).run_id == "r1"


def test_find_active_run_returns_the_most_recent(repo):
    repo.save_run(_run("old", age_minutes=30))
    repo.save_run(_run("new", age_minutes=1))

    assert repo.find_active_run(APP).run_id == "new"


def test_find_active_run_tolerates_runs_without_app_url(repo):
    repo.save_run(_run("r1", None))

    assert repo.find_active_run(APP) is None


# ── 编排器守卫 ───────────────────────────────────────────────────────────────


def test_start_refuses_when_same_app_already_running(repo):
    repo.save_run(_run("r1"))

    with pytest.raises(ConcurrentRunError) as excinfo:
        _orchestrator(repo).start(_request())

    assert "r1" in str(excinfo.value)


def test_start_allows_explicit_override(repo):
    repo.save_run(_run("r1"))

    run = _orchestrator(repo).start(_request(), allow_concurrent=True)

    assert run.run_id != "r1"


def test_start_skips_guard_when_no_app_url(repo):
    """导入模式没有 App 链接，无从判断重复，不应误拦。"""
    repo.save_run(_run("r1"))

    run = _orchestrator(repo).start(_request(None))

    assert run.run_id != "r1"


def test_start_succeeds_after_previous_run_finished(repo):
    repo.save_run(_run("r1", status=RunStatus.COMPLETED))

    run = _orchestrator(repo).start(_request())

    assert run.run_id != "r1"


def test_concurrent_error_is_user_facing():
    """UI 需要把它当作可展示的输入类错误，而不是崩溃。"""
    from app_review_insights.errors import AppReviewInsightsError

    assert issubclass(ConcurrentRunError, AppReviewInsightsError)
