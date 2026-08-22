"""Agent 编排器：规划 → 执行工具 → Reviewer 复核 →（重做循环）→ 定稿。

运行过程中发射 AgentEvent（JSON Lines），路径见 settings.agent_db_path 的同目录。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app_review_insights.agent.reviewer import Reviewer
from app_review_insights.agent.schemas import AgentEvent
from app_review_insights.agent.tools import ToolRegistry
from app_review_insights.models import AgentRun, AgentRunStatus
from app_review_insights.storage.agent_repository import AgentRepository


def _event_path(agent_repository: AgentRepository) -> Path:
    return agent_repository.path.parent / "agent_events.jsonl"


def _log_event(path: Path, event: AgentEvent) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(event.model_dump_json() + "\n")


class AgentOrchestrator:
    def __init__(
        self,
        planner: Any,
        registry: ToolRegistry,
        reviewer: Reviewer,
        agent_repository: AgentRepository,
        max_review_rounds: int = 2,
        on_event: Any | None = None,
    ):
        self.planner = planner
        self.registry = registry
        self.reviewer = reviewer
        self.agent_repository = agent_repository
        self.max_review_rounds = max(1, max_review_rounds)
        self.on_event = on_event

    def _emit_event(self, log_path: Path, event: AgentEvent) -> None:
        """写 JSONL 文件 + 通知 on_event 回调（UI 实时可见性）。"""
        _log_event(log_path, event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - 回调不应阻断编排流程
                pass

    def run(
        self,
        goal: str,
        app_url: str,
        require_approval: bool = False,
        review_limit: int = 200,
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
        log_path = _event_path(self.agent_repository)

        try:
            # 1) 规划
            t0 = time.monotonic()
            plan = self.planner.plan(goal, app_url, review_limit)
            agent_run = self._update(
                agent_run,
                plan_summary=plan.rationale,
            )
            self._emit_event(log_path, AgentEvent(
                run_id=agent_run.run_id,
                step="plan",
                detail={"rationale": plan.rationale, "tools": [c.tool for c in plan.tool_calls]},
                timestamp=datetime.now(UTC),
                duration_s=round(time.monotonic() - t0, 3),
            ))

            # 2) 逐步执行工具
            analysis_run_id: str | None = None
            for call in plan.tool_calls:
                tc = time.monotonic()
                result = self.registry.invoke(call.tool, **call.arguments)
                result_summary = (
                    {k: str(v)[:100] for k, v in result.items()}
                    if isinstance(result, dict) else str(result)[:200]
                )
                self._emit_event(log_path, AgentEvent(
                    run_id=agent_run.run_id,
                    step="tool_call",
                    detail={
                        "tool": call.tool,
                        "arguments": {k: str(v)[:200] for k, v in call.arguments.items()},
                        "result_summary": result_summary,
                    },
                    timestamp=datetime.now(UTC),
                    duration_s=round(time.monotonic() - tc, 3),
                ))
                if call.tool == "run_analysis" and isinstance(result, dict):
                    analysis_run_id = result.get("run_id") or analysis_run_id
            if analysis_run_id is None:
                raise RuntimeError("计划执行后未获得分析运行 ID")

            agent_run = self._update(
                agent_run,
                analysis_run_id=analysis_run_id,
            )

            # 3) 复核循环
            current_goal = goal
            for round_index in range(self.max_review_rounds):
                tr = time.monotonic()
                verdict = self.reviewer.review(current_goal, analysis_run_id)
                self._emit_event(log_path, AgentEvent(
                    run_id=agent_run.run_id,
                    step="review",
                    detail={
                        "round": round_index + 1,
                        "approved": verdict.approved,
                        "feedback": verdict.feedback[:500],
                    },
                    timestamp=datetime.now(UTC),
                    duration_s=round(time.monotonic() - tr, 3),
                ))

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
                    self._emit_event(log_path, AgentEvent(
                        run_id=agent_run.run_id, step="finalize",
                        detail={"outcome": "approved"},
                        timestamp=datetime.now(UTC),
                    ))
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
                    self._emit_event(log_path, AgentEvent(
                        run_id=agent_run.run_id, step="finalize",
                        detail={"outcome": "failed", "reason": verdict.feedback[:200]},
                        timestamp=datetime.now(UTC),
                    ))
                    agent_run = self._update(
                        agent_run,
                        status=AgentRunStatus.FAILED,
                        error=f"复核未通过（已达最大轮数）：{verdict.feedback}",
                    )
                    return agent_run
                # 重做
                self._emit_event(log_path, AgentEvent(
                    run_id=agent_run.run_id, step="redo",
                    detail={"feedback": verdict.feedback[:300]},
                    timestamp=datetime.now(UTC),
                ))
                if verdict.feedback:
                    current_goal = f"{current_goal}\n评审反馈：{verdict.feedback}"
                redo = self.registry.invoke(
                    "run_analysis",
                    app_url=app_url,
                    goal=current_goal,
                    review_limit=review_limit,
                )
                if not isinstance(redo, dict) or not redo.get("run_id"):
                    raise RuntimeError("重做分析未获得运行 ID")
                analysis_run_id = redo["run_id"]
                agent_run = self._update(agent_run, analysis_run_id=analysis_run_id)

            agent_run = self._update(
                agent_run,
                status=AgentRunStatus.FAILED,
                error="复核循环意外结束",
            )
            return agent_run
        except Exception as exc:  # noqa: BLE001 - 编排器兜底
            self._emit_event(log_path, AgentEvent(
                run_id=agent_run.run_id, step="error",
                detail={"error": str(exc)[:500]},
                timestamp=datetime.now(UTC),
            ))
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
