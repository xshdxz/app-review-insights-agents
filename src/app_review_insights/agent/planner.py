from __future__ import annotations

from typing import Any

from app_review_insights.agent.prompts import PLANNER_SYSTEM_PROMPT, render_planner_user_prompt
from app_review_insights.agent.schemas import AgentPlan, ToolCall
from app_review_insights.agent.tools import ToolRegistry
from app_review_insights.errors import RecoverableModelError

_DEFAULT_TOOL = "run_analysis"


def default_plan(app_url: str, goal: str) -> AgentPlan:
    return AgentPlan(
        rationale="模型不可用或计划无效，回退到默认标准分析流程。",
        tool_calls=[
            ToolCall(
                tool=_DEFAULT_TOOL,
                arguments={"app_url": app_url, "goal": goal},
            )
        ],
    )


class Planner:
    """规划 Agent：LLM 生成工具调用计划，任何失败都回退默认计划。"""

    def __init__(self, provider: Any, registry: ToolRegistry):
        self.provider = provider
        self.registry = registry

    def plan(self, goal: str, app_url: str) -> AgentPlan:
        if self.provider is None:
            return default_plan(app_url, goal)
        try:
            plan = self.provider.generate(
                PLANNER_SYSTEM_PROMPT,
                render_planner_user_prompt(goal, app_url, self.registry.schemas()),
                AgentPlan,
            )
        except RecoverableModelError:
            return default_plan(app_url, goal)
        if not self._is_valid(plan):
            return default_plan(app_url, goal)
        return plan

    def _is_valid(self, plan: AgentPlan) -> bool:
        if not plan.tool_calls:
            return False
        known = set(self.registry.names())
        if not all(call.tool in known for call in plan.tool_calls):
            return False
        if not any(call.tool == _DEFAULT_TOOL for call in plan.tool_calls):
            return False
        for call in plan.tool_calls:
            if call.tool == _DEFAULT_TOOL:
                args = call.arguments
                if not args.get("app_url") or not args.get("goal"):
                    return False
        return True
