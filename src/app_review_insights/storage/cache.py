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


def _build_downloads_from_outputs(
    outputs: dict[str, dict[str, Any] | None],
) -> dict[str, bytes]:
    clean = outputs.get(Stage.CLEAN.value) or {"reviews": []}
    finding_payload = outputs.get(Stage.VALIDATE_FINDINGS.value) or {"findings": []}
    plan = outputs.get(Stage.PLAN.value) or {"requirements": []}
    test_payload = outputs.get(Stage.GENERATE_TESTS.value) or {"test_cases": []}

    findings = [Finding.model_validate(item) for item in finding_payload["findings"]]
    requirements = [Requirement.model_validate(item) for item in plan["requirements"]]
    test_cases = [TestCase.model_validate(item) for item in test_payload["test_cases"]]
    return {
        "cleaned_reviews": to_json_bytes(clean["reviews"]),
        "prd": to_json_bytes(plan),
        "test_cases": rows_to_csv_bytes([item.model_dump(mode="json") for item in test_cases]),
        "traceability": rows_to_csv_bytes(
            build_traceability_rows(findings, requirements, test_cases)
        ),
    }


def build_downloads(repository, run_id: str) -> dict[str, bytes]:
    return _build_downloads_from_outputs(_stage_outputs(repository, run_id))


def build_demo_downloads(demo: dict[str, Any]) -> dict[str, bytes]:
    if (
        demo.get("mode") != "historical_cache_demo"
        or demo.get("is_live") is not False
        or not isinstance(demo.get("result"), dict)
    ):
        raise ValueError("demo cache must be explicitly labeled as non-live")
    return _build_downloads_from_outputs(demo["result"])


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
