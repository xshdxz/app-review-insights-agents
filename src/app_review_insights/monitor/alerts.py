"""监控任务的失败/恢复告警。

A2 让失败留下了 traceback，但没人会一直盯着日志——失败必须主动通知。

关键设计是**按状态跃迁告警**，而不是每次失败都发：

- 首次失败 → 告警
- 持续失败 → 静默（否则每 5 分钟一次的任务会把群刷爆）
- 从失败恢复 → 告知一次
- 一直成功 → 静默

跃迁判定复用 `MonitorJob.last_status`（保存前的旧值），不需要额外状态表。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app_review_insights.models import MonitorJob
from app_review_insights.monitor.webhook import WebhookSender

logger = logging.getLogger("ari-alerts")

#: (任务保存前的状态, 保存后的任务, 错误文本) -> None
StatusAlertHook = Callable[[MonitorJob, MonitorJob, "str | None"], None]


def build_failure_alert(job: MonitorJob, error: str | None) -> str:
    """构造失败告警文案。"""
    return (
        "⚠️ 监控任务失败\n"
        f"任务：{job.name}\n"
        f"App：{job.app_url}\n"
        f"目标：{job.goal}\n"
        f"错误：{error or '未知错误'}\n"
        "已完成的工作保存在检查点，修复后可从运行记录中续跑。"
    )


def build_recovery_alert(job: MonitorJob) -> str:
    """构造恢复通知文案。"""
    return (
        "✅ 监控任务已恢复\n"
        f"任务：{job.name}\n"
        f"App：{job.app_url}\n"
        "上一条失败告警对应的任务已重新执行成功。"
    )


def make_status_alerter(
    sender: WebhookSender,
    settings,
) -> StatusAlertHook:
    """构造状态跃迁告警回调，供 `MonitorScheduler` 在任务结束后调用。"""

    def _alert(previous: MonitorJob, updated: MonitorJob, error: str | None) -> None:
        try:
            if updated.last_status == "failed" and previous.last_status != "failed":
                delivered = sender.send_text(build_failure_alert(updated, error), settings)
                if delivered:
                    logger.warning("已推送失败告警 job=%s -> %s", updated.job_id, delivered)
                else:
                    logger.warning("失败告警未能送达（未配置 Webhook 或全部推送失败）")
            elif updated.last_status == "completed" and previous.last_status == "failed":
                delivered = sender.send_text(build_recovery_alert(updated), settings)
                if delivered:
                    logger.info("已推送恢复通知 job=%s", updated.job_id)
        except Exception:
            # 告警通道故障绝不能反过来搞挂调度器，但必须留痕
            logger.warning("告警推送失败（不影响调度）", exc_info=True)

    return _alert
