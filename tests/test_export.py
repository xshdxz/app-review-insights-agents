import json
from datetime import UTC, datetime

import pytest

from app_review_insights.export import (
    build_traceability_rows,
    rows_to_csv_bytes,
    to_json_bytes,
)
from app_review_insights.models import (
    AnalysisRequest,
    EvidenceStatus,
    Finding,
    Requirement,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.models import (
    TestCase as DomainTestCase,
)
from app_review_insights.storage import RunRepository
from app_review_insights.storage.cache import (
    build_demo_downloads,
    build_downloads,
    export_demo_run,
    load_demo_run,
)


def test_traceability_export_contains_complete_entity_chain():
    finding = Finding(
        finding_id="F-001",
        title="Problem",
        problem_statement="Problem",
        topic_label="topic",
        supporting_review_ids=["r-1", "r-2"],
        support_count=2,
        confidence=0.8,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="reason",
    )
    requirement = Requirement(
        requirement_id="REQ-001",
        finding_ids=["F-001"],
        title="Requirement",
        user_problem="Problem",
        objective="Objective",
        scope=["Scope"],
        non_goals=[],
        functional_rules=["Rule"],
        edge_cases=[],
        acceptance_criteria=["Criterion"],
        success_metrics=["Metric"],
        impact=3,
        complexity="low",
        target_version="V1.0",
        source_review_ids=["r-1", "r-2"],
    )
    test_case = DomainTestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Case",
        preconditions=[],
        steps=["Act"],
        expected_result="Result",
        case_type="normal",
        source_review_ids=["r-1", "r-2"],
    )

    rows = build_traceability_rows([finding], [requirement], [test_case])

    assert rows == [
        {
            "review_ids": "r-1,r-2",
            "finding_id": "F-001",
            "requirement_id": "REQ-001",
            "test_case_id": "TC-001",
        }
    ]


def test_json_and_csv_exports_are_utf8_compatible():
    payload = {"title": "订阅说明"}
    rows = [
        {
            "review_ids": "r-1",
            "finding_id": "F-001",
            "requirement_id": "REQ-001",
            "test_case_id": "TC-001",
        }
    ]

    assert json.loads(to_json_bytes(payload).decode("utf-8")) == payload
    csv_bytes = rows_to_csv_bytes(rows)
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert "REQ-001" in csv_bytes.decode("utf-8-sig")


def test_empty_traceability_rows_export_as_empty_bytes():
    assert rows_to_csv_bytes([]) == b""


def _finding() -> Finding:
    return Finding(
        finding_id="F-001",
        title="Timer resume failure",
        problem_statement="Timer freezes after pause.",
        topic_label="timer reliability",
        supporting_review_ids=["r-1", "r-2"],
        support_count=2,
        confidence=0.9,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="Two reviews describe the same failure.",
    )


def _requirement() -> Requirement:
    return Requirement(
        requirement_id="REQ-001",
        finding_ids=["F-001"],
        title="Restore timer after pause",
        user_problem="Paused workouts cannot reliably resume.",
        objective="Keep the workout timer continuous after resume.",
        scope=["Pause and resume timer state"],
        non_goals=[],
        functional_rules=["Resume from the saved elapsed time"],
        edge_cases=["App returns from the background while paused"],
        acceptance_criteria=["Timer advances within one second after resume"],
        success_metrics=["Resume failure rate"],
        impact=5,
        complexity="medium",
        target_version="V1.0",
        source_review_ids=["r-1", "r-2"],
    )


def _test_case() -> DomainTestCase:
    return DomainTestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Resume a paused workout",
        preconditions=["Workout timer is running"],
        steps=["Pause the workout", "Resume the workout"],
        expected_result="Timer continues from the saved elapsed time.",
        case_type="regression",
        source_review_ids=["r-1", "r-2"],
    )


def _save_export_outputs(repository: RunRepository, run_id: str) -> None:
    repository.save_output(
        run_id,
        Stage.CLEAN,
        {"reviews": [{"review_id": "r-1", "content_original": "Timer freezes."}]},
    )
    repository.save_output(
        run_id,
        Stage.VALIDATE_FINDINGS,
        {"findings": [_finding().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.PLAN,
        {"requirements": [_requirement().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.GENERATE_TESTS,
        {"test_cases": [_test_case().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.VALIDATE_TRACEABILITY,
        {"valid": True, "issues": []},
    )


def test_demo_cache_is_explicitly_labeled(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(
        json.dumps(
            {
                "mode": "historical_cache_demo",
                "is_live": False,
                "collected_at": "2026-08-15T00:00:00+00:00",
                "result": {},
            }
        ),
        encoding="utf-8",
    )

    demo = load_demo_run(path)

    assert demo["mode"] == "historical_cache_demo"
    assert demo["is_live"] is False
    assert demo["collected_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "live", "is_live": False, "collected_at": "now", "result": {}},
        {
            "mode": "historical_cache_demo",
            "is_live": True,
            "collected_at": "now",
            "result": {},
        },
    ],
)
def test_demo_cache_rejects_live_or_ambiguous_labels(tmp_path, payload):
    path = tmp_path / "invalid-demo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="non-live"):
        load_demo_run(path)


def test_build_downloads_restores_domain_models_for_traceability(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    _save_export_outputs(repository, "run-1")

    downloads = build_downloads(repository, "run-1")

    assert json.loads(downloads["cleaned_reviews"].decode("utf-8"))[0][
        "review_id"
    ] == "r-1"
    assert json.loads(downloads["prd"].decode("utf-8"))["requirements"][0][
        "requirement_id"
    ] == "REQ-001"
    assert "TC-001" in downloads["test_cases"].decode("utf-8-sig")
    traceability = downloads["traceability"].decode("utf-8-sig")
    assert "r-1,r-2" in traceability
    assert "F-001,REQ-001,TC-001" in traceability


def test_export_demo_run_writes_non_live_pipeline_outputs(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    now = datetime.now(UTC)
    repository.save_run(
        RunRecord(
            run_id="run-1",
            request=AnalysisRequest(
                source_type=SourceType.JSON,
                analysis_goal="Identify reliability problems",
                review_limit=100,
            ),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            coverage_ratio=1,
            created_at=now,
            updated_at=now,
        )
    )
    _save_export_outputs(repository, "run-1")
    destination = tmp_path / "demo-run.json"

    export_demo_run(
        repository,
        "run-1",
        destination,
        "https://apps.apple.com/us/app/example/id123456789",
    )

    demo = load_demo_run(destination)
    assert demo["run"]["run_id"] == "run-1"
    assert demo["source_app_url"].endswith("id123456789")
    assert demo["result"]["clean"]["reviews"][0]["review_id"] == "r-1"
    assert demo["result"]["validate_traceability"]["valid"] is True

    downloads = build_demo_downloads(demo)
    assert "REQ-001" in downloads["prd"].decode("utf-8")
    assert "TC-001" in downloads["traceability"].decode("utf-8-sig")
