from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app_review_insights.models import (
    AnalysisRequest,
    EvidenceStatus,
    Review,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.models import (
    TestCase as DomainTestCase,
)


def test_review_rejects_out_of_range_rating():
    with pytest.raises(ValidationError):
        Review(
            review_id="r-1",
            app_id="839285684",
            content_original="Useful but crashes.",
            rating=6,
            published_at=datetime.now(UTC),
            source="fixture",
        )


def test_test_case_requires_traceability_fields():
    case = DomainTestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Show renewal date",
        preconditions=["User has an active trial"],
        steps=["Open subscription screen"],
        expected_result="Renewal date is visible before confirmation.",
        case_type="normal",
        source_review_ids=["r-1"],
    )

    assert case.requirement_id == "REQ-001"
    assert case.source_review_ids == ["r-1"]
    assert EvidenceStatus.VALIDATED.value == "validated"
    assert Stage.COMPLETE.value == "complete"


def test_completed_run_requires_complete_stage():
    now = datetime.now(UTC)
    request = AnalysisRequest(
        source_type=SourceType.JSON,
        analysis_goal="分析订阅转化问题",
    )

    with pytest.raises(ValidationError, match="complete stage"):
        RunRecord(
            run_id="run-1",
            request=request,
            current_stage=Stage.PLAN,
            status=RunStatus.COMPLETED,
            created_at=now,
            updated_at=now,
        )
