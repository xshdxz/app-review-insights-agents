from __future__ import annotations

from datetime import datetime
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


class AgentEvent(BaseModel):
    """Agent 推理链的单步事件，用于结构化日志与实时可见性。"""

    run_id: str
    step: str  # "plan" | "tool_call" | "review" | "redo" | "finalize"
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime | None = None
    duration_s: float | None = None
