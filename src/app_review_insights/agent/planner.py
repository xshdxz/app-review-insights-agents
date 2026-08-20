from __future__ import annotations

from typing import Any

from app_review_insights.agent.prompts import PLANNER_SYSTEM_PROMPT, render_planner_user_prompt
from app_review_insights.agent.schemas import AgentPlan, ToolCall
from app_review_insights.agent.tools import ToolRegistry

_DEFAULT_TOOL = "run_analysis"
# 与 PLANNER_SYSTEM_PROMPT 允许的工具集合保持一致；send_report 由调度/审批流程触发，不得入计划
_PLANNER_ALLOWED_TOOLS = frozenset({"run_analysis", "query_corpus", "get_latest_report"})


def default_plan(app_url: str, goal: str, review_limit: int = 200) -> AgentPlan:
    return AgentPlan(
        rationale="模型不可用或计划无效，回退到默认标准分析流程。",
        tool_calls=[
            ToolCall(
                tool=_DEFAULT_TOOL,
                arguments={
                    "app_url": app_url,
                    "goal": goal,
                    "review_limit": review_limit,
                },
            )
        ],
    )


class Planner:
    """规划 Agent：LLM 生成工具调用计划，任何失败都回退默认计划。"""

    def __init__(self, provider: Any, registry: ToolRegistry):
        self.provider = provider
        self.registry = registry

    def plan(self, goal: str, app_url: str, review_limit: int = 200) -> AgentPlan:
        if self.provider is None:
            return default_plan(app_url, goal, review_limit)
        try:
            plan = self.provider.generate(
                PLANNER_SYSTEM_PROMPT,
                render_planner_user_prompt(goal, app_url, self.registry.schemas()),
                AgentPlan,
            )
        except Exception:  # noqa: BLE001 - 规划失败一律回退默认计划（离线演示永不失效）
            return default_plan(app_url, goal, review_limit)
        if plan is None or not self._is_valid(plan):
            return default_plan(app_url, goal, review_limit)
        return plan

    def _is_valid(self, plan: AgentPlan) -> bool:
        if not plan.tool_calls:
            return False
        known = set(self.registry.names())
        if not all(call.tool in known for call in plan.tool_calls):
            return False
        if not all(call.tool in _PLANNER_ALLOWED_TOOLS for call in plan.tool_calls):
            return False
        run_analysis_calls = [call for call in plan.tool_calls if call.tool == _DEFAULT_TOOL]
        if len(run_analysis_calls) != 1:
            return False
        for call in run_analysis_calls:
            args = call.arguments
            if not args.get("app_url") or not args.get("goal"):
                return False
        return True
