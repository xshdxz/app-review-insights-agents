from typing import Literal

from pydantic import BaseModel, Field


class FindingDraft(BaseModel):
    title: str
    problem_statement: str
    topic_label: str
    supporting_review_ids: list[str] = Field(min_length=1)
    conflicting_review_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str
    limitations: list[str] = Field(default_factory=list)


class ReviewSummaryDraft(BaseModel):
    review_id: str
    summary_zh: str = Field(min_length=1)


class BatchAnalysisResult(BaseModel):
    findings: list[FindingDraft]
    review_summaries: list[ReviewSummaryDraft] = Field(default_factory=list)
    batch_limitations: list[str] = Field(default_factory=list)


class ConsolidationResult(BaseModel):
    findings: list[FindingDraft]


class RequirementDraft(BaseModel):
    finding_ids: list[str] = Field(min_length=1)
    title: str
    user_problem: str
    objective: str
    scope: list[str]
    non_goals: list[str]
    functional_rules: list[str]
    edge_cases: list[str]
    acceptance_criteria: list[str]
    success_metrics: list[str]
    impact: int = Field(ge=1, le=5)
    complexity: Literal["low", "medium", "high"]
    proposed_version: Literal["V1.0", "V1.1", "Future"]
    assumptions: list[str] = Field(default_factory=list)


class RequirementPlanResult(BaseModel):
    requirements: list[RequirementDraft] = Field(default_factory=list, max_length=10)


class TestCaseDraft(BaseModel):
    requirement_id: str
    title: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    case_type: Literal["normal", "exception", "boundary", "regression"]


class TestCasePlanResult(BaseModel):
    test_cases: list[TestCaseDraft] = Field(min_length=2, max_length=4)
