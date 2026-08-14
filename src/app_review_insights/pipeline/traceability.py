from collections import Counter

from app_review_insights.models import (
    EvidenceStatus,
    Finding,
    Requirement,
    TestCase,
    ValidationIssue,
    ValidationReport,
)


def _duplicate_ids(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def validate_traceability(
    review_ids: set[str],
    findings: list[Finding],
    requirements: list[Requirement],
    test_cases: list[TestCase],
) -> ValidationReport:
    issues: list[ValidationIssue] = []

    entity_groups = {
        "finding": [item.finding_id for item in findings],
        "requirement": [item.requirement_id for item in requirements],
        "test_case": [item.test_case_id for item in test_cases],
    }
    for entity_type, entity_ids in entity_groups.items():
        for duplicate_id in _duplicate_ids(entity_ids):
            issues.append(
                ValidationIssue(
                    entity_type=entity_type,
                    entity_id=duplicate_id,
                    rule="entity_id_unique",
                    severity="error",
                    message=f"{entity_type} ID 重复，追溯链不唯一。",
                )
            )

    finding_index = {item.finding_id: item for item in findings}
    requirement_index = {item.requirement_id: item for item in requirements}

    for finding in findings:
        referenced_reviews = set(finding.supporting_review_ids) | set(
            finding.conflicting_review_ids
        )
        if not referenced_reviews.issubset(review_ids):
            issues.append(
                ValidationIssue(
                    entity_type="finding",
                    entity_id=finding.finding_id,
                    rule="review_to_finding",
                    severity="error",
                    message="Finding 引用了不存在的评论。",
                )
            )

    for requirement in requirements:
        linked_findings = [
            finding_index[finding_id]
            for finding_id in requirement.finding_ids
            if finding_id in finding_index
        ]
        if len(linked_findings) != len(set(requirement.finding_ids)):
            issues.append(
                ValidationIssue(
                    entity_type="requirement",
                    entity_id=requirement.requirement_id,
                    rule="finding_to_requirement",
                    severity="error",
                    message="Requirement 缺少有效且唯一的 Finding 路径。",
                )
            )
        if any(
            finding.evidence_status == EvidenceStatus.REJECTED
            for finding in linked_findings
        ):
            issues.append(
                ValidationIssue(
                    entity_type="requirement",
                    entity_id=requirement.requirement_id,
                    rule="requirement_uses_eligible_finding",
                    severity="error",
                    message="Requirement 不能基于已拒绝的 Finding。",
                )
            )

        expected_reviews = {
            review_id
            for finding in linked_findings
            for review_id in finding.supporting_review_ids
        }
        if set(requirement.source_review_ids) != expected_reviews:
            issues.append(
                ValidationIssue(
                    entity_type="requirement",
                    entity_id=requirement.requirement_id,
                    rule="requirement_reviews_inherit_findings",
                    severity="error",
                    message="Requirement 未完整继承 Finding 的评论来源。",
                )
            )

    cases_by_requirement: dict[str, list[TestCase]] = {
        requirement.requirement_id: [] for requirement in requirements
    }
    for test_case in test_cases:
        requirement = requirement_index.get(test_case.requirement_id)
        if requirement is None:
            issues.append(
                ValidationIssue(
                    entity_type="test_case",
                    entity_id=test_case.test_case_id,
                    rule="requirement_to_test_case",
                    severity="error",
                    message="TestCase 关联了不存在的 Requirement。",
                )
            )
            continue

        cases_by_requirement[requirement.requirement_id].append(test_case)
        if set(test_case.source_review_ids) != set(requirement.source_review_ids):
            issues.append(
                ValidationIssue(
                    entity_type="test_case",
                    entity_id=test_case.test_case_id,
                    rule="requirement_to_test_case",
                    severity="error",
                    message="TestCase 未完整继承 Requirement 的评论来源。",
                )
            )

    for requirement_id, linked_cases in cases_by_requirement.items():
        if not 2 <= len(linked_cases) <= 4:
            issues.append(
                ValidationIssue(
                    entity_type="requirement",
                    entity_id=requirement_id,
                    rule="test_case_count_per_requirement",
                    severity="error",
                    message="每个 Requirement 必须关联 2–4 条 TestCase。",
                )
            )

    return ValidationReport(valid=not issues, issues=issues)
