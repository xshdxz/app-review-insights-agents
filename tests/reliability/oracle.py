"""崩溃一致性 oracle：运行快照的导出、规范化与比较。

两条比较口径，用途不同：

- `output_snapshot`：只含阶段输出（stage_outputs）+ 运行的最终态。**崩溃+续跑 vs 一次跑完**
  用它比较——续跑必然多出事件（"Analysis run resumed"、阶段重启），事件不能进这个口径。
- `full_snapshot`：runs + stage_outputs + events + model_usage 全量。**同一路径跑两遍**
  用它做基线确定性自检（keys 归一化后逐字节比较）。

规范化规则只剔除"每次运行都会变、且与正确性无关"的字段（run_id / 各类时间戳 / 租约归属），
其余一律原样保留——规范化范围越窄，断言越强。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app_review_insights.storage.sqlite import connect

#: 每次运行都会变、与正确性无关的键。命中即替换为占位符。
VOLATILE_KEYS = frozenset(
    {"run_id", "created_at", "updated_at", "heartbeat_at", "lease_owner", "recorded_at"}
)


def canonical(value: Any, key: str | None = None) -> Any:
    """按键排序 + 剔除易变字段，得到可逐字节比较的结构。"""
    if isinstance(value, dict):
        return {k: canonical(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(item, key) for item in value]
    if key in VOLATILE_KEYS:
        return f"<{key}>"
    return value


def _rows(db_path: Path, sql: str) -> list[Any]:
    connection = connect(db_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def stage_outputs(db_path: Path) -> list[dict[str, Any]]:
    """按 (stage, batch_index) 排序的阶段输出，已规范化。"""
    rows = _rows(
        db_path,
        "SELECT stage, batch_index, payload_json FROM stage_outputs ORDER BY stage, batch_index",
    )
    return [
        {
            "stage": row["stage"],
            "batch_index": row["batch_index"],
            "payload": canonical(json.loads(row["payload_json"])),
        }
        for row in rows
    ]


def output_snapshot(db_path: Path) -> dict[str, Any]:
    """崩溃收敛性口径：阶段输出 + 运行终态。"""
    runs = _rows(db_path, "SELECT payload_json FROM runs ORDER BY run_id")
    return {
        "stage_outputs": stage_outputs(db_path),
        "runs": [canonical(json.loads(row["payload_json"])) for row in runs],
    }


def full_snapshot(db_path: Path) -> dict[str, Any]:
    """基线确定性口径：四张表全量。"""
    return {
        "stage_outputs": stage_outputs(db_path),
        "runs": [
            canonical(json.loads(row["payload_json"]))
            for row in _rows(db_path, "SELECT payload_json FROM runs ORDER BY run_id")
        ],
        "events": [
            canonical(json.loads(row["payload_json"]))
            for row in _rows(db_path, "SELECT payload_json FROM events ORDER BY id")
        ],
        "model_usage": [
            canonical(dict(row))
            for row in _rows(
                db_path,
                "SELECT model, prompt_tokens, completion_tokens, total_tokens, "
                "estimated_cost_usd, created_at FROM model_usage ORDER BY id",
            )
        ],
    }


def as_json(snapshot: dict[str, Any]) -> str:
    """稳定的文本表示，用于逐字节比较。"""
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)


def stage_keys(db_path: Path) -> set[tuple[str, int]]:
    """已落盘的 (stage, batch_index) 集合——用于负向断言。"""
    rows = _rows(
        db_path,
        "SELECT stage, batch_index FROM stage_outputs",
    )
    return {(row["stage"], row["batch_index"]) for row in rows}


def assert_payloads_wellformed(db_path: Path) -> None:
    """原子性：落盘的每个阶段输出都必须是完整、可被对应模型校验的对象。

    "半个 JSON" 在 SQLite 事务下本不该出现——这条断言守的是**它真的没出现**，
    以及输出没有在 schema 层面走形（崩溃点之后的续跑若拼错结构，会在这里先炸）。
    """
    from app_review_insights.cleaning import CleaningResult
    from app_review_insights.llm.schemas import (
        BatchAnalysisResult,
        ConsolidationResult,
        EvidenceAuditResult,
    )
    from app_review_insights.models import (
        Finding,
        Requirement,
        Review,
        TestCase,
        ValidationReport,
    )

    for stage, batch_index, payload in raw_outputs(db_path):
        where = f"{stage}[{batch_index}]"
        if stage == "collect":
            for item in payload["reviews"]:
                Review.model_validate(item)
        elif stage == "clean":
            CleaningResult.model_validate(payload)
        elif stage == "analyze_batches" and batch_index == -1:
            assert isinstance(payload["batch_review_indices"], list), where
            assert isinstance(payload["completed"], bool), where
        elif stage == "analyze_batches":
            BatchAnalysisResult.model_validate(payload)
        elif stage == "consolidate":
            ConsolidationResult.model_validate(payload)
        elif stage == "audit_evidence":
            EvidenceAuditResult.model_validate(payload)
        elif stage == "validate_findings":
            for item in payload["findings"]:
                Finding.model_validate(item)
            ValidationReport.model_validate(payload["report"])
        elif stage == "plan":
            for item in payload["requirements"]:
                Requirement.model_validate(item)
        elif stage == "generate_tests":
            for item in payload["test_cases"]:
                TestCase.model_validate(item)
        elif stage == "validate_traceability":
            ValidationReport.model_validate(payload)
        else:
            raise AssertionError(f"未覆盖的阶段输出：{where}")


def run_ids(db_path: Path) -> list[str]:
    """库里全部 run_id（崩溃子进程不会打印摘要，只能从库里取）。"""
    return [row["run_id"] for row in _rows(db_path, "SELECT run_id FROM runs ORDER BY run_id")]


def run_status(db_path: Path) -> str | None:
    """当前（唯一一条）运行的状态；没有运行则返回 None。"""
    import json as _json

    rows = _rows(db_path, "SELECT payload_json FROM runs ORDER BY run_id")
    if not rows:
        return None
    return _json.loads(rows[0]["payload_json"])["status"]


def raw_outputs(db_path: Path) -> list[tuple[str, int, dict[str, Any]]]:
    """未规范化的阶段输出，用于按 Pydantic 模型校验（原子性断言）。"""
    rows = _rows(
        db_path,
        "SELECT stage, batch_index, payload_json FROM stage_outputs ORDER BY stage, batch_index",
    )
    return [(row["stage"], row["batch_index"], json.loads(row["payload_json"])) for row in rows]


def describe_difference(left: Any, right: Any, path: str = "", limit: int = 8) -> list[str]:
    """递归定位差异，生成可读的失败信息（逐字节比较失败时用）。"""
    found: list[str] = []

    def walk(a: Any, b: Any, where: str) -> None:
        if len(found) >= limit:
            return
        if type(a) is not type(b):
            found.append(f"{where}: 类型不同 {type(a).__name__} != {type(b).__name__}")
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a:
                    found.append(f"{where}.{key}: 左侧缺失")
                elif key not in b:
                    found.append(f"{where}.{key}: 右侧缺失")
                else:
                    walk(a[key], b[key], f"{where}.{key}")
            return
        if isinstance(a, list):
            if len(a) != len(b):
                found.append(f"{where}: 长度不同 {len(a)} != {len(b)}")
                return
            for index, (item_a, item_b) in enumerate(zip(a, b, strict=True)):
                walk(item_a, item_b, f"{where}[{index}]")
            return
        if a != b:
            found.append(f"{where}: {a!r} != {b!r}")

    walk(left, right, path or "$")
    return found
