"""队列执行者：认领排队中的运行并把它跑完。

worker 此前只有"定时调度"一个职责；"执行用户提交的运行"是另一件事——它由队列驱动、
按需开工、与 cron 无关。两者可以装在同一个常驻进程里，但依赖不同：执行者要流水线装配，
调度器要 agent 栈，所以本模块刻意不认识 `Settings`（参数由调用方给），既好测，
也让 worker 能按需要只装配其中一半。

**空队列的代价是每 `poll_seconds` 秒一次索引查询**（默认 2 秒）。刻意不做指数退避：
退避会让"提交之后多久被接手"随空闲时长漂移（最坏要等一个退避上限），而本机 SQLite 上
这点空转成本可以忽略。等到轮询真的成为瓶颈，那也正是该换成独立队列的信号——
而不是先加个退避把信号盖住。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app_review_insights.models import RunRecord, RunStatus
from app_review_insights.monitor.health import HealthState
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage import lease
from app_review_insights.storage.repository import RunRepository

logger = logging.getLogger("agent-worker")


def execute_once(
    repository: RunRepository,
    services_factory: Callable[[RunRecord], PipelineServices],
    *,
    lease_timeout_seconds: float,
    max_duration_seconds: float | None = None,
) -> RunRecord | None:
    """认领并执行一条排队中的运行；队列空（或全被持有）时返回 `None`。

    认领与执行分成两步是有意的：`claim_next_run` 先在事务里把运行**据为己有**，
    之后 `run_claimed` 不再做"能不能接管"的判断——租约按进程判定，区分不了线程，
    再判一次只会把"我自己刚认领的"误判成"别人在写"。
    """
    claimed = repository.claim_next_run(
        owner=lease.make_owner(),
        timeout_seconds=lease_timeout_seconds,
    )
    if claimed is None:
        return None
    logger.info("认领到运行 %s（下一阶段 %s）", claimed.run_id, claimed.current_stage.value)
    try:
        services = services_factory(claimed)
    except Exception as exc:
        # 起不来就如实判失败：留在"排队中且被我持有"的状态，既不会被别人接手，
        # 又会在重启后被反复重试（毒丸）。
        repository.fail_run(claimed.run_id, error=str(exc))
        raise
    orchestrator = AnalysisOrchestrator(services, max_duration_seconds=max_duration_seconds)
    return orchestrator.run_claimed(claimed)


def serve_forever(
    repository: RunRepository,
    services_factory: Callable[[RunRecord], PipelineServices],
    state: HealthState,
    stop_event: threading.Event,
    *,
    poll_seconds: float,
    lease_timeout_seconds: float,
    max_duration_seconds: float | None = None,
) -> None:
    """常驻循环：认领 → 执行 → 记结果；空队列就等着，下一轮再看。"""
    while not stop_event.is_set():
        try:
            executed = execute_once(
                repository,
                services_factory,
                lease_timeout_seconds=lease_timeout_seconds,
                max_duration_seconds=max_duration_seconds,
            )
        except Exception:
            # 单条运行出错不能弄死整个执行者：下一个提交还得有人接
            logger.exception("执行排队运行时出错（继续下一轮）")
            state.record_job(success=False)
            stop_event.wait(poll_seconds)
            continue

        if executed is None:
            stop_event.wait(poll_seconds)
            continue

        if executed.status is RunStatus.COMPLETED:
            state.record_job(success=True)
        elif executed.status is RunStatus.FAILED:
            state.record_job(success=False)
        # 其余状态（WAITING / PARTIAL / CANCELLED）不计入任务成败：它们不是"任务失败"，
        # 混进去会让 AriJobFailures 告警与任务成功率 SLI 一起失去意义。
        logger.info("运行 %s 结束：%s", executed.run_id, executed.status.value)


def start_queue_executor(
    repository: RunRepository,
    services_factory: Callable[[RunRecord], PipelineServices],
    state: HealthState,
    stop_event: threading.Event,
    *,
    poll_seconds: float,
    lease_timeout_seconds: float,
    max_duration_seconds: float | None = None,
) -> threading.Thread:
    """把执行循环放进守护线程；返回线程对象供调用方观测。"""
    thread = threading.Thread(
        target=serve_forever,
        args=(repository, services_factory, state, stop_event),
        kwargs={
            "poll_seconds": poll_seconds,
            "lease_timeout_seconds": lease_timeout_seconds,
            "max_duration_seconds": max_duration_seconds,
        },
        name="ari-queue-executor",
        daemon=True,
    )
    thread.start()
    return thread
