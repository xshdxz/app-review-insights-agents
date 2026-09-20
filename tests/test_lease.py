"""运行租约的规则。

端到端矩阵（tests/reliability/）验证的是**系统行为**——真杀进程、真续跑；这里验证的是
**规则本身**：什么算"被持有"、什么算"可以接管"。两层都要有：规则单测跑得快、失败信息
直指哪条规则不对；矩阵则证明这些规则在真实崩溃下确实生效。
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime, timedelta

import pytest

from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType, Stage
from app_review_insights.storage import lease

HOST = socket.gethostname()
TIMEOUT = 300.0


def _run(
    status: RunStatus = RunStatus.RUNNING,
    *,
    lease_owner: str | None = None,
    heartbeat_age_seconds: float = 0.0,
    now: datetime | None = None,
) -> RunRecord:
    moment = now or datetime.now(UTC)
    heartbeat = (
        None if heartbeat_age_seconds is None else moment - timedelta(seconds=heartbeat_age_seconds)
    )
    stage = Stage.COMPLETE if status == RunStatus.COMPLETED else Stage.ANALYZE_BATCHES
    return RunRecord(
        run_id="r1",
        request=AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        current_stage=stage,
        status=status,
        lease_owner=lease_owner,
        heartbeat_at=heartbeat,
        created_at=moment,
        updated_at=heartbeat or moment,
    )


# ── 持有者身份 ───────────────────────────────────────────────────────────────


def test_make_owner_is_stable_within_a_process_and_identifies_this_pid():
    first = lease.make_owner()

    assert first == lease.make_owner(), "同一个进程里的两次调用必须是同一个持有者"
    host, pid, token = first.split(":")
    assert host == HOST
    assert int(pid) == os.getpid()
    assert token


@pytest.mark.parametrize("owner", [None, "", "no-colon", "host:not-a-pid"])
def test_owner_is_alive_returns_none_for_unparseable_owners(owner):
    assert lease.owner_is_alive(owner) is None


def test_owner_is_alive_returns_none_for_another_host():
    """跨主机判不出来 ⇒ 调用方必须退回心跳窗口，而不是当成"已经死了"。"""
    assert lease.owner_is_alive("some-other-host:1234:abcd") is None


def test_owner_is_alive_is_true_for_this_process():
    assert lease.owner_is_alive(lease.make_owner()) is True


def test_owner_is_alive_follows_the_pid_probe(monkeypatch):
    monkeypatch.setattr(lease, "_pid_exists", lambda pid: False)
    assert lease.owner_is_alive(f"{HOST}:999999:abcd") is False

    monkeypatch.setattr(lease, "_pid_exists", lambda pid: None)
    assert lease.owner_is_alive(f"{HOST}:999999:abcd") is None


# ── is_held ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.WAITING]
)
def test_is_held_is_false_for_states_that_are_not_executing(status):
    assert lease.is_held(_run(status, lease_owner="x:1:y"), datetime.now(UTC), TIMEOUT) is False


def test_is_held_is_true_while_the_owner_process_is_alive(monkeypatch):
    """持有者活着 ⇒ 一律算被持有，哪怕心跳很旧：宁可保守，也不能出现两个执行者。"""
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: True)
    run = _run(lease_owner="x:1:y", heartbeat_age_seconds=99999)

    assert lease.is_held(run, datetime.now(UTC), TIMEOUT) is True


def test_is_held_is_false_when_the_owner_process_is_gone(monkeypatch):
    """崩溃恢复的判定核心：持有者进程已不存在 ⇒ 立刻可以接管。"""
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: False)
    run = _run(lease_owner="x:1:y", heartbeat_age_seconds=0)

    assert lease.is_held(run, datetime.now(UTC), TIMEOUT) is False


def test_is_held_falls_back_to_heartbeat_when_liveness_is_unknown(monkeypatch):
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: None)

    fresh = _run(lease_owner="x:1:y", heartbeat_age_seconds=1)
    stale = _run(lease_owner="x:1:y", heartbeat_age_seconds=TIMEOUT + 60)

    assert lease.is_held(fresh, datetime.now(UTC), TIMEOUT) is True
    assert lease.is_held(stale, datetime.now(UTC), TIMEOUT) is False


def test_is_held_falls_back_to_updated_at_without_any_lease(monkeypatch):
    """旧记录没有租约字段，只能看"最近一次更新是否够新"。"""
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: None)

    fresh = _run(lease_owner=None, heartbeat_age_seconds=None)
    stale = _run(lease_owner=None, heartbeat_age_seconds=None)
    stale = stale.model_copy(update={"updated_at": datetime.now(UTC) - timedelta(seconds=99999)})

    assert lease.is_held(fresh, datetime.now(UTC), TIMEOUT) is True
    assert lease.is_held(stale, datetime.now(UTC), TIMEOUT) is False


# ── blocks_new_run / can_resume ──────────────────────────────────────────────


def test_waiting_run_blocks_new_runs_even_without_a_live_owner(monkeypatch):
    """停在检查点上等模型恢复的运行仍然占着这个 App——沿用既有互斥语义。"""
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: False)

    assert lease.blocks_new_run(_run(RunStatus.WAITING), datetime.now(UTC), TIMEOUT) is True


def test_queued_run_without_a_lease_is_not_held(monkeypatch):
    """排队中的运行**没有执行者**：没有租约就不算被持有。

    否则它会被按心跳窗口判成"仍有人在写"，任何执行者都不敢认领——队列就死了。
    """
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: None)

    queued = _run(RunStatus.PENDING, lease_owner=None, heartbeat_age_seconds=0)

    assert lease.is_held(queued, datetime.now(UTC), TIMEOUT) is False
    assert lease.can_resume(queued, datetime.now(UTC), TIMEOUT) is True


def test_queued_run_still_blocks_a_second_run_for_the_same_app(monkeypatch):
    """ "没人持有"不等于"这个 App 可以再来一次"。

    排着队的运行同样占位：否则同一个 App 会被排两次队，模型额度烧两份。
    """
    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: None)

    queued = _run(RunStatus.PENDING, lease_owner=None, heartbeat_age_seconds=0)

    assert lease.is_held(queued, datetime.now(UTC), TIMEOUT) is False
    assert lease.blocks_new_run(queued, datetime.now(UTC), TIMEOUT) is True


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PARTIAL])
def test_terminal_runs_do_not_block_new_runs(status):
    assert lease.blocks_new_run(_run(status), datetime.now(UTC), TIMEOUT) is False


@pytest.mark.parametrize("status", [RunStatus.WAITING, RunStatus.TIMED_OUT])
def test_waiting_and_timed_out_runs_are_always_resumable(status):
    assert lease.can_resume(_run(status, lease_owner="x:1:y"), datetime.now(UTC), TIMEOUT) is True


def test_running_run_is_resumable_only_after_the_owner_is_gone(monkeypatch):
    now = datetime.now(UTC)
    run = _run(lease_owner="x:1:y")

    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: True)
    assert lease.can_resume(run, now, TIMEOUT) is False, "持有者还活着时绝不能接管"

    monkeypatch.setattr(lease, "owner_is_alive", lambda owner: False)
    assert lease.can_resume(run, now, TIMEOUT) is True, "持有者已消失时必须允许接管"


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PARTIAL])
def test_terminal_runs_are_not_resumable(status):
    assert lease.can_resume(_run(status), datetime.now(UTC), TIMEOUT) is False
