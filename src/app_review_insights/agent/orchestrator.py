"""Agent 编排器：规划 → 执行工具 → Reviewer 复核 →（重做循环）→ 定稿。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app_review_insights.agent.reviewer import Reviewer
from app_review_insights.agent.tools import ToolRegistry
from app_review_insights.models import AgentRun, AgentRunStatus
from app_review_insights.storage.agent_repository import AgentRepository


class AgentOrchestrator:
    def __init__(
        self,
        planner: Any,
        registry: ToolRegistry,
        reviewer: Reviewer,
        agent_repository: AgentRepository,
        max_review_rounds: int = 2,
    ):
        self.planner = planner
        self.registry = registry
        self.reviewer = reviewer
        self.agent_repository = agent_repository
        self.max_review_rounds = max(1, max_review_rounds)

    def run(
        self,
        goal: str,
        app_url: str,
        require_approval: bool = False,
    ) -> AgentRun:
        now = datetime.now(UTC)
        agent_run = AgentRun(
            run_id=str(uuid4()),
            goal=goal,
            app_url=app_url,
            status=AgentRunStatus.RUNNING,
            require_approval=require_approval,
            created_at=now,
            updated_at=now,
        )
        self.agent_repository.save_agent_run(agent_run)

        try:
            plan = self.planner.plan(goal, app_url)
            agent_run = self._update(
                agent_run,
                plan_summary=plan.rationale,
            )

            analysis_run_id: str | None = None
            for call in plan.tool_calls:
                result = self.registry.invoke(call.tool, **call.arguments)
                if call.tool == "run_analysis" and isinstance(result, dict):
                    analysis_run_id = result.get("run_id") or analysis_run_id
            if analysis_run_id is None:
                raise RuntimeError("计划执行后未获得分析运行 ID")

            agent_run = self._update(
                agent_run,
                analysis_run_id=analysis_run_id,
            )

            current_goal = goal
            for round_index in range(self.max_review_rounds):
                verdict = self.reviewer.review(current_goal, analysis_run_id)
                agent_run = self._update(
                    agent_run,
                    review_rounds=round_index + 1,
                    feedback=(
                        [*agent_run.feedback, verdict.feedback]
                        if verdict.feedback
                        else agent_run.feedback
                    ),
                )
                if verdict.approved:
                    agent_run = self._update(
                        agent_run,
                        status=(
                            AgentRunStatus.WAITING_APPROVAL
                            if require_approval
                            else AgentRunStatus.COMPLETED
                        ),
                    )
                    return agent_run
                if round_index + 1 >= self.max_review_rounds:
                    agent_run = self._update(
                        agent_run,
                        status=AgentRunStatus.FAILED,
                        error=f"复核未通过（已达最大轮数）：{verdict.feedback}",
                    )
                    return agent_run
                # 重做：把反馈并入目标，重新分析（新 run_id）
                current_goal = f"{current_goal}\n评审反馈：{verdict.feedback}"
                redo = self.registry.invoke(
                    "run_analysis",
                    app_url=app_url,
                    goal=current_goal,
                )
                analysis_run_id = redo.get("run_id") or analysis_run_id
                agent_run = self._update(agent_run, analysis_run_id=analysis_run_id)

            agent_run = self._update(
                agent_run,
                status=AgentRunStatus.FAILED,
                error="复核循环意外结束",
            )
            return agent_run
        except Exception as exc:  # noqa: BLE001 - 编排器兜底
            agent_run = self._update(
                agent_run,
                status=AgentRunStatus.FAILED,
                error=str(exc)[:500],
            )
            return agent_run

    def _update(self, agent_run: AgentRun, **updates: Any) -> AgentRun:
        updates["updated_at"] = datetime.now(UTC)
        updated = agent_run.model_copy(update=updates)
        self.agent_repository.save_agent_run(updated)
        return updated
