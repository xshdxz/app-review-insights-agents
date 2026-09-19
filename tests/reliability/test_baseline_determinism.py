"""基线确定性：同一输入跑两遍，产物必须逐字节相同。

这是整个可靠性基座的地基——如果两次「一次跑完」的结果都不相同，
「崩溃后收敛到与一次跑完相同的结果」就无从比较。

同时验证两件与设计直接相关的事实：
  1. 回放运行的标注是 mode=recorded_live_run / is_live=false；
  2. 回放模式**不产生 model_usage 记录**（ReplayProvider 不走 usage_recorder），
     因此「续跑不重复调用模型」这条断言必须靠 provider 请求计数，不能查用量表。
"""

from __future__ import annotations

import oracle
import pytest


def _stage_names(db_path) -> set[str]:
    return {row["stage"] for row in oracle.stage_outputs(db_path)}


def _payload(db_path, stage: str):
    for row in oracle.stage_outputs(db_path):
        if row["stage"] == stage:
            return row["payload"]
    raise AssertionError(f"阶段输出缺失：{stage}")


@pytest.mark.reliability
def test_replaying_same_input_twice_is_byte_identical(tmp_path, run_child):
    readings = []
    for name in ("a", "b"):
        db_path = tmp_path / f"{name}.sqlite3"
        result = run_child(db_path)
        readings.append((db_path, result.require_ok()))

    # 活性对照：比较的必须是两次真的跑完、且有实质产物的运行，
    # 否则「两次都空」也能让逐字节比较通过（恒真陷阱）。
    db_a, summary_a = readings[0]
    db_b, _ = readings[1]
    assert summary_a["status"] == "completed"
    assert summary_a["coverage"] == 1
    assert {
        "collect",
        "clean",
        "analyze_batches",
        "consolidate",
        "audit_evidence",
        "validate_findings",
        "plan",
        "generate_tests",
        "validate_traceability",
    } <= _stage_names(db_a)

    findings = _payload(db_a, "validate_findings")["findings"]
    requirements = _payload(db_a, "plan")["requirements"]
    test_cases = _payload(db_a, "generate_tests")["test_cases"]
    assert findings and requirements and test_cases, "样例回放必须产出非空的发现/需求/用例"

    # 回放标注（非实时不变式）
    assert summary_a["mode"] == "recorded_live_run"
    assert summary_a["is_live"] is False

    snapshot_a = oracle.full_snapshot(db_a)
    snapshot_b = oracle.full_snapshot(db_b)
    difference = oracle.describe_difference(snapshot_a, snapshot_b)
    assert not difference, "两次回放不完全一致：\n" + "\n".join(difference)


@pytest.mark.reliability
def test_replay_run_records_no_model_usage(tmp_path, run_child):
    """回放不经过 DeepSeekProvider，用量表必须为空（设计事实，非缺陷）。"""
    db_path = tmp_path / "usage.sqlite3"
    run_child(db_path).require_ok()

    assert oracle.full_snapshot(db_path)["model_usage"] == []
