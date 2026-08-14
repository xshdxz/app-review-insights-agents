import math

from app_review_insights.llm.schemas import FindingDraft
from app_review_insights.models import (
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
) -> tuple[list[Finding], ValidationReport]:
    review_ids = {review.review_id for review in reviews}
    minimum_support = 1 if len(reviews) < 20 else 2
    findings: list[Finding] = []
    issues: list[ValidationIssue] = []

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
            (set(draft.supporting_review_ids) | set(draft.conflicting_review_ids))
            - review_ids
        )
        limitations = list(dict.fromkeys(draft.limitations))

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
            limitations.append(
                f"支持证据少于当前样本要求的 {minimum_support} 条，按假设展示。"
            )
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

        evidence_status = (
            EvidenceStatus.VALIDATED
            if has_enough_support and not invalid_ids
            else EvidenceStatus.ASSUMPTION
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                title=draft.title,
                problem_statement=draft.problem_statement,
                topic_label=draft.topic_label,
                supporting_review_ids=valid_support,
                conflicting_review_ids=valid_conflicts,
                support_count=len(valid_support),
                conflict_count=len(valid_conflicts),
                confidence=_confidence(len(valid_support), len(valid_conflicts)),
                evidence_status=evidence_status,
                model_reasoning_summary=draft.reasoning_summary,
                limitations=list(dict.fromkeys(limitations)),
            )
        )

    report = ValidationReport(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )
    return findings, report
