"""worker 的存活/就绪探针与极简指标端点（标准库实现，无额外依赖）。

worker 是纯 Python 进程、没有 HTTP 入口：一个"活着但卡死"的 worker
（调度器线程挂了、任务僵住）此前无法被任何外部手段发现，Docker 也无从重启它。

端点：

- `/healthz` 存活探针：进程还在就返回 200。**刻意不检查依赖**——否则依赖抖动
  会引发无意义的重启。
- `/readyz` 就绪探针：调度器就绪才 200，否则 503。
- `/metrics` Prometheus 文本格式的指标（含阶段耗时与模型耗时的分位数）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("ari-health")

#: 暴露哪些分位数。0.5/0.95 足够回答"典型多慢"和"最慢的那批多慢"，
#: 再加 P99 只会让样本量小的阶段给出更不稳的数。
_QUANTILES = ((0.5, "0.5"), (0.95, "0.95"))


def render_duration_family(
    name: str,
    help_text: str,
    groups: dict[str, dict[str, float]],
) -> list[str]:
    """把一个"按阶段分组的耗时摘要"渲染成 Prometheus 文本。

    用秒而不是毫秒：Prometheus 的惯例是基本单位（秒），面板上的换算交给展示层。
    """
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for label, stats in sorted(groups.items()):
        count = int(stats.get("count", 0) or 0)
        if not count:
            continue
        for quantile, text in _QUANTILES:
            key = f"p{int(quantile * 100)}"
            value = float(stats.get(key, 0.0)) / 1000
            lines.append(f'{name}{{stage="{label}",quantile="{text}"}} {value:.6f}')
        lines.append(f'{name}_max{{stage="{label}"}} {float(stats.get("max", 0.0)) / 1000:.6f}')
        lines.append(f'{name}_count{{stage="{label}"}} {count}')
    return lines


class HealthState:
    """进程内共享的健康与指标状态（线程安全）。

    `duration_stats` 是一个可选回调，返回 `{"stage": {...}, "model": {...}}` 形态的
    耗时摘要（来自检查点库）。它**可以失败**——指标端点绝不能因为数据库忙就 500，
    那会让"抓不到指标"变成"服务看起来挂了"。
    """

    def __init__(
        self,
        duration_stats: Callable[[], dict[str, Any]] | None = None,
        queue_stats: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._duration_stats = duration_stats
        self._queue_stats = queue_stats
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._ready = False
        self._jobs_completed = 0
        self._jobs_failed = 0
        self._last_job_at: float | None = None

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = ready

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def record_job(self, *, success: bool) -> None:
        with self._lock:
            if success:
                self._jobs_completed += 1
            else:
                self._jobs_failed += 1
            self._last_job_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self._ready,
                "uptime_seconds": time.time() - self._started_at,
                "jobs_completed": self._jobs_completed,
                "jobs_failed": self._jobs_failed,
                "last_job_timestamp_seconds": self._last_job_at or 0,
            }

    def duration_families(self) -> list[str]:
        """耗时指标段。取数失败时返回一句注释而不是抛异常——prometheus 抓到的短一段，
        总好过整个端点 500（那会被读成"服务不健康"）。"""
        if self._duration_stats is None:
            return []
        try:
            stats = self._duration_stats()
        except Exception:
            logger.warning("耗时指标取数失败（端点仍可用）", exc_info=True)
            return ["# 耗时指标暂不可用（取数失败，详见日志）"]
        return render_duration_family(
            "ari_stage_duration_seconds",
            "各阶段耗时（秒）；样本少时 P95 接近最大值，属正常",
            stats.get("stage") or {},
        ) + render_duration_family(
            "ari_model_latency_seconds",
            "模型调用耗时（秒），按阶段分组；与阶段耗时对照可分清慢在模型还是别处",
            stats.get("model") or {},
        )

    def queue_families(self) -> list[str]:
        """队列指标：积压多少、有没有人在消费。

        只有在**知道答案的进程**里才输出（worker 与 API 给得出，纯 web 进程给不出）。
        宁可整段不出现，也不要输出一个看着像 0 的假值——告警会当真。
        """
        if self._queue_stats is None:
            return []
        try:
            stats = self._queue_stats()
        except Exception:
            logger.warning("队列指标取数失败（端点仍可用）", exc_info=True)
            return ["# 队列指标暂不可用（取数失败，详见日志）"]
        return [
            "# HELP ari_queue_depth 排队中、等待被认领的运行数",
            "# TYPE ari_queue_depth gauge",
            f"ari_queue_depth {int(stats.get('depth', 0))}",
            "# HELP ari_queue_executor_seen 最近有队列执行者报到（1/0）",
            "# TYPE ari_queue_executor_seen gauge",
            f"ari_queue_executor_seen {1 if stats.get('executor_seen') else 0}",
            "# HELP ari_queue_oldest_wait_seconds 排队中最久的一条已等待秒数",
            "# TYPE ari_queue_oldest_wait_seconds gauge",
            f"ari_queue_oldest_wait_seconds {float(stats.get('oldest_wait_seconds', 0.0)):.3f}",
        ]

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        lines = [
            "# HELP ari_worker_up worker 进程存活（能被抓取即为 1）",
            "# TYPE ari_worker_up gauge",
            "ari_worker_up 1",
            "# HELP ari_worker_ready 调度器就绪",
            "# TYPE ari_worker_ready gauge",
            f"ari_worker_ready {1 if snap['ready'] else 0}",
            "# HELP ari_worker_uptime_seconds 进程运行时长",
            "# TYPE ari_worker_uptime_seconds gauge",
            f"ari_worker_uptime_seconds {snap['uptime_seconds']:.3f}",
            "# HELP ari_worker_jobs_total 已执行任务数",
            "# TYPE ari_worker_jobs_total counter",
            f'ari_worker_jobs_total{{result="completed"}} {snap["jobs_completed"]}',
            f'ari_worker_jobs_total{{result="failed"}} {snap["jobs_failed"]}',
            "# HELP ari_worker_last_job_timestamp_seconds 最近一次任务结束时间",
            "# TYPE ari_worker_last_job_timestamp_seconds gauge",
            f"ari_worker_last_job_timestamp_seconds {snap['last_job_timestamp_seconds']:.3f}",
        ]
        lines.extend(self.duration_families())
        lines.extend(self.queue_families())
        return "\n".join(lines) + "\n"


def build_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    """构造绑定到给定状态的请求处理器（供测试直接注入状态）。"""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "ari-worker-health"

        def _respond(self, code: int, body: str, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _respond_json(self, code: int, data: dict[str, Any]) -> None:
            self._respond(code, json.dumps(data, ensure_ascii=False), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._respond_json(200, {"status": "ok"})
            elif path == "/readyz":
                snapshot = state.snapshot()
                self._respond_json(200 if snapshot["ready"] else 503, snapshot)
            elif path == "/metrics":
                self._respond(
                    200,
                    state.render_prometheus(),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
            else:
                self._respond_json(404, {"error": "not found"})

        def log_message(self, *args: Any) -> None:
            """静音默认的逐请求 stderr 输出，避免探针刷爆日志。"""

    return _Handler


def start_health_server(
    state: HealthState,
    host: str = "0.0.0.0",
    port: int = 9100,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """在守护线程里启动健康/指标服务；`port=0` 表示由系统分配可用端口。"""
    server = ThreadingHTTPServer((host, port), build_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="ari-health", daemon=True)
    thread.start()
    return server, thread
