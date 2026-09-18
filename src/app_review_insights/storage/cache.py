import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app_review_insights.export import (
    build_traceability_rows,
    rows_to_csv_bytes,
    to_json_bytes,
)
from app_review_insights.models import Finding, Requirement, Stage, TestCase

#: 仓库自带的样例评论。演示模式与录制脚本共用这一处定义。
#: 放在包内而不是 scripts/：界面（ui/main.py）也要用它，
#: 让 src 反过来依赖 scripts 是分层倒置。
SAMPLE_PATH = Path(__file__).resolve().parents[3] / "data" / "samples" / "reviews-sample.json"

#: 演示模式固定使用的分析目标。录制脚本与界面共用这一处定义——
#: 它进 prompt，改了就会导致回放未命中，因此必须是同一个来源。
#: 它同时充当普通表单的分析目标默认值（ui/main.py）：为产品原因调整这个默认值，
#: 会一并改掉演示目标、静默作废已提交的录制件 data/recordings/demo-replay.json，
#: 演示随即停在 waiting_for_model，而重新录制要再花一次真实的模型费用。
DEMO_ANALYSIS_GOAL = "识别影响用户体验与产品增长的核心问题，并形成可追溯需求"

_DEMO_STAGES = (
    Stage.CLEAN,
    Stage.VALIDATE_FINDINGS,
    Stage.PLAN,
    Stage.GENERATE_TESTS,
    Stage.VALIDATE_TRACEABILITY,
)


def load_demo_run(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("demo cache must be a JSON object labeled as non-live")
    if payload.get("mode") != "historical_cache_demo" or payload.get("is_live") is not False:
        raise ValueError("demo cache must be explicitly labeled as non-live")
    if not payload.get("collected_at") or not isinstance(payload.get("result"), dict):
        raise ValueError("demo cache must include collected_at and result")
    return payload


def _stage_outputs(repository, run_id: str) -> dict[str, dict[str, Any] | None]:
    return {stage.value: repository.get_output(run_id, stage) for stage in _DEMO_STAGES}


def _labeled_json(payload: Any, label: dict[str, Any]) -> Any:
    """把标注与载荷合成一个对象。

    数组形载荷（清洗评论）套一层 `reviews` 信封：这正是 `input_parsing` 已接受的导入
    形状（docs/data-format.md 写明顶层可以是数组**或**含 reviews 的对象），因此标注不会
    破坏「导出后重新导入」的往返。对象形载荷直接并列即可。
    """
    if isinstance(payload, dict):
        return {**label, **payload}
    return {**label, "reviews": payload}


def _build_downloads_from_outputs(
    outputs: dict[str, dict[str, Any] | None],
    label: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    """按阶段输出构造四个下载。

    `label` 非空时给每个下载都盖上来源标注，且四个下载一个不落：
    JSON 两个内嵌 `mode`/`is_live` 两个键，CSV 两个各加同名的两列——CSV 无法内嵌字段，
    所以用**列**承载同一套键名，格式上仍然合法、按表头读取的下游不受影响。
    `label` 为空（实时运行）时产物与加标注之前**逐字节相同**，不给实时结果盖回放的章。
    """
    clean = outputs.get(Stage.CLEAN.value) or {"reviews": []}
    finding_payload = outputs.get(Stage.VALIDATE_FINDINGS.value) or {"findings": []}
    plan = outputs.get(Stage.PLAN.value) or {"requirements": []}
    test_payload = outputs.get(Stage.GENERATE_TESTS.value) or {"test_cases": []}

    findings = [Finding.model_validate(item) for item in finding_payload["findings"]]
    requirements = [Requirement.model_validate(item) for item in plan["requirements"]]
    test_cases = [TestCase.model_validate(item) for item in test_payload["test_cases"]]

    cleaned_reviews: Any = clean["reviews"]
    prd: Any = plan
    test_case_rows = [item.model_dump(mode="json") for item in test_cases]
    traceability_rows = build_traceability_rows(findings, requirements, test_cases)
    if label:
        cleaned_reviews = _labeled_json(cleaned_reviews, label)
        prd = _labeled_json(prd, label)
        # 标注列放在原有列之后：按位置读取的下游不受影响，表头读法照常
        test_case_rows = [{**row, **label} for row in test_case_rows]
        traceability_rows = [{**row, **label} for row in traceability_rows]

    return {
        "cleaned_reviews": to_json_bytes(cleaned_reviews),
        "prd": to_json_bytes(prd),
        "test_cases": rows_to_csv_bytes(test_case_rows),
        "traceability": rows_to_csv_bytes(traceability_rows),
    }


def _run_label(repository, run_id: str) -> dict[str, Any] | None:
    """运行来源标注；实时运行返回 None（产物一个字都不加）。"""
    try:
        run = repository.get_run(run_id)
    except KeyError:
        # 运行记录不存在（例如刚被清理）不该让下载整个失败：无标注按实时处理，
        # 与加标注之前的行为一致。
        return None
    if run.is_live:
        return None
    return {"mode": run.mode, "is_live": run.is_live}


def build_downloads(repository, run_id: str) -> dict[str, bytes]:
    return _build_downloads_from_outputs(
        _stage_outputs(repository, run_id), _run_label(repository, run_id)
    )


def build_demo_downloads(demo: dict[str, Any]) -> dict[str, bytes]:
    if (
        demo.get("mode") != "historical_cache_demo"
        or demo.get("is_live") is not False
        or not isinstance(demo.get("result"), dict)
    ):
        raise ValueError("demo cache must be explicitly labeled as non-live")
    # 历史档案这条路径此前只标注了档案信封与页面徽章，下载产物本身没有标注——
    # 与回放路径一样补上，让「非实时产物一律带标注」成为真正成立的不变式。
    return _build_downloads_from_outputs(
        demo["result"], {"mode": demo["mode"], "is_live": demo["is_live"]}
    )


def export_demo_run(
    repository,
    run_id: str,
    destination: str | Path,
    source_app_url: str,
) -> None:
    run = repository.get_run(run_id)
    payload = {
        "mode": "historical_cache_demo",
        "is_live": False,
        "collected_at": datetime.now(UTC).isoformat(),
        "source_app_url": source_app_url,
        "model_provider": "deepseek",
        "model_name": "deepseek-chat",
        "run": run.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in repository.list_events(run_id)],
        "result": _stage_outputs(repository, run_id),
    }
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
