"""运行队列：把「提交」与「执行」拆成两半。

今天两者粘在 `start()` 里——提交的同时就在**调用者的线程**里把整条流水线跑完，于是
浏览器标签页一关、会话结束、运行就死掉（只能事后用同一 `run_id` 补救续跑）。
拆开之后"谁提交"与"谁执行"才可能分离；但 `start()` 的外部行为必须逐字不变，
所以这里同时钉住两半各自的诚实。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from app_review_insights.monitor.health import HealthState
from app_review_insights.monitor.queue_executor import execute_once, start_queue_executor
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


# ── 队列内核：原子认领 ───────────────────────────────────────────────────────


def test_claim_next_run_takes_the_oldest_queued_run_first(tmp_path):
    """先到先得：队列按提交顺序出队，不是"谁抢到算谁的"。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    first = orchestrator.enqueue(make_request())
    second = orchestrator.enqueue(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="另一个目标")
    )

    claimed = repo.claim_next_run(owner="w:1:x", timeout_seconds=300.0)

    assert claimed is not None
    assert claimed.run_id == first.run_id
    assert claimed.lease_owner == "w:1:x", "认领后必须写上执行者身份"
    assert repo.get_run(second.run_id).status == RunStatus.PENDING


def test_claim_next_run_returns_none_on_an_empty_queue(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")

    assert repo.claim_next_run(owner="w:1:x", timeout_seconds=300.0) is None


def test_two_executors_never_claim_the_same_run(tmp_path):
    """真并发（两个连接、同一时刻）：同一条运行只能被一个执行者拿到。

    这是整个队列的安全底线——两个执行者写同一份检查点会把结果搅坏，
    比"没跑"严重得多。检查与写入必须在同一个立即事务里。
    """
    path = tmp_path / "runs.sqlite3"
    AnalysisOrchestrator(make_services(RunRepository(path), CountingAnalyzer())).enqueue(
        make_request()
    )

    barrier = threading.Barrier(2)

    def claim(owner: str):
        own_repo = RunRepository(path)
        barrier.wait(timeout=10)
        return own_repo.claim_next_run(owner=owner, timeout_seconds=300.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result() for future in [pool.submit(claim, "w:1:a"), pool.submit(claim, "w:1:b")]
        ]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1, f"同一条运行被多个执行者认领：{results}"


def test_claim_skips_a_queued_run_that_a_live_owner_holds(tmp_path):
    """保守优先：一条理论上"排队中"却带着活租约的记录，认领方必须绕开它。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    queued = orchestrator.enqueue(make_request())
    repo.save_run(
        repo.get_run(queued.run_id).model_copy(update={"lease_owner": lease.make_owner()})
    )

    assert repo.claim_next_run(owner="w:1:x", timeout_seconds=300.0) is None


# ── 取消：请求与生效是两件事 ─────────────────────────────────────────────────


class CancelOnFirstBatch:
    """在第一次批次分析时请求取消——模拟"跑到一半用户点了停止"。"""

    def __init__(self, repo: RunRepository, run_id: str) -> None:
        self.repo = repo
        self.run_id = run_id
        self.calls = 0

    def __call__(self, reviews, goal):
        self.calls += 1
        self.repo.request_cancel(self.run_id)
        return BatchAnalysisResult(findings=[], batch_limitations=[])


def test_cancel_takes_effect_at_the_next_stage_boundary(tmp_path):
    """取消是**协作式**的：请求只置标志，真正停下来发生在下一个阶段边界。

    而且取消**不是回滚**——已经跑完的阶段照常留在检查点上，续跑时不必重做。
    """
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    queued = orchestrator.enqueue(make_request())
    analyzer = CancelOnFirstBatch(repo, queued.run_id)
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))

    run = orchestrator.execute(queued.run_id, imported_reviews=make_reviews(4))

    assert run.status == RunStatus.CANCELLED
    assert analyzer.calls >= 1
    assert repo.get_output(queued.run_id, Stage.COLLECT) is not None
    assert repo.get_output(queued.run_id, Stage.ANALYZE_BATCHES, batch_index=0) is not None
    # 精确的取消点：批次分析整个跑完（同阶段内的批次不会半途而废），下一个阶段没开始
    assert repo.get_output(queued.run_id, Stage.CONSOLIDATE) is None
    assert any("cancelled" in event.message.lower() for event in repo.list_events(queued.run_id))


def test_cancel_before_execution_prevents_the_run_from_starting(tmp_path):
    """排队期间就被取消：执行者拿到它时应当直接了结，而不是"先跑起来再说"。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = CountingAnalyzer()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))
    queued = orchestrator.enqueue(make_request())

    accepted = orchestrator.cancel(queued.run_id)
    run = orchestrator.execute(queued.run_id, imported_reviews=make_reviews(4))

    assert accepted is True
    assert repo.cancel_requested(queued.run_id) is True
    assert run.status == RunStatus.CANCELLED
    assert analyzer.calls == 0, "已经请求取消的运行，一次模型调用都不该发出去"


