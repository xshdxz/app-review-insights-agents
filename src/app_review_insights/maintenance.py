"""数据维护：按保留策略清理历史数据并回收磁盘空间。

events 每推进一个阶段写一条、stage_outputs 保存每个批次的完整 JSON、
reports 每次监控新增一份——不清理的话磁盘只涨不跌。

用法：

    python -m app_review_insights.maintenance

worker 默认每天自动跑一次（`MAINTENANCE_ENABLED=true`）。

安全底线：只清理**终态**运行（completed / failed / partial）；
pending / running / waiting_for_model 一律保留，因为它们还可以续跑。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app_review_insights.config import Settings, load_settings
from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.repository import RunRepository

logger = logging.getLogger("ari-maintenance")


def run_maintenance(settings: Settings | None = None) -> dict[str, int]:
    """执行一轮清理与空间回收，返回各项删除计数。"""
    settings = settings or load_settings()
    run_repository = RunRepository(settings.database_path)
    agent_repository = AgentRepository(settings.agent_db_path)

    result: dict[str, Any] = {
        "events_removed": run_repository.prune_events(keep_per_run=settings.events_keep_per_run),
        "runs_removed": run_repository.prune_runs(older_than_days=settings.retention_days),
        "reports_removed": agent_repository.prune_reports(
            keep_per_app=settings.reports_keep_per_app
        ),
    }

    if any(result.values()):
        # 只有真的删过东西才值得付出 VACUUM 的代价（它会重写整个库文件）
        run_repository.vacuum()
        agent_repository.vacuum()
        result["vacuumed"] = 1
    else:
        result["vacuumed"] = 0

    logger.info("数据维护完成：%s", result)
    return result


def start_maintenance_loop(
    settings: Settings,
    stop_event: threading.Event,
) -> threading.Thread:
    """后台线程：按间隔周期性执行维护，直到 `stop_event` 被设置。

    维护失败只告警，绝不影响调度主流程。
    """

    def _loop() -> None:
        while not stop_event.wait(settings.maintenance_interval_seconds):
            try:
                run_maintenance(settings)
            except Exception:
                logger.warning("数据维护失败（不影响调度）", exc_info=True)

    thread = threading.Thread(target=_loop, name="ari-maintenance", daemon=True)
    thread.start()
    return thread


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    result = run_maintenance()
    print(result)


if __name__ == "__main__":
    main()
