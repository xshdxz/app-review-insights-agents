from datetime import UTC, datetime

import pytest

from app_review_insights.agent.human_in_loop import approve_run, reject_run
from app_review_insights.agent.orchestrator import AgentOrchestrator
from app_review_insights.agent.schemas import AgentPlan, ToolCall
from app_review_insights.models import (
    AgentRun,
    AgentRunStatus,
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.repository import RunRepository


class _FakePlanner:
    def __init__(self, plan=None):
        self._plan = plan or AgentPlan(
            rationale="r",
            tool_calls=[ToolCall(tool="run_analysis", arguments={"goal": "g", "app_url": "u"})],
        )

    def plan(self, goal, app_url, review_limit=200):
        # 模拟真实 default_plan：把 review_limit 并入 run_analysis 参数
        plan = self._plan.model_copy(deep=True)
        for call in plan.tool_calls:
            if call.tool == "run_analysis":
                call.arguments = {**call.arguments, "review_limit": review_limit}
        return plan


class _FakeRegistry:
    def __init__(self, run_ids):
        self.run_ids = list(run_ids)
        self.calls = []

    def invoke(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"run_id": self.run_ids.pop(0) if self.run_ids else "run-x", "status": "completed"}


class _FakeReviewer:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    def review(self, goal, analysis_run_id):
        self.calls.append((goal, analysis_run_id))
        return self.verdicts.pop(0)


def _completed_run(run_id):
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="goal",
            app_url="https://apps.apple.com/us/app/x/id1",
        ),
        current_stage=Stage.COMPLETE,
        status=RunStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_agent_run_completes_on_first_approval(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    run_repo.save_run(_completed_run("run-1"))
    run_repo.save_output(
        "run-1",
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True).model_dump(mode="json"),
    )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1"]),
        reviewer=_FakeReviewer([_Verdict(True)]),
        agent_repository=agent_repo,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")
    assert isinstance(result, AgentRun)
    assert result.status == AgentRunStatus.COMPLETED
    assert result.analysis_run_id == "run-1"
    assert result.review_rounds == 1


