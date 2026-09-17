import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def normalize_topic_key(value: str) -> str:
    """把主题键规范成 ascii snake_case；无法规范化时返回空串。

    空串表示"这次没有可比的键"，评测会把它计入覆盖率缺口，而不是拿一个
    不可比的中文标签去和黄金集做精确匹配——那只会得到假阴性。
    """
    ascii_only = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^0-9a-zA-Z]+", "_", ascii_only).strip("_").lower()


class FindingDraft(BaseModel):
    title: str
    problem_statement: str
    topic_label: str
    topic_key: str = ""
    supporting_review_ids: list[str] = Field(min_length=1)
    conflicting_review_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str
    limitations: list[str] = Field(default_factory=list)

    @field_validator("topic_key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        return normalize_topic_key(value)


class ReviewSummaryDraft(BaseModel):
    review_id: str
    summary_zh: str = Field(min_length=1)


class BatchAnalysisResult(BaseModel):
    findings: list[FindingDraft]
    review_summaries: list[ReviewSummaryDraft] = Field(default_factory=list)
    batch_limitations: list[str] = Field(default_factory=list)


class ConsolidationResult(BaseModel):
    findings: list[FindingDraft]


class EvidenceAssessmentDraft(BaseModel):
    review_id: str
    role: Literal["supporting", "conflicting", "irrelevant"]
    rationale_zh: str = Field(min_length=1)


class FindingEvidenceAuditDraft(BaseModel):
    finding_index: int = Field(ge=0)
    assessments: list[EvidenceAssessmentDraft] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EvidenceAuditResult(BaseModel):
    findings: list[FindingEvidenceAuditDraft] = Field(default_factory=list)


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
