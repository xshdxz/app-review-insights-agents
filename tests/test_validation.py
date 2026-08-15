from datetime import UTC, datetime

from app_review_insights.llm.schemas import (
    EvidenceAssessmentDraft,
    EvidenceAuditResult,
    FindingDraft,
    FindingEvidenceAuditDraft,
)
from app_review_insights.models import EvidenceStatus, Review
from app_review_insights.pipeline.validate import validate_finding_drafts


def review(review_id: str, rating: int = 2) -> Review:
    return Review(
        review_id=review_id,
        app_id="app-1",
        content_original=f"Review evidence {review_id}",
        rating=rating,
        published_at=datetime.now(UTC),
        source="fixture",
    )


def draft(
    supporting_review_ids: list[str],
    conflicting_review_ids: list[str] | None = None,
) -> FindingDraft:
    return FindingDraft(
        title="Trial clarity",
        problem_statement="Some users cannot understand renewal timing.",
        topic_label="subscription",
        supporting_review_ids=supporting_review_ids,
        conflicting_review_ids=conflicting_review_ids or [],
        reasoning_summary="The cited reviews discuss renewal timing.",
    )


def audit(*assessments: EvidenceAssessmentDraft) -> EvidenceAuditResult:
    return EvidenceAuditResult(
        findings=[
            FindingEvidenceAuditDraft(
                finding_index=0,
                assessments=list(assessments),
            )
        ]
    )


def assessment(review_id: str, role: str) -> EvidenceAssessmentDraft:
    return EvidenceAssessmentDraft(
        review_id=review_id,
        role=role,
        rationale_zh=f"{review_id} 被复核为 {role}",
    )


def test_semantic_audit_removes_irrelevant_and_reclassifies_roles():
    reviews = [review("r-1"), review("r-2"), review("r-3")]

    findings, report = validate_finding_drafts(
        [draft(["r-1", "r-2"], ["r-3"])],
        reviews,
        audit(
            assessment("r-1", "irrelevant"),
            assessment("r-2", "conflicting"),
            assessment("r-3", "supporting"),
        ),
    )

    assert findings[0].supporting_review_ids == ["r-3"]
    assert findings[0].conflicting_review_ids == ["r-2"]
    assert findings[0].support_count == 1
    assert findings[0].conflict_count == 1
    assert findings[0].schema_validated is True
    assert findings[0].reference_validated is True
    assert findings[0].semantic_validated is True
    assert [item.role for item in findings[0].evidence_assessments] == [
        "irrelevant",
        "conflicting",
        "supporting",
    ]
    assert report.valid is True


def test_semantic_audit_downgrades_when_a_cited_review_is_not_assessed():
    findings, report = validate_finding_drafts(
        [draft(["r-1", "r-2"])],
        [review("r-1"), review("r-2")],
        audit(assessment("r-1", "supporting")),
    )

    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].semantic_validated is False
    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert any(issue.rule == "evidence_audit_complete" for issue in report.issues)


def test_semantic_audit_rejects_unknown_assessment_references():
    findings, report = validate_finding_drafts(
        [draft(["r-1"])],
        [review("r-1"), review("r-2")],
        audit(
            assessment("r-1", "supporting"),
            assessment("r-2", "supporting"),
        ),
    )

    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].semantic_validated is False
    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert report.valid is False
    assert any(issue.rule == "evidence_audit_reference_exists" for issue in report.issues)


def test_semantic_audit_rejects_finding_when_all_citations_are_irrelevant():
    findings, report = validate_finding_drafts(
        [draft(["r-1"])],
        [review("r-1")],
        audit(assessment("r-1", "irrelevant")),
    )

    assert findings == []
    assert report.valid is False
    assert any(issue.rule == "finding_has_support" for issue in report.issues)


def test_semantic_audit_validates_only_complete_threshold_evidence():
    reviews = [review(f"r-{index}") for index in range(25)]

    findings, report = validate_finding_drafts(
        [draft(["r-1", "r-2"])],
        reviews,
        audit(
            assessment("r-1", "supporting"),
            assessment("r-2", "supporting"),
        ),
    )

    assert findings[0].evidence_status == EvidenceStatus.VALIDATED
    assert findings[0].reference_validated is True
    assert findings[0].semantic_validated is True
    assert report.valid is True


def test_semantic_audit_downgrades_when_finding_entry_is_missing():
    findings, report = validate_finding_drafts(
        [draft(["r-1"])],
        [review("r-1")],
        EvidenceAuditResult(findings=[]),
    )

    assert findings == []
    assert report.valid is False
    assert any(issue.rule == "evidence_audit_complete" for issue in report.issues)
    assert any(issue.rule == "finding_has_support" for issue in report.issues)


def test_semantic_audit_rejects_duplicate_assessment_references():
    findings, report = validate_finding_drafts(
        [draft(["r-1"])],
        [review("r-1")],
        audit(
            assessment("r-1", "supporting"),
            assessment("r-1", "supporting"),
        ),
    )

    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].semantic_validated is False
    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert any(issue.rule == "evidence_audit_unique" for issue in report.issues)
    assert report.valid is True


def test_validation_removes_invented_ids_and_recomputes_counts():
    reviews = [review("r-1", 2), review("r-2", 5)]

    findings, report = validate_finding_drafts(
        [draft(["r-1", "invented"], ["r-2"])],
        reviews,
    )

    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].conflicting_review_ids == ["r-2"]
    assert findings[0].support_count == 1
    assert findings[0].conflict_count == 1
    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert findings[0].confidence < 0.4
    assert report.valid is False
    assert any(issue.rule == "review_reference_exists" for issue in report.issues)
    assert any("不存在" in item for item in findings[0].limitations)


def test_validation_rejects_draft_without_any_real_support():
    findings, report = validate_finding_drafts(
        [draft(["invented"])],
        [review("r-1")],
    )

    assert findings == []
    assert report.valid is False
    assert any(issue.rule == "finding_has_support" for issue in report.issues)


def test_validation_deduplicates_ids_and_removes_support_conflict_overlap():
    findings, report = validate_finding_drafts(
        [draft(["r-1", "r-1"], ["r-1", "r-2", "r-2"])],
        [review("r-1"), review("r-2")],
    )

    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].conflicting_review_ids == ["r-2"]
    assert findings[0].support_count == 1
    assert findings[0].conflict_count == 1
    assert report.valid is True


def test_validation_uses_adaptive_support_threshold_for_larger_samples():
    reviews = [review(f"r-{index}") for index in range(25)]
    drafts = [
        draft(["r-1"]),
        FindingDraft(
            title="Reliable evidence",
            problem_statement="Multiple users report the same issue.",
            topic_label="reliability",
            supporting_review_ids=["r-2", "r-3"],
            reasoning_summary="Two independent reviews support this finding.",
        ),
    ]

    findings, report = validate_finding_drafts(drafts, reviews)

    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert findings[1].evidence_status == EvidenceStatus.VALIDATED
    assert findings[1].confidence > findings[0].confidence
    assert report.valid is True