def test_cancelled_run_is_still_resumable(tmp_path):
    """取消停在检查点上，与超时同构：已完成的工作不该因为"停过一次"而作废。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    queued = orchestrator.enqueue(make_request())
    orchestrator.cancel(queued.run_id)

    assert repo.can_resume(repo.get_run(queued.run_id), timeout_seconds=300.0) is True


def test_resume_clears_a_previous_cancel_request(tmp_path):
    """人明确要求继续时，旧的取消意图必须让路——否则续跑会立刻又把自己停掉。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    queued = orchestrator.enqueue(make_request())
    orchestrator.cancel(queued.run_id)

    resumed = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).resume(
        queued.run_id, imported_reviews=make_reviews(4)
    )

    assert resumed.status == RunStatus.COMPLETED
    assert repo.cancel_requested(queued.run_id) is False, "续跑必须清掉旧的取消意图"


# ── 输入持久化：执行者手里没有调用者的那批评论 ───────────────────────────────


def test_enqueue_persists_inputs_so_the_executor_needs_no_caller_state(tmp_path):
    """执行者可能是另一个进程、也可能在几小时后才接手，靠内存传评论是行不通的。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    reviews = make_reviews(4)
    queued = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
        make_request(), imported_reviews=reviews
    )

    # 换一个编排器（等价于另一个进程）执行，且**不传**任何评论
    executor = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    run = executor.execute(queued.run_id)

    assert run.status == RunStatus.COMPLETED
    collected = repo.get_output(queued.run_id, Stage.COLLECT)
    assert collected is not None
    assert len(collected["reviews"]) == len(reviews)


def test_pruning_a_run_also_removes_its_persisted_inputs(tmp_path):
    """不做这一步，`run_inputs` 就会变成一个只涨不跌的表。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    queued = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
        make_request(), imported_reviews=make_reviews(4)
    )
    AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).execute(queued.run_id)

    assert repo.prune_runs(older_than_days=0) == 1
    assert repo.get_inputs(queued.run_id) is None


# ── 执行者：认领 → 执行 → 记结果 ─────────────────────────────────────────────


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _services_factory(repo: RunRepository):
    return lambda run: make_services(repo, CountingAnalyzer())


def test_executor_completes_an_enqueued_run(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
        make_request(), imported_reviews=make_reviews(4)
    )

    executed = execute_once(repo, _services_factory(repo), lease_timeout_seconds=300.0)

    assert executed is not None
    assert executed.status == RunStatus.COMPLETED
    assert repo.get_output(executed.run_id, Stage.COLLECT) is not None


