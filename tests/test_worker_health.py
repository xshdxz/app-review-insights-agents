"""worker 存活/就绪探针与指标端点。

worker 是纯 Python 进程、没有 HTTP 入口，一个"活着但卡死"的 worker
（调度器线程挂了、任务僵住）此前完全无法被发现。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from app_review_insights.monitor.health import HealthState, start_health_server


@pytest.fixture
def health_server():
    state = HealthState()
    server, thread = start_health_server(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def test_healthz_is_always_ok_while_process_is_alive(health_server):
    base, _ = health_server
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_readyz_is_ok_when_ready(health_server):
    base, state = health_server
    state.set_ready(True)
    status, body = _get(f"{base}/readyz")
    assert status == 200
    assert json.loads(body)["ready"] is True


def test_readyz_returns_503_when_not_ready(health_server):
    """调度器没跑起来时，worker 必须报告未就绪而不是假装健康。"""
    base, state = health_server
    state.set_ready(False)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}/readyz")
    assert excinfo.value.code == 503
    assert json.loads(excinfo.value.read().decode("utf-8"))["ready"] is False


def test_metrics_exposes_job_counters_in_prometheus_format(health_server):
    base, state = health_server
    state.record_job(success=True)
    state.record_job(success=True)
    state.record_job(success=False)

    status, body = _get(f"{base}/metrics")

    assert status == 200
    assert 'ari_worker_jobs_total{result="completed"} 2' in body
    assert 'ari_worker_jobs_total{result="failed"} 1' in body
    assert "ari_worker_up 1" in body
    assert "ari_worker_uptime_seconds" in body
    assert body.endswith("\n")


def test_metrics_records_last_job_timestamp(health_server):
    base, state = health_server
    state.record_job(success=True)
    _, body = _get(f"{base}/metrics")
    assert "ari_worker_last_job_timestamp_seconds" in body
    assert "ari_worker_last_job_timestamp_seconds 0" not in body


def test_unknown_path_returns_404(health_server):
    base, _ = health_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}/nope")
    assert excinfo.value.code == 404
