import json

from app_review_insights.export import (
    build_traceability_rows,
    rows_to_csv_bytes,
    to_json_bytes,
)
from app_review_insights.models import (
    EvidenceStatus,
    Finding,
    Requirement,
)
from app_review_insights.models import (
    TestCase as DomainTestCase,
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
