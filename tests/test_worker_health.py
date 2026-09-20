"""worker 存活/就绪探针与指标端点。

worker 是纯 Python 进程、没有 HTTP 入口，一个"活着但卡死"的 worker
（调度器线程挂了、任务僵住）此前完全无法被发现。
"""

from __future__ import annotations

import json
import sqlite3
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


# ── 阶段耗时与模型耗时的分位数 ───────────────────────────────────────────────


def test_metrics_exposes_stage_and_model_duration_percentiles():
    """以秒暴露分位数——毫秒是内部口径，Prometheus 的基本单位是秒。"""
    state = HealthState(
        duration_stats=lambda: {
            "stage": {"clean": {"count": 4, "p50": 100.0, "p95": 400.0, "max": 500.0}},
            "model": {"plan": {"count": 2, "p50": 2000.0, "p95": 3000.0, "max": 3000.0}},
        }
    )

    text = state.render_prometheus()

    assert 'ari_stage_duration_seconds{stage="clean",quantile="0.5"} 0.100000' in text
    assert 'ari_stage_duration_seconds{stage="clean",quantile="0.95"} 0.400000' in text
    assert 'ari_stage_duration_seconds_max{stage="clean"} 0.500000' in text
    assert 'ari_stage_duration_seconds_count{stage="clean"} 4' in text
    assert 'ari_model_latency_seconds{stage="plan",quantile="0.95"} 3.000000' in text


def test_metrics_stay_available_when_duration_stats_fail():
    """取数失败只让那一段缺失，端点仍然 200。

    否则"数据库忙"会表现为"服务不健康"——一次正确的重启就会把好端端的进程杀掉。
    """

    def boom():
        raise sqlite3.OperationalError("database is locked")

    text = HealthState(duration_stats=boom).render_prometheus()

    assert "ari_worker_up 1" in text
    assert "耗时指标暂不可用" in text


def test_metrics_without_duration_stats_are_unchanged():
    text = HealthState().render_prometheus()

    assert "ari_worker_up 1" in text
    assert "ari_stage_duration_seconds" not in text


def test_stages_with_no_samples_are_skipped():
    state = HealthState(duration_stats=lambda: {"stage": {"clean": {"count": 0}}, "model": {}})

    text = state.render_prometheus()

    assert 'ari_stage_duration_seconds{stage="clean"' not in text


def test_web_process_starts_the_health_endpoint_only_once(monkeypatch):
    """Streamlit 每次交互都重跑脚本——不防就会起一堆服务器。"""
    from unittest.mock import Mock

    import app_review_insights.ui.main as ui_main
    from app_review_insights.config import Settings

    started: list[dict] = []
    monkeypatch.setattr(
        ui_main, "start_health_server", lambda state, **kwargs: started.append(kwargs)
    )
    monkeypatch.setattr(ui_main, "_health_server_started", False)

    settings = Settings(WEB_HEALTH_HOST="127.0.0.1", WEB_HEALTH_PORT=0)
    repository = Mock()

    ui_main._ensure_health_server(settings, repository)
    ui_main._ensure_health_server(settings, repository)

    assert len(started) == 1
    assert started[0] == {"host": "127.0.0.1", "port": 0}