def test_agent_run_retries_with_feedback_then_completes(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    for run_id in ("run-1", "run-2"):
        run_repo.save_run(_completed_run(run_id))
        run_repo.save_output(
            run_id,
            Stage.VALIDATE_TRACEABILITY,
            ValidationReport(valid=True).model_dump(mode="json"),
        )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1", "run-2"]),
        reviewer=_FakeReviewer([_Verdict(False, "覆盖不足"), _Verdict(True)]),
        agent_repository=agent_repo,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")
    assert result.status == AgentRunStatus.COMPLETED
    assert result.review_rounds == 2
    assert "覆盖不足" in result.feedback
    # 第二次分析的 goal 应带上反馈
    assert "覆盖不足" in orchestrator.registry.calls[1][1]["goal"]


def test_agent_run_fails_after_max_rounds(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    run_repo.save_run(_completed_run("run-1"))
    run_repo.save_output(
        "run-1",
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True).model_dump(mode="json"),
    )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1"]),
        reviewer=_FakeReviewer([_Verdict(False, "不行")]),
        agent_repository=agent_repo,
        max_review_rounds=1,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")
    assert result.status == AgentRunStatus.FAILED
    assert result.error


def test_agent_run_waits_for_approval(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    run_repo.save_run(_completed_run("run-1"))
    run_repo.save_output(
        "run-1",
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True).model_dump(mode="json"),
    )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1"]),
        reviewer=_FakeReviewer([_Verdict(True)]),
        agent_repository=agent_repo,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1", require_approval=True)
    assert result.status == AgentRunStatus.WAITING_APPROVAL


class _Verdict:
    def __init__(self, approved, feedback=""):
        self.approved = approved
        self.feedback = feedback


def test_approve_run_changes_status(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    agent_repo.save_agent_run(
        AgentRun(
            run_id="a1",
            goal="g",
            app_url="https://apps.apple.com/us/app/x/id1",
            status=AgentRunStatus.WAITING_APPROVAL,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    approved = approve_run(agent_repo, "a1")
    assert approved.status == AgentRunStatus.COMPLETED


def test_reject_run_records_reason(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    agent_repo.save_agent_run(
        AgentRun(
            run_id="a2",
            goal="g",
            app_url="https://apps.apple.com/us/app/x/id1",
            status=AgentRunStatus.WAITING_APPROVAL,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    rejected = reject_run(agent_repo, "a2", "报告不完整")
    assert rejected.status == AgentRunStatus.FAILED
    assert "报告不完整" in rejected.error


def test_agent_run_fails_after_two_disapprovals_with_max_two(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    for run_id in ("run-1", "run-2"):
        run_repo.save_run(_completed_run(run_id))
        run_repo.save_output(
            run_id,
            Stage.VALIDATE_TRACEABILITY,
            ValidationReport(valid=True).model_dump(mode="json"),
        )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1", "run-2"]),
        reviewer=_FakeReviewer([_Verdict(False, "a"), _Verdict(False, "b")]),
        agent_repository=agent_repo,
        max_review_rounds=2,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")
    assert result.status == AgentRunStatus.FAILED
    assert result.review_rounds == 2
    assert "最大轮数" in result.error


def test_agent_run_redo_goal_passed_to_reviewer(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    for run_id in ("run-1", "run-2"):
        run_repo.save_run(_completed_run(run_id))
        run_repo.save_output(
            run_id,
            Stage.VALIDATE_TRACEABILITY,
            ValidationReport(valid=True).model_dump(mode="json"),
        )
    reviewer = _FakeReviewer([_Verdict(False, "覆盖不足"), _Verdict(True)])
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1", "run-2"]),
        reviewer=reviewer,
        agent_repository=agent_repo,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")
    assert result.status == AgentRunStatus.COMPLETED
    # 第二轮 review 的 goal 必须带反馈（不能还是原始 goal）
    assert "覆盖不足" in reviewer.calls[1][0]


def test_agent_run_forwards_review_limit(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    for run_id in ("run-1", "run-2"):
        run_repo.save_run(_completed_run(run_id))
        run_repo.save_output(
            run_id,
            Stage.VALIDATE_TRACEABILITY,
            ValidationReport(valid=True).model_dump(mode="json"),
        )
    registry = _FakeRegistry(["run-1", "run-2"])
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=registry,
        reviewer=_FakeReviewer([_Verdict(False, "覆盖不足"), _Verdict(True)]),
        agent_repository=agent_repo,
    )
    result = orchestrator.run("g", "https://apps.apple.com/us/app/x/id1", review_limit=777)
    assert result.status == AgentRunStatus.COMPLETED
    # 计划路径与 redo 路径都要携带 review_limit
    assert registry.calls[0][1].get("review_limit") == 777
    assert registry.calls[1][1].get("review_limit") == 777


def test_reject_run_wrong_state_raises(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    agent_repo.save_agent_run(
        AgentRun(
            run_id="a3",
            goal="g",
            app_url="https://apps.apple.com/us/app/x/id1",
            status=AgentRunStatus.COMPLETED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    with pytest.raises(ValueError):
        reject_run(agent_repo, "a3", "不该拒绝已完成运行")


def test_reject_run_empty_reason(tmp_path):
    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    agent_repo.save_agent_run(
        AgentRun(
            run_id="a4",
            goal="g",
            app_url="https://apps.apple.com/us/app/x/id1",
            status=AgentRunStatus.WAITING_APPROVAL,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    rejected = reject_run(agent_repo, "a4", "")
    assert rejected.status == AgentRunStatus.FAILED
    assert rejected.error == "审批驳回"


def test_agent_run_emits_structured_events(tmp_path):
    """验证编排器在运行过程中写入 agent_events.jsonl（结构化推理日志）。"""
    from app_review_insights.agent.orchestrator import _event_path

    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    run_repo = RunRepository(tmp_path / "runs.sqlite3")
    run_repo.save_run(_completed_run("run-1"))
    run_repo.save_output(
        "run-1",
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True).model_dump(mode="json"),
    )
    orchestrator = AgentOrchestrator(
        planner=_FakePlanner(),
        registry=_FakeRegistry(["run-1"]),
        reviewer=_FakeReviewer([_Verdict(True)]),
        agent_repository=agent_repo,
    )
    orchestrator.run("g", "https://apps.apple.com/us/app/x/id1")

    log_path = _event_path(agent_repo)
    assert log_path.exists(), "agent_events.jsonl 应在运行后创建"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 3, f"至少应有 plan + tool_call + finalize 三条事件，实际 {len(lines)} 条"
    import json
    for line in lines:
        event = json.loads(line)
        assert "run_id" in event
        assert "step" in event
        assert event["step"] in ("plan", "tool_call", "review", "redo", "finalize", "error")
    # 第一条应该是 plan，最后一条应该是 finalize（approved）或 review
    assert json.loads(lines[0])["step"] == "plan"
    assert json.loads(lines[-1])["step"] in ("finalize", "review")
