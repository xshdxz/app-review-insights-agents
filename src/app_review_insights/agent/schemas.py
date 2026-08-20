from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentPlan(BaseModel):
    rationale: str = ""
    tool_calls: list[ToolCall]


class ReviewVerdict(BaseModel):
    approved: bool
    feedback: str = ""
