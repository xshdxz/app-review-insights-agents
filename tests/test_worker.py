"""Tests for monitor/worker.py — make_run_job_fn and main()."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app_review_insights.models import AgentRun, AgentRunStatus, MonitorReport

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_agent_run(status=AgentRunStatus.COMPLETED, run_id="ar-1"):
    return AgentRun(
        run_id=run_id,
        goal="分析订阅转化",
        app_url="https://apps.apple.com/us/app/x/id1",
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _make_report(report_id="rep-1"):
    return MonitorReport(
        report_id=report_id,
        agent_run_id="ar-1",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="分析订阅转化",
        markdown="# 报告",
        summary="摘要",
        findings_count=7,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── make_run_job_fn tests ────────────────────────────────────────────────────


def test_make_run_job_fn_returns_callable():
    from app_review_insights.monitor.worker import make_run_job_fn

    stack = MagicMock()
    settings = MagicMock()
    fn = make_run_job_fn(stack, settings)
    assert callable(fn)


def test_run_job_builds_report_and_saves_when_completed():
    from app_review_insights.monitor.worker import make_run_job_fn

    agent_run = _make_agent_run(AgentRunStatus.COMPLETED)
    report = _make_report()

    stack = MagicMock()
    stack.orchestrator.run.return_value = agent_run

    settings = MagicMock()
    settings.database_path = "/tmp/fake.db"

    with (
        patch("app_review_insights.monitor.worker.build_report", return_value=report) as mock_build,
        patch("app_review_insights.monitor.worker.WebhookSender") as mock_sender_class,
        patch("app_review_insights.monitor.worker.RunRepository"),
    ):
        mock_sender = MagicMock()
        mock_sender.send_report_by_id.return_value = ["https://hooks.feishu.cn/test"]
        mock_sender_class.return_value = mock_sender

        fn = make_run_job_fn(stack, settings)
        fn("分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=False)

    # orchestrator.run 被正确调用
    stack.orchestrator.run.assert_called_once_with(
        "分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=False
    )
    # report 构建并保存
    mock_build.assert_called_once()
    stack.agent_repository.save_agent_run.assert_called()
    stack.agent_repository.save_report.assert_called()


def test_run_job_skips_webhook_when_waiting_approval():
    from app_review_insights.monitor.worker import make_run_job_fn

    agent_run = _make_agent_run(AgentRunStatus.WAITING_APPROVAL)
    report = _make_report()

    stack = MagicMock()
    stack.orchestrator.run.return_value = agent_run

    settings = MagicMock()
    settings.database_path = "/tmp/fake.db"

    with (
        patch("app_review_insights.monitor.worker.build_report", return_value=report),
        patch("app_review_insights.monitor.worker.WebhookSender") as mock_sender_class,
        patch("app_review_insights.monitor.worker.RunRepository"),
    ):
        mock_sender = MagicMock()
        mock_sender_class.return_value = mock_sender

        fn = make_run_job_fn(stack, settings)
        fn("分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=True)

    # waiting_approval 状态不推送 webhook
    mock_sender.send_report_by_id.assert_not_called()


def test_run_job_skips_report_when_failed():
    from app_review_insights.monitor.worker import make_run_job_fn

    agent_run = _make_agent_run(AgentRunStatus.FAILED)

    stack = MagicMock()
    stack.orchestrator.run.return_value = agent_run
    settings = MagicMock()

    with patch("app_review_insights.monitor.worker.build_report") as mock_build:
        fn = make_run_job_fn(stack, settings)
        fn("分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=False)

    # failed 状态不构建报告
    mock_build.assert_not_called()
    stack.agent_repository.save_agent_run.assert_not_called()


def test_run_job_saves_delivered_to_on_webhook_success():
    from app_review_insights.monitor.worker import make_run_job_fn

    agent_run = _make_agent_run(AgentRunStatus.COMPLETED)
    report = _make_report()

    stack = MagicMock()
    stack.orchestrator.run.return_value = agent_run

    settings = MagicMock()
    settings.database_path = "/tmp/fake.db"

    with (
        patch("app_review_insights.monitor.worker.build_report", return_value=report),
        patch("app_review_insights.monitor.worker.WebhookSender") as mock_sender_class,
        patch("app_review_insights.monitor.worker.RunRepository"),
    ):
        mock_sender = MagicMock()
        mock_sender.send_report_by_id.return_value = ["https://hooks.feishu.cn/test"]
        mock_sender_class.return_value = mock_sender

        fn = make_run_job_fn(stack, settings)
        fn("分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=False)

    calls = stack.agent_repository.save_report.call_args_list
    assert len(calls) >= 1
    saved_report = calls[-1][0][0]
    assert saved_report.delivered_to == ["https://hooks.feishu.cn/test"]


def test_run_job_no_delivered_on_webhook_failure():
    from app_review_insights.monitor.worker import make_run_job_fn

    agent_run = _make_agent_run(AgentRunStatus.COMPLETED)
    report = _make_report()

    stack = MagicMock()
    stack.orchestrator.run.return_value = agent_run

    settings = MagicMock()
    settings.database_path = "/tmp/fake.db"

    with (
        patch("app_review_insights.monitor.worker.build_report", return_value=report),
        patch("app_review_insights.monitor.worker.WebhookSender") as mock_sender_class,
        patch("app_review_insights.monitor.worker.RunRepository"),
    ):
        mock_sender = MagicMock()
        mock_sender.send_report_by_id.return_value = []  # 推送失败
        mock_sender_class.return_value = mock_sender

        fn = make_run_job_fn(stack, settings)
        fn("分析订阅转化", "https://apps.apple.com/us/app/x/id1", require_approval=False)

    # delivered_to 为空，不保存 delivered_to 更新
    calls = stack.agent_repository.save_report.call_args_list
    if calls:
        saved_report = calls[-1][0][0]
        assert saved_report.delivered_to == []


# ── main() tests ─────────────────────────────────────────────────────────────


def test_main_exits_when_scheduler_disabled():
    from app_review_insights.monitor.worker import main

    settings = MagicMock()
    settings.scheduler_enabled = False

    with patch("app_review_insights.monitor.worker.load_settings", return_value=settings):
        # main() 应该正常返回（不抛异常），只是发 warning 并退出
        main()


def test_main_starts_scheduler_when_enabled():
    from app_review_insights.monitor.worker import main

    settings = MagicMock()
    settings.scheduler_enabled = True
    settings.database_path = "/tmp/fake.db"
    settings.model_available = False
    settings.agent_db_path = "/tmp/agent.db"
    settings.embedding_enabled = False
    settings.embedding_api_key = ""
    settings.model_api_key = ""
    settings.deepseek_api_key = ""
    settings.agent_max_review_rounds = 2

    # Mock 掉所有重依赖，只验证 main 流程不崩
    fake_stack = MagicMock()
    fake_scheduler = MagicMock()
    fake_scheduler._scheduler = MagicMock()
    fake_scheduler._scheduler.get_jobs.return_value = []

    with (
        patch("app_review_insights.monitor.worker.load_settings", return_value=settings),
        patch("app_review_insights.monitor.worker.build_agent_stack", return_value=fake_stack),
        patch("app_review_insights.monitor.worker.MonitorScheduler", return_value=fake_scheduler),
        patch("app_review_insights.monitor.worker.make_run_job_fn") as mock_fn,
        # 装上"信号处理器"即刻请求停止，等价于收到一次 SIGTERM
        patch(
            "app_review_insights.monitor.worker.install_signal_handlers",
            side_effect=lambda stop_event: stop_event.set(),
        ),
    ):
        main()

    fake_scheduler.start.assert_called_once()
    fake_scheduler.shutdown.assert_called_once()
    mock_fn.assert_called_once_with(fake_stack, settings)
