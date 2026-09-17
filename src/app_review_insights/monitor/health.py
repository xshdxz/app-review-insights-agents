"""worker 的存活/就绪探针与极简指标端点（标准库实现，无额外依赖）。

worker 是纯 Python 进程、没有 HTTP 入口：一个"活着但卡死"的 worker
（调度器线程挂了、任务僵住）此前无法被任何外部手段发现，Docker 也无从重启它。

端点：

- `/healthz` 存活探针：进程还在就返回 200。**刻意不检查依赖**——否则依赖抖动
  会引发无意义的重启。
- `/readyz` 就绪探针：调度器就绪才 200，否则 503。
- `/metrics` Prometheus 文本格式的极简指标。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthState:
    """进程内共享的健康与指标状态（线程安全）。"""

    def __init__(self) -> None:
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
