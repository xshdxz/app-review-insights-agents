"""崩溃矩阵：在阶段边界与批次边界上真实杀死进程，再用同一 run_id 续跑。

为什么必须是真子进程 + 真死亡：进程内注入异常会被 except/finally 接住，
证明不了"检查点真的落盘了"。只有真实进程死亡才算数。

五条断言：
  1. 崩溃确实生效（进程以 137 退出，且没走到打印摘要那一步）；
  2. 负向——崩溃点之后的阶段输出必须不存在（否则"续跑正确"可能只是因为根本没崩）；
  3. 原子性——已落盘的每个阶段输出都能被对应的 Pydantic 模型校验；
  4. 收敛性——续跑后的阶段输出与"一次跑完"逐字节相同；
  5. 不重复计费——崩溃进程 + 续跑进程的模型请求总数 == 一次跑完的请求数。

场景枚举在 scenario.py（文档一致性测试共用同一来源）。
"""

from __future__ import annotations

import child
import oracle
import pytest
import scenario

CRASH_POINTS = scenario.crash_points()


@pytest.mark.reliability
@pytest.mark.parametrize("point", CRASH_POINTS, ids=[point.label for point in CRASH_POINTS])
def test_crash_then_resume_converges(tmp_path, baseline, point: scenario.CrashPoint):
    db_path = tmp_path / "crashed.sqlite3"
    crash_log = tmp_path / "crash-requests.log"
    resume_log = tmp_path / "resume-requests.log"

    crashed = child.spawn(
        db_path,
        crash_after_stage=point.stage,
        crash_after_batch=point.batch,
        extra_env={"ARI_REQUEST_LOG": str(crash_log)},
    )
    assert crashed.crashed, (
        f"崩溃注入未生效（{point.label}）：returncode={crashed.returncode}\n"
        f"stdout={crashed.stdout[-1200:]}\nstderr={crashed.stderr[-1200:]}"
    )
    assert crashed.summary is None, "崩溃必须发生在打印摘要之前"

    # 负向断言：崩溃点之后的输出必须不存在
    assert oracle.stage_keys(db_path) == set(point.expected_keys), (
        f"{point.label} 崩溃点的已落盘输出与预期不符："
        f"实际={sorted(oracle.stage_keys(db_path))}，预期={sorted(point.expected_keys)}"
    )
    oracle.assert_payloads_wellformed(db_path)

    run_ids = oracle.run_ids(db_path)
    assert len(run_ids) == 1, f"崩溃后应当只有一条运行记录，实际 {len(run_ids)}"
    crashed_requests = child.request_keys(crash_log)

    resumed = child.spawn(
        db_path,
        resume_run_id=run_ids[0],
        extra_env={"ARI_REQUEST_LOG": str(resume_log)},
    )
    summary = resumed.require_ok()
    assert summary["status"] == "completed", f"续跑未完成（{point.label}）：{summary}"
    assert summary["coverage"] == 1, f"续跑覆盖率不为 1（{point.label}）：{summary}"

    difference = oracle.describe_difference(oracle.output_snapshot(db_path), baseline.snapshot)
    assert not difference, f"崩溃+续跑的结果与一次跑完不一致（{point.label}）：\n" + "\n".join(
        difference
    )

    resumed_requests = child.request_keys(resume_log)
    total = len(crashed_requests) + len(resumed_requests)
    assert total == len(baseline.requests), (
        f"续跑重复调用了已完成的模型工作（{point.label}）："
        f"崩溃进程 {len(crashed_requests)} 次 + 续跑进程 {len(resumed_requests)} 次 "
        f"= {total}，基线 {len(baseline.requests)} 次"
    )
