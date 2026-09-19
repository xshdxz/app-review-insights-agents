"""随机时刻硬杀：在阶段边界**之外**的任意瞬间杀死进程。

确定性矩阵覆盖"检查点刚落盘"这一瞬间；这里覆盖的是阶段**内部**——
模型调用途中、写事务途中，以及各种没有名字的瞬间。

两个必要的前置条件，缺一不可：

  1. **注入模型调用延迟**（ARI_REQUEST_DELAY_MS）：回放没有网络往返，整条流水线
     只要几百毫秒，"杀在调用途中"根本定不了时；不注入延迟，这一支会退化成空测。
  2. **空洞守卫**：断言子进程没打印摘要、库里状态仍是 running。否则子进程若在被杀
     之前已经跑完，resume() 会看到 completed 直接返回、所有断言全绿——什么都没测到。
     实测踩到过：save4 / save9 两个参数就是这样假绿的。

与确定性矩阵的一处口径差异（刻意）：允许重做**被中断阶段**的模型调用——
没落盘的结果不算已完成的工作。"已落盘的工作不被重做"由矩阵精确断言。
"""

from __future__ import annotations

import child
import oracle
import pytest
import scenario

#: 每次模型调用注入的延迟，让"杀在调用途中"成为可控事件
REQUEST_DELAY_MS = 120

#: (在第 N 个检查点落盘后, 再随机等多少秒才杀)。全部取在流水线中段，
#: 保证被杀时仍有大量工作未完成。
KILL_POINTS = scenario.KILL_POINTS


@pytest.mark.reliability
@pytest.mark.parametrize(
    ("kill_after_saves", "jitter"),
    KILL_POINTS,
    ids=[scenario.kill_label(saves, jitter) for saves, jitter in KILL_POINTS],
)
def test_kill_at_arbitrary_moment_then_resume(tmp_path, baseline, kill_after_saves, jitter):
    db_path = tmp_path / "killed.sqlite3"
    crash_log = tmp_path / "crash-requests.log"
    resume_log = tmp_path / "resume-requests.log"
    progress = tmp_path / "progress.log"

    result, killed = child.spawn_and_kill(
        db_path,
        kill_after_saves=kill_after_saves,
        jitter_seconds=jitter,
        extra_env={
            "ARI_REQUEST_LOG": str(crash_log),
            "ARI_REQUEST_DELAY_MS": str(REQUEST_DELAY_MS),
            "ARI_PROGRESS_LOG": str(progress),
        },
    )

    # 无论杀在哪一刻，库里都不能有半截数据
    oracle.assert_payloads_wellformed(db_path)

    run_ids = oracle.run_ids(db_path)
    assert run_ids, "杀之前应当已经创建了运行记录（第 3 个检查点远晚于运行创建）"

    # ── 空洞守卫：必须是"真被杀在半途"，否则这一轮什么都没测到 ──
    observed = child.count_lines(progress)
    assert killed, (
        f"子进程在第 {kill_after_saves} 个检查点后 {jitter}s 内就自己跑完了"
        f"（已落盘 {observed} 个检查点），本轮没有真正测到硬杀"
    )
    assert result.summary is None, "子进程在被杀前已经打印摘要 ⇒ 它跑完了，本轮无效"
    assert oracle.run_status(db_path) == "running", (
        f"被杀时运行状态应当是 running，实际 {oracle.run_status(db_path)} ⇒ 本轮无效"
    )
    assert observed >= kill_after_saves

    crash_keys = set(child.request_keys(crash_log))

    resumed = child.spawn(
        db_path,
        resume_run_id=run_ids[0],
        extra_env={"ARI_REQUEST_LOG": str(resume_log)},
    )
    summary = resumed.require_ok()
    assert summary["status"] == "completed", (
        f"任意时刻被杀后未能续跑完成（第 {kill_after_saves} 个检查点后被杀）：{summary}"
    )

    difference = oracle.describe_difference(oracle.output_snapshot(db_path), baseline.snapshot)
    assert not difference, (
        f"任意时刻被杀后未能收敛（第 {kill_after_saves} 个检查点后被杀）：\n"
        + "\n".join(difference)
    )

    baseline_keys = set(baseline.requests)
    resumed_keys = child.request_keys(resume_log)
    assert set(resumed_keys) <= baseline_keys, "续跑请求了录制里不存在的内容"
    assert crash_keys | set(resumed_keys) == baseline_keys, (
        f"全部工作最终没有被做完：缺失 {sorted(baseline_keys - (crash_keys | set(resumed_keys)))}"
    )
    assert len(resumed_keys) == len(set(resumed_keys)), "续跑进程内部重复请求了同一内容"
