"""常驻 worker：启动调度器并保持进程存活（Docker worker 容器入口）。

用法：python -m app_review_insights.monitor.worker
"""

from __future__ import annotations

import logging
import time

from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack
from app_review_insights.monitor.report import build_report
from app_review_insights.monitor.scheduler import MonitorScheduler
from app_review_insights.monitor.webhook import WebhookSender
from app_review_insights.storage.repository import RunRepository

logger = logging.getLogger("agent-worker")


def make_run_job_fn(stack, settings):
    webhook = WebhookSender()

    def run_job(goal: str, app_url: str, require_approval: bool) -> None:
        agent_run = stack.orchestrator.run(goal, app_url, require_approval=require_approval)
        if agent_run.status.value in ("completed", "waiting_approval"):
            report = build_report(
                agent_run,
                RunRepository(settings.database_path),
                stack.agent_repository,
            )
            agent_run = agent_run.model_copy(update={"report_id": report.report_id})
            stack.agent_repository.save_agent_run(agent_run)
            if agent_run.status.value == "completed":
                delivered = webhook.send_report_by_id(
                    report.report_id, settings, stack.agent_repository
                )
                if delivered:
                    report = report.model_copy(update={"delivered_to": delivered})
                    stack.agent_repository.save_report(report)
                logger.info("job %s report=%s delivered=%s", app_url, report.report_id, delivered)

    return run_job


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    if not settings.scheduler_enabled:
        logger.warning("SCHEDULER_ENABLED=false，worker 退出（请在 .env 开启）")
        return
    stack = build_agent_stack(settings)
    scheduler = MonitorScheduler(
        agent_repository=stack.agent_repository,
        run_job_fn=make_run_job_fn(stack, settings),
    )
    scheduler.start()
    logger.info("scheduler started with %s jobs", len(scheduler._scheduler.get_jobs()))
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
