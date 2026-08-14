from datetime import UTC, datetime

from app_review_insights.llm.schemas import FindingDraft
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
