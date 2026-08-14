from app_review_insights.llm.schemas import RequirementDraft, RequirementPlanResult
from app_review_insights.models import EvidenceStatus, Finding
from app_review_insights.pipeline.planning import build_requirements


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate(self, system_prompt, user_prompt, schema):
        self.calls.append((system_prompt, user_prompt, schema))
        return self.result


def finding(
    finding_id: str,
    review_ids: list[str],
    confidence: float,
    status: EvidenceStatus = EvidenceStatus.VALIDATED,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        title=f"Finding {finding_id}",
        problem_statement="Users experience a concrete problem.",
        topic_label="topic",
        supporting_review_ids=review_ids,
        support_count=len(review_ids),
        confidence=confidence,
        evidence_status=status,
        model_reasoning_summary="Direct evidence.",
    )


def requirement_draft(
    finding_ids: list[str],
    title: str,
    impact: int,
    complexity: str = "low",
    proposed_version: str = "V1.0",
) -> RequirementDraft:
    return RequirementDraft(
        finding_ids=finding_ids,
        title=title,
        user_problem="Users cannot complete the intended task reliably.",
        objective="Make the task clear and reliable.",
        scope=["Implement the evidence-backed behavior"],
        non_goals=["Unrelated redesign"],
        functional_rules=["Show the required state before confirmation"],
        edge_cases=["Required upstream data is temporarily unavailable"],
        acceptance_criteria=["The expected state is visible and testable"],
        success_metrics=["Reduce related negative reviews"],
        impact=impact,
        complexity=complexity,
        proposed_version=proposed_version,
    )


def test_build_requirements_attaches_reviews_scores_and_versions():
    findings = [
        finding("F-001", ["r-1", "r-2"], 0.8),
        finding(
            "F-002",
            ["r-3"],
            0.3,
            status=EvidenceStatus.ASSUMPTION,
        ),
    ]
    provider = Provider(
        RequirementPlanResult(
            requirements=[
                requirement_draft(["F-002"], "Assumption-backed option", 3),
                requirement_draft(
                    ["F-001", "invented"],
                    "Evidence-backed fix",
                    5,
                ),
            ]
        )
    )

    requirements = build_requirements(
        provider,
        findings,
        "订阅转化",
        total_reviews=100,
    )

    assert [item.title for item in requirements] == [
        "Evidence-backed fix",
        "Assumption-backed option",
    ]
    assert requirements[0].finding_ids == ["F-001"]
    assert requirements[0].source_review_ids == ["r-1", "r-2"]
    assert requirements[0].priority_score > requirements[1].priority_score
    assert requirements[0].target_version == "V1.0"
    assert requirements[1].target_version == "Future"


def test_build_requirements_skips_model_when_no_findings_are_eligible():
    provider = Provider(RequirementPlanResult(requirements=[]))

    requirements = build_requirements(provider, [], "订阅转化", total_reviews=10)

    assert requirements == []
    assert provider.calls == []
