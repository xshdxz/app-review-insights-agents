"""运行队列：把「提交」与「执行」拆成两半。

今天两者粘在 `start()` 里——提交的同时就在**调用者的线程**里把整条流水线跑完，于是
浏览器标签页一关、会话结束、运行就死掉（只能事后用同一 `run_id` 补救续跑）。
拆开之后"谁提交"与"谁执行"才可能分离；但 `start()` 的外部行为必须逐字不变，
所以这里同时钉住两半各自的诚实。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app_review_insights.llm.schemas import BatchAnalysisResult, ConsolidationResult
from app_review_insights.models import (
    AnalysisRequest,
    Review,
    RunStatus,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.pipeline.orchestrator import (
    AnalysisOrchestrator,
    PipelineServices,
)
from app_review_insights.storage import lease
from app_review_insights.storage.repository import RunRepository


class CountingAnalyzer:
    """记录批次分析被调用了几次：提交阶段一次都不该被调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, reviews, goal):
        self.calls += 1
        return BatchAnalysisResult(findings=[], batch_limitations=[])


def make_reviews(count: int) -> list[Review]:
    return [
        Review(
            review_id=f"r{index}",
            app_id="demo",
            content_original=f"distinct review {index}: {chr(0x4E00 + index) * 40}",
            rating=1,
            published_at=datetime.now(UTC),
            source="fixture",
        )
        for index in range(count)
    ]


def make_services(repo: RunRepository, analyzer) -> PipelineServices:
    return PipelineServices(
        repository=repo,
        batch_analyzer=analyzer,
        consolidator=lambda results, goal: ConsolidationResult(findings=[]),
        finding_validator=lambda drafts, reviews: ([], ValidationReport(valid=True)),
        requirement_builder=lambda findings, goal, total: [],
        test_case_builder=lambda requirements: [],
        traceability_validator=(
            lambda review_ids, findings, requirements, cases: ValidationReport(valid=True)
        ),
        batch_size=2,
        batch_max_characters=10_000,
    )


def make_request() -> AnalysisRequest:
    return AnalysisRequest(source_type=SourceType.JSON, analysis_goal="查找产品问题")


def test_enqueue_persists_a_pending_run_and_runs_nothing(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = CountingAnalyzer()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))

    queued = orchestrator.enqueue(make_request())

    assert queued.status == RunStatus.PENDING
    assert repo.get_run(queued.run_id).status == RunStatus.PENDING
    assert analyzer.calls == 0, "提交阶段绝不能碰模型"
    assert repo.get_output(queued.run_id, Stage.COLLECT) is None, "提交阶段不能落任何阶段输出"
    assert [event.message for event in repo.list_events(queued.run_id)] == ["Analysis run created"]


def test_queued_run_carries_no_lease_so_an_executor_can_claim_it(tmp_path):
    """排队中的运行不属于任何执行者。

    带租约的话会被 `is_held` 判成"仍有人在写"，于是**谁也认领不了**——队列直接死掉。
    """
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))

    queued = orchestrator.enqueue(make_request())

    assert queued.lease_owner is None
    assert repo.can_resume(queued, timeout_seconds=300.0) is True


def test_execute_runs_an_enqueued_run_to_completion(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = CountingAnalyzer()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))
    reviews = make_reviews(4)

    queued = orchestrator.enqueue(make_request())
    completed = orchestrator.execute(queued.run_id, imported_reviews=reviews)

    assert completed.status == RunStatus.COMPLETED
    assert repo.get_output(queued.run_id, Stage.COLLECT) is not None
    assert analyzer.calls > 0


def test_execute_refuses_a_run_another_live_process_is_holding(tmp_path):
    """两个执行者写同一份检查点会把结果搅坏——这是安全底线，宁可不干也不能重复干。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = CountingAnalyzer()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))
    reviews = make_reviews(4)

    queued = orchestrator.enqueue(make_request())
    # 模拟"另一个活着的进程"持有它：用本进程的租约身份，存活判定必然为真
    held = repo.get_run(queued.run_id).model_copy(update={"lease_owner": lease.make_owner()})
    repo.save_run(held)

    unchanged = orchestrator.execute(queued.run_id, imported_reviews=reviews)

    assert unchanged.status == RunStatus.PENDING
    assert analyzer.calls == 0, "持有者还活着时绝不能开工"


def test_start_still_enqueues_and_executes_in_one_call(tmp_path):
    """外部行为逐字不变：`start()` 仍然是「提交 + 立刻在本进程执行」。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = CountingAnalyzer()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))

    run = orchestrator.start(make_request(), imported_reviews=make_reviews(4))

    assert run.status == RunStatus.COMPLETED
    assert len(repo.list_runs()) == 1
    assert [event.message for event in repo.list_events(run.run_id)][0] == "Analysis run created"
