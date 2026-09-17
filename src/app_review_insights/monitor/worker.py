"""常驻 worker：启动调度器并保持进程存活（Docker worker 容器入口）。

用法：python -m app_review_insights.monitor.worker
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from typing import Any

from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack
from app_review_insights.monitor.alerts import make_status_alerter
from app_review_insights.monitor.health import HealthState, start_health_server
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


def track_job_outcome(run_job: Callable[..., Any], state: HealthState) -> Callable[..., Any]:
    """把任务成败计入健康状态，供 `/metrics` 暴露。

    异常继续向上抛，由 `MonitorScheduler` 带 traceback 记录——这里只负责计数。
    """

    def _wrapper(goal: str, app_url: str, require_approval: bool = False) -> None:
        try:
            run_job(goal, app_url, require_approval)
        except Exception:
            state.record_job(success=False)
            raise
        state.record_job(success=True)

    return _wrapper


def install_signal_handlers(stop_event: threading.Event) -> None:
    """把 SIGTERM / SIGINT 转成 `stop_event.set()`，让 worker 有机会优雅退出。

    Docker 停止容器发的是 SIGTERM；只捕获 KeyboardInterrupt 会让每次
    `docker compose down` 都变成硬杀。
    """

    def _request_stop(signum, _frame) -> None:
        logger.info("收到信号 %s，停止接受新任务并等待在跑任务结束", signum)
        stop_event.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
        except ValueError:
            # 非主线程无法注册信号处理器；此时优雅关闭依赖外部传入的 stop_event
            logger.debug("无法注册 %s 处理器（非主线程）", name)


def serve(scheduler: MonitorScheduler, stop_event: threading.Event) -> None:
    """阻塞直到收到停止请求，然后等待在跑任务结束后关闭调度器。"""
    try:
        while not stop_event.wait(1):
            pass
    finally:
        scheduler.shutdown(wait=True)
        logger.info("worker 已停止")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    if not settings.scheduler_enabled:
        logger.warning("SCHEDULER_ENABLED=false，worker 退出（请在 .env 开启）")
        return

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    state = HealthState()
    server, _health_thread = start_health_server(
        state,
        host=settings.worker_health_host,
        port=settings.worker_health_port,
    )
    logger.info(
        "健康/指标端点已启动 %s:%s（/healthz /readyz /metrics）",
        settings.worker_health_host,
        server.server_address[1],
    )

    stack = build_agent_stack(settings)
    webhook = WebhookSender()
    scheduler = MonitorScheduler(
        agent_repository=stack.agent_repository,
        run_job_fn=track_job_outcome(make_run_job_fn(stack, settings), state),
        on_job_result=make_status_alerter(webhook, settings),
    )
    scheduler.start()
    state.set_ready(True)
    logger.info("scheduler started with %s jobs", len(scheduler._scheduler.get_jobs()))
    try:
        serve(scheduler, stop_event)
    finally:
        state.set_ready(False)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
