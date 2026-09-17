"""定时调度器：把 monitor_jobs 表中的 cron 任务注册进 APScheduler。

web 进程默认不启动（SCHEDULER_ENABLED=false）；worker 进程启动。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app_review_insights.storage.agent_repository import AgentRepository

logger = logging.getLogger("agent-scheduler")


class MonitorScheduler:
    def __init__(
        self,
        agent_repository: AgentRepository,
        run_job_fn: Callable[[str], Any],
        timezone: str = "UTC",
    ):
        self.agent_repository = agent_repository
        self.run_job_fn = run_job_fn
        self._scheduler = BackgroundScheduler(timezone=timezone)

    def start(self) -> None:
        self.refresh()
        if not self._scheduler.running:
            self._scheduler.start()

    def refresh(self) -> None:
        """按当前任务表重建全部定时任务（CRUD 后调用）。"""
        for job in self._scheduler.get_jobs():
            job.remove()
        for job in self.agent_repository.list_jobs():
            if not job.enabled:
                continue
            try:
                trigger = CronTrigger.from_crontab(job.cron)
            except ValueError:
                continue
            self._scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=f"job-{job.job_id}",
                replace_existing=True,
                misfire_grace_time=3600,
                args=[job.job_id],
            )

    def _run_job(self, job_id: str) -> None:
        job = self.agent_repository.get_job(job_id)
        try:
            self.run_job_fn(job.goal, job.app_url, job.require_approval)
            status = "completed"
        except Exception:
            # 必须带 traceback 记录：只写 last_status="failed" 会让故障原因彻底丢失，
            # 线上将无法判断是采集、模型还是校验环节出错。
            status = "failed"
            logger.exception(
                "监控任务执行失败 job_id=%s app_url=%s goal=%s",
                job_id,
                job.app_url,
                job.goal,
            )
        updated = job.model_copy(
            update={
                "last_run_at": datetime.now(UTC),
                "last_status": status,
                "updated_at": datetime.now(UTC),
            }
        )
        self.agent_repository.save_job(updated)

    def shutdown(self, wait: bool = True) -> None:
        """停止调度器。

        默认 `wait=True`：不再接受新任务，但等正在执行的任务跑完，
        避免进程退出把进行中的分析拦腰砍断（流水线虽有检查点可续跑，
        但被动中断会留下 `running` 状态的孤儿运行）。
        """
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
