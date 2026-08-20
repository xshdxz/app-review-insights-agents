from datetime import UTC, datetime

import pytest

from app_review_insights.models import (
    AgentRun,
    AgentRunStatus,
    AnalysisRequest,
    MonitorReport,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.monitor.report import build_report, summarize_changes
from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.repository import RunRepository


@pytest.fixture
def run_repo(tmp_path):
    return RunRepository(tmp_path / "runs.sqlite3")


@pytest.fixture
def agent_repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


def _run(run_id):
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="分析订阅转化",
            app_url="https://apps.apple.com/us/app/x/id1",
        ),
        current_stage=Stage.COMPLETE,
        status=RunStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _agent_run(run_id):
    return AgentRun(
        run_id="agent-1",
        goal="分析订阅转化",
        app_url="https://apps.apple.com/us/app/x/id1",
        status=AgentRunStatus.COMPLETED,
        analysis_run_id=run_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _seed_run_outputs(run_repo, run_id):
    run_repo.save_run(_run(run_id))
    run_repo.save_output(
        run_id,
        Stage.VALIDATE_FINDINGS,
        {
            "findings": [
                {
                    "finding_id": "f1",
                    "title": "订阅价格敏感",
                    "problem_statement": "用户认为价格偏高",
                    "topic_label": "价格",
                    "supporting_review_ids": ["v1"],
                    "support_count": 1,
                    "confidence": 0.8,
                    "evidence_status": "validated",
                    "model_reasoning_summary": "多条评论提及",
                }
            ],
            "report": ValidationReport(valid=True).model_dump(mode="json"),
        },
    )
    run_repo.save_output(
        run_id,
        Stage.PLAN,
        {
            "requirements": [
                {
                    "requirement_id": "req1",
                    "finding_ids": ["f1"],
                    "title": "增加年度订阅折扣",
                    "user_problem": "价格敏感",
                    "objective": "提升转化",
                    "scope": [],
                    "non_goals": [],
                    "functional_rules": [],
                    "edge_cases": [],
                    "acceptance_criteria": [],
                    "success_metrics": [],
                    "impact": 3,
                    "complexity": "medium",
                    "priority_score": 12.0,
                    "target_version": "V1.1",
                    "source_review_ids": ["v1"],
                }
            ],
            "quantity_notice": None,
        },
    )
    run_repo.save_output(
        run_id,
        Stage.GENERATE_TESTS,
        {"test_cases": []},
    )
    run_repo.save_output(
        run_id,
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True).model_dump(mode="json"),
    )


def test_build_report_contains_sections(run_repo, agent_repo):
    _seed_run_outputs(run_repo, "run-1")
    report = build_report(_agent_run("run-1"), run_repo, agent_repo)
    assert isinstance(report, MonitorReport)
    assert "订阅价格敏感" in report.markdown
    assert "增加年度订阅折扣" in report.markdown
    assert report.findings_count == 1
    assert "发现" in report.summary


def test_build_report_records_changes_first_time(run_repo, agent_repo):
    _seed_run_outputs(run_repo, "run-1")
    report = build_report(_agent_run("run-1"), run_repo, agent_repo)
    assert "首次" in " ".join(report.changes)


def test_build_report_diff_against_previous(run_repo, agent_repo):
    _seed_run_outputs(run_repo, "run-1")
    previous = MonitorReport(
        report_id="old",
        agent_run_id="a0",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="g",
        markdown="",
        summary="",
        findings_count=3,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    report = build_report(_agent_run("run-1"), run_repo, agent_repo, previous=previous)
    assert any("3" in change and "1" in change for change in report.changes)


def test_summarize_changes():
    previous = MonitorReport(
        report_id="old",
        agent_run_id="a0",
        app_url="u",
        goal="g",
        markdown="",
        summary="",
        findings_count=2,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    current = MonitorReport(
        report_id="new",
        agent_run_id="a1",
        app_url="u",
        goal="g",
        markdown="",
        summary="",
        findings_count=4,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    changes = summarize_changes(previous, current)
    assert any("发现数量" in change for change in changes)
