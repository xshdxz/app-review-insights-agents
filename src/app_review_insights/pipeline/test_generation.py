import json
from typing import Any

from app_review_insights.llm.prompts import TEST_GENERATION_SYSTEM_PROMPT
from app_review_insights.llm.schemas import TestCasePlanResult
from app_review_insights.models import Requirement, TestCase


def generate_test_cases(
    provider: Any,
    requirements: list[Requirement],
) -> list[TestCase]:
    test_cases: list[TestCase] = []

    for requirement in requirements:
        result = provider.generate(
            TEST_GENERATION_SYSTEM_PROMPT,
            json.dumps(requirement.model_dump(mode="json"), ensure_ascii=False),
            TestCasePlanResult,
        )
        for draft in result.test_cases:
            test_cases.append(
                TestCase(
                    test_case_id=f"TC-{len(test_cases) + 1:03d}",
                    requirement_id=requirement.requirement_id,
                    title=draft.title,
                    preconditions=draft.preconditions,
                    steps=draft.steps,
                    expected_result=draft.expected_result,
                    case_type=draft.case_type,
                    source_review_ids=requirement.source_review_ids,
                )
            )

    return test_cases