def test_executor_returns_none_when_the_queue_is_empty(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")

    assert execute_once(repo, _services_factory(repo), lease_timeout_seconds=300.0) is None


def test_executor_loop_picks_up_a_run_enqueued_after_it_started(tmp_path):
    """常驻循环：先空转一阵，之后新提交的运行不需要重启进程就能被接手。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    state = HealthState()
    stop_event = threading.Event()
    thread = start_queue_executor(
        repo,
        _services_factory(repo),
        state,
        stop_event,
        poll_seconds=0.05,
        lease_timeout_seconds=300.0,
    )
    try:
        time.sleep(0.3)  # 让它先在空队列上转几圈
        queued = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
            make_request(), imported_reviews=make_reviews(4)
        )

        assert _wait_for(lambda: repo.get_run(queued.run_id).status is RunStatus.COMPLETED), (
            "提交之后的运行没有被执行者接手"
        )
    finally:
        stop_event.set()
        thread.join(timeout=10)

    assert not thread.is_alive(), "执行者应当能干净地停下来"
    assert state.snapshot()["jobs_completed"] == 1


def test_executor_marks_a_run_failed_when_it_cannot_even_start(tmp_path):
    """认领了却起不来（装配报错等）：如实判失败。

    若退回 `PENDING`，这条必然失败的运行会被反复重试——毒丸：日志刷屏，
    每一轮都白占一个执行者。而且执行者本身不能因此死掉：后面的运行还得有人接。
    """
    repo = RunRepository(tmp_path / "runs.sqlite3")
    first = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
        make_request(), imported_reviews=make_reviews(4)
    )
    second = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="第二个目标"),
        imported_reviews=make_reviews(4),
    )
    state = HealthState()
    stop_event = threading.Event()
    broken = {"pending": True}

    def flaky_factory(run):
        if broken["pending"]:
            broken["pending"] = False
            raise RuntimeError("装配失败：假装没有可用的模型密钥")
        return make_services(repo, CountingAnalyzer())

    thread = start_queue_executor(
        repo,
        flaky_factory,
        state,
        stop_event,
        poll_seconds=0.05,
        lease_timeout_seconds=300.0,
    )
    try:
        assert _wait_for(lambda: repo.get_run(second.run_id).status is RunStatus.COMPLETED), (
            "一次装配失败不该让后面的运行没人接"
        )
    finally:
        stop_event.set()
        thread.join(timeout=10)

    assert repo.get_run(first.run_id).status is RunStatus.FAILED
    assert "装配失败" in (repo.get_run(first.run_id).last_error or "")
    assert state.snapshot()["jobs_failed"] == 1


# ── 回队与执行者心跳 ─────────────────────────────────────────────────────────


def test_requeue_puts_a_stopped_run_back_in_the_queue(tmp_path):
    """队列模式下的「继续」＝重新入队：清租约、清取消请求，然后等执行者接手。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    orchestrator = AnalysisOrchestrator(make_services(repo, CountingAnalyzer()))
    queued = orchestrator.enqueue(make_request())
    orchestrator.cancel(queued.run_id)

    requeued = repo.requeue(queued.run_id, timeout_seconds=300.0)

    assert requeued is not None
    assert requeued.status == RunStatus.PENDING
    assert requeued.lease_owner is None
    assert repo.cancel_requested(queued.run_id) is False
    # 入队之后必须真的能被认领，否则「继续」只是换了个地方卡住
    claimed = repo.claim_next_run(owner="w:1:x", timeout_seconds=300.0)
    assert claimed is not None
    assert claimed.run_id == queued.run_id


def test_requeue_refuses_while_a_live_executor_holds_the_run(tmp_path):
    """把一条仍被活执行者持有的运行重新入队，等于制造第二个执行者。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    queued = AnalysisOrchestrator(make_services(repo, CountingAnalyzer())).enqueue(make_request())
    repo.save_run(
        repo.get_run(queued.run_id).model_copy(update={"lease_owner": lease.make_owner()})
    )

    assert repo.requeue(queued.run_id, timeout_seconds=300.0) is None


def test_executor_heartbeat_answers_whether_anyone_consumes_the_queue(tmp_path):
    """queued 模式下没人消费时，界面唯一的诚实回答来自这个心跳。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    assert repo.executor_seen_within(30.0) is False

    repo.record_executor_heartbeat("w:1:x")

    assert repo.executor_seen_within(30.0) is True
    assert repo.executor_seen_within(0.0) is False, "心跳会过期——这正是界面据以报警的信号"


def test_executor_loop_reports_a_heartbeat(tmp_path):
    """常驻执行者必须让别人看得见它在，否则界面只能说「不知道有没有人在跑」。"""
    repo = RunRepository(tmp_path / "runs.sqlite3")
    stop_event = threading.Event()
    thread = start_queue_executor(
        repo,
        _services_factory(repo),
        HealthState(),
        stop_event,
        poll_seconds=0.05,
        lease_timeout_seconds=300.0,
        heartbeat_seconds=0.0,
    )
    try:
        assert _wait_for(lambda: repo.executor_seen_within(60.0), timeout=5)
    finally:
        stop_event.set()
        thread.join(timeout=10)
