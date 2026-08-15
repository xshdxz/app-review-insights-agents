from datetime import UTC, datetime

from app_review_insights.errors import CollectionError, RecoverableModelError
from app_review_insights.llm.schemas import (
    BatchAnalysisResult,
    ConsolidationResult,
    FindingDraft,
    ReviewSummaryDraft,
)
from app_review_insights.models import (
    AnalysisRequest,
    EvidenceStatus,
    Finding,
    Requirement,
    Review,
    RunStatus,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.models import TestCase as DomainTestCase
from app_review_insights.pipeline.orchestrator import (
    AnalysisOrchestrator,
    PipelineServices,
)
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.storage.repository import RunRepository


class FailOnceOnSecondBatch:
    def __init__(self):
        self.calls = 0
        self.failed = False

    def __call__(self, reviews, goal):
        self.calls += 1
        if self.calls == 2 and not self.failed:
            self.failed = True
            raise RecoverableModelError("temporary failure")
        return BatchAnalysisResult(findings=[], batch_limitations=[])


class SummarizingFailOnceOnSecondBatch:
    def __init__(self):
        self.calls = 0

    def __call__(self, reviews, goal):
        self.calls += 1
        if self.calls == 2:
            raise RecoverableModelError("temporary failure")
        return BatchAnalysisResult(
            findings=[],
            review_summaries=[
                ReviewSummaryDraft(
                    review_id=item.review_id,
                    summary_zh=f"评论 {item.review_id} 的中文摘要",
                )
                for item in reviews
            ],
        )


def make_reviews(count: int) -> list[Review]:
    return [
        Review(
            review_id=f"r-{index}",
            app_id="app",
            content_original=f"distinct review {index}: {chr(0x4E00 + index) * 40}",
            rating=1,
            published_at=datetime.now(UTC),
            source="fixture",
        )
        for index in range(count)
    ]


def make_services(repo, analyzer, traceability_validator=None):
    return PipelineServices(
        repository=repo,
        batch_analyzer=analyzer,
        consolidator=lambda results, goal: ConsolidationResult(findings=[]),
        finding_validator=lambda drafts, reviews: (
            [],
            ValidationReport(valid=True),
        ),
        requirement_builder=lambda findings, goal, total: [],
        test_case_builder=lambda requirements: [],
        traceability_validator=(
            traceability_validator
            or (lambda review_ids, findings, requirements, cases: ValidationReport(valid=True))
        ),
        batch_size=2,
        batch_max_characters=10000,
    )


def test_orchestrator_resumes_same_run_without_repeating_completed_batch(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = FailOnceOnSecondBatch()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))
    request = AnalysisRequest(
        source_type=SourceType.JSON,
        analysis_goal="查找产品问题",
    )

    waiting = orchestrator.start(request, imported_reviews=make_reviews(4))

    assert waiting.status == RunStatus.WAITING
    assert waiting.current_stage == Stage.ANALYZE_BATCHES
    assert waiting.current_batch == 1
    assert repo.get_output(waiting.run_id, Stage.COLLECT) is not None
    assert repo.get_output(waiting.run_id, Stage.CLEAN) is not None
    assert (
        repo.get_output(
            waiting.run_id,
            Stage.ANALYZE_BATCHES,
            batch_index=0,
        )
        is not None
    )

    completed = orchestrator.resume(waiting.run_id)

    assert completed.run_id == waiting.run_id
    assert completed.status == RunStatus.COMPLETED
    assert completed.current_stage == Stage.COMPLETE
    assert analyzer.calls == 3
    assert len(repo.list_events(waiting.run_id)) >= 5


def test_orchestrator_persists_completed_batch_summaries_before_model_retry(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = SummarizingFailOnceOnSecondBatch()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))

    waiting = orchestrator.start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="查找产品问题",
        ),
        imported_reviews=make_reviews(4),
    )

    clean_output = repo.get_output(waiting.run_id, Stage.CLEAN)

    assert waiting.status == RunStatus.WAITING
    assert clean_output["stats"]["output_count"] == 4
    assert [item["content_summary_zh"] for item in clean_output["reviews"]] == [
        "评论 r-0 的中文摘要",
        "评论 r-1 的中文摘要",
        None,
        None,
    ]


def test_orchestrator_uses_persisted_batch_boundaries_after_config_change(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = FailOnceOnSecondBatch()
    original_services = make_services(repo, analyzer)
    waiting = AnalysisOrchestrator(original_services).start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="查找产品问题",
        ),
        imported_reviews=make_reviews(4),
    )

    resumed_services = make_services(repo, analyzer)
    resumed_services.batch_size = 100
    completed = AnalysisOrchestrator(resumed_services).resume(waiting.run_id)

    assert completed.status == RunStatus.COMPLETED
    assert analyzer.calls == 3


class FailOnceConsolidator:
    def __init__(self):
        self.calls = 0

    def __call__(self, results, goal):
        self.calls += 1
        if self.calls == 1:
            raise RecoverableModelError("temporary consolidation failure")
        return ConsolidationResult(findings=[])


def test_orchestrator_records_batch_completion_event_only_once_on_late_resume(
    tmp_path,
):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    services = make_services(repo, FailOnceOnSecondBatch())
    services.consolidator = FailOnceConsolidator()
    orchestrator = AnalysisOrchestrator(services)
    waiting = orchestrator.start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="查找产品问题",
        ),
        imported_reviews=make_reviews(2),
    )

    completed = orchestrator.resume(waiting.run_id)
    batch_completion_events = [
        event
        for event in repo.list_events(completed.run_id)
        if event.message == "All review batches analyzed"
    ]

    assert completed.status == RunStatus.COMPLETED
    assert [event.stage for event in batch_completion_events] == [Stage.ANALYZE_BATCHES]


