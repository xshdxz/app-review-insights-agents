"""可靠性用例的公共夹具。

子进程化的理由是**证据强度**：进程内注入异常会被 except/finally 接住，
证明不了"检查点真的落盘了"。只有真实进程死亡才算数。
"""

from __future__ import annotations

import time

import child
import oracle
import pytest


@pytest.fixture
def run_child():
    """运行器：run_child(db_path, crash_after_stage=..., resume_run_id=...)"""
    return child.spawn


@pytest.fixture(scope="session")
def baseline(tmp_path_factory) -> child.Baseline:
    """一次跑完的参照。会话级——整个 reliability 套件只跑一次，避免重复付启动开销。"""
    root = tmp_path_factory.mktemp("baseline")
    db_path = root / "baseline.sqlite3"
    log = root / "requests.log"

    started = time.monotonic()
    summary = child.spawn(db_path, extra_env={"ARI_REQUEST_LOG": str(log)}).require_ok()
    duration = time.monotonic() - started

    assert summary["status"] == "completed"
    assert summary["coverage"] == 1
    requests = child.request_keys(log)
    assert requests, "基线必须真的发生过模型请求，否则后续的计数断言毫无意义"

    return child.Baseline(
        db=db_path,
        snapshot=oracle.output_snapshot(db_path),
        requests=requests,
        duration_seconds=duration,
    )
