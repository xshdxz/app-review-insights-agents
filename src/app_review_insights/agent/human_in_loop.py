"""人工审批闸门：报告推送前由用户在 UI 批准/驳回。"""

from __future__ import annotations

from datetime import UTC, datetime

from app_review_insights.models import AgentRun, AgentRunStatus
from app_review_insights.storage.agent_repository import AgentRepository


def approve_run(agent_repository: AgentRepository, run_id: str) -> AgentRun:
    run = agent_repository.get_agent_run(run_id)
    if run.status != AgentRunStatus.WAITING_APPROVAL:
        raise ValueError(f"运行 {run_id} 不在待审批状态")
    updated = run.model_copy(
        update={
            "status": AgentRunStatus.COMPLETED,
            "updated_at": datetime.now(UTC),
        }
    )
    agent_repository.save_agent_run(updated)
    return updated


def reject_run(
    agent_repository: AgentRepository,
    run_id: str,
    reason: str,
) -> AgentRun:
    run = agent_repository.get_agent_run(run_id)
    if run.status != AgentRunStatus.WAITING_APPROVAL:
        raise ValueError(f"运行 {run_id} 不在待审批状态")
    updated = run.model_copy(
        update={
            "status": AgentRunStatus.FAILED,
            "error": f"审批驳回：{reason}" if reason else "审批驳回",
            "updated_at": datetime.now(UTC),
        }
    )
    agent_repository.save_agent_run(updated)
    return updated
