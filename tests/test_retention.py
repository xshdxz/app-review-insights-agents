"""数据保留策略与空间回收。

events 表每推进一个阶段就写一条，stage_outputs 保存每个批次的完整 JSON，
reports 每次监控都新增一份——不清理的话磁盘只涨不跌。

删除策略的**安全底线**：只清理终态运行（completed / failed / partial）；
pending / running / waiting 一律保留，因为它们还可以续跑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app_review_insights.llm.usage import build_usage
from app_review_insights.models import (
    AnalysisRequest,
    MonitorReport,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    StageEvent,
)
from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.repository import RunRepository


@pytest.fixture
def repo(tmp_path):
    return RunRepository(tmp_path / "runs.sqlite3")


def _run(run_id: str, *, status=RunStatus.COMPLETED, age_days: int = 0) -> RunRecord:
    moment = datetime.now(UTC) - timedelta(days=age_days)
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="分析订阅转化",
            app_url="https://apps.apple.com/us/app/x/id1",
        ),
        current_stage=Stage.COMPLETE,
        status=status,
        created_at=moment,
        updated_at=moment,
    )


def _seed_events(repo: RunRepository, run_id: str, count: int) -> None:
    for index in range(count):
        repo.add_event(
            run_id,
            StageEvent(
                stage=Stage.ANALYZE_BATCHES,
                status=RunStatus.RUNNING,
                message=f"事件 {index}",
                created_at=datetime.now(UTC),
            ),
        )


# ── 事件裁剪 ─────────────────────────────────────────────────────────────────


def test_prune_events_keeps_only_the_latest_per_run(repo):
    repo.save_run(_run("run-1"))
    repo.save_run(_run("run-2"))
    _seed_events(repo, "run-1", 30)
    _seed_events(repo, "run-2", 5)

    removed = repo.prune_events(keep_per_run=10)

    assert removed == 20  # run-1 删 20 条（30→10），run-2 未超限不删
    assert len(repo.list_events("run-1")) == 10
    assert len(repo.list_events("run-2")) == 5


def test_prune_events_keeps_the_newest(repo):
    repo.save_run(_run("run-1"))
    _seed_events(repo, "run-1", 30)

    repo.prune_events(keep_per_run=3)

    messages = [event.message for event in repo.list_events("run-1")]
    assert messages == ["事件 27", "事件 28", "事件 29"]


def test_prune_events_is_a_noop_when_under_the_limit(repo):
    repo.save_run(_run("run-1"))
    _seed_events(repo, "run-1", 2)

    assert repo.prune_events(keep_per_run=10) == 0


# ── 运行清理 ─────────────────────────────────────────────────────────────────


def test_prune_runs_removes_old_terminal_runs_with_their_data(repo):
    repo.save_run(_run("old", age_days=200))
    repo.save_output("old", Stage.CLEAN, {"reviews": []})
    _seed_events(repo, "old", 3)
    repo.record_model_usage(
        build_usage(
            model="deepseek-chat",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1.0,
            run_id="old",
        )
    )

    removed = repo.prune_runs(older_than_days=90)

    assert removed == 1
    with pytest.raises(KeyError):
        repo.get_run("old")
    assert repo.get_output("old", Stage.CLEAN) is None
    assert repo.list_events("old") == []
    assert repo.model_usage_summary(run_id="old")["calls"] == 0


def test_prune_runs_keeps_recent_runs(repo):
    repo.save_run(_run("fresh", age_days=1))

    assert repo.prune_runs(older_than_days=90) == 0
    assert repo.get_run("fresh").run_id == "fresh"


@pytest.mark.parametrize(
    "status",
    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING],
)
def test_prune_runs_never_deletes_resumable_runs(repo, status):
    """未终结的运行还能续跑，无论多老都不能删。"""
    repo.save_run(_run("stuck", status=status, age_days=999))

    assert repo.prune_runs(older_than_days=90) == 0
    assert repo.get_run("stuck").status == status


def test_prune_runs_keeps_other_runs_intact(repo):
    repo.save_run(_run("old", age_days=200))
    repo.save_run(_run("new", age_days=1))

    repo.prune_runs(older_than_days=90)

    assert repo.get_run("new").run_id == "new"


# ── 报告清理与空间回收 ───────────────────────────────────────────────────────


def test_prune_reports_keeps_latest_per_app(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    for index in range(6):
        agent_repo.save_report(
            MonitorReport(
                report_id=f"rep-{index}",
                agent_run_id="ar-1",
                app_url="https://apps.apple.com/us/app/x/id1",
                goal="g",
                markdown="",
                summary="",
                findings_count=1,
                created_at=datetime.now(UTC) - timedelta(days=6 - index),
            )
        )

    removed = agent_repo.prune_reports(keep_per_app=2)

    assert removed == 4
    remaining = agent_repo.list_reports(limit=10)
    assert len(remaining) == 2
    assert {report.report_id for report in remaining} == {"rep-4", "rep-5"}


# ── 维护入口 ─────────────────────────────────────────────────────────────────


def _settings(tmp_path, **overrides):
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.database_path = tmp_path / "runs.sqlite3"
    settings.agent_db_path = tmp_path / "agent.sqlite3"
    settings.retention_days = 90
    settings.events_keep_per_run = 5
    settings.reports_keep_per_app = 2
    settings.maintenance_interval_seconds = 86400
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def test_run_maintenance_prunes_events_and_vacuums(tmp_path):
    from app_review_insights.maintenance import run_maintenance

    settings = _settings(tmp_path)
    repo = RunRepository(settings.database_path)
    repo.save_run(_run("run-1"))
    _seed_events(repo, "run-1", 20)

    result = run_maintenance(settings)

    assert result["events_removed"] == 15
    assert result["vacuumed"] == 1
    assert len(repo.list_events("run-1")) == 5


def test_run_maintenance_skips_vacuum_when_nothing_to_remove(tmp_path):
    """没删东西就不该付 VACUUM 重写整个库的代价。"""
    from app_review_insights.maintenance import run_maintenance

    settings = _settings(tmp_path)
    result = run_maintenance(settings)

    assert result["events_removed"] == 0
    assert result["runs_removed"] == 0
    assert result["vacuumed"] == 0


def test_maintenance_loop_exits_when_stop_already_set(tmp_path):
    import threading

    from app_review_insights.maintenance import start_maintenance_loop

    stop = threading.Event()
    stop.set()
    thread = start_maintenance_loop(_settings(tmp_path), stop)
    thread.join(timeout=5)

    assert not thread.is_alive()


def test_maintenance_loop_survives_failures(tmp_path, monkeypatch):
    """维护失败必须只告警，不能让后台线程死掉、更不能影响调度。"""
    import threading
    import time

    from app_review_insights import maintenance

    calls: list[int] = []

    def _boom(_settings):
        calls.append(1)
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(maintenance, "run_maintenance", _boom)
    stop = threading.Event()
    thread = maintenance.start_maintenance_loop(
        _settings(tmp_path, maintenance_interval_seconds=0.01), stop
    )
    for _ in range(200):
        if len(calls) >= 2:
            break
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)

    assert len(calls) >= 2
    assert not thread.is_alive()


def test_vacuum_reclaims_space_without_corrupting(repo):
    repo.save_run(_run("old", age_days=200))
    _seed_events(repo, "old", 20)
    repo.prune_runs(older_than_days=90)

    repo.vacuum()  # 不抛

    repo.save_run(_run("after"))
    assert repo.get_run("after").run_id == "after"
