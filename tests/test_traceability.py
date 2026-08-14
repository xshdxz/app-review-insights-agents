from app_review_insights.models import (
    EvidenceStatus,
    Finding,
    Requirement,
)
from app_review_insights.models import (
    TestCase as DomainTestCase,
)
from app_review_insights.pipeline.traceability import validate_traceability


def finding(finding_id: str = "F-001") -> Finding:
    return Finding(
        finding_id=finding_id,
        title="Problem",
        problem_statement="A concrete problem exists.",
        topic_label="topic",
        supporting_review_ids=["r-1"],
        support_count=1,
        confidence=0.5,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="reason",
    )


def requirement(requirement_id: str = "REQ-001") -> Requirement:
    return Requirement(
        requirement_id=requirement_id,
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
        source_review_ids=["r-1"],
    )


def case(
    test_case_id: str,
    source_review_ids: list[str] | None = None,
) -> DomainTestCase:
    return DomainTestCase(
        test_case_id=test_case_id,
        requirement_id="REQ-001",
        title="Case",
        preconditions=[],
        steps=["Act"],
        expected_result="Result",
        case_type="normal",
        source_review_ids=source_review_ids or ["r-1"],
    )


def test_traceability_accepts_complete_chain_with_two_cases():
    report = validate_traceability(
        {"r-1"},
        [finding()],
        [requirement()],
        [case("TC-001"), case("TC-002")],
    )

    assert report.valid is True
    assert report.issues == []


def test_traceability_rejects_test_case_without_valid_review_path():
    report = validate_traceability(
        {"r-1"},
        [finding()],
        [requirement()],
        [case("TC-001", ["invented"]), case("TC-002")],
    )

    assert report.valid is False
    assert any(issue.rule == "requirement_to_test_case" for issue in report.issues)


def test_traceability_requires_exact_inherited_reviews_and_two_to_four_cases():
    linked_finding = finding()
    linked_finding.supporting_review_ids = ["r-1", "r-2"]
    linked_finding.support_count = 2
    incomplete_requirement = requirement()
    incomplete_requirement.source_review_ids = ["r-1"]

    report = validate_traceability(
        {"r-1", "r-2"},
        [linked_finding],
        [incomplete_requirement],
        [case("TC-001")],
    )

    assert report.valid is False
    rules = {issue.rule for issue in report.issues}
    assert "requirement_reviews_inherit_findings" in rules
    assert "test_case_count_per_requirement" in rules


def test_traceability_rejects_duplicate_entity_ids():
    report = validate_traceability(
        {"r-1"},
        [finding(), finding()],
        [requirement(), requirement()],
        [case("TC-001"), case("TC-001")],
    )

    assert report.valid is False
    assert any(issue.rule == "entity_id_unique" for issue in report.issues)
