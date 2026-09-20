"""常驻 worker：启动调度器并保持进程存活（Docker worker 容器入口）。

用法：python -m app_review_insights.monitor.worker
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from typing import Any

from app_review_insights.config import Settings, load_settings
from app_review_insights.factory import build_agent_stack, build_pipeline_services
from app_review_insights.llm.recording import input_fingerprint
from app_review_insights.logging_setup import configure_logging
from app_review_insights.maintenance import start_maintenance_loop
from app_review_insights.models import Review, RunRecord
from app_review_insights.monitor.alerts import make_status_alerter
from app_review_insights.monitor.health import HealthState, start_health_server
from app_review_insights.monitor.queue_executor import start_queue_executor
from app_review_insights.monitor.report import build_report
from app_review_insights.monitor.scheduler import MonitorScheduler
from app_review_insights.monitor.webhook import WebhookSender
from app_review_insights.pipeline.orchestrator import PipelineServices
from app_review_insights.storage.repository import RunRepository
from app_review_insights.tracing import configure_tracing

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


def make_queue_services_factory(settings: Settings) -> Callable[[RunRecord], PipelineServices]:
    """按运行装配流水线。

    输入指纹取自**提交时落盘的评论**：回放模式要靠它命中录制件，而执行者手里没有调用者
    的内存，只能从库里重建。
    """

    def build(run: RunRecord) -> PipelineServices:
        payload = RunRepository(settings.database_path).get_inputs(run.run_id)
        fingerprint = (
            input_fingerprint([Review.model_validate(item) for item in payload])
            if payload
            else None
        )
        return build_pipeline_services(settings, input_fingerprint=fingerprint)

    return build


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


def serve(
    scheduler: MonitorScheduler | None,
    stop_event: threading.Event,
    executor: threading.Thread | None = None,
    grace_seconds: float = 10.0,
) -> None:
    """阻塞直到收到停止请求，然后收尾：关调度器、给执行者一点时间跑完手上那条运行。"""
    try:
        while not stop_event.wait(1):
            pass
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=True)
        if executor is not None and executor.is_alive():
            # 跑在手上的运行不该被腰斩：要么跑完，要么在检查点上被硬杀——两条路都可续跑
            # （T2 的保证）。等的上限与 Docker 默认的 stop 宽限期一致。
            executor.join(timeout=grace_seconds)
            if executor.is_alive():
                logger.warning("队列执行者仍在跑一条运行；本次退出会在检查点上留下可续跑的运行")
        logger.info("worker 已停止")


def main() -> None:
    # 先用兜底日志（load_settings 本身可能失败），拿到配置后再按 LOG_FORMAT 重配
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("worker 启动 log_format=%s", settings.log_format)
    if configure_tracing():
        logger.info("三层追踪已启用（OTel）")
    if not settings.scheduler_enabled and not settings.run_queue_enabled:
        logger.warning(
            "SCHEDULER_ENABLED 与 RUN_QUEUE_ENABLED 均为 false：worker 没有可做的事，退出"
        )
        return

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    if settings.maintenance_enabled:
        start_maintenance_loop(settings, stop_event)
        logger.info("数据维护已启用：每 %s 秒一次", settings.maintenance_interval_seconds)

    # 指标端点读检查点库里的阶段耗时；取数失败只会让那一段缺失，不影响探针
    run_repository = RunRepository(settings.database_path)
    state = HealthState(
        duration_stats=lambda: {
            "stage": run_repository.stage_timing_summary(),
            "model": run_repository.model_latency_summary(),
        }
    )
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

    executor = None
    if settings.run_queue_enabled:
        executor = start_queue_executor(
            run_repository,
            make_queue_services_factory(settings),
            state,
            stop_event,
            poll_seconds=settings.run_queue_poll_seconds,
            lease_timeout_seconds=settings.lease_timeout_seconds,
            max_duration_seconds=settings.run_max_duration_seconds or None,
        )
        logger.info("运行队列执行者已启用（每 %s 秒轮询一次）", settings.run_queue_poll_seconds)

    scheduler = None
    if settings.scheduler_enabled:
        stack = build_agent_stack(settings)
        webhook = WebhookSender()
        scheduler = MonitorScheduler(
            agent_repository=stack.agent_repository,
            run_job_fn=track_job_outcome(make_run_job_fn(stack, settings), state),
            on_job_result=make_status_alerter(webhook, settings),
        )
        scheduler.start()
        logger.info("scheduler started with %s jobs", len(scheduler._scheduler.get_jobs()))
    else:
        # 只消费队列的 worker 不必装配 agent 栈——那是调度器才需要的东西
        logger.info("SCHEDULER_ENABLED=false：本进程只执行队列中的运行")

    state.set_ready(True)
    try:
        serve(scheduler, stop_event, executor)
    finally:
        state.set_ready(False)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
