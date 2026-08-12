from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SourceType(StrEnum):
    ONLINE = "online"
    JSON = "json"
    CSV = "csv"
    CACHE = "cache"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    WAITING = "waiting_for_model"
    COMPLETED = "completed"
    FAILED = "failed"


class Stage(StrEnum):
    SCOPE = "scope"
    COLLECT = "collect"
    CLEAN = "clean"
    ANALYZE_BATCHES = "analyze_batches"
    CONSOLIDATE = "consolidate"
    VALIDATE_FINDINGS = "validate_findings"
    PLAN = "plan"
    GENERATE_TESTS = "generate_tests"
    VALIDATE_TRACEABILITY = "validate_traceability"
    COMPLETE = "complete"


class EvidenceStatus(StrEnum):
    VALIDATED = "validated"
    ASSUMPTION = "assumption"
    REJECTED = "rejected"


class Review(BaseModel):
    review_id: str
    app_id: str
    storefront: str = "us"
    title: str = ""
    content_original: str = Field(min_length=1)
    content_summary_zh: str | None = None
    rating: int = Field(ge=1, le=5)
    app_version: str | None = None
    author: str | None = None
    published_at: datetime
    language: str | None = None
    source: str
    source_page: int | None = None
    content_hash: str = ""


class AnalysisRequest(BaseModel):
    source_type: SourceType
    analysis_goal: str = Field(min_length=3)
    app_url: str | None = None
    review_limit: int = Field(default=500, ge=100, le=1000)
    ratings: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    app_versions: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str
    title: str
    problem_statement: str
    topic_label: str
    supporting_review_ids: list[str]
    conflicting_review_ids: list[str] = Field(default_factory=list)
    support_count: int = 0
    conflict_count: int = 0
    confidence: float = Field(ge=0, le=1)
    evidence_status: EvidenceStatus
    model_reasoning_summary: str
    limitations: list[str] = Field(default_factory=list)


class Requirement(BaseModel):
    requirement_id: str
    finding_ids: list[str]
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
    priority_score: float = 0
    target_version: Literal["V1.0", "V1.1", "Future"]
    source_review_ids: list[str]
    assumptions: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    test_case_id: str
    requirement_id: str
    title: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    case_type: Literal["normal", "exception", "boundary", "regression"]
    source_review_ids: list[str] = Field(min_length=1)


class ValidationIssue(BaseModel):
    entity_type: str
    entity_id: str
    rule: str
    severity: Literal["warning", "error"]
    message: str
    revision_action: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class StageEvent(BaseModel):
    stage: Stage
    status: RunStatus
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RunRecord(BaseModel):
    run_id: str
    request: AnalysisRequest
    current_stage: Stage
    status: RunStatus
    current_batch: int = 0
    total_batches: int = 0
    coverage_ratio: float = Field(default=0, ge=0, le=1)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def completed_run_must_be_complete_stage(self):
        if self.status == RunStatus.COMPLETED and self.current_stage != Stage.COMPLETE:
            raise ValueError("completed runs must use the complete stage")
        return self
