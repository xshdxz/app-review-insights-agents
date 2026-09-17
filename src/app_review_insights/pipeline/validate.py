import math

from app_review_insights.llm.schemas import EvidenceAuditResult, FindingDraft
from app_review_insights.models import (
    EvidenceAssessment,
    EvidenceStatus,
    Finding,
    Review,
    ValidationIssue,
    ValidationReport,
)


def _confidence(support: int, conflicts: int) -> float:
    volume = min(1.0, math.log1p(support) / math.log1p(10))
    conflict_ratio = conflicts / max(1, support + conflicts)
    return round(max(0.0, volume * (1 - 0.5 * conflict_ratio)), 2)


def _unique_valid_ids(values: list[str], valid_ids: set[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value in valid_ids))


def validate_finding_drafts(
    drafts: list[FindingDraft],
    reviews: list[Review],
    audit: EvidenceAuditResult | None = None,
) -> tuple[list[Finding], ValidationReport]:
    review_ids = {review.review_id for review in reviews}
    minimum_support = 1 if len(reviews) < 20 else 2
    findings: list[Finding] = []
    issues: list[ValidationIssue] = []
    audit_by_index = (
        {item.finding_index: item for item in audit.findings} if audit is not None else {}
    )

    for index, draft in enumerate(drafts, start=1):
        finding_id = f"F-{index:03d}"
        valid_support = _unique_valid_ids(draft.supporting_review_ids, review_ids)
        valid_conflicts = [
            review_id
            for review_id in _unique_valid_ids(
                draft.conflicting_review_ids,
                review_ids,
            )
            if review_id not in valid_support
        ]
        invalid_ids = sorted(
            (set(draft.supporting_review_ids) | set(draft.conflicting_review_ids)) - review_ids
        )
        limitations = list(dict.fromkeys(draft.limitations))
        evidence_assessments: list[EvidenceAssessment] = []
        semantic_validated = False

        if invalid_ids:
            issues.append(
                ValidationIssue(
                    entity_type="finding",
                    entity_id=finding_id,
                    rule="review_reference_exists",
                    severity="error",
                    message=f"删除不存在的评论引用：{invalid_ids}",
                    revision_action="remove_invalid_references",
                )
            )
            limitations.append("模型引用了不存在的评论 ID，已由系统删除。")

        if audit is not None:
            audit_item = audit_by_index.get(index - 1)
            cited_ids = list(dict.fromkeys(valid_support + valid_conflicts))
            cited_id_set = set(cited_ids)
            assessment_by_id = {}
            unknown_audit_ids: list[str] = []
            duplicate_audit_ids: list[str] = []

            if audit_item is None:
                missing_audit_ids = cited_ids
                limitations.append("该 Finding 缺少证据语义复核结果。")
            else:
                limitations.extend(audit_item.limitations)
                for assessment in audit_item.assessments:
                    if assessment.review_id not in cited_id_set:
                        unknown_audit_ids.append(assessment.review_id)
                        continue
                    if assessment.review_id in assessment_by_id:
                        duplicate_audit_ids.append(assessment.review_id)
                        continue
                    assessment_by_id[assessment.review_id] = assessment
                    evidence_assessments.append(
                        EvidenceAssessment.model_validate(assessment.model_dump())
                    )
                missing_audit_ids = [
                    review_id for review_id in cited_ids if review_id not in assessment_by_id
                ]

            if unknown_audit_ids:
                issues.append(
                    ValidationIssue(
                        entity_type="finding",
                        entity_id=finding_id,
                        rule="evidence_audit_reference_exists",
                        severity="error",
                        message=(
                            "证据复核引用了未被该 Finding 引用的评论："
                            f"{sorted(set(unknown_audit_ids))}"
                        ),
                        revision_action="remove_unknown_audit_references",
                    )
                )
                limitations.append("证据语义复核包含未知引用，已由系统删除。")
            if duplicate_audit_ids:
                issues.append(
                    ValidationIssue(
                        entity_type="finding",
                        entity_id=finding_id,
                        rule="evidence_audit_unique",
                        severity="warning",
                        message=f"证据复核重复返回评论：{sorted(set(duplicate_audit_ids))}",
                        revision_action="deduplicate_audit_references",
                    )
                )
            if missing_audit_ids:
                issues.append(
                    ValidationIssue(
                        entity_type="finding",
                        entity_id=finding_id,
                        rule="evidence_audit_complete",
                        severity="warning",
                        message=f"以下引用缺少语义复核：{missing_audit_ids}",
                        revision_action="audit_missing_references",
                    )
                )
                limitations.append("部分引用缺少语义复核，未计入有效证据。")

            semantic_validated = not (missing_audit_ids or unknown_audit_ids or duplicate_audit_ids)
            valid_support = [
                review_id
                for review_id in cited_ids
                if review_id in assessment_by_id
                and assessment_by_id[review_id].role == "supporting"
            ]
            valid_conflicts = [
                review_id
                for review_id in cited_ids
                if review_id in assessment_by_id
                and assessment_by_id[review_id].role == "conflicting"
            ]
            irrelevant_count = sum(
                assessment.role == "irrelevant" for assessment in assessment_by_id.values()
            )
            if irrelevant_count:
                limitations.append(f"语义复核移除了 {irrelevant_count} 条无关评论。")

        if not valid_support:
            issues.append(
                ValidationIssue(
                    entity_type="finding",
                    entity_id=finding_id,
                    rule="finding_has_support",
                    severity="error",
                    message="Finding 没有可验证的支持评论，已拒绝进入结果。",
                    revision_action="reject_finding",
                )
            )
            continue

        has_enough_support = len(valid_support) >= minimum_support
        if not has_enough_support:
            limitations.append(f"支持证据少于当前样本要求的 {minimum_support} 条，按假设展示。")
            issues.append(
                ValidationIssue(
                    entity_type="finding",
                    entity_id=finding_id,
                    rule="finding_support_threshold",
                    severity="warning",
                    message="支持证据不足，Finding 标记为 Assumption。",
                )
            )
        if valid_conflicts:
            limitations.append(f"存在 {len(valid_conflicts)} 条冲突评论。")

        evidence_status = EvidenceStatus.ASSUMPTION
        if has_enough_support and not invalid_ids and (audit is None or semantic_validated):
            evidence_status = EvidenceStatus.VALIDATED
        findings.append(
            Finding(
                finding_id=finding_id,
                title=draft.title,
                problem_statement=draft.problem_statement,
                topic_label=draft.topic_label,
                topic_key=draft.topic_key,
                supporting_review_ids=valid_support,
                conflicting_review_ids=valid_conflicts,
                support_count=len(valid_support),
                conflict_count=len(valid_conflicts),
                confidence=_confidence(len(valid_support), len(valid_conflicts)),
                evidence_status=evidence_status,
                model_reasoning_summary=draft.reasoning_summary,
                schema_validated=True,
                reference_validated=not invalid_ids,
                semantic_validated=semantic_validated,
                evidence_assessments=evidence_assessments,
                limitations=list(dict.fromkeys(limitations)),
            )
        )

    report = ValidationReport(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )
    return findings, report
