"""worker 进程生命周期：信号处理与优雅关闭。

Docker 停止容器发的是 **SIGTERM**（不是 KeyboardInterrupt）。若只捕获
KeyboardInterrupt，每次 `docker compose down` 都会硬杀正在跑的分析。
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock

import pytest

from app_review_insights.monitor.health import HealthState
from app_review_insights.monitor.worker import (
    install_signal_handlers,
    serve,
    track_job_outcome,
)


def _capture_handlers(monkeypatch) -> dict:
    registered: dict = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))
    return registered


def test_install_signal_handlers_registers_sigterm_and_sigint(monkeypatch):
    registered = _capture_handlers(monkeypatch)

    install_signal_handlers(threading.Event())

    assert signal.SIGTERM in registered
    assert signal.SIGINT in registered


def test_sigterm_handler_requests_graceful_stop(monkeypatch):
    registered = _capture_handlers(monkeypatch)
    stop_event = threading.Event()

    install_signal_handlers(stop_event)
    assert not stop_event.is_set()

    registered[signal.SIGTERM](signal.SIGTERM, None)

    assert stop_event.is_set()


def test_serve_shuts_down_scheduler_waiting_for_running_jobs():
    """停止时必须 shutdown(wait=True)，否则在跑的任务被直接砍断。"""
    scheduler = MagicMock()
    stop_event = threading.Event()
    stop_event.set()

    serve(scheduler, stop_event)

    scheduler.shutdown.assert_called_once_with(wait=True)


def test_track_job_outcome_counts_success():
    state = HealthState()
    wrapped = track_job_outcome(lambda *args, **kwargs: None, state)

    wrapped("目标", "https://example.invalid", False)

    snapshot = state.snapshot()
    assert snapshot["jobs_completed"] == 1
    assert snapshot["jobs_failed"] == 0
    assert snapshot["last_job_timestamp_seconds"] > 0


def test_track_job_outcome_counts_failure_and_reraises():
    """计数之后必须继续抛，否则 MonitorScheduler 就记不到 traceback 了。"""
    state = HealthState()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("采集失败")

    wrapped = track_job_outcome(_boom, state)

    with pytest.raises(RuntimeError, match="采集失败"):
        wrapped("目标", "https://example.invalid", False)

    snapshot = state.snapshot()
    assert snapshot["jobs_failed"] == 1
    assert snapshot["jobs_completed"] == 0


def test_serve_blocks_until_stop_requested():
    scheduler = MagicMock()
    stop_event = threading.Event()

    thread = threading.Thread(target=serve, args=(scheduler, stop_event), daemon=True)
    thread.start()
    stop_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    scheduler.shutdown.assert_called_once_with(wait=True)