def test_orchestrator_enters_batch_stage_when_there_are_no_reviews(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    result = AnalysisOrchestrator(make_services(repo, FailOnceOnSecondBatch())).start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="查找产品问题",
        ),
        imported_reviews=[],
    )

    batch_completion = next(
        event
        for event in repo.list_events(result.run_id)
        if event.message == "All review batches analyzed"
    )

    assert result.status == RunStatus.COMPLETED
    assert batch_completion.stage == Stage.ANALYZE_BATCHES


def test_orchestrator_persists_final_evidence_chain(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")

    def analyze(reviews, goal):
        return BatchAnalysisResult(
            findings=[
                FindingDraft(
                    title="Timer freezes",
                    problem_statement="Timer freezes after pause.",
                    topic_label="reliability",
                    supporting_review_ids=[reviews[0].review_id],
                    reasoning_summary="Direct review evidence.",
                )
            ]
        )

    def validate(drafts, reviews):
        finding = Finding(
            finding_id="F-001",
            title=drafts[0].title,
            problem_statement=drafts[0].problem_statement,
            topic_label=drafts[0].topic_label,
            supporting_review_ids=drafts[0].supporting_review_ids,
            support_count=1,
            confidence=0.6,
            evidence_status=EvidenceStatus.VALIDATED,
            model_reasoning_summary=drafts[0].reasoning_summary,
        )
        return [finding], ValidationReport(valid=True)

    def build_requirements(findings, goal, total):
        return [
            Requirement(
                requirement_id="REQ-001",
                finding_ids=["F-001"],
                title="Keep timer state",
                user_problem="Timer cannot resume.",
                objective="Make pause and resume reliable.",
                scope=["Persist timer state"],
                non_goals=[],
                functional_rules=["Resume from saved elapsed time"],
                edge_cases=["App enters background"],
                acceptance_criteria=["Timer resumes without freezing"],
                success_metrics=["Fewer timer complaints"],
                impact=5,
                complexity="medium",
                target_version="V1.0",
                source_review_ids=findings[0].supporting_review_ids,
            )
        ]

    def build_cases(requirements):
        return [
            DomainTestCase(
                test_case_id=f"TC-{index:03d}",
                requirement_id="REQ-001",
                title=title,
                preconditions=["Workout is running"],
                steps=["Pause workout", "Resume workout"],
                expected_result="Timer continues from saved time.",
                case_type=case_type,
                source_review_ids=requirements[0].source_review_ids,
            )
            for index, (title, case_type) in enumerate(
                [("Normal resume", "normal"), ("Background resume", "regression")],
                start=1,
            )
        ]

    services = PipelineServices(
        repository=repo,
        batch_analyzer=analyze,
        consolidator=lambda results, goal: ConsolidationResult(findings=results[0].findings),
        finding_validator=validate,
        requirement_builder=build_requirements,
        test_case_builder=build_cases,
        traceability_validator=validate_traceability,
        batch_size=10,
        batch_max_characters=10000,
    )

    completed = AnalysisOrchestrator(services).start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="提升训练可靠性",
        ),
        imported_reviews=make_reviews(2),
    )

    assert completed.status == RunStatus.COMPLETED
    assert len(repo.get_output(completed.run_id, Stage.VALIDATE_FINDINGS)["findings"]) == 1
    plan_output = repo.get_output(completed.run_id, Stage.PLAN)
    assert len(plan_output["requirements"]) == 1
    assert plan_output["quantity_notice"] == ("可验证证据不足，因此本次输出少于 5 个核心需求。")
    assert len(repo.get_output(completed.run_id, Stage.GENERATE_TESTS)["test_cases"]) == 2
    assert repo.get_output(completed.run_id, Stage.VALIDATE_TRACEABILITY)["valid"] is True
    assert any(
        event.stage == Stage.VALIDATE_TRACEABILITY
        and event.message == "Traceability validation completed"
        for event in repo.list_events(completed.run_id)
    )


def test_orchestrator_marks_invalid_traceability_as_partial(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = FailOnceOnSecondBatch()
    analyzer.failed = True
    services = make_services(
        repo,
        analyzer,
        traceability_validator=lambda review_ids, findings, requirements, cases: ValidationReport(
            valid=False
        ),
    )

    result = AnalysisOrchestrator(services).start(
        AnalysisRequest(
            source_type=SourceType.JSON,
            analysis_goal="查找产品问题",
        ),
        imported_reviews=make_reviews(2),
    )

    assert result.status == RunStatus.PARTIAL
    assert result.current_stage == Stage.VALIDATE_TRACEABILITY


class FailingCollector:
    def collect(self, app_url, limit):
        raise CollectionError("upstream unavailable")


def test_orchestrator_records_online_collection_failure(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    services = make_services(repo, FailOnceOnSecondBatch())
    services.collector = FailingCollector()

    result = AnalysisOrchestrator(services).start(
        AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="查找产品问题",
            app_url="https://apps.apple.com/us/app/example/id839285684",
        )
    )

    assert result.status == RunStatus.FAILED
    assert result.current_stage == Stage.COLLECT
    assert "upstream unavailable" in result.last_error
