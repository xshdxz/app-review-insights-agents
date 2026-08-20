from pydantic import BaseModel

from app_review_insights.agent.planner import Planner, default_plan
from app_review_insights.agent.schemas import AgentPlan
from app_review_insights.agent.tools import Tool, ToolRegistry
from app_review_insights.errors import RecoverableModelError


class _Params(BaseModel):
    app_url: str
    goal: str


def _registry():
    return ToolRegistry(
        tools=[
            Tool(
                name="run_analysis",
                description="run",
                parameters=_Params,
                func=lambda app_url, goal: {"ok": True},
            ),
            Tool(
                name="send_report",
                description="send",
                parameters=_Params,
                func=lambda app_url, goal: {"ok": True},
            ),
        ]
    )


class _FakeProvider:
    def __init__(self, plan=None, error=False):
        self.plan = plan
        self.error = error
        self.calls = 0

    def generate(self, system_prompt, user_prompt, schema):
        self.calls += 1
        if self.error:
            raise RecoverableModelError("boom")
        if self.plan is not None:
            return schema.model_validate(self.plan)
        return schema(
            rationale="默认",
            tool_calls=[{"tool": "run_analysis", "arguments": {"app_url": "x", "goal": "g"}}],
        )


def test_default_plan_uses_run_analysis():
    plan = default_plan("https://apps.apple.com/us/app/x/id1", "分析订阅")
    assert plan.tool_calls[0].tool == "run_analysis"
    assert plan.tool_calls[0].arguments["goal"] == "分析订阅"


def test_planner_returns_model_plan():
    planner = Planner(_FakeProvider(), _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert isinstance(plan, AgentPlan)
    assert plan.tool_calls[0].tool == "run_analysis"


def test_planner_falls_back_on_model_error():
    planner = Planner(_FakeProvider(error=True), _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"
    assert plan.rationale  # 非空即回退说明


def test_planner_falls_back_on_unknown_tool():
    provider = _FakeProvider(
        plan={
            "rationale": "hack",
            "tool_calls": [{"tool": "not_a_tool", "arguments": {}}],
        }
    )
    planner = Planner(provider, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"


def test_planner_falls_back_on_empty_calls():
    provider = _FakeProvider(plan={"rationale": "empty", "tool_calls": []})
    planner = Planner(provider, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"


def test_planner_falls_back_on_none_provider():
    planner = Planner(None, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"


def test_planner_falls_back_on_missing_goal_arg():
    provider = _FakeProvider(
        plan={
            "rationale": "no goal",
            "tool_calls": [{"tool": "run_analysis", "arguments": {"app_url": "x"}}],
        }
    )
    planner = Planner(provider, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"


def test_planner_falls_back_on_duplicate_run_analysis():
    provider = _FakeProvider(
        plan={
            "rationale": "dup",
            "tool_calls": [
                {"tool": "run_analysis", "arguments": {"app_url": "x", "goal": "g"}},
                {"tool": "run_analysis", "arguments": {"app_url": "y", "goal": "g2"}},
            ],
        }
    )
    planner = Planner(provider, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"
    assert len(plan.tool_calls) == 1


def test_planner_falls_back_on_disallowed_tool():
    provider = _FakeProvider(
        plan={
            "rationale": "bad tool",
            "tool_calls": [
                {"tool": "run_analysis", "arguments": {"app_url": "x", "goal": "g"}},
                {"tool": "send_report", "arguments": {"report_id": "r1"}},
            ],
        }
    )
    planner = Planner(provider, _registry())
    plan = planner.plan("分析订阅", "https://apps.apple.com/us/app/x/id1")
    assert plan.tool_calls[0].tool == "run_analysis"
    assert len(plan.tool_calls) == 1
