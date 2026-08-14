import pytest
from pydantic import ValidationError

from app_review_insights.llm.schemas import (
    TestCaseDraft as CaseDraft,
)
from app_review_insights.llm.schemas import (
    TestCasePlanResult as CasePlanResult,
)
from app_review_insights.models import Requirement
from app_review_insights.pipeline.test_generation import generate_test_cases


class QueueProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def generate(self, system_prompt, user_prompt, schema):
        self.calls.append((system_prompt, user_prompt, schema))
        return self.results.pop(0)


def requirement(requirement_id: str, review_id: str) -> Requirement:
    return Requirement(
        requirement_id=requirement_id,
        finding_ids=["F-001"],
        title=f"Requirement {requirement_id}",
        user_problem="The current behavior is unclear.",
        objective="Make the behavior reliable.",
        scope=["Implement expected behavior"],
        non_goals=[],
        functional_rules=["Show state before confirmation"],
        edge_cases=["Data is unavailable"],
        acceptance_criteria=["State is visible"],
        success_metrics=["Fewer complaints"],
        impact=5,
        complexity="low",
        priority_score=8.0,
        target_version="V1.0",
        source_review_ids=[review_id],
    )


def case(requirement_id: str, title: str, case_type: str) -> CaseDraft:
    return CaseDraft(
        requirement_id=requirement_id,
        title=title,
        preconditions=["App is installed"],
        steps=["Open the relevant flow", "Perform the action"],
        expected_result="The evidence-backed behavior is shown.",
        case_type=case_type,
    )


def test_test_case_schema_requires_two_to_four_cases_per_requirement():
    with pytest.raises(ValidationError):
        CasePlanResult(test_cases=[case("REQ-001", "Only case", "normal")])


def test_test_cases_are_generated_per_requirement_and_inherit_reviews():
    requirements = [
        requirement("REQ-001", "r-1"),
        requirement("REQ-002", "r-2"),
    ]
    provider = QueueProvider(
        [
            CasePlanResult(
                test_cases=[
                    case("REQ-001", "Normal flow", "normal"),
                    case("REQ-001", "Missing data", "exception"),
                ]
            ),
            CasePlanResult(
                test_cases=[
                    case("wrong-id", "Boundary flow", "boundary"),
                    case("wrong-id", "Regression flow", "regression"),
                ]
            ),
        ]
    )

    cases = generate_test_cases(provider, requirements)

    assert len(provider.calls) == 2
    assert [item.test_case_id for item in cases] == [
        "TC-001",
        "TC-002",
        "TC-003",
        "TC-004",
    ]
    assert [item.requirement_id for item in cases] == [
        "REQ-001",
        "REQ-001",
        "REQ-002",
        "REQ-002",
    ]
    assert cases[0].source_review_ids == ["r-1"]
    assert cases[-1].source_review_ids == ["r-2"]


def test_test_generation_skips_model_when_no_requirements_exist():
    provider = QueueProvider([])

    assert generate_test_cases(provider, []) == []
    assert provider.calls == []
