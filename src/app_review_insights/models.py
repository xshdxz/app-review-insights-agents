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
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class Stage(StrEnum):
    SCOPE = "scope"
    COLLECT = "collect"
    CLEAN = "clean"
    ANALYZE_BATCHES = "analyze_batches"
    CONSOLIDATE = "consolidate"
    AUDIT_EVIDENCE = "audit_evidence"
    VALIDATE_FINDINGS = "validate_findings"
    PLAN = "plan"
    GENERATE_TESTS = "generate_tests"
    VALIDATE_TRACEABILITY = "validate_traceability"
    COMPLETE = "complete"


class EvidenceStatus(StrEnum):
    VALIDATED = "validated"
    ASSUMPTION = "assumption"
    REJECTED = "rejected"


class EvidenceAssessment(BaseModel):
    review_id: str
    role: Literal["supporting", "conflicting", "irrelevant"]
    rationale_zh: str = Field(min_length=1)


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
    platform: str = "app-store"


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
    #: 语言无关的稳定主题键（ascii snake_case），供评测比对；展示仍用 topic_label
    topic_key: str = ""
    supporting_review_ids: list[str] = Field(min_length=1)
    conflicting_review_ids: list[str] = Field(default_factory=list)
    support_count: int = 0
    conflict_count: int = 0
    confidence: float = Field(ge=0, le=1)
    evidence_status: EvidenceStatus
    model_reasoning_summary: str
    schema_validated: bool = True
    reference_validated: bool = False
    semantic_validated: bool = False
    evidence_assessments: list[EvidenceAssessment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Requirement(BaseModel):
    requirement_id: str
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
    priority_score: float = 0
    target_version: Literal["V1.0", "V1.1", "Future"]
    source_review_ids: list[str] = Field(min_length=1)
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


#: 实时运行的来源标注取值。与 llm/recording.py 的 RECORDING_MODE、storage/cache.py 的
#: historical_cache_demo 是同一套 mode/is_live 约定。
LIVE_RUN_MODE = "live"


class RunRecord(BaseModel):
    run_id: str
    request: AnalysisRequest
    current_stage: Stage
    status: RunStatus
    #: 运行来源标注：live/is_live=true 表示本次真的调用了模型；回放一次真实运行的录制则为
    #: recorded_live_run/is_live=false。标注落在**运行**上而不是只放在界面上——下载产物
    #: 会离开页面，离开之后仍要能自证不是实时结果（AGENTS.md 设计约束第 4 条）。
    #: 旧运行记录里没有这两个键，反序列化时按默认值（实时）补齐。
    mode: str = LIVE_RUN_MODE
    is_live: bool = True
    #: 运行租约：持有者身份（host:pid:token）与最近心跳。进程被硬杀后，靠"持有者进程
    #: 是否还在"判定能否接管——没有它就只能退回心跳超时，而崩溃刚发生时心跳是新鲜的，
    #: 用户得白等一个窗口才能续跑。旧记录没有这两个键，反序列化时按 None 补齐。
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None
    #: 产生这次运行的 prompt 版本标签与文本指纹。两者一起记录，是为了让"文本改了但
    #: 版本号没动"这种漂移可发现；旧记录没有这两个键，反序列化时按 None 补齐。
    #: 它们只进记录、不进请求文本，所以不影响录制件。
    prompt_version: str | None = None
    prompt_fingerprint: str | None = None
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


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRun(BaseModel):
    run_id: str
    goal: str
    app_url: str
    status: AgentRunStatus
    plan_summary: str = ""
    analysis_run_id: str | None = None
    report_id: str | None = None
    review_rounds: int = 0
    feedback: list[str] = Field(default_factory=list)
    require_approval: bool = False
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class MonitorJob(BaseModel):
    job_id: str
    name: str
    app_url: str
    goal: str
    cron: str
    review_limit: int = Field(default=200, ge=100, le=1000)
    enabled: bool = True
    require_approval: bool = False
    last_run_at: datetime | None = None
    last_status: str | None = None
    created_at: datetime
    updated_at: datetime


class MonitorReport(BaseModel):
    report_id: str
    agent_run_id: str
    app_url: str
    goal: str
    markdown: str
    summary: str
    findings_count: int
    changes: list[str] = Field(default_factory=list)
    created_at: datetime
    delivered_to: list[str] = Field(default_factory=list)
