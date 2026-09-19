"""崩溃场景的唯一枚举来源。

测试从这里取场景，文档一致性测试也从这里取——这样"文档里那张矩阵"与"代码里真正跑的场景"
不可能各说各话。散在两处的枚举迟早会漂移，而一份会漂移的可靠性矩阵还不如没有。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 会写检查点的阶段（scope 与 complete 不落盘，因此没有对应的崩溃点）
STAGE_ORDER = (
    "collect",
    "clean",
    "analyze_batches",
    "consolidate",
    "audit_evidence",
    "validate_findings",
    "plan",
    "generate_tests",
    "validate_traceability",
)


@dataclass(frozen=True)
class CrashPoint:
    stage: str | None
    batch: int | None
    expected_keys: frozenset
    label: str


def stage_prefix(stage: str) -> set[tuple[str, int]]:
    """崩溃发生在某阶段检查点落盘之后 ⇒ 已落盘的是该阶段及其之前的全部阶段。

    `analyze_batches` 是个例外：它除了清单行（batch_index=-1）还会给**每个批次**
    各写一行（batch_index=0…n-1）。批次循环跑完之后的崩溃点，这些行也必须已经在了——
    漏掉它们，负向断言会把"正常落盘的批次结果"误报成越界写入。
    """
    index = STAGE_ORDER.index(stage)
    keys = {(name, -1) for name in STAGE_ORDER[: index + 1]}
    if index > STAGE_ORDER.index("analyze_batches"):
        keys |= {("analyze_batches", 0)}
    return keys


def crash_points() -> list[CrashPoint]:
    """确定性崩溃点：每个阶段检查点落盘之后各一个，外加批次边界一个。"""
    points = [
        CrashPoint(
            stage=name,
            batch=None,
            expected_keys=frozenset(stage_prefix(name)),
            label=f"stage:{name}",
        )
        for name in STAGE_ORDER
    ]
    points.append(
        CrashPoint(
            stage=None,
            batch=0,
            expected_keys=frozenset(stage_prefix("analyze_batches") | {("analyze_batches", 0)}),
            label="batch:0",
        )
    )
    return points


#: 随机时刻硬杀：(在第 N 个检查点落盘后再等多少秒才杀)。全部取在流水线中段，
#: 保证被杀时仍有大量工作未完成——否则子进程会自己跑完，这一轮就什么都没测到。
KILL_POINTS: tuple[tuple[int, float], ...] = (
    (3, 0.05),
    (5, 0.05),
    (7, 0.06),
    (9, 0.06),
    (10, 0.05),
)


def kill_label(kill_after_saves: int, jitter: float) -> str:
    return f"kill:after-{kill_after_saves}-saves-{(jitter * 1000):.0f}ms"


def scenario_ids() -> list[str]:
    """全部崩溃场景 ID —— 文档一致性测试逐条核对用。"""
    return [point.label for point in crash_points()] + [
        kill_label(saves, jitter) for saves, jitter in KILL_POINTS
    ]
