"""监控任务失败告警。

A2 已经让失败留下 traceback，但没人会一直盯着日志——失败必须主动通知。
关键在于**按状态跃迁告警**：首次失败通知、持续失败不刷屏、恢复时也告知，
否则一个每 5 分钟跑一次的任务会把群刷爆。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app_review_insights.models import MonitorJob
from app_review_insights.monitor.alerts import build_failure_alert, build_recovery_alert
from app_review_insights.monitor.webhook import WebhookSender


def _job(last_status=None, name="每日监控", app_url="https://apps.apple.com/us/app/x/id1"):
    return MonitorJob(
        job_id="j1",
        name=name,
        app_url=app_url,
        goal="监控订阅转化",
        cron="0 9 * * *",
        last_status=last_status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _settings(urls=("https://hooks.example.com/x",), webhook_type="feishu"):
    settings = MagicMock()
    settings.webhook_urls = list(urls)
    settings.webhook_type = webhook_type
    return settings


# ── 告警文案 ─────────────────────────────────────────────────────────────────


def test_failure_alert_names_the_job_app_and_error():
    text = build_failure_alert(_job(), "采集源返回 0 条评论")

    assert "失败" in text
    assert "每日监控" in text
    assert "https://apps.apple.com/us/app/x/id1" in text
    assert "采集源返回 0 条评论" in text


def test_failure_alert_handles_missing_error_text():
    assert "未知错误" in build_failure_alert(_job(), None)


def test_recovery_alert_mentions_recovery():
    text = build_recovery_alert(_job(last_status="failed"))
    assert "恢复" in text
    assert "每日监控" in text


# ── 通用文本推送 ─────────────────────────────────────────────────────────────


def test_send_text_posts_to_every_configured_url():
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200)
    sender = WebhookSender(client=client)

    delivered = sender.send_text(
        "告警内容", _settings(urls=["https://a.example/h", "https://b.example/h"])
    )

    assert delivered == ["https://a.example/h", "https://b.example/h"]
    assert client.post.call_count == 2


def test_send_text_returns_empty_when_not_configured():
    client = MagicMock()
    sender = WebhookSender(client=client)

    assert sender.send_text("告警内容", _settings(urls=(), webhook_type="")) == []
    client.post.assert_not_called()


def test_send_text_skips_urls_that_fail():
    client = MagicMock()
    client.post.side_effect = [MagicMock(status_code=500), MagicMock(status_code=200)]
    sender = WebhookSender(client=client)

    delivered = sender.send_text(
        "告警", _settings(urls=["https://bad.example/h", "https://ok.example/h"])
    )

    assert delivered == ["https://ok.example/h"]


def test_send_text_ignores_unknown_channel():
    client = MagicMock()
    sender = WebhookSender(client=client)

    assert sender.send_text("告警", _settings(webhook_type="telegram")) == []
    client.post.assert_not_called()


# ── 状态跃迁：只在变化时告警 ─────────────────────────────────────────────────


def test_alerter_fires_on_first_failure():
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    sender.send_text.return_value = ["https://hooks.example.com/x"]
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status="completed"), _job(last_status="failed"), "炸了")

    sender.send_text.assert_called_once()
    assert "炸了" in sender.send_text.call_args[0][0]


def test_alerter_stays_silent_on_repeated_failure():
    """连续失败不刷屏——否则每 5 分钟一次的任务会把群刷爆。"""
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status="failed"), _job(last_status="failed"), "还是炸")

    sender.send_text.assert_not_called()


def test_alerter_fires_on_recovery():
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    sender.send_text.return_value = ["https://hooks.example.com/x"]
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status="failed"), _job(last_status="completed"), None)

    sender.send_text.assert_called_once()
    assert "恢复" in sender.send_text.call_args[0][0]


def test_alerter_silent_on_repeated_success():
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status="completed"), _job(last_status="completed"), None)

    sender.send_text.assert_not_called()


def test_alerter_fires_on_first_ever_run_success():
    """首次成功不必告警，避免新任务上线就发一条无意义通知。"""
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status=None), _job(last_status="completed"), None)

    sender.send_text.assert_not_called()


def test_alerter_never_raises_even_if_push_explodes():
    """告警通道故障绝不能反过来搞挂调度器。"""
    from app_review_insights.monitor.alerts import make_status_alerter

    sender = MagicMock()
    sender.send_text.side_effect = RuntimeError("webhook 挂了")
    alerter = make_status_alerter(sender, _settings())

    alerter(_job(last_status="completed"), _job(last_status="failed"), "炸了")  # 不抛
