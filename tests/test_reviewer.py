from datetime import UTC, datetime

import pytest

from app_review_insights.agent.reviewer import Reviewer
from app_review_insights.errors import RecoverableModelError
from app_review_insights.models import (
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.storage.repository import RunRepository


@pytest.fixture
def repo(tmp_path):
    return RunRepository(tmp_path / "runs.sqlite3")


def _run(run_id, status=RunStatus.COMPLETED):
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="分析订阅转化",
            app_url="https://apps.apple.com/us/app/x/id1",
        ),
        current_stage=Stage.COMPLETE if status == RunStatus.COMPLETED else Stage.CONSOLIDATE,
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _FakeProvider:
    def __init__(self, verdict=None, error=False):
        self.verdict = verdict
        self.error = error

    def generate(self, system_prompt, user_prompt, schema):
        if self.error:
            raise RecoverableModelError("boom")
        if self.verdict is not None:
            return schema.model_validate(self.verdict)
        return schema(approved=True, feedback="")


def _save_valid_traceability(repo, run_id):
    repo.save_output(
        run_id,
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=True, issues=[]).model_dump(mode="json"),
    )


def test_reviewer_rejects_incomplete_run(repo):
    repo.save_run(_run("r1", status=RunStatus.WAITING))
    reviewer = Reviewer(provider=None, repository=repo)
    verdict = reviewer.review("goal", "r1")
    assert not verdict.approved
    assert "未完成" in verdict.feedback


def test_reviewer_rejects_invalid_traceability(repo):
    repo.save_run(_run("r2"))
    repo.save_output(
        "r2",
        Stage.VALIDATE_TRACEABILITY,
        ValidationReport(valid=False, issues=[]).model_dump(mode="json"),
    )
    reviewer = Reviewer(provider=None, repository=repo)
    assert not reviewer.review("goal", "r2").approved


def test_reviewer_approves_completed_valid_run(repo):
    repo.save_run(_run("r3"))
    _save_valid_traceability(repo, "r3")
    reviewer = Reviewer(provider=None, repository=repo)
    verdict = reviewer.review("goal", "r3")
    assert verdict.approved


def test_reviewer_llm_spot_check_feedback(repo):
    repo.save_run(_run("r4"))
    _save_valid_traceability(repo, "r4")
    provider = _FakeProvider(verdict={"approved": False, "feedback": "目标覆盖不足"})
    reviewer = Reviewer(provider=provider, repository=repo)
    verdict = reviewer.review("分析订阅转化", "r4")
    assert not verdict.approved
    assert verdict.feedback == "目标覆盖不足"


def test_reviewer_llm_failure_degrades_to_deterministic(repo):
    repo.save_run(_run("r5"))
    _save_valid_traceability(repo, "r5")
    reviewer = Reviewer(provider=_FakeProvider(error=True), repository=repo)
    assert reviewer.review("goal", "r5").approved


def test_reviewer_rejects_malformed_traceability(repo):
    repo.save_run(_run("r6"))
    repo.save_output("r6", Stage.VALIDATE_TRACEABILITY, {"nonsense": 1})
    reviewer = Reviewer(provider=None, repository=repo)
    verdict = reviewer.review("goal", "r6")
    assert not verdict.approved
    assert "格式异常" in verdict.feedback
